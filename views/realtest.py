"""Real API Test: reproducible recoveries against the controlled test service over real HTTP."""
import pandas as pd
import streamlit as st

from views.common import api, badge, fmt_ms, go, header, safe_call

TEST_BADGE = '<span class="ax-badge" style="background:#EEF2FF;color:#3730A3;border-color:#A5B4FC">REAL API TEST</span>'
STEP_LABEL = {"preflight": "Preflight: service and Gemini reachable", "initial_call": "Original task succeeds on schema v1",
              "schema_change": "Controlled schema change applied", "original_request_fails": "Original request now fails",
              "pipeline": "ADAPTIX: diagnose, propose (Gemini), validate, apply, retry, verify",
              "business_verification": "Independent business verification (read-back)",
              "rollback": "Rollback to the last known-good version"}


def _steps(run: dict):
    for s in run["steps"]:
        icon = "✅" if s["status"] == "ok" else "❌"
        st.markdown(f"{icon} **{s['step']}. {STEP_LABEL.get(s['name'], s['name'])}** · "
                    f"<span style='color:#64748B'>{s['ts'][11:23]}Z</span><br>{s['title']}", unsafe_allow_html=True)
        if s.get("detail"):
            with st.expander("evidence", expanded=False):
                st.json(s["detail"])


def _demo(status: dict):
    st.subheader("End-to-end recovery: customer_id → client_id")
    st.markdown("1. The order API (schema v1) takes `customer_id`.\n"
                "2. The test service switches to v2, which renames it to `client_id`.\n"
                "3. Real Gemini must propose the repair, and the independent validator decides.\n"
                "4. The repaired call is retried, and the stored order is read back to confirm the business result.")
    if not status["gemini_configured"]:
        st.warning("Gemini is not configured on this server (GEMINI_API_KEY), so the live demonstration cannot run. "
                   "It will not fall back to a mock.", icon=":material/key_off:")
    c1, c2, c3 = st.columns(3)
    if c1.button("Check service and Gemini", icon=":material/network_check:",
                 help="Starts the test service if needed and sends one minimal real Gemini request"):
        with st.spinner("Checking…"):
            st.session_state["rt_preflight"] = safe_call(api, "POST", "/api/test/preflight")
        st.rerun()                                  # redraw the status lines with the new state
    run = c2.button("Run the recovery demo", type="primary", icon=":material/play_arrow:",
                    disabled=not status["gemini_configured"])
    fail = c3.button("Run the failure demo", icon=":material/undo:", disabled=not status["gemini_configured"],
                     help="v2 accepts the order but does not store it: verification must fail and the registry roll back")
    pf = st.session_state.get("rt_preflight")
    if pf:
        p1, p2 = st.columns(2)
        p1.markdown(f"**Test service** {badge(pf['service']['status'])} {pf['service']['detail']}", unsafe_allow_html=True)
        p2.markdown(f"**Gemini** {badge(pf['gemini']['status'])} {pf['gemini']['detail']}", unsafe_allow_html=True)
    if run or fail:
        with st.spinner("Running against the real test service and Gemini…"):
            st.session_state["rt_demo"] = safe_call(api, "POST", "/api/test/demo",
                                                    json={"fault": "drop_writes" if fail else None})
        st.rerun()
    res = st.session_state.get("rt_demo")
    if res:
        with st.container(border=True):
            ok = res.get("verified_recovery")
            st.markdown(f"{TEST_BADGE} &nbsp; run `{res['run_id']}` · outcome {badge(res['outcome'])} · "
                        + ("**verified recovery**" if ok else "**not a verified recovery**")
                        + (f" · {res['elapsed_s']} s" if res.get("elapsed_s") else ""), unsafe_allow_html=True)
            if res.get("error"):
                st.error(res["error"], icon=":material/error:")
            _steps(res)
            b1, b2 = st.columns(2)
            if res.get("incident") and b1.button("Open the incident timeline", key="rt-open"):
                go("test_records", incident_id=res["incident"])
            if res.get("evidence_id") and b2.button("Open the evidence record", key="rt-ev"):
                st.session_state["ev_env"] = "REAL_API_TEST"
                st.session_state["ev_pick"] = res["evidence_id"]
                go("evidence")


def _matrix(status: dict):
    st.subheader("Scenario matrix over real HTTP")
    st.caption("Each scenario installs schema v1 on the test service, registers a tool, switches to v2 and calls the "
               "tool. Expected outcomes are fixed before the run; mismatches are reported, not hidden.")
    labels = {m["scenario"]: m["change_type"] for m in status["matrix"]}
    c1, c2, c3 = st.columns([3, 1, 1.4])
    pick = c1.multiselect("Scenarios", list(labels), default=list(labels), format_func=labels.get)
    runs = c2.number_input("Runs each", 1, 5, 1)
    llm_opts = ["none"] + (["gemini"] if status["gemini_configured"] else [])
    llm = c3.selectbox("LLM planner", llm_opts, format_func={"none": "Rules only", "gemini": "Real Gemini"}.get)
    if st.button("Run the matrix", icon=":material/playlist_play:", disabled=not pick):
        with st.spinner(f"Running {len(pick) * runs} real HTTP scenario run(s)…"):
            st.session_state["rt_matrix"] = safe_call(api, "POST", "/api/test/matrix",
                                                      json={"scenarios": pick, "runs": int(runs), "llm": llm})
        st.rerun()
    m = st.session_state.get("rt_matrix")
    if not m:
        return
    sc = m["scorecard"]
    st.markdown(f"{TEST_BADGE} &nbsp; batch `{m['batch']}` · {len(m['rows'])} run(s) · LLM: "
                f"{'real Gemini' if m['llm'] == 'gemini' else 'rules only'} · real Gemini requests: {sc['real_llm_requests']}",
                unsafe_allow_html=True)
    st.dataframe(pd.DataFrame([{"scenario": labels.get(r["scenario"], r["scenario"]), "expected": r["expected"],
                                "outcome": r["outcome"], "as expected": "yes" if r["matches_expectation"] else "NO",
                                "category": r["category"], "LLM": r["llm"]["label"], "calls": r["upstream_calls"],
                                "side effects": r["side_effects"], "latency": fmt_ms(r["duration_ms"])}
                               for r in m["rows"]]), hide_index=True, use_container_width=True)
    st.dataframe(pd.DataFrame([{"scenario": labels.get(k, k), **v} for k, v in sc["by_scenario"].items()])
                 .drop(columns=["environments"]), hide_index=True, use_container_width=True)


def render():
    header("Real API Test", "Controlled real HTTP recoveries",
           "A versioned test API that ADAPTIX controls, served by a real HTTP server on this host. Schema changes are "
           "applied through its admin API, so every call, failure and repair here is real, and nothing is mocked. "
           "Results are recorded as REAL_API_TEST, separately from production and simulation.")
    st.markdown(TEST_BADGE, unsafe_allow_html=True)
    status = safe_call(api, "GET", "/api/test/status")
    if status is None:
        return
    s1, s2 = st.columns(2)
    s1.markdown(f"**Test service** {badge('VERIFIED' if status['service_running'] else 'NOT RUN')} "
                + (f"`{status['service_url']}` · schema {status['service_state']['version']} · "
                   f"{status['service_state']['orders_stored']} orders stored" if status["service_running"]
                   else "starts on first use"), unsafe_allow_html=True)
    if status["service_running"]:
        s1.caption("This address is inside the app's own server, with a port chosen at start-up. It is not reachable "
                   "from your browser or computer. To run the service locally: python -m adaptix.testservice --port 8765")
    s2.markdown(f"**Gemini** {badge('CONFIGURED' if status['gemini_configured'] else 'ACTION REQUIRED')} "
                f"`{status['gemini_model']}`", unsafe_allow_html=True)
    t1, t2 = st.tabs(["End-to-end demo", "Scenario matrix"])
    with t1:
        _demo(status)
    with t2:
        _matrix(status)
    with st.expander("Reset REAL_API_TEST records"):
        st.caption("Deletes only the REAL_API_TEST database (tools, incidents, evidence). Production and simulation "
                   "data are in other databases.")
        if st.checkbox("I understand this deletes the REAL_API_TEST records") and \
                st.button("Delete REAL_API_TEST records", type="primary"):
            if safe_call(api, "POST", "/api/test/reset"):
                for k in ("rt_demo", "rt_matrix"):
                    st.session_state.pop(k, None)
                st.success("REAL_API_TEST records deleted.")
