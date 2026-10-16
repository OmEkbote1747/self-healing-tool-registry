"""Field-name comparison used by the planner and the validator."""
from __future__ import annotations

import re

from rapidfuzz import fuzz

# words with no identity of their own ("target.id" says nothing about *what* id)
GENERIC = {"id", "ids", "ref", "reference", "number", "no", "num", "key", "identifier", "data", "payload",
           "meta", "body", "target", "destination", "params", "value", "info", "details", "object", "obj",
           "item", "record", "field", "the", "a", "query", "path", "response"}
# unit / scale words: a change here usually means the value must be transformed, not just moved
UNITS = {"cents", "paise", "pence", "ms", "millis", "milliseconds", "sec", "secs", "seconds", "minutes",
         "mins", "hours", "hrs", "days", "kg", "grams", "lb", "lbs", "km", "meters", "metres", "miles",
         "usd", "inr", "eur", "gbp", "pct", "percent", "percentage", "ratio", "bps", "bytes", "kb", "mb",
         "gb", "celsius", "fahrenheit", "utc", "epoch", "unix"}


def words(name: str) -> list[str]:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[._\-:\s]+", name.lower()) if w]


def tokens(name: str) -> str:
    return " ".join(words(name))


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return max(fuzz.token_set_ratio(ta, tb), fuzz.ratio(ta.replace(" ", ""), tb.replace(" ", "")))


def unit_change(source: str, target: str) -> set[str]:
    return (set(words(source)) & UNITS) ^ (set(words(target)) & UNITS)


def entity_mismatch(source: str, target: str) -> bool:
    se = [w for w in words(source) if w not in GENERIC and w not in UNITS]
    te = [w for w in words(target) if w not in GENERIC and w not in UNITS]
    return bool(se and te and not any(fuzz.ratio(a, b) >= 70 for a in se for b in te))
