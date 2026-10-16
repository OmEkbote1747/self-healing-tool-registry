"""Baseline-versus-ADAPTIX evaluation on deterministic, SIMULATED suites.

Suites:  scenarios   - the 19 Chaos Lab scenarios
         planned     - the 125 planned cases (5 categories x 25)
         adversarial - the 30 JSON fixtures in fixtures/adversarial.json
Each case runs twice on fresh simulated APIs: baseline (plain client, no self-healing) and ADAPTIX.
Ground truth: the expected outcome, and for repairable cases the exact body a correct repair must apply,
so a recovery that applied the wrong values counts as an *incorrect patch acceptance*.
"""
from __future__ import annotations

import statistics
import time
import uuid

from . import simulation as sim
from .cases import build_cases, load_fixtures
from .metrics import pct
from .llm import is_live
from .mock_api import make_app
from .models import ToolSpec

SUITES = {"scenarios": "19 Chaos Lab scenarios", "planned": "125 planned cases", "adversarial": "30 adversarial fixtures"}
CONFIGURED_HEADERS = {"x-api-key", "x-client-version"}    # headers that have a (simulated) credential group


def _run_case(engine, c, heal: bool, run_id: str) -> dict:
    mode = "adaptix" if heal else "baseline"
    name = f"{c.id.lower()}-{mode}-{run_id}"
    group_for = {}
    for h in c.v2_headers:
        if h in CONFIGURED_HEADERS:
            g = f"sim-hdr-{h}"
            engine.creds.add_simulated_group(g, h, [f"{g}-a"])
            group_for[h] = g
    engine.upstreams.set_simulated(name, make_app(c.path, c.v1, method=c.method, title=c.note))
    api_group = group_for.get("x-api-key")                # the operator binds the tool to its credential group
    engine.register(ToolSpec(name=name, base_url=f"sim://{name}", path=c.path, method=c.method,
                             description=c.note, simulated=True, auth_header="x-api-key" if api_group else None,
                             credential_group=api_group))
    engine.upstreams.set_simulated(name, make_app(c.path, c.v2, c.v2_headers, method=c.app_method or c.method,
                                                  content_type=c.content_type, title=c.note))
    res = engine.execute(name, dict(c.args), scenario=f"eval:{c.id}", heal=heal)
    outcome = "recovered" if res["status"] in ("ok", "recovered") else res["status"]
    expected = ("recovered" if c.expected == "repair" else "stopped") if heal else "stopped"
    applied = (res.get("result") or {}).get("received") if outcome == "recovered" else None
    if c.method == "GET" and applied is not None:
        applied = {k: v for k, v in applied.items() if v}
    wrong = outcome == "recovered" and (c.expected == "stop" or (c.expected_body is not None and applied != c.expected_body))
    inc = engine.storage.incident(res["incident"]) if res.get("incident") else None
    return {"case": c.id, "category": c.category, "change": c.note, "mode": mode, "expected": expected,
            "outcome": outcome, "correct": outcome == expected and not wrong, "incorrect_patch": wrong,
            "duration_ms": res.get("duration_ms"), "failure_category": res.get("category"),
            "retries": inc.retries if inc else 0,
            "plan_rejected": bool(inc and inc.validation and not inc.validation.get("accepted")),
            "plan_proposed": bool(inc and inc.validation),
            "tokens": ((inc.llm or {}).get("prompt_tokens") or 0) + ((inc.llm or {}).get("output_tokens") or 0) if inc else 0,
            "rolled_back": bool(inc and inc.rolled_back), "reason": res.get("reason")}


def _cases(suite: str):
    if suite == "planned":
        return build_cases()
    if suite == "adversarial":
        return load_fixtures("adversarial")
    raise KeyError(suite)


def run(engine, suite: str = "scenarios", seed: int = 7, progress=None) -> dict:
    """Run a suite in both modes and return per-case rows plus a summary per mode."""
    t0 = time.perf_counter()
    rows: list[dict] = []
    run_id = f"{seed}-{uuid.uuid4().hex[:6]}"
    if suite == "scenarios":
        items = sim.SCENARIOS
        total = len(items) * 2
        for i, s in enumerate(items):
            for heal in (False, True):
                r = sim.run(engine, s.id, seed=seed, heal=heal)
                inc = engine.storage.incident(r["incident"]) if r.get("incident") else None
                rows.append({"case": s.id, "category": s.group, "change": s.title, "mode": r["mode"],
                             "expected": r["expected"], "outcome": r["outcome"], "correct": r["matches_expectation"],
                             "incorrect_patch": r["outcome"] == "recovered" and r["expected"] != "recovered",
                             "duration_ms": r["duration_ms"], "failure_category": r["category"],
                             "retries": inc.retries if inc else 0,
                             "plan_rejected": bool(inc and inc.validation and not inc.validation.get("accepted")),
                             "plan_proposed": bool(inc and inc.validation),
                             "tokens": ((inc.llm or {}).get("prompt_tokens") or 0) + ((inc.llm or {}).get("output_tokens") or 0) if inc else 0,
                             "rolled_back": bool(inc and inc.rolled_back), "reason": r["reason"]})
                if progress:
                    progress(len(rows) / total)
    else:
        cases = _cases(suite)
        total = len(cases) * 2
        for c in cases:
            for heal in (False, True):
                rows.append(_run_case(engine, c, heal, run_id))
                if progress:
                    progress(len(rows) / total)
    summary = {mode: summarize([r for r in rows if r["mode"] == mode]) for mode in ("baseline", "adaptix")}
    out = {"suite": suite, "label": SUITES[suite], "seed": seed, "simulated": True, "cases": len(rows) // 2,
           "llm": engine.llm.status().get("provider"), "llm_real": is_live(engine.llm),
           "duration_s": round(time.perf_counter() - t0, 2), "summary": summary, "rows": rows}
    engine.storage.event("evaluation_run", None, {k: v for k, v in out.items() if k != "rows"}, simulated=True)
    return out


def summarize(rows: list[dict]) -> dict:
    should_recover = [r for r in rows if r["expected"] == "recovered"]
    should_stop = [r for r in rows if r["expected"] == "stopped"]
    recovered = [r for r in rows if r["outcome"] == "recovered"]
    times = [r["duration_ms"] for r in recovered if r["duration_ms"] is not None]
    proposed = [r for r in rows if r["plan_proposed"]]
    rollbacks = [r for r in rows if r["outcome"] == "rolled_back"]
    return {
        "n": len(rows),
        "accuracy": pct(sum(r["correct"] for r in rows), len(rows)),
        "recovery_success_rate": pct(sum(r["outcome"] == "recovered" and not r["incorrect_patch"] for r in should_recover),
                                     len(should_recover)),
        "zero_touch_adaptation_rate": pct(sum(r["outcome"] == "recovered" and not r["incorrect_patch"]
                                              for r in should_recover if r["failure_category"] == "schema_drift"
                                              or r["failure_category"] is None),
                                          len([r for r in should_recover])),
        "safe_stop_rate": pct(sum(r["outcome"] in ("stopped", "rolled_back") for r in should_stop), len(should_stop)),
        # repairable in principle, but ADAPTIX stopped instead of guessing (no wrong data was sent)
        "safe_misses": sum(r["outcome"] in ("stopped", "rolled_back") for r in should_recover),
        "incorrect_patch_acceptance_rate": pct(sum(r["incorrect_patch"] for r in rows), len(recovered) or 1),
        "incorrect_patches": sum(r["incorrect_patch"] for r in rows),
        "unsafe_patch_rejection_rate": pct(sum(r["plan_rejected"] for r in proposed), len(proposed)),
        "median_recovery_ms": round(statistics.median(times), 1) if times else None,
        "mean_recovery_ms": round(statistics.mean(times), 1) if times else None,
        "under_4s_rate": pct(sum(t < 4000 for t in times), len(times)),
        "mean_retries": round(statistics.mean(r["retries"] for r in rows), 2) if rows else 0,
        "rollbacks": len(rollbacks),
        "tokens": sum(r["tokens"] for r in rows),
        "recovered": len(recovered), "should_recover": len(should_recover), "should_stop": len(should_stop),
    }


if __name__ == "__main__":                       # python -m adaptix.evaluation --suite planned
    import argparse
    import json

    from .api import State
    from .config import get_settings
    ap = argparse.ArgumentParser(description="Baseline vs ADAPTIX on simulated upstreams")
    ap.add_argument("--suite", choices=list(SUITES), default="scenarios")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    res = run(State(get_settings()).engine, a.suite, a.seed)
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=2))
