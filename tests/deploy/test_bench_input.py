"""python -m api.bench --input FILE: the early case-budget check on the frozen SUS-19 input (06-CONTEXT D-15, D-19).

DB-free: every test here would fail if the bench touched the database (db.tx and db.engine are replaced by a function
that raises). The gate is the median of 3 runs against the spec's 3 s, compared unrounded; the release gate
(slowest of 3 on the database, tests/api/test_bench.py) is a separate check that this one never replaces (D-16)."""
import hashlib
import json
import os
import pathlib
import re
import secrets

import pytest

os.environ.setdefault("DEMO_PASSCODE", secrets.token_urlsafe(16))  # before importing api, like tests/deploy's others

from api import bench, config, db, evaluate, worker  # noqa: E402
from engine import ENGINE_VERSION  # noqa: E402
from tests.data.test_honesty import PROJECT_ID_LITERAL, STALE_PROJECT_ID, denylist  # noqa: E402  (never respelled)

INPUT = pathlib.Path(bench.__file__).resolve().parent / "bench_input.json"
MEDIAN = re.compile(r"^SUS-19 \(frozen input\): evaluate \+ fact checks, median of 3 = ([0-9.]+) s \(budget 3 s\)$",
                    re.M)
ORDER = (
    re.compile(r"^config: ENGINE_WORKER=(on|off) engine_version=engine-3 git_commit=\S+ cpu\.max=\S+ memory\.max=\S+ "
               r"cpu_count=\S+ job=\S+ execution=\S+ revision=\S+$"),
    re.compile(r"^input: .+ sha256=[0-9a-f]{64}$"),  # the path as given (it may hold spaces)
    re.compile(r"^in-process startup \(not a platform cold start\): imports [0-9.]+ s, worker warm-up "
               r"([0-9.]+ s|off \(inline\))$"),
    re.compile(r"^run 1: [0-9.]+ s$"),
    re.compile(r"^run 2: [0-9.]+ s$"),
    re.compile(r"^run 3: [0-9.]+ s$"),
    MEDIAN,
)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """File mode never opens a connection: any database call fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("the file-mode bench touched the database")

    monkeypatch.setattr(db, "tx", refuse)
    monkeypatch.setattr(db, "engine", refuse)


def fixed(*times):
    """A _time_runs stand-in that returns the given seconds for the shipped input."""
    return lambda inp, runs=bench.RUNS: list(times)


def lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line]


def test_the_budget_is_three_seconds_and_nothing_raises_it():
    assert bench.BUDGET_S == 3.0 and bench.RUNS == 3
    for extra in (["--budget", "5"], ["--input", str(INPUT), "--budget=5"]):
        with pytest.raises(SystemExit) as exc:
            bench.main(extra)
        assert exc.value.code == 2  # argparse refuses an unknown option


def test_the_committed_input_is_a_current_engine_input():
    d = json.loads(INPUT.read_text(encoding="utf-8"))
    assert d["engine_version"] == ENGINE_VERSION == "engine-3"
    assert d["case_lot_id"] and d["lots"] and d["forecasts"]
    assert d["policy"]["shortlist_step"] == 0  # the gate times the full search, as shipped


def test_the_committed_input_holds_no_project_or_resource_identifier():
    text = INPUT.read_text(encoding="utf-8")
    assert not PROJECT_ID_LITERAL.search(text)
    assert STALE_PROJECT_ID not in text


def test_the_committed_input_never_names_the_retailer():
    text = INPUT.read_text(encoding="utf-8")
    for index, name in enumerate(denylist(), 1):  # skips when the denylist file is not configured
        assert not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I), f"denylist entry {index}"


def test_file_mode_times_the_frozen_input_through_the_worker(capsys, monkeypatch):
    monkeypatch.setattr(config, "ENGINE_WORKER", True)  # the service's path: one warm worker process
    timed, real = [], worker.run

    def spy(fn, *args):
        timed.append(fn)
        return real(fn, *args)

    monkeypatch.setattr(worker, "run", spy)
    assert worker.pid() is None
    assert bench.main(["--input", str(INPUT)]) == 0
    out = lines(capsys.readouterr().out)
    assert len(out) == len(ORDER), out
    for line, pattern in zip(out, ORDER):
        assert pattern.match(line), (line, pattern.pattern)
    assert out[0].startswith("config: ENGINE_WORKER=on ")
    assert re.match(r"^in-process startup .*worker warm-up [0-9.]+ s$", out[2])
    assert timed == [evaluate._compute] * 3  # evaluate + fact checks, three times, through the worker
    assert worker.pid() is None  # the worker the bench started is stopped again


def test_the_input_line_names_the_sha256_of_the_file_bytes(capsys, monkeypatch):
    monkeypatch.setattr(bench, "_time_runs", fixed(1.0, 1.0, 1.0))
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    assert bench.main(["--input", str(INPUT)]) == 0
    digest = hashlib.sha256(INPUT.read_bytes()).hexdigest()
    assert f"input: {INPUT} sha256={digest}" in lines(capsys.readouterr().out)


def test_inline_mode_says_the_warm_up_is_off(capsys, monkeypatch):
    monkeypatch.setattr(bench, "_time_runs", fixed(1.0, 1.0, 1.0))
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    assert bench.main(["--input", str(INPUT)]) == 0
    out = capsys.readouterr().out
    assert "config: ENGINE_WORKER=off " in out
    assert re.search(r"^in-process startup \(not a platform cold start\): imports [0-9.]+ s, worker warm-up off "
                     r"\(inline\)$", out, re.M)


@pytest.mark.parametrize(("times", "code"), [
    ((2.99, 2.99, 2.99), 0),
    ((3.0, 3.0, 3.0), 1),  # exactly the budget fails: strictly below 3 s
    ((3.1, 1.0, 3.2), 1),  # median 3.1
    ((1.0, 3.5, 2.0), 0),  # the median gates here, not the slowest run (the release gate is the slowest)
    ((2.996, 2.996, 2.996), 0),  # prints 3.00 and passes: the printed value never decides
    ((3.004, 3.004, 3.004), 1),  # prints 3.00 and fails
], ids=["2.99", "3.0-boundary", "median-over", "median-not-slowest", "prints-3.00-passes", "prints-3.00-fails"])
def test_the_gate_is_the_unrounded_median_strictly_below_3_s(times, code, capsys, monkeypatch):
    monkeypatch.setattr(bench, "_time_runs", fixed(*times))
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    assert bench.main(["--input", str(INPUT)]) == code
    out = capsys.readouterr().out
    printed = MEDIAN.search(out).group(1)
    assert printed == f"{sorted(times)[1]:.2f}"
    assert [f"run {n}: {t:.2f} s" for n, t in enumerate(times, 1)] == [x for x in lines(out) if x.startswith("run ")]


def test_another_engine_version_is_refused(tmp_path, capsys):
    d = json.loads(INPUT.read_text(encoding="utf-8"))
    d["engine_version"] = "engine-2"
    old = tmp_path / "old.json"
    old.write_text(json.dumps(d), encoding="utf-8")
    assert bench.main(["--input", str(old)]) == 2
    err = capsys.readouterr().err
    assert "re-export with make bench-input" in err and "engine-2" in err


@pytest.mark.parametrize("content", [None, b"{not json", b"[1, 2]", b'{"engine_version": "engine-3"}', b"\xff\xfe"],
                         ids=["missing", "invalid-json", "not-an-object", "incomplete", "not-utf8"])
def test_a_missing_or_unusable_input_exits_2(content, tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(bench, "_time_runs", lambda inp, runs=bench.RUNS: called.append(inp) or [1.0] * runs)
    path = tmp_path / "bench_input.json"
    if content is not None:
        path.write_bytes(content)
    assert bench.main(["--input", str(path)]) == 2
    captured = capsys.readouterr()
    assert str(path) in captured.err
    assert "median" not in captured.out and called == []  # nothing was timed


def test_shortlist_info_never_changes_the_exit_code(capsys, monkeypatch):
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    seen = []

    def by_step(inp, runs=bench.RUNS):
        seen.append(inp.policy.shortlist_step)
        return [1.0] * runs if inp.policy.shortlist_step == 0 else [4.0, 5.0, 6.0]  # info runs over budget

    monkeypatch.setattr(bench, "_time_runs", by_step)
    assert bench.main(["--input", str(INPUT), "--shortlist-info"]) == 0
    out = lines(capsys.readouterr().out)
    assert seen == [0, 5, 10]  # the gate first, on the input as shipped
    gate = next(i for i, x in enumerate(out) if MEDIAN.match(x))
    assert out[gate + 1:] == ["info, not the gate: shortlist_step 5: median of 3 = 5.00 s",
                              "info, not the gate: shortlist_step 10: median of 3 = 5.00 s"]


def test_shortlist_info_does_not_rescue_a_failed_gate(capsys, monkeypatch):
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    monkeypatch.setattr(bench, "_time_runs",
                        lambda inp, runs=bench.RUNS: [3.5] * runs if inp.policy.shortlist_step == 0 else [0.1] * runs)
    assert bench.main(["--input", str(INPUT), "--shortlist-info"]) == 1
    assert "info, not the gate: shortlist_step 10: median of 3 = 0.10 s" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--input", "a.json", "--export", "b.json"], ["--export", "b.json", "--shortlist-info"]],
                         ids=["input-and-export", "export-with-info"])
def test_conflicting_options_are_refused(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        bench.main(argv)
    assert exc.value.code == 2
    assert not (tmp_path / "b.json").exists()


def test_the_cgroup_limits_read_unknown_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "CGROUP", tmp_path)
    assert " cpu.max=unknown memory.max=unknown " in bench.config_line()
    (tmp_path / "cpu.max").write_text("100000 100000\n")
    (tmp_path / "memory.max").write_text("536870912\n")
    monkeypatch.setenv("CLOUD_RUN_JOB", "bench-job")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "bench-job-x1")
    monkeypatch.delenv("K_REVISION", raising=False)
    line = bench.config_line()
    assert " cpu.max=100000/100000 memory.max=536870912 " in line
    assert line.endswith(" job=bench-job execution=bench-job-x1 revision=-")
