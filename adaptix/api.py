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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import evaluation, integrations, metrics, simulation
from .config import Settings, get_settings
from .credentials import CredentialRegistry
from .engine import Engine, EngineError
from .llm import NoProvider, is_live, make_provider
from .llm.mock import MockProvider
from .models import ToolSpec
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
        if storage is None and sim_storage is None and settings.db_path == settings.sim_db_path:
            raise IsolationError("ADAPTIX_DB_PATH and ADAPTIX_SIM_DB_PATH must be different files")
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
        self.monitor = Monitor(self.engine, settings)
        self.started = time.time()
        self.lock = threading.Lock()
        self.storage.purge(settings.retention_days)
        self.sim_storage.purge(settings.retention_days)
        if start_monitor if start_monitor is not None else settings.monitor_enabled:
            self.monitor.start()

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

        for fn in (tools, tool, versions, drift, rollback, delete, incidents, incident, audit, get_metrics):
            fn.__name__ = f"{storage.mode}_{fn.__name__}"

    mount("/api", st.storage, st.engine)
    mount("/api/sim", st.sim_storage, st.sim_engine)

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
