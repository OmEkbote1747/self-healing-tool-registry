# SchemaHeal: a guarded OpenAPI request adapter for AI agents (prototype)

PS-01 · Genesis 2.0, Track EVA 02 · Team Runtime Terrors

When an API changes its request schema, an agent's tool calls start failing with `422`. SchemaHeal sits
between the agent and its tools. It reads the error and the new `/openapi.json`, and then does one of three things:

| Outcome | When |
|---|---|
| **Repairs automatically** | the change is tightly defined and reversible (see the boundary below) |
| **Holds for a human** | the repair passes validation but could be wrong in meaning (medium-confidence match, a different entity name, a unit word, an LLM suggestion, or a tool whose policy says so) |
| **Stops and reports** | a required value does not exist, a value is not allowed, the method or body format changed, auth changed, or the API failed in a way that is not a schema problem |

It never invents a value: every value it sends comes from the agent's own arguments (after a lossless
conversion) or from approved configuration.

> This is a prototype. It is not general "self-healing": a schema-valid request can still be wrong in
> meaning, so anything ambiguous goes to a person. Results below come from cases written by our team
> against our own mock API.

## Automatic repair boundary

Applied without a human only when **all** hold:

- a renamed or moved field matches an orphaned argument with similarity ≥ 75, and no other candidate is close;
- the names do not refer to different things (`channel_id → thread_id` is held) and no unit word changed
  (`amount → amount_cents`, `timeout_ms → timeout_seconds` are held);
- type changes are lossless (`"3" → 3` yes, `"3.5" → int` stop, `"yes" → bool` stop);
- enum values are still allowed and dates are ISO;
- new headers come from approved configuration;
- the tool's policy allows automatic repair (e.g. payments can be set to always need approval);
- the repaired request validates against the new schema before it is sent.

Automatic repairs then start as a **canary** used only by the agent that made them. They are shared after
3 successful calls, and rolled back if they fail.

## Architecture

```
agent ──► SchemaHeal (Streamlit console or middleware API)
            ├─ transport: timeouts, pooling, rate limit, circuit breaker, retry budget, idempotency key
            ├─ schema reader: /openapi.json → body/query/path fields, required, enum, format, arrays, unions
            ├─ Tier 1 rules → (Tier 2 LLM, names and types only) → policy gate → validator
            ├─ durable registry (SQLite): tenant-scoped versions, proposals, policies, events
            └─ redacted traces and audit log
                    │
                    ▼
                 the API
```

| File | What it does |
|---|---|
| `adaptix/healer.py` | orchestration: diagnose, map, convert, validate, gate, patch, retry once |
| `adaptix/policy.py` | repair policy: confidence thresholds, entity and unit checks, per-tool overrides |
| `adaptix/schema.py` | OpenAPI operation reader (methods, params, optional/enum/array/union/format fields) |
| `adaptix/transport.py` | timeouts, pooling, rate limit, circuit breaker, retry budget, idempotency, replay rules |
| `adaptix/store.py` | durable tenant-scoped registry: versions, proposals, policies, events (SQLite) |
| `adaptix/security.py` | redaction, secrets from the environment, audit retention |
| `adaptix/llm.py` | optional Tier 2: an LLM picks which existing argument fits (never values; always reviewed) |
| `adaptix/service.py` | middleware API with tenant keys and roles, review queue, OpenAI tool-calling adapter |
| `adaptix/client.py` | Python SDK for the service |
| `adaptix/baselines.py` | retry-loop and greedy error-reading baselines |
| `adaptix/mock_api.py` | mock API with schema drift, idempotency, fault injection |
| `adaptix/cases.py`, `fixtures/` | 125 planned cases, 10 stress cases, 30 adversarial JSON fixtures |
| `app.py`, `views/` | Streamlit console (landing page, demo, benchmark, review queue, API docs) |

## Run it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/streamlit run app.py                     # console
.venv/bin/python -m pytest -q                      # 76 tests
.venv/bin/python -m adaptix.bench                 # all suites, three agents
SCHEMAHEAL_API_KEYS="acme:agent:AGENT_KEY,acme:reviewer:REVIEW_KEY" \
  .venv/bin/uvicorn adaptix.service:app --port 8000   # middleware API, docs at /docs
```

Secrets: header values come from `SCHEMAHEAL_SECRET_<NAME>` environment variables (e.g.
`SCHEMAHEAL_SECRET_X_API_KEY`) or Streamlit secrets, never from code. Optional LLM tier: set `ANTHROPIC_API_KEY`.

## Middleware API (first integration target: OpenAI-compatible function calling)

```python
from adaptix.client import SchemaHealClient
sh = SchemaHealClient("http://localhost:8000", api_key=AGENT_KEY)
sh.register_tool("update_user", base_url="https://api.example.com", path="/users/update",
                 arg_schema={"user_id": "str", "email": "str"})
resp = llm.chat.completions.create(model=MODEL, messages=messages, tools=sh.openai_tools())
messages += sh.handle_tool_calls(resp.choices[0].message.tool_calls)
```

Endpoints: `POST /v1/tools`, `GET /v1/openai/tools`, `POST /v1/openai/tool_calls`, `POST /v1/tools/{name}/call`,
`GET /v1/tools/{name}/versions`, `GET /v1/repairs`, `POST /v1/repairs/{id}/approve|reject`,
`POST /v1/tools/{name}/rollback`, `PUT /v1/policies/{tool}`, `GET /v1/events`. Keys map to a tenant and a role
(agent or reviewer); every query is tenant-scoped.

## Results

Same cases for three agents. "False repairs" are wrong requests that were actually sent.

| Suite | Agent | Zero-touch fixed | Held for approval | Safe stops | False repairs | Invented values |
|---|---|---|---|---|---|---|
| 125 planned | Retry loop | 0 / 85 | 0 | 40 / 40 | 0 | 0 |
| | Greedy error-reader | 60 / 85 | 0 | 0 / 40 | 40 | 30 |
| | **SchemaHeal** | **79 / 85 (93%)** | 6 | **40 / 40** | **0** | **0** |
| 10 stress | Greedy error-reader | 4 / 5 | 0 | 1 / 5 | 4 | 6 |
| | **SchemaHeal** | 2 / 5 | 3 | **5 / 5** | **0** | **0** |
| 30 adversarial | Greedy error-reader | 12 / 13 | 0 | 6 / 17 | 11 | 5 |
| | **SchemaHeal** | 10 / 13 (77%) | 7 | **17 / 17** | **0** | **0** |

Median time from a failed call to recovery or a safe stop is a few milliseconds against the in-process mock.
The greedy baseline is a heuristic stand-in for an error-reading LLM agent, not a real model.

## Production readiness

| Gap | Status | What exists | Still open |
|---|---|---|---|
| Middleware / agent integration | **Partial** | FastAPI service, SDK, OpenAI-compatible adapter, tenant keys and roles | MCP server, agent-framework adapters, real upstream trial |
| Unsafe automatic repair | **Addressed in prototype** | confidence thresholds, entity and unit checks, per-tool policies, review queue, canary rollout, auto-rollback | semantic compatibility rules per domain; approval UX for teams |
| Schema coverage | **Partial** | methods, query/path params, optional, enums, arrays, unions, date format, content-type check | auth scheme changes, oneOf discriminators, non-JSON payloads, response-schema drift |
| Durable registry | **Partial** | SQLite with tenants, atomic versioning, conflict checks, rollback history, events | Postgres for multi-host, migrations tooling, cache invalidation across hosts |
| Security and privacy | **Partial** | redaction, masked error echoes, env/platform secrets, value-free LLM prompts, retention | encrypted secret store/KMS, SSO/RBAC, per-tenant secrets, incident response |
| Request handling | **Addressed in prototype** | timeouts, pooling, rate limit, circuit breaker, retry budget, idempotency keys, no replay after timeouts/5xx | distributed rate limits and breakers, backoff policies per upstream |
| Evaluation | **Partial** | stronger baseline, adversarial fixtures, separate false-repair and safe-stop rates | independently sourced real API changes, staged deployment with auto-repair off by default |

## Honest limits

- Every suite was written by our team and runs against our own mock API.
- Name similarity cannot prove two fields mean the same thing; that is why medium-confidence and
  different-entity matches go to a human.
- The LLM tier is implemented but was not used in these results (no API key was set).
- SQLite suits one host; multi-instance deployments need Postgres behind the same interface.
