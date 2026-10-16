"""Backend API contracts, auth, limits, world separation and secret hygiene."""
import json

import pytest
from fastapi.testclient import TestClient

from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import Credential, CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.mock_api import make_app
from adaptix.storage import Storage
from tests.fakes import FakeGuard

TOKEN = "test-admin-token-1234567890"
REAL_LOOKING_SECRET = "AIzaTEST-must-never-leak-0001"
HOST = "api.example.test"


@pytest.fixture
def world(tmp_path):
    creds = CredentialRegistry([Credential("gemini-primary", "gemini", "gemini", "GEMINI_TEST", scope=["llm"])],
                               secrets={"gemini-primary": REAL_LOOKING_SECRET}, allow_simulated=False)
    guard = FakeGuard()
    st = State(Settings(admin_token=TOKEN, cors_origins=("https://dash.example",), max_body_bytes=2000,
                        allowed_hosts=(HOST,)),
               storage=Storage(tmp_path / "api.db"), creds=creds, llm=NoProvider(), sleep=lambda s: None,
               guard=guard, start_monitor=False)
    return TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"}), guard


@pytest.fixture
def client(world):
    return world[0]


def real_tool(client, guard, fields=None, name="update_user"):
    guard.install(HOST, make_app("/v1/users", fields or {"user_id": "str", "email": "str"}, title="Provider"))
    r = client.post("/api/tools", json={"name": name, "base_url": f"https://{HOST}", "path": "/v1/users",
                                        "method": "POST", "provider": "Example Provider"})
    assert r.status_code == 201, r.text
    return name


def test_auth_required_everywhere_but_liveness(client):
    bare = TestClient(client.app)
    assert bare.get("/api/health/live").status_code == 200
    for path in ("/api/health", "/api/tools", "/api/incidents", "/api/metrics", "/api/audit", "/api/simulations",
                 "/api/sim/tools", "/api/monitor", "/api/integrations"):
        assert bare.get(path).status_code == 401, path
        assert bare.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, path


def test_request_size_limit_and_structured_errors(client):
    r = client.post("/api/repair/execute", content=b"x" * 5000, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "request_too_large"
    r = client.post("/api/repair/execute", json={"tool": "nope", "arguments": {}})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"
    assert client.post("/api/repair/execute", json={"tool": "x", "unexpected": 1}).status_code == 422


def test_cors_allows_only_configured_origins(client):
    ok = client.options("/api/tools", headers={"Origin": "https://dash.example", "Access-Control-Request-Method": "GET"})
    bad = client.options("/api/tools", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "https://dash.example"
    assert "access-control-allow-origin" not in bad.headers


def test_simulation_records_stay_in_the_simulation_world(client):
    r = client.post("/api/simulations", json={"scenario": "documented-nested-rename", "mode": "both"}).json()
    assert r["simulated"] and [x["outcome"] for x in r["runs"]] == ["stopped", "recovered"]
    incs = client.get("/api/sim/incidents").json()
    assert len(incs) == 2 and all(i["simulated"] for i in incs)
    # nothing leaked into production
    assert client.get("/api/incidents").json() == [] and client.get("/api/tools").json() == []
    assert client.get("/api/metrics").json()["sample"]["incidents"] == 0
    m = client.get("/api/sim/metrics").json()
    assert m["world"] == "simulation" and m["sample"]["incidents"] == 1 and m["sample"]["baseline_incidents_excluded"] == 1
    assert m["recovered"] == 1 and m["zero_touch_adaptation_rate"] == 100.0
    inc = client.get(f"/api/sim/incidents/{r['runs'][1]['incident']}").json()
    assert [s["stage"] for s in inc["timeline"]][-1] == "outcome"
    assert len(client.get(f"/api/sim/tools/{inc['tool']}/versions").json()) == 2
    d = client.get(f"/api/sim/tools/{inc['tool']}/drift").json()
    assert d["available"] and d["report"]["changed"] is False      # already adapted to the live schema


def test_production_refuses_simulated_tools(client):
    for body in ({"name": "x", "base_url": "sim://x", "path": "/v1/resource", "simulated": True},
                 {"name": "x", "base_url": "sim://x", "path": "/v1/resource"}):
        r = client.post("/api/tools", json=body)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "simulation_not_allowed"


def test_registration_of_unapproved_host_is_refused_before_sending(world):
    client, guard = world
    r = client.post("/api/tools", json={"name": "x", "base_url": "https://not-approved.example", "path": "/v1/users"})
    assert r.status_code == 422 and "not in the approved host list" in r.json()["detail"]["detail"]
    assert guard.calls == []


def test_analyze_proposes_without_sending(world):
    client, guard = world
    tool = real_tool(client, guard)
    guard.install(HOST, make_app("/v1/users", {"userId": "str", "email": "str"}, title="Provider"))
    before = len(guard.calls)
    a = client.post("/api/repair/analyze", json={"tool": tool, "arguments": {"user_id": "7", "email": "a@b.co"},
                                                 "observed_status": 422,
                                                 "observed_body": {"detail": [{"type": "missing", "loc": ["body", "userId"], "msg": "x"}]}}).json()
    assert a["classification"]["category"] == "schema_drift" and a["validation"]["accepted"]
    assert [c for c in guard.calls[before:] if c[0] != "GET"] == []            # only the schema was read
    assert len(client.get(f"/api/tools/{tool}/versions").json()) == 1


def test_evaluation_endpoint_runs_in_simulation(client):
    out = client.post("/api/evaluation", json={"suite": "scenarios"}).json()
    assert out["simulated"] and out["summary"]["adaptix"]["incorrect_patches"] == 0 and out["llm_choice"] == "none"
    assert out["summary"]["adaptix"]["real_llm_calls"] == 0
    assert client.get("/api/evaluation/latest").json()[0]["suite"] == "scenarios"
    assert client.get("/api/audit?kind=evaluation_run").json() == []


def test_real_gemini_choice_refused_without_key(client):
    r = client.post("/api/simulations", json={"scenario": "window-rename-llm", "mode": "adaptix", "llm": "gemini"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "llm_not_configured"
    r = client.post("/api/health/llm-check")
    assert r.status_code == 409


def test_mock_llm_runs_are_labelled_mock(client):
    r = client.post("/api/simulations", json={"scenario": "ambiguous-rename", "mode": "adaptix", "llm": "mock"}).json()
    run = r["runs"][0]
    assert run["llm"]["used"] and run["llm"]["label"] == "MOCK (not a real model)" and run["llm"]["real"] is False


def test_openai_adapter_uses_production_tools(world):
    client, guard = world
    real_tool(client, guard)
    tools = client.get("/v1/openai/tools").json()
    fn = tools[0]["function"]
    assert fn["parameters"]["required"] == ["user_id", "email"]
    msgs = client.post("/v1/openai/tool_calls", json=[
        {"id": "c1", "type": "function", "function": {"name": fn["name"], "arguments": '{"user_id": "1", "email": "a@b.co"}'}},
        {"id": "c2", "type": "function", "function": {"name": "missing", "arguments": "{}"}},
        {"id": "c3", "type": "function", "function": {"name": fn["name"], "arguments": "not json"}}]).json()
    assert [m["tool_call_id"] for m in msgs] == ["c1", "c2", "c3"]
    assert json.loads(msgs[0]["content"])["status"] == "ok"


def test_integration_health_never_verifies_from_config_alone(world):
    from adaptix.llm.gemini import GeminiProvider
    client, guard = world
    st = client.app.state.adaptix

    def never_called(key):
        raise AssertionError("integration health must not call the provider")
    st.llm = GeminiProvider(st.creds, "gemini-3.5-flash-lite", client_factory=never_called)
    st.engine.set_llm(st.llm)
    real_tool(client, guard)
    rep = client.get("/api/integrations").json()
    g = rep["gemini"]
    assert g["sdk"]["status"] == "VERIFIED" and g["pipeline"]["status"] == "YES"
    assert g["credentials"]["status"] == "CONFIGURED" and g["real_request"]["status"] == "NOT RUN"
    api = rep["external"]["apis"][0]
    assert api["connectivity"]["status"] == "NOT RUN" and api["host_allowed"]["status"] == "CONFIGURED"
    client.post(f"/api/monitor/{api['tool']}/check")
    api = client.get("/api/integrations").json()["external"]["apis"][0]
    assert api["connectivity"]["status"] == "VERIFIED" and api["schema_discovery"]["status"] == "VERIFIED"
    assert REAL_LOOKING_SECRET not in json.dumps(rep)


def test_no_secret_in_any_response_log_or_record(world):
    client, guard = world
    real_tool(client, guard)
    client.post("/api/repair/execute", json={"tool": "update_user", "arguments": {"user_id": "1", "email": "a@b.co"}})
    for sc in ("documented-nested-rename", "credential-failover", "credential-invalid", "header-configured"):
        client.post("/api/simulations", json={"scenario": sc, "mode": "both"})
    st = client.app.state.adaptix
    secrets = [REAL_LOOKING_SECRET] + list(st.creds._secrets.values()) + list(st.sim_creds._secrets.values())
    dumps = [client.get(p).text for p in ("/api/health", "/api/tools", "/api/incidents?limit=1000", "/api/audit?limit=2000",
                                          "/api/metrics", "/v1/openai/tools", "/api/sim/incidents?limit=1000",
                                          "/api/sim/audit?limit=2000", "/api/integrations", "/api/monitor")]
    for storage in (st.storage, st.sim_storage):
        with storage._conn() as c:
            dumps += [r[0] for r in c.execute("SELECT data FROM incidents")] + [r[0] for r in c.execute("SELECT data FROM events")]
    blob = "\n".join(dumps)
    for s in secrets:
        assert s not in blob
