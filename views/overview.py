import pandas as pd
import streamlit as st

from views.common import REAL_BADGE, SIM_BADGE, api, badge, empty, fmt_ms, fmt_pct, go, header, kpis, safe_call


def render():
    header("ADAPTIX · Overview", "APIs change. ADAPTIX adapts.",
           "Autonomous API resilience for real APIs: detects schema drift, repairs supported changes without human "
           "approval, verifies the task and rolls back repairs that fail. Unsafe or ambiguous changes stop with a "
           "clear reason. This page shows production (real API) data only.")
    health = safe_call(api, "GET", "/api/health")
    m = safe_call(api, "GET", "/api/metrics")
    mon = safe_call(api, "GET", "/api/monitor")
    if health is None or m is None or mon is None:
        return
    llm = health["llm"]
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(f"**Service** &nbsp; {badge('healthy' if health['status'] == 'ok' else 'degraded')}", unsafe_allow_html=True)
    c2.markdown(f"**Database** &nbsp; {badge('healthy' if health['database'] == 'ok' else 'degraded')}", unsafe_allow_html=True)
    c3.markdown(f"**LLM** &nbsp; `{llm['provider']}` · {llm['connectivity'].replace('_', ' ')}")
    c4.markdown(f"**Approved hosts** &nbsp; {len(health['allowed_hosts'])}")

    tools = mon["tools"]
    checks = [t["last_check"] for t in tools if t["last_check"]]
    kpis([
        ("Real APIs monitored", len(tools), f"{sum(t['monitor_enabled'] for t in tools)} on a schedule"),
        ("Schema drift (latest checks)", sum(c["drift"] for c in checks), f"{len(checks)} tool(s) checked"),
        ("Autonomous repairs", m["recovered"], f"zero-touch rate {fmt_pct(m['zero_touch_adaptation_rate'])}"),
        ("Median recovery", fmt_ms(m["median_recovery_ms"]), f"{fmt_pct(m['under_4s_rate'])} under 4 s"),
    ])
    kpis([
        ("Incidents (real traffic)", m["sample"]["incidents"], f"{m['schema_drift_incidents']} schema drift"),
        ("Stopped safely", m["stopped_safely"], "ambiguous, unsafe or not schema drift"),
        ("Rollbacks", m["rollbacks"], f"rollback success {fmt_pct(m['rollback_success_rate'])}"),
        ("Unreachable at last check", sum(c["connectivity"] == "unreachable" for c in checks), "connectivity"),
    ])
    if not tools:
        empty("No real API is registered yet. Register one on the Real API Monitoring page.",
              "Open Real API Monitoring", "monitoring")
    else:
        st.subheader("Real APIs")
        st.dataframe([{"tool": t["tool"], "provider": t["provider"], "endpoint": f"{t['method']} {t['endpoint']}",
                       "last checked": (t["last_check"] or {}).get("ts", "")[:19],
                       "schema": ((t["last_check"] or {}).get("drift_summary") or (t["last_check"] or {}).get("schema_status")
                                  or "not checked"),
                       "verification": (t["last_check"] or {}).get("verification", "")} for t in tools],
                     hide_index=True, use_container_width=True)
    if m["sample"]["incidents"]:
        left, right = st.columns([1.1, 1], gap="large")
        with left:
            st.subheader("Incidents by outcome")
            st.bar_chart(pd.DataFrame({"incidents": m["incidents_by_status"]}), horizontal=True, height=220, color="#2563EB")
        with right:
            st.subheader("Recent incidents")
            for i in safe_call(api, "GET", "/api/incidents", params={"limit": 6}) or []:
                with st.container(border=True):
                    st.markdown(f"{badge(i['status'])} &nbsp; **{i['tool']}** &nbsp; {REAL_BADGE}", unsafe_allow_html=True)
                    st.caption(f"{i['category'].replace('_', ' ')} · {fmt_ms(i['duration_ms'])} · {i['started_at'][:19]}Z")
                    if st.button("Open timeline", key=f"ov-{i['id']}", icon=":material/timeline:"):
                        go("recovery", incident_id=i["id"])
    with st.container(border=True):
        st.markdown(f"{SIM_BADGE} &nbsp; **Simulation Lab** has {health['simulated_tools']} simulated tool(s) in its "
                    "own database. Simulated results never appear on this page.", unsafe_allow_html=True)
        if st.button("Open the Simulation Lab", icon=":material/science:"):
            go("chaos")
