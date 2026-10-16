"""Credential registry with authorized failover.

Credentials are *references* configured by the operator: an alias, a provider, a group, the scope it may be
used for, and the name of the environment variable (or platform secret) that holds the value. The value is
read server-side only and never leaves this module except as an outgoing request header or SDK argument.

Failover moves to another enabled credential in the same group, for the same permitted scope, and only for
the failure categories the operator allowed (default: rate_limited, credential_expired). ADAPTIX never
guesses, generates, edits or retries a rejected key, and never uses a schema repair to "fix" an auth error.

ADAPTIX_CREDENTIALS (JSON list), e.g.:
  [{"alias": "gemini-primary", "provider": "gemini", "group": "gemini", "secret_env": "GEMINI_API_KEY",
    "scope": ["llm"]},
   {"alias": "crm-key-a", "provider": "crm", "group": "crm", "secret_env": "CRM_KEY_A", "header": "x-api-key",
    "scope": ["tool:update_user"], "failover_on": ["rate_limited", "credential_expired"]}]
"""
from __future__ import annotations

import json
import secrets as _secrets
import threading
import time
from dataclasses import dataclass, field

from .config import env
from .security import register_secret

FAILOVER_DEFAULT = ("rate_limited", "credential_expired")


class CredentialError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category      # missing_configuration | no_valid_credential | not_permitted


@dataclass
class Credential:
    alias: str
    provider: str
    group: str
    secret_env: str | None = None
    header: str | None = None
    scope: list[str] = field(default_factory=lambda: ["*"])
    enabled: bool = True
    version: str = "1"
    failover_on: list[str] = field(default_factory=lambda: list(FAILOVER_DEFAULT))
    simulated: bool = False
    # runtime state (never persisted with the secret)
    state: str = "unverified"        # unverified | ok | cooling_down | rejected
    last_category: str | None = None
    cooldown_until: float = 0.0

    def permits(self, scope: str) -> bool:
        return "*" in self.scope or scope in self.scope or scope.split(":")[0] in self.scope

    def safe(self) -> dict:
        return {"alias": self.alias, "provider": self.provider, "group": self.group, "header": self.header,
                "scope": self.scope, "enabled": self.enabled, "version": self.version, "simulated": self.simulated,
                "configured": self.secret_env is not None, "state": self.state,
                "last_failure_category": self.last_category, "failover_on": self.failover_on}


class CredentialRegistry:
    def __init__(self, credentials: list[Credential], secrets: dict[str, str] | None = None):
        self._creds = credentials
        self._secrets: dict[str, str] = {}       # alias -> value (server memory only)
        self._lock = threading.Lock()
        for c in credentials:
            value = (secrets or {}).get(c.alias) or (env(c.secret_env) if c.secret_env else None)
            if value:
                self._secrets[c.alias] = value
                register_secret(value)            # scrubbed from every log, trace and error text
            else:
                c.state = "missing"

    # ---------------- construction
    @staticmethod
    def from_env() -> "CredentialRegistry":
        raw = env("ADAPTIX_CREDENTIALS")
        creds: list[Credential] = []
        if raw:
            for item in json.loads(raw):
                creds.append(Credential(**{k: v for k, v in item.items() if k in Credential.__dataclass_fields__}))
        else:
            creds.append(Credential("gemini-primary", "gemini", "gemini", "GEMINI_API_KEY", scope=["llm"]))
            if env("GEMINI_API_KEY_SECONDARY"):
                creds.append(Credential("gemini-secondary", "gemini", "gemini", "GEMINI_API_KEY_SECONDARY",
                                        scope=["llm"]))
        return CredentialRegistry(creds)

    def add_simulated_group(self, group: str, header: str, aliases: list[str]) -> dict[str, str]:
        """Throwaway random keys for the simulated APIs in the Chaos Lab. Never real credentials."""
        values = {}
        with self._lock:
            for a in aliases:
                if any(c.alias == a for c in self._creds):
                    values[a] = self._secrets.get(a, "")
                    continue
                v = "sim-" + _secrets.token_urlsafe(16)
                self._creds.append(Credential(a, "simulated", group, None, header, ["*"], simulated=True))
                self._secrets[a] = v
                register_secret(v)
                values[a] = v
        return values

    # ---------------- selection and failover
    def candidates(self, group: str, scope: str) -> list[Credential]:
        now = time.monotonic()
        return [c for c in self._creds if c.group == group and c.enabled and c.permits(scope)
                and c.alias in self._secrets and c.state != "rejected" and c.cooldown_until <= now]

    def select(self, group: str, scope: str, exclude: set[str] | None = None) -> tuple[Credential, str]:
        in_group = [c for c in self._creds if c.group == group]
        if not in_group:
            raise CredentialError("missing_configuration", f"no credential is configured for group '{group}'")
        if not any(c.permits(scope) for c in in_group):
            raise CredentialError("not_permitted", f"no credential in '{group}' is permitted for {scope}")
        for c in self.candidates(group, scope):
            if c.alias not in (exclude or set()):
                return c, self._secrets[c.alias]
        raise CredentialError("no_valid_credential",
                              f"no valid credential is currently available in group '{group}' for {scope}")

    def report(self, alias: str, category: str, retry_after: float | None = None):
        """Record the outcome of using a credential (safe metadata only)."""
        with self._lock:
            c = next((c for c in self._creds if c.alias == alias), None)
            if c is None:
                return
            c.last_category = category
            if category == "ok":
                c.state = "ok"
            elif category == "rate_limited":
                c.state = "cooling_down"
                c.cooldown_until = time.monotonic() + min(max(retry_after or 5.0, 1.0), 300.0)
            elif category in ("credential_expired", "credential_revoked", "credential_invalid"):
                c.state = "rejected"          # never edited or retried; an operator must rotate it

    def failover(self, current: Credential, category: str, group: str, scope: str,
                 tried: set[str]) -> tuple[Credential, str] | None:
        """Another configured credential, only if the operator allowed failover for this category."""
        if category not in current.failover_on:
            return None
        try:
            return self.select(group, scope, exclude=tried)
        except CredentialError:
            return None

    def status(self) -> list[dict]:
        return [c.safe() for c in self._creds]

    def has_group(self, group: str) -> bool:
        return any(c.group == group for c in self._creds)

    def groups_for_header(self, header: str, scope: str) -> list[str]:
        return sorted({c.group for c in self._creds if (c.header or "").lower() == header.lower() and c.permits(scope)})
