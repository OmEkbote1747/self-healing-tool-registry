import pandas as pd
import streamlit as st

from views.common import SIM_BADGE, api, badge, empty, fmt_ms, fmt_pct, go, header, kpis, safe_call


def render():
    header("ADAPTIX · Overview", "APIs change. ADAPTIX adapts.",
           "Autonomous API resilience: detects schema drift, repairs supported changes without human approval, "
           "verifies the task and rolls back repairs that fail. Unsafe or ambiguous changes stop with a clear reason.")
    health = safe_call(api, "GET", "/api/health")
    m = safe_call(api, "GET", "/api/metrics")
    tools = safe_call(api, "GET", "/api/tools")
    if health is None or m is None or tools is None:
        return
    llm = health["llm"]
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"**Service** &nbsp; {badge('healthy' if health['status'] == 'ok' else 'degraded')}", unsafe_allow_html=True)
    c2.markdown(f"**Database** &nbsp; {badge('healthy' if health['database'] == 'ok' else 'degraded')}", unsafe_allow_html=True)
    c3.markdown(f"**LLM** &nbsp; `{llm['provider']}` · {llm['connectivity'].replace('_', ' ')}")
    c4.markdown(f"**Uptime** &nbsp; {health['uptime_s'] // 60} min")

    sample = m["sample"]
    all_sim = sample["incidents"] and sample["simulated_incidents"] == sample["incidents"]
    if all_sim:
        st.markdown(f"{SIM_BADGE} &nbsp; All incidents below come from Chaos Lab simulations, not production traffic.",
                    unsafe_allow_html=True)
    kpis([
        ("Registered tools", len(tools), f"{sum(t['simulated'] for t in tools)} simulated"),
        ("Detected incidents", sample["incidents"], f"{m['schema_drift_incidents']} schema drift"),
        ("Autonomous repairs", m["recovered"], f"zero-touch rate {fmt_pct(m['zero_touch_adaptation_rate'])}"),
        ("Median recovery", fmt_ms(m["median_recovery_ms"]), f"{fmt_pct(m['under_4s_rate'])} under 4 s"),
    ])
    kpis([
        ("Stopped safely", m["stopped_safely"], "ambiguous, unsafe or not schema drift"),
        ("Failed recoveries", m["failed_recoveries"], "includes rolled-back repairs"),
        ("Rollbacks", m["rollbacks"], f"rollback success {fmt_pct(m['rollback_success_rate'])}"),
        ("Unsafe patches rejected", fmt_pct(m["unsafe_patch_rejection_rate"]), "of plans that reached validation"),
    ])
    if not sample["incidents"]:
        empty("No incidents recorded yet. Run a scenario in the Chaos Lab, or register a real tool in the Tool "
              "Registry and call it through the API.", "Open the Chaos Lab", "chaos")
        return
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        st.subheader("Incidents by outcome")
        st.bar_chart(pd.DataFrame({"incidents": m["incidents_by_status"]}), horizontal=True, height=220, color="#2563EB")
        st.subheader("By failure category")
        st.bar_chart(pd.DataFrame({"incidents": m["incidents_by_category"]}), horizontal=True, height=260, color="#0B1B36")
    with right:
        st.subheader("Recent incidents")
        incs = safe_call(api, "GET", "/api/incidents", params={"limit": 8}) or []
        for i in incs:
            with st.container(border=True):
                st.markdown(f"{badge(i['status'])} &nbsp; **{i['tool']}** &nbsp; "
                            + (SIM_BADGE if i["simulated"] else ""), unsafe_allow_html=True)
                st.caption(f"{i['category'].replace('_', ' ')} · {fmt_ms(i['duration_ms'])} · {i['started_at'][:19]}Z")
                if st.button("Open timeline", key=f"ov-{i['id']}", icon=":material/timeline:"):
                    go("recovery", incident_id=i["id"])
    st.subheader("Recent audit events")
    ev = safe_call(api, "GET", "/api/audit", params={"limit": 15}) or []
    st.dataframe([{"time": e["ts"][:19], "event": e["kind"], "tool": e["tool"], "simulated": e["simulated"],
                   "correlation": (e["correlation_id"] or "")[:10]} for e in ev], hide_index=True, use_container_width=True)
