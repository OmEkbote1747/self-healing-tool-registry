"""Production / simulation separation, enforced in the backend, plus Simulation Lab repeatability and honesty."""
from types import SimpleNamespace

import pytest

from adaptix import simulation as sim
from adaptix.api import State
from adaptix.config import Settings
from adaptix.credentials import CredentialError, CredentialRegistry
from adaptix.engine import Engine, EngineError
from adaptix.llm import NoProvider, make_provider
from adaptix.llm.mock import MockProvider
from adaptix.models import Incident, ToolSpec
from adaptix.netguard import EgressDenied
from adaptix.storage import IsolationError, Storage
from adaptix.upstreams import Upstreams


def test_each_store_refuses_the_other_worlds_records(tmp_path):
    prod, simu = Storage(tmp_path / "p.db", "production"), Storage(tmp_path / "s.db", "simulation")
    with pytest.raises(IsolationError):
        prod.save_incident(Incident(tool="t", simulated=True))
    with pytest.raises(IsolationError):
        simu.save_incident(Incident(tool="t", simulated=False))
    with pytest.raises(IsolationError):
        prod.event("x", simulated=True)
    with pytest.raises(IsolationError):
        prod.record_execution("t", True, "ok", 1.0, simulated=True)
    with pytest.raises(IsolationError):
        prod.reset()
    with pytest.raises(IsolationError):
        prod.register(ToolSpec(name="t", base_url="sim://t", path="/x", simulated=True), None, None)
    with pytest.raises(IsolationError):
        simu.register(ToolSpec(name="t", base_url="https://api.example.com", path="/x"), None, None)


def test_a_database_file_cannot_be_reopened_in_the_other_mode(tmp_path):
    Storage(tmp_path / "p.db", "production")
    with pytest.raises(IsolationError):
        Storage(tmp_path / "p.db", "simulation")


def test_state_refuses_a_shared_database_path(tmp_path):
    p = str(tmp_path / "same.db")
    with pytest.raises(IsolationError):
        State(Settings(db_path=p, sim_db_path=p), start_monitor=False)


def test_upstreams_enforce_their_world():
    with pytest.raises(EgressDenied):
        Upstreams(mode="production").request("sim://x", "POST", "/v1")
    with pytest.raises(EgressDenied):
        Upstreams(mode="production").set_simulated("x", None)
    with pytest.raises(EgressDenied) as e:
        Upstreams(mode="simulation").request("https://api.example.com", "POST", "/v1")
    assert e.value.category == "real_in_simulation"
    with pytest.raises(EgressDenied):
        Upstreams(mode="simulation").fetch_json("https://api.example.com/openapi.json")


def test_production_never_uses_the_mock_llm(tmp_path):
    prod = Storage(tmp_path / "p.db", "production")
    with pytest.raises(EngineError):
        Engine(prod, CredentialRegistry([]), MockProvider(), Settings(), Upstreams(mode="production"))
    eng = Engine(prod, CredentialRegistry([]), NoProvider(), Settings(), Upstreams(mode="production"))
    with pytest.raises(EngineError):
        eng.set_llm(MockProvider())
    s = Settings(llm_provider="mock")
    assert isinstance(make_provider(s, CredentialRegistry([]), mode="production"), NoProvider)
    assert isinstance(make_provider(s, CredentialRegistry([]), mode="simulation"), MockProvider)


def test_production_credentials_refuse_simulated_keys():
    reg = CredentialRegistry([], allow_simulated=False)
    with pytest.raises(CredentialError):
        reg.add_simulated_group("g", "x-api-key", ["a"])


def test_simulations_refuse_the_production_engine(tmp_path):
    eng = Engine(Storage(tmp_path / "p.db", "production"), CredentialRegistry([]), NoProvider(), Settings(),
                 Upstreams(mode="production"))
    with pytest.raises(sim.SimulationError):
        sim.run(eng, "rename-param")


def test_scenarios_are_repeatable_and_report_actual_outcomes(make_engine):
    a, b = make_engine(), make_engine()
    runs_a = [sim.run(a, s.id, seed=11) for s in sim.SCENARIOS]
    runs_b = [sim.run(b, s.id, seed=11) for s in sim.SCENARIOS]
    key = lambda r: (r["scenario"], r["outcome"], r["category"], r["tool_version"], r["upstream_calls"], r["side_effects"])
    assert [key(r) for r in runs_a] == [key(r) for r in runs_b]
    outcomes = {r["outcome"] for r in runs_a}
    assert {"recovered", "stopped", "rolled_back"} <= outcomes              # not "always success"
    assert all(r["simulated"] for r in runs_a)
    assert all(r["matches_expectation"] for r in runs_a), [(r["scenario"], r["outcome"], r["expected"])
                                                           for r in runs_a if not r["matches_expectation"]]


def test_mismatches_are_reported_not_hidden(make_engine):
    eng = make_engine()
    s = sim.BY_ID["window-rename-llm"]                  # needs a real LLM
    r = sim.run(eng, s.id)
    assert r["outcome"] == "stopped" and r["expected"] == "stopped" and r["llm"]["used"] is False
    # if the expectation is wrong, the summary says so instead of reporting success
    fake = sim._summarize(s, "adaptix", {"status": "stopped", "incident": None}, SimpleNamespace(state=None), "recovered", eng)
    assert fake["matches_expectation"] is False and fake["outcome"] == "stopped"


def test_llm_involvement_is_labelled(make_engine):
    eng = make_engine(llm=MockProvider())
    r = sim.run(eng, "ambiguous-rename")
    assert r["llm"]["label"] == "MOCK (not a real model)" and r["llm"]["real"] is False
    assert sim.llm_label({"provider": "gemini", "real": True, "ok": True})["label"] == "REAL Gemini request"
    assert sim.llm_label(None) == {"used": False, "label": "not used"}
