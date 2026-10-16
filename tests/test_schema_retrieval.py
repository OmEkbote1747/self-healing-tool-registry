"""Authoritative schema retrieval: permitted hosts, the controlled test service across restarts, SSRF limits,
malformed and non-JSON schemas, timeouts, and that an unavailable schema never means 'no drift' or a repair."""
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from adaptix import netguard
from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.mock_api import make_app
from adaptix.netguard import EgressDenied, GuardedHTTP
from adaptix.storage import Storage
from tests.fakes import FakeGuard

TOKEN = "schema-retrieval-token-00000000"
HOST = "api.example.test"


def state(tmp_path, guard=None, settings=None, name="p.db"):
    return State(settings or Settings(admin_token=TOKEN, allowed_hosts=(HOST,)), storage=Storage(tmp_path / name),
                 creds=CredentialRegistry([], allow_simulated=False), llm=NoProvider(), sleep=lambda s: None,
                 guard=guard, start_monitor=False)


def client(st):
    return TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})


# ---------------------------------------------------------------- the reported error and its root cause
def test_controlled_service_schema_survives_a_restart(tmp_path):
    """Root cause: a REAL API TEST tool recorded http://127.0.0.1:<old port>; a new process had no exemption for
    that address, so the guard refused it with egress_denied:scheme_not_allowed. Now the URL is resolved against
    the controlled service of the process doing the request, and the demo APIs are restored."""
    st1 = state(tmp_path)
    c1 = client(st1)
    job = c1.post("/api/demo/run", json={"scenario": "rename", "llm": "none", "confirm": True}).json()
    while c1.get(f"/api/demo/jobs/{job['id']}").json()["status"] == "running":
        time.sleep(0.05)
    tool = c1.get("/api/test/tools").json()[0]["name"]
    st2 = state(tmp_path)                                    # same records, new process-level state, new port
    try:
        d = client(st2).get(f"/api/test/tools/{tool}/drift").json()
        assert d["available"] and d["state"] == "no_drift" and d["repair_enabled"], d
        assert st2.test_service.port != st1.test_service.port
        assert d["schema_url"].startswith(st2.test_service.url)          # fetched from THIS runtime's service
    finally:
        for s in (st1, st2):
            if s.test_service:
                s.test_service.stop()


def test_plain_http_to_a_real_host_is_still_refused_as_scheme_not_allowed(tmp_path):
    st = state(tmp_path, guard=GuardedHTTP((HOST,), min_interval_s=0))
    spec = __import__("adaptix.models", fromlist=["ToolSpec"]).ToolSpec(
        name="plain", base_url=f"http://{HOST}", path="/v1/users")
    d = st.engine.discover_detailed(spec)
    assert d.op is None and d.error_category == "egress_denied:scheme_not_allowed"


# ---------------------------------------------------------------- permitted vs unregistered hosts
def test_registered_host_allowed_unregistered_refused_without_sending(tmp_path):
    guard = FakeGuard()
    guard.install(HOST, make_app("/v1/users", {"user_id": "str"}))
    st = state(tmp_path, guard=guard)
    c = client(st)
    assert c.post("/api/tools", json={"name": "ok", "base_url": f"https://{HOST}", "path": "/v1/users"}).status_code == 201
    r = c.post("/api/tools", json={"name": "bad", "base_url": "https://not-approved.example", "path": "/v1/users"})
    assert r.status_code == 422 and "approved host" in r.json()["detail"]["detail"]
    assert all(HOST in u for _, u in guard.calls)


def _dns(monkeypatch, table):
    def fake(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
    monkeypatch.setattr(netguard.socket, "getaddrinfo", fake)


def _guard(handler, allowed=("api.example.com",)):
    g = GuardedHTTP(allowed, min_interval_s=0)
    g._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return g


def test_private_and_metadata_destinations_and_redirects_are_refused(monkeypatch):
    _dns(monkeypatch, {"api.example.com": "93.184.215.14", "intra.example.com": "10.1.2.3"})
    with pytest.raises(EgressDenied) as e:
        _guard(lambda r: httpx.Response(200), ("intra.example.com",)).get_json("https://intra.example.com/o.json", 10_000)
    assert e.value.category == "private_address"
    to_meta = lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    with pytest.raises(EgressDenied):
        _guard(to_meta).get_json("https://api.example.com/openapi.json", 10_000)


def test_non_json_and_oversized_schema_documents(monkeypatch):
    _dns(monkeypatch, {"api.example.com": "93.184.215.14"})
    html = lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>login</html>")
    assert _guard(html).get_json("https://api.example.com/openapi.json", 10_000) == (200, None)
    big = lambda r: httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"a":"' + b"x" * 5000 + b'"}')
    with pytest.raises(EgressDenied) as e:
        _guard(big).get_json("https://api.example.com/openapi.json", 1_000)
    assert e.value.category == "response_too_large"
    ok = lambda r: httpx.Response(200, headers={"content-type": "application/vnd.oai.openapi+json"}, json={"openapi": "3"})
    assert _guard(ok).get_json("https://api.example.com/openapi.json", 10_000) == (200, {"openapi": "3"})


# ---------------------------------------------------------------- malformed, missing, slow
class _Serve:
    """FakeGuard variant that answers the schema URL with a fixed response (test only)."""
    def __init__(self, status=200, doc=None, exc=None):
        self.status, self.doc, self.exc = status, doc, exc

    def request(self, method, url, **kw):
        raise EgressDenied("host_not_allowed", "not used")

    def get_json(self, url, max_bytes):
        if self.exc:
            raise self.exc
        return self.status, self.doc


@pytest.mark.parametrize("serve,category,state_", [
    (_Serve(doc={"openapi": "3.0.0", "paths": ["not", "a", "dict"]}), "invalid_schema", "invalid"),
    (_Serve(doc=None), "not_json", "invalid"),
    (_Serve(doc={"openapi": "3.0.0", "paths": {}}), "operation_not_in_schema", "invalid"),
    (_Serve(status=404), "http_404", "unavailable"),
    (_Serve(exc=httpx.ConnectTimeout("slow")), "timeout", "unavailable"),
    (_Serve(exc=EgressDenied("host_not_allowed", "x")), "egress_denied:host_not_allowed", "unavailable"),
])
def test_schema_failures_are_distinct_and_never_no_drift(tmp_path, serve, category, state_):
    guard = FakeGuard()
    guard.install(HOST, make_app("/v1/users", {"user_id": "str"}))
    st = state(tmp_path, guard=guard)
    c = client(st)
    c.post("/api/tools", json={"name": "t", "base_url": f"https://{HOST}", "path": "/v1/users"})
    st.engine.upstreams.guard = serve                       # the schema now fails in this specific way
    d = c.get("/api/tools/t/drift").json()
    assert d["available"] is False and d["error_category"] == category and d["state"] == state_
    assert d["repair_enabled"] is False and "report" not in d
    assert c.get("/api/tools/t/schema").json()["fields"]     # the registered snapshot is still inspectable


def test_unavailable_schema_cannot_trigger_a_repair(tmp_path):
    guard = FakeGuard()
    guard.install(HOST, make_app("/v1/users", {"user_id": "str"}))
    st = state(tmp_path, guard=guard)
    c = client(st)
    c.post("/api/tools", json={"name": "t", "base_url": f"https://{HOST}", "path": "/v1/users"})
    v2 = make_app("/v1/users", {"userId": "str"})
    v2.openapi_url = None
    v2.router.routes = [r for r in v2.router.routes if getattr(r, "path", "") != "/openapi.json"]
    guard.install(HOST, v2)                                  # the call breaks AND the schema is unreadable
    r = c.post("/api/repair/execute", json={"tool": "t", "arguments": {"user_id": "7"}}).json()
    inc = c.get(f"/api/incidents/{r['incident']}").json()
    assert r["status"] == "stopped" and inc["category"] == "schema_unavailable"
    assert inc["plan"] is None and inc["validation"] is None
    assert len(c.get("/api/tools/t/versions").json()) == 1   # nothing applied
