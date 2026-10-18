"""The public set's load-ready parts: .venv-data/bin/python -I tools/frn.py stock ... (spec §5 Public dataset (rev 6.6);
DATA-08, DATA-11, DATA-14; 05.2-CONTEXT D-08, D-15, D-16, D-18, D-20, D-29..D-31).

Every one of the 50,000 FreshRetailNet-50K series is streamed once, in (store, product) code order, through
`data.public.series_iter` (D-01: none is subset away; D-11: memory is one sorted table plus one series). For each
series the stage writes, as gz CSV parts under `<out>/parts/` (git-ignored, never in git):

- `sales.csv.gz`: the public day rows. `units` = sale_amount x `units.factor` (D-20), rounded once to 6 places, an
  Assumption kept beside the public `sale_amount`; the hours out of stock, the stockout and unknown-price flags, the
  discount and the holiday and activity flags are the public values (D-14, D-17).
- `weather.csv.gz`: one row per store and day. Weather is a public column of every product row; when two products of
  one store-day disagree, the first product in code order is kept and the store-day is counted (`weather_conflicts`).
- `products.csv.gz`: one row per product code with its category codes and its category row's Assumptions (zone,
  price, disposal cost, tote, weight, shelf life, markdown schedule; D-33). `name` is always empty: names are never
  invented (D-02). A row without a base price writes an empty price (D-35: its money figures are unavailable).
- `lots0.csv.gz`, `inbound0.csv.gz`: the simulated record (`data.stock.simulate` with `public_config.stock_params`):
  the lots on hand at the start of day 0 and the regular deliveries of days 0..last_day (D-39: eval-day top-ups reach
  the inbound only when `stock.topup_on_eval_days` is true). Simulated, never observed (D-03).
- `truth_physical.csv.gz`: the physical quantity of every lot and delivery id (hidden truth).
- `truth_demand.csv.gz`: the held-out demand of days 0..last_day, which is the public eval units (D-15).

Stores and products are keyed by their display codes (`public_config.code`, D-02), the codes the engine Product and
the network parts use. The category level columns of `products` keep the dataset's codes; `category` is the display
code at `categories.level`.

The invariant (D-16): simulated sold equals the public units on every simulated day of every series. `stock.simulate`
raises when stock could not cover a day; the stage checks the history again and stops naming the series either way.

A designed example (D-18, D-40) exists only when reference data names one at `designed.verify_example` (store,
product, rule, label); reference/public.json does not set it, the user decides at 05.2-18. That one series is
simulated as usual (its record is unchanged) and every row it emits from day 0 has physical 0; its lots are tagged
`designed` and its lots and deliveries carry origin `designed`. It is counted apart (`designed_series`) and left out
of the consistency counters and of `sold_equals_public`.

`<out>/reports/public-consistency.json` holds aggregates only: how the simulated stock relates to the public sales
(top-up share, waste by category, simulated sell-out days beside public stockout days and their agreement, record
error lots, unknown-price rows, weather conflicts, the eval-day series-days the regular deliveries alone leave short),
peak memory and time. Consistency figures, reported and never tuned; never a match to observed stock.

`frn.py network` writes the local candidate network (`data.runs`, `public_config.network_params`): `stores.csv.gz`
(store, city, geo_known), `runs.csv.gz` (same city, within the service limit, at most k per store and day; truck
schedules are simulated) and `capacity.csv.gz` (every store, zone and day 0..horizon_end, unmapped stores included),
plus `<out>/reports/public-network.json`: pairs per source, runs per store per day, the cold-chain share and the
stores missing from the mapping, which get no run (D-31). Positions and cold-chain draws stay keyed by the dataset ids
as 05.2-05 measured them; the parts carry display codes.

`frn.py build` runs the whole pipeline into one versioned artifact set (05.2-10): the stock stage, the network stage,
the forecast stage (`data.public_forecast.forecast_stage`; `--forecast local` is the stand-in, development and tests
only) and then the manifest (`data.manifest.write_manifest`), and checks the set (`data.manifest.check`) before it
prints `ok <dataset_version>`. The stage reports, which hold times and memory, go to `--reports` (default
`<out>/reports`), so a set's own files repeat byte for byte:

    .venv-data/bin/python -I tools/frn.py build --raw public-data/raw --out public-data/out --forecast local
    .venv/bin/python tools/frn.py build --csv tests/data/fixtures/frn_sample_synthetic.csv \
        --fixture tests/data/fixtures/frn_sample_synthetic.json --out tests/data/fixtures/public_tiny \
        --forecast local --reports <a scratch folder>

`--forecast keep` (05.2-11) reruns the stock and network stages but no forecast provider: it reuses the forecast parts
already in `<out>/parts` (the user's `frn.py forecast --provider ai_forecast` run, D-22), refuses them when one is
missing, when the input part's bytes are not the ones its meta records, or when the input or any run was built on
other inputs than the reference gives now (reference_history_id), and then rewrites the manifest over every part's
bytes and checks the set:

    .venv-data/bin/python -I tools/frn.py build --raw public-data/raw --out public-data/out --forecast keep

Parts are byte-identical across runs: gzip with mtime 0, LF line ends, a header row naming the columns (`PARTS`),
floats as `repr(round(x, 6))`, booleans `true`/`false`, an empty string for null, nested values as compact JSON. Each
part replaces the previous one only after the whole stage succeeded. No retailer or dataset value is a code literal:
every value comes from the reference through `data.public_config`.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import dataclasses
import gzip
import io
import json
import os
import pathlib
import resource
import sys
import time
from collections import Counter, defaultdict

from engine.allocator import EPS

from . import config, public, public_config, runs, stock
from .public import PublicDataError

UNIT_PLACES = 6  # units and every written float are rounded once to 6 places
SHARE_PLACES = 4  # shares in the reports
GZIP_LEVEL = 6
WEATHER = ("precip", "temp", "humidity", "wind")
DESIGNED = "designed"  # the tag and origin of a designed example's lots (D-18, D-40)
DESIGNED_FIELDS = ("store", "product", "rule", "label")

PARTS = {
    "sales.csv.gz": ("store", "sku", "day", "units", "sale_amount", "oos_hours", "stockout", "discount",
                     "unknown_price", "holiday", "activity"),
    "weather.csv.gz": ("store", "day") + WEATHER,
    "products.csv.gz": ("sku", "mgmt", "cat1", "cat2", "cat3", "category", "zone", "price", "disposal_cost",
                        "units_per_tote", "kg_per_unit", "shelf_life_days", "schedule", "name"),
    "lots0.csv.gz": ("lot_id", "store", "sku", "qty", "expiry_day", "received_seq", "markdown_pct", "expiry_source",
                     "last_verified_day", "tag", "origin"),
    "inbound0.csv.gz": ("inbound_id", "store", "sku", "qty", "arrival_day", "arrival_min", "expiry_day",
                        "received_seq", "expiry_source", "last_verified_day", "origin"),
    "truth_physical.csv.gz": ("lot_id", "qty"),
    "truth_demand.csv.gz": ("store", "sku", "day", "demand"),
    "stores.csv.gz": ("store", "city", "geo_known"),
    "runs.csv.gz": ("run_code", "day", "origin", "dest", "loading_cutoff_min", "departs_min", "arrives_min",
                    "spare_totes", "cold_chain", "fixed_cost", "per_unit_cost", "cancel_cost", "travel_min"),
    "capacity.csv.gz": ("store", "zone", "day", "free_totes"),
}
STOCK_PARTS = ("sales.csv.gz", "weather.csv.gz", "products.csv.gz", "lots0.csv.gz", "inbound0.csv.gz",
               "truth_physical.csv.gz", "truth_demand.csv.gz")
NETWORK_PARTS = ("stores.csv.gz", "runs.csv.gz", "capacity.csv.gz")

CONSISTENCY_NOTE = ("Consistency figures of the simulated stock against the public sales; reported, not tuned; the stock "
                    "is simulated, never observed")
WEATHER_RULE = ("weather is a column of every product row; when products of one store-day disagree, the first product "
                "in code order is kept and the store-day is counted in weather_conflicts")
SHORT_NOTE = ("series-days of days 0..last_day on which the regular deliveries alone (after the simulation's earlier "
              "top-ups) would not cover the public units; the simulation tops them up so its record stays equal to "
              "the public units, and stock.topup_on_eval_days says whether those top-ups reach the worlds (D-39); a "
              "simulated result, never observed in the public dataset")
CATEGORY_CONFLICT_RULE = ("a product's category codes come from its first series in code order; a product whose codes "
                          "differ between stores is counted in product_category_conflicts")


NETWORK_NOTE = ("Assumed positions and runs (reference/geography.json); not observed; stores missing from the mapping are "
                "excluded from transfers and counted")
RUNS_LABEL = "a simulated truck schedule (D-34)"
DRAW_KEYS = ("positions and cold-chain draws are keyed by the dataset's store and city ids, as 05.2-05 measured them "
             "(geography positions.seed); the parts carry the display codes")


class PublicBuildError(ValueError):
    pass


# --------------------------------------------------------------------------------------------------- writing parts

def _cell(value) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, UNIT_PLACES) + 0.0)  # + 0.0 turns -0.0 into 0.0
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class _Part:
    """One gz CSV part written to a temp file beside its final path; `commit` renames it into place."""

    def __init__(self, parts_dir: pathlib.Path, name: str):
        self.name, self.columns = name, PARTS[name]
        self.path = parts_dir / name
        self.tmp = parts_dir / f".{name}.tmp"
        self._raw = open(self.tmp, "wb")
        self._gz = gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, mtime=0, compresslevel=GZIP_LEVEL)
        self._text = io.TextIOWrapper(self._gz, encoding="utf-8", newline="")
        self._csv = csv.writer(self._text, lineterminator="\n")
        self._csv.writerow(self.columns)
        self.rows = 0

    def write(self, values) -> None:
        self._csv.writerow([_cell(v) for v in values])
        self.rows += 1

    def close(self) -> None:
        if not self._raw.closed:
            self._text.close()  # closes the gzip stream (writes its trailer), not the file under it
            self._raw.flush()
            os.fsync(self._raw.fileno())
            self._raw.close()

    def commit(self) -> None:
        self.close()
        os.replace(self.tmp, self.path)

    def discard(self) -> None:
        self.close()
        self.tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def _parts(parts_dir: pathlib.Path, names):
    """{name: _Part}; every part is renamed into place only when the block finished, else every temp file goes."""
    parts_dir.mkdir(parents=True, exist_ok=True)
    opened = {}
    try:
        for name in names:
            opened[name] = _Part(parts_dir, name)
        yield opened
    except BaseException:
        for p in opened.values():
            p.discard()
        raise
    for p in opened.values():
        p.close()
    for p in opened.values():
        p.commit()


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1 << 20) if sys.platform == "darwin" else peak / (1 << 10), 1)  # bytes on macOS, KiB else


def _round(value: float) -> float:
    return round(value, UNIT_PLACES) + 0.0


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, SHARE_PLACES) if whole else None


def write_report(path: pathlib.Path | str, report: dict) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(path, config.canonical(report) + "\n")


# --------------------------------------------------------------------------------------------------- designed example

def designed_example(cfg: dict) -> dict | None:
    """The designed example reference data names at `designed.verify_example` ({store, product, rule, label}, the
    store and product as dataset ids or display codes), as {store, product (dataset ids), rule, label}; None when the
    reference names none (reference/public.json does not, D-40)."""
    block = cfg.get("designed")
    if block is None:
        return None
    example = block.get("verify_example") if isinstance(block, dict) else None
    if example is None:
        return None
    where = "designed.verify_example"
    if not isinstance(example, dict) or sorted(example) != sorted(DESIGNED_FIELDS):
        raise PublicBuildError(f"{where}: must hold exactly {', '.join(DESIGNED_FIELDS)}")
    out = {}
    for field in ("rule", "label"):
        if not isinstance(example[field], str) or not example[field].strip():
            raise PublicBuildError(f"{where}.{field}: a non-empty text is required (the example is labelled designed)")
        out[field] = example[field]
    for field, kind in (("store", "store"), ("product", "product")):
        value = example[field]
        if isinstance(value, str) and public_config.RAW_ID.fullmatch(value):
            out[field] = value
            continue
        try:
            out[field] = public_config.raw_id(cfg, kind, value)
        except config.ConfigError:
            raise PublicBuildError(f"{where}.{field}: {value!r} is not a {kind} id or code") from None
    return out


# --------------------------------------------------------------------------------------------------- stock stage

class _Consistency:
    """The consistency counters over the series that are not a designed example, plus the eval-day shortage."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.series = self.series_days = self.unknown_price = 0
        self.sums = Counter()
        self.by_category = defaultdict(Counter)
        self.short_days = 0
        self.short_units = 0.0
        self.eval_series_days = 0
        self.eval_topup_units = 0.0
        self.lots0 = Counter()

    def add(self, st: stock.SeriesStock, category: str) -> None:
        self.series += 1
        c = st.counters
        self.sums.update({k: c[k] for k in stock.COUNTERS})
        delivered = c["topup_units"] + c["regular_units"]
        self.by_category[category].update({"series": 1, "delivered_units": delivered,
                                           "wasted_units": c["wasted_units"]})
        for h in st.history:
            if h["day"] >= 0:
                self.eval_series_days += 1
                if h["topup"] > 0:
                    self.short_days += 1
                    self.short_units += max(0.0, h["units"] - h["start_stock"])
        self.eval_topup_units += sum(r["qty"] for r in st.eval_topups)
        for lot in st.lots_day0:
            self.lots0["rows"] += 1
            self.lots0["short"] += lot["physical"] < lot["qty"] - EPS
            self.lots0["phantom"] += lot["physical"] <= EPS < lot["qty"]

    def report(self) -> dict:
        s = self.sums
        delivered = s["topup_units"] + s["regular_units"]
        return {
            "sold_equals_public_series": self.series,
            "topup_share": _share(s["topup_units"], delivered),
            "topup_units": _round(s["topup_units"]),
            "regular_units": _round(s["regular_units"]),
            "eval_topups": {"units": _round(self.eval_topup_units),
                            "delivered_to_worlds": self.cfg["stock"]["topup_on_eval_days"]},
            "wasted_units": _round(s["wasted_units"]),
            "waste_by_category": {
                code: {"series": c["series"], "delivered_units": _round(c["delivered_units"]),
                       "wasted_units": _round(c["wasted_units"]),
                       "waste_share": _share(c["wasted_units"], c["delivered_units"])}
                for code, c in sorted(self.by_category.items())},
            "sim_stockout_days": s["sim_stockout_days"],
            "public_stockout_days": s["public_stockout_days"],
            "agree_stockout_days": s["agree_stockout_days"],
            "agree_share_of_public_stockout_days": _share(s["agree_stockout_days"], s["public_stockout_days"]),
            "agree_share_of_sim_stockout_days": _share(s["agree_stockout_days"], s["sim_stockout_days"]),
            "error_lots": s["error_lots"],
            "lots0": {"rows": self.lots0["rows"], "physical_short": self.lots0["short"],
                      "physical_zero": self.lots0["phantom"]},
            "eval_days_short_without_topup": {"series_days": self.short_days, "of_series_days": self.eval_series_days,
                                              "share": _share(self.short_days, self.eval_series_days),
                                              "units_short": _round(self.short_units), "note": SHORT_NOTE},
        }


def _check_sold(st: stock.SeriesStock, store: str, product: str, designed: bool) -> None:
    """D-16 on every simulated day (a designed series: on every day before day 0)."""
    for h in st.history:
        if designed and h["day"] >= 0:
            continue
        if abs(h["sold"] - h["units"]) > EPS or h["lost"] > EPS:
            raise PublicBuildError(f"store {store} product {product} day {h['day']}: simulated sold {h['sold']} is "
                                   f"not the public units {h['units']}; public sales are never changed (D-16)")


def stock_stage(source, cfg: dict, out_dir: pathlib.Path | str, reports_dir: pathlib.Path | str | None = None) -> dict:
    """Write the seven stock-stage parts under `<out_dir>/parts/` and `public-consistency.json` under `reports_dir`
    (default `<out_dir>/reports`) from `source`, an iterable of ((city, store, product, cats), [day rows]) in (store,
    product) code order (`data.public.series_iter`). Returns the report. See the module doc."""
    start = time.perf_counter()
    out_dir = pathlib.Path(out_dir)
    reports_dir = out_dir / "reports" if reports_dir is None else pathlib.Path(reports_dir)
    factor, level = cfg["units"]["factor"], cfg["categories"]["level"]
    designed = designed_example(cfg)
    found_designed = []
    acc = _Consistency(cfg)
    series = series_days = unknown = weather_conflicts = 0
    products: dict[str, tuple] = {}
    product_conflicts = set()

    with _parts(out_dir / "parts", STOCK_PARTS) as w:
        weather_store, weather, conflicted = None, {}, set()

        def flush_weather():
            for day in sorted(weather):
                w["weather.csv.gz"].write((public_config.code(cfg, "store", weather_store), day, *weather[day]))

        for (city, store, product, cats), rows in source:
            if store != weather_store:
                if weather_store is not None:
                    flush_weather()
                    weather_conflicts += len(conflicted)
                weather_store, weather, conflicted = store, {}, set()
            s_code, p_code = public_config.code(cfg, "store", store), public_config.code(cfg, "product", product)
            prow = {"product": product, **dict(zip(public.CATEGORY_LEVELS, cats))}
            is_designed = designed is not None and (store, product) == (designed["store"], designed["product"])
            days = [r["day"] for r in rows]
            units = [_round(r["sale_amount"] * factor) for r in rows]
            try:
                st = stock.simulate(s_code, p_code, days, units, [r["stockout"] for r in rows],
                                    public_config.stock_params(cfg, prow), designed_physical_zero=is_designed)
            except (RuntimeError, ValueError) as e:
                raise PublicBuildError(f"store {s_code} product {p_code}: {e}") from e
            _check_sold(st, s_code, p_code, is_designed)
            series += 1
            series_days += len(rows)
            for r, u in zip(rows, units):
                w["sales.csv.gz"].write((s_code, p_code, r["day"], u, r["sale_amount"], r["oos_hours"], r["stockout"],
                                         r["discount"], r["unknown_price"], r["holiday"], r["activity"]))
                unknown += r["unknown_price"]
                values = tuple(r[k] for k in WEATHER)
                if r["day"] not in weather:
                    weather[r["day"]] = values
                elif weather[r["day"]] != values:
                    conflicted.add(r["day"])
                if r["day"] >= 0:
                    w["truth_demand.csv.gz"].write((s_code, p_code, r["day"], u))
            tag, origin = (DESIGNED, DESIGNED) if is_designed else (None, None)
            for lot in st.lots_day0:
                w["lots0.csv.gz"].write((lot["lot_id"], lot["branch"], lot["sku"], lot["qty"], lot["expiry_day"],
                                         lot["received_seq"], lot["markdown_pct"], lot["expiry_source"],
                                         lot["last_verified_day"], tag, origin or lot["origin"]))
                w["truth_physical.csv.gz"].write((lot["lot_id"], lot["physical"]))
            for row in st.inbound:
                w["inbound0.csv.gz"].write((row["lot_id"], row["branch"], row["sku"], row["qty"], row["arrival_day"],
                                            row["arrival_min"], row["expiry_day"], row["received_seq"],
                                            row["expiry_source"], row["last_verified_day"], origin or row["origin"]))
                w["truth_physical.csv.gz"].write((row["lot_id"], row["physical"]))
            if product not in products:
                products[product] = cats
            elif products[product] != cats:
                product_conflicts.add(product)
            if is_designed:
                found_designed.append([s_code, p_code])
            else:
                category = public_config.code(cfg, "category", prow[level])
                acc.add(st, category)
        if weather_store is not None:
            flush_weather()
            weather_conflicts += len(conflicted)
        if designed is not None and not found_designed:
            raise PublicBuildError(f"designed.verify_example: store {designed['store']} product "
                                   f"{designed['product']} is not a series of the data")
        for product in sorted(products, key=int):
            prow = {"product": product, **dict(zip(public.CATEGORY_LEVELS, products[product]))}
            row = public_config.category_row(cfg, prow)
            w["products.csv.gz"].write((
                public_config.code(cfg, "product", product), *products[product],
                public_config.code(cfg, "category", prow[level]), row["zone"], row["price"], row["disposal_cost"],
                row["units_per_tote"], row["kg_per_unit"], row["shelf_life_days"],
                [list(step) for step in public_config.schedule_for(cfg, prow)], None))
        part_rows = {name: p.rows for name, p in w.items()}

    cal = cfg["calendar"]
    report = {
        "note": CONSISTENCY_NOTE,
        "dataset": cfg["dataset"]["name"],
        "days": {"first": cal["first_day"], "last": cal["last_day"]},
        "units": {"factor": factor, "rule": cfg["units"]["rule"]},
        "series": series,
        "series_days": series_days,
        "sold_equals_public": True,  # the stage stops naming the series otherwise
        "designed_series": len(found_designed),
        "designed": ({"series": found_designed, "label": designed["label"], "rule": designed["rule"]}
                     if designed is not None else None),
        **acc.report(),
        "unknown_price_rows": unknown,
        "weather_conflicts": weather_conflicts,
        "weather_rule": WEATHER_RULE,
        "product_category_conflicts": len(product_conflicts),
        "product_category_rule": CATEGORY_CONFLICT_RULE,
        "parts": part_rows,
        "peak_rss_mb": peak_rss_mb(),
        "seconds": round(time.perf_counter() - start, 1),
    }
    report = json.loads(config.canonical(report))
    write_report(reports_dir / "public-consistency.json", report)
    return report


# --------------------------------------------------------------------------------------------------- network stage

def stores_by_city(raw_dir, cfg: dict, csv_path=None) -> dict[str, set]:
    """{city id: {store ids}} of the checked dataset files (`raw_dir`) or a checked CSV in their schema (`csv_path`),
    through the same checks as every other reader (sha256 and size first, then the row and date checks)."""
    if csv_path is not None:
        public.verify_known(csv_path, cfg)
        raw = public.read_csv(csv_path, cfg["dataset"]["columns"])
    else:
        raw = public.read_files(raw_dir, cfg)
    out = defaultdict(set)
    for row in public.day_rows(raw, cfg):
        out[row["city"]].add(row["store"])
    return dict(out)


def _horizon_end(cfg: dict) -> int:
    """The last day a lot of the demo can still be on hand: last_demo_day + the longest category shelf life."""
    cats = cfg["categories"]
    rows = [cats["default"], *({**cats["default"], **o} for o in cats["overrides"].values())]
    return cfg["calendar"]["last_demo_day"] + max(r["shelf_life_days"] for r in rows)


def network_stage(stores: dict, cfg: dict, out_dir: pathlib.Path | str,
                  reports_dir: pathlib.Path | str | None = None) -> dict:
    """Write stores.csv.gz (store, city, geo_known), runs.csv.gz (data.runs.run_rows) and capacity.csv.gz (every store,
    zone and day 0..horizon_end, unmapped stores included) under `<out_dir>/parts/`, and `public-network.json` under
    `reports_dir` (default `<out_dir>/reports`), from {city id: store ids} (D-29..D-31, D-34). Returns the report.

    Stage 1's lot-independent part only: same city, both stores mapped, within the service limit, k nearest
    (data.runs). Cost never filters a destination. A store missing from the mapping gets no run and keeps its
    capacity; it is counted and listed."""
    start = time.perf_counter()
    out_dir = pathlib.Path(out_dir)
    reports_dir = out_dir / "reports" if reports_dir is None else pathlib.Path(reports_dir)
    city_of = {}
    for city in sorted(stores, key=int):
        for store in sorted(stores[city], key=int):
            if store in city_of:
                raise PublicBuildError(f"store {store} is listed in city {city_of[store]} and city {city}; a store "
                                       "belongs to one city")
            city_of[store] = city
    params = public_config.network_params(cfg)
    raw_params = dataclasses.replace(
        params, unmapped=frozenset(public_config.raw_id(cfg, "store", s) for s in params.unmapped))
    pos = runs.positions(stores, raw_params)
    pairs, unmapped = runs.candidate_pairs(stores, pos, raw_params)
    run_rows = runs.run_rows(pairs, raw_params)
    S = lambda raw: public_config.code(cfg, "store", raw)  # noqa: E731
    horizon_end = _horizon_end(cfg)
    zones = sorted({cfg["categories"]["default"]["zone"]} | {
        o["zone"] for o in cfg["categories"]["overrides"].values() if "zone" in o})
    unmapped_set = set(unmapped)
    with _parts(out_dir / "parts", NETWORK_PARTS) as w:
        for store in sorted(city_of, key=int):
            w["stores.csv.gz"].write((S(store), public_config.code(cfg, "city", city_of[store]),
                                      store not in unmapped_set))
        for r in run_rows:
            origin, dest = S(r["origin"]), S(r["dest"])
            # the run code format of data.runs.run_rows (R-origin-dest), on the display codes
            row = {**r, "run_code": f"R-{origin}-{dest}", "origin": origin, "dest": dest}
            w["runs.csv.gz"].write(tuple(row[c] for c in PARTS["runs.csv.gz"]))
        for r in runs.capacity_rows([S(s) for s in city_of], zones, range(0, horizon_end + 1), params):
            w["capacity.csv.gz"].write((r["branch"], r["zone"], r["day"], r["free_totes"]))
        part_rows = {name: p.rows for name, p in w.items()}
    per_source = Counter({s: 0 for s in city_of if s not in unmapped_set})
    per_source.update(p["origin"] for p in pairs)
    per_day = Counter((r["origin"], r["day"]) for r in run_rows)
    cold = {(r["origin"], r["dest"]): r["cold_chain"] for r in run_rows}
    spread = public._spread(per_source.values())
    report = {
        "note": NETWORK_NOTE,
        "runs_label": RUNS_LABEL,
        "draw_keys": DRAW_KEYS,
        "stores": len(city_of),
        "cities": len(stores),
        "k": params.k,
        "max_travel_min": params.max_travel_min,
        "run_days": list(params.run_days),
        "pairs": len(pairs),
        "pairs_per_source": spread,
        "sources_without_destination": sum(1 for n in per_source.values() if n == 0),
        "max_runs_per_store_per_day": max(per_day.values(), default=0),
        "cold_chain_share": _share(sum(cold.values()), len(cold)) if cold else None,
        "travel_min": public._spread(p["travel_min"] for p in pairs),
        "unmapped_stores": len(unmapped),
        "unmapped": [S(s) for s in unmapped],
        "horizon_end": horizon_end,
        "zones": zones,
        "parts": part_rows,
        "peak_rss_mb": peak_rss_mb(),
        "seconds": round(time.perf_counter() - start, 1),
    }
    report = json.loads(config.canonical(report))
    write_report(reports_dir / "public-network.json", report)
    return report


# --------------------------------------------------------------------------------------------------- command line

def _summary(command: str, report: dict) -> str:
    if command == "stock":
        return (f"stock: {report['series']} series, sold_equals_public {report['sold_equals_public']}, top-up share "
                f"{report['topup_share']}, {report['seconds']} s, peak {report['peak_rss_mb']} MB")
    return (f"network: {report['stores']} stores, {report['pairs']} pairs, unmapped {report['unmapped_stores']}, max "
            f"runs per store per day {report['max_runs_per_store_per_day']} (k {report['k']}), {report['seconds']} s")


BUILD_FORECASTS = ("local", "keep")  # local: the stand-in; keep: the finished forecast parts (the BigQuery run)


def kept_forecast(cfg: dict, out_dir: pathlib.Path | str) -> dict:
    """`--forecast keep`: the forecast parts already in `<out>/parts`, reused with no provider call. Refuses a missing
    part, an input part whose sha256 is not its meta's, and an input or a run whose reference_history_id is not the
    one `cfg` gives. Returns the summary forecast_stage returns (runs, points, fallbacks, clamped, provider_summary),
    recomputed from the parts."""
    from . import forecast as F
    from . import public_forecast as PF  # imported here: it imports this module

    parts = pathlib.Path(out_dir) / PF.PARTS
    for name in (PF.INPUT_NAME, PF.INPUT_META, PF.RUNS_NAME, PF.POINTS_NAME):
        if not (parts / name).exists():
            raise PublicBuildError(f"--forecast keep: no {name} in {parts}; run tools/frn.py forecast first")
    try:
        meta = PF.read_input_meta(parts)
    except ValueError as e:
        raise PublicBuildError(f"--forecast keep: {e}") from e
    history = PF.reference_history_id(cfg)
    if meta["reference_history_id"] != history:
        raise PublicBuildError(f"--forecast keep: {PF.INPUT_NAME} was built on other inputs "
                               f"({meta['reference_history_id']}, the reference gives {history}); rerun the forecast")
    runs = []
    for row in PF.read_csv_gz(parts / PF.RUNS_NAME):
        run = dict(zip(PF.RUN_COLUMNS, row))
        run_meta = json.loads(run["meta"])
        if run_meta.get("reference_history_id") != history:
            raise PublicBuildError(f"--forecast keep: run {run['forecast_run_id']} ({run['job']}) was built on other "
                                   f"inputs ({run_meta.get('reference_history_id')}, the reference gives {history}); "
                                   "rerun the forecast")
        runs.append({"forecast_run_id": run["forecast_run_id"], "job": run["job"],
                     "fallbacks": run_meta.get("fallbacks", []), "clamped_points": run_meta.get("clamped_points", 0)})
    i_run, i_store, i_sku, i_prov = (PF.POINT_COLUMNS.index(c) for c in ("forecast_run_id", "store", "sku",
                                                                           "provider"))
    series, n = set(), 0
    for row in PF.read_csv_gz(parts / PF.POINTS_NAME):
        series.add((row[i_run], row[i_store], row[i_sku], row[i_prov]))
        n += 1
    headline = F.provider_summary(runs, ({"forecast_run_id": r, "branch": s, "sku": k, "provider": p}
                                         for r, s, k, p in series))
    return {"runs": len(runs), "points": n, "series_runs": meta["series_runs"],
            "fallbacks": sum(len(r["fallbacks"]) for r in runs), "clamped": sum(r["clamped_points"] for r in runs),
            "provider_summary": headline, "reference_history_id": history, "kept": True}


def build(raw, csv_path, cfg: dict, out_dir: pathlib.Path | str, forecast: str = "local",
          reports_dir: pathlib.Path | str | None = None, fixture: pathlib.Path | str | None = None) -> dict:
    """The whole set: stock, network and forecast stages, then the manifest, then the check (data.manifest). Returns
    {dataset_version, stock, network, forecast, seconds}; raises when a stage or the check refuses."""
    from . import manifest, public_forecast  # imported here: both import this module

    if forecast not in BUILD_FORECASTS:
        raise PublicBuildError(f"--forecast {forecast!r}: this build knows {', '.join(BUILD_FORECASTS)}")
    start = time.perf_counter()
    out_dir = pathlib.Path(out_dir)
    if forecast == "keep":
        kept_forecast(cfg, out_dir)  # refused before the stages run, so a bad forecast costs no stock stage
    stock_report = stock_stage(public.series_iter(raw, cfg, csv_path=csv_path), cfg, out_dir, reports_dir)
    network_report = network_stage(stores_by_city(raw, cfg, csv_path=csv_path), cfg, out_dir, reports_dir)
    if forecast == "keep":
        summary = kept_forecast(cfg, out_dir)
    else:
        summary = public_forecast.forecast_stage(public_forecast.series_source(cfg, raw, csv_path), cfg, out_dir,
                                                 forecast)
    manifest.write_manifest(out_dir, cfg, fixture=fixture)
    version = manifest.check(out_dir)
    return {"dataset_version": version, "stock": stock_report, "network": network_report, "forecast": summary,
            "seconds": round(time.perf_counter() - start, 1), "peak_rss_mb": peak_rss_mb()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frn.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("stock", "public rows, simulated lots and deliveries, held-out demand, consistency report"),
                       ("network", "stores, runs within a city and the service limit, capacity, network report"),
                       ("build", "every stage, then the manifest and its check: one versioned artifact set")):
        each = sub.add_parser(name, help=text)
        source = each.add_mutually_exclusive_group(required=True)
        source.add_argument("--raw", type=pathlib.Path, help="the folder holding the dataset files")
        source.add_argument("--csv", type=pathlib.Path, help="a checked CSV in the dataset's schema (tests, fixture)")
        each.add_argument("--fixture", type=pathlib.Path, help="a fixture's override JSON (public_config.load_for)")
        each.add_argument("--out", required=True, type=pathlib.Path, help="parts go to <out>/parts, reports to "
                                                                          "<out>/reports")
        if name == "build":
            each.add_argument("--forecast", required=True, choices=BUILD_FORECASTS,
                              help="local: the stand-in forecast (development and tests; refused for a release); "
                                   "keep: reuse the finished forecast parts in <out>/parts (the BigQuery run) with "
                                   "no provider call, then rewrite the manifest")
            each.add_argument("--reports", type=pathlib.Path,
                              help="where the stage reports go (default <out>/reports); they hold times and memory")
        else:
            each.add_argument("--report", type=pathlib.Path, help="also write the report here (the committed copy)")
    args = parser.parse_args(argv)
    try:
        cfg = public_config.load_for(args.fixture) if args.fixture else public_config.load()
        if args.command == "build":
            from .manifest import ManifestError
            try:
                info = build(args.raw, args.csv, cfg, args.out, args.forecast, args.reports, args.fixture)
            except ManifestError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 1
            kept = "kept, " if info["forecast"].get("kept") else ""
            print(f"build: ok {info['dataset_version']}; {info['stock']['series']} series; forecast {kept}"
                  f"{info['forecast']['provider_summary']['headline']}; {info['seconds']} s, peak "
                  f"{info['peak_rss_mb']} MB")
            return 0
        if args.command == "stock":
            report = stock_stage(public.series_iter(args.raw, cfg, csv_path=args.csv), cfg, args.out)
        else:
            report = network_stage(stores_by_city(args.raw, cfg, csv_path=args.csv), cfg, args.out)
    except (PublicDataError, PublicBuildError, config.ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    if args.report is not None:
        write_report(args.report, report)
    print(_summary(args.command, report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
