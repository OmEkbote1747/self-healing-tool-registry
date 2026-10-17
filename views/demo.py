"""Drift Demo: introduce ONE controlled schema change; ADAPTIX detects it and recovers automatically."""
import time

import streamlit as st

from views.common import api, badge, fmt_ms, go, header, ist, mutated, safe_call

TEST_BADGE = ('<span class="ax-badge" style="background:#EEF2FF;color:#3730A3;border-color:#A5B4FC">'
              'REAL API TEST · controlled demo service, not production</span>')
ICON = {"ok": "✅", "fail": "❌", "warn": "⚠️", "info": "•"}
OUTCOME_TEXT = {"recovered": "Recovered and verified", "stopped": "Stopped safely (no unsafe repair applied)",
                "rolled_back": "Repair rolled back (verification failed)", "failed": "Failed", "blocked": "Did not run"}


def _events(job: dict, box):
    with box.container():
        for e in job["events"]:
            st.markdown(f"{ICON.get(e['status'], '•')} **{e['phase']}** · {e['title']} "
                        f"<span style='color:#64748B;font-size:.8rem'>+{fmt_ms(e['elapsed_ms'])}</span>",
                        unsafe_allow_html=True)


def _result(job: dict):
    r = job.get("result") or {}
    status = r.get("status") or ("failed" if job["status"] == "error" else "running")
    with st.container(border=True):
        st.markdown(f"### {OUTCOME_TEXT.get(status, status)}")
        st.markdown(f"{TEST_BADGE} &nbsp; {badge(status if status != 'blocked' else 'NOT RUN')} &nbsp; "
                    f"started {ist(job['started_at'])} · total {fmt_ms(job['elapsed_ms'])}"
                    + (f" · expected **{r['expected']}** ({'as expected' if r.get('as_expected') else 'NOT as expected'})"
                       if r.get("expected") else "")
                    + (f" · LLM: {r['llm']['label']}" if r.get("llm") else "")
                    + (f" · patch proposed by **{r['repair_by']}**" if r.get("repair_by") else ""),
                    unsafe_allow_html=True)
        if r.get("reason"):
            st.write(r["reason"])
        if job.get("error"):
            st.error(job["error"])
        c1, c2 = st.columns(2)
        if r.get("incident") and c1.button("Open in Recovery Center", icon=":material/timeline:"):
            go("recovery", incident_id=r["incident"], env_choice="REAL API TEST")
        if r.get("evidence_id") and c2.button("Open in Evidence & Scorecard", icon=":material/gavel:"):
            go("evidence", ev_pick=r.get("incident"), env_choice="REAL API TEST")


def render():
    header("Drift Demo", "Introduce a schema change and watch ADAPTIX recover",
           "Pick a change and confirm it. ADAPTIX first checks that the call works, applies the change to its own "
           "controlled test API, then makes the same call again. The failure is detected and repaired automatically: "
           "diagnosis, proposal, validation, patch, retry and verification. You do not need a second action.")
    st.markdown(TEST_BADGE, unsafe_allow_html=True)
    cat = safe_call(api, "GET", "/api/demo/scenarios")
    if cat is None:
        return
    opts = {s["key"]: s for s in cat["scenarios"]}
    c1, c2 = st.columns([2.2, 1])
    key = c1.selectbox("1. Choose the change to introduce", list(opts),
                       format_func=lambda k: opts[k]["title"] + ("" if opts[k]["available"] else " (needs GEMINI_API_KEY)"))
    s = opts[key]
    llm_choices = ["gemini"] if s["needs_gemini"] else (["none", "gemini"] if cat["gemini_configured"] else ["none"])
    llm = c2.selectbox("2. Repair engine", llm_choices,
                       format_func={"none": "Rules only (deterministic)", "gemini": "Gemini Flash-Lite (real)"}.get,
                       help="Gemini is offered only when GEMINI_API_KEY is configured. With Gemini, one real request "
                            "is made and Gemini proposes the repair; rules only cross-check it and independent "
                            "validation decides whether it is applied. ADAPTIX never switches engines.")
    st.caption(f"Change: **{s['change']}** · expected outcome: **{s['expected']}**")
    if not s["available"]:
        st.warning("This scenario needs a real Gemini key on the server (GEMINI_API_KEY). Nothing will run without it.",
                   icon=":material/key_off:")
    confirm = st.checkbox("3. I confirm: change the controlled demo API (never a real provider)", key="demo_confirm")
    start = st.button("Introduce change", type="primary", icon=":material/bolt:",
                      disabled=not (confirm and s["available"]) or cat["running"])
    if start:
        job = safe_call(api, "POST", "/api/demo/run", json={"scenario": key, "llm": llm, "confirm": True})
        if job:
            mutated()
            st.session_state["demo_job"] = job["id"]
    job_id = st.session_state.get("demo_job")
    if job_id:
        box = st.empty()
        job = safe_call(api, "GET", f"/api/demo/jobs/{job_id}")
        with st.status("Running…" if job and job["status"] == "running" else "Finished", expanded=True) as stat:
            while job and job["status"] == "running":
                _events(job, box)
                time.sleep(0.4)
                job = safe_call(api, "GET", f"/api/demo/jobs/{job_id}")
            if job:
                _events(job, box)
                stat.update(label=f"Finished in {fmt_ms(job['elapsed_ms'])}", state="complete" if
                            (job.get("result") or {}).get("status") == "recovered" else "error", expanded=True)
        if job:
            _result(job)
    with st.expander("Run every scenario as a batch (scenario matrix)"):
        status = safe_call(api, "GET", "/api/test/status")
        if status:
            from views.realtest import matrix_panel
            matrix_panel(status)
    with st.expander("Reset the controlled demo to a known-good state"):
        st.caption("Clears the REAL API TEST records (incidents, versions, evidence) and returns the test service to "
                   "schema v1. Production and simulation data are not touched.")
        ok = st.checkbox("I understand this deletes the REAL API TEST records", key="demo_reset_ok")
        if st.button("Reset demo", disabled=not ok):
            if safe_call(api, "POST", "/api/demo/reset", json={"confirm": True}):
                mutated()
                st.session_state.pop("demo_job", None)
                st.success("Demo reset to a known-good state.")
