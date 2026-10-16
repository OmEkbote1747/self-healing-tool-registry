"""Typed models shared by the engine, the API and the dashboard."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Transform = Literal["none", "to_int", "to_float", "to_str", "to_bool", "iso_date"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SchemaSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # "none": the provider publishes no machine-readable schema. ADAPTIX then uses the operator's declared
    # fields (NOT authoritative), cannot verify drift and never repairs the tool (fail closed).
    kind: Literal["openapi", "json_schema", "none"] = "openapi"
    url: str | None = None            # default: <base_url>/openapi.json
    declared_fields: dict[str, str] = {}   # kind "none" only: {"query:status": "str", "body:name": "str"}


class ToolSpec(BaseModel):
    """A registered tool: where it lives, how to read its schema and how to verify its result."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z0-9_\-]{1,64}$")
    base_url: str
    path: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    description: str = ""
    schema_source: SchemaSource = SchemaSource()
    auth_header: str | None = None              # header that carries the credential, if any
    credential_group: str | None = None         # credential group allowed for this tool
    idempotent: bool | None = None              # default: GET/PUT/DELETE are idempotent
    supports_idempotency_key: bool = True       # upstream de-duplicates on Idempotency-Key
    status_output: str = "status"               # response path holding the task outcome
    success_values: list[str] = ["updated", "created", "ok", "success"]
    echo_output: str | None = "received"        # response path echoing the applied data (verification)
    depends_on: list[str] = []
    simulated: bool = False
    # real-API monitoring
    provider: str = Field(default="", max_length=120)   # who operates the API, e.g. "Swagger Petstore (swagger.io)"
    monitor_enabled: bool = True
    monitor_interval_s: int | None = Field(default=None, ge=300, le=7 * 86400)   # None: the global default
    # A safe, read-only sample call used to verify the task during checks. Only executed for GET tools.
    probe_arguments: dict | None = None

    def is_idempotent(self) -> bool:
        return self.idempotent if self.idempotent is not None else self.method in ("GET", "PUT", "DELETE")

    def schema_url(self) -> str:
        return self.schema_source.url or self.base_url.rstrip("/") + "/openapi.json"


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_field: str                  # location-keyed: "body:target.id", "query:limit"
    source_arg: str | None             # an argument the caller supplied, or null if none fits
    transform: Transform = "none"
    evidence: str = ""                 # quote from the authoritative schema supporting the mapping


class ResponseMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output: Literal["status", "echo"]
    response_path: str
    evidence: str = ""


class LLMRepairPlan(BaseModel):
    """The ONLY shape accepted from a model. It can name fields and arguments; it cannot carry values,
    credentials, code or arbitrary operations."""
    model_config = ConfigDict(extra="forbid")
    diagnosis: str = Field(max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    affected_fields: list[str] = Field(default_factory=list, max_length=30)
    mappings: list[Mapping] = Field(default_factory=list, max_length=30)
    response_mappings: list[ResponseMapping] = Field(default_factory=list, max_length=4)
    compatibility_risks: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)
    verification_steps: list[str] = Field(default_factory=list, max_length=8)


class RepairPlan(LLMRepairPlan):
    source: Literal["rules", "llm", "reuse"] = "rules"
    header_sources: dict[str, str] = {}     # header -> credential group (decided by configuration only)
    unresolved: list[str] = []              # required fields no evidence-backed source was found for


class ValidationResult(BaseModel):
    accepted: bool
    checks: list[dict] = []                 # {"name", "ok", "detail"}
    rejected_reasons: list[str] = []


class VerificationResult(BaseModel):
    ok: bool
    checks: list[dict] = []
    contract_drift: bool = False            # response fields missing: possible response-schema drift


class Step(BaseModel):
    ts: str = Field(default_factory=now_iso)
    stage: str                              # request | classify | discover | diff | plan | validate | apply |
                                            # retry | verify | rollback | outcome | credential
    status: Literal["ok", "warn", "fail", "info"] = "info"
    title: str
    detail: dict | str | None = None


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: "inc_" + uuid.uuid4().hex[:12])
    correlation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    tool: str
    simulated: bool = False
    scenario: str | None = None
    healing: bool = True                    # False only for baseline comparison runs (repair disabled)
    environment: str = ""                   # LIVE_API | REAL_API_TEST | SIMULATION (set by the store)
    started_at: str = Field(default_factory=now_iso)
    finished_at: str | None = None
    category: str = "unknown"
    status: Literal["open", "recovered", "stopped", "failed", "rolled_back"] = "open"
    reason: str = ""
    zero_touch: bool = False
    duration_ms: float | None = None
    retries: int = 0
    repair_attempts: int = 0
    from_version: int | None = None
    to_version: int | None = None
    rolled_back: bool = False
    old_signature: str | None = None
    new_signature: str | None = None
    drift: dict | None = None
    plan: dict | None = None
    validation: dict | None = None
    verification: dict | None = None
    llm: dict | None = None
    timeline: list[Step] = []
    result: dict | None = None

    def add(self, stage: str, title: str, status: str = "info", detail=None) -> Step:
        s = Step(stage=stage, status=status, title=title, detail=detail)
        self.timeline.append(s)
        return s
