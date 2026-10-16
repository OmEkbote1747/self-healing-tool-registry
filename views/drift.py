"""Schema Drift Explorer: one selected tool at a time - registered schema vs the live authoritative schema."""
import streamlit as st

from views.common import api, empty, env_picker, ENV_PREFIX, header, ist, safe_call, schema_state_chip, schema_unavailable

KIND = {"added": "Added", "removed": "Removed", "renamed": "Renamed", "moved": "Moved", "type_changed": "Type changed",
        "required_changed": "Required ↔ optional", "enum_changed": "Allowed values changed",
        "format_changed": "Format changed", "header_added": "Header now required", "header_removed": "Header removed"}


def implication(c: dict) -> str:
    """What ADAPTIX will do about this kind of change (how it is repaired, not why it happened)."""
    k = c["kind"]
    if k in ("renamed", "moved"):
        return "Repairable: the caller's value is re-mapped if the schema documents the rename or the names clearly match"
    if k == "added":
        return ("Needs a value: repaired only if an existing caller argument maps to it, otherwise ADAPTIX stops"
                if c.get("detail") == "required" else "Optional: no repair needed")
    if k == "removed":
        return "Not repairable automatically: a value the caller sends would be dropped, so ADAPTIX stops"
    if k == "type_changed":
        return "Repairable only with a lossless conversion (e.g. \"3\" -> 3); otherwise ADAPTIX stops"
    if k == "required_changed":
        return "Repairable only if the caller supplies the field"
    if k == "header_added":
        return "Repairable only from a configured, authorized credential; never guessed"
    return "Checked by the validator before any repair"


def _changes(rep: dict):
    rows = [{"change": KIND.get(c["kind"], c["kind"]), "field": c["location"], "registered": c.get("old") or "—",
             "live": c.get("new") or "—", "what ADAPTIX does": implication(c)}
            for c in rep["request_changes"] + rep["response_changes"]]
    st.dataframe(rows, hide_index=True, use_container_width=True,
                 column_config={"what ADAPTIX does": st.column_config.TextColumn(width="large"),
                                "field": st.column_config.TextColumn(width="medium")})


def _side(title: str, side: dict | None, other: dict | None):
    st.markdown(f"**{title}**")
    if side is None:
        st.caption("not available")
        return
    rows = []
    for k, label in {**side["fields"], **side["response"]}.items():
        o = (other or {}).get("fields", {}) | (other or {}).get("response", {})
        state = "same" if o.get(k) == label else ("changed" if k in o else "only here")
        rows.append({"field": k, "type": label, "state": state})
    rows += [{"field": f"header:{h}", "type": "required", "state": "same" if h in (other or {}).get("headers", [])
              else "only here"} for h in side["headers"]]
    st.dataframe(rows, hide_index=True, use_container_width=True)


def render():
    header("Schema Drift Explorer", "What changed for one tool, and what ADAPTIX does about it",
           "Choose a tool. ADAPTIX reads its live authoritative schema and compares it with the schema the tool was "
           "registered with. Each difference is explained together with how it would be repaired.")
    env = env_picker("drift_env")
    prefix = ENV_PREFIX[env]
    tools = safe_call(api, "GET", f"{prefix}/tools")
    if tools is None:
        return
    if not tools:
        empty("No tools in this environment yet. Register a real API in API Registry, or run the Drift Demo.",
              "Open API Registry", "monitoring")
        return
    names = [t["name"] for t in tools]
    want = st.session_state.get("tool")
    sel = st.selectbox("Tool", names, index=names.index(want) if want in names else 0, key=f"drift_tool_{env}")
    st.session_state["tool"] = sel
    t = next(x for x in tools if x["name"] == sel)
    st.caption(f"{t['method']} {t['base_url'].rstrip('/')}{t['path']} · tool v{t['current_version']} · "
               f"schema source: {t.get('schema_source', 'openapi')}")
    st.subheader("Live comparison")
    with st.spinner("Reading the live authoritative schema…"):
        d = safe_call(api, "GET", f"{prefix}/tools/{sel}/drift")
    if d is None:
        return
    if not d["available"]:
        if d.get("state") == "unsupported":
            st.markdown(f"{schema_state_chip('unsupported')} &nbsp; **{sel}**", unsafe_allow_html=True)
            st.caption("This provider publishes no machine-readable schema, so drift cannot be verified and the tool "
                       "is never repaired automatically.")
        elif schema_unavailable(d, sel, f"retry-{env}-{sel}"):
            st.rerun()
        with st.expander("Registered schema (what the tool was built against, not the live schema)"):
            _side(f"Registered (tool v{d['current_version']})", d.get("registered"), None)
    else:
        rep = d["report"]
        st.markdown(f"{schema_state_chip(d['state'])} &nbsp; **{sel}** · read from `{d.get('schema_url')}`",
                    unsafe_allow_html=True)
        if not rep["changed"]:
            st.success(f"No schema drift detected: the live schema matches tool v{d['current_version']}. "
                       "Nothing needs repair.", icon=":material/check_circle:")
        else:
            st.warning(f"{rep['summary']} · compatibility **{rep['compatibility']}**", icon=":material/difference:")
            _changes(rep)
        with st.expander("Registered vs live schema, field by field", expanded=rep["changed"]):
            c1, c2 = st.columns(2, gap="large")
            with c1:
                _side(f"Registered (tool v{d['current_version']})", d["registered"], d["live"])
            with c2:
                _side("Live (authoritative)", d["live"], d["registered"])
            st.caption(f"Fingerprints: registered `{rep['old_signature']}` · live `{rep['new_signature']}`")
    incs = [i for i in (safe_call(api, "GET", f"{prefix}/incidents", params={"tool": sel, "limit": 20}) or [])
            if i.get("drift")]
    st.subheader("Drift incidents for this tool")
    if not incs:
        st.caption("None recorded for this tool.")
        return
    labels = {i["id"]: f"{ist(i['started_at'])} · {i['status']} · {i['drift']['summary']}" for i in incs}
    pick = st.selectbox("Incident", list(labels), format_func=labels.get, key=f"drift_inc_{env}_{sel}")
    i = next(x for x in incs if x["id"] == pick)
    _changes(i["drift"])
    if i.get("plan"):
        st.markdown(f"**Selected repair** (planner `{i['plan']['source']}`)")
        st.dataframe([{"new field": m["target_field"], "from argument": m["source_arg"], "transform": m["transform"],
                       "evidence": m["evidence"]} for m in i["plan"]["mappings"] if m.get("source_arg")],
                     hide_index=True, use_container_width=True)
    if i.get("validation"):
        st.markdown("**Validation:** " + ("accepted" if i["validation"]["accepted"] else
                                          "rejected: " + "; ".join(i["validation"].get("rejected_reasons", []))[:300]))
