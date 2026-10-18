"""A natural world from a seed: record-only sizing in whole cases, each product's markdown schedule in every step,
per-discount history (DATA-01, DATA-02, ESL-01; 03-CONTEXT D-01, D-02, D-03, D-05, D-20).

Expected values are written from the rules (the plan's must_haves and 03-CONTEXT), never pasted from generator
output. World values (branches, SKUs, case sizes) are read through data.config, not repeated as literals.
"""
import copy
import dataclasses
import json
import math
from collections import Counter

import pytest

from data import config as C
from data import seed as S
from data import world as W

CFG = C.load()
BRANCHES = CFG["world"]["branches"]
PRODUCTS = CFG["world"]["products"]


def _spy_run(seed, scenario="evaluation", cfg=None):
    """run_world with step_day wrapped: logs every lot's percent and stock around each step, and step_day's sold."""
    log = {"sold": {}, "lots": {}}
    real = S.step_day

    def spy(lots, day, demand, m, product, **kw):
        before = [(lot, lot.qty) for lot in (*lots, *kw.get("arrivals", ()))]
        res = real(lots, day, demand, m, product, **kw)
        for branch, units in res.sold.items():
            log["sold"][(branch, product.sku, day)] = units
        for lot, q0 in before:
            log["lots"][(lot.lot_id, day)] = (lot.pct, q0, lot.qty)
        return res

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(S, "step_day", spy)
        world = S.run_world(seed, scenario, cfg)
    return world, log


@pytest.fixture(scope="module")
def spied():
    return _spy_run(1)


@pytest.fixture(scope="module")
def overstocked_milk():
    """A test-only world (never a release value) whose milk order case is 200 units, so milk lots live to their
    first markdown step with stock. In the natural world a 10-day milk lot on a 3-day cycle almost always sells out
    before day X − 2, so milk markdown sales are rare there; this world exercises schedule A's sales."""
    cfg = copy.deepcopy(CFG)
    cfg["world"]["products"]["MILK-1L"]["order_case"] = 200
    return _spy_run(1, "evaluation", cfg)


@pytest.fixture(scope="module")
def pair1():
    return S.generate(1, "evaluation")


def test_an_evaluation_world_has_5_branches_3_skus_and_days_minus_90_to_6(pair1):
    seed, _ = pair1
    rows = seed["daily_sales"]
    assert len(BRANCHES) == 5 and len(PRODUCTS) == 3
    assert {r["day"] for r in rows} == set(range(-90, 7))
    per_series = Counter((r["branch"], r["sku"]) for r in rows)
    assert set(per_series) == {(b, s) for b in BRANCHES for s in PRODUCTS}
    assert set(per_series.values()) == {97}  # days −90..6
    assert len(rows) == 97 * 15
    assert seed["dataset"]["scenario"] == "evaluation" and seed["dataset"]["seed"] == 1
    assert seed["dataset"]["label"] == "Synthetic data · hypothetical scenario"
    for r in rows:
        assert set(r) == {"branch", "sku", "day", "recorded", "sold_full", "sold_20", "sold_30", "stockout_flag",
                          "markdown_flag"}
    order = [(r["day"], BRANCHES.index(r["branch"]), r["sku"]) for r in rows]
    assert order == sorted(order)


def test_recorded_sales_are_full_price_plus_discount_sales(spied, pair1):
    world, log = spied
    for r in world.days:
        for k in ("sold_full", "sold_20", "sold_30", "recorded"):
            assert r[k] == round(r[k], 2), (k, r)
        assert r["recorded"] == round(r["sold_full"] + r["sold_20"] + r["sold_30"], 2)
        # three roundings of at most 0.005 each
        assert abs(r["recorded"] - log["sold"][(r["branch"], r["sku"], r["day"])]) <= 0.015 + 1e-9
        assert r["stockout_flag"] == (r["lost"] > 1e-9)
    for r in pair1[0]["daily_sales"]:
        assert r["recorded"] == round(r["sold_full"] + r["sold_20"] + r["sold_30"], 2)


def test_markdown_days_exist_under_each_products_schedule(spied, overstocked_milk):
    world, _ = spied
    history = [r for r in world.days if -90 <= r["day"] <= -1]
    for sku in ("STRAW-500", "BREAD-400"):  # schedule B: 20% the day before expiry, 30% on it
        mine = [r for r in history if r["sku"] == sku]
        assert any(r["sold_20"] > 0 for r in mine), sku
        assert any(r["sold_30"] > 0 for r in mine), sku
    # milk under schedule A, in a world where milk lots reach their steps with stock
    milk = [r for r in overstocked_milk[0].days if r["sku"] == "MILK-1L" and -90 <= r["day"] <= -1]
    assert any(r["sold_20"] > 0 for r in milk) and any(r["sold_30"] > 0 for r in milk)
    for r in (*world.days, *overstocked_milk[0].days):
        # the flag is decided on the unrounded units; 2-place rounding could hide only a sub-cent quantity
        assert r["markdown_flag"] == (r["sold_20"] + r["sold_30"] > 0), r


def test_regular_deliveries_are_whole_cases(spied):
    world, _ = spied
    regular = [d for d in world.deliveries if d["kind"] == "regular"]
    assert regular and len(regular) == len(world.deliveries)  # the evaluation scenario has no designed lots
    for d in regular:
        case = PRODUCTS[d["sku"]]["order_case"]
        assert isinstance(d["qty"], int) and d["qty"] >= 0 and d["qty"] % case == 0, d
        assert d["origin"] == "natural" and d["tag"] is None


def test_whole_case_rounding_edges():
    # multiplier [0.90, 1.00]; u = 1 gives 1.00, u = 0 gives 0.90; case 10
    assert S.regular_qty(0.0, 1.0, (0.9, 1.0), 10) == 0  # 0 orders nothing
    assert S.regular_qty(0.9, 1.0, (0.9, 1.0), 10) == 0  # floor(0.9) = 0 still orders nothing
    assert S.regular_qty(1.0, 1.0, (0.9, 1.0), 10) == 10  # 1 unit orders one whole case
    assert S.regular_qty(20.0, 1.0, (0.9, 1.0), 10) == 20  # exactly k cases stays k cases
    assert S.regular_qty(21.0, 1.0, (0.9, 1.0), 10) == 30  # k cases + 1 orders k + 1 cases
    assert S.regular_qty(22.0, 0.0, (0.9, 1.0), 10) == 20  # 22 × 0.90 = 19.8 -> 19 -> 2 cases
    assert S.regular_qty(22.0, 0.5, (0.9, 1.0), 10) == 20  # 22 × 0.95 = 20.9 -> 20 -> 2 cases
    assert S.regular_qty(23.0, 0.5, (0.9, 1.0), 10) == 30  # 23 × 0.95 = 21.85 -> 21 -> 3 cases
    # the multiplier applies to the unrounded sum, which is rounded down once: 7.6 + 7.6 + 7.6 = 22.8 -> 22, where
    # rounding each estimate first would give 21 (7 + 7 + 7) and rounding to nearest 24
    assert S.regular_qty(7.6 + 7.6 + 7.6, 1.0, (0.9, 1.0), 1) == 22


def test_deliveries_are_in_receipt_order_with_a_stable_received_seq(spied):
    world, _ = spied
    rows = world.deliveries
    order = [(d["day"], BRANCHES.index(d["branch"]), d["sku"]) for d in rows]
    assert order == sorted(order)
    index_in_day = Counter()
    for d in rows:
        assert d["received_seq"] == d["day"] * 1000 + index_in_day[d["day"]]
        index_in_day[d["day"]] += 1
        assert d["expiry_day"] == d["day"] + PRODUCTS[d["sku"]]["shelf_life_days"]
        assert d["lot_id"].startswith(PRODUCTS[d["sku"]]["lot_prefix"] + "-")
    assert len({d["lot_id"] for d in rows}) == len(rows)
    for sku, p in PRODUCTS.items():  # each product's counter starts at 100
        assert min(int(d["lot_id"].split("-")[1]) for d in rows if d["sku"] == sku) == 100
        # each branch receives every delivery_cycle_days days from the burn-in start to the last demo day
        for b in BRANCHES:
            days = [d["day"] for d in rows if d["sku"] == sku and d["branch"] == b]
            assert all(later - earlier == p["delivery_cycle_days"] for earlier, later in zip(days, days[1:]))
            assert days[0] < W.burn_in_start() + p["delivery_cycle_days"]
            assert days[-1] > CFG["world"]["calendar"]["last_demo_day"] - p["delivery_cycle_days"]


def test_burn_in_days_are_not_kept(spied, pair1):
    world, _ = spied
    assert W.first_history_day() == -90 and W.burn_in_start() == -146  # 90 kept days, 56 burn-in days (D-03)
    assert min(r["day"] for r in world.days) == -146  # simulated
    assert min(r["day"] for r in pair1[0]["daily_sales"]) == -90  # but not kept
    assert not any(r["day"] < -90 for r in pair1[0]["daily_sales"])


def test_sizing_never_reads_future_demand(spied, monkeypatch):
    base, _ = spied
    real = S.latent_demand

    def tripled(seed, cfg):
        demand, mean = real(seed, cfg)
        return {k: (3 * v if k[2] >= -30 else v) for k, v in demand.items()}, mean

    monkeypatch.setattr(S, "latent_demand", tripled)
    perturbed = S.run_world(1, "evaluation")
    qty = {d["lot_id"]: d["qty"] for d in base.deliveries}
    after = {d["lot_id"]: d["qty"] for d in perturbed.deliveries}
    assert qty.keys() == after.keys()
    sized_before = [d["lot_id"] for d in base.deliveries if d["day"] <= -30]
    assert sized_before and all(after[i] == qty[i] for i in sized_before)
    # the perturbation is real: later deliveries see the larger recorded sales
    assert any(after[d["lot_id"]] != qty[d["lot_id"]] for d in base.deliveries if d["day"] > -30)


def _first_step_pct(sku, expiry, day):
    """The percent off on `day` for a lot expiring on `expiry`, written out by hand per product (03-CONTEXT D-20):
    milk under A: 0% through X−3, 20% on X−2, 30% on X−1 and X; strawberries and bread under B: 0% through X−2,
    20% on X−1, 30% on X."""
    if sku == "MILK-1L":
        return 0 if day <= expiry - 3 else 20 if day == expiry - 2 else 30
    return 0 if day <= expiry - 2 else 20 if day == expiry - 1 else 30


def test_a_lot_sells_at_its_scheduled_percent(spied, overstocked_milk):
    assert CFG["policy"]["product_schedule"] == {"MILK-1L": "A", "STRAW-500": "B", "BREAD-400": "B"}
    checked = Counter()
    for world, log in (spied, overstocked_milk):
        expiry = {d["lot_id"]: (d["sku"], d["expiry_day"], d["branch"]) for d in world.deliveries}
        for (lot_id, day), (pct, _, _) in log["lots"].items():  # every lot on the list, every day
            sku, x, _ = expiry[lot_id]
            assert pct == _first_step_pct(sku, x, day), (lot_id, day, pct)
        rows = {(r["branch"], r["sku"], r["day"]): r for r in world.days}
        for lot_id, (sku, x, branch) in expiry.items():
            first_step = x - (2 if sku == "MILK-1L" else 1)
            entry = log["lots"].get((lot_id, first_step))
            if entry is None:
                continue
            pct, q0, q1 = entry
            assert pct == 20
            if q0 - q1 > 0.01:  # units this lot sold on its first step day are in its branch's sold_20
                assert rows[(branch, sku, first_step)]["sold_20"] >= round(q0 - q1, 2) - 0.005
                checked[sku] += 1
    assert checked["MILK-1L"] and checked["BREAD-400"] and checked["STRAW-500"]


def test_every_world_steps_with_its_products_schedule(monkeypatch):
    seen = {}
    real = S.step_day

    def spy(lots, day, demand, m, product, **kw):
        seen.setdefault(product.sku, set()).add(kw["schedule"])
        assert not kw.get("price_changes")  # schedule steps are never price changes (ESL-01)
        return real(lots, day, demand, m, product, **kw)

    monkeypatch.setattr(S, "step_day", spy)
    S.run_world(2, "evaluation")
    assert seen == {"MILK-1L": {((2, 20), (1, 30))}, "STRAW-500": {((1, 20), (0, 30))},
                    "BREAD-400": {((1, 20), (0, 30))}}


def test_the_same_seed_gives_the_same_world_and_another_seed_another():
    a, b, c = S.generate(3, "evaluation"), S.generate(3, "evaluation"), S.generate(4, "evaluation")
    assert a == b
    assert a[0]["daily_sales"] != c[0]["daily_sales"]


# --- the rest of the natural world: record errors, day-0 and day-3 stock, inbound, runs, capacity, truth -----------

SEED_KEYS = {"dataset", "calendar", "branches", "products", "markdown_assumption", "policy", "settings", "forecast",
             "error_model", "lots", "inbound", "runs", "capacity", "daily_sales"}


def test_the_session_world_is_the_first_evaluation_seed():
    from tests.data.conftest import EVALUATION_SEED
    assert EVALUATION_SEED == CFG["scenarios"]["evaluation"]["seeds"][0]


def test_the_seed_holds_every_key_phase_4_reads(evaluation_pair):
    seed, truth = evaluation_pair
    assert set(seed) == SEED_KEYS
    assert set(truth) == {"demand", "price_response", "physical", "input_version"}  # 03-04: versioned
    assert set(seed["calendar"]) == {"day0", "first_history_day", "burn_in_days", "history_days", "demo_open_day",
                                     "last_demo_day", "horizon_end", "open_min", "delivery_min", "open_within_days",
                                     "backtest_days", "case_no_start"}
    assert seed["calendar"]["case_no_start"] is None  # evaluation worlds open no numbered cases
    assert seed["branches"] == BRANCHES
    assert [p["sku"] for p in seed["products"]] == list(PRODUCTS)  # reference order
    for p in seed["products"]:
        ref = PRODUCTS[p["sku"]]
        for k in ("name", "zone", "price", "disposal_cost", "units_per_tote", "kg_per_unit", "shelf_life_days",
                  "selling_window", "order_case", "delivery_cycle_days"):
            assert p[k] == ref[k], (p["sku"], k)
    policy = C.engine_policy(CFG)
    assert seed["policy"] == {f.name: getattr(policy, f.name) for f in dataclasses.fields(policy)}
    assert seed["settings"] == {"currency": "USD", "label": "Synthetic data · hypothetical scenario"}
    assert seed["forecast"] == {k: CFG["world"]["forecast"][k]
                                for k in ("model", "fallback", "confidence_level", "location", "dataset")}
    assert seed["error_model"] == {"share": 0.10, "range": [0.60, 0.95], "from_day": -15, "scripted": []}


def test_record_errors_follow_the_declared_model(evaluation_world):
    world = evaluation_world
    em = CFG["world"]["error_model"]
    erred = [d for d in world.deliveries if d["physical"] != d["qty"]]
    assert erred
    for d in world.deliveries:
        if d["physical"] == d["qty"]:
            continue
        assert d["kind"] == "regular" and d["day"] >= em["from_day"], d  # only regular lots from day −15 on
        assert d["physical"] == int(d["physical"])  # whole units, rounded down
        assert int(d["qty"] * em["range"][0]) <= d["physical"] <= int(d["qty"] * em["range"][1]), d
        # the declared draw: the first draw decides (< share), the second scales within the range
        rng = S._rng("error", world.seed, d["branch"], d["sku"], d["day"])
        assert rng.random() < em["share"]
        lo, hi = em["range"]
        assert d["physical"] == float(int(d["qty"] * (lo + rng.random() * (hi - lo))))
    for d in world.deliveries:  # every regular lot whose first draw is under the share carries an error
        if d["kind"] == "regular" and d["day"] >= em["from_day"] and d["qty"] > 0:
            decided = S._rng("error", world.seed, d["branch"], d["sku"], d["day"]).random() < em["share"]
            assert decided == (d["physical"] != d["qty"]), d  # factor ≤ 0.95, so floor(q × factor) < q


def test_truth_lives_only_in_truth_json(evaluation_pair):
    seed, truth = evaluation_pair
    assert not {"demand", "physical", "price_response", "markdown_truth"} & set(seed)
    md = CFG["policy"]["markdown_response"]
    assert seed["markdown_assumption"] == {"m20": md["m20"], "m30": md["m30"]}
    hidden = C.load_hidden()
    assert truth["price_response"] == hidden
    assert hidden["m20"] not in seed["markdown_assumption"]["m20"]
    assert hidden["m30"] not in seed["markdown_assumption"]["m30"]
    for row in seed["daily_sales"]:
        assert "demand" not in row and "lost" not in row


def test_day0_lots_carry_their_scheduled_markdown(evaluation_pair, evaluation_world):
    from engine.model import scheduled_pct
    seed, truth = evaluation_pair
    schedule = {p["sku"]: tuple(tuple(step) for step in p["markdown_schedule"]) for p in seed["products"]}
    assert seed["lots"]
    for lot in seed["lots"]:
        assert lot["expiry_day"] >= 0
        assert lot["markdown_pct"] == scheduled_pct(schedule[lot["sku"]], lot["expiry_day"], -1), lot
        assert lot["expiry_source"] == "PRINTED" and lot["verified_by"] == "COUNT"
    assert [l["lot_id"] for l in seed["lots"]] == sorted(l["lot_id"] for l in seed["lots"])
    received = {d["lot_id"]: d["day"] for d in evaluation_world.deliveries}
    for lot in seed["lots"]:
        assert lot["last_verified_day"] == received[lot["lot_id"]] < 0  # counted on receipt, before day 0
    # the snapshots are the stock at the start of the day, before that day's 06:00 arrivals
    for day in (0, CFG["world"]["calendar"]["demo_open_day"]):
        snap = evaluation_world.snapshots[day]
        assert snap and all(received[l["lot_id"]] < day <= l["expiry_day"] for l in snap)
    # a day-0 lot's record is its qty in seed.json; its physical stock is in truth only when it differs
    snap0 = {l["lot_id"]: l for l in evaluation_world.snapshots[0]}
    for lot in seed["lots"]:
        assert lot["qty"] == snap0[lot["lot_id"]]["recorded"]
        physical = snap0[lot["lot_id"]]["qty"]
        assert truth["physical"].get(lot["lot_id"], lot["qty"]) == physical


def test_inbound_is_the_demo_weeks_deliveries_at_06_00(evaluation_pair, evaluation_world):
    seed, truth = evaluation_pair
    cal = CFG["world"]["calendar"]
    expected = {d["lot_id"] for d in evaluation_world.deliveries if 0 <= d["day"] <= cal["last_demo_day"]
                and d["qty"] > 0}
    assert {i["inbound_id"] for i in seed["inbound"]} == expected
    by_id = {d["lot_id"]: d for d in evaluation_world.deliveries}
    for i in seed["inbound"]:
        d = by_id[i["inbound_id"]]
        assert (i["qty"], i["arrival_day"], i["arrival_min"], i["expiry_day"]) == (
            float(d["qty"]), d["day"], cal["delivery_min"], d["day"] + PRODUCTS[i["sku"]]["shelf_life_days"])
        assert i["last_verified_day"] == i["arrival_day"]
        assert truth["physical"].get(i["inbound_id"], i["qty"]) == d["physical"]


def test_every_row_is_natural_in_the_evaluation_scenario(evaluation_pair, evaluation_world):
    seed, _ = evaluation_pair
    for row in (*seed["lots"], *seed["inbound"]):
        assert row["origin"] == "natural" and row["tag"] is None, row
    assert all(d["origin"] == "natural" and d["tag"] is None for d in evaluation_world.deliveries)


def test_the_opening_value_is_used_only_in_the_burn_in(evaluation_world):
    base = CFG["world"]["demand"]["base_demand"]["MILK-1L"]["Bang Na"]
    assert S.opening_value(CFG, 1.0, "Bang Na", "MILK-1L", -91) == base  # the last burn-in order day
    assert S.opening_value(CFG, 1.0, "Bang Na", "MILK-1L", -90) is None  # the first kept day: record only
    assert S.opening_value(CFG, 0.8, "Bang Na", "MILK-1L", -146) == 0.8 * base
    tier_c = [d for d in evaluation_world.deliveries if "c" in d["estimate"]["tiers"]]
    assert tier_c and all(d["day"] < -90 for d in tier_c)


def test_init_failed_targets_order_nothing(monkeypatch):
    # with no opening value at all, the burn-in's first deliveries know nothing: every target is init_failed
    monkeypatch.setattr(S, "opening_value", lambda *a, **k: None)
    world = S.run_world(2, "evaluation")
    assert world.init_failed
    blind = [d for d in world.deliveries if set(d["estimate"]["tiers"]) == {"init_failed"}]
    assert blind and all(d["qty"] == 0 and d["estimate"]["e_sum"] == 0.0 for d in blind)
    counted = {(f["branch"], f["sku"], f["order_day"], f["target_day"]) for f in world.init_failed}
    for d in world.deliveries:
        for x, tier in zip(range(d["day"], d["day"] + d["cycle"]), d["estimate"]["tiers"]):
            assert ((d["branch"], d["sku"], d["day"], x) in counted) == (tier == "init_failed")
    # a 0-unit delivery puts no lot on the shelf, so it is never inbound or a day-0 lot
    assert all(d["lot_id"] not in {e["lot_id"] for e in world.expired} for d in blind)


def test_the_natural_world_never_fails_initialization_after_the_burn_in(evaluation_world):
    assert not [f for f in evaluation_world.init_failed if f["order_day"] >= -90]


def test_truth_demand_covers_minus_14_to_the_horizon_end(evaluation_pair):
    _, truth = evaluation_pair
    assert {r["day"] for r in truth["demand"]} == set(range(-14, 17))  # −backtest_days .. day 6 + 10-day milk
    assert len(truth["demand"]) == 31 * 15
    assert all(r["demand"] == int(r["demand"]) >= 0 for r in truth["demand"])


def test_every_product_carries_a_markdown_schedule(evaluation_pair, tmp_path, monkeypatch):
    import json
    import shutil
    seed, _ = evaluation_pair
    for p in seed["products"]:
        assert p["markdown_schedule"]
        assert p["markdown_schedule"] == [list(step) for step in C.product_schedule(CFG, p["sku"])]
    # a reference copy whose product_schedule names an empty schedule fails at config
    ref = tmp_path / "reference"
    shutil.copytree(C.REF_IN, ref)
    path = ref / "policy.json"
    tree = json.loads(path.read_text(encoding="utf-8"))
    tree["markdown_schedules"]["E"] = {"value": [], "source": "test: an empty schedule", "type": "Assumption"}
    tree["product_schedule"]["BREAD-400"]["value"] = "E"
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(C.ConfigError, match="markdown_schedules.E"):
        C.load(ref)
    # a schedule that comes back empty anyway is refused by the generator, naming the product
    real = C.product_schedule
    monkeypatch.setattr(C, "product_schedule", lambda cfg, sku: () if sku == "BREAD-400" else real(cfg, sku))
    with pytest.raises(C.ConfigError, match="BREAD-400"):
        S.generate(1, "evaluation")


def test_runs_and_capacity_come_from_reference(evaluation_pair):
    seed, _ = evaluation_pair
    ref = CFG["world"]["runs"]["R-02"]
    tt = ref["timetable"]
    r02 = [r for r in seed["runs"] if r["run_code"] == "R-02" and r["day"] == 3]
    assert r02 == [{"run_code": "R-02", "day": 3, "origin": tt["origin"], "dest": tt["dest"],
                    "loading_cutoff_min": tt["loading_cutoff_min"], "departs_min": tt["departs_min"],
                    "arrives_min": tt["arrives_min"], "spare_totes": tt["spare_totes"],
                    "cold_chain": tt["cold_chain"], "fixed_cost": ref["fixed_cost"],
                    "per_unit_cost": ref["per_unit_cost"], "cancel_cost": ref["cancel_cost"]}]
    assert r02[0]["cancel_cost"] is None
    expected_runs = {(code, day) for code, run in CFG["world"]["runs"].items() for day in run["timetable"]["days"]}
    assert {(r["run_code"], r["day"]) for r in seed["runs"]} == expected_runs
    end = seed["calendar"]["horizon_end"]
    lat = [c for c in seed["capacity"] if c["branch"] == "Lat Phrao" and c["zone"] == "chilled"]
    assert sorted(c["day"] for c in lat) == list(range(0, end + 1))
    assert {c["free_totes"] for c in lat} == {CFG["world"]["capacity"]["chilled"]["Lat Phrao"]}
    assert len(seed["capacity"]) == len(CFG["world"]["capacity"]) * len(BRANCHES) * (end + 1)


# --- the demo release world: designed story lots, the phantom lot and skips on the natural world (03-04) ------------
# Lot ids, branches and dates below are the reviewer cases' design (03-CONTEXT D-18 amended, D-20); `extra` and
# `verified` are D-15 knobs the story re-tune may change, so they are always read through config, never written here.

DEMO = CFG["scenarios"]["demo"]
DESIGNED = DEMO["designed_lots"]
REVIEWER_LOTS = ("MLK-042", "MLK-055", "STR-201", "BRD-301")


def _designed_rows(world):
    return {d["lot_id"]: d for d in world.deliveries if d["origin"] == "designed"}


def test_day3_story_lots_are_in_place(release_pair, release_world):
    seed, truth = release_pair
    lots = {l["lot_id"]: l for l in seed["lots"]}
    inbound = {i["inbound_id"]: i for i in seed["inbound"]}
    for lot_id, branch in (("MLK-042", "Bang Na"), ("MLK-055", "Bang Kapi")):  # day-0 milk lots expiring day 6
        lot = lots[lot_id]
        assert (lot["branch"], lot["sku"], lot["expiry_day"]) == (branch, "MILK-1L", 6), lot
        assert lot["last_verified_day"] == DESIGNED[lot_id]["options"]["verified"]
        assert lot["origin"] == "designed" and lot["tag"] == DESIGNED[lot_id]["tag"]
    assert lots["MLK-042"]["tag"] == "milk-story"
    phantom = lots["MLK-038"]  # the record says `extra`, the shelf holds nothing, last counted on receipt (−5)
    assert (phantom["branch"], phantom["sku"], phantom["expiry_day"]) == ("Lat Phrao", "MILK-1L", 5)
    assert phantom["qty"] == float(DESIGNED["MLK-038"]["extra"])
    assert phantom["last_verified_day"] == DESIGNED["MLK-038"]["received_day"] == -5
    assert phantom["tag"] == "milk-phantom" and truth["physical"]["MLK-038"] == 0.0
    for inbound_id, branch, sku in (("STR-201", "Rangsit", "STRAW-500"), ("BRD-301", "Thonburi", "BREAD-400")):
        i = inbound[inbound_id]
        assert (i["branch"], i["sku"], i["arrival_day"], i["arrival_min"], i["expiry_day"]) == (
            branch, sku, 2, CFG["world"]["calendar"]["delivery_min"], 5), i
        assert i["last_verified_day"] == DESIGNED[inbound_id]["options"]["verified"]
        assert i["origin"] == "designed" and i["tag"] == DESIGNED[inbound_id]["tag"]
    assert inbound["STR-201"]["expiry_source"] == "ESTIMATED" and inbound["BRD-301"]["expiry_source"] == "PRINTED"
    # designed lots are in truth's physical stock only where the record differs: the phantom lot alone
    assert set(truth["physical"]) & set(DESIGNED) == {"MLK-038"}
    assert seed["error_model"]["scripted"] == ["MLK-038"]
    assert seed["calendar"]["case_no_start"] == DEMO["case_no_start"]
    assert seed["dataset"]["scenario"] == "demo" and seed["dataset"]["seed"] == DEMO["seed"]


def test_the_four_reviewer_lots_meet_their_first_step_on_day_4(release_pair):
    seed, _ = release_pair
    schedule = {p["sku"]: p["markdown_schedule"] for p in seed["products"]}
    assert schedule["MILK-1L"] == [[2, 20], [1, 30]]  # A (D-20)
    assert schedule["STRAW-500"] == schedule["BREAD-400"] == [[1, 20], [0, 30]]  # B (D-20)
    rows = {l["lot_id"]: l for l in seed["lots"]} | {i["inbound_id"]: i for i in seed["inbound"]}
    for lot_id in REVIEWER_LOTS:
        row = rows[lot_id]
        first_step = row["expiry_day"] - max(days for days, _ in schedule[row["sku"]])
        assert first_step == 4, (lot_id, row["expiry_day"])  # Fri: MLK 6 − 2, STR/BRD 5 − 1


def test_story_lots_are_the_record_estimate_plus_extra_and_never_rounded(release_world):
    from data import estimator
    world = release_world
    designed = _designed_rows(world)
    assert set(designed) == set(DESIGNED)
    ordering = CFG["world"]["ordering"]
    start = W.burn_in_start()
    for lot_id, d in designed.items():
        ref = DESIGNED[lot_id]
        assert (d["branch"], d["sku"], d["day"]) == (ref["branch"], ref["sku"], ref["received_day"])
        assert d["expiry_day"] == d["day"] + PRODUCTS[d["sku"]]["shelf_life_days"]
        if d["kind"] == "phantom":
            continue
        assert d["kind"] == "story"
        est = d["estimate"]
        # one estimate per sellable day, the receipt day included: shelf life + 1 days (D-13)
        assert len(est["tiers"]) == PRODUCTS[d["sku"]]["shelf_life_days"] + 1
        assert d["qty"] == math.floor(est["e_sum"]) + ref["extra"], lot_id  # no multiplier, no case rounding
        # the estimate reads only the record before the receipt day, rebuilt here from the history rows
        rec = {(r["branch"], r["sku"], r["day"]): r["recorded"] for r in world.days if r["day"] < d["day"]}
        so = {(r["branch"], r["sku"], r["day"]): r["stockout_flag"] for r in world.days if r["day"] < d["day"]}
        again = [estimator.estimate(rec, so, d["branch"], d["sku"], x, d["day"], start, None,
                                    lookback=ordering["lookback_days"], same_weekday=ordering["same_weekday_obs"])
                 for x in range(d["day"], d["expiry_day"] + 1)]
        assert sum(e for e, _ in again) == pytest.approx(est["e_sum"], abs=1e-9), lot_id
        assert [t for _, t in again] == est["tiers"], lot_id
    story = designed["MLK-042"]
    assert story["qty"] == math.floor(story["estimate"]["e_sum"]) + DESIGNED["MLK-042"]["extra"]


def test_regular_deliveries_pause_while_a_designed_lot_lives_and_on_skip_days(release_world):
    regular = [d for d in release_world.deliveries if d["kind"] == "regular"]
    days = lambda branch, sku: [d["day"] for d in regular if d["branch"] == branch and d["sku"] == sku]
    assert not [x for x in days("Bang Na", "MILK-1L") if -4 <= x <= 6]  # MLK-042 lives −4..6
    assert not [x for x in days("Rangsit", "MILK-1L") if 3 <= x <= 4]  # the rangsit-milk skip
    for lot_id, ref in DESIGNED.items():
        life = PRODUCTS[ref["sku"]]["shelf_life_days"]
        mine = days(ref["branch"], ref["sku"])
        assert mine and not [x for x in mine if ref["received_day"] <= x <= ref["received_day"] + life], lot_id
        # the cycle is aligned on the designed receipt day, so no older lot is left over when it arrives
        cycle = PRODUCTS[ref["sku"]]["delivery_cycle_days"]
        assert all((x - ref["received_day"]) % cycle == 0 for x in mine), lot_id
    for name, skip in DEMO["skips"].items():
        assert not [x for x in days(skip["branch"], skip["sku"]) if skip["first_day"] <= x <= skip["last_day"]], name
    # the pauses are the demo's only change to the calendar: other branch × SKU pairs keep their evaluation days
    touched = {(r["branch"], r["sku"]) for r in (*DESIGNED.values(), *DEMO["skips"].values())}
    natural = S.delivery_calendar(DEMO["seed"], "evaluation", CFG)
    for b in BRANCHES:
        for s in PRODUCTS:
            if (b, s) not in touched:
                assert days(b, s) == [d["day"] for d in natural if d["branch"] == b and d["sku"] == s], (b, s)


def test_designed_lots_never_carry_a_record_error(release_world, release_pair):
    _, truth = release_pair
    for lot_id, d in _designed_rows(release_world).items():
        if d["kind"] == "phantom":
            assert d["qty"] == DESIGNED[lot_id]["extra"] and d["physical"] == 0.0 and d["estimate"] is None
        else:
            assert d["physical"] == float(d["qty"]), lot_id
            assert lot_id not in truth["physical"], lot_id
    # the declared error model still runs on regular lots in the demo world
    assert any(d["physical"] != d["qty"] for d in release_world.deliveries if d["kind"] == "regular")


def test_every_row_has_an_origin(release_pair, release_world):
    seed, _ = release_pair
    rows = (*seed["lots"], *seed["inbound"])
    for row in rows:
        lot_id = row.get("lot_id", row.get("inbound_id"))
        if lot_id in DESIGNED:
            assert row["origin"] == "designed" and row["tag"] == DESIGNED[lot_id]["tag"], row
        else:
            assert row["origin"] == "natural" and row["tag"] is None, row
    shown = {row.get("lot_id", row.get("inbound_id")) for row in rows}
    # every designed lot is a day-0 lot or a demo-week delivery; natural rows sit beside them, left as they fall
    assert set(DESIGNED) <= shown and len(shown) > len(DESIGNED)
    assert {d["origin"] for d in release_world.deliveries} == {"designed", "natural"}
    assert seed["error_model"]["scripted"] == ["MLK-038"]


def test_an_evaluation_world_is_never_written_over_the_release_set(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(S, "generate", lambda *a, **k: calls.append(("generate", a)) or ({}, {}))
    monkeypatch.setattr(S, "write", lambda *a, **k: calls.append(("write", a)))
    before = {p.name: p.read_bytes() for p in S.OUT.iterdir() if p.is_file()}
    for argv in (["--scenario", "evaluation"], ["--scenario", "evaluation", "--seed", "1"],
                 ["--scenario", "evaluation", "--seed", "1", "--out", str(S.OUT)]):
        with pytest.raises(SystemExit) as exc:
            S.main(argv)
        assert exc.value.code == 2, argv
        assert "refusing to write an evaluation world over the release reference set; pass --out" in \
            capsys.readouterr().err
    assert calls == []  # refused before any world is built or any file written
    assert {p.name: p.read_bytes() for p in S.OUT.iterdir() if p.is_file()} == before
    # another directory is fine
    monkeypatch.undo()
    assert S.main(["--scenario", "evaluation", "--seed", "1", "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "seed.json").read_text(encoding="utf-8"))["dataset"]["scenario"] == "evaluation"


# --- versions and reproducibility (DATA-05; 03-04 Task 2) ------------------------------------------------------------

ROOT = C.REF_IN.parent  # the ShelfShift checkout: python -m data.seed runs from here


def test_the_input_version_changes_with_any_input(release_pair, tmp_path, monkeypatch):
    import shutil
    from data import versions as V
    seed, truth = release_pair
    version = seed["dataset"]["input_version"]
    assert version.startswith("in-") and len(version) == 3 + 12
    assert truth["input_version"] == version
    assert seed["dataset"]["reference_fingerprint"] == C.fingerprint()
    # the stamp is the hash of the fingerprint and both bodies without their version fields
    assert V.input_version(C.fingerprint(), *V.bodies(seed, truth)) == version
    assert V.input_version(C.fingerprint(), seed, truth) == version  # version fields are ignored, not hashed
    # a reference copy with one source string changed has another fingerprint, so another input_version
    ref = tmp_path / "reference"
    shutil.copytree(C.REF_IN, ref)
    path = ref / "world.json"
    tree = json.loads(path.read_text(encoding="utf-8"))
    tree["demand"]["sigma"]["source"] += " (edited)"
    path.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert C.load(ref)["world"] == CFG["world"]  # the values are unchanged; only a source differs
    assert C.fingerprint(ref) != C.fingerprint()
    assert V.input_version(C.fingerprint(ref), seed, truth) != version
    # and generate() stamps whatever fingerprint the reference set has
    monkeypatch.setattr(C, "fingerprint", lambda *a, **k: "ref-000000000000")
    edited, _ = S.generate()
    assert edited["dataset"]["reference_fingerprint"] == "ref-000000000000"
    assert edited["dataset"]["input_version"] != version
    monkeypatch.undo()
    # the same call twice gives the same version; another seed number or scenario gives another
    a, b = S.generate(1, "evaluation"), S.generate(1, "evaluation")
    assert a[0]["dataset"]["input_version"] == b[0]["dataset"]["input_version"]
    assert S.generate(2, "evaluation")[0]["dataset"]["input_version"] != a[0]["dataset"]["input_version"]
    assert S.generate(DEMO["seed"], "evaluation")[0]["dataset"]["input_version"] != version
    # the generator's output alone: one changed sales value changes the version
    changed = copy.deepcopy(seed)
    changed["daily_sales"][0]["recorded"] += 1
    assert V.input_version(C.fingerprint(), changed, truth) != version


def test_basis_id_follows_only_what_the_forecast_reads(release_pair):
    from data import versions as V
    seed, _ = release_pair
    base = V.basis_id(seed)
    assert base.startswith("fb-") and len(base) == 3 + 12 and V.basis_id(copy.deepcopy(seed)) == base

    def edited(change):
        s = copy.deepcopy(seed)
        change(s)
        return V.basis_id(s)

    # what the forecast reads
    assert edited(lambda s: s["daily_sales"][5].__setitem__("recorded", s["daily_sales"][5]["recorded"] + 1)) != base
    assert edited(lambda s: s["calendar"].__setitem__("backtest_days", 7)) != base
    assert edited(lambda s: s["calendar"].__setitem__("horizon_end", 17)) != base
    assert edited(lambda s: s["markdown_assumption"]["m20"].__setitem__(1, 1.5)) != base
    assert edited(lambda s: s["forecast"].__setitem__("confidence_level", 0.95)) != base
    assert edited(lambda s: s["forecast"].__setitem__("model", "ARIMA_PLUS")) != base
    # what it does not read
    assert edited(lambda s: s["runs"][0].__setitem__("fixed_cost", 99.0)) == base
    assert edited(lambda s: s["capacity"][0].__setitem__("free_totes", 0)) == base
    assert edited(lambda s: s["lots"][0].__setitem__("qty", 1.0)) == base
    assert edited(lambda s: s["markdown_assumption"]["m20"].__setitem__(0, 1.0)) == base  # low, not the base
    assert edited(lambda s: s["dataset"].__setitem__("input_version", "in-000000000000")) == base


def test_dataset_version_changes_with_any_part(release_pair):
    from data import versions as V
    seed, truth = release_pair
    forecast = {"input_version": seed["dataset"]["input_version"], "points": [{"day": 3, "value": 1.0}]}
    ds = V.dataset_version(seed, forecast, truth)
    assert ds.startswith("ds-") and len(ds) == 3 + 12
    assert V.dataset_version(copy.deepcopy(seed), copy.deepcopy(forecast), copy.deepcopy(truth)) == ds
    s2 = copy.deepcopy(seed)
    s2["runs"][0]["fixed_cost"] = 99.0
    f2 = copy.deepcopy(forecast)
    f2["points"][0]["value"] = 2.0
    t2 = copy.deepcopy(truth)
    t2["demand"][0]["demand"] += 1
    assert len({ds, V.dataset_version(s2, forecast, truth), V.dataset_version(seed, f2, truth),
                V.dataset_version(seed, forecast, t2)}) == 4
    assert V.dataset_version(seed, truth, forecast) != ds  # the order is part of the id
    assert V.digest("x-", 1, 2) != V.digest("x-", [1, 2]) and V.digest("x-", 1, 2) == V.digest("x-", 1, 2)


def test_two_processes_with_different_hash_seeds_write_identical_files(tmp_path):
    import os
    import subprocess
    import sys
    for name, hash_seed in (("a", "1"), ("b", "2")):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        done = subprocess.run([sys.executable, "-m", "data.seed", "--out", str(tmp_path / name)], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert "input_version in-" in done.stdout
    for f in ("seed.json", "truth.json"):
        a, b = (tmp_path / "a" / f).read_bytes(), (tmp_path / "b" / f).read_bytes()
        assert a == b, f
        assert a == (S.OUT / f).read_bytes(), f"{f}: the committed release file is stale"
    assert not [p.name for p in (tmp_path / "a").iterdir() if p.name.endswith(".tmp")]  # atomic writes left no temp


def test_the_committed_release_files_match_the_generator(release_pair):
    seed, truth = release_pair
    assert (S.OUT / "seed.json").read_text(encoding="utf-8") == S.dumps(seed)
    assert (S.OUT / "truth.json").read_text(encoding="utf-8") == S.dumps(truth)
    committed = json.loads((S.OUT / "seed.json").read_text(encoding="utf-8"))
    assert committed["dataset"]["input_version"] == json.loads(
        (S.OUT / "truth.json").read_text(encoding="utf-8"))["input_version"]


def test_generate_does_not_mutate_the_config(monkeypatch):
    cfg = C.load()
    before = copy.deepcopy(cfg)
    monkeypatch.setattr(C, "load", lambda *a, **k: cfg)
    seed, truth = S.generate()
    assert cfg == before
    # no list or dict of the output is an object of the config, so editing the output cannot reach the config
    def containers(obj, out):
        if isinstance(obj, (dict, list)):
            out.add(id(obj))
            for v in (obj.values() if isinstance(obj, dict) else obj):
                containers(v, out)
        return out
    assert not containers(cfg, set()) & containers([seed, truth], set())
    # two worlds in one process share nothing either
    w1, w2 = S.run_world(1, "evaluation", cfg), S.run_world(1, "evaluation", cfg)
    assert w1.deliveries == w2.deliveries and w1.days == w2.days
    assert not containers(w1.deliveries, set()) & containers(w2.deliveries, set())
    assert cfg == before


def test_history_days_are_exactly_minus_90_to_6(release_pair):
    seed, truth = release_pair
    per_series = {}
    for r in seed["daily_sales"]:
        per_series.setdefault((r["branch"], r["sku"]), []).append(r["day"])
    assert set(per_series) == {(b, s) for b in BRANCHES for s in PRODUCTS}
    for key, days in per_series.items():
        assert days == list(range(-90, 7)), key  # none at −91, none at 7, no gap, no repeat
    demand_days = {}
    for r in truth["demand"]:
        demand_days.setdefault((r["branch"], r["sku"]), []).append(r["day"])
    assert set(demand_days) == set(per_series)
    for key, days in demand_days.items():
        assert days == list(range(-14, 17)), key
