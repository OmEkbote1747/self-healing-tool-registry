"""Simulation Lab: controlled, repeatable schema-drift scenarios against isolated simulated APIs."""
import streamlit as st

from views.common import SIM_BADGE, api, badge, fmt_ms, go, header, safe_call

LLM_CHOICES = {"none": "Rules only (deterministic, no LLM)", "mock": "Mock LLM (deterministic, NOT a real model)",
               "gemini": "Real Gemini (real requests, labelled)"}


def _fields(d: dict) -> list[dict]:
    out = []
    for k, v in d.items():
        spec = v if isinstance(v, dict) else {"type": v}
        where = "query" if k.startswith("q:") else "path" if k.startswith("p:") else "body"
        out.append({"field": f"{where}:{k.split(':', 1)[-1]}", "type": spec["type"],
                    "documentation": spec.get("description", "") + (f" (renamed from {spec['renamed_from']})"
                                                                    if spec.get("renamed_from") else "")})
    return out


def _detail(r: dict, scenario: dict):
    """Everything that happened in one run, from the stored SIMULATED incident."""
    if not r.get("incident"):
        st.caption("The call succeeded without an incident (no failure to diagnose).")
        return
    inc = safe_call(api, "GET", f"/api/sim/incidents/{r['incident']}")
    if not inc:
        return
    tabs = st.tabs(["Drift", "Repair", "Validation", "Retry & verification", "Timeline"])
    with tabs[0]:
        if inc.get("drift"):
            d = inc["drift"]
            st.markdown(f"**{d['summary']}** · compatibility {d['compatibility']}")
            st.dataframe([{"change": c["kind"].replace("_", " "), "field": c["location"], "was": c.get("old") or "",
                           "now": c.get("new") or "", "detail": c.get("detail", "")}
                          for c in d["request_changes"] + d["response_changes"]], hide_index=True,
                         use_container_width=True)
        else:
            st.caption(f"No drift report: the failure was classified as {inc['category'].replace('_', ' ')}, "
                       "so no schema repair was attempted.")
    with tabs[1]:
        p = inc.get("plan")
        if p:
            st.markdown(f"Planner `{p['source']}` · confidence {p['confidence']:.2f}")
            if p["mappings"]:
                st.dataframe([{"new field": m["target_field"], "from argument": m["source_arg"],
                               "transform": m["transform"], "evidence": m["evidence"]} for m in p["mappings"]],
                             hide_index=True, use_container_width=True)
            if p.get("response_mappings"):
                st.dataframe([{"output": m["output"], "response path": m["response_path"], "evidence": m.get("evidence")}
                              for m in p["response_mappings"]], hide_index=True, use_container_width=True)
            for u in p.get("unresolved", []):
                st.warning(f"Unresolved: {u}")
            for k in p.get("compatibility_risks", []):
                st.caption(f"Risk: {k}")
        else:
            st.caption("No repair was proposed.")
        st.markdown(f"**LLM:** {r['llm']['label']}" + (f" · `{r['llm'].get('model')}` · "
                                                       f"{r['llm'].get('error_category') or 'ok'}" if r["llm"]["used"] else ""))
    with tabs[2]:
        v = inc.get("validation")
        if v:
            st.markdown(badge("VERIFIED" if v["accepted"] else "FAILED") + " &nbsp; " +
                        ("accepted by independent validation" if v["accepted"] else "rejected"), unsafe_allow_html=True)
            st.dataframe([{"check": c["name"], "passed": c["ok"], "detail": c["detail"]} for c in v["checks"]],
                         hide_index=True, use_container_width=True)
        else:
            st.caption("Nothing reached validation.")
    with tabs[3]:
        reqs = [s for s in inc["timeline"] if s["stage"] in ("request", "retry")]
        for s in reqs:
            st.markdown(f"{'✅' if s['status'] == 'ok' else '❌' if s['status'] == 'fail' else '•'} {s['title']}")
        ver = inc.get("verification")
        if ver:
            st.markdown("**Verification:** " + ("task verified" if ver["ok"] else "task NOT verified"))
            for c in ver.get("checks", []):
                st.markdown(f"{'✅' if c['ok'] else '❌'} {c['name']} · {c['detail']}")
        rb = [s for s in inc["timeline"] if s["stage"] == "rollback"]
        for s in rb:
            st.warning("Rollback: " + s["title"], icon=":material/undo:")
    with tabs[4]:
        for s in inc["timeline"]:
            st.markdown(f"`{s['stage']}` {s['title']}")
        if st.button("Open in Simulation Records", key=f"open-{inc['id']}"):
            go("sim_records", incident_id=inc["id"])


def _result(r: dict, scenario: dict):
    with st.container(border=True):
        mode = "Baseline (no repair)" if r["mode"] == "baseline" else "ADAPTIX"
        st.markdown(f"{SIM_BADGE} &nbsp; **{mode}** &nbsp; {badge(r['outcome'])} &nbsp; "
                    + ("✅ as expected" if r["matches_expectation"] else f"⚠️ expected {r['expected']}, got {r['outcome']}"),
                    unsafe_allow_html=True)
        st.caption(f"{(r.get('category') or 'ok').replace('_', ' ')} · {fmt_ms(r['duration_ms'])}"
                   + (f" · tool v{r['tool_version']}" if r.get("tool_version") else "")
                   + f" · simulated upstream calls {r.get('upstream_calls')}"
                   + (f" · side effects {r['side_effects']}" if r.get("side_effects") is not None else "")
                   + f" · LLM: {r['llm']['label']}")
        st.write(r.get("reason") or "")
        _detail(r, scenario)


def render():
    header("Simulation Lab", "Controlled, repeatable API failures",
           "Each scenario registers a simulated tool against v1 of an isolated in-process API, switches it to v2 "
           "and calls the tool. Simulation data lives in its own database and never reaches a real provider.")
    st.markdown(f"{SIM_BADGE} &nbsp; Everything on this page is simulated.", unsafe_allow_html=True)
    cat = safe_call(api, "GET", "/api/simulations")
    integ = safe_call(api, "GET", "/api/integrations")
    if cat is None or integ is None:
        return
    gemini_ok = integ["gemini"]["credentials"]["status"] == "CONFIGURED"
    groups = list(dict.fromkeys(s["group"] for s in cat))
    c1, c2, c3, c4 = st.columns([1, 2, 1.1, 1.6])
    group = c1.selectbox("Group", groups)
    items = [s for s in cat if s["group"] == group]
    s = c2.selectbox("Scenario", items, format_func=lambda s: s["title"])
    mode = c3.selectbox("Run", ["both", "adaptix", "baseline"],
                        format_func={"both": "Baseline vs ADAPTIX", "adaptix": "ADAPTIX only",
                                     "baseline": "Baseline only"}.get)
    choices = [k for k in LLM_CHOICES if k != "gemini" or gemini_ok]
    llm = c4.selectbox("LLM planner", choices, format_func=LLM_CHOICES.get,
                       help="Real Gemini appears only when GEMINI_API_KEY is configured on the server")
    seed = st.number_input("Seed (same seed = same run)", 0, 10_000, 7)
    st.markdown(s["description"])
    exp = s["expected_now"] if llm != "gemini" else s["expected_with_real_llm"]
    st.caption(f"Expected with ADAPTIX: **{exp}** (category {s['expect_category'].replace('_', ' ')})"
               + (" · needs a real LLM; without one the correct outcome is a safe stop" if s["needs_llm"] else ""))
    a, b = st.columns(2)
    a.markdown("**Original schema (v1)**")
    a.dataframe(_fields(s["v1"]), hide_index=True, use_container_width=True)
    b.markdown("**Simulated changed schema (v2)**")
    b.dataframe(_fields(s["v2"]), hide_index=True, use_container_width=True)
    extra = {k: s[k] for k in ("v2_headers", "v2_response", "v2_faults", "v2_method", "auth") if s.get(k)}
    if extra:
        st.caption("Other simulated changes: " + ", ".join(f"{k}={v}" for k, v in extra.items()))
    st.caption(f"Caller arguments: {s['args']}")
    b1, b2, b3 = st.columns(3)
    if b1.button("Run scenario", type="primary", icon=":material/bolt:"):
        with st.spinner("Running…"):
            res = safe_call(api, "POST", "/api/simulations",
                            json={"scenario": s["id"], "mode": mode, "llm": llm, "seed": int(seed)})
        if res:
            st.session_state["chaos_last"] = (s["id"], res["runs"])
    if b2.button("Run all scenarios", icon=":material/playlist_play:"):
        out, bar = [], st.progress(0.0)
        for i, sc in enumerate(cat):
            r = safe_call(api, "POST", "/api/simulations",
                          json={"scenario": sc["id"], "mode": "adaptix", "llm": llm, "seed": int(seed)})
            if r:
                out += r["runs"]
            bar.progress((i + 1) / len(cat))
        st.session_state["chaos_all"] = out
    if b3.button("Reset simulation data", icon=":material/restart_alt:"):
        if safe_call(api, "POST", "/api/simulations/reset"):
            st.session_state.pop("chaos_last", None)
            st.session_state.pop("chaos_all", None)
            st.success("Simulation database cleared. Production data is in a different database and was not touched.")
    last = st.session_state.get("chaos_last")
    if last and last[0] == s["id"]:
        cols = st.columns(len(last[1]))
        for col, r in zip(cols, last[1]):
            with col:
                _result(r, s)
    if st.session_state.get("chaos_all"):
        rows = st.session_state["chaos_all"]
        ok = sum(r["matches_expectation"] for r in rows)
        st.markdown(f"**All scenarios (ADAPTIX, {LLM_CHOICES[llm]}):** {ok}/{len(rows)} matched the expected outcome")
        st.dataframe([{"scenario": r["title"], "expected": r["expected"], "outcome": r["outcome"],
                       "as expected": r["matches_expectation"], "category": r.get("category"),
                       "LLM": r["llm"]["label"], "duration": fmt_ms(r["duration_ms"]), "reason": r.get("reason")}
                      for r in rows], hide_index=True, use_container_width=True)
