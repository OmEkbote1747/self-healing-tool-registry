"""ADAPTIX dashboard. Every page talks to the ADAPTIX API (in-process by default, or ADAPTIX_API_URL).

Production pages read /api/* (real APIs only); Simulation Lab pages read /api/sim/* (a separate database)."""
import streamlit as st

from views import audit, chaos, drift, health, monitoring, overview, recovery, tools
from views.common import ROOT, api, api_mode, inject_css, safe_call

REPO_URL = "https://github.com/Paras131206/self-healing-tool-registry"

st.set_page_config(page_title="ADAPTIX · Autonomous API Resilience", page_icon=":material/autorenew:",
                   layout="wide", initial_sidebar_state="expanded")
inject_css()
st.logo(str(ROOT / "assets" / "logo.svg"), size="large", link=REPO_URL)


def sim_tools():
    tools.render("/api/sim")


def sim_drift():
    drift.render("/api/sim")


def sim_records():
    recovery.render("/api/sim")


def sim_evaluation():
    audit.render("/api/sim")


RENDER = {"overview": overview.render, "monitoring": monitoring.render, "tools": tools.render, "drift": drift.render,
          "recovery": recovery.render, "audit": audit.render, "chaos": chaos.render, "sim_records": sim_records,
          "sim_eval": sim_evaluation, "sim_tools": sim_tools, "sim_drift": sim_drift, "health": health.render}
P = st.Page
pages = {
    "overview": P(overview.render, title="Overview", icon=":material/dashboard:", default=True),
    "monitoring": P(monitoring.render, title="Real API Monitoring", icon=":material/monitor_heart:", url_path="monitoring"),
    "tools": P(tools.render, title="Tool Registry", icon=":material/inventory_2:", url_path="tools"),
    "drift": P(drift.render, title="Schema Drift Explorer", icon=":material/difference:", url_path="drift"),
    "recovery": P(recovery.render, title="Recovery Center", icon=":material/healing:", url_path="recovery"),
    "audit": P(audit.render, title="Production Audit", icon=":material/fact_check:", url_path="audit"),
    "chaos": P(chaos.render, title="Simulation Lab", icon=":material/science:", url_path="simulation-lab"),
    "sim_records": P(sim_records, title="Simulation Records", icon=":material/history:", url_path="simulation-records"),
    "sim_eval": P(sim_evaluation, title="Simulation Evaluation", icon=":material/query_stats:", url_path="simulation-evaluation"),
    "sim_tools": P(sim_tools, title="Simulated Tools", icon=":material/inventory:", url_path="simulation-tools"),
    "sim_drift": P(sim_drift, title="Simulated Drift", icon=":material/compare_arrows:", url_path="simulation-drift"),
    "health": P(health.render, title="Integration Health", icon=":material/verified:", url_path="integrations"),
}
st.session_state["_pages"] = pages
nav = st.navigation({
    "Production (real APIs)": [pages[k] for k in ("overview", "monitoring", "tools", "drift", "recovery", "audit")],
    "Simulation Lab (simulated)": [pages[k] for k in ("chaos", "sim_records", "sim_eval", "sim_tools", "sim_drift")],
    "System": [pages["health"]]})

with st.sidebar:
    h = safe_call(api, "GET", "/api/health")
    if h:
        llm = h["llm"]
        st.caption(f"API: {api_mode()} · v{h['version']}")
        st.caption(f"LLM: {llm['provider']} · {llm['connectivity'].replace('_', ' ')}")
        st.caption(f"Real APIs: {h['real_tools']} · approved hosts: {len(h['allowed_hosts'])}")
        st.caption(f"Simulated tools (separate DB): {h['simulated_tools']}")
    st.caption("Team Runtime Terrors · PS-01")

# tests can render a page directly; visitors always go through the navigation
if "_force_page" in st.session_state:
    RENDER[st.session_state["_force_page"]]()
else:
    nav.run()
