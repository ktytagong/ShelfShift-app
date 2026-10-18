"""A public artifact set in shared relational tables, installed by COPY under the manifest's checks and read one
series at a time (spec §4 Data flow 1, §5 Public dataset; DATA-08, DATA-15; 05.2-CONTEXT D-08, D-11, D-15).

build/api reads the artifact format and never imports the pipeline that writes it (build/web's data/manifest.py): a
set is `manifest.json` plus one gz CSV part with a header row per reference table. `install` trusts only what the
manifest lists and the bytes it can check:

- the manifest is of kind "public", format 1, and its dataset_version recomputes (`data.versions.digest("ds-", the
  manifest without it)`);
- every part names a table of ALLOWED_TABLES (the table list is code, never data) and each table is named once; the
  part sits in the set's parts/ folder; its bytes are read once, their sha256 must equal the manifest's, and only those
  checked bytes are decompressed and loaded; its header must equal the manifest's columns and the table's columns
  (information_schema, dataset_version left out), so COPY never names a column the code did not create;
- COPY loads each part (dataset_version prefixed to every row) and the rows it loaded must equal the manifest's count;
- coverage (D-15): every forecast run covers every series on every day from its first to its last day, and hidden
  demand covers every series on days 0..last_demo_day; a refusal names the job, the series and the day;
- with `allow_local` False (the deploy path) every forecast point's provider is ai_forecast or arima_plus.

All of it runs in one transaction under an advisory lock per dataset_version, with ref_set written last, so a refused
set leaves no row and a version listed in ref_set is complete. Installing a version that is already there is a no-op.
Every refusal is a ValueError naming the part (or the manifest field).

`PublicRef` holds in memory only what is small: the manifest's settings (calendar, Policy, markdown response, currency,
the label), the products, the stores and the forecast runs' metadata (thousands of rows at most). Sales, forecast
points, capacity, runs, the day-0 lots and deliveries and hidden demand are read per series (store and product), per
(product, day) or per key, on the caller's connection when it is inside a transaction (WR-10), through bounded caches
(sizes below: infrastructure, not retailer values), never as a whole table (D-11; research Pitfall 2: about 128 MB per
million rows against Cloud Run's 1 GiB). It answers the same methods as the synthetic `refdata.Ref`, so every call
site reads both kinds alike. `PublicTruth` answers hidden demand per (product, day) and physical stock per lot: only
advance-day and the template checks call `refdata.truth` (spec §5).
"""
from __future__ import annotations

import csv
import functools
import gzip
import hashlib
import io
import pathlib
from collections import Counter
from datetime import date, timedelta

from data import config as refconfig
from data import forecast as F
from data import versions
from engine.model import MarkdownResponse, Policy, Product, Run

from . import db

KIND = "public"
FORMAT = 1
MANIFEST = "manifest.json"
PARTS_DIR = "parts"
ALLOWED_TABLES = ("ref_sales_day", "ref_weather_day", "ref_product", "ref_store", "ref_lot0", "ref_inbound0",
                  "ref_run", "ref_capacity", "ref_forecast_run", "ref_forecast_point", "ref_truth_physical",
                  "ref_truth_demand")
RELEASE_PROVIDERS = (F.AI_FORECAST, F.ARIMA_PLUS)
INSTALL_LOCK = 7212027  # pg_advisory_xact_lock class key: one install of a dataset_version at a time
COPY_CHUNK = 1 << 16  # characters of CSV per COPY message
SERIES_CACHE = 4096  # per-series results kept per PublicRef (forecast, runs, sales, capacity lookups)


def _read(sql: str, **params) -> list[dict]:
    """Rows read on the caller's connection when this thread is inside a transaction, else in a short one (WR-10)."""
    conn = db.current()
    if conn is not None:
        return db.rows(conn, sql, **params)
    with db.tx() as own:
        return db.rows(own, sql, **params)


# --------------------------------------------------------------------------------------------------- install

def _manifest(root: pathlib.Path) -> dict:
    try:
        m = refconfig.read(root / MANIFEST)
    except (refconfig.ConfigError, OSError) as e:
        raise ValueError(f"{MANIFEST}: {e}") from None
    if not isinstance(m, dict) or m.get("kind") != KIND or m.get("format") != FORMAT:
        raise ValueError(f"{MANIFEST}: not a {KIND} artifact set of format {FORMAT}")
    for field in ("dataset_version", "files", "settings", "forecast"):
        if field not in m:
            raise ValueError(f"{MANIFEST}: missing {field}")
    want = versions.digest("ds-", {k: v for k, v in m.items() if k != "dataset_version"})
    if m["dataset_version"] != want:
        raise ValueError(f"{MANIFEST}: dataset_version {m['dataset_version']!r} does not recompute ({want}); the "
                         "manifest changed after the build")
    return m


def _check_tables(m: dict) -> None:
    """Every part names an allowed table, each allowed table exactly once."""
    named = Counter()
    for part, entry in m["files"].items():
        rel = entry.get("path", part)
        if entry.get("table") not in ALLOWED_TABLES:
            raise ValueError(f"{rel}: table {entry.get('table')!r} is not one of the reference tables "
                             "(api/publicref.py ALLOWED_TABLES)")
        named[entry["table"]] += 1
    twice = sorted(t for t, n in named.items() if n > 1)
    missing = sorted(set(ALLOWED_TABLES) - set(named))
    if twice or missing:
        raise ValueError(f"{MANIFEST}: files must name each reference table once (twice: {twice}, missing: {missing})")


def _table_columns(conn, table: str) -> list[str]:
    return [r["column_name"] for r in db.rows(conn, "SELECT column_name FROM information_schema.columns WHERE "
                                                    "table_schema = current_schema() AND table_name = :t "
                                                    "ORDER BY ordinal_position", t=table)
            if r["column_name"] != "dataset_version"]


def _chunks(dataset_version: str, reader):
    """The part's rows after its header as CSV text, dataset_version first, in COPY-sized chunks (pg8000 sends each
    item of an iterable stream as one CopyData message). An empty field stays empty: COPY reads it as null."""
    buf = io.StringIO()
    out = csv.writer(buf, lineterminator="\n")
    for row in reader:
        out.writerow([dataset_version, *row])
        if buf.tell() >= COPY_CHUNK:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    if buf.tell():
        yield buf.getvalue()


def _load_part(conn, root: pathlib.Path, dataset_version: str, part: str, entry: dict) -> None:
    rel, table, columns = entry.get("path", part), entry["table"], entry.get("columns")
    parts = (root / PARTS_DIR).resolve()
    path = (root / rel).resolve()
    if path.parent != parts:
        raise ValueError(f"{rel}: not a file of the set's {PARTS_DIR}/ folder")
    try:
        data = path.read_bytes()  # read once: only the checked bytes are loaded
    except OSError as e:
        raise ValueError(f"{rel}: {e}") from None
    actual = hashlib.sha256(data).hexdigest()
    if actual != entry.get("sha256"):
        raise ValueError(f"{rel}: sha256 {actual} is not the manifest's {entry.get('sha256')}; the part changed after "
                         "the build")
    reader = csv.reader(gzip.open(io.BytesIO(data), "rt", encoding="utf-8", newline=""))
    header = next(reader, None)
    if header != columns:
        raise ValueError(f"{rel}: header {header} is not the manifest's columns {columns}")
    expected = _table_columns(conn, table)
    if columns != expected:
        raise ValueError(f"{rel}: columns {columns} are not {table}'s {expected}")
    cursor = conn.connection.driver_connection.cursor()  # pg8000's own cursor: COPY with stream= (research Pattern 1)
    try:
        cursor.execute(f"COPY {table} (dataset_version, {', '.join(columns)}) FROM STDIN WITH (FORMAT csv)",
                       stream=_chunks(dataset_version, reader))
    except Exception as e:  # pg8000 DatabaseError: a malformed row, a duplicate key, a null in a NOT NULL column
        raise ValueError(f"{rel}: COPY into {table} refused: {e}") from None
    if cursor.rowcount != entry.get("rows"):
        raise ValueError(f"{rel}: {cursor.rowcount} rows loaded, the manifest says {entry.get('rows')}")
    db.run(conn, f"ANALYZE {table}")  # planner statistics for the checks below (a fresh table has none)


SERIES = "install_series"  # the set's series (store, sku), from its sales, for the checks of one install


def _series(conn, dataset_version: str) -> int:
    """The set's series into a temporary table that drops at commit or rollback; returns how many."""
    db.run(conn, f"CREATE TEMPORARY TABLE {SERIES} ON COMMIT DROP AS SELECT DISTINCT store, sku FROM ref_sales_day "
                 f"WHERE dataset_version = :v", v=dataset_version)
    db.run(conn, f"ALTER TABLE {SERIES} ADD PRIMARY KEY (store, sku)")
    db.run(conn, f"ANALYZE {SERIES}")
    return db.one(conn, f"SELECT count(*) AS n FROM {SERIES}")["n"]


def _check_forecast(conn, m: dict, dataset_version: str, n_series: int) -> None:
    """Every forecast run of the manifest is in the table, every point belongs to one, and each run covers every
    series on each of its days: one grouped pass; the gap or the stray point is looked up only on a refusal."""
    rel = m["files"]["forecast_points"]["path"]
    v = {"v": dataset_version}
    runs = db.rows(conn, f"SELECT r.forecast_run_id, r.job, r.first_day, r.last_day, count(p.day) AS total, "
                         f"count(s.store) FILTER (WHERE p.day BETWEEN r.first_day AND r.last_day) AS good "
                         f"FROM ref_forecast_run r LEFT JOIN ref_forecast_point p ON p.dataset_version = "
                         f"r.dataset_version AND p.forecast_run_id = r.forecast_run_id LEFT JOIN {SERIES} s "
                         f"ON s.store = p.store AND s.sku = p.sku WHERE r.dataset_version = :v "
                         f"GROUP BY r.forecast_run_id, r.job, r.first_day, r.last_day ORDER BY r.job, r.forecast_run_id",
                   **v)
    listed = sorted(r.get("forecast_run_id") for r in m["forecast"].get("runs", []))
    if sorted(r["forecast_run_id"] for r in runs) != listed:
        raise ValueError(f"{MANIFEST}: forecast runs {listed} are not the forecast_runs part's "
                         f"{sorted(r['forecast_run_id'] for r in runs)}")
    points = db.one(conn, "SELECT count(*) AS n FROM ref_forecast_point WHERE dataset_version = :v", **v)["n"]
    if points != sum(r["total"] for r in runs):
        stray = db.one(conn, "SELECT forecast_run_id FROM ref_forecast_point p WHERE p.dataset_version = :v AND NOT "
                             "EXISTS (SELECT 1 FROM ref_forecast_run r WHERE r.dataset_version = p.dataset_version AND "
                             "r.forecast_run_id = p.forecast_run_id) LIMIT 1", **v)
        raise ValueError(f"{rel}: forecast_run_id {stray['forecast_run_id']!r} is not a run of the forecast_runs part")
    for r in runs:
        days = r["last_day"] - r["first_day"] + 1
        if r["good"] == r["total"] == n_series * days:
            continue  # one row per key (the primary key), all inside the run's series and days, as many as needed
        p = {**v, "f": r["forecast_run_id"], "a": r["first_day"], "b": r["last_day"]}
        gap = db.one(conn, f"SELECT s.store, s.sku, d.day FROM {SERIES} s CROSS JOIN generate_series(CAST(:a AS int), "
                           f"CAST(:b AS int)) AS d(day) WHERE NOT EXISTS (SELECT 1 FROM ref_forecast_point x WHERE "
                           f"x.dataset_version = :v AND x.forecast_run_id = :f AND x.store = s.store AND x.sku = s.sku "
                           f"AND x.day = d.day) ORDER BY s.store, s.sku, d.day LIMIT 1", **p)
        if gap:
            raise ValueError(f"{rel}: no point for job {r['job']}, series {gap['store']}|{gap['sku']}, day "
                             f"{gap['day']} (every job covers its days)")
        extra = db.one(conn, f"SELECT x.store, x.sku, x.day FROM ref_forecast_point x WHERE x.dataset_version = :v "
                             f"AND x.forecast_run_id = :f AND (x.day NOT BETWEEN :a AND :b OR NOT EXISTS (SELECT 1 FROM "
                             f"{SERIES} s WHERE s.store = x.store AND s.sku = x.sku)) ORDER BY x.store, x.sku, x.day "
                             f"LIMIT 1", **p)
        raise ValueError(f"{rel}: job {r['job']}, series {extra['store']}|{extra['sku']}, day {extra['day']}: a point "
                         "the job does not forecast")


def _check_truth(conn, m: dict, dataset_version: str, n_series: int) -> None:
    """Hidden demand covers every series on days 0..last_demo_day (D-15: the public eval week is the held-out demand;
    forecasts still reach horizon_end)."""
    rel = m["files"]["truth_demand"]["path"]
    last = int(m["settings"]["calendar"]["last_demo_day"])
    p = {"v": dataset_version, "b": last}
    n = db.one(conn, f"SELECT count(*) AS n FROM ref_truth_demand t JOIN {SERIES} s USING (store, sku) "
                     f"WHERE t.dataset_version = :v AND t.day BETWEEN 0 AND :b", **p)["n"]
    if n == n_series * (last + 1):
        return
    gap = db.one(conn, f"SELECT s.store, s.sku, d.day FROM {SERIES} s CROSS JOIN generate_series(0, CAST(:b AS int)) "
                       f"AS d(day) WHERE NOT EXISTS (SELECT 1 FROM ref_truth_demand x WHERE x.dataset_version = :v "
                       f"AND x.store = s.store AND x.sku = s.sku AND x.day = d.day) ORDER BY s.store, s.sku, d.day "
                       f"LIMIT 1", **p)
    raise ValueError(f"{rel}: no hidden demand for series {gap['store']}|{gap['sku']}, day {gap['day']} (truth covers "
                     f"days 0..last_demo_day {last})")


def _check_providers(conn, m: dict, dataset_version: str) -> None:
    """The deploy path: every point comes from BigQuery (ai_forecast, or arima_plus as its declared fallback)."""
    rel = m["files"]["forecast_points"]["path"]
    for r in db.rows(conn, "SELECT provider, count(*) AS n FROM ref_forecast_point WHERE dataset_version = :v "
                           "GROUP BY provider ORDER BY provider", v=dataset_version):
        if r["provider"] not in RELEASE_PROVIDERS:
            raise ValueError(f"{rel}: refusing provider {r['provider']!r}: {r['n']} points; a release set's forecast "
                             f"comes from {' or '.join(RELEASE_PROVIDERS)} (the local stand-in is for development and "
                             "tests only; 05.2-11)")


def install(ref_dir: pathlib.Path | str, allow_local: bool = True) -> str:
    """Check and load a public artifact set; returns its dataset_version. A version already in ref_set is a no-op."""
    root = pathlib.Path(ref_dir)
    m = _manifest(root)
    dv = m["dataset_version"]
    if _read("SELECT 1 AS x FROM ref_set WHERE dataset_version = :v", v=dv):
        return dv
    _check_tables(m)
    with db.tx() as conn:
        db.one(conn, "SELECT pg_advisory_xact_lock(:k, hashtext(:v)) AS x", k=INSTALL_LOCK, v=dv)
        if db.one(conn, "SELECT 1 AS x FROM ref_set WHERE dataset_version = :v", v=dv):
            return dv  # another install of this version finished while this one waited
        for part, entry in m["files"].items():  # manifest order
            _load_part(conn, root, dv, part, entry)
        if not allow_local:
            _check_providers(conn, m, dv)
        n_series = _series(conn, dv)
        _check_forecast(conn, m, dv, n_series)
        _check_truth(conn, m, dv, n_series)
        db.run(conn, "INSERT INTO ref_set (dataset_version, kind, manifest) VALUES (:v, :k, CAST(:m AS jsonb))",
               v=dv, k=KIND, m=db.js(m))
    return dv


def kind_of(dataset_version: str) -> dict | None:
    """The ref_set row (kind, manifest) of a dataset_version, or None for a set installed some other way."""
    found = _read("SELECT kind, manifest FROM ref_set WHERE dataset_version = :v", v=dataset_version)
    return found[0] if found else None


# --------------------------------------------------------------------------------------------------- reads

class Called(dict):
    """A part of a reference set read as a dict (its payload, as before) that also answers a call: lets `forecast` be
    both the forecast block and the shared method `forecast(day, store, sku)` on either kind of Ref."""

    def __init__(self, payload: dict, call):
        super().__init__(payload)
        self._call = call

    def __call__(self, *args):
        return self._call(*args)


def _cached(fn):
    return functools.lru_cache(maxsize=SERIES_CACHE)(fn)


def _provider_summary(runs: list[dict]) -> dict:
    """data.forecast.provider_summary's figures and headline from each run's own series counts by provider (its
    meta.providers), so the points are never read; tests/api/test_publicref.py checks both give the same."""
    demo, back = Counter(), Counter()
    checks = Counter()
    for r in runs:
        (back if r["job"] == F.BACKTEST else demo).update(r["providers"])
        if r["job"] != F.BACKTEST:
            checks.update(f["failed_check"] for f in r.get("fallbacks") or [])
    total = sum(demo.values())
    order = {name: i for i, name in enumerate(F.PROVIDER_ORDER)}
    top = min(demo, key=lambda name: (-demo[name], order.get(name, len(order)), name)) if demo else None
    headline = f"{F.PROVIDERS.get(top, top)} for {demo[top]} of {total} series-runs" if top else "no demo series-runs"
    if sum(checks.values()):
        headline += f"; ARIMA_PLUS fallback for {sum(checks.values())} (failed {F._check_phrase(checks)})"
    return {"demo": {"series_runs": total, "by_provider": dict(sorted(demo.items())),
                     "fallback_checks": dict(sorted(checks.items()))},
            "backtest": {"series_runs": sum(back.values()), "by_provider": dict(sorted(back.items()))},
            "headline": headline}


def _run(r: dict) -> Run:
    return Run(r["run_code"], r["origin"], r["dest"], r["day"], r["loading_cutoff_min"], r["departs_min"],
               r["arrives_min"], r["spare_totes"], r["cold_chain"], r["fixed_cost"], r["per_unit_cost"],
               cancel_cost=r["cancel_cost"])


RUN_COLUMNS = ("run_code, origin, dest, day, loading_cutoff_min, departs_min, arrives_min, spare_totes, cold_chain, "
               "fixed_cost, per_unit_cost, cancel_cost")
LOT_COLUMNS = ("lot_id, store AS branch, sku, qty, expiry_day, received_seq, markdown_pct, expiry_source, "
               "last_verified_day, tag, origin")
INBOUND_COLUMNS = ("inbound_id, store AS branch, sku, qty, arrival_day, arrival_min, expiry_day, received_seq, "
                   "expiry_source, last_verified_day, NULL AS tag, origin")


class PublicRef:
    """A public set's reference data: small tables in memory, everything per series read on demand (D-11)."""

    kind = KIND

    def __init__(self, dataset_version: str, manifest: dict):
        self.dataset_version = dataset_version
        self.manifest = manifest
        self.settings = manifest["settings"]
        v = dataset_version
        products = _read("SELECT * FROM ref_product WHERE dataset_version = :v ORDER BY sku", v=v)
        self.products = {p["sku"]: Product(p["sku"], p["zone"], p["price"], p["disposal_cost"], p["units_per_tote"],
                                           p["kg_per_unit"]) for p in products}
        self.names = {p["sku"]: p["name"] or p["sku"] for p in products}  # the code; a name is never invented (D-02)
        self.schedules = {p["sku"]: tuple(tuple(step) for step in p["schedule"]) for p in products}
        stores = _read("SELECT store, city, geo_known FROM ref_store WHERE dataset_version = :v ORDER BY store", v=v)
        self.stores = {s["store"]: {"city": s["city"], "geo_known": s["geo_known"]} for s in stores}
        self.branches = [s["store"] for s in stores]
        runs = [{**r["meta"], **{k: r[k] for k in r if k != "meta"}}
                for r in _read("SELECT * FROM ref_forecast_run WHERE dataset_version = :v ORDER BY job", v=v)]
        self.forecast_runs = {r["demo_day"]: r for r in runs if r["job"] != F.BACKTEST}
        self._run_of_day = {day: r["forecast_run_id"] for day, r in self.forecast_runs.items()}
        self.forecast = Called({**manifest["forecast"], "provider_summary": _provider_summary(runs)},
                               self._forecast_point_rows)
        md = self.settings["markdown_response"]
        self.markdown = MarkdownResponse(tuple(md["m20"]), tuple(md["m30"]))
        self.policy = Policy(**self.settings["policy"])
        self.currency = self.settings["currency"]
        self.label = self.settings["mark"]["text"]
        # the case counter's start: a manifest that names none numbers its keys from SUS-1 (the template build reads it)
        self.calendar = {"case_no_start": 1, **self.settings["calendar"], "horizon_end": self.settings["horizon_end"]}
        self._points = _cached(self._load_points)
        self._runs = _cached(self._load_runs)
        self.run_on = _cached(self._run_on)
        self.free_totes = _cached(self._free_totes)
        self.sale = _cached(self._sale)
        self._carried = _cached(self._load_carried)

    @property
    def last_review_day(self) -> int:
        """The last morning that reopens a waiting case: the dataset calendar's last_demo_day (G-05-13)."""
        return int(self.calendar["last_demo_day"])

    def date(self, day: int) -> date:
        """The calendar date of day index `day`: the set's shifted calendar, day0 plus `day` (D-13: the sales dates
        +840 days, weekdays kept)."""
        return date.fromisoformat(self.calendar["day0"]) + timedelta(days=day)

    @property
    def data_as_of(self) -> str | None:
        """The scenario date the mark shows (ISO), from the set's calendar (D-04, D-13); the UI formats it."""
        return self.calendar.get("data_as_of")

    # the shared methods (refdata.Ref answers the same)

    def _load_points(self, day: int, store: str, sku: str) -> tuple[dict, dict]:
        run_id = self._run_of_day.get(day)
        if run_id is None:
            return {}, {}
        rows = _read("SELECT day, low, base, high, provider FROM ref_forecast_point WHERE dataset_version = :v "
                     "AND forecast_run_id = :f AND store = :s AND sku = :k ORDER BY day",
                     v=self.dataset_version, f=run_id, s=store, k=sku)
        return ({r["day"]: (r["low"], r["base"], r["high"]) for r in rows}, {r["day"]: r["provider"] for r in rows})

    def _forecast_point_rows(self, day: int, store: str, sku: str) -> dict[int, tuple[float, float, float]]:
        """The forecast made on demo day `day` for one series: {day: (low, base, high)}; {} when there is none."""
        return dict(self._points(day, store, sku)[0])

    def provider_of(self, day: int, store: str, sku: str, point_day: int) -> str | None:
        """The provider of one forecast point (each point names its own)."""
        return self._points(day, store, sku)[1].get(point_day)

    def _load_runs(self, origin: str, day: int) -> tuple[Run, ...]:
        return tuple(_run(r) for r in _read(f"SELECT {RUN_COLUMNS} FROM ref_run WHERE dataset_version = :v "
                                            f"AND origin = :o AND day = :d ORDER BY run_code",
                                            v=self.dataset_version, o=origin, d=day))

    def runs_from(self, origin: str, day: int) -> list[Run]:
        """The runs leaving `origin` on `day`, in run-code order (a new list: the cached tuple is never handed out)."""
        return list(self._runs(origin, day))

    def store_known(self, store: str) -> bool:
        """Whether the assumed location and route mapping covers the store (ref_store.geo_known; D-30, D-31). A store
        it lacks gets no transfer and keeps every other function."""
        return self.stores.get(store, {}).get("geo_known") is True

    def _run_on(self, run_code: str, day: int) -> Run | None:
        rows = _read(f"SELECT {RUN_COLUMNS} FROM ref_run WHERE dataset_version = :v AND run_code = :c AND day = :d",
                     v=self.dataset_version, c=run_code, d=day)
        return _run(rows[0]) if rows else None

    def _free_totes(self, store: str, zone: str, day: int) -> int | None:
        rows = _read("SELECT free_totes FROM ref_capacity WHERE dataset_version = :v AND store = :s AND zone = :z "
                     "AND day = :d", v=self.dataset_version, s=store, z=zone, d=day)
        return rows[0]["free_totes"] if rows else None

    def _load_carried(self, store: str) -> frozenset[str]:
        return frozenset(r["sku"] for r in _read("SELECT DISTINCT sku FROM ref_sales_day WHERE dataset_version = :v "
                                                 "AND store = :s", v=self.dataset_version, s=store))

    def carries(self, store: str, sku: str) -> bool:
        """Whether `store` carries `sku`: the one definition of "carries the product" (05.2 D-34's product-aware
        destination rule; 08-CONTEXT D-26). The pair is one of the set's series, exactly as `install` builds
        `install_series` from the sales (ref_sales_day). The install checks that every forecast run and the hidden
        demand cover exactly those series, so a carried series with a missing forecast day is a data gap that opens as
        missing data (D-28), never "not carried". `overlay.run_series` is a workspace's ownership of a series, not
        this. The cost: one primary-key range read per store per PublicRef, cached."""
        return sku in self._carried(store)

    def _sale(self, store: str, sku: str, day: int) -> float | None:
        """Recorded units sold on a history day (public sales x the units factor), None when the set has no row. A demo
        day (0 on) is never answered: the set's eval-week sales are the held-out week (D-15), and a demo day's sales
        come from the worlds' outcomes, as for the synthetic set."""
        if day >= 0:
            return None
        rows = _read("SELECT units FROM ref_sales_day WHERE dataset_version = :v AND store = :s AND sku = :k "
                     "AND day = :d", v=self.dataset_version, s=store, k=sku, d=day)
        return rows[0]["units"] if rows else None

    # one product across the set's stores, for a morning screen's per-call memo (api/inputs.py ScreenReads); never
    # lru-cached: a whole product's forecast is large, and only that memo holds it, until the morning ends

    def forecast_of_product(self, day: int, sku: str) -> dict[str, dict[int, tuple[float, float, float]]]:
        """The forecast made on demo day `day` for one product at every store: {store: {day: (low, base, high)}}, only
        stores with points; {} when no run was made that day. Each store's dict equals `forecast(day, store, sku)`.
        One query, bound to the set's stores and the product (the primary key), in the caller's transaction."""
        run_id = self._run_of_day.get(day)
        if run_id is None:
            return {}
        out: dict[str, dict[int, tuple[float, float, float]]] = {}
        for r in _read("SELECT store, day, low, base, high FROM ref_forecast_point WHERE dataset_version = :v "
                       "AND forecast_run_id = :f AND store = ANY(:s) AND sku = :k ORDER BY store, day",
                       v=self.dataset_version, f=run_id, s=self.branches, k=sku):
            out.setdefault(r["store"], {})[r["day"]] = (r["low"], r["base"], r["high"])
        return out

    def sales_of_product(self, sku: str, day: int) -> dict[str, float]:
        """Recorded units sold of one product on a history day at every store: {store: units}, only stores with a row;
        {} on a demo day (0 on), as `sale` answers None there. `.get(store)` equals `sale(store, sku, day)`."""
        if day >= 0:
            return {}
        return {r["store"]: r["units"] for r in _read(
            "SELECT store, units FROM ref_sales_day WHERE dataset_version = :v AND store = ANY(:s) AND sku = :k "
            "AND day = :d ORDER BY store", v=self.dataset_version, s=self.branches, k=sku, d=day)}

    @property
    def series(self) -> dict:
        """The one-series history route's limits from the set's settings: `max_days` (the longest window) and
        `statement_timeout_ms` (05.2 D-11)."""
        return dict(self.settings["series"])

    def history(self, store: str, sku: str, from_day: int, to_day: int) -> list[dict]:
        """One series' public daily rows in [from_day, to_day], in day order: {day, units, stockout, discount,
        unknown_price, holiday, activity, weather} with weather {precip, temp, humidity, wind} joined by store and
        day (null when the set has no row). One query on ref_sales_day's primary key, run under the set's
        `series.statement_timeout_ms` (SET LOCAL through set_config, put back after the query), so a slow scan fails
        as a plain error instead of holding a connection. Hidden demand and physical stock are never read here."""
        sql = ("SELECT s.day, s.units, s.stockout, s.discount, s.unknown_price, s.holiday, s.activity, w.precip, "
               "w.temp, w.humidity, w.wind, w.store AS weather_store FROM ref_sales_day s LEFT JOIN ref_weather_day w "
               "ON w.dataset_version = s.dataset_version AND w.store = s.store AND w.day = s.day "
               "WHERE s.dataset_version = :v AND s.store = :s AND s.sku = :k AND s.day BETWEEN :a AND :b ORDER BY s.day")
        params = {"v": self.dataset_version, "s": store, "k": sku, "a": from_day, "b": to_day}
        limit = str(int(self.series["statement_timeout_ms"]))

        def read(conn) -> list[dict]:
            before = db.one(conn, "SELECT current_setting('statement_timeout') AS t")["t"]
            db.one(conn, "SELECT set_config('statement_timeout', :t, true) AS x", t=limit)
            rows = db.rows(conn, sql, **params)
            db.one(conn, "SELECT set_config('statement_timeout', :t, true) AS x", t=before)
            return rows
        conn = db.current()
        if conn is not None:
            rows = read(conn)
        else:
            with db.tx() as own:
                rows = read(own)
        return [{"day": r["day"], "units": r["units"], "stockout": r["stockout"], "discount": r["discount"],
                 "unknown_price": r["unknown_price"], "holiday": r["holiday"], "activity": r["activity"],
                 "weather": None if r["weather_store"] is None else
                 {k: r[k] for k in ("precip", "temp", "humidity", "wind")}} for r in rows]

    def lots0(self, store: str | None = None, sku: str | None = None) -> list[dict]:
        """The day-0 record lots of one series, in lot_id order. A public set is read one series at a time."""
        if store is None or sku is None:
            raise ValueError("a public set's day-0 lots are read one series (store and product) at a time")
        return _read(f"SELECT {LOT_COLUMNS} FROM ref_lot0 WHERE dataset_version = :v AND store = :s AND sku = :k "
                     f"ORDER BY lot_id", v=self.dataset_version, s=store, k=sku)

    def inbound0(self, store: str | None = None, sku: str | None = None) -> list[dict]:
        """The scheduled deliveries of one series, in inbound_id order."""
        if store is None or sku is None:
            raise ValueError("a public set's deliveries are read one series (store and product) at a time")
        return _read(f"SELECT {INBOUND_COLUMNS} FROM ref_inbound0 WHERE dataset_version = :v AND store = :s "
                     f"AND sku = :k ORDER BY inbound_id", v=self.dataset_version, s=store, k=sku)


    def copy_worlds(self, conn, run_id: str, worlds) -> None:
        """A template's day-0 worlds from the shared tables, inside the database (INSERT ... SELECT, one statement per
        table and world; spec §8 template step 2): the record lots with their physical stock from truth (the record
        when truth does not list the lot) and the scheduled deliveries. The parts carry no verified_by and the
        deliveries no tag, so those stay null (unknown, never invented)."""
        for world in worlds:
            p = {"r": run_id, "w": world, "v": self.dataset_version}
            db.run(conn, "INSERT INTO lots (run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, "
                         "markdown_pct, expiry_source, last_verified_day, verified_by, tag, origin, moved_from) "
                         "SELECT :r, :w, lot_id, store, sku, qty, expiry_day, received_seq, markdown_pct, expiry_source, "
                         "last_verified_day, NULL, tag, origin, NULL FROM ref_lot0 WHERE dataset_version = :v", **p)
            db.run(conn, "INSERT INTO truth_lots (run_id, world, lot_id, qty) SELECT :r, :w, l.lot_id, "
                         "coalesce(t.qty, l.qty) FROM ref_lot0 l LEFT JOIN ref_truth_physical t ON t.dataset_version = "
                         "l.dataset_version AND t.lot_id = l.lot_id WHERE l.dataset_version = :v", **p)
            db.run(conn, "INSERT INTO inbound (run_id, world, inbound_id, branch, sku, qty, arrival_day, arrival_min, "
                         "expiry_day, received_seq, expiry_source, last_verified_day, tag, origin) SELECT :r, :w, "
                         "inbound_id, store, sku, qty, arrival_day, arrival_min, expiry_day, received_seq, "
                         "expiry_source, last_verified_day, NULL, origin FROM ref_inbound0 WHERE dataset_version = :v",
                   **p)


class PublicTruth:
    """A public set's hidden truth, read on demand: demand per (product, day), physical stock per lot (spec §5)."""

    kind = KIND

    def __init__(self, dataset_version: str, manifest: dict):
        self.dataset_version = dataset_version
        self.price_response = dict(manifest["settings"]["truth"]["price_response"])  # D-36: an unmeasured assumption
        # (day, {sku: {store: demand}}): the products demand_of_products read for one day, so the three worlds of one
        # advance share one read per product batch (quick 261018-4nj); one day only, replaced by the next day's read
        self._demand: tuple[int | None, dict[str, dict[str, float]]] = (None, {})

    def demand_for(self, sku: str, day: int) -> dict[str, float]:
        """{store: hidden demand} of one product on one day (a new dict; from demand_of_products' read when it
        covered the product that day, else one query)."""
        held_day, held = self._demand
        if held_day == day and sku in held:
            return dict(held[sku])
        return {r["store"]: r["demand"] for r in _read(
            "SELECT store, demand FROM ref_truth_demand WHERE dataset_version = :v AND sku = :k AND day = :d "
            "ORDER BY store", v=self.dataset_version, k=sku, d=day)}

    def demand_of_products(self, skus, day: int) -> dict[str, dict[str, float]]:
        """{sku: demand_for(sku, day)} for one product batch (quick 261018-4nj): one query, bound to the batch's
        products and the day, for the products this day's memo does not hold yet; each sku's dict equals demand_for's,
        in store order, {} for a product with no series. The memo keeps one day (at most the set's series), so a
        later demand_for of the same product and day reads nothing."""
        skus = list(skus)
        held_day, held = self._demand
        if held_day != day:
            held = {}
            self._demand = (day, held)
        missing = [s for s in skus if s not in held]
        if missing:
            got: dict[str, dict[str, float]] = {s: {} for s in missing}
            for r in _read("SELECT sku, store, demand FROM ref_truth_demand WHERE dataset_version = :v "
                           "AND sku = ANY(:k) AND day = :d ORDER BY sku, store",
                           v=self.dataset_version, k=missing, d=day):
                got[r["sku"]][r["store"]] = r["demand"]
            held.update(got)
        return {s: dict(held[s]) for s in skus}

    def units0(self, sku: str, day: int) -> tuple[float, float]:
        """(the day-0 lots' physical units, the physical units of deliveries arriving by `day`) of one product, summed
        in SQL (the template's units check, spec §6); physical is the record where truth lists no figure."""
        p = {"v": self.dataset_version, "k": sku, "d": day}
        initial = _read("SELECT coalesce(sum(coalesce(t.qty, l.qty)), 0) AS q FROM ref_lot0 l LEFT JOIN "
                        "ref_truth_physical t ON t.dataset_version = l.dataset_version AND t.lot_id = l.lot_id "
                        "WHERE l.dataset_version = :v AND l.sku = :k", **p)[0]["q"]
        inbound = _read("SELECT coalesce(sum(coalesce(t.qty, i.qty)), 0) AS q FROM ref_inbound0 i LEFT JOIN "
                        "ref_truth_physical t ON t.dataset_version = i.dataset_version AND t.lot_id = i.inbound_id "
                        "WHERE i.dataset_version = :v AND i.sku = :k AND i.arrival_day <= :d", **p)[0]["q"]
        return float(initial), float(inbound)

    def physical_of(self, lot_ids) -> dict[str, float]:
        """{lot_id: physical qty} for the ids truth lists; an id it does not list is left out."""
        ids = list(lot_ids)
        if not ids:
            return {}
        return {r["lot_id"]: r["qty"] for r in _read(
            "SELECT lot_id, qty FROM ref_truth_physical WHERE dataset_version = :v AND lot_id = ANY(:ids)",
            v=self.dataset_version, ids=ids)}
