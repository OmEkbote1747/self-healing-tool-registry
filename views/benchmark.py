import pandas as pd
import streamlit as st

from selfheal.bench import rates, run_benchmark, summarize
from views.common import footer, kpis, page_header, use_llm


def render():
    page_header("Console · Benchmark", "125 schema changes, two agents",
                "Both agents run the same 125 synthetic cases: 25 each for field renames, type changes, nested fields "
                "and new required headers, plus 25 where a value is missing and the right answer is to refuse.")
    if st.button("Run all 125 cases", type="primary", icon=":material/play_arrow:"):
        bar = st.progress(0.0, text="Running…")
        st.session_state.bench = run_benchmark(use_llm=use_llm(), progress=lambda f: bar.progress(f, text="Running…"))
        bar.empty()
    rows = st.session_state.get("bench")
    if not rows:
        st.info("Click **Run all 125 cases** to measure both agents.", icon=":material/info:")
        footer()
        return
    df = pd.DataFrame(rows)
    heal, stop = df[df.expected == "repair"], df[df.expected == "stop"]
    rule = df[(df.ours_outcome == "repaired") & (~df.tier.str.contains("Tier 2"))]
    n = rates(rows)
    kpis([
        {"label": "Zero-touch adaptation rate", "value": f"{n['zero_touch_rate']:.0f}%",
         "detail": f"{n['zero_touch']} / {n['heal_total']} healable fixed, no human",
         "target": f"baseline {n['baseline_rate']:.0f}% · {n['held']} held for approval"},
        {"label": "Safe stop rate", "value": f"{n['safe_stop_rate']:.0f}%",
         "detail": f"{n['safe_stops']} / {n['stop_total']} unhealable stopped or held", "target": "target 100%"},
        {"label": "False-repair rate", "value": f"{n['false_repair_rate']:.0f}%",
         "detail": f"{n['false_repairs']} wrong requests sent", "target": f"unsafe values sent: {n['unsafe']}"},
        {"label": "MTTR (median)", "value": f"{df.recovery_ms.median():.1f} ms",
         "detail": "API break to recovery or safe stop",
         "target": f"tokens per rule fix: {rule.tokens.mean():.0f}" if len(rule) else "target: < 4 s"},
    ])
    st.subheader("Results by scenario")
    st.dataframe(pd.DataFrame(summarize(rows)), hide_index=True, use_container_width=True)
    with st.expander("Every case", icon=":material/table_rows:"):
        st.dataframe(df, hide_index=True, use_container_width=True)
    st.download_button("Download results (CSV)", df.to_csv(index=False), "benchmark_125.csv",
                       icon=":material/download:")
    st.caption("Honest limits: these 125 cases were written by us alongside the healer and run against our own mock "
               "API, so a high score shows the rules handle these patterns, not every real API. The baseline is a "
               "simple retry loop; an LLM agent reading the error itself would do better than 0%.")
    footer()
