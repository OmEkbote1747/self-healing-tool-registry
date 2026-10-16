"""Failure classification.

Only `schema_drift` leads to a schema repair. A 400/422 is schema drift only when the authoritative schema
has actually changed since the tool version was registered AND the API's errors point at what changed.
An unchanged schema with field errors is invalid user input; a non-field error is a business rule.
"""
from __future__ import annotations

from pydantic import BaseModel

RETRYABLE = {"rate_limited", "server_error", "timeout", "network", "dns_failure"}
EXPLAIN = {
    "ok": "The call succeeded.",
    "schema_drift": "The API's request or response schema changed since this tool was registered.",
    "invalid_input": "The request was rejected for its values, and the API schema has not changed. The caller "
                     "must correct the input; ADAPTIX does not alter user data.",
    "business_rule": "The API understood the request but refused it under a business rule.",
    "authentication_missing": "The API requires credentials and none were sent.",
    "credential_invalid": "The API rejected the credential as invalid. An operator must check the configured key.",
    "credential_expired": "The configured credential has expired. Rotate it, or configure a failover credential.",
    "credential_revoked": "The configured credential was revoked. An operator must issue a new one.",
    "authorization_failed": "The credential is valid but lacks permission for this operation.",
    "rate_limited": "The API is rate limiting this credential.",
    "server_error": "The API failed on its side (5xx). The outcome of a non-idempotent call is unknown.",
    "timeout": "The API did not answer in time. The outcome is unknown, so non-idempotent calls are not replayed.",
    "network": "The API could not be reached.",
    "endpoint_not_found": "The endpoint no longer exists at this path. A human must locate the new endpoint.",
    "method_changed": "The API no longer accepts this HTTP method. A method change can alter side effects.",
    "unsupported_change": "The API changed in a way ADAPTIX does not repair automatically (e.g. a non-JSON body).",
    "schema_unavailable": "The authoritative schema could not be fetched, so no repair is attempted (fail closed).",
    "credential_unavailable": "No authorized credential is configured or available for this call.",
    "egress_denied": "The request was refused before sending: the destination is not an approved, public HTTPS "
                     "host. Add the host to ADAPTIX_ALLOWED_HOSTS only if you are authorized to call it.",
    "dns_failure": "The provider's host name could not be resolved.",
    "tls_error": "The provider's TLS certificate could not be verified; nothing was sent.",
    "rate_limited_local": "The provider asked us to wait (Retry-After); ADAPTIX is respecting that cooldown.",
    "unknown": "The failure does not match a known category.",
}


class Classification(BaseModel):
    category: str
    retryable: bool
    explanation: str
    evidence: list[str] = []


def _c(category: str, *evidence: str) -> Classification:
    return Classification(category=category, retryable=category in RETRYABLE, explanation=EXPLAIN[category],
                          evidence=[e for e in evidence if e])


def classify_exception(kind: str) -> Classification:
    cat = kind if kind in ("timeout", "network", "tls_error", "dns_failure", "egress_denied", "rate_limited") \
        else "network"
    return _c(cat, f"transport: {kind}")


def classify_response(status: int, body, schema_changed: bool | None = None,
                      touched: set[str] | None = None, conflicts: list[str] | None = None) -> Classification:
    """`schema_changed`: None = authoritative schema unavailable; `touched`: locations changed by the drift;
    `conflicts`: ways the request that was actually sent violates the new authoritative schema on changed
    locations (independent evidence for providers whose error bodies do not name fields)."""
    detail = body.get("detail") if isinstance(body, dict) else None
    text = str(detail if detail is not None else body)[:300].lower()
    if status < 400:
        return _c("ok")
    if status == 401:
        if "expired" in text:
            return _c("credential_expired", "401 says the key expired")
        if "revoked" in text:
            return _c("credential_revoked", "401 says the key was revoked")
        if "missing" in text:
            return _c("authentication_missing", "401 says no credential was sent")
        return _c("credential_invalid", "401 from the API")
    if status == 403:
        return _c("authorization_failed", "403 from the API")
    if status == 404:
        return _c("endpoint_not_found", "404 from the API")
    if status == 405:
        return _c("method_changed", "405 Method Not Allowed")
    if status in (413, 415):
        return _c("unsupported_change", f"{status} from the API")
    if status == 429:
        return _c("rate_limited", "429 Too Many Requests")
    if status >= 500:
        return _c("server_error", f"{status} from the API")
    if status in (400, 409, 422):
        field_errors = detail if isinstance(detail, list) else []
        if not field_errors:
            if schema_changed and conflicts:
                return _c("schema_drift", "authoritative schema changed",
                          "the request sent does not conform to the new schema: " + "; ".join(conflicts)[:200])
            if schema_changed is None and status in (400, 422):
                return _c("schema_unavailable", f"{status}, and the authoritative schema could not be read")
            return _c("business_rule", f"{status} with a non-field error: {text[:120]}")
        if schema_changed is None:
            return _c("schema_unavailable", "field errors, but the authoritative schema could not be read")
        locs = []
        for e in field_errors:
            loc = [str(x) for x in (e.get("loc") or [])]
            if loc:
                locs.append(f"{loc[0]}:{'.'.join(loc[1:])}")
        if not schema_changed:
            return _c("invalid_input", "schema unchanged", f"rejected fields: {', '.join(locs)[:200]}")
        touched = touched or set()
        related = [l for l in locs if any(l == t or l.startswith(t + ".") or t.startswith(l + ".") for t in touched)]
        missing = [e for e in field_errors if e.get("type") == "missing"]
        if related or missing:
            return _c("schema_drift", "authoritative schema changed", f"errors on changed fields: {', '.join(related or locs)[:200]}")
        return _c("invalid_input", "schema changed, but the errors are on unchanged fields")
    return _c("unknown", f"HTTP {status}")
