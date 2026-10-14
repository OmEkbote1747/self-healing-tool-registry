"""SchemaHeal: Streamlit front end for the PS-01 self-healing tool registry prototype."""
import streamlit as st

from selfheal import llm
from selfheal.cases import APPROVED_CONFIG
from views import benchmark, demo, home, how, registry, stress
from views.common import REPO_URL, ROOT, inject_css

st.set_page_config(page_title="SchemaHeal · Self-Healing Tool Registry", page_icon=":material/healing:",
                   layout="wide", initial_sidebar_state="expanded")
inject_css()
st.logo(str(ROOT / "assets" / "logo.svg"), size="large", link=REPO_URL)

pages = {
    "home": st.Page(home.render, title="Home", icon=":material/home:", url_path="home", default=True),
    "how": st.Page(how.render, title="How it works", icon=":material/account_tree:", url_path="how-it-works"),
    "demo": st.Page(demo.render, title="Live demo", icon=":material/play_circle:", url_path="demo"),
    "bench": st.Page(benchmark.render, title="Benchmark (125 cases)", icon=":material/monitoring:", url_path="benchmark"),
    "stress": st.Page(stress.render, title="Stress tests", icon=":material/science:", url_path="stress-tests"),
    "registry": st.Page(registry.render, title="Registry & audit log", icon=":material/inventory_2:", url_path="registry"),
}
st.session_state["_pages"] = pages
nav = st.navigation({"Overview": [pages["home"], pages["how"]],
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
    st.caption("PS-01 · Team Runtime Terrors · Genesis 2.0")

nav.run()
