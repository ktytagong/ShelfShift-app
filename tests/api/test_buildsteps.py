"""The template build's step lines (quick 261018-4nj): one flushed `step d<day> <name>: <s> s, <n> statements` line
per step of a template build's run, nothing for any other run, far fewer lines than the release reads, and none that
the release's build-log patterns (deploy/release.py parse_build and job_lines on build/deploy) could take for theirs."""
from __future__ import annotations

import re
import uuid

import pytest

from api import buildsteps, db, sandbox
from tests.api.conftest import drop_runs

LINE = re.compile(r"^step d\d+ [a-z0-9_.]+( [a-z_]+)?: \d+\.\d s, \d+ statements$")
# deploy/release.py on build/deploy (read only): parse_build's three patterns and job_lines' stop pattern
RELEASE = (re.compile(r"^previous ACTIVE: (\S+)$"), re.compile(r"^template (\S+) ACTIVE for \S+$"),
           re.compile(r"^retired: (\S+)$"), re.compile(r"^retired: "))
RELEASE_LIMIT = 500  # job_lines reads the job's log with --limit 500


class _Rollback(Exception):
    pass


def _scratch_template(conn, dataset_version: str) -> dict:
    run_id = f"tpl-steps-{uuid.uuid4().hex[:8]}"
    db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                 "(:r, 'TEMPLATE', :v, 'INITIALIZING', 2, 1)", r=run_id, v=dataset_version)
    return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


def test_a_template_builds_step_prints_one_line_with_its_day_and_statements(schema, capsys):
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            run = _scratch_template(conn, schema)
            capsys.readouterr()
            with buildsteps.step(run, "probe"):
                db.one(conn, "SELECT 1 AS x")
            raise _Rollback
    (line,) = _lines(capsys.readouterr().out)
    assert LINE.match(line), line
    assert line.startswith("step d2 probe: ") and line.endswith(" s, 1 statements"), line


def test_a_nested_steps_statements_count_in_the_outer_step_too(schema, capsys):
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            run = _scratch_template(conn, schema)
            capsys.readouterr()
            with buildsteps.step(run, "outer"):
                db.one(conn, "SELECT 1 AS x")
                with buildsteps.step(run, "step_world shelfshift"):
                    db.one(conn, "SELECT 2 AS x")
            raise _Rollback
    inner, outer = _lines(capsys.readouterr().out)
    assert all(LINE.match(line) for line in (inner, outer)), (inner, outer)
    assert inner.startswith("step d2 step_world shelfshift: ") and inner.endswith(" s, 1 statements")
    assert outer.startswith("step d2 outer: ") and outer.endswith(" s, 2 statements")


def test_a_failing_step_still_prints_its_line(schema, capsys):
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            run = _scratch_template(conn, schema)
            capsys.readouterr()
            with pytest.raises(ZeroDivisionError):
                with buildsteps.step(run, "probe"):
                    1 / 0
            raise _Rollback
    (line,) = _lines(capsys.readouterr().out)
    assert line.startswith("step d2 probe: ") and line.endswith(" s, 0 statements"), line


@pytest.mark.parametrize("kind, status", [("SANDBOX", "ACTIVE"), ("TEMPLATE", "ACTIVE"), ("TEMPLATE", "RETIRED")])
def test_any_other_run_prints_nothing(schema, capsys, kind, status):
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            run = {**_scratch_template(conn, schema), "kind": kind, "status": status}
            capsys.readouterr()
            with buildsteps.step(run, "probe"):
                db.one(conn, "SELECT 1 AS x")
            with buildsteps.step(None, "probe"):
                db.one(conn, "SELECT 1 AS x")
            raise _Rollback
    assert capsys.readouterr().out == ""


def test_the_tiny_build_prints_its_step_lines_and_nothing_else(template, public_dv, capsys):
    """A tiny public build through sandbox.build_template: fewer than 100 lines, every one a step line, none a line
    the release reads. Then the synthetic template is ACTIVE again and the tiny build's runs are deleted, as conftest's
    public_template teardown does."""
    with db.tx() as conn:
        before = sandbox.active_template(conn)["run_id"]
    capsys.readouterr()
    run_id = sandbox.build_template(public_dv)
    try:
        lines = _lines(capsys.readouterr().out)
        assert 0 < len(lines) < 100 and len(lines) < RELEASE_LIMIT, len(lines)
        assert [line for line in lines if not LINE.match(line)] == []
        assert [line for line in lines if any(p.search(line) for p in RELEASE)] == []
        names = [line.split(":")[0] for line in lines]
        for want in ("step d0 init_worlds", "step d0 day_start", "step d0 analyze", "step d0 open_cases",
                     "step d0 autopilot", "step d0 lifecycle_before", "step d0 baseline_morning",
                     "step d0 step_world shelfshift", "step d0 step_world baseline_rule", "step d0 step_world do_nothing",
                     "step d0 lifecycle_after", "step d1 day_start", "step d1 cases_morning", "step d0 advance_day",
                     "step d3 pick", "step d3 network_check", "step d3 limits_check", "step d3 problems"):
            assert want in names, (want, names)
        assert names.index("step d0 step_world do_nothing") < names.index("step d0 advance_day")
        assert names[-1] == "step d3 problems"
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :p", p=run_id)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=before)
        with db.tx() as conn:
            clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = :r",
                                                   r=run_id)]
            drop_runs(conn, [run_id, *clones])
