"""python -m api.bench: the 3 s case budget on SUS-19, timed through the engine worker as requests use it
(spec §6 Time budget; 04-CONTEXT D-07). The budget is the spec's 3 s and is never raised here."""
import json
import pathlib
import re

import pytest

from api import bench, config, db, evaluate, sandbox, worker
from engine import to_dict

LINE = re.compile(r"^SUS-19: evaluate \+ fact checks, slowest of 3 = [0-9.]+ s \(budget 3 s\)$", re.M)


def test_the_story_case_fits_the_budget_here(template, capsys, monkeypatch):
    assert bench.BUDGET_S == 3.0
    monkeypatch.setattr(config, "ENGINE_WORKER", True)  # the request path: one warm worker process
    timed, real = [], worker.run

    def spy(fn, *args):
        timed.append(fn)
        return real(fn, *args)

    monkeypatch.setattr(worker, "run", spy)
    assert bench.main() == 0
    assert LINE.search(capsys.readouterr().out)
    assert timed == [evaluate._compute] * 3  # evaluate + fact checks, three times, through the worker
    assert worker.pid() is None  # the worker the bench started is stopped again


def test_bench_without_a_template_exits_2(template, capsys, monkeypatch):
    monkeypatch.setattr(sandbox, "active_template", lambda conn: None)
    assert bench.main() == 2
    assert "no ACTIVE template" in capsys.readouterr().err


# The release mode's printout and gate (06-CONTEXT D-16, D-19), and the frozen input's freshness (D-15).

INPUT = pathlib.Path(bench.__file__).resolve().parent / "bench_input.json"
RELEASE_ORDER = (
    re.compile(r"^config: ENGINE_WORKER=(on|off) engine_version=engine-3 git_commit=\S+ cpu\.max=\S+ memory\.max=\S+ "
               r"cpu_count=\S+ job=\S+ execution=\S+ revision=\S+$"),
    re.compile(r"^in-process startup \(not a platform cold start\): imports [0-9.]+ s, worker warm-up "
               r"([0-9.]+ s|off \(inline\))$"),
    re.compile(r"^run 1: [0-9.]+ s$"),
    re.compile(r"^run 2: [0-9.]+ s$"),
    re.compile(r"^run 3: [0-9.]+ s$"),
    LINE,
)


def template_story_input() -> dict:
    """to_dict of SUS-19's stored raw input on the ACTIVE template, as JSON would hold it (lists, not tuples)."""
    with db.tx() as conn:
        inp = bench.story_input(conn, sandbox.active_template(conn))
    return json.loads(json.dumps(to_dict(inp)))


def test_the_release_mode_prints_its_configuration_startup_and_every_run(template, capsys):
    assert bench.main() == 0  # conftest runs the engine inline (ENGINE_WORKER off)
    out = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(out) == len(RELEASE_ORDER), out
    for line, pattern in zip(out, RELEASE_ORDER):
        assert pattern.match(line), (line, pattern.pattern)
    assert "worker warm-up off (inline)" in out[1]
    assert not any(line.startswith("input: ") for line in out)  # the database input has no file hash


@pytest.mark.parametrize(("times", "code"), [
    ((2.99, 2.99, 2.99), 0),
    ((3.0, 1.0, 1.0), 1),  # one run at exactly the budget fails the release
    ((1.0, 3.5, 2.0), 1),  # the median (2.0) would pass the early check; the release gate is the slowest run
    ((2.996, 1.0, 1.0), 0),  # prints 3.00 and passes: the printed value never decides
    ((3.004, 1.0, 1.0), 1),  # prints 3.00 and fails
], ids=["2.99", "3.0-boundary", "slowest-not-median", "prints-3.00-passes", "prints-3.00-fails"])
def test_the_release_gate_is_the_unrounded_slowest_run(template, times, code, capsys, monkeypatch):
    monkeypatch.setattr(bench, "_time_runs", lambda inp, runs=bench.RUNS: list(times))
    assert bench.main() == code
    out = capsys.readouterr().out
    assert LINE.search(out)
    assert f"slowest of 3 = {max(times):.2f} s (budget 3 s)" in out


def test_the_frozen_input_matches_the_templates_story_input(template):
    committed = json.loads(INPUT.read_text(encoding="utf-8"))
    assert committed == template_story_input(), (
        "api/bench_input.json is stale against SUS-19 on a template built from the committed reference data: "
        "re-export with make bench-input")


def test_export_writes_the_templates_story_input(template, tmp_path, capsys):
    out = tmp_path / "bench_input.json"
    assert bench.main(["--export", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert json.loads(text) == template_story_input()
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=1) + "\n"  # the committed file's format
    assert f"wrote {out}: SUS-19 from template " in capsys.readouterr().out


def test_export_without_a_template_exits_2_and_writes_nothing(template, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sandbox, "active_template", lambda conn: None)
    out = tmp_path / "bench_input.json"
    assert bench.main(["--export", str(out)]) == 2
    assert "no ACTIVE template" in capsys.readouterr().err
    assert not out.exists()
