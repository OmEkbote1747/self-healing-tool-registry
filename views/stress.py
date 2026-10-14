import pandas as pd
import streamlit as st

from selfheal.bench import run_benchmark
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
    kpis([
        {"label": "Passed", "value": f"{int(df.ours_pass.sum())} / {len(df)}", "detail": "correct repair or correct stop"},
        {"label": "Safe failures", "value": str(int(((~df.ours_pass) & (df.ours_outcome == 'stopped')).sum())),
         "detail": "stopped instead of guessing"},
        {"label": "Wrong repairs", "value": str(int(df.wrong_repair.sum())), "detail": "the dangerous kind"},
    ], cols=3)
    view = df[["case", "category", "change", "expected", "ours_outcome", "ours_pass", "wrong_repair", "ours_reason"]]
    st.dataframe(view.rename(columns={"ours_outcome": "what we did", "ours_pass": "pass", "wrong_repair": "wrong repair",
                                      "ours_reason": "reason"}), hide_index=True, use_container_width=True)
    st.markdown(
        "**What this shows**\n"
        "- *Synonyms with no shared words* (`user_id → customer_ref`): the rules stop safely. This is what the LLM "
        "tier is for.\n"
        "- *Similar name, different meaning* (`channel_id → thread_id`) and *hidden unit changes* "
        "(`amount → amount_cents`) fool the rules: the values are the agent's own, so the validator accepts them. "
        "Next step: send medium-confidence matches to the LLM or a human for approval.")
    footer()
