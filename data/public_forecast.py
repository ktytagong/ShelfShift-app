"""The public forecast at scale: censored input, runs d0..d3 plus the backtest, per-series checks, sorted parts (spec §5
Forecast; DATA-12, DATA-13; 05.2-CONTEXT D-14, D-15, D-17, D-19, D-35).

    .venv-data/bin/python -I tools/frn.py forecast --provider local --raw public-data/raw --out public-data/out
    .venv/bin/python tools/frn.py forecast --provider local --csv FILE --fixture JSON --out DIR
    .venv-data/bin/python -I tools/frn.py forecast --provider ai_forecast --project P --preflight --out public-data/out
    .venv-data/bin/python -I tools/frn.py forecast --provider ai_forecast --project P --dry-run --out public-data/out
    .venv-data/bin/python -I tools/frn.py forecast --provider ai_forecast --project P --out public-data/out
    .venv-data/bin/python -I tools/frn.py backtest --raw public-data/raw --out public-data/out --report PATH

The BigQuery providers (data/bq.py, the file path) run only on the user's machine with the user's credentials (D-22):
the project comes from --project or GOOGLE_CLOUD_PROJECT, never from code. They read the input part already in
<out>/parts (written by the local run or the build), refuse one built on other inputs, and `--partition city` runs one
AI.FORECAST per city into the same table, only if the single query fails or times out.

The synthetic forecast path in data/forecast.py is untouched: this module reuses its per-row checks
(`series_problems`), its stand-in (`local_fetch`), its clamping rule and its `provider_summary`, and adds what 50,000
series need: one series at a time, parts on disk, nothing held whole in memory.

The input is estimated full-price-equivalent public units, never true demand. Per (store, product) series and per run,
up to the run's cutoff (D-05's tags, with the public rules):
- units = sale_amount x the units factor (an Assumption, D-33), rounded once to UNIT_PLACES;
- an in-stock day at full price is `recorded`; a discounted in-stock day is `converted`: units / m(depth), where m is
  the store's assumed response (reference/policy.json base values) interpolated linearly by discount depth between
  (0%, 1.0), (20%, m20) and (30%, m30), and capped at 30% (D-17); the depths are the engine's markdown steps;
- an in-stock day whose discount is unknown (D-35: at or below / above the reference bounds) is converted at the full
  base price, no response applied, tagged `unknown_price` and counted per run (reference `unknown_price.rule`);
- a stockout day (any out-of-stock hour 06-22, D-14) is `imputed`: the mean of the in-stock days on its weekday (at
  least SAME_WEEKDAY_MIN of them, else the last LAST_DAYS in-stock days), never below that day's own converted units
  (the documented floor, applied for the public set); with no in-stock day at all it keeps its own units, `unfilled`.
  A stockout day is filled, never converted (data.preprocess's order). The counts per tag are every run's
  input_counts.

Runs (D-19): `forecast.runs` reads "d0..d<N> plus the backtest". d<n> uses data up to day n-1 and forecasts days
n..horizon_end, where horizon_end = last_demo_day + the longest category shelf life (as data/public_build.py's capacity
days); the backtest uses data up to day -(backtest_days + 1) and forecasts -backtest_days..-1. Series ids are
"<job>|<store code>|<product code>".

The stage writes, under <out>/parts (git-ignored):
- forecast_input.csv.gz (series_id, ts, value, how), ts = midnight UTC of day0 + day: the file BigQuery loads, and the
  file the local stand-in reads, so every provider answers the same input; forecast_input.json beside it holds the
  run plan, the counts, the store-to-city map and the input's sha256 and reference_history_id;
- forecast_points.csv.gz (POINT_COLUMNS), sorted by job, store code and product code as text, then day as a number;
- forecast_runs.csv.gz (RUN_COLUMNS), one row per run, its metadata as canonical JSON in `meta`.

Every provider's rows are checked per series with data.forecast.series_problems (an empty status, low <= base <= high
on finite numbers, every required day once, confidence 0.8) before anything else. A series that fails, or that the
provider never returns, falls back alone to the fallback provider, checked the same way; if that fails too the run
raises data.forecast.ForecastFailed and the previous parts stay byte-identical. A negative bound is set to 0 only after
the checks, and the point keeps its raw triple and a clamped flag, counted per run (data.forecast._point's rule). The
parts are replaced only after the whole run succeeded (atomic rename). Each run also counts its all-zero series: a
product that never sold at a store is forecast and reported, never dropped.

reference_history_id = "fb-" + 12 hex over the input files' sha256, the units rule and factor, the calendar, the
stockout, discount and unknown-price rules, the markdown base and the forecast config, so a forecast is tied to what
it read and not to the stock stage's parts; data/manifest.py recomputes it from the same values.

`score_public` scores the backtest run (D-15): see its docstring.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import heapq
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, Iterator

from engine.adjust import MARKDOWN_PCTS

from . import config
from . import forecast as F
from . import public, public_config, versions
from .config import ConfigError
from .preprocess import LAST_DAYS, PLACES, SAME_WEEKDAY_MIN

UNIT_PLACES = 6  # units are rounded once after the factor, as data.measure and the stock stage do
PUBLIC_TAGS = ("recorded", "converted", "unknown_price", "imputed", "unfilled")
RUNS_RE = re.compile(r"d0\.\.d([0-9]+) plus the backtest")
UNKNOWN_RULE = "converted at the full base price"  # the D-35 rule this module implements
PARTS = "parts"
INPUT_NAME, INPUT_META = "forecast_input.csv.gz", "forecast_input.json"
POINTS_NAME, RUNS_NAME = "forecast_points.csv.gz", "forecast_runs.csv.gz"
INPUT_COLUMNS = ("series_id", "ts", "value", "how")  # data/bq.py SCHEMA's names, in its order
POINT_COLUMNS = ("forecast_run_id", "store", "sku", "day", "low", "base", "high", "provider", "clamped", "raw_low",
                 "raw_base", "raw_high")
RUN_COLUMNS = ("forecast_run_id", "job", "demo_day", "cutoff_day", "first_day", "last_day", "meta")
CHUNK_ROWS = 200_000  # points per sorted run of the external sort: memory, not a retailer value
GZ_LEVEL = 6
DIAGNOSTICS = "forecast-diagnostics.json"


# --------------------------------------------------------------------------------------------------- the input rule

@dataclass(frozen=True)
class InputRule:
    """What the input conversion reads from reference data: the units factor (D-33), the full-price discount value,
    and the base of the store's assumed response at 20% and 30% off."""
    factor: float
    full_price: float
    m20: float
    m30: float


def input_rule(cfg: dict) -> InputRule:
    """The InputRule of a public config. The unknown-price rule must be the one this module implements (D-35)."""
    rule = cfg["unknown_price"]["rule"]
    if not isinstance(rule, str) or UNKNOWN_RULE not in rule:
        raise ConfigError(f"unknown_price.rule: this pipeline implements only rows {UNKNOWN_RULE!r} (D-35); the "
                          f"reference says {rule!r}")
    md = public_config.markdown(cfg)
    return InputRule(factor=cfg["units"]["factor"], full_price=cfg["discount"]["full_price"], m20=md.m20[1],
                     m30=md.m30[1])


def depth_response(discount: float, rule: InputRule) -> float:
    """The assumed response at this discount: linear in depth between the engine's markdown steps (0% -> 1.0, 20% ->
    m20, 30% -> m30), capped at the deepest step (D-17)."""
    depth = round((rule.full_price - discount) / rule.full_price * 100, UNIT_PLACES)
    points = list(zip(MARKDOWN_PCTS, (1.0, rule.m20, rule.m30)))
    if depth <= points[0][0]:
        return points[0][1]
    for (d0, m0), (d1, m1) in zip(points, points[1:]):
        if depth <= d1:
            return m0 + (m1 - m0) * (depth - d0) / (d1 - d0)
    return points[-1][1]


def units_of(row: dict, rule: InputRule) -> float:
    return round(row["sale_amount"] * rule.factor, UNIT_PLACES)


def _own(row: dict, rule: InputRule) -> tuple[float, str]:
    """(the day's own converted units, its in-stock tag)."""
    units = units_of(row, rule)
    if row["unknown_price"]:
        return units, "unknown_price"
    if row["discount"] >= rule.full_price:
        return units, "recorded"
    return units / depth_response(row["discount"], rule), "converted"


def public_input(rows: list[dict], cutoff: int, rule: InputRule) -> tuple[list[tuple[int, float, str]], Counter]:
    """((day, value, tag) for every day row with day <= cutoff, in day order; the count per tag). The rows are read,
    never written; nothing after the cutoff is read."""
    known = sorted((r for r in rows if r["day"] <= cutoff), key=lambda r: r["day"])
    own = [_own(r, rule) for r in known]
    clean = [(r["day"], v) for r, (v, _) in zip(known, own) if not r["stockout"]]
    out, counts = [], Counter()
    for r, (value, tag) in zip(known, own):
        if r["stockout"]:
            same = [v for d, v in clean if d % 7 == r["day"] % 7]
            pool = same if len(same) >= SAME_WEEKDAY_MIN else [v for _, v in clean[-LAST_DAYS:]]
            if pool:
                value, tag = max(sum(pool) / len(pool), value), "imputed"
            else:
                tag = "unfilled"
        out.append((r["day"], round(value, PLACES), tag))
        counts[tag] += 1
    return out, counts


# --------------------------------------------------------------------------------------------------- runs (D-19)

def horizon_end(cfg: dict) -> int:
    """The last forecast day: last_demo_day + the longest shelf life of any category row."""
    cats = cfg["categories"]
    rows = [cats["default"]] + [{**cats["default"], **o} for o in cats["overrides"].values()]
    return cfg["calendar"]["last_demo_day"] + max(r["shelf_life_days"] for r in rows)


def jobs(cfg: dict) -> list[tuple[str, int, list[int]]]:
    """(job, cutoff, days): d0..d<N> from `forecast.runs` (cutoff n-1, days n..horizon_end), then the backtest."""
    text = cfg["forecast"]["runs"]
    found = RUNS_RE.fullmatch(text) if isinstance(text, str) else None
    if not found:
        raise ConfigError(f"forecast.runs: {text!r} is not of the form 'd0..d<N> plus the backtest' (D-19)")
    last, cal = int(found.group(1)), cfg["calendar"]
    if last > cal["last_demo_day"]:
        raise ConfigError(f"forecast.runs: d{last} is after calendar.last_demo_day {cal['last_demo_day']}")
    end = horizon_end(cfg)
    out = [(f"d{n}", n - 1, list(range(n, end + 1))) for n in range(last + 1)]
    bt = cal["backtest_days"]
    out.append((F.BACKTEST, -bt - 1, list(range(-bt, 0))))
    return out


def required_days(cfg: dict, series_keys) -> dict[str, list[int]]:
    """Per series id: the days its run must forecast. `series_keys` are (store code, product code) pairs."""
    plan = jobs(cfg)
    return {F.series_id(job, store, sku): list(days) for store, sku in series_keys for job, _, days in plan}


def reference_history_id(cfg: dict) -> str:
    """'fb-' + 12 hex over what the forecast input is made from (see the module docstring)."""
    md = public_config.markdown(cfg)
    return versions.digest("fb-", {
        "files": {split: entry["sha256"] for split, entry in sorted(cfg["dataset"]["files"].items())},
        "units": {"rule": cfg["units"]["rule"], "factor": cfg["units"]["factor"]},
        "calendar": cfg["calendar"], "stockout": cfg["stockout"], "discount": cfg["discount"],
        "unknown_price": cfg["unknown_price"], "markdown_base": {"m20": md.m20[1], "m30": md.m30[1]},
        "forecast": cfg["forecast"], "horizon_end": horizon_end(cfg)})


# --------------------------------------------------------------------------------------------------- files

def _gz_writer(path: pathlib.Path):
    """A text writer of a gzip file with a fixed header (no name, mtime 0), so the bytes repeat run to run."""
    raw = open(path, "wb")
    gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=GZ_LEVEL)
    text = io.TextIOWrapper(gz, encoding="utf-8", newline="")

    class _Closing:
        def __enter__(self):
            return text

        def __exit__(self, *exc):
            text.close()
            raw.close()
    return _Closing()


def read_csv_gz(path: pathlib.Path) -> Iterator[list[str]]:
    """Every row of a gz CSV part after its header."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        yield from reader


def _sha256(path: pathlib.Path) -> str:
    return public.sha256_of(path)


def _ts_of(day0: date):
    cache: dict[int, str] = {}

    def ts(day: int) -> str:
        if day not in cache:
            cache[day] = f"{(day0 + timedelta(days=day)).isoformat()}T00:00:00+00:00"
        return cache[day]
    return ts


def _day_of(day0: date):
    cache: dict[str, int] = {}

    def day(ts: str) -> int:
        if ts not in cache:
            cache[ts] = (date.fromisoformat(ts[:10]) - day0).days
        return cache[ts]
    return day


def iter_input(path: pathlib.Path, day0: date) -> Iterator[tuple[str, list[tuple[int, float, str]]]]:
    """(series id, [(day, value, tag), ...]) for each series of an input part, in file order (each series' rows are
    contiguous)."""
    day = _day_of(day0)
    sid, series = None, []
    for r in read_csv_gz(path):
        if r[0] != sid:
            if series:
                yield sid, series
            sid, series = r[0], []
        series.append((day(r[1]), float(r[2]), r[3]))
    if series:
        yield sid, series


def _series_ids(path: pathlib.Path) -> Iterator[str]:
    last = None
    for r in read_csv_gz(path):
        if r[0] != last:
            last = r[0]
            yield last


# --------------------------------------------------------------------------------------------------- the input part

def series_source(cfg: dict, raw: pathlib.Path | None = None, csv_path: pathlib.Path | None = None):
    """The checked public series, one at a time (data.public.series_iter: sha256 and size before any row)."""
    return public.series_iter(raw, cfg, csv_path=csv_path)


def prepare_input(source: Iterable, cfg: dict, parts_dir: pathlib.Path | str) -> dict:
    """Write forecast_input.csv.gz and forecast_input.json from the series in `source` ((key, day rows) as
    data.public.series_iter yields them); return the meta. One series at a time; the part is renamed into place only
    when complete."""
    parts_dir = pathlib.Path(parts_dir)
    parts_dir.mkdir(parents=True, exist_ok=True)
    rule, plan = input_rule(cfg), jobs(cfg)
    day0 = date.fromisoformat(cfg["calendar"]["day0"])
    ts = _ts_of(day0)
    counts = {job: Counter() for job, _, _ in plan}
    zero = Counter()
    store_city, n_series, n_rows = {}, 0, 0
    tmp = parts_dir / f".{INPUT_NAME}.tmp"
    try:
        with _gz_writer(tmp) as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(INPUT_COLUMNS)
            for (city, store, product, _), rows in source:
                s_code, p_code = public_config.code(cfg, "store", store), public_config.code(cfg, "product", product)
                store_city[s_code] = public_config.code(cfg, "city", city)
                n_series += 1
                for job, cutoff, _ in plan:
                    values, tags = public_input(rows, cutoff, rule)
                    counts[job].update(tags)
                    zero[job] += all(v == 0 for _, v, _ in values)
                    sid = F.series_id(job, s_code, p_code)
                    w.writerows((sid, ts(d), repr(v), t) for d, v, t in values)
                    n_rows += len(values)
        os.replace(tmp, parts_dir / INPUT_NAME)
    finally:
        tmp.unlink(missing_ok=True)
    fc = cfg["forecast"]
    meta = {
        "input_label": F.INPUT_LABEL, "reference_history_id": reference_history_id(cfg),
        "day0": cfg["calendar"]["day0"], "horizon_end": horizon_end(cfg),
        "jobs": [{"job": job, "cutoff": cutoff, "first_day": days[0], "last_day": days[-1]}
                 for job, cutoff, days in plan],
        "series": n_series, "series_runs": n_series * len(plan), "rows": n_rows,
        "input_counts": {job: {tag: c.get(tag, 0) for tag in PUBLIC_TAGS} for job, c in counts.items()},
        "all_zero_series": {job: zero.get(job, 0) for job, _, _ in plan},
        "store_city": dict(sorted(store_city.items())),
        "forecast": {"model": fc["model"], "fallback": fc["fallback"], "confidence_level": fc["confidence_level"],
                     "location": fc["location"], "dataset": fc["dataset"]},
        "sha256": _sha256(parts_dir / INPUT_NAME),
    }
    config.atomic_write(parts_dir / INPUT_META, json.dumps(meta, indent=1, sort_keys=True) + "\n")
    return meta


def read_input_meta(parts_dir: pathlib.Path | str) -> dict:
    """The input part's meta, after the input part's sha256 is checked against it."""
    parts_dir = pathlib.Path(parts_dir)
    meta = json.loads((parts_dir / INPUT_META).read_text(encoding="utf-8"))
    actual = _sha256(parts_dir / INPUT_NAME)
    if actual != meta["sha256"]:
        raise ValueError(f"{INPUT_NAME}: sha256 {actual} is not the {meta['sha256']} its meta records; rebuild it")
    return meta


def job_days(meta: dict) -> dict[str, list[int]]:
    return {j["job"]: list(range(j["first_day"], j["last_day"] + 1)) for j in meta["jobs"]}


def forecast_horizon(meta: dict) -> int:
    """The steps after each series' last input day (its cutoff) that cover every run's days."""
    return max(j["last_day"] - j["cutoff"] for j in meta["jobs"])


def split_by_city(input_path: pathlib.Path, store_city: dict, dest_dir: pathlib.Path) -> list[pathlib.Path]:
    """One input part per city, in city code order, from one pass over the input part (the --partition city run;
    05.2-RESEARCH recommendation 3). Every series goes to its store's city; a store without a city is refused."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    cities = sorted(set(store_city.values()))
    paths = {c: dest_dir / f"forecast_input-{c}.csv.gz" for c in cities}
    opened = {c: _gz_writer(p) for c, p in paths.items()}
    handles = {c: o.__enter__() for c, o in opened.items()}
    try:
        writers = {c: csv.writer(h, lineterminator="\n") for c, h in handles.items()}
        for w in writers.values():
            w.writerow(INPUT_COLUMNS)
        for r in read_csv_gz(input_path):
            store = r[0].split("|")[1]
            if store not in store_city:
                raise ValueError(f"{INPUT_NAME}: store {store} has no city in {INPUT_META}")
            writers[store_city[store]].writerow(r)
    finally:
        for o in opened.values():
            o.__exit__(None, None, None)
    return [paths[c] for c in cities]


# --------------------------------------------------------------------------------------------------- providers

@dataclass(frozen=True)
class StreamProvider:
    """`fetch(input_part_path, days_by_job)` yields (series id, raw rows) one series at a time, in any order, where a
    row is {day, low, base, high, confidence_level, status} as the provider gave it (unclamped, unrounded)."""
    name: str
    fetch: Callable[[pathlib.Path, dict], Iterable[tuple[str, list[dict]]]]


def local_rows(series: list[tuple[int, float, str]], days: list[int]) -> list[dict]:
    """The local seasonal-naive stand-in (data.forecast.local_fetch) on one series."""
    return F.local_fetch({"series": series}, {"series": days})["series"]


def input_day0(input_path: pathlib.Path) -> date:
    """day0 of an input part, from the forecast_input.json beside it."""
    meta = input_path.parent / INPUT_META
    if not meta.exists():
        raise ValueError(f"{input_path}: no {INPUT_META} beside it to read day0 from")
    return date.fromisoformat(json.loads(meta.read_text(encoding="utf-8"))["day0"])


def _local_fetch(input_path: pathlib.Path, days_by_job: dict) -> Iterator[tuple[str, list[dict]]]:
    for sid, series in iter_input(input_path, input_day0(input_path)):
        yield sid, local_rows(series, days_by_job[sid.split("|", 1)[0]])


LOCAL_STREAM = StreamProvider(F.LOCAL, _local_fetch)


# --------------------------------------------------------------------------------------------------- the stream

def _num(x: float, places: int) -> str:
    return repr(round(x, places))


class _Runs:
    """Per-run accumulators while points are written."""

    def __init__(self, meta: dict, primary: str):
        self.primary = primary
        self.providers = {j["job"]: Counter() for j in meta["jobs"]}
        self.fallbacks = {j["job"]: [] for j in meta["jobs"]}
        self.others = {j["job"]: {} for j in meta["jobs"]}  # series answered by another provider than primary
        self.clamped = Counter()

    def write(self, w, sid: str, rows: list[dict], days: list[int], provider: str) -> None:
        job, store, sku = sid.split("|")
        need = set(days)
        for r in rows:
            if r["day"] not in need:
                continue
            raw = (r["low"], r["base"], r["high"])
            clamped = any(x < 0 for x in raw)
            low, base, high = (_num(max(0.0, x), 2) for x in raw)
            extra = tuple(_num(x, 4) for x in raw) if clamped else ("", "", "")
            w.writerow((job, store, sku, r["day"], low, base, high, provider, "1" if clamped else "0") + extra)
            self.clamped[job] += clamped
        self.providers[job][provider] += 1
        if provider != self.primary:
            self.others[job][f"{store}|{sku}"] = provider


def _job(sid: str, days_by_job: dict) -> str:
    parts = sid.split("|") if isinstance(sid, str) else []
    if len(parts) != 3 or parts[0] not in days_by_job:
        raise ValueError(f"a provider returned series {sid!r}, not one of this input's series ids")
    return parts[0]


def _check_into(w, acc: _Runs, provider: StreamProvider, path: pathlib.Path, days_by_job: dict, level: float,
                expect: set[str] | None) -> tuple[dict, set[str]]:
    """Check each series the provider answers; write the passing ones. ({failing sid: problems}, the sids seen)."""
    bad, seen = {}, set()
    for sid, rows in provider.fetch(path, days_by_job):
        job = _job(sid, days_by_job)
        if expect is not None and sid not in expect:
            raise ValueError(f"{provider.name} returned series {sid!r}, which it was not asked for")
        if sid in seen:
            raise ValueError(f"{provider.name} returned series {sid!r} twice")
        seen.add(sid)
        problems = F.series_problems(rows, days_by_job[job], level)
        if problems:
            bad[sid] = problems
        else:
            acc.write(w, sid, rows, days_by_job[job], provider.name)
    return bad, seen


def _sort_key(row: list[str]) -> tuple:
    return row[0], row[1], row[2], int(row[3])


def _sorted_rows(unsorted: pathlib.Path, tmp: pathlib.Path, chunk_rows: int) -> Iterator[list[str]]:
    """The rows of `unsorted` (plain CSV, no header) sorted by (job, store, sku, day): sorted runs of `chunk_rows`
    written to `tmp`, then merged. Memory holds one run, not the file."""
    runs = []
    with open(unsorted, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        while True:
            chunk = [r for _, r in zip(range(chunk_rows), reader)]
            if not chunk:
                break
            chunk.sort(key=_sort_key)
            path = tmp / f"run-{len(runs):05d}.csv"
            with open(path, "w", newline="", encoding="utf-8") as out:
                csv.writer(out, lineterminator="\n").writerows(chunk)
            runs.append(path)
            del chunk
    handles = [open(p, newline="", encoding="utf-8") for p in runs]
    try:
        yield from heapq.merge(*(csv.reader(h) for h in handles), key=_sort_key)
    finally:
        for h in handles:
            h.close()


def _run_id(job: str, cutoff: int, basis: str, primary: str, series: int, others: dict) -> str:
    key = config.canonical({"job": job, "cutoff": cutoff, "reference_history_id": basis, "primary": primary,
                            "series": series, "other_providers": others})
    return f"fr-{job}-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def stream_forecast(parts_dir: pathlib.Path | str, primary: StreamProvider, fallback: StreamProvider | None = None,
                    level: float = F.LEVEL, chunk_rows: int = CHUNK_ROWS) -> dict:
    """Forecast every series of the input part with `primary`, check each series' rows (data.forecast
    .series_problems), send only the failing or missing series to `fallback` and check those the same way, then
    write forecast_points.csv.gz (sorted) and forecast_runs.csv.gz. Raises data.forecast.ForecastFailed when a series
    has no valid rows from any provider; the previous parts are then unchanged. Returns a summary."""
    parts_dir = pathlib.Path(parts_dir)
    meta = read_input_meta(parts_dir)
    input_path = parts_dir / INPUT_NAME
    days_by_job = job_days(meta)
    acc = _Runs(meta, primary.name)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=".forecast-", dir=parts_dir))
    try:
        unsorted = tmp / "unsorted.csv"
        with open(unsorted, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            bad, seen = _check_into(w, acc, primary, input_path, days_by_job, level, None)
            for sid in _series_ids(input_path):
                if sid not in seen:
                    bad[sid] = F.series_problems([], days_by_job[_job(sid, days_by_job)], level)
            del seen
            if bad:
                if fallback is None:
                    raise F.ForecastFailed(F._named(bad, primary.name))
                from . import bq  # imported here: data.bq imports data.forecast and data.world
                subset = tmp / INPUT_NAME
                bq.write_input_file(input_path, subset, set(bad))
                shutil.copy(parts_dir / INPUT_META, tmp / INPUT_META)
                try:
                    still, seen = _check_into(w, acc, fallback, subset, days_by_job, level, set(bad))
                except bq.BytesCapExceeded as e:  # the fallback priced over the cap: nothing trained, parts kept
                    e.fallback_series = len(bad)
                    e.failed_checks = dict(sorted(Counter(problems[0]["check"] for problems in bad.values()).items()))
                    raise
                for sid in set(bad) - seen:
                    still[sid] = F.series_problems([], days_by_job[_job(sid, days_by_job)], level)
                if still:
                    raise F.ForecastFailed([q for sid in sorted(still) for q in
                                            F._named({sid: bad[sid]}, primary.name)
                                            + F._named({sid: still[sid]}, fallback.name)])
                for sid, problems in sorted(bad.items()):
                    job, store, sku = sid.split("|")
                    acc.fallbacks[job].append({"store": store, "sku": sku, "failed_check": problems[0]["check"],
                                               "detail": problems[0]["detail"]})
        summary = _write_parts(parts_dir, tmp, meta, acc, unsorted, level, chunk_rows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return summary


def _write_parts(parts_dir, tmp, meta, acc: _Runs, unsorted, level, chunk_rows) -> dict:
    basis = meta["reference_history_id"]
    run_ids, runs = {}, []
    for j in meta["jobs"]:
        job = j["job"]
        run_ids[job] = rid = _run_id(job, j["cutoff"], basis, acc.primary, meta["series"], acc.others[job])
        runs.append({
            "forecast_run_id": rid, "job": job, "demo_day": None if job == F.BACKTEST else int(job[1:]),
            "cutoff_day": j["cutoff"], "days": [j["first_day"], j["last_day"]],
            "configured_model": meta["forecast"]["model"], "confidence_level": level, "label": F.LABEL,
            "input_label": meta["input_label"], "providers": dict(sorted(acc.providers[job].items())),
            "fallbacks": sorted(acc.fallbacks[job], key=lambda f: (f["store"], f["sku"])),
            "clamped_points": acc.clamped[job], "reference_history_id": basis,
            "input_counts": meta["input_counts"][job], "all_zero_series": meta["all_zero_series"][job],
            "series": meta["series"]})
    points_tmp, runs_tmp = tmp / POINTS_NAME, tmp / RUNS_NAME
    series = set()
    n = 0
    with _gz_writer(points_tmp) as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(POINT_COLUMNS)
        for r in _sorted_rows(unsorted, tmp, chunk_rows):
            rid = run_ids[r[0]]
            w.writerow([rid] + r[1:])
            series.add((rid, r[1], r[2], r[7]))
            n += 1
    with _gz_writer(runs_tmp) as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(RUN_COLUMNS)
        for run in sorted(runs, key=lambda r: r["job"]):
            meta_json = {k: v for k, v in run.items() if k not in ("forecast_run_id", "job", "demo_day", "cutoff_day",
                                                                     "days")}
            w.writerow((run["forecast_run_id"], run["job"], "" if run["demo_day"] is None else run["demo_day"],
                        run["cutoff_day"], run["days"][0], run["days"][1], config.canonical(meta_json)))
    summary_points = ({"forecast_run_id": rid, "branch": s, "sku": k, "provider": p} for rid, s, k, p in series)
    headline = F.provider_summary(runs, summary_points)
    del series
    os.replace(points_tmp, parts_dir / POINTS_NAME)
    os.replace(runs_tmp, parts_dir / RUNS_NAME)
    return {"runs": len(runs), "points": n, "series_runs": meta["series_runs"],
            "fallbacks": sum(len(r["fallbacks"]) for r in runs),
            "clamped": sum(r["clamped_points"] for r in runs), "provider_summary": headline,
            "reference_history_id": basis}


def forecast_stage(source: Iterable, cfg: dict, out_dir: pathlib.Path | str, provider="local",
                   fallback: StreamProvider | None = None) -> dict:
    """The forecast stage of the public build: the input part from `source`, then the forecast parts. `provider` is
    "local" (the stand-in, development and tests only) or a StreamProvider."""
    parts = pathlib.Path(out_dir) / PARTS
    prepare_input(source, cfg, parts)
    primary = LOCAL_STREAM if provider == "local" else provider
    return stream_forecast(parts, primary, fallback, cfg["forecast"]["confidence_level"])


# --------------------------------------------------------------------------------------------------- the backtest (D-15)

BACKTEST_LABEL = "accuracy on public sales, in-stock days"
PUBLIC_TAIL = (" versus same weekday last week, using identical inputs; evaluated against held-out public sales on "
               "in-stock days.")
ACTUALS = ("public units (sale_amount x the units factor, an Assumption) on in-stock days of the backtest window; "
           "stockout days are excluded, never filled")
STAND_IN_NOTE = "Stand-in forecast; replaced by the BigQuery run's figures in 05.2-11; never quoted"
RELEASE_NOTE = ("Figures of the released BigQuery forecast; quote them only with their label (accuracy on public "
                "sales, in-stock days) and providers")


class Sums:
    """Pooled sums for WAPE, bias and 80% coverage, added series by series (never averaged); `figures` gives what
    data.forecast._figures gives for the same rows, without holding them."""

    def __init__(self):
        self.n = 0
        self.actual = self.abs_err = self.err = self.base_abs = self.base_err = 0.0
        self.covered = 0

    def add(self, rows) -> None:
        """rows: ((low, base, high), actual, baseline value) per scored day."""
        for (low, base, high), a, b in rows:
            self.n += 1
            self.actual += a
            self.abs_err += abs(base - a)
            self.err += base - a
            self.base_abs += abs(b - a)
            self.base_err += b - a
            self.covered += low <= a <= high

    def figures(self) -> dict:
        if not self.n or self.actual <= 0:
            return {"wape": None, "bias": None, "baseline_wape": None, "baseline_bias": None, "coverage_80": None}
        return {"wape": round(self.abs_err / self.actual, 4), "bias": round(self.err / self.actual, 4),
                "baseline_wape": round(self.base_abs / self.actual, 4),
                "baseline_bias": round(self.base_err / self.actual, 4),
                "coverage_80": round(self.covered / self.n, 4)}


def _backtest_run(runs_path: pathlib.Path, cfg: dict) -> tuple[str, dict]:
    _, cutoff, days = next(j for j in jobs(cfg) if j[0] == F.BACKTEST)
    rows = [r for r in read_csv_gz(runs_path) if r[1] == F.BACKTEST]
    if len(rows) != 1:
        raise ValueError(f"{runs_path.name}: {len(rows)} backtest runs, expected 1")
    r = rows[0]
    if (int(r[3]), int(r[4]), int(r[5])) != (cutoff, days[0], days[-1]):
        raise ValueError(f"the backtest run (cutoff {r[3]}, days {r[4]}..{r[5]}) does not match the reference "
                         f"calendar (cutoff {cutoff}, days {days[0]}..{days[-1]})")
    return r[0], json.loads(r[6])


def score_public(points_part: pathlib.Path | str, source: Iterable, cfg: dict) -> dict:
    """Accuracy of the backtest run on public data (DATA-12, D-15).

    Actuals are the public units of the window days -backtest_days..-1 that are in stock, read from the same checked
    public series the forecast input came from (never the stock stage's parts); a stockout day is excluded and
    counted. The baseline is "same weekday last week": the latest input value on the day's weekday at or before the
    cutoff, from the identical input the model got. WAPE, bias and coverage are pooled from sums over the series
    whose in-stock actuals sum to more than 0 (`scored_days`, `scored_series`); a series summing to 0 reports MAE only
    (counted, with the mean of their MAEs). Per-category figures use reference categories.json's level."""
    points_part = pathlib.Path(points_part)
    rid, run_meta = _backtest_run(points_part.parent / RUNS_NAME, cfg)
    _, cutoff, days = next(j for j in jobs(cfg) if j[0] == F.BACKTEST)
    model: dict[tuple[str, str], dict[int, tuple[float, float, float]]] = {}
    provider_of: dict[tuple[str, str], str] = {}
    for r in read_csv_gz(points_part):
        if r[0] == rid:
            model.setdefault((r[1], r[2]), {})[int(r[3])] = (float(r[4]), float(r[5]), float(r[6]))
            provider_of[(r[1], r[2])] = r[7]
    rule = input_rule(cfg)
    level = cfg["categories"]["level"]
    at = public.CATEGORY_LEVELS.index(level)
    pooled, per_cat = Sums(), {}
    tags = Counter()
    excluded = scored_days = scored_series = no_in_stock = discounted = unknown = 0
    mae_only, mae_sum = 0, 0.0
    for (_, store, product, cats), rows in source:
        key = (public_config.code(cfg, "store", store), public_config.code(cfg, "product", product))
        points = model.pop(key, None)
        if points is None:
            raise ValueError(f"the backtest run has no point for {key[0]} {key[1]}")
        values, counts = public_input(rows, cutoff, rule)
        tags.update(counts)
        by_day = {r["day"]: r for r in rows}
        scored = []
        for d in days:
            if d not in points:
                raise ValueError(f"the backtest run has no point for {key[0]} {key[1]} day {d}")
            if d not in by_day:
                raise ValueError(f"the public series {key[0]} {key[1]} has no row for day {d}")
            row = by_day[d]
            if row["stockout"]:
                excluded += 1
                continue
            same = [v for day, v, _ in values if day % 7 == d % 7]
            if not same:
                raise ValueError(f"no input on day {d}'s weekday at or before day {cutoff} for {key[0]} {key[1]}")
            scored.append((points[d], units_of(row, rule), same[-1]))
            discounted += (not row["unknown_price"]) and row["discount"] < rule.full_price
            unknown += row["unknown_price"]
        if not scored:
            no_in_stock += 1
            continue
        if sum(a for _, a, _ in scored) > 0:
            scored_days += len(scored)
            scored_series += 1
            pooled.add(scored)
            per_cat.setdefault(public_config.code(cfg, "category", cats[at]), Sums()).add(scored)
        else:
            mae_only += 1
            mae_sum += sum(abs(p[1] - a) for p, a, _ in scored) / len(scored)
            per_cat.setdefault(public_config.code(cfg, "category", cats[at]), Sums())
    if model:
        raise ValueError(f"{len(model)} backtest series are not in the public series, e.g. {sorted(model)[0]}")
    providers = dict(sorted(Counter(provider_of.values()).items()))
    figures = pooled.figures()
    head = F.comparison_label(providers)[:-len(F.COMPARISON_TAIL)]
    per_category = {}
    for code in sorted(per_cat):
        f = per_cat[code].figures()
        per_category[code] = {**f, "model_vs_baseline": F._versus(f["wape"], f["baseline_wape"])}
    return {
        "label": BACKTEST_LABEL, "window": [days[0], days[-1]], "cutoff_day": cutoff, "forecast_run_id": rid,
        "reference_history_id": run_meta["reference_history_id"], "providers": providers, **figures,
        "model_vs_baseline": F._versus(figures["wape"], figures["baseline_wape"]),
        "baseline": "same weekday last week", "bias_definition": F.BIAS_DEFINITION, "actuals": ACTUALS,
        "input_label": F.INPUT_LABEL, "scored_days": scored_days, "scored_series": scored_series,
        "excluded_stockout_days": excluded, "series_without_in_stock_days": no_in_stock,
        "discounted_days_scored": discounted, "unknown_price_days_scored": unknown,
        "mae_only_series": {"count": mae_only, "mean_mae": round(mae_sum / mae_only, 4) if mae_only else None},
        "input_counts": {tag: tags.get(tag, 0) for tag in PUBLIC_TAGS},
        "category_level": level, "per_category": per_category,
        "comparison_label": head + PUBLIC_TAIL,
        "note": STAND_IN_NOTE if F.LOCAL in providers else RELEASE_NOTE,
    }


# --------------------------------------------------------------------------------------------------- command line

def _summary_line(s: dict) -> str:
    return (f"forecast: {s['runs']} runs, {s['points']} points; providers {s['provider_summary']['headline']}; "
            f"fallbacks {s['fallbacks']}; clamped {s['clamped']}")


def _dry_run_line(report: list[dict], cap: int) -> str:
    """One line to compare: every priced estimate against the per-query cap."""
    from . import bq
    priced = ", ".join(f"{r['query']} {r['bytes']} bytes ({bq.gib(r['bytes'])})" for r in report
                       if r["bytes"] is not None)
    over = [r["query"] for r in report if r["over_cap"]]
    verdict = f"OVER the cap: {', '.join(over)}; do not run the paid step" if over else "every estimate within it"
    return (f"cap {cap} bytes ({bq.gib(cap)}) per query; {priced or 'nothing priced'}; {verdict}; the ARIMA_PLUS "
            "fallback is priced after AI.FORECAST, on the series that need it")


def _spent(ledger: list[dict]) -> str:
    from . import bq
    if not ledger:
        return "no paid query was sent"
    each = []
    for q in ledger:
        billed = q.get("billed_bytes")
        n = billed if billed is not None else q["estimate_bytes"]
        each.append(f"{q['query']} {'billed' if billed is not None else 'estimated'} {n} bytes ({bq.gib(n)})")
    return "already sent: " + ", ".join(each)


def _cap_refusal(out: pathlib.Path, parts: pathlib.Path, e, ledger: list[dict]) -> str:
    """The one line (and, for the fallback, the diagnostics file) when a query was priced over the cap. Names the
    query, the estimate and the cap, never a project or table."""
    from . import bq
    if e.query != "arima_plus_model":
        return f"refused: {e}; {_spent(ledger)}; {parts / POINTS_NAME} is unchanged"
    n = getattr(e, "fallback_series", None)
    path = out / DIAGNOSTICS
    path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(path, json.dumps({
        "refused": "arima_plus_model", "fallback_series": n, "failed_checks": getattr(e, "failed_checks", {}),
        "estimate_bytes": e.estimate, "cap_bytes": e.cap, "queries_sent": ledger,
        "note": "AI.FORECAST ran (its cost is spent); the ARIMA_PLUS training for the series that need the fallback "
                "was priced over the per-query cap, so nothing was trained and the points part is unchanged",
        "next_step": "read failed_checks and fallback_series first; raising reference/public.json "
                     "budget.max_bytes_billed (a Policy) is the user's decision, never automatic"},
        indent=1, sort_keys=True) + "\n")
    return (f"refused: arima_plus_model: the dry run estimates {e.estimate} bytes ({bq.gib(e.estimate)}) for the {n} "
            f"series that need the fallback, over the cap of {e.cap} bytes ({bq.gib(e.cap)}) per query; nothing was "
            f"trained; {_spent(ledger)}; wrote {path}; {parts / POINTS_NAME} is unchanged")


def _write_diagnostics(out: pathlib.Path, primary: str, fallback: str | None, e: F.ForecastFailed) -> pathlib.Path:
    path = out / DIAGNOSTICS
    path.parent.mkdir(parents=True, exist_ok=True)
    config.atomic_write(path, json.dumps({"provider": primary, "fallback": fallback, "problems": e.problems,
                                          "next_step": F.NEXT_STEP.replace("forecast.json", "points part")},
                                         indent=1, sort_keys=True) + "\n")
    return path


def _config(args) -> dict:
    return public_config.load_for(args.fixture) if args.fixture else public_config.load()


def _bigquery_main(parser, args, cfg: dict) -> int:
    """forecast --provider ai_forecast|arima_plus: the user's run (D-22). Reads the input part in <out>/parts (or
    builds it first from --raw/--csv); --preflight needs no network; --dry-run issues only free dry runs."""
    from . import bq  # imported here: data.bq imports data.forecast and data.world

    parts = args.out / PARTS
    if args.raw is not None or args.csv is not None:
        prepare_input(series_source(cfg, args.raw, args.csv), cfg, parts)
    meta = read_input_meta(parts) if (parts / INPUT_META).exists() else None
    horizon = forecast_horizon(meta) if meta else None
    history = reference_history_id(cfg)
    fc = cfg["forecast"]
    cap = cfg["budget"]["max_bytes_billed"]  # every query job's maximum_bytes_billed (user 2026-10-17)
    ledger: list[dict] = []  # every query sent, with its estimate and billed bytes
    if args.preflight:
        cities = len(set(meta["store_city"].values())) if meta and args.partition else 1
        info = bq.preflight_public(cfg, args.project, meta, horizon, history, args.provider, cities)
        text = info.pop("sql")
        print(json.dumps(info, indent=1))
        if text:
            print(text, end="")
        return 1 if info["problems"] else 0
    if not args.project:
        print(F.NO_PROJECT, file=sys.stderr)
        return 2
    if meta is None:
        print(f"refused: no {INPUT_NAME} in {parts}; run --provider local first or pass --raw/--csv", file=sys.stderr)
        return 2
    if meta["reference_history_id"] != history:
        print(f"refused: {INPUT_NAME} was built on other inputs ({meta['reference_history_id']}, this reference gives "
              f"{history}); rebuild it", file=sys.stderr)
        return 2
    day0 = date.fromisoformat(meta["day0"])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=".partition-", dir=parts)) if args.partition else None
    try:
        try:
            ids = bq.PublicIds(args.project, fc["dataset"])
            partition = split_by_city(parts / INPUT_NAME, meta["store_city"], tmp) if tmp else None
            ai, arima = bq.public_fetchers(args.project, fc["location"], fc["dataset"], fc["confidence_level"],
                                           fc["model"], day0, horizon, partition=partition, max_bytes_billed=cap,
                                           ledger=ledger)
            if args.dry_run:
                if args.provider != F.AI_FORECAST:
                    parser.error("--dry-run checks the AI.FORECAST run; use it with --provider ai_forecast")
                report = bq.dry_run_file(bq.make_client(args.project, fc["location"]), ids, parts / INPUT_NAME,
                                         horizon, fc["confidence_level"], fc["model"], max_bytes_billed=cap)
                print(json.dumps({"dry_run": report, "max_bytes_billed": cap, "series_runs": meta["series_runs"],
                                  "input_rows": meta["rows"], "horizon": horizon}, indent=1))
                print(_dry_run_line(report, cap))
                print(f"dry run only: no paid query ran; {parts / POINTS_NAME} is unchanged")
                return 1 if any(r["over_cap"] for r in report) else 0
        except bq.BigQueryConfigError as e:
            print(f"BigQuery configuration refused: {e}", file=sys.stderr)
            return 2
        primary = StreamProvider(F.AI_FORECAST, ai) if args.provider == F.AI_FORECAST else \
            StreamProvider(F.ARIMA_PLUS, arima)
        fallback = StreamProvider(F.ARIMA_PLUS, arima) if args.provider == F.AI_FORECAST else None
        try:
            summary = stream_forecast(parts, primary, fallback, fc["confidence_level"])
        except bq.BytesCapExceeded as e:
            print(_cap_refusal(args.out, parts, e, ledger), file=sys.stderr)
            return 1
        except F.ForecastFailed as e:
            path = _write_diagnostics(args.out, primary.name, fallback.name if fallback else None, e)
            for line in e.lines()[:20]:
                print(f"FAILED {line}", file=sys.stderr)
            print(f"wrote {path}; {parts / POINTS_NAME} is unchanged", file=sys.stderr)
            return 1
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    print(_summary_line(summary))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="frn.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fc = sub.add_parser("forecast", help="the public forecast parts")
    fc.add_argument("--provider", choices=["local", F.AI_FORECAST, F.ARIMA_PLUS], required=True)
    src = fc.add_mutually_exclusive_group()
    src.add_argument("--raw", type=pathlib.Path, help="the folder holding the dataset files")
    src.add_argument("--csv", type=pathlib.Path, help="a checked CSV in the dataset's schema (tests, fixtures)")
    fc.add_argument("--fixture", type=pathlib.Path, help="a fixture's override JSON (dataset and calendar blocks)")
    fc.add_argument("--out", type=pathlib.Path, required=True, help="the output folder; parts go in <out>/parts")
    fc.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    help="Google Cloud project for the BigQuery providers (default: GOOGLE_CLOUD_PROJECT); no default")
    fc.add_argument("--dry-run", action="store_true",
                    help="load the input file and dry-run every query; print the bytes, run nothing paid, write nothing")
    fc.add_argument("--preflight", action="store_true",
                    help="check the package, project, config, input part, horizon and SQL without any network call")
    fc.add_argument("--partition", choices=["city"],
                    help="one AI.FORECAST per city into the same output table, only if the single query fails or "
                         "times out")
    bt = sub.add_parser("backtest", help="score the backtest run on held-out public in-stock days")
    bsrc = bt.add_mutually_exclusive_group(required=True)
    bsrc.add_argument("--raw", type=pathlib.Path, help="the folder holding the dataset files")
    bsrc.add_argument("--csv", type=pathlib.Path, help="a checked CSV in the dataset's schema (tests, fixtures)")
    bt.add_argument("--fixture", type=pathlib.Path, help="a fixture's override JSON (dataset and calendar blocks)")
    bt.add_argument("--out", type=pathlib.Path, required=True, help="the output folder holding <out>/parts")
    bt.add_argument("--report", type=pathlib.Path, required=True, help="write the backtest report JSON here")
    args = parser.parse_args(argv)
    try:
        cfg = _config(args)
        if args.command == "forecast":
            if args.provider != "local":
                return _bigquery_main(parser, args, cfg)
            if args.dry_run or args.preflight or args.partition:
                parser.error("--dry-run, --preflight and --partition are for a BigQuery provider")
            if args.raw is None and args.csv is None:
                parser.error("--provider local needs --raw or --csv")
            source = series_source(cfg, args.raw, args.csv)
            try:
                summary = forecast_stage(source, cfg, args.out, "local")
            except F.ForecastFailed as e:
                path = _write_diagnostics(args.out, F.LOCAL, None, e)
                for line in e.lines():
                    print(f"FAILED {line}", file=sys.stderr)
                print(f"wrote {path}; the previous parts are unchanged", file=sys.stderr)
                return 1
            print(_summary_line(summary))
            return 0
        report = score_public(args.out / PARTS / POINTS_NAME, series_source(cfg, args.raw, args.csv), cfg)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        config.atomic_write(args.report, json.dumps(report, indent=1, sort_keys=True) + "\n")
        print(f"backtest: {report['label']}; WAPE {report['wape']} vs baseline {report['baseline_wape']}, bias "
              f"{report['bias']}, coverage {report['coverage_80']}; {report['scored_days']} days scored, "
              f"{report['excluded_stockout_days']} stockout days excluded; providers {report['providers']}")
        return 0
    except (public.PublicDataError, ConfigError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
