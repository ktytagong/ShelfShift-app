"""The slowest-case and reset bench (spec §6 Time budget, §8 reset target; 05.2-CONTEXT D-12, D-21, D-28;
api/casebench.py).

On the tiny public template (`public_template`, the open day's flags and catalogue cases), the bench ranks every
flag and live case by construction (eligible destinations, then lots of the product at the source and those
destinations, then key) in one SQL query with no engine call; times evaluate + fact checks through the engine worker
(median of 3) on the top M; times N resets on throwaway sessions and removes them; and exits 1 when the slowest
median is 3 s or more or a reset is 2000 ms or more, 2 with no ACTIVE template. The budgets are the spec's and are
never raised. Every expectation is derived here from the template's own rows and the stage-1 rule.
"""
from __future__ import annotations

import json
import re

import pytest

from api import casebench, db, eligibility, evaluate, inputs, refdata, sandbox, worker
from engine import from_dict, rec_version
from tests.api.conftest import fresh

LINE = re.compile(r"^(\S+) (\S+) (\S+) dests=(\d+) lots=(\d+) median=(\d+\.\d+) s$")


def by_hand(conn, run: dict) -> list[tuple]:
    """Every flag of the day the template has not opened and every live case, with (eligible destinations, lots of
    the product at the source and those destinations), from the stage-1 rule and the template's own lot rows."""
    ref = refdata.get(run["dataset_version"])
    policy = inputs.sandbox_policy(conn, run, ref)
    items = [(c["case_id"], c["source_branch"], c["sku"], c["lot_id"])
             for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state IN ('OPEN', 'WAITING', "
                                    "'PENDING_APPROVAL')", r=run["run_id"])]
    opened = {i[3] for i in items}
    items += [(f["key"], f["branch"], f["sku"], f["lot_id"])
              for f in db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r AND day = :d", r=run["run_id"],
                               d=run["day"]) if f["lot_id"] not in opened]
    out = []
    for key, store, sku, lot_id in items:
        lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                     r=run["run_id"], l=lot_id)
        runs = eligibility.eligible_runs(ref, lot, ref.products[sku], run["day"], policy)[0]
        dests = sorted({r.dest for r in runs})
        lots = db.one(conn, "SELECT count(*) AS n FROM lots WHERE run_id = :r AND world = 'shelfshift' AND sku = :s "
                            "AND qty > 0 AND branch = ANY(:b)", r=run["run_id"], s=sku, b=[store, *dests])["n"]
        out.append((key, store, sku, len(dests), lots))
    return sorted(out, key=lambda t: (-t[3], -t[4], t[0]))


@pytest.fixture
def counted(monkeypatch):
    """Every engine run (worker.run) and every read of the flags (the ranking query) casebench makes, counted. The
    sandbox Policy is read once beside it (inputs.sandbox_policy), whatever the number of items."""
    calls = {"engine": 0, "sql": 0}
    real_run, real_rows = worker.run, db.rows

    def rows(conn, sql, **params):
        calls["sql"] += "case_flags" in sql
        return real_rows(conn, sql, **params)
    monkeypatch.setattr(casebench.worker, "run", lambda fn, *a: calls.__setitem__("engine", calls["engine"] + 1)
                        or real_run(fn, *a))
    monkeypatch.setattr(casebench.db, "rows", rows)
    return calls


def test_slowest_candidates_rank_by_construction_in_one_query(public_template, counted):
    with db.tx() as conn:
        run = fresh(public_template)
        want = by_hand(conn, run)
        counted["sql"] = 0
        got = casebench.slowest_candidates(conn, run, len(want) + 5)
    assert counted == {"engine": 0, "sql": 1}
    assert [(c["key"], c["store"], c["product"], c["dests"], c["lots"]) for c in got] == want
    assert any(c["dests"] > 0 for c in got) and any(c["dests"] == 0 for c in got)  # the unmapped store has none
    with db.tx() as conn:
        assert [c["key"] for c in casebench.slowest_candidates(conn, fresh(public_template), 2)] == \
            [w[0] for w in want[:2]]


def test_time_cases_times_each_input_through_the_worker_median_of_three(public_template, counted, capsys):
    with db.tx() as conn:
        run = fresh(public_template)
        top = casebench.slowest_candidates(conn, run, 3)
        timed = casebench.time_cases(conn, run, top)
    assert counted["engine"] == 3 * len(top)
    lines = [l for l in capsys.readouterr().out.splitlines() if LINE.match(l)]
    assert len(lines) == len(top) == len(timed)
    for line, cand, t in zip(lines, top, timed):
        key, store, product, dests, lots, median = LINE.match(line).groups()
        assert (key, store, product, int(dests), int(lots)) == \
            (cand["key"], cand["store"], cand["product"], cand["dests"], cand["lots"])
        assert float(median) == pytest.approx(t["median_s"], abs=0.005) and t["median_s"] >= 0
    with db.tx() as conn:  # the input timed is the one inputs.build_input gives for the item's lot
        assert rec_version(timed[0]["input"]) == rec_version(inputs.build_input(conn, run, top[0]["lot_id"]))


def test_time_resets_times_each_reset_and_removes_its_sessions(public_template, capsys):
    with db.tx() as conn:
        before = {t: db.one(conn, f"SELECT count(*) AS n FROM {t}")["n"] for t in ("sessions", "demo_runs", "cases")}
    times = casebench.time_resets(2)
    assert len(times) == 2 and all(isinstance(ms, int) and ms >= 0 for ms in times)
    out = capsys.readouterr().out
    assert all(f"reset {i + 1}: {ms} ms" in out for i, ms in enumerate(times))
    with db.tx() as conn:
        after = {t: db.one(conn, f"SELECT count(*) AS n FROM {t}")["n"] for t in ("sessions", "demo_runs", "cases")}
    assert after == before  # every throwaway session and workspace is gone


def test_main_measures_the_template_and_exports_the_slowest_input(public_template, tmp_path, capsys):
    path = tmp_path / "slowest.json"
    assert casebench.main(["--top", "3", "--resets", "2", "--export", str(path)]) == 0
    out = capsys.readouterr().out
    slowest = re.search(r"^slowest: (\S+) ", out, re.M)
    assert slowest and "budget 3 s" in out and "budget 2000 ms" in out
    inp = from_dict(json.loads(path.read_text(encoding="utf-8")))
    with db.tx() as conn:
        run = fresh(public_template)
        case = next(c for c in casebench.slowest_candidates(conn, run, 3) if c["key"] == slowest.group(1))
        assert rec_version(inp) == rec_version(inputs.build_input(conn, run, case["lot_id"]))
    assert evaluate._compute(inp)[0]["engine_version"]  # the exported input evaluates


@pytest.mark.parametrize("case_s, reset_ms, code", [(2.99, 1999, 0), (3.0, 10, 1), (0.1, 2000, 1)])
def test_main_exits_on_the_fixed_budgets(public_template, monkeypatch, case_s, reset_ms, code):
    assert (casebench.CASE_BUDGET_S, casebench.RESET_BUDGET_MS) == (3.0, 2000)  # the spec's, never raised
    monkeypatch.setattr(casebench, "time_cases", lambda conn, run, cands, runs=3: [
        {**c, "median_s": case_s, "input": None} for c in cands])
    monkeypatch.setattr(casebench, "time_resets", lambda n: [reset_ms] * n)
    assert casebench.main(["--top", "2", "--resets", "2"]) == code


def test_main_exits_2_without_an_active_template(monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "active_template", lambda conn: None)
    assert casebench.main(["--top", "1", "--resets", "1"]) == 2
    assert "no ACTIVE template" in capsys.readouterr().err
