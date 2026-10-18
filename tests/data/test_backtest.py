"""The backtest (DATA-04; 03-CONTEXT D-06): WAPE and bias against truth demand, the baseline from the identical
preprocessed input, and labels that name the real provider mix.

The hand world: a two-day backtest window (days -2, -1; cutoff -3), two series, milk at Bang Na and bread at
Thonburi, history -16..0. Milk sells 9 a day, 7 on day -15 and 11 on day -8; its day -9 is a stockout recording
1. Its only clean same weekday before the cutoff is -16, fewer than 2, so it is filled from the last 14 clean days,
here the 13 clean days -16..-3: (11 x 9 + 7 + 11) / 13 = 117 / 13 = 9.0. Bread sells 6 a day; day -8 sells 3 at
full price and 6.4 at 30% off, converted to 3 + 6.4 / 1.6 = 7.0 (9.4 recorded).

Same weekday last week, from the input: day -2 reads day -9, day -1 reads day -8.
  milk baseline (9.0, 11.0) vs actual (8, 12): |1| + |1| = 2, signed 1 - 1 = 0
  bread baseline (6.0, 7.0) vs actual (5, 10): |1| + |3| = 4, signed 1 - 3 = -2
Model: milk (10, 12), bread (5, 5).
  milk |2| + |0| = 2, signed +2; bread |0| + |5| = 5, signed -5
Pooled over Σ actual = 8 + 12 + 5 + 10 = 35:
  WAPE 7/35 = 0.2, bias -3/35 = -0.0857; baseline WAPE 6/35 = 0.1714, baseline bias -2/35 = -0.0571
Per SKU: milk WAPE 2/20 = 0.1 and bias 0.1 (baseline 0.1 and 0.0: equal); bread WAPE 5/15 = 0.3333 and
bias -5/15 = -0.3333 (baseline 4/15 = 0.2667 and -2/15 = -0.1333: worse).
Coverage: milk day -2 (9, 10, 11) misses 8, day -1 (10, 12, 14) holds 12; bread (4, 5, 6) holds 5, misses 10: 2/4.
"""
import copy
import json
import pathlib

import pytest

from data import forecast as F

MILK = ("Bang Na", "MILK-1L")
BREAD = ("Thonburi", "BREAD-400")
D06 = ("TimesFM versus same weekday last week, using identical preprocessed inputs; "
       "evaluated against synthetic full-price demand.")
REF = pathlib.Path(F.__file__).resolve().parent / "reference"


def sale(series, day, full, s30=0.0, stockout=False):
    return {"branch": series[0], "sku": series[1], "day": day, "sold_full": full, "sold_20": 0.0, "sold_30": s30,
            "recorded": full + s30, "stockout_flag": stockout, "markdown_flag": s30 > 0}


def hand_seed():
    rows = []
    for d in range(-16, 1):
        if d == -9:
            rows.append(sale(MILK, d, 1.0, stockout=True))
        else:
            rows.append(sale(MILK, d, {-15: 7.0, -8: 11.0}.get(d, 9.0)))
        rows.append(sale(BREAD, d, 3.0, s30=6.4) if d == -8 else sale(BREAD, d, 6.0))
    return {
        "calendar": {"first_history_day": -16, "last_demo_day": 0, "horizon_end": 1, "backtest_days": 2},
        "products": [{"sku": "MILK-1L"}, {"sku": "BREAD-400"}],
        "markdown_assumption": {"m20": [1.3, 43 / 30, 1.55], "m30": [1.4, 1.6, 1.8]},
        "forecast": {"model": "TimesFM 2.5", "confidence_level": 0.8},
        "daily_sales": rows,
    }


def hand_forecast(providers=(F.LOCAL, F.LOCAL), bases=((10.0, 12.0), (5.0, 5.0))):
    bounds = {MILK: ((9.0, 11.0), (10.0, 14.0)), BREAD: ((4.0, 6.0), (4.0, 6.0))}
    points = []
    for series, provider, values in zip((MILK, BREAD), providers, bases):
        for day, base, (low, high) in zip((-2, -1), values, bounds[series]):
            points.append({"forecast_run_id": "fr-backtest-hand", "branch": series[0], "sku": series[1],
                           "day": day, "low": low, "base": base, "high": high, "provider": provider,
                           "clamped": False})
    counts = {}
    for p in providers:
        counts[p] = counts.get(p, 0) + 1
    run = {"forecast_run_id": "fr-backtest-hand", "job": "backtest", "demo_day": None, "cutoff_day": -3,
           "days": [-2, -1], "providers": counts}
    return {"runs": [run], "points": points}


def hand_truth(milk=(8.0, 12.0), bread=(5.0, 10.0)):
    demand = []
    for series, values in ((MILK, milk), (BREAD, bread)):
        for day, v in zip((-2, -1), values):
            demand.append({"branch": series[0], "sku": series[1], "day": day, "demand": v})
    return {"demand": demand}


def test_wape_and_bias_by_hand():
    q = F.score(hand_truth(), hand_forecast(), hand_seed())
    assert (q["wape"], q["bias"]) == (0.2, -0.0857)
    assert (q["baseline_wape"], q["baseline_bias"]) == (0.1714, -0.0571)
    assert q["coverage_80"] == 0.5
    assert q["model_vs_baseline"] == "worse"
    assert (q["window"], q["cutoff_day"], q["forecast_run_id"]) == ([-2, -1], -3, "fr-backtest-hand")
    assert q["mae_only_series"] == []
    assert q["baseline"] == "same weekday last week"
    assert q["bias_definition"] == "Σ(forecast − actual) / Σ actual; positive means over-forecast"


def test_wape_and_bias_per_sku_by_hand():
    q = F.score(hand_truth(), hand_forecast(), hand_seed())
    assert list(q["per_sku"]) == ["MILK-1L", "BREAD-400"]  # reference order
    milk, bread = q["per_sku"]["MILK-1L"], q["per_sku"]["BREAD-400"]
    assert (milk["wape"], milk["bias"], milk["baseline_wape"], milk["baseline_bias"]) == (0.1, 0.1, 0.1, 0.0)
    assert milk["model_vs_baseline"] == "equal"
    assert (bread["wape"], bread["bias"], bread["baseline_wape"], bread["baseline_bias"]) == (
        0.3333, -0.3333, 0.2667, -0.1333)
    assert bread["model_vs_baseline"] == "worse"
    # the pooled figures are unchanged by the split
    assert (q["wape"], q["bias"]) == (0.2, -0.0857)
    # backtest input -16..-3: milk's day -9 imputed, bread's day -8 converted, all others recorded
    assert milk["input_counts"] == {"recorded": 13, "converted": 0, "imputed": 1, "unfilled": 0}
    assert bread["input_counts"] == {"recorded": 13, "converted": 1, "imputed": 0, "unfilled": 0}
    assert q["input_counts"] == {"recorded": 26, "converted": 1, "imputed": 1, "unfilled": 0}


def test_the_baseline_reads_the_identical_preprocessed_input():
    # read from the recorded values instead, milk day -9 would be 1 (|1 - 8| = 7) and bread day -8 would be 9.4
    q = F.score(hand_truth(), hand_forecast(), hand_seed())
    assert q["per_sku"]["MILK-1L"]["baseline_wape"] == 0.1  # (|9 - 8| + |11 - 12|) / 20
    assert q["per_sku"]["BREAD-400"]["baseline_wape"] == 0.2667  # (|6 - 5| + |7 - 10|) / 15
    # sales after the cutoff are never read: a huge day -2 changes nothing
    seed = hand_seed()
    for r in seed["daily_sales"]:
        if r["day"] > -3:
            r["sold_full"] = r["recorded"] = 10_000.0
    assert F.score(hand_truth(), hand_forecast(), seed) == q


def test_actuals_are_truth_demand_never_inputs():
    base = F.score(hand_truth(), hand_forecast(), hand_seed())
    seed = hand_seed()
    for r in seed["daily_sales"]:
        if (r["branch"], r["sku"]) == MILK and r["day"] == -16:
            r["sold_full"] = r["recorded"] = 37.0  # the imputed day -9 becomes 145 / 13 = 11.1538
    moved = F.score(hand_truth(), hand_forecast(), seed)
    assert moved["baseline_wape"] != base["baseline_wape"]  # the imputed input changed
    assert (moved["wape"], moved["bias"], moved["coverage_80"]) == (base["wape"], base["bias"], base["coverage_80"])
    other = F.score(hand_truth(milk=(8.0, 14.0)), hand_forecast(), hand_seed())
    assert other["wape"] == round(9 / 37, 4)  # |2| + |2| + |0| + |5| over 8 + 14 + 5 + 10


def test_a_series_with_zero_demand_reports_mae_only():
    q = F.score(hand_truth(bread=(0.0, 0.0)), hand_forecast(), hand_seed())
    assert q["mae_only_series"] == [{"series": ["Thonburi", "BREAD-400"], "mae": 5.0}]
    assert (q["wape"], q["bias"], q["baseline_wape"]) == (0.1, 0.1, 0.1)  # milk alone
    assert q["coverage_80"] == 0.5
    assert q["per_sku"]["BREAD-400"]["wape"] is None and q["per_sku"]["BREAD-400"]["bias"] is None


def test_all_zero_demand_gives_no_wape():
    q = F.score(hand_truth(milk=(0.0, 0.0), bread=(0.0, 0.0)), hand_forecast(), hand_seed())
    assert [m["series"] for m in q["mae_only_series"]] == [["Bang Na", "MILK-1L"], ["Thonburi", "BREAD-400"]]
    assert [m["mae"] for m in q["mae_only_series"]] == [11.0, 5.0]
    for key in ("wape", "bias", "baseline_wape", "baseline_bias", "coverage_80", "model_vs_baseline"):
        assert q[key] is None


def test_equal_wape_reads_equal():
    # bread day -1 at 6: model errors 2 + 0 + 0 + 4 = 6, the baseline's 6, both 0.1714
    assert F.score(hand_truth(), hand_forecast(bases=((10.0, 12.0), (5.0, 6.0))), hand_seed())[
        "model_vs_baseline"] == "equal"
    # 6.0001 / 35 = 0.171431 and 6 / 35 = 0.171429 agree to 4 places
    assert F.score(hand_truth(), hand_forecast(bases=((10.0, 12.0), (5.0, 5.9999))), hand_seed())[
        "model_vs_baseline"] == "equal"
    # bread day -1 at 7: 5 / 35 = 0.1429 < 0.1714
    assert F.score(hand_truth(), hand_forecast(bases=((10.0, 12.0), (5.0, 7.0))), hand_seed())[
        "model_vs_baseline"] == "better"


def test_the_comparison_label_names_the_real_provider_mix():
    def label(providers):
        return F.score(hand_truth(), hand_forecast(providers=providers), hand_seed())["comparison_label"]

    assert label((F.AI_FORECAST, F.AI_FORECAST)) == D06
    assert label((F.ARIMA_PLUS, F.ARIMA_PLUS)) == "ARIMA_PLUS" + D06[len("TimesFM"):]
    assert label((F.AI_FORECAST, F.ARIMA_PLUS)) == "TimesFM 2.5 (1 series) and ARIMA_PLUS (1 series)" + D06[
        len("TimesFM"):]
    local = label((F.LOCAL, F.LOCAL))
    assert local == "The local seasonal-naive stand-in" + D06[len("TimesFM"):]
    assert "TimesFM" not in local
    q = F.score(hand_truth(), hand_forecast(providers=(F.AI_FORECAST, F.ARIMA_PLUS)), hand_seed())
    assert q["providers"] == {F.AI_FORECAST: 1, F.ARIMA_PLUS: 1}
    assert q["label"] == "accuracy on synthetic data"
    assert q["note"] == ("A fair model comparison on identical inputs; it does not show that the preprocessing "
                         "assumptions are correct (D-06).")


def test_quality_is_written_into_the_artifact(tmp_path, capsys):
    seed = json.loads((REF / "seed.json").read_text(encoding="utf-8"))
    truth = json.loads((REF / "truth.json").read_text(encoding="utf-8"))
    (tmp_path / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    (tmp_path / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    assert F.main(["--provider", "local", "--ref", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "forecast.json").read_text(encoding="utf-8"))
    built = F.build(seed, F.LOCAL_PROVIDER)
    q = F.score(truth, built, seed)
    assert written["quality"] == q
    assert set(q["per_sku"]) == {"MILK-1L", "STRAW-500", "BREAD-400"}
    bt = next(r for r in built["runs"] if r["job"] == "backtest")
    assert q["input_counts"] == bt["input_counts"]
    assert sum(total(v["input_counts"]) for v in q["per_sku"].values()) == total(q["input_counts"])
    assert q["comparison_label"].startswith("The local seasonal-naive stand-in")
    line = capsys.readouterr().out
    assert f"WAPE {q['wape']} vs baseline {q['baseline_wape']}, bias {q['bias']} ({q['model_vs_baseline']})" in line


def total(counts):
    return sum(counts.values())


def test_the_backtest_run_must_match_the_seed_calendar():
    fc = hand_forecast()
    fc["runs"][0]["cutoff_day"] = -4
    with pytest.raises(ValueError, match="backtest"):
        F.score(hand_truth(), fc, hand_seed())
    fc = copy.deepcopy(hand_forecast())
    fc["points"] = fc["points"][:-1]
    with pytest.raises(ValueError, match="no point"):
        F.score(hand_truth(), fc, hand_seed())
