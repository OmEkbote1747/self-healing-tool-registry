import pandas as pd
import streamlit as st

from adaptix.bench import SUITES, compare, rates, run_benchmark, summarize
from views.common import footer, kpis, page_header, use_llm


def render():
    page_header("Console · Benchmark", "Three agents, same cases",
                "A retry loop, an error-reading agent that repairs greedily, and SchemaHeal run the same cases. "
                "Zero-touch fixes, cases held for approval, safe stops and false repairs are counted separately: "
                "a system that recovers often by making risky changes is not counted as a success.")
    suite = st.segmented_control("Test suite", list(SUITES), format_func=SUITES.get, default="planned")
    suite = suite or "planned"
    if st.button(f"Run {SUITES[suite]}", type="primary", icon=":material/play_arrow:"):
        bar = st.progress(0.0, text="Running…")
        st.session_state[f"bench_{suite}"] = run_benchmark(use_llm=use_llm(), suite=suite,
                                                           progress=lambda f: bar.progress(f, text="Running…"))
        bar.empty()
    rows = st.session_state.get(f"bench_{suite}")
    if not rows:
        st.info(f"Click **Run {SUITES[suite]}** to measure all three agents.", icon=":material/info:")
        footer()
        return
    df = pd.DataFrame(rows)
    rule = df[(df.ours_outcome == "repaired") & (~df.tier.str.contains("Tier 2"))]
    n = rates(rows)
    kpis([
        {"label": "Zero-touch adaptation rate", "value": f"{n['zero_touch_rate']:.0f}%",
         "detail": f"{n['zero_touch']} / {n['heal_total']} healable fixed, no human",
         "target": f"{n['held']} more held for approval"},
        {"label": "Safe stop rate", "value": f"{n['safe_stop_rate']:.0f}%",
         "detail": f"{n['safe_stops']} / {n['stop_total']} unhealable stopped or held", "target": "target 100%"},
        {"label": "False-repair rate", "value": f"{n['false_repair_rate']:.0f}%",
         "detail": f"{n['false_repairs']} wrong requests sent", "target": f"invented values sent: {n['unsafe']}"},
        {"label": "MTTR (median)", "value": f"{df.recovery_ms.median():.1f} ms",
         "detail": "API break to recovery or safe stop",
         "target": f"tokens per rule fix: {rule.tokens.mean():.0f}" if len(rule) else "target: < 4 s"},
    ])
    st.subheader("Against the baselines")
    st.dataframe(pd.DataFrame(compare(rows)), hide_index=True, use_container_width=True)
    st.caption("The error-reading baseline is a heuristic stand-in for an agent that feeds errors back to a model: "
               "it takes the closest leftover argument however weak the match and invents placeholders. It is not "
               "a real LLM.")
    st.subheader("SchemaHeal by scenario")
    st.dataframe(pd.DataFrame(summarize(rows)), hide_index=True, use_container_width=True)
    with st.expander("Every case", icon=":material/table_rows:"):
        st.dataframe(df[["case", "category", "change", "expected", "verdict", "greedy_verdict", "naive_verdict",
                         "ours_reason", "recovery_ms"]], hide_index=True, use_container_width=True)
    st.download_button("Download results (CSV)", df.to_csv(index=False), f"benchmark_{suite}.csv",
                       icon=":material/download:")
    st.caption("Honest limits: every suite here was written by our team and runs against our own mock API. The "
               "adversarial fixtures live in fixtures/adversarial.json so independently sourced, real API changes "
               "can be added in the same format. Results on real third-party APIs are still to be measured.")
    footer()
