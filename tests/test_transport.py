"""Request handling against a real HTTP server (uvicorn on a local port)."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from adaptix.cases import APPROVED_CONFIG, build_cases
from adaptix.healer import SelfHealer, ToolRegistry
from adaptix.mock_api import make_app
from adaptix.transport import Transport

CASE = next(c for c in build_cases() if c.note == "user_id -> userId")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server():
    started = []

    def start(app):
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        started.append(srv)
        return f"http://127.0.0.1:{port}"

    yield start
    for srv in started:
        srv.should_exit = True


def _healer():
    reg = ToolRegistry()
    reg.register(CASE.tool, CASE.path, CASE.v1)
    return SelfHealer(reg, APPROVED_CONFIG, use_llm=False)


def test_timeout_is_not_replayed(server):
    # the API does the work, then answers too late: we must not send it again
    app = make_app(CASE.path, CASE.v1, faults={"delay": 1.0})
    url = server(app)
    out = _healer().call(Transport(base_url=url, timeout=0.3), CASE.tool, CASE.args)
    assert out.stopped and out.stop_reason.startswith("timeout")
    assert out.calls == 1
    time.sleep(1.2)
    assert app.state.effects == 1


def test_idempotency_key_deduplicates_a_replay(server):
    app = make_app(CASE.path, CASE.v1)
    url = server(app)
    t = Transport(base_url=url)
    for _ in range(2):
        r = t.send("POST", CASE.path, tool="x", json=CASE.args, idempotency_key="same-key")
        assert r.status_code == 200
    assert app.state.effects == 1


def test_repair_retry_reuses_the_idempotency_key(server):
    app = make_app(CASE.path, CASE.v2)
    url = server(app)
    out = _healer().call(Transport(base_url=url), CASE.tool, CASE.args)
    assert out.success and app.state.effects == 1


def test_server_errors_are_not_repaired_and_open_the_breaker():
    client = TestClient(make_app(CASE.path, CASE.v1, faults={"status": 503}))
    t = Transport(client=client, breaker_threshold=3, breaker_cooldown=60)
    healer = _healer()
    reasons = [healer.call(t, CASE.tool, CASE.args).stop_reason for _ in range(4)]
    assert "outcome unknown, not replayed" in reasons[0]
    assert reasons[3].startswith("circuit_open")


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_non_schema_errors_stop_without_repair(status):
    client = TestClient(make_app(CASE.path, CASE.v1, faults={"status": status}))
    out = _healer().call(client, CASE.tool, CASE.args)
    assert out.stopped and out.calls == 1 and out.attempts == 0


def test_rate_limit():
    client = TestClient(make_app(CASE.path, CASE.v1))
    t = Transport(client=client, rate_per_sec=0.001, burst=1)
    healer = _healer()
    assert healer.call(t, CASE.tool, CASE.args).success
    assert healer.call(t, CASE.tool, CASE.args).stop_reason.startswith("rate_limited")


def test_retry_budget():
    client = TestClient(make_app(CASE.path, CASE.v2))
    t = Transport(client=client, retry_budget=0.0, min_retries=0)
    out = _healer().call(t, CASE.tool, CASE.args)
    assert out.stop_reason.startswith("retry_budget")
