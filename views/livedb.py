"""Live Project Integration: Neon PostgreSQL demo database, controlled schema-drift experiments and recovery."""
import json

import pandas as pd
import streamlit as st

from views.common import api, badge, fmt_ms, header, kpis, safe_call

DEMO_BADGE = ('<span class="ax-badge" style="background:#FFF7ED;color:#9A3412;border-color:#FDBA74">'
              'DEMO EXPERIMENT · synthetic data · not production monitoring</span>')
ENGINE_LABEL = {"gemini": "Gemini Flash-Lite (existing)", "local": "Local Repair Module"}
STAGE_LABEL = {"baseline_test": "Baseline test", "fault_injected": "Injected fault", "failure_detected": "Failure detection",
               "schema_retrieved": "Schema retrieval", "diagnosis": "Diagnosis", "proposal": "Proposal",
               "validation": "Validation", "sandbox_test": "Sandbox test", "patch_applied": "Patch application",
               "retry": "Retry", "business_verification": "Business verification", "rollback": "Rollback"}
STATE_CHIP = {"passed": "VERIFIED", "applied": "CONFIGURED", "detected": "ACTION REQUIRED", "retrieved": "CONFIGURED",
              "diagnosed": "CONFIGURED", "proposed": "CONFIGURED", "validated": "VERIFIED", "retried": "CONFIGURED",
              "verified": "VERIFIED", "rolled back": "UNSUPPORTED", "not required": "NOT REQUIRED", "refused": "ACTION REQUIRED",
              "failed": "FAILED", "not reproduced": "NOT RUN"}
SETUP_DOC = """
**1. Create a Neon project.** Sign in at neon.tech, create a project (the free plan is enough) and open its **SQL Editor**.

**2. (Recommended) Create a least-privilege role.** Paste `database/roles.sql`, replace `<CHOOSE_A_STRONG_PASSWORD>`
**in the editor only**, then run it. This creates the role `adaptix_app`, which owns only `adaptix_demo` and `adaptix_demo_sandbox`.

**3. Create the tables and the synthetic data.** Use one of these:
* Click **Set up demo database** on this page. It runs `database/init_demo.sql` as `adaptix_app`. This is recommended because
  the role then owns the tables and can run the reversible demo faults.
* Or paste `database/init_demo.sql` into the SQL Editor and run it. If you run it as the owner, also run the
  ownership snippet at the end of `database/roles.sql`.

**4. Add the connection string as a server-side secret.** In Neon, open **Connect** and choose the `adaptix_app` role.
Copy the connection string. It includes `sslmode=require`.
* Streamlit Community Cloud: open your app → **⋮ → Settings → Secrets**, then add
  `DATABASE_URL = "postgresql://adaptix_app:<password>@<host>/<database>?sslmode=require"` and click **Save**.
* Locally: put the same line in `.streamlit/secrets.toml`. That file is git-ignored, so never commit it.

**5.** Click **Test connection**, then **Set up demo database**.

**Reset:** **Reset to baseline** drops and recreates only the five business tables in `adaptix_demo` from `init_demo.sql`.
The experiment and audit logs are kept.
"""


def _chip(state: str) -> str:
    return badge(STATE_CHIP.get(state, "NOT RUN")).replace(STATE_CHIP.get(state, "NOT RUN"), state.upper())


def _setup(status: dict):
    if status["configured"]:
        t = status["target"]
        st.markdown(f"Target **{t['host']}** · database `{t['database']}` · TLS `{t['sslmode']}` · demo schema "
                    f"`{t['schema']}` · sandbox `{t['sandbox']}`")
    else:
        st.warning(status["problem"], icon=":material/key_off:")
    c1, c2, c3 = st.columns(3)
    if c1.button("Test connection", icon=":material/network_check:", disabled=not status["configured"]):
        st.session_state["ldb_conn"] = safe_call(api, "POST", "/api/livedb/test-connection")
    if c2.button("Set up demo database", icon=":material/database:", disabled=not status["configured"],
                 help="Creates whatever is missing from database/init_demo.sql (schemas, log tables, business tables)"):
        if safe_call(api, "POST", "/api/livedb/setup", json={}):
            st.rerun()
    confirm = c3.checkbox("I understand: reset recreates the demo tables")
    if c3.button("Reset to baseline", icon=":material/restart_alt:", disabled=not (status["configured"] and confirm)):
        if safe_call(api, "POST", "/api/livedb/setup", json={"reset": True, "confirm": True}):
            st.session_state.pop("ldb_exp", None)
            st.success("Business tables recreated from the baseline and the workflow mappings were reset. "
                       "The experiment logs were kept.")
    conn = st.session_state.get("ldb_conn")
    if conn:
        st.markdown(f"{badge('VERIFIED')} connected in {conn['latency_ms']} ms · `{conn['server']}` · SSL `{conn['ssl']}` "
                    f"· demo tables: {conn['demo_tables']}", unsafe_allow_html=True)
        for w in conn["warnings"]:
            st.warning(w, icon=":material/shield:")
    with st.expander("Setup instructions: Neon and Streamlit Cloud", expanded=not status["configured"]):
        st.markdown(SETUP_DOC)


def _data():
    s = safe_call(api, "GET", "/api/livedb/seed-summary")
    if s:
        st.markdown("**Seed data summary** (computed by the database)")
        kpis([(k.replace("_", " "), v, "rows") for k, v in s["counts"].items()][:4])
        st.caption(" · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in s["checks"].items())
                   + f" · inventory events: {s['counts']['inventory_events']}")
    tables = safe_call(api, "GET", "/api/livedb/tables")
    if not tables:
        return
    st.dataframe(pd.DataFrame([{"table": t["table"], "kind": t["kind"], "rows": t["rows"], "columns": len(t["columns"])}
                               for t in tables]), hide_index=True, use_container_width=True)
    name = st.selectbox("Table", [t["table"] for t in tables])
    tbl = next(t for t in tables if t["table"] == name)
    st.dataframe(pd.DataFrame([{"#": c["attnum"], "column": c["name"], "type": c["type"], "nullable": c["nullable"],
                                "default": c["default"] or "", "comment": c["comment"] or ""} for c in tbl["columns"]]),
                 hide_index=True, use_container_width=True)
    data = safe_call(api, "GET", f"/api/livedb/tables/{name}", params={"limit": 15})
    if data:
        st.markdown("**Sample records** (synthetic)")
        st.dataframe(pd.DataFrame(data["rows"]), hide_index=True, use_container_width=True)


def _schema():
    s = safe_call(api, "GET", "/api/livedb/schema")
    if not s:
        return
    d = s["diff"]
    (st.success if not d["changed"] else st.warning)(
        "No schema drift detected (current = baseline)" if not d["changed"] else f"Current schema differs: {d['summary']}")
    base = {(c["table"], c["attnum"]): c for c in s["baseline"]}
    cur = {(c["table"], c["attnum"]): c for c in s["current"]}
    rows = []
    for key in sorted(set(base) | set(cur)):
        b, c = base.get(key), cur.get(key)
        state = ("unchanged" if b and c and b["column"] == c["column"] and b["type"] == c["type"] else
                 "renamed" if b and c and b["column"] != c["column"] else "type changed" if b and c else
                 "removed" if b else "added")
        rows.append({"table": key[0], "attnum": key[1], "baseline column": (b or {}).get("column", "—"),
                     "current column": (c or {}).get("column", "—"), "baseline type": (b or {}).get("type", "—"),
                     "current type": (c or {}).get("type", "—"), "required now": (c or {}).get("required"),
                     "state": state})
    changed_only = st.toggle("Show changed columns only", value=d["changed"])
    df = pd.DataFrame(rows)
    st.dataframe(df[df["state"] != "unchanged"] if changed_only else df, hide_index=True, use_container_width=True)
    st.caption(f"Baseline fingerprint `{s['baseline_signature']}` · current `{s['current_signature']}`. Columns are "
               "matched by catalog attnum, so a rename is shown as the same column with a new name.")


def _timeline(r: dict):
    st.markdown(f"{DEMO_BADGE} &nbsp; `{r['experiment_id']}` · scenario **{r['scenario']}** · workflow "
                f"`{r['workflow']}` · engine `{r['engine']}` · result **{r['status'].upper()}**"
                + (f" · verified recovery in {fmt_ms(r['recovery_ms'])}" if r.get("verified") else ""),
                unsafe_allow_html=True)
    if r.get("error"):
        (st.info if r["status"] == "refused" else st.error)(r["error"])
    for s in r["stages"]:
        c1, c2, c3 = st.columns([1.3, 1.1, 4])
        c1.markdown(f"**{STAGE_LABEL.get(s['stage'], s['stage'])}**<br><span style='color:#64748B;font-size:.8rem'>"
                    f"{s['ts'][11:23]}Z</span>", unsafe_allow_html=True)
        c2.markdown(_chip(s["status"]), unsafe_allow_html=True)
        c3.write(s["title"])
        det = s.get("detail") or {}
        if det.get("patch"):
            c3.code(det["patch"], language="diff")
        if det.get("validation"):
            with c3.expander("Validation checks"):
                st.dataframe(pd.DataFrame(det["validation"]["checks"]), hide_index=True, use_container_width=True)
        if det.get("verification"):
            with c3.expander("Business verification evidence"):
                st.json(det["verification"], expanded=1)
        if det.get("engine"):
            c3.caption("Engine call: " + json.dumps(det["engine"]))
        if det.get("ddl"):
            c3.code("\n".join(det["ddl"]), language="sql")


def _experiments(status: dict):
    st.markdown(DEMO_BADGE, unsafe_allow_html=True)
    wf = safe_call(api, "GET", "/api/livedb/workflows")
    if not wf:
        return
    with st.expander("Business workflows (fixed SQL, versioned column mappings)"):
        for k, w in wf["workflows"].items():
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{w['title']}** (`{k}`) · {w['kind']} · parameters {w['params']} · mapping v{w['version']}")
            if c2.button("Run check", key=f"wf-{k}", help="Runs and verifies the workflow; writes are rolled back"):
                r = safe_call(api, "POST", f"/api/livedb/workflows/{k}/run")
                if r:
                    (st.success if r["ok"] else st.error)(
                        f"{k}: " + ((r.get("verification") or {}).get("summary") or f"SQLSTATE {r['sqlstate']}: {r['error']}"))
    sc = wf["scenarios"]
    c1, c2 = st.columns([2.2, 1.4])
    scenario = c1.selectbox("Fault scenario", list(sc), format_func=lambda k: f"{k}. {sc[k]['title']}")
    c1.caption(f"Workflow under test: `{sc[scenario]['workflow']}` · expected outcome: **{sc[scenario]['expect']}**")
    engines = status["engines"]
    engine = c2.radio("Repair engine for this experiment", list(ENGINE_LABEL), format_func=ENGINE_LABEL.get)
    info = engines[engine]
    c2.markdown(f"{badge('CONFIGURED' if info['available'] else 'ACTION REQUIRED')} {info['status']}: {info['detail']}",
                unsafe_allow_html=True)
    st.warning("Running an experiment changes the demo schema or a workflow mapping, inside the isolated demo schema "
               "only. It must start from the baseline. Use Reset to baseline afterwards.", icon=":material/warning:")
    confirm = st.checkbox("I confirm this schema-changing demo experiment")
    if st.button("Run experiment", type="primary", icon=":material/science:", disabled=not (confirm and info["available"])):
        with st.spinner("Running against the live database…"):
            st.session_state["ldb_exp"] = safe_call(api, "POST", "/api/livedb/experiment",
                                                    json={"scenario": scenario, "engine": engine, "confirm": True})
        st.rerun()
    if not info["available"]:
        st.caption("The selected engine is not available, so the experiment cannot run. ADAPTIX never switches to "
                   "another engine.")
    r = st.session_state.get("ldb_exp")
    if r:
        with st.container(border=True):
            _timeline(r)


def _automatic(status: dict):
    cfg = status["schedule"]
    st.error("Automatic fault injection changes the demo schema on a timer. Each run first resets the demo tables to "
             "the baseline, then applies one fault and attempts recovery. It is off by default.", icon=":material/warning:")
    if st.button("STOP NOW", type="primary", icon=":material/stop_circle:",
                 help="Disables automatic injection and interrupts a running experiment at its next stage"):
        if safe_call(api, "POST", "/api/livedb/stop"):
            st.success("Stopped. Automatic injection is disabled.")
            st.rerun()
    a, b, c = st.columns(3)
    enabled = a.toggle("Enable automatic injection", cfg["enabled"])
    interval = b.number_input("Interval (seconds)", 60, 86400, int(cfg["interval_s"]), step=60)
    max_runs = c.number_input("Maximum runs", 1, 50, int(cfg["max_runs"]))
    d, e = st.columns(2)
    scenarios = d.multiselect("Scenarios (cycled)", list("ABCDEF"), cfg["scenarios"])
    engines = [k for k, v in status["engines"].items() if v["available"]] or ["gemini"]
    eng = e.selectbox("Engine", engines, format_func=ENGINE_LABEL.get,
                      index=engines.index(cfg["engine"]) if cfg["engine"] in engines else 0)
    ok = st.checkbox("I confirm repeated schema-changing demo experiments", disabled=not enabled)
    st.caption(f"Runs done: {cfg['runs_done']} / {cfg['max_runs']} · currently "
               f"{'ENABLED' if cfg['enabled'] else 'disabled'}. The schedule only runs while the app is running, and it "
               "disables itself after the maximum number of runs.")
    if st.button("Save automatic injection settings", disabled=not scenarios or (enabled and not ok)):
        if safe_call(api, "PUT", "/api/livedb/schedule", json={"enabled": enabled, "interval_s": int(interval),
                                                              "max_runs": int(max_runs), "engine": eng,
                                                              "scenarios": scenarios, "confirm": ok}):
            st.rerun()


def _stats():
    s = safe_call(api, "GET", "/api/livedb/stats")
    if not s:
        return
    st.caption(s["note"])
    kpis([("Runs", s["runs"], ", ".join(f"{k} {v}" for k, v in s["by_status"].items()) or "none yet"),
          ("Detected faults", s["detected_faults"], f"{s['proposals']} proposal(s)"),
          ("Validated patches", s["validated_patches"], f"{s['applied_patches']} applied"),
          ("Verified recoveries", s["verified_recoveries"], f"{s['failures']} failed · {s['refused']} refused")])
    kpis([("Rollbacks", s["rollbacks"], "after failed verification"),
          ("Average recovery", fmt_ms(s["avg_recovery_ms"]), f"{s['recovery_samples']} verified recoveries"),
          ("Median recovery", fmt_ms(s["median_recovery_ms"]), "failure detection to verified result"),
          ("Proposals ≠ recoveries", "✓", "only verified results are counted")])
    data = safe_call(api, "GET", "/api/livedb/experiments")
    if data and data["experiments"]:
        st.markdown("**Experiments** (from `adaptix_experiments`)")
        st.dataframe(pd.DataFrame(data["experiments"]).drop(columns=["error_details"], errors="ignore"),
                     hide_index=True, use_container_width=True)
        st.markdown("**Audit log** (latest 60, from `adaptix_audit_log`)")
        st.dataframe(pd.DataFrame([{"time": a["event_time"][:19], "experiment": a["experiment_id"],
                                    "event": a["event_type"], "status": (a["details_json"] or {}).get("status"),
                                    "title": (a["details_json"] or {}).get("title")} for a in data["audit_log"][-60:]]),
                     hide_index=True, use_container_width=True)
        bundle = safe_call(api, "GET", "/api/livedb/export.json")
        csv_text = safe_call(api, "GET", "/api/livedb/export.csv")
        d1, d2 = st.columns(2)
        if bundle:
            d1.download_button("Export redacted evidence (JSON)", json.dumps(bundle, indent=2, default=str),
                               "adaptix-live-db-evidence.json", "application/json", icon=":material/download:")
        if isinstance(csv_text, str):
            d2.download_button("Export experiments (CSV)", csv_text, "adaptix-live-db-experiments.csv", "text/csv",
                               icon=":material/table:")


def _monitor():
    st.markdown("Read-only: compares the live catalog with the baseline. It never injects or writes.")
    if st.button("Check schema now", icon=":material/refresh:"):
        st.session_state["ldb_mon"] = safe_call(api, "GET", "/api/livedb/monitor")
    m = st.session_state.get("ldb_mon")
    if m:
        (st.success if not m["drift"] else st.warning)(f"{m['summary']} · checked {m['checked_at'][:19]}Z")


def render():
    header("Live Project Integration", "Neon PostgreSQL demo database",
           "A real hosted PostgreSQL database with deterministic synthetic data, used to demonstrate schema-drift recovery. "
           "Experiments change only the isolated adaptix_demo schema. This is kept separate from Real API Monitoring "
           "and from the Simulation Lab.")
    st.markdown(DEMO_BADGE, unsafe_allow_html=True)
    status = safe_call(api, "GET", "/api/livedb/status")
    if status is None:
        return
    tabs = st.tabs(["Connection & setup", "Data", "Schema comparison", "Experiments", "Automatic injection",
                    "Statistics & export", "Monitoring (read-only)"])
    with tabs[0]:
        _setup(status)
    for tab, fn in zip(tabs[1:], (_data, _schema, lambda: _experiments(status), lambda: _automatic(status), _stats,
                                  _monitor)):
        with tab:
            if not status["configured"]:
                st.info("Configure DATABASE_URL first (see Connection & setup).", icon=":material/info:")
            elif not status["set_up"]:
                st.info("Click **Set up demo database** first (Connection & setup).", icon=":material/info:")
            else:
                fn()
