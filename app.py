"""ADAPTIX dashboard. Every page talks to the ADAPTIX API (in-process by default, or ADAPTIX_API_URL).

Three separate environments, each with its own database: REAL API (/api, real providers), REAL API TEST
(/api/test, the controlled demo service over real HTTP) and SIMULATION (/api/sim). Pages that show records
offer one shared environment selector."""
import streamlit as st

from views import chaos, demo, drift, evidence, health, monitoring, overview, recovery
from views.common import ROOT, api, api_mode, inject_css, safe_call

REPO_URL = "https://github.com/Paras131206/self-healing-tool-registry"

st.set_page_config(page_title="ADAPTIX · Autonomous API Resilience", page_icon=":material/autorenew:",
                   layout="wide", initial_sidebar_state="expanded")
inject_css()
st.logo(str(ROOT / "assets" / "logo.svg"), size="large", link=REPO_URL)


def recovery_center():
    recovery.render()


RENDER = {"overview": overview.render, "monitoring": monitoring.render, "drift": drift.render,
          "recovery": recovery_center, "evidence": evidence.render, "demo": demo.render, "chaos": chaos.render,
          "health": health.render}
P = st.Page
pages = {
    "overview": P(overview.render, title="Overview", icon=":material/dashboard:", default=True),
    "monitoring": P(monitoring.render, title="API Registry", icon=":material/hub:", url_path="api-registry"),
    "drift": P(drift.render, title="Schema Drift Explorer", icon=":material/difference:", url_path="drift"),
    "recovery": P(recovery_center, title="Recovery Center", icon=":material/healing:", url_path="recovery"),
    "evidence": P(evidence.render, title="Evidence & Scorecard", icon=":material/gavel:", url_path="evidence"),
    "demo": P(demo.render, title="Drift Demo", icon=":material/bolt:", url_path="drift-demo"),
    "chaos": P(chaos.render, title="Simulation Lab", icon=":material/science:", url_path="simulation-lab"),
    "health": P(health.render, title="System Health", icon=":material/verified:", url_path="integrations"),
}
st.session_state["_pages"] = pages
nav = st.navigation({
    "Monitor and repair": [pages[k] for k in ("overview", "monitoring", "drift", "recovery", "evidence")],
    "Demonstrate (not production)": [pages["demo"], pages["chaos"]],
    "System": [pages["health"]]})

with st.sidebar:
    h = safe_call(api, "GET", "/api/health")
    if h:
        llm = h["llm"]
        st.caption(f"API: {api_mode()} · v{h['version']}")
        st.caption(f"LLM: {llm['provider']} · {llm['connectivity'].replace('_', ' ')}")
        st.caption(f"Real APIs: {h['real_tools']} · approved hosts: {len(h['allowed_hosts'])}")
    st.caption("Team Runtime Terrors · PS-01")

# tests can render a page directly; visitors always go through the navigation
if "_force_page" in st.session_state:
    RENDER[st.session_state["_force_page"]]()
else:
    nav.run()
