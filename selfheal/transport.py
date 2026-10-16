"""Production-shaped request handling for tool calls.

* timeouts and connection pooling (httpx.Client limits)
* per-tool token-bucket rate limiting
* per-tool circuit breaker (opens after repeated timeouts / 5xx / connection errors)
* retry budget: repairs may add at most a fixed share of extra calls
* idempotency: every logical call carries one Idempotency-Key, reused on its repaired retry

Replay rule: a request is only re-sent after a 400/422 validation error, because the server rejected it
before doing anything. After a timeout, 5xx or dropped connection the outcome is unknown, so nothing is
replayed automatically - that could duplicate a side effect such as a payment.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx

REPAIRABLE = {400, 422}           # validation errors: safe to repair and replay
NOT_SCHEMA = {401: "authentication failed or changed - new credentials are needed",
              403: "permission denied - not a schema problem",
              404: "endpoint not found - the API may have moved; needs a human",
              409: "conflict reported by the API - not replayed",
              429: "rate limited by the API - try later"}


def _in_process(client) -> bool:
    """Starlette's TestClient runs the app in-process and does not support per-request timeouts."""
    try:
        from starlette.testclient import TestClient
        return isinstance(client, TestClient)
    except Exception:
        return False


class TransportError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # timeout | connection | circuit_open | rate_limited | retry_budget


@dataclass
class _Bucket:
    rate: float
    burst: float
    tokens: float = 0.0
    stamp: float = field(default_factory=time.monotonic)

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


@dataclass
class _Breaker:
    threshold: int
    cooldown: float
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:   # half-open: let one call through
            return True
        return False

    def record(self, ok: bool):
        if ok:
            self.failures, self.opened_at = 0, None
        else:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


class Transport:
    def __init__(self, client=None, base_url: str | None = None, timeout: float = 5.0,
                 rate_per_sec: float = 20.0, burst: int = 40, breaker_threshold: int = 5,
                 breaker_cooldown: float = 30.0, retry_budget: float = 0.2, min_retries: int = 10):
        self.client = client or httpx.Client(base_url=base_url, timeout=httpx.Timeout(timeout),
                                             limits=httpx.Limits(max_connections=20, max_keepalive_connections=10))
        self.timeout = timeout
        self._buckets: dict[str, _Bucket] = {}
        self._breakers: dict[str, _Breaker] = {}
        self._lock = threading.Lock()
        self.rate, self.burst = rate_per_sec, burst
        self.breaker_threshold, self.breaker_cooldown = breaker_threshold, breaker_cooldown
        self.retry_budget, self.min_retries = retry_budget, min_retries
        self.requests = 0
        self.retries = 0

    @staticmethod
    def new_idempotency_key() -> str:
        return uuid.uuid4().hex

    def _bucket(self, tool):
        return self._buckets.setdefault(tool, _Bucket(self.rate, self.burst, tokens=self.burst))

    def _breaker(self, tool):
        return self._breakers.setdefault(tool, _Breaker(self.breaker_threshold, self.breaker_cooldown))

    def allow_retry(self) -> bool:
        """Retries may be at most `retry_budget` of all requests (after a small free allowance)."""
        return self.retries < self.min_retries or self.retries < self.retry_budget * self.requests

    def get(self, path: str):
        return self.client.get(path)

    def send(self, method: str, path: str, *, tool: str, json=None, headers=None, idempotency_key: str,
             is_retry: bool = False):
        with self._lock:
            if not self._breaker(tool).allow():
                raise TransportError("circuit_open", f"circuit open for {tool}: recent calls kept failing")
            if not self._bucket(tool).take():
                raise TransportError("rate_limited", f"local rate limit reached for {tool}")
            if is_retry and not self.allow_retry():
                raise TransportError("retry_budget", "retry budget exhausted; not repairing more calls right now")
            self.requests += 1
            self.retries += is_retry
        headers = {**(headers or {}), "Idempotency-Key": idempotency_key}
        try:
            extra = {} if _in_process(self.client) else {"timeout": self.timeout}
            r = self.client.request(method, path, json=json, headers=headers, **extra)
        except httpx.TimeoutException as e:
            self._breaker(tool).record(False)
            raise TransportError("timeout", f"no answer within {self.timeout:.1f}s - outcome unknown, not replayed") from e
        except httpx.TransportError as e:
            self._breaker(tool).record(False)
            raise TransportError("connection", f"connection failed - outcome unknown, not replayed: {e}") from e
        self._breaker(tool).record(r.status_code < 500)
        return r
