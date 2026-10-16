"""Manual drift demo (REAL_API_TEST): confirmation, automatic recovery after the change, live progress,
honest pipeline counts, cross-view consistency, reset and redaction. Real HTTP against the controlled service."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.storage import Storage

TOKEN = "demo-test-token-00000000000000"


@pytest.fixture
def client(tmp_path):
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"),
               creds=CredentialRegistry([], allow_simulated=False), llm=NoProvider(), sleep=lambda s: None,
               start_monitor=False)
    yield TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    if st.test_service:
        st.test_service.stop()


def run(client, scenario, llm="none"):
    job = client.post("/api/demo/run", json={"scenario": scenario, "llm": llm, "confirm": True})
    assert job.status_code == 200, job.text
    jid = job.json()["id"]
    for _ in range(200):
        j = client.get(f"/api/demo/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("demo did not finish")


def test_change_needs_explicit_confirmation(client):
    r = client.post("/api/demo/run", json={"scenario": "rename"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "confirmation_required"
    assert client.get("/api/test/incidents").json() == []


def test_manual_change_triggers_recovery_without_a_second_action(client):
    j = run(client, "rename")
    phases = [e["phase"] for e in j["events"]]
    order = ["Setup", "Baseline check", "Change introduced", "Call", "Diagnosis", "Schema retrieval",
             "Drift detection", "Repair proposal", "Validation", "Patch applied", "Verification", "Outcome"]
    assert set(order) <= set(phases)
    first = [phases.index(p) for p in order]
    assert first == sorted(first)                          # change first, then detection, repair, verification
    calls = [e for e in j["events"] if e["phase"] == "Call" and e["status"] != "info"]
    assert [e["status"] for e in calls] == ["fail", "ok"]  # the failing call, then the automatic retry
    assert j["result"]["status"] == "recovered" and j["result"]["as_expected"]
    assert all(e["elapsed_ms"] >= 0 for e in j["events"]) and j["elapsed_ms"] > 0       # measured, not implied
    assert j["environment"] == "REAL_API_TEST"


@pytest.mark.parametrize("scenario,status", [("type", "recovered"), ("required", "stopped"), ("remove", "stopped")])
def test_scenarios_report_actual_outcomes(client, scenario, status):
    j = run(client, scenario)
    assert j["result"]["status"] == status and j["result"]["as_expected"], j["events"]


def test_gemini_only_scenarios_refuse_without_a_key(client):
    for sc in ("prose", "orders"):
        r = client.post("/api/demo/run", json={"scenario": sc, "llm": "gemini", "confirm": True})
        assert r.status_code == 409 and "GEMINI_API_KEY" in r.json()["detail"]["detail"]
    assert client.post("/api/demo/run", json={"scenario": "rename", "llm": "mock", "confirm": True}).status_code == 422


def test_every_view_reads_the_same_records(client):
    j = run(client, "rename")
    run(client, "required")
    inc_id, tool = j["result"]["incident"], j["result"]["tool"]
    incs = client.get("/api/test/incidents").json()
    assert inc_id in [i["id"] for i in incs]
    versions = client.get(f"/api/test/tools/{tool}/versions").json()
    assert [v["status"] for v in versions] == ["superseded", "active"] and versions[1]["incident_id"] == inc_id
    drift = client.get(f"/api/test/tools/{tool}/drift").json()
    assert drift["available"] and drift["report"]["changed"] is False and drift["current_version"] == 2
    ev = client.get("/api/test/evidence").json()
    assert any(e["incident_id"] == inc_id and e["final"]["verified_recovery"] for e in ev)
    p = client.get("/api/test/scorecard").json()["pipeline"]
    # one recovered rename + one refused required-field change; a proposal is never counted as applied/verified
    assert p["incidents"] == 2 and p["detected_drift"] == 2 and p["validated_patches"] == 1
    assert p["applied_patches"] == 1 and p["verified_recoveries"] == 1 and p["refused_or_stopped"] == 1
    assert p["proposed_repairs"] >= p["validated_patches"] >= p["applied_patches"] >= p["verified_recoveries"]
    ov = client.get("/api/overview").json()["REAL_API_TEST"]
    assert ov["incidents"] == 2 and ov["recovered"] == 1
    changes = client.get("/api/changes", params={"environment": "REAL_API_TEST", "page_size": 200}).json()
    assert {r["incident_id"] for r in changes["rows"]} == {i["id"] for i in incs}


def test_reset_needs_confirmation_and_returns_to_known_good(client):
    run(client, "rename")
    assert client.post("/api/demo/reset", json={}).status_code == 422
    assert client.post("/api/demo/reset", json={"confirm": True}).json()["reset"] is True
    assert client.get("/api/test/incidents").json() == [] and client.get("/api/test/tools").json() == []
    assert client.get("/api/test/scorecard").json()["pipeline"]["incidents"] == 0
    assert client.get("/api/incidents").json() == []                         # production untouched


def test_exports_are_redacted(client):
    run(client, "rename")
    st = client.app.state.adaptix
    blobs = [client.get(p).text for p in ("/api/test/evidence/export.json", "/api/test/evidence/export.csv",
                                          "/api/test/audit?limit=500", "/api/test/incidents")]
    blobs.append(json.dumps(client.get(f"/api/demo/jobs/{next(iter(st.demo_jobs.jobs))}").json()))
    for b in blobs:
        assert TOKEN not in b and st.test_service.admin_token not in b
