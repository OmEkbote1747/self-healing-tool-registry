"""SchemaHeal: Streamlit front end for the PS-01 self-healing tool registry prototype."""
import streamlit as st

from selfheal import llm
from selfheal.cases import APPROVED_CONFIG
from views import api, benchmark, demo, home, how, registry, stress
from selfheal.security import purge_audit_log
from views.common import LOG, REPO_URL, ROOT, inject_css
import os

st.set_page_config(page_title="SchemaHeal · Self-Healing Tool Registry", page_icon=":material/healing:",
                   layout="wide", initial_sidebar_state="expanded")
inject_css()


@st.cache_resource
def _apply_retention():
    """Once per server process: drop audit entries past the retention window."""
    return purge_audit_log(LOG, int(os.environ.get("SCHEMAHEAL_RETENTION_DAYS", "30")))


_apply_retention()
st.logo(str(ROOT / "assets" / "logo.svg"), size="large", link=REPO_URL)

pages = {
    "home": st.Page(home.render, title="Home", icon=":material/home:", url_path="home", default=True),
    "how": st.Page(how.render, title="How it works", icon=":material/account_tree:", url_path="how-it-works"),
    "api": st.Page(api.render, title="API & integration", icon=":material/api:", url_path="api"),
    "demo": st.Page(demo.render, title="Live demo", icon=":material/play_circle:", url_path="demo"),
    "bench": st.Page(benchmark.render, title="Benchmark (125 cases)", icon=":material/monitoring:", url_path="benchmark"),
    "stress": st.Page(stress.render, title="Stress tests", icon=":material/science:", url_path="stress-tests"),
    "registry": st.Page(registry.render, title="Registry & audit log", icon=":material/inventory_2:", url_path="registry"),
}
st.session_state["_pages"] = pages
nav = st.navigation({"Overview": [pages["home"], pages["how"], pages["api"]],
                     "Console": [pages["demo"], pages["bench"], pages["stress"], pages["registry"]]})

with st.sidebar:
    st.divider()
    llm_ok = llm.available()
    st.session_state["use_llm"] = st.toggle("Tier 2: LLM repair proposals", value=llm_ok, disabled=not llm_ok,
                                            help="Needs ANTHROPIC_API_KEY. When off, anything the rules are unsure "
                                                 "about stops safely instead of being guessed.")
    if llm_ok:
        st.success(f"LLM tier on · {llm.MODEL}", icon=":material/smart_toy:")
    else:
        st.info("LLM tier off (no API key). Running rules + validator.", icon=":material/rule:")
    with st.expander("Safety rules", icon=":material/verified_user:", expanded=True):
        st.markdown("- Never invent a value\n- Validate before retry\n- Max 2 repair attempts\n"
                    "- Headers only from approved config")
        st.caption("Approved config keys: " + ", ".join(APPROVED_CONFIG))
    with st.expander("Data handling", icon=":material/lock:"):
        st.markdown("- Header values are never shown or logged\n- Sensitive fields are masked\n"
                    "- Values echoed in API errors are masked\n- The LLM sees field names and types only\n"
                    "- Secrets come from the environment, not code\n- Audit log kept "
                    f"{os.environ.get('SCHEMAHEAL_RETENTION_DAYS', '30')} days")
    st.caption("PS-01 · Team Runtime Terrors · Genesis 2.0")

# tests can render a specific page directly; normal visitors always go through the navigation
if "_force_page" in st.session_state:
    {"home": home.render, "how": how.render, "api": api.render, "demo": demo.render, "bench": benchmark.render,
     "stress": stress.render, "registry": registry.render}[st.session_state["_force_page"]]()
else:
    nav.run()
