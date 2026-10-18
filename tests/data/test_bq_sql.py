"""The BigQuery adapter up to the network boundary (DATA-03; 03-CONTEXT D-07, D-08, D-09; research Pitfalls 6, 8).

SQL is composed only from checked identifiers and the seed's forecast config; result rows are parsed raw; the CLI's
cloud providers run end to end against a fake client. Nothing here imports google: the module's job-config helpers
are replaced by sentinels, and the fake client answers queries with the local stand-in's numbers computed from the
rows it was loaded with.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from data import bq
from data import forecast as F
from data import versions
from data import world as W

ROOT = pathlib.Path(__file__).resolve().parents[2]
REF = ROOT / "data" / "reference"
PROJECT = "x-demo-12"  # a well-formed id that is no real project
TABLE = f"{PROJECT}.shelfshift.forecast_input"
MODEL = f"{PROJECT}.shelfshift.arima_plus_fallback"
FALLBACK = f"{PROJECT}.shelfshift.forecast_input_fallback"
LOAD_CFG, DRY_CFG = object(), object()
UTC = timezone.utc


@pytest.fixture(scope="module")
def seed():
    return json.loads((REF / "seed.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_google(monkeypatch):
    """The job-config helpers would import google; the fake client only checks it got these sentinels."""
    monkeypatch.setattr(bq, "_load_config", lambda: LOAD_CFG)
    monkeypatch.setattr(bq, "_dry_run_config", lambda: DRY_CFG)


def when(day):
    d = W.demo_date(day)
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


class Job:
    def __init__(self, rows=(), total_bytes_processed=None):
        self.rows, self.total_bytes_processed = list(rows), total_bytes_processed

    def result(self):
        return iter(self.rows)


class FakeClient:
    """Records every call. A query is answered from the table it reads: each loaded series gets `horizon` days after
    its last input day, valued by the local stand-in. Series in `spoil` get a crossed interval on their first
    required day from AI.FORECAST."""

    def __init__(self, spoil=()):
        self.spoil = set(spoil)
        self.tables, self.calls, self.datasets = {}, [], []

    def create_dataset(self, dataset, exists_ok=False):
        self.datasets.append((dataset, exists_ok))

    def load_table_from_json(self, rows, table, job_config=None):
        assert job_config is LOAD_CFG
        self.tables[table] = list(rows)
        self.calls.append(("load", table, sorted({r["series_id"] for r in rows})))
        return Job()

    def _answer(self, table, sql, ai):
        h = int((re.search(r"horizon => (\d+)", sql) or re.search(r"STRUCT\((\d+) AS horizon", sql)).group(1))
        ins = defaultdict(list)
        for r in self.tables[table]:
            ins[r["series_id"]].append(((datetime.fromisoformat(r["ts"]).date() - W.DAY0).days, r["value"], r["how"]))
        req = {sid: list(range(xs[-1][0] + 1, xs[-1][0] + h + 1)) for sid, xs in ins.items()}
        out = []
        for sid, rows in F.local_fetch(dict(ins), req).items():
            for i, r in enumerate(rows):
                low, high = (r["high"], r["low"]) if (ai and sid in self.spoil and i == 0) else (r["low"], r["high"])
                row = {"series_id": sid, "forecast_timestamp": when(r["day"]), "forecast_value": r["base"],
                       "prediction_interval_lower_bound": low, "prediction_interval_upper_bound": high,
                       "confidence_level": 0.8}
                if ai:
                    row["ai_forecast_status"] = ""
                out.append(row)
        return out

    def query(self, sql, job_config=None):
        self.calls.append(("query", sql, job_config))
        if job_config is DRY_CFG:
            return Job(total_bytes_processed=123456)
        assert job_config is None
        if "AI.FORECAST" in sql:
            return Job(self._answer(TABLE, sql, ai=True))
        if "CREATE OR REPLACE MODEL" in sql:
            return Job()
        if "ML.FORECAST" in sql:
            return Job(self._answer(FALLBACK, sql, ai=False))
        raise AssertionError(f"unexpected SQL {sql!r}")


def ai_sql(**over):
    fill = dict(table=TABLE, model_name="TimesFM 2.5", horizon=17, confidence_level=0.8) | over
    return bq.sql("ai_forecast", **fill)


# --- SQL ------------------------------------------------------------------------------------------------------------

def test_ai_forecast_sql_pins_model_level_horizon_and_selects_status():
    text = ai_sql()
    for part in ("model => 'TimesFM 2.5'", "confidence_level => 0.8", "horizon => 17", "ai_forecast_status",
                 f"TABLE `{TABLE}`", "id_cols => ['series_id']", "prediction_interval_lower_bound"):
        assert part in text
    assert re.search(r"SELECT[^;]*\bconfidence_level\b[^;]*FROM AI\.FORECAST", text)
    assert "{" not in text and "}" not in text


def test_arima_plus_uses_one_horizon_for_model_and_forecast():
    model = bq.sql("arima_plus_model", model=MODEL, fallback_table=FALLBACK, horizon=17)
    fc = bq.sql("arima_plus", model=MODEL, horizon=17, confidence_level=0.8)
    assert "horizon = 17" in model and f"MODEL `{MODEL}`" in model and f"FROM `{FALLBACK}`" in model
    assert "STRUCT(17 AS horizon, 0.8 AS confidence_level)" in fc and f"MODEL `{MODEL}`" in fc
    assert "model_type = 'ARIMA_PLUS'" in model and "data_frequency = 'DAILY'" in model


@pytest.mark.parametrize("args", [("Bad_Project", "shelfshift", "x"), (PROJECT, "a-b", "x"),
                                  ("x-demo-12", "shelfshift", "t;DROP"), ("x-demo-12", "shelfshift", "a`b"),
                                  ("ab", "shelfshift", "x"), ("x-demo-12", "", "x"), ("x-demo-12-", "s", "x")])
def test_bad_identifiers_are_refused(args):
    with pytest.raises(bq.BigQueryConfigError):
        bq.table_id(*args)


def test_good_identifier():
    assert bq.table_id(PROJECT, "shelfshift", "forecast_input") == \
        f"{PROJECT}.shelfshift.forecast_input"


@pytest.mark.parametrize("over", [
    {"horizon": "17"}, {"horizon": 17.0}, {"horizon": True}, {"horizon": 0}, {"horizon": -3},
    {"confidence_level": "0.8"}, {"confidence_level": 1}, {"confidence_level": True}, {"confidence_level": 1.5},
    {"confidence_level": float("nan")},
    {"model_name": "TimesFM 2.5'; DROP TABLE x; --"}, {"model_name": "ARIMA"}, {"model_name": 2.5},
    {"table": "x-demo-12.shelfshift.t; DROP"}, {"table": "shelfshift.forecast_input"}, {"table": "a.b.c.d"},
    {"table": "Bad_Project.shelfshift.forecast_input"}, {"extra": 1},
])
def test_sql_refuses_unchecked_fill_values(over):
    with pytest.raises(bq.BigQueryConfigError):
        ai_sql(**over)


def test_sql_refuses_an_unknown_section_and_a_missing_fill():
    with pytest.raises(bq.BigQueryConfigError):
        bq.sql("drop_everything", table=TABLE)
    with pytest.raises(bq.BigQueryConfigError):
        bq.sql("ai_forecast", table=TABLE, horizon=17)


# --- rows -----------------------------------------------------------------------------------------------------------

def test_input_rows_are_midnight_utc_on_the_demo_calendar():
    rows = bq.input_rows({"d0|Bang Na|MILK-1L": [(-1, 3, "imputed"), (0, 4.25, "recorded")]})
    assert rows == [
        {"series_id": "d0|Bang Na|MILK-1L", "ts": "2026-10-11T00:00:00+00:00", "value": 3.0, "how": "imputed"},
        {"series_id": "d0|Bang Na|MILK-1L", "ts": "2026-10-12T00:00:00+00:00", "value": 4.25, "how": "recorded"},
    ]
    assert [n for n, _ in bq.SCHEMA] == list(rows[0])


def test_parse_keeps_raw_values_and_reads_the_day_from_the_timestamp():
    rows = [
        {"series_id": "d3|Bang Na|MILK-1L", "forecast_timestamp": when(4), "forecast_value": 35.9,
         "prediction_interval_lower_bound": 36.5, "prediction_interval_upper_bound": 35.29,
         "confidence_level": 0.8, "ai_forecast_status": ""},  # crossed: lower above upper
        {"series_id": "d3|Bang Na|MILK-1L", "forecast_timestamp": when(3), "forecast_value": -0.4,
         "prediction_interval_lower_bound": -2.123456789, "prediction_interval_upper_bound": 1.0,
         "confidence_level": 0.8, "ai_forecast_status": None},
        {"series_id": "d3|Rangsit|BREAD-400",
         "forecast_timestamp": datetime(2026, 10, 15, 7, 0, tzinfo=timezone(timedelta(hours=7))),
         "forecast_value": 5.0, "prediction_interval_lower_bound": 4.0, "prediction_interval_upper_bound": 6.0,
         "confidence_level": 0.95, "ai_forecast_status": "The time series data is too short"},
    ]
    got = bq.parse(rows, "ai_forecast")
    assert got == {
        "d3|Bang Na|MILK-1L": [
            {"day": 4, "low": 36.5, "base": 35.9, "high": 35.29, "confidence_level": 0.8, "status": ""},
            {"day": 3, "low": -2.123456789, "base": -0.4, "high": 1.0, "confidence_level": 0.8, "status": ""},
        ],
        "d3|Rangsit|BREAD-400": [
            {"day": 3, "low": 4.0, "base": 5.0, "high": 6.0, "confidence_level": 0.95,
             "status": "The time series data is too short"},
        ],
    }


def test_parse_defaults_a_missing_status_column_to_empty():
    row = {"series_id": "backtest|Thonburi|STRAW-500", "forecast_timestamp": when(-14), "forecast_value": 1.0,
           "prediction_interval_lower_bound": 0.5, "prediction_interval_upper_bound": 1.5, "confidence_level": 0.8}
    assert bq.parse([row], "arima_plus")["backtest|Thonburi|STRAW-500"][0]["status"] == ""
    with pytest.raises(bq.BigQueryConfigError):
        bq.parse([row], "local-seasonal-naive")


def test_horizon_is_seventeen_for_the_release_calendar(seed):
    assert bq.horizon(F.required(seed), F.inputs(seed)) == 17


# --- import boundary ------------------------------------------------------------------------------------------------

def test_data_bq_and_forecast_import_with_google_blocked():
    code = ("import sys; sys.modules['google'] = None; import data.bq, data.forecast; "
            "print(data.bq.preflight.__name__)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "preflight"


def test_importing_the_adapter_loads_no_google_module():
    code = "import sys, data.bq, data.forecast; print(sorted(m for m in sys.modules if m.split('.')[0] == 'google'))"
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


# --- providers against the fake client ------------------------------------------------------------------------------

def make_providers(client, factory_calls):
    def factory(project, location):
        factory_calls.append((project, location))
        return client
    return bq.providers(PROJECT, "asia-southeast1", "shelfshift", 0.8, "TimesFM 2.5", client_factory=factory)


def test_arima_plus_is_asked_only_for_the_series_ai_forecast_got_wrong(seed):
    bad = "d3|Bang Na|MILK-1L"
    client, factory_calls = FakeClient(spoil={bad}), []
    ai, arima = make_providers(client, factory_calls)
    out = F.build(seed, ai, arima)
    assert factory_calls == [(PROJECT, "asia-southeast1")]  # one client, built lazily
    loads = [c for c in client.calls if c[0] == "load"]
    assert loads[0][1] == TABLE and len(loads[0][2]) == 120
    assert loads[1] == ("load", FALLBACK, [bad])
    queries = [c[1] for c in client.calls if c[0] == "query"]
    assert ["AI.FORECAST" in q for q in queries] == [True, False, False]
    assert "CREATE OR REPLACE MODEL" in queries[1] and "ML.FORECAST" in queries[2]
    assert set(client.datasets) == {(f"{PROJECT}.shelfshift", True)}
    run = next(r for r in out["runs"] if r["job"] == "d3")
    assert run["providers"] == {"ai_forecast": 14, "arima_plus": 1}
    assert run["fallbacks"][0]["branch"] == "Bang Na" and run["fallbacks"][0]["failed_check"] == "bounds"
    F.check_candidate(out, seed)


def test_fake_bigquery_round_trip_reproduces_the_local_numbers(seed):
    """Days survive the timestamp round trip: the fake answers with the stand-in's values, so every point matches."""
    ai, arima = make_providers(FakeClient(), [])
    cloud = F.build(seed, ai, arima)
    local = F.build(seed, F.LOCAL_PROVIDER)
    strip = lambda f: sorted((p["branch"], p["sku"], p["day"], p["low"], p["base"], p["high"], p["clamped"])
                             for p in f["points"])
    assert strip(cloud) == strip(local)
    assert {p["provider"] for p in cloud["points"]} == {"ai_forecast"}


def test_providers_check_everything_before_any_client_exists():
    calls = []
    for args in [("Bad_Project", "asia-southeast1", "shelfshift", 0.8, "TimesFM 2.5"),
                 (PROJECT, "asia-southeast1", "shelf-shift", 0.8, "TimesFM 2.5"),
                 (PROJECT, "asia southeast1", "shelfshift", 0.8, "TimesFM 2.5"),
                 (PROJECT, "asia-southeast1", "shelfshift", "0.8", "TimesFM 2.5"),
                 (PROJECT, "asia-southeast1", "shelfshift", 0.8, "TimesFM 2.5'")]:
        with pytest.raises(bq.BigQueryConfigError):
            bq.providers(*args, client_factory=lambda *a: calls.append(a))
    assert calls == []


# --- CLI ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def ref(tmp_path):
    for name in ("seed.json", "truth.json"):
        shutil.copy(REF / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def fake_cloud(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(bq, "make_client", lambda project, location: client)
    return client


def test_a_cloud_provider_without_a_project_exits_2(ref, monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    for provider in ("ai_forecast", "arima_plus"):
        with pytest.raises(SystemExit) as e:
            F.main(["--provider", provider, "--ref", str(ref)])
        assert e.value.code == 2
        assert "set GOOGLE_CLOUD_PROJECT or pass --project" in capsys.readouterr().err
    assert not (ref / "forecast.json").exists()


def test_a_malformed_project_exits_2_before_any_client(ref, monkeypatch, capsys):
    monkeypatch.setattr(bq, "make_client", lambda *a: pytest.fail("no client for a refused project"))
    with pytest.raises(SystemExit) as e:
        F.main(["--provider", "ai_forecast", "--project", "Bad_Project", "--ref", str(ref)])
    assert e.value.code == 2
    assert "BigQuery configuration refused" in capsys.readouterr().err


def test_preflight_needs_no_network(ref, monkeypatch, capsys):
    monkeypatch.setattr(bq, "make_client", lambda *a: pytest.fail("preflight must not build a client"))
    monkeypatch.setattr(bq, "_package_found", lambda: True)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    assert F.main(["--provider", "ai_forecast", "--preflight", "--ref", str(ref)]) == 0
    text = capsys.readouterr().out
    info = json.loads(text[:text.index("\nSELECT") + 1])
    assert (info["series"], info["horizon"], info["project"]) == (120, 17, PROJECT)
    assert (info["model"], info["confidence_level"], info["location"]) == ("TimesFM 2.5", 0.8, "asia-southeast1")
    assert info["problems"] == []
    assert "model => 'TimesFM 2.5'" in text and f"`{TABLE}`" in text
    assert not (ref / "forecast.json").exists()


def test_preflight_exits_1_when_the_package_is_missing_or_the_project_is_malformed(ref, monkeypatch, capsys):
    monkeypatch.setattr(bq, "make_client", lambda *a: pytest.fail("preflight must not build a client"))
    monkeypatch.setattr(bq, "_package_found", lambda: False)
    assert F.main(["--provider", "ai_forecast", "--preflight", "--project", PROJECT, "--ref", str(ref)]) == 1
    assert "google-cloud-bigquery is not installed" in capsys.readouterr().out
    monkeypatch.setattr(bq, "_package_found", lambda: True)
    assert F.main(["--provider", "arima_plus", "--preflight", "--project", "Bad_Project", "--ref", str(ref)]) == 1


def test_preflight_shows_the_arima_plus_sql(ref, monkeypatch, capsys):
    monkeypatch.setattr(bq, "_package_found", lambda: True)
    assert F.main(["--provider", "arima_plus", "--preflight", "--project", PROJECT, "--ref", str(ref)]) == 0
    text = capsys.readouterr().out
    assert "CREATE OR REPLACE MODEL" in text and "STRUCT(17 AS horizon, 0.8 AS confidence_level)" in text


def test_dry_run_prints_bytes_and_writes_nothing(ref, fake_cloud, capsys):
    assert F.main(["--provider", "ai_forecast", "--dry-run", "--project", PROJECT, "--ref", str(ref)]) == 0
    assert "123456 bytes" in capsys.readouterr().out
    assert [c[2] for c in fake_cloud.calls if c[0] == "query"] == [DRY_CFG]
    assert not (ref / "forecast.json").exists()


def test_arima_plus_provider_runs_arima_plus_for_every_series(ref, fake_cloud):
    assert F.main(["--provider", "arima_plus", "--project", PROJECT, "--ref", str(ref)]) == 0
    out = json.loads((ref / "forecast.json").read_text(encoding="utf-8"))
    assert {p["provider"] for p in out["points"]} == {"arima_plus"}
    assert not any("AI.FORECAST" in c[1] for c in fake_cloud.calls if c[0] == "query")


def test_local_never_replaces_a_valid_bigquery_forecast(ref, fake_cloud, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    assert F.main(["--provider", "ai_forecast", "--ref", str(ref)]) == 0
    path = ref / "forecast.json"
    before = path.read_bytes()
    assert {p["provider"] for p in json.loads(before)["points"]} == {"ai_forecast"}
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        F.main(["--provider", "local", "--ref", str(ref)])
    assert e.value.code == 2
    assert "refusing to replace a valid BigQuery forecast with the local stand-in" in capsys.readouterr().err
    assert path.read_bytes() == before
    # A re-tune changes the sales history: the BigQuery forecast is stale for this seed and may be replaced.
    seed = json.loads((ref / "seed.json").read_text(encoding="utf-8"))
    row = next(r for r in seed["daily_sales"] if not r["stockout_flag"] and r["sold_20"] + r["sold_30"] == 0)
    row["sold_full"] = round(row["sold_full"] + 1, 2)
    row["recorded"] = round(row["recorded"] + 1, 2)
    (ref / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    assert json.loads(before)["basis_id"] != versions.basis_id(seed)
    assert F.main(["--provider", "local", "--ref", str(ref)]) == 0
    assert {p["provider"] for p in json.loads(path.read_text(encoding="utf-8"))["points"]} == {F.LOCAL}


def test_local_rejects_cloud_only_flags(ref):
    for flag in ("--dry-run", "--preflight"):
        with pytest.raises(SystemExit) as e:
            F.main(["--provider", "local", flag, "--ref", str(ref)])
        assert e.value.code == 2


# --- the public set: file load, destination table, ordered paged read (05.2-09; DATA-13; D-22) ---------------------
#
# The parts come from tests/data/fixtures/frn_sample_synthetic.csv, a synthetic test fixture in the FreshRetailNet-50K
# schema; not FreshRetailNet rows (10 series x 5 runs). google is never imported: bq._bigquery is replaced by a fake
# module whose job configs record their arguments, and the fake client answers from the files it was loaded with.

import csv as _csv
import gzip as _gzip
import io as _io
from datetime import date

from data import public as U
from data import public_config as P
from data import public_forecast as PF

FIX = ROOT / "tests" / "data" / "fixtures"
FIX_CSV, FIX_JSON = FIX / "frn_sample_synthetic.csv", FIX / "frn_sample_synthetic.json"
DATASET = "shelfshift_public"
PIDS = bq.PublicIds(PROJECT, DATASET)


class _Config:
    def __init__(self, **kw):
        self.kw = kw


class FakeBigQuery:
    class LoadJobConfig(_Config):
        pass

    class QueryJobConfig(_Config):
        pass

    @staticmethod
    def SchemaField(name, kind):
        return (name, kind)


class Rows:
    def __init__(self, rows=(), total_bytes_processed=None, log=None):
        self.rows, self.total_bytes_processed, self.log = list(rows), total_bytes_processed, log

    def result(self, page_size=None):
        if self.log is not None:
            self.log.append(page_size)
        return iter(self.rows)


class PublicFake:
    """Answers AI.FORECAST and ML.FORECAST from the rows it was loaded with (the local stand-in's numbers, `horizon`
    days after each series' last input day) into the destination table; a read returns that table ordered by series.
    Series in `spoil_ai` / `spoil_arima` get a crossed interval on their first day."""

    def __init__(self, day0, spoil_ai=(), spoil_arima=(), dry_bytes=None):
        self.day0, self.spoil = day0, {True: set(spoil_ai), False: set(spoil_arima)}
        self.dry_bytes = dry_bytes or {}  # a dry run's estimate by the SQL's marker; 4242 otherwise
        self.tables, self.models, self.calls, self.pages, self.datasets = {}, {}, [], [], []

    def create_dataset(self, dataset, exists_ok=False):
        self.datasets.append((dataset, exists_ok))

    def load_table_from_file(self, fh, table, job_config=None):
        assert isinstance(job_config, FakeBigQuery.LoadJobConfig)
        rows = list(_csv.reader(_io.TextIOWrapper(_gzip.GzipFile(fileobj=fh), encoding="utf-8", newline="")))
        skip = job_config.kw["skip_leading_rows"]
        self.tables[table] = [(r[0], (date.fromisoformat(r[1][:10]) - self.day0).days, float(r[2]), r[3])
                              for r in rows[skip:]]
        self.calls.append(("load", table, job_config, sorted({r[0] for r in rows[skip:]})))
        return Rows()

    def _answer(self, source, sql, ai):
        h = int((re.search(r"horizon => (\d+)", sql) or re.search(r"STRUCT\((\d+) AS horizon", sql)).group(1))
        ins = defaultdict(list)
        for sid, day, value, how in self.tables[source]:
            ins[sid].append((day, value, how))
        req = {sid: list(range(xs[-1][0] + 1, xs[-1][0] + h + 1)) for sid, xs in ins.items()}
        out = []
        for sid, rows in F.local_fetch(dict(ins), req).items():
            for i, r in enumerate(rows):
                crossed = sid in self.spoil[ai] and i == 0
                low, high = (r["high"], r["low"]) if crossed else (r["low"], r["high"])
                d = self.day0 + timedelta(days=r["day"])
                row = {"series_id": sid, "forecast_timestamp": datetime(d.year, d.month, d.day, tzinfo=UTC),
                       "forecast_value": r["base"], "prediction_interval_lower_bound": low,
                       "prediction_interval_upper_bound": high, "confidence_level": 0.8}
                if ai:
                    row["ai_forecast_status"] = ""
                out.append(row)
        return out

    def query(self, sql, job_config=None):
        self.calls.append(("query", sql, job_config))
        if job_config is not None and job_config.kw.get("dry_run"):
            marker = next((m for m in self.dry_bytes if m in sql), None)
            return Rows(total_bytes_processed=self.dry_bytes[marker] if marker else 4242)
        if "CREATE OR REPLACE MODEL" in sql:
            model = re.search(r"MODEL `([^`]+)`", sql).group(1)
            self.models[model] = re.search(r"FROM `([^`]+)`", sql).group(1)
            return Rows()
        if "AI.FORECAST" in sql or "ML.FORECAST" in sql:
            if "AI.FORECAST" in sql:
                rows = self._answer(re.search(r"TABLE `([^`]+)`", sql).group(1), sql, True)
            else:
                rows = self._answer(self.models[re.search(r"MODEL `([^`]+)`", sql).group(1)], sql, False)
            dest, how = job_config.kw["destination"], job_config.kw["write_disposition"]
            self.tables[dest] = (self.tables.get(dest, []) if how == "WRITE_APPEND" else []) + rows
            return Rows()
        if sql.startswith("SELECT") and "ORDER BY series_id, forecast_timestamp" in sql:
            table = re.search(r"FROM `([^`]+)`", sql).group(1)
            return Rows(sorted(self.tables[table], key=lambda r: (r["series_id"], r["forecast_timestamp"])),
                        log=self.pages)
        raise AssertionError(f"unexpected SQL {sql!r}")


@pytest.fixture(autouse=True)
def fake_google_module(monkeypatch):
    monkeypatch.setattr(bq, "_bigquery", lambda: FakeBigQuery)


@pytest.fixture(scope="module")
def fix_cfg():
    return P.load_for(FIX_JSON)


@pytest.fixture
def parts(fix_cfg, tmp_path):
    PF.prepare_input(U.series_iter(None, fix_cfg, csv_path=FIX_CSV), fix_cfg, tmp_path / "parts")
    return tmp_path / "parts"


def day0(cfg):
    return date.fromisoformat(cfg["calendar"]["day0"])


def cap(cfg) -> int:
    return cfg["budget"]["max_bytes_billed"]


def paid(client) -> list:
    """The query calls that are not dry runs (the ones BigQuery bills)."""
    return [c for c in client.calls if c[0] == "query" and not c[2].kw.get("dry_run")]


def stream(parts, cfg, client, partition=None):
    meta = PF.read_input_meta(parts)
    fc = cfg["forecast"]
    ai, arima = bq.public_fetchers(PROJECT, fc["location"], fc["dataset"], fc["confidence_level"], fc["model"],
                                   day0(cfg), PF.forecast_horizon(meta), partition=partition,
                                   client_factory=lambda p, loc: client, max_bytes_billed=cap(cfg))
    return PF.stream_forecast(parts, PF.StreamProvider(F.AI_FORECAST, ai), PF.StreamProvider(F.ARIMA_PLUS, arima))


def test_public_ids_name_every_table_and_refuse_bad_identifiers():
    assert (PIDS.input_table, PIDS.output_table) == (f"{PROJECT}.{DATASET}.forecast_input",
                                                     f"{PROJECT}.{DATASET}.forecast_output")
    assert (PIDS.fallback_input, PIDS.fallback_output, PIDS.model) == (
        f"{PROJECT}.{DATASET}.forecast_input_fallback", f"{PROJECT}.{DATASET}.forecast_output_fallback",
        f"{PROJECT}.{DATASET}.arima_plus_fallback")
    for project, dataset in (("Bad_Project", DATASET), (PROJECT, "shelf-shift"), (PROJECT, "a`b"), (PROJECT, "")):
        with pytest.raises(bq.BigQueryConfigError):
            bq.PublicIds(project, dataset)


def test_public_fetchers_check_everything_before_any_client_exists(fix_cfg):
    calls = []
    for args in [("Bad_Project", "asia-southeast1", DATASET, 0.8, "TimesFM 2.5", 14),
                 (PROJECT, "asia southeast1", DATASET, 0.8, "TimesFM 2.5", 14),
                 (PROJECT, "asia-southeast1", DATASET, "0.8", "TimesFM 2.5", 14),
                 (PROJECT, "asia-southeast1", DATASET, 0.8, "TimesFM 2.5'", 14),
                 (PROJECT, "asia-southeast1", DATASET, 0.8, "TimesFM 2.5", 0)]:
        project, location, dataset, level, model, h = args
        with pytest.raises(bq.BigQueryConfigError):
            bq.public_fetchers(project, location, dataset, level, model, day0(fix_cfg), h,
                               client_factory=lambda *a: calls.append(a), max_bytes_billed=cap(fix_cfg))
    for bad in (0, -1, True, 5.0, "5368709120", None):
        with pytest.raises(bq.BigQueryConfigError, match="max_bytes_billed"):
            bq.public_fetchers(PROJECT, "asia-southeast1", DATASET, 0.8, "TimesFM 2.5", day0(fix_cfg), 14,
                               client_factory=lambda *a: calls.append(a), max_bytes_billed=bad)
    assert calls == []


def test_load_file_loads_the_gz_csv_with_the_explicit_schema(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg))
    bq.load_file(client, PIDS.input_table, parts / "forecast_input.csv.gz")
    (kind, table, config, sids), = client.calls
    assert (kind, table) == ("load", PIDS.input_table) and len(sids) == 50
    assert config.kw == {"source_format": "CSV", "skip_leading_rows": 1, "write_disposition": "WRITE_TRUNCATE",
                         "schema": list(bq.SCHEMA)}
    with pytest.raises(bq.BigQueryConfigError):
        bq.load_file(client, "x-demo-12.shelfshift_public.t;DROP", parts / "forecast_input.csv.gz")


def test_run_to_table_writes_ai_forecast_into_the_destination(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg))
    bq.load_file(client, PIDS.input_table, parts / "forecast_input.csv.gz")
    text = bq.sql("ai_forecast", table=PIDS.input_table, model_name="TimesFM 2.5", horizon=14, confidence_level=0.8)
    bq.run_to_table(client, text, PIDS.output_table, max_bytes_billed=cap(fix_cfg))
    _, sql_text, config = client.calls[-1]
    assert config.kw == {"destination": PIDS.output_table, "write_disposition": "WRITE_TRUNCATE",
                         "maximum_bytes_billed": cap(fix_cfg)}
    assert client.calls[-2][2].kw == {"dry_run": True, "use_query_cache": False, "maximum_bytes_billed": cap(fix_cfg)}
    for part in ("model => 'TimesFM 2.5'", "horizon => 14", "confidence_level => 0.8", f"TABLE `{PIDS.input_table}`"):
        assert part in sql_text
    assert not sql_text.rstrip().endswith(";")  # one statement, so the destination applies
    bq.run_to_table(client, text, PIDS.output_table, append=True, max_bytes_billed=cap(fix_cfg))
    assert client.calls[-1][2].kw["write_disposition"] == "WRITE_APPEND"


def test_read_ordered_yields_one_series_at_a_time_on_the_public_calendar(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg))
    bq.load_file(client, PIDS.input_table, parts / "forecast_input.csv.gz")
    text = bq.sql("ai_forecast", table=PIDS.input_table, model_name="TimesFM 2.5", horizon=14, confidence_level=0.8)
    bq.run_to_table(client, text, PIDS.output_table, max_bytes_billed=cap(fix_cfg))
    got = list(bq.read_ordered(client, PIDS.output_table, day0(fix_cfg), F.AI_FORECAST, page_size=7,
                               max_bytes_billed=cap(fix_cfg)))
    assert [sid for sid, _ in got] == sorted(sid for sid, _ in got) and len(got) == 50
    assert client.pages == [7]
    read = client.calls[-1][1]
    assert "ORDER BY series_id, forecast_timestamp" in read and "ai_forecast_status" in read
    sid, rows = next((s, r) for s, r in got if s == "d0|S11|P101")
    assert [r["day"] for r in rows] == list(range(0, 14))  # 14 steps after the cutoff -1, days from day0
    assert set(rows[0]) == {"day", "low", "base", "high", "confidence_level", "status"}
    table = client.tables[PIDS.output_table]  # a read that is not ordered by series is refused: A, B, then A again
    a_rows = [r for r in table if r["series_id"] == "d0|S11|P101"]
    b_rows = [r for r in table if r["series_id"] == "d0|S11|P102"]
    client.query = lambda sql, job_config=None: Rows(a_rows[:2] + b_rows[:2] + a_rows[2:4])
    with pytest.raises(ValueError, match="not ordered"):
        list(bq.read_ordered(client, PIDS.output_table, day0(fix_cfg), F.AI_FORECAST,
                             max_bytes_billed=cap(fix_cfg)))


def test_the_file_path_end_to_end_matches_the_local_stand_in(parts, fix_cfg, tmp_path):
    client = PublicFake(day0(fix_cfg))
    summary = stream(parts, fix_cfg, client)
    assert summary["fallbacks"] == 0 and summary["points"] == 480
    loads = [c for c in client.calls if c[0] == "load"]
    assert [c[1] for c in loads] == [PIDS.input_table]
    assert set(client.datasets) == {(f"{PROJECT}.{DATASET}", True)}
    cloud = [r[1:7] for r in PF.read_csv_gz(parts / "forecast_points.csv.gz")]
    local = tmp_path / "local" / "parts"
    PF.prepare_input(U.series_iter(None, fix_cfg, csv_path=FIX_CSV), fix_cfg, local)
    PF.stream_forecast(local, PF.LOCAL_STREAM)
    assert cloud == [r[1:7] for r in PF.read_csv_gz(local / "forecast_points.csv.gz")]  # days survive the round trip
    assert {r[7] for r in PF.read_csv_gz(parts / "forecast_points.csv.gz")} == {F.AI_FORECAST}


def test_a_failing_series_alone_is_forecast_by_arima_plus(parts, fix_cfg):
    bad = "d1|S21|P102"
    client = PublicFake(day0(fix_cfg), spoil_ai={bad})
    summary = stream(parts, fix_cfg, client)
    loads = [c for c in client.calls if c[0] == "load"]
    assert [(c[1], c[3]) for c in loads] == [(PIDS.input_table, loads[0][3]), (PIDS.fallback_input, [bad])]
    queries = [c[1] for c in paid(client)]
    assert ["AI.FORECAST" in q for q in queries].count(True) == 1
    assert any(f"FROM `{PIDS.fallback_input}`" in q and "CREATE OR REPLACE MODEL" in q for q in queries)
    assert summary["fallbacks"] == 1
    runs = {r[1]: json.loads(r[6]) for r in PF.read_csv_gz(parts / "forecast_runs.csv.gz")}
    assert runs["d1"]["providers"] == {F.AI_FORECAST: 9, F.ARIMA_PLUS: 1}
    assert runs["d1"]["fallbacks"][0]["failed_check"] == "bounds"


def test_a_double_failure_raises_and_writes_nothing(parts, fix_cfg):
    bad = "backtest|S13|P101"
    client = PublicFake(day0(fix_cfg), spoil_ai={bad}, spoil_arima={bad})
    with pytest.raises(F.ForecastFailed):
        stream(parts, fix_cfg, client)
    assert not (parts / "forecast_points.csv.gz").exists() and not (parts / "forecast_runs.csv.gz").exists()


def test_partition_by_city_runs_one_query_per_city_into_one_table(parts, fix_cfg, tmp_path):
    meta = PF.read_input_meta(parts)
    files = PF.split_by_city(parts / "forecast_input.csv.gz", meta["store_city"], tmp_path / "cities")
    assert [f.name for f in files] == ["forecast_input-C1.csv.gz", "forecast_input-C2.csv.gz"]
    client = PublicFake(day0(fix_cfg))
    summary = stream(parts, fix_cfg, client, partition=files)
    steps = [(c[0], c[2].kw["write_disposition"] if c[0] == "query" and c[2] is not None else None)
             for c in client.calls if c[0] == "load" or ("AI.FORECAST" in c[1] and c in paid(client))]
    assert steps == [("load", None), ("query", "WRITE_TRUNCATE"), ("load", None), ("query", "WRITE_APPEND")]
    assert summary["points"] == 480 and summary["fallbacks"] == 0  # each city's rows checked the same way


def test_dry_run_file_reports_bytes_with_no_paid_query(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg))
    report = bq.dry_run_file(client, PIDS, parts / "forecast_input.csv.gz", 14, 0.8, "TimesFM 2.5",
                             max_bytes_billed=cap(fix_cfg))
    assert [(r["query"], r["bytes"]) for r in report] == [("ai_forecast", 4242), ("arima_plus_model", None),
                                                          ("arima_plus", None), ("read_ordered", None)]
    assert not any(r["over_cap"] for r in report)
    assert "priced after AI.FORECAST" in report[1]["note"]  # never priced on every series up front (2026-10-17)
    queries = [c for c in client.calls if c[0] == "query"]
    assert len(queries) == 1 and "AI.FORECAST" in queries[0][1]
    assert all(c[2].kw == {"dry_run": True, "use_query_cache": False, "maximum_bytes_billed": cap(fix_cfg)}
               for c in queries)
    assert [c[0] for c in client.calls].count("load") == 1  # the free load, nothing else


def test_preflight_public_needs_no_network(parts, fix_cfg, monkeypatch, capsys):
    monkeypatch.setattr(bq, "make_client", lambda *a: pytest.fail("preflight must not build a client"))
    monkeypatch.setattr(bq, "_package_found", lambda: True)
    out = parts.parent
    args = ["forecast", "--provider", "ai_forecast", "--project", PROJECT, "--preflight", "--out", str(out),
            "--fixture", str(FIX_JSON)]
    assert PF.main(args) == 0
    text = capsys.readouterr().out
    info = json.loads(text[:text.index("\nSELECT") + 1])
    assert (info["series"], info["series_runs"], info["input_rows"], info["horizon"]) == (10, 50, 2020, 14)
    assert info["problems"] == [] and info["project"] == PROJECT
    assert info["max_bytes_billed"] == cap(fix_cfg)
    assert "model => 'TimesFM 2.5'" in text and "ORDER BY series_id, forecast_timestamp" in text
    assert "CREATE OR REPLACE MODEL" in text
    assert not (parts / "forecast_points.csv.gz").exists()


def test_preflight_reports_a_missing_or_stale_input_part(fix_cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bq, "_package_found", lambda: True)
    args = ["forecast", "--provider", "ai_forecast", "--project", PROJECT, "--preflight", "--out", str(tmp_path),
            "--fixture", str(FIX_JSON)]
    assert PF.main(args) == 1
    assert "no input part" in capsys.readouterr().out
    PF.prepare_input(U.series_iter(None, fix_cfg, csv_path=FIX_CSV), fix_cfg, tmp_path / "parts")
    assert PF.main(args[:-2]) == 1  # the full reference: the fixture's input part was built on other inputs
    assert "built on other inputs" in capsys.readouterr().out


def test_the_cli_runs_the_bigquery_path_and_refuses_without_a_project(parts, fix_cfg, monkeypatch, capsys):
    client = PublicFake(day0(fix_cfg))
    monkeypatch.setattr(bq, "make_client", lambda project, location: client)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    base = ["forecast", "--provider", "ai_forecast", "--out", str(parts.parent), "--fixture", str(FIX_JSON)]
    assert PF.main(base) == 2
    assert "set GOOGLE_CLOUD_PROJECT or pass --project" in capsys.readouterr().err
    assert PF.main(base + ["--project", PROJECT, "--dry-run"]) == 0
    assert "4242" in capsys.readouterr().out and not (parts / "forecast_points.csv.gz").exists()
    assert PF.main(base + ["--project", PROJECT]) == 0
    assert capsys.readouterr().out.startswith("forecast: 5 runs, 480 points;")
    assert {r[7] for r in PF.read_csv_gz(parts / "forecast_points.csv.gz")} == {F.AI_FORECAST}
    assert PF.main(base + ["--project", PROJECT, "--partition", "city"]) == 0
    assert not [p for p in parts.iterdir() if p.name.startswith(".")]  # the city files are cleaned up


def test_the_public_bigquery_code_names_no_project():
    for path in (ROOT / "data" / "bq.py", ROOT / "data" / "public_forecast.py", ROOT / "tools" / "frn.py"):
        assert not re.search(r"project\s*=\s*['\"][a-z][a-z0-9-]{4,28}[a-z0-9]['\"]", path.read_text(encoding="utf-8"))


# --- the budget cap (user 2026-10-17): maximum_bytes_billed on every query job, refused before it is sent --------
# The ARIMA_PLUS training is priced after AI.FORECAST, on the real table of the series that need the fallback; the
# user's dry run priced it on every series at 7.59 GiB against the 5 GiB cap, which only the worst case reaches.

CREATE = "CREATE OR REPLACE MODEL"
SPOILED = "d1|S21|P102"


def cli(parts, extra=()):
    return PF.main(["forecast", "--provider", "ai_forecast", "--project", PROJECT, "--out", str(parts.parent),
                    "--fixture", str(FIX_JSON), *extra])


def test_the_cap_reaches_every_query_job_of_a_run_with_a_fallback(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg), spoil_ai={SPOILED})
    stream(parts, fix_cfg, client)
    queries = [c for c in client.calls if c[0] == "query"]
    assert queries and all(c[2] is not None and c[2].kw["maximum_bytes_billed"] == cap(fix_cfg) for c in queries)
    texts = [q for _, q, _ in paid(client)]
    for marker in ("AI.FORECAST", CREATE, "ML.FORECAST"):
        assert sum(marker in q for q in texts) == 1, marker
    assert sum(q.startswith("SELECT") and "ORDER BY series_id" in q and "FORECAST(" not in q for q in texts) == 2
    for i, c in enumerate(client.calls):  # every paid query was priced by a dry run of the same text just before
        if c[0] == "query" and not c[2].kw.get("dry_run"):
            assert client.calls[i - 1][1] == c[1] and client.calls[i - 1][2].kw["dry_run"] is True


def test_an_ai_forecast_estimate_over_the_cap_refuses_before_any_paid_query(parts, fix_cfg):
    over = cap(fix_cfg) + 1
    client = PublicFake(day0(fix_cfg), dry_bytes={"AI.FORECAST": over})
    with pytest.raises(bq.BytesCapExceeded) as e:
        stream(parts, fix_cfg, client)
    assert paid(client) == []  # nothing billed: only the free load and a dry run ran
    assert str(e.value).startswith(f"ai_forecast: the dry run estimates {over} bytes")
    assert f"cap of {cap(fix_cfg)} bytes" in str(e.value) and PROJECT not in str(e.value)
    assert not (parts / "forecast_points.csv.gz").exists()


def test_zero_failing_series_never_touch_arima_plus(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg), dry_bytes={CREATE: cap(fix_cfg) + 1})  # would be refused if it were asked
    summary = stream(parts, fix_cfg, client)
    assert summary["fallbacks"] == 0
    assert not [c for c in client.calls if c[0] == "query" and (CREATE in c[1] or "ML.FORECAST" in c[1])]
    assert [c[1] for c in client.calls if c[0] == "load"] == [PIDS.input_table]


def test_a_small_fallback_within_the_cap_is_priced_on_its_own_table_then_trained(parts, fix_cfg):
    client = PublicFake(day0(fix_cfg), spoil_ai={SPOILED}, dry_bytes={CREATE: cap(fix_cfg)})  # exactly the cap: in
    summary = stream(parts, fix_cfg, client)
    assert summary["fallbacks"] == 1
    creates = [c for c in client.calls if c[0] == "query" and CREATE in c[1]]
    assert [c[2].kw.get("dry_run", False) for c in creates] == [True, False]  # priced, then trained
    assert all(f"FROM `{PIDS.fallback_input}`" in c[1] for c in creates)  # the real fallback table, not the input
    first_create = client.calls.index(creates[0])
    assert any("AI.FORECAST" in c[1] for c in paid(client) if client.calls.index(c) < first_create)
    assert creates[1][2].kw["maximum_bytes_billed"] == cap(fix_cfg)


def test_a_fallback_over_the_cap_trains_nothing_and_keeps_the_points(parts, fix_cfg, monkeypatch, capsys):
    PF.stream_forecast(parts, PF.LOCAL_STREAM)  # a previous points part, which must survive
    before = (parts / "forecast_points.csv.gz").read_bytes()
    over = cap(fix_cfg) + 1
    client = PublicFake(day0(fix_cfg), spoil_ai={SPOILED}, dry_bytes={CREATE: over})
    monkeypatch.setattr(bq, "make_client", lambda project, location: client)
    assert cli(parts) == 1
    out = capsys.readouterr()
    line, = out.err.strip().splitlines()
    assert line.startswith(f"refused: arima_plus_model: the dry run estimates {over} bytes ")
    assert f"for the 1 series that need the fallback, over the cap of {cap(fix_cfg)} bytes" in line
    assert "nothing was trained; already sent: ai_forecast estimated 4242 bytes" in line  # AI.FORECAST's cost
    assert line.endswith("forecast_points.csv.gz is unchanged") and PROJECT not in out.out + out.err
    assert not [c for c in paid(client) if CREATE in c[1]]
    assert any("AI.FORECAST" in c[1] for c in paid(client))
    assert (parts / "forecast_points.csv.gz").read_bytes() == before
    diag = json.loads((parts.parent / "forecast-diagnostics.json").read_text(encoding="utf-8"))
    assert (diag["refused"], diag["fallback_series"], diag["failed_checks"]) == ("arima_plus_model", 1, {"bounds": 1})
    assert (diag["estimate_bytes"], diag["cap_bytes"]) == (over, cap(fix_cfg))
    assert [q["query"] for q in diag["queries_sent"]] == ["ai_forecast", "read_ordered (ai_forecast)"]
    assert PROJECT not in json.dumps(diag)


def test_the_cli_dry_run_shows_the_cap_and_prices_only_ai_forecast(parts, fix_cfg, monkeypatch, capsys):
    c = cap(fix_cfg)
    monkeypatch.setattr(bq, "make_client", lambda project, location: PublicFake(day0(fix_cfg)))
    assert cli(parts, ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f'"max_bytes_billed": {c}' in out
    assert (f"cap {c} bytes ({bq.gib(c)}) per query; ai_forecast 4242 bytes (0.00 GiB); every estimate within it; "
            "the ARIMA_PLUS fallback is priced after AI.FORECAST, on the series that need it\n") in out

    over = PublicFake(day0(fix_cfg), dry_bytes={"AI.FORECAST": c + 1})
    monkeypatch.setattr(bq, "make_client", lambda project, location: over)
    assert cli(parts, ["--dry-run"]) == 1
    assert "OVER the cap: ai_forecast; do not run the paid step" in capsys.readouterr().out
    assert cli(parts) == 1
    text = capsys.readouterr()
    assert text.err.startswith(f"refused: ai_forecast: the dry run estimates {c + 1} bytes")
    assert "no paid query was sent" in text.err
    assert PROJECT not in text.out + text.err and paid(over) == []
    assert not (parts / "forecast_points.csv.gz").exists()
