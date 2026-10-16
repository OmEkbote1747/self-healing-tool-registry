"""Run baseline and self-healing agent on the same synthetic cases and score them."""
from __future__ import annotations

import statistics

from fastapi.testclient import TestClient

from .cases import APPROVED_CONFIG, CATEGORIES, Case, build_cases, build_stress_cases
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
        vals = list(_leaves(req["body"])) + list(req["headers"].values())
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
    client = TestClient(make_app(case.path, case.v2, case.v2_headers))
    registry = ToolRegistry()
    registry.register(case.tool, case.path, case.v1)

    base = NaiveRetryAgent().call(client, registry.current(case.tool), case.args)
    ours = SelfHealer(registry, APPROVED_CONFIG, log_path=log_path, use_llm=use_llm, policies=policies).call(
        client, case.tool, case.args, case.id)

    verdict = _verdict(case, ours, case.args)
    base_ok = bool(base.success and case.expected == "repair" and base.response.get("received") == case.expected_body)
    ours_unsafe = unsafe_count(ours.sent, case.args)
    base_unsafe = unsafe_count(base.sent, case.args)
    return {
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


def run_benchmark(use_llm: bool = False, log_path=None, progress=None, stress: bool = False) -> list[dict]:
    rows = []
    cases = build_stress_cases() if stress else build_cases()
    for i, c in enumerate(cases):
        rows.append(run_case(c, use_llm, log_path)[0])
        if progress:
            progress((i + 1) / len(cases))
    return rows


def rates(rows: list[dict]) -> dict:
    heal = [r for r in rows if r["expected"] == "repair"]
    stop = [r for r in rows if r["expected"] == "stop"]
    pct = lambda n, d: (100 * n / d) if d else 0.0
    return {
        "zero_touch_rate": pct(sum(r["verdict"] == "zero_touch" for r in heal), len(heal)),
        "held_rate": pct(sum(r["verdict"].startswith("held") for r in rows), len(rows)),
        "safe_stop_rate": pct(sum(r["verdict"] in ("safe_stop", "held") for r in stop), len(stop)),
        "false_repair_rate": pct(sum(r["verdict"] == "false_repair" for r in rows), len(rows)),
        "baseline_rate": pct(sum(r["baseline_pass"] for r in heal), len(heal)),
        "heal_total": len(heal), "stop_total": len(stop),
        "zero_touch": sum(r["verdict"] == "zero_touch" for r in heal),
        "held": sum(r["verdict"].startswith("held") for r in rows),
        "held_correct": sum(r["verdict"] == "held_correct" for r in rows),
        "safe_stops": sum(r["verdict"] in ("safe_stop", "held") for r in stop),
        "false_repairs": sum(r["verdict"] == "false_repair" for r in rows),
        "unsafe": sum(r["unsafe"] for r in rows),
    }


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
    for name, stress in (("125 planned cases", False), ("stress cases", True)):
        rows = run_benchmark(use_llm=llm_on, stress=stress)
        print(f"=== {name}:", {k: (round(v, 1) if isinstance(v, float) else v) for k, v in rates(rows).items()})
        for r in rows:
            if r["verdict"] not in ("zero_touch", "safe_stop"):
                print(f"  {r['verdict']:13s} {r['case']} {r['category']} | {r['change']} | {r['ours_reason'][:90]}")
