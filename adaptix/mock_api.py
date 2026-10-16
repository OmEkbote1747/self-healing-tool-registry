"""Simulated upstream API used by the Chaos Lab, the evaluation suites and the tests.

Everything here is SIMULATED. It behaves like a FastAPI production service (real 422 bodies, a real
/openapi.json with request and response schemas), so the repair engine is exercised end to end.

Field keys:   body "target.id" (dotted = nested), query "q:limit", path "p:user_id" ({user_id} in path)
Field specs:  "str" | "int" | "float" | "bool" | "date" | "list[str]" | "enum[a|b]"  (trailing "?" = optional)
              or {"type": "int", "description": "...", "renamed_from": "user_id"}  (documentation evidence)
Response:     {"status_field": "status", "status_value": "updated", "echo_field": "received"}
Auth:         {"header": "x-api-key", "valid": [...], "expired": [...], "revoked": [...], "rate_limited": [...]}
Faults:       {"status": 503} every call fails · {"delay": 1.0} slow answer after doing the work ·
              {"business_rule": "insufficient_balance"} rejects with a business error ·
              {"retry_after": 1, "fail_times": 1} 429 with Retry-After for the first N calls ·
              {"status_value": "no_change"} accepts the call but reports the task was not applied
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, create_model

SCALARS = {"str": str, "int": int, "float": float, "bool": bool, "date": _dt.date}


def _norm(spec) -> dict:
    return spec if isinstance(spec, dict) else {"type": spec}


def parse_type(spec):
    """Return (python type, required) for a type spec (string or dict)."""
    spec = _norm(spec)["type"]
    required = not spec.endswith("?")
    spec = spec.rstrip("?")
    if spec.startswith("list[") and spec.endswith("]"):
        typ: Any = list[SCALARS[spec[5:-1]]]
    elif spec.startswith("enum[") and spec.endswith("]"):
        typ = Literal[tuple(spec[5:-1].split("|"))]
    else:
        typ = SCALARS[spec]
    return typ, required


def _field(spec):
    spec = _norm(spec)
    typ, required = parse_type(spec)
    extra = {"x-renamed-from": spec["renamed_from"]} if spec.get("renamed_from") else None
    info = Field(... if required else None, description=spec.get("description"), json_schema_extra=extra)
    return (typ if required else Optional[typ], info)


def _build_model(name: str, fields: dict, strict: bool = True) -> type[BaseModel]:
    flat: dict[str, Any] = {}
    nested: dict[str, dict] = {}
    for path, spec in fields.items():
        head, _, rest = path.partition(".")
        if rest:
            nested.setdefault(head, {})[rest] = spec
        else:
            flat[head] = _field(spec)
    for head, sub in nested.items():
        flat[head] = (_build_model(f"{name}_{head.title()}", sub, strict), ...)
    return create_model(name, __config__={"strict": strict}, **flat)


DEFAULT_RESPONSE = {"status_field": "status", "status_value": "updated", "echo_field": "received"}


def make_app(path: str, fields: dict, headers: list[str] | None = None, title: str = "Simulated API",
             faults: dict | None = None, method: str = "POST", content_type: str = "application/json",
             response: dict | None = None, auth: dict | None = None) -> FastAPI:
    headers = [h.lower() for h in (headers or [])]
    method = method.upper()
    resp_cfg = {**DEFAULT_RESPONSE, **(response or {})}
    faults = dict(faults or {})
    body_fields = {k: v for k, v in fields.items() if not k.startswith(("q:", "p:"))}
    query_fields = {k[2:]: v for k, v in fields.items() if k.startswith("q:")}
    path_fields = {k[2:]: v for k, v in fields.items() if k.startswith("p:")}
    base = path.strip("/").replace("/", "_").replace("{", "").replace("}", "") or "root"
    body_model = _build_model("Body_" + base, body_fields) if body_fields else None
    query_model = _build_model("Query_" + base, query_fields, strict=False) if query_fields else None
    path_model = _build_model("Path_" + base, path_fields, strict=False) if path_fields else None
    def _doc(key, text):
        old = resp_cfg.get(f"{key}_renamed_from")
        return Field(..., description=text + (f" (formerly {old})" if old else ""),
                     json_schema_extra={"x-renamed-from": old} if old else None)
    response_model = create_model("Response_" + base, **{
        resp_cfg["status_field"]: (str, _doc("status", "Outcome of the operation")),
        resp_cfg["echo_field"]: (dict, _doc("echo", "The data the API applied"))})

    app = FastAPI(title=title + " (simulated)")
    app.state.effects = 0                 # how many times the side effect really happened
    app.state.calls = 0
    app.state.idempotency = {}            # Idempotency-Key -> cached response
    auth_header = (auth or {}).get("header", "").lower()

    def _param_schema(model, name):
        return model.model_json_schema()["properties"][name]

    params = [{"name": h, "in": "header", "required": True, "schema": {"type": "string"}} for h in headers]
    if auth_header and auth_header not in headers:
        params.append({"name": auth_header, "in": "header", "required": True, "schema": {"type": "string"},
                       "description": "API key"})
    for name, spec in query_fields.items():
        params.append({"name": name, "in": "query", "required": parse_type(spec)[1],
                       "schema": _param_schema(query_model, name)})
    for name in path_fields:
        params.append({"name": name, "in": "path", "required": True, "schema": _param_schema(path_model, name)})
    extra: dict[str, Any] = {"parameters": params, "responses": {"200": {"description": "OK", "content": {
        "application/json": {"schema": {"$ref": f"#/components/schemas/{response_model.__name__}"}}}}}}
    if body_model is not None:
        extra["requestBody"] = {"required": True, "content": {
            content_type: {"schema": {"$ref": f"#/components/schemas/{body_model.__name__}"}}}}

    async def endpoint(request: Request):
        app.state.calls += 1
        if faults.get("status"):
            return JSONResponse(status_code=faults["status"], content={"detail": "injected failure"})
        if faults.get("fail_times", 0) > 0:
            faults["fail_times"] -= 1
            return JSONResponse(status_code=429, content={"detail": "rate limited"},
                                headers={"Retry-After": str(faults.get("retry_after", 1))})
        if auth:
            key = request.headers.get(auth_header)
            if not key:
                return JSONResponse(status_code=401, content={"detail": "missing API key"})
            if key in auth.get("expired", []):
                return JSONResponse(status_code=401, content={"detail": "API key expired"})
            if key in auth.get("revoked", []):
                return JSONResponse(status_code=401, content={"detail": "API key revoked"})
            if key in auth.get("rate_limited", []):
                return JSONResponse(status_code=429, content={"detail": "quota exceeded for this key"},
                                    headers={"Retry-After": "1"})
            if key in auth.get("forbidden", []):
                return JSONResponse(status_code=403, content={"detail": "key lacks permission for this operation"})
            if key not in auth.get("valid", []):
                return JSONResponse(status_code=401, content={"detail": "invalid API key"})
        ikey = request.headers.get("idempotency-key")
        if ikey and ikey in app.state.idempotency:          # replay of a request we already processed
            return app.state.idempotency[ikey]
        errors = []
        for h in headers:
            if not request.headers.get(h):
                errors.append({"type": "missing", "loc": ["header", h], "msg": "Field required", "input": None})
        received: dict[str, Any] = {}
        for where, model, data in (("query", query_model, dict(request.query_params)),
                                   ("path", path_model, dict(request.path_params))):
            if model is None:
                continue
            try:
                received[where] = model.model_validate(data).model_dump(mode="json")
            except ValidationError as e:
                errors += [{"type": err["type"], "loc": [where, *err["loc"]], "msg": err["msg"],
                            "input": err.get("input")} for err in e.errors(include_url=False)]
        if body_model is not None:
            if content_type != "application/json":
                return JSONResponse(status_code=415, content={"detail": f"expected {content_type}"})
            raw = await request.body()
            try:
                received["body"] = body_model.model_validate_json(raw or b"null").model_dump(mode="json")
            except ValidationError as e:
                errors += [{"type": err["type"], "loc": ["body", *err["loc"]], "msg": err["msg"],
                            "input": err.get("input")} for err in e.errors(include_url=False)]
        if errors:
            return JSONResponse(status_code=422, content={"detail": errors})
        if faults.get("business_rule"):
            return JSONResponse(status_code=422, content={"detail": {"code": faults["business_rule"],
                                                                     "message": "The request was valid but "
                                                                                "violates a business rule"}})
        app.state.effects += 1
        echo = received.get("body", {}) if body_model is not None else {k: v for k, v in received.items()}
        if body_model is not None and set(received) - {"body"}:
            echo = {**echo, **{f"_{k}": v for k, v in received.items() if k != "body"}}
        result = {resp_cfg["status_field"]: faults.get("status_value", resp_cfg["status_value"]),
                  resp_cfg["echo_field"]: echo}
        if ikey:
            app.state.idempotency[ikey] = result
        if faults.get("delay"):
            await asyncio.sleep(faults["delay"])        # the work is done, but the answer is late
        return result

    app.add_api_route(path, endpoint, methods=[method], openapi_extra=extra)

    original = app.openapi

    def openapi():
        schema = original()
        comps = schema.setdefault("components", {}).setdefault("schemas", {})
        for m in (body_model, response_model):
            if m is None:
                continue
            js = m.model_json_schema(ref_template="#/components/schemas/{model}")
            for k, v in js.pop("$defs", {}).items():
                comps[k] = v
            comps[m.__name__] = js
        return schema

    app.openapi = openapi
    return app
