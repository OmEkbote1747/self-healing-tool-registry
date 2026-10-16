import streamlit as st

from views.common import api, api_mode, badge, header, safe_call

CONN = {"verified": ("Verified (a real request succeeded)", "#047857"),
        "configured_unverified": ("Configured, not yet verified", "#B45309"),
        "failing": ("Configured, last request failed", "#B91C1C"),
        "not_configured": ("Not configured", "#475569"), "mock": ("Mock (no real model)", "#6D28D9")}


def render():
    header("System Health", "Service, LLM provider and credentials",
           "Credential values never leave the server. This page shows aliases, groups and states only.")
    h = safe_call(api, "GET", "/api/health")
    if h is None:
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Service", h["status"])
    c2.metric("Database", "ok" if h["database"] == "ok" else "error")
    c3.metric("Version", h["version"])
    c4.metric("API", api_mode())
    llm = h["llm"]
    st.subheader("LLM provider")
    label, colour = CONN.get(llm["connectivity"], (llm["connectivity"], "#475569"))
    st.markdown(f"Provider `{llm['provider']}` · model `{llm['model']}` · "
                f"<b style='color:{colour}'>{label}</b>", unsafe_allow_html=True)
    if llm.get("note"):
        st.caption(llm["note"])
    if llm.get("last_success_at"):
        st.caption(f"Last successful request: {llm['last_success_at'][:19]}Z")
    if llm.get("last_error_category"):
        st.caption(f"Last error category: {llm['last_error_category']}")
    if llm["configured"] and llm["provider"] not in ("none",) and llm["connectivity"] != "mock":
        if st.button("Run connectivity check", icon=":material/network_check:",
                     help="Sends one minimal real request to the provider"):
            with st.spinner("Calling the provider…"):
                r = safe_call(api, "POST", "/api/health/llm-check")
            if r:
                res = r["result"]
                (st.success if res["ok"] else st.error)(
                    f"{'Succeeded' if res['ok'] else 'Failed'}: {res.get('error_category') or 'ok'} · "
                    f"{res.get('duration_ms')} ms · credential {res.get('credential_alias') or '—'}")
    else:
        st.info("No LLM provider is configured, so no connectivity check is offered. Set GEMINI_API_KEY on the "
                "server to enable the Gemini tier. Without it, repairs the rules cannot ground stop safely.",
                icon=":material/info:")
    st.subheader("Credential registry")
    creds = h["credentials"]
    if creds:
        st.dataframe([{"alias": c["alias"], "group": c["group"], "provider": c["provider"], "header": c["header"],
                       "scope": c["scope"], "configured": c["configured"], "enabled": c["enabled"], "state": c["state"],
                       "simulated": c["simulated"], "last failure": c["last_failure_category"],
                       "fails over on": ", ".join(c["failover_on"])} for c in creds],
                     hide_index=True, use_container_width=True)
    else:
        st.caption("No credentials configured.")
    st.caption("Failover moves to another authorised credential in the same group only for the categories listed. "
               "A rejected key is never modified, and an authentication error is never treated as schema drift.")
    st.subheader("Limits")
    st.json(h["limits"])
    st.markdown(badge("healthy" if h["status"] == "ok" else "degraded"), unsafe_allow_html=True)
