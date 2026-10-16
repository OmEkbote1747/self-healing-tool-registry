"""Classifier, validator and credential registry."""
import pytest
from pydantic import ValidationError

from adaptix.classify import classify_exception, classify_response
from adaptix.credentials import Credential, CredentialError, CredentialRegistry
from adaptix.models import LLMRepairPlan, Mapping, RepairPlan, ToolSpec
from adaptix.schema import FieldSpec, Operation
from adaptix.tooldef import ToolDef, apply_transform
from adaptix.validator import validate

FIELD_ERR = {"detail": [{"type": "missing", "loc": ["body", "userId"], "msg": "Field required"}]}


# ---------------------------------------------------------------- classification
@pytest.mark.parametrize("status,body,expected", [
    (401, {"detail": "API key expired"}, "credential_expired"),
    (401, {"detail": "API key revoked"}, "credential_revoked"),
    (401, {"detail": "missing API key"}, "authentication_missing"),
    (401, {"detail": "invalid API key"}, "credential_invalid"),
    (403, {"detail": "no"}, "authorization_failed"),
    (404, {}, "endpoint_not_found"),
    (405, {}, "method_changed"),
    (415, {}, "unsupported_change"),
    (429, {}, "rate_limited"),
    (503, {}, "server_error"),
    (422, {"detail": {"code": "insufficient_balance"}}, "business_rule"),
    (409, {"detail": "duplicate order"}, "business_rule"),
])
def test_status_classification(status, body, expected):
    assert classify_response(status, body, schema_changed=True).category == expected


def test_422_is_not_drift_when_the_schema_is_unchanged():
    assert classify_response(422, FIELD_ERR, schema_changed=False).category == "invalid_input"


def test_422_with_unavailable_schema_fails_closed():
    assert classify_response(422, FIELD_ERR, schema_changed=None).category == "schema_unavailable"


def test_422_is_drift_only_when_errors_touch_changed_fields():
    assert classify_response(422, FIELD_ERR, True, {"body:userId"}).category == "schema_drift"
    unrelated = {"detail": [{"type": "string_too_short", "loc": ["body", "email"], "msg": "too short"}]}
    assert classify_response(422, unrelated, True, {"body:userId"}).category == "invalid_input"


def test_transport_errors():
    assert classify_exception("timeout").category == "timeout"
    assert classify_exception("network").retryable


# ---------------------------------------------------------------- transforms
@pytest.mark.parametrize("value,t,ok", [("3", "to_int", True), ("3.5", "to_int", False), ("yes", "to_bool", False),
                                         ("2026-10-20", "iso_date", True), ("20/10/2026", "iso_date", False)])
def test_transforms_are_lossless(value, t, ok):
    if ok:
        apply_transform(value, t)
    else:
        with pytest.raises(ValueError):
            apply_transform(value, t)


# ---------------------------------------------------------------- validator: plans are untrusted
def _op(fields, headers=()):
    return Operation("POST", "/x", fields, list(headers), None, ["application/json"])


TD = ToolDef("t", 1, "POST", "/x", {"body:user_id": ["user_id", "none"]})
SPEC = ToolSpec(name="t", base_url="http://x", path="/x")
NEW = _op({"body:target_ref": FieldSpec(["str"], True, description="Reference of the target account (formerly user_id)")})


def _llm_plan(evidence, conf=0.95, source="user_id"):
    return RepairPlan(source="llm", diagnosis="d", confidence=conf,
                      mappings=[Mapping(target_field="body:target_ref", source_arg=source, evidence=evidence)])


def test_grounded_llm_plan_is_accepted():
    res, cand = validate(_llm_plan("Reference of the target account (formerly user_id)"), TD, SPEC, NEW,
                         {"user_id": "7"}, CredentialRegistry([]))
    assert res.accepted and cand.request_map == {"body:target_ref": ["user_id", "none"]}


def test_ungrounded_evidence_is_rejected():
    res, _ = validate(_llm_plan("the docs say this is the user id"), TD, SPEC, NEW, {"user_id": "7"}, CredentialRegistry([]))
    assert not res.accepted and any("verbatim" in r for r in res.rejected_reasons)


def test_low_confidence_llm_plan_is_rejected():
    res, _ = validate(_llm_plan("Reference of the target account (formerly user_id)", conf=0.5), TD, SPEC, NEW,
                      {"user_id": "7"}, CredentialRegistry([]))
    assert not res.accepted


def test_fabricated_source_is_rejected():
    res, _ = validate(_llm_plan("Reference of the target account (formerly user_id)", source="admin_override"),
                      TD, SPEC, NEW, {"user_id": "7"}, CredentialRegistry([]))
    assert not res.accepted


def test_model_output_cannot_carry_values_or_extra_operations():
    with pytest.raises(ValidationError):
        LLMRepairPlan.model_validate({"diagnosis": "x", "confidence": 0.9,
                                      "mappings": [{"target_field": "body:a", "source_arg": "b", "value": "admin"}]})
    with pytest.raises(ValidationError):
        LLMRepairPlan.model_validate({"diagnosis": "x", "confidence": 0.9, "run_shell": "rm -rf /"})
    with pytest.raises(ValidationError):
        LLMRepairPlan.model_validate({"diagnosis": "x", "confidence": 0.9,
                                      "mappings": [{"target_field": "body:a", "source_arg": "b", "transform": "eval"}]})


def test_unit_change_is_rejected_even_with_evidence():
    new = _op({"body:amount_cents": FieldSpec(["int"], True, description="Amount in cents (formerly amount)",
                                              renamed_from="amount")})
    td = ToolDef("t", 1, "POST", "/x", {"body:amount": ["amount", "none"]})
    plan = RepairPlan(source="rules", diagnosis="d", confidence=1.0, mappings=[
        Mapping(target_field="body:amount_cents", source_arg="amount", transform="to_int", evidence="x-renamed-from: amount")])
    res, _ = validate(plan, td, SPEC, new, {"amount": "20"}, CredentialRegistry([]))
    assert not res.accepted and any("unit" in r for r in res.rejected_reasons)


def test_enum_violation_is_rejected():
    new = _op({"body:role": FieldSpec(["str"], True, enum=["admin", "member"])})
    td = ToolDef("t", 1, "POST", "/x", {"body:role": ["role", "none"]})
    plan = RepairPlan(source="rules", diagnosis="d", confidence=1.0,
                      mappings=[Mapping(target_field="body:role", source_arg="role", evidence="unchanged field")])
    assert not validate(plan, td, SPEC, new, {"role": "guest"}, CredentialRegistry([]))[0].accepted


def test_header_requires_a_configured_permitted_credential():
    new = _op({"body:user_id": FieldSpec(["str"], True)}, headers=["x-api-key"])
    plan = RepairPlan(source="rules", diagnosis="d", confidence=1.0, header_sources={"x-api-key": "made-up-group"},
                      mappings=[Mapping(target_field="body:user_id", source_arg="user_id", evidence="unchanged field")])
    assert not validate(plan, TD, SPEC, new, {"user_id": "7"}, CredentialRegistry([]))[0].accepted


# ---------------------------------------------------------------- credentials
def _reg(**over):
    creds = [Credential("a", "crm", "crm", "A_ENV", "x-api-key", ["tool:t"]),
             Credential("b", "crm", "crm", "B_ENV", "x-api-key", ["tool:t"])]
    for c in creds:
        for k, v in over.items():
            setattr(c, k, v)
    return CredentialRegistry(creds, secrets={"a": "secret-value-aaaa", "b": "secret-value-bbbb"})


def test_missing_and_unpermitted_credentials():
    reg = _reg()
    with pytest.raises(CredentialError) as e:
        reg.select("billing", "tool:t")
    assert e.value.category == "missing_configuration"
    with pytest.raises(CredentialError) as e:
        reg.select("crm", "tool:other")
    assert e.value.category == "not_permitted"


@pytest.mark.parametrize("category,allowed", [("rate_limited", True), ("credential_expired", True),
                                               ("credential_invalid", False), ("authorization_failed", False)])
def test_failover_only_for_allowed_categories(category, allowed):
    reg = _reg()
    cred, _ = reg.select("crm", "tool:t")
    reg.report(cred.alias, category)
    alt = reg.failover(cred, category, "crm", "tool:t", {cred.alias})
    assert (alt is not None) is allowed
    if alt:
        assert alt[0].alias == "b" and alt[1] == "secret-value-bbbb"


def test_rejected_key_is_never_reused_or_modified():
    reg = _reg()
    reg.report("a", "credential_revoked")
    cred, value = reg.select("crm", "tool:t")
    assert cred.alias == "b" and value == "secret-value-bbbb"
    reg.report("b", "credential_invalid")
    with pytest.raises(CredentialError) as e:
        reg.select("crm", "tool:t")
    assert e.value.category == "no_valid_credential"


def test_status_never_contains_secret_values():
    text = str(_reg().status())
    assert "secret-value" not in text and "alias" in text


def test_schema_fingerprints_are_not_mistaken_for_secrets():
    from adaptix.security import redact
    out = redact({"old_signature": "abc123", "new_signature": "def456", "x-signature": "s3cr3t", "scorecard": {"n": 1},
                  "card_number": "4111"})
    assert out["old_signature"] == "abc123" and out["new_signature"] == "def456" and out["scorecard"] == {"n": 1}
    assert out["x-signature"] != "s3cr3t" and out["card_number"] != "4111"


def test_times_are_displayed_in_indian_standard_time():
    from adaptix.timefmt import ist, ist_time
    assert ist("2026-10-16T18:30:00+00:00") == "17 Oct 2026, 00:00:00 IST"          # date rolls over at IST midnight
    assert ist("2026-10-16T09:12:09.674+00:00", seconds=False) == "16 Oct 2026, 14:42 IST"
    assert ist_time("2026-10-16T09:12:09.674+00:00", millis=True) == "14:42:09.674 IST"
    assert ist("2026-10-16T09:12:09Z") == "16 Oct 2026, 14:42:09 IST"                  # 'Z' suffix
    assert ist("2026-10-16T09:12:09") == "16 Oct 2026, 14:42:09 IST"                   # naive = stored UTC
    assert ist(None) == "—" and ist("not a time") == "—"
