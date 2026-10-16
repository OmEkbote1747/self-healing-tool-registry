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
