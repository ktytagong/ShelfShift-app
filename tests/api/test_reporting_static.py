"""DB-free Reports boundaries: additive schema, shared results and read-module isolation (D-02, FLOW-08)."""
import ast
from pathlib import Path
import re

from api import db

PACKAGE = Path(db.__file__).resolve().parent
RESULT_TABLES = {'benchmark_runs', 'benchmark_days', 'benchmark_observations', 'benchmark_branch_day',
                 'benchmark_summary', 'benchmark_transfer_audit', 'evaluation_check_history'}


def test_result_tables_are_shared_without_run_id():
    found = set()
    for statement in db.statements(db.SCHEMA.read_text()):
        match = re.match(r'CREATE TABLE IF NOT EXISTS (\w+)', statement)
        if match and match[1] in RESULT_TABLES:
            found.add(match[1])
            # A sim_run_id FK may reference demo_runs(run_id). Only a declared run_id column is forbidden.
            assert not re.search(r'[(,]\s*run_id\s+\w+', statement), match[1]
    assert found == RESULT_TABLES


def test_schema_statements_are_additive_only():
    additive = re.compile(r'^(CREATE TABLE IF NOT EXISTS |CREATE (?:UNIQUE )?INDEX IF NOT EXISTS |'
                          r'ALTER TABLE \w+ ADD COLUMN IF NOT EXISTS )')
    statements = db.statements(db.SCHEMA.read_text())
    assert statements
    for statement in statements:
        assert additive.match(statement), statement
        assert not re.search(r'\b(?:DROP|RENAME|ALTER COLUMN)\b', statement, re.I), statement


def test_reporting_read_modules_never_reach_private_stock_or_driver():
    for name in ('reporting.py', 'routes_reporting.py'):
        path = PACKAGE / name
        source = path.read_text()
        assert 'truth_' not in source, name
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ''] + [a.name for a in node.names]
            else:
                continue
            assert not any(set(n.split('.')) & {'weekcheck', 'overlay'} for n in names), (name, names)
