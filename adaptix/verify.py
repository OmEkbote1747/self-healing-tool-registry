"""Verify that the requested task actually succeeded. An HTTP 2xx alone is not enough."""
from __future__ import annotations

from .models import ToolSpec, VerificationResult
from .tooldef import ToolDef, get_path


def _contains(expected, actual) -> bool:
    """Every value we sent is present, unchanged, in what the API says it applied."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _contains(v, actual[k]) for k, v in expected.items())
    return expected == actual


def expected_echo(req: dict) -> dict:
    if req.get("body") is not None:
        out = dict(req["body"])
        for where in ("query", "path_params"):
            if req.get(where):
                out[f"_{'path' if where == 'path_params' else where}"] = req[where]
        return out
    out = {}
    if req.get("query"):
        out["query"] = req["query"]
    if req.get("path_params"):
        out["path"] = req["path_params"]
    return out


def verify_by_schema(response, op) -> VerificationResult:
    """Read operations (no status/echo output): the response must conform to the authoritative response schema."""
    import jsonschema
    if op is None or not op.response_schema or op.source not in ("openapi", "json_schema"):
        return VerificationResult(ok=False, checks=[{"name": "authoritative response schema available", "ok": False,
                                                     "detail": "no authoritative response schema, so the result "
                                                               "cannot be verified"}])
    sample = response[:100] if isinstance(response, list) else response      # bounded work on large lists
    schema = op.response_schema
    try:
        jsonschema.validate(sample, schema)
        return VerificationResult(ok=True, checks=[{"name": "response conforms to the authoritative schema", "ok": True,
                                                    "detail": f"validated {len(sample) if isinstance(sample, list) else 1}"
                                                              f" item(s) against {op.source}"}])
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        return VerificationResult(ok=False, contract_drift=False, checks=[{
            "name": "response conforms to the authoritative schema", "ok": False,
            "detail": f"{where}: {e.validator} check failed"}])
    except jsonschema.SchemaError:
        return VerificationResult(ok=False, checks=[{"name": "response schema usable", "ok": False,
                                                     "detail": "the published response schema is not valid JSON Schema"}])


def verify(spec: ToolSpec, td: ToolDef, response, req: dict, op=None) -> VerificationResult:
    if not td.response_map:
        return verify_by_schema(response, op)
    checks, drift = [], False
    if not isinstance(response, dict):
        return VerificationResult(ok=False, checks=[{"name": "JSON object response", "ok": False,
                                                     "detail": type(response).__name__}])
    status_path = td.response_map.get("status")
    if status_path:
        try:
            value = get_path(response, status_path)
            ok = str(value) in spec.success_values
            checks.append({"name": "task reported as done", "ok": ok,
                           "detail": f"{status_path} = {value!r}" + ("" if ok else f" (expected one of {spec.success_values})")})
        except KeyError:
            drift = True
            checks.append({"name": "status output present", "ok": False,
                           "detail": f"response has no '{status_path}' (possible response schema change)"})
    echo_path = td.response_map.get("echo")
    if echo_path:
        try:
            echo = get_path(response, echo_path)
            ok = _contains(expected_echo(req), echo)
            checks.append({"name": "API applied exactly the values sent", "ok": ok,
                           "detail": "values match" if ok else "applied data differs from the request"})
        except KeyError:
            drift = True
            checks.append({"name": "echo output present", "ok": False,
                           "detail": f"response has no '{echo_path}' (possible response schema change)"})
    return VerificationResult(ok=bool(checks) and all(c["ok"] for c in checks), checks=checks, contract_drift=drift)
