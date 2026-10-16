"""Real API monitoring (production mode) with the network replaced by tests/fakes.FakeGuard.

These are NOT live tests: the provider is an in-process app behind a fake transport. They check the
production logic: schema comparison, no-drift results, autonomous repair, validation, rollback, and that
auth / business-rule / network errors are never treated as schema drift. The live test is test_live_api.py.
"""
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ConfigDict, create_model

from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import Credential, CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.mock_api import make_app
from adaptix.storage import Storage
from tests.fakes import FakeGuard

TOKEN = "monitor-test-token-0000000000"
HOST = "api.example.test"
KEY = "crm-key-value-never-logged-123"
DOC = lambda desc, old=None, t="str": {"type": t, "description": desc, **({"renamed_from": old} if old else {})}


@pytest.fixture
def env(tmp_path):
    creds = CredentialRegistry([Credential("crm-a", "crm", "crm", "CRM_A", header="x-api-key")],
                               secrets={"crm-a": KEY}, allow_simulated=False)
    guard = FakeGuard()
    st = State(Settings(admin_token=TOKEN, allowed_hosts=(HOST,)), storage=Storage(tmp_path / "prod.db"),
               creds=creds, llm=NoProvider(), sleep=lambda s: None, guard=guard, start_monitor=False)
    return TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"}), guard, st


def register(client, path="/v1/items", method="GET", probe=None, **extra):
    body = {"name": "items", "base_url": f"https://{HOST}", "path": path, "method": method,
            "provider": "Example Provider", "probe_arguments": probe, **extra}
    r = client.post("/api/tools", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_no_drift_is_reported_as_such_and_nothing_changes(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, probe={"limit": 5})
    c = client.post("/api/monitor/items/check").json()
    assert c["schema_status"] == "verified" and c["connectivity"] == "reachable"
    assert c["drift"] is False and c["drift_summary"] == "No schema drift detected"
    assert c["verification"] == "verified" and c["repair"] is None
    assert c["provider"] == "Example Provider" and c["endpoint"] == f"GET https://{HOST}/v1/items"
    assert len(client.get("/api/tools/items/versions").json()) == 1
    assert client.get("/api/monitor/items/checks").json()[0]["ts"] == c["ts"]


def test_drift_without_probe_is_reported_but_not_acted_on(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client)
    guard.install(HOST, make_app("/v1/items", {"q:page_size": "int"}, method="GET"))
    calls_before = len(guard.calls)
    c = client.post("/api/monitor/items/check").json()
    assert c["drift"] is True and c["schema_status"] == "verified"
    assert c["drift_summary"] != "No schema drift detected" and c["report"]["request_changes"]
    assert c["verification"] == "not_run" and "No probe configured" in c["verification_detail"]
    assert all(m == "GET" and u.endswith("/openapi.json") for m, u in guard.calls[calls_before:])
    assert len(client.get("/api/tools/items/versions").json()) == 1
    assert client.get("/api/audit?kind=schema_drift_detected").json()


def test_confirmed_drift_is_repaired_autonomously_and_verified(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, probe={"limit": 5})
    guard.install(HOST, make_app("/v1/items", {"q:page_size": DOC("Items per page (formerly limit)", "limit", "int")},
                                 method="GET"))
    c = client.post("/api/monitor/items/check").json()
    assert c["drift"] is True and c["verification"] == "verified"
    assert c["repair"]["tool_version"] == 2 and "recovered autonomously" in c["repair"]["reason"]
    inc = client.get(f"/api/incidents/{c['incident']}").json()
    stages = [s["stage"] for s in inc["timeline"]]
    for stage in ("classify", "discover", "diff", "plan", "validate", "apply", "verify"):
        assert stage in stages
    assert inc["category"] == "schema_drift" and inc["simulated"] is False
    assert [v["status"] for v in client.get("/api/tools/items/versions").json()] == ["superseded", "active"]
    # next check: the active version now matches the live schema
    assert client.post("/api/monitor/items/check").json()["drift_summary"] == "No schema drift detected"


def test_repair_that_fails_verification_is_rolled_back(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, probe={"limit": 5})
    guard.install(HOST, make_app("/v1/items", {"q:page_size": DOC("Items per page (formerly limit)", "limit", "int")},
                                 method="GET", faults={"status_value": "no_change"}))
    c = client.post("/api/monitor/items/check").json()
    assert c["verification"] == "failed" and c["repair"] is None
    inc = client.get(f"/api/incidents/{c['incident']}").json()
    assert inc["status"] == "rolled_back" and inc["rolled_back"]
    assert [v["status"] for v in client.get("/api/tools/items/versions").json()] == ["active", "rolled_back"]


def test_unsupported_repair_stops_without_guessing(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, probe={"limit": 5})
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int", "q:tenant": "str"}, method="GET"))   # new required
    c = client.post("/api/monitor/items/check").json()
    assert c["drift"] and c["verification"] == "failed"
    inc = client.get(f"/api/incidents/{c['incident']}").json()
    assert inc["status"] == "stopped" and not inc["validation"]["accepted"]
    assert len(client.get("/api/tools/items/versions").json()) == 1


def test_auth_failure_is_not_schema_drift_and_key_is_not_altered(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET",
                                 auth={"header": "x-api-key", "valid": [KEY]}))
    register(client, probe={"limit": 5}, auth_header="x-api-key", credential_group="crm")
    assert client.post("/api/monitor/items/check").json()["verification"] == "verified"
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET",
                                 auth={"header": "x-api-key", "valid": []}))       # key revoked by the provider
    c = client.post("/api/monitor/items/check").json()
    inc = client.get(f"/api/incidents/{c['incident']}").json()
    assert inc["category"] == "credential_invalid" and inc["plan"] is None and inc["validation"] is None
    assert st.creds._secrets["crm-a"] == KEY                       # unchanged, never manipulated
    assert len(client.get("/api/tools/items/versions").json()) == 1
    assert KEY not in client.get("/api/audit?limit=500").text + client.get(f"/api/incidents/{c['incident']}").text


def test_business_rule_is_not_schema_drift(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/pay", {"amount": "float"}))
    register(client, path="/v1/pay", method="POST")
    guard.install(HOST, make_app("/v1/pay", {"amount": "float"}, faults={"business_rule": "insufficient_balance"}))
    r = client.post("/api/repair/execute", json={"tool": "items", "arguments": {"amount": 5.0}}).json()
    assert r["status"] == "stopped" and r["category"] == "business_rule"


def _plain_error_app(field: str):
    """A provider whose 400 errors do not name fields (unlike FastAPI's 422 detail list)."""
    app = FastAPI()
    Body = create_model("Body", __config__=ConfigDict(extra="forbid"), **{field: (str, ...)})
    Out = create_model("Out", status=(str, ...), received=(dict, ...))

    @app.exception_handler(RequestValidationError)
    async def bad(request: Request, exc):
        return JSONResponse(status_code=400, content={"error": "Bad Request"})

    @app.post("/v1/users", response_model=Out)
    def endpoint(body: Body):
        return {"status": "updated", "received": body.model_dump()}
    return app


def test_drift_is_confirmed_from_the_schema_when_errors_name_no_fields(env):
    client, guard, st = env
    guard.install(HOST, _plain_error_app("user_id"))
    register(client, path="/v1/users", method="POST")
    guard.install(HOST, _plain_error_app("userId"))
    r = client.post("/api/repair/execute", json={"tool": "items", "arguments": {"user_id": "7"}}).json()
    assert r["status"] == "recovered" and r["category"] == "schema_drift"
    inc = client.get(f"/api/incidents/{r['incident']}").json()
    ev = next(s for s in inc["timeline"] if s["stage"] == "classify")["detail"]["evidence"]
    assert any("does not conform to the new schema" in e for e in ev)


def test_network_failure_is_reported_as_connectivity_not_drift(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, probe={"limit": 5})
    guard.fail[HOST] = httpx.ConnectError("connection refused")
    c = client.post("/api/monitor/items/check").json()
    assert c["schema_status"] == "failed" and c["connectivity"] == "unreachable"
    assert c["error_category"] == "network" and c["drift"] is False and c["drift_summary"] is None
    assert c["verification"] == "failed"
    inc = client.get(f"/api/incidents/{c['incident']}").json()
    assert inc["category"] == "network" and inc["plan"] is None


def test_unavailable_schema_fails_closed(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, probe={"limit": 5})
    v2 = make_app("/v1/items", {"q:page_size": "int"}, method="GET")
    v2.openapi_url = None
    v2.router.routes = [r for r in v2.router.routes if getattr(r, "path", "") != "/openapi.json"]
    guard.install(HOST, v2)
    c = client.post("/api/monitor/items/check").json()
    assert c["schema_status"] == "failed" and c["error_category"] == "http_404" and c["drift"] is False
    inc = client.get(f"/api/incidents/{c['incident']}").json()
    assert inc["category"] == "schema_unavailable" and inc["status"] == "stopped"
    assert len(client.get("/api/tools/items/versions").json()) == 1


def test_provider_without_schema_explains_the_limitation(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client, schema_source={"kind": "none", "declared_fields": {"query:limit": "int"}}, probe={"limit": 3})
    c = client.post("/api/monitor/items/check").json()
    assert c["schema_status"] == "unsupported" and c["drift"] is False and c["drift_summary"] is None
    assert "cannot verify schema drift" in c["detail"]
    assert c["verification"] == "failed"          # no authoritative response schema: the result cannot be verified
    calls = [u for _, u in guard.calls]
    assert not any(u.endswith("/openapi.json") for u in calls)


def test_probe_only_for_get_and_interval_limits(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/pay", {"amount": "float"}))
    r = client.post("/api/tools", json={"name": "pay", "base_url": f"https://{HOST}", "path": "/v1/pay",
                                        "method": "POST", "probe_arguments": {"amount": 1}})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "unsafe_probe"
    register(client, path="/v1/pay", method="POST")
    assert client.put("/api/monitor/items", json={"interval_s": 60}).status_code == 422
    assert client.put("/api/monitor/items", json={"interval_s": 900}).json()["interval_s"] == 900
    assert client.get("/api/monitor").json()["tools"][0]["interval_s"] == 900


def test_scheduler_runs_only_due_real_tools(env):
    client, guard, st = env
    guard.install(HOST, make_app("/v1/items", {"q:limit": "int"}, method="GET"))
    register(client)
    now = [1_000_000_000.0]
    st.monitor.clock = lambda: now[0]
    assert st.monitor.due() == ["items"]
    st.monitor.run_due()
    now[0] = __import__("time").time() + 10
    assert st.monitor.due() == []                      # checked moments ago
    now[0] += 3600
    assert st.monitor.due() == ["items"]
    client.put("/api/monitor/items", json={"enabled": False})
    assert st.monitor.due() == []
