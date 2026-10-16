"""Real API monitoring: periodic and on-demand schema checks for REAL tools (production mode only).

A check:
1. fetches the provider's authoritative schema through the SSRF guard (or reports that the provider has
   none - then drift cannot be verified and the tool is never repaired);
2. compares it with the schema of the tool's active version (no change -> "No schema drift detected");
3. if the tool has a safe read-only probe (GET only), runs it through the full repair pipeline: a call that
   fails because of confirmed drift is diagnosed, repaired, retried, verified and rolled back if needed;
   a call that still succeeds shows the drift does not break this task;
4. stores the result (never fabricated: every field comes from what actually happened).

Nothing here sends a write request to a provider, and nothing runs against simulated endpoints.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .diff import diff
from .models import now_iso
from .schema import Operation

NO_SCHEMA_LIMITATION = ("This provider publishes no machine-readable schema. ADAPTIX can check that the endpoint "
                        "is reachable (with a probe), but it cannot verify schema drift, so it never repairs this "
                        "tool automatically. Failures are reported with their category instead.")


class MonitorError(RuntimeError):
    pass


def _parse(ts: str | None) -> float:
    if not ts:
        return 0.0
    return datetime.fromisoformat(ts).timestamp()


class Monitor:
    def __init__(self, engine, settings, clock=time.time):
        if engine.mode != "production":
            raise MonitorError("real API monitoring runs only on the production engine")
        self.engine, self.s, self.clock = engine, settings, clock
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def interval(self, spec) -> int:
        return max(spec.monitor_interval_s or self.s.monitor_interval_s, self.s.monitor_min_interval_s)

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    # ---------------------------------------------------------------- one check
    def check(self, name: str, trigger: str = "manual") -> dict:
        spec = self.engine.storage.tool(name)
        if spec is None:
            raise MonitorError(f"unknown tool {name}")
        lock = self._lock(name)
        if not lock.acquire(blocking=False):
            raise MonitorError(f"a check of {name} is already running")
        try:
            return self._check(spec, trigger)
        finally:
            lock.release()

    def _check(self, spec, trigger: str) -> dict:
        t0 = time.perf_counter()
        st = self.engine.storage
        td = st.current(spec.name)
        old = st.get_schema(td.schema_signature)
        out = {"ts": now_iso(), "tool": spec.name, "trigger": trigger, "provider": spec.provider,
               "endpoint": f"{spec.method} {spec.base_url.rstrip('/')}{spec.path}", "tool_version": td.version,
               "schema_source": spec.schema_source.kind, "schema_url": None if spec.schema_source.kind == "none"
               else spec.schema_url(), "schema_status": "failed", "connectivity": "not_checked",
               "error_category": None, "detail": "", "drift": False, "drift_summary": None, "report": None,
               "verification": "not_run", "verification_detail": "", "incident": None, "repair": None}
        d = self.engine.discover_detailed(spec)
        if d.status == "unsupported":
            out.update(schema_status="unsupported", detail=NO_SCHEMA_LIMITATION)
        elif d.op is None:
            out.update(schema_status="failed", error_category=d.error_category, detail=d.detail)
            out["connectivity"] = "unreachable" if d.error_category in ("timeout", "network", "dns_failure", "tls_error") \
                or (d.error_category or "").startswith("egress_denied") else "reachable"
        else:
            out["connectivity"] = "reachable"
            out["schema_status"] = "verified"
            out["live_signature"] = d.op.signature() if isinstance(d.op, Operation) else None
            rep = diff(old, d.op)
            out["drift"] = bool(rep.changed)
            out["report"] = rep.model_dump()
            out["drift_summary"] = rep.summary if rep.changed else "No schema drift detected"
            if rep.changed:
                st.event("schema_drift_detected", spec.name, {"summary": rep.summary, "compatibility": rep.compatibility,
                                                              "registered": td.schema_signature,
                                                              "live": out.get("live_signature")})
        self._probe(spec, out)
        out["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        st.save_check(out)
        st.event("schema_check", spec.name, {k: out[k] for k in ("trigger", "schema_status", "connectivity",
                                                                 "error_category", "drift", "verification")})
        return out

    def _probe(self, spec, out: dict):
        if spec.probe_arguments is None:
            out["verification_detail"] = ("No probe configured: the task is verified on real calls. If drift "
                                          "breaks a call, the repair pipeline runs on that call.")
            return
        if spec.method != "GET":
            out["verification_detail"] = "Probes run only for read-only GET tools; this tool was not called."
            return
        if out["schema_status"] == "failed" and (out["error_category"] or "").startswith("egress_denied"):
            out["verification_detail"] = "Not called: the destination is not an approved public host."
            return
        res = self.engine.execute(spec.name, dict(spec.probe_arguments), scenario=None)
        out["incident"] = res.get("incident")
        status = res["status"]
        if status in ("ok", "recovered"):
            out["connectivity"] = "reachable"
            out["verification"] = "verified"
            out["verification_detail"] = ("Probe call succeeded and its result conforms to the schema"
                                          if status == "ok" else f"Recovered: {res.get('reason')}")
            if status == "recovered":
                out["repair"] = {"incident": res.get("incident"), "tool_version": res.get("tool_version"),
                                 "reason": res.get("reason")}
            elif out["drift"]:
                out["verification_detail"] += " (the schema changed, but this call still works with the current version)"
        else:
            out["verification"] = "failed"
            out["error_category"] = out["error_category"] or res.get("category")
            out["verification_detail"] = f"{status}: {res.get('reason')}"
            if res.get("category") in ("timeout", "network", "dns_failure", "tls_error", "egress_denied"):
                out["connectivity"] = "unreachable"
            elif res.get("category"):
                out["connectivity"] = "reachable"

    # ---------------------------------------------------------------- schedule
    def due(self) -> list[str]:
        now = self.clock()
        names = []
        for t in self.engine.storage.tools():
            spec = t["spec"]
            if spec.simulated or not spec.monitor_enabled:
                continue
            last = self.engine.storage.checks(spec.name, limit=1)
            if not last or now - _parse(last[0]["ts"]) >= self.interval(spec):
                names.append(spec.name)
        return names

    def run_due(self) -> list[dict]:
        results = []
        for name in self.due():
            try:
                results.append(self.check(name, trigger="scheduled"))
            except MonitorError:
                continue
        return results

    def start(self, tick_s: float = 30.0):
        if self._thread and self._thread.is_alive():
            return
        def loop():
            while not self._stop.wait(tick_s):
                try:
                    self.run_due()
                except Exception:          # a monitoring failure must never take the service down
                    pass
        self._thread = threading.Thread(target=loop, name="adaptix-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        return {"scheduler_running": bool(self._thread and self._thread.is_alive()),
                "default_interval_s": self.s.monitor_interval_s, "min_interval_s": self.s.monitor_min_interval_s}
