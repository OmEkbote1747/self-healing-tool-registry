"""Chaos Lab: reproducible, SIMULATED schema-drift scenarios.

Each scenario installs a simulated API (sim://<name>), registers a tool against its v1 schema, applies a
mutation (v2), then calls the tool - with self-healing (ADAPTIX) or without (baseline). Every incident it
creates is marked simulated=True with the scenario id. Nothing here is a real production incident.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .mock_api import make_app
from .models import ToolSpec

DOC = lambda desc, old=None, t="str": {"type": t, "description": desc, **({"renamed_from": old} if old else {})}


@dataclass
class Scenario:
    id: str
    title: str
    group: str
    description: str
    v1: dict
    v2: dict
    args: dict
    expect: str                         # recovered | stopped | rolled_back
    expect_category: str
    method: str = "POST"
    path: str = "/v1/resource"
    v1_headers: list = field(default_factory=list)
    v2_headers: list = field(default_factory=list)
    v2_response: dict | None = None
    v2_faults: dict | None = None
    v2_method: str | None = None
    v2_content_type: str = "application/json"
    auth: str | None = None            # None | "ok" | "expired_primary" | "invalid" | "forbidden"
    needs_llm: bool = False

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "group": self.group, "description": self.description,
                "expect": self.expect, "expect_category": self.expect_category, "needs_llm": self.needs_llm,
                "v1": self.v1, "v2": self.v2, "args": self.args, "method": self.method,
                "v2_headers": self.v2_headers, "v2_response": self.v2_response, "v2_faults": self.v2_faults,
                "v2_method": self.v2_method, "auth": self.auth}


U = {"user_id": "str", "email": "str"}
UA = {"user_id": "7", "email": "asha@example.com"}

SCENARIOS: list[Scenario] = [
    Scenario("rename-param", "Renamed parameter", "Request drift", "user_id is renamed to userId.",
             U, {"userId": "str", "email": "str"}, UA, "recovered", "schema_drift"),
    Scenario("documented-nested-rename", "Documented rename into a nested object + new header", "Request drift",
             "user_id becomes target.id (an integer); the schema documents the rename and now requires an API key.",
             {"user_id": "str"}, {"target.id": DOC("ID of the user to update (formerly user_id)", "user_id", "int")},
             {"user_id": "123"}, "recovered", "schema_drift", v2_headers=["x-api-key"]),
    Scenario("safe-type-change", "Safe type change", "Request drift", 'quantity changes from string to integer ("3" -> 3).',
             {"sku": "str", "quantity": "str"}, {"sku": "str", "quantity": "int"}, {"sku": "S1", "quantity": "3"},
             "recovered", "schema_drift"),
    Scenario("unsafe-type-change", "Incompatible type change", "Request drift",
             'quantity becomes an integer but the value is "3.5" - converting would lose data.',
             {"sku": "str", "quantity": "str"}, {"sku": "str", "quantity": "int"}, {"sku": "S1", "quantity": "3.5"},
             "stopped", "schema_drift"),
    Scenario("nested-move", "Fields move into a nested object", "Request drift", "name and email move under customer.",
             {"name": "str", "email": "str"}, {"customer.name": "str", "customer.email": "str"},
             {"name": "Asha", "email": "asha@example.com"}, "recovered", "schema_drift"),
    Scenario("response-rename", "Renamed response field", "Response drift",
             "The API renames its 'status' output to 'state' (documented). The call itself still works.",
             U, U, UA, "recovered", "schema_drift",
             v2_response={"status_field": "state", "status_value": "updated", "echo_field": "received",
                          "status_renamed_from": "status"}),
    Scenario("header-configured", "New required header (credential configured)", "Headers & auth",
             "The API starts requiring x-api-key; a credential group for it is configured.",
             U, U, UA, "recovered", "schema_drift", v2_headers=["x-api-key"]),
    Scenario("header-unconfigured", "New required header (no credential)", "Headers & auth",
             "The API starts requiring x-tenant-id; nothing is configured for it, so ADAPTIX must not guess.",
             U, U, UA, "stopped", "schema_drift", v2_headers=["x-tenant-id"]),
    Scenario("new-required-field", "New required field with no source", "Request drift",
             "A required 'reason' field is added; the caller never supplied one.",
             U, {**U, "reason": "str"}, UA, "stopped", "schema_drift"),
    Scenario("ambiguous-rename", "Ambiguous rename", "Request drift",
             "sender_id and receiver_id become 'from' and 'to' - which is which cannot be proven.",
             {"sender_id": "str", "receiver_id": "str"}, {"from": "str", "to": "str"},
             {"sender_id": "u1", "receiver_id": "u2"}, "stopped", "schema_drift"),
    Scenario("unit-change", "Hidden unit change", "Request drift",
             "amount (rupees) becomes amount_cents; moving the value unchanged would be wrong.",
             {"payment_id": "str", "amount": "str"}, {"payment_id": "str", "amount_cents": "int"},
             {"payment_id": "p1", "amount": "20"}, "stopped", "schema_drift"),
    Scenario("invalid-input", "Invalid user input (no drift)", "Not schema drift",
             "The schema is unchanged; the caller sends a role the API does not allow.",
             {"user_id": "str", "role": "enum[admin|member]"}, {"user_id": "str", "role": "enum[admin|member]"},
             {"user_id": "7", "role": "guest"}, "stopped", "invalid_input"),
    Scenario("business-rule", "Business-rule rejection", "Not schema drift",
             "The request is valid but the API refuses it (insufficient balance).",
             {"payment_id": "str", "amount": "float"}, {"payment_id": "str", "amount": "float"},
             {"payment_id": "p1", "amount": 10.0}, "stopped", "business_rule",
             v2_faults={"business_rule": "insufficient_balance"}),
    Scenario("credential-failover", "Expired key with authorized failover", "Headers & auth",
             "The primary simulated key expires; a second key in the same group is configured for failover.",
             U, U, UA, "recovered", "credential_expired", auth="expired_primary"),
    Scenario("credential-invalid", "Invalid key", "Headers & auth",
             "Every configured simulated key is rejected as invalid; ADAPTIX must not alter keys or 'repair' auth.",
             U, U, UA, "stopped", "credential_invalid", auth="invalid"),
    Scenario("rate-limit", "Rate limit with Retry-After", "Transient",
             "The API answers 429 with Retry-After once, then succeeds.",
             U, U, UA, "recovered", "rate_limited", v2_faults={"fail_times": 1, "retry_after": 0.2}),
    Scenario("verification-rollback", "Repair accepted but task not applied", "Verification",
             "After a rename, the API returns 200 but reports 'no_change'; the repair must be rolled back.",
             U, {"userId": "str", "email": "str"}, UA, "rolled_back", "schema_drift",
             v2_faults={"status_value": "no_change"}),
    Scenario("method-change", "HTTP method changed", "Not repairable",
             "The endpoint moves from POST to PUT; changing a method can alter side effects.",
             U, U, UA, "stopped", "method_changed", v2_method="PUT"),
    Scenario("window-rename-llm", "Prose-documented rename (needs the LLM tier)", "LLM-assisted",
             "start_date/end_date become window_start/window_end, documented only in prose. Rules cannot prove "
             "the mapping; an LLM can, if it quotes the schema text. Without an LLM it stops safely.",
             {"start_date": "str", "end_date": "str"},
             {"window_start": DOC("First day of the booking window (inclusive)"),
              "window_end": DOC("Last day of the booking window (inclusive)")},
             {"start_date": "2026-01-01", "end_date": "2026-01-31"}, "recovered", "schema_drift", needs_llm=True),
]
BY_ID = {s.id: s for s in SCENARIOS}


def install(engine, s: Scenario, run_id: str, mode: str) -> str:
    """Install the v1 simulated API, register the tool against it, then mutate the API to v2."""
    name = f"{s.id}-{mode}-{run_id}"
    group = None
    if "x-api-key" in s.v2_headers + s.v1_headers:
        group = f"sim-{s.id}"
        engine.creds.add_simulated_group(group, "x-api-key", [f"{group}-a", f"{group}-b"])
    engine.upstreams.set_simulated(name, make_app(s.path, s.v1, s.v1_headers, title=s.title, method=s.method))
    spec = ToolSpec(name=name, base_url=f"sim://{name}", path=s.path, method=s.method, description=s.description,
                    auth_header="x-api-key" if group else None, credential_group=group, simulated=True)
    engine.register(spec)
    engine.upstreams.set_simulated(name, make_app(
        s.path, s.v2, s.v2_headers, title=s.title, method=s.v2_method or s.method, content_type=s.v2_content_type,
        response=s.v2_response, faults=dict(s.v2_faults or {})))
    return name


def run(engine, scenario_id: str, seed: int = 7, heal: bool = True) -> dict:
    """Run one scenario. Deterministic for a given seed (ids are derived from it)."""
    s = BY_ID[scenario_id]
    rnd = random.Random(f"{scenario_id}:{seed}:{heal}")
    run_id = f"{seed}-{rnd.randrange(16 ** 6):06x}"
    mode = "adaptix" if heal else "baseline"
    if s.auth:
        return _run_auth(engine, s, run_id, mode, heal)
    name = install(engine, s, run_id, mode)
    res = engine.execute(name, dict(s.args), scenario=s.id, heal=heal)
    return _summarize(s, mode, res, engine.upstreams.sim_app(name), expected_outcome(s, engine, heal))


def _run_auth(engine, s: Scenario, run_id: str, mode: str, heal: bool) -> dict:
    """Auth scenarios: the API requires x-api-key from v1 on; v2 changes the key's status, not the schema."""
    name = f"{s.id}-{mode}-{run_id}"
    group = f"sim-{s.id}"
    keys = engine.creds.add_simulated_group(group, "x-api-key", [f"{group}-a", f"{group}-b"])
    a, b = keys[f"{group}-a"], keys[f"{group}-b"]
    engine.upstreams.set_simulated(name, make_app(s.path, s.v1, title=s.title, auth={"header": "x-api-key", "valid": [a, b]}))
    spec = ToolSpec(name=name, base_url=f"sim://{name}", path=s.path, description=s.description,
                    auth_header="x-api-key", credential_group=group, simulated=True)
    engine.register(spec)
    for c in engine.creds._creds:                         # fresh state for this run's simulated keys
        if c.group == group:
            c.state, c.cooldown_until = "unverified", 0.0
    if not heal:                                          # baseline: no failover
        for c in engine.creds._creds:
            if c.group == group:
                c.failover_on = []
    auth = {"header": "x-api-key", "valid": [b], "expired": [a]} if s.auth == "expired_primary" else \
        {"header": "x-api-key", "valid": []}
    engine.upstreams.set_simulated(name, make_app(s.path, s.v2, title=s.title, auth=auth))
    res = engine.execute(name, dict(s.args), scenario=s.id, heal=heal)
    for c in engine.creds._creds:
        if c.group == group:
            c.failover_on = ["rate_limited", "credential_expired"]
    return _summarize(s, mode, res, engine.upstreams.sim_app(name), expected_outcome(s, engine, heal))


def expected_outcome(s: Scenario, engine, heal: bool = True) -> str:
    """What a correct run should end with. Baseline (no self-healing) only succeeds when nothing changed."""
    if not heal:
        return "stopped"
    if s.needs_llm and not getattr(engine.llm, "real", False):
        return "stopped"                    # without a real model the safe outcome is to stop
    return s.expect


def _summarize(s: Scenario, mode: str, res: dict, app, expected: str) -> dict:
    outcome = "recovered" if res["status"] in ("ok", "recovered") else res["status"]
    return {"scenario": s.id, "title": s.title, "mode": mode, "simulated": True, "expected": expected,
            "outcome": outcome, "matches_expectation": outcome == expected, "category": res.get("category"),
            "incident": res.get("incident"), "reason": res.get("reason"), "duration_ms": res.get("duration_ms"),
            "tool_version": res.get("tool_version"), "upstream_calls": getattr(app.state, "calls", None),
            "side_effects": getattr(app.state, "effects", None)}
