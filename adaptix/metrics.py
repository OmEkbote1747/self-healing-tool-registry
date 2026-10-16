"""Metrics calculated only from persisted records (incidents, executions, audit events)."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict


def pct(n, d) -> float | None:
    return round(100.0 * n / d, 1) if d else None


def compute(storage, simulated: bool | None = None) -> dict:
    incs = storage.incidents(simulated=simulated, limit=5000)
    execs = storage.executions(simulated=simulated)
    drift = [i for i in incs if i.category == "schema_drift"]
    recovered = [i for i in incs if i.status == "recovered"]
    times = [i.duration_ms for i in recovered if i.duration_ms is not None]
    validated = [i for i in incs if i.validation]
    rollback_attempts = [i for i in incs if any(s.stage == "rollback" for s in i.timeline)]
    llm_calls = [i.llm for i in incs if i.llm]
    per_tool: dict[str, dict] = defaultdict(lambda: Counter())
    for i in incs:
        per_tool[i.tool]["incidents"] += 1
        per_tool[i.tool][i.status] += 1
    return {
        "scope": "all" if simulated is None else ("simulated" if simulated else "real"),
        "sample": {"incidents": len(incs), "executions": len(execs),
                   "simulated_incidents": sum(i.simulated for i in incs)},
        "executions_ok": sum(e["ok"] for e in execs),
        "incidents_by_status": dict(Counter(i.status for i in incs)),
        "incidents_by_category": dict(Counter(i.category for i in incs)),
        "recovery_success_rate": pct(len(recovered), len(incs)),
        "zero_touch_adaptation_rate": pct(sum(i.status == "recovered" and i.zero_touch for i in drift), len(drift)),
        "schema_drift_incidents": len(drift),
        "recovered": len(recovered),
        "failed_recoveries": sum(i.status in ("failed", "rolled_back") for i in incs),
        "stopped_safely": sum(i.status == "stopped" for i in incs),
        "rollbacks": sum(i.rolled_back for i in incs),
        "rollback_success_rate": pct(sum(i.rolled_back for i in rollback_attempts), len(rollback_attempts)),
        "median_recovery_ms": round(statistics.median(times), 1) if times else None,
        "mean_recovery_ms": round(statistics.mean(times), 1) if times else None,
        "under_4s_rate": pct(sum(t < 4000 for t in times), len(times)),
        "mean_retries": round(statistics.mean(i.retries for i in incs), 2) if incs else None,
        "unsafe_patch_rejection_rate": pct(sum(not i.validation.get("accepted") for i in validated), len(validated)),
        "llm": {"calls": len(llm_calls), "real_calls": sum(bool(c.get("real")) for c in llm_calls),
                "ok": sum(bool(c.get("ok")) for c in llm_calls),
                "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in llm_calls),
                "output_tokens": sum(c.get("output_tokens") or 0 for c in llm_calls),
                "error_categories": dict(Counter(c.get("error_category") for c in llm_calls if c.get("error_category")))},
        "per_tool": {t: dict(c) for t, c in per_tool.items()},
    }
