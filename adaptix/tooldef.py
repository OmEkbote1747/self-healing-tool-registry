"""Versioned tool definitions: how a tool's arguments become an HTTP request and how its response is read.

A repair only ever changes these mappings. It never stores or invents values: every value sent comes from
the caller's arguments (through an allowlisted lossless transform) or from a configured credential.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass, field

TRANSFORMS = ("none", "to_int", "to_float", "to_str", "to_bool", "iso_date")   # the complete allowlist


def apply_transform(value, transform: str):
    """Lossless conversions only; raises ValueError otherwise."""
    if transform in (None, "none"):
        return value
    if transform == "to_str":
        if isinstance(value, (dict, list)):
            raise ValueError("not a scalar")
        return str(value)
    if transform == "to_int":
        if isinstance(value, bool):
            raise ValueError("bool is not an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise ValueError(f"{value!r} is not a whole number")
    if transform == "to_float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
            return float(value)
        raise ValueError(f"{value!r} is not a number")
    if transform == "to_bool":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "false"):
            return str(value).lower() == "true"
        raise ValueError(f"{value!r} is not true/false")
    if transform == "iso_date":
        try:
            return _dt.date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise ValueError(f"{value!r} is not an ISO date (YYYY-MM-DD)") from None
    raise ValueError(f"transform {transform!r} is not allowed")


TYPE_TO_TRANSFORM = {"int": "to_int", "float": "to_float", "str": "to_str", "bool": "to_bool", "date": "iso_date"}


def py_type(v) -> str:
    return {bool: "bool", int: "int", float: "float", list: "list", dict: "object"}.get(type(v), "str")


def set_path(d: dict, path: str, value):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def get_path(d, path: str):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            raise KeyError(path)
        d = d[p]
    return d


@dataclass
class ToolDef:
    name: str
    version: int
    method: str
    path: str
    # request location ("body:target.id" / "query:limit" / "path:id") -> [argument name, transform]
    request_map: dict[str, list] = field(default_factory=dict)
    # header name -> credential group (values come from the credential registry at send time)
    header_map: dict[str, str] = field(default_factory=dict)
    # logical output ("status", "echo") -> response path ("status", "data.result")
    response_map: dict[str, str] = field(default_factory=dict)
    schema_signature: str = ""
    status: str = "active"            # active | superseded | rolled_back
    origin: str = "registered"        # registered | repair | reuse | rollback
    parent_version: int | None = None
    incident_id: str | None = None
    patch: list[dict] = field(default_factory=list)   # reversible operations that produced this version

    def build(self, args: dict) -> dict:
        body: dict = {}
        query, path_params = {}, {}
        for loc, (src, transform) in self.request_map.items():
            if src not in args:                   # optional argument the caller did not send
                continue
            value = apply_transform(args[src], transform)
            where, name = loc.split(":", 1)
            if where == "body":
                set_path(body, name, value)
            elif where == "query":
                query[name] = value
            else:
                path_params[name] = value
        url = self.path.format(**{k: str(v) for k, v in path_params.items()}) if path_params else self.path
        has_body = any(k.startswith("body:") for k in self.request_map)
        return {"method": self.method, "url": url, "query": query, "path_params": path_params,
                "body": body if has_body else None}

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "ToolDef":
        return ToolDef(**d)

    def describe(self) -> dict:
        return {"version": self.version, "status": self.status, "origin": self.origin, "method": self.method,
                "path": self.path,
                "request": {loc: (s if t in (None, "none") else f"{s} ({t})") for loc, (s, t) in self.request_map.items()},
                "headers": {h: f"credential group '{g}'" for h, g in self.header_map.items()},
                "response": dict(self.response_map), "schema_signature": self.schema_signature}
