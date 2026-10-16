"""Redaction, secrets and log retention.

* Header values are never shown or logged (they are usually credentials).
* Body fields whose names look sensitive are masked.
* Values echoed back by an API in its error body ("input") are masked to their type.
* Secrets come from the environment or the hosting platform's secret store, never from code.
"""
from __future__ import annotations

import json
import os
import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

MASK = "***"
SENSITIVE = re.compile(r"(auth|api[-_]?key|token|secret|password|passwd|cookie|session|signature|ssn|"
                       r"card|cvv|iban|account[-_]?number|pin\b|otp)", re.I)
SAFE_HEADERS = {"content-type", "accept", "x-client-version", "idempotency-key"}


_KNOWN_SECRETS: set[str] = set()


def register_secret(value: str):
    """Every configured secret value is scrubbed from all text that passes through redact()/scrub()."""
    if value and len(value) >= 6:
        _KNOWN_SECRETS.add(value)


def scrub(text: str) -> str:
    """Remove any known secret value (and anything that looks like a bearer token) from free text."""
    if not isinstance(text, str):
        return text
    for s in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if s in text:
            text = text.replace(s, MASK)
    return re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1" + MASK, text)


def _shape(v) -> str:
    return f"<{type(v).__name__}>"


def redact(obj, key: str | None = None):
    """Return a copy of obj that is safe to display or log."""
    if key is not None and SENSITIVE.search(str(key)):
        return MASK
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return scrub(obj)
    return obj


def redact_headers(headers: dict) -> dict:
    return {k: (v if k.lower() in SAFE_HEADERS else MASK) for k, v in (headers or {}).items()}


def redact_reply(payload):
    """Mask the raw values an API echoes back inside validation errors."""
    if isinstance(payload, dict) and isinstance(payload.get("detail"), list):
        detail = []
        for e in payload["detail"]:
            e = dict(e)
            if "input" in e:
                e["input"] = _shape(e["input"]) if not isinstance(e["input"], (dict, list)) else "<object>"
            detail.append(e)
        return {**payload, "detail": detail}
    return redact(payload)


def get_secret(name: str, default: str | None = None) -> str | None:
    """Environment first (SCHEMAHEAL_SECRET_<NAME>), then Streamlit secrets, else default."""
    env = "SCHEMAHEAL_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    if os.environ.get(env):
        return os.environ[env]
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default


def demo_secret() -> str:
    """A throwaway per-process value for the demo, so no real-looking credential lives in the code."""
    return "demo-" + _secrets.token_urlsafe(12)


def purge_audit_log(path: str | Path, retention_days: int = 30) -> int:
    """Drop audit entries older than the retention window. Returns how many were removed."""
    path = Path(path)
    if not path.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    keep, dropped = [], 0
    for line in path.read_text().splitlines():
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"])
        except Exception:
            dropped += 1
            continue
        if ts >= cutoff:
            keep.append(line)
        else:
            dropped += 1
    path.write_text("\n".join(keep) + ("\n" if keep else ""))
    return dropped
