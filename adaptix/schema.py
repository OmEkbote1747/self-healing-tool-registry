"""Authoritative schema discovery and normalisation.

Reads an operation from an OpenAPI document (or a configured JSON Schema) into location-keyed fields:
"body:target.id", "query:limit", "path:user_id", plus response fields "response:status". Documentation
(descriptions, x-renamed-from) is kept as *evidence*; every evidence quote a repair relies on must appear
verbatim in this text. Schemas are untrusted data: they are parsed, never executed or obeyed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

JSON_TO_PY = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "array": "list",
              "object": "object", "null": "null"}


@dataclass
class FieldSpec:
    types: list[str]
    required: bool
    enum: list | None = None
    fmt: str | None = None
    description: str = ""
    renamed_from: str | None = None
    schema: dict = field(default_factory=dict)

    @property
    def type(self) -> str:
        if self.enum is not None:
            return "enum"
        if self.fmt == "date":
            return "date"
        return self.types[0] if self.types else "str"

    def label(self) -> str:
        t = f"enum[{'|'.join(map(str, self.enum))}]" if self.enum is not None else self.type
        return t + ("" if self.required else "?")


@dataclass
class Operation:
    method: str
    path: str
    fields: dict[str, FieldSpec]              # request fields, location-keyed
    headers: list[str]                        # required header names (lower case)
    body_schema: dict | None
    content_types: list[str]
    response_fields: dict[str, FieldSpec] = field(default_factory=dict)   # "response:status" ...
    response_schema: dict | None = None
    source: str = ""

    def signature(self) -> str:
        """Stable hash of everything that affects request/response compatibility."""
        def norm(fs):
            return {k: [v.types, v.required, v.enum, v.fmt] for k, v in sorted(fs.items())}
        blob = json.dumps({"m": self.method, "p": self.path, "f": norm(self.fields), "h": sorted(self.headers),
                           "c": sorted(self.content_types), "r": norm(self.response_fields)}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def evidence_text(self) -> str:
        """All documentation text a repair may quote as evidence."""
        parts = []
        for k, f in {**self.fields, **self.response_fields}.items():
            parts.append(k.split(":", 1)[1])
            if f.description:
                parts.append(f.description)
            if f.renamed_from:
                parts.append(f"x-renamed-from: {f.renamed_from}")
        return "\n".join(parts)

    def to_json(self) -> dict:
        d = asdict(self)
        d["signature"] = self.signature()
        return d

    @staticmethod
    def from_json(d: dict) -> "Operation":
        d = dict(d)
        d.pop("signature", None)
        d["fields"] = {k: FieldSpec(**v) for k, v in d["fields"].items()}
        d["response_fields"] = {k: FieldSpec(**v) for k, v in d.get("response_fields", {}).items()}
        return Operation(**d)


def _resolve(s, comps, depth=0):
    if depth > 20:
        return {}
    if isinstance(s, dict):
        if "$ref" in s:
            return _resolve(comps.get(s["$ref"].split("/")[-1], {}), comps, depth + 1)
        return {k: _resolve(v, comps, depth + 1) for k, v in s.items()}
    if isinstance(s, list):
        return [_resolve(x, comps, depth + 1) for x in s]
    return s


def _spec(s: dict, required: bool) -> FieldSpec:
    options = s.get("anyOf") or s.get("oneOf") or [s]
    types, enum, fmt = [], None, None
    nullable = False
    for o in options:
        if o.get("type") == "null":
            nullable = True
            continue
        if "enum" in o:
            enum = list(o["enum"])
        if o.get("format"):
            fmt = o["format"]
        types.append(JSON_TO_PY.get(o.get("type"), "str") if "type" in o else "str")
    return FieldSpec(types or ["str"], required and not nullable, enum, fmt, str(s.get("description") or "")[:500],
                     s.get("x-renamed-from"), s)


def _walk(s: dict, prefix: str, loc: str, required_here: bool, out: dict[str, FieldSpec]):
    req = set(s.get("required", []))
    for name, sub in (s.get("properties") or {}).items():
        key = f"{prefix}{name}"
        if (sub.get("type") == "object" and "properties" in sub) or "properties" in sub:
            _walk(sub, key + ".", loc, required_here and name in req, out)
        else:
            out[f"{loc}:{key}"] = _spec(sub, required_here and name in req)


def parse_openapi(spec: dict, path: str, method: str = "POST") -> "Operation | str | None":
    """Operation, a string naming another method if `method` is no longer accepted, or None."""
    item = (spec.get("paths") or {}).get(path)
    if item is None:
        return None
    op = item.get(method.lower())
    if op is None:
        others = [m.upper() for m in item if m in ("get", "post", "put", "patch", "delete")]
        return others[0] if others else None
    comps = (spec.get("components") or {}).get("schemas") or {}
    fields: dict[str, FieldSpec] = {}
    headers = []
    for p in op.get("parameters") or []:
        where = p.get("in")
        if where == "header":
            if p.get("required"):
                headers.append(p["name"].lower())
            continue
        if where in ("query", "path"):
            fs = _spec(_resolve(p.get("schema") or {}, comps), bool(p.get("required")) or where == "path")
            fs.description = fs.description or str(p.get("description") or "")[:500]
            fields[f"{where}:{p['name']}"] = fs
    body_schema, content_types = None, []
    rb = op.get("requestBody")
    if rb:
        content_types = list((rb.get("content") or {}))
        js = (rb.get("content") or {}).get("application/json")
        if js:
            body_schema = _resolve(js.get("schema") or {}, comps)
            _walk(body_schema, "", "body", True, fields)
    response_fields: dict[str, FieldSpec] = {}
    response_schema = None
    ok = (op.get("responses") or {}).get("200") or (op.get("responses") or {}).get("201") or {}
    rjs = ((ok.get("content") or {}).get("application/json") or {}).get("schema")
    if rjs:
        response_schema = _resolve(rjs, comps)
        _walk(response_schema, "", "response", True, response_fields)
        for name, sub in (response_schema.get("properties") or {}).items():   # object-valued outputs too
            if f"response:{name}" not in response_fields and not any(k.startswith(f"response:{name}.")
                                                                     for k in response_fields):
                response_fields[f"response:{name}"] = _spec(sub, name in set(response_schema.get("required", [])))
    return Operation(method.upper(), path, fields, headers, body_schema, content_types, response_fields,
                     response_schema, source="openapi")


def parse_json_schema(doc: dict, path: str, method: str) -> Operation:
    """A configured JSON Schema for the request body (optionally {"request": ..., "response": ...})."""
    req = doc.get("request", doc)
    fields: dict[str, FieldSpec] = {}
    _walk(req, "", "body", True, fields)
    response_fields: dict[str, FieldSpec] = {}
    if doc.get("response"):
        _walk(doc["response"], "", "response", True, response_fields)
    return Operation(method.upper(), path, fields, [h.lower() for h in doc.get("x-required-headers", [])], req,
                     ["application/json"], response_fields, doc.get("response"), source="json_schema")


def discover(client, schema_url: str, path: str, method: str, kind: str = "openapi") -> "Operation | str | None":
    """Fetch an authoritative schema. Returns None when it cannot be obtained (callers must fail closed)."""
    try:
        r = client.get(schema_url)
        if r.status_code != 200:
            return None
        doc = r.json()
    except Exception:
        return None
    if kind == "json_schema":
        return parse_json_schema(doc, path, method)
    return parse_openapi(doc, path, method)


def read_operation(client, path: str, method: str = "POST"):
    """Compatibility wrapper for the previous engine (to be removed when the new pipeline lands)."""
    return discover(client, "/openapi.json", path, method)
