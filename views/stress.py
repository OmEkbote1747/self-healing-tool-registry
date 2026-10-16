import pandas as pd
import streamlit as st

from adaptix.bench import rates, run_benchmark
from views.common import footer, kpis, page_header, use_llm


def render():
    page_header("Console · Stress tests", "Where it breaks",
                "Ten harder cases outside the 125, kept separate on purpose to show the limits of the approach.")
    if st.button("Run stress tests", type="primary", icon=":material/play_arrow:"):
        st.session_state.stress = run_benchmark(use_llm=use_llm(), stress=True)
    rows = st.session_state.get("stress")
    if not rows:
        st.info("Click **Run stress tests** to see the hard cases.", icon=":material/info:")
        footer()
        return
    df = pd.DataFrame(rows)
    n = rates(rows)
    kpis([
        {"label": "Correct outcome", "value": f"{int(df.ours_pass.sum())} / {len(df)}", "detail": "right repair or right stop"},
        {"label": "Held for approval", "value": str(n["held"]), "detail": "not sent; a human decides"},
        {"label": "Safe misses", "value": str(int((df.verdict == "safe_miss").sum())), "detail": "stopped instead of guessing"},
        {"label": "False repairs", "value": str(n["false_repairs"]), "detail": "the dangerous kind"},
    ], cols=4)
    view = df[["case", "category", "change", "expected", "verdict", "ours_pass", "ours_reason"]]
    st.dataframe(view.rename(columns={"verdict": "what happened", "ours_pass": "correct", "ours_reason": "reason"}),
                 hide_index=True, use_container_width=True)
    st.markdown(
        "**What this shows**\n"
        "- *Similar name, different meaning* (`channel_id → thread_id`) and *hidden unit changes* "
        "(`amount → amount_cents`) used to be repaired wrongly. The repair policy now spots the different entity "
        "name and the unit word and **holds them for a human** instead of sending them.\n"
        "- *Synonyms with no shared words* (`user_id → customer_ref`) stop safely. With the LLM tier on, its "
        "suggestion is still held for approval.\n"
        "- Passing these 10 cases is not proof of safety on real APIs: they were written by us.")
    footer()
