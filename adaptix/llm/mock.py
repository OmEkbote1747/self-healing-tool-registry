"""MOCK provider - NOT a real model. Deterministic, for unit tests and offline demos only.

It proposes a mapping only when a field description quotes a candidate argument verbatim, so its output is
predictable. Results are always labelled provider="mock (not a real model)", real=False.
"""
from __future__ import annotations

import time

from ..models import LLMRepairPlan, Mapping, now_iso
from . import LLMResult


class MockProvider:
    name, model, real = "mock (not a real model)", "deterministic-mock", False

    def configured(self) -> bool:
        return True

    def plan(self, context: dict) -> LLMResult:
        t0 = time.perf_counter()
        mappings = []
        for f in context.get("unresolved_fields", []):
            desc = f.get("description") or ""
            hit = next((a["name"] for a in context.get("candidate_arguments", []) if a["name"] in desc), None)
            mappings.append(Mapping(target_field=f["field"], source_arg=hit, evidence=desc if hit else ""))
        plan = LLMRepairPlan(diagnosis="mock diagnosis", mappings=mappings, confidence=0.9 if mappings else 0.0,
                             evidence=[m.evidence for m in mappings if m.source_arg])
        return LLMResult(provider=self.name, model=self.model, ok=True, real=False, plan=plan,
                         duration_ms=round((time.perf_counter() - t0) * 1000, 2), attempts=1, ts=now_iso())

    def check(self) -> LLMResult:
        return LLMResult(provider=self.name, model=self.model, ok=True, real=False, attempts=0, ts=now_iso())

    def status(self) -> dict:
        return {"provider": self.name, "model": self.model, "configured": True, "connectivity": "mock",
                "note": "deterministic mock for tests and demos; no real model is called"}
