"""LIVE test against a genuine external API: Swagger Petstore (https://petstore3.swagger.io), a public demo
service published by Swagger for exactly this kind of testing. Read-only GET requests only, through the real
SSRF guard. Opt-in, because it needs the internet:

    ADAPTIX_LIVE_API_TESTS=1 pytest -q tests/test_live_api.py
"""
import os

import pytest
from fastapi.testclient import TestClient

from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.storage import Storage

pytestmark = pytest.mark.skipif(os.environ.get("ADAPTIX_LIVE_API_TESTS") != "1",
                                reason="live test: set ADAPTIX_LIVE_API_TESTS=1 (needs internet access)")
HOST = "petstore3.swagger.io"
TOKEN = "live-test-token-000000000000"


@pytest.fixture
def client(tmp_path):
    st = State(Settings(admin_token=TOKEN, allowed_hosts=(HOST,), host_min_interval_s=1.0),
               storage=Storage(tmp_path / "live.db"), creds=CredentialRegistry([], allow_simulated=False),
               llm=NoProvider(), start_monitor=False)
    return TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})


def test_register_check_and_call_a_real_api(client):
    r = client.post("/api/tools", json={"name": "petstore_find_by_status", "base_url": f"https://{HOST}/api/v3",
                                        "path": "/pet/findByStatus", "method": "GET",
                                        "provider": "Swagger Petstore (public demo API, swagger.io)",
                                        "description": "Finds pets by status", "probe_arguments": {"status": "available"}})
    assert r.status_code == 201, r.text
    c = client.post("/api/monitor/petstore_find_by_status/check").json()
    assert c["schema_status"] == "verified" and c["connectivity"] == "reachable", c
    assert c["drift"] is False and c["drift_summary"] == "No schema drift detected"
    assert c["verification"] == "verified", c
    call = client.post("/api/repair/execute", json={"tool": "petstore_find_by_status",
                                                    "arguments": {"status": "sold"}}).json()
    assert call["status"] == "ok" and isinstance(call["result"], list)


def test_unapproved_real_host_is_refused(client):
    r = client.post("/api/tools", json={"name": "x", "base_url": "https://example.com", "path": "/"})
    assert r.status_code == 422 and "approved host" in r.json()["detail"]["detail"]
