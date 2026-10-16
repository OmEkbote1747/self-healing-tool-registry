"""Durable state: registry, schema history, incidents, audit events, executions, mapping cache, locks.

SQLite in WAL mode with IMMEDIATE transactions: one writer at a time across threads and processes on a
host. Versions are never deleted (superseded / rolled_back), so every change is reversible and auditable.
Everything written here has already been redacted; no secret is ever passed to this module.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Incident, ToolSpec, now_iso
from .schema import Operation
from .security import redact
from .tooldef import ToolDef

DDL = """
CREATE TABLE IF NOT EXISTS tools (name TEXT PRIMARY KEY, spec TEXT NOT NULL, created_at TEXT NOT NULL,
  last_success_at TEXT, last_failure_at TEXT, last_category TEXT);
CREATE TABLE IF NOT EXISTS versions (tool TEXT NOT NULL, version INTEGER NOT NULL, def TEXT NOT NULL,
  status TEXT NOT NULL, origin TEXT NOT NULL, parent_version INTEGER, incident_id TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (tool, version));
CREATE TABLE IF NOT EXISTS schemas (signature TEXT PRIMARY KEY, op TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, correlation_id TEXT, tool TEXT, simulated INTEGER,
  scenario TEXT, started_at TEXT, finished_at TEXT, category TEXT, status TEXT, zero_touch INTEGER,
  duration_ms REAL, retries INTEGER, repair_attempts INTEGER, rolled_back INTEGER, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, correlation_id TEXT,
  tool TEXT, kind TEXT NOT NULL, simulated INTEGER DEFAULT 0, data TEXT);
CREATE TABLE IF NOT EXISTS executions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT,
  ok INTEGER, category TEXT, duration_ms REAL, simulated INTEGER DEFAULT 0, incident_id TEXT);
CREATE TABLE IF NOT EXISTS mappings (tool TEXT, from_sig TEXT, to_sig TEXT, plan TEXT, created_at TEXT,
  uses INTEGER DEFAULT 0, PRIMARY KEY (tool, from_sig, to_sig));
CREATE TABLE IF NOT EXISTS locks (tool TEXT PRIMARY KEY, holder TEXT, expires_at REAL);
CREATE INDEX IF NOT EXISTS ix_inc_tool ON incidents (tool, started_at);
CREATE INDEX IF NOT EXISTS ix_events ON events (tool, ts);
"""


class ConflictError(RuntimeError):
    """The tool changed since the repair was planned."""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(DDL)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self):
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- audit
    def event(self, kind: str, tool: str | None = None, data: dict | None = None, correlation_id: str | None = None,
              simulated: bool = False, conn=None):
        row = (now_iso(), correlation_id, tool, kind, int(simulated), json.dumps(redact(data or {}), default=str))
        sql = "INSERT INTO events (ts, correlation_id, tool, kind, simulated, data) VALUES (?,?,?,?,?,?)"
        if conn is not None:
            conn.execute(sql, row)
        else:
            with self._tx() as c:
                c.execute(sql, row)

    def events(self, tool: str | None = None, kind: str | None = None, simulated: bool | None = None,
               correlation_id: str | None = None, limit: int = 200) -> list[dict]:
        q, a = "SELECT * FROM events WHERE 1=1", []
        for col, val in (("tool", tool), ("kind", kind), ("correlation_id", correlation_id)):
            if val:
                q += f" AND {col}=?"
                a.append(val)
        if simulated is not None:
            q += " AND simulated=?"
            a.append(int(simulated))
        q += " ORDER BY id DESC LIMIT ?"
        a.append(min(limit, 2000))
        with self._conn() as c:
            return [{"ts": r["ts"], "correlation_id": r["correlation_id"], "tool": r["tool"], "kind": r["kind"],
                     "simulated": bool(r["simulated"]), "data": json.loads(r["data"] or "{}")}
                    for r in c.execute(q, a)]

    # ---------------------------------------------------------------- schemas
    def save_schema(self, op: Operation) -> str:
        sig = op.signature()
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO schemas VALUES (?,?,?)", (sig, json.dumps(op.to_json(), default=str), now_iso()))
        return sig

    def get_schema(self, signature: str) -> Operation | None:
        with self._conn() as c:
            r = c.execute("SELECT op FROM schemas WHERE signature=?", (signature,)).fetchone()
        return Operation.from_json(json.loads(r["op"])) if r else None

    # ---------------------------------------------------------------- tools and versions
    def register(self, spec: ToolSpec, td: ToolDef, op: Operation):
        sig = self.save_schema(op)
        td.schema_signature = sig
        with self._tx() as c:
            c.execute("DELETE FROM versions WHERE tool=?", (spec.name,))
            c.execute("INSERT OR REPLACE INTO tools (name, spec, created_at) VALUES (?,?,?)",
                      (spec.name, spec.model_dump_json(), now_iso()))
            c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                      (spec.name, 1, json.dumps(td.to_json()), "active", "registered", None, None, now_iso()))
            self.event("tool_registered", spec.name, {"version": 1, "schema_signature": sig,
                                                       "base_url": spec.base_url, "path": spec.path},
                       simulated=spec.simulated, conn=c)

    def delete_tool(self, name: str):
        with self._tx() as c:
            for t in ("tools", "versions", "mappings", "locks"):
                c.execute(f"DELETE FROM {t} WHERE {'name' if t == 'tools' else 'tool'}=?", (name,))
            self.event("tool_deleted", name, conn=c)

    def tool(self, name: str) -> ToolSpec | None:
        with self._conn() as c:
            r = c.execute("SELECT spec FROM tools WHERE name=?", (name,)).fetchone()
        return ToolSpec.model_validate_json(r["spec"]) if r else None

    def tools(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM tools ORDER BY name").fetchall()
        out = []
        for r in rows:
            spec = ToolSpec.model_validate_json(r["spec"])
            cur = self.current(spec.name)
            out.append({"spec": spec, "current_version": cur.version if cur else None,
                        "schema_signature": cur.schema_signature if cur else None,
                        "last_success_at": r["last_success_at"], "last_failure_at": r["last_failure_at"],
                        "last_category": r["last_category"]})
        return out

    def _row_def(self, r) -> ToolDef:
        td = ToolDef.from_json(json.loads(r["def"]))
        td.status, td.origin, td.parent_version, td.incident_id = r["status"], r["origin"], r["parent_version"], r["incident_id"]
        td.version = r["version"]
        return td

    def current(self, tool: str, conn=None) -> ToolDef | None:
        q = "SELECT * FROM versions WHERE tool=? AND status='active' ORDER BY version DESC LIMIT 1"
        if conn is not None:
            r = conn.execute(q, (tool,)).fetchone()
        else:
            with self._conn() as c:
                r = c.execute(q, (tool,)).fetchone()
        return self._row_def(r) if r else None

    def versions(self, tool: str) -> list[ToolDef]:
        with self._conn() as c:
            return [self._row_def(r) for r in c.execute("SELECT * FROM versions WHERE tool=? ORDER BY version", (tool,))]

    def apply_repair(self, td: ToolDef, expected_version: int, origin: str, incident_id: str | None,
                     correlation_id: str | None = None, simulated: bool = False) -> ToolDef:
        """Atomically make `td` the active version, but only if the active version is still `expected_version`."""
        with self._tx() as c:
            cur = self.current(td.name, conn=c)
            if cur is None or cur.version != expected_version:
                raise ConflictError(f"{td.name} changed: expected v{expected_version}, found v{cur.version if cur else '?'}")
            new_v = c.execute("SELECT MAX(version) FROM versions WHERE tool=?", (td.name,)).fetchone()[0] + 1
            td.version, td.parent_version, td.status, td.origin, td.incident_id = new_v, cur.version, "active", origin, incident_id
            c.execute("UPDATE versions SET status='superseded' WHERE tool=? AND version=?", (td.name, cur.version))
            c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                      (td.name, new_v, json.dumps(td.to_json()), "active", origin, cur.version, incident_id, now_iso()))
            self.event("registry_updated", td.name, {"from": cur.version, "to": new_v, "origin": origin, "patch": td.patch},
                       correlation_id, simulated, conn=c)
        return td

    def rollback(self, tool: str, version: int, reason: str, correlation_id: str | None = None,
                 simulated: bool = False) -> ToolDef | None:
        """Mark `version` rolled back and reactivate its parent. Returns the restored version."""
        with self._tx() as c:
            r = c.execute("SELECT * FROM versions WHERE tool=? AND version=?", (tool, version)).fetchone()
            if r is None or r["status"] != "active" or r["parent_version"] is None:
                return None
            c.execute("UPDATE versions SET status='rolled_back' WHERE tool=? AND version=?", (tool, version))
            c.execute("UPDATE versions SET status='active' WHERE tool=? AND version=?", (tool, r["parent_version"]))
            self.event("rolled_back", tool, {"from": version, "to": r["parent_version"], "reason": reason},
                       correlation_id, simulated, conn=c)
        return self.current(tool)

    # ---------------------------------------------------------------- locks (one repair per tool at a time)
    def acquire_lock(self, tool: str, holder: str, ttl: float = 30.0, wait: float = 5.0) -> bool:
        deadline = time.monotonic() + wait
        while True:
            with self._tx() as c:
                r = c.execute("SELECT holder, expires_at FROM locks WHERE tool=?", (tool,)).fetchone()
                if r is None or r["expires_at"] < time.time() or r["holder"] == holder:
                    c.execute("INSERT OR REPLACE INTO locks VALUES (?,?,?)", (tool, holder, time.time() + ttl))
                    return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)

    def release_lock(self, tool: str, holder: str):
        with self._tx() as c:
            c.execute("DELETE FROM locks WHERE tool=? AND holder=?", (tool, holder))

    # ---------------------------------------------------------------- mapping reuse
    def get_mapping(self, tool: str, from_sig: str, to_sig: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT plan FROM mappings WHERE tool=? AND from_sig=? AND to_sig=?",
                          (tool, from_sig, to_sig)).fetchone()
        return json.loads(r["plan"]) if r else None

    def put_mapping(self, tool: str, from_sig: str, to_sig: str, plan: dict):
        with self._tx() as c:
            c.execute("INSERT INTO mappings (tool, from_sig, to_sig, plan, created_at, uses) VALUES (?,?,?,?,?,0) "
                      "ON CONFLICT(tool, from_sig, to_sig) DO UPDATE SET uses = uses + 1",
                      (tool, from_sig, to_sig, json.dumps(plan), now_iso()))

    # ---------------------------------------------------------------- incidents and executions
    def save_incident(self, inc: Incident):
        data = json.dumps(redact(inc.model_dump(mode="json")), default=str)
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (inc.id, inc.correlation_id, inc.tool, int(inc.simulated), inc.scenario, inc.started_at,
                       inc.finished_at, inc.category, inc.status, int(inc.zero_touch), inc.duration_ms, inc.retries,
                       inc.repair_attempts, int(inc.rolled_back), data))

    def incidents(self, tool: str | None = None, status: str | None = None, simulated: bool | None = None,
                  limit: int = 200) -> list[Incident]:
        q, a = "SELECT data FROM incidents WHERE 1=1", []
        if tool:
            q += " AND tool=?"
            a.append(tool)
        if status:
            q += " AND status=?"
            a.append(status)
        if simulated is not None:
            q += " AND simulated=?"
            a.append(int(simulated))
        q += " ORDER BY started_at DESC LIMIT ?"
        a.append(min(limit, 5000))
        with self._conn() as c:
            return [Incident.model_validate_json(r["data"]) for r in c.execute(q, a)]

    def incident(self, incident_id: str) -> Incident | None:
        with self._conn() as c:
            r = c.execute("SELECT data FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return Incident.model_validate_json(r["data"]) if r else None

    def record_execution(self, tool: str, ok: bool, category: str, duration_ms: float, simulated: bool,
                         incident_id: str | None = None):
        with self._tx() as c:
            c.execute("INSERT INTO executions (ts, tool, ok, category, duration_ms, simulated, incident_id) "
                      "VALUES (?,?,?,?,?,?,?)", (now_iso(), tool, int(ok), category, duration_ms, int(simulated), incident_id))
            col = "last_success_at" if ok else "last_failure_at"
            c.execute(f"UPDATE tools SET {col}=?, last_category=? WHERE name=?", (now_iso(), category, tool))

    def executions(self, simulated: bool | None = None, limit: int = 5000) -> list[dict]:
        q, a = "SELECT * FROM executions WHERE 1=1", []
        if simulated is not None:
            q += " AND simulated=?"
            a.append(int(simulated))
        q += " ORDER BY id DESC LIMIT ?"
        a.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, a)]

    def purge(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._tx() as c:
            n = c.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            n += c.execute("DELETE FROM incidents WHERE started_at < ?", (cutoff,)).rowcount
            n += c.execute("DELETE FROM executions WHERE ts < ?", (cutoff,)).rowcount
        return n

    def reset(self, simulated_only: bool = True):
        """Remove simulated data (the Chaos Lab's 'reset' action)."""
        with self._tx() as c:
            names = [json.loads(r["spec"])["name"] for r in c.execute("SELECT spec FROM tools")
                     if json.loads(r["spec"]).get("simulated") or not simulated_only]
            for n in names:
                for t in ("versions", "mappings", "locks"):
                    c.execute(f"DELETE FROM {t} WHERE tool=?", (n,))
                c.execute("DELETE FROM tools WHERE name=?", (n,))
            w = " WHERE simulated=1" if simulated_only else ""
            c.execute("DELETE FROM incidents" + w)
            c.execute("DELETE FROM events" + w)
            c.execute("DELETE FROM executions" + w)
            self.event("reset", None, {"simulated_only": simulated_only, "tools": names}, conn=c)


def new_holder() -> str:
    return "w_" + uuid.uuid4().hex[:10]
