"""Live Project Integration: an online PostgreSQL (Neon) demo schema, fault injection and verified recovery."""
import json

import pandas as pd
import streamlit as st

from views.common import api, badge, fmt_ms, header, kpis, safe_call

LIVE_BADGE = '<span class="ax-badge" style="background:#ECFEFF;color:#155E75;border-color:#67E8F9">LIVE DB DEMO</span>'
FAULT_LABEL = {"rename_column": "Rename column", "change_type": "Change column type",
               "add_required_column": "Add required column", "remove_column": "Remove column"}
ENGINE_LABEL = {"gemini": "Gemini (existing, Flash-Lite)", "local": "Local Repair Module"}
PHASE_LABEL = {"execute": "Execute", "diagnose": "Catalog + diff", "propose": "Proposal", "validate": "Validation",
               "sandbox_test": "Sandbox test", "apply": "Apply", "retry_and_verify": "Retry + verify"}
SETUP_DOC = """
**1. Create a Neon project** at neon.tech (free tier is enough) and copy its connection string.

**2. Use a dedicated least-privilege role.** In Neon's SQL editor, run this as the owner:
```sql
CREATE ROLE adaptix_app LOGIN PASSWORD '<generate a strong password>';
CREATE SCHEMA adaptix_demo AUTHORIZATION adaptix_app;
CREATE SCHEMA adaptix_demo_sandbox AUTHORIZATION adaptix_app;
REVOKE CREATE ON SCHEMA public FROM adaptix_app;
```
The role then owns only the two demo schemas: it cannot read or change anything else.

**3. Set the secret on the server.** Never commit it, and never paste it into code.
* Locally: add a line to `.streamlit/secrets.toml` (this file is git-ignored):
  `DATABASE_URL = "postgresql://adaptix_app:<password>@<host>/<db>?sslmode=require"`
* On Streamlit Community Cloud: open the app → **⋮ → Settings → Secrets**, paste the same line, then Save.

**4. Optional settings:**
* `ADAPTIX_LIVE_DB_SCHEMA` sets the demo schema name. It must start with `adaptix_` (default `adaptix_demo`).
* `ADAPTIX_LOCAL_REPAIR_MODULE` is the Python import path of the Local Repair Module.
  The interface is in `examples/local_repair_module_template.py`.

**5.** Click **Test connection**, then **Set up demo schema**.
"""


def _cols(cols: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"#": c["attnum"], "column": c["name"], "type": c["type"], "nullable": c["nullable"],
                          "default": c["default"] or "", "comment": c["comment"] or ""} for c in cols])


def _setup_tab(status: dict):
    if status["configured"]:
        t = status["target"]
        st.markdown(f"Target **{t['host']}** · database `{t['database']}` · TLS `{t['sslmode']}` · demo schema "
                    f"`{t['schema']}` · sandbox `{t['sandbox']}`")
    else:
        st.warning(status["problem"], icon=":material/key_off:")
    c1, c2, c3 = st.columns(3)
    if c1.button("Test connection", icon=":material/network_check:", disabled=not status["configured"]):
        st.session_state["ldb_conn"] = safe_call(api, "POST", "/api/livedb/test-connection")
    if c2.button("Set up demo schema", icon=":material/database:", disabled=not status["configured"],
                 help="Creates the demo and sandbox schemas with synthetic data if missing, and records the baseline"):
        if safe_call(api, "POST", "/api/livedb/setup", json={"reset": False}):
            st.rerun()
    confirm = c3.checkbox("I want to drop and recreate the demo schema")
    if c3.button("Reset to baseline", icon=":material/restart_alt:", disabled=not (status["configured"] and confirm)):
        if safe_call(api, "POST", "/api/livedb/setup", json={"reset": True, "confirm": True}):
            st.success("Demo schema dropped and recreated from the baseline; the tool registry was reset.")
    conn = st.session_state.get("ldb_conn")
    if conn:
        st.markdown(f"{badge('VERIFIED')} connected in {conn['latency_ms']} ms · `{conn['server']}` · SSL `{conn['ssl']}` "
                    f"· demo schema exists: {conn['demo_schema_exists']}", unsafe_allow_html=True)
        for w in conn["warnings"]:
            st.warning(w, icon=":material/shield:")
    with st.expander("Setup instructions (local and Streamlit Cloud)", expanded=not status["configured"]):
        st.markdown(SETUP_DOC)


def _tables_tab():
    tables = safe_call(api, "GET", "/api/livedb/tables")
    if not tables:
        return
    st.dataframe(pd.DataFrame([{"table": t["table"], "rows": t["rows"], "columns": len(t["columns"])} for t in tables]),
                 hide_index=True, use_container_width=True)
    name = st.selectbox("Table", [t["table"] for t in tables])
    tbl = next(t for t in tables if t["table"] == name)
    st.markdown("**Columns**")
    st.dataframe(_cols(tbl["columns"]), hide_index=True, use_container_width=True)
    data = safe_call(api, "GET", f"/api/livedb/tables/{name}", params={"limit": 20})
    if data:
        st.markdown("**Latest rows (synthetic data)**")
        st.dataframe(pd.DataFrame(data["rows"]), hide_index=True, use_container_width=True)


def _schema_tab():
    s = safe_call(api, "GET", "/api/livedb/schema")
    if not s:
        return
    base, cur = s["baseline"], s["current"]
    names = list(dict.fromkeys(list(base) + list(cur)))
    rows = []
    for n in names:
        b, c = base.get(n), cur.get(n)
        renamed_to = next((k for k, v in cur.items() if v.get("renamed_from") == n), None)
        state = "unchanged" if b and c and b["type"] == c["type"] and b["required"] == c["required"] else \
            ("renamed to " + renamed_to if renamed_to else "removed" if b and not c else
             ("renamed from " + c["renamed_from"] if c and c.get("renamed_from") else "added" if c and not b else "changed"))
        rows.append({"column": n, "baseline type": (b or {}).get("type") or "—", "current type": (c or {}).get("type") or "—",
                     "baseline required": (b or {}).get("required"), "current required": (c or {}).get("required"),
                     "state": state, "tool maps": " ← ".join([n, s["mapping"][n][0]]) if n in s["mapping"] else "—"})
    st.markdown(f"Baseline `{s['baseline_signature']}` → current `{s['current_signature']}` · tool version "
                f"v{s['tool_version']}")
    rep = s["vs_baseline"]
    (st.success if not rep["changed"] else st.warning)(
        "No schema drift detected" if not rep["changed"] else f"Schema drift vs baseline: {rep['summary']}")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if s["vs_active_tool"]["changed"]:
        st.caption(f"Against the tool's active version: {s['vs_active_tool']['summary']}")


def _faults_tab(status: dict):
    st.markdown("Faults change only the isolated demo schema. Each one records the exact DDL, before/after columns, "
                "time and any error.")
    c1, c2, c3 = st.columns([1.4, 1, 1])
    fault = c1.selectbox("Fault", list(FAULT_LABEL), format_func=FAULT_LABEL.get)
    cols = ["customer_id", "sku", "quantity", "note"]
    body = {"fault": fault}
    if fault == "rename_column":
        body["column"] = c2.selectbox("Column", cols)
        body["new_name"] = c3.text_input("New name", "client_id")
    elif fault == "change_type":
        body["column"] = c2.selectbox("Column", cols, index=2)
        body["new_type"] = c3.selectbox("New type", ["text", "varchar(64)", "bigint", "numeric", "integer", "jsonb"])
    elif fault == "add_required_column":
        body["new_name"] = c2.text_input("Column name", "channel")
        c3.caption("Added as NOT NULL text without a default for new rows.")
    else:
        body["column"] = c2.selectbox("Column", cols, index=3)
    if st.button("Inject fault", type="primary", icon=":material/bug_report:"):
        r = safe_call(api, "POST", "/api/livedb/inject", json=body)
        if r:
            (st.success if r["ok"] else st.error)(("Applied: " if r["ok"] else "Failed (nothing changed): ")
                                                  + (r["error"] or "; ".join(r["ddl"])))
    with st.expander("Scheduled automatic injection", icon=":material/schedule:"):
        sch = status["schedule"]
        a, b, c = st.columns(3)
        enabled = a.toggle("Enabled", sch["enabled"])
        interval = b.number_input("Interval (seconds)", 60, 86400, int(sch["interval_s"]), step=60)
        limit = c.number_input("Run limit", 1, 100, int(sch["run_limit"]))
        d, e = st.columns(2)
        faults = d.multiselect("Faults to cycle through", list(FAULT_LABEL), sch["faults"], format_func=FAULT_LABEL.get)
        auto = e.toggle("Run a recovery experiment after each injection", sch["auto_recover"])
        engines = [k for k, v in status["engines"].items() if v["available"]] or ["gemini"]
        eng = e.selectbox("Engine for scheduled runs", engines, format_func=ENGINE_LABEL.get,
                          index=engines.index(sch["engine"]) if sch["engine"] in engines else 0)
        st.caption(f"Runs done: {sch['runs_done']} / {sch['run_limit']}. The scheduler checks every few seconds "
                   "while the app is running.")
        if st.button("Save schedule", disabled=not faults):
            if safe_call(api, "PUT", "/api/livedb/schedule", json={"enabled": enabled, "interval_s": int(interval),
                                                                  "run_limit": int(limit), "auto_recover": auto,
                                                                  "engine": eng, "faults": faults}):
                st.rerun()
    log = safe_call(api, "GET", "/api/livedb/faults")
    if log and log["all"]:
        st.markdown("**Fault log**")
        st.dataframe(pd.DataFrame([{"time": f["ts"][:19].replace("T", " "), "trigger": f.get("trigger"),
                                    "fault": FAULT_LABEL.get(f["fault"], f["fault"]), "column": f.get("column"),
                                    "new": f.get("new_name") or f.get("new_type"), "ok": f["ok"],
                                    "error": f.get("error") or "", "DDL": "; ".join(f["ddl"])} for f in log["all"]]),
                     hide_index=True, use_container_width=True)


def _experiments_tab(status: dict):
    engines = status["engines"]
    st.markdown("Runs the original business operation, an INSERT of an order with an idempotency key. "
                "On failure it then diagnoses, proposes a repair with the selected engine, validates it, tests it in the "
                "sandbox schema, applies it as a new tool version, retries and verifies the stored row. If verification "
                "fails, it rolls back.")
    c1, c2 = st.columns([1.3, 2])
    eng = c1.radio("Repair engine", list(ENGINE_LABEL), format_func=ENGINE_LABEL.get)
    info = engines[eng]
    c2.markdown(f"{badge('CONFIGURED' if info['available'] else 'ACTION REQUIRED')} {info['detail']}",
                unsafe_allow_html=True)
    if not info["available"]:
        c2.caption("This engine cannot run, and ADAPTIX will not switch to another one. The run would stop and say why.")
    a1, a2, a3, a4 = st.columns(4)
    args = {"customer_id": a1.text_input("customer_id", "cust-0003"), "sku": a2.selectbox("sku", ["SKU-100", "SKU-200", "SKU-300"], 1),
            "quantity": int(a3.number_input("quantity", 1, 100, 2)), "note": a4.text_input("note", "deliver after 5pm") or None}
    st.caption(f"Expected outcome given the faults injected since the last reset: **{status['expected_now']}**")
    if st.button("Run experiment", type="primary", icon=":material/play_arrow:", disabled=not info["available"]):
        with st.spinner("Running against the live database…"):
            st.session_state["ldb_run"] = safe_call(api, "POST", "/api/livedb/experiment", json={"engine": eng, **args})
        st.rerun()
    r = st.session_state.get("ldb_run")
    if r:
        with st.container(border=True):
            st.markdown(f"{LIVE_BADGE} run `{r['run_id']}` · outcome {badge(r['outcome'] if r['outcome'] != 'succeeded_without_incident' else 'ok')}"
                        f" · engine `{r['engine']}` · total {fmt_ms(r.get('total_ms'))}", unsafe_allow_html=True)
            if r.get("reason"):
                st.write(r["reason"])
            ph = r.get("phases") or {}
            if ph:
                st.dataframe(pd.DataFrame([{"phase": PHASE_LABEL.get(k, k), "ms": v} for k, v in ph.items()]),
                             hide_index=True, use_container_width=True)
            if r.get("patch"):
                st.markdown("**Patch applied to the tool definition**")
                st.code("\n".join(f"- {p.get('from')}  ({p['argument']})\n+ {p.get('to')}  ({p['argument']}, "
                                  f"transform {p.get('transform')})" for p in r["patch"]), language="diff")
            if r.get("business"):
                b = r["business"]
                st.markdown(f"**Business verification:** {'✅' if b['ok'] else '❌'} {b['detail']}")
                st.json(b.get("checks", {}))
            if r.get("incident"):
                inc = safe_call(api, "GET", f"/api/livedb/incidents/{r['incident']}")
                if inc:
                    with st.expander("Timeline, validation and evidence"):
                        st.dataframe(pd.DataFrame([{"time": s["ts"][11:23], "stage": s["stage"], "status": s["status"],
                                                    "title": s["title"]} for s in inc["timeline"]]),
                                     hide_index=True, use_container_width=True)
                        if inc.get("validation"):
                            st.dataframe(pd.DataFrame(inc["validation"]["checks"]), hide_index=True,
                                         use_container_width=True)
                        if inc.get("llm"):
                            st.caption("Engine call: " + json.dumps({k: inc["llm"].get(k) for k in
                                                                     ("engine", "model", "real", "ok", "error_category",
                                                                      "duration_ms")}))
    _stats()


def _stats():
    sc = safe_call(api, "GET", "/api/livedb/scorecard")
    if not sc:
        return
    st.subheader("Real statistics (LIVE DB DEMO runs)")
    st.caption(sc["disclosure"])
    kpis([("Runs", sc["records"], f"{sc['real_llm_requests']} real Gemini request(s)"),
          ("Verified recoveries", f"{sc['verified_recoveries']}/{sc['eligible_incidents']}", "eligible runs"),
          ("Stopped correctly", f"{sc['stopped_correctly']}/{sc['should_stop']}", "runs that had to stop"),
          ("Unsafe repairs accepted", sc["unsafe_repairs_accepted"], f"rollbacks: {sc['rollbacks_attempted']}")])
    ev = safe_call(api, "GET", "/api/livedb/evidence", params={"limit": 200}) or []
    if ev:
        st.dataframe(pd.DataFrame([{"time": e["recorded_at"][:19].replace("T", " "), "engine": e.get("engine"),
                                    "trigger": e.get("trigger"), "classification": e["first_failure"]["classification"],
                                    "outcome": e["final"]["outcome"], "expected": e["expected"]["outcome"],
                                    "as expected": e["expected"]["matches"], "real LLM": e["llm"]["real_request_performed"],
                                    "rolled back": e["rollback"]["performed"], "total ms": e["timing"]["elapsed_ms"]}
                                   for e in ev]), hide_index=True, use_container_width=True)
        bundle = safe_call(api, "GET", "/api/livedb/evidence/export.json")
        csv_text = safe_call(api, "GET", "/api/livedb/evidence/export.csv")
        d1, d2 = st.columns(2)
        if bundle:
            d1.download_button("Export evidence (JSON)", json.dumps(bundle, indent=2, default=str),
                               "adaptix-livedb-evidence.json", "application/json")
        if isinstance(csv_text, str):
            d2.download_button("Export report (CSV)", csv_text, "adaptix-livedb-evidence.csv", "text/csv")


def _monitor_tab():
    st.markdown("Read-only: compares the live catalog with the baseline. It never injects or writes, and it is kept "
                "separate from the controlled experiments.")
    if st.button("Check now", icon=":material/refresh:"):
        st.session_state["ldb_mon"] = safe_call(api, "GET", "/api/livedb/monitor")
    m = st.session_state.get("ldb_mon")
    if m:
        (st.success if not m["drift"] else st.warning)(f"{m['summary']} · checked {m['checked_at'][:19]}Z")
        changes = m["report"]["request_changes"]
        if changes:
            st.dataframe(pd.DataFrame(changes), hide_index=True, use_container_width=True)


def render():
    header("Live Project Integration", "Online PostgreSQL (Neon) demo target",
           "A real database reached with a server-side DATABASE_URL. Experiments change only an isolated demo schema "
           "with synthetic data. They inject schema faults, then recover the original operation with a selected engine "
           "and verify the stored result.")
    st.markdown(LIVE_BADGE, unsafe_allow_html=True)
    status = safe_call(api, "GET", "/api/livedb/status")
    if status is None:
        return
    tabs = st.tabs(["Connect & setup", "Tables", "Schema comparison", "Fault injection", "Recovery experiments",
                    "Monitoring (read-only)"])
    with tabs[0]:
        _setup_tab(status)
    for tab, fn in zip(tabs[1:], (_tables_tab, _schema_tab, lambda: _faults_tab(status),
                                  lambda: _experiments_tab(status), _monitor_tab)):
        with tab:
            if not status["configured"]:
                st.info("Configure DATABASE_URL first (see Connect & setup).", icon=":material/info:")
            elif not status["set_up"]:
                st.info("Run **Set up demo schema** first (Connect & setup).", icon=":material/info:")
            else:
                fn()
