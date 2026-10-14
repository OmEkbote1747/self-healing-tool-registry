import streamlit as st
from fastapi.testclient import TestClient

from selfheal.cases import APPROVED_CONFIG, DEMO_CASE, build_cases, build_stress_cases
from selfheal.healer import NaiveRetryAgent, SelfHealer, ToolRegistry
from selfheal.mock_api import make_app
from views.common import LOG, footer, page_header, show_trace, use_llm


def render():
    page_header("Console · Live demo", "Break the API, watch it heal",
                "Pick a schema change. We change the mock API live, then run a normal retry-loop agent and our "
                "self-healing agent on exactly the same task.")

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
        registry = ToolRegistry()
        registry.register(case.tool, case.path, case.v1)
        base = NaiveRetryAgent().call(client, registry.current(case.tool), case.args)
        healer = SelfHealer(registry, APPROVED_CONFIG, log_path=LOG, use_llm=use_llm())
        ours = healer.call(client, case.tool, case.args, case.id)
        reuse = healer.call(client, case.tool, case.args, case.id + "-agentB") if ours.success else None
        st.session_state.demo = (label, base, ours, reuse, registry)

    demo = st.session_state.get("demo")
    if demo and demo[0] == label:
        _, base, ours, reuse, registry = demo
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
            m1.metric("Result", "Repaired" if ours.success else "Safe stop", border=True)
            m2.metric("API calls", ours.calls, border=True)
            m3, m4 = st.columns(2)
            m3.metric("Recovery time", f"{ours.recovery_ms:.1f} ms" if ours.recovery_ms else "-", border=True)
            m4.metric("LLM tokens", ours.tokens, border=True)
            if ours.stopped:
                st.warning("Stopped and reported: " + ours.stop_reason, icon=":material/front_hand:")
            show_trace(ours.trace)
            if reuse is not None:
                st.success(f"Shared registry: a second agent calling `{case.tool}` used tool "
                           f"v{registry.current(case.tool).version} and succeeded in {reuse.calls} call, "
                           f"with no new failure.", icon=":material/share:")
    footer()
