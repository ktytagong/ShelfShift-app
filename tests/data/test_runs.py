"""The local candidate network per source store (05.2-04; DATA-14; D-21, D-29, D-30, D-31).

The stores come from tests/data/fixtures/frn_sample_synthetic.csv (a synthetic test fixture in the FreshRetailNet-50K
schema; not FreshRetailNet rows): city "1" holds stores "11", "12", "13" and city "2" holds "21", "22"; the fixture's
geography leaves "13" unmapped. Every NetworkParams value below is a test value, not a reference value.

Hand derivation for the boundary (test_exactly_the_maximum_travel_time_is_in_and_one_minute_over_is_out): speed
60 km/h and 0 handling minutes make a minute per km. Store "a" at (0, 0), "b" at (30, 0), "c" at (31, 0) and "d" at
(30.5, 0) in one city: a->b = ceil(30) = 30 min (in at a 30-minute limit), a->c = 31 (out), a->d = ceil(30.5) = 31
(out). With 5 handling minutes a->b is 35.
"""
import dataclasses
import math
import pathlib

import pytest

from data import public as U
from data import public_config as P
from data import runs as N

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
RUN_KEYS = {"run_code", "day", "origin", "dest", "loading_cutoff_min", "departs_min", "arrives_min", "spare_totes",
            "cold_chain", "fixed_cost", "per_unit_cost", "cancel_cost", "travel_min"}


def params(**changes) -> N.NetworkParams:
    base = dict(city_radius_km=3.0, speed_kmh=30.0, handling_min=10, max_travel_min=30, k=2,
                run_days=(0, 1, 2, 3, 4, 5, 6), loading_cutoff_min=720, departs_min=750, spare_totes=20,
                cold_chain_share=0.5, fixed_cost_base=10.0, per_km_cost=0.5, per_unit_cost=0.05, cancel_cost=None,
                capacity_totes={"chilled": 20, "ambient": 25}, unmapped=frozenset({"13"}), seed=7)
    base.update(changes)
    return N.NetworkParams(**base)


@pytest.fixture(scope="module")
def stores_by_city():
    cfg = P.load_for(SIDECAR)
    out: dict[str, set] = {}
    for row in U.day_rows(U.read_csv(CSV, cfg["dataset"]["columns"]), cfg):
        out.setdefault(row["city"], set()).add(row["store"])
    return {city: sorted(stores) for city, stores in out.items()}


def city_of(stores_by_city) -> dict:
    return {s: c for c, stores in stores_by_city.items() for s in stores}


def test_the_fixture_has_two_cities(stores_by_city):
    assert stores_by_city == {"1": ["11", "12", "13"], "2": ["21", "22"]}


def test_positions_are_keyed_draws_inside_the_city_disc_and_skip_unmapped_stores(stores_by_city):
    p = params()
    pos = N.positions(stores_by_city, p)
    assert sorted(pos) == ["11", "12", "21", "22"]  # "13" is unmapped: no position
    for store, (x, y) in pos.items():
        assert math.hypot(x, y) <= p.city_radius_km, store
    assert N.positions(stores_by_city, p) == pos  # the same params give the same positions
    assert N.positions(stores_by_city, dataclasses.replace(p, seed=8)) != pos


def test_candidates_are_same_city_within_the_limit_nearest_first_at_most_k(stores_by_city):
    p = params(city_radius_km=10.0, max_travel_min=40, k=1)
    pos = N.positions(stores_by_city, p)
    pairs, unmapped = N.candidate_pairs(stores_by_city, pos, p)
    city = city_of(stores_by_city)
    assert unmapped == ["13"]
    assert pairs
    per_source: dict[str, list] = {}
    for pair in pairs:
        assert city[pair["origin"]] == city[pair["dest"]] == pair["city"]
        assert pair["origin"] != pair["dest"]
        assert "13" not in (pair["origin"], pair["dest"])
        assert pair["travel_min"] == N.travel_min(pos[pair["origin"]], pos[pair["dest"]], p)
        assert pair["travel_min"] <= p.max_travel_min
        per_source.setdefault(pair["origin"], []).append(pair)
    for origin, mine in per_source.items():
        assert len(mine) <= p.k
        # nearest first: no eligible destination left out is nearer than the last one kept
        kept = [(x["travel_min"], x["dest"]) for x in mine]
        assert kept == sorted(kept)
        others = [(N.travel_min(pos[origin], pos[d], p), d) for d in pos
                  if d != origin and city[d] == city[origin] and d not in {x["dest"] for x in mine}]
        assert all(o > kept[-1] for o in others if o[0] <= p.max_travel_min)
    wide = N.candidate_pairs(stores_by_city, pos, dataclasses.replace(p, k=5, max_travel_min=60))[0]
    assert {(x["origin"], x["dest"]) for x in wide} == {("11", "12"), ("12", "11"), ("21", "22"), ("22", "21")}


def test_exactly_the_maximum_travel_time_is_in_and_one_minute_over_is_out():
    p = params(speed_kmh=60.0, handling_min=0, max_travel_min=30, k=5, unmapped=frozenset())
    pos = {"a": (0.0, 0.0), "b": (30.0, 0.0), "c": (31.0, 0.0), "d": (30.5, 0.0)}
    assert N.travel_min(pos["a"], pos["b"], p) == 30
    assert N.travel_min(pos["a"], pos["c"], p) == 31
    assert N.travel_min(pos["a"], pos["d"], p) == 31  # rounded up to a whole minute
    assert N.travel_min(pos["a"], pos["b"], dataclasses.replace(p, handling_min=5)) == 35
    pairs, unmapped = N.candidate_pairs({"X": ["a", "b", "c", "d"]}, pos, p)
    assert unmapped == []
    assert [x["dest"] for x in pairs if x["origin"] == "a"] == ["b"]


def test_k_zero_and_a_lone_mapped_store_give_no_pair(stores_by_city):
    p = params(city_radius_km=10.0, max_travel_min=60)
    pos = N.positions(stores_by_city, p)
    assert N.candidate_pairs(stores_by_city, pos, dataclasses.replace(p, k=0))[0] == []
    lone = dataclasses.replace(p, unmapped=frozenset({"13", "22"}))
    pairs, unmapped = N.candidate_pairs(stores_by_city, N.positions(stores_by_city, lone), lone)
    assert unmapped == ["13", "22"]
    assert not [x for x in pairs if "21" in (x["origin"], x["dest"]) or "22" in (x["origin"], x["dest"])]
    assert {(x["origin"], x["dest"]) for x in pairs} == {("11", "12"), ("12", "11")}


def test_a_store_missing_from_the_mapping_gets_no_invented_route(stores_by_city):
    p = params(city_radius_km=10.0, max_travel_min=60, k=5, unmapped=frozenset())
    pos = N.positions(stores_by_city, p)
    del pos["12"]  # the mapping does not hold "12": no pair from or to it, and it is listed
    pairs, unmapped = N.candidate_pairs(stores_by_city, pos, p)
    assert unmapped == ["12"]
    assert all("12" not in (x["origin"], x["dest"]) for x in pairs)
    assert {(x["origin"], x["dest"]) for x in pairs} >= {("11", "13"), ("13", "11")}


def test_proximity_comes_from_the_mapping_never_from_the_codes():
    p = params(speed_kmh=60.0, handling_min=0, max_travel_min=100, k=1, unmapped=frozenset())
    # "12" is far from "11" and "19" is near it: the codes' closeness means nothing
    pos = {"11": (0.0, 0.0), "12": (50.0, 0.0), "19": (5.0, 0.0)}
    pairs = N.candidate_pairs({"1": ["11", "12", "19"]}, pos, p)[0]
    assert [x["dest"] for x in pairs if x["origin"] == "11"] == ["19"]
    swapped = {"11": (0.0, 0.0), "12": (5.0, 0.0), "19": (50.0, 0.0)}
    pairs = N.candidate_pairs({"1": ["11", "12", "19"]}, swapped, p)[0]
    assert [x["dest"] for x in pairs if x["origin"] == "11"] == ["12"]


def test_run_rows_have_the_seed_row_keys_one_per_pair_and_run_day(stores_by_city):
    p = params(city_radius_km=10.0, max_travel_min=60, run_days=(0, 2, 3))
    pos = N.positions(stores_by_city, p)
    pairs = N.candidate_pairs(stores_by_city, pos, p)[0]
    rows = N.run_rows(pairs, p)
    assert len(rows) == len(pairs) * len(p.run_days)
    assert rows == sorted(rows, key=lambda r: (r["run_code"], r["day"]))
    by_pair = {(x["origin"], x["dest"]): x for x in pairs}
    per_day: dict = {}
    for r in rows:
        assert set(r) == RUN_KEYS
        pair = by_pair[(r["origin"], r["dest"])]
        assert r["run_code"] == f"R-{r['origin']}-{r['dest']}"
        assert r["day"] in p.run_days
        assert r["arrives_min"] == p.departs_min + pair["travel_min"] and r["travel_min"] == pair["travel_min"]
        assert (r["loading_cutoff_min"], r["departs_min"], r["spare_totes"]) == (720, 750, 20)
        assert r["fixed_cost"] == round(p.fixed_cost_base + p.per_km_cost * pair["km"], 2)
        assert r["per_unit_cost"] == p.per_unit_cost and r["cancel_cost"] is None  # null = the full quote
        assert isinstance(r["cold_chain"], bool)
        per_day[(r["origin"], r["day"])] = per_day.get((r["origin"], r["day"]), 0) + 1
    assert max(per_day.values()) <= p.k  # at most k runs leave a store per day
    cold = {r["run_code"]: r["cold_chain"] for r in rows}
    assert {r["run_code"]: r["cold_chain"] for r in N.run_rows(pairs, p)} == cold  # keyed, repeatable
    assert N.run_rows(pairs, dataclasses.replace(p, cold_chain_share=1.0))[0]["cold_chain"] is True
    assert N.run_rows(pairs, dataclasses.replace(p, cold_chain_share=0.0))[0]["cold_chain"] is False
    quoted = N.run_rows(pairs, dataclasses.replace(p, cancel_cost=4.0))
    assert {r["cancel_cost"] for r in quoted} == {4.0}


def test_transport_cost_never_filters_a_destination(stores_by_city):
    cheap = params(city_radius_km=10.0, max_travel_min=60, fixed_cost_base=0.0, per_km_cost=0.0, per_unit_cost=0.0)
    dear = dataclasses.replace(cheap, fixed_cost_base=500.0, per_km_cost=90.0, per_unit_cost=7.5)

    def network(p):
        pos = N.positions(stores_by_city, p)
        pairs, unmapped = N.candidate_pairs(stores_by_city, pos, p)
        return N.run_rows(pairs, p), unmapped

    (rows_cheap, un_cheap), (rows_dear, un_dear) = network(cheap), network(dear)
    assert rows_cheap
    assert {(r["origin"], r["dest"], r["day"]) for r in rows_cheap} == \
        {(r["origin"], r["dest"], r["day"]) for r in rows_dear}
    assert un_cheap == un_dear
    # only the cost fields differ
    cost = ("fixed_cost", "per_unit_cost")
    strip = lambda rows: [{k: v for k, v in r.items() if k not in cost} for r in rows]  # noqa: E731
    assert strip(rows_cheap) == strip(rows_dear)
    assert [r["fixed_cost"] for r in rows_cheap] != [r["fixed_cost"] for r in rows_dear]


def test_capacity_covers_every_store_zone_and_day_unmapped_stores_included(stores_by_city):
    p = params()
    stores = [s for c in sorted(stores_by_city) for s in stores_by_city[c]]
    rows = N.capacity_rows(stores, ("chilled", "ambient"), range(0, 7), p)
    assert len(rows) == 5 * 2 * 7
    assert {r["branch"] for r in rows} == {"11", "12", "13", "21", "22"}  # "13" keeps capacity (D-31)
    for r in rows:
        assert set(r) == {"branch", "zone", "day", "free_totes"}
        assert r["free_totes"] == p.capacity_totes[r["zone"]]
    assert rows == sorted(rows, key=lambda r: (r["branch"], r["zone"], r["day"]))


def test_bad_params_are_refused(stores_by_city):
    with pytest.raises(ValueError, match="k"):
        N.candidate_pairs(stores_by_city, {}, params(k=-1))
    with pytest.raises(ValueError, match="speed_kmh"):
        N.travel_min((0.0, 0.0), (1.0, 0.0), params(speed_kmh=0.0))
    with pytest.raises(ValueError, match="zone"):
        N.capacity_rows(["11"], ("frozen",), range(0, 1), params())
