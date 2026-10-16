"""Batch scenario matrix over real HTTP against the controlled test service (REAL_API_TEST)."""
import pandas as pd
import streamlit as st

from views.common import api, fmt_ms, mutated, safe_call

TEST_BADGE = ('<span class="ax-badge" style="background:#EEF2FF;color:#3730A3;border-color:#A5B4FC">'
              'REAL API TEST</span>')


def matrix_panel(status: dict):
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
        mutated("rt_matrix")
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

