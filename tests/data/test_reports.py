"""Reports for the record (03-06; 03-CONTEXT D-16, D-04, D-11): shapes and hand examples, never pinned seed numbers."""
import json
import re
import shutil
from types import SimpleNamespace

import pytest

from data import config as C
from data import reports as R
from data import versions as V

SEEDS = [20261012, 1]  # two of the evaluation seeds keep the test fast
REPORTS = ("five_seed", "init_sensitivity", "release")


@pytest.fixture(scope="module")
def cfg():
    return C.load()


@pytest.fixture(scope="module")
def two_seed(cfg):
    return R.five_seed(cfg, seeds=SEEDS)


def day(d, demand, lost, *, full=0.0, d20=0.0, d30=0.0, wasted=0.0, revenue=0.0, disposal=0.0):
    return {"branch": "Bang Na", "sku": "MILK-1L", "day": d, "demand": demand, "lost": lost, "sold_full": full,
            "sold_20": d20, "sold_30": d30, "wasted": wasted, "revenue": revenue, "disposal": disposal}


def delivery(d, kind, qty, estimate=None):
    return {"branch": "Bang Na", "sku": "MILK-1L", "day": d, "kind": kind, "qty": qty, "estimate": estimate}


def tiny_world(days, deliveries=(), expired=(), init_failed=()):
    return SimpleNamespace(days=list(days), deliveries=list(deliveries), expired=list(expired),
                           init_failed=list(init_failed))


def keys_of(tree):
    if isinstance(tree, dict):
        for k, v in tree.items():
            yield k
            yield from keys_of(v)
    elif isinstance(tree, list):
        for v in tree:
            yield from keys_of(v)


def test_world_measures_has_every_d16_field(cfg, evaluation_world):
    m = R.world_measures(evaluation_world, cfg)
    assert set(m) == {"history", "case_window", "estimator"}
    assert {"received", "sold_full", "sold_20", "sold_30", "wasted", "stockout_share", "half_day_or_more_share",
            "net_revenue_after_operating_costs", "series_days", "stockout_series_days",
            "half_day_or_more_series_days", "revenue", "disposal", "transport_and_handling"} == set(m["history"])
    assert set(m["case_window"]) == {"lots_with_stock_left", "units_left", "lots_expiring_empty"}
    assert {"bias_vs_true_mean", "tier_counts", "fallback_count", "init_failed_count"} <= set(m["estimator"])
    assert set(m["estimator"]["tier_counts"]) == {"four", "a", "b", "c", "init_failed"}
    h = m["history"]
    assert h["series_days"] == 90 * len(cfg["world"]["branches"]) * len(cfg["world"]["products"])
    assert 0 <= h["stockout_share"] <= 1 and 0 <= h["half_day_or_more_share"] <= h["stockout_share"]
    assert h["transport_and_handling"] == 0.0
    assert h["net_revenue_after_operating_costs"] == pytest.approx(h["revenue"] - h["disposal"], abs=0.011)


def test_shares_by_hand_on_a_tiny_world(cfg):
    # the plan's example: (demand 10, lost 6) and (demand 10, lost 0) -> 0.5 and 0.5
    m = R.world_measures(tiny_world([day(-10, 10, 6), day(-9, 10, 0)]), cfg)
    assert (m["history"]["stockout_share"], m["history"]["half_day_or_more_share"]) == (0.5, 0.5)
    # the edges: exactly half counts, a small loss is a stockout only, zero demand is neither; days outside
    # −90..−1 are left out
    m = R.world_measures(tiny_world([day(-90, 8, 4), day(-50, 10, 1), day(-1, 0, 0), day(-91, 10, 10),
                                     day(0, 10, 10)]), cfg)
    h = m["history"]
    assert (h["series_days"], h["stockout_series_days"], h["half_day_or_more_series_days"]) == (3, 2, 1)
    assert (h["stockout_share"], h["half_day_or_more_share"]) == (round(2 / 3, 4), round(1 / 3, 4))


def test_units_money_case_window_and_estimator_by_hand(cfg):
    days = [day(-10, 10, 6, full=4, revenue=4 * 1.5), day(-9, 10, 0, full=8, d20=1, d30=1, wasted=2,
                                                            revenue=8 * 1.5 + 1.2 + 1.05, disposal=0.5),
            day(0, 10, 0, full=10, wasted=7, revenue=15, disposal=3)]  # day 0 is outside the history
    deliveries = [delivery(-10, "regular", 30, {"e_sum": 20.0, "mean_sum": 25.0,
                                                "tiers": ["four", "b", "c", "init_failed"]}),
                  delivery(-5, "story", 50, {"e_sum": 100.0, "mean_sum": 1.0, "tiers": ["a"] * 5}),
                  delivery(-5, "phantom", 60),
                  delivery(0, "regular", 12, {"e_sum": 9.0, "mean_sum": 9.0, "tiers": ["four"] * 3})]
    expired = [{"expiry_day": 0, "qty_left": 5.5}, {"expiry_day": 6, "qty_left": 0.0},
               {"expiry_day": 7, "qty_left": 3.0}, {"expiry_day": -1, "qty_left": 9.0}]
    init_failed = [{"branch": "Bang Na", "sku": "MILK-1L", "order_day": -10, "target_day": -7},
                   {"branch": "Bang Na", "sku": "MILK-1L", "order_day": -5, "target_day": -3},  # the story lot's
                   {"branch": "Bang Na", "sku": "MILK-1L", "order_day": -95, "target_day": -93}]  # burn-in
    m = R.world_measures(tiny_world(days, deliveries, expired, init_failed), cfg)
    h = m["history"]
    assert (h["received"], h["sold_full"], h["sold_20"], h["sold_30"], h["wasted"]) == (140, 12, 1, 1, 2)
    assert (h["revenue"], h["disposal"], h["net_revenue_after_operating_costs"]) == (20.25, 0.5, 19.75)
    assert m["case_window"] == {"lots_with_stock_left": 1, "units_left": 5.5, "lots_expiring_empty": 1}
    e = m["estimator"]  # regular deliveries arriving −90..−1 only
    assert e["bias_vs_true_mean"] == -0.2
    assert e["tier_counts"] == {"four": 1, "a": 0, "b": 1, "c": 1, "init_failed": 1}
    assert (e["fallback_count"], e["init_failed_count"]) == (2, 1)


def test_pooled_shares_are_recomputed_from_counts(cfg):
    a = R.world_measures(tiny_world([day(-10, 10, 6), day(-9, 10, 0)],
                                    [delivery(-10, "regular", 10, {"e_sum": 30.0, "mean_sum": 20.0,
                                                                   "tiers": ["four"]})]), cfg)
    b = R.world_measures(tiny_world([day(d, 10, 0) for d in range(-8, 0)],
                                    [delivery(-8, "regular", 10, {"e_sum": 60.0, "mean_sum": 80.0,
                                                                  "tiers": ["a"]})]), cfg)
    p = R.pool([a, b])
    assert p["history"]["series_days"] == 10 and p["history"]["stockout_share"] == 0.1  # 1/10, not (0.5 + 0) / 2
    assert p["history"]["half_day_or_more_share"] == 0.1
    assert p["estimator"]["bias_vs_true_mean"] == -0.1  # (90 − 100) / 100, not (0.5 + −0.25) / 2
    assert p["estimator"]["tier_counts"] == {"four": 1, "a": 1, "b": 0, "c": 0, "init_failed": 0}


def test_five_seed_lists_both_ranges_and_every_seed(cfg, two_seed):
    ordering = cfg["world"]["ordering"]
    release = R.range_name(ordering["delivery_multiplier_release"])
    reported = R.range_name(ordering["delivery_multiplier_reported"])
    assert set(two_seed["ranges"]) == {release, reported}
    assert two_seed["ranges"][release]["role"].startswith("release")
    for name, r in two_seed["ranges"].items():
        assert set(r["per_seed"]) == {str(s) for s in SEEDS}, name  # every seed, even with an empty case window
        for m in r["per_seed"].values():
            assert {"lots_with_stock_left", "units_left", "lots_expiring_empty"} == set(m["case_window"])
        assert r["pooled"] == R.pool(list(r["per_seed"].values()))
        assert R.range_name(r["multiplier"]) == name
        assert r["configuration"]["opening_factor"] == ordering["opening_factor"]
    assert two_seed["scenario"] == "evaluation" and two_seed["seeds"] == SEEDS
    b = cfg["world"]["benchmarks"]
    assert two_seed["benchmarks"]["stockout_days"] == b["freshretailnet_stockout_days"]
    assert two_seed["benchmarks"]["half_day_or_more"] == b["freshretailnet_half_day_or_more"]
    assert "never targets" in two_seed["benchmarks"]["label"]
    assert "open cases per demo day" in two_seed["case_window_label"]
    assert "ESL-02" in two_seed["case_window_label"]
    assert two_seed["label"] == cfg["policy"]["settings"]["synthetic_label"]
    assert two_seed["currency"] == cfg["policy"]["settings"]["currency"]
    assert "no pass/fail" in two_seed["note"]
    # no verdict anywhere: the only "fail" keys are the estimator's initialization-failed counts
    verdicts = [k for k in keys_of(two_seed)
                if re.search(r"pass|target|verdict", k) or ("fail" in k and not k.startswith("init_failed"))]
    assert verdicts == []


def check_ids(stamp):
    assert stamp["input_version"].startswith("in-") and len(stamp["input_version"]) == 15
    assert stamp["reference_fingerprint"].startswith("ref-")
    assert stamp["dataset_version"] is None or re.fullmatch(r"ds-[0-9a-f]{12}", stamp["dataset_version"])
    assert re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?|unavailable: \w+", stamp["generator_commit"])


def test_reports_carry_versions_and_the_commit(tmp_path):
    check_ids(R.ids())
    assert R.main(["--out", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "five_seed.json").read_text(encoding="utf-8"))
    check_ids(written)
    committed = json.loads((R.OUT / "five_seed.json").read_text(encoding="utf-8"))
    check_ids(committed)  # formats only: 03-14 may move the seed before 03-11 regenerates the reports
    assert set(written) == set(committed)


def test_dataset_version_needs_a_forecast_built_on_the_seed(tmp_path):
    for name in ("seed.json", "truth.json"):
        shutil.copy(R.REF / name, tmp_path / name)
    assert R.ids(tmp_path)["dataset_version"] is None  # no forecast
    seed = json.loads((tmp_path / "seed.json").read_text(encoding="utf-8"))
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    forecast = {"basis_id": "fb-000000000000", "points": []}
    (tmp_path / "forecast.json").write_text(json.dumps(forecast), encoding="utf-8")
    assert R.ids(tmp_path)["dataset_version"] is None  # built on another seed
    forecast["basis_id"] = V.basis_id(seed)
    (tmp_path / "forecast.json").write_text(json.dumps(forecast), encoding="utf-8")
    assert R.ids(tmp_path)["dataset_version"] == V.dataset_version(seed, forecast, truth)


def test_thirds_split_the_history_window(cfg):
    assert [f"{lo}..{hi}" for lo, hi in R.thirds(cfg)] == ["-90..-61", "-60..-31", "-30..-1"]


def test_init_sensitivity_reports_three_factors_by_third_and_the_gap(cfg, evaluation_world):
    ordering = cfg["world"]["ordering"]
    s = R.init_sensitivity(cfg, seeds=[20261012])
    factors = sorted({*map(float, ordering["init_sensitivity_factors"]), float(ordering["opening_factor"])})
    assert set(s["factors"]) == {R.factor_name(f) for f in factors} == {"0.8", "1.0", "1.2"}
    third_names = {f"{lo}..{hi}" for lo, hi in R.thirds(cfg)}
    for by_third in s["factors"].values():
        assert set(by_third) == third_names
        for t in by_third.values():
            assert t["stockout_share"] == round(t["stockout_series_days"] / t["series_days"], 4)
    low, high = s["factors"]["0.8"], s["factors"]["1.2"]
    assert set(s["gap_1.2_minus_0.8"]) == third_names
    for name, gap in s["gap_1.2_minus_0.8"].items():  # recomputed from the counts, not from rounded shares
        assert gap == round(high[name]["stockout_series_days"] / high[name]["series_days"]
                            - low[name]["stockout_series_days"] / low[name]["series_days"], 4)
    # the configured factor is the release-range world itself: its thirds add up to its history
    configured = s["factors"][R.factor_name(ordering["opening_factor"])]
    h = R.world_measures(evaluation_world, cfg)["history"]
    assert sum(t["stockout_series_days"] for t in configured.values()) == h["stockout_series_days"]
    assert sum(t["series_days"] for t in configured.values()) == h["series_days"]
    assert s["multiplier"] == list(ordering["delivery_multiplier_release"]) and s["scenario"] == "evaluation"
    assert "consistency check" in s["note"] and "nothing is tuned" in s["note"]
    assert "opening_factor" not in s["configuration"]


def test_release_reports_x_and_y_beside_the_benchmarks(cfg, release_world):
    r = R.release(cfg)
    h = R.world_measures(release_world, cfg)["history"]
    assert r["scenario"] == "demo" and r["seed"] == cfg["scenarios"]["demo"]["seed"]
    assert (r["stockout_days"], r["half_day_or_more"]) == (h["stockout_share"], h["half_day_or_more_share"])
    assert 0 <= r["half_day_or_more"] <= r["stockout_days"] <= 1
    assert r["counts"] == {k: h[k] for k in R.HISTORY_COUNTS}
    assert r["multiplier"] == list(cfg["world"]["ordering"]["delivery_multiplier_release"])
    b = cfg["world"]["benchmarks"]
    assert r["benchmarks"]["stockout_days"] == b["freshretailnet_stockout_days"]
    assert r["benchmarks"]["half_day_or_more"] == b["freshretailnet_half_day_or_more"]
    for share in (b["freshretailnet_stockout_days"], b["freshretailnet_half_day_or_more"]):
        assert f"{share * 100:.1f}%" in r["label"]
    assert "benchmarks" in r["label"] and "not matched to them" in r["label"]
    assert r["synthetic_label"] == cfg["policy"]["settings"]["synthetic_label"]


def test_report_text_makes_no_profit_or_match_claim():
    from tests.data.test_honesty import CALIBRATION_CLAIMS, PROFIT  # the same patterns, so the tests cannot disagree

    paths = [R.ROOT / "data" / "reports.py", *(R.OUT / f"{n}.json" for n in REPORTS)]
    assert all(p.is_file() for p in paths), [str(p) for p in paths if not p.is_file()]
    found = [f"{p.name}:{i}" for p in paths for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if PROFIT.search(line) or any(c.search(line) for c in CALIBRATION_CLAIMS)]
    assert found == []
    # the release label says the figures are not matched; the patterns must let that through and catch a claim
    assert not any(c.search("as benchmarks; not matched to them (D-11)") for c in CALIBRATION_CLAIMS)
    assert any(c.search("the stockout share matches FreshRetailNet-50K") for c in CALIBRATION_CLAIMS)



def test_main_writes_the_three_reports_with_the_same_ids(tmp_path):
    assert R.main(["--out", str(tmp_path)]) == 0
    written = {n: json.loads((tmp_path / f"{n}.json").read_text(encoding="utf-8")) for n in REPORTS}
    for report in written.values():
        check_ids(report)
    assert len({tuple(r[k] for k in ("input_version", "reference_fingerprint", "dataset_version",
                                     "generator_commit")) for r in written.values()}) == 1
    assert written["release"]["scenario"] == "demo" and "gap_1.2_minus_0.8" in written["init_sensitivity"]
    for n in REPORTS:  # formats only for the committed files (03-11 regenerates them)
        check_ids(json.loads((R.OUT / f"{n}.json").read_text(encoding="utf-8")))
