"""API Registry: register genuine HTTPS APIs, check their live schema, see drift, repairs and versions."""
import json

import streamlit as st

from views.common import api, badge, empty, fmt_ms, go, header, ist, mutated, REAL_BADGE, safe_call

PETSTORE = {"name": "petstore_find_by_status", "provider": "Swagger Petstore (public demo API, swagger.io)",
            "base_url": "https://petstore3.swagger.io/api/v3", "path": "/pet/findByStatus", "method": "GET",
            "schema_kind": "openapi", "schema_url": "", "probe": '{"status": "available"}',
            "description": "Finds pets by status (public demo API)"}
LIMITATION = ("This provider publishes no machine-readable schema. ADAPTIX checks reachability with the probe, "
              "but it cannot verify schema drift and never repairs this tool automatically.")


def _register_form(allowed: list[str]):
    pre = st.session_state.get("mon_prefill", {})
    with st.expander("Register a real API", icon=":material/add_link:", expanded=not st.session_state.get("_has_tools")):
        if st.button("Fill in the Swagger Petstore example", help="A public demo API. Fills the form only; "
                     "nothing is sent until you register. Its host must be in ADAPTIX_ALLOWED_HOSTS."):
            st.session_state["mon_prefill"] = PETSTORE
            st.rerun()
        with st.form("register_real"):
            c1, c2 = st.columns(2)
            name = c1.text_input("Tool name", pre.get("name", ""), placeholder="find_pets")
            provider = c2.text_input("Provider (who operates the API)", pre.get("provider", ""))
            c3, c4, c5 = st.columns([2.2, 1.6, 0.8])
            base = c3.text_input("Base URL (https)", pre.get("base_url", ""), placeholder="https://api.example.com/v1")
            path = c4.text_input("Path", pre.get("path", ""), placeholder="/items")
            methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
            method = c5.selectbox("Method", methods, index=methods.index(pre.get("method", "GET")))
            kinds = {"openapi": "OpenAPI document", "json_schema": "JSON Schema document",
                     "none": "Provider publishes no schema"}
            c6, c7 = st.columns([1, 2])
            kind = c6.selectbox("Schema source", list(kinds), format_func=kinds.get,
                                index=list(kinds).index(pre.get("schema_kind", "openapi")))
            schema_url = c7.text_input("Schema URL (blank = <base URL>/openapi.json)", pre.get("schema_url", ""))
            declared = st.text_input("Declared fields (only without a schema; not authoritative)", "",
                                     placeholder='{"query:limit": "int"}')
            c8, c9 = st.columns(2)
            auth_header = c8.text_input("Auth header (optional)", placeholder="x-api-key")
            group = c9.text_input("Credential group (optional, from ADAPTIX_CREDENTIALS)")
            c10, c11 = st.columns([2, 1])
            probe = c10.text_input("Safe read-only probe arguments (GET only, optional)", pre.get("probe", ""),
                                   placeholder='{"status": "available"}')
            interval = c11.number_input("Check every (minutes)", 5, 7 * 24 * 60, 60, step=5)
            desc = st.text_input("Description", pre.get("description", ""))
            st.caption("Only hosts in ADAPTIX_ALLOWED_HOSTS can be registered "
                       f"({', '.join(allowed) if allowed else 'none configured'}). Credentials are never entered "
                       "here: the group refers to server-side configuration.")
            if st.form_submit_button("Register and read the schema", type="primary"):
                try:
                    body = {"name": name, "provider": provider, "base_url": base, "path": path, "method": method,
                            "description": desc, "auth_header": auth_header or None, "credential_group": group or None,
                            "monitor_interval_s": int(interval) * 60,
                            "schema_source": {"kind": kind, "url": schema_url or None,
                                              "declared_fields": json.loads(declared) if declared else {}},
                            "probe_arguments": json.loads(probe) if probe else None}
                except json.JSONDecodeError:
                    st.error("Probe arguments and declared fields must be JSON objects.")
                    return
                res = safe_call(api, "POST", "/api/tools", json=body)
                if res:
                    mutated()
                    st.session_state.pop("mon_prefill", None)
                    st.success(f"Registered {res['name']} (schema {res['schema_signature']}, source {res['schema_source']})")
                    st.rerun()


def _changes(report: dict):
    rows = [{"change": c["kind"].replace("_", " "), "field": c["location"], "was": c.get("old") or "",
             "now": c.get("new") or "", "detail": c.get("detail", "")}
            for c in report.get("request_changes", []) + report.get("response_changes", [])]
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)


def _tool_card(t: dict):
    c = t["last_check"]
    with st.container(border=True):
        top = st.columns([3, 1.3])
        top[0].markdown(f"### `{t['tool']}` &nbsp; {REAL_BADGE}", unsafe_allow_html=True)
        top[0].markdown(f"**{t['provider'] or 'provider not named'}** · `{t['method']} {t['endpoint']}`")
        top[0].caption(f"schema source: {t['schema_source']}" + (f" · {t['schema_url']}" if t["schema_url"] else "")
                       + f" · tool v{t['current_version']} · checks every {t['interval_s'] // 60} min"
                       + ("" if t["monitor_enabled"] else " (scheduled checks off)"))
        if top[1].button("Check now", key=f"chk-{t['tool']}", type="primary", icon=":material/refresh:"):
            with st.spinner("Contacting the provider…"):
                res = safe_call(api, "POST", f"/api/monitor/{t['tool']}/check")
            if res:
                mutated()
                st.rerun()
        if c is None:
            st.info("Not checked yet. Use **Check now**.", icon=":material/schedule:")
        else:
            m = st.columns(4)
            m[0].markdown(f"**Connectivity**<br>{badge(c['connectivity'])}", unsafe_allow_html=True)
            schema_state = c["schema_status"] if c["schema_status"] != "verified" else ("drift" if c["drift"] else "no_drift")
            m[1].markdown(f"**Schema**<br>{badge(schema_state if c['schema_status'] == 'verified' else c['schema_status'])}",
                          unsafe_allow_html=True)
            m[2].markdown(f"**Task verification**<br>{badge(c['verification'])}", unsafe_allow_html=True)
            m[3].markdown(f"**Last checked**<br>{ist(c['ts'])} · {fmt_ms(c.get('duration_ms'))} "
                          f"· {c['trigger']}", unsafe_allow_html=True)
            if c["schema_status"] == "verified":
                if c["drift"]:
                    st.warning(f"Schema drift detected: {c['drift_summary']}", icon=":material/difference:")
                    _changes(c.get("report") or {})
                else:
                    st.success("No schema drift detected", icon=":material/check_circle:")
            elif c["schema_status"] == "unsupported":
                st.info(LIMITATION, icon=":material/info:")
            else:
                st.error(f"Schema could not be verified: {c.get('error_category')} · {c.get('detail', '')}",
                         icon=":material/error:")
            if c.get("verification_detail"):
                st.caption("Verification: " + c["verification_detail"])
            if c.get("repair"):
                st.success(f"Autonomous repair: {c['repair']['reason']}", icon=":material/healing:")
            if c.get("incident") and st.button("Open incident timeline", key=f"inc-{t['tool']}-{c['ts']}"):
                go("recovery", incident_id=c["incident"], env_choice="REAL API")
        with st.expander("Versions, rollback and delete"):
            from views.tools import versions_panel
            versions_panel("/api", t["tool"], t["current_version"])
        with st.expander("Check schedule and history"):
            s1, s2, s3 = st.columns([1, 1, 1])
            enabled = s1.toggle("Scheduled checks", t["monitor_enabled"], key=f"en-{t['tool']}")
            minutes = s2.number_input("Every (minutes)", 5, 7 * 24 * 60, t["interval_s"] // 60, step=5, key=f"iv-{t['tool']}")
            if s3.button("Save schedule", key=f"sv-{t['tool']}"):
                if safe_call(api, "PUT", f"/api/monitor/{t['tool']}", json={"enabled": enabled, "interval_s": int(minutes) * 60}):
                    st.rerun()
            hist = safe_call(api, "GET", f"/api/monitor/{t['tool']}/checks", params={"limit": 20}) or []
            if hist:
                st.dataframe([{"time": ist(h["ts"]), "trigger": h["trigger"], "connectivity": h["connectivity"],
                               "schema": h["schema_status"], "drift": h["drift"], "verification": h["verification"],
                               "error": h.get("error_category") or ""} for h in hist],
                             hide_index=True, use_container_width=True)


def render():
    header("API Registry", "Your real APIs and their live schemas",
           "Registered tools call real HTTPS providers through the SSRF guard. ADAPTIX reads each provider's "
           "authoritative schema on a schedule or on demand, compares it with the saved version and, when drift "
           "is confirmed on a real call, repairs, verifies and rolls back autonomously. No simulated data appears here.")
    ov = safe_call(api, "GET", "/api/monitor")
    if ov is None:
        return
    allowed = ov["allowed_hosts"]
    sch = ov["scheduler"]
    c1, c2 = st.columns([2, 1])
    if allowed:
        c1.markdown(f"**Approved hosts** {badge('CONFIGURED')} &nbsp; `{'`, `'.join(allowed)}`", unsafe_allow_html=True)
    else:
        c1.markdown(f"**Approved hosts** {badge('ACTION REQUIRED')}", unsafe_allow_html=True)
        c1.caption("No real egress is possible until ADAPTIX_ALLOWED_HOSTS lists the hosts you are authorized to "
                   "call (Streamlit Cloud: app Settings → Secrets).")
    c2.markdown(f"**Scheduler** {badge('YES' if sch['scheduler_running'] else 'NO')}", unsafe_allow_html=True)
    c2.caption(f"default interval {sch['default_interval_s'] // 60} min · minimum {sch['min_interval_s'] // 60} min")
    st.session_state["_has_tools"] = bool(ov["tools"])
    _register_form(allowed)
    if not ov["tools"]:
        empty("No real API is registered yet. Register one above (try the Swagger Petstore example).")
        return
    for t in ov["tools"]:
        _tool_card(t)
