"""Baselines to compare against.

NaiveRetryAgent    - resends the same request until it runs out of iterations (a retry loop with no repair)
GreedyRepairAgent  - reads the 422 errors and patches the request itself, like an agent that feeds errors
                     back to a model and lets it "fix" the call: it fills each missing field with the most
                     similar leftover argument (however weak the match), converts types when it can, and
                     invents a placeholder when nothing fits. It never asks a human and never stops early.

The greedy agent is a heuristic stand-in for an error-reading LLM agent, not a real model; it is here to
show what happens when repairs are made without confidence thresholds, unit checks or a validator.
"""
from __future__ import annotations

import copy

from .healer import Outcome, ToolDef, _set, convert, similarity
from .security import redact, redact_reply

PLACEHOLDER = {"int": 0, "float": 0.0, "bool": False, "str": "unknown"}


def _get(d: dict, path: str):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


class GreedyRepairAgent:
    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations

    def call(self, client, td: ToolDef, args: dict) -> Outcome:
        out = Outcome(False, None, None)
        req = td.build_request(args, {})
        body, headers, query = copy.deepcopy(req["body"] or {}), {}, dict(req["query"])
        leftovers = dict(args)
        for i in range(self.max_iterations):
            r = client.request(req["method"], req["url"], json=body if req["body"] is not None or body else None,
                               params=query or None, headers=headers)
            out.calls += 1
            out.sent.append({"body": copy.deepcopy(body), "headers": dict(headers), "params": dict(query),
                             "status": r.status_code})
            try:
                payload = r.json()
            except ValueError:
                payload = {"detail": r.text[:200]}
            out.trace.append({"step": f"Attempt {i + 1}", "ok": r.status_code < 400,
                              "detail": {"sent": redact(body), "status": r.status_code, "reply": redact_reply(payload)}})
            if r.status_code < 400:
                out.success, out.status, out.response = True, r.status_code, payload
                return out
            if r.status_code not in (400, 422) or not isinstance(payload.get("detail"), list):
                break
            out.attempts += 1
            for e in payload["detail"]:
                loc = e.get("loc", [])
                where, path = (loc[0], ".".join(str(x) for x in loc[1:])) if loc else ("body", "")
                if where == "header":
                    headers[loc[1]] = "unknown"                       # guesses a credential
                    continue
                target = query if where == "query" else None
                etype = e.get("type", "")
                if etype == "missing":
                    name = path.split(".")[-1]
                    cands = sorted(leftovers, key=lambda a: similarity(name, a), reverse=True)
                    value = args[cands[0]] if cands else PLACEHOLDER["str"]
                    if cands:
                        leftovers.pop(cands[0])
                    if target is not None:
                        target[path] = value
                    else:
                        _set(body, path, value)
                elif any(etype.startswith(t) for t in ("int", "float", "bool", "string")):
                    want = {"string": "str"}.get(etype.split("_")[0], etype.split("_")[0])
                    current = query.get(path) if target is not None else _get(body, path)
                    try:
                        value = convert(current, want)
                    except Exception:
                        value = PLACEHOLDER.get(want, "unknown")          # invents a value
                    if target is not None:
                        target[path] = value
                    else:
                        _set(body, path, value)
                elif etype in ("literal_error", "enum"):
                    allowed = (e.get("ctx") or {}).get("expected", "")
                    first = allowed.split(" or ")[0].split(",")[0].strip(" '\"")
                    if first:
                        _set(body, path, first)                         # picks an allowed value at random
        out.stopped = True
        out.stop_reason = "gave up"
        return out
