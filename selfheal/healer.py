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

from jsonschema import Draft202012Validator, FormatChecker
from rapidfuzz import fuzz

from . import llm
from .policy import PolicySet
from .schema import FieldSpec, Operation, read_operation
from .security import redact, redact_headers, redact_reply
from .transport import NOT_SCHEMA, REPAIRABLE, Transport, TransportError

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
    method: str = "POST"
    param_map: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # "query:x"/"path:x" -> (arg, conv)

    def build(self, args: dict, config: dict) -> tuple[dict, dict]:
        body: dict = {}
        for target, (src, conv) in self.body_map.items():
            value = args[src] if conv is None else convert(args[src], conv)
            _set(body, target, value)
        headers = {h: config[k] for h, k in self.header_map.items()}
        return body, headers

    def build_request(self, args: dict, config: dict) -> dict:
        body, headers = self.build(args, config)
        query, path_params = {}, {}
        for loc, (src, conv) in self.param_map.items():
            where, name = loc.split(":", 1)
            value = args[src] if conv is None else convert(args[src], conv)
            (query if where == "query" else path_params)[name] = value
        url = self.path.format(**{k: str(v) for k, v in path_params.items()}) if path_params else self.path
        return {"method": self.method, "url": url, "query": query, "path_params": path_params,
                "body": body if self.body_map else None, "headers": headers}

    def describe(self) -> dict:
        d = {"version": self.version, "status": self.status, "origin": self.origin, "method": self.method,
             "body": {t: (s if c is None else f"{s} as {c}") for t, (s, c) in self.body_map.items()},
             "headers": {h: f"config[{k}]" for h, k in self.header_map.items()}}
        if self.param_map:
            d["params"] = {t: (s if c is None else f"{s} as {c}") for t, (s, c) in self.param_map.items()}
        return d


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

    def register(self, name: str, path: str, schema: dict[str, str], method: str = "POST"):
        self.versions[name] = [initial_tooldef(name, path, schema, method)]

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
    if to == "date":
        import datetime as _dt
        try:
            return _dt.date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise ValueError(f"{value!r} is not an ISO date (YYYY-MM-DD)") from None
    if to == "list":
        if isinstance(value, list):
            return value
        raise ValueError(f"{value!r} is not a list")
    raise ValueError(f"unknown type {to}")


def py_type(v) -> str:
    return {bool: "bool", int: "int", float: "float", list: "list", dict: "object"}.get(type(v), "str")


def initial_tooldef(name: str, path: str, schema: dict[str, str], method: str = "POST") -> ToolDef:
    """Version 1 of a tool: every argument goes where the original schema put it."""
    body = {k: (k, None) for k in schema if not k.startswith(("q:", "p:"))}
    params = {("query:" if k.startswith("q:") else "path:") + k[2:]: (k[2:], None)
              for k in schema if k.startswith(("q:", "p:"))}
    return ToolDef(name, path, 1, body, method=method.upper(), param_map=params)


def tokens(name: str) -> str:
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return " ".join(t for t in re.split(r"[._\-\s]+", name.lower()) if t)


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return max(fuzz.token_set_ratio(ta, tb), fuzz.ratio(ta.replace(" ", ""), tb.replace(" ", "")))


# ---------------------------------------------------------------- schema reading
def schema_from_errors(td: ToolDef, args: dict, errors: list[dict]) -> Operation:
    """Fallback when /openapi.json is not available: infer the new shape from 422 error locations."""
    fields = {"body:" + t: FieldSpec([py_type(convert(args[s], c) if c else args[s])], True)
              for t, (s, c) in td.body_map.items()}
    fields.update({loc: FieldSpec([py_type(args[s])], True) for loc, (s, _) in td.param_map.items()})
    headers = []
    for e in errors:
        loc = e.get("loc", [])
        if loc and loc[0] == "header":
            headers.append(loc[1])
            continue
        where = loc[0] if loc and loc[0] in ("query", "path") else "body"
        key = f"{where}:" + ".".join(str(x) for x in loc[1:])
        etype = e.get("type", "")
        m = re.match(r"(int|float|string|bool)", etype)
        if etype == "missing":
            fields.setdefault(key, FieldSpec(["str"], True))
        elif m:
            fields[key] = FieldSpec([{"string": "str"}.get(m.group(1), m.group(1))], True)
    return Operation(td.method, td.path, fields, headers, None, ["application/json"])


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
        self._transports: dict[int, Transport] = {}

    def transport(self, client) -> Transport:
        if isinstance(client, Transport):
            return client
        return self._transports.setdefault(id(client), Transport(client=client))

    # -- public
    def call(self, client, tool: str, args: dict, case_id: str = "", agent_id: str = "agent-A") -> Outcome:
        out = Outcome(False, None, None)
        client = self.transport(client)
        key = Transport.new_idempotency_key()           # one key per logical call, reused on its retry
        td = self.registry.current(tool, agent_id)
        try:
            status, resp = self._send(client, td, args, out, label=f"call with tool v{td.version} ({td.status})",
                                      key=key)
        except TransportError as e:
            return self._transport_stop(out, e)
        if status < 400:
            self.registry.record_success(td)
            out.success, out.status, out.response = True, status, resp
            return out
        if status not in REPAIRABLE:          # not a schema problem: never repaired or replayed
            out.status, out.response = status, resp
            out.stopped = True
            out.stop_reason = NOT_SCHEMA.get(status, f"API error {status} - outcome unknown, not replayed"
                                             if status >= 500 else f"API error {status} - not a schema problem")
            out.trace.append({"step": "Stop and report", "ok": False, "detail": out.stop_reason})
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
            try:
                status, resp = self._send(client, td, args, out, label=f"retry with tool v{td.version} ({td.status})",
                                          key=key, is_retry=True)
            except TransportError as e:
                return self._transport_stop(out, e)
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
    def _transport_stop(self, out: Outcome, e: TransportError) -> Outcome:
        if e.kind in ("circuit_open", "rate_limited", "retry_budget"):
            out.calls -= 1                 # blocked locally, never left the process
        out.stopped = True
        out.stop_reason = f"{e.kind}: {e}"
        out.trace.append({"step": "Stop and report", "ok": False, "detail": out.stop_reason})
        return out

    def _send(self, client: Transport, td: ToolDef, args, out: Outcome, label: str, key: str, is_retry=False):
        req = td.build_request(args, self.config)
        body, headers = req["body"] or {}, req["headers"]
        out.calls += 1                     # counted even if the answer never arrives
        r = client.send(req["method"], req["url"], tool=td.name, json=req["body"], params=req["query"] or None,
                        headers=headers, idempotency_key=key, is_retry=is_retry)
        out.sent.append({"body": body, "headers": headers, "params": {**req["query"], **req["path_params"]},
                         "status": r.status_code})
        try:
            payload = r.json()
        except ValueError:
            payload = {"detail": r.text[:200]}
        detail = {"method": req["method"], "url": req["url"], "sent": redact(body),
                  "headers": redact_headers(headers), "status": r.status_code, "reply": redact_reply(payload)}
        if req["query"]:
            detail["query"] = redact(req["query"])
        out.trace.append({"step": f"Request: {label}", "ok": r.status_code < 400, "detail": detail})
        return r.status_code, payload

    def _stop(self, out: Outcome, reason: str):
        out.stop_reason = reason
        out.trace.append({"step": "Stop and report", "ok": False, "detail": reason})
        return None

    def _diagnose_and_validate(self, client, td: ToolDef, args: dict, errors: list[dict], out: Outcome,
                               agent_id: str = "agent-A"):
        policy = self.policies.for_tool(td.name)
        # 1. read the new schema (prefer /openapi.json, fall back to the error locations)
        op = read_operation(client, td.path, td.method)
        source = "/openapi.json"
        if isinstance(op, str):
            return self._stop(out, f"the API now expects {op} instead of {td.method} on {td.path}; "
                                   "a method change can alter side effects, so a human must decide")
        if op is None:
            op, source = schema_from_errors(td, args, errors), "422 error locations"
        if op.content_types and "application/json" not in op.content_types:
            return self._stop(out, f"request body is now {', '.join(op.content_types)}; only JSON bodies are "
                                   "repaired automatically")
        fields = op.fields

        # 2. what changed (every field is keyed by location: body:..., query:..., path:...)
        current = {"body:" + t: v for t, v in td.body_map.items()} | dict(td.param_map)
        kept = {loc: v for loc, v in current.items() if loc in fields}
        unfilled = [loc for loc, f in fields.items() if f.required and loc not in kept]
        optional = [loc for loc, f in fields.items() if not f.required and loc not in kept]
        orphans = [s for loc, (s, _) in current.items() if loc not in fields]
        missing_headers = [h for h in op.headers if h not in td.header_map]
        name = lambda loc: loc.split(":", 1)[1]
        changes = ([f"removed/renamed: {loc}" for loc in sorted(set(current) - set(fields))] +
                   [f"new required {loc} ({fields[loc].type})" for loc in unfilled] +
                   [f"type changed: {loc} is now {fields[loc].type}" for loc, (s, c) in kept.items()
                    if py_type(convert(args[s], c) if c else args[s]) not in fields[loc].types
                    and fields[loc].type not in ("enum", "date")] +
                   [f"new required header: {h}" for h in missing_headers])
        out.trace.append({"step": "Detected schema change", "ok": True,
                          "detail": {"source": source, "errors": redact_reply({"detail": errors})["detail"],
                                     "changes": changes}})

        new_map: dict[str, tuple[str, str | None]] = {loc: (s, None) for loc, (s, _) in kept.items()}

        # 3a. Tier 1 rules: match each new required field to an orphaned argument, then ask the policy
        tier = "Tier 1 (rules)"
        notes, needs_approval = [], []
        field_reasons: dict[str, list[str]] = {}   # approval reasons per mapped field
        unresolved: list[str] = []
        used: set[str] = set()
        confirm: list[str] = []   # medium-confidence matches the LLM should double-check

        def accept(loc, s, score, why):
            decision, reasons = policy.assess(s, name(loc), score)
            if decision == "block":
                unresolved.append(loc)
                return
            new_map[loc] = (s, None)
            used.add(s)
            notes.append(f"{s} -> {loc} ({why}, similarity {score:.0f})")
            if decision == "approval":
                field_reasons[loc] = reasons
                confirm.append(loc)

        if len(unfilled) == 1 and len(orphans) == 1:
            accept(unfilled[0], orphans[0], similarity(name(unfilled[0]), orphans[0]), "only candidate")
        else:
            for loc in unfilled:
                scored = sorted(((similarity(name(loc), s), s) for s in orphans if s not in used), reverse=True)
                if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 15):
                    accept(loc, scored[0][1], scored[0][0], "best match")
                else:
                    unresolved.append(loc)
        # optional fields are only filled by an exact name match; otherwise they are left out
        for loc in optional:
            exact = [s for s in orphans if s not in used and tokens(s) == tokens(name(loc).split(".")[-1])]
            if exact:
                new_map[loc] = (exact[0], None)
                used.add(exact[0])
                notes.append(f"{exact[0]} -> {loc} (optional field, exact name)")

        # 3b. Tier 2: an LLM picks among existing arguments when the rules are unsure. It sees field
        #     names and types only - never values - and its picks always need human approval.
        if confirm and self.use_llm:
            for loc in confirm:
                s = new_map.pop(loc)[0]
                used.discard(s)
                unresolved.append(loc)
                field_reasons.pop(loc, None)
        if unresolved and [s for s in orphans if s not in used]:
            if self.use_llm:
                tier = "Tier 1 + Tier 2 (LLM)"
                cand = {s: py_type(args[s]) for s in orphans if s not in used}
                proposal, tok, note = llm.propose_mapping(td.name, {name(l): fields[l].type for l in unresolved},
                                                          cand, errors)
                out.tokens += tok
                out.trace.append({"step": "LLM proposed a repair", "ok": bool(proposal),
                                  "detail": {"proposal": proposal, "note": note, "tokens": tok}})
                for loc in list(unresolved):
                    s = proposal.get(name(loc))
                    if s in cand and s not in used:      # validator rule: source must be a real, unused arg
                        new_map[loc] = (s, None)
                        used.add(s)
                        unresolved.remove(loc)
                        notes.append(f"{s} -> {loc} (LLM)")
                        if policy.llm_needs_approval:
                            needs_approval.append(f"{s} -> {name(loc)}: proposed by the LLM")
            else:
                notes.append("LLM tier is off, so ambiguous fields are not guessed")
        out.tier = tier
        for reasons in field_reasons.values():
            needs_approval.extend(reasons)

        if unresolved:
            return self._stop(out, "cannot fill required field(s) " + ", ".join(unresolved) +
                              " from the agent's data - asking the user instead of guessing")

        # 3c. compatibility of each value with its new field: lossless conversion, enum, format, list
        for loc, (s, _) in list(new_map.items()):
            f, value = fields[loc], args[s]
            if f.enum is not None:
                if str(value) not in [str(e) for e in f.enum]:
                    return self._stop(out, f"{s}={value!r} is not one of the values {loc} now allows "
                                           f"({', '.join(map(str, f.enum))}); choosing one would be a guess")
                continue
            want = f.type if f.type == "date" else (py_type(value) if py_type(value) in f.types else f.types[0])
            if want == "object":
                return self._stop(out, f"{loc} is now an object with unknown inner fields; needs a human")
            if py_type(value) == want and want != "date":
                continue
            try:
                converted = convert(value, want)
            except ValueError as e:
                return self._stop(out, f"cannot convert {s}={value!r} to {want} for {loc}: {e}")
            if converted != value:
                new_map[loc] = (s, want)
                notes.append(f"{s}: {py_type(value)} -> {want}")
                if not policy.auto_type_conversion:
                    needs_approval.append(f"{s}: {py_type(value)} -> {want} conversion (tool policy)")

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

        body_map = {loc[5:]: v for loc, v in new_map.items() if loc.startswith("body:")}
        param_map = {loc: v for loc, v in new_map.items() if not loc.startswith("body:")}
        candidate = ToolDef(td.name, td.path, td.version + 1, body_map, header_map, status="canary",
                            origin="auto", owner=agent_id, method=td.method, param_map=param_map)
        out.trace.append({"step": "Proposed repair", "ok": True,
                          "detail": {"tier": tier, "mapping": notes, "tool": candidate.describe()}})

        # 4. validator: schema + format check and provenance check, before anything is sent
        req = candidate.build_request(args, self.config)
        problems = []
        if op.body_schema is not None:
            problems += [e.message for e in Draft202012Validator(op.body_schema, format_checker=FormatChecker())
                         .iter_errors(req["body"] or {})]
        for loc, f in fields.items():
            if loc.startswith("body:") or loc not in param_map:
                continue
            where, pname = loc.split(":", 1)
            value = (req["query"] if where == "query" else req["path_params"])[pname]
            problems += [f"{loc}: {e.message}" for e in
                         Draft202012Validator(f.schema, format_checker=FormatChecker()).iter_errors(value)]
        allowed = set(map(repr, args.values()))
        for s, c in new_map.values():
            if c:
                allowed.add(repr(convert(args[s], c)))
        sent_values = list(_leaves(req["body"] or {})) + list(req["query"].values()) + list(req["path_params"].values())
        for leaf in sent_values:
            if repr(leaf) not in allowed and not any(repr(leaf) == repr(x) for x in _leaves(list(args.values()))):
                problems.append(f"value {leaf!r} does not come from the agent's data")
        for h in op.headers:
            if h not in req["headers"]:
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
                              "detail": {"sent": redact(body), "status": r.status_code, "reply": redact_reply(r.json())}})
            if r.status_code < 400:
                out.success, out.status, out.response = True, r.status_code, r.json()
                return out
        out.status, out.response = r.status_code, r.json()
        out.trace.append({"step": "Gave up", "ok": False,
                          "detail": f"out of iterations after {self.max_iterations} identical requests"})
        return out
