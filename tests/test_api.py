"""Backend API contracts, auth, limits and secret hygiene."""
import json

import pytest
from fastapi.testclient import TestClient

from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import Credential, CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.storage import Storage

TOKEN = "test-admin-token-1234567890"
REAL_LOOKING_SECRET = "AIzaTEST-must-never-leak-0001"


@pytest.fixture
def client(tmp_path):
    creds = CredentialRegistry([Credential("gemini-primary", "gemini", "gemini", "GEMINI_TEST", scope=["llm"])],
                               secrets={"gemini-primary": REAL_LOOKING_SECRET})
    st = State(Settings(admin_token=TOKEN, cors_origins=("https://dash.example",), max_body_bytes=2000),
               storage=Storage(tmp_path / "api.db"), creds=creds, llm=NoProvider(), sleep=lambda s: None)
    return TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})


def test_auth_required_everywhere_but_liveness(client):
    bare = TestClient(client.app)
    assert bare.get("/api/health/live").status_code == 200
    for path in ("/api/health", "/api/tools", "/api/incidents", "/api/metrics", "/api/audit", "/api/simulations"):
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


def test_simulation_creates_real_records_and_metrics(client):
    r = client.post("/api/simulations", json={"scenario": "documented-nested-rename", "mode": "both"}).json()
    assert r["simulated"] and [x["outcome"] for x in r["runs"]] == ["stopped", "recovered"]
    incs = client.get("/api/incidents?simulated=true").json()
    assert len(incs) == 2 and all(i["simulated"] for i in incs)
    m = client.get("/api/metrics").json()
    # the baseline run (repair disabled) is stored and labelled, but kept out of the metrics
    assert m["sample"]["incidents"] == 1 and m["sample"]["baseline_incidents_excluded"] == 1 and m["recovered"] == 1
    assert m["zero_touch_adaptation_rate"] == 100.0
    assert sorted(i["healing"] for i in incs) == [False, True]
    inc = client.get(f"/api/incidents/{r['runs'][1]['incident']}").json()
    assert [s["stage"] for s in inc["timeline"]][-1] == "outcome"
    tool = r["runs"][1]["incident"] and inc["tool"]
    assert len(client.get(f"/api/tools/{tool}/versions").json()) == 2
    d = client.get(f"/api/tools/{tool}/drift").json()
    assert d["available"] and d["report"]["changed"] is False      # already adapted to the live schema


def test_analyze_proposes_without_sending(client):
    client.post("/api/simulations", json={"scenario": "rename-param", "mode": "baseline"})
    tool = client.get("/api/tools").json()[0]["name"]
    app = client.app.state.adaptix.engine.upstreams.sim_app(tool)
    before = app.state.calls
    a = client.post("/api/repair/analyze", json={"tool": tool, "arguments": {"user_id": "7", "email": "a@b.co"},
                                                 "observed_status": 422,
                                                 "observed_body": {"detail": [{"type": "missing", "loc": ["body", "userId"], "msg": "x"}]}}).json()
    assert a["classification"]["category"] == "schema_drift" and a["validation"]["accepted"]
    assert app.state.calls == before and len(client.get(f"/api/tools/{tool}/versions").json()) == 1


def test_evaluation_endpoint(client):
    out = client.post("/api/evaluation", json={"suite": "scenarios"}).json()
    assert out["simulated"] and out["summary"]["adaptix"]["incorrect_patches"] == 0
    assert client.get("/api/evaluation/latest").json()[0]["suite"] == "scenarios"


def test_openai_adapter(client):
    client.post("/api/simulations", json={"scenario": "rename-param", "mode": "adaptix"})
    tools = client.get("/v1/openai/tools").json()
    fn = tools[0]["function"]
    assert fn["parameters"]["required"] == ["user_id", "email"]
    msgs = client.post("/v1/openai/tool_calls", json=[
        {"id": "c1", "type": "function", "function": {"name": fn["name"], "arguments": '{"user_id": "1", "email": "a@b.co"}'}},
        {"id": "c2", "type": "function", "function": {"name": "missing", "arguments": "{}"}},
        {"id": "c3", "type": "function", "function": {"name": fn["name"], "arguments": "not json"}}]).json()
    assert [m["tool_call_id"] for m in msgs] == ["c1", "c2", "c3"]
    assert json.loads(msgs[0]["content"])["status"] in ("ok", "recovered")


def test_no_secret_in_any_response_log_or_record(client):
    for sc in ("documented-nested-rename", "credential-failover", "credential-invalid", "header-configured"):
        client.post("/api/simulations", json={"scenario": sc, "mode": "both"})
    st = client.app.state.adaptix
    secrets = [REAL_LOOKING_SECRET] + list(st.creds._secrets.values())
    dumps = [client.get(p).text for p in ("/api/health", "/api/tools", "/api/incidents?limit=1000", "/api/audit?limit=2000",
                                          "/api/metrics", "/v1/openai/tools")]
    dumps.append(client.post("/api/health/llm-check").text)
    with st.storage._conn() as c:
        dumps += [r[0] for r in c.execute("SELECT data FROM incidents")] + [r[0] for r in c.execute("SELECT data FROM events")]
    blob = "\n".join(dumps)
    for s in secrets:
        assert s not in blob
