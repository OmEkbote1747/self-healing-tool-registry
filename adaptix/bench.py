"""Run baseline and self-healing agent on the same synthetic cases and score them."""
from __future__ import annotations

import statistics

from fastapi.testclient import TestClient

from .baselines import GreedyRepairAgent
from .cases import APPROVED_CONFIG, CATEGORIES, Case, build_cases, build_stress_cases, load_fixtures
from .healer import MAX_PATCH_ATTEMPTS, NaiveRetryAgent, SelfHealer, ToolRegistry, _leaves, convert
from .mock_api import make_app


def _allowed_values(args: dict) -> set[str]:
    """Every value a safe repair could send: the args, their lossless conversions, approved config."""
    ok = {repr(v) for v in args.values()} | {repr(v) for v in APPROVED_CONFIG.values()}
    for v in args.values():
        for t in ("str", "int", "float", "bool"):
            try:
                ok.add(repr(convert(v, t)))
            except ValueError:
                pass
    return ok


def unsafe_count(sent: list[dict], args: dict) -> int:
    allowed = _allowed_values(args)
    bad = 0
    for req in sent:
        vals = list(_leaves(req["body"])) + list(req["headers"].values()) + list(req.get("params", {}).values())
        bad += any(repr(v) not in allowed for v in vals)
    return bad


def _verdict(case: Case, o, args) -> str:
    """zero_touch | held_correct | held | safe_stop | safe_miss | false_repair"""
    if o.success:
        ok = case.expected == "repair" and o.response.get("received") == case.expected_body
        return "zero_touch" if ok else "false_repair"
    if o.awaiting is not None:
        if case.expected == "repair":
            body, _ = o.awaiting.candidate.build(args, APPROVED_CONFIG)
            return "held_correct" if body == case.expected_body else "held"
        return "held"
    return "safe_stop" if case.expected == "stop" else "safe_miss"


def run_case(case: Case, use_llm: bool = False, log_path=None, policies=None):
    def fresh():
        return TestClient(make_app(case.path, case.v2, case.v2_headers, method=case.app_method or case.method,
                                   content_type=case.content_type))
    registry = ToolRegistry()
    registry.register(case.tool, case.path, case.v1, method=case.method)
    v1 = registry.current(case.tool)

    base = NaiveRetryAgent().call(fresh(), v1, case.args)
    greedy = GreedyRepairAgent().call(fresh(), v1, case.args)
    ours = SelfHealer(registry, APPROVED_CONFIG, log_path=log_path, use_llm=use_llm, policies=policies).call(
        fresh(), case.tool, case.args, case.id)

    verdict = _verdict(case, ours, case.args)
    base_ok = bool(base.success and case.expected == "repair" and base.response.get("received") == case.expected_body)
    ours_unsafe = unsafe_count(ours.sent, case.args)
    base_unsafe = unsafe_count(base.sent, case.args)
    return {
        "naive_verdict": _verdict(case, base, case.args), "naive_unsafe": base_unsafe,
        "greedy_verdict": _verdict(case, greedy, case.args), "greedy_unsafe": unsafe_count(greedy.sent, case.args),
        "greedy_calls": greedy.calls,
        "case": case.id, "category": case.category, "tool": case.tool, "change": case.note,
        "expected": case.expected,
        "baseline_pass": base_ok and base_unsafe == 0, "baseline_calls": base.calls,
        # headline correctness: healable -> fixed with no human; unhealable -> stopped or held, never sent
        "ours_pass": verdict in ("zero_touch", "safe_stop") or (case.expected == "stop" and verdict == "held"),
        "verdict": verdict, "ours_calls": ours.calls, "ours_outcome": ours.state,
        "ours_reason": ours.stop_reason, "tier": ours.tier, "tokens": ours.tokens,
        "recovery_ms": round(ours.recovery_ms, 2) if ours.recovery_ms is not None else None,
        "unsafe": ours_unsafe, "baseline_unsafe": base_unsafe,
        "wrong_repair": verdict == "false_repair",
    }, base, ours


SUITES = {"planned": "125 planned cases", "stress": "10 stress cases", "adversarial": "30 adversarial fixtures"}


def suite_cases(suite: str) -> list[Case]:
    return {"planned": build_cases, "stress": build_stress_cases, "adversarial": load_fixtures}[suite]()


def run_benchmark(use_llm: bool = False, log_path=None, progress=None, stress: bool = False,
                  suite: str | None = None) -> list[dict]:
    rows = []
    cases = suite_cases(suite or ("stress" if stress else "planned"))
    for i, c in enumerate(cases):
        rows.append(run_case(c, use_llm, log_path)[0])
        if progress:
            progress((i + 1) / len(cases))
    return rows


def rates(rows: list[dict], agent: str = "ours") -> dict:
    """Zero-touch, held, safe-stop and false-repair rates for one agent (ours | naive | greedy)."""
    vk = {"ours": "verdict", "naive": "naive_verdict", "greedy": "greedy_verdict"}[agent]
    uk = {"ours": "unsafe", "naive": "naive_unsafe", "greedy": "greedy_unsafe"}[agent]
    heal = [r for r in rows if r["expected"] == "repair"]
    stop = [r for r in rows if r["expected"] == "stop"]
    pct = lambda n, d: (100 * n / d) if d else 0.0
    return {
        "zero_touch_rate": pct(sum(r[vk] == "zero_touch" for r in heal), len(heal)),
        "held_rate": pct(sum(r[vk].startswith("held") for r in rows), len(rows)),
        "safe_stop_rate": pct(sum(r[vk] in ("safe_stop", "held") for r in stop), len(stop)),
        "false_repair_rate": pct(sum(r[vk] == "false_repair" for r in rows), len(rows)),
        "baseline_rate": pct(sum(r["baseline_pass"] for r in heal), len(heal)),
        "heal_total": len(heal), "stop_total": len(stop),
        "zero_touch": sum(r[vk] == "zero_touch" for r in heal),
        "held": sum(r[vk].startswith("held") for r in rows),
        "held_correct": sum(r[vk] == "held_correct" for r in rows),
        "safe_stops": sum(r[vk] in ("safe_stop", "held") for r in stop),
        "false_repairs": sum(r[vk] == "false_repair" for r in rows),
        "unsafe": sum(r[uk] for r in rows),
    }


def compare(rows: list[dict]) -> list[dict]:
    """One row per agent, same cases."""
    names = {"naive": "Retry loop (no repair)", "greedy": "Error-reading greedy repair", "ours": "SchemaHeal"}
    out = []
    for agent in ("naive", "greedy", "ours"):
        n = rates(rows, agent)
        out.append({"Agent": names[agent],
                    "Zero-touch fixed": f"{n['zero_touch']} / {n['heal_total']} ({n['zero_touch_rate']:.0f}%)",
                    "Held for approval": n["held"],
                    "Safe stops": f"{n['safe_stops']} / {n['stop_total']} ({n['safe_stop_rate']:.0f}%)",
                    "False repairs": f"{n['false_repairs']} ({n['false_repair_rate']:.0f}%)",
                    "Requests with invented values": n["unsafe"]})
    return out


def summarize(rows: list[dict], categories=None) -> list[dict]:
    out = []
    cats = categories or sorted({r["category"] for r in rows}, key=lambda c: (CATEGORIES + [c]).index(c))
    for cat in cats + ["Total"]:
        rs = rows if cat == "Total" else [r for r in rows if r["category"] == cat]
        times = [r["recovery_ms"] for r in rs if r["recovery_ms"] is not None]
        rule = [r["tokens"] for r in rs if r["ours_outcome"] == "repaired" and "Tier 2" not in r["tier"]]
        heal = [r for r in rs if r["expected"] == "repair"]
        out.append({
            "Test scenario": cat, "Cases": len(rs),
            "Baseline success": f"{100 * sum(r['baseline_pass'] for r in heal) / len(heal):.0f}%" if heal else "-",
            "Zero-touch fixed": sum(r["verdict"] == "zero_touch" for r in rs),
            "Held for approval": sum(r["verdict"].startswith("held") for r in rs),
            "Safe stops": sum(r["verdict"] in ("safe_stop", "safe_miss") for r in rs),
            "False repairs": sum(r["verdict"] == "false_repair" for r in rs),
            "Median time (ms)": f"{statistics.median(times):.1f}" if times else "-",
            "Tokens / rule fix": f"{statistics.mean(rule):.0f}" if rule else "-",
            "Unsafe values sent": sum(r["unsafe"] for r in rs),
        })
    return out


if __name__ == "__main__":
    import sys
    llm_on = "--llm" in sys.argv
    for suite, label in SUITES.items():
        rows = run_benchmark(use_llm=llm_on, suite=suite)
        print(f"=== {label}")
        for c in compare(rows):
            print("   ", c)
        for r in rows:
            if r["verdict"] not in ("zero_touch", "safe_stop"):
                print(f"      {r['verdict']:13s} {r['case']} {r['category']} | {r['change']} | {r['ours_reason'][:80]}")
