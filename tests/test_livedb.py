"""Live Project Integration against a REAL PostgreSQL server (embedded via `pgserver`, no internet needed).

These are real-database tests: DDL, inserts, transactions and catalog reads all run on PostgreSQL 16.
Repair proposals come from a TEST-ONLY Local Repair Module (tests/local_repair_testmodule.py) because these
tests must run without a Gemini key; the live Gemini variant runs only with GEMINI_API_KEY (bottom of file).
"""
import json
import os
import uuid

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient  # noqa: E402

from adaptix import dbrepair, livedb  # noqa: E402
from adaptix.api import State, create_app  # noqa: E402
from adaptix.config import Settings  # noqa: E402
from adaptix.credentials import CredentialRegistry  # noqa: E402
from adaptix.llm import NoProvider  # noqa: E402
from adaptix.storage import Storage  # noqa: E402

TOKEN = "livedb-test-token-0000000000000"


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    yield srv.get_uri()


@pytest.fixture
def env(pg_uri, tmp_path, monkeypatch):
    schema = "adaptix_t" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("ADAPTIX_LIVE_DB_SCHEMA", schema)
    monkeypatch.setenv("ADAPTIX_LOCAL_REPAIR_MODULE", "tests.local_repair_testmodule")
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"),
               creds=CredentialRegistry([], allow_simulated=False), llm=NoProvider(), sleep=lambda s: None,
               start_monitor=False)
    client = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    assert client.post("/api/livedb/setup", json={"reset": True, "confirm": True}).status_code == 200
    return client, st, livedb.target_from_env()


def experiment(client, engine="local", **args):
    return client.post("/api/livedb/experiment", json={"engine": engine, **args}).json()


def test_status_never_exposes_the_connection_url(env, pg_uri):
    client, st, t = env
    s = client.get("/api/livedb/status").json()
    assert s["configured"] and s["set_up"] and pg_uri not in json.dumps(s)
    conn = client.post("/api/livedb/test-connection").json()
    assert conn["ok"] and conn["target"]["schema"] == t.schema and pg_uri not in json.dumps(conn)
    assert any("SUPERUSER" in w for w in conn["warnings"])       # the embedded server runs as a superuser


def test_unsafe_schema_names_and_missing_url_are_refused(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@example.invalid/db")
    with pytest.raises(livedb.LiveDBError):
        livedb.target_from_env(schema="public")
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(livedb.LiveDBError) as e:
        livedb.target_from_env()
    assert e.value.category == "not_configured"
    with pytest.raises(livedb.LiveDBError):
        livedb._with_tls("postgresql://u:p@db.example.com/db?sslmode=disable")
    assert livedb._with_tls("postgresql://u:p@db.example.com/db").endswith("sslmode=require")


def test_table_browser_and_baseline(env):
    client, st, t = env
    tables = {x["table"]: x for x in client.get("/api/livedb/tables").json()}
    assert set(tables) == {"customers", "orders"} and tables["customers"]["rows"] == 12
    cols = {c["name"]: c for c in tables["orders"]["columns"]}
    assert cols["customer_id"]["comment"] == "Identifier of the customer placing the order"
    assert cols["note"]["nullable"] is True and cols["customer_id"]["nullable"] is False
    rows = client.get("/api/livedb/tables/orders", params={"limit": 3}).json()
    assert len(rows["rows"]) == 3 and all(r["request_key"].startswith("seed-") for r in rows["rows"])
    cust = client.get("/api/livedb/tables/customers", params={"limit": 2}).json()["rows"]
    assert all(r["full_name"].startswith("Synthetic Customer") for r in cust)          # synthetic data only
    sch = client.get("/api/livedb/schema").json()
    assert sch["vs_baseline"]["changed"] is False and sch["baseline_signature"] == sch["current_signature"]
    assert client.get("/api/livedb/monitor").json()["summary"] == "No schema drift detected"


def test_no_fault_succeeds_without_an_incident(env):
    client, st, t = env
    r = experiment(client)
    assert r["outcome"] == "succeeded_without_incident" and r["business"]["ok"]
    ev = client.get("/api/livedb/evidence").json()[0]
    assert ev["environment"] == "LIVE_DB_DEMO" and ev["expected"]["matches"] is True


def test_rename_is_repaired_validated_sandboxed_applied_and_verified(env):
    client, st, t = env
    f = client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "customer_id",
                                                "new_name": "client_id"}).json()
    assert f["ok"] and 'RENAME COLUMN "customer_id" TO "client_id"' in f["ddl"][0]
    sch = client.get("/api/livedb/schema").json()
    assert sch["current"]["client_id"]["renamed_from"] == "customer_id"          # proven by the catalog attnum
    r = experiment(client)
    assert r["outcome"] == "recovered" and r["tool_version"] == 2 and r["business"]["ok"]
    assert set(r["phases"]) >= {"execute", "diagnose", "propose", "validate", "sandbox_test", "apply", "retry_and_verify"}
    assert r["patch"] == [{"op": "map", "argument": "customer_id", "from": "body:customer_id", "to": "body:client_id",
                           "transform": "none"}]
    inc = client.get(f"/api/livedb/incidents/{r['incident']}").json()
    assert inc["environment"] == "LIVE_DB_DEMO" and inc["llm"]["engine"] == "local"
    assert any(s["stage"] == "validate" and "Sandbox test passed" in s["title"] for s in inc["timeline"])
    with livedb.connect(t) as c:                                               # the order really is stored
        n = c.execute(psycopg.sql.SQL("SELECT count(*) FROM {}.orders WHERE client_id='cust-0003' AND note=%s")
                      .format(psycopg.sql.Identifier(t.schema)), ("deliver after 5pm",)).fetchone()[0]
        sandbox_tables = c.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=%s",
                                   (t.sandbox,)).fetchone()[0]
    assert n == 1 and sandbox_tables == 0                                      # sandbox test left nothing behind
    assert experiment(client)["outcome"] == "succeeded_without_incident"        # the repaired tool keeps working


@pytest.mark.parametrize("fault,why", [
    ({"fault": "add_required_column", "new_name": "channel"}, "required field"),
    ({"fault": "remove_column", "column": "note"}, "silently dropped"),
    ({"fault": "change_type", "column": "note", "new_type": "jsonb"}, "not of type"),
])
def test_unsafe_changes_stop_without_writing(env, fault, why):
    client, st, t = env
    assert client.post("/api/livedb/inject", json=fault).json()["ok"]
    before = client.get("/api/livedb/tables").json()
    r = experiment(client)
    assert r["outcome"] == "stopped" and r["category"] == "schema_drift" and why in r["reason"]
    after = client.get("/api/livedb/tables").json()
    assert [x["rows"] for x in before] == [x["rows"] for x in after]           # nothing was inserted
    assert len(client.get("/api/livedb/versions").json()) == 1
    ev = client.get("/api/livedb/evidence").json()[0]
    assert ev["expected"]["outcome"] == "stopped" and ev["expected"]["matches"] is True


def test_coerced_type_change_is_not_a_failure(env):
    client, st, t = env
    assert client.post("/api/livedb/inject", json={"fault": "change_type", "column": "quantity",
                                                   "new_type": "text"}).json()["ok"]
    assert client.get("/api/livedb/monitor").json()["drift"] is True          # monitoring still reports it
    r = experiment(client)
    assert r["outcome"] == "succeeded_without_incident" and r["business"]["ok"]


def test_failed_verification_rolls_back_transaction_and_registry(env):
    client, st, t = env
    client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "customer_id", "new_name": "client_id"})
    with livedb.connect(t) as c:                                               # TEST-ONLY: silently discard inserts
        s = psycopg.sql.Identifier(t.schema)
        c.execute(psycopg.sql.SQL("CREATE FUNCTION {}.drop_it() RETURNS trigger LANGUAGE plpgsql AS "
                                  "$$ BEGIN RETURN NULL; END $$").format(s))
        c.execute(psycopg.sql.SQL("CREATE TRIGGER drop_it BEFORE INSERT ON {}.orders FOR EACH ROW EXECUTE FUNCTION "
                                  "{}.drop_it()").format(s, s))
        c.commit()
    r = experiment(client)
    assert r["outcome"] == "rolled_back" and r["rolled_back"] and r["business"]["ok"] is False
    assert [v["status"] for v in client.get("/api/livedb/versions").json()] == ["active", "rolled_back"]
    ev = client.get("/api/livedb/evidence").json()[0]
    assert ev["rollback"]["performed"] and ev["final"]["verified_recovery"] is False


def test_engine_selection_never_switches_or_fabricates(env, monkeypatch):
    client, st, t = env
    client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "customer_id", "new_name": "client_id"})
    r = experiment(client, engine="gemini")                                    # no key in this test
    assert r["outcome"] == "stopped" and "repair engine unavailable" in r["reason"]
    inc = client.get(f"/api/livedb/incidents/{r['incident']}").json()
    assert inc["llm"] is None and len(client.get("/api/livedb/versions").json()) == 1
    monkeypatch.setattr("tests.local_repair_testmodule.propose",
                        __import__("tests.local_repair_testmodule").local_repair_testmodule.propose_garbage)
    r = experiment(client, engine="local")
    inc = client.get(f"/api/livedb/incidents/{r['incident']}").json()
    assert r["outcome"] == "stopped" and inc["llm"]["error_category"] == "invalid_output"
    monkeypatch.delenv("ADAPTIX_LOCAL_REPAIR_MODULE")
    assert client.get("/api/livedb/status").json()["engines"]["local"]["available"] is False
    r = experiment(client, engine="local")
    assert r["outcome"] == "stopped" and "Local Repair Module unavailable" in r["reason"]


def test_reset_restores_baseline_and_clears_faults(env):
    client, st, t = env
    client.post("/api/livedb/inject", json={"fault": "remove_column", "column": "note"})
    assert client.get("/api/livedb/status").json()["faults_since_reset"] == 1
    assert client.post("/api/livedb/setup", json={"reset": True}).status_code == 422       # needs confirmation
    client.post("/api/livedb/setup", json={"reset": True, "confirm": True})
    assert client.get("/api/livedb/status").json()["faults_since_reset"] == 0
    assert client.get("/api/livedb/schema").json()["vs_baseline"]["changed"] is False
    bad = client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "no_such", "new_name": "x"}).json()
    assert bad["ok"] is False and bad["error"]                                 # failed DDL is reported, nothing changed
    r = client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "x; DROP TABLE y", "new_name": "z"})
    assert r.status_code == 422                                               # identifiers are validated


def test_scheduler_injects_within_interval_and_run_limit(env):
    client, st, t = env
    cfg = client.put("/api/livedb/schedule", json={"enabled": True, "interval_s": 60, "run_limit": 2,
                                                   "auto_recover": True, "engine": "local",
                                                   "faults": ["rename_column", "remove_column"]}).json()
    assert cfg["runs_done"] == 0
    sch = st.livedb_scheduler
    first = sch.tick(now=1_000.0)
    assert first["fault"]["fault"] == "rename_column" and first["experiment"]["outcome"] == "recovered"
    assert sch.tick(now=1_030.0) is None                                       # interval not reached
    second = sch.tick(now=1_061.0)
    assert second["fault"]["fault"] == "remove_column" and second["fault"]["trigger"] == "scheduled"
    assert sch.tick(now=2_000.0) is None and sch.settings()["runs_done"] == 2  # run limit reached
    client.put("/api/livedb/schedule", json={**cfg, "enabled": False})
    assert sch.due(now=9_999.0) is False


def test_records_are_isolated_and_exports_are_redacted(env, pg_uri):
    client, st, t = env
    client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "customer_id", "new_name": "client_id"})
    experiment(client)
    assert client.get("/api/incidents").json() == [] and client.get("/api/sim/incidents").json() == []
    changes = client.get("/api/changes", params={"environment": "LIVE_DB_DEMO"}).json()
    assert changes["total"] >= 1 and {r["environment_label"] for r in changes["rows"]} == {"LIVE DB DEMO"}
    blobs = [client.get("/api/livedb/evidence/export.json").text, client.get("/api/livedb/evidence/export.csv").text,
             client.get("/api/livedb/faults").text, client.get("/api/livedb/status").text]
    password = __import__("urllib.parse").parse.urlsplit(pg_uri).password
    for b in blobs:
        assert pg_uri not in b and (not password or password not in b)


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="live test: GEMINI_API_KEY not set")
def test_live_gemini_repairs_a_renamed_column(pg_uri, tmp_path, monkeypatch):
    """REAL Gemini proposes the repair against the real PostgreSQL catalog."""
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("ADAPTIX_LIVE_DB_SCHEMA", "adaptix_live" + uuid.uuid4().hex[:6])
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), start_monitor=False)
    client = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    client.post("/api/livedb/setup", json={"reset": True, "confirm": True})
    client.post("/api/livedb/inject", json={"fault": "rename_column", "column": "customer_id", "new_name": "client_id"})
    r = experiment(client, engine="gemini")
    ev = client.get("/api/livedb/evidence").json()[0]
    assert r["outcome"] == "recovered" and ev["llm"]["real_request_performed"] and r["business"]["ok"]


def test_dashboard_page_renders_every_tab_against_a_real_database(env, tmp_path, monkeypatch):
    from pathlib import Path

    import streamlit as st_
    from streamlit.testing.v1 import AppTest

    from adaptix.config import get_settings
    client, st, t = env
    for k, v in {"ADAPTIX_DB_PATH": "p", "ADAPTIX_SIM_DB_PATH": "s", "ADAPTIX_TEST_DB_PATH": "t",
                 "ADAPTIX_LIVEDB_DB_PATH": "l"}.items():
        monkeypatch.setenv(k, str(tmp_path / f"ui-{v}.db"))
    monkeypatch.setenv("ADAPTIX_MONITOR_ENABLED", "false")
    get_settings.cache_clear()
    st_.cache_resource.clear()
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=60)
    at.session_state["_force_page"] = "livedb"
    at.run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Set up demo schema").click().run()
    assert not at.exception, at.exception
    texts = [s.value for s in at.success] + [w.value for w in at.warning]
    assert any("No schema drift detected" in v or "Schema drift" in v for v in texts), texts
    get_settings.cache_clear()
    st_.cache_resource.clear()
