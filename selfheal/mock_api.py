"""Mock API whose request schema can be changed ("drifted") at runtime.

A schema is described as a flat dict of dotted field paths -> type name, e.g.
    {"target.id": "int", "email": "str"}
plus a list of required headers. FastAPI + Pydantic generate real 422 errors
and a real /openapi.json from it, exactly like a production FastAPI service.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError, create_model

PY_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


def _build_model(name: str, fields: dict[str, str]) -> type[BaseModel]:
    """Build a (possibly nested) Pydantic model from dotted paths."""
    flat: dict[str, Any] = {}
    nested: dict[str, dict[str, str]] = {}
    for path, typ in fields.items():
        head, _, rest = path.partition(".")
        if rest:
            nested.setdefault(head, {})[rest] = typ
        else:
            flat[head] = (PY_TYPES[typ], ...)
    for head, sub in nested.items():
        flat[head] = (_build_model(f"{name}_{head.title()}", sub), ...)
    # strict=True: "123" is NOT silently accepted for an int field, like many real APIs
    return create_model(name, __config__={"strict": True}, **flat)


def make_app(path: str, fields: dict[str, str], headers: list[str] | None = None,
             title: str = "Mock API", faults: dict | None = None) -> FastAPI:
    """faults: {"status": 503} to fail every call, {"delay": 1.0} to answer slowly *after* doing the work."""
    headers = [h.lower() for h in (headers or [])]
    model = _build_model("Body_" + path.strip("/").replace("/", "_"), fields)
    app = FastAPI(title=title)
    app.state.effects = 0                 # how many times the side effect really happened
    app.state.idempotency = {}            # Idempotency-Key -> cached response
    faults = faults or {}

    header_params = [{"name": h, "in": "header", "required": True, "schema": {"type": "string"}}
                     for h in headers]

    @app.post(path, openapi_extra={
        "parameters": header_params,
        "requestBody": {"required": True,
                        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}}},
    })
    async def endpoint(request: Request):
        if faults.get("status"):
            return JSONResponse(status_code=faults["status"], content={"detail": "injected failure"})
        key = request.headers.get("idempotency-key")
        if key and key in app.state.idempotency:          # replay of a request we already processed
            return app.state.idempotency[key]
        errors = []
        for h in headers:
            if not request.headers.get(h):
                errors.append({"type": "missing", "loc": ["header", h], "msg": "Field required", "input": None})
        try:
            body = await request.json()
        except Exception:
            body = None
        try:
            parsed = model.model_validate(body)
        except ValidationError as e:
            for err in e.errors(include_url=False):
                errors.append({"type": err["type"], "loc": ["body", *err["loc"]], "msg": err["msg"],
                               "input": err.get("input")})
            parsed = None
        if errors:
            return JSONResponse(status_code=422, content={"detail": errors})
        app.state.effects += 1
        result = {"status": "updated", "received": parsed.model_dump()}
        if key:
            app.state.idempotency[key] = result
        if faults.get("delay"):
            await asyncio.sleep(faults["delay"])        # the work is done, but the answer is late
        return result

    # make sure the model shows up in /openapi.json components
    original = app.openapi

    def openapi():
        schema = original()
        comps = schema.setdefault("components", {}).setdefault("schemas", {})
        js = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for k, v in js.pop("$defs", {}).items():
            comps[k] = v
        comps[model.__name__] = js
        return schema

    app.openapi = openapi
    return app
