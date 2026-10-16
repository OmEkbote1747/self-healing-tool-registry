"""The Changes table backend: row building, filtering, sorting, pagination, labels and redaction."""
import pytest
from fastapi.testclient import TestClient

from adaptix import changes
from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.credentials import CredentialRegistry
from adaptix.llm import NoProvider
from adaptix.storage import Storage

TOKEN = "changes-token-00000000000000000"


def inc(i: int, **kw) -> dict:
    base = {"id": f"inc_{i:03d}", "tool": f"tool_{i % 3}", "started_at": f"2026-10-{10 + i % 5:02d}T10:00:00+00:00",
            "status": "stopped", "drift": {"summary": "1 renamed", "compatibility": "repairable",
                                           "old_signature": "a" * 16, "new_signature": "b" * 16,
                                           "request_changes": [{"kind": "renamed", "location": "body:userId",
                                                                "old": "body:user_id", "new": "str", "detail": "x"}],
                                           "response_changes": []},
            "timeline": [{"stage": "diff", "status": "info", "title": "d", "ts": "t"}]}
    base.update(kw)
    return base


def test_states_never_claim_more_than_recorded():
    assert changes.rows_from_incident(inc(1), "SIMULATION")[0]["repair_status"] == "Analyzed"
    assert changes.rows_from_incident(inc(1, plan={"mappings": []}), "SIMULATION")[0]["repair_status"] == "Proposed"
    rej = inc(1, plan={"mappings": []}, validation={"accepted": False, "checks": []})
    assert changes.rows_from_incident(rej, "SIMULATION")[0]["repair_status"] == "Requires review"
    ok = inc(1, status="recovered", plan={"mappings": []}, validation={"accepted": True, "checks": []},
             verification={"ok": True, "checks": []})
    r = changes.rows_from_incident(ok, "REAL_API_TEST")[0]
    assert r["repair_status"] == "Verified" and r["verification"] == "Verified" and r["environment_label"] == "REAL API TEST"
    applied_unverified = inc(1, plan={"mappings": []}, validation={"accepted": True, "checks": []},
                             timeline=[{"stage": "apply", "status": "ok", "title": "a", "ts": "t"}])
    assert changes.rows_from_incident(applied_unverified, "SIMULATION")[0]["repair_status"] == "Applied"
    rb = inc(1, status="rolled_back", rolled_back=True, verification={"ok": False, "checks": []})
    r = changes.rows_from_incident(rb, "SIMULATION")[0]
    assert r["repair_status"] == "Rolled back" and r["verification"] == "Failed"


def test_missing_fields_and_long_nested_values_do_not_break_rows():
    weird = inc(2, drift={"request_changes": [{"kind": "added", "location": "body:" + "x" * 300, "detail": "required"}],
                          "response_changes": [{"kind": "moved", "location": "response:result.status",
                                                "old": "response:status", "new": None}]},
                timeline=[], started_at=None)
    rows = changes.rows_from_incident(weird, "SIMULATION")
    assert [r["change_type"] for r in rows] == ["Required field added", "Nested schema changed"]
    assert rows[0]["previous"] is None and rows[0]["timestamp"] is None and rows[1]["new"] is None
    assert changes.rows_from_incident({"id": "i", "tool": "t"}, "SIMULATION") == []          # no drift: no rows


def test_ambiguous_change_is_labelled():
    a = inc(3, drift={"request_changes": [{"kind": "added", "location": "body:from", "detail": "required"}],
                      "response_changes": []},
            plan={"mappings": [], "compatibility_risks": ["sender_id -> from: two arguments match almost equally"]})
    assert changes.rows_from_incident(a, "SIMULATION")[0]["change_type"] == "Ambiguous change"


def test_filter_sort_paginate_and_counts():
    rows = []
    for i in range(30):
        env = ["SIMULATION", "REAL_API_TEST", "LIVE_API"][i % 3]
        rows += changes.rows_from_incident(inc(i), env)
    page = changes.query(rows, page=2, page_size=10)
    assert page["total"] == 30 and page["pages"] == 3 and len(page["rows"]) == 10 and page["page"] == 2
    sim = changes.query(rows, environment=["SIMULATION"], page_size=200)
    assert sim["total"] == 10 and {r["environment"] for r in sim["rows"]} == {"SIMULATION"}
    assert sim["unfiltered_total"] == 30
    q = changes.query(rows, q="inc_007")
    assert q["total"] == 1 and q["rows"][0]["incident_id"] == "inc_007"
    assert changes.query(rows, q="body:userid")["total"] == 30                     # path search, case-insensitive
    dated = changes.query(rows, since="2026-10-12", until="2026-10-12", page_size=200)
    assert dated["total"] == 6 and all(r["timestamp"].startswith("2026-10-12") for r in dated["rows"])
    asc = changes.query(rows, sort="tool", order="asc", page_size=200)["rows"]
    assert [r["tool"] for r in asc] == sorted(r["tool"] for r in asc)
    empty = changes.query(rows, q="no-such-thing")
    assert empty["total"] == 0 and empty["rows"] == [] and empty["pages"] == 1
    assert changes.query(rows, page=99, page_size=10)["page"] == 3                 # clamped, never past the end


@pytest.fixture
def client(tmp_path):
    st = State(Settings(admin_token=TOKEN), storage=Storage(tmp_path / "p.db"),
               creds=CredentialRegistry([], allow_simulated=False), llm=NoProvider(), sleep=lambda s: None,
               start_monitor=False)
    return TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})


def test_api_changes_from_real_records_with_details_and_redaction(client):
    client.post("/api/simulations", json={"scenario": "documented-nested-rename", "mode": "adaptix"})
    client.post("/api/simulations", json={"scenario": "ambiguous-rename", "mode": "adaptix"})
    res = client.get("/api/changes", params={"page_size": 200}).json()
    assert res["total"] == len(res["rows"]) > 0 and {r["environment"] for r in res["rows"]} == {"SIMULATION"}
    row = next(r for r in res["rows"] if r["field_path"] == "body:target.id")
    assert row["change_type"] in ("Field renamed", "Nested schema changed") and row["repair_status"] == "Verified"
    d = row["details"]
    assert d["mapping"]["source_arg"] == "user_id" and d["validation_accepted"] is True
    assert d["timeline"] and d["request"]["status"] == 422 and d["final_response"]["status"] == 200
    assert d["final_response"]["headers"]["x-api-key"] == "***"            # credential header redacted
    sim_creds = client.app.state.adaptix.sim_creds
    blob = client.get("/api/changes", params={"page_size": 200}).text
    assert all(v not in blob for v in sim_creds._secrets.values())
    only_live = client.get("/api/changes", params={"environment": "LIVE_API"}).json()
    assert only_live["total"] == 0 and only_live["rows"] == []
    rr = client.get("/api/changes", params={"repair_status": "Requires review", "page_size": 200}).json()
    assert rr["total"] >= 1 and all(r["repair_status"] == "Requires review" for r in rr["rows"])
