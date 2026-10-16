import streamlit as st

from views.common import SIM_BADGE, api, badge, fmt_ms, go, header, safe_call

def _result(r: dict):
    with st.container(border=True):
        mode = "Baseline (no repair)" if r["mode"] == "baseline" else "ADAPTIX"
        st.markdown(f"**{mode}** &nbsp; {badge(r['outcome'])} &nbsp; "
                    + ("✅ as expected" if r["matches_expectation"] else f"⚠️ expected {r['expected']}"),
                    unsafe_allow_html=True)
        st.caption(f"{(r.get('category') or 'ok').replace('_', ' ')} · {fmt_ms(r['duration_ms'])}"
                   + (f" · tool v{r['tool_version']}" if r.get("tool_version") else "")
                   + f" · upstream calls {r.get('upstream_calls')}"
                   + (f" · side effects {r['side_effects']}" if r.get("side_effects") is not None else ""))
        st.write(r.get("reason") or "")
        if r.get("incident") and st.button("Open timeline", key=f"tl-{r['incident']}", icon=":material/timeline:"):
            go("recovery", incident_id=r["incident"])


def render():
    header("Chaos Lab", "Inject schema drift into simulated APIs",
           "Each scenario registers a simulated tool against v1 of an in-process API, switches the API to v2 and "
           "calls the tool. Nothing here is production traffic; all results are labelled simulated.")
    st.markdown(f"{SIM_BADGE} &nbsp; Upstreams are in-process simulations (sim://).", unsafe_allow_html=True)
    cat = safe_call(api, "GET", "/api/simulations")
    if cat is None:
        return
    groups = list(dict.fromkeys(s["group"] for s in cat))      # catalog order
    c1, c2, c3 = st.columns([1, 2, 1])
    group = c1.selectbox("Group", groups)
    items = [s for s in cat if s["group"] == group]
    s = c2.selectbox("Scenario", items, format_func=lambda s: s["title"])
    mode = c3.selectbox("Run", ["both", "adaptix", "baseline"],
                        format_func={"both": "Baseline vs ADAPTIX", "adaptix": "ADAPTIX only",
                                     "baseline": "Baseline only"}.get)
    st.markdown(s["description"])
    st.caption(f"Expected with ADAPTIX: **{s['expected_now']}**"
               + (" · needs a real LLM; without one it stops safely" if s["needs_llm"] else ""))
    with st.expander("Schema change", icon=":material/difference:"):
        a, b = st.columns(2)
        a.markdown("**v1 request**")
        a.json(s["v1"])
        b.markdown("**v2 request**")
        b.json(s["v2"])
        extra = {k: s[k] for k in ("v2_headers", "v2_response", "v2_faults", "v2_method", "auth") if s.get(k)}
        if extra:
            st.markdown("**Other v2 changes**")
            st.json(extra)
        st.markdown("**Caller arguments**")
        st.json(s["args"])
    b1, b2, b3 = st.columns([1, 1, 1])
    if b1.button("Run scenario", type="primary", icon=":material/bolt:"):
        with st.spinner("Running…"):
            res = safe_call(api, "POST", "/api/simulations", json={"scenario": s["id"], "mode": mode})
        if res:
            st.session_state["chaos_last"] = res["runs"]
    if b2.button("Run all scenarios", icon=":material/playlist_play:"):
        out, bar = [], st.progress(0.0)
        for i, sc in enumerate(cat):
            r = safe_call(api, "POST", "/api/simulations", json={"scenario": sc["id"], "mode": "adaptix"})
            if r:
                out += r["runs"]
            bar.progress((i + 1) / len(cat))
        st.session_state["chaos_all"] = out
    if b3.button("Reset simulated data", icon=":material/restart_alt:"):
        if safe_call(api, "POST", "/api/simulations/reset"):
            st.session_state.pop("chaos_last", None)
            st.session_state.pop("chaos_all", None)
            st.success("Simulated tools, incidents and events removed. Real data is untouched.")
    if st.session_state.get("chaos_last"):
        cols = st.columns(len(st.session_state["chaos_last"]))
        for col, r in zip(cols, st.session_state["chaos_last"]):
            with col:
                _result(r)
    if st.session_state.get("chaos_all"):
        rows = st.session_state["chaos_all"]
        ok = sum(r["matches_expectation"] for r in rows)
        st.markdown(f"**All scenarios (ADAPTIX):** {ok}/{len(rows)} matched the expected outcome")
        st.dataframe([{"scenario": r["title"], "expected": r["expected"], "outcome": r["outcome"],
                       "as expected": r["matches_expectation"], "category": r.get("category"),
                       "duration": fmt_ms(r["duration_ms"]), "reason": r.get("reason")} for r in rows],
                     hide_index=True, use_container_width=True)
