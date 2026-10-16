"""Repair policy: which repairs may be applied automatically and which must wait for a human.

A schema-valid request can still be wrong in meaning (a renamed field may hide a unit or a
different entity). So the default is: only tightly defined, reversible changes are automatic;
anything ambiguous is held for approval or stopped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from rapidfuzz import fuzz

# words that carry no identity of their own ("target.id" says nothing about *what* id)
GENERIC = {"id", "ids", "ref", "reference", "number", "no", "num", "key", "identifier", "data", "payload",
           "meta", "body", "target", "destination", "params", "value", "info", "details", "object", "obj",
           "item", "record", "field", "the", "a"}
# unit / scale words: a change here usually means the value must be transformed, not just moved
UNITS = {"cents", "paise", "pence", "ms", "millis", "milliseconds", "sec", "secs", "seconds", "minutes",
         "mins", "hours", "hrs", "days", "kg", "grams", "lb", "lbs", "km", "meters", "metres", "miles",
         "usd", "inr", "eur", "gbp", "pct", "percent", "percentage", "ratio", "bps", "bytes", "kb", "mb",
         "gb", "celsius", "fahrenheit", "utc", "epoch", "unix"}


def _words(name: str) -> list[str]:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[._\-\s]+", name.lower()) if w]


@dataclass
class RepairPolicy:
    auto_repair: bool = True              # False: every repair for this tool needs approval
    auto_min_similarity: float = 75       # name match needed to repair without a human
    approval_min_similarity: float = 50   # below this, the match is not even proposed
    llm_needs_approval: bool = True       # LLM-proposed mappings always go to a human
    auto_type_conversion: bool = True     # lossless conversions ("123" -> 123) are automatic
    auto_headers_from_config: bool = True # headers taken from approved config are automatic
    promote_after: int = 3                # successful calls before a repair is shared with other agents

    def assess(self, source: str, target: str, score: float) -> tuple[str, list[str]]:
        """Return ("auto" | "approval" | "block", reasons) for mapping argument `source` to field `target`."""
        if score < self.approval_min_similarity:
            return "block", [f"{source} -> {target}: names too different (similarity {score:.0f})"]
        reasons = []
        su, tu = set(_words(source)) & UNITS, set(_words(target)) & UNITS
        if su != tu:
            reasons.append(f"{source} -> {target}: possible unit change ({', '.join(sorted(su ^ tu))})")
        se = [w for w in _words(source) if w not in GENERIC and w not in UNITS]
        te = [w for w in _words(target) if w not in GENERIC and w not in UNITS]
        if se and te and not any(fuzz.ratio(a, b) >= 70 for a in se for b in te):
            reasons.append(f"{source} -> {target}: names refer to different things ({' '.join(se)} vs {' '.join(te)})")
        if score < self.auto_min_similarity:
            reasons.append(f"{source} -> {target}: medium-confidence match (similarity {score:.0f})")
        return ("approval" if reasons else "auto"), reasons


@dataclass
class PolicySet:
    default: RepairPolicy = field(default_factory=RepairPolicy)
    per_tool: dict[str, dict] = field(default_factory=dict)   # tool name -> overrides

    def for_tool(self, tool: str) -> RepairPolicy:
        return replace(self.default, **self.per_tool.get(tool, {}))
