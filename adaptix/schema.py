"""Read an operation's request schema from /openapi.json.

Covers what the repair needs: the HTTP method, path and query parameters, required headers, JSON body
fields (nested objects, arrays, optional/nullable fields, unions, enums, formats) and the request
content type. Each field is keyed by its location: "body:target.id", "query:limit", "path:user_id".
"""
from __future__ import annotations

from dataclasses import dataclass, field

JSON_TO_PY = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "array": "list",
              "object": "object", "null": "null"}


@dataclass
class FieldSpec:
    types: list[str]                    # e.g. ["int"], ["str", "int"] for a union; "null" removed
    required: bool
    enum: list | None = None
    fmt: str | None = None              # e.g. "date"
    schema: dict = field(default_factory=dict)

    @property
    def type(self) -> str:
        if self.enum is not None:
            return "enum"
        if self.fmt == "date":
            return "date"
        return self.types[0] if self.types else "str"


@dataclass
class Operation:
    method: str
    path: str
    fields: dict[str, FieldSpec]        # location-qualified key -> spec
    headers: list[str]                  # required header names (lower case)
    body_schema: dict | None            # resolved JSON schema of the body, for validation
    content_types: list[str]


def _resolve(s, comps):
    if isinstance(s, dict):
        if "$ref" in s:
            return _resolve(comps[s["$ref"].split("/")[-1]], comps)
        return {k: _resolve(v, comps) for k, v in s.items()}
    if isinstance(s, list):
        return [_resolve(x, comps) for x in s]
    return s


def _spec(s: dict, required: bool) -> FieldSpec:
    """Normalise a property schema: unions (anyOf/oneOf), nullable, enum, format, arrays."""
    options = s.get("anyOf") or s.get("oneOf") or [s]
    types, enum, fmt = [], None, None
    for o in options:
        t = JSON_TO_PY.get(o.get("type"), "str") if "type" in o or "enum" not in o else "str"
        if o.get("type") == "null":
            continue
        if "enum" in o:
            enum = list(o["enum"])
        if o.get("format"):
            fmt = o["format"]
        types.append(t)
    nullable = any(o.get("type") == "null" for o in options)
    return FieldSpec(types or ["str"], required and not nullable, enum, fmt, s)


def _walk(s: dict, prefix: str, required_here: bool, out: dict[str, FieldSpec]):
    req = set(s.get("required", []))
    for name, sub in s.get("properties", {}).items():
        key = f"{prefix}{name}"
        if sub.get("type") == "object" or "properties" in sub:
            _walk(sub, key + ".", required_here and name in req, out)
        else:
            out["body:" + key] = _spec(sub, required_here and name in req)


def read_operation(client, path: str, method: str = "POST") -> Operation | None | str:
    """Return the Operation, None if /openapi.json is unavailable, or a string naming another method
    if the path still exists but no longer accepts `method`."""
    try:
        spec = client.get("/openapi.json").json()
        item = spec["paths"][path]
    except Exception:
        return None
    op = item.get(method.lower())
    if op is None:
        others = [m.upper() for m in item if m in ("get", "post", "put", "patch", "delete")]
        return others[0] if others else None
    comps = spec.get("components", {}).get("schemas", {})
    fields: dict[str, FieldSpec] = {}
    headers = []
    for p in op.get("parameters", []):
        where = p.get("in")
        if where == "header":
            if p.get("required"):
                headers.append(p["name"].lower())
            continue
        if where in ("query", "path"):
            fields[f"{where}:{p['name']}"] = _spec(_resolve(p.get("schema", {}), comps),
                                                   bool(p.get("required")) or where == "path")
    body_schema, content_types = None, []
    rb = op.get("requestBody")
    if rb:
        content_types = list(rb.get("content", {}))
        js = rb.get("content", {}).get("application/json")
        if js:
            body_schema = _resolve(js["schema"], comps)
            _walk(body_schema, "", True, fields)
    return Operation(method.upper(), path, fields, headers, body_schema, content_types)
