import streamlit as st

from views.common import REPO_URL, footer, page_header


def render():
    page_header("Overview · API & integration", "Use it from any agent",
                "SchemaHeal also runs as a standalone middleware service. Agents call their tools through it over "
                "HTTP; the first integration target is OpenAI-compatible function calling.")
    st.subheader("Run the service")
    st.code('SCHEMAHEAL_API_KEYS="acme:agent:AGENT_KEY,acme:reviewer:REVIEW_KEY" \\\n'
            '  uvicorn adaptix.service:app --port 8000      # docs at http://localhost:8000/docs', language="bash")
    st.subheader("Endpoints")
    st.dataframe({
        "Method": ["POST", "GET", "POST", "GET", "POST", "GET", "POST", "POST", "POST", "PUT", "GET"],
        "Path": ["/v1/tools", "/v1/openai/tools", "/v1/openai/tool_calls", "/v1/tools/{name}/versions",
                 "/v1/tools/{name}/call", "/v1/repairs", "/v1/repairs/{id}/approve", "/v1/repairs/{id}/reject",
                 "/v1/tools/{name}/rollback", "/v1/policies/{tool}", "/v1/events"],
        "Role": ["agent", "agent", "agent", "agent", "agent", "reviewer", "reviewer", "reviewer", "reviewer",
                 "reviewer", "reviewer"],
        "What it does": ["register a tool (upstream URL, path, method, argument schema)",
                         "tool definitions in OpenAI function-calling format",
                         "execute a model's tool_calls, returns role='tool' messages",
                         "version history with status (active / canary / rolled_back)",
                         "call a tool; safe drift is repaired, ambiguous drift is held",
                         "repairs waiting for a human", "approve a held repair (stale ones are refused)",
                         "reject a held repair", "roll back the latest version",
                         "per-tool policy, e.g. auto_repair=false for payments", "tenant audit events"]},
        hide_index=True, use_container_width=True)
    st.subheader("OpenAI-compatible agent loop")
    st.code('''from adaptix.client import SchemaHealClient
sh = SchemaHealClient("http://localhost:8000", api_key=AGENT_KEY)
sh.register_tool("update_user", base_url="https://api.example.com", path="/users/update",
                 arg_schema={"user_id": "str", "email": "str"})

resp = llm.chat.completions.create(model=MODEL, messages=messages, tools=sh.openai_tools())
msg = resp.choices[0].message
if msg.tool_calls:
    messages += sh.handle_tool_calls(msg.tool_calls)   # executed and, if safe, repaired''', language="python")
    st.markdown("**Guarantees per call:** tenant-scoped API keys and roles · one idempotency key per call · "
                "timeouts, rate limit and circuit breaker per tool · only 400/422 are repaired and replayed · "
                "traces returned to the caller are redacted.")
    st.caption(f"Full example: `examples/openai_agent_loop.py` in the [repository]({REPO_URL}).")
    footer()
