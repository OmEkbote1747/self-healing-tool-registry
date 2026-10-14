import pandas as pd
import streamlit as st

from selfheal.bench import run_benchmark, summarize
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
    kpis([
        {"label": "Zero-touch adaptation rate", "value": f"{100 * heal.ours_pass.mean():.0f}%",
         "detail": f"{int(heal.ours_pass.sum())} / {len(heal)} healable fixed", "target": f"baseline {100 * heal.baseline_pass.mean():.0f}%"},
        {"label": "Safe stop rate", "value": f"{100 * stop.ours_pass.mean():.0f}%",
         "detail": f"{int(stop.ours_pass.sum())} / {len(stop)} unhealable stopped", "target": f"unsafe repairs: {int(df.unsafe.sum())}"},
        {"label": "MTTR (median)", "value": f"{df.recovery_ms.median():.1f} ms",
         "detail": "API break to recovery or safe stop", "target": "target: < 4 s"},
        {"label": "Token overhead", "value": f"{rule.tokens.mean():.0f}" if len(rule) else "-",
         "detail": "tokens per rule-based fix", "target": f"baseline: {df.baseline_calls.mean():.0f} failed calls / case"},
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
