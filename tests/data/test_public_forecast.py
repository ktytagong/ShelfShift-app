"""The public forecast at scale (spec §5 Forecast; DATA-12, DATA-13; 05.2-CONTEXT D-14, D-15, D-17, D-19, D-35).

Hand rows check the censored input rule by rule: an in-stock day is recorded, or converted by the store's assumed
response interpolated by discount depth and capped at 30% (D-17); a stockout day is the same-weekday in-stock mean,
never below its own converted units (D-14); an unknown-price row is converted at the full base price, tagged and
counted (D-35). The response base values are reference/policy.json's: m20 = 43/30, m30 = 1.6.

The stage runs on tests/data/fixtures/frn_sample_synthetic.csv, a synthetic test fixture in the FreshRetailNet-50K
schema; not FreshRetailNet rows (5 stores x 2 products, days -42..6). Its special rows are listed in
make_frn_sample.py; the day of each is its date minus 2024-06-26 (day 0).
"""
import csv
import gzip
import io
import json
import math
import pathlib

import pytest

from data import forecast as F
from data import public as U
from data import public_config as P
from data import public_forecast as PF

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
M20, M30 = 43 / 30, 1.6
RULE = PF.InputRule(factor=10, full_price=1.0, m20=M20, m30=M30)


@pytest.fixture(scope="module")
def ref():
    return P.load()


@pytest.fixture(scope="module")
def fix():
    return P.load_for(SIDECAR)


def source(cfg):
    return U.series_iter(None, cfg, csv_path=CSV)


def row(day, units, stockout=False, discount=1.0, unknown=False):
    """A typed day row as data.public.day_rows gives it; sale_amount is units / the factor 10."""
    return {"day": day, "sale_amount": units / 10, "stockout": stockout, "discount": discount,
            "unknown_price": unknown}


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


# --- the censored input (D-05 tags, D-14, D-17, D-35) ---------------------------------------------------------------

def test_depth_response_interpolates_between_the_assumed_points_and_caps_at_30():
    assert PF.depth_response(1.0, RULE) == 1.0  # no discount: no response
    assert PF.depth_response(0.8, RULE) == pytest.approx(M20)  # 20% off: m20 base
    assert PF.depth_response(0.9, RULE) == pytest.approx(73 / 60)  # 10% off: 1 + (43/30 - 1) x 1/2
    assert PF.depth_response(0.85, RULE) == pytest.approx(1.325)  # 15% off: 1 + 13/30 x 3/4
    assert PF.depth_response(0.75, RULE) == pytest.approx(91 / 60)  # 25% off: 43/30 + (1.6 - 43/30) x 1/2
    assert PF.depth_response(0.7, RULE) == pytest.approx(M30)  # 30% off: m30 base
    assert PF.depth_response(0.6, RULE) == pytest.approx(M30)  # 40% off: capped at 30%


def test_in_stock_days_are_recorded_or_converted_by_depth():
    rows = [row(-4, 23), row(-3, 43, discount=0.8), row(-2, 73, discount=0.9), row(-1, 16, discount=0.6)]
    got, counts = PF.public_input(rows, -1, RULE)
    assert got == [(-4, 23.0, "recorded"),  # 2.3 x 10 = 23 units at full price
                   (-3, 30.0, "converted"),  # 43 / (43/30) = 30
                   (-2, 60.0, "converted"),  # 73 / (73/60) = 60
                   (-1, 10.0, "converted")]  # 16 / 1.6 = 10 (40% off is capped at 30%)
    assert counts["recorded"] == 1 and counts["converted"] == 3


def test_a_stockout_day_is_the_same_weekday_in_stock_mean_never_below_its_own_units():
    # Days -22..-1, all in stock at 7 units, except the hand-set ones. Weekday class of day -1: -8, -15, -22 (in
    # stock 12, 9, 15: mean 12); of day -2: -9, -16 (in stock 4, 6: mean 5).
    units = {d: 7 for d in range(-22, 0)}
    units.update({-8: 12, -15: 9, -22: 15, -9: 4, -16: 6, -1: 5, -2: 8})
    rows = [row(d, u, stockout=d in (-1, -2)) for d, u in units.items()]
    got = dict((d, (v, t)) for d, v, t in PF.public_input(rows, -1, RULE)[0])
    assert got[-1] == (12.0, "imputed")  # max(mean 12, own 5) = 12
    assert got[-2] == (8.0, "imputed")  # max(mean 5, own 8) = 8: the floor (D-14); the unfloored rule gave 5


def test_the_floor_uses_the_stockout_days_own_converted_units():
    # Day -1 is a stockout sold at 20% off: own converted units 43 / (43/30) = 30, above the weekday mean 10.
    rows = [row(-15, 10), row(-8, 10), row(-1, 43, stockout=True, discount=0.8)]
    assert PF.public_input(rows, -1, RULE)[0][-1] == (-1, 30.0, "imputed")


def test_fewer_than_two_same_weekday_days_use_the_last_14_in_stock_days_and_none_leaves_it_unfilled():
    # Days -10..-2 in stock at day + 20 units (10..18); day -1 a stockout with 0 units. Its weekday class holds only
    # day -8, so the pool is the last 14 in-stock days: the 9 days -10..-2, mean 14.
    rows = [row(d, d + 20) for d in range(-10, -1)] + [row(-1, 0, stockout=True)]
    assert PF.public_input(rows, -1, RULE)[0][-1] == (-1, 14.0, "imputed")
    alone, counts = PF.public_input([row(-2, 3, stockout=True), row(-1, 4, stockout=True)], -1, RULE)
    assert alone == [(-2, 3.0, "unfilled"), (-1, 4.0, "unfilled")]  # no in-stock day: its own units
    assert counts["unfilled"] == 2


def test_unknown_price_rows_are_converted_at_full_price_tagged_and_counted():
    rows = [row(-3, 21, discount=0.0, unknown=True), row(-2, 13, discount=1.05, unknown=True), row(-1, 5)]
    got, counts = PF.public_input(rows, -1, RULE)
    assert got == [(-3, 21.0, "unknown_price"), (-2, 13.0, "unknown_price"), (-1, 5.0, "recorded")]
    assert counts["unknown_price"] == 2


def test_rows_after_the_cutoff_are_never_read():
    rows = [row(-2, 4), row(-1, 6), row(0, 900), row(1, 900, stockout=True)]
    assert [d for d, _, _ in PF.public_input(rows, -1, RULE)[0]] == [-2, -1]


def test_the_input_rule_comes_from_reference_data(ref):
    rule = PF.input_rule(ref)
    assert (rule.factor, rule.full_price) == (10, 1.0)
    assert rule.m20 == pytest.approx(M20) and rule.m30 == pytest.approx(M30)


def test_an_unknown_price_rule_this_pipeline_does_not_implement_is_refused(ref):
    other = {**ref, "unknown_price": {**ref["unknown_price"], "rule": "treat the row like a stockout day"}}
    with pytest.raises(P.ConfigError, match="unknown_price.rule"):
        PF.input_rule(other)


# --- runs (D-19) ----------------------------------------------------------------------------------------------------

def test_jobs_are_d0_to_d3_plus_the_backtest(ref):
    # horizon_end = last_demo_day 6 + the longest category shelf life 3 = 9
    assert PF.horizon_end(ref) == 9
    assert PF.jobs(ref) == [("d0", -1, list(range(0, 10))), ("d1", 0, list(range(1, 10))),
                            ("d2", 1, list(range(2, 10))), ("d3", 2, list(range(3, 10))),
                            ("backtest", -15, list(range(-14, 0)))]


def test_required_days_per_series_id(ref):
    need = PF.required_days(ref, [("S11", "P101")])
    assert need == {"d0|S11|P101": list(range(0, 10)), "d1|S11|P101": list(range(1, 10)),
                    "d2|S11|P101": list(range(2, 10)), "d3|S11|P101": list(range(3, 10)),
                    "backtest|S11|P101": list(range(-14, 0))}


def test_a_runs_value_the_pipeline_cannot_read_is_refused(ref):
    with pytest.raises(P.ConfigError, match="forecast.runs"):
        PF.jobs({**ref, "forecast": {**ref["forecast"], "runs": "every day"}})


def test_the_reference_history_id_binds_the_input_rules(ref):
    rid = PF.reference_history_id(ref)
    assert rid.startswith("fb-") and rid == PF.reference_history_id(P.load())
    assert PF.reference_history_id({**ref, "units": {**ref["units"], "factor": 5}}) != rid
    assert PF.reference_history_id({**ref, "forecast": {**ref["forecast"], "model": "TimesFM 3.0"}}) != rid


# --- the stage on the fixture ---------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def staged(fix, tmp_path_factory):
    out = tmp_path_factory.mktemp("stage")
    summary = PF.forecast_stage(source(fix), fix, out, "local")
    return out / "parts", summary


def test_the_stage_writes_the_input_part_for_bigquery(staged, fix):
    parts, _ = staged
    rows = read_gz(parts / "forecast_input.csv.gz")
    assert rows[0] == ["series_id", "ts", "value", "how"]
    sids = {r[0] for r in rows[1:]}
    assert len(sids) == 50 and "d0|S11|P101" in sids and "backtest|S22|P102" in sids  # 10 series x 5 runs
    # d0..d3 read days -42..cutoff (42, 43, 44, 45 rows), the backtest -42..-15 (28 rows): 202 rows per series
    assert len(rows) - 1 == 10 * (42 + 43 + 44 + 45 + 28)
    first = next(r for r in rows[1:] if r[0] == "d0|S11|P101")
    assert first[1] == "2026-09-02T00:00:00+00:00"  # day -42 = 2026-10-14 - 42 days, midnight UTC
    meta = json.loads((parts / "forecast_input.json").read_text(encoding="utf-8"))
    assert meta["series"] == 10 and meta["rows"] == len(rows) - 1
    assert meta["reference_history_id"] == PF.reference_history_id(fix)


def test_the_points_part_is_sorted_and_complete(staged):
    parts, summary = staged
    rows = read_gz(parts / "forecast_points.csv.gz")
    assert rows[0] == list(PF.POINT_COLUMNS)
    body = rows[1:]
    # 10 series x (10 + 9 + 8 + 7 + 14) required days = 480 points
    assert len(body) == 480 == summary["points"]
    runs = {r[0]: r for r in read_gz(parts / "forecast_runs.csv.gz")[1:]}
    job_of = {rid: r[1] for rid, r in runs.items()}
    keys = [(job_of[r[0]], r[1], r[2], int(r[3])) for r in body]
    assert keys == sorted(keys)  # by (job, store, sku, day)
    assert len(set(keys)) == 480
    assert {r[7] for r in body} == {F.LOCAL}
    for r in body:
        low, base, high = float(r[4]), float(r[5]), float(r[6])
        assert 0 <= low <= base <= high


def test_one_run_row_per_job_with_its_metadata(staged, fix):
    parts, _ = staged
    rows = read_gz(parts / "forecast_runs.csv.gz")
    assert rows[0] == list(PF.RUN_COLUMNS)
    by_job = {r[1]: r for r in rows[1:]}
    assert sorted(by_job) == ["backtest", "d0", "d1", "d2", "d3"]
    d0, bt = by_job["d0"], by_job["backtest"]
    assert (d0[2], d0[3], d0[4], d0[5]) == ("0", "-1", "0", "9")
    assert (bt[2], bt[3], bt[4], bt[5]) == ("", "-15", "-14", "-1")
    meta = json.loads(d0[6])
    assert meta["providers"] == {F.LOCAL: 10} and meta["fallbacks"] == []
    assert meta["reference_history_id"] == PF.reference_history_id(fix)
    assert meta["series"] == 10 and meta["all_zero_series"] == 0
    # d0 reads days -42..-1: stockouts on days -37, -29, -23, -16, -11; discounts on -33, -19, -12, -5; unknown
    # price on -21 (0.0) and -14 (1.05); the other 420 - 11 = 409 rows at full price, in stock.
    assert meta["input_counts"] == {"recorded": 409, "converted": 4, "unknown_price": 2, "imputed": 5,
                                    "unfilled": 0}
    # the backtest reads -42..-15 (280 rows): stockouts -37, -29, -23, -16; discounts -33, -19; unknown price -21
    assert json.loads(bt[6])["input_counts"] == {"recorded": 273, "converted": 2, "unknown_price": 1,
                                                 "imputed": 4, "unfilled": 0}
    assert rows[1][0].startswith("fr-backtest-") and by_job["d3"][0].startswith("fr-d3-")


def test_the_stage_is_deterministic(staged, fix, tmp_path):
    parts, _ = staged
    PF.forecast_stage(source(fix), fix, tmp_path, "local")
    for name in ("forecast_input.csv.gz", "forecast_points.csv.gz", "forecast_runs.csv.gz"):
        assert (tmp_path / "parts" / name).read_bytes() == (parts / name).read_bytes()


# --- per-series checks, fallback and clamping (DATA-13) -------------------------------------------------------------

def spoiled(name, bad, how):
    """A stream provider answering like the local stand-in, except for series in `bad`, changed by `how`."""
    def fetch(input_path, required):
        for sid, rows in PF.LOCAL_STREAM.fetch(input_path, required):
            yield sid, (how(rows) if sid in bad else rows)
    return PF.StreamProvider(name, fetch)


def crossed(rows):
    first = dict(rows[0], low=rows[0]["high"], high=rows[0]["low"])
    return [first] + rows[1:]


def negative(rows):
    return [dict(rows[0], low=-1.5)] + rows[1:]


@pytest.fixture
def prepared(fix, tmp_path):
    PF.prepare_input(source(fix), fix, tmp_path / "parts")
    return tmp_path


def test_a_failing_series_alone_falls_back(prepared):
    bad = "d2|S12|P101"
    primary = spoiled(F.AI_FORECAST, {bad}, crossed)
    fallback = spoiled(F.ARIMA_PLUS, set(), crossed)
    summary = PF.stream_forecast(prepared / "parts", primary, fallback)
    runs = {r[1]: json.loads(r[6]) for r in read_gz(prepared / "parts" / "forecast_runs.csv.gz")[1:]}
    assert runs["d2"]["providers"] == {F.AI_FORECAST: 9, F.ARIMA_PLUS: 1}
    assert runs["d2"]["fallbacks"] == [{"store": "S12", "sku": "P101", "failed_check": "bounds",
                                        "detail": runs["d2"]["fallbacks"][0]["detail"]}]
    assert "lower above upper" in runs["d2"]["fallbacks"][0]["detail"]
    assert runs["d1"]["providers"] == {F.AI_FORECAST: 10}
    points = read_gz(prepared / "parts" / "forecast_points.csv.gz")[1:]
    rid = next(r[0] for r in read_gz(prepared / "parts" / "forecast_runs.csv.gz")[1:] if r[1] == "d2")
    assert {p[7] for p in points if p[0] == rid and p[1] == "S12" and p[2] == "P101"} == {F.ARIMA_PLUS}
    assert summary["fallbacks"] == 1
    assert "ARIMA_PLUS fallback for 1" in summary["provider_summary"]["headline"]


def test_a_missing_series_falls_back_too(prepared):
    gone = "backtest|S21|P102"

    def fetch(input_path, required):
        for sid, rows in PF.LOCAL_STREAM.fetch(input_path, required):
            if sid != gone:
                yield sid, rows
    PF.stream_forecast(prepared / "parts", PF.StreamProvider(F.AI_FORECAST, fetch),
                       spoiled(F.ARIMA_PLUS, set(), crossed))
    runs = {r[1]: json.loads(r[6]) for r in read_gz(prepared / "parts" / "forecast_runs.csv.gz")[1:]}
    assert runs["backtest"]["fallbacks"][0]["failed_check"] == "coverage"


def test_a_double_failure_raises_and_keeps_the_previous_points_part(prepared):
    parts = prepared / "parts"
    PF.stream_forecast(parts, PF.LOCAL_STREAM)
    before = {n: (parts / n).read_bytes() for n in ("forecast_points.csv.gz", "forecast_runs.csv.gz")}
    bad = {"d0|S11|P102"}
    with pytest.raises(F.ForecastFailed) as e:
        PF.stream_forecast(parts, spoiled(F.AI_FORECAST, bad, crossed), spoiled(F.ARIMA_PLUS, bad, crossed))
    assert {p["provider"] for p in e.value.problems} == {F.AI_FORECAST, F.ARIMA_PLUS}
    assert {p["series_id"] for p in e.value.problems} == bad
    for name, data in before.items():
        assert (parts / name).read_bytes() == data
    assert sorted(p.name for p in parts.iterdir()) == ["forecast_input.csv.gz", "forecast_input.json",
                                                         "forecast_points.csv.gz", "forecast_runs.csv.gz"]


def test_a_negative_bound_is_clamped_after_the_checks_and_counted(prepared):
    sid = "d1|S13|P102"
    PF.stream_forecast(prepared / "parts", spoiled(F.AI_FORECAST, {sid}, negative))
    runs = {r[1]: (r[0], json.loads(r[6])) for r in read_gz(prepared / "parts" / "forecast_runs.csv.gz")[1:]}
    rid, meta = runs["d1"]
    points = [p for p in read_gz(prepared / "parts" / "forecast_points.csv.gz")[1:] if p[0] == rid]
    hit = next(p for p in points if p[1] == "S13" and p[2] == "P102" and p[3] == "1")
    assert (hit[4], hit[8], hit[9]) == ("0.0", "1", "-1.5")  # low 0, clamped, the raw low kept
    assert meta["clamped_points"] == sum(p[8] == "1" for p in points) >= 1
    assert meta["providers"] == {F.AI_FORECAST: 10} and meta["fallbacks"] == []


def test_without_a_fallback_a_failing_series_fails_the_run(prepared):
    with pytest.raises(F.ForecastFailed):
        PF.stream_forecast(prepared / "parts", spoiled(F.AI_FORECAST, {"d3|S22|P101"}, crossed))
    assert not (prepared / "parts" / "forecast_points.csv.gz").exists()


def test_local_rows_is_the_seasonal_naive_stand_in_on_one_series():
    series = [(d, float(d + 30), "recorded") for d in range(-14, 0)]
    rows = PF.local_rows(series, [0, 1])
    assert rows == F.local_fetch({"x": series}, {"x": [0, 1]})["x"]
    # day 0 shares its weekday with -7 and -14: values 23 and 16, mean 19.5
    assert rows[0]["base"] == 19.5 and math.isclose(rows[0]["low"], 19.5 - F.Z80 * math.sqrt(19.5))


# --- the backtest on held-out public units, in-stock days only (D-15) -----------------------------------------------

def constant(name="ai_forecast", low=8.0, base=10.0, high=12.0):
    """A stream provider answering every required day of every series with the same triple."""
    def fetch(input_path, required):
        for sid in PF._series_ids(input_path):
            yield sid, [{"day": d, "low": low, "base": base, "high": high, "confidence_level": 0.8, "status": ""}
                        for d in required[sid.split("|")[0]]]
    return PF.StreamProvider(name, fetch)


def hand_source():
    """Three series on the fixture calendar (days -42..6), sale_amount = units / 10.
    A (store 1, product 5, mgmt 1): 10 units a day, except day -21 (20 units), day -14 a stockout (0 units),
      day -13 12 units, day -12 5 units.
    B (store 1, product 6, mgmt 2): 10 units a day before the window, 0 units on every window day, all in stock.
    C (store 2, product 5, mgmt 1): 10 units a day, every window day -14..-1 a stockout."""
    def series(units, stockout=()):
        return [{"day": d, "sale_amount": units(d) / 10, "stockout": d in stockout, "discount": 1.0,
                 "unknown_price": False} for d in range(-42, 7)]
    a = series(lambda d: {-21: 20, -14: 0, -13: 12, -12: 5}.get(d, 10), stockout={-14})
    b = series(lambda d: 0 if -14 <= d <= -1 else 10)
    c = series(lambda d: 10, stockout=set(range(-14, 0)))
    return [(("1", "1", "5", ("1", "10", "100", "1000")), a), (("1", "1", "6", ("2", "20", "200", "2000")), b),
            (("1", "2", "5", ("1", "10", "100", "1000")), c)]


@pytest.fixture
def hand(fix, tmp_path):
    parts = tmp_path / "parts"
    PF.prepare_input(hand_source(), fix, parts)
    PF.stream_forecast(parts, constant())
    return PF.score_public(parts / "forecast_points.csv.gz", hand_source(), fix)


def test_the_backtest_scores_in_stock_days_only_against_public_units(hand):
    # A's in-stock window days: -13..-1 (13 days; -14 is a stockout). Actuals: 12 on -13, 5 on -12, 10 elsewhere:
    # sum 11 x 10 + 12 + 5 = 127. The model says 10 every day: |10 - 12| + |10 - 5| = 7.
    assert hand["wape"] == round(7 / 127, 4) == 0.0551
    assert hand["bias"] == round((-2 + 5) / 127, 4) == 0.0236  # Σ(forecast - actual) / Σ actual
    assert hand["coverage_80"] == round(12 / 13, 4)  # 5 is outside [8, 12]
    assert (hand["scored_days"], hand["scored_series"]) == (13, 1)
    # stockout days are excluded and counted: A's day -14 and C's 14 window days
    assert hand["excluded_stockout_days"] == 15
    assert hand["series_without_in_stock_days"] == 1  # C


def test_the_baseline_is_the_latest_same_weekday_input_at_or_before_the_cutoff(hand):
    # A's input (cutoff -15) is 10 everywhere but day -21 (20). Day -7 shares its weekday with -21, the latest such
    # day at or before -15, so its baseline is 20; every other baseline is 10. Errors: -13: 2, -12: 5, -7: 10.
    assert hand["baseline_wape"] == round(17 / 127, 4) == 0.1339
    assert hand["baseline_bias"] == round((-2 + 5 + 10) / 127, 4) == 0.1024
    assert hand["model_vs_baseline"] == "better"
    assert hand["baseline"] == "same weekday last week"


def test_a_series_with_zero_in_stock_actuals_reports_mae_only(hand):
    # B sells 0 on all 14 window days and the model says 10: MAE 10; it is not pooled into WAPE or bias
    assert hand["mae_only_series"] == {"count": 1, "mean_mae": 10.0}


def test_the_backtest_is_labelled_and_names_its_providers(hand):
    assert hand["label"] == "accuracy on public sales, in-stock days"
    assert hand["providers"] == {"ai_forecast": 3}
    assert hand["comparison_label"].startswith("TimesFM versus same weekday last week")
    assert "held-out public sales" in hand["comparison_label"] and "synthetic" not in hand["comparison_label"]
    assert hand["window"] == [-14, -1] and hand["cutoff_day"] == -15


def test_per_category_figures_use_the_reference_category_level(hand):
    assert hand["category_level"] == "mgmt"
    assert sorted(hand["per_category"]) == ["G1", "G2"]
    assert hand["per_category"]["G1"]["wape"] == 0.0551  # A alone is scored in G1 (C has no in-stock day)
    assert hand["per_category"]["G2"]["wape"] is None  # B: zero in-stock actuals


def test_pooled_sums_equal_data_forecast_figures():
    rows = [[({"low": 1.0, "base": 2.0, "high": 3.0}, 2.5, 1.0), ({"low": 0.0, "base": 4.0, "high": 5.0}, 7.0, 6.0)],
            [({"low": 2.0, "base": 2.0, "high": 2.0}, 2.0, 3.0)]]
    sums = PF.Sums()
    for series in rows:
        sums.add([((p["low"], p["base"], p["high"]), a, b) for p, a, b in series])
    assert sums.figures() == F._figures(rows)


@pytest.fixture(scope="module")
def fixture_backtest(staged, fix):
    parts, _ = staged
    return PF.score_public(parts / "forecast_points.csv.gz", source(fix), fix)


def test_the_fixture_backtest_excludes_its_stockout_day_and_calls_the_stand_in_never_quoted(fixture_backtest, staged):
    # one stockout falls in the window: store 22, product 102, 2024-06-15 = day -11
    assert fixture_backtest["excluded_stockout_days"] == 1
    assert fixture_backtest["providers"] == {F.LOCAL: 10}
    assert "never quoted" in fixture_backtest["note"]
    runs = {r[1]: json.loads(r[6]) for r in read_gz(staged[0] / "forecast_runs.csv.gz")[1:]}
    assert fixture_backtest["input_counts"] == runs["backtest"]["input_counts"]
    assert fixture_backtest["scored_days"] == 10 * 14 - 1
