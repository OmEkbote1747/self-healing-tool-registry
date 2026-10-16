"""Version history, rollback and deletion for one registered tool (used inside API Registry)."""
import streamlit as st

from views.common import api, go, mutated, safe_call


def versions_panel(prefix: str, name: str, current_version: int):
    vers = safe_call(api, "GET", f"{prefix}/tools/{name}/versions")
    if vers is None:
        return
    for v in reversed(vers):
        line = f"**v{v['version']}** · {v['status']} · {v['origin']}" + (f" · from v{v['parent_version']}"
                                                                        if v["parent_version"] else "")
        if v["patch"]:
            line += " · " + "; ".join(f"{p.get('argument') or p.get('header') or p.get('output')}: "
                                      f"{p.get('from') or '—'} -> {p.get('to') or p.get('credential_group') or '—'}"
                                      for p in v["patch"])
        st.markdown(line)
        if v["incident_id"] and st.button("Open incident", key=f"vinc-{name}-{v['version']}"):
            go("recovery", incident_id=v["incident_id"], env_choice="REAL API")
    c1, c2 = st.columns(2)
    if c1.button("Roll back current version", key=f"rb-{name}", icon=":material/undo:", disabled=current_version <= 1,
                 help="Restores the parent of the current version"):
        res = safe_call(api, "POST", f"{prefix}/tools/{name}/rollback")
        if res:
            mutated()
            st.success(f"Rolled back to v{res['version']}")
            st.rerun()
    if c2.checkbox("Confirm delete", key=f"delok-{name}") and c2.button("Delete tool", key=f"del-{name}",
                                                                        icon=":material/delete:"):
        if safe_call(api, "DELETE", f"{prefix}/tools/{name}"):
            mutated()
            st.rerun()
