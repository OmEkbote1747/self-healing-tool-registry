"""ADAPTIX dashboard. Every page talks to the ADAPTIX API (in-process by default, or ADAPTIX_API_URL)."""
import streamlit as st

from views import audit, chaos, drift, health, overview, recovery, tools
from views.common import ROOT, api, api_mode, inject_css, safe_call

REPO_URL = "https://github.com/Paras131206/self-healing-tool-registry"

st.set_page_config(page_title="ADAPTIX · Autonomous API Resilience", page_icon=":material/autorenew:",
                   layout="wide", initial_sidebar_state="expanded")
inject_css()
st.logo(str(ROOT / "assets" / "logo.svg"), size="large", link=REPO_URL)

RENDER = {"overview": overview.render, "tools": tools.render, "drift": drift.render, "recovery": recovery.render,
          "audit": audit.render, "chaos": chaos.render, "health": health.render}
pages = {
    "overview": st.Page(overview.render, title="Overview", icon=":material/dashboard:", url_path="overview", default=True),
    "tools": st.Page(tools.render, title="Tool Registry", icon=":material/inventory_2:", url_path="tools"),
    "drift": st.Page(drift.render, title="Schema Drift Explorer", icon=":material/difference:", url_path="drift"),
    "recovery": st.Page(recovery.render, title="Recovery Center", icon=":material/healing:", url_path="recovery"),
    "audit": st.Page(audit.render, title="Audit & Evaluation", icon=":material/fact_check:", url_path="audit"),
    "chaos": st.Page(chaos.render, title="Chaos Lab", icon=":material/science:", url_path="chaos-lab"),
    "health": st.Page(health.render, title="System Health", icon=":material/monitor_heart:", url_path="health"),
}
st.session_state["_pages"] = pages
nav = st.navigation({"Monitor": [pages["overview"], pages["tools"], pages["drift"], pages["recovery"]],
                     "Evidence": [pages["audit"], pages["chaos"]], "System": [pages["health"]]})

with st.sidebar:
    h = safe_call(api, "GET", "/api/health")
    if h:
        llm = h["llm"]
        st.caption(f"API: {api_mode()} · v{h['version']}")
        st.caption(f"LLM: {llm['provider']} · {llm['connectivity'].replace('_', ' ')}")
        if h["simulated_tools"]:
            st.caption(f"{h['simulated_tools']} simulated tool(s) registered (Chaos Lab)")
    st.caption("Team Runtime Terrors · PS-01")

# tests can render a page directly; visitors always go through the navigation
if "_force_page" in st.session_state:
    RENDER[st.session_state["_force_page"]]()
else:
    nav.run()
