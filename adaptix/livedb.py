"""Live Project Integration: a PostgreSQL database (e.g. Neon) reached through a server-side DATABASE_URL.

Everything ADAPTIX does here is confined to an ISOLATED demo schema (default `adaptix_demo`) and its sandbox
(`<schema>_sandbox`), filled with SYNTHETIC data. Identifiers are always passed through psycopg.sql
(never string-formatted), schema names must start with `adaptix_`, and the connection URL is never logged,
returned or shown: only the host, database name and TLS mode are displayed.

The database plays the role of the "API" whose schema drifts: the business operation is an INSERT of an order,
and the tool definition maps the caller's arguments to columns. Schema facts come from the catalog
(information_schema + pg_attribute), which is authoritative: a renamed column keeps its attnum, so a rename is
proven by the catalog rather than guessed from names.
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import env
from .schema import FieldSpec, Operation
from .security import register_secret, scrub

SCHEMA_RE = re.compile(r"^adaptix_[a-z0-9_]{1,40}$")
TABLE = "orders"
SYSTEM_COLUMNS = {"order_id", "request_key", "created_at"}          # managed by ADAPTIX / the database
PG_TO_PY = {"integer": "int", "bigint": "int", "smallint": "int", "numeric": "float", "real": "float",
            "double precision": "float", "text": "str", "character varying": "str", "character": "str",
            "uuid": "str", "boolean": "bool", "date": "date", "timestamp with time zone": "str",
            "timestamp without time zone": "str", "jsonb": "object", "json": "object"}
PY_TO_JSON = {"int": "integer", "float": "number", "str": "string", "bool": "boolean", "date": "string",
              "object": "object"}
TYPE_CHOICES = {"text": "text", "bigint": "bigint", "integer": "integer", "numeric": "numeric",
                "varchar(64)": "varchar(64)", "jsonb": "jsonb"}

BASELINE_DDL = """
CREATE TABLE {s}.customers (
  customer_id text PRIMARY KEY,
  full_name   text NOT NULL,
  tier        text NOT NULL DEFAULT 'standard'
);
CREATE TABLE {s}.orders (
  order_id    bigserial PRIMARY KEY,
  request_key text NOT NULL UNIQUE,
  customer_id text NOT NULL,
  sku         text NOT NULL,
  quantity    integer NOT NULL,
  note        text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
COMMENT ON COLUMN {s}.orders.customer_id IS 'Identifier of the customer placing the order';
COMMENT ON COLUMN {s}.orders.sku IS 'Product SKU';
COMMENT ON COLUMN {s}.orders.quantity IS 'Number of units ordered';
COMMENT ON COLUMN {s}.orders.note IS 'Optional free-text note from the customer';
"""


class LiveDBError(RuntimeError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category        # not_configured | connection | permission | invalid | unsafe


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
        if host.startswith("/"):
            host = "local socket"
        return {"host": host, "database": (p.path or "/").lstrip("/") or q.get("dbname", ""),
                "sslmode": q.get("sslmode", "default"), "schema": self.schema, "sandbox": self.sandbox}


def target_from_env(url: str | None = None, schema: str | None = None) -> Target:
    url = url or env("DATABASE_URL")
    if not url:
        raise LiveDBError("not_configured", "DATABASE_URL is not set on the server (Streamlit Cloud: Settings -> "
                                            "Secrets). See docs: Live Project Integration setup.")
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
def connect(t: Target, statement_timeout_ms: int = 5000):
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
        conn.commit()
        yield conn
    finally:
        conn.close()


def test_connection(t: Target) -> dict:
    t0 = time.perf_counter()
    with connect(t) as c:
        row = c.execute("SELECT version(), current_database(), current_user, "
                        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                        "(SELECT rolcreaterole FROM pg_roles WHERE rolname = current_user), "
                        "has_database_privilege(current_database(), 'CREATE'), "
                        "(SELECT count(*) FROM pg_namespace WHERE nspname = %s), "
                        "current_setting('ssl', true)", (t.schema,)).fetchone()
        c.rollback()
    warnings = []
    if row[3]:
        warnings.append("This role is a SUPERUSER. Use a dedicated least-privilege role (see setup instructions).")
    if row[4]:
        warnings.append("This role can create roles. A dedicated role without CREATEROLE is recommended.")
    return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "server": row[0].split(",")[0],
            "database": row[1], "superuser": bool(row[3]), "can_create_schema": bool(row[5]),
            "demo_schema_exists": bool(row[6]), "ssl": row[7], "target": t.display(), "warnings": warnings}


# ---------------------------------------------------------------- setup, reset, synthetic data
def _sql(stmt: str, t: Target):
    from psycopg import sql
    return sql.SQL(stmt).format(s=sql.Identifier(t.schema))


def setup(t: Target, reset: bool = False) -> dict:
    """Create (or reset) the isolated demo schema and sandbox with SYNTHETIC data only."""
    from psycopg import sql
    with connect(t, 30000) as c:
        for s in (t.schema, t.sandbox):
            if reset:
                c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(s)))
            c.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(s)))
        exists = c.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema=%s AND table_name='orders'",
                           (t.schema,)).fetchone()[0]
        if not exists:
            for stmt in [x.strip() for x in BASELINE_DDL.split(";") if x.strip()]:
                c.execute(_sql(stmt, t))
            c.execute(sql.SQL("INSERT INTO {}.customers VALUES " + ",".join(["(%s,%s,%s)"] * 12)).format(
                sql.Identifier(t.schema)),
                [v for i in range(1, 13) for v in (f"cust-{i:04d}", f"Synthetic Customer {i:02d}",
                                                    "gold" if i % 4 == 0 else "standard")])
            c.execute(sql.SQL("INSERT INTO {}.orders (request_key, customer_id, sku, quantity, note) VALUES "
                              + ",".join(["(%s,%s,%s,%s,%s)"] * 6)).format(sql.Identifier(t.schema)),
                      [v for i in range(1, 7) for v in (f"seed-{i}", f"cust-{i:04d}", f"SKU-{100 * (i % 3 + 1)}",
                                                         i, None if i % 2 else "synthetic seed row")])
        c.commit()
    return {"schema": t.schema, "sandbox": t.sandbox, "created": not exists or reset}


# ---------------------------------------------------------------- introspection (authoritative catalog)
def columns(c, schema: str, table: str = TABLE) -> list[dict]:
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


def operation(cols: list[dict], schema: str, registered: Operation | None = None) -> Operation:
    """The table as an Operation: one location per writable column. A column whose attnum matches a column
    of the registered schema but whose name changed is a catalog-proven rename (renamed_from)."""
    by_attnum = {}
    if registered is not None:
        for loc, f in registered.fields.items():
            if f.schema.get("x-attnum") is not None:
                by_attnum[f.schema["x-attnum"]] = loc.split(":", 1)[1]
    fields = {}
    for col in cols:
        if col["name"] in SYSTEM_COLUMNS:
            continue
        py = PG_TO_PY.get(col["data_type"] or "", "str")
        old = by_attnum.get(col["attnum"])
        renamed = old if old and old != col["name"] else None
        desc = col["comment"] or ""
        if renamed:
            desc = (desc + " " if desc else "") + f"(catalog: column attnum {col['attnum']} was named {renamed})"
        fields[f"body:{col['name']}"] = FieldSpec(
            [py], required=not col["nullable"] and col["default"] is None and not col["identity"],
            description=desc[:500], renamed_from=renamed,
            schema={"type": PY_TO_JSON.get(py, "string"), "x-attnum": col["attnum"], "x-pg-type": col["type"]})
    return Operation("INSERT", f"{schema}.{TABLE}", fields, [], None, [], {}, None, source="postgres_catalog")


def snapshot(t: Target, registered: Operation | None = None) -> Operation:
    with connect(t) as c:
        cols = columns(c, t.schema)
        c.rollback()
    if not cols:
        raise LiveDBError("invalid", f"{t.schema}.{TABLE} does not exist - run the setup first")
    return operation(cols, t.schema, registered)


def browse(t: Target) -> list[dict]:
    from psycopg import sql
    with connect(t) as c:
        tables = [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
                                          "ORDER BY table_name", (t.schema,))]
        out = []
        for name in tables:
            n = c.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(t.schema), sql.Identifier(name))
                          ).fetchone()[0]
            out.append({"table": name, "rows": n, "columns": columns(c, t.schema, name)})
        c.rollback()
    return out


def sample(t: Target, table: str, limit: int = 20) -> dict:
    from psycopg import sql
    with connect(t) as c:
        if not c.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                         (t.schema, table)).fetchone():
            raise LiveDBError("invalid", f"no table {table} in {t.schema}")
        cur = c.execute(sql.SQL("SELECT * FROM {}.{} ORDER BY 1 DESC LIMIT %s").format(
            sql.Identifier(t.schema), sql.Identifier(table)), (min(limit, 100),))
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, [str(v) if v is not None else None for v in r])) for r in cur.fetchall()]
        c.rollback()
    return {"table": table, "columns": names, "rows": rows}


# ---------------------------------------------------------------- fault injection (demo schema only)
FAULTS = ("rename_column", "change_type", "add_required_column", "remove_column")
IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")


def inject(t: Target, fault: str, column: str | None = None, new_name: str | None = None,
           new_type: str | None = None) -> dict:
    """Apply one schema fault to <schema>.orders. Returns the exact DDL that ran."""
    from psycopg import sql
    if fault not in FAULTS:
        raise LiveDBError("invalid", f"unknown fault {fault}")
    for v in (column, new_name):
        if v is not None and not IDENT.match(v):
            raise LiveDBError("invalid", f"invalid identifier {v!r}")
    if column in SYSTEM_COLUMNS:
        raise LiveDBError("invalid", f"{column} is managed by ADAPTIX and cannot be targeted")
    tbl = sql.SQL("{}.{}").format(sql.Identifier(t.schema), sql.Identifier(TABLE))
    if fault == "rename_column":
        stmts = [sql.SQL("ALTER TABLE {} RENAME COLUMN {} TO {}").format(tbl, sql.Identifier(column),
                                                                          sql.Identifier(new_name))]
    elif fault == "change_type":
        if new_type not in TYPE_CHOICES:
            raise LiveDBError("invalid", f"type must be one of {sorted(TYPE_CHOICES)}")
        using = "to_jsonb({})" if new_type == "jsonb" else "{}::" + TYPE_CHOICES[new_type]
        stmts = [sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE " + TYPE_CHOICES[new_type] + " USING " + using).format(
            tbl, sql.Identifier(column), sql.Identifier(column))]
    elif fault == "add_required_column":
        name = new_name or "channel"
        stmts = [sql.SQL("ALTER TABLE {} ADD COLUMN {} text NOT NULL DEFAULT 'legacy'").format(tbl, sql.Identifier(name)),
                 sql.SQL("ALTER TABLE {} ALTER COLUMN {} DROP DEFAULT").format(tbl, sql.Identifier(name))]
    else:
        stmts = [sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(tbl, sql.Identifier(column))]
    with connect(t, 15000) as c:
        before = columns(c, t.schema)
        try:
            for s in stmts:
                c.execute(s)
            after = columns(c, t.schema)
            c.commit()
            err = None
        except Exception as e:                       # the DDL failed: nothing changed (transactional DDL)
            c.rollback()
            after, err = before, scrub(str(e)).split("\n")[0][:200]
        ddl = [s.as_string(c) for s in stmts]
    return {"fault": fault, "column": column, "new_name": new_name, "new_type": new_type, "ddl": ddl,
            "before": before, "after": after, "ok": err is None, "error": err}
