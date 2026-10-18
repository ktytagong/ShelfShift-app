"""The stock stage on a checked FreshRetailNet-schema file (05.2-08; DATA-08, DATA-11; D-08, D-15, D-16, D-18, D-20).

The series come from tests/data/fixtures/frn_sample_synthetic.csv (a synthetic test fixture in the FreshRetailNet-50K
schema; not FreshRetailNet rows): 5 stores ("11", "12", "13" in city "1", "21", "22" in city "2"), 2 products ("101",
"102"), days -42..6 (42 train days and the 7 eval days), so 10 series and 490 series-days. Every value the stage uses
comes from the reference through `data.public_config.load_for` (the fixture's dataset and calendar blocks laid over
reference/public.json). The designed example is set on the loaded configuration here, as test data only: the release
reference does not set one (the user decides at 05.2-18, D-40).
"""
import copy
import csv
import gzip
import io
import json
import pathlib
from collections import Counter

import pytest

from data import public as U
from data import public_build as B
from data import public_config as P
from data import stock as K

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
STOCK_PARTS = ("sales", "weather", "products", "lots0", "inbound0", "truth_physical", "truth_demand")
DESIGNED = {"store": "12", "product": "101", "rule": "test data: one series with physical 0 from day 0",
            "label": "Designed example (test fixture)"}
N_STORES, N_PRODUCTS, N_DAYS, N_EVAL_DAYS = 5, 2, 49, 7  # the fixture: 42 train days + 7 eval days


@pytest.fixture(scope="module")
def cfg():
    # the plain build: the fixture's override file names a designed example (05.2-10, for the tiny set); these tests
    # lay one on explicitly in designed_cfg, so the plain configuration leaves it out
    out = P.load_for(SIDECAR)
    out.pop("designed", None)
    return out


@pytest.fixture(scope="module")
def designed_cfg(cfg):
    out = copy.deepcopy(cfg)
    out["designed"] = {"verify_example": dict(DESIGNED)}
    return out


def run(cfg, out: pathlib.Path) -> dict:
    return B.stock_stage(U.series_iter(None, cfg, csv_path=CSV), cfg, out)


@pytest.fixture(scope="module")
def plain(cfg, tmp_path_factory):
    out = tmp_path_factory.mktemp("plain")
    return out, run(cfg, out)


@pytest.fixture(scope="module")
def designed(designed_cfg, tmp_path_factory):
    out = tmp_path_factory.mktemp("designed")
    return out, run(designed_cfg, out)


def part(out: pathlib.Path, name: str) -> tuple[list[str], list[dict]]:
    """(header, rows as dicts of text) of one part file."""
    raw = gzip.decompress((out / "parts" / f"{name}.csv.gz").read_bytes()).decode("utf-8")
    assert "\r" not in raw  # LF line ends
    reader = csv.reader(io.StringIO(raw))
    header = next(reader)
    return header, [dict(zip(header, line)) for line in reader]


def day_rows(cfg) -> list[dict]:
    return list(U.day_rows(U.read_csv(CSV, cfg["dataset"]["columns"]), cfg))


def S(cfg, raw):
    return P.code(cfg, "store", raw)


def SKU(cfg, raw):
    return P.code(cfg, "product", raw)


def test_every_part_has_its_header_row_and_sales_cover_every_series_day(cfg, plain):
    out, _ = plain
    for name in STOCK_PARTS:
        header, _ = part(out, name)
        assert tuple(header) == B.PARTS[f"{name}.csv.gz"], name
    _, sales = part(out, "sales")
    assert len(sales) == N_STORES * N_PRODUCTS * N_DAYS
    factor = cfg["units"]["factor"]
    by_key = {(S(cfg, r["store"]), SKU(cfg, r["product"]), r["day"]): r for r in day_rows(cfg)}
    for row in sales:
        src = by_key[(row["store"], row["sku"], int(row["day"]))]
        # units = sale_amount x the units factor, rounded once to 6 places (D-20); the public value is kept beside it
        assert float(row["units"]) == round(src["sale_amount"] * factor, 6), row
        assert float(row["sale_amount"]) == src["sale_amount"], row
        assert row["stockout"] == ("true" if src["stockout"] else "false"), row
        assert row["unknown_price"] == ("true" if src["unknown_price"] else "false"), row


def test_weather_is_one_row_per_store_day_and_the_first_product_wins_a_disagreement(cfg, plain):
    out, report = plain
    _, weather = part(out, "weather")
    assert len(weather) == N_STORES * N_DAYS
    first, conflicts = {}, set()
    for r in sorted(day_rows(cfg), key=lambda r: (int(r["store"]), int(r["product"]), r["day"])):
        key = (r["store"], r["day"])
        values = (r["precip"], r["temp"], r["humidity"], r["wind"])
        if key not in first:
            first[key] = values
        elif values != first[key]:
            conflicts.add(key)
    for row in weather:
        raw = P.raw_id(cfg, "store", row["store"])
        assert tuple(float(row[c]) for c in ("precip", "temp", "humidity", "wind")) == first[(raw, int(row["day"]))]
    assert report["weather_conflicts"] == len(conflicts)
    assert "first product in code order" in report["weather_rule"]


def test_products_carry_codes_and_the_category_assumptions_never_a_name(cfg, plain):
    out, _ = plain
    _, products = part(out, "products")
    assert [p["sku"] for p in products] == [SKU(cfg, "101"), SKU(cfg, "102")]
    row = cfg["categories"]["default"]
    for p in products:
        assert p["name"] == ""  # D-02: no invented name
        assert p["category"] == P.code(cfg, "category", p[cfg["categories"]["level"]])
        assert float(p["price"]) == row["price"] and p["zone"] == row["zone"]
        assert int(p["shelf_life_days"]) == row["shelf_life_days"]
        assert json.loads(p["schedule"]) == [list(s) for s in P.schedule_for(cfg, {"mgmt": p["mgmt"]})]


def test_lots0_inbound0_and_truth_hold_what_they_should(cfg, plain):
    out, _ = plain
    _, lots0 = part(out, "lots0")
    _, inbound = part(out, "inbound0")
    _, physical = part(out, "truth_physical")
    _, demand = part(out, "truth_demand")
    last = cfg["calendar"]["last_day"]
    assert lots0 and inbound
    for lot in lots0:  # received before day 0 (counted on receipt) and alive at day 0
        assert int(lot["last_verified_day"]) < 0 <= int(lot["expiry_day"]), lot
        assert lot["origin"] == K.ORIGIN and lot["tag"] == "", lot
    for row in inbound:  # only regular deliveries arriving on days 0..last_day (D-39: top-ups not delivered)
        assert 0 <= int(row["arrival_day"]) <= last, row
        assert row["inbound_id"].endswith("-" + K.KIND_CODE[K.REGULAR]), row
    ids = [r["lot_id"] for r in lots0] + [r["inbound_id"] for r in inbound]
    assert len(set(ids)) == len(ids)
    assert sorted(r["lot_id"] for r in physical) == sorted(ids)  # one truth row per lot and delivery id
    # D-15: the held-out demand of days 0..last_day is the public eval units
    units = {(S(cfg, r["store"]), SKU(cfg, r["product"]), r["day"]): round(r["sale_amount"] * cfg["units"]["factor"], 6)
             for r in day_rows(cfg) if r["day"] >= 0}
    assert len(demand) == N_STORES * N_PRODUCTS * N_EVAL_DAYS
    assert {(r["store"], r["sku"], int(r["day"])): float(r["demand"]) for r in demand} == units


def test_running_twice_gives_byte_identical_parts(cfg, plain, tmp_path):
    out, _ = plain
    run(cfg, tmp_path)
    for name in STOCK_PARTS:
        assert (tmp_path / "parts" / f"{name}.csv.gz").read_bytes() == \
            (out / "parts" / f"{name}.csv.gz").read_bytes(), name


def test_the_report_holds_the_invariant_and_the_sums_over_the_series(cfg, plain):
    out, report = plain
    on_disk = json.loads((out / "reports" / "public-consistency.json").read_text(encoding="utf-8"))
    assert on_disk == report
    assert report["sold_equals_public"] is True and report["series"] == N_STORES * N_PRODUCTS
    assert report["designed_series"] == 0
    sums = dict.fromkeys(K.COUNTERS, 0)
    short = 0
    for (city, store, product, cats), rows in U.series_iter(None, cfg, csv_path=CSV):
        prow = {"product": product, **dict(zip(U.CATEGORY_LEVELS, cats))}
        st = K.simulate(S(cfg, store), SKU(cfg, product), [r["day"] for r in rows],
                        [round(r["sale_amount"] * cfg["units"]["factor"], 6) for r in rows],
                        [r["stockout"] for r in rows], P.stock_params(cfg, prow))
        for k in K.COUNTERS:
            sums[k] += st.counters[k]
        short += sum(1 for h in st.history if h["day"] >= 0 and h["topup"] > 0)
    for k in ("error_lots", "sim_stockout_days", "public_stockout_days", "agree_stockout_days"):
        assert report[k] == sums[k], k
    assert report["topup_share"] == round(sums["topup_units"] / (sums["topup_units"] + sums["regular_units"]), 4)
    assert report["eval_days_short_without_topup"]["series_days"] == short
    assert report["unknown_price_rows"] == sum(r["unknown_price"] for r in day_rows(cfg))
    assert sum(c["wasted_units"] for c in report["waste_by_category"].values()) == pytest.approx(
        sums["wasted_units"], abs=1e-3)
    assert "reported, not tuned" in report["note"] and "never observed" in report["note"]
    for key in ("peak_rss_mb", "seconds"):
        assert isinstance(report[key], (int, float)), key


def test_a_designed_example_has_physical_zero_from_day0_its_record_unchanged_and_is_counted_apart(
        cfg, plain, designed):
    out_p, _ = plain
    out_d, report = designed
    store, sku = S(cfg, DESIGNED["store"]), SKU(cfg, DESIGNED["product"])
    assert report["designed_series"] == 1 and report["sold_equals_public"] is True
    assert report["series"] == N_STORES * N_PRODUCTS
    phys_p = {r["lot_id"]: r["qty"] for r in part(out_p, "truth_physical")[1]}
    phys_d = {r["lot_id"]: r["qty"] for r in part(out_d, "truth_physical")[1]}
    assert phys_p.keys() == phys_d.keys()
    for name, id_col in (("lots0", "lot_id"), ("inbound0", "inbound_id")):
        rows_p, rows_d = part(out_p, name)[1], part(out_d, name)[1]
        assert len(rows_p) == len(rows_d)
        mine = 0
        for a, b in zip(rows_d, rows_p):
            if (a["store"], a["sku"]) == (store, sku):
                mine += 1
                if "tag" in a:  # lots0 carries a tag; inbound0 carries the origin only
                    assert a["tag"] == "designed", a
                assert a["origin"] == "designed" and a["qty"] == b["qty"], a  # the record is unchanged
                assert float(phys_d[a[id_col]]) == 0, a  # nothing on the shelf from day 0
                assert {k: v for k, v in a.items() if k not in ("tag", "origin")} == \
                    {k: v for k, v in b.items() if k not in ("tag", "origin")}
            else:  # every other series' rows are identical to the build without it
                assert a == b and phys_d[a[id_col]] == phys_p[a[id_col]], a
        assert mine > 0, name
    for name in ("sales", "weather", "products", "truth_demand"):
        assert (out_d / "parts" / f"{name}.csv.gz").read_bytes() == (out_p / "parts" / f"{name}.csv.gz").read_bytes()
    assert report["designed"]["series"] == [[store, sku]] and report["designed"]["label"] == DESIGNED["label"]


def test_a_designed_example_naming_no_series_of_the_data_is_refused(cfg, tmp_path):
    bad = copy.deepcopy(cfg)
    bad["designed"] = {"verify_example": {**DESIGNED, "store": "99"}}
    with pytest.raises(B.PublicBuildError, match="designed.verify_example"):
        run(bad, tmp_path)


def test_a_series_that_does_not_sell_the_public_units_stops_the_stage_naming_it(cfg, tmp_path, monkeypatch):
    real = K.simulate

    def broken(store, product, days, units, stockout, params, **kw):
        st = real(store, product, days, units, stockout, params, **kw)
        if (store, product) == (S(cfg, "21"), SKU(cfg, "102")):
            st.history[-3]["sold"] -= 1  # one history day sells one unit less than the public units
        return st

    monkeypatch.setattr(B.stock, "simulate", broken)
    with pytest.raises(B.PublicBuildError, match=r"S21.*P102|store S21 product P102"):
        run(cfg, tmp_path)


def test_the_stock_command_writes_the_parts_and_the_committed_report(tmp_path):
    report = tmp_path / "committed" / "public-consistency.json"
    assert B.main(["stock", "--csv", str(CSV), "--fixture", str(SIDECAR), "--out", str(tmp_path / "out"),
                   "--report", str(report)]) == 0
    for name in STOCK_PARTS:
        assert (tmp_path / "out" / "parts" / f"{name}.csv.gz").stat().st_size > 0, name
    assert json.loads(report.read_text(encoding="utf-8"))["sold_equals_public"] is True
    assert (tmp_path / "out" / "reports" / "public-consistency.json").read_bytes() == report.read_bytes()


# --------------------------------------------------------------------------------------------------- network stage
# The fixture geography leaves store "13" unmapped (test data only; reference/geography.json maps every store, D-34).
# Cities: "1" holds stores 11, 12, 13 and "2" holds 21, 22, so the mapped sources are 11, 12, 21 and 22. With the
# reference positions their travel times are 11-12 63 min and 21-22 69 min (11-13 49, 12-13 43), all over the
# reference 60 min limit once 13 is out, so the fixture network also sets its own service limit of 90 min (test data
# only): both mapped pairs are then in, and 13 still gets no run.
FIXTURE_MAX_TRAVEL_MIN = 90
NETWORK_PARTS = ("stores", "runs", "capacity")


@pytest.fixture(scope="module")
def net_cfg(cfg):
    out = copy.deepcopy(cfg)
    out["geography"]["coverage"]["unmapped"] = ["13"]
    out["geography"]["service"]["max_travel_min"] = FIXTURE_MAX_TRAVEL_MIN
    return out


def run_network(cfg, out: pathlib.Path) -> dict:
    return B.network_stage(B.stores_by_city(None, cfg, csv_path=CSV), cfg, out)


@pytest.fixture(scope="module")
def network(net_cfg, tmp_path_factory):
    out = tmp_path_factory.mktemp("network")
    return out, run_network(net_cfg, out)


def test_stores_by_city_reads_the_codes_of_the_checked_file(cfg):
    assert {c: sorted(s) for c, s in B.stores_by_city(None, cfg, csv_path=CSV).items()} == {
        "1": ["11", "12", "13"], "2": ["21", "22"]}


def test_stores_carry_the_geography_flag_and_an_unmapped_store_is_in_no_run(net_cfg, network):
    out, _ = network
    for name in NETWORK_PARTS:
        assert tuple(part(out, name)[0]) == B.PARTS[f"{name}.csv.gz"], name
    _, stores = part(out, "stores")
    assert {r["store"]: (r["city"], r["geo_known"]) for r in stores} == {
        S(net_cfg, s): (P.code(net_cfg, "city", c), "false" if s == "13" else "true")
        for c, ss in (("1", ("11", "12", "13")), ("2", ("21", "22"))) for s in ss}
    _, runs = part(out, "runs")
    assert runs
    assert S(net_cfg, "13") not in {r["origin"] for r in runs} | {r["dest"] for r in runs}


def test_runs_stay_in_one_city_and_never_exceed_k_per_store_and_day(net_cfg, network):
    out, report = network
    city = {r["store"]: r["city"] for r in part(out, "stores")[1]}
    _, runs = part(out, "runs")
    k = net_cfg["geography"]["runs"]["k"]
    per = Counter((r["origin"], r["day"]) for r in runs)
    assert max(per.values()) <= k and report["max_runs_per_store_per_day"] == max(per.values()) <= report["k"] == k
    for r in runs:
        assert r["origin"] != r["dest"] and city[r["origin"]] == city[r["dest"]], r
        assert int(r["travel_min"]) <= net_cfg["geography"]["service"]["max_travel_min"], r
        assert int(r["day"]) in net_cfg["geography"]["runs"]["days"], r
        assert r["run_code"] == f"R-{r['origin']}-{r['dest']}", r
    assert sorted(report["pairs_per_source"]) == ["max", "median", "min"]


def test_capacity_covers_every_store_zone_and_day_to_the_horizon_end(net_cfg, network):
    out, report = network
    _, capacity = part(out, "capacity")
    cats = net_cfg["categories"]
    rows = [cats["default"], *({**cats["default"], **o} for o in cats["overrides"].values())]
    horizon_end = net_cfg["calendar"]["last_demo_day"] + max(r["shelf_life_days"] for r in rows)
    zones = sorted({r["zone"] for r in rows})
    stores = sorted(S(net_cfg, s) for s in ("11", "12", "13", "21", "22"))  # the unmapped store "13" included
    assert report["horizon_end"] == horizon_end
    assert sorted((r["store"], r["zone"], int(r["day"])) for r in capacity) == sorted(
        (s, z, d) for s in stores for z in zones for d in range(0, horizon_end + 1))
    for r in capacity:
        assert int(r["free_totes"]) == net_cfg["geography"]["capacity"][r["zone"]], r


def test_the_network_report_counts_the_unmapped_stores_and_says_it_is_assumed(net_cfg, network):
    out, report = network
    assert report["unmapped_stores"] == len(net_cfg["geography"]["coverage"]["unmapped"]) == 1
    assert report["unmapped"] == [S(net_cfg, "13")]
    assert report["stores"] == 5 and report["cities"] == 2
    assert "not observed" in report["note"] and "excluded from transfers and counted" in report["note"]
    assert json.loads((out / "reports" / "public-network.json").read_text(encoding="utf-8")) == report
    assert 0 <= report["cold_chain_share"] <= 1


def test_network_parts_are_byte_identical_across_runs(net_cfg, network, tmp_path):
    out, _ = network
    run_network(net_cfg, tmp_path)
    for name in NETWORK_PARTS:
        assert (tmp_path / "parts" / f"{name}.csv.gz").read_bytes() == \
            (out / "parts" / f"{name}.csv.gz").read_bytes(), name


def test_a_store_listed_in_two_cities_is_refused(net_cfg, tmp_path):
    with pytest.raises(B.PublicBuildError, match="store 12"):
        B.network_stage({"1": ["11", "12"], "2": ["12", "21"]}, net_cfg, tmp_path)


def test_the_network_command_writes_the_parts_and_the_committed_report(tmp_path):
    report = tmp_path / "committed" / "public-network.json"
    assert B.main(["network", "--csv", str(CSV), "--fixture", str(SIDECAR), "--out", str(tmp_path / "out"),
                   "--report", str(report)]) == 0
    for name in NETWORK_PARTS:
        assert (tmp_path / "out" / "parts" / f"{name}.csv.gz").stat().st_size > 0, name
    r = json.loads(report.read_text(encoding="utf-8"))
    # the fixture's own geography (its override file, 05.2-10) leaves store 13 unmapped; the reference maps every
    # store (D-34), which test_network_params_equal_the_d34_values checks
    assert r["unmapped_stores"] == 1 and r["unmapped"] == ["S13"] and r["stores"] == 5
    assert (tmp_path / "out" / "reports" / "public-network.json").read_bytes() == report.read_bytes()
