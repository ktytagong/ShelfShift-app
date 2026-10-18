"""python -m api.bench --top N [--resets M]: the release gate bench on a public set (05.2 D-12; quick 261017-v6z).

On the tiny public template (`public_template`) the cases are ranked by api/casebench.py's rule before any timing, each
is timed as the SUS-19 gate times it (its raw input built on the template, then evaluate + fact checks through the
worker 3 times), then M resets. The two gate lines are a contract with deploy/release.py, so they are matched here
with the release's own regexes. The budgets are the existing 3 s and 2000 ms and are never raised here.
"""
from __future__ import annotations

import re

import pytest

from api import bench, casebench, db, evaluate, inputs, sandbox, worker

TOP_GATE = re.compile(r"^top \d+: evaluate \+ fact checks, slowest of \d+ = (\d+\.\d+) s on \S+ \(budget 3 s\); "
                      r"\d+ not timed$")
RESET_GATE = re.compile(r"^resets: \d+, slowest (\d+) ms \(budget 2000 ms\)$")
TIMED = re.compile(r"^(\S+) (\S+) (\S+) dests=(\d+) lots=(\d+) slowest=(\d+\.\d+) s median=(\d+\.\d+) s$")
UNTIMED = re.compile(r"^(\S+) (\S+) (\S+) dests=(\d+) lots=(\d+) not timed: .+$")


def ranked(top: int) -> list[dict]:
    with db.tx() as conn:
        return casebench.slowest_candidates(conn, sandbox.active_template(conn), top)


def run(capsys, *argv: str) -> tuple[int, list[str], str]:
    code = bench.main(list(argv))
    printed = capsys.readouterr()
    return code, printed.out.splitlines(), printed.err


def test_the_budgets_are_the_existing_constants():
    assert (bench.BUDGET_S, casebench.RESET_BUDGET_MS, bench.RUNS) == (3.0, 2000, 3)


def test_top_times_the_ranked_cases_and_prints_the_two_gate_lines(public_template, capsys, monkeypatch):
    timed, real = [], worker.run

    def spy(fn, *args):
        timed.append(fn)
        return real(fn, *args)

    monkeypatch.setattr(worker, "run", spy)
    want = ranked(20)
    assert want, "the tiny public template has no candidate"
    code, lines, _ = run(capsys, "--top", "20", "--resets", "2")
    assert code == 0, lines
    assert lines[0].startswith("config: ") and lines[1].startswith("in-process startup (not a platform cold start)")
    rows = [TIMED.match(x) or UNTIMED.match(x) for x in lines[2:2 + len(want)]]
    assert all(rows), lines
    assert [m.group(1) for m in rows] == [c["key"] for c in want]  # ranked before any timing, in that order
    assert [(m.group(4), m.group(5)) for m in rows] == [(str(c["dests"]), str(c["lots"])) for c in want]
    n_timed = sum(1 for m in rows if m.re is TIMED)
    assert n_timed and timed == [evaluate._compute] * (3 * n_timed)
    assert lines[2 + len(want):2 + len(want) + 2] == [x for x in lines if x.startswith("reset ")]
    top, resets = lines[-2], lines[-1]
    assert TOP_GATE.match(top) and top.startswith("top 20: ") and top.endswith(f"; {len(want) - n_timed} not timed")
    slowest = max(rows, key=lambda m: float(m.group(6)) if m.re is TIMED else -1.0)
    assert f" on {slowest.group(1)} " in top
    assert RESET_GATE.match(resets) and resets.startswith("resets: 2, ")
    assert worker.pid() is None


def test_without_resets_there_is_no_reset_line(public_template, capsys):
    code, lines, _ = run(capsys, "--top", "1")
    assert code == 0 and TOP_GATE.match(lines[-1]) and not [x for x in lines if RESET_GATE.match(x)]


def test_an_item_without_its_data_is_reported_not_timed(public_template, capsys, monkeypatch):
    first = ranked(2)[0]
    real = inputs.build_input

    def missing(conn, run, lot_id, *a, **k):
        if lot_id == first["lot_id"]:
            raise LookupError("no forecast for that day")
        return real(conn, run, lot_id, *a, **k)

    monkeypatch.setattr(inputs, "build_input", missing)
    code, lines, _ = run(capsys, "--top", "2")
    assert f"{first['key']} {first['store']} {first['product']} dests={first['dests']} lots={first['lots']} " \
           f"not timed: no forecast for that day" in lines
    assert TOP_GATE.match(lines[-1]) and lines[-1].endswith("; 1 not timed") and code == 0


def test_a_case_over_3_s_exits_1(public_template, capsys, monkeypatch):
    monkeypatch.setattr(bench, "_time_runs", lambda inp, runs=bench.RUNS: [1.0, 3.0, 1.0])
    code, lines, _ = run(capsys, "--top", "1")
    assert code == 1 and float(TOP_GATE.match(lines[-1]).group(1)) == 3.0


def test_a_reset_over_2000_ms_exits_1(public_template, capsys, monkeypatch):
    monkeypatch.setattr(casebench, "time_resets", lambda n: [5] * (n - 1) + [2000])
    code, lines, _ = run(capsys, "--top", "1", "--resets", "3")
    assert code == 1 and RESET_GATE.match(lines[-1]).group(1) == "2000"


def test_nothing_timed_or_no_template_exits_2(public_template, capsys, monkeypatch):
    monkeypatch.setattr(casebench, "slowest_candidates", lambda conn, template, top: [])
    code, _, err = run(capsys, "--top", "5")
    assert code == 2 and "nothing" in err
    monkeypatch.setattr(sandbox, "active_template", lambda conn: None)
    code, _, err = run(capsys, "--top", "5")
    assert code == 2 and "no ACTIVE template" in err


@pytest.mark.parametrize("argv", [["--top", "2", "--input", "x.json"], ["--top", "2", "--export", "x.json"],
                                  ["--resets", "2"], ["--top", "0"]])
def test_top_is_its_own_mode(argv):
    with pytest.raises(SystemExit):
        bench.main(argv)
