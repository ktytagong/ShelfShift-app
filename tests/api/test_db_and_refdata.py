"""The database layer (spec §8 Locking rule, spec §4 Data flow), the reference install and the settings files.

Expected values come from the locked decisions and the contract (04-API-CONTRACT "Errors", "Settings"; CLAUDE.md
pool notes; 04-CONTEXT D-08, D-09, D-11, D-23; spec §5), never pasted from output. The release reference set is never
assumed to be the stand-in: a stand-in is built in tmp_path when a test needs one (03-05 rule).
"""
import json
import pathlib
import re
import shutil
import sys
import threading
import types

import pytest
import sqlalchemy as sa

from api import config, db, refdata
from api.errors import ApiError
from data import config as refconfig
from data import forecast as F
from data import reference
from tests.api.conftest import require_test_database

NEW_COLUMNS = {("engine_cache", "cache_key"), ("engine_cache", "rec_version"), ("lots", "origin"),
               ("lots", "moved_from"), ("inbound", "origin"), ("world_actions", "origin"),
               ("evidence_requests", "lot_ids"), ("run_policy", "run_id"), ("run_policy", "field"),
               ("run_policy", "value")}


def add_run(run_id: str, status: str = "ACTIVE", version: int = 0) -> None:
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, version, next_case_no) "
                     "VALUES (:r, 'SANDBOX', 'x', :s, 3, :v, 1)", r=run_id, s=status, v=version)


def log_count(run_id: str) -> int:
    with db.tx() as conn:
        return db.one(conn, "SELECT count(*) AS n FROM activity_log WHERE run_id = :r", r=run_id)["n"]


def test_migrate_is_idempotent(schema):
    db.migrate()
    db.migrate()
    with db.tx() as conn:
        cols = {(r["table_name"], r["column_name"]): r["data_type"] for r in db.rows(
            conn, "SELECT table_name, column_name, data_type FROM information_schema.columns "
                  "WHERE table_schema = 'public'")}
        pk = db.rows(conn, "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                           "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'engine_cache'::regclass "
                           "AND i.indisprimary")
    assert NEW_COLUMNS <= cols.keys()
    assert [r["attname"] for r in pk] == ["cache_key"]  # keyed by input hash, not rec_version (IN-01)
    assert cols[("evidence_requests", "lot_ids")] == "jsonb"
    assert cols[("run_policy", "value")] == "jsonb"
    # every quantity is float8: pg8000 returns numeric as Decimal (04-RESEARCH Pattern 5)
    assert "numeric" not in cols.values()
    for table in ("lots", "truth_lots", "inbound", "reservations"):
        assert cols[(table, "qty")] == "double precision"


def test_a_transfer_action_must_name_its_origin(schema):
    insert = ("INSERT INTO world_actions (run_id, world, action_id, kind, lot_id, sku, origin) "
              "VALUES ('sbx-orig', 'shelfshift', :a, :k, 'L1', 'MILK-1L', :o)")
    with db.tx() as conn:
        db.run(conn, insert, a="price", k="MARKDOWN", o=None)  # only a transfer needs an origin
        db.run(conn, insert, a="move", k="TRANSFER", o="Bang Na")
    # pg8000 maps only 23505 to IntegrityError; a check violation (23514) is read from the SQLSTATE
    with pytest.raises(sa.exc.DBAPIError) as e, db.tx() as conn:
        db.run(conn, insert, a="no-origin", k="TRANSFER", o=None)
    assert db.sql_code(e.value) == "23514"


def test_activity_log_rolls_back_with_the_change(schema):
    run_id = "sbx-log"  # the log has no foreign key: no sandbox needed
    before = log_count(run_id)
    with pytest.raises(RuntimeError):
        with db.tx() as conn:
            db.log(conn, run_id, 3, "sent", "SUS-19")
            raise RuntimeError("the write failed")
    assert log_count(run_id) == before
    with db.tx() as conn:
        db.log(conn, run_id, 3, "sent", "SUS-19", round=1)
    assert log_count(run_id) == before + 1


def test_a_retired_sandbox_rejects_writes(schema):
    add_run("sbx-retired", status="RETIRED")
    with pytest.raises(ApiError) as e, db.tx() as conn:
        db.lock_run(conn, "sbx-retired")
        db.log(conn, "sbx-retired", 3, "sent")  # never reached: the lock comes first
    assert (e.value.status, e.value.code) == (409, "sandbox_reset")
    assert e.value.message == "Your sandbox was reset. Reload to continue."
    assert log_count("sbx-retired") == 0  # FLOW-02: nothing changed


def test_an_unknown_sandbox_rejects_writes(schema):
    for run_id in ("sbx-never-made", None):
        with pytest.raises(ApiError) as e, db.tx() as conn:
            db.lock_run(conn, run_id, mode="UPDATE")
        assert (e.value.status, e.value.code) == (409, "sandbox_reset")
    with pytest.raises(ValueError, match="lock mode"), db.tx() as conn:
        db.lock_run(conn, "sbx-never-made", mode="SHARE; DROP TABLE lots")


def test_a_stale_run_version_is_refused(schema):
    add_run("sbx-stale", version=4)
    with pytest.raises(ApiError) as e, db.tx() as conn:
        db.lock_run(conn, "sbx-stale", mode="UPDATE", version=3)
    assert (e.value.status, e.value.code) == (409, "stale_version")
    assert e.value.message == "The demo moved on since you loaded this page."
    assert e.value.extra == {"version": 4}
    with db.tx() as conn:
        assert db.lock_run(conn, "sbx-stale", mode="UPDATE", version=4)["run_id"] == "sbx-stale"


def db_error(code: str) -> sa.exc.DBAPIError:
    """A DBAPIError wrapping a pg8000-shaped error whose first arg is the field dict, "C" the SQLSTATE."""
    class Orig(Exception):
        pass
    return sa.exc.DBAPIError("x", {}, Orig({"C": code, "M": "simulated"}))


def test_deadlocks_are_retried(schema, monkeypatch):
    sleeps = []
    monkeypatch.setattr(db.time, "sleep", sleeps.append)

    def failing(codes):
        calls = []

        def fn(conn):
            calls.append(1)
            if len(calls) <= len(codes):
                raise db_error(codes[len(calls) - 1])
            return "done"
        return fn, calls

    fn, calls = failing(["40P01", "40001", "40P01"])  # three retries, then success
    assert db.in_tx(fn) == "done" and len(calls) == 4 and len(sleeps) == 3

    fn, calls = failing(["40P01"] * 4)  # a fourth failure is not retried
    with pytest.raises(sa.exc.DBAPIError):
        db.in_tx(fn)
    assert len(calls) == 4

    fn, calls = failing(["23505"])  # anything else is raised at once
    with pytest.raises(sa.exc.DBAPIError):
        db.in_tx(fn)
    assert len(calls) == 1

    # the real driver: pg8000's error carries the SQLSTATE in its "C" field (22012 = division_by_zero)
    with pytest.raises(sa.exc.DBAPIError) as e, db.tx() as conn:
        conn.execute(sa.text("SELECT 1 / 0"))
    assert db.sql_code(e.value) == "22012"


def test_a_url_without_test_is_refused():
    with pytest.raises(RuntimeError, match="_test"):
        require_test_database("postgresql+pg8000://postgres@localhost:5432/shelfshift")
    with pytest.raises(RuntimeError, match="_test"):  # "_test" elsewhere in the URL does not count
        require_test_database("postgresql+pg8000://ci_test@db_test:5432/shelfshift")
    require_test_database("postgresql+pg8000://postgres@localhost:5432/shelfshift_test")


def test_the_pool_fits_db_f1_micro():
    db.reset_engine()
    pool = db.engine().pool
    assert pool.size() == 5 and pool._max_overflow == 2  # 7 per process; db-f1-micro allows 25 in all
    assert pool._pre_ping is True and pool._timeout == 10


def test_cloud_sql_uses_the_lazy_connector(monkeypatch):
    made = []

    class Connector:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def connect(self, *args, **kwargs):
            raise AssertionError("no connection is opened in this test")

    fake = types.ModuleType("google.cloud.sql.connector")
    fake.Connector = Connector
    monkeypatch.setitem(sys.modules, "google.cloud.sql.connector", fake)
    monkeypatch.setattr(config, "CLOUD_SQL_INSTANCE", "project:region:instance")
    db.reset_engine()
    try:
        eng = db.engine()
        assert made == [{"refresh_strategy": "lazy"}]
        assert eng.pool.size() == 5 and eng.pool._max_overflow == 2
    finally:
        db.reset_engine()  # the next test builds the local engine again


# --- the reference install -----------------------------------------------------------------------------------------

def ref_copy(tmp_path, part=None, edit=None):
    """seed.json, forecast.json and truth.json copied into tmp_path, with `edit(payload)` applied to `<part>.json`."""
    out = tmp_path / "reference"
    out.mkdir()
    for name in refdata.PARTS:
        shutil.copy(refdata.REF / f"{name}.json", out / f"{name}.json")
    if part is not None:
        path = out / f"{part}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        edit(payload)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return out


def table_counts() -> tuple[int, int]:
    with db.tx() as conn:
        return (db.one(conn, "SELECT count(*) AS n FROM ref_data")["n"],
                db.one(conn, "SELECT count(*) AS n FROM ref_truth")["n"])


def test_installing_the_same_reference_twice_is_a_no_op(schema):
    before = table_counts()
    assert refdata.install() == schema
    assert refdata.install() == schema
    assert table_counts() == before
    assert schema.startswith("ds-")  # Phase 3's dataset_version (data.versions), not an id made up here


def test_the_deploy_refuses_a_local_stand_in_forecast(schema, tmp_path):
    out = ref_copy(tmp_path)
    seed = json.loads((out / "seed.json").read_text(encoding="utf-8"))
    F.write_forecast(out / "forecast.json", F.run_forecasts(seed, F.LOCAL))
    before = table_counts()
    with pytest.raises(reference.ReleaseError, match=r"^refusing the local stand-in: .*; "
                                                     r"run python -m data\.forecast --provider ai_forecast$"):
        refdata.install(out, allow_local=False)  # Phase 3's release gate (D-13), not a provider compare here
    assert table_counts() == before


def test_the_release_set_installs_through_the_release_gate(schema):
    ref = reference.load(refdata.REF)
    assert reference.release_check(ref)["dataset_version"] == ref["dataset_version"]  # the gate passes, note or not
    assert refdata.install(allow_local=False) == ref["dataset_version"]  # load's version, not one derived here


def test_truth_is_stored_apart_from_the_public_reference(schema):
    with db.tx() as conn:
        parts = {r["part"] for r in db.rows(conn, "SELECT part FROM ref_data")}
        public = json.dumps([r["payload"] for r in db.rows(conn, "SELECT payload FROM ref_data")])
        hidden = db.one(conn, "SELECT payload FROM ref_truth WHERE dataset_version = :v", v=schema)["payload"]
    assert parts == {"seed", "forecast"}
    assert '"physical"' not in public and '"price_response"' not in public  # FLOW-01 prohibition
    assert {"demand", "physical", "price_response"} <= hidden.keys()
    truth = refdata.truth(schema)
    assert set(truth) == {"demand", "price_response", "physical"}


@pytest.mark.parametrize("part, rows", [("seed", "daily_sales"), ("forecast", "points"), ("seed", "capacity"),
                                        ("seed", "runs")])
def test_duplicate_rows_are_refused(schema, tmp_path, part, rows):
    def duplicate(payload):
        payload[rows].append(dict(payload[rows][0]))
    out = ref_copy(tmp_path, part, duplicate)
    before = table_counts()
    with pytest.raises(ValueError, match=f"{part}.json {rows}: duplicate row"):
        refdata.install(out)
    assert table_counts() == before


def stamp(field):
    def edit(payload):
        (payload["dataset"] if field == "reference_fingerprint" else payload)[field] = "x-000000000000"
    return edit


@pytest.mark.parametrize("part, field, reason", [("seed", "reference_fingerprint", "not this reference/"),
                                                 ("truth", "input_version", "truth is from another world")])
def test_a_seed_from_another_reference_set_is_refused(schema, tmp_path, part, field, reason):
    out = ref_copy(tmp_path, part, stamp(field))
    with pytest.raises(ValueError, match=reason):
        refdata.install(out)


def test_every_point_keeps_its_provider(schema):
    ref = refdata.get(schema)
    demo_day = {r["forecast_run_id"]: r["demo_day"] for r in ref.forecast["runs"] if r["job"] != F.BACKTEST}
    points = [p for p in ref.forecast["points"] if p["forecast_run_id"] in demo_day]
    assert points and len(ref.point_provider) == len(points)
    for p in points:
        assert ref.point_provider[(demo_day[p["forecast_run_id"]], p["branch"], p["sku"], p["day"])] == p["provider"]
    assert {(k[0], k[1], k[2]) for k in ref.point_provider} == set(ref.points)
    for run in ref.forecast_runs.values():  # each run keeps its provider counts and its cutoff
        assert {"providers", "cutoff_day", "forecast_run_id"} <= run.keys()


def test_policy_and_schedules_come_from_the_seed(schema):
    cfg = refconfig.load()
    ref = refdata.get(schema)
    assert ref.policy == refconfig.engine_policy(cfg)
    assert ref.schedules == {sku: refconfig.product_schedule(cfg, sku) for sku in cfg["world"]["products"]}
    assert ref.schedules["MILK-1L"] == ((2, 20), (1, 30))  # schedule A (03-CONTEXT D-20)
    assert ref.branches == ["Bang Na", "Rangsit", "Lat Phrao", "Bang Kapi", "Thonburi"]  # spec §5
    assert ref.currency == "USD" and ref.label == "Synthetic data · hypothetical scenario"


# --- the settings files ----------------------------------------------------------------------------------------------

def settings_copy(tmp_path, edit):
    tree = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    edit(tree)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(tree), encoding="utf-8")
    return path


@pytest.mark.parametrize("edit, where", [
    (lambda t: t["holds"]["hold_ttl_min"].pop("source"), "settings.holds.hold_ttl_min: leaf is missing source"),
    (lambda t: t["caps"]["session"]["reset"]["value"].update(window="week"), "settings.caps.session.reset"),
    (lambda t: t["caps"]["global"]["sandbox"].update(value=True), "settings.caps.global.sandbox"),
    (lambda t: t["caps"]["time_zone"].update(value="Mars/Olympus"), "settings.caps.time_zone"),
    (lambda t: t["caps"]["workspace"]["signal"]["value"].update(limit=0), "settings.caps.workspace.signal.limit"),
    (lambda t: t["caps"]["session"].update(signal=t["caps"]["workspace"]["signal"]),
     "a kind is capped per session or per workspace, not both"),  # 07-CONTEXT D-20: one own cap per kind
])
def test_a_malformed_settings_file_names_the_leaf(tmp_path, edit, where):
    assert config.load_settings() == config.SETTINGS  # the committed file loads
    with pytest.raises(refconfig.ConfigError, match=where):
        config.load_settings(settings_copy(tmp_path, edit))


def test_settings_bounds_cover_exactly_the_three_writable_fields():
    assert config.SETTINGS_BOUNDS == {  # 04-API-CONTRACT Settings; 04-CONTEXT D-23
        "waste_price": {"min": 0, "max": 100, "unit": "{currency}/kg"},  # the currency is filled in later (IN-07)
        "min_sellable_days": {"min": 1, "max": 7, "unit": "days"},
        "shortlist_step": {"allowed": [0, 5, 10]},
    }
    # the policy values FLOW-11 and spec §8 name, read from api/settings.json
    assert config.HOLD_TTL_MIN == 60 and config.ENGINE_TIMEOUT_S == 15
    assert config.SESSION_CAPS == {"ai": (60, "hour"), "reset": (20, "day")}
    # per workspace (07-CONTEXT D-20); the preview caps are read from the settings (conftest puts them out of reach)
    assert {k: v for k, v in config.WORKSPACE_CAPS.items() if k != "preview"} == {"signal": (20, "day"), "upload": (10, "day")}
    assert {k: v for k, v in config.GLOBAL_CAPS.items() if k != "preview"} == {"ai": 300, "sandbox": 300, "upload": 500}  # ai: 06-CONTEXT D-12
    assert config.SETTINGS["caps"]["workspace"]["preview"] == {"limit": 4, "window": "minute"}  # 07-20 review
    assert config.SETTINGS["caps"]["global"]["preview"] == {"limit": 12, "window": "minute"}  # across workspaces
    assert config.GLOBAL_WINDOWS == {"preview": "minute"}  # every other global cap is per day
    assert config.CLIENT_CAPS == {"passcode": (10, "minute")}  # 06-CONTEXT D-24
    assert str(config.CAP_TZ) == "Asia/Bangkok"
    assert (config.REQUEST_LEAD_MIN, config.MAX_REQUESTS_PER_VERSION) == (15, 2)


def test_the_local_database_is_published_on_loopback_only():
    """WR-04 (04-SECURITY F-2): the trust-auth Postgres of docker-compose.yml is reachable from this machine only;
    every published port names the loopback address."""
    compose = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    ports = re.findall(r"^\s*ports:\s*\[(.*)\]\s*$", compose, flags=re.M)
    published = [p.strip().strip("'\"") for line in ports for p in line.split(",")]
    assert published and all(p.startswith("127.0.0.1:") for p in published), published


@pytest.fixture
def tiny_pool(monkeypatch):
    """A pool of two connections and no overflow, with a short checkout wait (test values), rebuilt afterwards."""
    monkeypatch.setattr(config, "POOL_SIZE", 2)
    monkeypatch.setattr(config, "MAX_OVERFLOW", 0)
    monkeypatch.setattr(config, "POOL_TIMEOUT_S", 2)
    db.reset_engine()
    yield 2
    monkeypatch.undo()
    db.reset_engine()


def test_loading_reference_data_never_takes_a_second_connection(schema, tiny_pool):
    """WR-10: on a cold cache, a request that already holds a connection loads ref_data and ref_truth on that
    connection, so a full pool of cold requests never waits on itself until the checkout times out (db_busy); the
    load runs once at a time."""
    refdata.get.cache_clear()
    refdata.truth.cache_clear()
    start, out = threading.Barrier(tiny_pool), []

    def cold_request():
        try:
            with db.tx() as conn:
                db.one(conn, "SELECT 1 AS ok")  # this request holds its connection
                start.wait(10)  # every connection of the pool is now checked out
                out.append((refdata.get(schema).dataset_version, len(refdata.truth(schema)["demand"]) > 0))
        except Exception as exc:  # recorded and asserted below
            out.append(exc)
    threads = [threading.Thread(target=cold_request) for _ in range(tiny_pool)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert out == [(schema, True)] * tiny_pool


LOT_COLUMNS = ("run_id", "world", "lot_id", "branch", "sku", "qty", "expiry_day", "received_seq", "markdown_pct",
               "expiry_source", "last_verified_day", "verified_by", "tag", "origin", "moved_from")


def lot_row(run_id: str, i: int) -> tuple:
    """A lots row with floats, ints and NULLs in the nullable columns (verified_by, tag, origin, moved_from)."""
    return (run_id, "shelfshift", f"L{i:05d}", f"S{i % 7}", f"P{i % 3}", [1.5, 2, 0.1 + 0.2, 1e-7][i % 4], i, i * 3,
            [0, 20, 30][i % 3], "PRINTED", None if i % 5 == 0 else -i, None if i % 2 else "COUNT",
            None if i % 3 else "tag", None if i % 4 else "simulated", None if i % 6 else f"L{i - 1:05d}")


def test_insert_rows_matches_single_row_inserts(schema):
    """db.insert_rows (quick 261017-p52) writes exactly what single-row INSERTs write, across a chunk boundary, in a
    transaction that is rolled back; an empty list sends nothing; a name outside [a-z_] is refused."""
    n = db.INSERT_CHUNK + 1
    statements = []

    def count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    with db.engine().connect() as conn:
        for i in range(n):
            values = lot_row("p52-single", i)
            db.run(conn, f"INSERT INTO lots ({', '.join(LOT_COLUMNS)}) VALUES "
                         f"({', '.join(':' + c for c in LOT_COLUMNS)})", **dict(zip(LOT_COLUMNS, values)))
        sa.event.listen(db.engine(), "before_cursor_execute", count)
        try:
            assert db.insert_rows(conn, "lots", LOT_COLUMNS, [lot_row("p52-batch", i) for i in range(n)]) == n
        finally:
            sa.event.remove(db.engine(), "before_cursor_execute", count)
        assert 2 <= len(statements) < n  # the chunk boundary was crossed, and rows went in many per statement
        single, batch = (db.rows(conn, "SELECT * FROM lots WHERE run_id = :r ORDER BY lot_id", r=r)
                         for r in ("p52-single", "p52-batch"))
        assert len(single) == n
        assert [{k: v for k, v in r.items() if k != "run_id"} for r in batch] == \
            [{k: v for k, v in r.items() if k != "run_id"} for r in single]
        assert batch[2]["qty"] == 0.1 + 0.2 and batch[0]["verified_by"] == "COUNT" and batch[1]["verified_by"] is None
        conn.rollback()
    assert db.insert_rows(None, "lots", LOT_COLUMNS, []) == 0  # no connection is touched: nothing is sent
    for table, columns in (("lots; DROP TABLE lots", LOT_COLUMNS), ("lots", ("qty)", "lot_id")),
                           ("lots", ("Qty",)), ("lots", ("lot_id", "qty -- x"))):
        with pytest.raises(ValueError):
            db.insert_rows(None, table, columns, [tuple(range(len(columns)))])
