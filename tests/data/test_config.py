"""Reference data loads through one validated loader into engine objects the engine accepts (DATA-06, DATA-07, ESL-01).

Expected values are written from the locked decisions (03-01 plan must_haves, 01-CONTEXT D-14, 03-CONTEXT D-20),
never pasted from loader output.
"""
import dataclasses
import json
import os
import shutil

import pytest

from data import config as C
from engine.adjust import InputError
from engine.model import Policy, Product

# spec §5 product values, written here by hand for the self-check
MILK = Product("MILK-1L", "chilled", 1.11, 0.06, 10, 1.03)
STRAW = Product("STRAW-500", "chilled", 2.64, 0.08, 8, 0.50)
BREAD = Product("BREAD-400", "ambient", 0.97, 0.03, 12, 0.40)

USD_SEED = Policy(packing_min=120, min_sellable_days=1, relabel_min=60, markdown_same_day_by_min=720,
                  sellable_same_day_before_min=840, min_regret=0.03, crossover_range_floor=555.56,
                  adjustment_cost_max=2777.78, stale_after_days=7, stale_factor=0.7, phantom_min_forecast=10.0,
                  baseline_near_expiry_days=1, baseline_markdown_pct=30, waste_price=0.0, waste_flip_max=5.0,
                  shortlist_step=0)
A = ((2, 20), (1, 30))
B = ((1, 20), (0, 30))


def ref_copy(tmp_path, edit=None, name="policy"):
    """A copy of the reference directory in tmp_path, with `edit(tree)` applied to `<name>.json`."""
    out = tmp_path / "reference"
    shutil.copytree(C.REF_IN, out)
    if edit is not None:
        path = out / f"{name}.json"
        tree = json.loads(path.read_text(encoding="utf-8"))
        edit(tree)
        path.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def set_schedule(name, value):
    def edit(tree):
        tree["markdown_schedules"][name]["value"] = value
    return edit


def test_every_policy_leaf_has_a_value_a_source_and_a_known_type():
    found = C.leaves(C.read(C.REF_IN / "policy.json"), "policy")
    assert found
    for path, leaf in found:
        assert C.LEAF_KEYS <= leaf.keys() <= C.LEAF_KEYS | C.OPTIONAL_KEYS, path
        assert isinstance(leaf["source"], str) and leaf["source"].strip(), path
        assert leaf["type"] in C.TYPES, path
    paths = {path for path, _ in found}
    for f in dataclasses.fields(Policy):  # every engine Policy field once, each its own leaf
        assert f"policy.engine_policy.{f.name}" in paths


def test_engine_policy_is_the_usd_seed_for_every_policy_field():
    policy = C.engine_policy(C.load())
    assert policy == USD_SEED
    for f in dataclasses.fields(Policy):  # 10.0 is not 10: float fields stay floats, int fields ints
        assert type(getattr(policy, f.name)) is type(getattr(USD_SEED, f.name)), f.name


@pytest.mark.parametrize("edit, message", [
    (lambda ep: ep.pop("waste_flip_max"), r"missing \['waste_flip_max'\], unknown none"),
    (lambda ep: ep.update(fill_rate={"value": 0.9, "source": "x", "type": "Policy"}),
     r"missing none, unknown \['fill_rate'\]"),
])
def test_the_engine_policy_holds_every_policy_field_once_and_nothing_else(tmp_path, edit, message):
    def change(tree):
        edit(tree["engine_policy"])
    with pytest.raises(C.ConfigError, match=rf"^policy\.engine_policy: {message}"):
        C.load(ref_copy(tmp_path, change))


def test_the_m20_base_is_exactly_43_over_30():
    md = C.markdown_response(C.load())
    assert md.m20 == (1.30, 43 / 30, 1.55)
    assert md.m20[1] == 43 / 30
    assert md.m30 == (1.40, 1.60, 1.80)


def test_a_is_the_default_b_is_the_3_day_schedule_and_c_is_sensitivity_only():
    cfg = C.load()
    assert cfg["policy"]["markdown_schedules"]["default"] == "A"
    assert C.schedule(cfg) == C.schedule(cfg, "A") == A
    assert C.schedule(cfg, "B") == B
    assert C.schedule(cfg, "C") == ((0, 30),)
    assert C.product_schedule(cfg, "MILK-1L") == A
    assert C.product_schedule(cfg, "STRAW-500") == C.product_schedule(cfg, "BREAD-400") == B
    assert C.product_schedule(cfg, "NO-ENTRY") == A  # products without an entry follow the default
    assert "C" not in cfg["policy"]["product_schedule"].values()  # sensitivity only, never a product's baseline


def test_schedule_steps_become_int_tuples_sorted_by_days_before_expiry(tmp_path):
    cfg = C.load(ref_copy(tmp_path, set_schedule("A", [[1, 30], [2, 20]])))
    got = C.schedule(cfg, "A")
    assert got == ((2, 20), (1, 30))
    assert isinstance(got, tuple) and all(isinstance(s, tuple) for s in got)
    assert all(type(v) is int for s in got for v in s)


@pytest.mark.parametrize("steps", [[[2.0, 20], [1, 30]], [[2, 20.0], [1, 30]], [[True, 20], [1, 30]],
                                   [[2, 20], [1, False]]])
def test_a_float_or_bool_step_is_refused(tmp_path, steps):
    with pytest.raises(C.ConfigError, match=r"markdown_schedules\.A: .*whole numbers"):
        C.load(ref_copy(tmp_path, set_schedule("A", steps)))


@pytest.mark.parametrize("name", ["A", "B"])
def test_an_empty_food_schedule_is_refused(tmp_path, name):
    with pytest.raises(C.ConfigError, match=rf"markdown_schedules\.{name}: a food schedule must not be empty"):
        C.load(ref_copy(tmp_path, set_schedule(name, [])))


def test_a_bad_schedule_is_a_config_error_naming_it(tmp_path):
    with pytest.raises(C.ConfigError, match=r"markdown_schedules\.A: step \(2, 25\): pct must be 20 or 30"):
        C.load(ref_copy(tmp_path, set_schedule("A", [[2, 25]])))


@pytest.mark.parametrize("field, value", [("shortlist_step", 7), ("baseline_markdown_pct", 25),
                                          ("min_sellable_days", 0)])  # the last: 02-CONTEXT D-33, engine since 02-31
def test_a_policy_value_outside_the_engine_bounds_is_the_engines_input_error(tmp_path, field, value):
    def edit(tree):
        tree["engine_policy"][field]["value"] = value
    ref = ref_copy(tmp_path, edit)
    with pytest.raises(InputError, match=rf"policy\.{field}={value}"):
        C.self_check(C.load(ref), MILK)


@pytest.mark.parametrize("change, message", [
    (lambda leaf: leaf.pop("source"), "leaf is missing source"),
    (lambda leaf: leaf.pop("type"), "leaf is missing type"),
    (lambda leaf: leaf.update(type="Guess"), "type 'Guess' is not one of"),
    (lambda leaf: leaf.update(owner="me"), "unknown leaf key"),
    (lambda leaf: leaf.update(unit=""), "unit must be a non-empty string"),
])
def test_a_leaf_without_source_or_type_or_with_an_unknown_type_names_its_path(tmp_path, change, message):
    def edit(tree):
        change(tree["engine_policy"]["waste_price"])
    with pytest.raises(C.ConfigError, match=rf"^policy\.engine_policy\.waste_price: {message}"):
        C.load(ref_copy(tmp_path, edit))


def test_a_bare_value_is_not_a_leaf(tmp_path):
    def edit(tree):
        tree["engine_policy"]["waste_price"] = 0.0
    with pytest.raises(C.ConfigError, match=r"^policy\.engine_policy\.waste_price: a value must be a "):
        C.load(ref_copy(tmp_path, edit))


@pytest.mark.parametrize("source", ["", "   ", 7])
def test_an_empty_source_is_refused(tmp_path, source):
    def edit(tree):
        tree["markdown_response"]["m30"]["source"] = source
    with pytest.raises(C.ConfigError, match=r"^policy\.markdown_response\.m30: source must be a non-empty string"):
        C.load(ref_copy(tmp_path, edit))


def test_a_duplicate_key_is_refused(tmp_path):
    ref = ref_copy(tmp_path)
    path = ref / "policy.json"
    text = path.read_text(encoding="utf-8")
    twice = text.replace('"settings": {', '"settings": {\n    "currency": {"value": "EUR", "source": "x", '
                                          '"type": "Policy"},', 1)
    assert twice != text
    path.write_text(twice, encoding="utf-8")
    with pytest.raises(C.ConfigError, match=r"policy\.json: duplicate key 'currency'"):
        C.load(ref)


def test_nan_or_infinity_is_not_a_reference_value(tmp_path):
    ref = ref_copy(tmp_path)
    path = ref / "policy.json"
    text = path.read_text(encoding="utf-8").replace('"value": 0.7', '"value": NaN', 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(C.ConfigError, match=r"policy\.json: NaN is not a reference value"):
        C.load(ref)


def test_a_missing_currency_is_an_error_and_the_seed_is_usd(tmp_path):
    assert C.load()["policy"]["settings"]["currency"] == "USD"

    def edit(tree):
        del tree["settings"]["currency"]
    with pytest.raises(C.ConfigError, match=r"policy\.settings\.currency: .*never defaulted in code"):
        C.load(ref_copy(tmp_path, edit))


def test_reference_files_are_read_as_utf8():
    assert C.load()["policy"]["settings"]["synthetic_label"] == "Synthetic data · hypothetical scenario"


def test_self_check_accepts_the_seeded_policy_and_schedule():
    cfg = C.load()
    for product in (MILK, STRAW, BREAD):
        assert C.self_check(cfg, product) is None


def test_atomic_write_keeps_the_old_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "out.json"
    target.write_bytes(b'{"old": "\xc2\xb7"}\n')
    before = target.read_bytes()

    def boom(src, dst):
        raise OSError("interrupted")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="interrupted"):
        C.atomic_write(target, '{"new": 1}\n')
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json"]  # no temp file left


def test_atomic_write_writes_utf8(tmp_path):
    target = tmp_path / "out.md"
    C.atomic_write(target, "Synthetic data · hypothetical scenario\n")
    assert target.read_bytes() == "Synthetic data · hypothetical scenario\n".encode("utf-8")


def test_the_fingerprint_changes_with_any_reference_value(tmp_path):
    same = ref_copy(tmp_path / "a")
    assert C.fingerprint(same) == C.fingerprint(C.REF_IN)
    assert C.fingerprint(same).startswith("ref-") and len(C.fingerprint(same)) == 4 + 12

    def edit(tree):
        tree["engine_policy"]["waste_price"]["value"] = 0.01
    assert C.fingerprint(ref_copy(tmp_path / "b", edit)) != C.fingerprint(C.REF_IN)


# --- the world, the hidden response and the scenarios (Task 2; D-01, D-02, D-03, D-13, D-18, D-20, D-22) ---

SKUS = ("MILK-1L", "STRAW-500", "BREAD-400")
BRANCHES = ["Bang Na", "Rangsit", "Lat Phrao", "Bang Kapi", "Thonburi"]


@pytest.mark.parametrize("name", ["policy", "world", "scenarios", "hidden"])
def test_every_reference_leaf_has_a_value_a_source_and_a_known_type(name):
    found = C.leaves(C.read(C.REF_IN / f"{name}.json"), name)
    assert found
    for path, leaf in found:
        assert C.LEAF_KEYS <= leaf.keys() <= C.LEAF_KEYS | C.OPTIONAL_KEYS, path
        assert isinstance(leaf["source"], str) and leaf["source"].strip(), path
        assert leaf["type"] in C.TYPES, path


def test_world_values_are_the_locked_values():
    """Every value a decision fixes, so a later re-tune cannot move it unseen. Runs, capacity and the designed lots'
    extra/verified are D-15 knobs and are deliberately not pinned here."""
    w = C.load()["world"]
    assert w["calendar"] == {"day0": "2026-10-12", "history_days": 90, "last_demo_day": 6, "demo_open_day": 3,
                             "open_min": 540, "delivery_min": 360, "backtest_days": 14, "burn_in_days": 56,
                             "open_within_days": 2}
    assert w["branches"] == BRANCHES
    assert w["demand"]["sigma"] == 0.45
    o = w["ordering"]
    assert o["delivery_multiplier_release"] == [0.90, 1.00]  # D-01
    assert o["delivery_multiplier_reported"] == [0.85, 0.95]
    assert (o["lookback_days"], o["same_weekday_obs"], o["opening_factor"]) == (28, 4, 1.0)
    assert o["init_sensitivity_factors"] == [0.8, 1.2]
    p = w["products"]
    #        sku: (name, zone, price, disposal, tote, kg, shelf life, order case, cycle, prefix)
    locked = {"MILK-1L": ("Fresh milk 1 L", "chilled", 1.11, 0.06, 10, 1.03, 10, 10, 3, "MLK"),
              "STRAW-500": ("Strawberries 500 g", "chilled", 2.64, 0.08, 8, 0.50, 3, 8, 2, "STR"),
              "BREAD-400": ("Bread 400 g", "ambient", 0.97, 0.03, 12, 0.40, 3, 12, 2, "BRD")}
    for sku, row in locked.items():
        got = p[sku]
        assert (got["name"], got["zone"], got["price"], got["disposal_cost"], got["units_per_tote"],
                got["kg_per_unit"], got["shelf_life_days"], got["order_case"], got["delivery_cycle_days"],
                got["lot_prefix"]) == row, sku
    assert w["demand"]["base_demand"] == {
        "MILK-1L": {"Bang Na": 100, "Rangsit": 60, "Lat Phrao": 45, "Bang Kapi": 55, "Thonburi": 50},
        "STRAW-500": {"Bang Na": 20, "Rangsit": 18, "Lat Phrao": 22, "Bang Kapi": 15, "Thonburi": 16},
        "BREAD-400": {"Bang Na": 40, "Rangsit": 35, "Lat Phrao": 30, "Bang Kapi": 28, "Thonburi": 25}}
    assert w["demand"]["weekday_profile_raw"] == [0.9514, 0.9735, 0.9004, 0.8664, 0.9219, 1.1447, 1.1897]
    assert w["error_model"] == {"share": 0.10, "range": [0.60, 0.95], "from_day": -15}
    assert w["forecast"] == {"model": "TimesFM 2.5", "fallback": "ARIMA_PLUS", "confidence_level": 0.8,
                             "location": "asia-southeast1", "dataset": "shelfshift"}
    assert w["benchmarks"] == {"freshretailnet_stockout_days": 0.443, "freshretailnet_half_day_or_more": 0.175}


def test_order_case_is_its_own_value_beside_the_tote():
    tree = C.read(C.REF_IN / "world.json")["products"]
    for sku in SKUS:
        case, tote = tree[sku]["order_case"], tree[sku]["units_per_tote"]
        assert case is not tote and case["value"] == tote["value"]  # two leaves, set equal (D-02)
        assert case["type"] == "Assumption" and "D-02" in case["source"]
        assert "assumption · pending retailer" in case["source"]


def test_every_product_states_its_selling_window_as_a_pending_retailer_assumption():
    tree = C.read(C.REF_IN / "world.json")["products"]
    for sku in SKUS:
        window = tree[sku]["selling_window"]
        assert window["type"] == "Assumption"
        assert "assumption · pending retailer" in window["source"] and "D-13" in window["source"]
        assert "shelf life + 1 days" in window["value"]
        assert "shelf_life_days" in tree[sku]  # stated next to the shelf life


def test_the_weekday_profile_is_normalised_to_mean_one():
    cfg = C.load()
    raw = cfg["world"]["demand"]["weekday_profile_raw"]
    profile = C.weekday_profile(cfg)
    assert len(profile) == 7
    assert abs(sum(profile) - 7) <= 1e-12
    for i in range(7):
        assert profile[i] / profile[0] == pytest.approx(raw[i] / raw[0], rel=1e-15)
    assert max(range(7), key=profile.__getitem__) == 6 and min(range(7), key=profile.__getitem__) == 3  # Sun, Thu


def test_the_four_reviewer_lots_meet_their_first_step_on_day_4():
    cfg = C.load()
    lots = cfg["scenarios"]["demo"]["designed_lots"]
    life = {sku: p["shelf_life_days"] for sku, p in cfg["world"]["products"].items()}
    #         lot: (received, expiry, schedule)  D-18 amended, D-20
    expected = {"MLK-042": (-4, 6, A), "MLK-055": (-4, 6, A), "STR-201": (2, 5, B), "BRD-301": (2, 5, B)}
    for lot_id, (received, expiry, steps) in expected.items():
        lot = lots[lot_id]
        assert lot["received_day"] == received, lot_id
        assert lot["received_day"] + life[lot["sku"]] == expiry, lot_id
        assert C.product_schedule(cfg, lot["sku"]) == steps, lot_id
        assert expiry - max(k for k, _ in C.product_schedule(cfg, lot["sku"])) == 4, lot_id
    assert lots["STR-201"]["options"]["verified"] == lots["BRD-301"]["options"]["verified"] == 2  # counted on receipt
    assert lots["MLK-042"]["tag"] == "milk-story"


def test_the_evaluation_scenario_has_no_designed_lots_or_skips():
    sc = C.load()["scenarios"]
    assert sc["evaluation"]["designed_lots"] == {} and sc["evaluation"]["skips"] == {}
    assert sc["evaluation"]["seeds"] == [20261012, 1, 2, 3, 4]
    assert sc["demo"]["seed"] == 20261012 and sc["demo"]["case_no_start"] == 16
    assert len(sc["demo"]["designed_lots"]) == 9 and len(sc["demo"]["skips"]) == 3


def test_the_hidden_response_is_only_in_hidden_json():
    cfg = C.load()
    assert "markdown_truth" not in C.canonical(cfg)
    assert "hidden" not in cfg and "hidden" not in C.PUBLIC
    assert C.load_hidden() == {"m20": 1.38, "m30": 1.52}
    for name in C.PUBLIC:
        assert "markdown_truth" not in (C.REF_IN / f"{name}.json").read_text(encoding="utf-8")


def test_milk_has_schedule_a_and_the_3_day_products_schedule_b():
    cfg = C.load()
    assert C.product_schedule(cfg, "MILK-1L") == A
    assert C.product_schedule(cfg, "STRAW-500") == C.product_schedule(cfg, "BREAD-400") == B
    assert cfg["policy"]["markdown_schedules"]["default"] == "A"
    for product in C.products(cfg).values():  # each passes the engine
        assert C.self_check(cfg, product) is None


def test_the_case_opening_screen_thresholds_are_reference_data():
    cfg = C.load()
    assert cfg["world"]["case_opening"] == {"days_before_first_step": 1, "min_waste_units": 1,
                                            "min_discounted_totes": 1, "min_short_totes": 1}
    tree = C.read(C.REF_IN / "world.json")
    units = {"days_before_first_step": "days", "min_waste_units": "units", "min_discounted_totes": "totes",
             "min_short_totes": "totes"}
    for key, leaf in tree["case_opening"].items():
        assert leaf["type"] == "Policy" and leaf["unit"] == units[key]
        assert leaf["source"] == "ESL-02 case-opening screen (03-CONTEXT D-22); pending retailer"
    assert "open_within_days" not in tree["case_opening"]
    assert "superseded by ESL-02" in tree["calendar"]["open_within_days"]["source"]


def test_data06_values_are_all_present():
    cfg = C.load()
    p, w = cfg["policy"], cfg["world"]
    assert set(p["markdown_response"]) == {"m20", "m30"}
    assert set(w["runs"]) == {"R-02", "R-03", "R-05", "R-07", "R-08", "R-09"}
    for code, run in w["runs"].items():
        assert {"fixed_cost", "cancel_cost", "per_unit_cost", "timetable"} <= run.keys(), code
    for sku in SKUS:
        assert "disposal_cost" in w["products"][sku]
    assert {"delivery_multiplier_release", "delivery_multiplier_reported"} <= w["ordering"].keys()
    assert {"waste_price", "shortlist_step"} <= p["engine_policy"].keys()


def test_the_two_documented_notes_are_on_their_leaves():
    """03-13's generated table carries these notes (stale by construction; in-stock sizing biased low)."""
    policy = C.read(C.REF_IN / "policy.json")
    world = C.read(C.REF_IN / "world.json")
    assert "stale by construction" in policy["engine_policy"]["stale_after_days"]["note"]
    assert "biased low" in world["ordering"]["delivery_multiplier_release"]["note"]


MONEY_KEYS = {"min_regret", "crossover_range_floor", "adjustment_cost_max", "waste_flip_max"}


def test_money_leaves_are_in_usd():
    checked = 0
    for name in ("policy", "world", "scenarios", "hidden"):
        for path, leaf in C.leaves(C.read(C.REF_IN / f"{name}.json"), name):
            key = path.rsplit(".", 1)[-1]
            if "price" in key or "cost" in key or key in MONEY_KEYS:
                assert leaf.get("unit", "").startswith("USD"), path
                checked += 1
    assert checked == 5 + 3 * 2 + 6 * 3  # policy guards and waste money, product price/disposal, run costs


def test_reference_has_no_project_id():
    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)
    for name in ("policy", "world", "scenarios", "hidden"):
        assert not [k for k in keys(C.read(C.REF_IN / f"{name}.json")) if "project" in k.lower()], name


@pytest.mark.parametrize("name, edit, message", [
    ("policy", lambda t: t["product_schedule"].pop("BREAD-400"), r"^policy\.product_schedule: keys"),
    ("scenarios", lambda t: t["demo"]["designed_lots"]["MLK-042"]["value"].update(branch="Nowhere"),
     r"^scenarios\.demo\.designed_lots\.MLK-042: unknown branch 'Nowhere'"),
    ("scenarios", lambda t: t["demo"]["skips"]["rangsit-milk"]["value"].update(sku="EGG-10"),
     r"^scenarios\.demo\.skips\.rangsit-milk: unknown sku 'EGG-10'"),
    ("world", lambda t: t["runs"]["R-02"]["timetable"]["value"].update(dest="Nowhere"),
     r"^world\.runs\.R-02\.timetable: unknown dest 'Nowhere'"),
])
def test_the_reference_files_must_agree_on_branches_and_products(tmp_path, name, edit, message):
    with pytest.raises(C.ConfigError, match=message):
        C.load(ref_copy(tmp_path, edit, name=name))


def test_a_bad_product_value_is_the_engines_input_error(tmp_path):
    def edit(tree):
        tree["products"]["BREAD-400"]["units_per_tote"]["value"] = 0
    with pytest.raises(InputError, match=r"product\.units_per_tote=0"):
        C.load(ref_copy(tmp_path, edit, name="world"))


def test_the_horizon_runs_to_the_last_demo_day_plus_the_longest_shelf_life():
    assert C.horizon_end(C.load()) == 6 + 10
