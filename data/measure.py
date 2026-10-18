"""The measurement spike on the full public data: `.venv-data/bin/python -I tools/frn.py measure <step> ...`
(spec §5 Public dataset; 05.2 D-10, D-11, D-12, D-21, D-23, D-27, D-28; DATA-15, DATA-17).

A measurement tool: every parameter is a command-line value or a value of the --params file, and is printed in the
report; nothing here is a product value. The parameter sets are provisional (05.2-06 decides D-33..D-39). The report
records what they give; no value is tuned to make a budget pass (benchmark, not target), and the slowest case is
picked by a rule printed in the report, never by hand (D-28).

Steps (each merges its block into the --out JSON, beside the parameters, the machine and the load average):

- stock: every series from `data.public.series_iter` (all of them; --limit marks a report partial), units =
  sale_amount x the set's units factor, through `data.stock.simulate`. Then the do-nothing warm-up of days
  0..open_day-1 (`engine.world.step_day`, the public eval units as demand at the stated warm-up response, the inbound
  as D-39 leaves it) and the open day's 06:00 deliveries joined as lots (D-21: after the day start they are lots).
  Lots alive per series at the start of day 0 and at the open; top-up share; warm-up shortfall; time; peak memory.
  Writes one state file per set and the day rows as a gz CSV (git-ignored, for the later steps).
- screen: the set's candidate network (`data.runs`, k from the set), then for every lot alive at the open whose
  ESL-02 window holds the open day, the EngineInput api/inputs.py would build, through `api.screen.screen`: the
  source plus today's run destinations that carry the product (a destination with no series of the product has no
  forecast, so it cannot be priced; such runs are counted, never routed), their lots of the product (the record),
  later inbound up to the lot's expiry, the local seasonal-naive stand-in forecast (`data.forecast.local_fetch` on
  the public units up to the day before the open, clamped and rounded as data.forecast stores points), capacity and
  yesterday's warm-up sales. Qualifying lots total, by store and by city; time per screen.
- latency: for each k and shortlist step, the qualifying lots ranked by the printed rule, the top N timed with
  `engine.evaluate` then `engine.fact_checks` (the API's `_compute`), inline, median of R runs each.
- catalogue: the first N qualifying lots in (city, store, product, lot) code order at the stated k and step,
  evaluated and fact-checked; outcome kinds tallied with three example lot ids each (codes only).
- db: a scratch database whose name ends in _test (any other, and shelfshift, shelfshift_test and
  shelfshift_web_test, are refused): api/schema.sql applied, COPY of the day rows (primary key built after, and
  present before), per-series reads, a workspace-creation prototype (workflow rows only), materializing one series and
  every series; then the template build estimate (one day's step_day over every series in three worlds, timed, plus
  the screen and catalogue costs measured by the earlier steps).

This module never imports api.db, api.app or api.refdata; api.screen imports only the engine.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import gzip
import json
import math
import os
import pathlib
import platform
import random
import re
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from typing import NamedTuple

from api import screen
from engine import evaluate, fact_checks, to_dict
from engine.adjust import coerce_input, effective
from engine.allocator import EPS, ShelfLot
from engine.limits import transfer_caps
from engine.model import (ENGINE_VERSION, Capacity, EngineInput, Forecast, Inbound, Lot, MarkdownResponse, Policy,
                          Product, Run)
from engine.simulate import HorizonError
from engine.world import step_day

from . import config, public, public_config
from . import runs as network
from . import stock as stock_mod
from .forecast import local_fetch

STEPS = ("stock", "screen", "latency", "catalogue", "db")
WORLDS = ("shelfshift", "baseline_rule", "do_nothing")  # the template's three worlds, as api/advance.py WORLDS
VERIFY_FACTS = ("PHANTOM_STOCK", "STALE_COUNT")  # stock facts a count settles (verify before you move)
KINDS = ("transfer", "markdown", "keep", "verify", "cold_chain", "transfers_unavailable")
EXTRA_KINDS = ("no_destination", "split_scenarios")  # reported beside the six, never instead of one
UNIT_PLACES = 6  # units are rounded to 6 decimals after the factor, so float noise never adds a sliver of stock
MONEY_PLACES = 2
EXAMPLES = 3
TOP_STORES = 10
SCRATCH_DB = re.compile(r"[a-z0-9_]+_test")
PROTECTED_DBS = frozenset({"shelfshift", "shelfshift_test", "shelfshift_web_test"})
SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "api" / "schema.sql"
TEMPLATE_RUN = "tpl-measure"
DATASET_VERSION = "ds-measure"
DAY_ROW_COLUMNS = ("store", "product", "day", "units", "sale_amount", "oos_hours", "discount", "holiday", "activity",
                   "precip", "temp", "humidity", "wind")


# ---------------------------------------------------------------- parameters

class SetCfg(NamedTuple):
    name: str
    factor: float
    product: Product  # sku filled per series
    schedule: tuple[tuple[int, int], ...]
    stock: stock_mod.StockParams
    k: int


def load_params(path: pathlib.Path | str) -> dict:
    params = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    for key in ("seed", "sets", "network", "policy", "markdown_response", "case_opening", "warmup_response"):
        if key not in params:
            raise ValueError(f"--params: {key} is missing")
    if not params["sets"]:
        raise ValueError("--params: sets is empty")
    return params


def _schedule(steps) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((tuple(s) for s in steps), reverse=True))


def set_cfg(params: dict, name: str, cal: dict) -> SetCfg:
    s = params["sets"][name]
    pr = s["product"]
    product = Product("", pr["zone"], pr["price"], pr["disposal_cost"], pr["units_per_tote"], pr["kg_per_unit"])
    schedule = _schedule(s["schedule"])
    sp = stock_mod.StockParams(
        product=product, schedule=schedule, shelf_life_days=s["shelf_life_days"], cycle_days=s["cycle_days"],
        order_case=s["order_case"], multiplier=tuple(s["multiplier"]), lookback_days=s["lookback_days"],
        same_weekday_obs=s["same_weekday_obs"], error_share=s["error_share"], error_range=tuple(s["error_range"]),
        error_from_day=s["error_from_day"], expiry_estimated_share=s["expiry_estimated_share"],
        first_day=cal["first_day"], last_day=cal["last_day"], delivery_min=cal["delivery_min"], seed=params["seed"],
        topup_on_eval_days=s["topup_on_eval_days"])
    return SetCfg(name, s["units_factor"], product, schedule, sp, s["k"])


def network_params(params: dict, k: int) -> network.NetworkParams:
    n = params["network"]
    return network.NetworkParams(
        city_radius_km=n["city_radius_km"], speed_kmh=n["speed_kmh"], handling_min=n["handling_min"],
        max_travel_min=n["max_travel_min"], k=k, run_days=tuple(n["run_days"]),
        loading_cutoff_min=n["loading_cutoff_min"], departs_min=n["departs_min"], spare_totes=n["spare_totes"],
        cold_chain_share=n["cold_chain_share"], fixed_cost_base=n["fixed_cost_base"], per_km_cost=n["per_km_cost"],
        per_unit_cost=n["per_unit_cost"], cancel_cost=n["cancel_cost"], capacity_totes=dict(n["capacity_totes"]),
        unmapped=frozenset(n["unmapped"]), seed=params["seed"])


def _response(params: dict) -> dict[int, float]:
    return {int(k): float(v) for k, v in params["warmup_response"].items()}


# ---------------------------------------------------------------- small helpers

def spread(values, pcts=(90,)) -> dict:
    """min, median, the nearest-rank percentiles asked for, max; None for each when empty."""
    v = sorted(values)
    keys = ("min", "median", *(f"p{p}" for p in pcts), "max")
    if not v:
        return dict.fromkeys(keys)
    out = {"min": v[0], "median": statistics.median(v)}
    for p in pcts:
        out[f"p{p}"] = v[math.ceil(p / 100 * len(v)) - 1]
    out["max"] = v[-1]
    return out


def ms_spread(seconds) -> dict:
    s = spread([x * 1000 for x in seconds], pcts=(95,))
    out = {k: (round(v, 3) if v is not None else None) for k, v in s.items()}
    out["mean"] = round(statistics.fmean(seconds) * 1000, 3) if seconds else None
    out["n"] = len(seconds)
    return out


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1 << 20) if sys.platform == "darwin" else peak / (1 << 10), 1)  # bytes on macOS, KiB else


def machine() -> dict:
    return {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(),
            "python": platform.python_version(), "load_avg": [round(x, 2) for x in os.getloadavg()],
            "at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def code_order(key) -> tuple:
    """(city, store, product, lot_id) with the codes compared as integers (they are integer ids, D-02)."""
    city, store, product, lot_id = key
    return int(city), int(store), int(product), lot_id


# ---------------------------------------------------------------- stock

def warmup(st: stock_mod.SeriesStock, units_at, sp: stock_mod.StockParams, open_day: int, response: dict):
    """The do-nothing world from the start of day 0 to the open of `open_day`: (lots at the open, later inbound,
    outcomes of days 0..open_day-1). Demand is the public units (the eval week, D-15); the world gets the inbound the
    simulation delivers (eval-day top-ups only when D-39's flag is on); the open day's deliveries join as lots."""
    store, product = st.store, sp.product
    meta = {r["lot_id"]: r for r in (*st.lots_day0, *st.inbound)}

    def shelf(r: dict) -> ShelfLot:
        return ShelfLot(r["lot_id"], store, r["physical"], r["expiry_day"], r["received_seq"], r["markdown_pct"],
                        recorded=r["qty"])

    lots = [shelf(r) for r in st.lots_day0]
    outcomes = []
    for day in range(0, open_day):
        arrivals = tuple(shelf(r) for r in st.inbound if r["arrival_day"] == day)
        res = step_day(lots, day, {store: units_at(day)}, response, product, arrivals=arrivals, schedule=sp.schedule)
        outcomes.append([day, units_at(day), round(res.sold.get(store, 0.0), UNIT_PLACES),
                         round(res.wasted.get(store, 0.0), UNIT_PLACES), round(res.lost.get(store, 0.0), UNIT_PLACES),
                         round(res.revenue.get(store, 0.0), MONEY_PLACES),
                         round(res.disposal.get(store, 0.0), MONEY_PLACES)])
    lots.extend(shelf(r) for r in st.inbound if r["arrival_day"] == open_day)
    at_open = [[l.lot_id, round(l.recorded, UNIT_PLACES), round(l.qty, UNIT_PLACES), l.expiry_day, l.received_seq,
                l.pct, meta[l.lot_id]["expiry_source"], meta[l.lot_id]["last_verified_day"],
                meta[l.lot_id]["received_day"], meta[l.lot_id]["kind"]]
               for l in sorted(lots, key=lambda l: l.received_seq) if l.qty > EPS or l.recorded > EPS]
    later = [[r["lot_id"], r["qty"], r["physical"], r["arrival_day"], r["arrival_min"], r["expiry_day"],
              r["received_seq"], r["expiry_source"], r["last_verified_day"], r["kind"]]
             for r in st.inbound if r["arrival_day"] > open_day]
    return at_open, later, outcomes


def stock_series(key, rows, sc: SetCfg, open_day: int, response: dict):
    """(state line, stats) for one series under one parameter set."""
    city, store, product_code, cats = key
    days = [r["day"] for r in rows]
    units = [round(r["sale_amount"] * sc.factor, UNIT_PLACES) for r in rows]
    stockout = [r["stockout"] for r in rows]
    sp = dataclasses.replace(sc.stock, product=dataclasses.replace(sc.product, sku=product_code))
    st = stock_mod.simulate(store, product_code, days, units, stockout, sp)
    first = days[0]
    at_open, later, outcomes = warmup(st, lambda d: units[d - first], sp, open_day, response)
    stats = {"lots_day0": len(st.lots_day0), "lots_open": len(at_open), "units": sum(units), "days": len(units),
             "topup_units": st.counters["topup_units"], "regular_units": st.counters["regular_units"],
             "wasted_units": st.counters["wasted_units"], "error_lots": st.counters["error_lots"],
             "eval_topup_units": sum(r["qty"] for r in st.eval_topups),
             "warmup_lost": sum(o[4] for o in outcomes), "warmup_short_days": sum(1 for o in outcomes if o[4] > EPS),
             "open_short_lots": sum(1 for l in at_open if l[2] < l[1] - EPS),
             "open_phantom_lots": sum(1 for l in at_open if l[2] <= EPS < l[1])}
    state = {"k": [city, store, product_code, list(cats)], "f": first, "u": units, "l": at_open, "i": later,
             "o": outcomes}
    return state, stats


class StockAcc:
    def __init__(self):
        self.series = 0
        self.lots_day0, self.lots_open = [], []
        self.sums = Counter()
        self.compute_s = 0.0

    def add(self, stats: dict, seconds: float) -> None:
        self.series += 1
        self.lots_day0.append(stats["lots_day0"])
        self.lots_open.append(stats["lots_open"])
        self.sums.update({k: v for k, v in stats.items() if k not in ("lots_day0", "lots_open")})
        self.compute_s += seconds

    def report(self, sc: SetCfg) -> dict:
        s = self.sums
        delivered = s["topup_units"] + s["regular_units"]
        return {
            "series": self.series, "units_factor": sc.factor, "k": sc.k,
            "lots_day0": {**spread(self.lots_day0), "total": sum(self.lots_day0)},
            "lots_open": {**spread(self.lots_open), "total": sum(self.lots_open)},
            "units_per_series_day": round(s["units"] / s["days"], 4) if s["days"] else None,
            "topup": {"units": round(s["topup_units"], 2), "regular_units": round(s["regular_units"], 2),
                      "share_of_delivered": round(s["topup_units"] / delivered, 4) if delivered else None,
                      "eval_day_units": round(s["eval_topup_units"], 2),
                      "eval_days_delivered_to_worlds": sc.stock.topup_on_eval_days},
            "wasted_units_simulation": round(s["wasted_units"], 2),
            "warmup": {"lost_units": round(s["warmup_lost"], 2), "short_series_days": s["warmup_short_days"]},
            "record_errors": {"error_lots": s["error_lots"], "open_lots_short": s["open_short_lots"],
                              "open_lots_phantom": s["open_phantom_lots"]},
            "compute_s": round(self.compute_s, 2),
            "per_series_ms": round(self.compute_s / self.series * 1000, 3) if self.series else None,
        }


def run_stock(series, params: dict, cal: dict, sets: dict, state_dir: pathlib.Path, limit: int | None) -> dict:
    open_day, response = cal["demo_open_day"], _response(params)
    state_dir.mkdir(parents=True, exist_ok=True)
    acc = {name: StockAcc() for name in sets}
    writers = {name: gzip.open(state_dir / f"state-{name}.jsonl.gz", "wt", encoding="utf-8") for name in sets}
    first_set = next(iter(sets.values()))
    rows_path = state_dir / "day-rows.csv.gz"
    start = time.perf_counter()
    read_s = write_s = 0.0
    try:
        with gzip.open(rows_path, "wt", encoding="utf-8", newline="") as day_csv:
            day_csv.write(",".join(DAY_ROW_COLUMNS) + "\n")
            it = iter(series)
            while limit is None or acc[first_set.name].series < limit:
                t = time.perf_counter()
                item = next(it, None)
                read_s += time.perf_counter() - t
                if item is None:
                    break
                key, rows = item
                t = time.perf_counter()
                store, product = key[1], key[2]
                day_csv.write("".join(
                    f"{store},{product},{r['day']},{round(r['sale_amount'] * first_set.factor, UNIT_PLACES)!r},"
                    f"{r['sale_amount']!r},{r['oos_hours']},{r['discount']!r},{r['holiday']},{r['activity']},"
                    f"{r['precip']!r},{r['temp']!r},{r['humidity']!r},{r['wind']!r}\n" for r in rows))
                write_s += time.perf_counter() - t
                for name, sc in sets.items():
                    t = time.perf_counter()
                    state, stats = stock_series(key, rows, sc, open_day, response)
                    acc[name].add(stats, time.perf_counter() - t)
                    t = time.perf_counter()
                    writers[name].write(json.dumps(state, separators=(",", ":")) + "\n")
                    write_s += time.perf_counter() - t
    finally:
        for w in writers.values():
            w.close()
    wall = time.perf_counter() - start
    shared = {"read_and_sort_s": round(read_s, 2), "write_state_s": round(write_s, 2), "wall_s": round(wall, 2),
              "peak_rss_mb": peak_rss_mb(), "partial": limit is not None,
              "day_rows_units_factor_of": first_set.name}
    return {name: {**acc[name].report(sc), **shared} for name, sc in sets.items()}


# ---------------------------------------------------------------- state and case inputs

class Series(NamedTuple):
    city: str
    store: str
    product: str
    cats: list
    first: int
    units: list
    lots: list  # [lot_id, record, physical, expiry, seq, pct, source, verified, received_day, kind]
    later: list  # [lot_id, record, physical, arrival_day, arrival_min, expiry, seq, source, verified, kind]
    outcomes: list  # [day, demand, sold, wasted, lost, revenue, disposal]


def load_state(path: pathlib.Path) -> dict[tuple[str, str], Series]:
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            city, store, product, cats = d["k"]
            out[(store, product)] = Series(city, store, product, cats, d["f"], d["u"], d["l"], d["i"], d["o"])
    return out


class Ctx:
    """Everything a case input needs for one set at one k: the state, the network, the forecast cache."""

    def __init__(self, params: dict, cal: dict, sc: SetCfg, state: dict, k: int):
        self.sc, self.state, self.k = sc, state, k
        self.open, self.open_min = cal["demo_open_day"], cal["open_min"]
        self.policy = Policy(**params["policy"])
        md = params["markdown_response"]
        self.markdown = MarkdownResponse(tuple(md["m20"]), tuple(md["m30"]))
        self.totes = params["network"]["capacity_totes"][sc.product.zone]
        npar = network_params(params, k)
        stores_by_city = defaultdict(set)
        for s in state.values():
            stores_by_city[s.city].add(s.store)
        self.stores_by_city = stores_by_city
        pos = network.positions(stores_by_city, npar)
        pairs, unmapped = network.candidate_pairs(stores_by_city, pos, npar)
        self.pairs, self.unmapped = pairs, set(unmapped)
        self.runs_by_origin: dict[str, list[Run]] = defaultdict(list)
        for r in network.run_rows(pairs, npar):
            if r["day"] == self.open:
                self.runs_by_origin[r["origin"]].append(Run(
                    r["run_code"], r["origin"], r["dest"], r["day"], r["loading_cutoff_min"], r["departs_min"],
                    r["arrives_min"], r["spare_totes"], r["cold_chain"], r["fixed_cost"], r["per_unit_cost"], True,
                    r["cancel_cost"]))
        self.fc: dict[tuple[str, str], dict[int, tuple[float, float, float]]] = {}

    def forecast(self, key, last_day: int) -> dict[int, tuple[float, float, float]]:
        """The seasonal-naive stand-in (data.forecast.local_fetch) on the public units up to the day before the open,
        for days open..last_day; points clamped at 0 and rounded to cents as data.forecast stores them."""
        have = self.fc.get(key)
        if have is not None and last_day in have:
            return have
        s = self.state[key]
        cutoff = self.open - 1
        inputs = [(d, s.units[d - s.first], "recorded") for d in range(s.first, cutoff + 1)]
        top = max(last_day, self.open + 2 * self.sc.stock.shelf_life_days + self.sc.stock.cycle_days)
        rows = local_fetch({"x": inputs}, {"x": list(range(self.open, top + 1))})["x"]
        self.fc[key] = {r["day"]: tuple(round(max(0.0, r[c]), 2) for c in ("low", "base", "high")) for r in rows}
        return self.fc[key]

    def case_input(self, store: str, product: str, lot_id: str, shortlist_step: int | None = None):
        """(EngineInput, info) for the lot, shaped as api/inputs.py builds it, from the in-memory state."""
        s = self.state[(store, product)]
        case = next(l for l in s.lots if l[0] == lot_id)
        all_runs = self.runs_by_origin.get(store, ())
        runs = tuple(r for r in all_runs if (r.dest, product) in self.state)
        branches = sorted({store} | {r.dest for r in runs})
        lots = sorted((Lot(l[0], b, float(l[1]), l[3], l[4], l[5], l[6], l[7])
                       for b in branches for l in self.state[(b, product)].lots if l[1] > 0 or l[0] == lot_id),
                      key=lambda l: l.lot_id)
        inbound = sorted((Inbound(i[0], b, float(i[1]), i[3], i[4], i[5], i[6])
                          for b in branches for i in self.state[(b, product)].later
                          if self.open <= i[3] <= case[3]), key=lambda i: i.inbound_id)
        end = max([self.open] + [l.expiry_day for l in lots] + [i.expiry_day for i in inbound])
        forecasts = []
        for b in branches:
            points = self.forecast((b, product), end)
            forecasts += [Forecast(b, d, *points[d]) for d in range(self.open, end + 1)]
        capacity = tuple(Capacity(dest, d, self.totes) for dest in sorted({r.dest for r in runs})
                         for d in range(self.open, end + 1))
        sales = []
        for b in branches:
            o = next((o for o in self.state[(b, product)].outcomes if o[0] == self.open - 1), None)
            if o is not None:
                sales.append((b, round(float(o[2]), 2)))
        policy = self.policy if shortlist_step is None else dataclasses.replace(self.policy,
                                                                                shortlist_step=shortlist_step)
        inp = EngineInput(ENGINE_VERSION, self.open, self.open_min, lot_id,
                          dataclasses.replace(self.sc.product, sku=product), tuple(lots), tuple(inbound), runs,
                          tuple(forecasts), capacity, tuple(sales), self.markdown, policy,
                          markdown_schedule=self.sc.schedule)
        info = {"runs_network": len(all_runs), "runs": len(runs), "lots": len(lots),
                "uncounted_dest_lots": sum(1 for l in lots if l.branch != store and l.last_verified_day != self.open)}
        return inp, info

    def eligible(self, inp: EngineInput) -> int:
        """Destinations of today's runs in the input with no transfer_caps reason (cold chain, deadline, ...)."""
        eff = effective(coerce_input(inp))
        return len({r.dest for r in eff.runs if not transfer_caps(eff, r)[1]})


def _qualifying_path(state_dir: pathlib.Path, name: str) -> pathlib.Path:
    return state_dir / f"qualifying-{name}.json.gz"


def load_qualifying(state_dir: pathlib.Path, name: str) -> list[tuple[str, str, str, str]]:
    with gzip.open(_qualifying_path(state_dir, name), "rt", encoding="utf-8") as fh:
        return [tuple(q) for q in json.load(fh)]


# ---------------------------------------------------------------- screen

def run_screen(params: dict, cal: dict, sets: dict, state_dir: pathlib.Path) -> dict:
    thresholds = params["case_opening"]
    before, open_day = thresholds["days_before_first_step"], cal["demo_open_day"]
    out = {}
    for name, sc in sets.items():
        start = time.perf_counter()
        state = load_state(state_dir / f"state-{name}.jsonl.gz")
        load_s = time.perf_counter() - start
        t = time.perf_counter()
        ctx = Ctx(params, cal, sc, state, sc.k)
        network_s = time.perf_counter() - t
        lots_open = in_window = missing = a = b = with_runs = with_network = 0
        build_s, screen_s = [], []
        qualifying: list[list[str]] = []
        by_store: Counter = Counter()
        by_city: Counter = Counter()
        for key in sorted(state, key=lambda k: (int(k[0]), int(k[1]))):
            s = state[key]
            for lot in s.lots:
                lots_open += 1
                low, high = screen.window(lot[3], sc.schedule, before)
                if not low <= open_day <= high:
                    continue
                in_window += 1
                t = time.perf_counter()
                inp, info = ctx.case_input(s.store, s.product, lot[0])
                build_s.append(time.perf_counter() - t)
                t = time.perf_counter()
                try:
                    res = screen.screen(inp, thresholds)
                except HorizonError:
                    missing += 1  # counted, never dropped silently: the app opens such a case as missing data
                    continue
                finally:
                    screen_s.append(time.perf_counter() - t)
                if res["qualifies"]:
                    qualifying.append([s.city, s.store, s.product, lot[0]])
                    by_store[s.store] += 1
                    by_city[s.city] += 1
                    a += res["a"]
                    b += res["b"]
                    with_runs += info["runs"] > 0
                    with_network += info["runs_network"] > 0
        stores = sorted({st for st, _ in state})
        city_of = {s.store: s.city for s in state.values()}
        counts = [by_store.get(st, 0) for st in stores]
        top = sorted(by_store.items(), key=lambda kv: (-kv[1], int(kv[0])))[:TOP_STORES]
        qualifying.sort(key=code_order)
        with gzip.open(_qualifying_path(state_dir, name), "wt", encoding="utf-8") as fh:
            json.dump(qualifying, fh, separators=(",", ":"))
        n_runs = sum(len(v) for v in ctx.runs_by_origin.values())
        products_of = defaultdict(set)
        for st, pp in state:
            products_of[st].add(pp)
        carried = sum(len(products_of[r.origin] & products_of[r.dest])
                      for v in ctx.runs_by_origin.values() for r in v)
        series_runs = sum(len(products_of[o]) * len(v) for o, v in ctx.runs_by_origin.items())
        out[name] = {
            "k": sc.k, "series": len(state), "lots_open": lots_open, "in_window": in_window,
            "screened": len(screen_s), "missing_data": missing, "total": len(qualifying),
            "branch_a": a, "branch_b": b,
            "qualifying_with_a_run_to_a_store_carrying_the_product": with_runs,
            "qualifying_whose_store_has_runs": with_network,
            "by_store": {**spread(counts), "stores": len(stores), "stores_with_any": sum(1 for c in counts if c),
                         "top": [{"store": st, "city": city_of[st], "lots": n} for st, n in top]},
            "by_city": {c: by_city.get(c, 0) for c in sorted({s.city for s in state.values()}, key=int)},
            "network": {"pairs": len(ctx.pairs), "runs_on_open_day": n_runs, "unmapped_stores": len(ctx.unmapped),
                        "series_x_runs": series_runs, "series_x_runs_destination_carries_product": carried,
                        "note": "(series, run) pairs: every run leaving a series' store on the open day; the second "
                                "count keeps those whose destination has a series of the same product"},
            "per_screen_ms": ms_spread(screen_s), "build_input_ms": ms_spread(build_s),
            "load_state_s": round(load_s, 2), "network_s": round(network_s, 2),
            "screen_s": round(sum(screen_s), 2), "wall_s": round(time.perf_counter() - start, 2),
            "peak_rss_mb": peak_rss_mb(),
        }
        del ctx, state
    return out


# ---------------------------------------------------------------- latency and catalogue

LATENCY_RULE = ("Among the open day's qualifying lots of the screen step (set and k of that step), each case is "
                "rebuilt at the row's k and ranked by (eligible destinations = today's run destinations that carry "
                "the product and have no transfer_caps reason, lots at the source and destinations, uncounted "
                "destination lots), descending, ties by (city, store, product, lot) code order; the top N are timed. "
                "Time = engine.evaluate then engine.fact_checks on the raw input, inline (no worker), median of R "
                "runs per case; the slowest median per (k, step) is reported with its case.")


def time_case(inp: EngineInput, reps: int):
    times, result, checks = [], None, None
    for _ in range(reps):
        t = time.perf_counter()
        result = evaluate(inp)
        checks = fact_checks(inp, result)
        times.append(time.perf_counter() - t)
    return statistics.median(times), result, checks


QUANTITY_RULE = ("Supplementary to the plan's rule: the same qualifying lots ranked by (the case lot's recorded units, "
                 "eligible destinations, lots at the source and destinations), descending, ties by code order; the "
                 "candidate count grows with the units a transfer can move, which the plan's rule does not rank by.")


def _rank_key(rule: str, inp: EngineInput, info: dict, eligible: int) -> tuple:
    if rule == "destinations":
        return eligible, info["lots"], info["uncounted_dest_lots"]
    return inp.case_lot().qty, eligible, info["lots"]


def latency_sweep(params: dict, cal: dict, sc: SetCfg, state: dict, qualifying: list, rule: str) -> tuple:
    """(rows, ranking) for one set and one ranking rule: one row per (k, shortlist step)."""
    lp = params["latency"]
    rows, ranking = [], {}
    for k in lp["ks"]:
        ctx = Ctx(params, cal, sc, state, k)
        t = time.perf_counter()
        ranked = []
        for q in qualifying:
            inp, info = ctx.case_input(q[1], q[2], q[3])
            eligible = ctx.eligible(inp)
            ranked.append((_rank_key(rule, inp, info, eligible), code_order(q), q, eligible, info["lots"]))
        ranked.sort(key=lambda x: (tuple(-v for v in x[0]), x[1]))
        top = ranked[:lp["top"]]
        ranking[str(k)] = {"rank_s": round(time.perf_counter() - t, 2),
                           "eligible_destinations": dict(sorted(Counter(r[3] for r in ranked).items())),
                           "top_keys": [list(r[0]) for r in top]}
        for step in lp["steps"]:
            cases = []
            for _, _, q, eligible, lots in top:
                inp, _ = ctx.case_input(q[1], q[2], q[3], shortlist_step=step)
                med, result, _ = time_case(inp, lp["reps"])
                cases.append({"seconds": med, "city": q[0], "store": q[1], "product": q[2], "lot_id": q[3],
                              "case_lot_units": inp.case_lot().qty,
                              "candidates_checked": result["candidates_checked"], "eligible": eligible,
                              "lots": lots, "runs": len(inp.runs)})
            if not cases:  # nothing qualified: the cell is reported empty, never filled in
                rows.append({"k": k, "shortlist_step": step, "cases": 0, "reps": lp["reps"],
                             "slowest_median_s": None, "slowest_case": None, "median_over_cases_s": None,
                             "max_candidates": None, "under_3s": 0})
                continue
            slowest = max(cases, key=lambda c: c["seconds"])
            rows.append({"k": k, "shortlist_step": step, "cases": len(cases), "reps": lp["reps"],
                         "slowest_median_s": round(slowest["seconds"], 4),
                         "slowest_case": {**slowest, "seconds": round(slowest["seconds"], 4)},
                         "median_over_cases_s": round(statistics.median(c["seconds"] for c in cases), 4),
                         "max_candidates": max(c["candidates_checked"] for c in cases),
                         "under_3s": sum(1 for c in cases if c["seconds"] < 3.0)})
        del ctx
    return rows, ranking


def run_latency(params: dict, cal: dict, sets: dict, state_dir: pathlib.Path) -> dict:
    """The plan's twelve cells (the stated set, the destinations rule) as `rows`; then, as `supplementary`, every set
    of --params under both rules, so a heavier case the plan's rule does not rank first is still timed."""
    lp = params["latency"]
    start = time.perf_counter()
    out, supplementary = {}, []
    for name in [lp["set"]] + [n for n in sets if n != lp["set"]]:
        sc = sets[name]
        state = load_state(state_dir / f"state-{name}.jsonl.gz")
        qualifying = load_qualifying(state_dir, name)
        for rule in ("destinations", "quantity"):
            rows, ranking = latency_sweep(params, cal, sc, state, qualifying, rule)
            if name == lp["set"] and rule == "destinations":
                out = {"set": name, "rule": LATENCY_RULE, "top": lp["top"], "qualifying": len(qualifying),
                       "rows": rows, "ranking": ranking}
            else:
                supplementary.append({"set": name, "rule": rule, "qualifying": len(qualifying), "rows": rows,
                                      "ranking": ranking})
        del state
    return {**out, "quantity_rule": QUANTITY_RULE, "supplementary": supplementary,
            "wall_s": round(time.perf_counter() - start, 2), "peak_rss_mb": peak_rss_mb()}


CATALOGUE_RULE = ("The first N of the open day's qualifying lots in (city, store, product, lot_id) code order (codes "
                  "compared as integers), each evaluated and fact-checked at the stated k and shortlist step. Kinds: "
                  "transfer / markdown / keep = the recommended plan's kind; verify = a PHANTOM_STOCK or STALE_COUNT "
                  "check with pick_changed true and decision_regret >= the input Policy's min_regret (a count would "
                  "change the plan); cold_chain = a rejected row whose reasons include 'cold chain'; "
                  "transfers_unavailable = the source store is unmapped (D-31). Extra, reported beside them: "
                  "no_destination = no run today reaches a store that carries the product; split_scenarios = the "
                  "recommended plan does not win all three scenarios.")


def classify(inp: EngineInput, result: dict, checks: list[dict], unmapped: set) -> list[str]:
    rec = result["recommended"]
    kinds = ["keep" if rec == "keep" else "markdown" if rec.startswith("md") else "transfer"]
    if any(c["kind"] in VERIFY_FACTS and c["pick_changed"] and c["decision_regret"] >= inp.policy.min_regret
           for c in checks):
        kinds.append("verify")
    if any("cold chain" in r["reasons"] for r in result["rejected"]):
        kinds.append("cold_chain")
    if inp.source in unmapped:
        kinds.append("transfers_unavailable")
    if not inp.runs:
        kinds.append("no_destination")
    if result["scenario_wins"]["wins"] < 3:
        kinds.append("split_scenarios")
    return kinds


def catalogue_preview(params: dict, cal: dict, sc: SetCfg, state: dict, qualifying: list,
                      payloads: pathlib.Path | None) -> dict:
    cp = params["catalogue"]
    ctx = Ctx(params, cal, sc, state, cp["k"])
    kinds = dict.fromkeys(KINDS + EXTRA_KINDS, 0)
    examples = {k: [] for k in kinds}
    times = []
    picked = sorted(qualifying, key=code_order)[:cp["n"]]
    fh = gzip.open(payloads, "wt", encoding="utf-8") if payloads else None
    try:
        for q in picked:
            inp, _ = ctx.case_input(q[1], q[2], q[3], shortlist_step=cp["step"])
            t = time.perf_counter()
            result = evaluate(inp)
            checks = fact_checks(inp, result)
            times.append(time.perf_counter() - t)
            for kind in classify(inp, result, checks, ctx.unmapped):
                kinds[kind] += 1
                if len(examples[kind]) < EXAMPLES:
                    examples[kind].append(q[3])
            if fh:
                fh.write(json.dumps({"engine_input": to_dict(inp), "result": result, "fact_checks": checks},
                                    separators=(",", ":")) + "\n")
    finally:
        if fh:
            fh.close()
    return {"set": sc.name, "k": cp["k"], "shortlist_step": cp["step"], "qualifying": len(qualifying),
            "evaluated": len(picked), "kinds": kinds, "examples": examples,
            "case_s": {k: (round(v / 1000, 4) if v is not None else None)
                       for k, v in ms_spread(times).items() if k != "n"}}


def run_catalogue(params: dict, cal: dict, sets: dict, state_dir: pathlib.Path) -> dict:
    """The stated set's preview (its payloads feed the db step's workspace prototype), then every other set of
    --params as `supplementary`, by the same rule, k, step and N."""
    cp = params["catalogue"]
    start = time.perf_counter()
    out, supplementary = {}, []
    for name in [cp["set"]] + [n for n in sets if n != cp["set"]]:
        state = load_state(state_dir / f"state-{name}.jsonl.gz")
        qualifying = load_qualifying(state_dir, name)
        primary = name == cp["set"]
        block = catalogue_preview(params, cal, sets[name], state, qualifying,
                                  state_dir / "catalogue-payloads.jsonl.gz" if primary else None)
        if primary:
            out = block
        else:
            supplementary.append(block)
        del state
    return {**out, "rule": CATALOGUE_RULE, "supplementary": supplementary,
            "wall_s": round(time.perf_counter() - start, 2)}


# ---------------------------------------------------------------- db

def scratch_url(url: str):
    import sqlalchemy as sa

    u = sa.engine.make_url(url)
    name = u.database or ""
    if not SCRATCH_DB.fullmatch(name) or name in PROTECTED_DBS:
        raise ValueError(f"refused: the db step runs only on a scratch database whose name ends in _test and is not "
                         f"{', '.join(sorted(PROTECTED_DBS))}; got {name!r}")
    return u


def schema_statements(sql: str) -> list[str]:
    """api/schema.sql as statements, split as api.db.statements does (comments dropped, then split on ';')."""
    sql = "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())
    return [s.strip() for s in sql.split(";") if s.strip()]


def _copy(conn, table: str, columns, path: pathlib.Path) -> None:
    """COPY a gz CSV into `table` on the caller's connection. Call it inside `engine.begin()`: SQLAlchemy commits
    only a transaction it began, so a COPY on a bare connection would be rolled back when the pool takes it back."""
    if not conn.in_transaction():
        raise RuntimeError("_copy needs a transaction begun by SQLAlchemy (engine.begin())")
    cur = conn.connection.driver_connection.cursor()
    with gzip.open(path, "rb") as fh:
        cur.execute(f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, HEADER true)", stream=fh)


def _write_csv(path: pathlib.Path, columns, rows) -> int:
    n = 0
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write(",".join(columns) + "\n")
        for r in rows:
            fh.write(",".join("" if v is None else str(v) for v in r) + "\n")
            n += 1
    return n


LOT_COLUMNS = ("run_id", "world", "lot_id", "branch", "sku", "qty", "expiry_day", "received_seq", "markdown_pct",
               "expiry_source", "last_verified_day", "origin")
TRUTH_COLUMNS = ("run_id", "world", "lot_id", "qty")
INBOUND_COLUMNS = ("run_id", "world", "inbound_id", "branch", "sku", "qty", "arrival_day", "arrival_min",
                   "expiry_day", "received_seq", "expiry_source", "last_verified_day", "origin")
OUTCOME_COLUMNS = ("run_id", "world", "day", "branch", "sku", "demand", "sold", "wasted", "lost", "revenue",
                   "transport", "handling", "disposal")
STOCK_TABLES = ("lots", "truth_lots", "inbound", "outcomes")


def template_csvs(state: dict, state_dir: pathlib.Path) -> dict:
    """The template's per-run stock rows at the open (three worlds, identical before any action) as gz CSVs."""
    def lots():
        for s in state.values():
            for w in WORLDS:
                for l in s.lots:
                    yield (TEMPLATE_RUN, w, l[0], s.store, s.product, l[1], l[3], l[4], l[5], l[6], l[7],
                           stock_mod.ORIGIN)

    def truth():
        for s in state.values():
            for w in WORLDS:
                for l in s.lots:
                    yield TEMPLATE_RUN, w, l[0], l[2]

    def inbound():
        for s in state.values():
            for w in WORLDS:
                for i in s.later:
                    yield (TEMPLATE_RUN, w, i[0], s.store, s.product, i[1], i[3], i[4], i[5], i[6], i[7], i[8],
                           stock_mod.ORIGIN)

    def outcomes():
        for s in state.values():
            for w in WORLDS:
                for o in s.outcomes:
                    yield TEMPLATE_RUN, w, o[0], s.store, s.product, o[1], o[2], o[3], o[4], o[5], 0, 0, o[6]

    out = {}
    for table, cols, gen in (("lots", LOT_COLUMNS, lots), ("truth_lots", TRUTH_COLUMNS, truth),
                             ("inbound", INBOUND_COLUMNS, inbound), ("outcomes", OUTCOME_COLUMNS, outcomes)):
        path = state_dir / f"template-{table}.csv.gz"
        out[table] = (path, cols, _write_csv(path, cols, gen()))
    return out


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def materialize_sql(new_run: str, one_series: bool) -> list[tuple[str, dict]]:
    where = " AND branch = :s AND sku = :p" if one_series else ""
    names = {t: [c for c in cols if c != "run_id"] for t, cols in
             (("lots", LOT_COLUMNS), ("inbound", INBOUND_COLUMNS), ("outcomes", OUTCOME_COLUMNS))}
    stmts = [(f"INSERT INTO {t} (run_id, {', '.join(c)}) SELECT :n, {', '.join(c)} FROM {t} "
              f"WHERE run_id = :t{where}", {}) for t, c in names.items()]
    join = (" JOIN lots l ON l.run_id = x.run_id AND l.world = x.world AND l.lot_id = x.lot_id "
            "WHERE x.run_id = :t AND l.branch = :s AND l.sku = :p") if one_series else " WHERE x.run_id = :t"
    stmts.append((f"INSERT INTO truth_lots (run_id, world, lot_id, qty) SELECT :n, x.world, x.lot_id, x.qty "
                  f"FROM truth_lots x{join}", {}))
    return stmts


def run_db(params: dict, cal: dict, sets: dict, state_dir: pathlib.Path, url: str, report: dict) -> dict:
    import sqlalchemy as sa

    dbp = params["db"]
    sc = sets[dbp["set"]]
    u = scratch_url(url)
    eng = sa.create_engine(u, pool_size=1, max_overflow=0)
    out: dict = {"database": u.database, "server": None}
    rng = random.Random(f"measure-db:{params['seed']}")
    t_all = time.perf_counter()
    try:
        with eng.begin() as conn:
            out["server"] = conn.execute(sa.text("SHOW server_version")).scalar()
            conn.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS activity_seq"))
            for stmt in schema_statements(SCHEMA.read_text(encoding="utf-8")):
                conn.execute(sa.text(stmt))
        # (a) COPY of the day rows
        rows_path = state_dir / "day-rows.csv.gz"
        cols = ("dataset_version text NOT NULL DEFAULT 'ds-measure', store text NOT NULL, product text NOT NULL, "
                "day int NOT NULL, units float8 NOT NULL, sale_amount float8 NOT NULL, oos_hours int NOT NULL, "
                "discount float8 NOT NULL, holiday int NOT NULL, activity int NOT NULL, precip float8, temp float8, "
                "humidity float8, wind float8")
        pk = "PRIMARY KEY (dataset_version, store, product, day)"
        copy = {}
        for variant in ("pk_after", "pk_before"):
            with eng.begin() as conn:
                conn.execute(sa.text("DROP TABLE IF EXISTS m_sales_day"))
                conn.execute(sa.text(f"CREATE TABLE m_sales_day ({cols}"
                                     + (f", {pk})" if variant == "pk_before" else ")")))
            t = time.perf_counter()
            with eng.begin() as conn:  # timed through the commit
                _copy(conn, "m_sales_day", DAY_ROW_COLUMNS, rows_path)
            copy_s = time.perf_counter() - t
            key_s = None
            if variant == "pk_after":
                t = time.perf_counter()
                with eng.begin() as conn:
                    conn.execute(sa.text(f"ALTER TABLE m_sales_day ADD {pk}"))
                key_s = time.perf_counter() - t
            with eng.connect() as conn:  # a new connection: sees only committed rows
                conn.execute(sa.text("ANALYZE m_sales_day"))
                n = conn.execute(sa.text("SELECT count(*) FROM m_sales_day")).scalar()
                size = conn.execute(sa.text("SELECT pg_total_relation_size('m_sales_day')")).scalar()
            copy[variant] = {"rows": n, "copy_s": round(copy_s, 2),
                             "primary_key_s": round(key_s, 2) if key_s is not None else None,
                             "total_s": round(copy_s + (key_s or 0), 2), "table_mb": round(size / (1 << 20), 1)}
        out["copy_day_rows"] = copy
        # (b) per-series indexed reads
        state = load_state(state_dir / f"state-{sc.name}.jsonl.gz")
        keys = sorted(state, key=lambda k: (int(k[0]), int(k[1])))
        sample = rng.sample(keys, min(dbp["reads"], len(keys)))
        reads, lengths = [], set()
        with eng.connect() as conn:
            q = sa.text("SELECT day, units, sale_amount, oos_hours, discount FROM m_sales_day WHERE "
                        "dataset_version = :v AND store = :s AND product = :p ORDER BY day")
            conn.execute(q, {"v": DATASET_VERSION, "s": sample[0][0], "p": sample[0][1]}).all()  # warm the plan
            for store, product in sample:
                t = time.perf_counter()
                got = conn.execute(q, {"v": DATASET_VERSION, "s": store, "p": product}).all()
                reads.append(time.perf_counter() - t)
                lengths.add(len(got))
        days = cal["last_day"] - cal["first_day"] + 1
        if lengths != {days}:  # a read that finds no rows measures nothing: refuse it rather than report it
            raise RuntimeError(f"series reads returned {sorted(lengths)} rows, expected {days} each")
        out["series_read"] = {"series": len(sample), "rows_per_read": sorted(lengths), "ms": ms_spread(reads)}
        # (c) workspace creation: workflow rows only (a demo_runs row, N cases, N recommendations)
        payloads = []
        with gzip.open(state_dir / "catalogue-payloads.jsonl.gz", "rt", encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                payloads.append((json.dumps(d["engine_input"]), json.dumps(d["result"]),
                                 json.dumps(d["fact_checks"])))
        out["payload_bytes"] = spread([sum(len(x) for x in p) for p in payloads])
        workspace = {}
        for n in dbp["workspace_n"]:
            with eng.begin() as conn:
                for table in ("cases", "recommendations", "demo_runs"):
                    conn.execute(sa.text(f"DELETE FROM {table}"))
                conn.execute(sa.text("INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, "
                                     "next_case_no) VALUES (:r, 'TEMPLATE', :v, 'ACTIVE', :d, :n)"),
                             {"r": TEMPLATE_RUN, "v": DATASET_VERSION, "d": cal["demo_open_day"], "n": n + 1})
                conn.execute(sa.text("INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, state, "
                                     "opened_day) VALUES (:r, :c, :k, :l, :b, :s, 'OPEN', :d)"),
                             [{"r": TEMPLATE_RUN, "c": f"C-{i}", "k": f"k-{i}", "l": f"lot-{i}", "b": "0",
                               "s": "0", "d": cal["demo_open_day"]} for i in range(n)])
                conn.execute(sa.text("INSERT INTO recommendations (run_id, case_id, rec_version, day, engine_input, "
                                     "result, fact_checks) VALUES (:r, :c, 'v1', :d, CAST(:i AS jsonb), "
                                     "CAST(:res AS jsonb), CAST(:f AS jsonb))"),
                             [{"r": TEMPLATE_RUN, "c": f"C-{i}", "d": cal["demo_open_day"],
                               "i": payloads[i % len(payloads)][0], "res": payloads[i % len(payloads)][1],
                               "f": payloads[i % len(payloads)][2]} for i in range(n)])
            create, reset = [], []
            for rep in range(dbp["reps"]):
                new = f"sbx-m{n}-{rep}"

                def clone(new=new):
                    with eng.begin() as conn:
                        conn.execute(sa.text("INSERT INTO demo_runs (run_id, kind, dataset_version, template_run_id, "
                                             "status, day, next_case_no) SELECT :n, 'SANDBOX', dataset_version, "
                                             "run_id, 'ACTIVE', day, next_case_no FROM demo_runs WHERE run_id = :t"),
                                     {"n": new, "t": TEMPLATE_RUN})
                        for table, names in (("cases", "case_id, key, lot_id, source_branch, sku, state, opened_day"),
                                             ("recommendations", "case_id, rec_version, day, engine_input, result, "
                                                                 "fact_checks")):
                            conn.execute(sa.text(f"INSERT INTO {table} (run_id, {names}) SELECT :n, {names} "
                                                 f"FROM {table} WHERE run_id = :t"), {"n": new, "t": TEMPLATE_RUN})

                def wipe(new=new):
                    with eng.begin() as conn:
                        for table in ("recommendations", "cases", "demo_runs"):
                            conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id = :n"), {"n": new})

                create.append(_timed(clone))
                reset.append(_timed(lambda: (wipe(), clone())))  # Reset: drop the private rows, clone again
            workspace[str(n)] = {"create_ms": ms_spread(create), "reset_ms": ms_spread(reset)}
        out["workspace_creation"] = workspace
        # (d), (e) materialize the stock of one series / of every series into a new run id
        csvs = template_csvs(state, state_dir)
        t = time.perf_counter()
        with eng.begin() as conn:
            for table, (path, columns, _) in csvs.items():
                _copy(conn, table, columns, path)
        template_load_s = time.perf_counter() - t
        with eng.begin() as conn:
            for table, (_, _, n_rows) in csvs.items():
                got = conn.execute(sa.text(f"SELECT count(*) FROM {table} WHERE run_id = :t"),
                                   {"t": TEMPLATE_RUN}).scalar()
                if got != n_rows:
                    raise RuntimeError(f"template {table}: {got} rows committed, {n_rows} written")
            conn.execute(sa.text("ANALYZE"))
        out["template_rows"] = {t: c[2] for t, c in csvs.items()}
        out["template_copy_s"] = round(template_load_s, 2)
        one = {}
        for indexed in (False, True):
            if indexed:
                with eng.begin() as conn:
                    for table in ("lots", "inbound", "outcomes"):
                        conn.execute(sa.text(f"CREATE INDEX IF NOT EXISTS m_{table}_series ON {table} "
                                             f"(run_id, branch, sku)"))
                    conn.execute(sa.text("ANALYZE"))
            picks = rng.sample(keys, dbp["materialize_one"])
            times, rows_one = [], []
            for i, (store, product) in enumerate(picks):
                new = f"mat1-{int(indexed)}-{i}"
                stmts = materialize_sql(new, True)

                copied = []

                def go(stmts=stmts, new=new, store=store, product=product, copied=copied):
                    with eng.begin() as conn:
                        for sql, _ in stmts:
                            copied.append(conn.execute(sa.text(sql), {"n": new, "t": TEMPLATE_RUN, "s": store,
                                                                      "p": product}).rowcount)
                times.append(_timed(go))
                if not sum(copied):  # every series has warm-up outcomes, so a copy of 0 rows is a broken query
                    raise RuntimeError(f"materializing series ({store}, {product}) copied no row")
                rows_one.append(sum(copied))
            with eng.begin() as conn:
                for table in STOCK_TABLES:
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id LIKE 'mat1-%'"))
            one["with_series_index" if indexed else "schema_as_is"] = {**ms_spread(times),
                                                                       "rows_per_series": spread(rows_one)}
        out["materialize_one_series"] = one
        every = []
        for rep in range(dbp["materialize_all_reps"]):
            new = f"matall-{rep}"
            stmts = materialize_sql(new, False)

            copied = []

            def go_all(stmts=stmts, new=new, copied=copied):
                with eng.begin() as conn:
                    for sql, _ in stmts:
                        copied.append(conn.execute(sa.text(sql), {"n": new, "t": TEMPLATE_RUN}).rowcount)
            every.append(round(_timed(go_all), 2))
            if sum(copied) != sum(c[2] for c in csvs.values()):
                raise RuntimeError(f"materializing every series copied {sum(copied)} rows, expected "
                                   f"{sum(c[2] for c in csvs.values())}")
            with eng.begin() as conn:
                for table in STOCK_TABLES:
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE run_id = :n"), {"n": new})
        out["materialize_every_series_s"] = every
        out["materialize_every_series_rows"] = sum(c[2] for c in csvs.values())
    finally:
        eng.dispose()
    out["wall_s"] = round(time.perf_counter() - t_all, 2)
    out["build_estimate"] = build_estimate(params, cal, sc, state, report)
    return out


def build_estimate(params: dict, cal: dict, sc: SetCfg, state: dict, report: dict) -> dict:
    """The template build (days 0..open-1), estimated from parts: one day's step_day over every series in three
    worlds (timed here, on the open-day snapshot with the open day's public units), plus the screen (screened lots
    per day x the mean per-screen time of the screen step x the two worlds that screen: ShelfShift and the
    baseline rule), plus the catalogue's evaluations (N x the median case time of the catalogue step)."""
    response, open_day = _response(params), cal["demo_open_day"]
    t = time.perf_counter()
    for s in state.values():
        product = dataclasses.replace(sc.product, sku=s.product)
        demand = {s.store: s.units[open_day - s.first]}
        for _ in WORLDS:
            lots = [ShelfLot(l[0], s.store, l[2], l[3], l[4], l[5], recorded=l[1]) for l in s.lots]
            step_day(lots, open_day, demand, response, product, schedule=sc.schedule)
    step_all = time.perf_counter() - t
    scr = report.get("screen", {}).get(sc.name, {})
    cat = report.get("catalogue", {})
    screened, per_screen = scr.get("screened"), (scr.get("per_screen_ms") or {}).get("mean")
    screen_day = screened * per_screen / 1000 * 2 if screened is not None and per_screen is not None else None
    evals = (cat.get("evaluated") or 0) * ((cat.get("case_s") or {}).get("median") or 0)
    days = open_day
    total = days * (step_all + (screen_day or 0)) + evals
    return {"set": sc.name, "days": days, "step_day_all_series_3_worlds_s": round(step_all, 2),
            "screen_per_day_s": round(screen_day, 2) if screen_day is not None else None,
            "screened_per_day": screened, "per_screen_ms_mean": per_screen,
            "catalogue_evaluations_s": round(evals, 2), "total_s": round(total, 2),
            "total_h": round(total / 3600, 3),
            "method": "days x (step_day over every series in 3 worlds + screened lots x mean screen time x 2 "
                      "screening worlds) + catalogue N x median case time; the open day's screened count stands in "
                      "for each warm-up day's; local, single process, a lower bound for a Cloud Run job"}


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frn.py measure", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["measure"])
    parser.add_argument("step", choices=STEPS)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--raw", type=pathlib.Path, help="the folder holding the dataset files (stock step)")
    source.add_argument("--csv", type=pathlib.Path, help="a checked CSV in the dataset schema (tests)")
    parser.add_argument("--fixture", type=pathlib.Path, help="a fixture's override JSON (dataset and calendar)")
    parser.add_argument("--params", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True, help="the JSON report; each step merges into it")
    parser.add_argument("--state", type=pathlib.Path, help="folder for state files (default: next to --out)")
    parser.add_argument("--db", help="the scratch database URL (db step)")
    parser.add_argument("--limit", type=int, help="stock step: stop after this many series (a partial report)")
    args = parser.parse_args(argv)
    state_dir = args.state or args.out.parent / "measure-state"
    try:
        cfg = public_config.load_for(args.fixture) if args.fixture else public_config.load()
        cal = cfg["calendar"]
        params = load_params(args.params)
        sets = {name: set_cfg(params, name, cal) for name in params["sets"]}
        report = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
        before = machine()
        if args.step == "stock":
            if args.raw is None and args.csv is None:
                parser.error("the stock step needs --raw or --csv")
            series = public.series_iter(args.raw, cfg, csv_path=args.csv)
            block = run_stock(series, params, cal, sets, state_dir, args.limit)
        elif args.step == "screen":
            block = run_screen(params, cal, sets, state_dir)
        elif args.step == "latency":
            block = run_latency(params, cal, sets, state_dir)
        elif args.step == "catalogue":
            block = run_catalogue(params, cal, sets, state_dir)
        else:
            if not args.db:
                parser.error("the db step needs --db")
            block = run_db(params, cal, sets, state_dir, args.db, report)
    except (public.PublicDataError, config.ConfigError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    report["params"] = params
    report[args.step] = block
    report.setdefault("runs", {})[args.step] = {"before": before, "after": machine()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(args.out, json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(f"measure {args.step}: written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
