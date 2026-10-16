"""HTTP clients for upstream APIs.

Real tools (http/https base URLs) get a pooled httpx.Client with timeouts. Simulated tools (sim://name)
are routed in-process to a simulated FastAPI app from the Chaos Lab; nothing leaves the process.
"""
from __future__ import annotations

import threading

import httpx


class Upstreams:
    def __init__(self, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self._clients: dict[str, httpx.Client] = {}
        self._sim_apps: dict[str, object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def is_simulated(base_url: str) -> bool:
        return base_url.startswith("sim://")

    def set_simulated(self, name: str, app):
        """Install or replace (i.e. 'drift') the simulated API behind sim://<name>."""
        from fastapi.testclient import TestClient
        with self._lock:
            self._sim_apps[name] = app
            self._clients[f"sim://{name}"] = TestClient(app, base_url=f"http://{name}.simulated")

    def sim_app(self, name: str):
        return self._sim_apps.get(name)

    def client(self, base_url: str) -> httpx.Client:
        with self._lock:
            if base_url not in self._clients:
                if self.is_simulated(base_url):
                    raise KeyError(f"simulated API {base_url} is not running")
                self._clients[base_url] = httpx.Client(
                    base_url=base_url, timeout=httpx.Timeout(self.timeout_s),
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10), follow_redirects=False)
            return self._clients[base_url]

    def request(self, base_url: str, method: str, url: str, **kw) -> httpx.Response:
        c = self.client(base_url)
        if self.is_simulated(base_url):
            kw.pop("timeout", None)            # in-process test client does not support per-request timeouts
        return c.request(method, url, **kw)

    def get(self, base_url: str, url: str) -> httpx.Response:
        return self.client(base_url).get(url)
