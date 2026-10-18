"""The public set's reference values (05.2-07; DATA-09, DATA-14, DATA-16, DATA-17; 05.2-CONTEXT D-02, D-04, D-20,
D-29..D-31, D-33..D-39).

Every expected value below is the user's decision text in 05.2-CONTEXT.md, copied by hand, never read back from the
reference file under test: D-33's default row (chilled, $2.64, $0.08, 8 per tote, 0.5 kg, 3 d, schedule B, case 8,
cycle 2 d, factor 10), D-34's network (15 km disc, 30 km/h + 15 min, 60 min, k 4, days 0..6, 12:00 / 12:30, 15 spare
totes, 0.667 cold chain, $12 + $0.50/km, $0 per unit, cancel = the full quote, 20 chilled / 25 ambient, every store
mapped), D-36's no-lift response 1.0, D-38's catalogue rule and D-39's stock values (0.9-1.0, lookback 28, 4 same
weekdays, error share 0.1 in [0.6, 0.95] from day -15, 0.0 estimated, eval-day top-ups not delivered). Product rows
use the fixture's codes (city 1, store 11, product 101, management group 1), which the real profile also has.
"""
import dataclasses
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from data import config as C
from data import public_config as P
from data.config import ConfigError
from data.runs import NetworkParams
from data.stock import StockParams
from engine.adjust import InputError
from engine.model import Product

ROOT = pathlib.Path(__file__).resolve().parents[2]
REF = ROOT / "reference"
SIDECAR = ROOT / "tests" / "data" / "fixtures" / "frn_sample_synthetic.json"
FILES = ("public.json", "categories.json", "geography.json", "policy.json")
ROW = {"city": "1", "store": "11", "product": "101", "mgmt": "1", "cat1": "10", "cat2": "100", "cat3": "1000"}
SCHEDULE_B = ((1, 20), (0, 30))  # D-33: markdown schedule B, 20% at D-1, 30% at D
MARK = "Public sales dataset · Simulated operational scenario · Scenario date: {date}"  # D-04 as amended 2026-10-17


@pytest.fixture(scope="module")
def cfg():
    return P.load()


def ref_copy(tmp_path, name=None, change=None):
    """The reference files copied to tmp_path, with `change(tree)` applied to `name` first."""
    for file in FILES:
        if (REF / file).exists():
            shutil.copy(REF / file, tmp_path / file)
    if name is not None:
        tree = json.loads((tmp_path / name).read_text(encoding="utf-8"))
        change(tree)
        (tmp_path / name).write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def leaf(value, type_="Assumption", source="test"):
    return {"value": value, "type": type_, "source": source}


def at(tree, path):
    for key in path.split("."):
        tree = tree[key]
    return tree


# --- D-33: the category level, the default row and the overrides ---------------------------------------------------

def test_the_category_level_and_the_default_row_are_the_d33_values(cfg):
    assert cfg["categories"]["level"] == "mgmt"
    assert cfg["categories"]["overrides"] == {}
    assert cfg["categories"]["default"] == {
        "zone": "chilled", "price": 2.64, "disposal_cost": 0.08, "units_per_tote": 8, "kg_per_unit": 0.5,
        "shelf_life_days": 3, "schedule": "B", "order_case": 8, "delivery_cycle_days": 2}
    assert cfg["units"]["factor"] == 10


def test_the_d33_values_are_labelled_configurable_scenario_assumptions():
    tree = json.loads((REF / "categories.json").read_text(encoding="utf-8"))
    public = json.loads((REF / "public.json").read_text(encoding="utf-8"))
    labelled = [(f"categories.default.{k}", v) for k, v in tree["default"].items()]
    labelled += [("units.factor", public["units"]["factor"]), ("units.rule", public["units"]["rule"])]
    for path, node in labelled:
        assert node["type"] == "Assumption", path
        assert "05.2-06 D-33 (user)" in node["source"], path
        assert "configurable scenario assumption" in node["source"] + node.get("note", ""), path
    assert tree["level"]["type"] == "Policy"


def test_a_product_resolves_to_the_default_row_overlaid_by_its_override(tmp_path):
    ref_in = ref_copy(tmp_path, "categories.json",
                      lambda t: t["overrides"].update({"1": {"price": leaf(1.99), "shelf_life_days": leaf(5)}}))
    cfg = P.load(ref_in)
    row = P.category_row(cfg, ROW)
    assert (row["price"], row["shelf_life_days"]) == (1.99, 5)
    assert {k: v for k, v in row.items() if k not in ("price", "shelf_life_days")} == {
        "zone": "chilled", "disposal_cost": 0.08, "units_per_tote": 8, "kg_per_unit": 0.5, "schedule": "B",
        "order_case": 8, "delivery_cycle_days": 2}
    assert P.category_row(cfg, {**ROW, "mgmt": "2"}) == cfg["categories"]["default"]


def test_an_override_for_a_code_the_profile_never_saw_is_refused(tmp_path):
    # the profile's management groups are 0..6 (7 codes); 99 is not one of them
    ref_in = ref_copy(tmp_path, "categories.json", lambda t: t["overrides"].update({"99": {"price": leaf(1.0)}}))
    with pytest.raises(ConfigError, match=r"categories\.overrides\.99"):
        P.load(ref_in)


def test_an_override_with_a_field_the_row_lacks_is_refused(tmp_path):
    ref_in = ref_copy(tmp_path, "categories.json", lambda t: t["overrides"].update({"1": {"colour": leaf("red")}}))
    with pytest.raises(ConfigError, match=r"categories\.overrides\.1\.colour"):
        P.load(ref_in)


def test_a_product_row_without_the_category_level_is_refused(cfg):
    with pytest.raises(ConfigError, match=r"categories\.level"):
        P.category_row(cfg, {k: v for k, v in ROW.items() if k != "mgmt"})


# --- D-20: the engine's own input contract on every distinct row ---------------------------------------------------

def test_product_and_schedule_are_the_engine_product_and_schedule(cfg):
    assert P.product(cfg, ROW) == Product("P101", "chilled", 2.64, 0.08, 8, 0.5)
    assert P.schedule_for(cfg, ROW) == SCHEDULE_B


def test_every_distinct_category_row_passes_the_engine_self_check(tmp_path):
    ref_in = ref_copy(tmp_path, "categories.json", lambda t: t["overrides"].update({"2": {"units_per_tote": leaf(0)}}))
    with pytest.raises(InputError, match=r"units_per_tote"):
        P.load(ref_in)
    ref_in = ref_copy(tmp_path, "categories.json", lambda t: t["default"]["schedule"].update(value="Z"))
    with pytest.raises(ConfigError, match=r"markdown_schedules\.Z"):
        P.load(ref_in)


def test_policy_is_the_policy_file_with_the_public_shortlist_step(cfg, tmp_path):
    synthetic = C.engine_policy(C.load())
    assert P.policy(cfg) == dataclasses.replace(synthetic, shortlist_step=0)  # D-34: full search
    ref_in = ref_copy(tmp_path, "public.json", lambda t: t["policy"]["shortlist_step"].update(value=5))
    assert P.policy(P.load(ref_in)).shortlist_step == 5
    assert P.markdown(cfg) == C.markdown_response(C.load())


# --- D-03: typed leaves -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, path", [("categories.json", "default.price"), ("public.json", "units.factor")])
def test_a_leaf_without_a_source_or_with_an_unknown_type_is_refused(tmp_path, name, path):
    dotted = (f"categories.{path}" if name == "categories.json" else path).replace(".", r"\.")
    ref_in = ref_copy(tmp_path, name, lambda t: at(t, path).pop("source"))
    with pytest.raises(ConfigError, match=dotted):
        P.load(ref_in)
    ref_in = ref_copy(tmp_path, name, lambda t: at(t, path).update(type="Public"))
    with pytest.raises(ConfigError, match=dotted):
        P.load(ref_in)


@pytest.mark.parametrize("name, path", [
    ("public.json", "units.factor"), ("public.json", "stock.multiplier"), ("public.json", "stock.error_share"),
    ("public.json", "truth.price_response.m20"), ("categories.json", "default.price"),
    ("categories.json", "default.disposal_cost"), ("categories.json", "default.shelf_life_days"),
    ("categories.json", "default.units_per_tote"), ("categories.json", "default.kg_per_unit"),
    ("categories.json", "default.order_case"), ("categories.json", "default.delivery_cycle_days")])
def test_an_assumed_value_typed_data_is_refused(tmp_path, name, path):
    dotted = (f"categories.{path}" if name == "categories.json" else path).replace(".", r"\.")
    ref_in = ref_copy(tmp_path, name, lambda t: at(t, path).update(type="Data"))
    with pytest.raises(ConfigError, match=dotted + r".*Assumption"):
        P.load(ref_in)


# --- D-02: codes ------------------------------------------------------------------------------------------------------

def test_codes_are_prefixed_for_display_and_reversible(cfg):
    assert [P.code(cfg, kind, "12") for kind in ("city", "store", "product", "category")] == ["C12", "S12", "P12",
                                                                                              "G12"]
    for kind in ("city", "store", "product", "category"):
        for raw in ("0", "12", "897"):
            assert P.raw_id(cfg, kind, P.code(cfg, kind, raw)) == raw
    assert cfg["codes"]["note"] == "codes prefixed for display; a disclosed modification"
    with pytest.raises(ConfigError, match=r"codes\.store"):
        P.raw_id(cfg, "store", "P12")
    with pytest.raises(ConfigError, match=r"codes\.store"):
        P.raw_id(cfg, "store", "S1a")
    with pytest.raises(ConfigError, match=r"codes\.note"):
        P.code(cfg, "note", "12")
    with pytest.raises(ConfigError, match=r"codes\.store"):
        P.code(cfg, "store", "1 2")


@pytest.mark.parametrize("fmt", ["S", "S{}{}", "{}", "S{0}", "S1{}"])
def test_a_code_format_that_is_not_reversible_is_refused(tmp_path, fmt):
    ref_in = ref_copy(tmp_path, "public.json", lambda t: t["codes"]["store"].update(value=fmt))
    with pytest.raises(ConfigError, match=r"codes\.store"):
        P.load(ref_in)


def test_two_kinds_may_not_share_a_prefix(tmp_path):
    ref_in = ref_copy(tmp_path, "public.json", lambda t: t["codes"]["city"].update(value="S{}"))
    with pytest.raises(ConfigError, match=r"codes\.(city|store)"):
        P.load(ref_in)


# --- D-33, D-39: stock parameters ------------------------------------------------------------------------------------

def test_stock_params_equal_the_reference_values(cfg):
    assert P.stock_params(cfg, ROW) == StockParams(
        product=Product("P101", "chilled", 2.64, 0.08, 8, 0.5), schedule=SCHEDULE_B, shelf_life_days=3,
        cycle_days=2, order_case=8, multiplier=(0.9, 1.0), lookback_days=28, same_weekday_obs=4, error_share=0.1,
        error_range=(0.6, 0.95), error_from_day=-15, expiry_estimated_share=0.0, first_day=-90, last_day=6,
        delivery_min=360, seed=52052, topup_on_eval_days=False)


# --- D-35: unknown price and the missing-price guard -----------------------------------------------------------------

def test_the_unknown_price_rule_is_reference_policy(cfg):
    rule = json.loads((REF / "public.json").read_text(encoding="utf-8"))["unknown_price"]["rule"]
    assert rule["type"] == "Policy" and "D-35" in rule["source"]
    assert "full base price" in rule["value"] and "unavailable" in rule["value"]


def test_a_product_without_a_base_price_gets_no_money_figure(cfg, tmp_path):
    assert P.money_available(cfg, ROW) is True
    ref_in = ref_copy(tmp_path, "categories.json", lambda t: t["overrides"].update({"1": {"price": leaf(None)}}))
    unpriced = P.load(ref_in)
    assert P.money_available(unpriced, ROW) is False
    assert P.money_available(unpriced, {**ROW, "mgmt": "2"}) is True
    with pytest.raises(ConfigError, match=r"D-35"):
        P.product(unpriced, ROW)
    sp = P.stock_params(unpriced, ROW)  # the stock still simulates: only money is unavailable
    assert sp.product.price is None and sp.shelf_life_days == 3


# --- D-04, D-25, D-26, D-36, D-38: settings ---------------------------------------------------------------------------

def test_settings_carry_the_mark_the_calendar_and_the_rules(cfg):
    s = P.settings(cfg)
    assert s["mark"]["text"] == MARK
    assert s["calendar"]["data_as_of"] == "2026-10-17" and s["calendar"]["demo_open_day"] == 3
    assert s["currency"] == "USD"
    assert s["truth"]["price_response"] == {"m20": 1.0, "m30": 1.0}
    assert s["evaluation"]["on_demand"] is True
    assert s["board"]["default_scope"] == "branch" and s["board"]["page_size"] >= 1
    cat = s["catalogue"]
    assert cat["budget"] == 200 and cat["warmup_scope"] == "none"
    assert cat["kinds"] == ["verify", "transfer", "markdown", "keep", "cold_chain", "transfers_unavailable",
                            "close_call"]
    assert cat["required"] == ["verify", "transfer", "markdown", "keep"]
    assert cat["start_kind"] == ["verify", "transfer"]
    assert s["forecast"]["model"] == "TimesFM 2.5" and s["forecast"]["fallback"] == "ARIMA_PLUS"
    assert s["forecast"]["confidence_level"] == 0.8 and s["forecast"]["dataset"] == "shelfshift_public"
    assert s["series"]["max_days"] >= 1 and s["series"]["statement_timeout_ms"] >= 1


def test_case_opening_is_the_synthetic_screen(cfg):
    assert P.settings(cfg)["case_opening"] == C.load()["world"]["case_opening"]


def test_the_warm_up_response_is_the_unmeasured_no_lift_assumption():
    node = json.loads((REF / "public.json").read_text(encoding="utf-8"))["truth"]["price_response"]
    for key in ("m20", "m30"):
        assert node[key]["type"] == "Assumption" and "D-36" in node[key]["source"]
        assert "unmeasured no-lift assumption" in node[key]["source"] + node[key].get("note", "")


# --- the assumptions table (05.2-20 puts it in DATASET.md) -----------------------------------------------------------

def test_the_assumptions_table_lists_every_assumption_leaf(capsys):
    rows = P.assumptions_table()
    paths = [r[0] for r in rows]
    assert paths == sorted(paths) and len(paths) == len(set(paths))
    assert {"units.factor", "categories.default.price", "stock.multiplier"} <= set(paths)
    trees = {"": json.loads((REF / "public.json").read_text(encoding="utf-8")),
             "categories": json.loads((REF / "categories.json").read_text(encoding="utf-8"))}
    if not trees["categories"]["overrides"]:
        del trees["categories"]["overrides"]  # an empty group holds no leaf
    if (REF / "geography.json").exists():
        trees["geography"] = json.loads((REF / "geography.json").read_text(encoding="utf-8"))
    expected = sorted(p for prefix, tree in trees.items() for p, node in C.leaves(tree, prefix)
                      if node["type"] == "Assumption")
    assert paths == expected
    assert P.main(["assumptions", "--md"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "| Path | Value | Unit | Source |" and out[1].startswith("|---")
    assert len(out) == 2 + len(rows)
    assert any(line.startswith("| units.factor | 10 |") for line in out)


def test_the_fixture_load_skips_the_profile_check_with_a_reason():
    fix = P.load_for(SIDECAR)
    assert fix["profile_check"]["checked"] is False and fix["profile_check"]["reason"]
    assert P.load()["profile_check"]["checked"] is True


# --- D-29..D-31, D-34: the assumed location and route mapping ---------------------------------------------------------

GEOGRAPHY_POLICY = {"geography.service.max_travel_min", "geography.runs.k"}


def geography_leaves():
    return C.leaves(json.loads((REF / "geography.json").read_text(encoding="utf-8")), "geography")


def test_network_params_equal_the_d34_values(cfg):
    assert P.network_params(cfg) == NetworkParams(
        city_radius_km=15, speed_kmh=30, handling_min=15, max_travel_min=60, k=4, run_days=(0, 1, 2, 3, 4, 5, 6),
        loading_cutoff_min=720, departs_min=750, spare_totes=15, cold_chain_share=0.667, fixed_cost_base=12,
        per_km_cost=0.5, per_unit_cost=0, cancel_cost=None, capacity_totes={"chilled": 20, "ambient": 25},
        unmapped=frozenset(), seed=52052)
    assert cfg["geography"]["coverage"]["unmapped"] == []  # D-34: every store mapped


def test_unmapped_raw_store_codes_come_back_as_formatted_codes(tmp_path):
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: t["coverage"]["unmapped"].update(value=["13", "0"]))
    assert P.network_params(P.load(ref_in)).unmapped == frozenset({"S13", "S0"})


@pytest.mark.parametrize("unmapped", [["5", "5"], ["9999"], [5], "13"])
def test_an_unmapped_store_listed_twice_or_unknown_is_refused(tmp_path, unmapped):
    # the profile's stores are 0..897; 9999 is not one of them
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: t["coverage"]["unmapped"].update(value=unmapped))
    with pytest.raises(ConfigError, match=r"coverage\.unmapped"):
        P.load(ref_in)


def test_a_fixture_load_does_not_check_unmapped_stores_against_the_full_profile(tmp_path):
    # the fixture's stores are 11, 12, 13, 21, 22; the committed profile describes the full dataset
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: t["coverage"]["unmapped"].update(value=["13"]))
    fix = P.load_for(SIDECAR, ref_in)
    assert fix["profile_check"]["checked"] is False
    assert P.network_params(fix).unmapped == frozenset({"S13"})


def test_no_geography_leaf_orders_filters_or_ranks_destinations_by_cost(cfg):
    service = cfg["geography"]["service"]
    assert set(service) == {"filters", "same_city", "max_travel_min"}
    assert service["filters"] == ["same_city", "max_travel_min", "runs.k", "runs.days"]
    assert service["same_city"] is True and service["max_travel_min"] == 60
    for path, node in geography_leaves():
        if not path.startswith("geography.costs."):
            assert "cost" not in path, path
        text = json.dumps(node["value"]) + node.get("note", "")
        assert not re.search(r"(order|rank|sort|filter)\w*\s+(\w+\s+){0,3}by\s+(\w+\s+)?cost", text, re.I), path


def test_every_geography_leaf_is_an_assumption_except_the_service_limit_and_k():
    for path, node in geography_leaves():
        assert node["type"] == ("Policy" if path in GEOGRAPHY_POLICY else "Assumption"), path
        assert "05.2-06 D-34 (user)" in node["source"], path


def test_truck_schedules_are_labelled_simulated():
    for path, node in geography_leaves():
        if path.startswith("geography.runs."):
            assert "simulated truck schedule" in node["source"] + node.get("note", ""), path


@pytest.mark.parametrize("path, type_", [("runs.spare_totes", "Policy"), ("runs.k", "Assumption"),
                                         ("costs.per_km", "Data"), ("service.max_travel_min", "Assumption")])
def test_a_geography_leaf_of_another_type_is_refused(tmp_path, path, type_):
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: at(t, path).update(type=type_))
    with pytest.raises(ConfigError, match=r"geography\." + path.replace(".", r"\.")):
        P.load(ref_in)


def test_the_travel_time_source_and_the_service_limit_are_settings(tmp_path):
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: t["travel"]["source"].update(value="road_network"))
    with pytest.raises(ConfigError, match=r"geography\.travel\.source"):
        P.load(ref_in)
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: t["service"]["max_travel_min"].update(value=45))
    assert P.network_params(P.load(ref_in)).max_travel_min == 45


def test_a_cost_named_as_a_filter_is_refused(tmp_path):
    ref_in = ref_copy(tmp_path, "geography.json",
                      lambda t: t["service"]["filters"].update(value=["same_city", "costs.per_km"]))
    with pytest.raises(ConfigError, match=r"geography\.service\.filters"):
        P.load(ref_in)


def test_every_category_zone_has_a_receiving_capacity(tmp_path):
    ref_in = ref_copy(tmp_path, "geography.json", lambda t: t["capacity"].pop("chilled"))
    with pytest.raises(ConfigError, match=r"geography\.capacity"):
        P.load(ref_in)


def test_the_geography_file_is_required(tmp_path):
    ref_in = ref_copy(tmp_path)
    (ref_in / "geography.json").unlink()
    with pytest.raises(ConfigError, match=r"geography"):
        P.load(ref_in)


# --- T-05.2-13: the synthetic set is untouched (must stay the last test of Task 1) ----------------------------------

def test_the_synthetic_fingerprint_is_unchanged(tmp_path):
    for name in C.FINGERPRINTED:
        shown = subprocess.run(["git", "-C", str(ROOT), "show", f"phase052-web-base:reference/{name}.json"],
                               capture_output=True)
        if shown.returncode != 0:
            pytest.skip(f"the tag phase052-web-base is not in this clone: {shown.stderr.decode().strip()}")
        (tmp_path / f"{name}.json").write_bytes(shown.stdout)
    assert C.fingerprint() == C.fingerprint(tmp_path)
