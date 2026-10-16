import streamlit as st

from views.common import SIM_BADGE, api, empty, header, safe_call

KIND = {"added": ("Added", "#047857"), "removed": ("Removed", "#B91C1C"), "renamed": ("Renamed", "#B45309"),
        "moved": ("Moved", "#B45309"), "type_changed": ("Type changed", "#6D28D9"),
        "required_changed": ("Required changed", "#6D28D9"), "enum_changed": ("Allowed values changed", "#6D28D9"),
        "format_changed": ("Format changed", "#6D28D9"), "header_added": ("Header added", "#047857"),
        "header_removed": ("Header removed", "#B91C1C")}
EXPLAIN = {"added": "A field the API now accepts{req}.", "removed": "The API no longer accepts this field.",
           "renamed": "Probably the same field under a new name ({detail}).",
           "moved": "The same field moved to a different place ({detail}).",
           "type_changed": "The value must now be {new} instead of {old}.",
           "required_changed": "Required: {old} -> {new}.", "enum_changed": "Allowed values: {old} -> {new}.",
           "format_changed": "Format: {old} -> {new}.", "header_added": "Requests must now send this header.",
           "header_removed": "The header is no longer required."}


def _changes_table(changes):
    rows = []
    for c in changes:
        label, _ = KIND.get(c["kind"], (c["kind"], "#334155"))
        rows.append({"change": label, "field": c["location"], "was": c.get("old") or "",
                     "now": c.get("new") or "", "explanation": EXPLAIN.get(c["kind"], "").format(
                         req=" (required)" if c.get("detail") == "required" else " (optional)",
                         detail=c.get("detail", ""), old=c.get("old"), new=c.get("new"))})
    return rows


def _side(title, side, other):
    st.markdown(f"**{title}**")
    if side is None:
        st.caption("not available")
        return
    rows = []
    for k, label in side["fields"].items():
        state = "unchanged" if other and k in other["fields"] and other["fields"][k] == label else \
            ("changed" if other and k in other["fields"] else "only here")
        rows.append({"field": k, "type": label, "state": state, "documentation": side["descriptions"].get(k, "")})
    for h in side["headers"]:
        rows.append({"field": f"header:{h}", "type": "required", "state": "unchanged" if other and h in other["headers"]
                     else "only here", "documentation": ""})
    for k, label in side["response"].items():
        rows.append({"field": k, "type": label, "state": "unchanged" if other and k in other["response"] else "only here",
                     "documentation": side["descriptions"].get(k, "")})
    st.dataframe(rows, hide_index=True, use_container_width=True,
                 column_config={"state": st.column_config.TextColumn(width="small")})


def render(prefix: str = "/api"):
    sim = prefix == "/api/sim"
    header("Simulation Lab · Drift" if sim else "Schema Drift Explorer", "What changed, field by field",
           "The registered schema next to the live authoritative schema, with each change explained. For an "
           "incident, the repair ADAPTIX selected and why.")
    tools = safe_call(api, "GET", f"{prefix}/tools")
    if tools is None:
        return
    if not tools:
        empty("No tools registered yet.", "Open the Simulation Lab" if sim else "Open Real API Monitoring",
              "chaos" if sim else "monitoring")
        return
    names = [t["name"] for t in tools]
    tab_live, tab_inc, tab_all = st.tabs(["Live comparison", "Drift in an incident", "All changes (every environment)"])
    with tab_all:
        from views.changes import render_table
        render_table(["SIMULATION"] if sim else None, key="drift-chg")
    with tab_live:
        sel = st.selectbox("Tool", names, index=names.index(st.session_state["tool"]) if st.session_state.get("tool") in names else 0)
        d = safe_call(api, "GET", f"{prefix}/tools/{sel}/drift")
        if d is None:
            return
        if not d["available"]:
            if d.get("status") == "unsupported":
                st.info("This provider publishes no machine-readable schema, so drift cannot be verified and the "
                        "tool is never repaired automatically.")
            else:
                st.warning(f"The authoritative schema could not be fetched ({d.get('error_category')}), so ADAPTIX "
                           "will not repair this tool (fail closed).")
        else:
            rep = d["report"]
            sim = next(t for t in tools if t["name"] == sel)["simulated"]
            st.markdown((SIM_BADGE + " &nbsp; " if sim else "") +
                        f"Compatibility: **{rep['compatibility']}** · {rep['summary']} · "
                        f"registered `{rep['old_signature']}` → live `{rep['new_signature']}`", unsafe_allow_html=True)
            if not rep["changed"]:
                st.success("The live schema matches the tool's current version. Nothing to repair.", icon=":material/check_circle:")
            c1, c2 = st.columns(2, gap="large")
            with c1:
                _side(f"Registered (v{d['current_version']})", d["registered"], d["live"])
            with c2:
                _side("Live (authoritative)", d["live"], d["registered"])
            if rep["changed"]:
                st.markdown("**Changes**")
                st.dataframe(_changes_table(rep["request_changes"] + rep["response_changes"]), hide_index=True,
                             use_container_width=True)
    with tab_inc:
        incs = [i for i in (safe_call(api, "GET", f"{prefix}/incidents", params={"limit": 300}) or []) if i.get("drift")]
        if not incs:
            st.info("No incident with a drift report yet.")
            return
        labels = {f"{i['started_at'][:19]} · {i['tool']} · {i['status']}": i for i in incs}
        i = labels[st.selectbox("Incident", list(labels))]
        rep = i["drift"]
        st.markdown(f"**{rep['summary']}** · compatibility {rep['compatibility']}")
        st.dataframe(_changes_table(rep["request_changes"] + rep["response_changes"]), hide_index=True,
                     use_container_width=True)
        if i.get("plan"):
            p = i["plan"]
            st.markdown(f"**Selected repair** · planner: `{p['source']}` · confidence {p['confidence']:.2f}")
            st.dataframe([{"new field": m["target_field"], "from argument": m["source_arg"], "transform": m["transform"],
                           "evidence": m["evidence"]} for m in p["mappings"]], hide_index=True, use_container_width=True)
            if p.get("unresolved"):
                st.warning("Unresolved: " + ", ".join(p["unresolved"]))
            for r in p.get("compatibility_risks", []):
                st.caption(f"Risk considered: {r}")
        if i.get("validation"):
            v = i["validation"]
            st.markdown("**Validation** · " + ("accepted" if v["accepted"] else "rejected"))
            st.dataframe([{"check": c["name"], "passed": c["ok"], "detail": c["detail"]} for c in v["checks"]],
                         hide_index=True, use_container_width=True)
