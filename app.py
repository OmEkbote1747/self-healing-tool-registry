"""Streamlit dashboard for the Self-Healing Tool Registry prototype (PS-01)."""
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from fastapi.testclient import TestClient

from selfheal import llm
from selfheal.bench import run_benchmark, run_case, summarize
from selfheal.cases import APPROVED_CONFIG, CATEGORIES, DEMO_CASE, build_cases, build_stress_cases
from selfheal.healer import NaiveRetryAgent, SelfHealer, ToolRegistry
from selfheal.mock_api import make_app

LOG = Path(__file__).parent / "logs" / "patches.jsonl"

st.set_page_config(page_title="Self-Healing Tool Registry", page_icon="🩹", layout="wide")

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### PS-01 · Self-Healing Tool Registry")
    st.caption("Genesis 2.0 · Track EVA 02")
    llm_ok = llm.available()
    use_llm = st.toggle("Tier 2: LLM repair proposals", value=llm_ok, disabled=not llm_ok,
                        help="Needs ANTHROPIC_API_KEY. When off, anything the rules are unsure about stops safely.")
    if llm_ok:
        st.success(f"LLM tier available ({llm.MODEL})")
    else:
        st.info("LLM tier off: no API key set. Running rules + validator only.")
    st.divider()
    st.markdown("**Safety rules**\n- never invent a value\n- validate before retry\n- max 2 repair attempts\n"
                "- headers only from approved config")
    st.caption("Approved config keys: " + ", ".join(APPROVED_CONFIG))

st.title("🩹 Self-Healing Tool Registry")
st.write("When an API changes its schema, a normal agent gets a **422** and loops. Our layer works out what changed, "
         "checks the fix is safe, patches the tool and finishes the task, **without making up data**.")

tab_demo, tab_bench, tab_stress, tab_log, tab_how = st.tabs(
    ["▶️ Live demo", "📊 Benchmark (125 cases)", "🧪 Stress tests", "🗂️ Registry & audit log", "ℹ️ How it works"])


def show_trace(trace):
    for i, step in enumerate(trace):
        icon = "✅" if step["ok"] else "❌"
        repeat = step["step"].startswith("Attempt") and i > 0   # baseline retries look identical
        with st.expander(f"{icon} {step['step']}", expanded=(not step["ok"] and not repeat)
                         or "Validator" in step["step"] or "Proposed" in step["step"]):
            d = step["detail"]
            if isinstance(d, str):
                st.write(d)
            else:
                st.json(d)


# ------------------------------------------------------------------ live demo
with tab_demo:
    cases = build_cases()
    stress = build_stress_cases()
    options = {"⭐ Worked example: user_id → target.id (int) + X-Api-Key": DEMO_CASE}
    for c in cases:
        options[f"{c.id} · {c.category} · {c.tool} · {c.note}"] = c
    for c in stress:
        options[f"{c.id} · stress · {c.category} · {c.note}"] = c
    label = st.selectbox("Pick a schema change", list(options))
    case = options[label]

    c1, c2, c3 = st.columns(3)
    c1.markdown("**Agent's arguments**")
    c1.json(case.args)
    c2.markdown("**API schema before (v1)**")
    c2.json(case.v1)
    c3.markdown("**API schema after (v2)**")
    c3.json({"body": case.v2, "required headers": case.v2_headers})
    st.caption(f"Correct outcome: **{'repair the call' if case.expected == 'repair' else 'stop and report, do not guess'}**")

    if st.button("Change the API and run both agents", type="primary"):
        client = TestClient(make_app(case.path, case.v2, case.v2_headers))
        registry = ToolRegistry()
        registry.register(case.tool, case.path, case.v1)
        base = NaiveRetryAgent().call(client, registry.current(case.tool), case.args)
        healer = SelfHealer(registry, APPROVED_CONFIG, log_path=LOG, use_llm=use_llm)
        ours = healer.call(client, case.tool, case.args, case.id)
        reuse = healer.call(client, case.tool, case.args, case.id + "-agentB") if ours.success else None
        st.session_state.demo = (label, base, ours, reuse, registry)

    if st.session_state.get("demo") and st.session_state.demo[0] == label:
        _, base, ours, reuse, registry = st.session_state.demo
        left, right = st.columns(2)
        with left:
            st.subheader("Normal agent (retry loop)")
            m1, m2 = st.columns(2)
            m1.metric("Result", "Done" if base.success else "Failed")
            m2.metric("API calls", base.calls)
            show_trace(base.trace)
        with right:
            st.subheader("Our self-healing agent")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Result", "Repaired" if ours.success else "Stopped safely")
            m2.metric("API calls", ours.calls)
            m3.metric("Recovery", f"{ours.recovery_ms:.1f} ms" if ours.recovery_ms else "-")
            m4.metric("LLM tokens", ours.tokens)
            if ours.stopped:
                st.warning("Stopped and reported: " + ours.stop_reason)
            show_trace(ours.trace)
            if reuse is not None:
                st.success(f"Shared registry: a second agent calling `{case.tool}` used tool "
                           f"v{registry.current(case.tool).version} and succeeded in {reuse.calls} call, "
                           f"with no new failure.")

# ------------------------------------------------------------------ benchmark
with tab_bench:
    st.write("Both agents run the **same 125 synthetic cases**: 25 each for field renames, type changes, nested "
             "fields and new required headers, plus 25 where a value is missing and the right answer is to refuse.")
    if st.button("Run all 125 cases", type="primary"):
        bar = st.progress(0.0)
        st.session_state.bench = run_benchmark(use_llm=use_llm, progress=bar.progress)
        bar.empty()
    rows = st.session_state.get("bench")
    if rows:
        df = pd.DataFrame(rows)
        n = len(df)
        rec = df[df.expected == "repair"]
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Recovery rate (ours vs baseline)", f"{100 * df.ours_pass.mean():.0f}%",
                  f"baseline {100 * df.baseline_pass.mean():.0f}%")
        k2.metric("Median recovery / safe stop", f"{df.recovery_ms.median():.1f} ms")
        k3.metric("Unsafe repairs", f"{int(df.unsafe.sum())} / {n}", "target 0", delta_color="off")
        rule = df[(df.ours_outcome == "repaired") & (~df.tier.str.contains("Tier 2"))]
        k4.metric("Tokens per rule-based fix", f"{rule.tokens.mean():.0f}" if len(rule) else "-")
        st.dataframe(pd.DataFrame(summarize(rows)), hide_index=True, use_container_width=True)
        with st.expander("Every case"):
            st.dataframe(df, hide_index=True, use_container_width=True)
        st.download_button("Download results (CSV)", df.to_csv(index=False), "benchmark_125.csv")
        st.caption("Honest limits: these 125 cases were written by us alongside the healer, against our own mock "
                   "API, so a high score shows the rules work on these patterns, not on every real API. The baseline "
                   "is a simple retry loop; an LLM agent reading the error itself would do better than 0%.")

# ------------------------------------------------------------------ stress
with tab_stress:
    st.write("Ten harder cases **outside** the 125, kept separate to show where the system breaks.")
    if st.button("Run stress tests"):
        st.session_state.stress = run_benchmark(use_llm=use_llm, stress=True)
    srows = st.session_state.get("stress")
    if srows:
        sdf = pd.DataFrame(srows)
        a, b, c = st.columns(3)
        a.metric("Passed", f"{int(sdf.ours_pass.sum())} / {len(sdf)}")
        b.metric("Safe failures (stopped)", int(((~sdf.ours_pass) & (sdf.ours_outcome == "stopped")).sum()))
        c.metric("Wrong repairs (dangerous)", int(sdf.wrong_repair.sum()))
        view = sdf[["case", "category", "change", "expected", "ours_outcome", "ours_pass", "wrong_repair",
                    "ours_reason"]].rename(columns={"ours_outcome": "what we did", "ours_pass": "pass"})
        st.dataframe(view, hide_index=True, use_container_width=True)
        st.markdown(
            "**What this shows**\n"
            "- *Synonyms with no shared words* (`user_id → customer_ref`): the rules stop safely; this is what "
            "the LLM tier is for.\n"
            "- *Similar name, different meaning* (`channel_id → thread_id`) and *hidden unit changes* "
            "(`amount → amount_cents`) fool the rules: the values are the agent's own, so the validator "
            "accepts them. Next step: send medium-confidence matches to the LLM or a human for approval.")

# ------------------------------------------------------------------ registry & log
with tab_log:
    st.write("Every accepted repair is a new tool version, written to an append-only JSON Lines audit log.")
    demo = st.session_state.get("demo")
    if demo:
        registry = demo[4]
        for name, versions in registry.versions.items():
            st.markdown(f"**Tool `{name}`**: {len(versions)} version(s)")
            st.json([v.describe() for v in versions])
            if len(versions) > 1 and st.button(f"Roll back {name} to v{versions[-2].version}"):
                registry.rollback(name)
                st.rerun()
    else:
        st.info("Run the live demo to see the registry.")
    if LOG.exists():
        lines = LOG.read_text().strip().splitlines()[-30:]
        st.markdown(f"**Audit log** (`logs/patches.jsonl`, last {len(lines)} entries)")
        st.code("\n".join(lines), language="json")

# ------------------------------------------------------------------ how it works
with tab_how:
    st.graphviz_chart("""
    digraph {
      rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#ffffff", fontname="Helvetica"];
      agent [label="LLM agent"]; reg [label="Tool registry\\n(versioned)"]; api [label="API"];
      parse [label="Error parser\\n+ /openapi.json"]; t1 [label="Tier 1: rules"]; t2 [label="Tier 2: LLM\\n(only if unsure)"];
      val [label="Validator\\nschema + provenance", fillcolor="#e0f4f5"];
      ok [label="Patch + log\\nretry once", fillcolor="#e4f4e5"]; stop [label="Stop and report", fillcolor="#fbe9e7"];
      agent -> reg -> api; api -> parse [label="422"]; parse -> t1; t1 -> t2 [label="unsure"];
      t1 -> val; t2 -> val; val -> ok [label="accept"]; val -> stop [label="reject"]; ok -> reg;
    }""")
    st.markdown(
        "1. The agent calls a tool through the registry, never the API directly.\n"
        "2. On a 422, the error is parsed and the new schema read from `/openapi.json` "
        "(falls back to the error locations).\n"
        "3. **Tier 1 rules** match renamed or moved fields to the agent's leftover arguments by name similarity and "
        "plan lossless type conversions. If they are unsure, **Tier 2** asks an LLM which existing argument fits; "
        "the LLM can only pick arguments, never supply values.\n"
        "4. The **validator** checks the repaired request against the new schema and checks every value comes from "
        "the agent's data or approved config. Otherwise it stops and reports.\n"
        "5. Accepted repairs become a new tool version, are logged, shared with other agents, and can be rolled back.")
