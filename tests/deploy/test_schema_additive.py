"""api/schema.sql stays additive (06-CONTEXT D-17): a release migrates the database before its new image serves, and
a failed release goes back to the previous known-good image on that migrated database, so no statement may drop,
rename or change the type of a table or column. DB-free: it reads the file through db.statements, as migrate does."""
import re

from api import db

ADDITIVE = re.compile(r"(CREATE TABLE IF NOT EXISTS|CREATE (UNIQUE )?INDEX IF NOT EXISTS|CREATE SEQUENCE IF NOT EXISTS"
                      r"|ALTER TABLE \S+ ADD COLUMN IF NOT EXISTS)\s", re.IGNORECASE)
FORBIDDEN = re.compile(r"\b(DROP|RENAME|ALTER\s+COLUMN)\b", re.IGNORECASE)
# The only recorded exceptions (deploy owner, 2026-10-18, merging Phase 9 into the freeze), matched as exact text.
# A rollback to the freeze image (195e298) on a database these ran on still works: that image never reads or writes
# ai_outputs or a GENERATING row, so the dropped index guarded nothing it does; and it reads templates only by
# status ACTIVE or RETIRED, so a READY row the widened status check allows is invisible to it.
EXCEPTIONS = (
    "ALTER TABLE demo_runs DROP CONSTRAINT IF EXISTS demo_runs_status_check",
    "ALTER TABLE demo_runs ADD CONSTRAINT demo_runs_status_check "
    "CHECK (status IN ('INITIALIZING', 'READY', 'ACTIVE', 'RETIRED', 'FAILED'))",
    "DROP INDEX IF EXISTS one_generation_at_a_time",
)


def normal(statement: str) -> str:
    return " ".join(statement.split())


def schema_statements() -> list[str]:
    return db.statements(db.SCHEMA.read_text(encoding="utf-8"))


def checked_statements() -> list[str]:
    """Every schema statement except the recorded exceptions."""
    return [s for s in schema_statements() if normal(s) not in EXCEPTIONS]


def test_every_schema_statement_only_adds():
    stmts = checked_statements()
    assert len(stmts) > 20  # the whole file was read, not an empty split
    assert [s.splitlines()[0] for s in stmts if not ADDITIVE.match(s)] == []


def test_no_schema_statement_drops_renames_or_alters_a_column():
    assert [s.splitlines()[0] for s in checked_statements() if FORBIDDEN.search(s)] == []


def test_each_exception_is_in_the_schema_exactly_once():
    """An exception the schema no longer holds is removed from the list, never left to excuse a later statement."""
    found = [normal(s) for s in schema_statements()]
    assert [found.count(e) for e in EXCEPTIONS] == [1] * len(EXCEPTIONS)


def test_the_guard_catches_a_destructive_statement():
    for bad in ("DROP TABLE lots", "ALTER TABLE lots RENAME COLUMN qty TO units", "ALTER TABLE lots ALTER COLUMN qty "
                "TYPE int", "ALTER TABLE lots DROP COLUMN tag", "CREATE TABLE lots (run_id text)"):
        assert not ADDITIVE.match(bad) or FORBIDDEN.search(bad), bad
