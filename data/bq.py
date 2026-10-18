"""BigQuery adapter for the forecast runs (spec §5 Forecast; DATA-03; 03-CONTEXT D-07, D-08).

    GOOGLE_CLOUD_PROJECT=<project> python -m data.forecast --provider ai_forecast --preflight   # no network
    GOOGLE_CLOUD_PROJECT=<project> python -m data.forecast --provider ai_forecast --dry-run     # load + dry run
    GOOGLE_CLOUD_PROJECT=<project> python -m data.forecast --provider ai_forecast               # the real run

Run only on the user's machine, with the user's credentials (Application Default Credentials). Tests never import
google: `google.cloud.bigquery` is imported inside the client functions only, and tests inject a fake client. The
running app never queries BigQuery; it reads the precomputed forecast.json.

The pure part checks identifiers, composes the SQL from data/forecast.sql, builds the input rows and parses result
rows. Every project, dataset, table and model identifier passes a regex before it is formatted into SQL; otherwise
only an int horizon, a float confidence level and a TimesFM model name matching MODEL_NAME_RE are formatted. `parse`
returns the rows as BigQuery sent them (no clamp, no sort, no rounding), so data.forecast's checks see the raw
values (D-07). The project comes only from the caller (`--project` or GOOGLE_CLOUD_PROJECT); this module holds none.
"""
from __future__ import annotations

import importlib.util
import math
import pathlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import forecast as F
from . import world as W

SQL = (pathlib.Path(__file__).resolve().parent / "forecast.sql").read_text(encoding="utf-8")

PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
MODEL_NAME_RE = re.compile(r"^TimesFM [0-9.]+$")
LOCATION_RE = re.compile(r"^[a-z]+(-[a-z0-9]+)*$")  # passed to the client only, never formatted into SQL

PACKAGE = "google-cloud-bigquery"
SECTIONS = ("ai_forecast", "arima_plus_model", "arima_plus")
TABLE_KEYS = ("table", "model", "fallback_table")  # full `project.dataset.name` identifiers
FILL_KEYS = frozenset(TABLE_KEYS + ("horizon", "confidence_level", "model_name"))

INPUT_TABLE = "forecast_input"
FALLBACK_TABLE = "forecast_input_fallback"
FALLBACK_MODEL = "arima_plus_fallback"

SCHEMA = (("series_id", "STRING"), ("ts", "TIMESTAMP"), ("value", "FLOAT64"), ("how", "STRING"))


class BigQueryConfigError(ValueError):
    """An identifier or SQL fill value that may not be formatted into SQL, or a malformed configuration."""


# --- pure part -----------------------------------------------------------------------------------------------------

def table_id(project: str, dataset: str, name: str) -> str:
    """`project.dataset.name` after each part passes its regex."""
    if not isinstance(project, str) or not PROJECT_RE.match(project):
        raise BigQueryConfigError(f"project {project!r} is not a valid Google Cloud project id")
    for kind, value in (("dataset", dataset), ("table or model", name)):
        if not isinstance(value, str) or not NAME_RE.match(value):
            raise BigQueryConfigError(f"{kind} name {value!r} may hold only letters, digits and underscores")
    return f"{project}.{dataset}.{name}"


def _checked_table(key: str, value) -> str:
    parts = value.split(".") if isinstance(value, str) else []
    if len(parts) != 3:
        raise BigQueryConfigError(f"{key} {value!r} is not a project.dataset.name identifier")
    return table_id(*parts)


def _checked_level(level) -> float:
    if not isinstance(level, float) or not math.isfinite(level) or not 0.0 < level < 1.0:
        raise BigQueryConfigError(f"confidence_level {level!r} must be a float between 0 and 1")
    return level


def _checked_model_name(name) -> str:
    if not isinstance(name, str) or not MODEL_NAME_RE.match(name):
        raise BigQueryConfigError(f"model name {name!r} does not match {MODEL_NAME_RE.pattern}")
    return name


def sql(name: str, **fill) -> str:
    """The `-- name: <name>` section of forecast.sql with `fill` formatted in, after every value is checked."""
    if name not in SECTIONS:
        raise BigQueryConfigError(f"no SQL section {name!r}; known: {', '.join(SECTIONS)}")
    unknown = sorted(set(fill) - FILL_KEYS)
    if unknown:
        raise BigQueryConfigError(f"unknown SQL fill keys {unknown}")
    for key in TABLE_KEYS:
        if key in fill:
            _checked_table(key, fill[key])
    if "horizon" in fill:
        h = fill["horizon"]
        if not isinstance(h, int) or isinstance(h, bool) or h <= 0:
            raise BigQueryConfigError(f"horizon {h!r} must be a positive int")
    if "confidence_level" in fill:
        _checked_level(fill["confidence_level"])
    if "model_name" in fill:
        _checked_model_name(fill["model_name"])
    part = SQL.split(f"-- name: {name}\n", 1)[1].split("\n-- name:", 1)[0]
    try:
        return part.format(**fill).strip() + "\n"
    except KeyError as e:
        raise BigQueryConfigError(f"SQL section {name!r} needs {e.args[0]!r}") from None


def _ts(day: int) -> str:
    """Midnight UTC of the day's calendar date, as ISO 8601."""
    d = W.demo_date(day)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def input_rows(inputs: dict) -> list[dict]:
    """The load rows for `inputs` ({series_id: [(day, value, tag), ...]}), in their order."""
    return [{"series_id": sid, "ts": _ts(day), "value": float(value), "how": tag}
            for sid, series in inputs.items() for day, value, tag in series]


def _day(ts) -> int:
    if isinstance(ts, datetime):
        ts = (ts.astimezone(timezone.utc) if ts.tzinfo else ts).date()
    if not isinstance(ts, date):
        raise ValueError(f"forecast_timestamp {ts!r} is not a date or timestamp")
    return (ts - W.DAY0).days


def parse(rows, provider: str) -> dict[str, list[dict]]:
    """{series_id: [{day, low, base, high, confidence_level, status}, ...]} from BigQuery result rows, raw: values
    are not clamped, sorted, rounded or reordered; a missing ai_forecast_status (ML.FORECAST has none) is ""."""
    if provider not in (F.AI_FORECAST, F.ARIMA_PLUS):
        raise BigQueryConfigError(f"unknown BigQuery provider {provider!r}")
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["series_id"], []).append({
            "day": _day(r["forecast_timestamp"]),
            "low": r["prediction_interval_lower_bound"],
            "base": r["forecast_value"],
            "high": r["prediction_interval_upper_bound"],
            "confidence_level": r.get("confidence_level"),
            "status": r.get("ai_forecast_status") or "",
        })
    return out


def horizon(required: dict, inputs: dict) -> int:
    """The steps to forecast after each series' last input day (its run's cutoff), the most any series needs."""
    return max(max(days) - inputs[sid][-1][0] for sid, days in required.items())


@dataclass(frozen=True)
class Ids:
    """The project and dataset the forecast tables live in; both are checked on construction."""
    project: str
    dataset: str

    def __post_init__(self):
        table_id(self.project, self.dataset, INPUT_TABLE)

    @property
    def dataset_id(self) -> str:
        return f"{self.project}.{self.dataset}"

    @property
    def input_table(self) -> str:
        return table_id(self.project, self.dataset, INPUT_TABLE)

    @property
    def fallback_table(self) -> str:
        return table_id(self.project, self.dataset, FALLBACK_TABLE)

    @property
    def model(self) -> str:
        return table_id(self.project, self.dataset, FALLBACK_MODEL)


def _location(location) -> str:
    if not isinstance(location, str) or not LOCATION_RE.match(location):
        raise BigQueryConfigError(f"location {location!r} is not a BigQuery location name")
    return location


# --- client part: google.cloud.bigquery is imported only inside these functions -----------------------------------

def make_client(project: str, location: str):
    from google.cloud import bigquery  # the user's machine only; tests inject a fake client
    return bigquery.Client(project=project, location=location)


def _load_config():
    from google.cloud import bigquery
    return bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE",
                                  schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA])


def _dry_run_config():
    from google.cloud import bigquery
    return bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)


def load_rows(client, table: str, rows: list[dict]) -> None:
    """Replace `table` with `rows` under the explicit SCHEMA (no autodetect)."""
    client.load_table_from_json(rows, _checked_table("table", table), job_config=_load_config()).result()


def _ai_forecast_sql(ids: Ids, inputs: dict, required: dict, level: float, model_name: str) -> str:
    return sql("ai_forecast", table=ids.input_table, model_name=model_name,
               horizon=horizon(required, inputs), confidence_level=level)


def fetch_ai_forecast(client, ids: Ids, inputs: dict, required: dict, level: float, model_name: str) -> dict:
    """Load every series into `forecast_input` and run AI.FORECAST over all of them in one query."""
    client.create_dataset(ids.dataset_id, exists_ok=True)
    load_rows(client, ids.input_table, input_rows(inputs))
    return parse(client.query(_ai_forecast_sql(ids, inputs, required, level, model_name)).result(), F.AI_FORECAST)


def fetch_arima_plus(client, ids: Ids, inputs: dict, required: dict, level: float) -> dict:
    """Load only the given series into `forecast_input_fallback`, train ARIMA_PLUS on them, and forecast with the
    same horizon as the model (ML.FORECAST may not exceed it)."""
    h = horizon(required, inputs)
    client.create_dataset(ids.dataset_id, exists_ok=True)
    load_rows(client, ids.fallback_table, input_rows(inputs))
    client.query(sql("arima_plus_model", model=ids.model, fallback_table=ids.fallback_table, horizon=h)).result()
    rows = client.query(sql("arima_plus", model=ids.model, horizon=h, confidence_level=level)).result()
    return parse(rows, F.ARIMA_PLUS)


def dry_run(client, ids: Ids, inputs: dict, required: dict, level: float, model_name: str) -> int:
    """Load the input table (load jobs are free), then dry-run the AI.FORECAST query; the bytes it would process."""
    client.create_dataset(ids.dataset_id, exists_ok=True)
    load_rows(client, ids.input_table, input_rows(inputs))
    job = client.query(_ai_forecast_sql(ids, inputs, required, level, model_name), job_config=_dry_run_config())
    return job.total_bytes_processed


def providers(project: str, location: str, dataset: str, level: float, model_name: str,
              client_factory=None) -> tuple[F.Provider, F.Provider]:
    """(AI.FORECAST, ARIMA_PLUS) providers for data.forecast.build. Every identifier and fill value is checked
    here, before any client exists; the client is built once, lazily, on the first fetch."""
    ids = Ids(project, dataset)
    _location(location)
    _checked_level(level)
    _checked_model_name(model_name)
    cache = {}

    def client():
        if "client" not in cache:
            cache["client"] = (client_factory or make_client)(project, location)
        return cache["client"]

    def ai(inputs, required):
        return fetch_ai_forecast(client(), ids, inputs, required, level, model_name)

    def arima(inputs, required):
        return fetch_arima_plus(client(), ids, inputs, required, level)

    return F.Provider(F.AI_FORECAST, ai), F.Provider(F.ARIMA_PLUS, arima)


def _package_found() -> bool:
    try:
        return importlib.util.find_spec("google.cloud.bigquery") is not None
    except (ImportError, ValueError):  # no google namespace at all, or google blocked
        return False


def preflight(seed: dict, project, provider: str = F.AI_FORECAST) -> dict:
    """Everything the run needs that can be checked without a network call: the package, the project id, the
    seed's forecast config, the series and rows to load, the horizon and the composed SQL."""
    fc = seed["forecast"]
    ins, req = F.inputs(seed), F.required(seed)
    out = {
        "package": PACKAGE, "package_found": _package_found(),
        "project": project, "project_ok": isinstance(project, str) and bool(PROJECT_RE.match(project)),
        "provider": provider, "location": fc["location"], "dataset": fc["dataset"], "model": fc["model"],
        "confidence_level": fc["confidence_level"], "series": len(req), "input_rows": len(input_rows(ins)),
        "horizon": horizon(req, ins), "problems": [], "sql": None,
    }
    try:
        _location(fc["location"])
        _checked_model_name(fc["model"])
        _checked_level(fc["confidence_level"])
        if out["project_ok"]:
            ids = Ids(project, fc["dataset"])
            if provider == F.AI_FORECAST:
                out["sql"] = _ai_forecast_sql(ids, ins, req, fc["confidence_level"], fc["model"])
            else:
                out["sql"] = (sql("arima_plus_model", model=ids.model, fallback_table=ids.fallback_table,
                                  horizon=out["horizon"])
                              + sql("arima_plus", model=ids.model, horizon=out["horizon"],
                                    confidence_level=fc["confidence_level"]))
    except BigQueryConfigError as e:
        out["problems"].append(str(e))
    if not out["package_found"]:
        out["problems"].append(f"{PACKAGE} is not installed in this environment")
    if not out["project_ok"]:
        out["problems"].append(f"project {project!r} is not a valid Google Cloud project id")
    if fc["confidence_level"] != F.LEVEL:
        out["problems"].append(f"confidence_level {fc['confidence_level']!r} is not the spec's {F.LEVEL!r}")
    return out


# --- the public set: file load, destination table, ordered paged read (05.2-09; DATA-13; D-22) ---------------------
#
# At 50,000 series x 5 runs the input is about 22M rows and the output about 3.5M, so nothing goes through
# load_table_from_json or a list of result dicts: the input part (data/public_forecast.py, forecast_input.csv.gz) is
# loaded as a file, AI.FORECAST writes into a destination table, and the result is read back page by page ordered by
# series, one series at a time. The public set has its own dataset (reference forecast.dataset), so the synthetic
# run's tables stay as they are. Identifiers pass the same regexes as above; only an int horizon, the configured
# level and the configured TimesFM model name are formatted into SQL. Days come from the public calendar's day0
# passed in, never data.world.DAY0.

PUBLIC_INPUT, PUBLIC_OUTPUT = "forecast_input", "forecast_output"
PUBLIC_FALLBACK_INPUT, PUBLIC_FALLBACK_OUTPUT = "forecast_input_fallback", "forecast_output_fallback"
PUBLIC_MODEL = "arima_plus_fallback"
PAGE_SIZE = 100_000  # rows per result page: memory, not a retailer value
READ_COLUMNS = ("series_id", "forecast_timestamp", "forecast_value", "prediction_interval_lower_bound",
                "prediction_interval_upper_bound", "confidence_level")
GIB = 1024 ** 3  # only to print a byte count readably


# The budget cap (reference budget.max_bytes_billed, user 2026-10-17): every query job below carries it as
# maximum_bytes_billed, so BigQuery itself refuses a query over it without charging, and every paid query is first
# dry-run (free) and refused here when its estimate is over the cap. The file loads are not queries.

class BytesCapExceeded(RuntimeError):
    """A dry run estimated more bytes than the per-query cap: nothing was sent as a paid query. The message names the
    query and the cap, never a project or table."""

    def __init__(self, query: str, estimate: int, cap: int):
        self.query, self.estimate, self.cap = query, estimate, cap
        super().__init__(f"{query}: the dry run estimates {estimate} bytes ({gib(estimate)}), over the cap of {cap} "
                         f"bytes ({gib(cap)}) per query (reference/public.json budget.max_bytes_billed); this query "
                         "was not run")


def gib(n: int) -> str:
    return f"{n / GIB:.2f} GiB"


def checked_cap(cap) -> int:
    """The per-query byte cap: a positive int (bool refused)."""
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        raise BigQueryConfigError(f"max_bytes_billed {cap!r} must be a positive whole number of bytes")
    return cap


def _query_config(cap: int, **kw):
    return _bigquery().QueryJobConfig(maximum_bytes_billed=checked_cap(cap), **kw)


def _dry(client, text: str, cap: int) -> int:
    """The bytes a query would process, from a free dry run (the cap rides along like on every query job)."""
    job = client.query(text.strip().rstrip(";"), job_config=_query_config(cap, dry_run=True, use_query_cache=False))
    return job.total_bytes_processed


def _within_cap(query: str, estimate, cap: int) -> None:
    if estimate is not None and estimate > cap:
        raise BytesCapExceeded(query, estimate, cap)


def _paid(client, query: str, text: str, cap: int, ledger: list | None = None, **kw):
    """Dry-run `text` and refuse it over the cap (BytesCapExceeded, nothing sent), then run it with
    maximum_bytes_billed = cap. `ledger`, when given, gets {query, estimate_bytes} for every query actually sent, so a
    later refusal can report what was already spent."""
    estimate = _dry(client, text, cap)
    _within_cap(query, estimate, cap)
    if ledger is not None:
        ledger.append({"query": query, "estimate_bytes": estimate})
    return client.query(text.strip().rstrip(";"), job_config=_query_config(cap, **kw))


def _billed(job, ledger: list | None) -> None:
    """Record the bytes BigQuery billed for the job just finished (None when the client does not say)."""
    if ledger:
        ledger[-1]["billed_bytes"] = getattr(job, "total_bytes_billed", None)


@dataclass(frozen=True)
class PublicIds:
    """The public set's tables in its own dataset; project and dataset are checked on construction."""
    project: str
    dataset: str

    def __post_init__(self):
        table_id(self.project, self.dataset, PUBLIC_INPUT)

    @property
    def dataset_id(self) -> str:
        return f"{self.project}.{self.dataset}"

    def _t(self, name: str) -> str:
        return table_id(self.project, self.dataset, name)

    @property
    def input_table(self) -> str:
        return self._t(PUBLIC_INPUT)

    @property
    def output_table(self) -> str:
        return self._t(PUBLIC_OUTPUT)

    @property
    def fallback_input(self) -> str:
        return self._t(PUBLIC_FALLBACK_INPUT)

    @property
    def fallback_output(self) -> str:
        return self._t(PUBLIC_FALLBACK_OUTPUT)

    @property
    def model(self) -> str:
        return self._t(PUBLIC_MODEL)


def _bigquery():
    from google.cloud import bigquery  # the user's machine only; tests replace this function with a fake module
    return bigquery


def write_input_file(src, dest, keep) -> int:
    """Copy the rows of the input part `src` whose series_id is in `keep` (a set, or a predicate) into a new gz CSV
    part `dest` with the same header; the rows copied. Pure: no client."""
    import csv
    import gzip
    import io

    test = keep if callable(keep) else keep.__contains__
    n = 0
    with gzip.open(src, "rt", encoding="utf-8", newline="") as fh, open(dest, "wb") as raw:
        with io.TextIOWrapper(gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0), encoding="utf-8",
                              newline="") as out:
            reader, writer = csv.reader(fh), csv.writer(out, lineterminator="\n")
            header = next(reader, None)
            if header != [name for name, _ in SCHEMA]:
                raise BigQueryConfigError(f"{pathlib.Path(src).name}: header {header} is not {[n for n, _ in SCHEMA]}")
            writer.writerow(header)
            for row in reader:
                if test(row[0]):
                    writer.writerow(row)
                    n += 1
    return n


def load_file(client, table: str, path) -> None:
    """Replace `table` with the gz CSV input part at `path`: explicit SCHEMA (no autodetect), one header row
    skipped, WRITE_TRUNCATE. Load jobs are not billed as query bytes."""
    table = _checked_table("table", table)
    b = _bigquery()
    job_config = b.LoadJobConfig(source_format="CSV", skip_leading_rows=1, write_disposition="WRITE_TRUNCATE",
                                 schema=[b.SchemaField(n, t) for n, t in SCHEMA])
    with open(path, "rb") as fh:
        client.load_table_from_file(fh, table, job_config=job_config).result()


def run_to_table(client, sql_text: str, destination: str, append: bool = False, *, max_bytes_billed: int,
                 query: str = "query", ledger: list | None = None) -> None:
    """Run one query into `destination` (WRITE_TRUNCATE, or WRITE_APPEND when `append`), and wait for it: dry-run
    first and refused over the cap (BytesCapExceeded), then run with maximum_bytes_billed."""
    destination = _checked_table("destination", destination)
    job = _paid(client, query, sql_text, max_bytes_billed, ledger, destination=destination,
                write_disposition="WRITE_APPEND" if append else "WRITE_TRUNCATE")
    job.result()
    _billed(job, ledger)


def read_sql(table: str, provider: str) -> str:
    """The ordered read of a result table: every column the checks need, ordered by series then timestamp."""
    if provider not in (F.AI_FORECAST, F.ARIMA_PLUS):
        raise BigQueryConfigError(f"unknown BigQuery provider {provider!r}")
    columns = READ_COLUMNS + (("ai_forecast_status",) if provider == F.AI_FORECAST else ())
    return (f"SELECT {', '.join(columns)}\nFROM `{_checked_table('table', table)}`\n"
            "ORDER BY series_id, forecast_timestamp\n")


def _public_day(ts, day0: date) -> int:
    if isinstance(ts, datetime):
        ts = (ts.astimezone(timezone.utc) if ts.tzinfo else ts).date()
    if not isinstance(ts, date):
        raise ValueError(f"forecast_timestamp {ts!r} is not a date or timestamp")
    return (ts - day0).days


def read_ordered(client, table: str, day0: date, provider: str, page_size: int = PAGE_SIZE, *,
                 max_bytes_billed: int, ledger: list | None = None):
    """Yield (series_id, rows) one series at a time from `table`, read in pages ordered by series; rows are raw, as
    `parse` gives them, with days on the public calendar (day0 passed in). Never holds more than one series and one
    page. The read is a query too: dry-run and capped like the others."""
    rows = _paid(client, f"read_ordered ({provider})", read_sql(table, provider), max_bytes_billed,
                 ledger).result(page_size=page_size)
    sid, series, done = None, [], set()
    for r in rows:
        if r["series_id"] != sid:
            if series:
                yield sid, series
                done.add(sid)
            sid, series = r["series_id"], []
            if sid in done:
                raise ValueError(f"{table}: series {sid!r} came back after another series; the read is not ordered")
        series.append({
            "day": _public_day(r["forecast_timestamp"], day0),
            "low": r["prediction_interval_lower_bound"],
            "base": r["forecast_value"],
            "high": r["prediction_interval_upper_bound"],
            "confidence_level": r.get("confidence_level"),
            "status": r.get("ai_forecast_status") or "",
        })
    if series:
        yield sid, series


def _public_ai_sql(ids: PublicIds, horizon: int, level: float, model_name: str) -> str:
    return sql("ai_forecast", table=ids.input_table, model_name=model_name, horizon=horizon, confidence_level=level)


def fetch_public_ai(client, ids: PublicIds, paths, horizon: int, level: float, model_name: str, day0: date,
                    page_size: int = PAGE_SIZE, *, max_bytes_billed: int, ledger: list | None = None):
    """Load each input file and run AI.FORECAST into the output table (WRITE_TRUNCATE for the first file,
    WRITE_APPEND after: one file is the single query, several are the --partition city run), then read the output
    back ordered by series. Each query is dry-run and capped; the ARIMA_PLUS fallback is priced later, on the series
    that need it (fetch_public_arima)."""
    checked_cap(max_bytes_billed)
    client.create_dataset(ids.dataset_id, exists_ok=True)
    text = _public_ai_sql(ids, horizon, level, model_name)
    for i, path in enumerate(paths):
        load_file(client, ids.input_table, path)
        run_to_table(client, text, ids.output_table, append=i > 0, max_bytes_billed=max_bytes_billed,
                     query="ai_forecast", ledger=ledger)
    yield from read_ordered(client, ids.output_table, day0, F.AI_FORECAST, page_size,
                            max_bytes_billed=max_bytes_billed, ledger=ledger)


def fetch_public_arima(client, ids: PublicIds, path, horizon: int, level: float, day0: date,
                       page_size: int = PAGE_SIZE, *, max_bytes_billed: int, ledger: list | None = None):
    """Load only the given series (the fallback input file, built after AI.FORECAST's per-series checks), price the
    ARIMA_PLUS training on that real table with a dry run and train only within the cap (BytesCapExceeded
    'arima_plus_model' otherwise: nothing trained), forecast into the fallback output table with the same horizon,
    and read it back ordered by series; every query capped."""
    checked_cap(max_bytes_billed)
    client.create_dataset(ids.dataset_id, exists_ok=True)
    load_file(client, ids.fallback_input, path)
    job = _paid(client, "arima_plus_model",
                sql("arima_plus_model", model=ids.model, fallback_table=ids.fallback_input, horizon=horizon),
                max_bytes_billed, ledger)
    job.result()
    _billed(job, ledger)
    run_to_table(client, sql("arima_plus", model=ids.model, horizon=horizon, confidence_level=level),
                 ids.fallback_output, max_bytes_billed=max_bytes_billed, query="arima_plus", ledger=ledger)
    yield from read_ordered(client, ids.fallback_output, day0, F.ARIMA_PLUS, page_size,
                            max_bytes_billed=max_bytes_billed, ledger=ledger)


def dry_run_file(client, ids: PublicIds, path, horizon: int, level: float, model_name: str, *,
                 max_bytes_billed: int) -> list[dict]:
    """The bytes of every query the run would make, from free dry runs, before any paid query (D-22). The input file
    is loaded first (load jobs are not billed as query bytes). Only AI.FORECAST can be priced now: the ARIMA_PLUS
    training is priced by the paid run after AI.FORECAST, on the real table of the series that need the fallback
    (pricing it on every series overstated it: 7.59 GiB against a 5 GiB cap on 2026-10-17); ML.FORECAST and the
    ordered reads read a model or a table the run itself creates. Each priced entry says whether it is over the
    per-query cap (`over_cap`); the paid run refuses such a query before running it."""
    cap = checked_cap(max_bytes_billed)

    def dry(text: str) -> int:
        return _dry(client, text, cap)

    client.create_dataset(ids.dataset_id, exists_ok=True)
    load_file(client, ids.input_table, path)
    out = [{"query": "ai_forecast", "bytes": dry(_public_ai_sql(ids, horizon, level, model_name)),
            "note": "AI.FORECAST (TimesFM) over every series into the output table"}]
    out.append({"query": "arima_plus_model", "bytes": None,
                "note": "priced after AI.FORECAST, by a dry run on the series that need the fallback; trained only "
                        "within the cap, otherwise refused with nothing trained"})
    out.append({"query": "arima_plus", "bytes": None,
                "note": "ML.FORECAST reads only the fallback model the run creates; not dry-runnable before it exists"})
    out.append({"query": "read_ordered", "bytes": None,
                "note": "reads the output table the run creates (series-runs x horizon rows) and, after a fallback, "
                        "the fallback output table; not dry-runnable before they exist"})
    for entry in out:
        entry["over_cap"] = entry["bytes"] is not None and entry["bytes"] > cap
    return out


def public_fetchers(project: str, location: str, dataset: str, level: float, model_name: str, day0: date,
                    horizon: int, page_size: int = PAGE_SIZE, partition=None, client_factory=None, *,
                    max_bytes_billed: int, ledger: list | None = None):
    """(AI.FORECAST fetch, ARIMA_PLUS fetch) for data.public_forecast.stream_forecast: each takes (input part path,
    days by job) and yields (series_id, raw rows). `partition`, when given, is the list of input files to run one
    AI.FORECAST each (the --partition city run). Every identifier and fill value is checked here, before any client
    exists; the client is built once, lazily. `ledger` collects every query sent, with its estimate and billed bytes.
    """
    ids = PublicIds(project, dataset)
    _location(location)
    _checked_level(level)
    _checked_model_name(model_name)
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
        raise BigQueryConfigError(f"horizon {horizon!r} must be a positive int")
    cap = checked_cap(max_bytes_billed)
    cache = {}

    def client():
        if "client" not in cache:
            cache["client"] = (client_factory or make_client)(project, location)
        return cache["client"]

    def ai(path, days_by_job):
        yield from fetch_public_ai(client(), ids, list(partition) if partition else [path], horizon, level,
                                   model_name, day0, page_size, max_bytes_billed=cap, ledger=ledger)

    def arima(path, days_by_job):
        yield from fetch_public_arima(client(), ids, path, horizon, level, day0, page_size, max_bytes_billed=cap,
                                      ledger=ledger)

    return ai, arima


def preflight_public(cfg: dict, project, meta: dict | None, horizon: int | None, history_id: str,
                     provider: str = F.AI_FORECAST, partitions: int = 1) -> dict:
    """Everything the public run needs that can be checked without a network call: the package, the project id,
    the forecast config, the input part's series, series-runs and rows, the horizon, that the input part was built on
    this reference (its reference_history_id), and the composed SQL of every query."""
    fc = cfg["forecast"]
    out = {
        "package": PACKAGE, "package_found": _package_found(),
        "project": project, "project_ok": isinstance(project, str) and bool(PROJECT_RE.match(project)),
        "provider": provider, "location": fc["location"], "dataset": fc["dataset"], "model": fc["model"],
        "fallback": fc["fallback"], "confidence_level": fc["confidence_level"],
        "series": meta and meta["series"], "series_runs": meta and meta["series_runs"],
        "input_rows": meta and meta["rows"], "horizon": horizon, "partitions": partitions,
        "reference_history_id": meta and meta["reference_history_id"],
        "max_bytes_billed": cfg["budget"]["max_bytes_billed"], "problems": [], "sql": None,
    }
    if meta is None:
        out["problems"].append("no input part: run the forecast with --provider local or pass --raw/--csv first")
    elif meta["reference_history_id"] != history_id:
        out["problems"].append(f"the input part was built on other inputs ({meta['reference_history_id']}, this "
                               f"reference gives {history_id}); rebuild it")
    try:
        checked_cap(cfg["budget"]["max_bytes_billed"])
        _location(fc["location"])
        _checked_model_name(fc["model"])
        _checked_level(fc["confidence_level"])
        if out["project_ok"] and horizon:
            ids = PublicIds(project, fc["dataset"])
            if provider == F.AI_FORECAST:
                out["sql"] = (_public_ai_sql(ids, horizon, fc["confidence_level"], fc["model"])
                              + read_sql(ids.output_table, F.AI_FORECAST))
            else:
                out["sql"] = ""
            out["sql"] += (sql("arima_plus_model", model=ids.model, fallback_table=ids.fallback_input, horizon=horizon)
                           + sql("arima_plus", model=ids.model, horizon=horizon,
                                 confidence_level=fc["confidence_level"])
                           + read_sql(ids.fallback_output, F.ARIMA_PLUS))
    except BigQueryConfigError as e:
        out["problems"].append(str(e))
    if not out["package_found"]:
        out["problems"].append(f"{PACKAGE} is not installed in this environment")
    if not out["project_ok"]:
        out["problems"].append(f"project {project!r} is not a valid Google Cloud project id")
    if fc["confidence_level"] != F.LEVEL:
        out["problems"].append(f"confidence_level {fc['confidence_level']!r} is not the spec's {F.LEVEL!r}")
    return out
