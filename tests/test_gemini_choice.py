"""Choosing "Real Gemini" makes Gemini propose the repair that is validated, applied and verified.

These use a FAKE Gemini SDK client (no network, no real key): they are NOT live-provider tests, and the results are
labelled "not a real request (test double)". The live test is tests/test_gemini_live.py."""
import json
import time

import pytest
from fastapi.testclient import TestClient

import adaptix.api as api_mod
import adaptix.demo as demo_mod
from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.storage import Storage
from tests.test_gemini import FakeClient, creds

TOKEN = "gemini-choice-token-000000000000"
from adaptix.llm.gemini import GeminiProvider  # noqa: E402


def plan(target, source, confidence=0.93):
    return json.dumps({"diagnosis": f"{source} was renamed to {target}", "evidence": [f"{source} -> {target}"],
                       "affected_fields": [target],
                       "mappings": [{"target_field": target, "source_arg": source, "transform": "none",
                                     "evidence": f"field names: {source} -> {target.split(':')[-1]}"}],
                       "response_mappings": [], "compatibility_risks": [], "confidence": confidence,
                       "verification_steps": []})


@pytest.fixture
def world(tmp_path, monkeypatch):
    script, calls = [], []
    gem = GeminiProvider(creds(False), "gemini-3.5-flash-lite", client_factory=lambda k: FakeClient(script, k, calls),
                         sleep=lambda s: None)
    # the fake client is a test double (never labelled real); let the server offer the Gemini choice anyway
    monkeypatch.setattr(api_mod, "is_live", lambda p: p is gem)
    monkeypatch.setattr(demo_mod, "is_live", lambda p: p is gem)
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"), sim_storage=Storage(tmp_path / "s.db", mode="simulation"),
               creds=creds(False), llm=gem, sleep=lambda s: None, start_monitor=False)
    client = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    yield client, st, script, calls
    if st.test_service:
        st.test_service.stop()


def simulate(client, llm):
    r = client.post("/api/simulations", json={"scenario": "rename-param", "mode": "adaptix", "llm": llm, "seed": 7})
    assert r.status_code == 200, r.text
    return r.json()["runs"][0]


def test_gemini_choice_makes_gemini_propose_the_applied_patch(world):
    client, st, script, calls = world
    script.append(plan("body:userId", "user_id"))
    run = simulate(client, "gemini")
    assert len(calls) == 1                                       # Gemini was actually asked, even for a simple rename
    assert run["outcome"] == "recovered" and run["tool_version"] == 2
    inc = client.get(f"/api/sim/incidents/{run['incident']}").json()
    assert inc["plan"]["source"] == "llm"
    assert {(m["target_field"], m["source_arg"]) for m in inc["plan"]["mappings"]} >= {("body:userId", "user_id")}
    assert inc["validation"]["accepted"] and inc["verification"]["ok"]   # validated, applied, verified by a retry
    assert run["repair_by"].startswith("not a real request (test double)")  # a fake is never labelled real
    assert "REAL" not in run["repair_by"] and run["llm"]["real"] is False
    assert not st.sim_engine.llm_required                        # reset after the run


def test_a_low_confidence_gemini_proposal_is_not_applied(world):
    client, _, script, calls = world
    script.append(plan("body:userId", "user_id", confidence=0.2))
    run = simulate(client, "gemini")
    assert len(calls) == 1 and run["outcome"] == "stopped" and run["repair_by"] is None
    tool = client.get(f"/api/sim/incidents/{run['incident']}").json()["tool"]
    assert [v["version"] for v in client.get(f"/api/sim/tools/{tool}/versions").json()] == [1]   # nothing applied


def test_gemini_failure_stops_safely_without_falling_back_to_rules(world):
    client, _, script, calls = world
    script.extend(["{not json"] * 10)
    run = simulate(client, "gemini")
    assert calls and run["outcome"] == "stopped" and run["repair_by"] is None


def test_rules_only_never_calls_gemini(world):
    client, _, _, calls = world
    run = simulate(client, "none")
    assert not calls and run["outcome"] == "recovered" and run["repair_by"] == "rules planner"


def test_drift_demo_gemini_choice_applies_geminis_patch_on_the_controlled_service(world):
    client, st, script, calls = world
    script.append(plan("body:userId", "user_id"))
    jid = client.post("/api/demo/run", json={"scenario": "rename", "llm": "gemini", "confirm": True}).json()["id"]
    for _ in range(200):
        j = client.get(f"/api/demo/jobs/{jid}").json()
        if j["status"] != "running":
            break
        time.sleep(0.05)
    assert len(calls) == 1
    assert j["result"]["status"] == "recovered" and j["result"]["repair_by"].startswith("not a real request")
    inc = client.get(f"/api/test/incidents/{j['result']['incident']}").json()
    assert inc["plan"]["source"] == "llm" and inc["verification"]["ok"]
    assert not st.test_engine.llm_required


def test_name_based_evidence_from_gemini_is_held_to_the_strict_name_check(world):
    client, _, script, _ = world
    script.append(plan("body:userId", "email"))                  # "field names: email -> userId" is not a rename
    run = simulate(client, "gemini")
    assert run["outcome"] == "stopped" and run["repair_by"] is None
