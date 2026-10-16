"""Smoke tests: every dashboard page renders against the in-process API without exceptions."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

PAGES = ["overview", "monitoring", "tools", "drift", "recovery", "audit", "chaos", "sim_records", "sim_eval",
         "sim_tools", "sim_drift", "health"]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTIX_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("ADAPTIX_SIM_DB_PATH", str(tmp_path / "ui-sim.db"))
    monkeypatch.setenv("ADAPTIX_MONITOR_ENABLED", "false")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from adaptix.config import get_settings
    get_settings.cache_clear()
    import streamlit as st
    st.cache_resource.clear()
    yield
    get_settings.cache_clear()
    st.cache_resource.clear()


def _page(name, timeout=60):
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=timeout)
    at.session_state["_force_page"] = name
    return at.run()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_empty(page):
    at = _page(page)
    assert not at.exception, at.exception


def test_chaos_run_then_pages_show_records():
    at = _page("chaos")
    at.button[0].click().run()          # "Run scenario" (baseline vs ADAPTIX)
    assert not at.exception, at.exception
    assert "chaos_last" in at.session_state and len(at.session_state["chaos_last"][1]) == 2
    assert any("SIMULATED" in m.value for m in at.markdown)
    for page in ["overview", "sim_records", "sim_eval", "sim_tools", "sim_drift"]:
        at.session_state["_force_page"] = page
        at.run()
        assert not at.exception, (page, at.exception)
