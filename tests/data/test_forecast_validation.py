"""Bad provider output never reaches the engine (DATA-03, DATA-05; 03-CONTEXT D-07, D-08, D-09, D-19).

Every raw provider row is checked before anything else; a failing series falls back alone; a failing fallback
stops the command and keeps the previous forecast.json. Providers here are fakes built from recorded row dicts;
nothing calls BigQuery.
"""
import copy
import json
import math
import pathlib

import pytest

from data import forecast as F

REF = pathlib.Path(F.__file__).resolve().parent / "reference"

# The AI.FORECAST day-1 check (ROADMAP Phase 3, user, 2026-10-13): 4 of 7 rows had an inconsistent interval.
# (low, base, high) values copied from ROADMAP. The ROADMAP gives only the offending pair per row, so the member it
# does not give is filled with a value that keeps the stated violation:
FEB3 = (36.50, 35.90, 35.29)  # lower 36.50 above upper 35.29; base 35.90 filled (between them)
FEB4 = (37.65, 36.90, 36.20)  # lower 37.65 above upper 36.20; base 36.90 filled (between them)
FEB5 = (34.00, 36.50, 34.50)  # value 36.50 above upper 34.50; low 34.00 filled (below upper, so only base is out)
FEB6 = (33.00, 37.29, 33.89)  # value 37.29 above upper 33.89; low 33.00 filled (below upper, so only base is out)
DAY1_BAD = (FEB3, FEB4, FEB5, FEB6)
DAY1_LEVEL = 0.95  # the test query left confidence_level at its 0.95 default

TARGET = "d3|Bang Na|MILK-1L"


def row(day, low, base, high, level=0.8, status=""):
    return {"day": day, "low": low, "base": base, "high": high, "confidence_level": level, "status": status}


def checks(problems):
    return [p["check"] for p in problems]


def fake(name, rows_by_series):
    """A provider that returns copies of recorded rows for the series it is asked for, and records each call's
    series ids. A series missing from `rows_by_series` is missing from its answer."""
    calls = []

    def fetch(ins, req):
        assert set(ins) == set(req)
        calls.append(sorted(req))
        return {sid: copy.deepcopy(rows_by_series[sid]) for sid in req if sid in rows_by_series}

    return F.Provider(name, fetch), calls


def replace_day(rows, day, triple, **extra):
    return [dict(r, low=triple[0], base=triple[1], high=triple[2], **extra) if r["day"] == day else r
            for r in rows]


@pytest.fixture(scope="module")
def seed():
    return json.loads((REF / "seed.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(seed):
    """Valid raw rows for every series, from the local stand-in's arithmetic (unclamped, unrounded)."""
    return F.local_fetch(F.inputs(seed), F.required(seed))


def points_of(fc, job, branch=None, sku=None):
    run_id = next(r["forecast_run_id"] for r in fc["runs"] if r["job"] == job)
    return [p for p in fc["points"] if p["forecast_run_id"] == run_id
            and (branch is None or p["branch"] == branch) and (sku is None or p["sku"] == sku)]


# --- series_problems (D-07) ---

def test_the_day1_crossed_and_outside_rows_fail_bounds():
    for triple, why in zip(DAY1_BAD, ("lower above upper", "lower above upper",
                                      "value above upper", "value above upper")):
        problems = F.series_problems([row(1, *triple)], [1], 0.8)
        assert checks(problems) == ["bounds"], triple
        assert problems[0]["day"] == 1
        assert problems[0]["detail"] == f"low {triple[0]!r}, base {triple[1]!r}, high {triple[2]!r}: {why}"
    # the four rows as recorded, at the 0.95 default: each fails bounds and confidence
    recorded = [row(d, *t, level=DAY1_LEVEL) for d, t in enumerate(DAY1_BAD, start=3)]
    problems = F.series_problems(recorded, [3, 4, 5, 6], 0.8)
    assert [(p["day"], p["check"]) for p in problems] == [
        (d, c) for d in (3, 4, 5, 6) for c in ("bounds", "confidence")]
    assert problems[1]["detail"] == "confidence_level 0.95, expected 0.8"


def test_status_confidence_coverage_and_duplicates_fail():
    ok = row(1, 1.0, 2.0, 3.0)
    assert F.series_problems([ok], [1], 0.8) == []
    short = F.series_problems([row(1, 1.0, 2.0, 3.0, status="The time series data is too short")], [1], 0.8)
    assert short == [{"check": "status", "day": 1, "detail": "The time series data is too short"}]
    assert checks(F.series_problems([row(1, 1.0, 2.0, 3.0, level=0.95)], [1], 0.8)) == ["confidence"]
    missing = F.series_problems([ok], [1, 2], 0.8)
    assert missing == [{"check": "coverage", "day": 2, "detail": "missing: no row for day 2"}]
    dup = F.series_problems([ok, dict(ok)], [1], 0.8)
    assert dup == [{"check": "coverage", "day": 1, "detail": "duplicate: 2 rows for day 1"}]
    for bad in (math.nan, None, math.inf, -math.inf, "2.0", True):
        problems = F.series_problems([row(1, 1.0, bad, 3.0)], [1], 0.8)
        assert checks(problems) == ["bounds"], bad
        assert problems[0]["detail"].endswith("not a finite number")
    # a row for a day that is not required is ignored, whatever it holds
    assert F.series_problems([ok, row(9, 5.0, 1.0, 0.0, level=0.5, status="x")], [1], 0.8) == []
    # problems come ordered by day, then by check
    mixed = F.series_problems([row(2, *FEB3, level=0.95, status="bad"), row(1, 1.0, 2.0, 3.0)], [1, 2, 3], 0.8)
    assert [(p["day"], p["check"]) for p in mixed] == [(2, "status"), (2, "bounds"), (2, "confidence"),
                                                       (3, "coverage")]


def test_equal_bounds_pass():
    assert F.series_problems([row(1, 5.0, 5.0, 5.0)], [1], 0.8) == []
    assert F.series_problems([row(1, 5, 5, 5)], [1], 0.8) == []
    # exact comparison, no tolerance: a base a hair above high fails
    assert checks(F.series_problems([row(1, 5.0, 5.0 + 1e-12, 5.0)], [1], 0.8)) == ["bounds"]


def test_a_negative_alone_passes_validation(seed, rows):
    assert F.series_problems([row(1, -1.0, 2.0, 5.0)], [1], 0.8) == []
    assert F.series_problems([row(1, -5.0, -2.0, -1.0)], [1], 0.8) == []
    primary, _ = fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 5, (-1.0, 2.0, 5.0))})
    fallback, calls = fake(F.ARIMA_PLUS, rows)
    fc = F.build(seed, primary, fallback)
    assert calls == []
    p = next(p for p in points_of(fc, "d3", "Bang Na", "MILK-1L") if p["day"] == 5)
    assert (p["provider"], p["low"], p["clamped"]) == (F.AI_FORECAST, 0.0, True)


# --- build: per-series fallback (D-08, D-09) ---

def test_only_failing_series_fall_back(seed, rows):
    primary, first_calls = fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 3, FEB3)})
    fallback, calls = fake(F.ARIMA_PLUS, rows)
    fc = F.build(seed, primary, fallback)
    assert len(first_calls) == 1 and first_calls[0] == sorted(F.required(seed))
    assert calls == [[TARGET]]  # the fallback receives only the failing series
    fell = points_of(fc, "d3", "Bang Na", "MILK-1L")
    assert len(fell) == 14 and {p["provider"] for p in fell} == {F.ARIMA_PLUS}
    others = [p for p in fc["points"] if p not in fell]
    assert {p["provider"] for p in others} == {F.AI_FORECAST}
    # the stored day-3 point is the fallback's interval, never the crossed triple sorted
    p3 = next(p for p in fell if p["day"] == 3)
    r3 = next(r for r in rows[TARGET] if r["day"] == 3)
    assert (p3["low"], p3["base"], p3["high"]) == tuple(round(max(0.0, r3[k]), 2) for k in ("low", "base", "high"))
    runs = {r["job"]: r for r in fc["runs"]}
    assert runs["d3"]["fallbacks"] == [{"branch": "Bang Na", "sku": "MILK-1L", "failed_check": "bounds",
                                        "detail": "low 36.5, base 35.9, high 35.29: lower above upper"}]
    assert runs["d3"]["providers"] == {F.AI_FORECAST: 14, F.ARIMA_PLUS: 1}
    assert all(r["fallbacks"] == [] and r["providers"] == {F.AI_FORECAST: 15} for j, r in runs.items() if j != "d3")
    s = fc["provider_summary"]
    assert s["demo"]["fallback_checks"] == {"bounds": 1}
    assert s["headline"] == ("TimesFM 2.5 via BigQuery AI.FORECAST for 104 of 105 series-runs; "
                             "ARIMA_PLUS fallback for 1 (failed interval checks)")


def test_no_failing_series_never_calls_the_fallback(seed, rows):
    primary, _ = fake(F.AI_FORECAST, rows)
    fallback, calls = fake(F.ARIMA_PLUS, rows)
    fc = F.build(seed, primary, fallback)
    assert calls == []
    assert {p["provider"] for p in fc["points"]} == {F.AI_FORECAST}
    assert all(r["fallbacks"] == [] for r in fc["runs"])


def test_an_empty_series_fails_coverage_and_falls_back(seed, rows):
    gone = "backtest|Rangsit|STRAW-500"
    recorded = {**rows, TARGET: []}
    del recorded[gone]  # one series answered with no rows, one not answered at all
    primary, _ = fake(F.AI_FORECAST, recorded)
    fallback, calls = fake(F.ARIMA_PLUS, rows)
    fc = F.build(seed, primary, fallback)
    assert calls == [sorted([TARGET, gone])]
    runs = {r["job"]: r for r in fc["runs"]}
    assert runs["d3"]["fallbacks"] == [{"branch": "Bang Na", "sku": "MILK-1L", "failed_check": "coverage",
                                        "detail": "missing: no row for day 3"}]
    assert runs["backtest"]["fallbacks"] == [{"branch": "Rangsit", "sku": "STRAW-500", "failed_check": "coverage",
                                              "detail": "missing: no row for day -14"}]
    assert {p["provider"] for p in points_of(fc, "backtest", "Rangsit", "STRAW-500")} == {F.ARIMA_PLUS}
    # with no fallback, the empty series stops the build
    with pytest.raises(F.ForecastFailed) as e:
        F.build(seed, primary)
    assert {p["series_id"] for p in e.value.problems} == {TARGET, gone}
    assert {p["check"] for p in e.value.problems} == {"coverage"}


def test_a_failing_fallback_raises_with_both_providers(seed, rows):
    primary, _ = fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 4, FEB4)})
    fallback, _ = fake(F.ARIMA_PLUS, {**rows, TARGET: replace_day(rows[TARGET], 6, FEB5, confidence_level=0.95)})
    with pytest.raises(F.ForecastFailed) as e:
        F.build(seed, primary, fallback)
    assert isinstance(e.value, ValueError)
    assert [(p["provider"], p["check"], p["day"]) for p in e.value.problems] == [
        (F.AI_FORECAST, "bounds", 4), (F.ARIMA_PLUS, "bounds", 6), (F.ARIMA_PLUS, "confidence", 6)]
    assert all((p["series_id"], p["job"], p["branch"], p["sku"]) == (TARGET, "d3", "Bang Na", "MILK-1L")
               for p in e.value.problems)
    assert e.value.problems[0]["detail"] == "low 37.65, base 36.9, high 36.2: lower above upper"
    assert e.value.lines() == [
        f"{TARGET} from ai_forecast: bounds on day 4: low 37.65, base 36.9, high 36.2: lower above upper; "
        f"then arima_plus: bounds on day 6: low 34.0, base 36.5, high 34.5: value above upper (+1 more)"]
    assert TARGET in str(e.value) and "ai_forecast" in str(e.value) and "arima_plus" in str(e.value)


def test_the_cli_keeps_the_old_file_and_writes_diagnostics(tmp_path, monkeypatch, capsys):
    for name in ("seed.json", "truth.json"):
        (tmp_path / name).write_bytes((REF / name).read_bytes())
    old = b'{"a known previous forecast": true}\n'
    (tmp_path / "forecast.json").write_bytes(old)
    bad_series = "d1|Thonburi|BREAD-400"

    def crossed(ins, req):
        out = F.local_fetch(ins, req)
        out[bad_series] = replace_day(out[bad_series], 2, FEB4)
        return out

    monkeypatch.setattr(F, "LOCAL_PROVIDER", F.Provider(F.LOCAL, crossed))
    with pytest.raises(SystemExit) as e:
        F.main(["--provider", "local", "--ref", str(tmp_path)])
    assert e.value.code == 1
    assert (tmp_path / "forecast.json").read_bytes() == old
    diag = json.loads((tmp_path / "reports" / "forecast-diagnostics.json").read_text(encoding="utf-8"))
    assert diag["provider"] == F.LOCAL and diag["fallback"] is None and diag["next_step"] == F.NEXT_STEP
    assert diag["problems"] == [{"series_id": bad_series, "job": "d1", "branch": "Thonburi", "sku": "BREAD-400",
                                 "provider": F.LOCAL, "check": "bounds", "day": 2,
                                 "detail": "low 37.65, base 36.9, high 36.2: lower above upper"}]
    err = capsys.readouterr().err
    assert f"FAILED {bad_series} from {F.LOCAL}: bounds on day 2" in err
    assert sorted(p.name for p in tmp_path.rglob("*")) == [
        "forecast-diagnostics.json", "forecast.json", "reports", "seed.json", "truth.json"]  # no temp files left


# --- check_candidate: the whole artifact before the atomic replace (DATA-05, D-09) ---

@pytest.fixture(scope="module")
def candidate(seed):
    return F.build(seed, F.LOCAL_PROVIDER)


def refused(fc, seed, match):
    with pytest.raises(F.CoverageError, match=match):
        F.check_candidate(fc, seed)


def test_a_valid_candidate_passes(seed, candidate, rows):
    F.check_candidate(candidate, seed)
    primary, _ = fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 3, FEB3)})
    mixed = F.build(seed, primary, fake(F.ARIMA_PLUS, rows)[0])
    F.check_candidate(mixed, seed)
    clamped = F.build(seed, fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 5, (-1.0, 2.0, 5.0))})[0])
    F.check_candidate(clamped, seed)


def test_a_missing_point_names_its_series_and_day(seed, candidate):
    fc = copy.deepcopy(candidate)
    run_id = next(r["forecast_run_id"] for r in fc["runs"] if r["job"] == "d4")
    gone = next(p for p in fc["points"] if p["forecast_run_id"] == run_id
                and (p["branch"], p["sku"], p["day"]) == ("Lat Phrao", "STRAW-500", 9))
    fc["points"].remove(gone)
    refused(fc, seed, rf"^forecast run {run_id} has no STRAW-500 at Lat Phrao for day 9$")
    # a duplicated point fails too
    fc = copy.deepcopy(candidate)
    fc["points"].append(dict(fc["points"][0]))
    refused(fc, seed, r"has 2 points for")


def test_a_missing_demo_run_is_named(seed, candidate):
    fc = copy.deepcopy(candidate)
    d4 = next(r for r in fc["runs"] if r["job"] == "d4")
    fc["runs"].remove(d4)
    fc["points"] = [p for p in fc["points"] if p["forecast_run_id"] != d4["forecast_run_id"]]
    refused(fc, seed, r"^no forecast run for demo day 4$")
    fc = copy.deepcopy(candidate)
    fc["runs"] = [r for r in fc["runs"] if r["job"] != "backtest"]
    refused(fc, seed, r"^no forecast run for the backtest$")


def test_counts_that_do_not_recompute_fail(seed, candidate):
    fc = copy.deepcopy(candidate)
    d2 = next(r for r in fc["runs"] if r["job"] == "d2")
    d2["providers"] = {F.AI_FORECAST: 15}
    refused(fc, seed, rf"forecast run {d2['forecast_run_id']} providers .* do not recompute from its points")
    fc = copy.deepcopy(candidate)
    next(r for r in fc["runs"] if r["job"] == "d2")["clamped_points"] = 1
    refused(fc, seed, r"clamped_points 1 do not recompute")
    fc = copy.deepcopy(candidate)
    fc["provider_summary"]["headline"] = "TimesFM 2.5 via BigQuery AI.FORECAST for 105 of 105 series-runs"
    refused(fc, seed, r"provider_summary does not recompute")
    fc = copy.deepcopy(candidate)
    next(r for r in fc["runs"] if r["job"] == "d0")["confidence_level"] = 0.95
    refused(fc, seed, r"confidence_level 0\.95, expected 0\.8")


def test_an_unknown_provider_fails(seed, candidate):
    fc = copy.deepcopy(candidate)
    fc["points"][7]["provider"] = "prophet"
    refused(fc, seed, r"has an unknown provider 'prophet'")


def test_a_crossed_or_negative_stored_point_fails(seed, candidate):
    for change in ({"low": 9.0, "base": 8.0}, {"base": 5.0, "high": 4.0}, {"low": -0.01},
                   {"base": math.nan}, {"high": None}):
        fc = copy.deepcopy(candidate)
        p = fc["points"][11]
        p.update({"low": 1.0, "base": 2.0, "high": 3.0})
        p.update(change)
        refused(fc, seed, r"is not an ordered non-negative triple")


def test_validation_runs_on_raw_rows_before_clamping(seed, rows):
    # base below low: clamping would give (0, 0, 3), an ordered triple, but the raw row fails bounds
    assert [(p["check"], p["detail"]) for p in F.series_problems([row(4, -1.0, -2.0, 3.0)], [4], 0.8)] == [
        ("bounds", "low -1.0, base -2.0, high 3.0: value below lower")]
    primary, _ = fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 4, (-1.0, -2.0, 3.0))})
    fallback, calls = fake(F.ARIMA_PLUS, rows)
    fc = F.build(seed, primary, fallback)
    assert calls == [[TARGET]]
    assert next(r for r in fc["runs"] if r["job"] == "d3")["fallbacks"][0]["failed_check"] == "bounds"
    with pytest.raises(F.ForecastFailed):
        F.build(seed, primary)
    # an ordered raw row with negatives passes and is stored clamped, with the provider's raw triple kept
    primary, _ = fake(F.AI_FORECAST, {**rows, TARGET: replace_day(rows[TARGET], 4, (-5.0, -2.0, 3.0))})
    fc = F.build(seed, primary, fallback)
    p = next(p for p in points_of(fc, "d3", "Bang Na", "MILK-1L") if p["day"] == 4)
    assert (p["provider"], p["low"], p["base"], p["high"], p["clamped"]) == (F.AI_FORECAST, 0.0, 0.0, 3.0, True)
    assert p["raw"] == {"low": -5.0, "base": -2.0, "high": 3.0}
    assert next(r for r in fc["runs"] if r["job"] == "d3")["clamped_points"] == 1
    F.check_candidate(fc, seed)


def test_a_failed_candidate_check_keeps_the_old_file(tmp_path, monkeypatch, capsys):
    for name in ("seed.json", "truth.json"):
        (tmp_path / name).write_bytes((REF / name).read_bytes())
    old = b'{"a known previous forecast": true}\n'
    (tmp_path / "forecast.json").write_bytes(old)
    seen = []

    def refuse(forecast, seed):
        seen.append(sorted(forecast))
        raise F.CoverageError("forecast run fr-d0-x has no MILK-1L at Bang Na for day 0")

    monkeypatch.setattr(F, "check_candidate", refuse)
    with pytest.raises(SystemExit) as e:
        F.main(["--provider", "local", "--ref", str(tmp_path)])
    assert e.value.code == 1
    assert seen and "quality" in seen[0]  # called with the scored candidate, before any write
    assert (tmp_path / "forecast.json").read_bytes() == old
    assert "forecast candidate refused: forecast run fr-d0-x has no MILK-1L at Bang Na" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["forecast.json", "seed.json", "truth.json"]
    # with the real check, the same command replaces the file with a candidate that passes it
    monkeypatch.undo()
    assert F.main(["--provider", "local", "--ref", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "forecast.json").read_text(encoding="utf-8"))
    F.check_candidate(written, json.loads((tmp_path / "seed.json").read_text(encoding="utf-8")))
