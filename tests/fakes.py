"""Test-only stand-in for the network layer of PRODUCTION mode.

FakeGuard replaces GuardedHTTP (the real SSRF-guarded client) so production-path logic - registration,
monitoring, drift repair, rollback - can be tested without the internet. It still refuses hosts that are not
installed, and it records every request. It is used only inside tests; the application never imports it.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from adaptix.netguard import EgressDenied


class FakeGuard:
    def __init__(self):
        self.apps: dict[str, TestClient] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}      # host -> exception to raise (network/TLS/DNS simulations)

    def install(self, host: str, app):
        self.apps[host] = TestClient(app, base_url=f"https://{host}")

    def _client(self, url: str) -> TestClient:
        host = urlsplit(url).hostname
        if host in self.fail:
            raise self.fail[host]
        if host not in self.apps:
            raise EgressDenied("host_not_allowed", f"{host} is not in the approved host list")
        return self.apps[host]

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        c = self._client(url)
        self.calls.append((method, url))
        kw.pop("timeout", None)
        parts = urlsplit(url)
        return c.request(method, parts.path + (f"?{parts.query}" if parts.query else ""), **kw)

    def get_json(self, url: str, max_bytes: int):
        r = self.request("GET", url)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
