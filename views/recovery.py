import streamlit as st

from views.common import BASELINE_BADGE, SIM_BADGE, STAGE_ICON, api, badge, empty, fmt_ms, header, safe_call

DOT = {"ok": "#047857", "warn": "#B45309", "fail": "#B91C1C", "info": "#2563EB"}


def timeline(inc: dict):
    for s in inc["timeline"]:
        icon = STAGE_ICON.get(s["stage"], "radio_button_checked")
        st.markdown(f'<div class="ax-step" style="border-left-color:{DOT[s["status"]]}">'
                    f'<span class="ax-eyebrow" style="color:{DOT[s["status"]]}">{s["stage"]}</span> &nbsp; '
                    f'<b>{s["title"]}</b> <span style="color:#94A3B8;font-size:.8rem">{s["ts"][11:23]}</span></div>',
                    unsafe_allow_html=True)
        if s.get("detail"):
            with st.expander(f":material/{icon}: details", expanded=False):
                if isinstance(s["detail"], (dict, list)):
                    st.json(s["detail"], expanded=True)
                else:
                    st.write(s["detail"])


def render(prefix: str = "/api"):
    sim = prefix == "/api/sim"
    header("Simulation Records" if sim else "Recovery Center", "Every incident, step by step",
           "Request, classification, schema discovery, diff, plan, validation, apply, retry, verification and "
           "rollback, with the outcome and the reason. Correlation IDs link incidents to audit events.")
    if sim:
        st.markdown(f"{SIM_BADGE} &nbsp; Simulation database only.", unsafe_allow_html=True)
    c1, c3 = st.columns(2)
    status = c1.selectbox("Outcome", ["all", "recovered", "stopped", "failed", "rolled_back"])
    tool = c3.text_input("Tool contains")
    params = {"limit": 300}
    if status != "all":
        params["status"] = status
    incs = safe_call(api, "GET", f"{prefix}/incidents", params=params)
    if incs is None:
        return
    incs = [i for i in incs if tool.lower() in i["tool"].lower()]
    if not incs:
        empty("No incidents match. Incidents are created when a tool call fails; a successful call creates none.",
              "Open the Chaos Lab", "chaos")
        return
    ids = [i["id"] for i in incs]
    want = st.session_state.get("incident_id")
    labels = {i["id"]: f"{i['started_at'][:19]} · {i['tool']} · {i['status']} · {i['category']}"
              + ("" if i.get("healing", True) else " · baseline") for i in incs}
    sel = st.selectbox("Incident", ids, index=ids.index(want) if want in ids else 0, format_func=labels.get)
    inc = safe_call(api, "GET", f"{prefix}/incidents/{sel}")
    if not inc:
        return
    st.markdown(f"### {badge(inc['status'])} &nbsp; `{inc['tool']}` " + (SIM_BADGE if inc["simulated"] else "")
                + ("" if inc.get("healing", True) else " " + BASELINE_BADGE), unsafe_allow_html=True)
    st.markdown(f"**Reason:** {inc['reason'] or '—'}")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Category", inc["category"].replace("_", " "))
    m2.metric("Duration", fmt_ms(inc["duration_ms"]))
    m3.metric("Retries", inc["retries"])
    m4.metric("Repair attempts", inc["repair_attempts"])
    m5.metric("Version", f"v{inc['from_version']} → v{inc['to_version']}" if inc["to_version"] else
              (f"v{inc['from_version']}" if inc["from_version"] else "—"))
    st.caption(f"incident {inc['id']} · correlation {inc['correlation_id']}"
               + (f" · scenario {inc['scenario']}" if inc.get("scenario") else "")
               + (" · zero-touch" if inc["zero_touch"] else "") + (" · rolled back" if inc["rolled_back"] else ""))
    left, right = st.columns([1.4, 1], gap="large")
    with left:
        st.subheader("Timeline")
        timeline(inc)
    with right:
        if inc.get("verification"):
            v = inc["verification"]
            st.subheader("Task verification")
            (st.success if v.get("ok") else st.error)("Task verified" if v.get("ok") else "Task not verified")
            for c in v.get("checks", []):
                st.markdown(f"{'✅' if c.get('ok') else '❌'} {c.get('name')}"
                            + (f" · {c['detail']}" if c.get("detail") else ""))
            if v.get("contract_drift"):
                st.warning("Expected response fields are missing: possible response-schema drift.")
        if inc.get("llm"):
            l = inc["llm"]
            st.subheader("LLM call")
            st.caption("real provider request" if l.get("real") else "not a real provider request (mock or disabled)")
            st.json({k: l.get(k) for k in ("provider", "model", "ok", "error_category", "duration_ms",
                                           "prompt_tokens", "output_tokens", "attempts", "credential_alias")})
        if inc.get("result") is not None:
            st.subheader("Mapped result")
            st.json(inc["result"], expanded=False)
        events = safe_call(api, "GET", f"{prefix}/audit", params={"correlation_id": inc["correlation_id"], "limit": 50}) or []
        if events:
            st.subheader("Audit events")
            st.dataframe([{"time": e["ts"][11:19], "event": e["kind"]} for e in events], hide_index=True,
                         use_container_width=True)
