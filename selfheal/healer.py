"""The self-healing layer.

    agent -> ToolRegistry -> API
                 |  422
                 v
    Error parser -> Tier 1 rules -> (Tier 2 LLM if unsure) -> Validator -> patch + log -> retry once

Hard rules:
  * a repair may only MOVE, RENAME or CONVERT values the agent already has,
    or read a header from approved config - it never invents a value
  * a repair is validated against the new schema before it is sent
  * at most MAX_PATCH_ATTEMPTS repairs per call, then a clean stop
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from rapidfuzz import fuzz

from . import llm
from .policy import PolicySet

MAX_PATCH_ATTEMPTS = 2
ELIMINATION_MIN = 50   # below this, a lone candidate is not trusted
JSON_TO_PY = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}


# ---------------------------------------------------------------- tool registry
@dataclass
class ToolDef:
    name: str
    path: str
    version: int
    body_map: dict[str, tuple[str, str | None]]   # new field path -> (source arg, convert-to type)
    header_map: dict[str, str] = field(default_factory=dict)  # header -> approved-config key
    status: str = "active"        # active | canary | rolled_back
    origin: str = "initial"       # initial | auto | approved
    owner: str | None = None      # agent that created a canary version
    successes: int = 0

    def build(self, args: dict, config: dict) -> tuple[dict, dict]:
        body: dict = {}
        for target, (src, conv) in self.body_map.items():
            value = args[src] if conv is None else convert(args[src], conv)
            _set(body, target, value)
        headers = {h: config[k] for h, k in self.header_map.items()}
        return body, headers

    def describe(self) -> dict:
        return {"version": self.version, "status": self.status, "origin": self.origin,
                "body": {t: (s if c is None else f"{s} as {c}") for t, (s, c) in self.body_map.items()},
                "headers": {h: f"config[{k}]" for h, k in self.header_map.items()}}


@dataclass
class Proposal:
    """A repair that passed validation but needs a human decision before it is used."""
    id: str
    tool: str
    base_version: int
    candidate: ToolDef
    reasons: list[str]
    status: str = "pending"       # pending | approved | rejected
    decided_by: str | None = None


class ConflictError(RuntimeError):
    """The tool changed since the proposal was made; re-diagnose instead of applying a stale repair."""


class ToolRegistry:
    """Versioned tool definitions shared by every agent.

    New automatic repairs start as a *canary*: only the agent that created them uses them until they
    have succeeded `promote_after` times. A failing canary is rolled back. Human-approved repairs are
    active immediately.
    """

    def __init__(self, promote_after: int = 3):
        self.versions: dict[str, list[ToolDef]] = {}
        self.proposals: dict[str, Proposal] = {}
        self.promote_after = promote_after

    def register(self, name: str, path: str, schema: dict[str, str]):
        self.versions[name] = [ToolDef(name, path, 1, {k: (k, None) for k in schema})]

    def current(self, name: str, agent_id: str | None = None) -> ToolDef:
        for td in reversed(self.versions[name]):
            if td.status == "active" or (td.status == "canary" and td.owner == agent_id):
                return td
        return self.versions[name][0]

    def latest_version(self, name: str) -> int:
        return self.versions[name][-1].version

    def add(self, td: ToolDef):
        td.version = self.latest_version(td.name) + 1
        self.versions[td.name].append(td)

    def record_success(self, td: ToolDef):
        td.successes += 1
        if td.status == "canary" and td.successes >= self.promote_after:
            td.status = "active"

    def record_failure(self, td: ToolDef):
        if td.status == "canary":
            td.status = "rolled_back"

    def rollback(self, name: str) -> ToolDef:
        live = [v for v in self.versions[name] if v.status in ("active", "canary")]
        if len(live) > 1:
            live[-1].status = "rolled_back"
        return self.current(name)

    # -- human approval
    def propose(self, tool: str, base_version: int, candidate: ToolDef, reasons: list[str]) -> Proposal:
        p = Proposal(uuid.uuid4().hex[:8], tool, base_version, candidate, reasons)
        self.proposals[p.id] = p
        return p

    def pending(self) -> list[Proposal]:
        return [p for p in self.proposals.values() if p.status == "pending"]

    def approve(self, proposal_id: str, by: str = "reviewer") -> ToolDef:
        p = self.proposals[proposal_id]
        if p.status != "pending":
            raise ConflictError(f"proposal {proposal_id} is already {p.status}")
        if self.current(p.tool).version != p.base_version:
            p.status = "rejected"
            raise ConflictError("tool changed since this repair was proposed; re-run diagnosis")
        td = copy.deepcopy(p.candidate)
        td.status, td.origin, td.owner = "active", "approved", None
        self.add(td)
        p.status, p.decided_by = "approved", by
        return td

    def reject(self, proposal_id: str, by: str = "reviewer"):
        p = self.proposals[proposal_id]
        p.status, p.decided_by = "rejected", by


# ---------------------------------------------------------------- helpers
def _set(d: dict, path: str, value):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def convert(value, to: str):
    """Lossless conversions only; raises ValueError otherwise."""
    if to == "str":
        if isinstance(value, (dict, list)):
            raise ValueError("not a scalar")
        return str(value)
    if to == "int":
        if isinstance(value, bool):
            raise ValueError("bool is not int")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"{value!r} is not a whole number")
    if to == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
            return float(value)
        raise ValueError(f"{value!r} is not a number")
    if to == "bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValueError(f"{value!r} is not true/false")
    raise ValueError(f"unknown type {to}")


def py_type(v) -> str:
    return {bool: "bool", int: "int", float: "float"}.get(type(v), "str")


def tokens(name: str) -> str:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return " ".join(t for t in re.split(r"[._\-\s]+", name.lower()) if t)


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return max(fuzz.token_set_ratio(ta, tb), fuzz.ratio(ta.replace(" ", ""), tb.replace(" ", "")))


# ---------------------------------------------------------------- schema reading
def read_openapi(client, path: str) -> tuple[dict, dict[str, str], list[str]] | None:
    """Return (resolved body json-schema, {leaf path: type}, [required headers]) from /openapi.json."""
    try:
        spec = client.get("/openapi.json").json()
        op = spec["paths"][path]["post"]
    except Exception:
        return None
    comps = spec.get("components", {}).get("schemas", {})

    def resolve(s):
        if isinstance(s, dict):
            if "$ref" in s:
                return resolve(comps[s["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in s.items()}
        if isinstance(s, list):
            return [resolve(x) for x in s]
        return s

    body = resolve(op["requestBody"]["content"]["application/json"]["schema"])
    leaves: dict[str, str] = {}

    def walk(s, prefix=""):
        for name, sub in s.get("properties", {}).items():
            p = f"{prefix}{name}"
            if sub.get("type") == "object" or "properties" in sub:
                walk(sub, p + ".")
            elif name in s.get("required", []):
                leaves[p] = JSON_TO_PY.get(sub.get("type"), "str")

    walk(body)
    headers = [p["name"].lower() for p in op.get("parameters", []) if p.get("in") == "header" and p.get("required")]
    return body, leaves, headers


def schema_from_errors(td: ToolDef, args: dict, errors: list[dict]) -> tuple[None, dict[str, str], list[str]]:
    """Fallback when /openapi.json is not available: infer the new shape from 422 error locations."""
    leaves = {t: py_type(convert(args[s], c) if c else args[s]) for t, (s, c) in td.body_map.items()}
    headers = []
    for e in errors:
        loc = e.get("loc", [])
        if loc and loc[0] == "header":
            headers.append(loc[1])
            continue
        p = ".".join(str(x) for x in loc[1:])
        etype = e.get("type", "")
        m = re.match(r"(int|float|string|bool)", etype)
        if etype == "missing":
            leaves[p] = leaves.get(p, "str")
        elif m:
            leaves[p] = {"string": "str"}.get(m.group(1), m.group(1))
    return None, leaves, headers


# ---------------------------------------------------------------- result object
@dataclass
class Outcome:
    success: bool
    status: int | None
    response: dict | None
    stopped: bool = False
    stop_reason: str = ""
    awaiting: Proposal | None = None   # repair held for human approval
    attempts: int = 0
    calls: int = 0
    tokens: int = 0
    tier: str = "-"
    recovery_ms: float | None = None
    sent: list[dict] = field(default_factory=list)  # every request actually sent
    trace: list[dict] = field(default_factory=list)

    @property
    def state(self) -> str:
        return "repaired" if self.success else ("awaiting_approval" if self.awaiting else "stopped")


# ---------------------------------------------------------------- the healer
class SelfHealer:
    def __init__(self, registry: ToolRegistry, config: dict, log_path: str | Path | None = None,
                 use_llm: bool | None = None, policies: PolicySet | None = None):
        self.registry = registry
        self.config = {k.lower(): v for k, v in config.items()}
        self.log_path = Path(log_path) if log_path else None
        self.use_llm = llm.available() if use_llm is None else use_llm
        self.policies = policies or PolicySet()

    # -- public
    def call(self, client, tool: str, args: dict, case_id: str = "", agent_id: str = "agent-A") -> Outcome:
        out = Outcome(False, None, None)
        td = self.registry.current(tool, agent_id)
        status, resp = self._send(client, td, args, out, label=f"call with tool v{td.version} ({td.status})")
        if status < 400:
            self.registry.record_success(td)
            out.success, out.status, out.response = True, status, resp
            return out
        self.registry.record_failure(td)     # a failing canary is rolled back
        t0 = time.perf_counter()
        while out.attempts < MAX_PATCH_ATTEMPTS:
            out.attempts += 1
            errors = resp.get("detail", []) if isinstance(resp, dict) else []
            decision = self._diagnose_and_validate(client, td, args, errors, out, agent_id)
            if decision is None:          # stopped, or held for approval (reason already in trace)
                out.stopped = out.awaiting is None
                break
            td = decision
            self.registry.add(td)
            self._log(case_id, tool, td, out)
            status, resp = self._send(client, td, args, out, label=f"retry with tool v{td.version} ({td.status})")
            if status < 400:
                self.registry.record_success(td)
                out.success, out.status, out.response = True, status, resp
                out.recovery_ms = (time.perf_counter() - t0) * 1000
                return out
            self.registry.record_failure(td)
        if not out.stopped and out.awaiting is None:
            out.stopped = True
            out.stop_reason = f"stop switch: still failing after {MAX_PATCH_ATTEMPTS} repair attempts"
            out.trace.append({"step": "Stop switch", "ok": False, "detail": out.stop_reason})
        out.status = status
        out.response = resp
        out.recovery_ms = (time.perf_counter() - t0) * 1000
        return out

    # -- internals
    def _send(self, client, td: ToolDef, args, out: Outcome, label: str):
        body, headers = td.build(args, self.config)
        r = client.post(td.path, json=body, headers=headers)
        out.calls += 1
        out.sent.append({"body": body, "headers": headers, "status": r.status_code})
        payload = r.json()
        out.trace.append({"step": f"Request: {label}", "ok": r.status_code < 400,
                          "detail": {"sent": body, "headers": headers, "status": r.status_code, "reply": payload}})
        return r.status_code, payload

    def _stop(self, out: Outcome, reason: str):
        out.stop_reason = reason
        out.trace.append({"step": "Stop and report", "ok": False, "detail": reason})
        return None

    def _diagnose_and_validate(self, client, td: ToolDef, args: dict, errors: list[dict], out: Outcome,
                               agent_id: str = "agent-A"):
        policy = self.policies.for_tool(td.name)
        # 1. read the new schema (prefer /openapi.json, fall back to the error locations)
        found = read_openapi(client, td.path)
        source = "/openapi.json"
        if found is None:
            found, source = schema_from_errors(td, args, errors), "422 error locations"
        body_schema, leaves, req_headers = found

        # 2. what changed
        old_targets = set(td.body_map)
        kept = {t: v for t, v in td.body_map.items() if t in leaves}
        unfilled = [p for p in leaves if p not in kept]
        orphans = [s for t, (s, _) in td.body_map.items() if t not in leaves]
        missing_headers = [h for h in req_headers if h not in td.header_map]
        changes = ([f"field removed/renamed: {t}" for t in sorted(old_targets - set(leaves))] +
                   [f"new required field: {p} ({leaves[p]})" for p in unfilled] +
                   [f"type changed: {t} is now {leaves[t]}" for t, (s, c) in kept.items()
                    if py_type(convert(args[s], c) if c else args[s]) != leaves[t]] +
                   [f"new required header: {h}" for h in missing_headers])
        out.trace.append({"step": "Detected schema change", "ok": True,
                          "detail": {"source": source, "errors": errors, "changes": changes}})

        new_map: dict[str, tuple[str, str | None]] = {}
        for t, (s, _) in kept.items():
            new_map[t] = (s, None if py_type(args[s]) == leaves[t] else leaves[t])

        # 3a. Tier 1 rules: match each new field to an orphaned argument, then ask the policy
        tier = "Tier 1 (rules)"
        notes, needs_approval = [], []
        field_reasons: dict[str, list[str]] = {}   # approval reasons per mapped field
        unresolved: list[str] = []
        used: set[str] = set()
        confirm: list[str] = []   # medium-confidence matches the LLM should double-check

        def accept(p, s, score, why):
            decision, reasons = policy.assess(s, p, score)
            if decision == "block":
                unresolved.append(p)
                return
            new_map[p] = (s, None)
            used.add(s)
            notes.append(f"{s} -> {p} ({why}, similarity {score:.0f})")
            if decision == "approval":
                field_reasons[p] = reasons
                confirm.append(p)

        if len(unfilled) == 1 and len(orphans) == 1:
            accept(unfilled[0], orphans[0], similarity(unfilled[0], orphans[0]), "only candidate")
        else:
            for p in unfilled:
                scored = sorted(((similarity(p, s), s) for s in orphans if s not in used), reverse=True)
                if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 15):
                    accept(p, scored[0][1], scored[0][0], "best match")
                else:
                    unresolved.append(p)

        # 3b. Tier 2: an LLM picks among existing arguments when the rules are unsure. It sees field
        #     names and types only - never values - and its picks always need human approval.
        if confirm and self.use_llm:
            for p in confirm:
                s = new_map.pop(p)[0]
                used.discard(s)
                unresolved.append(p)
                field_reasons.pop(p, None)
        if unresolved and [s for s in orphans if s not in used]:
            if self.use_llm:
                tier = "Tier 1 + Tier 2 (LLM)"
                cand = {s: py_type(args[s]) for s in orphans if s not in used}
                safe_errors = [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")} for e in errors]
                proposal, tok, note = llm.propose_mapping(td.name, {p: leaves[p] for p in unresolved}, cand,
                                                          safe_errors)
                out.tokens += tok
                out.trace.append({"step": "LLM proposed a repair", "ok": bool(proposal),
                                  "detail": {"proposal": proposal, "note": note, "tokens": tok}})
                for p in list(unresolved):
                    s = proposal.get(p)
                    if s in cand and s not in used:      # validator rule: source must be a real, unused arg
                        new_map[p] = (s, None)
                        used.add(s)
                        unresolved.remove(p)
                        notes.append(f"{s} -> {p} (LLM)")
                        if policy.llm_needs_approval:
                            needs_approval.append(f"{s} -> {p}: proposed by the LLM")
            else:
                notes.append("LLM tier is off, so ambiguous fields are not guessed")
        out.tier = tier
        for reasons in field_reasons.values():
            needs_approval.extend(reasons)

        if unresolved:
            return self._stop(out, "cannot fill required field(s) " + ", ".join(unresolved) +
                              " from the agent's data - asking the user instead of guessing")

        # 3c. lossless type conversions only
        for p, (s, _) in list(new_map.items()):
            have, want = py_type(args[s]), leaves[p]
            if have != want:
                try:
                    convert(args[s], want)
                except ValueError as e:
                    return self._stop(out, f"cannot convert {s}={args[s]!r} to {want} for {p}: {e}")
                new_map[p] = (s, want)
                notes.append(f"{s}: {have} -> {want}")
                if not policy.auto_type_conversion:
                    needs_approval.append(f"{s}: {have} -> {want} conversion (tool policy)")

        # 3d. headers from approved config only, never guessed
        header_map = dict(td.header_map)
        for h in missing_headers:
            if h in self.config:
                header_map[h] = h
                notes.append(f"header {h} from approved config")
                if not policy.auto_headers_from_config:
                    needs_approval.append(f"header {h} (tool policy)")
            else:
                return self._stop(out, f"API now needs header '{h}' and it is not in approved config")

        if not policy.auto_repair:
            needs_approval.append(f"tool policy: every repair of {td.name} needs approval")

        candidate = ToolDef(td.name, td.path, td.version + 1, new_map, header_map,
                            status="canary", origin="auto", owner=agent_id)
        out.trace.append({"step": "Proposed repair", "ok": True,
                          "detail": {"tier": tier, "mapping": notes, "tool": candidate.describe()}})

        # 4. validator: schema check + provenance check, before anything is sent
        body, headers = candidate.build(args, self.config)
        problems = []
        if body_schema is not None:
            problems += [e.message for e in Draft202012Validator(body_schema).iter_errors(body)]
        allowed = set(map(repr, args.values()))
        for s, c in new_map.values():
            if c:
                allowed.add(repr(convert(args[s], c)))
        for leaf in _leaves(body):
            if repr(leaf) not in allowed:
                problems.append(f"value {leaf!r} does not come from the agent's data")
        for h in req_headers:
            if h not in headers:
                problems.append(f"header {h} missing")
        out.trace.append({"step": "Validator decision", "ok": not problems,
                          "detail": "ACCEPT: fits the new schema, every value comes from known data or config"
                          if not problems else {"REJECT": problems}})
        if problems:
            return self._stop(out, "validator rejected the repair: " + "; ".join(problems))

        # 5. policy gate: ambiguous or risky repairs wait for a human instead of being sent
        if needs_approval:
            prop = self.registry.propose(td.name, self.registry.current(td.name).version, candidate,
                                         sorted(set(needs_approval)))
            out.awaiting = prop
            out.stop_reason = "held for human approval: " + "; ".join(prop.reasons)
            out.trace.append({"step": "Held for approval", "ok": False,
                              "detail": {"proposal": prop.id, "why": prop.reasons,
                                         "what": "nothing was sent; a reviewer can approve or reject this repair"}})
            return None
        return candidate

    def _log(self, case_id: str, tool: str, td: ToolDef, out: Outcome):
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "case": case_id, "tool": tool,
                 "version": f"v{td.version}", "status": td.status, "tier": out.tier,
                 "patch": td.describe(), "tokens": out.tokens}
        out.trace.append({"step": "Patched tool + audit log", "ok": True, "detail": entry})
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")


def _leaves(d):
    if isinstance(d, dict):
        for v in d.values():
            yield from _leaves(v)
    else:
        yield d


# ---------------------------------------------------------------- baseline
class NaiveRetryAgent:
    """Baseline: on an error it retries the same request (what a retry loop without
    schema repair does) until it runs out of iterations."""

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations

    def call(self, client, td: ToolDef, args: dict) -> Outcome:
        out = Outcome(False, None, None)
        body, _ = td.build(args, {})
        for i in range(self.max_iterations):
            r = client.post(td.path, json=copy.deepcopy(body))
            out.calls += 1
            out.sent.append({"body": body, "headers": {}, "status": r.status_code})
            out.trace.append({"step": f"Attempt {i + 1}", "ok": r.status_code < 400,
                              "detail": {"sent": body, "status": r.status_code, "reply": r.json()}})
            if r.status_code < 400:
                out.success, out.status, out.response = True, r.status_code, r.json()
                return out
        out.status, out.response = r.status_code, r.json()
        out.trace.append({"step": "Gave up", "ok": False,
                          "detail": f"out of iterations after {self.max_iterations} identical requests"})
        return out
