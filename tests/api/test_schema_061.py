"""The shared 06.1 schema and the route loader (04-API-CONTRACT "Product mode (06.1)", Tables and error codes;
06.1-04 Task 2).

Every additive 06.1 statement is written once in api/schema.sql by 06.1-04, so the later 06.1 API plans run in
parallel without sharing it; each new per-run table has run_id first and is copied by the workspace clone. Route
plans add an `api/routes_*.py` module that `app.load_route_modules` imports once; no test writes a file.
"""
import importlib.abc
import importlib.util
import pkgutil
import sys

import pytest
from fastapi.testclient import TestClient

from api import app as appmod
from api import db, sandbox

COLUMNS = {("sessions", "account"), ("cases", "owner_taken_by"), ("cases", "owner_taken_at"), ("cases", "closed_as"),
           ("cases", "closed_at"), ("cases", "closed_by"), ("engine_cache", "simple_rule"),
           ("recommendations", "simple_rule")}
TABLES = ("movements", "discrepancies", "lot_holds", "review_flags")
INDEXES = {("lot_holds", "one_active_hold_per_lot_kind"), ("review_flags", "one_flag_per_case_fact")}
# one row per new table with only its required columns (test scratch, rolled back)
ROWS = {
    "movements": "INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, qty, recorded_by, role, "
                 "scenario_day) VALUES (:r, 'm-1', 'c-1', 'a-1', 'DISPATCH', 3, 'S01', 'manager', 0)",
    "discrepancies": "INSERT INTO discrepancies (run_id, discrepancy_id, case_id, action_id, kind, units, open_units, "
                     "state) VALUES (:r, 'd-1', 'c-1', 'a-1', 'SHORT', 2, 2, 'OPEN')",
    "lot_holds": "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, state) VALUES "
                 "(:r, 'h-1', 'l-1', 'S01', 'MIXED_DATES', 'test', 'S01', 'ACTIVE')",
    "review_flags": "INSERT INTO review_flags (run_id, flag_id, case_id, fact_id, reason) VALUES "
                    "(:r, 'f-1', 'c-1', 'fact-1', 'test')",
}


class _Rollback(Exception):
    """Raised inside db.tx() so everything the test wrote is rolled back."""


def test_the_061_columns_tables_and_indexes_exist(schema):
    with db.tx() as conn:
        here = "table_schema = current_schema()"
        cols = {(r["table_name"], r["column_name"]) for r in db.rows(
            conn, f"SELECT table_name, column_name FROM information_schema.columns WHERE {here}")}
        tables = {r["table_name"] for r in db.rows(
            conn, "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
        indexes = {(r["tablename"], r["indexname"]) for r in db.rows(
            conn, "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = current_schema()")}
        first = {r["table_name"]: r["column_name"] for r in db.rows(
            conn, f"SELECT table_name, column_name FROM information_schema.columns WHERE {here} "
                  "AND ordinal_position = 1 AND table_name = ANY(:t)", t=list(TABLES))}
    assert COLUMNS <= cols, COLUMNS - cols
    assert set(TABLES) <= tables and INDEXES <= indexes
    assert first == {t: "run_id" for t in TABLES}  # run_id first, so the clone copies them


def test_a_fresh_workspace_copies_the_new_per_run_tables(template):
    with db.tx() as conn:
        assert set(TABLES) <= set(sandbox.per_run_tables(conn))
    copied = {}
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            tpl = sandbox.active_template(conn)
            for sql in ROWS.values():
                db.run(conn, sql, r=tpl["run_id"])
            new = sandbox.clone(conn, tpl)
            copied = {t: db.one(conn, f"SELECT count(*) AS n FROM {t} WHERE run_id = :r", r=new)["n"] for t in TABLES}
            raise _Rollback
    assert copied == {t: 1 for t in TABLES}
    with db.tx() as conn:  # nothing of the scratch rows is left on the template
        tpl = sandbox.active_template(conn)
        assert all(db.one(conn, f"SELECT count(*) AS n FROM {t} WHERE run_id = :r", r=tpl["run_id"])["n"] == 0
                   for t in TABLES)


def test_the_route_loader_imports_each_routes_module_once(monkeypatch):
    """A routes_ module the package lists (pkgutil.iter_modules patched to name `routes_probe`) and an in-memory
    loader provides (no file is written) is imported by load_route_modules once; its GET /api/probe answers; a second
    call registers nothing twice."""
    ran = []

    class Loader(importlib.abc.Loader):
        def create_module(self, spec):
            return None

        def exec_module(self, module):
            ran.append(module.__name__)

            @appmod.app.get("/api/probe")
            def probe():
                return {"probe": True}

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            return importlib.util.spec_from_loader(name, Loader()) if name == "api.routes_probe" else None

    real = pkgutil.iter_modules
    monkeypatch.setattr(pkgutil, "iter_modules", lambda path=None, prefix="": [
        *real(path, prefix), pkgutil.ModuleInfo(None, "routes_probe", False)])
    monkeypatch.setattr(sys, "meta_path", [Finder(), *sys.meta_path])
    before = list(appmod.app.router.routes)
    try:
        appmod.load_route_modules()
        appmod.load_route_modules()
        assert ran == ["api.routes_probe"]
        assert [getattr(r, "path", None) for r in appmod.app.router.routes].count("/api/probe") == 1
        assert TestClient(appmod.app).get("/api/probe").json() == {"probe": True}
    finally:
        appmod.app.router.routes[:] = before
        sys.modules.pop("api.routes_probe", None)
        appmod._route_modules.discard("routes_probe")
