"""Live Project Integration against a REAL PostgreSQL 16 server (embedded via `pgserver`, no internet needed).

DDL, workflows, transactions and catalog reads all run on PostgreSQL. Repair proposals come from a TEST-ONLY
Local Repair Module (tests/local_repair_testmodule.py) - a unit-test stand-in; the live variant at the bottom
uses the real configured Gemini when GEMINI_API_KEY is set.
"""
import json
import os
import uuid
from decimal import Decimal

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
from database import build_init_sql, seed_data as sd  # noqa: E402

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
    assert client.post("/api/livedb/setup", json={}).status_code == 200
    return client, st, livedb.target_from_env()


def experiment(client, scenario, engine="local", confirm=True):
    return client.post("/api/livedb/experiment", json={"scenario": scenario, "engine": engine, "confirm": confirm})


def q(t, query, *args):
    with livedb.connect(t) as c:
        rows = c.execute(psycopg.sql.SQL(query).format(s=psycopg.sql.Identifier(t.schema)), args)
        out = rows.fetchall() if rows.description else []
        c.commit()
        return out


# ---------------------------------------------------------------- seed data
def test_init_sql_is_generated_from_the_seed_module():
    assert build_init_sql.OUT.read_text() == build_init_sql.build(), "run: python -m database.build_init_sql"


def test_seed_counts_foreign_keys_and_totals(env):
    client, st, t = env
    s = client.get("/api/livedb/seed-summary").json()
    assert s["counts"] == {"customers": 20, "products": 15, "orders": 40, "order_items": 79, "inventory_events": 30}
    assert s["checks"]["orders_with_consistent_totals"] == "40/40"
    assert q(t, "SELECT count(*) FROM {s}.order_items oi LEFT JOIN {s}.products p USING (product_id) "
                "WHERE p.product_id IS NULL")[0][0] == 0
    assert q(t, "SELECT count(*) FROM {s}.orders o LEFT JOIN {s}.customers c USING (customer_id) "
                "WHERE c.customer_id IS NULL")[0][0] == 0
    assert all(e.endswith("@example.com") for (e,) in q(t, "SELECT email FROM {s}.customers"))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):             # FKs are enforced
        q(t, "INSERT INTO {s}.orders VALUES ('O9999', 'C999', '2026-01-01', 'paid', 1, 'card')")
    with pytest.raises(psycopg.errors.CheckViolation):                  # CHECK constraints are enforced
        q(t, "UPDATE {s}.order_items SET line_total = line_total + 1 WHERE item_id = 1")
    want = sd.expected_order_total("O1012")                             # Python expectations == stored data
    assert q(t, "SELECT total_amount FROM {s}.orders WHERE order_id='O1012'")[0][0] == Decimal(want["stored_total"])


def test_setup_is_idempotent_and_reset_keeps_logs(env):
    client, st, t = env
    assert client.post("/api/livedb/setup", json={}).json()["business_tables_loaded"] is False
    experiment(client, "A")
    assert client.post("/api/livedb/setup", json={"reset": True}).status_code == 422          # needs confirmation
    assert client.post("/api/livedb/setup", json={"reset": True, "confirm": True}).status_code == 200
    assert client.get("/api/livedb/stats").json()["runs"] == 1                                # logs survive a reset
    assert client.get("/api/livedb/schema").json()["diff"]["changed"] is False


@pytest.mark.parametrize("wf", list(dbrepair.WORKFLOWS))
def test_baseline_workflows_verify_real_business_values(env, wf):
    client, st, t = env
    r = client.post(f"/api/livedb/workflows/{wf}/run").json()
    assert r["ok"] and r["committed"] is False and all(r["verification"]["checks"].values())


def test_verification_detects_wrong_business_values(env):
    """Genuine verification: a query that succeeds but returns wrong data is NOT a success."""
    client, st, t = env
    q(t, "UPDATE {s}.orders SET order_status='cancelled' WHERE order_id = "
         "(SELECT order_id FROM {s}.orders WHERE customer_id='C008' ORDER BY order_date LIMIT 1)")
    r = client.post("/api/livedb/workflows/customer_orders/run").json()
    assert r["ok"] is False and r["error"] is None and r["verification"]["ok"] is False


# ---------------------------------------------------------------- scenarios A-F
@pytest.mark.parametrize("scenario,status", [("A", "recovered"), ("B", "recovered"), ("C", "recovered"),
                                             ("D", "refused"), ("E", "refused"), ("F", "recovered")])
def test_each_fault_scenario(env, scenario, status):
    client, st, t = env
    r = experiment(client, scenario).json()
    stages = {s["stage"]: s["status"] for s in r["stages"]}
    assert stages["baseline_test"] == "passed" and stages["fault_injected"] == "applied"
    assert stages["failure_detected"] == "detected" and r["status"] == status, r["stages"]
    if status == "recovered":
        assert r["verified"] and r["recovery_ms"] > 0 and r["patch_diff"].startswith("- ")
        assert [stages[k] for k in ("validation", "sandbox_test", "patch_applied", "retry", "business_verification")] \
            == ["validated", "passed", "applied", "retried", "verified"]
    else:
        assert stages["validation"] == "refused" and "patch_applied" not in stages and not r["verified"]
    exp = client.get("/api/livedb/experiments").json()
    row = next(e for e in exp["experiments"] if e["experiment_id"] == r["experiment_id"])
    assert row["status"] == status and row["selected_engine"] == "local" and row["injected_fault"] == scenario
    events = [a["event_type"] for a in exp["audit_log"] if a["experiment_id"] == r["experiment_id"]]
    assert events[:3] == ["baseline_test", "fault_injected", "failure_detected"]


def test_scenario_c_cast_and_inventory_check_rolls_back(env):
    client, st, t = env
    r = experiment(client, "C").json()
    assert "line_total::numeric" in r["patch_diff"]
    client.post("/api/livedb/setup", json={"reset": True, "confirm": True})
    before = dict(q(t, "SELECT product_id, stock_quantity FROM {s}.products"))
    r = client.post("/api/livedb/workflows/inventory_update/run").json()
    assert r["ok"] and r["committed"] is False
    assert dict(q(t, "SELECT product_id, stock_quantity FROM {s}.products")) == before      # check run rolled back


def test_experiments_require_confirmation_and_a_baseline(env):
    client, st, t = env
    assert experiment(client, "A", confirm=False).status_code == 422
    assert experiment(client, "A").json()["status"] == "recovered"
    r = experiment(client, "B")                                                # not at baseline any more
    assert r.status_code == 422 and "reset" in r.json()["detail"]["detail"]


def test_failed_verification_rolls_back(env, monkeypatch):
    client, st, t = env
    real = dbrepair.verify
    calls = {"n": 0}

    def flaky(conn, schema, *a, **kw):                     # TEST-ONLY: the final check on the demo target fails
        v = real(conn, schema, *a, **kw)
        if schema == t.schema:
            calls["n"] += 1
            if calls["n"] >= 2:                            # 1st = baseline; the retry is the next verified target run
                v = {**v, "ok": False, "summary": "forced mismatch"}
        return v
    monkeypatch.setattr(dbrepair, "verify", flaky)
    r = experiment(client, "A").json()
    assert r["status"] == "rolled_back" and r["rollback_performed"] and not r["verified"], r["stages"]
    stages = {s["stage"]: s["status"] for s in r["stages"]}
    assert stages["business_verification"] == "failed" and stages["rollback"] == "rolled back"
    assert [x.status for x in st.livedb_storage.versions(dbrepair.tool_name("customer_orders"))] == ["active",
                                                                                                    "rolled_back"]


# ---------------------------------------------------------------- engines
def test_engine_selection_never_switches_or_fakes(env, monkeypatch):
    client, st, t = env
    s = client.get("/api/livedb/status").json()
    assert s["engines"]["gemini"]["status"] == "not configured" and s["engines"]["local"]["status"] == "configured"
    r = experiment(client, "A", engine="gemini").json()                       # no key: stop, do not switch
    assert r["status"] == "failed" and "engine unavailable" in r["error"]
    assert {x["stage"]: x["status"] for x in r["stages"]}["proposal"] == "failed"
    client.post("/api/livedb/setup", json={"reset": True, "confirm": True})
    monkeypatch.delenv("ADAPTIX_LOCAL_REPAIR_MODULE")
    s = client.get("/api/livedb/status").json()
    assert s["engines"]["local"]["status"] == "not configured" and "not configured" in s["engines"]["local"]["detail"]
    r = experiment(client, "A", engine="local").json()
    assert r["status"] == "failed" and "not configured" in r["error"]


def test_invalid_engine_output_is_rejected(env, monkeypatch):
    client, st, t = env
    import tests.local_repair_testmodule as mod
    monkeypatch.setattr(mod, "propose", mod.propose_garbage)
    r = experiment(client, "A").json()
    stages = {x["stage"]: x for x in r["stages"]}
    assert r["status"] == "refused" and stages["proposal"]["status"] == "failed"
    assert stages["validation"]["status"] == "refused"


def test_unsafe_proposals_are_refused(env, monkeypatch):
    client, st, t = env
    import tests.local_repair_testmodule as mod
    bad = {"diagnosis": "x", "evidence": [], "affected_fields": [], "response_mappings": [], "compatibility_risks": [],
           "confidence": 0.99, "verification_steps": [],
           "mappings": [{"target_field": "body:customers.full_name", "source_arg": "customers.customer_id",
                         "transform": "none", "evidence": "Stable identifier of the customer (C001..)"}]}
    monkeypatch.setattr(mod, "propose", lambda ctx: bad)
    r = experiment(client, "A").json()
    v = next(x for x in r["stages"] if x["stage"] == "validation")
    assert r["status"] == "refused" and "catalog identity" in v["title"]
    m = dbrepair.mapping(st.livedb_storage.current(dbrepair.tool_name("customer_orders")))
    assert m["customers.customer_id"][1] == "customer_id"                    # nothing applied


def test_engine_context_contains_no_row_data(env, monkeypatch):
    """The context sent to an engine has names, types, comments, the error and the template - never row values."""
    client, st, t = env
    seen = {}
    real = dbrepair.propose

    def spy(state, engine, context):
        seen["ctx"] = json.dumps(context)
        return real(state, engine, context)
    monkeypatch.setattr(dbrepair, "propose", spy)
    experiment(client, "B")
    blob = seen["ctx"]
    for c in sd.customers():
        assert c["full_name"] not in blob and c["email"] not in blob
    assert "C008" not in blob and "O10" not in blob


# ---------------------------------------------------------------- automatic injection
def test_schedule_is_off_by_default_needs_confirmation_and_stops(env):
    client, st, t = env
    assert client.get("/api/livedb/schedule").json()["enabled"] is False
    body = {"enabled": True, "interval_s": 60, "max_runs": 2, "engine": "local", "scenarios": ["A", "F"]}
    assert client.put("/api/livedb/schedule", json=body).status_code == 422          # needs confirmation
    assert client.put("/api/livedb/schedule", json={**body, "confirm": True}).json()["enabled"] is True
    sch = st.livedb_scheduler
    first = sch.tick(now=1_000.0)
    assert first["scenario"] == "A" and first["status"] == "recovered" and first["trigger"] == "scheduled"
    assert sch.tick(now=1_030.0) is None                                              # interval not reached
    second = sch.tick(now=1_061.0)
    assert second["scenario"] == "F" and second["status"] == "recovered"             # each run starts from baseline
    assert sch.settings()["enabled"] is False and sch.tick(now=9_999.0) is None      # max runs reached -> disabled
    client.put("/api/livedb/schedule", json={**body, "confirm": True})
    assert client.post("/api/livedb/stop").json()["schedule"]["enabled"] is False   # immediate stop
    assert st.livedb_stop.is_set()


def test_stop_interrupts_a_running_experiment(env):
    client, st, t = env
    st.livedb_stop.set()
    r = dbrepair.run_experiment(st, t, "A", "local")
    assert r["status"] == "blocked" and "stopped by the operator" in r["error"] and len(r["stages"]) == 1


# ---------------------------------------------------------------- statistics, export, redaction, separation
def test_stats_come_from_persisted_records(env):
    client, st, t = env
    for sc in ("A", "D"):
        client.post("/api/livedb/setup", json={"reset": True, "confirm": True})
        experiment(client, sc)
    s = client.get("/api/livedb/stats").json()
    assert s["runs"] == 2 and s["detected_faults"] == 2 and s["validated_patches"] == 1
    assert s["applied_patches"] == 1 and s["verified_recoveries"] == 1 and s["refused"] == 1
    assert s["recovery_samples"] == 1 and s["median_recovery_ms"] > 0


def test_exports_and_status_never_contain_credentials(env, pg_uri):
    client, st, t = env
    experiment(client, "A")
    blobs = [client.get(p).text for p in ("/api/livedb/status", "/api/livedb/export.json", "/api/livedb/export.csv",
                                          "/api/livedb/experiments", "/api/livedb/evidence/export.json")]
    blobs += [json.dumps(r, default=str) for r in q(t, "SELECT details_json, validation_result, verification_result "
                                                       "FROM {s}.adaptix_audit_log")]
    pw = __import__("urllib.parse").parse.urlsplit(pg_uri).password
    for b in blobs:
        assert pg_uri not in b and (not pw or pw not in b)
    assert client.get("/api/incidents").json() == [] and client.get("/api/sim/incidents").json() == []
    assert "DEMO EXPERIMENT" in client.get("/api/livedb/status").json()["label"]


def test_safety_rules(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.com/db")
    with pytest.raises(livedb.LiveDBError):
        livedb.target_from_env(schema="public")
    assert livedb._with_tls("postgresql://u:p@db.example.com/db").endswith("sslmode=require")
    with pytest.raises(livedb.LiveDBError):
        livedb._with_tls("postgresql://u:p@db.example.com/db?sslmode=disable")
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(livedb.LiveDBError) as e:
        livedb.target_from_env()
    assert e.value.category == "not_configured"


def test_no_free_form_sql_route(env):
    client, st, t = env
    paths = {r.path for r in client.app.routes if r.path.startswith("/api/livedb")}
    assert not any("sql" in p.lower() or "query" in p.lower() for p in paths)
    assert client.get("/api/livedb/tables/pg_authid").status_code == 422


def test_dashboard_page_renders_against_a_real_database(env, tmp_path, monkeypatch):
    from pathlib import Path

    import streamlit as st_
    from streamlit.testing.v1 import AppTest

    from adaptix.config import get_settings
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
    next(b for b in at.button if b.label == "Set up demo database").click().run()
    assert not at.exception, at.exception
    assert any("DEMO EXPERIMENT" in m.value for m in at.markdown)
    client, _, _ = env
    at.session_state["ldb_exp"] = experiment(client, "C").json()            # a real experiment's timeline renders
    at.run()
    assert not at.exception, at.exception
    assert any("PATCH APPLICATION" in m.value.upper() or "Patch application" in m.value for m in at.markdown)
    get_settings.cache_clear()
    st_.cache_resource.clear()


@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="live test: GEMINI_API_KEY not set")
@pytest.mark.parametrize("scenario,status", [("A", "recovered"), ("D", "refused"), ("F", "recovered")])
def test_live_gemini_experiments(pg_uri, tmp_path, monkeypatch, scenario, status):
    """REAL Gemini (no mock) proposes repairs against the real PostgreSQL catalog."""
    monkeypatch.setenv("DATABASE_URL", pg_uri)
    monkeypatch.setenv("ADAPTIX_LIVE_DB_SCHEMA", "adaptix_live" + uuid.uuid4().hex[:6])
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), start_monitor=False)
    client = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    client.post("/api/livedb/setup", json={})
    r = experiment(client, scenario, engine="gemini").json()
    prop = next(x for x in r["stages"] if x["stage"] == "proposal")
    assert prop["detail"]["engine"]["real"] is True and r["status"] == status, r["stages"]
