"""Shared reference tables: loaded once per dataset_version by the deploy job, read-only afterwards.

The app never queries BigQuery: Phase 3 exports the forecast runs into data/reference/ and this module loads that
export (spec §4 Data flow). `install` reads seed.json, forecast.json and truth.json, refuses a set that is not one
consistent release, and stores seed and forecast in ref_data (every request may read it) and truth in ref_truth
(physical stock, hidden demand and the hidden price response, read only by advance-day and metrics). The
dataset_version is Phase 3's `data.versions.dataset_version`, never re-derived here.

The set is read once with Phase 3's `data.reference.load`. Refused, each with a ValueError naming the reason: a
duplicate daily-sales, forecast-point, capacity or run row (Carried IN-05), a seed built from another reference/ (its
`reference_fingerprint` is not `data.config.fingerprint()`, so the case-opening thresholds would not match the
dataset), a truth whose `input_version` is not the seed's, and then Phase 3's gate: with `allow_local` False (the
deploy path) `data.reference.release_check` (ReleaseError: the local stand-in, any other provider, a forecast from
another sales history, mismatched truth, a changed reference/, a non-demo world), otherwise `data.reference.check`
(the horizon-coverage gate; local builds and tests). release_check's `note` (the seed moved outside the forecast
basis, the forecast still matching its sales history) is not a refusal (04-CONTEXT D-13, 03-05 phase_4_handoff).

`get` and `truth` are cached per dataset_version (the last CACHED versions). A miss loads under one module lock, so
concurrent misses load once, and on the caller's own connection when it is inside a transaction (`db.current`), so a
cold request never checks out a second pool connection while holding one (WR-10: with every connection taken by cold
requests, each would wait on itself until the checkout timed out).

`get` exposes everything the API derives from the set as read: the engine Products and Runs, capacity, each forecast
point with its provider, the seeded Policy (every field of seed.json `policy`), each product's markdown schedule, the
branches, the currency and the label. No value is re-derived in code (data, not hardcoding).

A public artifact set (a directory with manifest.json; 05.2 D-08, D-11) is installed and read by api/publicref.py
instead: `install` dispatches on the manifest, and `get` and `truth` return a PublicRef or PublicTruth for a version
listed in ref_set, read one series at a time. Both kinds answer the same methods (runs_from, run_on, free_totes,
forecast, provider_of, sale, lots0, inbound0; truth: demand_for, physical_of, price_response), and every call site
reads reference data through them.
"""
from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass
from datetime import date, timedelta

from data import config as refconfig
from data import forecast as F
from data import reference
from engine.model import MarkdownResponse, Policy, Product, Run

from . import db, publicref

REF = reference.REF  # data/reference/
PARTS = reference.PARTS  # the dataset_version order (data.versions)
UNIQUE_ROWS = (  # (file, list, key fields): one row per key (Carried IN-05)
    ("seed", "daily_sales", ("branch", "sku", "day")),
    ("forecast", "points", ("forecast_run_id", "branch", "sku", "day")),
    ("seed", "capacity", ("branch", "zone", "day")),
    ("seed", "runs", ("run_code", "day")),
)


def _refuse_duplicates(parts: dict) -> None:
    for part, name, key in UNIQUE_ROWS:
        seen = set()
        for row in parts[part][name]:
            k = tuple(row[f] for f in key)
            if k in seen:
                raise ValueError(f"{part}.json {name}: duplicate row {dict(zip(key, k))}")
            seen.add(k)


def install(ref_dir: pathlib.Path | str = REF, allow_local: bool = True) -> str:
    """Validate and load a reference set; returns its dataset_version. Loading the same version again is a no-op."""
    if (pathlib.Path(ref_dir) / publicref.MANIFEST).exists():  # a public artifact set: shared tables (05.2-12)
        return publicref.install(ref_dir, allow_local)
    ref = reference.load(ref_dir)
    parts = {name: ref[name] for name in PARTS}
    seed, truth = ref["seed"], ref["truth"]
    _refuse_duplicates(parts)
    built_from, here = seed["dataset"].get("reference_fingerprint"), refconfig.fingerprint()
    if built_from != here:
        raise ValueError(f"seed.json was built from reference {built_from}, not this reference/ ({here}): "
                         "regenerate it with python -m data.seed")
    if truth.get("input_version") != seed["dataset"].get("input_version"):
        raise ValueError(f"truth.json input_version {truth.get('input_version')} is not seed.json's "
                         f"{seed['dataset'].get('input_version')}: truth is from another world")
    if allow_local:
        reference.check(ref)  # CoverageError (a ValueError)
    else:
        reference.release_check(ref)  # ReleaseError or CoverageError (both ValueErrors); its note is not a refusal
    dv = ref["dataset_version"]
    with db.tx() as conn:
        for part in ("seed", "forecast"):
            db.run(conn, "INSERT INTO ref_data (dataset_version, part, payload) VALUES (:v, :p, CAST(:x AS jsonb)) "
                         "ON CONFLICT DO NOTHING", v=dv, p=part, x=db.js(parts[part]))
        db.run(conn, "INSERT INTO ref_truth (dataset_version, payload) VALUES (:v, CAST(:x AS jsonb)) "
                     "ON CONFLICT DO NOTHING", v=dv, x=db.js(truth))
    return dv


@dataclass
class Ref:
    dataset_version: str
    seed: dict
    forecast: dict
    products: dict[str, Product]
    names: dict[str, str]
    runs: list[Run]
    capacity: dict[tuple[str, str, int], int]
    points: dict[tuple[int, str, str], dict[int, tuple[float, float, float]]]  # (demo day, branch, sku) -> day -> l/b/h
    point_provider: dict[tuple[int, str, str, int], str]  # (demo day, branch, sku, day) -> the point's provider
    forecast_runs: dict[int, dict]  # demo day -> run metadata (forecast_run_id, cutoff_day, providers, fallbacks, ...)
    sales: dict[tuple[str, str, int], float]  # reference history: recorded sales
    markdown: MarkdownResponse
    schedules: dict[str, tuple[tuple[int, int], ...]]  # sku -> (days before expiry, pct) steps, as seeded
    policy: Policy  # the seeded Policy; a sandbox's D-09 writes overlay it
    branches: list[str]
    currency: str
    label: str

    kind = "synthetic"  # a public set's Ref is publicref.PublicRef; both answer the shared methods below (05.2-12)

    @property
    def calendar(self) -> dict:
        return self.seed["calendar"]

    @property
    def settings(self) -> dict:
        """The set's own settings (seed.json `settings`: currency and label); a public set's are its manifest's, which
        also carry `evaluation`, `board` and `series` (05.2 D-11, D-25, D-26). A key the set lacks keeps today's
        behaviour: eager case opening, every case on one page."""
        return self.seed["settings"]

    def date(self, day: int) -> date:
        """The calendar date of day index `day`: the dataset calendar's day0 plus `day` (05.2 D-13; the synthetic
        seed's day0 is reference/world.json's)."""
        return date.fromisoformat(self.calendar["day0"]) + timedelta(days=day)

    data_as_of = None  # only a public set has a scenario date for the mark (05.2 D-04)

    # The shared methods: every call site reads reference data through these, so a public set (publicref.PublicRef,
    # read per series) and this one read alike. `forecast(day, store, sku)` is answered by the forecast block itself
    # (publicref.Called), which stays a dict as before.

    def runs_from(self, origin: str, day: int) -> list[Run]:
        """The runs leaving `origin` on `day`, in seed order."""
        return [r for r in self.runs if r.origin == origin and r.day == day]

    def run_on(self, run_code: str, day: int) -> Run | None:
        return next((r for r in self.runs if r.run_code == run_code and r.day == day), None)

    def free_totes(self, store: str, zone: str, day: int) -> int | None:
        return self.capacity.get((store, zone, day))

    def provider_of(self, day: int, store: str, sku: str, point_day: int) -> str | None:
        return self.point_provider.get((day, store, sku, point_day))

    def sale(self, store: str, sku: str, day: int) -> float | None:
        return self.sales.get((store, sku, day))

    def store_known(self, store: str) -> bool:
        """Whether the store has a location and routes (05.2 D-31): every branch of the synthetic seed has."""
        return store in self.branches

    @property
    def series(self) -> dict:
        """The one-series history route's limits (05.2 D-11): the longest window is the calendar's whole span, from
        the first history day to the horizon end (the seed's own calendar, never a constant); no statement timeout
        (the seed is in memory)."""
        return {"max_days": int(self.calendar["horizon_end"]) - int(self.calendar["first_history_day"]) + 1,
                "statement_timeout_ms": None}

    def history(self, store: str, sku: str, from_day: int, to_day: int) -> list[dict]:
        """One series' recorded daily sales on the history days (before day 0) in [from_day, to_day], in day order:
        {day, units, stockout, discount, unknown_price, holiday, activity, weather}. The seed has no discount,
        holiday, activity or weather columns, so those are null; a demo day's sales come from the worlds' outcomes,
        never from the seed (as `sale`)."""
        return [{"day": r["day"], "units": r["recorded"], "stockout": r["stockout_flag"], "discount": None,
                 "unknown_price": None, "holiday": None, "activity": None, "weather": None}
                for r in sorted(self.seed["daily_sales"], key=lambda r: r["day"])
                if r["branch"] == store and r["sku"] == sku and from_day <= r["day"] <= min(to_day, -1)]

    def lots0(self, store: str | None = None, sku: str | None = None) -> list[dict]:
        """The seed's day-0 lots, in seed order; of one store or product when given."""
        return [l for l in self.seed["lots"] if store in (None, l["branch"]) and sku in (None, l["sku"])]

    def inbound0(self, store: str | None = None, sku: str | None = None) -> list[dict]:
        return [i for i in self.seed["inbound"] if store in (None, i["branch"]) and sku in (None, i["sku"])]

    @property
    def last_review_day(self) -> int:
        """The last day whose morning reopens a waiting case: advance_day runs the case morning only up to it, so a
        wait's review day may not be later (G-05-13). The calendar's last_demo_day of this dataset_version (reference
        data), never a constant."""
        return int(self.calendar["last_demo_day"])


CACHED = 4  # dataset versions kept in memory (a deploy serves one; the build job may add the next)
_lock = threading.Lock()  # one load at a time, so concurrent cold requests load once
_refs: dict[str, "Ref"] = {}
_truths: dict[str, dict] = {}


def _read(sql: str, **params) -> list[dict]:
    """Rows read on the caller's connection when this thread is inside a transaction, else in a short one."""
    conn = db.current()
    if conn is not None:
        return db.rows(conn, sql, **params)
    with db.tx() as own:
        return db.rows(own, sql, **params)


def _cached(cache: dict, dataset_version: str, load):
    found = cache.get(dataset_version)
    if found is not None:
        return found
    with _lock:
        if dataset_version not in cache:
            cache[dataset_version] = load(dataset_version)
            while len(cache) > CACHED:
                cache.pop(next(iter(cache)))
        return cache[dataset_version]


def get(dataset_version: str) -> Ref:
    """The reference set of a dataset_version, cached (WR-10: loaded once, on the caller's connection)."""
    return _cached(_refs, dataset_version, _load_ref)


def _load_ref(dataset_version: str) -> Ref:
    public = publicref.kind_of(dataset_version)  # a public set is read per series (05.2 D-11)
    if public is not None:
        return publicref.PublicRef(dataset_version, public["manifest"])
    parts = {r["part"]: r["payload"] for r in _read("SELECT part, payload FROM ref_data WHERE dataset_version = :v",
                                                    v=dataset_version)}
    seed, fc = parts["seed"], parts["forecast"]
    by_run = {r["forecast_run_id"]: r for r in fc["runs"] if r["job"] != F.BACKTEST}
    points: dict = {}
    provider: dict = {}
    for p in fc["points"]:
        run = by_run.get(p["forecast_run_id"])
        if run is not None:
            points.setdefault((run["demo_day"], p["branch"], p["sku"]), {})[p["day"]] = (p["low"], p["base"], p["high"])
            provider[(run["demo_day"], p["branch"], p["sku"], p["day"])] = p["provider"]
    md = seed["markdown_assumption"]
    return Ref(
        dataset_version, seed, publicref.Called(fc, lambda day, store, sku: dict(points.get((day, store, sku), {}))),
        products={p["sku"]: Product(p["sku"], *(p[f] for f in refconfig.PRODUCT_FIELDS)) for p in seed["products"]},
        names={p["sku"]: p["name"] for p in seed["products"]},
        runs=[Run(r["run_code"], r["origin"], r["dest"], r["day"], r["loading_cutoff_min"], r["departs_min"],
                  r["arrives_min"], r["spare_totes"], r["cold_chain"], r["fixed_cost"], r["per_unit_cost"],
                  cancel_cost=r["cancel_cost"]) for r in seed["runs"]],
        capacity={(c["branch"], c["zone"], c["day"]): c["free_totes"] for c in seed["capacity"]},
        points=points,
        point_provider=provider,
        forecast_runs={r["demo_day"]: r for r in by_run.values()},
        sales={(s["branch"], s["sku"], s["day"]): s["recorded"] for s in seed["daily_sales"]},
        markdown=MarkdownResponse(tuple(md["m20"]), tuple(md["m30"])),
        schedules={p["sku"]: tuple(tuple(step) for step in p["markdown_schedule"]) for p in seed["products"]},
        policy=Policy(**seed["policy"]),
        branches=list(seed["branches"]),
        currency=seed["settings"]["currency"],
        label=seed["dataset"]["label"],
    )


class Truth(dict):
    """The synthetic hidden truth: the dict it was (demand, physical, price_response by key) that also answers the
    methods publicref.PublicTruth answers, so advance-day reads both kinds alike (05.2-12)."""

    kind = "synthetic"

    def __init__(self, payload: dict):
        super().__init__(payload)
        self._by_day: dict[tuple[str, int], dict[str, float]] = {}
        for (branch, sku, day), demand in self["demand"].items():
            self._by_day.setdefault((sku, day), {})[branch] = demand

    def demand_for(self, sku: str, day: int) -> dict[str, float]:
        """{branch: hidden demand} of one product on one day."""
        return dict(self._by_day.get((sku, day), {}))

    def demand_of_products(self, skus, day: int) -> dict[str, dict[str, float]]:
        """{sku: demand_for(sku, day)} for several products (in memory here; one read per batch on a public set)."""
        return {sku: self.demand_for(sku, day) for sku in skus}

    def physical_of(self, lot_ids) -> dict[str, float]:
        """{lot_id: physical qty} for the ids truth lists; an id it does not list is left out."""
        return {i: self["physical"][i] for i in lot_ids if i in self["physical"]}

    @property
    def price_response(self) -> dict:
        return self["price_response"]


def truth(dataset_version: str) -> dict:
    """Hidden truth. Only advance-day and metrics call this; no agent tool can reach it (spec §5). Cached as `get`."""
    return _cached(_truths, dataset_version, _load_truth)


def _load_truth(dataset_version: str) -> dict:
    public = publicref.kind_of(dataset_version)
    if public is not None:
        return publicref.PublicTruth(dataset_version, public["manifest"])
    payload = _read("SELECT payload FROM ref_truth WHERE dataset_version = :v", v=dataset_version)[0]["payload"]
    return Truth({"demand": {(t["branch"], t["sku"], t["day"]): t["demand"] for t in payload["demand"]},
                  "price_response": payload["price_response"], "physical": payload["physical"]})


get.cache_clear = _refs.clear  # as functools.lru_cache names it (tests rebuild the schema)
truth.cache_clear = _truths.clear
