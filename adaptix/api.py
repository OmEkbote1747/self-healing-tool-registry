"""ADAPTIX backend API (FastAPI).

Run:  uvicorn adaptix.api:app --port 8000          (docs at /docs)
Auth: every route except GET /api/health/live requires  Authorization: Bearer <ADAPTIX_ADMIN_TOKEN>.
      The Streamlit dashboard calls these same routes (in-process by default, or over HTTP via ADAPTIX_API_URL).

Two isolated worlds, enforced in the backend:
  /api/...      PRODUCTION: real providers only (SSRF-guarded), production database, never the mock LLM
  /api/sim/...  SIMULATION: in-process sim:// APIs only, a separate database and credential registry
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from . import changes as changes_mod
from . import evaluation, integrations, metrics, realtest, simulation
from . import evidence as evidence_mod
from .config import Settings, get_settings
from .credentials import CredentialRegistry
from .engine import Engine, EngineError
from .llm import NoProvider, is_live, make_provider
from .llm.mock import MockProvider
from .models import ToolSpec, now_iso
from .monitor import Monitor, MonitorError
from .netguard import GuardedHTTP
from .security import redact, scrub
from .storage import IsolationError, Storage
from .upstreams import Upstreams

VERSION = "1.0.0"


class ExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=64)


class AnalyzeIn(ExecuteIn):
    observed_status: int | None = Field(default=None, ge=100, le=599)
    observed_body: Any = None


LLMChoice = Literal["none", "mock", "gemini"]


class SimulationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    seed: int = Field(default=7, ge=0, le=10_000)
    mode: Literal["adaptix", "baseline", "both"] = "both"
    llm: LLMChoice = "none"           # rules only (default, deterministic) | labelled mock | real Gemini


class EvaluationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: Literal["scenarios", "planned", "adversarial"] = "scenarios"
    seed: int = Field(default=7, ge=0, le=10_000)
    llm: LLMChoice = "none"


class DemoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: Literal["drop_writes"] | None = None


class MatrixIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[str] | None = None
    runs: int = Field(default=1, ge=1, le=5)
    llm: Literal["none", "gemini"] = "none"
    seed: int = Field(default=7, ge=0, le=10_000)


class LiveFaultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fault: Literal["rename_column", "change_type", "add_required_column", "remove_column"]
    column: str | None = Field(default=None, max_length=41)
    new_name: str | None = Field(default=None, max_length=41)
    new_type: str | None = None


class LiveExperimentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: Literal["gemini", "local"]
    customer_id: str = Field(default="cust-0003", max_length=64)
    sku: str = Field(default="SKU-200", max_length=64)
    quantity: int = Field(default=2, ge=1, le=100)
    note: str | None = Field(default="deliver after 5pm", max_length=200)


class LiveScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    interval_s: int = Field(ge=60, le=86400)
    run_limit: int = Field(ge=1, le=100)
    auto_recover: bool = True
    engine: Literal["gemini", "local"] = "gemini"
    faults: list[Literal["rename_column", "change_type", "add_required_column", "remove_column"]] = Field(min_length=1)


class LiveSetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reset: bool = False
    confirm: bool = False


class MonitorSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    interval_s: int | None = Field(default=None, ge=300, le=7 * 86400)


class OpenAIToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: dict


class State:
    """Process-wide services, split into two isolated worlds.

    production: real tools, production database, production credential registry (no simulated keys),
                guarded real egress, the real LLM provider or none - never the mock
    simulation: simulated tools, a separate database, its own throwaway credential registry, sim:// only
    """
    def __init__(self, settings: Settings, storage: Storage | None = None, creds: CredentialRegistry | None = None,
                 llm=None, sleep=time.sleep, sim_storage: Storage | None = None, guard: GuardedHTTP | None = None,
                 start_monitor: bool | None = None):
        self.settings = settings
        if storage is None and sim_storage is None and len({settings.db_path, settings.sim_db_path,
                                                             settings.test_db_path, settings.livedb_db_path}) < 4:
            raise IsolationError("ADAPTIX_DB_PATH, ADAPTIX_SIM_DB_PATH and ADAPTIX_TEST_DB_PATH must be different files")
        self.storage = storage or Storage(settings.db_path, mode="production")
        self.creds = creds or CredentialRegistry.from_env()
        self.llm = llm if llm is not None else make_provider(settings, self.creds, mode="production")
        guard = guard or GuardedHTTP(settings.allowed_hosts, settings.allow_http, settings.request_timeout_s,
                                     settings.max_redirects, settings.host_min_interval_s)
        self.engine = Engine(self.storage, self.creds, self.llm, settings,
                             Upstreams(settings.request_timeout_s, "production", guard, settings.max_schema_bytes),
                             sleep=sleep)
        sim_path = settings.sim_db_path if sim_storage is None and storage is None else None
        self.sim_storage = sim_storage or Storage(sim_path or (str(self.storage.path) + ".sim"), mode="simulation")
        self.sim_creds = CredentialRegistry([])          # throwaway simulated keys only
        self.sim_engine = Engine(self.sim_storage, self.sim_creds, NoProvider(), settings,
                                 Upstreams(settings.request_timeout_s, "simulation"), sleep=sleep)
        # REAL_API_TEST: the controlled test service (started on first use) and its own records
        test_path = settings.test_db_path if storage is None else str(self.storage.path) + ".test"
        self.test_storage = Storage(test_path, mode="real_api_test")
        self.test_creds = CredentialRegistry([])          # throwaway keys for the controlled service only
        self.test_engine = Engine(self.test_storage, self.test_creds, NoProvider(), settings,
                                  Upstreams(settings.request_timeout_s, "real_api_test",
                                            GuardedHTTP((), timeout_s=settings.request_timeout_s, min_interval_s=0),
                                            settings.max_schema_bytes), sleep=sleep)
        self.test_service = None
        self.test_lock = threading.Lock()
        self._svc_lock = threading.Lock()
        # LIVE_DB_DEMO: controlled experiments on an online PostgreSQL demo schema (separate records)
        live_path = settings.livedb_db_path if storage is None else str(self.storage.path) + ".livedb"
        self.livedb_storage = Storage(live_path, mode="live_db")
        from .dbrepair import Scheduler
        self.livedb_scheduler = Scheduler(self)
        self.monitor = Monitor(self.engine, settings)
        self.started = time.time()
        self.lock = threading.Lock()
        self.storage.purge(settings.retention_days)
        self.sim_storage.purge(settings.retention_days)
        self.test_storage.purge(settings.retention_days)
        if start_monitor if start_monitor is not None else settings.monitor_enabled:
            self.monitor.start()
            self.livedb_scheduler.start()          # idle unless the operator enables a schedule

    def test_service_started(self):
        """Start the controlled test service once; only its exact address is exempt from the egress allowlist."""
        from .security import register_secret
        from .testservice import TestService
        with self._svc_lock:
            if self.test_service is None or not self.test_service.running():
                self.test_service = TestService().start()
                register_secret(self.test_service.admin_token)
                self.test_engine.upstreams.guard = GuardedHTTP(
                    (), timeout_s=self.settings.request_timeout_s, min_interval_s=0,
                    exempt=frozenset({self.test_service.address}))
        return self.test_service

    def sim_llm(self, choice: str):
        """LLM for a simulation run. 'gemini' is the real provider (labelled real); 'mock' is labelled mock."""
        if choice == "mock":
            return MockProvider()
        if choice == "gemini":
            if not is_live(self.llm):
                raise HTTPException(409, {"error": "llm_not_configured",
                                          "detail": "real Gemini is not configured on this server (GEMINI_API_KEY)"})
            return self.llm
        return NoProvider()


def create_app(state: State | None = None) -> FastAPI:
    st = state or State(get_settings())
    s = st.settings
    app = FastAPI(title="ADAPTIX API", version=VERSION,
                  description="APIs change. ADAPTIX adapts. Autonomous API resilience: detects schema drift, "
                              "repairs supported changes, verifies the task and rolls back failed repairs.")
    app.state.adaptix = st
    if s.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(s.cors_origins), allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type"], allow_credentials=False)

    @app.middleware("http")
    async def limits(request: Request, call_next):
        size = request.headers.get("content-length")
        if size and int(size) > s.max_body_bytes:
            return JSONResponse(status_code=413, content={"error": "request_too_large",
                                                          "detail": f"limit is {s.max_body_bytes} bytes"})
        try:
            return await call_next(request)
        except Exception as e:                          # structured error, never a raw trace or secret
            return JSONResponse(status_code=500, content={"error": "internal_error", "detail": scrub(str(e))[:200]})

    def admin(authorization: str = Header(default="")) -> None:
        token = authorization.removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(token, s.admin_token):
            raise HTTPException(401, {"error": "unauthorized", "detail": "missing or invalid admin token"})

    def not_found(what: str):
        raise HTTPException(404, {"error": "not_found", "detail": what})

    # ---------------------------------------------------------------- health
    @app.get("/api/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/api/health", dependencies=[Depends(admin)])
    def health():
        try:
            st.storage.tools()
            db = "ok"
        except Exception as e:
            db = "error: " + scrub(str(e))[:80]
        return {"status": "ok" if db == "ok" else "degraded", "version": VERSION, "env": s.app_env,
                "uptime_s": round(time.time() - st.started), "database": db, "llm": st.llm.status(),
                "credentials": st.creds.status(), "real_tools": len(st.storage.tools()),
                "simulated_tools": len(st.sim_storage.tools()), "allowed_hosts": list(s.allowed_hosts),
                "monitor": st.monitor.status(),
                "limits": {"max_repair_attempts": s.max_repair_attempts, "max_retries": s.max_transport_retries,
                           "time_budget_s": s.total_time_budget_s, "request_timeout_s": s.request_timeout_s}}

    @app.post("/api/health/llm-check", dependencies=[Depends(admin)])
    def llm_check():
        """Performs one minimal REAL provider request (if a real provider is configured)."""
        if not is_live(st.llm):
            raise HTTPException(409, {"error": "llm_not_configured",
                                      "detail": "no real LLM provider is configured (set GEMINI_API_KEY); nothing was sent"})
        r = st.llm.check()
        st.storage.event("llm_check", None, r.meta())
        return {"result": r.meta(), "status": st.llm.status()}

    # ---------------------------------------------------------------- shared read/inspect routes, per world
    def mount(prefix: str, storage: Storage, engine: Engine):
        def tool_row(t: dict) -> dict:
            spec = t["spec"]
            incs = storage.incidents(tool=spec.name, limit=50)
            last = storage.checks(spec.name, limit=1)
            return {"name": spec.name, "description": spec.description, "method": spec.method,
                    "base_url": spec.base_url, "path": spec.path, "simulated": spec.simulated,
                    "provider": spec.provider, "schema_source": spec.schema_source.kind,
                    "current_version": t["current_version"], "schema_signature": t["schema_signature"],
                    "last_success_at": t["last_success_at"], "last_failure_at": t["last_failure_at"],
                    "last_category": t["last_category"],
                    "health": "healthy" if (t["last_category"] in (None, "ok") or (t["last_success_at"] or "") >
                                            (t["last_failure_at"] or "")) else "degraded",
                    "drift_incidents": sum(i.category == "schema_drift" for i in incs),
                    "versions": len(storage.versions(spec.name)), "depends_on": spec.depends_on,
                    "last_check": last[0] if last else None}

        @app.get(f"{prefix}/tools", dependencies=[Depends(admin)])
        def tools():
            return [tool_row(t) for t in storage.tools()]

        @app.get(prefix + "/tools/{name}", dependencies=[Depends(admin)])
        def tool(name: str):
            t = next((t for t in storage.tools() if t["spec"].name == name), None) or not_found(name)
            return {**tool_row(t), "spec": t["spec"].model_dump(), "arguments": engine.arguments_schema(name),
                    "dependents": [x["spec"].name for x in storage.tools() if name in x["spec"].depends_on
                                   or (x["spec"].base_url == t["spec"].base_url and x["spec"].name != name)]}

        @app.get(prefix + "/tools/{name}/versions", dependencies=[Depends(admin)])
        def versions(name: str):
            v = storage.versions(name) or not_found(name)
            return [x.describe() | {"parent_version": x.parent_version, "incident_id": x.incident_id, "patch": x.patch}
                    for x in v]

        @app.get(prefix + "/tools/{name}/drift", dependencies=[Depends(admin)])
        def drift(name: str):
            """Compare the registered schema with the live authoritative schema (reads the schema only)."""
            from .diff import diff as do_diff
            spec = storage.tool(name) or not_found(name)
            td = storage.current(name)
            old = storage.get_schema(td.schema_signature)
            d = engine.discover_detailed(spec)
            side = lambda op: None if isinstance(op, str) or op is None else {
                "fields": {k: f.label() for k, f in op.fields.items()}, "headers": op.headers,
                "response": {k: f.label() for k, f in op.response_fields.items()}, "signature": op.signature(),
                "source": op.source,
                "descriptions": {k: f.description for k, f in {**op.fields, **op.response_fields}.items() if f.description}}
            if d.op is None:
                return {"available": False, "status": d.status, "error_category": d.error_category,
                        "detail": d.detail, "registered": side(old), "current_version": td.version}
            rep = do_diff(old, d.op)
            return {"available": True, "registered": side(old), "live": side(d.op), "report": rep.model_dump(),
                    "current_version": td.version}

        @app.post(prefix + "/tools/{name}/rollback", dependencies=[Depends(admin)])
        def rollback(name: str):
            td = storage.current(name) or not_found(name)
            restored = storage.rollback(name, td.version, "manual rollback via API")
            if restored is None:
                raise HTTPException(409, {"error": "nothing_to_roll_back", "detail": f"v{td.version} has no parent version"})
            return restored.describe()

        @app.delete(prefix + "/tools/{name}", dependencies=[Depends(admin)])
        def delete(name: str):
            storage.tool(name) or not_found(name)
            storage.delete_tool(name)
            return {"deleted": name}

        @app.get(f"{prefix}/incidents", dependencies=[Depends(admin)])
        def incidents(tool: str | None = None, status: str | None = None,
                      limit: int = Query(100, ge=1, le=1000)):
            return [i.model_dump(mode="json") for i in storage.incidents(tool, status, None, limit)]

        @app.get(prefix + "/incidents/{incident_id}", dependencies=[Depends(admin)])
        def incident(incident_id: str):
            i = storage.incident(incident_id) or not_found(incident_id)
            return i.model_dump(mode="json")

        @app.get(f"{prefix}/audit", dependencies=[Depends(admin)])
        def audit(tool: str | None = None, kind: str | None = None, correlation_id: str | None = None,
                  limit: int = Query(200, ge=1, le=2000)):
            return storage.events(tool, kind, None, correlation_id, limit)

        @app.get(f"{prefix}/metrics", dependencies=[Depends(admin)])
        def get_metrics():
            return metrics.compute(storage, None) | {"world": storage.mode}

        @app.get(f"{prefix}/evidence", dependencies=[Depends(admin)])
        def evidence_list(run_id: str | None = None, limit: int = Query(200, ge=1, le=2000)):
            return storage.evidence(limit, run_id)

        @app.get(f"{prefix}/evidence/export.json", dependencies=[Depends(admin)])
        def evidence_json(run_id: str | None = None):
            recs = storage.evidence(5000, run_id)
            return {"environment": storage.environment, "exported_at": now_iso(),
                    "records": recs, "scorecard": evidence_mod.scorecard(recs),
                    "note": "Redacted: no API keys, authorization headers, passwords or sensitive fields."}

        @app.get(f"{prefix}/evidence/export.csv", dependencies=[Depends(admin)], response_class=PlainTextResponse)
        def evidence_csv(run_id: str | None = None):
            return PlainTextResponse(evidence_mod.to_csv(storage.evidence(5000, run_id)), media_type="text/csv")

        @app.get(prefix + "/evidence/{evidence_id}", dependencies=[Depends(admin)])
        def evidence_one(evidence_id: str):
            return storage.evidence_one(evidence_id) or not_found(evidence_id)

        @app.get(f"{prefix}/scorecard", dependencies=[Depends(admin)])
        def scorecard(run_id: str | None = None):
            return evidence_mod.scorecard(storage.evidence(5000, run_id)) | {"environment": storage.environment}

        for fn in (tools, tool, versions, drift, rollback, delete, incidents, incident, audit, get_metrics,
                   evidence_list, evidence_json, evidence_csv, evidence_one, scorecard):
            fn.__name__ = f"{storage.mode}_{fn.__name__}"

    mount("/api", st.storage, st.engine)
    mount("/api/test", st.test_storage, st.test_engine)
    mount("/api/sim", st.sim_storage, st.sim_engine)

    # ---------------------------------------------------------------- REAL_API_TEST: controlled test service
    @app.get("/api/test/status", dependencies=[Depends(admin)])
    def test_status():
        """Cheap status (no Gemini request): is the service running, is Gemini configured."""
        svc = st.test_service
        return {"service_running": bool(svc and svc.running()), "service_url": svc.url if svc and svc.running() else None,
                "service_state": svc.orders_state() if svc and svc.running() else None,
                "gemini_configured": is_live(st.llm), "gemini_model": st.llm.status().get("model"),
                "matrix": [{"scenario": sid, "change_type": label} for sid, label in realtest.MATRIX]}

    @app.post("/api/test/preflight", dependencies=[Depends(admin)])
    def test_preflight():
        """Starts the service if needed and sends ONE minimal real Gemini request."""
        return realtest.preflight(st, check_llm=True)

    @app.post("/api/test/demo", dependencies=[Depends(admin)])
    def test_demo(body: DemoIn):
        return realtest.run_orders_demo(st, body.fault)

    @app.post("/api/test/matrix", dependencies=[Depends(admin)])
    def test_matrix(body: MatrixIn):
        unknown = [s for s in body.scenarios or [] if s not in simulation.BY_ID]
        if unknown:
            raise HTTPException(422, {"error": "unknown_scenario", "detail": ", ".join(unknown)})
        try:
            return realtest.run_matrix(st, body.scenarios, body.runs, body.llm, body.seed)
        except realtest.RealTestError as e:
            raise HTTPException(409, {"error": "not_available", "detail": str(e)})

    @app.post("/api/test/reset", dependencies=[Depends(admin)])
    def test_reset():
        st.test_storage.reset()
        return {"reset": True, "scope": "REAL_API_TEST database only"}

    # ---------------------------------------------------------------- cross-environment views (labelled)
    def worlds(environment: list[str] | None = None) -> dict:
        all_w = {"LIVE_API": st.storage, "REAL_API_TEST": st.test_storage, "SIMULATION": st.sim_storage,
                 "LIVE_DB_DEMO": st.livedb_storage}
        return {k: v for k, v in all_w.items() if not environment or k in environment}

    @app.get("/api/changes", dependencies=[Depends(admin)])
    def list_changes(q: str = "", environment: list[str] = Query(default=[]), change_type: list[str] = Query(default=[]),
                     repair_status: list[str] = Query(default=[]), verification: list[str] = Query(default=[]),
                     since: str | None = None, until: str | None = None,
                     sort: Literal["timestamp", "tool", "change_type", "status"] = "timestamp",
                     order: Literal["asc", "desc"] = "desc", page: int = Query(1, ge=1),
                     page_size: int = Query(25, ge=1, le=200)):
        rows = changes_mod.collect(worlds())
        return changes_mod.query(rows, q, environment or None, change_type or None, repair_status or None,
                                 verification or None, since, until, sort, order, page, page_size)

    # ---------------------------------------------------------------- LIVE_DB_DEMO: online PostgreSQL demo target
    from . import dbrepair, livedb

    def live_target():
        try:
            return livedb.target_from_env()
        except livedb.LiveDBError as e:
            raise HTTPException(409 if e.category == "not_configured" else 422,
                                {"error": e.category, "detail": str(e)})

    def live_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except livedb.LiveDBError as e:
            raise HTTPException(502 if e.category in ("connection", "authentication") else 422,
                                {"error": e.category, "detail": scrub(str(e))[:300]})
        except dbrepair.EngineUnavailable as e:
            raise HTTPException(409, {"error": "engine_unavailable", "detail": str(e)})
        except Exception as e:                       # psycopg errors: categorised, scrubbed, never raw traces
            import psycopg
            if isinstance(e, psycopg.Error):
                raise HTTPException(502, {"error": "database_error", "sqlstate": getattr(e, "sqlstate", None),
                                          "detail": scrub(str(e)).split("\n")[0][:300]})
            raise

    @app.get("/api/livedb/status", dependencies=[Depends(admin)])
    def livedb_status():
        try:
            t = livedb.target_from_env()
            target, configured, problem = t.display(), True, None
        except livedb.LiveDBError as e:
            target, configured, problem = None, False, str(e)
        st_ = st.livedb_storage
        return {"configured": configured, "problem": problem, "target": target, "engines": dbrepair.engines(st),
                "set_up": st_.tool(dbrepair.TOOL) is not None, "schedule": st.livedb_scheduler.settings(),
                "expected_now": dbrepair.expected_outcome(st), "faults_since_reset": len(dbrepair.fault_log(st))}

    @app.post("/api/livedb/test-connection", dependencies=[Depends(admin)])
    def livedb_test():
        return live_call(livedb.test_connection, live_target())

    @app.post("/api/livedb/setup", dependencies=[Depends(admin)])
    def livedb_setup(body: LiveSetupIn):
        if body.reset and not body.confirm:
            raise HTTPException(422, {"error": "confirmation_required", "detail": "reset drops and recreates the demo "
                                      "schema; send confirm=true"})
        t = live_target()
        out = live_call(livedb.setup, t, body.reset)
        td = live_call(dbrepair.register, st, t)
        st.livedb_storage.set_meta("reset_at", now_iso())
        st.livedb_storage.event("demo_reset" if body.reset else "demo_setup", dbrepair.TOOL,
                                {"schema": t.schema, "baseline_signature": td.schema_signature})
        return out | {"baseline_signature": td.schema_signature, "tool_version": td.version}

    @app.get("/api/livedb/tables", dependencies=[Depends(admin)])
    def livedb_tables():
        return live_call(livedb.browse, live_target())

    @app.get("/api/livedb/tables/{table}", dependencies=[Depends(admin)])
    def livedb_table(table: str, limit: int = Query(20, ge=1, le=100)):
        return live_call(livedb.sample, live_target(), table, limit)

    @app.get("/api/livedb/schema", dependencies=[Depends(admin)])
    def livedb_schema():
        """Baseline (registered at setup) vs current catalog vs the tool's active version. Read-only."""
        from .diff import diff as do_diff
        stl = st.livedb_storage
        if stl.tool(dbrepair.TOOL) is None:
            raise HTTPException(409, {"error": "not_set_up", "detail": "run Setup first"})
        t = live_target()
        base = stl.get_schema(stl.get_meta("baseline_signature"))
        td = stl.current(dbrepair.TOOL)
        active = stl.get_schema(td.schema_signature)
        cur = live_call(livedb.snapshot, t, active)
        side = lambda op: {k.split(":", 1)[1]: {"type": f.schema.get("x-pg-type"), "required": f.required,
                                                "attnum": f.schema.get("x-attnum"), "comment": f.description,
                                                "renamed_from": f.renamed_from} for k, f in op.fields.items()}
        return {"baseline": side(base), "current": side(cur), "baseline_signature": base.signature(),
                "current_signature": cur.signature(), "vs_baseline": do_diff(base, cur).model_dump(),
                "vs_active_tool": do_diff(active, cur).model_dump(), "tool_version": td.version,
                "mapping": {loc.split(":", 1)[1]: v for loc, v in td.request_map.items()}}

    @app.post("/api/livedb/inject", dependencies=[Depends(admin)])
    def livedb_inject(body: LiveFaultIn):
        if st.livedb_storage.tool(dbrepair.TOOL) is None:
            raise HTTPException(409, {"error": "not_set_up", "detail": "run Setup first"})
        return live_call(dbrepair.record_fault, st, live_target(), body.fault, column=body.column,
                         new_name=body.new_name, new_type=body.new_type)

    @app.get("/api/livedb/faults", dependencies=[Depends(admin)])
    def livedb_faults():
        return {"since_reset": dbrepair.fault_log(st),
                "all": [e["data"] | {"ts": e["ts"]} for e in st.livedb_storage.events(kind="fault_injected", limit=200)]}

    @app.get("/api/livedb/schedule", dependencies=[Depends(admin)])
    def livedb_schedule_get():
        return st.livedb_scheduler.settings()

    @app.put("/api/livedb/schedule", dependencies=[Depends(admin)])
    def livedb_schedule_put(body: LiveScheduleIn):
        cfg = st.livedb_scheduler.settings()
        if body.engine == "local" and not dbrepair.engines(st)["local"]["available"]:
            raise HTTPException(409, {"error": "engine_unavailable", "detail": "the Local Repair Module is not available"})
        reset_count = body.enabled and not cfg["enabled"]
        cfg.update(body.model_dump())
        if reset_count:
            cfg.update(runs_done=0, last_run=None)
        st.livedb_scheduler.save(cfg)
        st.livedb_storage.event("schedule_updated", dbrepair.TOOL, cfg)
        return cfg

    @app.post("/api/livedb/experiment", dependencies=[Depends(admin)])
    def livedb_experiment(body: LiveExperimentIn):
        if st.livedb_storage.tool(dbrepair.TOOL) is None:
            raise HTTPException(409, {"error": "not_set_up", "detail": "run Setup first"})
        args = body.model_dump(exclude={"engine"})
        with st.livedb_scheduler.lock:
            return live_call(dbrepair.run, st, live_target(), body.engine, args,
                             dbrepair.expected_outcome(st, args))

    @app.get("/api/livedb/monitor", dependencies=[Depends(admin)])
    def livedb_monitor():
        """Read-only monitoring: compares the live catalog with the baseline. Never injects or writes."""
        from .diff import diff as do_diff
        stl = st.livedb_storage
        if stl.tool(dbrepair.TOOL) is None:
            raise HTTPException(409, {"error": "not_set_up", "detail": "run Setup first"})
        base = stl.get_schema(stl.get_meta("baseline_signature"))
        cur = live_call(livedb.snapshot, live_target(), base)
        rep = do_diff(base, cur)
        return {"checked_at": now_iso(), "drift": rep.changed,
                "summary": rep.summary if rep.changed else "No schema drift detected", "report": rep.model_dump()}

    @app.get("/api/livedb/incidents", dependencies=[Depends(admin)])
    def livedb_incidents(limit: int = Query(100, ge=1, le=1000)):
        return [i.model_dump(mode="json") for i in st.livedb_storage.incidents(limit=limit)]

    @app.get("/api/livedb/incidents/{incident_id}", dependencies=[Depends(admin)])
    def livedb_incident(incident_id: str):
        i = st.livedb_storage.incident(incident_id) or not_found(incident_id)
        return i.model_dump(mode="json")

    @app.get("/api/livedb/versions", dependencies=[Depends(admin)])
    def livedb_versions():
        return [x.describe() | {"parent_version": x.parent_version, "incident_id": x.incident_id, "patch": x.patch}
                for x in st.livedb_storage.versions(dbrepair.TOOL)]

    @app.get("/api/livedb/evidence", dependencies=[Depends(admin)])
    def livedb_evidence(limit: int = Query(200, ge=1, le=2000)):
        return st.livedb_storage.evidence(limit)

    @app.get("/api/livedb/evidence/export.json", dependencies=[Depends(admin)])
    def livedb_evidence_json():
        recs = st.livedb_storage.evidence(5000)
        return {"environment": "LIVE_DB_DEMO", "exported_at": now_iso(), "records": recs,
                "scorecard": evidence_mod.scorecard(recs),
                "note": "Redacted: no DATABASE_URL, passwords, API keys or authorization headers."}

    @app.get("/api/livedb/evidence/export.csv", dependencies=[Depends(admin)], response_class=PlainTextResponse)
    def livedb_evidence_csv():
        return PlainTextResponse(evidence_mod.to_csv(st.livedb_storage.evidence(5000)), media_type="text/csv")

    @app.get("/api/livedb/scorecard", dependencies=[Depends(admin)])
    def livedb_scorecard():
        return evidence_mod.scorecard(st.livedb_storage.evidence(5000)) | {"environment": "LIVE_DB_DEMO"}

    @app.get("/api/overview", dependencies=[Depends(admin)])
    def overview():
        out = {}
        for env, storage in worlds().items():
            incs = storage.incidents(limit=5000)
            ev = storage.evidence(5000)
            out[env] = {"incidents": len(incs), "recovered": sum(i.status == "recovered" for i in incs),
                        "unresolved": sum(i.status in ("stopped", "failed", "rolled_back") for i in incs),
                        "schema_drift": sum(i.category == "schema_drift" for i in incs),
                        "evidence_records": len(ev), "scorecard": evidence_mod.scorecard(ev),
                        "tools": len(storage.tools()), "source": evidence_mod.SOURCE[env]}
        return out

    # ---------------------------------------------------------------- production only: real tools
    @app.post("/api/tools", status_code=201, dependencies=[Depends(admin)])
    def register(spec: ToolSpec):
        if spec.simulated or spec.base_url.startswith("sim://"):
            raise HTTPException(422, {"error": "simulation_not_allowed",
                                      "detail": "simulated tools exist only in the Simulation Lab (/api/simulations)"})
        if spec.probe_arguments is not None and spec.method != "GET":
            raise HTTPException(422, {"error": "unsafe_probe", "detail": "probes are allowed only for GET tools"})
        if spec.credential_group and not st.creds.has_group(spec.credential_group):
            raise HTTPException(422, {"error": "credential_group_missing",
                                      "detail": f"credential group '{spec.credential_group}' is not configured"})
        try:
            td = st.engine.register(spec)
        except EngineError as e:
            raise HTTPException(422, {"error": "registration_failed", "detail": scrub(str(e))[:400]})
        return {"name": spec.name, "version": td.version, "schema_signature": td.schema_signature,
                "schema_source": spec.schema_source.kind}

    @app.post("/api/repair/analyze", dependencies=[Depends(admin)])
    def analyze(body: AnalyzeIn):
        try:
            return st.engine.analyze(body.tool, body.arguments, body.observed_status, body.observed_body)
        except EngineError as e:
            raise HTTPException(404, {"error": "not_found", "detail": str(e)})

    @app.post("/api/repair/execute", dependencies=[Depends(admin)])
    def execute(body: ExecuteIn):
        try:
            return redact(st.engine.execute(body.tool, body.arguments, body.correlation_id))
        except EngineError as e:
            raise HTTPException(404, {"error": "not_found", "detail": str(e)})

    # ---------------------------------------------------------------- production only: real API monitoring
    @app.get("/api/monitor", dependencies=[Depends(admin)])
    def monitor_overview():
        rows = []
        for t in st.storage.tools():
            spec = t["spec"]
            last = st.storage.checks(spec.name, limit=1)
            rows.append({"tool": spec.name, "provider": spec.provider, "method": spec.method,
                         "endpoint": spec.base_url.rstrip("/") + spec.path, "schema_source": spec.schema_source.kind,
                         "schema_url": None if spec.schema_source.kind == "none" else spec.schema_url(),
                         "monitor_enabled": spec.monitor_enabled, "interval_s": st.monitor.interval(spec),
                         "has_probe": spec.probe_arguments is not None, "current_version": t["current_version"],
                         "last_check": last[0] if last else None})
        return {"tools": rows, "allowed_hosts": list(s.allowed_hosts), "scheduler": st.monitor.status()}

    @app.post("/api/monitor/{name}/check", dependencies=[Depends(admin)])
    def monitor_check(name: str):
        st.storage.tool(name) or not_found(name)
        try:
            return st.monitor.check(name, trigger="manual")
        except MonitorError as e:
            raise HTTPException(409, {"error": "check_busy", "detail": str(e)})

    @app.get("/api/monitor/{name}/checks", dependencies=[Depends(admin)])
    def monitor_history(name: str, limit: int = Query(20, ge=1, le=200)):
        st.storage.tool(name) or not_found(name)
        return st.storage.checks(name, limit)

    @app.put("/api/monitor/{name}", dependencies=[Depends(admin)])
    def monitor_settings(name: str, body: MonitorSettingsIn):
        spec = st.storage.tool(name) or not_found(name)
        upd = {}
        if body.enabled is not None:
            upd["monitor_enabled"] = body.enabled
        if body.interval_s is not None:
            upd["monitor_interval_s"] = body.interval_s
        spec = spec.model_copy(update=upd)
        st.storage.update_spec(spec)
        return {"tool": name, "monitor_enabled": spec.monitor_enabled, "interval_s": st.monitor.interval(spec)}

    @app.get("/api/integrations", dependencies=[Depends(admin)])
    def integration_health():
        return integrations.report(st)

    # ---------------------------------------------------------------- simulation only: Simulation Lab
    @app.get("/api/simulations", dependencies=[Depends(admin)])
    def catalog():
        return [sc.public() | {"expected_now": simulation.expected_outcome(sc, st.sim_engine),
                               "expected_with_real_llm": sc.expect}
                for sc in simulation.SCENARIOS]

    @app.post("/api/simulations", dependencies=[Depends(admin)])
    def simulate(body: SimulationIn):
        if body.scenario not in simulation.BY_ID:
            not_found(body.scenario)
        modes = {"adaptix": [True], "baseline": [False], "both": [False, True]}[body.mode]
        with st.lock:                                   # the simulation engine's LLM is chosen per run
            st.sim_engine.set_llm(st.sim_llm(body.llm))
            try:
                runs = [simulation.run(st.sim_engine, body.scenario, body.seed, heal) for heal in modes]
            finally:
                st.sim_engine.set_llm(NoProvider())
        return {"simulated": True, "llm_choice": body.llm, "runs": runs}

    @app.post("/api/simulations/reset", dependencies=[Depends(admin)])
    def reset():
        st.sim_storage.reset(simulated_only=True)
        return {"reset": True, "scope": "simulation database only"}

    @app.post("/api/evaluation", dependencies=[Depends(admin)])
    def run_evaluation(body: EvaluationIn):
        with st.lock:                                   # one evaluation at a time
            st.sim_engine.set_llm(st.sim_llm(body.llm))
            try:
                return evaluation.run(st.sim_engine, body.suite, body.seed) | {"llm_choice": body.llm}
            finally:
                st.sim_engine.set_llm(NoProvider())

    @app.get("/api/evaluation/latest", dependencies=[Depends(admin)])
    def latest_evaluation():
        ev = st.sim_storage.events(kind="evaluation_run", limit=20)
        return [e["data"] | {"ts": e["ts"]} for e in ev]

    # ---------------------------------------------------------------- OpenAI-compatible tool calling
    @app.get("/v1/openai/tools", dependencies=[Depends(admin)])
    def openai_tools():
        return [{"type": "function", "function": {"name": t["spec"].name,
                 "description": t["spec"].description or f"{t['spec'].method} {t['spec'].path}",
                 "parameters": st.engine.arguments_schema(t["spec"].name)}} for t in st.storage.tools()]

    @app.post("/v1/openai/tool_calls", dependencies=[Depends(admin)])
    def openai_tool_calls(calls: list[OpenAIToolCall]):
        out = []
        for tc in calls[:32]:
            try:
                args = json.loads(tc.function.get("arguments") or "{}")
                content = st.engine.execute(tc.function.get("name", ""), args)
            except json.JSONDecodeError:
                content = {"status": "stopped", "reason": "arguments were not valid JSON"}
            except EngineError as e:
                content = {"status": "stopped", "reason": str(e)}
            out.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(redact(content), default=str)})
        return out

    return app


_app = None


def __getattr__(name):            # lazy module-level app for `uvicorn adaptix.api:app`
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
