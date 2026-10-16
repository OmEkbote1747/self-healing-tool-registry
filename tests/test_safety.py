import pytest
from fastapi.testclient import TestClient

from adaptix.bench import rates, run_benchmark
from adaptix.cases import APPROVED_CONFIG, DEMO_CASE
from adaptix.healer import ConflictError, SelfHealer, ToolRegistry
from adaptix.mock_api import make_app
from adaptix.policy import PolicySet, RepairPolicy


@pytest.mark.parametrize("src,dst,score,expected", [
    ("order_id", "orderId", 100, "auto"),
    ("order_id", "order.id", 100, "auto"),
    ("channel_id", "chan_id", 82, "auto"),          # abbreviation of the same entity
    ("channel_id", "thread_id", 80, "approval"),     # different entity, even if the score were high
    ("amount", "amount_cents", 100, "approval"),     # unit change
    ("user_id", "uid", 67, "approval"),              # medium confidence
    ("email", "contact.phone", 12, "block"),         # too different to even propose
])
def test_policy_assess(src, dst, score, expected):
    assert RepairPolicy().assess(src, dst, score)[0] == expected


@pytest.fixture(params=["memory", "sqlite"], autouse=True)
def registry_kind(request, tmp_path):
    global _KIND, _DB
    _KIND, _DB = request.param, tmp_path / "reg.db"
    yield request.param


def _new_registry():
    if _KIND == "sqlite":
        from adaptix.store import SqliteRegistry
        return SqliteRegistry(_DB, tenant="t1")
    return ToolRegistry()


def _setup(case=DEMO_CASE, policies=None):
    client = TestClient(make_app(case.path, case.v2, case.v2_headers))
    reg = _new_registry()
    reg.register(case.tool, case.path, case.v1)
    return client, reg, SelfHealer(reg, APPROVED_CONFIG, use_llm=False, policies=policies)


def test_ambiguous_repair_is_held_and_nothing_is_sent():
    client, reg, healer = _setup()
    out = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args)
    assert out.state == "awaiting_approval"
    assert out.calls == 1                 # only the original failing call went out
    assert reg.current(DEMO_CASE.tool).version == 1


def test_approve_then_retry_succeeds():
    client, reg, healer = _setup()
    out = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args)
    td = reg.approve(out.awaiting.id, by="test")
    assert td.status == "active" and td.origin == "approved"
    again = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args, agent_id="agent-B")
    assert again.success and again.calls == 1
    assert again.response["received"] == DEMO_CASE.expected_body


def test_stale_proposal_is_refused():
    client, reg, healer = _setup()
    first = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args)
    second = healer.call(client, DEMO_CASE.tool, DEMO_CASE.args)
    reg.approve(first.awaiting.id)
    with pytest.raises(ConflictError):
        reg.approve(second.awaiting.id)


def test_tool_policy_can_require_approval_for_everything():
    from adaptix.cases import build_cases
    case = next(c for c in build_cases() if c.note == "user_id -> userId")
    client, reg, healer = _setup(case, PolicySet(per_tool={case.tool: {"auto_repair": False}}))
    out = healer.call(client, case.tool, case.args)
    assert out.state == "awaiting_approval"
    assert any("tool policy" in r for r in out.awaiting.reasons)


def test_canary_is_private_until_promoted():
    from adaptix.cases import build_cases
    case = next(c for c in build_cases() if c.note == "user_id -> userId")
    client, reg, healer = _setup(case)
    a = healer.call(client, case.tool, case.args, agent_id="agent-A")
    assert a.success and reg.current(case.tool, "agent-A").status == "canary"
    assert reg.current(case.tool, "agent-B").version == 1      # B does not see the canary yet
    for _ in range(reg.promote_after - 1):
        healer.call(client, case.tool, case.args, agent_id="agent-A")
    assert reg.current(case.tool, "agent-B").status == "active"
    assert reg.current(case.tool, "agent-B").version == 2


def test_failing_canary_is_rolled_back():
    from adaptix.cases import build_cases
    case = next(c for c in build_cases() if c.note == "user_id -> userId")
    client, reg, healer = _setup(case)
    healer.call(client, case.tool, case.args, agent_id="agent-A")
    canary_version = reg.current(case.tool, "agent-A").version
    other = TestClient(make_app(case.path, {"user_identifier_v3": "str", "email": "str"}, []))
    healer.call(other, case.tool, case.args, agent_id="agent-A")
    status = {v.version: v.status for v in reg.versions[case.tool]}
    assert status[canary_version] == "rolled_back"


def test_benchmark_has_no_false_repairs():
    for suite in ("planned", "stress", "adversarial"):
        n = rates(run_benchmark(suite=suite))
        assert n["false_repairs"] == 0 and n["unsafe"] == 0, suite
        assert n["safe_stop_rate"] == 100.0, suite
    assert rates(run_benchmark())["zero_touch_rate"] > 90


def test_greedy_baseline_makes_false_repairs():
    # the stronger baseline recovers more often but sends wrong requests - that is the point of the comparison
    n = rates(run_benchmark(suite="adversarial"), agent="greedy")
    assert n["false_repairs"] > 0
