"""SchemaHeal middleware API: agents call their tools through this service instead of the API directly.

Run:   SCHEMAHEAL_API_KEYS="acme:agent:KEY1,acme:reviewer:KEY2" uvicorn selfheal.service:app
Docs:  /docs (OpenAPI)

Auth:  "Authorization: Bearer <key>". Each key belongs to one tenant and one role:
         agent    - register tools, call tools, export OpenAI tool definitions
         reviewer - everything an agent can do, plus approve/reject repairs, roll back, set policies
       Every request only ever sees its own tenant's data.

First integration target: OpenAI-compatible function calling.
  GET  /v1/openai/tools        -> the "tools" array to pass to a chat-completions request
  POST /v1/openai/tool_calls   -> execute the model's tool_calls, get back role="tool" messages
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .cases import APPROVED_CONFIG
from .healer import ConflictError, SelfHealer
from .mock_api import parse_type
from .policy import PolicySet, RepairPolicy
from .security import redact
from .store import SqliteRegistry
from .transport import Transport

ROLES = {"agent": 0, "reviewer": 1}
PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass
class Principal:
    tenant: str
    role: str


def parse_keys(spec: str) -> dict[str, Principal]:
    """"tenant:role:key,tenant:role:key" -> {key: Principal}"""
    keys = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        tenant, role, key = item.split(":", 2)
        if role not in ROLES:
            raise ValueError(f"unknown role {role}")
        keys[key] = Principal(tenant, role)
    return keys


# ---------------------------------------------------------------- request / response models
class ToolIn(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    base_url: str
    path: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    arg_schema: dict[str, str] = Field(description='field -> type, e.g. {"user_id": "str", "q:limit": "int?"}')
    description: str = ""


class CallIn(BaseModel):
    arguments: dict[str, Any]
    agent_id: str = "agent"


class CallOut(BaseModel):
    status: Literal["ok", "awaiting_approval", "stopped"]
    http_status: int | None = None
    result: Any = None
    tool_version: int | None = None
    proposal_id: str | None = None
    reason: str = ""
    repaired: bool = False
    trace: list[dict] = []


class OpenAIToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: dict


class PolicyIn(BaseModel):
    auto_repair: bool | None = None
    auto_min_similarity: float | None = None
    approval_min_similarity: float | None = None
    llm_needs_approval: bool | None = None
    auto_type_conversion: bool | None = None
    auto_headers_from_config: bool | None = None


# ---------------------------------------------------------------- helpers
def openai_tool(t: dict) -> dict:
    """Describe a registered tool in OpenAI function-calling format (from its original argument schema)."""
    props, required = {}, []
    for key, spec in t["arg_schema"].items():
        name = key[2:] if key.startswith(("q:", "p:")) else key
        typ, req = parse_type(spec)
        base = spec.rstrip("?")
        if base.startswith("list["):
            js = {"type": "array", "items": {"type": PY_TO_JSON.get(typ.__args__[0], "string")}}
        elif base.startswith("enum["):
            js = {"type": "string", "enum": base[5:-1].split("|")}
        elif base == "date":
            js = {"type": "string", "format": "date"}
        else:
            js = {"type": PY_TO_JSON.get(typ, "string")}
        props[name] = js
        if req:
            required.append(name)
    return {"type": "function", "function": {
        "name": t["name"], "description": t["description"] or f"{t['method']} {t['path']}",
        "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}}


def create_app(db_path: str | None = None, api_keys: dict[str, Principal] | None = None,
               transport_factory: Callable[[str, str], Transport] | None = None, use_llm: bool = False) -> FastAPI:
    db_path = db_path or os.environ.get("SCHEMAHEAL_DB", "data/schemaheal.db")
    keys = api_keys if api_keys is not None else parse_keys(os.environ.get("SCHEMAHEAL_API_KEYS", ""))
    transports: dict[tuple[str, str], Transport] = {}

    def transport_for(tenant: str, base_url: str) -> Transport:
        # one transport per tenant and upstream, so rate limits and circuit breakers are shared
        k = (tenant, base_url)
        if k not in transports:
            transports[k] = transport_factory(tenant, base_url) if transport_factory else Transport(base_url=base_url)
        return transports[k]

    app = FastAPI(title="SchemaHeal middleware API", version="0.2.0",
                  description="Guarded OpenAPI request adapter for AI agents: repairs safe schema drift, holds "
                              "ambiguous repairs for human approval, never invents values.")

    def principal(authorization: str = Header(default="")) -> Principal:
        token = authorization.removeprefix("Bearer ").strip()
        if token not in keys:
            raise HTTPException(401, "missing or invalid API key")
        return keys[token]

    def reviewer(p: Principal = Depends(principal)) -> Principal:
        if ROLES[p.role] < ROLES["reviewer"]:
            raise HTTPException(403, "reviewer role required")
        return p

    def registry(p: Principal) -> SqliteRegistry:
        return SqliteRegistry(db_path, tenant=p.tenant)

    def tool_or_404(reg: SqliteRegistry, name: str) -> dict:
        t = reg.tool(name)
        if t is None:
            raise HTTPException(404, f"unknown tool {name}")
        return t

    def run_call(p: Principal, name: str, arguments: dict, agent_id: str) -> CallOut:
        reg = registry(p)
        t = tool_or_404(reg, name)
        policies = PolicySet(RepairPolicy(), reg.policies())
        healer = SelfHealer(reg, APPROVED_CONFIG, use_llm=use_llm, policies=policies)
        out = healer.call(transport_for(p.tenant, t["base_url"]), name, arguments, agent_id=f"{p.tenant}/{agent_id}")
        status = {"repaired": "ok", "awaiting_approval": "awaiting_approval", "stopped": "stopped"}[out.state]
        return CallOut(status=status, http_status=out.status, result=out.response if out.success else None,
                       tool_version=reg.current(name, f"{p.tenant}/{agent_id}").version,
                       proposal_id=out.awaiting.id if out.awaiting else None, reason=out.stop_reason,
                       repaired=out.attempts > 0 and out.success, trace=redact(out.trace))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/v1/tools", status_code=201)
    def register_tool(body: ToolIn, p: Principal = Depends(principal)):
        for spec in body.arg_schema.values():
            try:
                parse_type(spec)
            except Exception:
                raise HTTPException(422, f"bad type spec {spec!r}")
        registry(p).register(body.name, body.path, body.arg_schema, body.method, body.base_url, body.description)
        return {"name": body.name, "version": 1}

    @app.get("/v1/tools")
    def list_tools(p: Principal = Depends(principal)):
        reg = registry(p)
        return [{**t, "current_version": reg.current(t["name"]).version} for t in reg.tools()]

    @app.get("/v1/tools/{name}/versions")
    def versions(name: str, p: Principal = Depends(principal)):
        reg = registry(p)
        tool_or_404(reg, name)
        return [v.describe() for v in reg.versions[name]]

    @app.post("/v1/tools/{name}/call", response_model=CallOut)
    def call_tool(name: str, body: CallIn, p: Principal = Depends(principal)):
        return run_call(p, name, body.arguments, body.agent_id)

    @app.post("/v1/tools/{name}/rollback")
    def rollback(name: str, p: Principal = Depends(reviewer)):
        reg = registry(p)
        tool_or_404(reg, name)
        return reg.rollback(name).describe()

    @app.get("/v1/repairs")
    def repairs(status: str = "pending", p: Principal = Depends(reviewer)):
        reg = registry(p)
        items = reg.pending() if status == "pending" else list(reg.proposals.values())
        return [{"id": x.id, "tool": x.tool, "base_version": x.base_version, "status": x.status,
                 "reasons": x.reasons, "repair": x.candidate.describe()} for x in items]

    @app.post("/v1/repairs/{proposal_id}/approve")
    def approve(proposal_id: str, p: Principal = Depends(reviewer)):
        try:
            td = registry(p).approve(proposal_id, by=f"{p.tenant}/reviewer")
        except KeyError:
            raise HTTPException(404, "unknown proposal")
        except ConflictError as e:
            raise HTTPException(409, str(e))
        return td.describe()

    @app.post("/v1/repairs/{proposal_id}/reject")
    def reject(proposal_id: str, p: Principal = Depends(reviewer)):
        registry(p).reject(proposal_id, by=f"{p.tenant}/reviewer")
        return {"id": proposal_id, "status": "rejected"}

    @app.get("/v1/policies")
    def get_policies(p: Principal = Depends(principal)):
        return registry(p).policies()

    @app.put("/v1/policies/{tool}")
    def set_policy(tool: str, body: PolicyIn, p: Principal = Depends(reviewer)):
        overrides = body.model_dump(exclude_none=True)
        registry(p).set_policy(tool, overrides)
        return {"tool": tool, "overrides": overrides}

    @app.get("/v1/events")
    def events(limit: int = 50, p: Principal = Depends(reviewer)):
        return registry(p).events(min(limit, 500))

    # ---------------- OpenAI-compatible adapter
    @app.get("/v1/openai/tools")
    def openai_tools(p: Principal = Depends(principal)):
        return [openai_tool(t) for t in registry(p).tools()]

    @app.post("/v1/openai/tool_calls")
    def openai_tool_calls(calls: list[OpenAIToolCall], agent_id: str = "agent", p: Principal = Depends(principal)):
        messages = []
        for tc in calls:
            try:
                args = json.loads(tc.function.get("arguments") or "{}")
            except json.JSONDecodeError:
                content = {"status": "stopped", "reason": "arguments were not valid JSON"}
            else:
                try:
                    content = run_call(p, tc.function.get("name", ""), args, agent_id).model_dump(exclude={"trace"})
                except HTTPException as e:            # one bad call must not fail the whole batch
                    content = {"status": "stopped", "reason": str(e.detail)}
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(content)})
        return messages

    return app


app = create_app()
