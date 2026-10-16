"""LIVE Gemini test - real network requests. Skipped unless GEMINI_API_KEY is set.

    GEMINI_API_KEY=... GEMINI_MODEL=gemini-3.5-flash-lite pytest -q tests/test_gemini_live.py
"""
import os

import pytest

from adaptix import simulation as sim
from adaptix.config import get_settings
from adaptix.credentials import CredentialRegistry
from adaptix.llm.gemini import GeminiProvider

pytestmark = pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="live test: GEMINI_API_KEY not set")


def _provider():
    s = get_settings()
    return GeminiProvider(CredentialRegistry.from_env(), s.gemini_model, s.llm_timeout_s, s.llm_max_retries)


def test_live_connectivity_check():
    p = _provider()
    r = p.check()
    assert r.real and r.ok, r.meta()
    assert p.status()["connectivity"] == "verified"


def test_live_structured_repair_plan(make_engine):
    eng = make_engine(llm=_provider())
    r = sim.run(eng, "window-rename-llm")
    inc = eng.storage.incident(r["incident"])
    assert inc.llm and inc.llm["real"] and inc.llm["provider"] == "gemini"
    # the model's plan is still subject to validation: either it was grounded and the task verified, or it stopped
    assert r["outcome"] in ("recovered", "stopped")


def test_live_end_to_end_recovery_on_the_controlled_service(tmp_path):
    """REAL Gemini + the controlled test service over real HTTP: customer_id -> client_id."""
    from adaptix import realtest
    from adaptix.api import State
    from adaptix.config import Settings
    from adaptix.storage import Storage
    s = get_settings()
    st = State(Settings(admin_token="live-demo-token", gemini_model=s.gemini_model), storage=Storage(tmp_path / "p.db"),
               start_monitor=False)
    try:
        r = realtest.run_orders_demo(st)
        assert r["outcome"] == "recovered" and r["verified_recovery"], r
        ev = st.test_storage.evidence_one(r["evidence_id"])
        assert ev["llm"]["real_request_performed"] and ev["llm"]["provider"] == "gemini"
        assert ev["verification"]["business_outcome"]["ok"] and ev["applied_change"]["to_version"] == 2
    finally:
        if st.test_service:
            st.test_service.stop()
