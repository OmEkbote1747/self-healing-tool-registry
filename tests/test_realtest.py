"""CONTROLLED REAL HTTP API tests (REAL_API_TEST).

These start the controlled test service as a real uvicorn server on 127.0.0.1 and talk to it over TCP
through the guarded client. They need no internet and no Gemini key.

Where a test needs a model, it uses a Gemini TEST DOUBLE (fake SDK client): such runs are labelled
"not a real request" in the evidence and are NOT counted as real Gemini calls. The live Gemini run of the
same demonstration is in tests/test_gemini_live.py (needs GEMINI_API_KEY).
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from adaptix import realtest
from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import Credential, CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.llm.gemini import GeminiProvider
from adaptix.netguard import EgressDenied
from adaptix.storage import Storage

TOKEN = "realtest-token-000000000000000"
KEY = "AIzaTEST-realtest-never-leak-01"


def gemini_double(plan: dict | None = None, raw: str | None = None):
    """A Gemini provider whose SDK client is a test double. Results are labelled not real."""
    def factory(key):
        def generate_content(model, contents, config):
            if raw is not None:
                return SimpleNamespace(text=raw, usage_metadata=None)
            if "single word OK" in contents:
                return SimpleNamespace(text="OK", usage_metadata=None)
            return SimpleNamespace(text=json.dumps(plan), usage_metadata=None)
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    creds = CredentialRegistry([Credential("gemini-primary", "gemini", "gemini", "X", scope=["llm"])],
                               secrets={"gemini-primary": KEY}, allow_simulated=False)
    return GeminiProvider(creds, "gemini-3.5-flash-lite", client_factory=factory, sleep=lambda s: None), creds


RENAME = {"diagnosis": "customer_id renamed to client_id", "evidence": [], "affected_fields": ["body:client_id"],
          "mappings": [{"target_field": "body:client_id", "source_arg": "customer_id", "transform": "none",
                        "evidence": "Identifier of the client placing the order (previously called customer_id)"}],
          "response_mappings": [], "compatibility_risks": [], "confidence": 0.95, "verification_steps": []}


@pytest.fixture
def state(tmp_path):
    llm, creds = gemini_double(RENAME)
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), creds=creds, llm=llm,
               sleep=lambda s: None, start_monitor=False)
    st.is_live_override = True
    yield st
    if st.test_service:
        st.test_service.stop()


@pytest.fixture
def client(state):
    return TestClient(create_app(state), headers={"Authorization": f"Bearer {TOKEN}"})


def test_service_is_a_real_http_server_with_versioned_schema(state):
    svc = state.test_service_started()
    v1 = httpx.get(svc.url + "/orders-api/openapi.json").json()
    assert v1["info"]["version"] == "1.0.0" and "customer_id" in json.dumps(v1["components"])
    svc.set_orders("v2")
    v2 = httpx.get(svc.url + "/orders-api/openapi.json").json()
    assert v2["info"]["version"] == "2.0.0" and "client_id" in json.dumps(v2["components"])
    assert "previously called customer_id" in json.dumps(v2)
    with pytest.raises(httpx.HTTPStatusError):                       # admin API needs the token
        r = httpx.post(svc.url + "/admin/orders", json={"version": "v1"})
        r.raise_for_status()


def test_guard_exempts_only_the_exact_test_service_address(state):
    from adaptix.netguard import check_url
    svc = state.test_service_started()
    up = state.test_engine.upstreams
    assert up.request(svc.url + "/orders-api", "GET", "/health").status_code == 200
    with pytest.raises(EgressDenied):                                # the guard refuses any other loopback port
        check_url(f"http://127.0.0.1:{svc.port + 1}/", (), exempt=up.guard.exempt)
    # a loopback URL recorded by another process is re-pointed to THIS process's controlled service only
    assert up.rebase(f"http://127.0.0.1:{svc.port + 1}/m/x/openapi.json") == svc.url + "/m/x/openapi.json"
    assert up.rebase("http://localhost:1/x") == svc.url + "/x"
    for url in ("http://169.254.169.254/latest", "http://10.0.0.5/x", "https://127.0.0.1:443/x"):
        assert up.rebase(url) == url                                 # never rewritten
        with pytest.raises(EgressDenied):
            up.request(url, "GET", "/")
    with pytest.raises(EgressDenied):
        up.request("sim://x", "GET", "/")
    prod = state.engine.upstreams                                    # production never rewrites loopback
    assert prod.rebase(f"http://127.0.0.1:{svc.port}/x") == f"http://127.0.0.1:{svc.port}/x"
    with pytest.raises(EgressDenied):
        prod.request(svc.url, "GET", "/orders-api/health")

def test_demo_blocks_without_real_gemini_and_never_falls_back(tmp_path):
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), llm=NoProvider(),
               creds=CredentialRegistry([], allow_simulated=False), sleep=lambda s: None, start_monitor=False)
    try:
        r = realtest.run_orders_demo(st)
        assert r["outcome"] == "blocked" and "GEMINI_API_KEY" in r["error"] and r["evidence_id"] is None
        assert st.test_storage.incidents() == []
    finally:
        if st.test_service:
            st.test_service.stop()


def _run_demo(state, fault=None):
    import adaptix.realtest as rt
    orig = rt.is_live
    rt.is_live = lambda llm: True                    # the double stands in for the configured model
    try:
        return rt.run_orders_demo(state, fault)
    finally:
        rt.is_live = orig


def test_end_to_end_recovery_with_business_verification(state):
    r = _run_demo(state)
    names = [s["name"] for s in r["steps"]]
    assert names == ["preflight", "initial_call", "schema_change", "original_request_fails", "pipeline",
                     "business_verification"], r
    assert r["outcome"] == "recovered" and r["verified_recovery"] is True
    fail = r["steps"][3]["detail"]
    assert fail["http_status"] == 422 and fail["classification"]["category"] == "schema_drift"
    ev = state.test_storage.evidence_one(r["evidence_id"])
    assert ev["environment"] == "REAL_API_TEST" and ev["evidence_source"].startswith("controlled test service")
    assert ev["llm"]["real_request_performed"] is False          # test double, honestly labelled
    assert ev["validation"]["accepted"] and ev["applied_change"]["from_version"] == 1
    assert ev["applied_change"]["to_version"] == 2 and ev["verification"]["business_outcome"]["ok"]
    before, after = ev["schema"]["before"]["schema_signature"], ev["schema"]["after"]["schema_signature"]
    assert before and after and "*" not in before and before != after
    stages = [t["stage"] for t in ev["timeline"]]
    for stage in ("request", "classify", "discover", "diff", "plan", "validate", "apply", "verify", "outcome"):
        assert stage in stages
    assert KEY not in json.dumps(ev)


def test_business_verification_failure_rolls_back_without_resending(state):
    before = state.test_service_started().orders_state()["calls"]
    r = _run_demo(state, "drop_writes")
    assert r["outcome"] == "rolled_back" and r["verified_recovery"] is False
    assert [s["name"] for s in r["steps"]][-2:] == ["business_verification", "rollback"]
    tool = [t["spec"].name for t in state.test_storage.tools()][0]
    assert [v.status for v in state.test_storage.versions(tool)] == ["active", "rolled_back"]
    # the order endpoint ran twice: the v1 order and the repaired v2 order. The unchanged v2 request was
    # rejected by schema validation before reaching it, and nothing is re-sent after the rollback.
    assert state.test_service.orders_state()["calls"] - before == 2


def test_malformed_model_output_is_rejected_safely(tmp_path):
    llm, creds = gemini_double(raw="this is not JSON {")
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), creds=creds, llm=llm,
               sleep=lambda s: None, start_monitor=False)
    try:
        r = _run_demo(st)
        assert r["outcome"] == "stopped"
        ev = st.test_storage.evidence_one(r["evidence_id"])
        assert ev["llm"]["error_category"] == "malformed_output" and ev["validation"]["accepted"] is False
        assert ev["applied_change"] is None
    finally:
        st.test_service.stop()


def test_unsafe_model_proposal_is_rejected(tmp_path):
    bad = {**RENAME, "mappings": [{"target_field": "body:client_id", "source_arg": "sku", "transform": "none",
                                   "evidence": "made up evidence that is not in the schema"}]}
    llm, creds = gemini_double(bad)
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), creds=creds, llm=llm,
               sleep=lambda s: None, start_monitor=False)
    try:
        r = _run_demo(st)
        ev = st.test_storage.evidence_one(r["evidence_id"])
        assert r["outcome"] == "stopped" and ev["validation"]["accepted"] is False and ev["applied_change"] is None
    finally:
        st.test_service.stop()


def test_matrix_over_real_http_matches_expectations(state):
    m = realtest.run_matrix(state, runs=1, llm="none")
    bad = [(r["scenario"], r["expected"], r["outcome"]) for r in m["rows"] if not r["matches_expectation"]]
    assert bad == []
    assert all(r["environment"] == "REAL_API_TEST" for r in m["rows"])
    sc = m["scorecard"]
    assert sc["unsafe_repairs_accepted"] == 0 and sc["false_positive_drift"] == 0 and sc["real_llm_requests"] == 0
    cats = {r["scenario"]: r["category"] for r in m["rows"]}
    assert cats["credential-invalid"] == "credential_invalid" and cats["server-error"] == "server_error"
    assert cats["rate-limit"] == "rate_limited"
    with pytest.raises(realtest.RealTestError):
        realtest.run_matrix(state, ["rename-param"], llm="mock")


def test_api_routes_and_exports_are_separated_and_redacted(client, state):
    import adaptix.realtest as rt
    orig = rt.is_live
    rt.is_live = lambda llm: True
    try:
        assert client.post("/api/test/demo", json={}).json()["outcome"] == "recovered"
    finally:
        rt.is_live = orig
    client.post("/api/simulations", json={"scenario": "rename-param", "mode": "adaptix"})
    assert client.get("/api/evidence").json() == []                       # nothing in LIVE_API
    test_ev = client.get("/api/test/evidence").json()
    sim_ev = client.get("/api/sim/evidence").json()
    assert {e["environment"] for e in test_ev} == {"REAL_API_TEST"} and {e["environment"] for e in sim_ev} == {"SIMULATION"}
    bundle = client.get("/api/test/evidence/export.json")
    csv_text = client.get("/api/test/evidence/export.csv").text
    assert bundle.status_code == 200 and bundle.json()["scorecard"]["records"] == len(test_ev)
    assert csv_text.splitlines()[0].startswith("evidence_id,run_id")
    for blob in (bundle.text, csv_text, client.get("/api/changes?page_size=200").text):
        assert KEY not in blob and state.test_service.admin_token not in blob
    ov = client.get("/api/overview").json()
    assert ov["REAL_API_TEST"]["evidence_records"] >= 1 and ov["LIVE_API"]["incidents"] == 0


def test_standalone_service_on_a_fixed_port_answers_health():
    import socket
    from adaptix.testservice import TestService
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    svc = TestService().start(port=port)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/orders-api/health")
        assert svc.port == port and r.status_code == 200 and r.json() == {"status": "ok", "schema_version": "v1"}
    finally:
        svc.stop()
