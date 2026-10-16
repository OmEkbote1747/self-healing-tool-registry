"""Live Project Integration - schema-drift experiments and recovery on the PostgreSQL demo schema.

The app reaches its data through BUSINESS WORKFLOWS whose SQL is fixed in code; only the COLUMN MAPPING (logical
reference such as "orders.customer_id" -> physical column, plus an optional allow-listed cast) is versioned in
the ADAPTIX registry. A repair therefore changes the mapping - never the database - and only inside the demo.

One experiment (explicit user action; every stage is persisted to adaptix_experiments / adaptix_audit_log):
   1 baseline test      the workflow succeeds AND its records/values match the seed data
   2 fault injection    one reversible fault (A-F) on the demo schema or the mapping
   3 failure detection  the same workflow is run again; the real error is captured (SQLSTATE + primary message)
   4 schema retrieval   authoritative catalog (renames proven by attnum)
   5 diagnosis          catalog diff + classification; auth/permission/timeout/constraint errors are not drift
   6 proposal           ONLY the selected engine (Gemini Flash-Lite or the Local Repair Module); never switched
   7 validation         independent: live catalog, same table, catalog identity, allowed casts, required columns,
                        verbatim evidence, confidence
   8 sandbox test       patched workflow on a copy of the demo tables in the sandbox schema; always rolled back
   9 patch applied      new registry version (optimistic version check), exact before/after diff
  10 retry              bounded retries (transient errors only), statement timeouts, transactions
  11 verification       returned records and calculations compared with the seed data, independently of the
                        mapping; a write is committed only if verified
  12 rollback           on failed verification: transaction rolled back and registry version restored
Nothing about customers is sent to an LLM: only table/column names, types, comments, the error and the query
template.
"""
from __future__ import annotations

import importlib
import json
import statistics
import threading
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

from database import seed_data as sd

from . import evidence as evidence_mod
from . import livedb
from .diff import diff
from .models import Incident, LLMRepairPlan, SchemaSource, ToolSpec, now_iso
from .names import entity_mismatch, unit_change
from .security import redact, scrub
from .storage import ConflictError
from .tooldef import ToolDef

CASTS = {"none": "", "to_float": "::numeric", "to_int": "::bigint", "to_str": "::text"}
WANT_TRANSFORM = {"float": "to_float", "int": "to_int", "str": "to_str"}
SCHEMA_SQLSTATES = {"42703": "undefined_column", "23502": "not_null_violation", "42804": "datatype_mismatch",
                    "22P02": "invalid_text_representation", "42883": "undefined_function"}
OTHER_SQLSTATES = {"42P01": ("endpoint_not_found", "a table no longer exists"),
                   "28P01": ("credential_invalid", "the database rejected the password"),
                   "28000": ("credential_invalid", "the database rejected the role"),
                   "42501": ("authorization_failed", "the role lacks a privilege"),
                   "57014": ("timeout", "the statement timed out"),
                   "40001": ("transient", "serialization failure"), "40P01": ("transient", "deadlock detected"),
                   "23505": ("business_rule", "unique constraint"), "23503": ("business_rule", "foreign key"),
                   "23514": ("business_rule", "check constraint")}
TRANSIENT = {"40001", "40P01", "57P01"}

WORKFLOWS = {
    "customer_orders": {"title": "List a customer's orders", "kind": "read", "params": {"customer_id": "C008"},
                        "refs": ["customers.customer_id", "orders.order_id", "orders.customer_id", "orders.order_date",
                                 "orders.order_status", "orders.total_amount"]},
    "order_details": {"title": "Retrieve an order and its items", "kind": "read", "params": {"order_id": "O1012"},
                      "refs": ["orders.order_id", "orders.customer_id", "orders.order_status", "orders.payment_method",
                               "orders.total_amount", "order_items.item_id", "order_items.order_id",
                               "order_items.product_id", "order_items.quantity", "order_items.unit_price",
                               "order_items.line_total", "products.product_id", "products.product_name"]},
    "order_total": {"title": "Calculate an order total", "kind": "read", "params": {"order_id": "O1012"},
                    "refs": ["orders.order_id", "orders.total_amount", "order_items.order_id", "order_items.line_total"]},
    "inventory_update": {"title": "Update inventory for a demo order", "kind": "write", "params": {"order_id": "O1039"},
                         "refs": ["order_items.order_id", "order_items.product_id", "order_items.quantity",
                                  "products.product_id", "products.stock_quantity", "inventory_events.product_id",
                                  "inventory_events.event_type", "inventory_events.quantity_change",
                                  "inventory_events.event_time", "inventory_events.notes"],
                         "insert_table": "inventory_events"},
}
SCENARIOS = {
    "A": {"title": "Rename customers.customer_id -> client_id", "workflow": "customer_orders", "expect": "recovered"},
    "B": {"title": "Rename orders.customer_id -> client_id", "workflow": "customer_orders", "expect": "recovered"},
    "C": {"title": "Change order_items.line_total numeric -> text (reversible)", "workflow": "order_total",
          "expect": "recovered"},
    "D": {"title": "Add required column inventory_events.source_system (no default)", "workflow": "inventory_update",
          "expect": "refused"},
    "E": {"title": "Remove orders.payment_method (referenced by a query)", "workflow": "order_details",
          "expect": "refused"},
    "F": {"title": "Query/ORM mapping mismatch: orders.order_status mapped to a non-existent column",
          "workflow": "customer_orders", "expect": "recovered"},
}
BAD_MAPPING_F = ("orders.order_status", "status")


class EngineUnavailable(RuntimeError):
    pass


class Stopped(RuntimeError):
    pass


def tool_name(workflow: str) -> str:
    return f"db_{workflow}"


# ---------------------------------------------------------------- repair engines (selected per experiment)
def local_module_name() -> str | None:
    from .config import env
    return env("ADAPTIX_LOCAL_REPAIR_MODULE")


def engines(state) -> dict:
    from .llm import is_live
    g = state.llm.status()
    mod = local_module_name()
    local = {"available": False, "status": "not configured", "module": None,
             "detail": "Local Repair Module not configured: ADAPTIX_LOCAL_REPAIR_MODULE is not set"}
    if mod:
        try:
            m = importlib.import_module(mod)
            ok = callable(getattr(m, "propose", None))
            local = {"available": ok, "module": mod, "status": "configured" if ok else "not configured",
                     "detail": f"module {mod} loaded" if ok else f"module {mod} has no propose(context) function"}
        except Exception as e:
            local = {"available": False, "module": mod, "status": "not configured",
                     "detail": f"cannot import {mod}: {type(e).__name__}"}
    live = is_live(state.llm)
    return {"gemini": {"available": live, "status": "configured" if live else "not configured",
                       "model": g.get("model"),
                       "detail": f"real Gemini ({g.get('model')}) via google-genai" if live else
                       "GEMINI_API_KEY is not configured on the server"},
            "local": local}


def propose(state, engine: str, context: dict) -> tuple[LLMRepairPlan | None, dict]:
    """Ask exactly the selected engine. An unavailable engine raises; nothing falls back to another engine."""
    t0 = time.perf_counter()
    info = engines(state).get(engine)
    if info is None:
        raise EngineUnavailable(f"unknown engine {engine!r}")
    if not info["available"]:
        raise EngineUnavailable(info["detail"])
    if engine == "gemini":
        res = state.llm.plan(context)
        return (res.plan if res.ok else None), res.meta() | {"engine": "gemini"}
    m = importlib.import_module(local_module_name())
    meta = {"engine": "local", "provider": "local-repair-module", "model": local_module_name(), "real": True,
            "attempts": 1}
    try:
        plan = LLMRepairPlan.model_validate(m.propose(json.loads(json.dumps(context))))   # untrusted: strict schema
        meta.update(ok=True, error_category=None)
    except Exception as e:
        plan = None
        meta.update(ok=False, error_category="invalid_output", error=scrub(str(e))[:200])
    meta["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return plan, meta


# ---------------------------------------------------------------- mapping registry (one versioned tool per workflow)
def register_all(state, t: livedb.Target):
    """(Re)register every workflow with the identity mapping against the CURRENT catalog (= the baseline)."""
    st = state.livedb_storage
    op = livedb.snapshot(t)
    for wf, spec in WORKFLOWS.items():
        name = tool_name(wf)
        if st.tool(name):
            st.delete_tool(name)
        request_map = {f"body:{ref}": [ref, "none"] for ref in spec["refs"]}
        td = ToolDef(name, 1, "SQL", t.schema, request_map, {}, {})
        st.register(ToolSpec(name=name, base_url=f"pg://{t.display()['host']}/{t.schema}", path=f"/{wf}",
                             method="POST", provider="Live Project Integration (Neon demo schema)",
                             description=spec["title"], schema_source=SchemaSource(kind="postgres_catalog"),
                             echo_output=None, monitor_enabled=False), td, op)
    st.set_meta("baseline_signature", op.signature())
    st.set_meta("reset_at", now_iso())


def mapping(td: ToolDef) -> dict[str, tuple[str, str, str]]:
    """ref -> (table, physical column, transform)."""
    out = {}
    for loc, (ref, transform) in td.request_map.items():
        tb, col = loc.split(":", 1)[1].split(".", 1)
        out[ref] = (tb, col, transform or "none")
    return out


def patch_diff(old: ToolDef, new: ToolDef) -> str:
    a, b = mapping(old), mapping(new)
    lines = []

    def fmt(m):
        return "(none)" if not m else f"{m[0]}.{m[1]}" + (CASTS[m[2]] if m[2] != "none" else "")
    for ref in sorted(set(a) | set(b)):
        if a.get(ref) != b.get(ref):
            lines += [f"- {ref:<34} -> {fmt(a.get(ref))}", f"+ {ref:<34} -> {fmt(b.get(ref))}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- the business workflows (fixed SQL, mapped columns)
def _col(m, ref, alias=None, cast=True):
    from psycopg import sql
    tb, col, tr = m[ref]
    ident = sql.SQL("{}.{}").format(sql.Identifier(alias), sql.Identifier(col)) if alias else sql.Identifier(col)
    return sql.SQL("{}{}").format(ident, sql.SQL(CASTS[tr] if cast else ""))


def run_workflow(conn, schema: str, wf: str, m: dict, params: dict, note: str | None = None) -> dict:
    """Execute a workflow through the mapping. Identifiers via psycopg.sql, values always bound."""
    from psycopg import sql

    def S(tb):
        return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(tb))

    def rows(cur):
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    if wf == "customer_orders":
        q = sql.SQL("SELECT {oid} AS order_id, {odate} AS order_date, {ost} AS order_status, {otot} AS total_amount "
                    "FROM {cust} c JOIN {ord} o ON {ocid} = {ccid} WHERE {ccid} = %s ORDER BY {odate}, {oid}").format(
            oid=_col(m, "orders.order_id", "o"), odate=_col(m, "orders.order_date", "o"),
            ost=_col(m, "orders.order_status", "o"), otot=_col(m, "orders.total_amount", "o"),
            ocid=_col(m, "orders.customer_id", "o"), ccid=_col(m, "customers.customer_id", "c"),
            cust=S("customers"), ord=S("orders"))
        return {"rows": rows(conn.execute(q, (params["customer_id"],)))}
    if wf == "order_details":
        h = sql.SQL("SELECT {oid} AS order_id, {ocid} AS customer_id, {ost} AS order_status, {pm} AS payment_method, "
                    "{tot} AS total_amount FROM {ord} o WHERE {oid} = %s").format(
            oid=_col(m, "orders.order_id", "o"), ocid=_col(m, "orders.customer_id", "o"),
            ost=_col(m, "orders.order_status", "o"), pm=_col(m, "orders.payment_method", "o"),
            tot=_col(m, "orders.total_amount", "o"), ord=S("orders"))
        i = sql.SQL("SELECT {iid} AS item_id, {ipid} AS product_id, {pname} AS product_name, {qty} AS quantity, "
                    "{up} AS unit_price, {lt} AS line_total FROM {items} oi JOIN {prod} p ON {ppid} = {ipid} "
                    "WHERE {ioid} = %s ORDER BY {iid}").format(
            iid=_col(m, "order_items.item_id", "oi"), ipid=_col(m, "order_items.product_id", "oi"),
            pname=_col(m, "products.product_name", "p"), qty=_col(m, "order_items.quantity", "oi"),
            up=_col(m, "order_items.unit_price", "oi"), lt=_col(m, "order_items.line_total", "oi"),
            ppid=_col(m, "products.product_id", "p"), ioid=_col(m, "order_items.order_id", "oi"),
            items=S("order_items"), prod=S("products"))
        header = rows(conn.execute(h, (params["order_id"],)))
        return {"header": header[0] if header else None, "items": rows(conn.execute(i, (params["order_id"],)))}
    if wf == "order_total":
        q = sql.SQL("SELECT {tot} AS stored_total, sum({lt}) AS computed_total, count(*) AS item_count "
                    "FROM {ord} o JOIN {items} oi ON {ioid} = {oid} WHERE {oid} = %s GROUP BY {tot_raw}").format(
            tot=_col(m, "orders.total_amount", "o"), tot_raw=_col(m, "orders.total_amount", "o", cast=False),
            lt=_col(m, "order_items.line_total", "oi"), ioid=_col(m, "order_items.order_id", "oi"),
            oid=_col(m, "orders.order_id", "o"), ord=S("orders"), items=S("order_items"))
        r = rows(conn.execute(q, (params["order_id"],)))
        return {"row": r[0] if r else None}
    if wf == "inventory_update":
        upd = sql.SQL("UPDATE {prod} p SET {stock_bare} = {stock} - x.qty FROM (SELECT {ipid} AS pid, sum({qty}) AS qty "
                      "FROM {items} oi WHERE {ioid} = %s GROUP BY {ipid}) x WHERE {ppid} = x.pid").format(
            prod=S("products"), stock_bare=_col(m, "products.stock_quantity", cast=False),
            stock=_col(m, "products.stock_quantity", "p"), ipid=_col(m, "order_items.product_id", "oi"),
            qty=_col(m, "order_items.quantity", "oi"), items=S("order_items"), ioid=_col(m, "order_items.order_id", "oi"),
            ppid=_col(m, "products.product_id", "p"))
        n_upd = conn.execute(upd, (params["order_id"],)).rowcount
        ins = sql.SQL("INSERT INTO {ev} ({epid}, {etype}, {eq}, {et}, {en}) SELECT {ipid}, 'sale', -{qty}, now(), %s "
                      "FROM {items} oi WHERE {ioid} = %s").format(
            ev=S("inventory_events"), epid=_col(m, "inventory_events.product_id", cast=False),
            etype=_col(m, "inventory_events.event_type", cast=False),
            eq=_col(m, "inventory_events.quantity_change", cast=False),
            et=_col(m, "inventory_events.event_time", cast=False), en=_col(m, "inventory_events.notes", cast=False),
            ipid=_col(m, "order_items.product_id", "oi"), qty=_col(m, "order_items.quantity", "oi"),
            items=S("order_items"), ioid=_col(m, "order_items.order_id", "oi"))
        n_ins = conn.execute(ins, (note, params["order_id"])).rowcount
        return {"products_updated": n_upd, "events_inserted": n_ins}
    raise ValueError(wf)


# ---------------------------------------------------------------- independent business verification
def _d(v) -> str:
    return str(Decimal(str(v)).quantize(Decimal("0.01")))


def resolve(conn, schema: str, baseline, table: str, column: str) -> str | None:
    """Current name of a BASELINE column, found by catalog attnum (independent of the repaired mapping)."""
    f = baseline.fields.get(f"body:{table}.{column}")
    if f is None:
        return None
    r = conn.execute("SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n "
                     "ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s AND a.attnum=%s AND NOT a.attisdropped",
                     (schema, table, f.schema["x-attnum"])).fetchone()
    return r[0] if r else None


def inventory_pre(conn, schema: str, baseline, order_id: str) -> dict:
    from psycopg import sql
    stock = resolve(conn, schema, baseline, "products", "stock_quantity")
    pid = resolve(conn, schema, baseline, "products", "product_id")
    want: dict[str, int] = {}
    for p, q in sd.order_lines(order_id):
        want[p] = want.get(p, 0) + q
    pre = {}
    for p in want:
        pre[p] = conn.execute(sql.SQL("SELECT {} FROM {}.products WHERE {} = %s").format(
            sql.Identifier(stock), sql.Identifier(schema), sql.Identifier(pid)), (p,)).fetchone()[0]
    return {"pre": pre, "deltas": want}


def verify(conn, schema: str, baseline, wf: str, params: dict, out: dict, pre: dict | None = None,
           note: str | None = None) -> dict:
    """Compare returned records/values with the seed data (or, for the write, with the pre-state)."""
    checks = {}
    if wf == "customer_orders":
        want = sd.expected_customer_orders(params["customer_id"])
        got = [{"order_id": r["order_id"], "order_date": str(r["order_date"]), "order_status": r["order_status"],
                "total_amount": _d(r["total_amount"])} for r in out["rows"]]
        checks["same orders, dates, statuses and totals as the seed data"] = got == want
        checks[f"{len(want)} order(s) returned"] = len(got) == len(want)
        detail = {"expected_orders": [w["order_id"] for w in want], "returned_orders": [g["order_id"] for g in got],
                  "expected_totals": [w["total_amount"] for w in want], "returned_totals": [g["total_amount"] for g in got]}
    elif wf == "order_details":
        want = sd.expected_order(params["order_id"])
        h = out["header"] or {}
        checks["order header matches"] = bool(h) and h.get("customer_id") == want["customer_id"] and \
            h.get("order_status") == want["order_status"] and h.get("payment_method") == want["payment_method"] and \
            _d(h.get("total_amount", 0)) == want["total_amount"]
        got = [{"item_id": r["item_id"], "product_id": r["product_id"], "product_name": r["product_name"],
                "quantity": int(r["quantity"]), "unit_price": _d(r["unit_price"]), "line_total": _d(r["line_total"])}
               for r in out["items"]]
        checks["items match (products, quantities, prices, line totals)"] = got == want["items"]
        detail = {"expected_items": len(want["items"]), "returned_items": len(got), "expected_total": want["total_amount"]}
    elif wf == "order_total":
        want = sd.expected_order_total(params["order_id"])
        r = out["row"] or {}
        got = {"stored_total": _d(r["stored_total"]) if r else None,
               "computed_total": _d(r["computed_total"]) if r else None, "item_count": r.get("item_count")}
        checks["computed total equals the expected total"] = got["computed_total"] == want["computed_total"]
        checks["stored total equals the computed total"] = got["stored_total"] == got["computed_total"] == want["stored_total"]
        checks["item count matches"] = got["item_count"] == want["item_count"]
        detail = {"expected": want, "returned": got}
    else:
        from psycopg import sql
        stock = resolve(conn, schema, baseline, "products", "stock_quantity")
        pid = resolve(conn, schema, baseline, "products", "product_id")
        ev_pid = resolve(conn, schema, baseline, "inventory_events", "product_id")
        ev_q = resolve(conn, schema, baseline, "inventory_events", "quantity_change")
        ev_n = resolve(conn, schema, baseline, "inventory_events", "notes")
        post, evs = {}, {}
        for p in pre["deltas"]:
            post[p] = conn.execute(sql.SQL("SELECT {} FROM {}.products WHERE {} = %s").format(
                sql.Identifier(stock), sql.Identifier(schema), sql.Identifier(pid)), (p,)).fetchone()[0]
            evs[p] = conn.execute(sql.SQL("SELECT coalesce(sum({}), 0) FROM {}.inventory_events WHERE {} = %s AND {} = %s")
                                  .format(sql.Identifier(ev_q), sql.Identifier(schema), sql.Identifier(ev_pid),
                                          sql.Identifier(ev_n)), (p, note)).fetchone()[0]
        checks["stock decreased by exactly the ordered quantity"] = all(post[p] == pre["pre"][p] - d
                                                                       for p, d in pre["deltas"].items())
        checks["one sale event per order line, with the right quantity"] = all(evs[p] == -d
                                                                              for p, d in pre["deltas"].items())
        checks["rows touched"] = out.get("products_updated") == len(pre["deltas"]) and \
            out.get("events_inserted") == len(sd.order_lines(params["order_id"]))
        detail = {"stock_before": pre["pre"], "stock_after": post, "ordered": pre["deltas"], "event_sums": evs}
    ok = bool(checks) and all(checks.values())
    return {"ok": ok, "checks": checks, "detail": json.loads(json.dumps(detail, default=str)),
            "summary": "returned records and values match the expected business result" if ok else
            "returned records or values do NOT match the expected business result"}


def attempt(t: livedb.Target, schema: str, baseline, wf: str, m: dict, params: dict, commit: bool,
            note: str | None) -> dict:
    """Run + verify in ONE transaction; commit only if verified and asked to."""
    import psycopg
    with livedb.connect(t) as c:
        try:
            pre = inventory_pre(c, schema, baseline, params["order_id"]) if wf == "inventory_update" else None
            out = run_workflow(c, schema, wf, m, params, note)
            v = verify(c, schema, baseline, wf, params, out, pre, note)
            (c.commit if (v["ok"] and commit) else c.rollback)()
            return {"ok": v["ok"], "verification": v, "sqlstate": None, "error": None,
                    "committed": bool(v["ok"] and commit)}
        except psycopg.Error as e:
            c.rollback()
            return {"ok": False, "verification": None, "sqlstate": e.sqlstate, "error": livedb.error_message(e),
                    "committed": False}


# ---------------------------------------------------------------- persisted experiment + audit records (Postgres)
class Log:
    def __init__(self, t: livedb.Target, experiment_id: str):
        self.t, self.id = t, experiment_id

    def _exec(self, q, args):
        from psycopg import sql
        with livedb.connect(self.t) as c:
            c.execute(sql.SQL(q).format(s=sql.Identifier(self.t.schema)), args)
            c.commit()

    def start(self, scenario: str, engine: str, fault: str):
        self._exec("INSERT INTO {s}.adaptix_experiments (experiment_id, scenario_name, selected_engine, injected_fault, "
                   "status) VALUES (%s, %s, %s, %s, 'running')", (self.id, scenario, engine, fault))

    def event(self, event_type: str, details: dict | None = None, patch: str | None = None,
              validation: dict | None = None, verification: dict | None = None):
        def j(x):
            return None if x is None else json.dumps(redact(x), default=str)
        self._exec("INSERT INTO {s}.adaptix_audit_log (experiment_id, event_type, details_json, patch_diff, "
                   "validation_result, verification_result) VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb)",
                   (self.id, event_type, j(details or {}), patch, j(validation), j(verification)))

    def finish(self, status: str, recovery_ms: float | None, verified: bool, rollback: bool, error: str | None):
        self._exec("UPDATE {s}.adaptix_experiments SET status=%s, completed_at=now(), recovery_ms=%s, verified=%s, "
                   "rollback_performed=%s, error_details=%s WHERE experiment_id=%s",
                   (status, recovery_ms, verified, rollback, scrub(error)[:500] if error else None, self.id))


def stats(t: livedb.Target) -> dict:
    """Real statistics from the persisted records. A proposed patch is never counted as a recovery."""
    from psycopg import sql
    s = sql.Identifier(t.schema)
    with livedb.connect(t, read_only=True) as c:
        exps = c.execute(sql.SQL("SELECT status, verified, rollback_performed, recovery_ms, selected_engine, "
                                 "scenario_name FROM {}.adaptix_experiments").format(s)).fetchall()
        ev = dict(c.execute(sql.SQL("SELECT event_type, count(DISTINCT experiment_id) FROM {}.adaptix_audit_log "
                                    "WHERE details_json->>'status' NOT IN ('failed', 'not reproduced') "
                                    "GROUP BY event_type").format(s)).fetchall())
        validated = c.execute(sql.SQL("SELECT count(*) FROM {}.adaptix_audit_log WHERE event_type='validation' "
                                      "AND (validation_result->>'accepted')::boolean").format(s)).fetchone()[0]
        c.rollback()
    times = [float(r[3]) for r in exps if r[0] == "recovered" and r[1] and r[3] is not None]
    by_status: dict[str, int] = {}
    for r in exps:
        by_status[r[0]] = by_status.get(r[0], 0) + 1
    return {"runs": len(exps), "by_status": by_status,
            "detected_faults": ev.get("failure_detected", 0),
            "proposals": ev.get("proposal", 0), "validated_patches": validated,
            "applied_patches": ev.get("patch_applied", 0),
            "verified_recoveries": sum(1 for r in exps if r[0] == "recovered" and r[1]),
            "failures": sum(1 for r in exps if r[0] in ("failed", "rolled_back")),
            "refused": by_status.get("refused", 0), "rollbacks": sum(1 for r in exps if r[2]),
            "avg_recovery_ms": round(statistics.mean(times), 1) if times else None,
            "median_recovery_ms": round(statistics.median(times), 1) if times else None,
            "recovery_samples": len(times),
            "note": "Recovery time runs from failure detection to verified completion; only verified recoveries are "
                    "timed. Small samples are demonstrations, not reliability guarantees."}


def export(t: livedb.Target) -> dict:
    from psycopg import sql
    s = sql.Identifier(t.schema)
    with livedb.connect(t, read_only=True) as c:
        cur = c.execute(sql.SQL("SELECT * FROM {}.adaptix_experiments ORDER BY started_at").format(s))
        names = [d.name for d in cur.description]
        exps = [dict(zip(names, r)) for r in cur.fetchall()]
        cur = c.execute(sql.SQL("SELECT * FROM {}.adaptix_audit_log ORDER BY audit_id").format(s))
        names = [d.name for d in cur.description]
        audit = [dict(zip(names, r)) for r in cur.fetchall()]
        c.rollback()
    return redact(json.loads(json.dumps({"experiments": exps, "audit_log": audit}, default=str)))


# ---------------------------------------------------------------- diagnosis, proposal context, validation
def _broken(m: dict, refs: list[str], base, cur) -> dict[str, str]:
    """ref -> why it no longer works against the current catalog."""
    out = {}
    for ref in refs:
        tb, col, tr = m[ref]
        f = cur.fields.get(f"body:{tb}.{col}")
        bf = base.fields.get(f"body:{ref}")
        if f is None:
            out[ref] = "mapped column does not exist"
        elif bf is not None and f.types != bf.types and tr == "none":
            out[ref] = f"type changed from {bf.schema.get('x-pg-type')} to {f.schema.get('x-pg-type')}"
    return out


def _new_required(wf: str, m: dict, cur) -> list[str]:
    tb = WORKFLOWS[wf].get("insert_table")
    if not tb:
        return []
    mapped = {f"body:{x[0]}.{x[1]}" for x in m.values() if x[0] == tb}
    return [loc for loc, f in cur.fields.items() if f.schema.get("x-table") == tb and f.required and loc not in mapped]


def classify(sqlstate: str | None, schema_changed: bool, broken: dict, required: list[str]) -> tuple[str, str]:
    if sqlstate in OTHER_SQLSTATES:
        return OTHER_SQLSTATES[sqlstate]
    if sqlstate and sqlstate.startswith("08"):
        return "network", "connection failure"
    if sqlstate in SCHEMA_SQLSTATES:
        if broken or required:
            what = "; ".join([f"{k}: {v}" for k, v in broken.items()] + [f"{r}: new required column" for r in required])
            if schema_changed:
                return "schema_drift", f"the catalog changed and the workflow conflicts with it ({what})"
            return "mapping_mismatch", f"the schema is unchanged but the query mapping is wrong ({what})"
        return "unknown", f"{SCHEMA_SQLSTATES[sqlstate]} not explained by the catalog"
    if sqlstate is None:
        return "verification_failed", "the workflow ran but its business result did not match"
    return "unknown", f"SQLSTATE {sqlstate}"


def context_for(wf: str, m: dict, base, cur, broken: dict, required: list[str], sqlstate: str, message: str,
                m_base: dict | None = None) -> dict:
    """What the engine sees: names, types, comments, the error and the query template. No row data."""
    tables = sorted({m[r][0] for r in WORKFLOWS[wf]["refs"]})
    mapped_now = {f"body:{x[0]}.{x[1]}": r for r, x in m.items()}
    unresolved = []
    for loc, f in cur.fields.items():
        tb = f.schema.get("x-table")
        if tb not in tables:
            continue
        typed = [r for r, why in broken.items() if "type changed" in why and f"body:{m[r][0]}.{m[r][1]}" == loc]
        missing_same_table = [r for r, why in broken.items() if "does not exist" in why and r.split(".")[0] == tb]
        if typed:
            need = base.fields[f"body:{typed[0]}"].type
            unresolved.append({"field": loc, "type": f.label(),
                               "description": ((f.description + " ") if f.description else "")
                               + f"(type changed; the workflow needs {need})"})
        elif (loc not in mapped_now and missing_same_table) or loc in required:
            desc = f.description or ""
            for r, (btb, bcol, _) in (m_base or {}).items():          # registry fact: what the baseline mapped here
                if f"body:{btb}.{bcol}" == loc and r in broken:
                    desc = (desc + " " if desc else "") + f"(baseline mapping: {r} -> {btb}.{bcol})"
            unresolved.append({"field": loc, "type": f.label(), "description": desc})

    def side(op):
        return {tb: [{"column": k.split(".", 1)[1], "type": f.schema.get("x-pg-type"),
                      "attnum": f.schema.get("x-attnum"), "comment": f.description}
                     for k, f in op.fields.items() if f.schema.get("x-table") == tb] for tb in tables}
    return {"workflow": wf, "workflow_title": WORKFLOWS[wf]["title"],
            "database_error": {"sqlstate": sqlstate, "message": message},
            "unresolved_fields": unresolved,
            "candidate_arguments": [{"name": r, "type": base.fields[f"body:{r}"].type if f"body:{r}" in base.fields
                                     else "str", "problem": why} for r, why in broken.items()],
            "current_mapping": {r: f"{x[0]}.{x[1]}" + (CASTS[x[2]] if x[2] != "none" else "") for r, x in m.items()},
            "baseline_schema": side(base), "current_schema": side(cur),
            "instructions": "Each candidate_arguments name is a logical column reference used by the query. Map it to "
                            "the unresolved field (a live column of the SAME table) that now holds the same data, with a "
                            "lossless transform if its type changed. Quote evidence verbatim from a column comment or a "
                            "'baseline mapping' line."}


def evidence_text(cur, base, m_baseline: dict) -> str:
    lines = [cur.evidence_text()]
    for ref, (tb, col, _) in m_baseline.items():
        lines.append(f"baseline mapping: {ref} -> {tb}.{col}")
    return "\n".join(lines)


def validate_mapping(plan: LLMRepairPlan | None, wf: str, m: dict, m_base: dict, base, cur, broken: dict,
                     required: list[str], min_conf: float, context: dict | None = None) -> tuple[dict, dict | None]:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok
    check("a structured proposal was returned", plan is not None)
    if plan is None:
        return {"accepted": False, "checks": checks, "rejected_reasons": ["no usable proposal"]}, None
    check("engine confidence", plan.confidence >= min_conf, f"{plan.confidence:.2f} (minimum {min_conf:.2f})")
    # evidence = catalog text + the field descriptions ADAPTIX itself derived from the catalog and the registry
    ev_text = evidence_text(cur, base, m_base) + "\n" + "\n".join(
        u.get("description") or "" for u in (context or {}).get("unresolved_fields", []))
    new_m = dict(m)
    used: set[str] = set()
    for mp in plan.mappings:
        if not mp.source_arg:
            continue
        tag = f"{mp.source_arg} -> {mp.target_field} ({mp.transform})"
        ref, loc = mp.source_arg, mp.target_field
        if not check("refers to a broken logical column of this workflow", ref in broken, tag):
            continue
        if not check("one-to-one", ref not in used, tag):
            continue
        f = cur.fields.get(loc)
        if not check("target column exists in the live catalog", f is not None, tag):
            continue
        tb = loc.split(":", 1)[1].split(".", 1)[0]
        if not check("same table (no cross-table remapping)", tb == ref.split(".")[0], tag):
            continue
        bf = base.fields.get(f"body:{m_base[ref][0]}.{m_base[ref][1]}")
        same = bf is not None and f.schema.get("x-attnum") == bf.schema.get("x-attnum")
        if not check("catalog identity: same column as the baseline (attnum)", same,
                     f"{tag}: attnum {f.schema.get('x-attnum')} vs baseline {bf.schema.get('x-attnum') if bf else '?'}"):
            continue
        if not check("allow-listed operation (column mapping + cast only)", mp.transform in CASTS, tag):
            continue
        need = WANT_TRANSFORM.get(bf.type, "none") if f.types != bf.types else "none"
        if not check("type-compatible transform", mp.transform == need,
                     f"{tag}: column is {f.schema.get('x-pg-type')}, workflow needs {bf.type}"):
            continue
        quote = (mp.evidence or "").strip()
        if not check("evidence quoted verbatim from the live schema", bool(quote) and quote in ev_text,
                     f"{tag}: {quote[:80]!r}"):
            continue
        old_col, new_col = m[ref][1], loc.split(".", 1)[1]
        if not same and (unit_change(old_col, new_col) or entity_mismatch(old_col, new_col)):
            check("same entity and unit", False, tag)
            continue
        new_m[ref] = (tb, new_col, mp.transform)
        used.add(ref)
    missing = [r for r in broken if r not in used]
    check("every broken reference repaired", not missing, ", ".join(missing))
    check("no required column left without a trusted value", not required,
          ", ".join(r.split(":", 1)[1] for r in required) + (" would need an invented value" if required else ""))
    accepted = all(c["ok"] for c in checks)
    return ({"accepted": accepted, "checks": checks,
             "rejected_reasons": [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]},
            new_m if accepted else None)


def sandbox_test(t: livedb.Target, base, wf: str, new_m: dict, params: dict, note: str) -> dict:
    """Copy the demo tables (structure + synthetic rows) into the sandbox schema, run the patched workflow there,
    verify it, and ALWAYS roll back. Nothing persists in either schema."""
    import psycopg
    from psycopg import sql
    with livedb.connect(t, 20000) as c:
        try:
            for tb in livedb.BUSINESS_TABLES:
                src = sql.SQL("{}.{}").format(sql.Identifier(t.schema), sql.Identifier(tb))
                dst = sql.SQL("{}.{}").format(sql.Identifier(t.sandbox), sql.Identifier(tb))
                c.execute(sql.SQL("CREATE TABLE {} (LIKE {} INCLUDING ALL)").format(dst, src))
                c.execute(sql.SQL("INSERT INTO {} OVERRIDING SYSTEM VALUE SELECT * FROM {}").format(dst, src))
            for tb in livedb.BUSINESS_TABLES:              # identity sequences continue after the copied rows
                for col in livedb.columns(c, t.sandbox, tb):
                    if col["identity"]:
                        c.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s, %s), "
                                          "(SELECT coalesce(max({}), 1) FROM {}.{}))").format(
                            sql.Identifier(col["name"]), sql.Identifier(t.sandbox), sql.Identifier(tb)),
                            (f"{t.sandbox}.{tb}", col["name"]))
            pre = inventory_pre(c, t.sandbox, base, params["order_id"]) if wf == "inventory_update" else None
            out = run_workflow(c, t.sandbox, wf, new_m, params, note)
            v = verify(c, t.sandbox, base, wf, params, out, pre, note)
            return {"ok": v["ok"], "detail": v["summary"], "checks": v["checks"]}
        except psycopg.Error as e:
            return {"ok": False, "detail": f"SQLSTATE {e.sqlstate}: {livedb.error_message(e)}"}
        finally:
            c.rollback()


# ---------------------------------------------------------------- one experiment
STAGES = ["baseline_test", "fault_injected", "failure_detected", "schema_retrieved", "diagnosis", "proposal",
          "validation", "sandbox_test", "patch_applied", "retry", "business_verification", "rollback"]
TIMELINE_STAGE = {"baseline_test": "request", "fault_injected": "apply", "failure_detected": "request",
                  "schema_retrieved": "discover", "diagnosis": "classify", "proposal": "plan", "validation": "validate",
                  "sandbox_test": "validate", "patch_applied": "apply", "retry": "request",
                  "business_verification": "verify", "rollback": "rollback"}
OK_STATES = {"passed", "applied", "validated", "proposed", "retried", "verified", "retrieved", "detected", "diagnosed",
             "rolled back", "not required"}


def at_baseline(state, t: livedb.Target, wf: str) -> tuple[bool, str]:
    st = state.livedb_storage
    td = st.current(tool_name(wf))
    if td is None:
        return False, "not set up - run Set up first"
    if td.version != 1:
        return False, "the workflow mapping is not at its baseline version - reset to baseline first"
    if livedb.snapshot(t).signature() != st.get_meta("baseline_signature"):
        return False, "the demo schema differs from the baseline - reset to baseline first"
    return True, "at baseline"


def run_experiment(state, t: livedb.Target, scenario: str, engine: str, trigger: str = "manual") -> dict:
    if scenario not in SCENARIOS:
        raise livedb.LiveDBError("invalid", f"unknown scenario {scenario}")
    if engine not in ("gemini", "local"):
        raise livedb.LiveDBError("invalid", f"unknown engine {engine}")
    sc = SCENARIOS[scenario]
    wf = sc["workflow"]
    params = dict(WORKFLOWS[wf]["params"])
    st = state.livedb_storage
    stop = state.livedb_stop
    exp_id = "exp_" + uuid.uuid4().hex[:12]
    note = f"ADAPTIX demo experiment {exp_id}"
    log = Log(t, exp_id)
    stages: list[dict] = []
    res = {"experiment_id": exp_id, "scenario": scenario, "scenario_title": sc["title"], "workflow": wf,
           "engine": engine, "trigger": trigger, "stages": stages, "expected": sc["expect"]}
    inc = Incident(tool=tool_name(wf), environment=st.environment, scenario=f"live-db:{scenario}:{engine}")
    clock = {"detect": None}

    def stage(name, status, title, **detail):
        stages.append({"stage": name, "status": status, "title": title, "ts": now_iso(), "detail": detail or None})
        inc.add(TIMELINE_STAGE[name], title, "ok" if status in OK_STATES else
                ("warn" if status in ("refused", "not reproduced") else "fail"), detail or None)
        log.event(name, {"status": status, "title": title,
                         **{k: v for k, v in detail.items() if k not in ("patch", "validation", "verification")}},
                  patch=detail.get("patch"), validation=detail.get("validation"),
                  verification=detail.get("verification"))
        if stop.is_set():
            raise Stopped("stopped by the operator")

    def finish(status, error=None, verified=False, rollback=False):
        rec_ms = round((time.perf_counter() - clock["detect"]) * 1000, 1) if (clock["detect"] and status ==
                                                                               "recovered") else None
        log.finish(status, rec_ms, verified, rollback, error)
        inc.status = {"recovered": "recovered", "rolled_back": "rolled_back", "failed": "failed"}.get(status, "stopped")
        inc.reason = error or ("recovered and verified" if status == "recovered" else status)
        inc.rolled_back = rollback
        inc.finished_at = now_iso()
        inc.duration_ms = rec_ms
        st.save_incident(inc)
        res.update(status=status, verified=verified, rollback_performed=rollback, recovery_ms=rec_ms, error=error,
                   incident=inc.id)
        ev = evidence_mod.build(SimpleNamespace(storage=st), inc.model_dump(mode="json"), run_id=exp_id,
                                scenario=f"live-db:{scenario}", tool=tool_name(wf),
                                expected={"recovered": "recovered", "refused": "stopped"}[sc["expect"]],
                                expected_category="schema_drift", business=None, result={"status": status},
                                extra={"engine": engine, "trigger": trigger, "stages": stages, "target": t.display()})
        res["evidence_id"] = ev["evidence_id"]
        return res

    ok, why = at_baseline(state, t, wf)
    if not ok:
        raise livedb.LiveDBError("invalid", why)
    log.start(f"{scenario}: {sc['title']}", engine, scenario)
    try:
        base = st.get_schema(st.get_meta("baseline_signature"))
        td0 = st.current(tool_name(wf))
        m_base = mapping(td0)
        # 1 baseline
        b = attempt(t, t.schema, base, wf, m_base, params, commit=False, note=note)
        stage("baseline_test", "passed" if b["ok"] else "failed",
              "Baseline workflow succeeded and matched the seed data" if b["ok"] else
              f"Baseline workflow failed: {b['error'] or b['verification']['summary']}", verification=b["verification"])
        if not b["ok"]:
            return finish("blocked", "baseline workflow does not verify - reset to baseline")
        # 2 fault
        if scenario == "F":
            bad = dict(m_base)
            ref, col = BAD_MAPPING_F
            bad[ref] = (ref.split(".")[0], col, "none")
            cand = ToolDef(td0.name, td0.version + 1, "SQL", td0.path,
                           {f"body:{v[0]}.{v[1]}": [r, v[2]] for r, v in bad.items()}, {}, {})
            cand.schema_signature = td0.schema_signature
            cand = st.apply_repair(cand, td0.version, "fault_injection", None)
            stage("fault_injected", "applied", f"Mapping fault: {ref} now points to column '{col}' (schema unchanged)",
                  patch=patch_diff(td0, cand))
        else:
            r = livedb.apply_ddl(t, livedb.fault_ddl(t, scenario))
            stage("fault_injected", "applied" if r["ok"] else "failed",
                  ("Applied: " + "; ".join(r["ddl"])) if r["ok"] else f"Fault could not be applied: {r['error']}",
                  ddl=r["ddl"])
            if not r["ok"]:
                return finish("failed", f"fault injection failed: {r['error']}")
        td = st.current(tool_name(wf))
        m = mapping(td)
        # 3 detection
        d = attempt(t, t.schema, base, wf, m, params, commit=False, note=note)
        clock["detect"] = time.perf_counter()
        if d["ok"]:
            stage("failure_detected", "not reproduced", "The workflow still succeeded and verified after the fault")
            return finish("not_reproduced", "the fault did not break this workflow")
        stage("failure_detected", "detected", f"Workflow failed: SQLSTATE {d['sqlstate']} - {d['error']}" if d["sqlstate"]
              else "Workflow ran but the business result did not match", sqlstate=d["sqlstate"], error=d["error"],
              verification=d["verification"])
        # 4 schema
        cur = livedb.snapshot(t, base)
        rep = diff(base, cur)
        inc.drift = rep.model_dump()
        inc.old_signature, inc.new_signature = base.signature(), cur.signature()
        stage("schema_retrieved", "retrieved", f"Catalog read: {rep.summary if rep.changed else 'no change vs baseline'}",
              diff=rep.model_dump())
        # 5 diagnosis
        broken = _broken(m, WORKFLOWS[wf]["refs"], base, cur)
        required = _new_required(wf, m, cur)
        category, explanation = classify(d["sqlstate"], rep.changed, broken, required)
        inc.category = category
        stage("diagnosis", "diagnosed" if category in ("schema_drift", "mapping_mismatch") else "failed",
              f"{category}: {explanation}", broken=broken, required=required)
        if category not in ("schema_drift", "mapping_mismatch"):
            return finish("refused", f"not a schema problem ({category}); no schema repair is attempted")
        # 6 proposal
        ctx = context_for(wf, m, base, cur, broken, required, d["sqlstate"], d["error"] or "", m_base)
        try:
            plan, meta = propose(state, engine, ctx)
        except EngineUnavailable as e:
            stage("proposal", "failed", f"Engine '{engine}' unavailable: {e}. Not switching engines.")
            return finish("failed", f"repair engine unavailable: {e}")
        inc.llm = redact(meta)
        inc.plan = plan.model_dump() if plan else None
        stage("proposal", "proposed" if plan else "failed",
              f"{engine} proposed {sum(1 for x in plan.mappings if x.source_arg)} mapping(s)" if plan else
              f"{engine} returned no usable proposal ({meta.get('error_category')})",
              engine={k: meta.get(k) for k in ("engine", "model", "real", "ok", "error_category", "duration_ms",
                                                "prompt_tokens", "output_tokens")},
              proposal=plan.model_dump() if plan else None)
        # 7 validation
        vres, new_m = validate_mapping(plan, wf, m, m_base, base, cur, broken, required,
                                       state.settings.llm_min_confidence, ctx)
        inc.validation = vres
        stage("validation", "validated" if vres["accepted"] else "refused",
              "Proposal validated independently" if vres["accepted"] else
              "Refused: " + "; ".join(vres["rejected_reasons"])[:300], validation=vres)
        if not vres["accepted"]:
            return finish("refused", "unsafe or ambiguous repair refused: " + "; ".join(vres["rejected_reasons"])[:400])
        # 8 sandbox
        sb = sandbox_test(t, base, wf, new_m, params, note + " (sandbox)")
        stage("sandbox_test", "passed" if sb["ok"] else "failed", f"Sandbox test: {sb['detail']}", sandbox=sb)
        if not sb["ok"]:
            return finish("refused", f"repair failed in the sandbox: {sb['detail']}")
        # 9 apply
        cand = ToolDef(td.name, td.version + 1, "SQL", td.path,
                       {f"body:{v[0]}.{v[1]}": [r, v[2]] for r, v in new_m.items()}, {}, {})
        diff_text = patch_diff(td, cand)
        st.save_schema(cur)
        cand.schema_signature = cur.signature()
        cand.patch = [{"op": "map", "argument": r, "from": f"{m[r][0]}.{m[r][1]}", "to": f"{v[0]}.{v[1]}",
                       "transform": v[2]} for r, v in new_m.items() if m.get(r) != v]
        try:
            cand = st.apply_repair(cand, td.version, "repair", inc.id, inc.correlation_id)
        except ConflictError as e:
            return finish("failed", f"registry changed during repair ({e})")
        inc.from_version, inc.to_version = td.version, cand.version
        stage("patch_applied", "applied", f"Mapping v{td.version} -> v{cand.version}", patch=diff_text)
        res["patch_diff"] = diff_text
        # 10-11 retry + verification
        final, n = None, 0
        for n in range(1, state.settings.max_transport_retries + 2):
            final = attempt(t, t.schema, base, wf, new_m, params, commit=True, note=note)
            if final["ok"] or final["sqlstate"] not in TRANSIENT:
                break
            time.sleep(min(0.2 * 2 ** n, 2.0))
        stage("retry", "retried" if final["sqlstate"] is None else "failed",
              f"Original workflow retried with mapping v{cand.version}" + (f" ({n} attempts)" if n > 1 else "")
              + ("" if final["sqlstate"] is None else f": SQLSTATE {final['sqlstate']} - {final['error']}"))
        res["verification"] = final["verification"]
        stage("business_verification", "verified" if final["ok"] else "failed",
              (final["verification"] or {}).get("summary") or "no result to verify",
              verification=final["verification"], committed=final["committed"])
        if final["ok"]:
            inc.verification = {"ok": True, "checks": [{"name": k, "ok": v, "detail": ""}
                                                       for k, v in final["verification"]["checks"].items()]}
            inc.status = "recovered"
            stage("rollback", "not required", "No rollback needed")
            return finish("recovered", verified=True)
        # 12 rollback
        inc.verification = {"ok": False, "checks": [{"name": k, "ok": v, "detail": ""} for k, v in
                                                    ((final["verification"] or {}).get("checks") or {}).items()]}
        restored = st.rollback(td.name, cand.version, "business verification failed", inc.correlation_id)
        stage("rollback", "rolled back" if restored else "failed",
              f"Registry restored to v{restored.version}; the failed transaction left no changes" if restored else
              "Rollback not possible")
        return finish("rolled_back", "repair rolled back: business verification failed", rollback=restored is not None)
    except Stopped as e:
        return finish("blocked", str(e))
    except livedb.LiveDBError:
        raise
    except Exception as e:                                  # record honestly; never hide a failure
        return finish("failed", f"{type(e).__name__}: {scrub(str(e))[:200]}")


# ---------------------------------------------------------------- reset
def reset(state, t: livedb.Target) -> dict:
    out = livedb.setup(t, reset=True)
    register_all(state, t)
    state.livedb_storage.event("demo_reset", None, {"schema": t.schema})
    return out


# ---------------------------------------------------------------- automatic injection (disabled by default)
SCHEDULE_DEFAULT = {"enabled": False, "interval_s": 600, "max_runs": 3, "runs_done": 0, "engine": "gemini",
                    "scenarios": ["A", "B", "C"], "last_run": None}


class Scheduler:
    def __init__(self, state, tick_s: float = 5.0):
        self.state, self.tick_s = state, tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lock = threading.Lock()

    def settings(self) -> dict:
        raw = self.state.livedb_storage.get_meta("schedule")
        return {**SCHEDULE_DEFAULT, **(json.loads(raw) if raw else {})}

    def save(self, cfg: dict):
        self.state.livedb_storage.set_meta("schedule", json.dumps(cfg))

    def stop_now(self):
        cfg = self.settings()
        cfg["enabled"] = False
        self.save(cfg)
        self.state.livedb_stop.set()               # interrupts a running experiment at its next stage

    def due(self, now: float | None = None) -> bool:
        cfg = self.settings()
        if not cfg["enabled"] or cfg["runs_done"] >= cfg["max_runs"]:
            return False
        return cfg["last_run"] is None or (now or time.time()) - cfg["last_run"] >= cfg["interval_s"]

    def tick(self, now: float | None = None) -> dict | None:
        if not self.due(now) or not self.lock.acquire(blocking=False):
            return None
        try:
            cfg = self.settings()
            scenario = cfg["scenarios"][cfg["runs_done"] % len(cfg["scenarios"])]
            t = livedb.target_from_env()
            self.state.livedb_stop.clear()
            reset(self.state, t)                     # every automatic run starts from the known-good baseline
            out = run_experiment(self.state, t, scenario, cfg["engine"], trigger="scheduled")
            cfg = self.settings()
            cfg.update(runs_done=cfg["runs_done"] + 1, last_run=now or time.time())
            if cfg["runs_done"] >= cfg["max_runs"]:
                cfg["enabled"] = False
            self.save(cfg)
            return out
        except Exception as e:
            self.state.livedb_storage.event("schedule_error", None, {"error": scrub(str(e))[:200]})
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
