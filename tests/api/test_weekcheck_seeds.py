"""The five-seed weekcheck (08-16; RPT-05, 08-CONTEXT D-06).

One seed end to end on its own generated evaluation set (synthetic, with the labelled local stand-in forecast) and its
own three-world simulation: the four gates, the information columns and the win/tie/loss rule. The command's order,
bare form, exit rule and per-seed isolation are tested with `seed_week` replaced (no database). The public
benchmark's four-world mode is tested on the TEST ONLY eligible tiny public set, apart from the seeds.

Nothing here tunes or regenerates a seed to change a result: the assertions are about what is reported and how,
never about which scenario wins (benchmark-not-target).
"""
from __future__ import annotations

import json
import math
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from api import advance, benchmark, cases, db, refdata, sandbox, weekcheck
from tests.api.benchmark_fixtures import eligible_dv  # noqa: F401
from tests.api.test_checked_morning import drop, stale_designed_lot

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_JSON = ROOT / "reference" / "scenarios.json"
EVALUATION_JSON = ROOT / "api" / "evaluation.json"
SEED = 1  # one of the reference evaluation seeds (asserted below), the tracer's seed


def leaf(name: str) -> dict:
    return json.loads(EVALUATION_JSON.read_text())[name]


def to_cents(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.fixture(scope="module")
def seed_one(schema, tmp_path_factory):
    """`seed_week(1)` once, with every checked_morning call recorded (its keyword arguments, the run day and the
    stock decisions on the run at that moment); the seed's simulation is removed afterwards."""
    calls = []
    real = weekcheck.checked_morning

    def spy(conn, run, **kwargs):
        n = db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r", r=run["run_id"])["n"]
        calls.append({"kwargs": dict(kwargs), "day": run["day"], "cases": n})
        return real(conn, run, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(weekcheck, "checked_morning", spy)
        report = weekcheck.seed_week(SEED, tmp_path_factory.mktemp("seed-one"))
    yield report, calls
    drop({"run_id": report["run_id"]})


def test_the_seed_is_one_of_the_reference_seeds_in_reference_order():
    listed = json.loads(SCENARIOS_JSON.read_text())["evaluation"]["seeds"]["value"]
    assert weekcheck.evaluation_seeds() == listed and SEED in listed


def test_one_seed_reports_every_key_and_passes_all_four_gates(seed_one):
    report, _ = seed_one
    assert list(report) == list(weekcheck.SEED_KEYS)
    assert report["report_version"] == weekcheck.REPORT_VERSION and report["command"] == "seeds"
    assert report["seed"] == SEED and report["run_id"].startswith("wk-")
    assert report["gates"] == {g: True for g in weekcheck.GATES}, report["failures"]
    assert report["failures"] == [] and report["status"] == "PASSED"
    assert report["days"] == [0, 6]
    assert report["label"].startswith("Synthetic")  # generated data, labelled so
    assert report["forecast"]["provider"] == "local-seasonal-naive"
    assert "stand-in" in report["forecast"]["label"] and "development and tests only" in report["forecast"]["label"]
    assert report["dataset_version"] == refdata.get(report["dataset_version"]).dataset_version


def test_the_seed_runs_the_three_app_worlds_from_day_zero_and_never_starts_a_later_day(seed_one):
    report, calls = seed_one
    run_id = report["run_id"]
    with db.tx() as conn:
        sim = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        assert (sim["kind"], sim["status"], sim["day"]) == ("TEMPLATE", "INITIALIZING", 6)
        for table in ("lots", "truth_lots", "outcomes", "world_actions", "world_events", "inbound"):
            worlds = {r["world"] for r in db.rows(conn, f"SELECT DISTINCT world FROM {table} WHERE run_id = :r",
                                                  r=run_id)}
            assert worlds <= set(advance.WORLDS), (table, worlds)
        assert {(r["world"], r["day"]) for r in db.rows(conn, "SELECT DISTINCT world, day FROM outcomes "
                                                              "WHERE run_id = :r", r=run_id)} == \
            {(w, d) for w in advance.WORLDS for d in range(7)}
        assert not db.rows(conn, "SELECT 1 FROM benchmark_runs WHERE sim_run_id = :r", r=run_id)
        assert not db.rows(conn, "SELECT 1 FROM benchmark_observations WHERE benchmark_id = :r", r=run_id)
        assert not db.rows(conn, "SELECT seq FROM activity_log WHERE run_id = :r AND type = 'day_advanced' "
                                 "AND (payload->>'to_day')::int > 6", r=run_id)
        day0 = db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r AND opened_day = 0", r=run_id)["n"]
    # one checked morning a day, days 0..6, each naming no comparison world and no benchmark explicitly
    assert [c["day"] for c in calls] == list(range(7))
    assert all(c["kwargs"] == {"bench_id": None, "compare_world": None} for c in calls)
    # day 0's start and eager morning ran before the first checked morning: its cases already exist then
    assert day0 > 0 and calls[0]["cases"] == day0


def test_open_decisions_per_day_count_every_day_and_the_mean_includes_the_zero_days(seed_one):
    report, _ = seed_one
    per_day = report["open_per_day"]
    assert len(per_day) == 7 and all(isinstance(n, int) and n >= 0 for n in per_day)
    assert report["open_max"] == max(per_day) and report["open_mean"] == sum(per_day) / len(per_day)
    with db.tx() as conn:
        day0 = db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r AND opened_day = 0",
                      r=report["run_id"])["n"]
    assert per_day[0] == day0  # day 0's decisions are all open at its decision point
    assert cases.LIVE_STATES == ("OPEN", "WAITING", "PENDING_APPROVAL")  # what "open stock decisions" counts


@pytest.mark.parametrize("per_day,expected", [
    ([0, 2, 0, 1, 0, 0, 0], {"open_max": 2, "open_mean": 3 / 7}),
    ([0, 0, 0, 0, 0, 0, 0], {"open_max": 0, "open_mean": 0.0}),
    ([3], {"open_max": 3, "open_mean": 3.0}),
    ([], {"open_max": 0, "open_mean": 0.0}),
])
def test_a_day_with_no_open_decision_counts_zero_and_the_mean_includes_it(per_day, expected):
    assert weekcheck.open_summary(per_day) == expected


def test_the_waste_column_is_kg_wasted_times_the_evaluation_json_assumption_rounded_to_cents(seed_one):
    report, _ = seed_one
    price = leaf("waste_price_info")
    assert report["waste_info"] == {"price": price["value"], "unit": price["unit"], "type": "Assumption",
                                    "source": price["source"], "use": "information only, never a gate"}
    scenarios = ["store_rule", "shelfshift", "markdowns_only"]
    assert list(report["kg_wasted"]) == list(report["waste_info_usd"]) == list(report["nets"]) == scenarios
    for key in scenarios:
        expected = (Decimal(str(report["kg_wasted"][key])) * Decimal(str(price["value"]))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert Decimal(str(report["waste_info_usd"][key])) == expected
    # kg and net derived independently from the outcome rows and the set's products
    ref = refdata.get(report["dataset_version"])
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT world, branch, day, sku, wasted, revenue, transport, handling, disposal "
                             "FROM outcomes WHERE run_id = :r", r=report["run_id"])
    for world, key in (("baseline_rule", "store_rule"), ("shelfshift", "shelfshift"), ("do_nothing", "markdowns_only")):
        mine = [r for r in rows if r["world"] == world]
        kg = sum(float(r["wasted"]) * ref.products[r["sku"]].kg_per_unit for r in mine)
        assert math.isclose(report["kg_wasted"][key], kg, abs_tol=1e-6)
        net = sum(float(r["revenue"]) - float(r["transport"]) - float(r["handling"]) - float(r["disposal"]) for r in mine)
        # at most five half-cent roundings per branch-day row (revenue, three costs, net): metrics.branch_day_rows
        assert abs(report["nets"][key] - net) <= 0.005 * 5 * 7 * len(ref.branches) + 1e-9


def test_the_result_is_the_rule_applied_to_the_reported_nets(seed_one):
    report, _ = seed_one
    threshold = leaf("tie_threshold")["value"]
    assert (report["result"], report["delta"]) == weekcheck.seed_result(
        report["nets"]["shelfshift"], report["nets"]["store_rule"], threshold)
    assert report["result"] in ("win", "tie", "loss")


@pytest.mark.parametrize("shelfshift,store_rule,result,delta", [
    (5.00, 5.00, "tie", 0.0),                    # delta 0
    (5.004, 4.995, "tie", 0.0),                  # a raw delta of 0.009 inside one cent: both are 5.00
    (5.009, 5.000, "win", 0.01),                 # a raw delta of 0.009 across a cent: 5.01 against 5.00
    (5.01, 5.00, "win", 0.01),                   # exactly the threshold: a win
    (5.00, 5.01, "loss", -0.01),                 # exactly the threshold below: a loss
    (0.1 + 0.2, 0.3, "tie", 0.0),                # 0.30000000000000004 is 0.30
    (1.13, 1.12, "win", 0.01),                   # float subtraction would give 0.0099999...: still a cent
    (0.3, 0.29, "win", 0.01),                    # float subtraction would give 0.0100000...09
    (1.005, 1.00, "win", 0.01),                  # binary 1.00499999... is the decimal 1.005, half up 1.01
    (1.00499, 1.00, "tie", 0.0),                 # just below the half cent
    (-0.0, 0.0, "tie", 0.0),
    (0.0, -0.0, "tie", 0.0),
    (-5.00, -4.99, "loss", -0.01),               # negative results
    (-4.99, -5.00, "win", 0.01),
    (-5.004, -4.996, "tie", 0.0),                # both -5.00
    (-1.005, -1.00, "loss", -0.01),              # half away from zero: -1.01
])
def test_win_tie_loss_converts_each_net_to_cents_once_before_subtracting(shelfshift, store_rule, result, delta):
    threshold = leaf("tie_threshold")["value"]
    got, change = weekcheck.seed_result(shelfshift, store_rule, threshold)
    assert (got, change) == (result, delta)
    assert math.copysign(1.0, change) == 1.0 or change < 0  # never a negative zero


def test_the_tie_threshold_and_waste_price_are_read_from_evaluation_json():
    values = benchmark.load_settings()
    assert values["tie_threshold"] == leaf("tie_threshold")["value"]
    assert values["waste_price_info"] == leaf("waste_price_info")["value"]
    assert leaf("tie_threshold")["unit"] == "USD" and leaf("waste_price_info")["type"] == "Assumption"
    source = (ROOT / "api" / "weekcheck.py").read_text()
    for literal in (str(leaf("waste_price_info")["value"]), str(weekcheck.evaluation_seeds()[0])):
        assert literal not in source, literal  # values come from data, never code constants


# ---- the seeds mode of the checked morning: durable history with no benchmark ---------------------------------------

@pytest.fixture
def seeds_mode_run(eligible_dv):
    """A three-world TEMPLATE simulation of the tiny public set initialised as `seed_week` initialises its own (no
    benchmark row), with the designed lot's record above its physical stock and last counted long ago."""
    run_id = "wk-" + uuid.uuid4().hex[:12]
    ref, truth = refdata.get(eligible_dv), refdata.truth(eligible_dv)
    with db.tx() as conn:
        run = db.one(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) "
                           "VALUES (:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1) RETURNING *", r=run_id, v=eligible_dv)
        sandbox.init_worlds(conn, run_id, ref, truth, worlds=weekcheck.SEED_WORLDS)
        stale_designed_lot(conn, run_id)
        advance.day_start(conn, run, worlds=weekcheck.SEED_WORLDS)
        advance.cases_morning(conn, run, eager=True)
    yield run
    drop(run)


def test_seeds_mode_counts_once_with_a_durable_history_and_no_benchmark_rows(seeds_mode_run):
    run_id = seeds_mode_run["run_id"]
    with db.tx() as conn:
        run = db.lock_run(conn, run_id, mode="UPDATE", statuses=("INITIALIZING",))
        first = weekcheck.checked_morning(conn, run, bench_id=None, compare_world=None)
    assert first["asked"] >= 1 and first["confirmed"] >= 1 and first["lots_counted"] >= 1
    with db.tx() as conn:
        history = db.rows(conn, "SELECT world, event_kind, benchmark_id FROM evaluation_check_history "
                                "WHERE sim_run_id = :r ORDER BY world, seq", r=run_id)
        assert history and all(h["benchmark_id"] is None for h in history)
        assert {h["world"] for h in history} == {"shelfshift"}  # no comparison world in seeds mode
        assert sum(h["event_kind"] == "request" for h in history) == first["asked"]
        assert sum(h["event_kind"] == "observation" for h in history) == first["lots_counted"]
        assert not db.rows(conn, "SELECT 1 FROM benchmark_observations WHERE benchmark_id = :r", r=run_id)
        assert not db.rows(conn, "SELECT 1 FROM lots WHERE run_id = :r AND world = 'baseline_checked'", r=run_id)
    # a re-entered morning in a new transaction reads the committed history: nothing is asked or counted again
    with db.tx() as conn:
        run = db.lock_run(conn, run_id, mode="UPDATE", statuses=("INITIALIZING",))
        again = weekcheck.checked_morning(conn, run, bench_id=None, compare_world=None)
    assert again["asked"] == 0 and again["lots_counted"] == 0


# ---- the public benchmark's four-world mode, apart from the seeds ---------------------------------------------------

def test_the_public_benchmark_names_its_comparison_world_explicitly(eligible_dv, monkeypatch):
    with db.tx() as conn:
        run = benchmark.create_run(conn, eligible_dv, trial=False, worlds=weekcheck.BENCH_WORLDS,
                                   image_digest="test-only", git_commit="test-only")
    calls = []
    real = weekcheck.checked_morning

    def spy(conn, sim, **kwargs):
        calls.append(dict(kwargs))
        return real(conn, sim, **kwargs)

    monkeypatch.setattr(weekcheck, "checked_morning", spy)
    try:
        weekcheck.run_days(run["run_id"], through=0, image_digest="test-only", git_commit="test-only")
        assert calls == [{"bench_id": run["run_id"], "compare_world": "baseline_checked"}]
        with db.tx() as conn:
            assert {r["world"] for r in db.rows(conn, "SELECT DISTINCT world FROM lots WHERE run_id = :r",
                                                r=run["run_id"])} == set(weekcheck.BENCH_WORLDS)
    finally:
        drop(run)


# ---- the command: order, bare form, exit rule, isolation (seed_week replaced; no database) -------------------------

def fake_report(seed, *, result="loss", gates_ok=True):
    gates = {g: gates_ok for g in weekcheck.GATES}
    return weekcheck._seed_report(seed=seed, status="PASSED" if gates_ok else "FAILED", gates=gates,
                                  failures=[] if gates_ok else ["no_limit_broken: test-only failure"],
                                  result=result, elapsed_s=0.0)


@pytest.fixture
def command(monkeypatch, capsys):
    """`command(argv, behave)` runs main(argv) with seed_week replaced by `behave(seed)`; returns (exit code, the
    seeds asked in order, the stdout lines parsed)."""
    def call(argv, behave=lambda seed: fake_report(seed)):
        asked = []

        def fake(seed, workdir):
            asked.append(seed)
            assert Path(workdir).is_dir()  # each seed gets its own working directory
            return behave(seed)
        monkeypatch.setattr(weekcheck, "seed_week", fake)
        capsys.readouterr()
        code = weekcheck.main(list(argv))
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        return code, asked, lines
    return call


def without_time(lines):
    return [{k: v for k, v in line.items() if k != "elapsed_s"} for line in lines]


def test_bare_and_seeds_run_the_same_five_seeds_in_reference_order_and_losses_never_fail(command):
    reference = weekcheck.evaluation_seeds()
    bare = command([])
    named = command(["seeds"])
    for code, asked, lines in (bare, named):
        assert code == 0  # every seed a loss, every gate passed: the exit code follows the gates only
        assert asked == reference and [line["seed"] for line in lines[:-1]] == reference
        summary = lines[-1]
        assert summary["seeds"] == reference and summary["gates_failed"] == 0 and summary["status"] == "PASSED"
        assert summary["results"] == {"win": 0, "tie": 0, "loss": len(reference)}
    assert without_time(bare[2]) == without_time(named[2])


def test_a_failed_gate_and_an_error_are_reported_for_their_seeds_while_the_others_still_run(command):
    reference = weekcheck.evaluation_seeds()
    failing, broken = reference[1], reference[2]

    def behave(seed):
        if seed == broken:
            raise RuntimeError("test-only stop")
        return fake_report(seed, result="win", gates_ok=seed != failing)
    code, asked, lines = command(["seeds"], behave)
    assert code == 1
    assert asked == reference and [line["seed"] for line in lines[:-1]] == reference
    by_seed = {line["seed"]: line for line in lines[:-1]}
    assert by_seed[failing]["status"] == "FAILED" and by_seed[failing]["gates"]["no_limit_broken"] is False
    assert by_seed[broken]["status"] == "ERROR" and by_seed[broken]["gates"] == {g: None for g in weekcheck.GATES}
    assert by_seed[broken]["failures"] == ["stopped: RuntimeError: test-only stop"]
    assert all(by_seed[s]["status"] == "PASSED" for s in reference if s not in (failing, broken))
    summary = lines[-1]
    assert summary["gates_failed"] == 2 and summary["failed_seeds"] == [failing, broken] and summary["errors"] == 1
    assert summary["status"] == "FAILED"


def test_a_seed_selection_keeps_reference_order(command):
    reference = weekcheck.evaluation_seeds()
    picked = [reference[3], reference[1]]
    code, asked, lines = command(["seeds", "--seeds", ",".join(map(str, picked))])
    assert code == 0 and asked == [reference[1], reference[3]] == lines[-1]["seeds"]


@pytest.mark.parametrize("selection", ["999999", "x", ","])
def test_an_unknown_or_malformed_selection_is_refused_before_any_seed_runs(command, selection):
    code, asked, lines = command(["seeds", "--seeds", selection])
    assert code == 2 and asked == [] and len(lines) == 1 and lines[0]["status"] == "REFUSED"
    assert lines[0]["reasons"][0].startswith("--seeds")


def test_the_benchmark_subcommands_keep_their_literal_names_and_flags():
    import ast
    tree = ast.parse((ROOT / "api" / "weekcheck.py").read_text())
    parsers = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
               and getattr(node.func, "attr", None) == "add_parser"}
    flags = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "attr", None) == "add_argument" and isinstance(node.args[0], ast.Constant)}
    assert {"benchmark", "start", "resume", "seeds"} <= parsers
    assert {"--dataset", "--trial", "--minutes", "--stores", "--seeds"} <= flags
