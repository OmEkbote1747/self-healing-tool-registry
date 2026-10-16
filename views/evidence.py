"""Evidence & Scorecard: inspect and export judge-verifiable records per environment."""
import json

import pandas as pd
import streamlit as st

from views.common import api, header, kpis, safe_call

ENVS = {"REAL_API_TEST": ("/api/test", "REAL API TEST"), "LIVE_API": ("/api", "REAL API"),
        "SIMULATION": ("/api/sim", "SIMULATION"), "LIVE_DB_DEMO": ("/api/livedb", "LIVE DB DEMO")}


def _pct(v, d):
    return "—" if v is None else f"{v:.0f}% ({d})"


def render():
    header("Evidence & Scorecard", "Judge-verifiable records",
           "One evidence record per run: environment, API, schema before/after, request and response (redacted), "
           "classification, diff, Gemini usage, proposal, validation, applied change, retries, verification, rollback "
           "and timing. Rates always show their denominators.")
    env = st.radio("Environment", list(ENVS), horizontal=True, format_func=lambda e: ENVS[e][1], key="ev_env")
    prefix, label = ENVS[env]
    sc = safe_call(api, "GET", f"{prefix}/scorecard")
    recs = safe_call(api, "GET", f"{prefix}/evidence", params={"limit": 500})
    if sc is None or recs is None:
        return
    st.info(sc["disclosure"], icon=":material/info:")
    kpis([("Eligible incidents", sc["eligible_incidents"], "runs where a repair was the correct outcome"),
          ("Verified recoveries", _pct(sc["verified_recovery_rate"], f"{sc['verified_recoveries']}/{sc['eligible_incidents']}"),
           "task outcome verified, not just HTTP 200"),
          ("Unsafe repairs accepted", sc["unsafe_repairs_accepted"], f"of {sc['records']} runs"),
          ("False-positive drift", sc["false_positive_drift"], f"of {sc['false_positive_denominator']} runs with a known category")])
    lat = sc["latency"]
    kpis([("Repairs applied", sc["repairs_applied"], f"{sc['successful_repairs']} succeeded · {sc['failed_repairs']} failed"),
          ("Rejected proposals", sc["rejected_repairs"], "stopped by independent validation"),
          ("Rollback success", _pct(sc["rollback_success_rate"], sc["rollbacks_attempted"]), "rollbacks needed"),
          ("Median recovery", "—" if lat["median_ms"] is None else f"{lat['median_ms']:.0f} ms",
           lat["note"] or (f"p90 {lat['p90_ms']} ms" + (f" · p95 {lat['p95_ms']} ms" if lat["p95_ms"] else "")))])
    st.caption(f"Real Gemini requests in these runs: {sc['real_llm_requests']}")
    e1, e2 = st.columns(2)
    bundle = safe_call(api, "GET", f"{prefix}/evidence/export.json")
    csv_text = safe_call(api, "GET", f"{prefix}/evidence/export.csv")
    if bundle is not None:
        e1.download_button("Export evidence bundle (JSON)", json.dumps(bundle, indent=2, default=str),
                           f"adaptix-evidence-{env.lower()}.json", "application/json", icon=":material/download:",
                           disabled=not recs)
    if csv_text is not None:
        e2.download_button("Export report (CSV)", csv_text if isinstance(csv_text, str) else str(csv_text),
                           f"adaptix-evidence-{env.lower()}.csv", "text/csv", icon=":material/table:", disabled=not recs)
    if not recs:
        st.info(f"No {label} evidence recorded yet.", icon=":material/inbox:")
        return
    if sc["by_scenario"]:
        st.subheader("By scenario")
        st.dataframe(pd.DataFrame([{"scenario": k, **{x: y for x, y in v.items() if x != "environments"}}
                                   for k, v in sc["by_scenario"].items()]), hide_index=True, use_container_width=True)
    st.subheader("Records")
    df = pd.DataFrame([{"evidence": r["evidence_id"], "recorded": r["recorded_at"][:19].replace("T", " "),
                        "scenario": r.get("scenario"), "tool": r["api"]["tool"], "classification":
                        r["first_failure"]["classification"], "Gemini": "REAL" if r["llm"]["real_request_performed"]
                        else ("used (not real)" if r["llm"]["used"] else "not used"),
                        "outcome": r["final"]["outcome"], "verified": r["final"]["verified_recovery"],
                        "as expected": r["expected"]["matches"], "elapsed ms": r["timing"]["elapsed_ms"]} for r in recs])
    sel = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                       key="ev-table")
    rows = (sel.selection.rows if sel and hasattr(sel, "selection") else []) or []
    want = st.session_state.pop("ev_pick", None)
    idx = rows[0] if rows else next((i for i, r in enumerate(recs) if r["evidence_id"] == want), None)
    if idx is None:
        st.caption("Select a record to inspect it.")
        return
    r = recs[idx]
    with st.container(border=True):
        st.markdown(f"**{r['evidence_id']}** · {label} · run `{r.get('run_id')}` · incident `{r.get('incident_id')}`")
        st.caption(r["evidence_source"])
        st.download_button("Download this record (JSON)", json.dumps(r, indent=2, default=str),
                           f"{r['evidence_id']}.json", "application/json", key="ev-one")
        st.json(r, expanded=1)
