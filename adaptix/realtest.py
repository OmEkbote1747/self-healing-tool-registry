"""REAL_API_TEST: reproducible end-to-end recoveries against the controlled test service over real HTTP.

run_orders_demo performs the 12-step demonstration:
  1  preflight: the test service answers over HTTP and Gemini answers a real minimal request
  2  register the tool against schema v1 and complete the task; record request, response, schema version and
     the independently read-back business outcome (GET /orders/{id})
  3  switch the service to v2 (customer_id -> client_id) through its admin API
  4  send the ORIGINAL request unchanged and record that it now fails, and why
  5-11 run the ADAPTIX pipeline with the LLM required (real Gemini proposes, the validator decides, the registry
     is versioned, the call is retried with the same idempotency key and the API's answer is verified), then
     read the order back independently to confirm the intended business result
  12 if the business result is not confirmed: roll the registry back to the last known-good version and
     report failure (the order request is not re-sent)

Nothing falls back to a mock. If the service or Gemini is unavailable, the run stops with an actionable error.
"""
from __future__ import annotations

import time
import uuid

from . import evidence as evidence_mod
from . import simulation as sim
from .classify import classify_response
from .diff import diff, request_conflicts, sent_locations
from .llm import is_live
from .models import ToolSpec, now_iso

DEMO_ARGS = {"customer_id": "cust-7781", "sku": "SKU-200", "quantity": 3}
MATRIX = [  # (scenario id, change type shown to judges)
    ("rename-param", "Field rename (name evidence)"),
    ("documented-nested-rename", "Field rename into nested object (documented)"),
    ("window-rename-llm", "Field rename documented only in prose (LLM)"),
    ("safe-type-change", "Type change - lossless conversion"),
    ("unsafe-type-change", "Type change - lossy, must be rejected"),
    ("new-required-field", "Newly required field without a trusted value"),
    ("removed-field", "Removed field the caller still sends"),
    ("nested-move", "Request fields moved into a nested object"),
    ("nested-response-move", "Response shape change (nested)"),
    ("ambiguous-rename", "Ambiguous rename - must not guess"),
    ("credential-invalid", "Authentication failure - not schema drift"),
    ("credential-failover", "Expired key with authorized failover"),
    ("rate-limit", "Rate limit with Retry-After"),
    ("server-error", "Upstream outage (503)"),
    ("verification-rollback", "Repair passes validation but fails verification"),
]


class RealTestError(RuntimeError):
    pass


def _step(steps: list, name: str, status: str, title: str, detail=None):
    steps.append({"step": len(steps) + 1, "name": name, "status": status, "title": title, "ts": now_iso(),
                  "detail": detail})


def preflight(state, check_llm: bool = True) -> dict:
    out = {"service": {"status": "FAILED", "detail": ""}, "gemini": {"status": "NOT RUN", "detail": ""}}
    try:
        svc = state.test_service_started()
        r = state.test_engine.upstreams.request(svc.url + "/orders-api", "GET", "/health")
        out["service"] = {"status": "VERIFIED" if r.status_code == 200 else "FAILED",
                          "detail": f"HTTP {r.status_code} from {svc.url}/orders-api/health",
                          "url": svc.url, "schema_version": state.test_service.orders_state()["version"]}
    except Exception as e:                       # never hide the reason
        out["service"]["detail"] = f"{type(e).__name__}: {str(e)[:200]}"
    if not is_live(state.llm):
        out["gemini"] = {"status": "ACTION REQUIRED", "detail": "GEMINI_API_KEY is not configured on the server; "
                                                                "the live demonstration needs real Gemini"}
    elif check_llm:
        res = state.llm.check()
        state.storage.event("llm_check", None, res.meta())
        out["gemini"] = {"status": "VERIFIED" if res.ok else "FAILED", "model": res.model,
                         "detail": f"real request {'succeeded' if res.ok else 'failed: ' + str(res.error_category)} "
                                   f"in {res.duration_ms} ms", "real_request_performed": res.attempts > 0}
    return out


def _read_back(engine, base: str, order_id: str | None, expect: dict) -> dict:
    """Independent business verification: read the stored order and compare with the caller's intent."""
    if not order_id:
        return {"ok": False, "detail": "no order id was returned, so nothing can be read back"}
    r = engine.upstreams.request(base, "GET", f"/orders/{order_id}")
    if r.status_code != 200:
        return {"ok": False, "http_status": r.status_code, "detail": f"order {order_id} is not stored (HTTP {r.status_code})"}
    rec = r.json()
    checks = {"placed_by": rec.get("placed_by") == expect["customer_id"], "sku": rec.get("sku") == expect["sku"],
              "quantity": rec.get("quantity") == expect["quantity"]}
    return {"ok": all(checks.values()), "http_status": 200, "order_id": order_id, "checks": checks,
            "stored": {k: rec.get(k) for k in ("placed_by", "sku", "quantity", "schema_version")},
            "detail": "stored order matches the intended customer, product and quantity" if all(checks.values())
            else "stored order does not match the request"}


def run_orders_demo(state, fault: str | None = None) -> dict:
    """fault=None: the success path. fault="drop_writes": v2 accepts orders but does not store them, so the
    independent read-back fails and the repair must be rolled back."""
    run_id = "run_" + uuid.uuid4().hex[:10]
    steps: list = []
    t_run = time.perf_counter()
    pf = preflight(state)
    _step(steps, "preflight", "ok" if pf["service"]["status"] == "VERIFIED" and pf["gemini"]["status"] == "VERIFIED"
          else "fail", "Test service and Gemini reachable", pf)
    if pf["service"]["status"] != "VERIFIED" or pf["gemini"]["status"] != "VERIFIED":
        return {"run_id": run_id, "outcome": "blocked", "steps": steps, "evidence_id": None,
                "error": "Preflight failed: " + "; ".join(f"{k}: {v['detail']}" for k, v in pf.items()
                                                           if v["status"] != "VERIFIED")}
    svc, eng = state.test_service, state.test_engine
    base = svc.url + "/orders-api"
    name = f"orders-{run_id[4:]}"
    with state.test_lock:
        svc.set_orders("v1", "normal")
        eng.register(ToolSpec(name=name, base_url=base, path="/orders", method="POST",
                              provider="ADAPTIX controlled test service", description="Place an order",
                              status_output="status", success_values=["created"], echo_output="received"))
        v1 = eng.storage.get_schema(eng.storage.current(name).schema_signature)
        first = eng.execute(name, dict(DEMO_ARGS), scenario="orders-demo:baseline-call")
        biz1 = _read_back(eng, base, (first.get("result") or {}).get("order_id"), DEMO_ARGS)
        _step(steps, "initial_call", "ok" if first["status"] == "ok" and biz1["ok"] else "fail",
              f"Schema v1 call: {first['status']}; business outcome {'confirmed' if biz1['ok'] else 'NOT confirmed'}",
              {"request": {"method": "POST", "path": "/orders", "body": DEMO_ARGS}, "response": first.get("result"),
               "schema_signature": v1.signature(), "schema_version": "1.0.0", "business": biz1,
               "expected_business_outcome": f"an order for {DEMO_ARGS['customer_id']}: {DEMO_ARGS['quantity']} x "
                                            f"{DEMO_ARGS['sku']} is stored"})
        if first["status"] != "ok" or not biz1["ok"]:
            return {"run_id": run_id, "outcome": "blocked", "steps": steps, "evidence_id": None,
                    "error": "the initial v1 call did not succeed, so there is nothing to recover"}
        svc.set_orders("v2", "drop_writes" if fault == "drop_writes" else "normal")
        _step(steps, "schema_change", "ok", "Test service switched to schema v2 (customer_id -> client_id)"
              + (" with writes silently dropped (fault injection)" if fault == "drop_writes" else ""),
              svc.orders_state())
        # 4: the original request, unchanged, now fails - and why
        td = eng.storage.current(name)
        req = td.build(dict(DEMO_ARGS))
        raw = eng.upstreams.request(base, "POST", req["url"], json=req["body"])
        new = eng.discover(eng.storage.tool(name))
        rep = diff(v1, new) if new is not None and not isinstance(new, str) else None
        conflicts = request_conflicts(rep, sent_locations(req), set()) if rep else []
        cls = classify_response(raw.status_code, raw.json() if raw.headers.get("content-type", "").startswith(
            "application/json") else None, rep.changed if rep else None, rep.touched() if rep else None, conflicts)
        _step(steps, "original_request_fails", "ok" if raw.status_code >= 400 else "fail",
              f"Original request now returns HTTP {raw.status_code}: classified {cls.category}",
              {"http_status": raw.status_code, "classification": cls.model_dump(),
               "schema_diff": rep.summary if rep else None, "mismatch": conflicts})
        # 5-11: the pipeline, with the LLM required to propose the repair
        eng.set_llm(state.llm)
        eng.llm_required = True
        try:
            res = eng.execute(name, dict(DEMO_ARGS), scenario="orders-demo" + (f":{fault}" if fault else ""))
        finally:
            eng.llm_required = False
        inc = eng.storage.incident(res["incident"]) if res.get("incident") else None
        inc_d = inc.model_dump(mode="json") if inc else None
        _step(steps, "pipeline", "ok" if res["status"] == "recovered" else "fail",
              f"ADAPTIX pipeline outcome: {res['status']} - {res.get('reason')}",
              {"incident": res.get("incident"), "tool_version": res.get("tool_version"),
               "llm": (inc_d or {}).get("llm")})
        biz = None
        if res["status"] == "recovered":
            biz = _read_back(eng, base, (res.get("result") or {}).get("order_id"), DEMO_ARGS)
            _step(steps, "business_verification", "ok" if biz["ok"] else "fail",
                  "Independent read-back: " + biz["detail"], biz)
            if not biz["ok"]:
                cur = eng.storage.current(name)
                restored = eng.storage.rollback(name, cur.version, "business outcome not confirmed by read-back",
                                                inc.correlation_id if inc else None)
                if inc:
                    inc.add("verify", "Independent business verification failed: " + biz["detail"], "fail", biz)
                    inc.add("rollback", f"Rolled back v{cur.version} -> v{restored.version if restored else '?'}: "
                                        "business outcome not confirmed (the order request was not re-sent)",
                            "warn" if restored else "fail")
                    inc.status, inc.rolled_back = "rolled_back", restored is not None
                    inc.reason = "repair rolled back: the API answered but the order was not stored"
                    eng.storage.save_incident(inc)
                    inc_d = inc.model_dump(mode="json")
                _step(steps, "rollback", "ok" if restored else "fail",
                      f"Registry restored to v{restored.version}" if restored else "Rollback not possible",
                      {"restored_version": restored.version if restored else None})
        elif inc_d and inc_d.get("rolled_back"):
            _step(steps, "rollback", "ok", "Registry restored by the pipeline after failed verification", None)
        expected = "rolled_back" if fault == "drop_writes" else "recovered"
        ev = evidence_mod.build(eng, inc_d, run_id=run_id, scenario="orders-demo" + (f":{fault}" if fault else ""),
                                tool=name, expected=expected, expected_category="schema_drift", business=biz,
                                result=res, extra={"demo_steps": steps,
                                                   "demo_preflight": pf, "test_service": svc.orders_state()})
        svc.set_orders("v1", "normal")             # leave the service ready for the next run
    outcome = ev["final"]["outcome"]
    return {"run_id": run_id, "outcome": outcome, "verified_recovery": ev["final"]["verified_recovery"],
            "steps": steps, "evidence_id": ev["evidence_id"], "incident": res.get("incident"),
            "elapsed_s": round(time.perf_counter() - t_run, 2)}


def run_matrix(state, scenarios: list[str] | None = None, runs: int = 1, llm: str = "none", seed: int = 7) -> dict:
    """Run the scenario matrix against the controlled service over real HTTP. llm: none | gemini (no mock)."""
    if llm not in ("none", "gemini"):
        raise RealTestError("REAL_API_TEST never uses the mock LLM: choose 'none' or 'gemini'")
    if llm == "gemini" and not is_live(state.llm):
        raise RealTestError("GEMINI_API_KEY is not configured on the server; choose 'none' or configure the key")
    svc = state.test_service_started()
    eng = state.test_engine
    target = sim.ServiceTarget(svc)
    ids = scenarios or [s for s, _ in MATRIX]
    batch = "mx_" + uuid.uuid4().hex[:8]
    rows = []
    with state.test_lock:
        from .llm import NoProvider
        eng.set_llm(state.llm if llm == "gemini" else NoProvider())
        try:
            for i in range(runs):
                for sid in ids:
                    rows.append(sim.run(eng, sid, seed=seed + i, heal=True, target=target, evidence_run=batch))
        finally:
            eng.set_llm(NoProvider())
    recs = [eng.storage.evidence_one(r["evidence_id"]) for r in rows]
    return {"batch": batch, "environment": "REAL_API_TEST", "llm": llm, "runs": runs, "rows": rows,
            "scorecard": evidence_mod.scorecard(recs)}

