"""ADAPTIX backend API (FastAPI).

Run:  uvicorn adaptix.api:app --port 8000          (docs at /docs)
Auth: every route except GET /api/health/live requires  Authorization: Bearer <ADAPTIX_ADMIN_TOKEN>.
      The Streamlit dashboard calls these same routes (in-process by default, or over HTTP via ADAPTIX_API_URL).
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

from . import evaluation, metrics, simulation
from .config import Settings, get_settings
from .credentials import CredentialRegistry
from .engine import Engine, EngineError
from .llm import make_provider
from .models import ToolSpec
from .security import redact, scrub
from .storage import Storage

VERSION = "1.0.0"


class ExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=64)


class AnalyzeIn(ExecuteIn):
    observed_status: int | None = Field(default=None, ge=100, le=599)
    observed_body: Any = None


class SimulationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    seed: int = Field(default=7, ge=0, le=10_000)
    mode: Literal["adaptix", "baseline", "both"] = "both"


class EvaluationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suite: Literal["scenarios", "planned", "adversarial"] = "scenarios"
    seed: int = Field(default=7, ge=0, le=10_000)


class OpenAIToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: dict


class State:
    """Process-wide services. One engine, one credential registry, one LLM provider."""
    def __init__(self, settings: Settings, storage: Storage | None = None, creds: CredentialRegistry | None = None,
                 llm=None, sleep=time.sleep):
        self.settings = settings
        self.storage = storage or Storage(settings.db_path)
        self.creds = creds or CredentialRegistry.from_env()
        self.llm = llm if llm is not None else make_provider(settings, self.creds)
        self.engine = Engine(self.storage, self.creds, self.llm, settings, sleep=sleep)
        self.started = time.time()
        self.lock = threading.Lock()
        self.storage.purge(settings.retention_days)


def create_app(state: State | None = None) -> FastAPI:
    st = state or State(get_settings())
    s = st.settings
    app = FastAPI(title="ADAPTIX API", version=VERSION,
                  description="APIs change. ADAPTIX adapts. Autonomous API resilience: detects schema drift, "
                              "repairs supported changes, verifies the task and rolls back failed repairs.")
    app.state.adaptix = st
    if s.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(s.cors_origins), allow_methods=["GET", "POST", "DELETE"],
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
                "credentials": st.creds.status(), "simulated_tools": sum(t["spec"].simulated for t in st.storage.tools()),
                "limits": {"max_repair_attempts": s.max_repair_attempts, "max_retries": s.max_transport_retries,
                           "time_budget_s": s.total_time_budget_s, "request_timeout_s": s.request_timeout_s}}

    @app.post("/api/health/llm-check", dependencies=[Depends(admin)])
    def llm_check():
        """Performs one minimal REAL provider request (if a real provider is configured)."""
        r = st.llm.check()
        st.storage.event("llm_check", None, r.meta())
        return {"result": r.meta(), "status": st.llm.status()}

    # ---------------------------------------------------------------- tools
    def tool_row(t: dict) -> dict:
        spec = t["spec"]
        incs = st.storage.incidents(tool=spec.name, limit=50)
        return {"name": spec.name, "description": spec.description, "method": spec.method, "base_url": spec.base_url,
                "path": spec.path, "simulated": spec.simulated, "current_version": t["current_version"],
                "schema_signature": t["schema_signature"], "last_success_at": t["last_success_at"],
                "last_failure_at": t["last_failure_at"], "last_category": t["last_category"],
                "health": "healthy" if (t["last_category"] in (None, "ok") or (t["last_success_at"] or "") >
                                        (t["last_failure_at"] or "")) else "degraded",
                "drift_incidents": sum(i.category == "schema_drift" for i in incs),
                "versions": len(st.storage.versions(spec.name)), "depends_on": spec.depends_on}

    @app.get("/api/tools", dependencies=[Depends(admin)])
    def tools(simulated: bool | None = None):
        rows = [tool_row(t) for t in st.storage.tools()]
        return [r for r in rows if simulated is None or r["simulated"] == simulated]

    @app.post("/api/tools", status_code=201, dependencies=[Depends(admin)])
    def register(spec: ToolSpec):
        if spec.base_url.startswith("sim://") and not spec.simulated:
            raise HTTPException(422, {"error": "invalid", "detail": "sim:// tools must be marked simulated"})
        try:
            td = st.engine.register(spec)
        except EngineError as e:
            raise HTTPException(422, {"error": "schema_unavailable", "detail": str(e)})
        return {"name": spec.name, "version": td.version, "schema_signature": td.schema_signature}

    @app.get("/api/tools/{name}", dependencies=[Depends(admin)])
    def tool(name: str):
        t = next((t for t in st.storage.tools() if t["spec"].name == name), None) or not_found(name)
        return {**tool_row(t), "spec": t["spec"].model_dump(), "arguments": st.engine.arguments_schema(name),
                "dependents": [x["spec"].name for x in st.storage.tools() if name in x["spec"].depends_on
                               or (x["spec"].base_url == t["spec"].base_url and x["spec"].name != name)]}

    @app.get("/api/tools/{name}/versions", dependencies=[Depends(admin)])
    def versions(name: str):
        v = st.storage.versions(name) or not_found(name)
        return [x.describe() | {"parent_version": x.parent_version, "incident_id": x.incident_id, "patch": x.patch}
                for x in v]

    @app.get("/api/tools/{name}/drift", dependencies=[Depends(admin)])
    def drift(name: str):
        """Compare the registered schema with the live authoritative schema (reads the schema only)."""
        from .diff import diff as do_diff
        spec = st.storage.tool(name) or not_found(name)
        td = st.storage.current(name)
        old = st.storage.get_schema(td.schema_signature)
        new = st.engine.discover(spec)
        if new is None:
            return {"available": False, "detail": "authoritative schema could not be fetched"}
        rep = do_diff(old, new)
        side = lambda op: None if isinstance(op, str) else {
            "fields": {k: f.label() for k, f in op.fields.items()}, "headers": op.headers,
            "response": {k: f.label() for k, f in op.response_fields.items()}, "signature": op.signature(),
            "descriptions": {k: f.description for k, f in {**op.fields, **op.response_fields}.items() if f.description}}
        return {"available": True, "registered": side(old), "live": side(new), "report": rep.model_dump(),
                "current_version": td.version}

    @app.post("/api/tools/{name}/rollback", dependencies=[Depends(admin)])
    def rollback(name: str):
        td = st.storage.current(name) or not_found(name)
        restored = st.storage.rollback(name, td.version, "manual rollback via API")
        if restored is None:
            raise HTTPException(409, {"error": "nothing_to_roll_back", "detail": f"v{td.version} has no parent version"})
        return restored.describe()

    @app.delete("/api/tools/{name}", dependencies=[Depends(admin)])
    def delete(name: str):
        st.storage.tool(name) or not_found(name)
        st.storage.delete_tool(name)
        return {"deleted": name}

    # ---------------------------------------------------------------- incidents, repair
    @app.get("/api/incidents", dependencies=[Depends(admin)])
    def incidents(tool: str | None = None, status: str | None = None, simulated: bool | None = None,
                  limit: int = Query(100, ge=1, le=1000)):
        return [i.model_dump(mode="json") for i in st.storage.incidents(tool, status, simulated, limit)]

    @app.get("/api/incidents/{incident_id}", dependencies=[Depends(admin)])
    def incident(incident_id: str):
        i = st.storage.incident(incident_id) or not_found(incident_id)
        return i.model_dump(mode="json")

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

    # ---------------------------------------------------------------- audit, metrics
    @app.get("/api/audit", dependencies=[Depends(admin)])
    def audit(tool: str | None = None, kind: str | None = None, simulated: bool | None = None,
              correlation_id: str | None = None, limit: int = Query(200, ge=1, le=2000)):
        return st.storage.events(tool, kind, simulated, correlation_id, limit)

    @app.get("/api/metrics", dependencies=[Depends(admin)])
    def get_metrics(simulated: bool | None = None):
        return metrics.compute(st.storage, simulated)

    # ---------------------------------------------------------------- simulations (Chaos Lab) and evaluation
    @app.get("/api/simulations", dependencies=[Depends(admin)])
    def catalog():
        return [s.public() | {"expected_now": simulation.expected_outcome(s, st.engine)} for s in simulation.SCENARIOS]

    @app.post("/api/simulations", dependencies=[Depends(admin)])
    def simulate(body: SimulationIn):
        if body.scenario not in simulation.BY_ID:
            not_found(body.scenario)
        modes = {"adaptix": [True], "baseline": [False], "both": [False, True]}[body.mode]
        return {"simulated": True, "runs": [simulation.run(st.engine, body.scenario, body.seed, heal) for heal in modes]}

    @app.post("/api/simulations/reset", dependencies=[Depends(admin)])
    def reset():
        st.storage.reset(simulated_only=True)
        return {"reset": True, "scope": "simulated data only"}

    @app.post("/api/evaluation", dependencies=[Depends(admin)])
    def run_evaluation(body: EvaluationIn):
        with st.lock:                                   # one evaluation at a time
            return evaluation.run(st.engine, body.suite, body.seed)

    @app.get("/api/evaluation/latest", dependencies=[Depends(admin)])
    def latest_evaluation():
        ev = st.storage.events(kind="evaluation_run", limit=20)
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
