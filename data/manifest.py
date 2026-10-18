"""The public artifact set: one directory of gz CSV parts plus manifest.json, versioned and checked (spec §4 Data flow 1,
§5 Public dataset and Horizon coverage; DATA-05 kept, DATA-08, DATA-13; 05.2-CONTEXT D-08, D-15).

    .venv/bin/python tools/frn.py check DIR              # prints "ok <dataset_version>" or the refusal (exit 1)
    .venv/bin/python tools/frn.py check DIR --release    # the deploy gate: the local stand-in is refused

The manifest is the contract between build/web's pipeline and build/api's loader (05.2-12): the loader trusts what the
manifest lists and checks, and never imports this module. A set is:

- `parts/<part>.csv.gz`, one per entry of TABLES (part -> the one shared table it loads into), each with a header
  row; `files.<part>` records its path, table, columns, row count, sha256 and whether it is hidden truth (TRUTH);
- `parts/forecast_input.csv.gz` and `parts/forecast_input.json`, the forecast's input (the file BigQuery loads; no
  table), recorded with their sha256 under `forecast.input`;
- `manifest.json`: kind "public", the inputs (the dataset files' checksums, and the fixture override file when the set
  was built from a test fixture), the reference fingerprint (`public_config.fingerprint`), the set's settings
  (calendar, mark, currency, Policy, markdown response, case opening, evaluation, board, catalogue, series window, the
  truth price response, categories, units and the input rules, horizon_end, zones, the network summary, the
  attribution block, the designed example), the forecast block (providers by points, runs, fallbacks, clamped points,
  whether it is the local stand-in) and the two version ids.

Versions (data.versions.digest; no clock, git or environment value enters a hash):
- `input_version` = "in-" + 12 hex over the inputs, the reference fingerprint, the settings and the sha256 of every
  non-forecast part: what the set was built from and everything it became before any forecast;
- `dataset_version` = "ds-" + 12 hex over the manifest without that field, so it changes when any part, setting or
  version changes, and never when only layout or key order does.

`check(dir)` refuses (ManifestError, naming the part, the series and the day; nothing is clamped or skipped): a
manifest whose dataset_version does not recompute; a part whose bytes (sha256), header or row count differ from the
manifest; a forecast that misses a required (job, series, day) through horizon_end (or has one twice, or one no job
needs); hidden demand that misses a series on any day 0..last_demo_day (D-15: the public eval week is the held-out
demand, so truth stops at last_demo_day while forecasts still reach horizon_end); a store without capacity for a zone
on a day 0..horizon_end; a lot or delivery expiring after horizon_end; a lot or delivery without its physical truth.

`release_check(dir)` runs check() and then what only a release needs (DATA-05 kept): every point's provider is
ai_forecast or arima_plus (the local stand-in and any other name are refused with the count of points); the forecast was
built on this set's inputs (every run's reference_history_id and the input part's equal the one recomputed from the
manifest's input checksums, units rule, calendar, input rules, markdown base and forecast config, and, for a set built
from the public dataset, the one reference/ gives now); input_version recomputes; and the reference fingerprint is the
reference/ being shipped. A set built from a test fixture passes with a note saying so, never silently.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import gzip
import json
import pathlib
import sys
from collections import Counter

from . import config
from . import forecast as F
from . import public, public_build, public_config, public_forecast, versions
from .config import ConfigError

KIND = "public"
FORMAT = 1
NAME = "manifest.json"
TABLES = {
    "sales": "ref_sales_day",
    "weather": "ref_weather_day",
    "products": "ref_product",
    "stores": "ref_store",
    "lots0": "ref_lot0",
    "inbound0": "ref_inbound0",
    "runs": "ref_run",
    "capacity": "ref_capacity",
    "forecast_runs": "ref_forecast_run",
    "forecast_points": "ref_forecast_point",
    "truth_physical": "ref_truth_physical",
    "truth_demand": "ref_truth_demand",
}
TRUTH = ("truth_physical", "truth_demand")  # hidden truth: loaded for the worlds, never shown as a record
FORECAST_PARTS = ("forecast_runs", "forecast_points")  # outside input_version: the forecast is built on the inputs
PATHS = {part: f"{public_forecast.PARTS}/{part}.csv.gz" for part in TABLES}
COLUMNS = {**{part: public_build.PARTS[f"{part}.csv.gz"] for part in TABLES if part not in FORECAST_PARTS},
           "forecast_runs": public_forecast.RUN_COLUMNS, "forecast_points": public_forecast.POINT_COLUMNS}
INPUT_PATH = f"{public_forecast.PARTS}/{public_forecast.INPUT_NAME}"
INPUT_META_PATH = f"{public_forecast.PARTS}/{public_forecast.INPUT_META}"
RELEASE_PROVIDERS = (F.AI_FORECAST, F.ARIMA_PLUS)
REQUIRED = ("kind", "format", "dataset_version", "input_version", "reference_fingerprint", "inputs", "settings",
            "files", "forecast")
ATTRIBUTION = ("name", "creator", "license", "license_url", "source_url", "citation")
INPUT_FILE_FIELDS = ("name", "sha256", "bytes", "rows")
STAND_IN_NOTE = ("the local stand-in forecast (development and tests only); the release check refuses it; the BigQuery "
                 "run replaces it (05.2-11)")
FIXTURE_NOTE = "built from a test fixture ({name}), not the public dataset"
RERUN = "run tools/frn.py forecast --provider ai_forecast on this set (05.2-11), then rewrite the manifest"


class ManifestError(ValueError):
    """The set may not be loaded (check) or released (release_check): the message names the part, series and day."""


# --------------------------------------------------------------------------------------------------- versions

def dataset_version(manifest: dict) -> str:
    """'ds-' + 12 hex over the manifest without its dataset_version field."""
    return versions.digest("ds-", {k: v for k, v in manifest.items() if k != "dataset_version"})


def input_version(manifest: dict) -> str:
    """'in-' + 12 hex over the inputs, the reference fingerprint, the settings and every non-forecast part's sha256."""
    parts = {part: entry["sha256"] for part, entry in manifest["files"].items() if part not in FORECAST_PARTS}
    return versions.digest("in-", {"inputs": manifest["inputs"], "reference_fingerprint":
                                   manifest["reference_fingerprint"], "settings": manifest["settings"],
                                   "parts": parts})


def history_id(manifest: dict) -> str:
    """The forecast's reference_history_id recomputed from the manifest's own values: the input files' sha256, the
    units rule and factor, the calendar, the stockout, discount and unknown-price rules, the markdown base, the
    forecast config and horizon_end (the same basis as public_forecast.reference_history_id)."""
    s = manifest["settings"]
    md = s["markdown_response"]
    return versions.digest("fb-", {
        "files": {split: entry["sha256"] for split, entry in sorted(manifest["inputs"]["files"].items())},
        "units": {"rule": s["units"]["rule"], "factor": s["units"]["factor"]},
        "calendar": s["calendar"], "stockout": s["stockout"], "discount": s["discount"],
        "unknown_price": s["unknown_price"], "markdown_base": {"m20": md["m20"][1], "m30": md["m30"][1]},
        "forecast": s["forecast"], "horizon_end": s["horizon_end"]})


# --------------------------------------------------------------------------------------------------- reading parts

def _rows(path: pathlib.Path):
    """(header, iterator over the rows after it) of a gz CSV part."""
    fh = gzip.open(path, "rt", encoding="utf-8", newline="")
    reader = csv.reader(fh)
    header = next(reader, None)

    def rows():
        try:
            yield from reader
        finally:
            fh.close()
    return header, rows()


def _count(path: pathlib.Path) -> tuple[list[str] | None, int]:
    header, rows = _rows(path)
    return header, sum(1 for _ in rows)


def _sha(path: pathlib.Path) -> str:
    return public.sha256_of(path)


def _job_order(job: str) -> tuple:
    return (1, 0) if job == F.BACKTEST else (0, int(job[1:]) if job[1:].isdigit() else 0)


def _mini_cfg(settings: dict) -> dict:
    """What public_forecast.jobs and horizon_end read, from the manifest's settings."""
    return {"forecast": settings["forecast"], "calendar": settings["calendar"], "categories": settings["categories"]}


def _points(n: int) -> str:
    return f"{n} point" if n == 1 else f"{n} points"


# --------------------------------------------------------------------------------------------------- writing

def _zones(cfg: dict) -> list[str]:
    cats = cfg["categories"]
    return sorted({cats["default"]["zone"]} | {o["zone"] for o in cats["overrides"].values() if "zone" in o})


def _designed(cfg: dict) -> dict | None:
    ex = public_build.designed_example(cfg)
    if ex is None:
        return None
    return {"store": public_config.code(cfg, "store", ex["store"]),
            "product": public_config.code(cfg, "product", ex["product"]), "rule": ex["rule"], "label": ex["label"]}


def _network(cfg: dict, root: pathlib.Path) -> dict:
    """The network summary, read from the stores and runs parts (D-29..D-31, D-34): stores, cities, the stores missing
    from the mapping (excluded from transfers), runs and pairs, the most runs one store sends on one day."""
    header, rows = _rows(root / PATHS["stores"])
    i_store, i_city, i_geo = (header.index(c) for c in ("store", "city", "geo_known"))
    stores, cities, unmapped = 0, set(), []
    for r in rows:
        stores += 1
        cities.add(r[i_city])
        if r[i_geo] != "true":
            unmapped.append(r[i_store])
    header, rows = _rows(root / PATHS["runs"])
    i_origin, i_dest, i_day = (header.index(c) for c in ("origin", "dest", "day"))
    n_runs, pairs, per_day = 0, set(), Counter()
    for r in rows:
        n_runs += 1
        pairs.add((r[i_origin], r[i_dest]))
        per_day[(r[i_origin], r[i_day])] += 1
    params = public_config.network_params(cfg)
    return {"note": public_build.NETWORK_NOTE, "runs_label": public_build.RUNS_LABEL, "stores": stores,
            "cities": len(cities), "unmapped_stores": len(unmapped), "unmapped": unmapped, "runs": n_runs,
            "pairs": len(pairs), "max_runs_per_store_per_day": max(per_day.values(), default=0), "k": params.k,
            "max_travel_min": params.max_travel_min, "run_days": list(params.run_days)}


def _settings(cfg: dict, root: pathlib.Path) -> dict:
    md = public_config.markdown(cfg)
    s = public_config.settings(cfg)
    s.update({
        "policy": dataclasses.asdict(public_config.policy(cfg)),
        "markdown_response": {"m20": list(md.m20), "m30": list(md.m30)},
        "categories": cfg["categories"],
        "units": dict(cfg["units"]),
        "stockout": dict(cfg["stockout"]),
        "discount": dict(cfg["discount"]),
        "unknown_price": dict(cfg["unknown_price"]),
        "codes": dict(cfg["codes"]),
        "horizon_end": public_forecast.horizon_end(cfg),
        "zones": _zones(cfg),
        "network": _network(cfg, root),
        "attribution": {k: cfg["dataset"][k] for k in ATTRIBUTION},
        "designed": _designed(cfg),
    })
    return s


def _forecast(root: pathlib.Path) -> dict:
    """The forecast block, read from the forecast parts and the input meta: providers by points, the runs with their
    providers (by series), fallbacks, clamped points and reference_history_id, and the input part's checksums."""
    header, rows = _rows(root / PATHS["forecast_runs"])
    runs = []
    for r in rows:
        row = dict(zip(header, r))
        meta = json.loads(row["meta"])
        runs.append({"forecast_run_id": row["forecast_run_id"], "job": row["job"], "cutoff_day": int(row["cutoff_day"]),
                     "first_day": int(row["first_day"]), "last_day": int(row["last_day"]),
                     "providers": meta.get("providers", {}), "fallbacks": len(meta.get("fallbacks", [])),
                     "clamped_points": meta.get("clamped_points", 0),
                     "reference_history_id": meta.get("reference_history_id")})
    runs.sort(key=lambda r: _job_order(r["job"]))
    header, rows = _rows(root / PATHS["forecast_points"])
    i = header.index("provider")
    providers = Counter(r[i] for r in rows)
    meta = json.loads((root / INPUT_META_PATH).read_text(encoding="utf-8"))
    stand_in = F.LOCAL in providers
    return {"providers": dict(sorted(providers.items())), "points": sum(providers.values()), "stand_in": stand_in,
            "note": STAND_IN_NOTE if stand_in else "", "runs": runs,
            "fallbacks": sum(r["fallbacks"] for r in runs), "clamped": sum(r["clamped_points"] for r in runs),
            "input": {"path": INPUT_PATH, "sha256": _sha(root / INPUT_PATH), "rows": meta["rows"],
                      "meta_path": INPUT_META_PATH, "meta_sha256": _sha(root / INPUT_META_PATH),
                      "reference_history_id": meta["reference_history_id"]}}


def write_manifest(out_dir: pathlib.Path | str, cfg: dict, fixture: pathlib.Path | str | None = None) -> dict:
    """Write `<out_dir>/manifest.json` over the parts already in `<out_dir>/parts` (every part's sha256 and rows read
    from disk) and `cfg`'s settings; `fixture` is the override file the set was built with, if any (its name and
    sha256 are recorded). Returns the manifest. It writes what is there and refuses only a missing part or a header
    that is not the format's; whether the set is complete is check()'s job."""
    root = pathlib.Path(out_dir)
    files = {}
    for part, table in TABLES.items():
        path = root / PATHS[part]
        if not path.exists():
            raise ManifestError(f"{PATHS[part]}: missing; run the stage that writes it (tools/frn.py build)")
        header, n = _count(path)
        if header != list(COLUMNS[part]):
            raise ManifestError(f"{PATHS[part]}: header {header} is not the format's {list(COLUMNS[part])}")
        files[part] = {"path": PATHS[part], "table": table, "columns": header, "rows": n, "sha256": _sha(path),
                       "truth": part in TRUTH}
    for rel in (INPUT_PATH, INPUT_META_PATH):
        if not (root / rel).exists():
            raise ManifestError(f"{rel}: missing; run the forecast stage (tools/frn.py build)")
    fixture_entry = None
    if fixture is not None:
        fixture = pathlib.Path(fixture)
        fixture_entry = {"name": fixture.name, "sha256": _sha(fixture)}
    manifest = {
        "kind": KIND,
        "format": FORMAT,
        "reference_fingerprint": public_config.fingerprint(),
        "inputs": {"dataset": cfg["dataset"]["name"], "fixture": fixture_entry,
                   "files": {split: {k: entry[k] for k in INPUT_FILE_FIELDS}
                             for split, entry in sorted(cfg["dataset"]["files"].items())}},
        "settings": _settings(cfg, root),
        "files": files,
        "forecast": _forecast(root),
    }
    manifest = json.loads(config.canonical(manifest))
    expected = public_forecast.reference_history_id(cfg)
    if history_id(manifest) != expected:  # the two recipes must never drift apart
        raise ManifestError(f"history_id recomputes to {history_id(manifest)!r}, public_forecast gives {expected!r}")
    manifest["input_version"] = input_version(manifest)
    manifest["dataset_version"] = dataset_version(manifest)
    config.atomic_write(root / NAME, json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return manifest


# --------------------------------------------------------------------------------------------------- check

def read_manifest(set_dir: pathlib.Path | str) -> dict:
    """manifest.json of a set (a repeated key, NaN or Infinity refused), of kind public and this format."""
    path = pathlib.Path(set_dir) / NAME
    try:
        m = config.read(path)
    except (ConfigError, OSError) as e:
        raise ManifestError(f"{NAME}: {e}") from None
    if not isinstance(m, dict):
        raise ManifestError(f"{NAME}: not an object")
    missing = [k for k in REQUIRED if k not in m]
    if missing:
        raise ManifestError(f"{NAME}: missing {', '.join(missing)}")
    if m["kind"] != KIND or m["format"] != FORMAT:
        raise ManifestError(f"{NAME}: kind {m['kind']!r} format {m['format']!r}; this reader knows kind {KIND!r} "
                            f"format {FORMAT}")
    return m


def _check_files(root: pathlib.Path, m: dict) -> None:
    """Every part listed once with its path, table, truth flag and columns; its bytes equal the manifest's sha256."""
    if set(m["files"]) != set(TABLES):
        missing, extra = sorted(set(TABLES) - set(m["files"])), sorted(set(m["files"]) - set(TABLES))
        raise ManifestError(f"{NAME}: files must list exactly the parts {sorted(TABLES)} (missing {missing}, "
                            f"unknown {extra})")
    for part in TABLES:
        entry, rel = m["files"][part], PATHS[part]
        want = {"path": rel, "table": TABLES[part], "truth": part in TRUTH, "columns": list(COLUMNS[part])}
        for field, value in want.items():
            if entry.get(field) != value:
                raise ManifestError(f"{rel}: {field} {entry.get(field)!r} is not {value!r}")
        path = root / rel
        if not path.exists():
            raise ManifestError(f"{rel}: missing")
        actual = _sha(path)
        if actual != entry["sha256"]:
            raise ManifestError(f"{rel}: sha256 {actual} is not the manifest's {entry['sha256']}; the part changed "
                                "after the build")
    inp = m["forecast"].get("input", {})
    for rel, key in ((INPUT_PATH, "sha256"), (INPUT_META_PATH, "meta_sha256")):
        path = root / rel
        if not path.exists() or _sha(path) != inp.get(key):
            raise ManifestError(f"{rel}: missing or not the manifest's {key} {inp.get(key)!r}")


class _Part:
    """One part read once: its header checked, its rows counted against the manifest."""

    def __init__(self, root: pathlib.Path, m: dict, part: str):
        self.part, self.rel, self.expected = part, PATHS[part], m["files"][part]["rows"]
        self.header, self._rows = _rows(root / self.rel)
        if self.header != list(COLUMNS[part]):
            raise ManifestError(f"{self.rel}: header {self.header} is not {list(COLUMNS[part])}")
        self.n = 0

    def col(self, name: str) -> int:
        return self.header.index(name)

    def __iter__(self):
        for r in self._rows:
            self.n += 1
            yield r
        if self.n != self.expected:
            raise ManifestError(f"{self.rel}: {self.n} rows, the manifest says {self.expected} rows")


def _drain(part: _Part) -> None:
    for _ in part:
        pass


def _series(s: str, k: str) -> str:
    return f"{s}|{k}"


def _check_forecast(root: pathlib.Path, m: dict, series: set) -> None:
    """Every job of the calendar, with its cutoff and days, has exactly the required days of every series."""
    cfg = _mini_cfg(m["settings"])
    try:
        plan = public_forecast.jobs(cfg)
    except ConfigError as e:
        raise ManifestError(f"{NAME}: settings.forecast: {e}") from None
    days_of = {job: days for job, _, days in plan}
    runs_part = _Part(root, m, "forecast_runs")
    i_id, i_job, i_cut, i_first, i_last = (runs_part.col(c) for c in ("forecast_run_id", "job", "cutoff_day",
                                                                       "first_day", "last_day"))
    job_of, seen_jobs = {}, {}
    for r in runs_part:
        job_of[r[i_id]] = r[i_job]
        seen_jobs[r[i_job]] = (int(r[i_cut]), int(r[i_first]), int(r[i_last]))
    want_jobs = {job: (cutoff, days[0], days[-1]) for job, cutoff, days in plan}
    if seen_jobs != want_jobs or len(job_of) != len(want_jobs):
        raise ManifestError(f"{runs_part.rel}: runs {sorted(seen_jobs.items())} are not the calendar's "
                            f"{sorted(want_jobs.items())} (job, cutoff, first and last day)")
    points = _Part(root, m, "forecast_points")
    i_run, i_store, i_sku, i_day = (points.col(c) for c in ("forecast_run_id", "store", "sku", "day"))
    rel = points.rel

    def close(key, days):
        job, store, sku = key
        need = days_of[job]
        if days == need:
            return
        have = set(days)
        missing = [d for d in need if d not in have]
        if missing:
            raise ManifestError(f"{rel}: no point for job {job}, series {_series(store, sku)}, day {missing[0]} "
                                f"(every job covers its days through horizon_end)")
        extra = sorted({d for d in days if days.count(d) > 1 or d not in need})
        raise ManifestError(f"{rel}: job {job}, series {_series(store, sku)}, day {extra[0]}: a point twice or on a "
                            "day the job does not forecast")

    seen, key, days = set(), None, []
    for r in points:
        if r[i_run] not in job_of:
            raise ManifestError(f"{rel}: forecast_run_id {r[i_run]!r} is not a run of {runs_part.rel}")
        this = (job_of[r[i_run]], r[i_store], r[i_sku])
        if this != key:
            if key is not None:
                close(key, days)
            if this in seen:
                raise ManifestError(f"{rel}: the rows of job {this[0]}, series {_series(*this[1:])} are not together")
            if (this[1], this[2]) not in series:
                raise ManifestError(f"{rel}: job {this[0]}, series {_series(*this[1:])} is not a series of the set")
            seen.add(this)
            key, days = this, []
        days.append(int(r[i_day]))
    if key is not None:
        close(key, days)
    for job, _, need in sorted(plan, key=lambda p: _job_order(p[0])):
        for store, sku in sorted(series):
            if (job, store, sku) not in seen:
                raise ManifestError(f"{rel}: no point for job {job}, series {_series(store, sku)}, day {need[0]}")


def check(set_dir: pathlib.Path | str) -> str:
    """The load gate: returns the dataset_version, or raises ManifestError naming the first problem (see the module
    doc). Nothing is clamped, filled in or skipped."""
    root = pathlib.Path(set_dir)
    m = read_manifest(root)
    want = dataset_version(m)
    if m["dataset_version"] != want:
        raise ManifestError(f"{NAME}: dataset_version {m['dataset_version']!r} does not recompute ({want!r}); the "
                            "manifest was edited after it was written")
    _check_files(root, m)
    s = m["settings"]
    end, last_demo = s["horizon_end"], s["calendar"]["last_demo_day"]

    stores_part = _Part(root, m, "stores")
    i = stores_part.col("store")
    stores = [r[i] for r in stores_part]
    sales = _Part(root, m, "sales")
    i_store, i_sku = sales.col("store"), sales.col("sku")
    series = set()
    for r in sales:
        series.add((r[i_store], r[i_sku]))
    unknown = sorted({st for st, _ in series} - set(stores))
    if unknown:
        raise ManifestError(f"{PATHS['stores']}: store {unknown[0]} sells in {PATHS['sales']} but is not listed")
    for part in ("weather", "products", "runs"):
        _drain(_Part(root, m, part))

    demand = _Part(root, m, "truth_demand")
    i_store, i_sku, i_day = demand.col("store"), demand.col("sku"), demand.col("day")
    covered: dict[tuple, set] = {}
    for r in demand:
        key = (r[i_store], r[i_sku])
        if key not in series:
            raise ManifestError(f"{demand.rel}: series {_series(*key)} is not a series of the set")
        covered.setdefault(key, set()).add(int(r[i_day]))
    for key in sorted(series):
        have = covered.get(key, set())
        for d in range(0, last_demo + 1):
            if d not in have:
                raise ManifestError(f"{demand.rel}: no hidden demand for series {_series(*key)} on day {d} (D-15: "
                                    f"every series on days 0..last_demo_day {last_demo})")
    del covered

    cap = _Part(root, m, "capacity")
    i_store, i_zone, i_day = cap.col("store"), cap.col("zone"), cap.col("day")
    have = {(r[i_store], r[i_zone], int(r[i_day])) for r in cap}
    for store in stores:
        for zone in s["zones"]:
            for d in range(0, end + 1):
                if (store, zone, d) not in have:
                    raise ManifestError(f"{cap.rel}: no {zone} capacity for store {store} on day {d} (days "
                                        f"0..horizon_end {end})")
    del have

    ids = set()
    for part, id_col in (("lots0", "lot_id"), ("inbound0", "inbound_id")):
        p = _Part(root, m, part)
        i_id, i_store, i_sku, i_exp = p.col(id_col), p.col("store"), p.col("sku"), p.col("expiry_day")
        for r in p:
            if int(r[i_exp]) > end:
                raise ManifestError(f"{p.rel}: {r[i_id]} ({_series(r[i_store], r[i_sku])}) expires on day {r[i_exp]}, "
                                    f"after horizon_end {end} (spec §5 Horizon coverage)")
            ids.add(r[i_id])
    phys = _Part(root, m, "truth_physical")
    i_id = phys.col("lot_id")
    known = {r[i_id] for r in phys}
    lost = sorted(ids - known)
    if lost:
        raise ManifestError(f"{phys.rel}: no physical quantity for {lost[0]} ({len(lost)} lots and deliveries)")
    del ids, known

    _check_forecast(root, m, series)
    return m["dataset_version"]


# --------------------------------------------------------------------------------------------------- release check

def release_check(set_dir: pathlib.Path | str) -> dict:
    """The deploy gate: check(), then the providers, the forecast's inputs, input_version and the reference
    fingerprint (see the module doc). Returns {dataset_version, input_version, providers, note}; raises
    ManifestError with the first reason the set may not ship."""
    root = pathlib.Path(set_dir)
    version = check(root)
    m = read_manifest(root)

    header, rows = _rows(root / PATHS["forecast_points"])
    i_run, i_store, i_sku, i_day, i_prov = (header.index(c) for c in ("forecast_run_id", "store", "sku", "day",
                                                                       "provider"))
    providers, refused, first = Counter(), Counter(), None
    for r in rows:
        providers[r[i_prov]] += 1
        if r[i_prov] not in RELEASE_PROVIDERS:
            refused[r[i_prov]] += 1
            if first is None:
                first = f"{r[i_run]} {_series(r[i_store], r[i_sku])} day {r[i_day]}"
    if refused:
        names = ", ".join(repr(p) for p in sorted(refused))
        why = " the local stand-in is for development and tests only;" if F.LOCAL in refused else ""
        raise ManifestError(f"refusing provider {names}: {_points(sum(refused.values()))} (first {first});{why} a "
                            f"release forecast comes only from {' or '.join(RELEASE_PROVIDERS)}; {RERUN}")

    history = history_id(m)
    header, rows = _rows(root / PATHS["forecast_runs"])
    for r in rows:
        row = dict(zip(header, r))
        got = json.loads(row["meta"]).get("reference_history_id")
        if got != history:
            raise ManifestError(f"the forecast was built on other inputs: run {row['forecast_run_id']} "
                                f"({row['job']}) has reference_history_id {got!r}, this set's inputs give "
                                f"{history!r}; {RERUN}")
    got = json.loads((root / INPUT_META_PATH).read_text(encoding="utf-8")).get("reference_history_id")
    if got != history:
        raise ManifestError(f"{INPUT_META_PATH}: reference_history_id {got!r} is not this set's inputs' {history!r}")
    fixture = m["inputs"].get("fixture")
    if fixture is None:
        current = public_forecast.reference_history_id(public_config.load())
        if history != current:
            raise ManifestError(f"the set was built on other inputs than reference/ gives now: reference_history_id "
                                f"{history!r}, reference/ gives {current!r}; rebuild with tools/frn.py build")
    recomputed = input_version(m)
    if m["input_version"] != recomputed:
        raise ManifestError(f"{NAME}: input_version {m['input_version']!r} does not recompute ({recomputed!r}) from "
                            "the inputs, the reference fingerprint, the settings and the parts; rebuild the set")
    fp = public_config.fingerprint()
    if m["reference_fingerprint"] != fp:
        raise ManifestError(f"{NAME}: the set was built from reference {m['reference_fingerprint']!r}, reference/ is "
                            f"now {fp!r}; rebuild with tools/frn.py build")
    return {"dataset_version": version, "input_version": recomputed, "providers": dict(sorted(providers.items())),
            "note": FIXTURE_NOTE.format(name=fixture["name"]) if fixture else ""}


# --------------------------------------------------------------------------------------------------- command line

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frn.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    each = sub.add_parser("check", help="check an artifact set: bytes, rows, versions and coverage")
    each.add_argument("set_dir", type=pathlib.Path, help="the set's folder (manifest.json and parts/)")
    each.add_argument("--release", action="store_true",
                      help="the deploy gate: also refuse the local stand-in or any provider other than ai_forecast "
                           "and arima_plus, a forecast built on other inputs, an input_version that does not "
                           "recompute and a set built from another reference/")
    args = parser.parse_args(argv)
    try:
        if args.release:
            info = release_check(args.set_dir)
            print(f"ok {info['dataset_version']} (release; providers {info['providers']})")
            if info["note"]:
                print(f"note: {info['note']}")
        else:
            print(f"ok {check(args.set_dir)}")
    except (ManifestError, ConfigError, OSError, KeyError, ValueError, TypeError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
