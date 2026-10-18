"""Seed generator: python -m data.seed --scenario demo|evaluation --seed N --out DIR (spec §5 Synthetic world).

Deterministic from the seed number. Writes the public reference (seed.json) and the hidden truth (truth.json).
Forecasts are produced separately (data.forecast), because they come from BigQuery.

History is simulated, not drawn: latent demand is sold from the stock that regular deliveries put on the shelf, with
`engine.world.step_day`, so recorded sales understate demand on stockout days exactly as in real data (DATA-01;
spec §5 Demand). Daily demand is Poisson(base × weekday profile × F), with F lognormal of mean 1 and σ from
reference data (Seed calibration 1).

Every regular delivery is sized from the store's record only (DATA-02): the in-stock estimate of each day of the
delivery cycle (`data.estimator`), summed, times a multiplier drawn in the reference range, rounded down once, then
up to whole order cases (an estimate of 1 unit or more orders at least one case, 0 orders nothing; 03-CONTEXT D-01,
D-02). Hidden demand and the model mean never feed a size; the model's base rate is offered only as the opening value
of the burn-in, an initialization that knows the mean, during the 56 simulated days before day −90 that are then
discarded (D-03).

Each product's markdown schedule (A for milk, B for strawberries and bread; 03-CONTEXT D-20) goes to `step_day`
through its `schedule` keyword, never as price changes (ESL-01). The history records the units sold at full price,
20% and 30% off per branch × SKU × day, with `stockout_flag` and `markdown_flag` (D-05).

The demo scenario adds its designed lots and skips on top of the same natural world (D-02, D-12). A designed story
lot is sized on its receipt day from the record, like a regular delivery, but over its whole selling window: floor(Σ
in-stock estimate over the receipt day .. the expiry day) + its `extra`, never rounded to cases. A phantom designed
lot has its `extra` as the record and nothing on the shelf (spec §5). Designed lots never carry a record error. Every
lot and inbound row says where it came from: `origin` "designed" with its story `tag`, or "natural" with tag None.
Natural surplus is left as it falls (Seed calibration 7).

Every random draw comes from its own `random.Random` keyed by a string (kind, seed, branch, SKU, day), so the output
is the same under every PYTHONHASHSEED and a skipped delivery never shifts a later draw. No retailer value is a code
literal: every number comes from `reference/` through `data.config`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import pathlib
import random
import sys
from dataclasses import dataclass, field
from datetime import date

from engine.allocator import ShelfLot
from engine.world import step_day

from . import config, estimator, versions
from . import world as W

OUT = pathlib.Path(__file__).resolve().parent / "reference"
EPS = 1e-9
SOLD_TOLERANCE = 1e-6  # per-discount units must add up to step_day's sold for the branch


def poisson(rng: random.Random, lam: float) -> int:
    if lam < 30:  # Knuth
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rng.random()
            if p <= limit:
                return k
            k += 1
    return max(0, round(rng.gauss(lam, math.sqrt(lam))))


def _rng(kind: str, seed: int, *key) -> random.Random:
    """A draw stream keyed by a string, so it is the same under every PYTHONHASHSEED."""
    return random.Random(f"{kind}:{seed}:" + ":".join(map(str, key)))


def _weekday(cfg: dict, day: int) -> int:
    """0 = Monday .. 6 = Sunday for day index `day`."""
    return (date.fromisoformat(cfg["world"]["calendar"]["day0"]).weekday() + day) % 7


def latent_demand(seed: int, cfg: dict) -> tuple[dict, dict]:
    """(demand, mean) keyed (branch, sku, day) for every day from the burn-in start to the horizon end.

    mean = base demand × normalised weekday profile; demand = Poisson(mean × F), F = lognormvariate(−σ²/2, σ), both
    drawn from the (seed, branch, SKU, day) stream. This is the hidden truth; sizing never reads it."""
    profile = config.weekday_profile(cfg)
    sigma = cfg["world"]["demand"]["sigma"]
    demand, mean = {}, {}
    for day in range(W.burn_in_start(cfg), W.horizon_end(cfg) + 1):
        for sku, by_branch in cfg["world"]["demand"]["base_demand"].items():
            for branch in cfg["world"]["branches"]:
                mu = by_branch[branch] * profile[_weekday(cfg, day)]
                rng = _rng("demand", seed, branch, sku, day)
                f = rng.lognormvariate(-sigma ** 2 / 2, sigma)
                demand[(branch, sku, day)] = poisson(rng, mu * f)
                mean[(branch, sku, day)] = mu
    return demand, mean


def delivery_calendar(seed: int, scenario: str, cfg: dict) -> list[dict]:
    """The deliveries of a scenario, regular and designed, in receipt order, before sizing.

    Each branch × SKU receives every `delivery_cycle_days` from the burn-in start to the last demo day, aligned on its
    designed lot's receipt day when the scenario has one, else on the branch's index in reference order. No regular
    delivery arrives while a designed lot of that branch × SKU lives, nor on a scenario skip day. Each designed lot of
    the scenario is one more row on its receipt day: kind "phantom" when its options say so, else "story"; origin
    "designed" with its tag and options (copied, never shared with the loaded config). Regular rows are kind "regular",
    origin "natural", tag None. Rows are sorted by (day, branch order, SKU); received_seq = day × 1000 + index within
    the day, so FEFO ties are stable; regular lot ids are the product's prefix and a 3-digit counter from 100,
    skipping ids a designed lot uses; a designed lot keeps its own id."""
    world, sc = cfg["world"], cfg["scenarios"][scenario]
    branches = list(world["branches"])
    designed, skips = sc["designed_lots"], sc["skips"]
    start, last = W.burn_in_start(cfg), world["calendar"]["last_demo_day"]
    rows: list[dict] = []
    for b_i, branch in enumerate(branches):
        for sku, product in world["products"].items():
            cycle, life = product["delivery_cycle_days"], product["shelf_life_days"]
            mine = sorted((d for d in designed.values() if d["branch"] == branch and d["sku"] == sku),
                          key=lambda d: d["received_day"])
            offset = mine[0]["received_day"] % cycle if mine else b_i % cycle
            for day in range(start, last + 1):
                if (day - offset) % cycle:
                    continue
                if any(d["received_day"] <= day <= d["received_day"] + life for d in mine):
                    continue
                if any(s["branch"] == branch and s["sku"] == sku and s["first_day"] <= day <= s["last_day"]
                       for s in skips.values()):
                    continue
                rows.append({"branch": branch, "sku": sku, "day": day, "kind": "regular", "cycle": cycle,
                             "u": _rng("mult", seed, branch, sku, day).random(), "origin": "natural", "tag": None})
    for lot_id, d in designed.items():
        options = dict(d.get("options", {}))
        rows.append({"lot_id": lot_id, "branch": d["branch"], "sku": d["sku"], "day": d["received_day"],
                     "kind": "phantom" if options.get("phantom") else "story",
                     "cycle": world["products"][d["sku"]]["delivery_cycle_days"], "extra": d["extra"],
                     "options": options, "origin": "designed", "tag": d["tag"]})
    rows.sort(key=lambda r: (r["day"], branches.index(r["branch"]), r["sku"]))
    used = set(designed)
    counters: dict[str, int] = {}
    index_in_day: dict[int, int] = {}
    for r in rows:
        i = index_in_day.get(r["day"], 0)
        index_in_day[r["day"]] = i + 1
        r["received_seq"] = r["day"] * 1000 + i
        r["expiry_day"] = r["day"] + world["products"][r["sku"]]["shelf_life_days"]
        if r["origin"] == "designed":  # a designed lot keeps its reference id
            continue
        prefix = world["products"][r["sku"]]["lot_prefix"]
        n = counters.get(prefix, 100)
        while f"{prefix}-{n:03d}" in used:
            n += 1
        r["lot_id"] = f"{prefix}-{n:03d}"
        used.add(r["lot_id"])
        counters[prefix] = n + 1
    return rows


def regular_qty(e_sum: float, u: float, multiplier, order_case: int) -> int:
    """A regular delivery's quantity: floor(ΣE × (lo + u × (hi − lo))), then up to whole cases (03-CONTEXT D-01,
    D-02). The multiplier applies to the unrounded sum, which is rounded once; 0 orders nothing."""
    lo, hi = multiplier
    q = math.floor(e_sum * (lo + u * (hi - lo)))
    if q <= 0:
        return 0
    return order_case * -(-q // order_case)


def opening_value(cfg: dict, opening_factor: float, branch: str, sku: str, order_day: int) -> float | None:
    """The burn-in's opening value (opening_factor × the model's base rate), offered only for order days before the
    first history day (−91 yes, −90 no). It is an initialization that knows the mean, and its days are discarded."""
    if order_day >= W.first_history_day(cfg):
        return None
    return opening_factor * cfg["world"]["demand"]["base_demand"][sku][branch]


@dataclass
class World:
    """One simulated world from the burn-in start to the last demo day, with the internals reports need."""

    seed: int
    scenario: str
    params: dict
    demand: dict
    mean: dict
    deliveries: list[dict] = field(default_factory=list)
    days: list[dict] = field(default_factory=list)
    expired: list[dict] = field(default_factory=list)
    snapshots: dict[int, list[dict]] = field(default_factory=dict)
    init_failed: list[dict] = field(default_factory=list)


def run_world(seed: int, scenario: str = "evaluation", cfg: dict | None = None, *, multiplier=None,
              opening_factor: float | None = None) -> World:
    """Simulate one world: size each delivery on its arrival day from the record so far, then step every SKU through
    `step_day` with its product's markdown schedule and record the day's sales by discount level."""
    cfg = config.load() if cfg is None else cfg
    if scenario not in cfg["scenarios"]:
        raise config.ConfigError(f"scenarios.{scenario}: no such scenario")
    world = cfg["world"]
    ordering = world["ordering"]
    branches = list(world["branches"])
    multiplier = tuple(ordering["delivery_multiplier_release"] if multiplier is None else multiplier)
    opening_factor = ordering["opening_factor"] if opening_factor is None else opening_factor
    lookback, same_weekday = ordering["lookback_days"], ordering["same_weekday_obs"]
    hidden = config.load_hidden()
    m_truth = {20: hidden["m20"], 30: hidden["m30"]}
    prods = config.products(cfg)
    schedules = {}
    for sku in prods:
        schedules[sku] = config.product_schedule(cfg, sku)
        if not schedules[sku]:  # 02-CONTEXT D-36: the engine-2 no-schedule walk never runs on Phase 3 data
            raise config.ConfigError(f"policy.product_schedule.{sku}: every product needs a markdown schedule")
    demand, mean = latent_demand(seed, cfg)
    rows = delivery_calendar(seed, scenario, cfg)
    out = World(seed, scenario, {"multiplier": list(multiplier), "opening_factor": opening_factor,
                                 "schedules": {s: [list(step) for step in sch] for s, sch in schedules.items()}},
                demand, mean, deliveries=rows)
    start, last = W.burn_in_start(cfg), world["calendar"]["last_demo_day"]
    snapshot_days = {0, world["calendar"]["demo_open_day"]}  # day 0 (the seed's lots) and the demo's opening day
    errors = world["error_model"]
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    rec: dict = {}  # the store's recorded sales, (branch, sku, day) -> units (2 places)
    so: dict = {}  # the store's recorded stockout flags
    lots: dict[str, list[ShelfLot]] = {sku: [] for sku in prods}
    for day in range(start, last + 1):
        if day in snapshot_days:  # the start of the day: before its 06:00 arrivals and its sales
            out.snapshots[day] = [{"lot_id": l.lot_id, "branch": l.branch, "sku": sku, "qty": l.qty,
                                   "recorded": l.recorded, "expiry_day": l.expiry_day,
                                   "received_seq": l.received_seq, "pct": l.pct}
                                  for sku, ls in lots.items() for l in ls]
        arrivals: dict[str, list[ShelfLot]] = {sku: [] for sku in prods}
        for r in by_day.get(day, ()):
            b, s = r["branch"], r["sku"]
            if r["kind"] == "phantom":  # spec §5: the record is `extra`, nothing is on the shelf; no estimate
                r["qty"], r["physical"], r["estimate"] = r["extra"], 0.0, None
                arrivals[s].append(ShelfLot(r["lot_id"], b, 0.0, r["expiry_day"], r["received_seq"],
                                            recorded=float(r["qty"])))
                continue
            # a regular delivery covers its cycle; a story lot covers its selling window, receipt day to expiry day
            span = range(day, day + r["cycle"]) if r["kind"] == "regular" else range(day, r["expiry_day"] + 1)
            opening = opening_value(cfg, opening_factor, b, s, day)
            es = [estimator.estimate(rec, so, b, s, x, day, start, opening, lookback=lookback,
                                     same_weekday=same_weekday) for x in span]
            for x, (_, tier) in zip(span, es):
                if tier == "init_failed":
                    out.init_failed.append({"branch": b, "sku": s, "order_day": day, "target_day": x})
            e_sum = sum(e for e, _ in es)
            if r["kind"] == "regular":
                q = regular_qty(e_sum, r["u"], multiplier, world["products"][s]["order_case"])
            else:  # story: floor(ΣE) + extra, never rounded to cases (DATA-02; D-02)
                q = math.floor(e_sum) + r["extra"]
            r["qty"] = q
            r["physical"] = float(q)
            # the declared record-error model: only regular lots received from `from_day` on; the first draw decides,
            # the second scales the record down within the range, rounded down to whole units
            if r["kind"] == "regular" and day >= errors["from_day"] and q > 0:
                err = _rng("error", seed, b, s, day)
                if err.random() < errors["share"]:
                    lo_e, hi_e = errors["range"]
                    r["physical"] = float(math.floor(q * (lo_e + err.random() * (hi_e - lo_e))))
            # diagnostics only, after the size is fixed: the model mean never feeds a size
            r["estimate"] = {"e_sum": e_sum, "mean_sum": sum(mean[(b, s, x)] for x in span),
                             "tiers": [tier for _, tier in es]}
            if q > 0:
                arrivals[s].append(ShelfLot(r["lot_id"], b, r["physical"], r["expiry_day"], r["received_seq"],
                                            recorded=float(q)))
        for sku, product in prods.items():
            cur = lots[sku]
            before = {id(l): (l, l.qty) for l in (*cur, *arrivals[sku])}
            day_demand = {b: float(demand[(b, sku, day)]) for b in branches}
            res = step_day(cur, day, day_demand, m_truth, product, arrivals=tuple(arrivals[sku]),
                           schedule=schedules[sku])
            sold = {b: {0: 0.0, 20: 0.0, 30: 0.0} for b in branches}
            for lot, q0 in before.values():  # no shipments in this world, so a qty drop is a sale
                sold[lot.branch][lot.pct] += q0 - lot.qty
            left = {id(l) for l in cur}
            for lot, _ in before.values():
                if id(lot) not in left:  # step_day removes a lot only at its expiry
                    out.expired.append({"lot_id": lot.lot_id, "branch": lot.branch, "sku": sku,
                                        "expiry_day": lot.expiry_day, "qty_left": lot.qty})
            for b in branches:
                if abs(sum(sold[b].values()) - res.sold[b]) > SOLD_TOLERANCE:
                    raise RuntimeError(f"{b} {sku} day {day}: sales by discount {sold[b]} do not add up to "
                                       f"step_day's {res.sold[b]}")
                full, d20, d30 = round(sold[b][0], 2), round(sold[b][20], 2), round(sold[b][30], 2)
                row = {"branch": b, "sku": sku, "day": day, "demand": day_demand[b],
                       "sold_full": full, "sold_20": d20, "sold_30": d30, "recorded": round(full + d20 + d30, 2),
                       "lost": res.lost[b], "wasted": res.wasted.get(b, 0.0), "revenue": res.revenue[b],
                       "disposal": res.disposal.get(b, 0.0), "stockout_flag": res.lost[b] > EPS,
                       "markdown_flag": sold[b][20] + sold[b][30] > EPS}
                out.days.append(row)
                rec[(b, sku, day)] = row["recorded"]
                so[(b, sku, day)] = row["stockout_flag"]
    return out


def _sort_key(branches: list[str]):
    return lambda r: (r["day"], branches.index(r["branch"]), r["sku"])


def generate(seed: int | None = None, scenario: str = "demo", *, multiplier=None,
             opening_factor: float | None = None) -> tuple[dict, dict]:
    """(seed.json, truth.json) as dicts. seed None means the demo seed; an evaluation world needs a seed number.

    Both carry the same `input_version` (data.versions), taken over the reference fingerprint and both bodies
    without their version fields; seed.json's `dataset` also records the reference fingerprint. Nothing here writes
    into the loaded config: every list or dict in the output is a new object."""
    cfg = config.load()
    if seed is None:
        if scenario != "demo":
            raise config.ConfigError(f"scenario {scenario!r}: a seed number is required")
        seed = cfg["scenarios"]["demo"]["seed"]
    wd = run_world(seed, scenario, cfg, multiplier=multiplier, opening_factor=opening_factor)
    world, cal = cfg["world"], cfg["world"]["calendar"]
    branches = list(world["branches"])
    first, end = W.first_history_day(cfg), W.horizon_end(cfg)
    keys = ("branch", "sku", "day", "recorded", "sold_full", "sold_20", "sold_30", "stockout_flag", "markdown_flag")
    daily_sales = sorted(({k: r[k] for k in keys} for r in wd.days if first <= r["day"]), key=_sort_key(branches))
    sc = cfg["scenarios"][scenario]
    by_id = {r["lot_id"]: r for r in wd.deliveries}

    def lot_fields(r: dict) -> dict:
        opts = r.get("options", {})
        return {"expiry_source": "ESTIMATED" if opts.get("estimated") else "PRINTED",
                "last_verified_day": opts.get("verified", r["day"]), "verified_by": "COUNT",
                "tag": r["tag"], "origin": r["origin"]}

    day0 = [l for l in wd.snapshots[0] if l["qty"] > EPS or l["recorded"] > EPS]
    lots = sorted(({"lot_id": l["lot_id"], "branch": l["branch"], "sku": l["sku"], "qty": l["recorded"],
                    "expiry_day": l["expiry_day"], "received_seq": l["received_seq"], "markdown_pct": l["pct"],
                    **lot_fields(by_id[l["lot_id"]])} for l in day0), key=lambda l: l["lot_id"])
    inbound = sorted(({"inbound_id": r["lot_id"], "branch": r["branch"], "sku": r["sku"], "qty": float(r["qty"]),
                       "arrival_day": r["day"], "arrival_min": cal["delivery_min"], "expiry_day": r["expiry_day"],
                       "received_seq": r["received_seq"], **lot_fields(r)}
                      for r in wd.deliveries if 0 <= r["day"] <= cal["last_demo_day"] and r["qty"] > 0),
                     key=lambda i: i["inbound_id"])
    products = []
    for sku, p in world["products"].items():
        steps = [list(step) for step in config.product_schedule(cfg, sku)]
        if not steps:  # 02-CONTEXT D-36: the seed validation asserts every product has a schedule
            raise config.ConfigError(f"products.{sku}: markdown_schedule is empty; every product needs one")
        products.append({"sku": sku, "name": p["name"], "zone": p["zone"], "price": p["price"],
                         "disposal_cost": p["disposal_cost"], "units_per_tote": p["units_per_tote"],
                         "kg_per_unit": p["kg_per_unit"], "shelf_life_days": p["shelf_life_days"],
                         "selling_window": p["selling_window"], "order_case": p["order_case"],
                         "delivery_cycle_days": p["delivery_cycle_days"], "markdown_schedule": steps})
    runs = sorted(({"run_code": code, "day": day, "origin": run["timetable"]["origin"],
                    "dest": run["timetable"]["dest"], "loading_cutoff_min": run["timetable"]["loading_cutoff_min"],
                    "departs_min": run["timetable"]["departs_min"], "arrives_min": run["timetable"]["arrives_min"],
                    "spare_totes": run["timetable"]["spare_totes"], "cold_chain": run["timetable"]["cold_chain"],
                    "fixed_cost": run["fixed_cost"], "per_unit_cost": run["per_unit_cost"],
                    "cancel_cost": run["cancel_cost"]}
                   for code, run in world["runs"].items() for day in run["timetable"]["days"]),
                  key=lambda r: (r["run_code"], r["day"]))
    capacity = [{"branch": b, "zone": zone, "day": day, "free_totes": by_branch[b]}
                for zone, by_branch in world["capacity"].items() for b in branches for day in range(0, end + 1)]
    phantoms = sorted(i for i, d in sc["designed_lots"].items() if d.get("options", {}).get("phantom"))
    errors = world["error_model"]
    md = cfg["policy"]["markdown_response"]
    policy = config.engine_policy(cfg)
    seed_json = {
        "dataset": {"scenario": scenario, "seed": seed, "label": cfg["policy"]["settings"]["synthetic_label"],
                    "multiplier": wd.params["multiplier"], "opening_factor": wd.params["opening_factor"]},
        "branches": branches,
        "products": products,
        "markdown_assumption": {"m20": list(md["m20"]), "m30": list(md["m30"])},
        "policy": {f.name: getattr(policy, f.name) for f in dataclasses.fields(policy)},
        "settings": {"currency": cfg["policy"]["settings"]["currency"],
                     "label": cfg["policy"]["settings"]["synthetic_label"]},
        "forecast": {k: world["forecast"][k] for k in ("model", "fallback", "confidence_level", "location",
                                                       "dataset")},
        "error_model": {"share": errors["share"], "range": list(errors["range"]), "from_day": errors["from_day"],
                        "scripted": phantoms},
        "lots": lots,
        "inbound": inbound,
        "runs": runs,
        "capacity": capacity,
        "calendar": {"day0": cal["day0"], "first_history_day": first, "burn_in_days": cal["burn_in_days"],
                     "history_days": cal["history_days"], "demo_open_day": cal["demo_open_day"],
                     "last_demo_day": cal["last_demo_day"], "horizon_end": end, "open_min": cal["open_min"],
                     "delivery_min": cal["delivery_min"], "open_within_days": cal["open_within_days"],
                     "backtest_days": cal["backtest_days"],
                     "case_no_start": cfg["scenarios"][scenario].get("case_no_start")},
        "daily_sales": daily_sales,
    }
    truth_json = {
        "demand": sorted(({"branch": b, "sku": s, "day": d, "demand": float(v)} for (b, s, d), v in wd.demand.items()
                          if -cal["backtest_days"] <= d <= end), key=_sort_key(branches)),
        "price_response": config.load_hidden(),
        # physical on hand where it differs from the record: day-0 lots as they stand, deliveries at receipt
        "physical": {**{l["lot_id"]: l["qty"] for l in day0 if abs(l["qty"] - l["recorded"]) > EPS},
                     **{i["inbound_id"]: by_id[i["inbound_id"]]["physical"] for i in inbound
                        if abs(by_id[i["inbound_id"]]["physical"] - i["qty"]) > EPS}},
    }
    if any(not p["markdown_schedule"] for p in seed_json["products"]):  # D-36, asserted on the output too
        raise config.ConfigError("seed.products: every product needs a non-empty markdown_schedule")
    # DATA-05: built without version fields, then stamped; no clock and no git in the hash
    fingerprint = config.fingerprint()
    version = versions.input_version(fingerprint, seed_json, truth_json)
    seed_json["dataset"]["input_version"] = truth_json["input_version"] = version
    seed_json["dataset"]["reference_fingerprint"] = fingerprint
    return seed_json, truth_json


def dumps(payload: dict) -> str:
    """The text seed.json and truth.json are written as: sorted keys, one-space indent, UTF-8, a final newline."""
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def write(out: pathlib.Path | str, seed_json: dict, truth_json: dict) -> None:
    """Write seed.json and truth.json into `out`, each atomically (a temp file, fsync, then os.replace)."""
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in (("seed.json", seed_json), ("truth.json", truth_json)):
        config.atomic_write(out / name, dumps(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m data.seed", description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=("demo", "evaluation"), default="demo")
    parser.add_argument("--seed", type=int, default=None, help="seed number (default: the demo seed)")
    parser.add_argument("--out", type=pathlib.Path, default=OUT, help=f"output directory (default: {OUT})")
    args = parser.parse_args(argv)
    # the release reference set holds the demo world only; an evaluation world goes elsewhere (T-3-09)
    if args.scenario == "evaluation" and args.out.resolve() == OUT.resolve():
        parser.error("refusing to write an evaluation world over the release reference set; pass --out")
    if args.seed is None and args.scenario != "demo":
        parser.error(f"--scenario {args.scenario} needs --seed")
    seed_json, truth_json = generate(args.seed, args.scenario)
    write(args.out, seed_json, truth_json)
    print(f"wrote {args.out}/seed.json ({len(seed_json.get('lots', []))} lots, "
          f"{len(seed_json.get('inbound', []))} deliveries, {len(seed_json['daily_sales'])} sales rows) and truth.json, "
          f"input_version {seed_json['dataset']['input_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
