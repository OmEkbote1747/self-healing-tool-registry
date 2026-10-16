"""Dashboard tests: navigation, page structure and the main workflows, against the in-process API."""
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["overview", "monitoring", "drift", "recovery", "evidence", "demo", "chaos", "health"]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTIX_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("ADAPTIX_SIM_DB_PATH", str(tmp_path / "ui-sim.db"))
    monkeypatch.setenv("ADAPTIX_TEST_DB_PATH", str(tmp_path / "ui-test.db"))
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
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=timeout)
    at.session_state["_force_page"] = name
    return at.run()


def _text(at) -> str:
    parts = []
    for kind in ("markdown", "caption", "success", "warning", "info", "error", "subheader", "title"):
        parts += [getattr(e, "value", "") or "" for e in getattr(at, kind)]
    return "\n".join(str(p) for p in parts)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_empty(page):
    at = _page(page)
    assert not at.exception, at.exception


def test_navigation_titles():
    src = (ROOT / "app.py").read_text()
    titles = re.findall(r'P\([^)]*?title="([^"]+)"', src)
    assert titles == ["Overview", "API Registry", "Schema Drift Explorer", "Recovery Center", "Evidence & Scorecard",
                      "Drift Demo", "Simulation Lab", "System Health"]
    every_view = "\n".join(p.read_text() for p in (ROOT / "views").glob("*.py")) + src
    for gone in ("Real API Monitoring", "Live Project Integration", "Production Audit", "Schema Changes",
                 "Tool Registry", "All changes", "Baseline (no repair)", "livedb", "run_limit", "max_runs",
                 "Automatic injection", "STOP NOW"):        # schema-check intervals in API Registry are monitoring, not injection
        assert gone not in every_view, gone


def test_drift_explorer_shows_only_the_selected_tool():
    at = _page("chaos")
    at.checkbox(key="sim_confirm").check().run()
    at.button[0].click().run()                                   # rename-param
    at.selectbox[1].set_value(next(o for o in at.selectbox[1].options if "nested object" in o.lower())).run()
    at.button[0].click().run()                                   # a second scenario -> a second tool
    at.session_state["_force_page"] = "drift"
    at.session_state["env_choice"] = "SIMULATION"
    at.run()
    assert not at.exception, at.exception
    assert not at.tabs                                           # no tabs any more (All changes removed)
    tool_box = next(s for s in at.selectbox if s.label == "Tool")
    assert len(tool_box.options) == 2
    for opt in tool_box.options:
        tool_box.set_value(opt).run()
        text = _text(at)
        other = next(o for o in tool_box.options if o != opt)
        assert opt in text and other not in text                 # nothing from the other tool is shown
        assert "No schema drift detected" in text                # repaired tool now matches the live schema


def test_simulation_lab_needs_confirmation_and_shows_no_baseline_card():
    at = _page("chaos")
    btn = next(b for b in at.button if b.label == "Introduce change")
    assert btn.disabled
    at.checkbox(key="sim_confirm").check().run()
    next(b for b in at.button if b.label == "Introduce change").click().run()
    assert not at.exception, at.exception
    runs = at.session_state["chaos_last"][1]
    assert len(runs) == 1 and runs[0]["mode"] == "adaptix"       # only the ADAPTIX result
    text = _text(at)
    assert "ADAPTIX recovery" in text and "SIMULATED" in text and "Baseline (no repair)" not in text
    result = [m.value for m in at.markdown if "ADAPTIX recovery" in m.value]
    assert len(result) == 1 and "Baseline" not in result[0]   # one result, no baseline card beside it


def test_drift_demo_introduce_change_recovers_automatically_and_syncs_every_page():
    at = _page("demo", timeout=120)
    assert next(b for b in at.button if b.label == "Introduce change").disabled       # needs confirmation
    at.checkbox(key="demo_confirm").check().run()
    next(b for b in at.button if b.label == "Introduce change").click().run()
    assert not at.exception, at.exception
    text = _text(at)
    assert "Recovered and verified" in text and "Change introduced" in text and "Baseline check" in text
    assert "REAL API TEST" in text and "Baseline (no repair)" not in text
    incident = at.session_state["demo_job"]
    # the same records drive every other page, without a reload of the app
    for page in ("evidence", "recovery", "drift", "overview"):
        at.session_state["_force_page"] = page
        at.session_state["env_choice"] = "REAL API TEST"
        at.run()
        assert not at.exception, (page, at.exception)
        t = _text(at)
        if page == "evidence":
            assert any("recovered" in str(s.options) for s in at.selectbox if s.label == "Incident")
            board = at.dataframe[0].value
            assert int(board.loc[board["stage"] == "Verified recoveries", "count"].iloc[0]) == 1
        if page == "drift":
            assert "No schema drift detected" in t                # registered schema now = live schema (v2)
    assert incident


def test_mutation_clears_derived_state():
    at = _page("chaos")
    at.session_state["rt_matrix"] = {"stale": True}          # derived copies that other pages would show
    at.session_state["demo_job"] = "demo_stale"
    at.session_state["chaos_last"] = ("some-other-scenario", [])
    at.checkbox(key="sim_reset_ok").check().run()
    next(b for b in at.button if b.label == "Reset simulation data").click().run()
    assert not at.exception, at.exception
    for k in ("rt_matrix", "demo_job", "chaos_last"):
        assert k not in at.session_state, k


def test_evidence_page_empty_state_and_scorecard_rows():
    at = _page("evidence")
    assert not at.exception, at.exception
    assert any("No incidents recorded" in i.value for i in at.info)
    stages = list(at.dataframe[0].value["stage"])
    assert stages[:6] == ["Incidents recorded", "Schema drift detected", "Repairs proposed", "Patches validated",
                          "Patches applied", "Verified recoveries"]
