"""Simulated stock from public sales (spec §5 Public dataset; DATA-11; 05.2 D-16, D-18).

One public series (a store code, a product code, its units per day and its stockout flags) becomes simulated stock:
the lots on hand at the start of day 0, the regular deliveries of days 0..last_day, and the physical quantity of every
lot. The public units are never changed (D-16): every simulated day sells exactly the public units, FEFO, through
`engine.world.step_day` with a unit price response (m20 = m30 = 1.0, so a day sells min(units, stock on sale)) and
the product's markdown schedule. `lost` is asserted to be 0 on every day; a RuntimeError names the store, the product
and the day otherwise.

Rules, each an Assumption the caller states in reference data:
- Regular deliveries arrive every `cycle_days`, the first on `first_day + phase`, the phase drawn per series in
  0..cycle_days-1. Each covers its own day .. day + cycle_days - 1 and is sized from the record only
  (`data.estimator.estimate` on the recorded units, which are the public units, and the public stockout flags, from
  `first_day` on, no opening value), times a multiplier drawn in `multiplier`, rounded down once, then up to whole
  order cases (`data.seed.regular_qty`; 0 orders nothing).
- Top-up: when the physical stock on sale after the morning's regular delivery would be below the day's public units,
  a top-up of the fewest whole order cases that cover the gap arrives the same morning (record = physical). This is
  the stated Assumption that keeps the public units unchanged; a shortfall is never covered by changing the units.
- Record errors (D-18, no scripted lot): a regular lot received on or after `error_from_day` is, with probability
  `error_share`, physically short: physical = floor(q × (lo + v × (hi − lo))) for v in [0, 1), the record stays q.
  Top-ups never carry an error.
- Expiry = receipt day + `shelf_life_days`. The expiry source is ESTIMATED with probability
  `expiry_estimated_share`, else PRINTED. Every lot is counted on receipt (last_verified_day = the receipt day).

Output (SeriesStock): `lots_day0` (lots received before day 0 and not yet expired, as at the start of day 0, before
its deliveries), `inbound` (the regular deliveries arriving on days 0..last_day), `eval_topups` (the top-ups of days
0..last_day, a report: they reach the worlds only when `topup_on_eval_days` is True, D-39, and then `inbound` carries
them too), `history` (per day: the public units, sold, lost, regular and top-up units, the stock on sale before the
top-up, waste, the stock left at the end of the day, the simulated sell-out beside the public stockout flag) and
`counters` over every simulated day. The agreement between simulated sell-outs and the public flag is reported, never
tuned (D-16).

A designed example (05.2-08; D-18, D-40: only when reference data names one, by the user's choice): with
`designed_physical_zero=True` the series is simulated exactly as usual, so its record (every lot's recorded quantity,
expiry and sequence, the history and the counters) is the record of the run without it; only the physical quantity of
every row emitted from day 0 on (the lots on hand at the start of day 0, the inbound and the eval-day top-ups) is 0: a
recorded quantity with nothing on the shelf. The caller tags those rows and counts the series apart.

Every draw comes from its own `random.Random` keyed by a string (kind, seed, store, product, day), the scheme of
`data.seed._rng`, so the output is the same under every PYTHONHASHSEED. No clock is read. No retailer value is a code
literal: every value is a StockParams field the caller fills from reference data (05.2-07).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from engine.allocator import EPS, ShelfLot
from engine.model import Product, schedule_problem
from engine.world import step_day

from . import estimator
from .seed import regular_qty

# The engine's two discount levels, each at a unit response: a day sells min(units, stock on sale) at any price.
UNIT_RESPONSE = {20: 1.0, 30: 1.0}
REGULAR, TOPUP = "regular", "topup"
KIND_CODE = {REGULAR: "r", TOPUP: "t"}
PRINTED, ESTIMATED = "PRINTED", "ESTIMATED"
ORIGIN = "simulated"
COUNTERS = ("topup_units", "regular_units", "wasted_units", "error_lots", "sim_stockout_days",
            "public_stockout_days", "agree_stockout_days")


def _rng(kind: str, seed: int, *key) -> random.Random:
    """A draw stream keyed by a string, so it is the same under every PYTHONHASHSEED (as data.seed._rng)."""
    return random.Random(f"{kind}:{seed}:" + ":".join(map(str, key)))


@dataclass(frozen=True, kw_only=True)
class StockParams:
    """Every value of the simulation; the caller fills each from reference data (05.2-07)."""

    product: Product
    schedule: tuple[tuple[int, int], ...]  # the product's markdown schedule, (days before expiry, pct) steps
    shelf_life_days: int
    cycle_days: int  # days between regular deliveries
    order_case: int  # units per order case
    multiplier: tuple[float, float]  # (lo, hi) × the in-stock estimate
    lookback_days: int
    same_weekday_obs: int
    error_share: float  # share of regular lots, from error_from_day on, that are physically short
    error_range: tuple[float, float]  # (lo, hi) physical ÷ record
    error_from_day: int
    expiry_estimated_share: float
    first_day: int  # the first simulated day; nothing is recorded before it
    last_day: int  # the last simulated day (the last eval day)
    delivery_min: int  # the minute of the day deliveries arrive
    seed: int
    topup_on_eval_days: bool = False  # D-39: eval-day top-ups are reported, not delivered, unless this is True


@dataclass(frozen=True)
class SeriesStock:
    store: str
    product: str
    lots_day0: list[dict]
    inbound: list[dict]
    eval_topups: list[dict]
    history: list[dict]
    counters: dict


def _check(params: StockParams) -> None:
    p = params
    reason = schedule_problem(p.schedule)
    if reason is not None:
        raise ValueError(f"schedule {p.schedule!r}: {reason}")
    whole = {"shelf_life_days": (p.shelf_life_days, 0), "cycle_days": (p.cycle_days, 1),
             "order_case": (p.order_case, 1), "lookback_days": (p.lookback_days, 0),
             "same_weekday_obs": (p.same_weekday_obs, 0)}
    for name, (value, low) in whole.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            raise ValueError(f"{name} must be a whole number of at least {low}, got {value!r}")
    lo, hi = p.multiplier
    if not 0 <= lo <= hi:
        raise ValueError(f"multiplier must be (lo, hi) with 0 <= lo <= hi, got {p.multiplier!r}")
    lo_e, hi_e = p.error_range
    if not 0 <= lo_e <= hi_e <= 1:
        raise ValueError(f"error_range must be (lo, hi) with 0 <= lo <= hi <= 1, got {p.error_range!r}")
    for name in ("error_share", "expiry_estimated_share"):
        if not 0 <= getattr(p, name) <= 1:
            raise ValueError(f"{name} must be in 0..1, got {getattr(p, name)!r}")
    if not p.first_day <= 0 <= p.last_day:
        raise ValueError(f"first_day {p.first_day} .. last_day {p.last_day} must include day 0")


def _check_series(days, units, stockout, params: StockParams) -> tuple[list[float], list[bool]]:
    expected = list(range(params.first_day, params.last_day + 1))
    if list(days) != expected:
        raise ValueError(f"days must be every day {params.first_day}..{params.last_day} in order")
    units, stockout = list(units), list(stockout)
    if len(units) != len(expected) or len(stockout) != len(expected):
        raise ValueError(f"units and stockout must have one value per day ({len(expected)})")
    for day, u in zip(expected, units):
        if isinstance(u, bool) or not isinstance(u, (int, float)) or not math.isfinite(u) or u < 0:
            raise ValueError(f"units on day {day} must be a finite number >= 0, got {u!r}")
    return [float(u) for u in units], [bool(s) for s in stockout]


def _row(meta: dict, lot: ShelfLot) -> dict:
    return {**meta, "qty": lot.recorded, "physical": lot.qty, "markdown_pct": lot.pct}


def simulate(store: str, product_code: str, days, units, stockout, params: StockParams,
             designed_physical_zero: bool = False) -> SeriesStock:
    """Simulated stock for one public series; sells exactly `units[i]` on `days[i]` (D-16). With
    `designed_physical_zero` every row emitted from day 0 on has physical 0, the record unchanged. See the module
    doc."""
    _check(params)
    units, stockout = _check_series(days, units, stockout, params)
    p, seed = params, params.seed
    first, last, cycle = p.first_day, p.last_day, p.cycle_days
    phase = _rng("phase", seed, store, product_code).randrange(cycle)
    delivery_days = set(range(first + phase, last + 1, cycle))
    rec: dict = {}  # the record: (store, product, day) -> units sold, which are the public units
    so: dict = {}  # the public stockout flags, same keys
    lots: list[ShelfLot] = []
    meta: dict[str, dict] = {}
    lots_day0: list[dict] = []
    inbound: list[dict] = []
    eval_topups: list[dict] = []
    history: list[dict] = []
    counters = dict.fromkeys(COUNTERS, 0)
    seq = 0

    def receive(kind: str, day: int, record: float, physical: float) -> ShelfLot:
        nonlocal seq
        lot_id = f"{store}-{product_code}-{day}-{KIND_CODE[kind]}"
        estimated = _rng("expiry", seed, store, product_code, day, kind).random() < p.expiry_estimated_share
        meta[lot_id] = {"lot_id": lot_id, "branch": store, "sku": product_code, "kind": kind, "received_day": day,
                        "expiry_day": day + p.shelf_life_days, "received_seq": seq,
                        "expiry_source": ESTIMATED if estimated else PRINTED, "last_verified_day": day,
                        "origin": ORIGIN}
        seq += 1
        return ShelfLot(lot_id, store, physical, day + p.shelf_life_days, meta[lot_id]["received_seq"],
                        recorded=record)

    for i, day in enumerate(range(first, last + 1)):
        if day == 0:  # the start of day 0: before its deliveries and its sales
            lots_day0 = [_row(meta[l.lot_id], l) for l in sorted(lots, key=lambda l: l.received_seq)
                         if l.qty > EPS or l.recorded > EPS]
        arrivals: list[ShelfLot] = []
        regular = 0
        if day in delivery_days:
            e_sum = sum(estimator.estimate(rec, so, store, product_code, x, day, first, None,
                                           lookback=p.lookback_days, same_weekday=p.same_weekday_obs)[0]
                        for x in range(day, day + cycle))
            regular = regular_qty(e_sum, _rng("u", seed, store, product_code, day).random(), p.multiplier,
                                  p.order_case)
            if regular > 0:
                physical = float(regular)
                if day >= p.error_from_day:
                    err = _rng("error", seed, store, product_code, day)
                    if err.random() < p.error_share:
                        lo_e, hi_e = p.error_range
                        physical = float(math.floor(regular * (lo_e + err.random() * (hi_e - lo_e))))
                if physical < regular:
                    counters["error_lots"] += 1
                arrivals.append(receive(REGULAR, day, float(regular), physical))
        start_stock = sum(l.qty for l in (*lots, *arrivals) if l.qty > EPS)
        gap = units[i] - start_stock
        topup = 0
        if gap > EPS:  # the fewest whole order cases that cover the gap, the same morning (record = physical)
            topup = p.order_case * math.ceil((gap - EPS) / p.order_case)
            arrivals.append(receive(TOPUP, day, float(topup), float(topup)))
        received = [(meta[l.lot_id], l.recorded, l.qty) for l in arrivals]
        res = step_day(lots, day, {store: units[i]}, UNIT_RESPONSE, p.product, arrivals=tuple(arrivals),
                       schedule=p.schedule)
        lost, sold = res.lost.get(store, 0.0), res.sold.get(store, 0.0)
        if lost > EPS:
            raise RuntimeError(f"store {store} product {product_code} day {day}: {lost} public units were not "
                               f"covered by the simulated stock; public sales are never changed (D-16)")
        for m, record, physical in received:
            row = {**m, "qty": record, "physical": physical, "markdown_pct": 0, "arrival_day": day,
                   "arrival_min": p.delivery_min}
            if day >= 0 and m["kind"] == TOPUP:
                eval_topups.append(row)
            if day >= 0 and (m["kind"] == REGULAR or p.topup_on_eval_days):
                inbound.append(row)
        rec[(store, product_code, day)] = units[i]
        so[(store, product_code, day)] = stockout[i]
        wasted = res.wasted.get(store, 0.0)
        end_stock = sum(l.qty for l in lots)
        sim_stockout = end_stock + wasted <= EPS  # nothing left on the shelf after the day's sales
        history.append({"day": day, "units": units[i], "sold": sold, "lost": lost, "regular": regular,
                        "topup": topup, "start_stock": start_stock, "wasted": wasted, "end_stock": end_stock,
                        "sim_stockout": sim_stockout, "public_stockout": stockout[i]})
        counters["topup_units"] += topup
        counters["regular_units"] += regular
        counters["wasted_units"] += wasted
        counters["sim_stockout_days"] += sim_stockout
        counters["public_stockout_days"] += stockout[i]
        counters["agree_stockout_days"] += sim_stockout and stockout[i]
    if designed_physical_zero:  # the record stays; nothing is on the shelf from day 0 (a designed example, D-40)
        lots_day0, inbound, eval_topups = ([{**r, "physical": 0.0} for r in rows]
                                           for rows in (lots_day0, inbound, eval_topups))
    return SeriesStock(store, product_code, lots_day0, inbound, eval_topups, history, counters)
