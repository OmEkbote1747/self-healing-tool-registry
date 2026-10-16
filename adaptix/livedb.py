"""Live Project Integration: an online PostgreSQL database (Neon) reached through a server-side DATABASE_URL.

Everything ADAPTIX does is confined to an ISOLATED demo schema (default `adaptix_demo`) and its sandbox
(`<schema>_sandbox`), filled with deterministic SYNTHETIC data from database/seed_data.py. Identifiers always go
through psycopg.sql (never string formatting), parameters are always bound, schema names must start with
`adaptix_`, there is no free-form SQL anywhere, and the connection URL is never logged, returned or shown -
only host, database name and TLS mode are displayed.

Schema facts come from the catalog (pg_attribute / information_schema), which is authoritative: a renamed
column keeps its attnum, so a rename is proven by the catalog rather than guessed from names.
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import env
from .schema import FieldSpec, Operation
from .security import register_secret, scrub

SCHEMA_RE = re.compile(r"^adaptix_[a-z0-9_]{1,40}$")
BUSINESS_TABLES = ["customers", "products", "orders", "order_items", "inventory_events"]
LOG_TABLES = ["adaptix_experiments", "adaptix_audit_log"]
INIT_SQL = Path(__file__).resolve().parent.parent / "database" / "init_demo.sql"
PG_TO_PY = {"integer": "int", "bigint": "int", "smallint": "int", "numeric": "float", "real": "float",
            "double precision": "float", "text": "str", "character varying": "str", "character": "str",
            "uuid": "str", "boolean": "bool", "date": "date", "timestamp with time zone": "str",
            "timestamp without time zone": "str", "jsonb": "object", "json": "object"}
PY_TO_JSON = {"int": "integer", "float": "number", "str": "string", "bool": "boolean", "date": "string",
              "object": "object"}


class LiveDBError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category        # not_configured | connection | authentication | permission | invalid | unsafe


@dataclass
class Target:
    url: str
    schema: str

    @property
    def sandbox(self) -> str:
        return self.schema + "_sandbox"

    def display(self) -> dict:
        """What may be shown: never the user name, password or full URL."""
        p = urlsplit(self.url)
        q = dict(x.split("=", 1) for x in (p.query or "").split("&") if "=" in x)
        host = p.hostname or q.get("host", "local socket")
        if host.startswith("/") or host.startswith("%2F"):
            host = "local socket"
        return {"host": host, "database": (p.path or "/").lstrip("/") or q.get("dbname", ""),
                "sslmode": q.get("sslmode", "default"), "schema": self.schema, "sandbox": self.sandbox}


def target_from_env(url: str | None = None, schema: str | None = None) -> Target:
    url = url or env("DATABASE_URL")
    if not url:
        raise LiveDBError("not_configured", "DATABASE_URL is not set on the server (Streamlit Cloud: Settings -> "
                                            "Secrets). See the setup instructions on this page.")
    schema = (schema or env("ADAPTIX_LIVE_DB_SCHEMA", "adaptix_demo") or "adaptix_demo").lower()
    if not SCHEMA_RE.match(schema):
        raise LiveDBError("unsafe", "ADAPTIX_LIVE_DB_SCHEMA must match adaptix_<name> (an isolated demo schema)")
    register_secret(url)
    pw = urlsplit(url).password
    if pw:
        register_secret(pw)
    return Target(url, schema)


def _with_tls(url: str) -> str:
    """Remote hosts must use TLS: add sslmode=require unless a stricter mode is already set."""
    p = urlsplit(url)
    local = (p.hostname in (None, "", "localhost", "127.0.0.1", "::1")) or "host=%2F" in url or "host=/" in url
    if local or "sslmode=" in url:
        if not local and re.search(r"sslmode=(disable|allow|prefer)\b", url):
            raise LiveDBError("unsafe", "remote connections must use sslmode=require or stricter")
        return url
    return url + ("&" if "?" in url else "?") + "sslmode=require"


@contextmanager
def connect(t: Target, statement_timeout_ms: int = 5000, read_only: bool = False):
    import psycopg
    try:
        conn = psycopg.connect(_with_tls(t.url), connect_timeout=10, autocommit=False,
                               application_name="adaptix-live-integration")
    except psycopg.OperationalError as e:
        msg = scrub(str(e)).split("\n")[0][:200]
        cat = "authentication" if "password" in msg.lower() or "authentication" in msg.lower() else "connection"
        raise LiveDBError(cat, msg) from None
    try:
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (str(int(statement_timeout_ms)),))
        conn.execute("SELECT set_config('idle_in_transaction_session_timeout', '30000', false)")
        conn.commit()
        if read_only:
            conn.read_only = True
        yield conn
    finally:
        conn.close()


def error_message(e) -> str:
    """Primary message only: PostgreSQL DETAIL lines can contain row values, so they are never kept."""
    diag = getattr(e, "diag", None)
    msg = (diag.message_primary if diag is not None and diag.message_primary else str(e).split("\n")[0])
    return scrub(msg)[:240]


def test_connection(t: Target) -> dict:
    t0 = time.perf_counter()
    with connect(t) as c:
        row = c.execute("SELECT version(), current_database(), "
                        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                        "(SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user), "
                        "has_database_privilege(current_database(), 'CREATE'), "
                        "(SELECT count(*) FROM pg_namespace WHERE nspname = %s), "
                        "current_setting('ssl', true), "
                        "(SELECT count(*) FROM information_schema.tables WHERE table_schema = %s)",
                        (t.schema, t.schema)).fetchone()
        c.rollback()
    warnings = []
    if row[2]:
        warnings.append("This role is a SUPERUSER. Use the dedicated least-privilege role from database/roles.sql.")
    if row[3]:
        warnings.append("This role can create roles. A dedicated role without CREATEROLE is recommended.")
    return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "server": row[0].split(",")[0],
            "database": row[1], "superuser": bool(row[2]), "can_create_schema": bool(row[4]),
            "demo_schema_exists": bool(row[5]), "demo_tables": row[7], "ssl": row[6], "target": t.display(),
            "warnings": warnings}


# ---------------------------------------------------------------- setup and reset (database/init_demo.sql)
def init_sections(t: Target) -> dict[str, str]:
    """The init script split into its sections, retargeted at this schema (validated name only)."""
    text = INIT_SQL.read_text()
    text = text.replace("adaptix_demo_sandbox", "\x00SB\x00")
    text = re.sub(r"\badaptix_demo\b", t.schema, text).replace("\x00SB\x00", t.sandbox)
    out, cur = {}, None
    for line in text.splitlines(keepends=True):
        m = re.match(r"-- @section (\w+)", line)
        if m:
            cur = m.group(1)
            out[cur] = ""
        elif cur:
            out[cur] += line
    return out


def setup(t: Target, reset: bool = False) -> dict:
    """Create missing schemas, ensure the log tables, and (re)load the business tables when asked or missing.
    A reset NEVER drops the experiment/audit logs."""
    from psycopg import sql
    sec = init_sections(t)
    with connect(t, 60000) as c:
        for s in (t.schema, t.sandbox):
            if not c.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (s,)).fetchone():
                c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(s)))
        c.execute(sec["logs"])
        have = c.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=%s AND table_name = ANY(%s)",
                         (t.schema, BUSINESS_TABLES)).fetchone()[0]
        loaded = False
        if reset or have < len(BUSINESS_TABLES):
            c.execute(sec["business"])
            loaded = True
        for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (t.sandbox,)).fetchall():
            c.execute(sql.SQL("DROP TABLE {}.{} CASCADE").format(sql.Identifier(t.sandbox), sql.Identifier(r[0])))
        c.commit()
    return {"schema": t.schema, "sandbox": t.sandbox, "business_tables_loaded": loaded}


# ---------------------------------------------------------------- introspection (authoritative catalog)
def columns(c, schema: str, table: str) -> list[dict]:
    rows = c.execute("""
        SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), cols.data_type, NOT a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attidentity <> '', col_description(a.attrelid, a.attnum)
        FROM pg_attribute a
        JOIN pg_class cl ON cl.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        LEFT JOIN information_schema.columns cols
               ON cols.table_schema = n.nspname AND cols.table_name = cl.relname AND cols.column_name = a.attname
        WHERE n.nspname = %s AND cl.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum""", (schema, table)).fetchall()
    return [{"attnum": r[0], "name": r[1], "type": r[2], "data_type": r[3], "nullable": r[4], "default": r[5],
             "identity": r[6], "comment": r[7]} for r in rows]


def catalog(c, schema: str) -> dict[str, list[dict]]:
    return {tb: columns(c, schema, tb) for tb in BUSINESS_TABLES}


def operation(cat: dict[str, list[dict]], schema: str, registered: Operation | None = None) -> Operation:
    """All business columns as one Operation ("body:<table>.<column>"). A column whose attnum matches a column of
    the registered schema in the same table but whose name changed is a catalog-proven rename (renamed_from)."""
    by_attnum = {}
    if registered is not None:
        for loc, f in registered.fields.items():
            tb, col = loc.split(":", 1)[1].split(".", 1)
            by_attnum[(tb, f.schema.get("x-attnum"))] = col
    fields = {}
    for tb, cols in cat.items():
        for col in cols:
            py = PG_TO_PY.get(col["data_type"] or "", "str")
            old = by_attnum.get((tb, col["attnum"]))
            renamed = old if old and old != col["name"] else None
            desc = col["comment"] or ""
            if renamed:
                desc = (desc + " " if desc else "") + f"(catalog: {tb} column attnum {col['attnum']} was named {renamed})"
            fields[f"body:{tb}.{col['name']}"] = FieldSpec(
                [py], required=not col["nullable"] and col["default"] is None and not col["identity"],
                description=desc[:500], renamed_from=renamed,
                schema={"type": PY_TO_JSON.get(py, "string"), "x-attnum": col["attnum"], "x-pg-type": col["type"],
                        "x-table": tb})
    return Operation("SQL", schema, fields, [], None, [], {}, None, source="postgres_catalog")


def snapshot(t: Target, registered: Operation | None = None) -> Operation:
    with connect(t) as c:
        cat = catalog(c, t.schema)
        c.rollback()
    missing = [tb for tb, cols in cat.items() if not cols]
    if missing:
        raise LiveDBError("invalid", f"missing tables in {t.schema}: {', '.join(missing)} - run Set up first")
    return operation(cat, t.schema, registered)


def browse(t: Target) -> list[dict]:
    from psycopg import sql
    with connect(t) as c:
        names = [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
                                         "ORDER BY table_name", (t.schema,))]
        out = []
        for name in names:
            n = c.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(t.schema), sql.Identifier(name))
                          ).fetchone()[0]
            out.append({"table": name, "rows": n, "columns": columns(c, t.schema, name),
                        "kind": "log" if name in LOG_TABLES else "business"})
        c.rollback()
    return out


def sample(t: Target, table: str, limit: int = 20) -> dict:
    from psycopg import sql
    if table not in BUSINESS_TABLES + LOG_TABLES:
        raise LiveDBError("invalid", f"unknown table {table}")
    with connect(t, read_only=True) as c:
        cur = c.execute(sql.SQL("SELECT * FROM {}.{} ORDER BY 1 LIMIT %s").format(
            sql.Identifier(t.schema), sql.Identifier(table)), (min(limit, 100),))
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, [str(v) if v is not None else None for v in r])) for r in cur.fetchall()]
        c.rollback()
    return {"table": table, "columns": names, "rows": rows}


def seed_summary(t: Target) -> dict:
    """Counts and integrity checks computed by the database itself."""
    from psycopg import sql
    s = sql.Identifier(t.schema)
    with connect(t, read_only=True) as c:
        counts = {tb: c.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(s, sql.Identifier(tb))).fetchone()[0]
                  for tb in BUSINESS_TABLES}
        try:
            row = c.execute(sql.SQL(
                "SELECT count(*) FILTER (WHERE o.total_amount = x.t), count(*), "
                "(SELECT count(DISTINCT customer_id) FROM {}.orders), "
                "(SELECT sum(total_amount) FROM {}.orders) "
                "FROM {}.orders o JOIN (SELECT order_id, sum(line_total) t FROM {}.order_items GROUP BY order_id) x "
                "USING (order_id)").format(s, s, s, s)).fetchone()
            checks = {"orders_with_consistent_totals": f"{row[0]}/{row[1]}", "customers_with_orders": row[2],
                      "revenue_total": str(row[3])}
        except Exception as e:                       # a fault may have broken these columns: say so
            c.rollback()
            checks = {"unavailable": error_message(e)}
        c.rollback()
    return {"counts": counts, "checks": checks}


# ---------------------------------------------------------------- reversible fault scenarios (demo schema only)
def fault_ddl(t: Target, scenario: str, revert: bool = False):
    """The exact DDL of a scenario (A-E) or its reverse. F changes no schema (see dbrepair)."""
    from psycopg import sql

    def tbl(name):
        return sql.SQL("{}.{}").format(sql.Identifier(t.schema), sql.Identifier(name))
    I = sql.Identifier
    if scenario == "A":
        a, b = ("client_id", "customer_id") if revert else ("customer_id", "client_id")
        return [sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(tbl("customers"), I(a), I(b))]
    if scenario == "B":
        a, b = ("client_id", "customer_id") if revert else ("customer_id", "client_id")
        return [sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(tbl("orders"), I(a), I(b))]
    if scenario == "C":
        if revert:
            return [sql.SQL("ALTER TABLE {} ALTER COLUMN line_total TYPE numeric(12,2) USING line_total::numeric")
                    .format(tbl("order_items")),
                    sql.SQL("ALTER TABLE {} ADD CONSTRAINT line_total_matches CHECK (line_total = quantity * unit_price)")
                    .format(tbl("order_items"))]
        return [sql.SQL("ALTER TABLE {} DROP CONSTRAINT line_total_matches").format(tbl("order_items")),
                sql.SQL("ALTER TABLE {} ALTER COLUMN line_total TYPE text USING line_total::text")
                .format(tbl("order_items"))]
    if scenario == "D":
        if revert:
            return [sql.SQL("ALTER TABLE {} DROP COLUMN source_system").format(tbl("inventory_events"))]
        return [sql.SQL("ALTER TABLE {} ADD COLUMN source_system text NOT NULL DEFAULT 'legacy'")
                .format(tbl("inventory_events")),
                sql.SQL("ALTER TABLE {} ALTER COLUMN source_system DROP DEFAULT").format(tbl("inventory_events"))]
    if scenario == "E":
        if revert:
            return None                                  # dropped data comes back only through a reset
        return [sql.SQL("ALTER TABLE {} DROP COLUMN payment_method").format(tbl("orders"))]
    return []


def apply_ddl(t: Target, statements) -> dict:
    with connect(t, 15000) as c:
        try:
            for s in statements:
                c.execute(s)
            text = [s.as_string(c) for s in statements]
            c.commit()
            return {"ok": True, "ddl": text, "error": None}
        except Exception as e:                           # transactional DDL: nothing changed
            c.rollback()
            return {"ok": False, "ddl": [s.as_string(c) for s in statements], "error": error_message(e),
                    "sqlstate": getattr(e, "sqlstate", None)}
