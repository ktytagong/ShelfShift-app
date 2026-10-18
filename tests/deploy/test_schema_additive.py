"""api/schema.sql stays additive (06-CONTEXT D-17): a release migrates the database before its new image serves, and
a failed release goes back to the previous known-good image on that migrated database, so no statement may drop,
rename or change the type of a table or column. DB-free: it reads the file through db.statements, as migrate does."""
import re

from api import db

ADDITIVE = re.compile(r"(CREATE TABLE IF NOT EXISTS|CREATE (UNIQUE )?INDEX IF NOT EXISTS|CREATE SEQUENCE IF NOT EXISTS"
                      r"|ALTER TABLE \S+ ADD COLUMN IF NOT EXISTS)\s", re.IGNORECASE)
FORBIDDEN = re.compile(r"\b(DROP|RENAME|ALTER\s+COLUMN)\b", re.IGNORECASE)


def schema_statements() -> list[str]:
    return db.statements(db.SCHEMA.read_text(encoding="utf-8"))


def test_every_schema_statement_only_adds():
    stmts = schema_statements()
    assert len(stmts) > 20  # the whole file was read, not an empty split
    assert [s.splitlines()[0] for s in stmts if not ADDITIVE.match(s)] == []


def test_no_schema_statement_drops_renames_or_alters_a_column():
    assert [s.splitlines()[0] for s in schema_statements() if FORBIDDEN.search(s)] == []


def test_the_guard_catches_a_destructive_statement():
    for bad in ("DROP TABLE lots", "ALTER TABLE lots RENAME COLUMN qty TO units", "ALTER TABLE lots ALTER COLUMN qty "
                "TYPE int", "ALTER TABLE lots DROP COLUMN tag", "CREATE TABLE lots (run_id text)"):
        assert not ADDITIVE.match(bad) or FORBIDDEN.search(bad), bad
