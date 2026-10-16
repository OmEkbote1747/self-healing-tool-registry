"""The middleware API end to end: auth, tenants, repairs, approvals, policies, OpenAI adapter."""
import json

import pytest
from fastapi.testclient import TestClient

from adaptix.client import SchemaHealClient
from adaptix.mock_api import make_app
from adaptix.service import Principal, create_app
from adaptix.transport import Transport

KEYS = {"a-agent": Principal("acme", "agent"), "a-rev": Principal("acme", "reviewer"),
        "g-agent": Principal("globex", "agent")}


@pytest.fixture
def env(tmp_path):
    upstreams = {}

    def factory(tenant, base_url):
        return Transport(client=TestClient(upstreams[base_url]))

    svc = TestClient(create_app(str(tmp_path / "svc.db"), KEYS, factory))
    agent = SchemaHealClient("", "a-agent", client=svc)
    rev = SchemaHealClient("", "a-rev", client=svc)
    return svc, upstreams, agent, rev


def test_auth_and_roles(env):
    svc, upstreams, agent, rev = env
    assert svc.get("/v1/tools").status_code == 401
    assert svc.get("/v1/tools", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert svc.get("/v1/repairs", headers={"Authorization": "Bearer a-agent"}).status_code == 403
    assert svc.get("/v1/repairs", headers={"Authorization": "Bearer a-rev"}).status_code == 200


def test_tenants_cannot_see_each_other(env):
    svc, upstreams, agent, rev = env
    upstreams["http://up"] = make_app("/u", {"user_id": "str"})
    agent.register_tool("update_user", "http://up", "/u", {"user_id": "str"})
    other = svc.get("/v1/tools", headers={"Authorization": "Bearer g-agent"}).json()
    assert other == []
    r = svc.post("/v1/tools/update_user/call", headers={"Authorization": "Bearer g-agent"},
                 json={"arguments": {"user_id": "1"}})
    assert r.status_code == 404


def test_safe_drift_is_repaired_through_the_api(env):
    svc, upstreams, agent, rev = env
    upstreams["http://up"] = make_app("/u", {"userId": "str", "email": "str"})
    agent.register_tool("update_user", "http://up", "/u", {"user_id": "str", "email": "str"})
    res = agent.call("update_user", {"user_id": "7", "email": "a@b.co"})
    assert res["status"] == "ok" and res["repaired"] and res["result"]["received"]["userId"] == "7"


def test_ambiguous_drift_waits_for_a_reviewer(env):
    svc, upstreams, agent, rev = env
    upstreams["http://up"] = make_app("/u", {"target.id": "int"}, ["x-api-key"])
    agent.register_tool("update_user", "http://up", "/u", {"user_id": "str"})
    res = agent.call("update_user", {"user_id": "123"})
    assert res["status"] == "awaiting_approval" and res["proposal_id"]
    with pytest.raises(Exception):
        agent.approve(res["proposal_id"])                 # agents cannot approve
    assert [p["id"] for p in rev.pending_repairs()] == [res["proposal_id"]]
    rev.approve(res["proposal_id"])
    again = agent.call("update_user", {"user_id": "123"})
    assert again["status"] == "ok" and again["result"]["received"] == {"target": {"id": 123}}


def test_policy_set_through_api_requires_approval(env):
    svc, upstreams, agent, rev = env
    upstreams["http://up"] = make_app("/pay", {"paymentId": "str"})
    agent.register_tool("refund", "http://up", "/pay", {"payment_id": "str"})
    rev.set_policy("refund", auto_repair=False)
    assert agent.call("refund", {"payment_id": "p1"})["status"] == "awaiting_approval"


def test_openai_adapter(env):
    svc, upstreams, agent, rev = env
    upstreams["http://up"] = make_app("/u", {"userId": "str", "tags": "list[str]?"})
    agent.register_tool("update_user", "http://up", "/u", {"user_id": "str", "tags": "list[str]?",
                                                           "role": "enum[admin|member]?"}, description="Update a user")
    tools = agent.openai_tools()
    fn = tools[0]["function"]
    assert fn["name"] == "update_user" and fn["parameters"]["required"] == ["user_id"]
    assert fn["parameters"]["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert fn["parameters"]["properties"]["role"]["enum"] == ["admin", "member"]
    msgs = agent.handle_tool_calls([
        {"id": "call_1", "type": "function", "function": {"name": "update_user", "arguments": '{"user_id": "9"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}},
        {"id": "call_3", "type": "function", "function": {"name": "update_user", "arguments": "not json"}}])
    assert [m["tool_call_id"] for m in msgs] == ["call_1", "call_2", "call_3"]
    assert json.loads(msgs[0]["content"])["status"] == "ok"
    assert json.loads(msgs[1]["content"])["status"] == "stopped"
    assert "not valid JSON" in json.loads(msgs[2]["content"])["reason"]


def test_traces_returned_by_api_are_redacted(env):
    svc, upstreams, agent, rev = env
    upstreams["http://up"] = make_app("/u", {"userId": "str"}, ["x-api-key"])
    agent.register_tool("update_user", "http://up", "/u", {"user_id": "str"})
    res = agent.call("update_user", {"user_id": "7"})
    from adaptix.cases import APPROVED_CONFIG
    assert APPROVED_CONFIG["x-api-key"] not in json.dumps(res)
