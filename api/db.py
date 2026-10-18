"""Postgres access: one SQLAlchemy engine, transactions with deadlock retry, small row helpers.

    python -m api.db migrate   # apply api/schema.sql (idempotent)

Spec §8 Locking rule: every sandbox write locks its run row first (`lock_run`) and checks it is current, so a write
on a RETIRED or unknown sandbox gets 409 sandbox_reset and a stale run version 409 stale_version, and changes
nothing. A deadlock or serialization failure (SQLSTATE 40P01, 40001, read from the pg8000 error's "C" field) is
retried up to 3 times with jitter (`in_tx`). The activity log is written in the same transaction as the change it
records, so a failed write leaves no entry (spec §4 Data flow).

The pool is 5 + 2 overflow with pre-ping and an explicit checkout wait (config), sized for Cloud SQL db-f1-micro's
25 connections. With CLOUD_SQL_INSTANCE set, the engine logs in through the Cloud SQL connector with IAM database
auth and lazy certificate refresh: under request-based billing the CPU is throttled between requests, so a
background refresh could miss its renewal (CLAUDE.md stack notes). The connector is imported only on that branch.
Every query is `sa.text` with bound parameters; the interpolated fragments are the lock mode, which comes from code
and is checked against LOCK_MODES, `insert_rows`' table, column and type names, which come from code and are checked
against [a-z_]+, and migrate()'s grantee, below.

migrate() runs only in the deploy job (and locally), never at app startup. With RUN_DB_USER set (the deploy job's
configuration, never a request), it validates the name against ROLE_NAME before its first statement, so a bad value
makes zero SQL calls (06-REVIEWS R2-9), applies the schema, then in a second transaction (a grant failure never rolls
back the schema) grants that role SELECT, INSERT, UPDATE, DELETE on every table and USAGE, SELECT on every sequence,
the name between double quotes. The deploy account owns the tables, so it can grant; doing it at every build replaces
default privileges, which only cover tables created after they were set (06-RESEARCH Pitfall 5: without the grants a
clone silently skips the tables information_schema hides). A role that does not exist fails the build job loudly.
"""
from __future__ import annotations

import functools
import json
import pathlib
import random
import re
import sys
import threading
import time
from contextlib import contextmanager

import sqlalchemy as sa

from . import config
from .errors import ApiError

_engine: sa.Engine | None = None
_local = threading.local()  # this thread's open transactions, innermost last (`current`)
RETRY_CODES = {"40P01", "40001"}  # deadlock, serialization failure
RETRIES = 3  # spec §8 Locking rule 3
LOCK_MODES = ("SHARE", "UPDATE")  # FOR SHARE for sandbox writes; FOR UPDATE: advance-day, settings, confirm, reset
MIGRATION_LOCK = 7212026  # pg_advisory_xact_lock key: one migration at a time
SCHEMA = pathlib.Path(__file__).with_name("schema.sql")
ROLE_NAME = re.compile(r"[a-z0-9][a-z0-9._@-]{0,62}")  # what RUN_DB_USER may be: an IAM database user name


class Unreachable(ConnectionError):
    """A connector failed before a database connection was available."""


def _connect(connector, **kwargs):
    try:
        return connector.connect(config.CLOUD_SQL_INSTANCE, "pg8000", user=config.DB_USER,
                                 db=config.DB_NAME, enable_iam_auth=True, **kwargs)
    except Exception as exc:
        raise Unreachable("database unreachable") from exc


def engine() -> sa.Engine:
    global _engine
    if _engine is None:
        if config.CLOUD_SQL_INSTANCE:
            from google.cloud.sql.connector import Connector  # Cloud SQL IAM database login (spec §4)
            connector = Connector(refresh_strategy="lazy")
            _engine = sa.create_engine(
                "postgresql+pg8000://", pool_size=config.POOL_SIZE, max_overflow=config.MAX_OVERFLOW,
                pool_timeout=config.POOL_TIMEOUT_S, pool_pre_ping=True,
                creator=lambda: _connect(connector))
        else:
            _engine = sa.create_engine(config.DATABASE_URL, pool_size=config.POOL_SIZE,
                                       max_overflow=config.MAX_OVERFLOW, pool_timeout=config.POOL_TIMEOUT_S,
                                       pool_pre_ping=True)
    return _engine


_probe_engine: sa.Engine | None = None


def probe_engine() -> sa.Engine:
    """A separate no-pool readiness engine with a 3 s timeout (11-CONTEXT D-01).

    The main engine is untouched: pg8000's timeout also applies to long statements.
    """
    global _probe_engine
    if _probe_engine is None:
        if config.CLOUD_SQL_INSTANCE:
            from google.cloud.sql.connector import Connector
            connector = Connector(refresh_strategy="lazy")
            _probe_engine = sa.create_engine(
                "postgresql+pg8000://", poolclass=sa.pool.NullPool,
                creator=lambda: _connect(connector, timeout=3))
        else:
            _probe_engine = sa.create_engine(config.DATABASE_URL, poolclass=sa.pool.NullPool,
                                             connect_args={"timeout": 3})
    return _probe_engine


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def sql_code(exc: Exception) -> str | None:
    """The SQLSTATE of a wrapped pg8000 error: pg8000 raises DatabaseError(dict of one-letter fields), "C" is the
    code."""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None)
    return args[0].get("C") if args and isinstance(args[0], dict) else None


@contextmanager
def tx():
    with engine().begin() as conn:
        stack = _local.__dict__.setdefault("conns", [])
        stack.append(conn)
        try:
            yield conn
        finally:
            stack.pop()


def current():
    """The connection of this thread's innermost open transaction, or None. Lets a shared loader (refdata) read on
    the caller's own connection instead of checking out a second one (WR-10)."""
    stack = getattr(_local, "conns", None)
    return stack[-1] if stack else None


def in_tx(fn, *args, **kwargs):
    """Run fn(conn, ...) in one transaction; retry up to 3 times on deadlock or serialization failure."""
    for attempt in range(RETRIES + 1):
        try:
            with tx() as conn:
                return fn(conn, *args, **kwargs)
        except sa.exc.DBAPIError as exc:
            if sql_code(exc) not in RETRY_CODES or attempt == RETRIES:
                raise
            time.sleep(random.uniform(0.02, 0.1) * (attempt + 1))


def rows(conn, sql: str, **params) -> list[dict]:
    return [dict(r) for r in conn.execute(sa.text(sql), params).mappings()]


def one(conn, sql: str, **params) -> dict | None:
    r = conn.execute(sa.text(sql), params).mappings().first()
    return dict(r) if r else None


def run(conn, sql: str, **params) -> int:
    return conn.execute(sa.text(sql), params).rowcount


INSERT_CHUNK = 512  # rows per multi-row INSERT at most; a power of two
MAX_PARAMS = 30000  # bound values per statement, far under PostgreSQL's 65,535
_NAME = re.compile(r"^[a-z_]+$")


@functools.lru_cache(maxsize=256)
def _insert_sql(table: str, columns: tuple[str, ...], n: int, casts: tuple[tuple[str, str], ...]) -> sa.TextClause:
    """The INSERT text for n rows: :p0, :p1, ... in row-major order, each value a bound parameter, a cast column
    wrapped in CAST(... AS type). Cached, so a few chunk sizes give a few SQL texts."""
    cast = dict(casts)
    width = len(columns)
    values = ", ".join("(" + ", ".join(f"CAST(:p{r * width + i} AS {cast[c]})" if c in cast else f":p{r * width + i}"
                                       for i, c in enumerate(columns)) + ")" for r in range(n))
    return sa.text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {values}")


def insert_rows(conn, table: str, columns, rows, casts: dict[str, str] | None = None) -> int:
    """Insert rows (sequences in `columns` order) with one multi-row INSERT per chunk (quick 261017-p52). Every value
    is bound as a parameter, exactly as a single-row INSERT binds it; `casts` maps a column to a SQL type (e.g.
    jsonb). The table, column and type names come from code and must match [a-z_]+, else ValueError. Chunks are
    powers of two up to INSERT_CHUNK, so the statement cache sees few texts. An empty list sends nothing. Returns the
    number of rows written."""
    columns = tuple(columns)
    casts = tuple(sorted((casts or {}).items()))
    for name in (table, *columns, *(n for pair in casts for n in pair)):
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(f"insert_rows: {name!r} is not a plain table, column or type name")
    rows = list(rows)
    width = len(columns)
    if any(len(r) != width for r in rows):
        raise ValueError(f"insert_rows: every row of {table} must have {width} values")
    cap = INSERT_CHUNK
    while cap > 1 and cap * width > MAX_PARAMS:
        cap //= 2
    i = 0
    while i < len(rows):
        left = len(rows) - i
        size = cap if left >= cap else 1 << (left.bit_length() - 1)
        params = {f"p{k}": v for k, v in enumerate(v for r in rows[i:i + size] for v in r)}
        conn.execute(_insert_sql(table, columns, size, casts), params)
        i += size
    return len(rows)


def js(value) -> str:
    """A Python value as a jsonb parameter: use CAST(:x AS jsonb) in the SQL."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def lock_run(conn, run_id: str | None, mode: str = "SHARE", version: int | None = None,
             statuses: tuple[str, ...] = ("ACTIVE",)) -> dict:
    """The locking rule (spec §8): every sandbox write first locks its run row and checks it is current."""
    if mode not in LOCK_MODES:
        raise ValueError(f"lock mode {mode!r} is not one of {', '.join(LOCK_MODES)}")
    r = one(conn, f"SELECT * FROM demo_runs WHERE run_id = :r FOR {mode}", r=run_id) if run_id else None
    if r is None or r["status"] not in statuses:
        raise ApiError(409, "sandbox_reset", "Your sandbox was reset. Reload to continue.")
    if version is not None and r["version"] != version:
        raise ApiError(409, "stale_version", "The demo moved on since you loaded this page.", version=r["version"])
    return r


def log(conn, run_id: str, day: int, type_: str, case_id: str | None = None, **payload) -> None:
    """Activity log entry, written in the same transaction as the change it records (spec §4 Data flow)."""
    run(conn, "INSERT INTO activity_log (run_id, seq, day, type, case_id, payload) "
              "VALUES (:r, nextval('activity_seq'), :d, :t, :c, CAST(:p AS jsonb))",
        r=run_id, d=day, t=type_, c=case_id, p=js(payload))


def statements(sql: str) -> list[str]:
    """schema.sql as statements: every `--` comment dropped (no string literal in it holds `--`), then split on `;`."""
    sql = "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())
    return [s.strip() for s in sql.split(";") if s.strip()]


def migrate() -> None:
    role = config.RUN_DB_USER
    if role and not ROLE_NAME.fullmatch(role):  # before any SQL; the message names the setting, never its value
        raise ValueError("RUN_DB_USER is not a valid database role name")
    with tx() as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK})
        conn.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS activity_seq"))
        for stmt in statements(SCHEMA.read_text(encoding="utf-8")):
            conn.execute(sa.text(stmt))
    if role:
        with tx() as conn:  # its own transaction: a grant failure never rolls back the schema
            conn.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"'))
            conn.execute(sa.text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))


if __name__ == "__main__":
    if sys.argv[1:] == ["migrate"]:
        migrate()
        print("schema up to date")
    else:
        print("usage: python -m api.db migrate", file=sys.stderr)
        sys.exit(2)
