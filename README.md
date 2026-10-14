# Self-Healing Tool Registry (PS-01 prototype)

When an API changes its request schema, a normal agent gets a `422` and retries the same request until it gives up.
This prototype sits between the agent and its tools. It works out what changed, checks that the fix is safe,
patches the tool definition in memory and finishes the task, **without making up data**.

```
agent -> tool registry -> API
              | 422
              v
 error parser + /openapi.json -> Tier 1 rules -> (Tier 2 LLM if unsure) -> validator -> patch + log -> retry once
                                                                              |
                                                                       reject -> stop and report
```

## Run it locally

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Benchmark from the command line:

```bash
.venv/bin/python -m selfheal.bench
```

Optional Tier 2 (LLM proposals): set `ANTHROPIC_API_KEY` before starting. Without it, anything the rules
are unsure about stops safely instead of being guessed.

## What's inside

| File | What it does |
|---|---|
| `selfheal/mock_api.py` | FastAPI + Pydantic mock API whose schema can be changed; returns real 422s and `/openapi.json` |
| `selfheal/healer.py` | Tool registry (versioned), error parser, Tier 1 rules, validator, stop switch, JSONL audit log, baseline retry agent |
| `selfheal/llm.py` | Tier 2: an LLM picks which existing argument fits a new field (never supplies values) |
| `selfheal/cases.py` | 125 synthetic cases (5 categories x 25) + 10 harder stress cases |
| `selfheal/bench.py` | Runs both agents on the same cases and scores them |
| `app.py` | Streamlit dashboard: live demo, benchmark, stress tests, registry + audit log |

## Results (offline, rules + validator only)

| Test scenario | Cases | Baseline success | Our success | Unsafe repairs |
|---|---|---|---|---|
| Field rename | 25 | 0% | 100% | 0 |
| Type change | 25 | 0% | 100% | 0 |
| Nested field change | 25 | 0% | 100% | 0 |
| New required header | 25 | 0% | 100% | 0 |
| Missing value (must stop) | 25 | 0% | 100% | 0 |
| **Total** | **125** | **0%** | **100%** | **0** |

Median time from failure to recovery or safe stop: about 1 ms (in-process mock API). Tokens per rule-based fix: 0.

Stress cases (outside the 125): 6/10 pass, 2 safe failures (synonyms with no shared words, e.g.
`user_id -> customer_ref`, which stop and need the LLM tier), and **2 wrong repairs**:
`channel_id -> thread_id` (similar name, different meaning) and `amount -> amount_cents` (hidden unit change).

## Honest limits

- The 125 cases were written by us alongside the healer and run against our own mock API, so 100% shows the
  rules handle these patterns, not that they handle every real API.
- The baseline is a simple retry loop. An LLM agent that reads the error message itself would do better than 0%.
- Name similarity can be fooled by similar names with different meanings, and unit changes are invisible in a
  schema. Medium-confidence matches should go to the LLM tier or a human for approval.
- The Tier 2 LLM path is implemented but was not exercised in these results (no API key was set).

## Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repository (`.venv/` and `logs/` are git-ignored).
2. Go to https://share.streamlit.io, sign in with GitHub, click **Create app**, pick the repo and set the
   main file to `app.py`.
3. Optional: add `ANTHROPIC_API_KEY` under **Advanced settings -> Secrets** to turn on Tier 2.
