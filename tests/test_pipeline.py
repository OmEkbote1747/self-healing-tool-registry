"""End-to-end repair pipeline on simulated APIs (deterministic; no live provider)."""
import threading

import pytest

from adaptix import simulation as sim
from adaptix.mock_api import make_app
from adaptix.models import ToolSpec


@pytest.mark.parametrize("scenario", [s.id for s in sim.SCENARIOS])
def test_every_scenario_ends_as_expected(engine, scenario):
    r = sim.run(engine, scenario)
    assert r["matches_expectation"], r


@pytest.mark.parametrize("scenario", [s.id for s in sim.SCENARIOS])
def test_baseline_never_recovers_from_drift(engine, scenario):
    r = sim.run(engine, scenario, heal=False)
    assert r["outcome"] == "stopped", r


def test_categories_are_reported(engine):
    for s in sim.SCENARIOS:
        if s.needs_llm:
            continue
        r = sim.run(engine, s.id)
        assert r["category"] == s.expect_category, (s.id, r["category"])


@pytest.mark.parametrize("scenario", ["invalid-input", "business-rule", "credential-invalid", "method-change",
                                      "ambiguous-rename", "unit-change", "new-required-field", "unsafe-type-change"])
def test_stopped_scenarios_cause_no_side_effects(engine, scenario):
    r = sim.run(engine, scenario)
    assert r["outcome"] == "stopped" and r["side_effects"] == 0, r


def test_invalid_input_never_triggers_a_schema_repair(engine):
    r = sim.run(engine, "invalid-input")
    inc = engine.storage.incident(r["incident"])
    assert inc.category == "invalid_input" and inc.plan is None and inc.repair_attempts == 0
    assert len(engine.storage.versions(inc.tool)) == 1


@pytest.mark.parametrize("scenario", ["credential-invalid", "credential-failover"])
def test_auth_errors_never_trigger_a_schema_repair(engine, scenario):
    r = sim.run(engine, scenario)
    inc = engine.storage.incident(r["incident"])
    assert inc.plan is None and inc.repair_attempts == 0
    assert len(engine.storage.versions(inc.tool)) == 1


def test_recovered_incident_has_full_timeline_and_reversible_patch(engine):
    r = sim.run(engine, "documented-nested-rename")
    inc = engine.storage.incident(r["incident"])
    stages = [s.stage for s in inc.timeline]
    for stage in ("request", "classify", "discover", "diff", "plan", "validate", "apply", "verify", "outcome"):
        assert stage in stages, stage
    assert inc.zero_touch and inc.from_version == 1 and inc.to_version == 2
    v2 = engine.storage.versions(inc.tool)[1]
    assert v2.parent_version == 1 and v2.patch and engine.storage.versions(inc.tool)[0].status == "superseded"
    assert r["side_effects"] == 1


def test_verification_failure_rolls_back(engine):
    r = sim.run(engine, "verification-rollback")
    inc = engine.storage.incident(r["incident"])
    assert inc.status == "rolled_back" and inc.rolled_back
    statuses = {v.version: v.status for v in engine.storage.versions(inc.tool)}
    assert statuses == {1: "active", 2: "rolled_back"}


def test_response_repair_does_not_resend(engine):
    r = sim.run(engine, "response-rename")
    assert r["outcome"] == "recovered" and r["upstream_calls"] == 1 and r["side_effects"] == 1


def test_mapping_is_reused_only_after_revalidation(engine):
    name = sim.install(engine, sim.BY_ID["rename-param"], "reuse", "adaptix")
    assert engine.execute(name, dict(sim.BY_ID["rename-param"].args))["status"] == "recovered"
    engine.storage.rollback(name, 2, "test: force the same drift again")
    res = engine.execute(name, dict(sim.BY_ID["rename-param"].args))
    inc = engine.storage.incident(res["incident"])
    assert res["status"] == "recovered" and inc.plan["source"] == "reuse"
    assert any(s.stage == "validate" and s.status == "ok" for s in inc.timeline)


def test_concurrent_repairs_create_one_version(engine):
    s = sim.BY_ID["rename-param"]
    name = sim.install(engine, s, "conc", "adaptix")
    results = []

    def worker():
        results.append(engine.execute(name, dict(s.args)))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r["status"] in ("recovered", "ok") for r in results), results
    active = [v for v in engine.storage.versions(name) if v.status == "active"]
    assert len(active) == 1
    assert len([v for v in engine.storage.versions(name) if v.origin in ("repair", "reuse")]) == 1


def test_idempotency_key_prevents_duplicate_side_effects(engine):
    s = sim.BY_ID["rename-param"]
    name = sim.install(engine, s, "idem", "adaptix")
    app = engine.upstreams.sim_app(name)
    engine.execute(name, dict(s.args))
    assert app.state.effects == 1 and len(app.state.idempotency) == 1


def test_retry_after_is_respected(make_engine):
    sleeps = []
    eng = make_engine(sleeps=sleeps)
    r = sim.run(eng, "rate-limit")
    assert r["outcome"] == "recovered" and sleeps == [0.2]


def test_retry_after_beyond_budget_is_not_waited(make_engine):
    eng = make_engine(total_time_budget_s=0.5)
    s = sim.BY_ID["rename-param"]
    eng.upstreams.set_simulated("slow", make_app(s.path, s.v1))
    eng.register(ToolSpec(name="slow", base_url="sim://slow", path=s.path, simulated=True))
    eng.upstreams.set_simulated("slow", make_app(s.path, s.v1, faults={"fail_times": 1, "retry_after": 30}))
    res = eng.execute("slow", dict(s.args))
    assert res["status"] == "stopped" and res["category"] == "rate_limited"


def test_server_errors_on_non_idempotent_calls_are_not_retried(make_engine):
    sleeps = []
    eng = make_engine(sleeps=sleeps)
    s = sim.BY_ID["rename-param"]
    eng.upstreams.set_simulated("err", make_app(s.path, s.v1))
    eng.register(ToolSpec(name="err", base_url="sim://err", path=s.path, simulated=True, supports_idempotency_key=False))
    eng.upstreams.set_simulated("err", make_app(s.path, s.v1, faults={"status": 503}))
    res = eng.execute("err", dict(s.args))
    assert res["category"] == "server_error" and sleeps == []
    assert eng.upstreams.sim_app("err").state.calls == 1


def test_repeated_failure_stops_within_budget(engine):
    s = sim.BY_ID["rename-param"]
    name = sim.install(engine, s, "loop", "adaptix")
    # the API keeps changing: the repaired call fails again with a new drift
    app_v3 = make_app(s.path, {"user_identifier_v3": "str", "email": "str"})
    orig = engine.upstreams.set_simulated

    calls = {"n": 0}
    real_request = engine.upstreams.request

    def flaky(base_url, method, url, **kw):
        calls["n"] += 1
        if calls["n"] == 2:                       # right after the repair is applied, the API changes again
            orig(name, app_v3)
        return real_request(base_url, method, url, **kw)

    engine.upstreams.request = flaky
    res = engine.execute(name, dict(s.args))
    assert res["status"] in ("rolled_back", "stopped") and calls["n"] <= 4


def test_second_drift_after_a_repair_is_still_diagnosed(engine):
    """Regression: the repaired version's schema must be stored so a later drift can be diffed."""
    s = sim.BY_ID["rename-param"]
    name = sim.install(engine, s, "twice", "adaptix")
    assert engine.execute(name, dict(s.args))["status"] == "recovered"
    engine.upstreams.set_simulated(name, make_app(s.path, {"userId": "str", "emailAddress": "str"}))
    res = engine.execute(name, dict(s.args))
    assert res["status"] == "recovered" and res["category"] == "schema_drift"
    assert [v.version for v in engine.storage.versions(name)] == [1, 2, 3]
