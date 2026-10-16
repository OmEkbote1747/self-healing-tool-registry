import streamlit as st

from views.common import SIM_BADGE, api, badge, empty, go, header, safe_call


def render(prefix: str = "/api"):
    sim = prefix == "/api/sim"
    header("Simulation Lab · Tools" if sim else "Tool Registry", "Registered tools and their versions",
           "Every tool keeps its full version history. A repair creates a new version and keeps the previous one, "
           "so any change can be rolled back." + (" These are SIMULATED tools." if sim else
                                                  " Register real APIs on the Real API Monitoring page."))
    tools = safe_call(api, "GET", f"{prefix}/tools")
    if tools is None:
        return
    if not tools:
        empty("No tools yet." if sim else "No real API is registered yet.",
              "Open the Simulation Lab" if sim else "Open Real API Monitoring", "chaos" if sim else "monitoring")
        return
    rows = tools
    st.dataframe([{"tool": t["name"], "simulated": t["simulated"], "health": t["health"], "version": t["current_version"],
                   "provider": t.get("provider") or "", "endpoint": f"{t['method']} {t['base_url'].rstrip('/')}{t['path']}", "last success": (t["last_success_at"] or "")[:19],
                   "last failure": (t["last_failure_at"] or "")[:19], "drift incidents": t["drift_incidents"],
                   "versions": t["versions"]} for t in rows], hide_index=True, use_container_width=True)
    names = [t["name"] for t in rows]
    if not names:
        return
    sel = st.selectbox("Tool", names, index=names.index(st.session_state["tool"]) if st.session_state.get("tool") in names else 0)
    t = safe_call(api, "GET", f"{prefix}/tools/{sel}")
    vers = safe_call(api, "GET", f"{prefix}/tools/{sel}/versions")
    if not t or vers is None:
        return
    st.markdown(f"### `{sel}` &nbsp; {badge(t['health'])} " + (SIM_BADGE if t["simulated"] else ""), unsafe_allow_html=True)
    st.caption(f"{t['method']} {t['base_url']}{t['path']} · current v{t['current_version']} · schema {t['schema_signature']}")
    a1, a2, a3 = st.columns(3)
    if a1.button("Inspect schema drift", icon=":material/difference:"):
        go("sim_drift" if sim else "drift", tool=sel)
    can_rollback = len([v for v in vers if v["status"] in ("active", "superseded")]) > 1 and t["current_version"] > 1
    if a2.button("Roll back current version", icon=":material/undo:", disabled=not can_rollback,
                 help="Restores the parent of the current version"):
        res = safe_call(api, "POST", f"{prefix}/tools/{sel}/rollback")
        if res:
            st.success(f"Rolled back to v{res['version']}")
            st.rerun()
    if a3.button("Delete tool", icon=":material/delete:"):
        st.session_state["confirm_delete"] = sel
    if st.session_state.get("confirm_delete") == sel:
        st.warning(f"Delete `{sel}` and its version history? Incidents and audit events are kept.")
        if st.button("Confirm delete", type="primary"):
            if safe_call(api, "DELETE", f"{prefix}/tools/{sel}"):
                st.session_state.pop("confirm_delete", None)
                st.rerun()
    c1, c2 = st.columns([1, 1.2], gap="large")
    with c1:
        st.markdown("**Arguments (caller-facing)**")
        st.json(t["arguments"], expanded=False)
        if t["dependents"]:
            st.markdown("**May be affected by a change to this endpoint**")
            st.write(", ".join(f"`{d}`" for d in t["dependents"]))
    with c2:
        st.markdown("**Version history**")
        for v in reversed(vers):
            with st.container(border=True):
                st.markdown(f"**v{v['version']}** · {v['status']} · {v['origin']}"
                            + (f" · from v{v['parent_version']}" if v["parent_version"] else ""))
                if v["patch"]:
                    st.caption("; ".join(f"{p['op']}: {p.get('argument') or p.get('header') or p.get('output')} -> "
                                         f"{p.get('to') or p.get('credential_group')}" for p in v["patch"]))
                if v["incident_id"] and st.button("Open incident", key=f"vinc-{v['version']}"):
                    go("sim_records" if sim else "recovery", incident_id=v["incident_id"])
