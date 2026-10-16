import streamlit as st
from fastapi.testclient import TestClient

from adaptix.cases import APPROVED_CONFIG, DEMO_CASE, build_cases, build_stress_cases
from adaptix.healer import ConflictError, NaiveRetryAgent, SelfHealer
from adaptix.mock_api import make_app
from views.common import LOG, footer, page_header, session_registry, show_trace, use_llm


def _healer(registry):
    return SelfHealer(registry, APPROVED_CONFIG, log_path=LOG, use_llm=use_llm())


def _share(healer, registry, client, case):
    """Show how a repair spreads: a canary is used only by its creator until it has succeeded enough times."""
    td = registry.current(case.tool, "agent-A")
    extra = 0
    while td.status == "canary" and extra < registry.promote_after:
        healer.call(client, case.tool, case.args, case.id + "-agentA", agent_id="agent-A")
        extra += 1
    b = healer.call(client, case.tool, case.args, case.id + "-agentB", agent_id="agent-B")
    return extra, b


def render():
    page_header("Console · Live demo", "Break the API, watch it heal",
                "Pick a schema change. We change the mock API live, then run a normal retry-loop agent and our "
                "agent on exactly the same task. Safe changes are fixed automatically; ambiguous ones wait for you.")

    options = {"Worked example: user_id → target.id (int) + X-Api-Key header": DEMO_CASE}
    for c in build_cases():
        options[f"{c.id} · {c.category} · {c.tool} · {c.note}"] = c
    for c in build_stress_cases():
        options[f"{c.id} · Stress · {c.category} · {c.note}"] = c

    with st.container(border=True):
        label = st.selectbox("Schema change", list(options))
        case = options[label]
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Agent's arguments**")
            st.json(case.args)
        with c2:
            st.markdown("**API before (v1)**")
            st.json(case.v1)
        with c3:
            st.markdown("**API after (v2)**")
            st.json({"body": case.v2, "required headers": case.v2_headers})
        expected = "repair the call" if case.expected == "repair" else "stop and report (nothing can be fixed safely)"
        st.caption(f"Correct outcome: **{expected}**")
        run = st.button("Change the API and run both agents", type="primary", icon=":material/play_arrow:")

    if run:
        client = TestClient(make_app(case.path, case.v2, case.v2_headers))
        registry = session_registry()        # durable, tenant-scoped (SQLite)
        registry.register(case.tool, case.path, case.v1)
        base = NaiveRetryAgent().call(client, registry.current(case.tool), case.args)
        healer = _healer(registry)
        ours = healer.call(client, case.tool, case.args, case.id)
        share = _share(healer, registry, client, case) if ours.success else None
        st.session_state.demo = {"label": label, "base": base, "ours": ours, "share": share, "registry": registry,
                                 "client": client, "case": case, "after": None}

    demo = st.session_state.get("demo")
    if not demo or demo["label"] != label:
        footer()
        return
    base, ours, registry = demo["base"], demo["ours"], demo["registry"]
    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Normal agent (retry loop)")
        m1, m2 = st.columns(2)
        m1.metric("Result", "Failed" if not base.success else "Done", border=True)
        m2.metric("API calls", base.calls, border=True)
        show_trace(base.trace)
    with right:
        st.subheader("SchemaHeal agent")
        m1, m2 = st.columns(2)
        m1.metric("Result", {"repaired": "Repaired", "awaiting_approval": "Needs approval",
                             "stopped": "Safe stop"}[ours.state], border=True)
        m2.metric("API calls", ours.calls, border=True)
        m3, m4 = st.columns(2)
        m3.metric("Recovery time", f"{ours.recovery_ms:.1f} ms" if ours.recovery_ms else "-", border=True)
        m4.metric("LLM tokens", ours.tokens, border=True)

        if ours.awaiting is not None and demo["after"] is None:
            with st.container(border=True):
                st.markdown("#### :material/approval: Held for your approval")
                st.write("The repair passed the schema check, but it is not certain enough to send on its own:")
                for r in ours.awaiting.reasons:
                    st.markdown(f"- {r}")
                st.json(ours.awaiting.candidate.describe())
                a, b = st.columns(2)
                if a.button("Approve and retry", type="primary", icon=":material/check:"):
                    try:
                        registry.approve(ours.awaiting.id, by="demo reviewer")
                        demo["after"] = ("approved", _healer(registry).call(demo["client"], demo["case"].tool,
                                                                            demo["case"].args, demo["case"].id))
                    except ConflictError as e:
                        demo["after"] = ("conflict", str(e))
                    st.rerun()
                if b.button("Reject", icon=":material/close:"):
                    registry.reject(ours.awaiting.id, by="demo reviewer")
                    demo["after"] = ("rejected", None)
                    st.rerun()
        elif ours.stopped:
            st.warning("Stopped and reported: " + ours.stop_reason, icon=":material/front_hand:")

        after = demo["after"]
        if after and after[0] == "approved":
            res = after[1]
            st.success(f"Approved. Retried with tool v{registry.current(demo['case'].tool).version}: "
                       f"{'success' if res.success else 'still failing'} in {res.calls} call(s).",
                       icon=":material/task_alt:")
        elif after and after[0] == "rejected":
            st.info("Rejected. Nothing was sent and the tool was not changed.", icon=":material/block:")
        elif after and after[0] == "conflict":
            st.error(after[1])

        show_trace(ours.trace)
        if demo["share"] is not None:
            extra, b = demo["share"]
            st.success(f"Canary → shared: the repair was used only by agent A until it had succeeded "
                       f"{registry.promote_after} times ({extra} extra call(s) here), then promoted. Agent B then "
                       f"used tool v{registry.current(demo['case'].tool).version} and succeeded in {b.calls} call.",
                       icon=":material/share:")
    footer()
