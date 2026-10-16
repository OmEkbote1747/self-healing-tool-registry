"""Recovery for the Live Project Integration (PostgreSQL demo target), reusing the ADAPTIX planner and validator.

One experiment:
  1  execute the original operation (INSERT an order) and verify it inside ONE transaction
  2  on failure: classify by SQLSTATE - only schema-related errors on a changed catalog are schema drift
  3  read the authoritative catalog and diff it against the tool's registered schema
  4  ask the SELECTED engine (Gemini, or the Local Repair Module) for a repair proposal - no silent switching
  5  validate the proposal independently (same validator as the API pipeline: evidence, types, no invented
     or silently dropped values)
  6  test the candidate in the isolated sandbox schema (copied table structure, transaction rolled back)
  7  apply it to the demo target's tool definition as a new registry version (optimistic version check)
  8  retry the original operation with bounded retries (transient errors only), same idempotency key
  9  verify the business outcome by reading the row back in the same transaction; commit only if verified
  10 if verification fails: roll the transaction back (nothing persists) and roll the registry back
The database schema itself is never "fixed": ADAPTIX adapts the tool, and only inside the demo schema.
"""
from __future__ import annotations

import importlib
import threading
import time
import uuid
from types import SimpleNamespace

from . import evidence as evidence_mod
from . import livedb
from .credentials import CredentialRegistry
from .diff import diff, request_conflicts, sent_locations
from .models import Incident, LLMRepairPlan, ToolSpec, SchemaSource, now_iso
from .planner import plan_request
from .security import redact, scrub
from .storage import ConflictError
from .tooldef import ToolDef, apply_transform, py_type
from .validator import validate

TOOL = "live_create_order"
DEFAULT_ARGS = {"customer_id": "cust-0003", "sku": "SKU-200", "quantity": 2, "note": "deliver after 5pm"}
SCHEMA_SQLSTATES = {"42703": "undefined_column", "23502": "not_null_violation", "42804": "datatype_mismatch",
                    "22P02": "invalid_text_representation", "42883": "undefined_function"}
OTHER_SQLSTATES = {"42P01": ("endpoint_not_found", "the table no longer exists"),
                   "28P01": ("credential_invalid", "the database rejected the password"),
                   "28000": ("credential_invalid", "the database rejected the role"),
                   "42501": ("authorization_failed", "the role lacks a privilege"),
                   "57014": ("timeout", "the statement timed out"),
                   "40001": ("rate_limited", "serialization failure (transient)"),
                   "40P01": ("rate_limited", "deadlock detected (transient)"),
                   "23505": ("business_rule", "unique constraint"), "23503": ("business_rule", "foreign key"),
                   "23514": ("business_rule", "check constraint")}
TRANSIENT = {"40001", "40P01", "57P01"}


class EngineUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------- repair engines
def local_module_name() -> str | None:
    from .config import env
    return env("ADAPTIX_LOCAL_REPAIR_MODULE")


def engines(state) -> dict:
    from .llm import is_live
    g = state.llm.status()
    mod = local_module_name()
    local = {"available": False, "detail": "ADAPTIX_LOCAL_REPAIR_MODULE is not set: the Local Repair Module has "
                                           "not been provided yet", "module": None}
    if mod:
        try:
            m = importlib.import_module(mod)
            ok = callable(getattr(m, "propose", None))
            local = {"available": ok, "module": mod,
                     "detail": f"module {mod} loaded" if ok else f"module {mod} has no propose(context) function"}
        except Exception as e:
            local = {"available": False, "module": mod, "detail": f"cannot import {mod}: {type(e).__name__}"}
    return {"gemini": {"available": is_live(state.llm), "model": g.get("model"),
                       "detail": "real Gemini via google-genai" if is_live(state.llm) else
                       "GEMINI_API_KEY is not configured"},
            "local": local}


def propose(state, engine: str, context: dict) -> tuple[LLMRepairPlan | None, dict]:
    """Ask exactly the selected engine. Unavailable engines raise; nothing falls back to another engine."""
    t0 = time.perf_counter()
    if engine == "gemini":
        from .llm import is_live
        if not is_live(state.llm):
            raise EngineUnavailable("Gemini is not configured on this server (GEMINI_API_KEY)")
        res = state.llm.plan(context)
        meta = res.meta() | {"engine": "gemini"}
        return (res.plan if res.ok else None), meta
    if engine == "local":
        mod = local_module_name()
        info = engines(state)["local"]
        if not info["available"]:
            raise EngineUnavailable("Local Repair Module unavailable: " + info["detail"])
        m = importlib.import_module(mod)
        meta = {"engine": "local", "provider": "local-repair-module", "model": mod, "real": True, "attempts": 1}
        try:
            raw = m.propose(dict(context))
            plan = LLMRepairPlan.model_validate(raw)            # untrusted output: strict schema
            meta.update(ok=True, error_category=None)
        except Exception as e:
            plan = None
            meta.update(ok=False, error_category="invalid_output", error=scrub(str(e))[:200])
        meta["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return plan, meta
    raise EngineUnavailable(f"unknown engine {engine!r}")


# ---------------------------------------------------------------- registration of the demo tool
def register(state, t: livedb.Target) -> ToolDef:
    """(Re)register the demo tool against the CURRENT catalog; this becomes the baseline schema."""
    st = state.livedb_storage
    op = livedb.snapshot(t)
    if st.tool(TOOL):
        st.delete_tool(TOOL)
    request_map = {loc: [loc.split(":", 1)[1], "none"] for loc in op.fields}
    td = ToolDef(TOOL, 1, "INSERT", f"{t.schema}.{livedb.TABLE}", request_map, {}, {})
    spec = ToolSpec(name=TOOL, base_url=f"pg://{t.display()['host']}/{t.schema}", path="/orders",
                    method="POST", provider="Live Project Integration (PostgreSQL demo schema)",
                    description="Insert an order into the demo schema", schema_source=SchemaSource(kind="postgres_catalog"),
                    echo_output=None, monitor_enabled=False)
    st.register(spec, td, op)
    st.set_meta("baseline_signature", td.schema_signature)
    return td


# ---------------------------------------------------------------- the operation and its verification
def _execute(conn, t: livedb.Target, td: ToolDef, args: dict, request_key: str, schema: str | None = None) -> dict:
    """INSERT the order and read it back IN THE SAME TRANSACTION. Commit is the caller's decision."""
    from psycopg import sql
    schema = schema or t.schema
    body = td.build(args)["body"] or {}
    cols = list(body) + ["request_key"]
    vals = [body[k] for k in body] + [request_key]
    conn.execute(sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT (request_key) DO NOTHING").format(
        sql.Identifier(schema), sql.Identifier(livedb.TABLE), sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(sql.Placeholder() * len(cols))), vals)
    cur = conn.execute(sql.SQL("SELECT * FROM {}.{} WHERE request_key = %s").format(
        sql.Identifier(schema), sql.Identifier(livedb.TABLE)), (request_key,))
    names = [d.name for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    return {"sent": {k: v for k, v in body.items()}, "rows": rows}


def business_check(td: ToolDef, args: dict, rows: list[dict]) -> dict:
    """Independent check of the INTENDED outcome: exactly one stored order carrying every value the caller gave."""
    if len(rows) != 1:
        return {"ok": False, "detail": f"expected exactly one stored order, found {len(rows)}"}
    row = rows[0]
    checks = {}
    for loc, (arg, transform) in td.request_map.items():
        if arg not in args or args[arg] is None:
            continue
        col = loc.split(":", 1)[1]
        want = apply_transform(args[arg], transform)
        checks[f"{arg} -> {col}"] = col in row and str(row[col]) == str(want)
    intent = {a for a, v in args.items() if v is not None}
    covered = {loc_arg for loc_arg, _ in td.request_map.values()}
    missing = sorted(intent - covered)
    if missing:
        checks["every caller value stored"] = False
    ok = bool(checks) and all(checks.values())
    return {"ok": ok, "checks": checks, "missing_intent": missing,
            "stored": {k: (str(v) if v is not None else None) for k, v in row.items() if k != "created_at"},
            "detail": "stored order matches the caller's intent" if ok else "stored order does not match the intent"}


def _classify(sqlstate: str | None, changed: bool | None, conflicts: list[str]) -> tuple[str, str]:
    if sqlstate in OTHER_SQLSTATES:
        return OTHER_SQLSTATES[sqlstate]
    if sqlstate in SCHEMA_SQLSTATES:
        if changed and conflicts:
            return "schema_drift", "the catalog changed and the operation conflicts with it: " + "; ".join(conflicts)[:200]
        if changed is False:
            return "invalid_input", f"{SCHEMA_SQLSTATES[sqlstate]} but the catalog is unchanged: the input is wrong"
        return "unsupported_change", f"{SCHEMA_SQLSTATES[sqlstate]}: the catalog changed in a way not touching this call"
    if sqlstate and sqlstate.startswith("08"):
        return "network", "connection failure"
    return "unknown", f"SQLSTATE {sqlstate}"


# ---------------------------------------------------------------- one experiment
def run(state, t: livedb.Target, engine: str, args: dict | None = None, expected: str | None = None,
        trigger: str = "manual") -> dict:
    import psycopg
    args = dict(args or DEFAULT_ARGS)
    st = state.livedb_storage
    if st.tool(TOOL) is None:
        raise livedb.LiveDBError("invalid", "the demo is not set up - run Setup first")
    run_id = "dbx_" + uuid.uuid4().hex[:10]
    phases: dict[str, float] = {}
    t_all = time.perf_counter()
    td = st.current(TOOL)
    registered = st.get_schema(td.schema_signature)
    inc = Incident(tool=TOOL, environment=st.environment, scenario=f"live-db:{engine}")
    inc.from_version = td.version
    request_key = "adx-" + uuid.uuid4().hex
    res = {"run_id": run_id, "engine": engine, "trigger": trigger, "args": args}

    def phase(name, t0):
        phases[name] = round((time.perf_counter() - t0) * 1000, 1)

    # 1. original operation
    t0 = time.perf_counter()
    sqlstate, err = None, None
    with livedb.connect(t) as c:
        try:
            out = _execute(c, t, td, args, request_key)
            biz = business_check(td, args, out["rows"])
            if biz["ok"]:
                c.commit()
            else:
                c.rollback()
        except psycopg.Error as e:
            c.rollback()
            sqlstate, err, out, biz = e.sqlstate, scrub(str(e)).split("\n")[0][:240], None, None
    phase("execute", t0)
    inc.add("request", f"original operation with v{td.version}: " + ("SQLSTATE " + str(sqlstate) if sqlstate else
                                                                     "succeeded"),
            "fail" if sqlstate else "ok", {"sent": (out or {}).get("sent") or td.build(args)["body"],
                                           "sqlstate": sqlstate, "error": err})
    if sqlstate is None and biz and biz["ok"]:
        res.update(outcome="succeeded_without_incident", status="ok", business=biz, phases=phases,
                   total_ms=round((time.perf_counter() - t_all) * 1000, 1))
        ev = _evidence(state, t, None, res, expected, biz)
        res["evidence_id"] = ev["evidence_id"]
        return res
    if sqlstate is None:
        inc.add("verify", "operation ran but the business outcome was not confirmed; transaction rolled back", "fail", biz)
        return _finish(state, t, inc, res, phases, t_all, "verification_failed", biz["detail"], "stopped", expected, biz)

    # 2-3. authoritative catalog + diff + classification
    t0 = time.perf_counter()
    try:
        new = livedb.snapshot(t, registered)
    except livedb.LiveDBError as e:
        inc.add("discover", "catalog unavailable: " + str(e), "fail")
        return _finish(state, t, inc, res, phases, t_all, "schema_unavailable", str(e), "stopped", expected)
    rep = diff(registered, new)
    conflicts = request_conflicts(rep, sent_locations(td.build(args)), set())
    phase("diagnose", t0)
    inc.old_signature, inc.new_signature = td.schema_signature, new.signature()
    inc.drift = rep.model_dump()
    inc.add("discover", f"authoritative catalog read from {t.schema}.{livedb.TABLE}", "ok", {"source": "postgres_catalog"})
    inc.add("diff", f"Drift report: {rep.summary}", "info", {"compatibility": rep.compatibility, "conflicts": conflicts})
    category, why = _classify(sqlstate, rep.changed, conflicts)
    inc.category = category
    inc.add("classify", why, "warn" if category == "schema_drift" else "fail", {"category": category, "sqlstate": sqlstate})
    if category != "schema_drift":
        return _finish(state, t, inc, res, phases, t_all, category, why, "stopped", expected)

    # 4. proposal from the selected engine (rules plan kept only as a cross-check)
    t0 = time.perf_counter()
    rules = plan_request(td, SimpleNamespace(name=TOOL, auth_header=None, credential_group=None), new, args,
                         CredentialRegistry([]), state.settings.auto_min_similarity)
    kept = [m for m in rules.mappings if m.evidence == "unchanged field"]
    targets = [loc for loc in new.fields if loc not in {m.target_field for m in kept}]
    used = {m.source_arg for m in kept}
    context = {"tool": TOOL, "drift_summary": rep.summary,
               "unresolved_fields": [{"field": loc, "type": new.fields[loc].label(), "description": new.fields[loc].description}
                                     for loc in targets],
               "candidate_arguments": [{"name": a, "type": py_type(v)} for a, v in args.items() if a not in used],
               "database_error": {"sqlstate": sqlstate, "kind": SCHEMA_SQLSTATES.get(sqlstate)}}
    inc.add("plan", f"Rules cross-check: {len(rules.mappings)} mapping(s), {len(rules.unresolved)} unresolved", "info",
            {"mappings": [m.model_dump() for m in rules.mappings], "unresolved": rules.unresolved})
    if not targets:
        # every changed column is still present (e.g. a type change): the deterministic lossless conversion is
        # the whole repair; there is nothing for an engine to decide, so none is called (and none is claimed)
        phase("propose", t0)
        plan = rules.model_copy(update={"source": "rules"})
        inc.plan = plan.model_dump()
        inc.add("plan", f"No field needs a proposal from '{engine}': the change is handled by a deterministic, "
                        "lossless conversion (engine not called)", "info",
                {"mappings": [m.model_dump() for m in plan.mappings]})
        return _validate_apply_retry(state, t, inc, res, phases, t_all, td, new, plan, args, request_key, engine,
                                     expected)
    try:
        proposal, meta = propose(state, engine, context)
    except EngineUnavailable as e:
        phase("propose", t0)
        inc.add("plan", f"Engine '{engine}' unavailable: {e}. Not switching engines.", "fail")
        return _finish(state, t, inc, res, phases, t_all, "schema_drift", f"repair engine unavailable: {e}",
                       "stopped", expected)
    phase("propose", t0)
    inc.llm = redact(meta)
    extra = [m for m in (proposal.mappings if proposal else []) if m.source_arg and m.target_field in targets]
    plan = rules.model_copy(update={
        "source": "llm", "mappings": kept + extra, "confidence": proposal.confidence if proposal else 0.0,
        "diagnosis": proposal.diagnosis if proposal else f"{engine} returned no usable proposal",
        "unresolved": [x for x in targets if new.fields[x].required and x not in {m.target_field for m in extra}],
        "compatibility_risks": rules.compatibility_risks + (proposal.compatibility_risks if proposal else [])})
    inc.plan = plan.model_dump()
    inc.add("plan", f"{engine} proposed {len(extra)} mapping(s)" + ("" if proposal else
                                                                     f" (no usable output: {meta.get('error_category')})"),
            "info" if proposal else "fail", {"mappings": [m.model_dump() for m in extra], "engine": meta})
    return _validate_apply_retry(state, t, inc, res, phases, t_all, td, new, plan, args, request_key, engine, expected)


def _validate_apply_retry(state, t, inc, res, phases, t_all, td, new, plan, args, request_key, engine, expected):
    import psycopg
    st = state.livedb_storage

    def phase(name, t0):
        phases[name] = round((time.perf_counter() - t0) * 1000, 1)

    # 5. independent validation
    t0 = time.perf_counter()
    vres, cand = validate(plan, td, SimpleNamespace(name=TOOL, auth_header=None, credential_group=None), new, args,
                          CredentialRegistry([]), state.settings.auto_min_similarity, state.settings.llm_min_confidence)
    phase("validate", t0)
    inc.validation = vres.model_dump()
    inc.add("validate", "Repair accepted by independent validation" if vres.accepted else
            "Repair rejected: " + "; ".join(vres.rejected_reasons)[:300], "ok" if vres.accepted else "fail",
            {"checks": vres.checks})
    if not vres.accepted:
        return _finish(state, t, inc, res, phases, t_all, "schema_drift",
                       "Stopped safely: the repair needs human review (" + "; ".join(vres.rejected_reasons)[:300] + ")",
                       "stopped", expected)

    # 6. sandbox test: same structure, isolated schema, transaction always rolled back
    t0 = time.perf_counter()
    from psycopg import sql
    with livedb.connect(t, 10000) as c:
        try:
            c.execute(sql.SQL("DROP TABLE IF EXISTS {}.{}").format(sql.Identifier(t.sandbox), sql.Identifier(livedb.TABLE)))
            c.execute(sql.SQL("CREATE TABLE {}.{} (LIKE {}.{} INCLUDING ALL)").format(
                sql.Identifier(t.sandbox), sql.Identifier(livedb.TABLE), sql.Identifier(t.schema), sql.Identifier(livedb.TABLE)))
            sb = _execute(c, t, cand, args, "sandbox-" + request_key, schema=t.sandbox)
            sbiz = business_check(cand, args, sb["rows"])
            sandbox = {"ok": sbiz["ok"], "detail": sbiz["detail"]}
        except psycopg.Error as e:
            sandbox = {"ok": False, "detail": f"SQLSTATE {e.sqlstate}: " + scrub(str(e)).split(chr(10))[0][:160]}
        finally:
            c.rollback()                                        # nothing from the sandbox test persists
    phase("sandbox_test", t0)
    inc.add("validate", "Sandbox test " + ("passed" if sandbox["ok"] else "failed") + f": {sandbox['detail']}",
            "ok" if sandbox["ok"] else "fail", sandbox)
    if not sandbox["ok"]:
        return _finish(state, t, inc, res, phases, t_all, "schema_drift", "repair failed in the sandbox; not applied",
                       "stopped", expected)

    # 7. apply to the demo target's tool definition (versioned)
    t0 = time.perf_counter()
    st.save_schema(new)
    try:
        cand = st.apply_repair(cand, td.version, "repair", inc.id, inc.correlation_id)
    except ConflictError as e:
        return _finish(state, t, inc, res, phases, t_all, "schema_drift", f"registry changed during repair ({e})",
                       "stopped", expected)
    phase("apply", t0)
    inc.to_version = cand.version
    inc.add("apply", f"Tool updated v{td.version} -> v{cand.version} (previous version kept)", "ok", {"patch": cand.patch})

    # 8-9. bounded retry with the same idempotency key; verify in the same transaction
    t0 = time.perf_counter()
    biz, last_state = None, None
    for attempt in range(1, state.settings.max_transport_retries + 2):
        with livedb.connect(t) as c:
            try:
                out = _execute(c, t, cand, args, request_key)
                biz = business_check(cand, args, out["rows"])
                (c.commit if biz["ok"] else c.rollback)()
                inc.add("request", f"retry with v{cand.version}: inserted; business outcome "
                        + ("verified" if biz["ok"] else "NOT verified"), "ok" if biz["ok"] else "fail",
                        {"sent": out["sent"], "attempt": attempt})
                break
            except psycopg.Error as e:
                c.rollback()
                last_state = e.sqlstate
                inc.add("request", f"retry with v{cand.version}: SQLSTATE {e.sqlstate}", "fail", {"attempt": attempt})
                inc.retries += 1
                if e.sqlstate not in TRANSIENT:
                    break
                time.sleep(min(0.2 * 2 ** attempt, 2.0))
    phase("retry_and_verify", t0)
    if biz and biz["ok"]:
        inc.verification = {"ok": True, "checks": [{"name": k, "ok": v, "detail": ""} for k, v in biz["checks"].items()]}
        inc.add("verify", "Business outcome verified by read-back in the same transaction (committed)", "ok", biz)
        how = f"using {engine}" if plan.source == "llm" else "with a deterministic conversion (engine not needed)"
        inc.status, inc.zero_touch, inc.reason = "recovered", True, f"recovered with tool v{cand.version} {how}"
        inc.add("outcome", inc.reason, "ok")
        return _finish(state, t, inc, res, phases, t_all, "schema_drift", inc.reason, "recovered", expected, biz)
    # 10. rollback: the transaction already rolled back; now restore the registry
    inc.verification = {"ok": False, "checks": [{"name": k, "ok": v, "detail": ""} for k, v in (biz or {}).get("checks", {}).items()]}
    restored = st.rollback(TOOL, cand.version, "retry did not verify the business outcome", inc.correlation_id)
    inc.rolled_back = restored is not None
    inc.add("rollback", f"Rolled back v{cand.version} -> v{restored.version if restored else '?'}; the failed "
            "transaction left no rows", "warn" if restored else "fail")
    return _finish(state, t, inc, res, phases, t_all, "schema_drift",
                   "repair rolled back: " + ((biz or {}).get("detail") or f"SQLSTATE {last_state}"), "rolled_back",
                   expected, biz)


def _finish(state, t, inc: Incident, res: dict, phases: dict, t_all: float, category: str, reason: str, status: str,
            expected: str | None, biz: dict | None = None) -> dict:
    inc.category = inc.category if inc.category != "unknown" else category
    if status != "recovered":
        inc.status, inc.reason = status, reason
        if not inc.timeline or inc.timeline[-1].stage != "outcome":
            inc.add("outcome", reason, "fail")
    inc.duration_ms = round((time.perf_counter() - t_all) * 1000, 1)
    inc.finished_at = now_iso()
    state.livedb_storage.save_incident(inc)
    res.update(outcome=inc.status, reason=inc.reason, incident=inc.id, category=inc.category, phases=phases,
               total_ms=inc.duration_ms, business=biz, tool_version=inc.to_version or inc.from_version,
               rolled_back=inc.rolled_back, patch=(state.livedb_storage.current(TOOL).patch
                                                   if inc.status == "recovered" else None))
    ev = _evidence(state, t, inc.model_dump(mode="json"), res, expected, biz)
    res["evidence_id"] = ev["evidence_id"]
    return res


def _evidence(state, t, inc_dict, res, expected, biz):
    eng = SimpleNamespace(storage=state.livedb_storage)
    return evidence_mod.build(eng, inc_dict, run_id=res["run_id"], scenario=f"live-db:{res['engine']}", tool=TOOL,
                              expected=expected, expected_category="schema_drift" if expected in
                              ("recovered", "stopped") else None, business=biz, result=res,
                              extra={"phase_ms": res.get("phases"), "engine": res["engine"], "trigger": res["trigger"],
                                     "target": t.display()})


# ---------------------------------------------------------------- expectations from the injected faults
def expected_outcome(state, args: dict | None = None) -> str | None:
    """What a correct run should end with, given the faults injected since the last reset."""
    faults = [f for f in fault_log(state) if f.get("ok")]
    used = {k for k, v in (args or DEFAULT_ARGS).items() if v is not None}
    outcome = "succeeded"            # no relevant change: the operation should simply succeed
    for f in faults:
        if f["fault"] == "add_required_column":
            return "stopped"         # no trusted value exists for the new column
        if f["fault"] == "remove_column" and f["column"] in used:
            return "stopped"         # the caller's value would be silently dropped
        if f["fault"] == "change_type" and f["column"] in used and f.get("new_type") == "jsonb":
            return "stopped"         # no lossless conversion from free text to JSON
        if f["fault"] == "rename_column" and f["column"] in used:
            outcome = "recovered"    # catalog-proven rename: a validated repair exists
        # other type changes (text, varchar, bigint, numeric, integer) are coerced by PostgreSQL on INSERT
    return outcome


def fault_log(state) -> list[dict]:
    since = state.livedb_storage.get_meta("reset_at") or ""
    return [e["data"] for e in reversed(state.livedb_storage.events(kind="fault_injected", limit=500)) if e["ts"] >= since]


# ---------------------------------------------------------------- scheduled automatic injection
SCHEDULE_DEFAULT = {"enabled": False, "interval_s": 300, "run_limit": 5, "runs_done": 0, "auto_recover": True,
                    "engine": "gemini", "faults": ["rename_column", "change_type"], "last_run": None}
PRESETS = {"rename_column": {"column": "customer_id", "new_name": "client_id"},
           "change_type": {"column": "quantity", "new_type": "text"},
           "add_required_column": {"new_name": "channel"},
           "remove_column": {"column": "note"}}


class Scheduler:
    def __init__(self, state, tick_s: float = 5.0):
        self.state, self.tick_s = state, tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def settings(self) -> dict:
        import json
        raw = self.state.livedb_storage.get_meta("schedule")
        return {**SCHEDULE_DEFAULT, **(json.loads(raw) if raw else {})}

    def save(self, cfg: dict):
        import json
        self.state.livedb_storage.set_meta("schedule", json.dumps(cfg))

    def due(self, now: float | None = None) -> bool:
        cfg = self.settings()
        if not cfg["enabled"] or cfg["runs_done"] >= cfg["run_limit"]:
            return False
        last = cfg["last_run"]
        return last is None or (now or time.time()) - last >= cfg["interval_s"]

    def tick(self, now: float | None = None) -> dict | None:
        if not self.due(now) or not self.lock.acquire(blocking=False):
            return None
        try:
            cfg = self.settings()
            fault = cfg["faults"][cfg["runs_done"] % len(cfg["faults"])]
            t = livedb.target_from_env()
            out = {"fault": record_fault(self.state, t, fault, **PRESETS[fault], trigger="scheduled")}
            if cfg["auto_recover"]:
                out["experiment"] = run(self.state, t, cfg["engine"], expected=expected_outcome(self.state, None),
                                        trigger="scheduled")
            cfg.update(runs_done=cfg["runs_done"] + 1, last_run=now or time.time())
            self.save(cfg)
            return out
        except Exception as e:
            self.state.livedb_storage.event("schedule_error", TOOL, {"error": scrub(str(e))[:200]})
            cfg = self.settings()
            cfg.update(last_run=now or time.time())
            self.save(cfg)
            return {"error": scrub(str(e))[:200]}
        finally:
            self.lock.release()

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while not self._stop.wait(self.tick_s):
                try:
                    self.tick()
                except Exception:
                    pass
        self._thread = threading.Thread(target=loop, name="adaptix-livedb-scheduler", daemon=True)
        self._thread.start()


def record_fault(state, t: livedb.Target, fault: str, trigger: str = "manual", **kw) -> dict:
    out = livedb.inject(t, fault, **kw)
    out["trigger"] = trigger
    out["ts"] = now_iso()
    state.livedb_storage.event("fault_injected", TOOL, out)
    return out
