import pandas as pd
import streamlit as st

from views.common import SIM_BADGE, api, fmt_ms, fmt_pct, header, kpis, safe_call

ROWS = [("Accuracy (outcome as expected)", "accuracy", fmt_pct),
        ("Recovery success (repairable cases)", "recovery_success_rate", fmt_pct),
        ("Zero-touch adaptation", "zero_touch_adaptation_rate", fmt_pct),
        ("Safe stop (unrepairable cases)", "safe_stop_rate", fmt_pct),
        ("Safe misses (stopped although repairable)", "safe_misses", str),
        ("Incorrect patches accepted", "incorrect_patches", str),
        ("Unsafe-patch rejection", "unsafe_patch_rejection_rate", fmt_pct),
        ("Median recovery time", "median_recovery_ms", fmt_ms),
        ("Recoveries under 4 s", "under_4s_rate", fmt_pct),
        ("Mean retries", "mean_retries", str),
        ("Rollbacks", "rollbacks", str),
        ("LLM calls", "llm_calls", str),
        ("of which real LLM requests", "real_llm_calls", str),
        ("LLM tokens", "tokens", str)]


def _evaluation():
    st.markdown("Runs every case twice against simulated upstreams: once as a **baseline** (no repair: the call "
                "simply fails) and once with **ADAPTIX**. Expected outcomes are fixed before the run.")
    c1, c2, c4, c3 = st.columns([1.4, .6, 1.4, 1])
    suites = {"scenarios": "Chaos Lab scenarios (19)", "planned": "Planned schema changes (125)",
              "adversarial": "Adversarial fixtures (30)"}
    suite = c1.selectbox("Suite", list(suites), format_func=suites.get)
    seed = c2.number_input("Seed", 0, 10_000, 7)
    llm_opts = {"none": "Rules only", "mock": "Mock LLM (not real)", "gemini": "Real Gemini"}
    llm = c4.selectbox("LLM planner", list(llm_opts), format_func=llm_opts.get, key="eval_llm")
    c3.write("")
    if c3.button("Run evaluation", type="primary", icon=":material/play_arrow:"):
        with st.spinner("Running both modes against simulated upstreams…"):
            res = safe_call(api, "POST", "/api/evaluation", json={"suite": suite, "seed": int(seed), "llm": llm})
        if res:
            st.session_state["eval"] = res
    res = st.session_state.get("eval")
    if not res:
        latest = safe_call(api, "GET", "/api/evaluation/latest") or []
        if latest:
            res = latest[0]
            st.caption(f"Showing the latest stored run ({res['ts'][:19]}Z). Per-case rows are shown after a run in "
                       "this session.")
    if not res:
        st.info("No evaluation has been run yet.")
        return
    st.markdown(f"{SIM_BADGE} &nbsp; **{res['label']}** · {res['cases']} cases · seed {res['seed']} · "
                f"LLM planner: `{res['llm']}` · real LLM requests: {res['summary']['adaptix'].get('real_llm_calls', 0)}"
                f" of {res['summary']['adaptix'].get('llm_calls', 0)} LLM calls · {res['duration_s']} s",
                unsafe_allow_html=True)
    b, a = res["summary"]["baseline"], res["summary"]["adaptix"]
    kpis([("Recovery success", fmt_pct(a["recovery_success_rate"]), f"baseline {fmt_pct(b['recovery_success_rate'])}"),
          ("Safe stop", fmt_pct(a["safe_stop_rate"]), f"{a['should_stop']} unrepairable cases"),
          ("Incorrect patches", a["incorrect_patches"], "accepted wrong repairs"),
          ("Median recovery", fmt_ms(a["median_recovery_ms"]), f"{fmt_pct(a['under_4s_rate'])} under 4 s")])
    st.dataframe([{"metric": label, "baseline": f(b[k]) if b.get(k) is not None else "—",
                   "ADAPTIX": f(a[k]) if a.get(k) is not None else "—"} for label, k, f in ROWS],
                 hide_index=True, use_container_width=True)
    if res.get("rows"):
        df = pd.DataFrame(res["rows"])
        only = st.toggle("Only ADAPTIX rows that did not match the expectation")
        view = df[df["mode"] == "adaptix"]
        if only:
            view = view[~view["correct"]]
        st.dataframe(view[["case", "category", "change", "expected", "outcome", "correct", "failure_category",
                           "duration_ms", "reason"]], hide_index=True, use_container_width=True)
        st.download_button("Download all rows (CSV)", df.to_csv(index=False), f"adaptix-eval-{res['suite']}.csv",
                           "text/csv", icon=":material/download:")


def _audit(prefix: str):
    c1, c2 = st.columns(2)
    kind = c1.text_input("Event kind", placeholder="schema_check", key=f"kind{prefix}")
    tool = c2.text_input("Tool", key=f"tool{prefix}")
    params = {"limit": 500}
    if kind:
        params["kind"] = kind
    if tool:
        params["tool"] = tool
    ev = safe_call(api, "GET", f"{prefix}/audit", params=params)
    if ev is None:
        return
    if not ev:
        st.info("No audit events match.")
        return
    st.caption(f"{len(ev)} events, newest first. Payloads are redacted before they are stored.")
    st.dataframe([{"time": e["ts"][:19], "event": e["kind"], "tool": e["tool"], "simulated": e["simulated"],
                   "correlation": e["correlation_id"], "data": str(e["data"])[:160]} for e in ev],
                 hide_index=True, use_container_width=True)
    pick = st.selectbox("Inspect event", range(len(ev)), format_func=lambda i: f"{ev[i]['ts'][:19]} · {ev[i]['kind']}")
    st.json(ev[pick]["data"])


def _metrics(prefix: str):
    m = safe_call(api, "GET", f"{prefix}/metrics")
    if m is None:
        return
    s = m["sample"]
    st.caption(f"Computed from {s['incidents']} stored incidents ({s['simulated_incidents']} simulated) and "
               f"{s['executions']} executions.")
    kpis([("Recovery success", fmt_pct(m["recovery_success_rate"]), "of all incidents"),
          ("Zero-touch adaptation", fmt_pct(m["zero_touch_adaptation_rate"]), f"{m['schema_drift_incidents']} drift incidents"),
          ("Mean retries", m["mean_retries"] if m["mean_retries"] is not None else "—", "per incident"),
          ("Rollbacks", m["rollbacks"], f"success {fmt_pct(m['rollback_success_rate'])}")])
    l = m["llm"]
    st.markdown(f"**LLM usage** · {l['calls']} calls ({l['real_calls']} to a real provider, {l['ok']} returned a plan) · "
                f"{l['prompt_tokens']} prompt / {l['output_tokens']} output tokens")
    if l["error_categories"]:
        st.caption("LLM errors: " + ", ".join(f"{k} ×{v}" for k, v in l["error_categories"].items()))
    if m["per_tool"]:
        st.dataframe(pd.DataFrame(m["per_tool"]).T.fillna(0).astype(int), use_container_width=True)


def render(prefix: str = "/api"):
    if prefix == "/api/sim":
        header("Simulation Evaluation", "Baseline vs ADAPTIX on simulated suites",
               "Deterministic suites against isolated simulated APIs, plus the simulation metrics and audit log. "
               "Nothing here is production data.")
        st.markdown(f"{SIM_BADGE} &nbsp; Simulation database only.", unsafe_allow_html=True)
        t1, t2, t3 = st.tabs(["Evaluation", "Metrics", "Audit log"])
        with t1:
            _evaluation()
        with t2:
            _metrics(prefix)
        with t3:
            _audit(prefix)
        return
    header("Production Audit", "Evidence from real API traffic",
           "Metrics computed from production records only (real providers), and the production audit log.")
    t2, t3 = st.tabs(["Metrics", "Audit log"])
    with t2:
        _metrics(prefix)
    with t3:
        _audit(prefix)
