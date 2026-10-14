import streamlit as st

from views.common import LOG, footer, page_header


def render():
    page_header("Console · Registry & audit log", "Every repair is versioned and logged",
                "Accepted repairs become a new tool version, are shared with other agents, and are written to an "
                "append-only JSON Lines audit log. Any version can be rolled back.")
    demo = st.session_state.get("demo")
    if demo:
        registry = demo[4]
        for name, versions in registry.versions.items():
            with st.container(border=True):
                st.markdown(f"#### Tool `{name}` · {len(versions)} version(s)")
                cols = st.columns(len(versions))
                for col, v in zip(cols, versions):
                    with col:
                        st.markdown(f"**v{v.version}**" + (" (current)" if v is versions[-1] else ""))
                        st.json(v.describe())
                if len(versions) > 1 and st.button(f"Roll back {name} to v{versions[-2].version}",
                                                   icon=":material/undo:"):
                    registry.rollback(name)
                    st.rerun()
    else:
        st.info("Run the live demo first to see a tool get repaired and versioned.", icon=":material/info:")
    if LOG.exists():
        lines = LOG.read_text().strip().splitlines()[-20:]
        st.subheader(f"Audit log · last {len(lines)} entries")
        st.code("\n".join(lines), language="json")
    footer()
