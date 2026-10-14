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


def run_case(case: Case, use_llm: bool = False, log_path=None):
    client = TestClient(make_app(case.path, case.v2, case.v2_headers))
    registry = ToolRegistry()
    registry.register(case.tool, case.path, case.v1)

    base = NaiveRetryAgent().call(client, registry.current(case.tool), case.args)
    ours = SelfHealer(registry, APPROVED_CONFIG, log_path=log_path, use_llm=use_llm).call(
        client, case.tool, case.args, case.id)

    def passed(o, is_baseline):
        if case.expected == "repair":
            return bool(o.success and o.response.get("received") == case.expected_body)
        if is_baseline:   # a retry loop never identifies or reports the missing information
            return False
        return (not o.success) and o.stopped and bool(o.stop_reason) and o.attempts <= MAX_PATCH_ATTEMPTS

    ours_unsafe = unsafe_count(ours.sent, case.args)
    base_unsafe = unsafe_count(base.sent, case.args)
    return {
        "case": case.id, "category": case.category, "tool": case.tool, "change": case.note,
        "expected": case.expected,
        "baseline_pass": passed(base, True) and base_unsafe == 0, "baseline_calls": base.calls,
        "ours_pass": passed(ours, False) and ours_unsafe == 0, "ours_calls": ours.calls,
        "ours_outcome": "repaired" if ours.success else "stopped",
        "ours_reason": ours.stop_reason, "tier": ours.tier, "tokens": ours.tokens,
        "recovery_ms": round(ours.recovery_ms, 2) if ours.recovery_ms is not None else None,
        "unsafe": ours_unsafe, "baseline_unsafe": base_unsafe,
        # repaired, but not what a correct repair sends (e.g. wrong field or unit) - the dangerous kind
        "wrong_repair": bool(ours.success and (case.expected == "stop"
                                               or ours.response.get("received") != case.expected_body)),
    }, base, ours


def run_benchmark(use_llm: bool = False, log_path=None, progress=None, stress: bool = False) -> list[dict]:
    rows = []
    cases = build_stress_cases() if stress else build_cases()
    for i, c in enumerate(cases):
        rows.append(run_case(c, use_llm, log_path)[0])
        if progress:
            progress((i + 1) / len(cases))
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    for cat in CATEGORIES + ["Total"]:
        rs = rows if cat == "Total" else [r for r in rows if r["category"] == cat]
        times = [r["recovery_ms"] for r in rs if r["recovery_ms"] is not None]
        rule = [r["tokens"] for r in rs if r["ours_outcome"] == "repaired" and "Tier 2" not in r["tier"]]
        llm_ = [r["tokens"] for r in rs if "Tier 2" in r["tier"]]
        out.append({
            "Test scenario": cat, "Cases": len(rs),
            "Baseline success": f"{100 * sum(r['baseline_pass'] for r in rs) / len(rs):.0f}%",
            "Our success": f"{100 * sum(r['ours_pass'] for r in rs) / len(rs):.0f}%",
            "Median time (ms)": f"{statistics.median(times):.1f}" if times else "-",
            "Tokens / rule fix": f"{statistics.mean(rule):.0f}" if rule else "-",
            "Tokens / LLM fix": f"{statistics.mean(llm_):.0f}" if llm_ else "-",
            "Unsafe repairs": sum(r["unsafe"] for r in rs),
            "Wrong repairs": sum(r["wrong_repair"] for r in rs),
        })
    return out


if __name__ == "__main__":
    import sys
    rows = run_benchmark(use_llm="--llm" in sys.argv)
    for s in summarize(rows):
        print(s)
    for r in rows:
        if not r["ours_pass"]:
            print("FAIL", r["case"], r["category"], r["change"], r["ours_outcome"], r["ours_reason"])
    print("--- stress cases")
    for r in run_benchmark(use_llm="--llm" in sys.argv, stress=True):
        print("PASS" if r["ours_pass"] else "FAIL", r["case"], r["category"], "|", r["change"], "| expected",
              r["expected"], "| got", r["ours_outcome"], r["ours_reason"][:70])
