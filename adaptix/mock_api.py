"""Mock API whose request schema can be changed ("drifted") at runtime.

A schema is a dict of field keys -> type specs:

    body fields   "target.id": "int"          (dotted = nested object)
    query params  "q:limit": "int"
    path params   "p:user_id": "str"           (the path must contain {user_id})

Type specs: str, int, float, bool, date, list[str], list[int], enum[a|b|c]; a trailing "?" makes the
field optional. FastAPI + Pydantic produce real 422 errors and a real /openapi.json from it, like a
production FastAPI service.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any, Literal, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError, create_model

SCALARS = {"str": str, "int": int, "float": float, "bool": bool, "date": _dt.date}


def parse_type(spec: str):
    """Return (python type, required) for a type spec."""
    required = not spec.endswith("?")
    spec = spec.rstrip("?")
    if spec.startswith("list[") and spec.endswith("]"):
        typ: Any = list[SCALARS[spec[5:-1]]]
    elif spec.startswith("enum[") and spec.endswith("]"):
        typ = Literal[tuple(spec[5:-1].split("|"))]
    else:
        typ = SCALARS[spec]
    return typ, required


def _build_model(name: str, fields: dict[str, str], strict: bool = True) -> type[BaseModel]:
    """Build a (possibly nested) Pydantic model from dotted paths."""
    flat: dict[str, Any] = {}
    nested: dict[str, dict[str, str]] = {}
    for path, spec in fields.items():
        head, _, rest = path.partition(".")
        if rest:
            nested.setdefault(head, {})[rest] = spec
        else:
            typ, required = parse_type(spec)
            flat[head] = (typ, ...) if required else (Optional[typ], None)
    for head, sub in nested.items():
        flat[head] = (_build_model(f"{name}_{head.title()}", sub, strict), ...)
    # strict: "123" is NOT silently accepted for an int body field, like many real APIs
    return create_model(name, __config__={"strict": strict}, **flat)


def make_app(path: str, fields: dict[str, str], headers: list[str] | None = None, title: str = "Mock API",
             faults: dict | None = None, method: str = "POST", content_type: str = "application/json") -> FastAPI:
    """faults: {"status": 503} fails every call; {"delay": 1.0} answers slowly *after* doing the work."""
    headers = [h.lower() for h in (headers or [])]
    method = method.upper()
    body_fields = {k: v for k, v in fields.items() if not k.startswith(("q:", "p:"))}
    query_fields = {k[2:]: v for k, v in fields.items() if k.startswith("q:")}
    path_fields = {k[2:]: v for k, v in fields.items() if k.startswith("p:")}
    base = path.strip("/").replace("/", "_").replace("{", "").replace("}", "") or "root"
    body_model = _build_model("Body_" + base, body_fields) if body_fields else None
    query_model = _build_model("Query_" + base, query_fields, strict=False) if query_fields else None
    path_model = _build_model("Path_" + base, path_fields, strict=False) if path_fields else None
    app = FastAPI(title=title)
    app.state.effects = 0                 # how many times the side effect really happened
    app.state.idempotency = {}            # Idempotency-Key -> cached response
    faults = faults or {}

    def _param_schema(model, name):
        return model.model_json_schema()["properties"][name]

    params = [{"name": h, "in": "header", "required": True, "schema": {"type": "string"}} for h in headers]
    for name, spec in query_fields.items():
        params.append({"name": name, "in": "query", "required": parse_type(spec)[1],
                       "schema": _param_schema(query_model, name)})
    for name in path_fields:
        params.append({"name": name, "in": "path", "required": True, "schema": _param_schema(path_model, name)})
    extra: dict[str, Any] = {"parameters": params}
    if body_model is not None:
        extra["requestBody"] = {"required": True, "content": {
            content_type: {"schema": {"$ref": f"#/components/schemas/{body_model.__name__}"}}}}

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
        app.state.effects += 1
        result = {"status": "updated", "received": received.get("body", received)}
        if set(received) - {"body"}:
            result["params"] = {k: v for k, v in received.items() if k != "body"}
        if key:
            app.state.idempotency[key] = result
        if faults.get("delay"):
            await asyncio.sleep(faults["delay"])        # the work is done, but the answer is late
        return result

    app.add_api_route(path, endpoint, methods=[method], openapi_extra=extra)

    # make sure the models show up in /openapi.json components
    original = app.openapi

    def openapi():
        schema = original()
        comps = schema.setdefault("components", {}).setdefault("schemas", {})
        if body_model is not None:
            js = body_model.model_json_schema(ref_template="#/components/schemas/{model}")
            for k, v in js.pop("$defs", {}).items():
                comps[k] = v
            comps[body_model.__name__] = js
        return schema

    app.openapi = openapi
    return app
