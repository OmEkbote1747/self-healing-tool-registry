"""Schema shapes beyond flat required JSON fields."""
import pytest
from fastapi.testclient import TestClient

from adaptix.cases import APPROVED_CONFIG
from adaptix.healer import SelfHealer, ToolRegistry
from adaptix.mock_api import make_app


def run(path, v1, v2, args, method="POST", **app_kw):
    client = TestClient(make_app(path, v2, method=app_kw.pop("app_method", method), **app_kw))
    reg = ToolRegistry()
    reg.register("tool", path, v1, method=method)
    return SelfHealer(reg, APPROVED_CONFIG, use_llm=False).call(client, "tool", args)


def test_rename_with_new_optional_field_leaves_it_out():
    out = run("/u", {"user_id": "str"}, {"userId": "str", "note": "str?"}, {"user_id": "7"})
    assert out.success and out.response["received"] == {"userId": "7", "note": None}


def test_enum_value_allowed_is_repaired():
    out = run("/u", {"user_id": "str", "role": "str"}, {"user_id": "str", "access_role": "enum[admin|member]"},
              {"user_id": "7", "role": "member"})
    assert out.success and out.response["received"]["access_role"] == "member"


def test_enum_value_not_allowed_stops():
    out = run("/u", {"user_id": "str", "role": "str"}, {"user_id": "str", "access_role": "enum[admin|member]"},
              {"user_id": "7", "role": "guest"})
    assert out.state == "stopped" and "not one of the values" in out.stop_reason


def test_array_field_rename():
    out = run("/t", {"tags": "list[str]", "user_id": "str"}, {"item_tags": "list[str]", "user_id": "str"},
              {"tags": ["a", "b"], "user_id": "7"})
    assert out.success and out.response["received"]["item_tags"] == ["a", "b"]


def test_get_query_param_rename():
    out = run("/orders", {"q:order_id": "str"}, {"q:orderId": "str"}, {"order_id": "A-1"}, method="GET")
    assert out.success and out.response["params"]["query"] == {"orderId": "A-1"}


def test_path_param_plus_body_rename_on_put():
    out = run("/users/{user_id}", {"p:user_id": "str", "email": "str"},
              {"p:user_id": "str", "emailAddress": "str"}, {"user_id": "42", "email": "a@b.co"}, method="PUT")
    assert out.success
    assert out.response["received"] == {"emailAddress": "a@b.co"}
    assert out.response["params"]["path"] == {"user_id": "42"}


@pytest.mark.parametrize("value,ok", [("2026-10-20", True), ("20/10/2026", False)])
def test_date_format(value, ok):
    out = run("/b", {"date": "str"}, {"booking_date": "date"}, {"date": value})
    assert out.success is ok
    if not ok:
        assert "ISO date" in out.stop_reason


def test_nullable_union_type_change():
    out = run("/u", {"user_id": "str", "age": "str"}, {"user_id": "str", "age": "int?"}, {"user_id": "7", "age": "30"})
    assert out.success and out.response["received"]["age"] == 30


def test_form_body_is_not_repaired():
    out = run("/u", {"user_id": "str"}, {"user_id": "str"}, {"user_id": "7"},
              content_type="application/x-www-form-urlencoded")
    assert out.state == "stopped" and "only JSON bodies" in out.stop_reason and out.calls == 1


def test_method_change_needs_a_human():
    out = run("/u", {"user_id": "str"}, {"user_id": "str"}, {"user_id": "7"}, app_method="PUT")
    assert out.state == "stopped" and "HTTP method" in out.stop_reason and out.calls == 1
