"""Forecast runs with a provider on every point (DATA-03; 03-CONTEXT D-05, D-08, D-10, D-19).

These tests never assume the committed forecast.json is the local stand-in: it is replaced by BigQuery's before
release. Stand-in artifacts are built in memory from the committed seed; the committed file is checked only for
what holds for any release.
"""
import copy
import json
import pathlib
import random
from collections import Counter

import pytest

from data import forecast as F
from data import preprocess, versions
from data import reference as R

REF = pathlib.Path(F.__file__).resolve().parent / "reference"
ALLOWED = {F.AI_FORECAST, F.ARIMA_PLUS, F.LOCAL}


@pytest.fixture(scope="module")
def seed():
    return json.loads((REF / "seed.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def local(seed):
    return F.build(seed, F.LOCAL_PROVIDER)


def recount(fc):
    """Series counts by provider per run, recomputed from the points alone."""
    series = {(p["forecast_run_id"], p["branch"], p["sku"], p["provider"]) for p in fc["points"]}
    out = {}
    for run_id, _, _, provider in series:
        out.setdefault(run_id, Counter())[provider] += 1
    return out


def assert_counts_recompute(fc):
    assert all(p["provider"] in ALLOWED for p in fc["points"])
    by_run = recount(fc)
    for r in fc["runs"]:
        assert r["providers"] == dict(by_run[r["forecast_run_id"]])
    backtest = {r["forecast_run_id"] for r in fc["runs"] if r["job"] == "backtest"}
    demo, back = Counter(), Counter()
    for run_id, counts in by_run.items():
        (back if run_id in backtest else demo).update(counts)
    s = fc["provider_summary"]
    assert s["demo"]["by_provider"] == dict(demo) and s["demo"]["series_runs"] == sum(demo.values())
    assert s["backtest"]["by_provider"] == dict(back) and s["backtest"]["series_runs"] == sum(back.values())
    assert s == F.provider_summary(fc["runs"], fc["points"])


def test_each_demo_run_uses_data_up_to_the_day_before_cutoff(seed, local):
    runs = {r["job"]: r for r in local["runs"]}
    days = {}
    for p in local["points"]:
        days.setdefault(p["forecast_run_id"], set()).add(p["day"])
    for n in range(7):
        r = runs[f"d{n}"]
        assert (r["demo_day"], r["cutoff_day"], r["days"]) == (n, n - 1, [n, 16])
        assert days[r["forecast_run_id"]] == set(range(n, 17))
    bt = runs["backtest"]
    assert (bt["demo_day"], bt["cutoff_day"], bt["days"]) == (None, -15, [-14, -1])
    assert days[bt["forecast_run_id"]] == set(range(-14, 0))
    for sid, series in F.inputs(seed).items():
        job = sid.split("|")[0]
        assert max(d for d, _, _ in series) == runs[job]["cutoff_day"]


def test_the_artifact_has_8_runs_and_1680_points(local):
    assert len(local["runs"]) == 8
    assert len(local["points"]) == 1680  # 15 series x (17 + 16 + ... + 11 demo days + 14 backtest days)
    keys = [(p["forecast_run_id"], p["branch"], p["sku"], p["day"]) for p in local["points"]]
    assert len(set(keys)) == len(keys)


def test_inputs_use_the_seeds_assumed_response_base(seed):
    md = seed["markdown_assumption"]
    rows = [r for r in seed["daily_sales"] if (r["branch"], r["sku"]) == ("Bang Na", "BREAD-400")]
    got = F.inputs(seed)["d3|Bang Na|BREAD-400"]
    assert got == preprocess.forecast_input(rows, 2, md["m20"][1], md["m30"][1])
    changed = copy.deepcopy(seed)
    changed["markdown_assumption"]["m30"][1] = 2.0
    assert F.inputs(changed)["d3|Bang Na|BREAD-400"] != got  # bread sells at 30% under schedule B


def test_every_point_has_an_allowed_provider_and_counts_recompute_from_points(seed, local):
    assert_counts_recompute(local)
    # a mixed artifact: every Bang Na series from ARIMA_PLUS after a failed interval check, the rest TimesFM
    rows = F.local_fetch(F.inputs(seed), F.required(seed))
    chosen = {sid: (F.ARIMA_PLUS if "|Bang Na|" in sid else F.AI_FORECAST, rows[sid]) for sid in rows}
    fallbacks = {sid: {"failed_check": "bounds", "detail": "low > base on day 3"}
                 for sid in rows if "|Bang Na|" in sid}
    mixed = F.assemble(seed, chosen, fallbacks)
    assert_counts_recompute(mixed)
    d3 = next(r for r in mixed["runs"] if r["job"] == "d3")
    assert d3["providers"] == {F.AI_FORECAST: 12, F.ARIMA_PLUS: 3}
    assert [(f["branch"], f["sku"], f["failed_check"]) for f in d3["fallbacks"]] == [
        ("Bang Na", "BREAD-400", "bounds"), ("Bang Na", "MILK-1L", "bounds"), ("Bang Na", "STRAW-500", "bounds")]
    s = mixed["provider_summary"]
    assert s["demo"]["fallback_checks"] == {"bounds": 21}
    assert s["headline"] == ("TimesFM 2.5 via BigQuery AI.FORECAST for 84 of 105 series-runs; "
                             "ARIMA_PLUS fallback for 21 (failed interval checks)")


def test_there_is_no_top_level_provider_key(local):
    assert "provider" not in local
    assert set(local) == {"label", "input_label", "confidence_level", "input_version", "basis_id", "runs",
                          "points", "provider_summary"}
    assert local["label"] == "Precomputed" and local["input_label"] == "estimated full-price-equivalent sales"
    assert all("provider" not in r for r in local["runs"])


def test_negative_values_are_clamped_after_and_counted(seed):
    target = "d2|Rangsit|STRAW-500"

    def fetch(ins, req):
        out = F.local_fetch(ins, req)
        out[target] = [dict(r, low=-1.0, base=2.0, high=5.0) if r["day"] == 4 else r for r in out[target]]
        return out

    fc = F.build(seed, F.Provider(F.LOCAL, fetch))
    run = next(r for r in fc["runs"] if r["job"] == "d2")
    p = next(p for p in fc["points"] if p["forecast_run_id"] == run["forecast_run_id"]
             and (p["branch"], p["sku"], p["day"]) == ("Rangsit", "STRAW-500", 4))
    assert (p["low"], p["base"], p["high"], p["clamped"]) == (0.0, 2.0, 5.0, True)
    assert p["raw"] == {"low": -1.0, "base": 2.0, "high": 5.0}
    assert run["clamped_points"] == 1
    assert sum(r["clamped_points"] for r in fc["runs"]) == 1
    others = [q for q in fc["points"] if q is not p]
    assert not any(q["clamped"] or "raw" in q for q in others)


def test_points_are_sorted_and_row_order_does_not_matter(seed, local):
    def shuffled(ins, req):
        out = F.local_fetch(ins, req)
        rng = random.Random(7)
        mixed = {}
        for sid in sorted(out, reverse=True):
            rows = list(out[sid])
            rng.shuffle(rows)
            mixed[sid] = rows
        return mixed

    other = F.build(seed, F.Provider(F.LOCAL, shuffled))
    assert F.dumps(other) == F.dumps(local)
    keys = [(p["forecast_run_id"], p["branch"], p["sku"], p["day"]) for p in local["points"]]
    assert keys == sorted(keys)


def test_a_provider_missing_a_required_day_is_refused(seed):
    def short(ins, req):
        out = F.local_fetch(ins, req)
        out["d0|Thonburi|MILK-1L"] = out["d0|Thonburi|MILK-1L"][:-1]
        return out

    with pytest.raises(ValueError, match=r"d0\|Thonburi\|MILK-1L .* no row for day 16"):
        F.build(seed, F.Provider(F.LOCAL, short))


def test_rows_outside_the_required_days_are_ignored(seed, local):
    def extra(ins, req):
        out = F.local_fetch(ins, req)
        out["d6|Bang Kapi|BREAD-400"].append({"day": 17, "low": -5.0, "base": -1.0, "high": 3.0,
                                              "confidence_level": 0.8, "status": ""})
        return out

    assert F.dumps(F.build(seed, F.Provider(F.LOCAL, extra))) == F.dumps(local)


def test_the_committed_forecast_belongs_to_the_committed_seed(seed):
    fc = json.loads((REF / "forecast.json").read_text(encoding="utf-8"))
    assert fc["basis_id"] == versions.basis_id(seed)
    # The seed may move outside the forecast basis after the BigQuery run (a run or capacity change, D-15's knobs;
    # the second-look fix of 2026-10-15 added R-02 on day 4). The forecast then keeps the input_version it was built
    # on, and the release gate accepts the set with its note; anything else must still be the same seed.
    info = R.release_check(R.load(REF))
    assert info["forecast_input_version"] == fc["input_version"]
    assert info["note"] == ("" if fc["input_version"] == seed["dataset"]["input_version"] else R.MOVED_NOTE)
    assert all(r["reference_history_id"] == fc["basis_id"] for r in fc["runs"])
    assert len(fc["runs"]) == 8 and len(fc["points"]) == 1680
    assert "provider" not in fc
    assert_counts_recompute(fc)


def test_the_local_stand_in_is_named_in_the_headline(local):
    headline = local["provider_summary"]["headline"]
    assert headline == f"{F.PROVIDERS[F.LOCAL]} for 105 of 105 series-runs"
    assert "TimesFM" not in headline and "BigQuery" not in headline


def test_main_writes_the_artifact_and_prints_its_counts(seed, local, tmp_path, capsys):
    truth = json.loads((REF / "truth.json").read_text(encoding="utf-8"))
    (tmp_path / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    (tmp_path / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    assert F.main(["--provider", "local", "--ref", str(tmp_path)]) == 0
    expected = dict(local, quality=F.score(truth, local, seed))
    assert (tmp_path / "forecast.json").read_text(encoding="utf-8") == F.dumps(expected)
    line = capsys.readouterr().out
    assert "8 runs, 1680 points; local seasonal-naive stand-in" in line and "; clamped 0; WAPE " in line
