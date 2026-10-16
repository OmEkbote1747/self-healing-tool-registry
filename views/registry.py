import streamlit as st

from adaptix.healer import ConflictError
from views.common import LOG, footer, page_header


def render():
    page_header("Console · Registry & review queue", "Every repair is versioned, reviewed and logged",
                "Automatic repairs start as a canary used only by the agent that made them, and are shared after "
                "they succeed a few times. Ambiguous repairs wait here for a human. Any version can be rolled back.")
    demo = st.session_state.get("demo")
    if not demo:
        st.info("Run the live demo first to see a tool get repaired, reviewed and versioned.", icon=":material/info:")
        footer()
        return
    registry = demo["registry"]

    st.subheader("Review queue")
    pending = registry.pending()
    if not pending:
        st.caption("No repairs waiting for approval.")
    for p in pending:
        with st.container(border=True):
            st.markdown(f"**Proposal `{p.id}`** for `{p.tool}` (based on v{p.base_version})")
            for r in p.reasons:
                st.markdown(f"- {r}")
            st.json(p.candidate.describe())
            a, b = st.columns(2)
            if a.button("Approve", key=f"ap-{p.id}", type="primary", icon=":material/check:"):
                try:
                    registry.approve(p.id, by="reviewer")
                except ConflictError as e:
                    st.error(str(e))
                st.rerun()
            if b.button("Reject", key=f"rj-{p.id}", icon=":material/close:"):
                registry.reject(p.id, by="reviewer")
                st.rerun()

    st.subheader("Tool versions")
    for name, versions in registry.versions.items():
        with st.container(border=True):
            st.markdown(f"#### Tool `{name}` · {len(versions)} version(s)")
            cols = st.columns(min(len(versions), 4))
            for col, v in zip(cols * 10, versions):
                with col:
                    current = v.version == registry.current(name).version
                    st.markdown(f"**v{v.version}** · {v.status}" + (" · *current*" if current else ""))
                    st.json(v.describe())
            live = [v for v in versions if v.status in ("active", "canary")]
            if len(live) > 1 and st.button(f"Roll back {name} to v{live[-2].version}", icon=":material/undo:"):
                registry.rollback(name)
                st.rerun()
    if hasattr(registry, "events"):
        st.subheader("Registry events (durable, this session's tenant only)")
        st.caption(f"Tenant `{registry.tenant}` · stored in SQLite with atomic, versioned writes")
        st.dataframe(registry.events(50), hide_index=True, use_container_width=True)
    if LOG.exists():
        lines = LOG.read_text().strip().splitlines()[-20:]
        with st.expander(f"Patch audit log (JSONL) · last {len(lines)} entries", icon=":material/receipt_long:"):
            st.code("\n".join(lines), language="json")
    footer()
