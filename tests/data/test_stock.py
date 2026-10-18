"""Simulated stock from public sales (05.2-04; DATA-11; D-16, D-18).

The public units are never changed: every simulated day sells exactly the public units (D-16). The series come from
tests/data/fixtures/frn_sample_synthetic.csv (a synthetic test fixture in the FreshRetailNet-50K schema; not
FreshRetailNet rows; units = sale_amount / the generator's STEP) and from 20 random series drawn from keyed seeds.
Every StockParams value below is a test value, not a reference value.

Hand derivation (test_a_ten_day_series_matches_the_hand_derivation). Store "11", product "101", days -7..2, units
4 6 5 0 7 3 | 2 5 4 6 (day 0 after the bar), no stockout flag. Deliveries every day (cycle 1, so the phase draw is
randrange(1) = 0), shelf life 1 (a lot received on day d sells on d and d+1, is written off at the end of d+1), order
case 6, multiplier (1.0, 1.0), same_weekday 1, lookback 7, no record error, schedule ((0, 30),) (30% off on the expiry
day only, so the expiring lot sells first, as FEFO would). E(d) is the estimate for day d itself:

  day  E (rule)                         regular  top-up  sells from                       waste  end
  -7   no record -> 0 (init_failed)      0        6       top-up 4 (2 left)                 0      2
  -6   lookback -7: 4                    6        0       top-up 2, lot(-6) 4               0      2
  -5   lookback -7..-6: 5                6        0       lot(-6) 2, lot(-5) 3              0      3
  -4   lookback -7..-5: 5                6        0       nothing (0 units)                 3      6
  -3   lookback -7..-4: 15/4 -> 3        6        0       lot(-4) 6, lot(-3) 1              0      5
  -2   lookback -7..-3: 22/5 -> 4        6        0       lot(-3) 3                         2      6
  -1   lookback -7..-2: 25/6 -> 4        6        0       lot(-2) 2                         4      6
   0   same weekday -7: 4                6        0       lot(-1) 5                         1      6
   1   same weekday -6: 6                6        0       lot(0) 4                          2      6
   2   same weekday -5: 5                6        0       lot(1) 6                          0      6

Any floor(E) of 1..6 orders one case of 6. Received 6 + 9 x 6 = 60 = sold 42 + waste 12 + end 6. At the start of day
0 only lot(-1) is on hand (6 recorded, 6 physical, expiry 0, at full price: its 30% step starts on day 0); the
inbound is lot(0), lot(1), lot(2), 6 each; no eval-day top-up.
"""
import dataclasses
import importlib.util
import json
import math
import os
import pathlib
import random
import subprocess
import sys
from collections import defaultdict

import pytest

from data import public as U
from data import public_config as P
from data import stock as K
from engine.model import Product

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
CSV, SIDECAR = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
ROOT = pathlib.Path(__file__).resolve().parents[2]
TOL = 1e-6
PRODUCT = Product("101", "chilled", 2.0, 0.5, 12, 1.0)


def params(**changes) -> K.StockParams:
    base = dict(product=PRODUCT, schedule=((1, 20), (0, 30)), shelf_life_days=3, cycle_days=2, order_case=6,
                multiplier=(0.9, 1.0), lookback_days=28, same_weekday_obs=4, error_share=0.1,
                error_range=(0.6, 0.95), error_from_day=-15, expiry_estimated_share=0.2, first_day=-42, last_day=6,
                delivery_min=360, seed=7)
    base.update(changes)
    return K.StockParams(**base)


def _generator():
    spec = importlib.util.spec_from_file_location("make_frn_sample", FIX / "make_frn_sample.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen


@pytest.fixture(scope="module")
def fixture_series():
    """{(store, product): (days, units, stockout)} from the synthetic fixture, in day order."""
    cfg = P.load_for(SIDECAR)
    step = _generator().STEP
    by_key = defaultdict(list)
    for row in U.day_rows(U.read_csv(CSV, cfg["dataset"]["columns"]), cfg):
        by_key[(row["store"], row["product"])].append(row)
    out = {}
    for key, rows in sorted(by_key.items()):
        rows.sort(key=lambda r: r["day"])
        out[key] = ([r["day"] for r in rows], [round(r["sale_amount"] / step) for r in rows],
                    [r["stockout"] for r in rows])
    return out


def random_series(i: int):
    """(store, product, days, units, stockout, params) for random series i: 30..97 days ending on day 0..6, a level
    in 0..200, about one day in eight flagged out of stock (selling less), and every odd series in fractional units."""
    rng = random.Random(f"test-stock:{i}")
    n, last = rng.randint(30, 97), rng.randint(0, 6)
    first = last - n + 1
    level = rng.uniform(0, 200)
    units, stockout = [], []
    for _ in range(n):
        out = rng.random() < 0.125
        u = min(200.0, max(0.0, level * rng.uniform(0.3, 1.7) * (rng.uniform(0, 0.6) if out else 1.0)))
        units.append(round(u, 1) if i % 2 else float(round(u)))
        stockout.append(out)
    lo = rng.uniform(0.7, 1.0)
    p = params(shelf_life_days=rng.randint(1, 5), cycle_days=rng.randint(1, 4), order_case=rng.randint(1, 12),
               multiplier=(lo, lo + rng.uniform(0, 0.2)), error_share=0.3, error_from_day=rng.randint(first, last),
               expiry_estimated_share=0.3, first_day=first, last_day=last, seed=i,
               same_weekday_obs=rng.randint(1, 4), lookback_days=rng.randint(7, 28))
    return f"S{i}", f"P{i}", list(range(first, last + 1)), units, stockout, p


def all_lots(res: K.SeriesStock) -> list[dict]:
    seen = {}
    for lot in (*res.lots_day0, *res.inbound, *res.eval_topups):
        seen[lot["lot_id"]] = lot
    return list(seen.values())


def check_invariants(res: K.SeriesStock, units, p: K.StockParams) -> None:
    assert [h["day"] for h in res.history] == list(range(p.first_day, p.last_day + 1))
    for h, u in zip(res.history, units):
        assert h["units"] == u, h  # the public value is carried unchanged
        assert abs(h["sold"] - u) <= TOL, h  # D-16: simulated sold == public units
        assert h["lost"] <= TOL, h
        assert h["end_stock"] >= -TOL, h
        # whole order cases; 0 orders nothing
        assert h["regular"] % p.order_case == 0 and h["topup"] % p.order_case == 0, h
        short = u - h["start_stock"]
        if h["topup"]:  # only on a day the physical shelf would fall short, and the fewest cases that cover it
            assert short > TOL and h["topup"] >= short - TOL and h["topup"] - p.order_case < short, h
        else:
            assert short <= TOL, h
    for lot in all_lots(res):
        assert lot["qty"] >= 0 and lot["physical"] >= 0, lot
        assert lot["physical"] <= lot["qty"] + TOL, lot


def test_simulated_sales_equal_public_units_every_history_day(fixture_series):
    assert len(fixture_series) == 10  # 5 stores x 2 products
    for (store, product), (days, units, stockout) in fixture_series.items():
        p = params(first_day=days[0], last_day=days[-1])
        res = K.simulate(store, product, days, units, stockout, p)
        check_invariants(res, units, p)
    for i in range(20):
        store, product, days, units, stockout, p = random_series(i)
        res = K.simulate(store, product, days, units, stockout, p)
        check_invariants(res, units, p)
        assert [h["public_stockout"] for h in res.history] == stockout


def test_a_ten_day_series_matches_the_hand_derivation():
    p = params(schedule=((0, 30),), shelf_life_days=1, cycle_days=1, multiplier=(1.0, 1.0), lookback_days=7,
               same_weekday_obs=1, error_share=0.0, expiry_estimated_share=0.0, first_day=-7, last_day=2)
    units = [4, 6, 5, 0, 7, 3, 2, 5, 4, 6]
    res = K.simulate("11", "101", list(range(-7, 3)), units, [False] * 10, p)
    assert [h["regular"] for h in res.history] == [0, 6, 6, 6, 6, 6, 6, 6, 6, 6]
    assert [h["topup"] for h in res.history] == [6, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert [h["wasted"] for h in res.history] == pytest.approx([0, 0, 0, 3, 0, 2, 4, 1, 2, 0])
    assert [h["end_stock"] for h in res.history] == pytest.approx([2, 2, 3, 6, 5, 6, 6, 6, 6, 6])
    assert [h["sim_stockout"] for h in res.history] == [False] * 10
    assert res.counters == {"topup_units": 6, "regular_units": 54, "wasted_units": pytest.approx(12),
                            "error_lots": 0, "sim_stockout_days": 0, "public_stockout_days": 0,
                            "agree_stockout_days": 0}
    assert res.lots_day0 == [{"lot_id": "11-101--1-r", "branch": "11", "sku": "101", "kind": "regular",
                              "qty": 6.0, "physical": 6.0, "received_day": -1, "expiry_day": 0,
                              "received_seq": res.lots_day0[0]["received_seq"], "markdown_pct": 0,
                              "expiry_source": "PRINTED", "last_verified_day": -1, "origin": "simulated"}]
    assert [(i["lot_id"], i["qty"], i["physical"], i["arrival_day"], i["arrival_min"], i["expiry_day"])
            for i in res.inbound] == [("11-101-0-r", 6.0, 6.0, 0, 360, 1), ("11-101-1-r", 6.0, 6.0, 1, 360, 2),
                                      ("11-101-2-r", 6.0, 6.0, 2, 360, 3)]
    assert res.eval_topups == []


def test_record_errors_follow_the_stated_rule(fixture_series):
    days, units, stockout = fixture_series[("12", "101")]
    none = K.simulate("12", "101", days, units, stockout,
                      params(error_share=0.0, first_day=days[0], last_day=days[-1]))
    assert none.counters["error_lots"] == 0
    assert all(lot["physical"] == lot["qty"] for lot in all_lots(none))
    # share 1 from day 2: every regular lot received on or after day 2 is short, within floor(q x range); the lots
    # received before day 2 (the day-0 lots, the day-0 and day-1 deliveries) and every top-up are not
    lo, hi = 0.6, 0.95
    every = K.simulate("12", "101", days, units, stockout,
                       params(error_share=1.0, error_range=(lo, hi), error_from_day=2, first_day=days[0],
                              last_day=days[-1]))
    short = clean = 0
    for lot in all_lots(every):
        if lot["kind"] == "regular" and lot["received_day"] >= 2:
            q = lot["qty"]
            assert lot["physical"] < q and math.floor(q * lo) <= lot["physical"] <= math.floor(q * hi), lot
            short += 1
        else:
            assert lot["physical"] == lot["qty"], lot
            clean += 1
    assert short > 0 and clean > 0  # both sides of the rule are exercised
    assert every.counters["error_lots"] == short  # no lot received on day 2 or later has expired by day 6


def test_expiry_is_receipt_plus_shelf_life_and_received_seq_follows_receipt(fixture_series):
    for (store, product), (days, units, stockout) in fixture_series.items():
        p = params(first_day=days[0], last_day=days[-1])
        lots = all_lots(K.simulate(store, product, days, units, stockout, p))
        assert lots
        for lot in lots:
            assert lot["expiry_day"] == lot["received_day"] + p.shelf_life_days, lot
            assert lot["last_verified_day"] == lot["received_day"], lot  # counted on receipt
            assert lot["origin"] == "simulated" and lot["expiry_source"] in ("PRINTED", "ESTIMATED"), lot
        by_seq = sorted(lots, key=lambda lot: lot["received_seq"])
        assert len({lot["received_seq"] for lot in lots}) == len(lots)
        assert [lot["received_day"] for lot in by_seq] == sorted(lot["received_day"] for lot in lots)


def test_day0_lots_inbound_and_eval_topups_are_kept_apart(fixture_series):
    days, units, stockout = fixture_series[("12", "101")]  # the fixture's eval-week shortfall store
    p = params(first_day=days[0], last_day=days[-1])
    res = K.simulate("12", "101", days, units, stockout, p)
    assert res.lots_day0 and res.inbound and res.eval_topups
    for lot in res.lots_day0:
        assert lot["received_day"] < 0 <= lot["expiry_day"], lot
    for row in res.inbound:
        assert row["kind"] == "regular" and 0 <= row["arrival_day"] <= p.last_day, row
        assert row["arrival_day"] == row["received_day"] and row["arrival_min"] == p.delivery_min, row
    for row in res.eval_topups:
        assert row["kind"] == "topup" and 0 <= row["received_day"] <= p.last_day, row
    by_day = {h["day"]: h for h in res.history}
    assert sum(r["qty"] for r in res.eval_topups) == sum(by_day[d]["topup"] for d in range(0, p.last_day + 1))
    # D-39 may deliver them to the worlds: then the inbound also carries them, and nothing else changes
    with_topups = K.simulate("12", "101", days, units, stockout, dataclasses.replace(p, topup_on_eval_days=True))
    assert with_topups.lots_day0 == res.lots_day0 and with_topups.history == res.history
    assert sorted(r["lot_id"] for r in with_topups.inbound) == sorted(
        [r["lot_id"] for r in res.inbound] + [r["lot_id"] for r in res.eval_topups])


def test_a_designed_series_has_physical_zero_from_day0_and_its_record_unchanged(fixture_series):
    """05.2-08 (D-18, D-40): `designed_physical_zero` simulates the series as usual, so its record (every lot's
    recorded quantity, expiry, sequence and the history) is the record of the run without it; only the physical
    quantity of every row it emits from day 0 on (lots on hand at the start of day 0, inbound, eval-day top-ups) is 0.
    Without the flag nothing changes (the default is False)."""
    days, units, stockout = fixture_series[("12", "101")]
    p = params(first_day=days[0], last_day=days[-1], topup_on_eval_days=True)
    plain = K.simulate("12", "101", days, units, stockout, p)
    assert K.simulate("12", "101", days, units, stockout, p, designed_physical_zero=False) == plain
    designed = K.simulate("12", "101", days, units, stockout, p, designed_physical_zero=True)
    assert designed.history == plain.history and designed.counters == plain.counters
    for name in ("lots_day0", "inbound", "eval_topups"):
        before, after = getattr(plain, name), getattr(designed, name)
        assert after and len(after) == len(before), name
        for a, b in zip(after, before):
            assert a["physical"] == 0 and a["qty"] == b["qty"] > 0, (name, a)
            assert {k: v for k, v in a.items() if k != "physical"} == {k: v for k, v in b.items() if k != "physical"}


def test_counters_report_stockout_agreement_beside_the_public_flag(fixture_series):
    for (store, product), (days, units, stockout) in fixture_series.items():
        res = K.simulate(store, product, days, units, stockout, params(first_day=days[0], last_day=days[-1]))
        h = res.history
        c = res.counters
        assert c["public_stockout_days"] == sum(stockout)
        assert c["sim_stockout_days"] == sum(r["sim_stockout"] for r in h)
        assert c["agree_stockout_days"] == sum(r["sim_stockout"] and r["public_stockout"] for r in h)
        assert c["topup_units"] == sum(r["topup"] for r in h)
        assert c["regular_units"] == sum(r["regular"] for r in h)
        assert c["wasted_units"] == pytest.approx(sum(r["wasted"] for r in h))
        for r in h:  # sold out = nothing left on the shelf after the day's sales
            assert r["sim_stockout"] == (r["end_stock"] + r["wasted"] <= TOL), r


def test_bad_inputs_are_refused():
    p = params(first_day=-2, last_day=1)
    with pytest.raises(ValueError, match="days"):
        K.simulate("11", "101", [-2, -1, 0], [1, 1, 1], [False] * 3, p)
    with pytest.raises(ValueError, match="units"):
        K.simulate("11", "101", [-2, -1, 0, 1], [1, -1, 1, 1], [False] * 4, p)
    with pytest.raises(ValueError, match="order_case"):
        K.simulate("11", "101", [-2, -1, 0, 1], [1, 1, 1, 1], [False] * 4, dataclasses.replace(p, order_case=0))
    with pytest.raises(ValueError, match="schedule"):
        K.simulate("11", "101", [-2, -1, 0, 1], [1, 1, 1, 1], [False] * 4,
                   dataclasses.replace(p, schedule=((0, 25),)))


SCRIPT = """
import dataclasses, json, random
from data import stock as K
from engine.model import Product
rng = random.Random("det")
units = [float(rng.randint(0, 40)) for _ in range(49)]
stockout = [rng.random() < 0.1 for _ in range(49)]
p = K.StockParams(product=Product("101", "chilled", 2.0, 0.5, 12, 1.0), schedule=((1, 20), (0, 30)),
                  shelf_life_days=3, cycle_days=2, order_case=6, multiplier=(0.9, 1.0), lookback_days=28,
                  same_weekday_obs=4, error_share=0.3, error_range=(0.6, 0.95), error_from_day=-15,
                  expiry_estimated_share=0.3, first_day=-42, last_day=6, delivery_min=360, seed=7)
res = K.simulate("11", "101", list(range(-42, 7)), units, stockout, p)
print(json.dumps(dataclasses.asdict(res), sort_keys=True))
"""


def test_output_is_the_same_under_any_pythonhashseed():
    outs = []
    for hashseed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hashseed}
        done = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True,
                              check=True)
        outs.append(done.stdout)
    assert outs[0] == outs[1]
    body = json.loads(outs[0])
    assert body["counters"]["error_lots"] > 0 and body["lots_day0"]  # the run is not vacuous
