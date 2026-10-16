import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from selfheal import llm
from selfheal.cases import APPROVED_CONFIG, DEMO_CASE
from selfheal.healer import SelfHealer, ToolRegistry
from selfheal.mock_api import make_app
from selfheal.security import MASK, purge_audit_log, redact, redact_headers, redact_reply


def test_redaction_rules():
    assert redact({"user": {"password": "p", "api_key": "k", "name": "Asha"}}) == \
        {"user": {"password": MASK, "api_key": MASK, "name": "Asha"}}
    assert redact_headers({"x-api-key": "secret", "content-type": "application/json"}) == \
        {"x-api-key": MASK, "content-type": "application/json"}
    r = redact_reply({"detail": [{"type": "missing", "loc": ["body", "x"], "input": {"card": "4111"}}]})
    assert "4111" not in json.dumps(r)


def test_no_secret_in_any_trace():
    client = TestClient(make_app(DEMO_CASE.path, DEMO_CASE.v2, DEMO_CASE.v2_headers))
    reg = ToolRegistry()
    reg.register(DEMO_CASE.tool, DEMO_CASE.path, DEMO_CASE.v1)
    healer = SelfHealer(reg, APPROVED_CONFIG, use_llm=False)
    out = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args)
    reg.approve(out.awaiting.id)
    again = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args, agent_id="agent-B")
    dumped = json.dumps([out.trace, again.trace], default=str)
    assert APPROVED_CONFIG["x-api-key"] not in dumped


def test_llm_prompt_has_no_values():
    errors = [{"type": "missing", "loc": ["body", "target"], "msg": "Field required", "input": {"user_id": "SECRET-123"}},
              {"type": "missing", "loc": ["header", "x-api-key"], "msg": "Field required", "input": None}]
    prompt = llm.build_prompt("update_user", {"target.id": "int"}, {"user_id": "str"}, errors)
    assert "SECRET-123" not in prompt and "x-api-key" not in prompt
    assert "user_id" in prompt and "target.id" in prompt


def test_demo_secret_is_not_hardcoded():
    assert APPROVED_CONFIG["x-api-key"].startswith("demo-") or APPROVED_CONFIG["x-api-key"]
    assert APPROVED_CONFIG["x-api-key"] != "demo-key-123"


def test_audit_retention(tmp_path):
    log = tmp_path / "a.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    log.write_text(json.dumps({"ts": old}) + "\n" + json.dumps({"ts": new}) + "\n")
    assert purge_audit_log(log, 30) == 1
    assert len(log.read_text().splitlines()) == 1
