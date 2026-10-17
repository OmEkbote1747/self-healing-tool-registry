"""Manual drift demo on the controlled test service (REAL_API_TEST): introduce one change -> ADAPTIX recovers.

The operator picks a scenario and confirms. One background job then:
  1 prepares a known-good tool against schema v1 of a scenario API served by the controlled test service
  2 runs an internal baseline check (the original call succeeds and is verified) - a timeline step, not a card
  3 introduces the selected change through the service's admin API (schema v2)
  4 makes the SAME call; ADAPTIX detects the failure and recovers automatically: classify, read the
    authoritative schema, diff, propose, validate, apply, retry, verify, roll back if needed
Every step is streamed with its timestamp and elapsed time so the UI can show live progress.
Nothing here touches a third-party API: the service is ADAPTIX's own server on 127.0.0.1, labelled REAL_API_TEST.
"""
from __future__ import annotations

import threading
import time
import uuid

from . import evidence as evidence_mod
from . import realtest
from . import simulation as sim
from .llm import NoProvider, is_live
from .models import ToolSpec, now_iso

SCENARIOS = {
    "rename": {"title": "Rename a field", "scenario": "rename-param", "change": "user_id becomes userId"},
    "type": {"title": "Change a field type", "scenario": "safe-type-change",
             "change": 'quantity changes from string to integer ("3" -> 3)'},
    "required": {"title": "Add a required field", "scenario": "new-required-field",
                 "change": "a required 'reason' field is added that the caller never supplied"},
    "remove": {"title": "Remove a field", "scenario": "removed-field",
               "change": "the 'nickname' field the caller sends is removed (and silently ignored)"},
    "prose": {"title": "Rename documented only in prose (needs Gemini)", "scenario": "window-rename-llm",
              "change": "start_date/end_date become window_start/window_end, documented only in descriptions"},
    "orders": {"title": "Orders API: customer_id -> client_id (Gemini proposes the repair)", "scenario": None,
               "change": "the orders API v2 renames customer_id to client_id; the stored order is read back"},
}
PHASE = {"request": "Call", "classify": "Diagnosis", "discover": "Schema retrieval", "diff": "Drift detection",
         "plan": "Repair proposal", "validate": "Validation", "apply": "Patch applied", "verify": "Verification",
         "rollback": "Rollback", "outcome": "Outcome", "credential": "Credentials", "retry": "Retry"}


class DemoError(RuntimeError):
    pass


def catalog(state) -> list[dict]:
    live = is_live(state.llm)
    out = []
    for key, d in SCENARIOS.items():
        s = sim.BY_ID.get(d["scenario"]) if d["scenario"] else None
        needs = key in ("prose", "orders")
        expected = (s.expect if s else "recovered")
        out.append({"key": key, "title": d["title"], "change": d["change"], "needs_gemini": needs,
                    "available": live or not needs, "expected": expected if (live or not needs) else "stopped",
                    "description": s.description if s else realtest.__doc__.split("\n")[0]})
    return out


class Jobs:
    """In-process registry of demo runs (one at a time), polled by the UI for live progress."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}

    def get(self, job_id: str) -> dict | None:
        j = self.jobs.get(job_id)
        if j is None:
            return None
        with self.lock:
            return {**j, "events": list(j["events"]),
                    "elapsed_ms": round(((j["finished"] or time.perf_counter()) - j["t0"]) * 1000, 1)}

    def running(self) -> dict | None:
        return next((j for j in self.jobs.values() if j["status"] == "running"), None)


def _event(job, phase: str, status: str, title: str):
    with_lock = job["_lock"]
    with with_lock:
        job["events"].append({"ts": now_iso(), "elapsed_ms": round((time.perf_counter() - job["t0"]) * 1000, 1),
                              "phase": phase, "status": status, "title": title})


def start(state, key: str, llm: str) -> dict:
    if key not in SCENARIOS:
        raise DemoError(f"unknown scenario {key}")
    if llm not in ("none", "gemini"):
        raise DemoError("REAL_API_TEST uses rules only or real Gemini (never the mock)")
    if (llm == "gemini" or key in ("prose", "orders")) and not is_live(state.llm):
        raise DemoError("Gemini is not configured on this server (set GEMINI_API_KEY); nothing was started")
    jobs: Jobs = state.demo_jobs
    with jobs.lock:
        if jobs.running():
            raise DemoError("another demo run is in progress")
        job = {"id": "demo_" + uuid.uuid4().hex[:10], "scenario": key, "title": SCENARIOS[key]["title"],
               "llm": "gemini" if key in ("prose", "orders") else llm, "status": "running", "events": [],
               "result": None, "error": None, "started_at": now_iso(), "t0": time.perf_counter(), "finished": None,
               "environment": "REAL_API_TEST", "_lock": threading.Lock()}
        jobs.jobs[job["id"]] = job
    threading.Thread(target=_run, args=(state, job), name=f"adaptix-{job['id']}", daemon=True).start()
    return public(jobs.get(job["id"]))


def public(j: dict) -> dict:
    return {k: v for k, v in j.items() if not k.startswith("_") and k not in ("t0", "finished")}


def _run(state, job: dict):
    try:
        with state.test_lock:
            if job["scenario"] == "orders":
                _run_orders(state, job)
            else:
                _run_scenario(state, job)
    except Exception as e:                                       # recorded honestly, never swallowed
        job["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        _event(job, "Outcome", "fail", "The demo run failed: " + job["error"])
        job["status"] = "error"
    finally:
        state.test_engine.set_llm(NoProvider())
        state.test_engine.llm_required = False
        job["finished"] = time.perf_counter()
        if job["status"] == "running":
            job["status"] = "done"


def _observer(job):
    def cb(step):
        _event(job, PHASE.get(step.stage, step.stage), step.status, step.title)
    return cb


def _run_scenario(state, job: dict):
    d = SCENARIOS[job["scenario"]]
    s = sim.BY_ID[d["scenario"]]
    eng = state.test_engine
    svc = state.test_service_started()
    target = sim.ServiceTarget(svc)
    eng.set_llm(state.llm if job["llm"] == "gemini" else NoProvider())
    eng.llm_required = job["llm"] == "gemini"         # Gemini chosen: Gemini proposes the repair, rules cross-check
    name = f"demo-{job['scenario']}-{uuid.uuid4().hex[:6]}"
    _event(job, "Setup", "info", f"Preparing a known-good tool against schema v1 on the controlled service ({svc.url})")
    target.install(name, s, "v1")
    eng.register(ToolSpec(name=name, base_url=target.base_url(name), path=s.path, method=s.method,
                          description=s.description, provider="ADAPTIX controlled test service"))
    base = eng.execute(name, dict(s.args), scenario=f"demo:{job['scenario']}:baseline")
    if base["status"] != "ok":
        _event(job, "Baseline check", "fail", f"Baseline call did not succeed ({base['status']}); not introducing a change")
        job["result"] = {"status": "blocked", "reason": "baseline check failed"}
        return
    _event(job, "Baseline check", "ok", "Original call succeeded and was verified on schema v1")
    target.install(name, s, "v2")
    _event(job, "Change introduced", "warn", f"Schema v2 is live: {d['change']}")
    _event(job, "Call", "info", "Making the same call again; ADAPTIX takes over automatically if it fails")
    res = eng.execute(name, dict(s.args), scenario=f"demo:{job['scenario']}", observer=_observer(job))
    inc = eng.storage.incident(res["incident"]) if res.get("incident") else None
    expected = sim.expected_outcome(s, eng, True)
    ev = evidence_mod.build(eng, inc.model_dump(mode="json") if inc else None, run_id=job["id"],
                            scenario=f"demo:{job['scenario']}", tool=name, expected=expected,
                            expected_category=s.expect_category, result=res, extra={"demo_events": job["events"]})
    outcome = "recovered" if res["status"] in ("ok", "recovered") else res["status"]
    job["result"] = {"status": outcome, "reason": res.get("reason"), "incident": res.get("incident"), "tool": name,
                     "tool_version": res.get("tool_version"), "evidence_id": ev["evidence_id"], "expected": expected,
                     "as_expected": outcome == expected, "recovery_ms": res.get("duration_ms"),
                     "llm": sim.llm_label(inc.llm if inc else None), "repair_by": sim.repair_by(inc)}


def _run_orders(state, job: dict):
    steps: list = []
    seen = 0
    _event(job, "Setup", "info", "Checking the controlled test service and sending one minimal real Gemini request")

    def pump():
        nonlocal seen
        for s in steps[seen:]:
            _event(job, s["name"].replace("_", " ").capitalize(), "ok" if s["status"] == "ok" else "fail", s["title"])
        seen = len(steps)
    r = realtest.run_orders_demo(state, None, steps=steps, observer=_observer(job), on_step=pump)
    pump()
    if r.get("error"):
        job["result"] = {"status": "blocked", "reason": r["error"]}
        return
    job["result"] = {"status": r["outcome"], "reason": "verified recovery" if r.get("verified_recovery") else r["outcome"],
                     "incident": r.get("incident"), "evidence_id": r.get("evidence_id"), "expected": "recovered",
                     "as_expected": r["outcome"] == "recovered", "verified": r.get("verified_recovery"),
                     "llm": {"label": "REAL Gemini request"}}


def reset(state) -> dict:
    """Return the controlled demo to a known-good state: REAL_API_TEST records cleared, service back to v1."""
    with state.test_lock:
        state.test_storage.reset()
        state.test_storage.set_meta("svc_apps", "{}")
        if state.test_service and state.test_service.running():
            state.test_service.set_orders("v1", "normal")
            state.test_service.clear_apps()
    return {"reset": True, "scope": "REAL_API_TEST records and the controlled test service"}
