# ADAPTIX: autonomous API resilience

**APIs change. ADAPTIX adapts.**

PS-01 · Genesis 2.0 · Team Runtime Terrors (Paras Patil, Nitish Shidne, Sanchit Tawde, Om Ekbote)

When an API that an AI agent depends on changes its schema, ADAPTIX:

1. detects the failure;
2. decides whether it really is schema drift;
3. reads the authoritative schema;
4. plans a repair and validates it independently;
5. applies the repair as a new tool version;
6. retries the original task and verifies it.

Supported repairs run **autonomously, with no human approval step**. A repair that is ambiguous, unsafe or unverifiable stops with a plain-language reason. A repair that fails verification is rolled back.

> Honest scope: all evaluation numbers below come from **simulated upstreams** (in-process FastAPI apps, `sim://…`) and from cases our team wrote. They are not measurements of production incidents.

## Pipeline

```
agent ──► /api/repair/execute  or  /v1/openai/tool_calls
            A  execute the call (timeouts, idempotency key, credential from the registry)
            B  classify the failure  ── auth / rate limit / 5xx / business rule / invalid input are NOT drift
            C  discover the authoritative schema (OpenAPI or JSON Schema; fail closed if unavailable)
            D  diff registered vs live (renames, moves, type/required/enum/format, headers, response fields)
            E  plan: deterministic rules first; Gemini only for what the rules cannot ground
            F  validate independently (evidence grounding, lossless transforms, schema, credentials, provenance)
            G  apply atomically as a new version (optimistic concurrency + per-tool lock)
            H  rebuild the request from the caller's original arguments
            I  retry, bounded (backoff + jitter, honours Retry-After, total time budget)
            J  verify the task actually succeeded (status + applied values); repair response drift if needed
            K  roll back if verification fails
```

What is never treated as schema drift: 401/403 (credential problems; failover only to another *authorised* credential), 404, 429, 5xx, business-rule errors, and invalid input against an *unchanged* schema. A 400 or 422 counts as drift only when the live schema actually changed in a way that touches the failing request.

## Repository layout

| Path | Purpose |
|---|---|
| `adaptix/engine.py` | the pipeline A–K, the incident timeline, reuse of known mappings, loop prevention |
| `adaptix/classify.py` | failure classification with a plain-language explanation per category |
| `adaptix/schema.py`, `adaptix/diff.py` | authoritative schema parsing (OpenAPI / JSON Schema) and the drift report |
| `adaptix/planner.py`, `adaptix/validator.py` | rule planner and the independent validator (the LLM never bypasses it) |
| `adaptix/llm/gemini.py` | Gemini via the official `google-genai` SDK, structured output validated with Pydantic |
| `adaptix/llm/mock.py` | deterministic **mock**, used only in tests and always labelled "mock (not a real model)" |
| `adaptix/credentials.py` | credential registry: aliases, groups, scopes, states, authorised failover |
| `adaptix/security.py` | redaction and secret scrubbing for logs, audit payloads, errors and prompts |
| `adaptix/storage.py` | SQLite (WAL): tools, versions, schema snapshots, incidents, events, executions, mappings, locks |
| `adaptix/verify.py`, `adaptix/upstreams.py` | task verification; pooled HTTP client and in-process simulated upstreams |
| `adaptix/api.py` | FastAPI backend (`/api/*`, OpenAI-compatible adapter), admin auth, CORS, size limits |
| `adaptix/simulation.py`, `adaptix/mock_api.py` | Chaos Lab: 19 drift/failure scenarios against simulated APIs |
| `adaptix/evaluation.py`, `adaptix/cases.py`, `fixtures/` | baseline vs ADAPTIX: 19 scenarios, 125 planned cases, 30 adversarial fixtures |
| `adaptix/metrics.py` | dashboard metrics computed only from stored records |
| `app.py`, `views/` | Streamlit dashboard: Overview, Tool Registry, Schema Drift Explorer, Recovery Center, Audit & Evaluation, Chaos Lab, System Health |
| `tests/` | pytest suite (see below) |

## Run it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                                   # fill in on the server only; never commit .env
.venv/bin/streamlit run app.py                         # dashboard (runs the API in-process)
.venv/bin/uvicorn adaptix.api:app --port 8000          # standalone API, OpenAPI docs at /docs
.venv/bin/python -m pytest -q                          # tests
.venv/bin/python -m adaptix.evaluation --suite planned # baseline vs ADAPTIX (scenarios | planned | adversarial)
```

To point the dashboard at a separately deployed API, set `ADAPTIX_API_URL` and `ADAPTIX_ADMIN_TOKEN`.

### API (all `/api/*` routes need `Authorization: Bearer $ADAPTIX_ADMIN_TOKEN`)

| Method and path | Purpose |
|---|---|
| `GET /api/health/live` | liveness check (no auth) |
| `GET /api/health` | DB, LLM status, credential aliases, limits |
| `POST /api/health/llm-check` | sends one minimal **real** request to the configured provider |
| `GET/POST /api/tools`, `GET/DELETE /api/tools/{name}` | registry (registration reads the authoritative schema) |
| `GET /api/tools/{name}/versions`, `/drift` | version history; registered vs live schema |
| `POST /api/tools/{name}/rollback` | restore the parent version |
| `POST /api/repair/execute` | run a tool call through the pipeline |
| `POST /api/repair/analyze` | diagnose and plan **without sending anything** |
| `GET /api/incidents[/{id}]`, `GET /api/audit`, `GET /api/metrics` | records, audit log, metrics |
| `GET/POST /api/simulations`, `POST /api/simulations/reset` | Chaos Lab (simulated data only) |
| `POST /api/evaluation`, `GET /api/evaluation/latest` | baseline vs ADAPTIX evaluation |
| `GET /v1/openai/tools`, `POST /v1/openai/tool_calls` | OpenAI-compatible tool calling (see `examples/openai_agent_loop.py`) |

## Configuration

See [.env.example](.env.example) for every variable. The main ones:

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | unset | enables the Gemini tier. Without it, gaps the rules cannot ground stop safely |
| `GEMINI_API_KEY_SECONDARY` | unset | optional second authorised key, used for failover only |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | model name |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MIN_CONFIDENCE` | 20, 2, 0.8 | LLM request limits; plans below the confidence floor are rejected |
| `ADAPTIX_CREDENTIALS` | unset | JSON credential registry. Values are read from the env vars it names, never stored |
| `ADAPTIX_MAX_REPAIR_ATTEMPTS`, `ADAPTIX_MAX_RETRIES`, `ADAPTIX_TIME_BUDGET_SECONDS` | 2, 2, 30 | repair and retry bounds |
| `ADAPTIX_ADMIN_TOKEN` | random per process | bearer token for `/api/*` |
| `ADAPTIX_CORS_ORIGINS` | empty (CORS off) | comma-separated allowed origins |
| `ADAPTIX_MAX_BODY_BYTES` | 256000 | larger requests are rejected with 413 |
| `ADAPTIX_DB_PATH`, `ADAPTIX_RETENTION_DAYS` | `data/adaptix.db`, 30 | storage and retention |

On Streamlit Cloud, put these under the app's **Secrets**. `config.env()` reads environment variables first, then `st.secrets`.

### Gemini

- Uses `google-genai==2.31.0`: `client.models.generate_content(model=…, contents=…, config=GenerateContentConfig(system_instruction=…, response_mime_type="application/json", response_json_schema=LLMRepairPlan schema, temperature=0))`.
- The SDK's own retries are disabled. ADAPTIX applies its own bounded retries, timeouts and failover.
- The model receives field names, types, documentation and the arguments' **names**, never their values. Schema text is marked as untrusted data.
- Every mapping must quote evidence that appears verbatim in the schema. The independent validator re-checks every plan; the LLM cannot approve its own repair.
- The connectivity status stays "configured, not yet verified" until a real request succeeds (`POST /api/health/llm-check`, or a plan call).
- `tests/test_gemini_live.py` makes a real call and runs only when `GEMINI_API_KEY` is set. All other Gemini tests use a **fake SDK client** and are not live-provider tests.

### Credential failover: what it does and does not do

- Failover moves to another configured credential **in the same group**, with a scope that permits the call, and only for the categories listed in that credential's `failover_on` setting (by default: `rate_limited`, `credential_expired`).
- An invalid or revoked credential is marked as such and is never modified, retried with variations, or "repaired". An authentication error is never treated as schema drift.
- It cannot create, refresh or rotate credentials; an operator must supply new ones. Credential state is held in process memory and resets on restart.

## Results (simulated upstreams, measured on 2026-10-16)

Without an LLM key:

"Baseline" makes the same call with repair disabled, so every drifted call fails.

| Suite | Cases | Recovered (repairable) | Safe stop (unrepairable) | Incorrect patches accepted | Median recovery |
|---|---|---|---|---|---|
| Chaos Lab scenarios | 19 | 8 / 8 (100%) | 10 / 10 (100%) | 0 | 6.7 ms |
| Planned schema changes | 125 | 79 / 85 (92.9%) | 40 / 40 (100%) | 0 | 6.7 ms |
| Adversarial fixtures | 30 | 10 / 13 (76.9%) | 17 / 17 (100%) | 0 | 6.7 ms |

With a **real Gemini key** (`gemini-3.5-flash-lite`, run on 2026-10-16):

- **Chaos Lab scenarios:** 9 / 9 recovered, including the LLM-only booking-window rename, and 10 / 10 stopped safely. 0 incorrect patches; 4 Gemini calls, all successful.
- **Adversarial fixtures:** unchanged at 10 / 13 recovered and 17 / 17 safe stops, with 0 incorrect patches. Gemini proposed mappings for the 3 misses, but the independent validator rejected them: the schema contains no text that grounds the rename, so ADAPTIX stops rather than trusting the model.

The repairable cases ADAPTIX did not recover are **safe misses**: it stopped instead of guessing, and no wrong data was sent. Examples: `uid`, `user_identifier`, `room_ref` and `phone → mobile` with no documentation linking the names, and the scenario that needs an LLM. Recovery times are against in-process simulations. They exclude network latency and LLM latency.

## Tests

`python -m pytest -q` → 127 passed. The 2 live Gemini tests in `tests/test_gemini_live.py` are skipped without `GEMINI_API_KEY`; with a real key they passed on 2026-10-16. The suite covers:

- every Chaos Lab scenario, plus baseline mode;
- classification, including that auth errors are never repaired;
- that a plain success creates no incident;
- side effects and idempotency, Retry-After handling and the time budget;
- that a 503 is not replayed for non-idempotent calls;
- loop prevention, concurrency, rollback, and response-drift repair without resending;
- the validator, transforms, credential failover, and the Gemini adapter (fake client and real SDK error types);
- the API: auth, 413, CORS, analyze-without-sending, the OpenAI adapter, and no secret leakage;
- smoke tests for every dashboard page.

## Limitations

- The evaluation uses simulated APIs and team-written cases. Independently sourced, real API changes are still needed.
- Real Gemini calls were verified on 2026-10-16. Google can return `503 high demand` for a model; ADAPTIX retries a bounded number of times and otherwise stops safely.
- Name similarity and documentation cannot prove two fields mean the same thing. When the evidence is not explicit, ADAPTIX stops.
- SQLite suits one host. Multiple instances need a shared database and a distributed lock. Credential state is in memory.
- On Streamlit Cloud the SQLite file is ephemeral, so records reset when the app restarts.
- Not yet covered: auth-scheme migrations, `oneOf` discriminators, non-JSON payloads and pagination changes.
