"""Durable, tenant-scoped tool registry backed by SQL.

Same interface as the in-memory ToolRegistry, but every read and write goes to the database inside a
transaction, so several app instances or worker threads see one consistent history:

* tenant isolation - every row carries a tenant id and every query is filtered by it
* atomic updates   - versions are numbered inside an IMMEDIATE transaction (no lost updates)
* concurrency      - approving a proposal checks that its base version is still current
* rollback history - versions are never deleted, only marked rolled_back
* no stale cache   - nothing is cached in the process; every call reads the database

SQLite keeps the prototype dependency-free; the SQL is plain enough to move to Postgres for a
multi-host deployment.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .healer import ConflictError, Proposal, ToolDef

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
  tenant TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (tenant, name));
CREATE TABLE IF NOT EXISTS versions (
  tenant TEXT NOT NULL, tool TEXT NOT NULL, version INTEGER NOT NULL, definition TEXT NOT NULL,
  status TEXT NOT NULL, origin TEXT NOT NULL, owner TEXT, successes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, PRIMARY KEY (tenant, tool, version));
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, tool TEXT NOT NULL, base_version INTEGER NOT NULL,
  candidate TEXT NOT NULL, reasons TEXT NOT NULL, status TEXT NOT NULL, decided_by TEXT,
  created_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
  tool TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_versions ON versions (tenant, tool, version);
CREATE INDEX IF NOT EXISTS ix_proposals ON proposals (tenant, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _td_to_json(td: ToolDef) -> str:
    return json.dumps({"path": td.path, "body_map": td.body_map, "header_map": td.header_map})


def _td_from_row(name: str, row) -> ToolDef:
    d = json.loads(row["definition"])
    return ToolDef(name, d["path"], row["version"], {k: tuple(v) for k, v in d["body_map"].items()},
                   d["header_map"], status=row["status"], origin=row["origin"], owner=row["owner"],
                   successes=row["successes"])


class SqliteRegistry:
    def __init__(self, path: str | Path, tenant: str = "default", promote_after: int = 3):
        self.path, self.tenant, self.promote_after = str(path), tenant, promote_after
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._tx(write=False) as c:     # executescript manages its own transaction
            c.executescript(SCHEMA)

    # -- plumbing
    @contextmanager
    def _tx(self, write: bool = True):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if write:
                conn.execute("BEGIN IMMEDIATE")      # one writer at a time across processes
            yield conn
            if write:
                conn.execute("COMMIT")
        except Exception:
            if write:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _event(self, c, kind: str, tool: str | None, data: dict):
        c.execute("INSERT INTO events (tenant, ts, kind, tool, data) VALUES (?,?,?,?,?)",
                  (self.tenant, _now(), kind, tool, json.dumps(data)))

    # -- tools and versions
    def register(self, name: str, path: str, schema: dict[str, str]):
        td = ToolDef(name, path, 1, {k: (k, None) for k in schema})
        with self._tx() as c:
            c.execute("DELETE FROM versions WHERE tenant=? AND tool=?", (self.tenant, name))
            c.execute("DELETE FROM proposals WHERE tenant=? AND tool=?", (self.tenant, name))
            c.execute("INSERT OR REPLACE INTO tools VALUES (?,?,?,?)", (self.tenant, name, path, _now()))
            c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                      (self.tenant, name, 1, _td_to_json(td), "active", "initial", None, 0, _now()))
            self._event(c, "registered", name, {"version": 1})

    def _rows(self, c, name):
        return c.execute("SELECT * FROM versions WHERE tenant=? AND tool=? ORDER BY version",
                         (self.tenant, name)).fetchall()

    @property
    def versions(self) -> dict[str, list[ToolDef]]:
        with self._tx(write=False) as c:
            names = [r["name"] for r in c.execute("SELECT name FROM tools WHERE tenant=?", (self.tenant,))]
            return {n: [_td_from_row(n, r) for r in self._rows(c, n)] for n in names}

    def current(self, name: str, agent_id: str | None = None) -> ToolDef:
        with self._tx(write=False) as c:
            rows = self._rows(c, name)
        for r in reversed(rows):
            if r["status"] == "active" or (r["status"] == "canary" and r["owner"] == agent_id):
                return _td_from_row(name, r)
        return _td_from_row(name, rows[0])

    def latest_version(self, name: str) -> int:
        with self._tx(write=False) as c:
            return c.execute("SELECT MAX(version) FROM versions WHERE tenant=? AND tool=?",
                             (self.tenant, name)).fetchone()[0]

    def add(self, td: ToolDef):
        with self._tx() as c:
            v = c.execute("SELECT MAX(version) FROM versions WHERE tenant=? AND tool=?",
                          (self.tenant, td.name)).fetchone()[0] + 1
            td.version = v
            c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                      (self.tenant, td.name, v, _td_to_json(td), td.status, td.origin, td.owner, 0, _now()))
            self._event(c, "version_added", td.name, {"version": v, "status": td.status, "origin": td.origin})

    def record_success(self, td: ToolDef):
        with self._tx() as c:
            c.execute("UPDATE versions SET successes = successes + 1 WHERE tenant=? AND tool=? AND version=?",
                      (self.tenant, td.name, td.version))
            row = c.execute("SELECT successes, status FROM versions WHERE tenant=? AND tool=? AND version=?",
                            (self.tenant, td.name, td.version)).fetchone()
            td.successes = row["successes"]
            if row["status"] == "canary" and row["successes"] >= self.promote_after:
                c.execute("UPDATE versions SET status='active' WHERE tenant=? AND tool=? AND version=?",
                          (self.tenant, td.name, td.version))
                td.status = "active"
                self._event(c, "promoted", td.name, {"version": td.version})

    def record_failure(self, td: ToolDef):
        if td.status != "canary":
            return
        with self._tx() as c:
            c.execute("UPDATE versions SET status='rolled_back' WHERE tenant=? AND tool=? AND version=? "
                      "AND status='canary'", (self.tenant, td.name, td.version))
            td.status = "rolled_back"
            self._event(c, "canary_rolled_back", td.name, {"version": td.version})

    def rollback(self, name: str) -> ToolDef:
        with self._tx() as c:
            live = [r for r in self._rows(c, name) if r["status"] in ("active", "canary")]
            if len(live) > 1:
                c.execute("UPDATE versions SET status='rolled_back' WHERE tenant=? AND tool=? AND version=?",
                          (self.tenant, name, live[-1]["version"]))
                self._event(c, "rolled_back", name, {"version": live[-1]["version"]})
        return self.current(name)

    # -- human approval
    def propose(self, tool: str, base_version: int, candidate: ToolDef, reasons: list[str]) -> Proposal:
        p = Proposal(uuid.uuid4().hex[:8], tool, base_version, candidate, reasons)
        with self._tx() as c:
            c.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (p.id, self.tenant, tool, base_version, _td_to_json(candidate), json.dumps(reasons),
                       "pending", None, _now(), None))
            self._event(c, "proposed", tool, {"proposal": p.id, "reasons": reasons})
        return p

    def _proposal(self, row) -> Proposal:
        cand = _td_from_row(row["tool"], {"definition": row["candidate"], "version": row["base_version"] + 1,
                                          "status": "canary", "origin": "auto", "owner": None, "successes": 0})
        return Proposal(row["id"], row["tool"], row["base_version"], cand, json.loads(row["reasons"]),
                        row["status"], row["decided_by"])

    def pending(self) -> list[Proposal]:
        with self._tx(write=False) as c:
            rows = c.execute("SELECT * FROM proposals WHERE tenant=? AND status='pending' ORDER BY created_at",
                             (self.tenant,)).fetchall()
        return [self._proposal(r) for r in rows]

    @property
    def proposals(self) -> dict[str, Proposal]:
        with self._tx(write=False) as c:
            rows = c.execute("SELECT * FROM proposals WHERE tenant=?", (self.tenant,)).fetchall()
        return {r["id"]: self._proposal(r) for r in rows}

    def approve(self, proposal_id: str, by: str = "reviewer") -> ToolDef:
        with self._tx() as c:
            row = c.execute("SELECT * FROM proposals WHERE id=? AND tenant=?", (proposal_id, self.tenant)).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "pending":
                raise ConflictError(f"proposal {proposal_id} is already {row['status']}")
            rows = self._rows(c, row["tool"])
            active = [r for r in rows if r["status"] == "active"]
            if not active or active[-1]["version"] != row["base_version"]:
                c.execute("UPDATE proposals SET status='rejected', decided_by=?, decided_at=? WHERE id=?",
                          ("system: stale", _now(), proposal_id))
                self._event(c, "proposal_stale", row["tool"], {"proposal": proposal_id})
                raise ConflictError("tool changed since this repair was proposed; re-run diagnosis")
            v = rows[-1]["version"] + 1
            c.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                      (self.tenant, row["tool"], v, row["candidate"], "active", "approved", None, 0, _now()))
            c.execute("UPDATE proposals SET status='approved', decided_by=?, decided_at=? WHERE id=?",
                      (by, _now(), proposal_id))
            self._event(c, "approved", row["tool"], {"proposal": proposal_id, "version": v, "by": by})
        return self.current(row["tool"])

    def reject(self, proposal_id: str, by: str = "reviewer"):
        with self._tx() as c:
            c.execute("UPDATE proposals SET status='rejected', decided_by=?, decided_at=? "
                      "WHERE id=? AND tenant=? AND status='pending'", (by, _now(), proposal_id, self.tenant))
            self._event(c, "rejected", None, {"proposal": proposal_id, "by": by})

    def events(self, limit: int = 50) -> list[dict]:
        with self._tx(write=False) as c:
            rows = c.execute("SELECT ts, kind, tool, data FROM events WHERE tenant=? ORDER BY id DESC LIMIT ?",
                             (self.tenant, limit)).fetchall()
        return [{"ts": r["ts"], "kind": r["kind"], "tool": r["tool"], **json.loads(r["data"] or "{}")} for r in rows]
