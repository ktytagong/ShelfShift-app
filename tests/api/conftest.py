"""API tests run against the local Postgres 17 (`make db-up`, D-11). They rebuild the schema once per test session
and install data/reference into it; the database URL must name a *_test database, checked before anything is
dropped."""
from __future__ import annotations

import os
import pathlib
import secrets
import uuid

import pytest
import sqlalchemy as sa

os.environ.setdefault("DATABASE_URL", "postgresql+pg8000://postgres@localhost:5432/shelfshift_test")
os.environ["ENGINE_WORKER"] = "off"  # 04-04's worker tests turn it on themselves
os.environ["DEMO_PASSCODE"] = secrets.token_urlsafe(16)  # generated per test session: no passcode is written in the repo
os.environ["HQ_PASSCODE"] = secrets.token_urlsafe(16)  # HQ's own credential (06.1 D-47), generated the same way
os.environ["DEMO_MODE"] = "on"  # the suite drives Next day and reset (06.1 D-04); off is the default (D-62),
# and a test of off sets config.DEMO_MODE to False itself

from api import config, db, refdata  # noqa: E402

config.ENGINE_WORKER = False
PASSCODE = config.DEMO_PASSCODE
HQ = config.HQ_PASSCODE


def require_test_database(url: str) -> None:
    """Refuse (before any DROP) a URL whose database name does not end in `_test` (D-11)."""
    name = sa.engine.make_url(url).database or ""
    if not name.endswith("_test"):
        raise RuntimeError(f"API tests drop the schema: point DATABASE_URL at a *_test database, not {name!r}")


@pytest.fixture(scope="session")
def schema():
    """An empty public schema with api/schema.sql applied and data/reference installed: the dataset_version."""
    require_test_database(config.DATABASE_URL)
    if config.CLOUD_SQL_INSTANCE:
        raise RuntimeError("API tests never run against Cloud SQL: unset CLOUD_SQL_INSTANCE")
    db.reset_engine()
    with db.tx() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    db.migrate()
    refdata.get.cache_clear()
    refdata.truth.cache_clear()
    return refdata.install(allow_local=True)  # D-13: Phase 4 builds and tests on the stand-in too


TINY = pathlib.Path(__file__).parent / "fixtures" / "public_tiny"  # build/web's tiny public set, copied byte for byte


@pytest.fixture(scope="session")
def public_dv(schema):
    """The tiny public artifact set (fixtures/public_tiny: a synthetic test fixture in the FreshRetailNet-50K schema)
    installed beside data/reference into the shared ref_* tables: its dataset_version (05.2-12)."""
    return refdata.install(TINY, allow_local=True)


def drop_runs(conn, run_ids: list[str]) -> None:
    """Delete these runs' per-run rows, their sessions and idempotency keys, then their demo_runs rows (test scratch
    only: a run no workspace of another test reads)."""
    from api import sandbox
    for table in [*sandbox.per_run_tables(conn), "sessions", "idempotency_keys", "demo_runs"]:
        db.run(conn, f"DELETE FROM {table} WHERE run_id = ANY(:ids)", ids=run_ids)


@pytest.fixture(scope="module")
def public_run(public_dv):
    """A TEMPLATE run of the tiny public set on day 0 (init_worlds, then the day start), as its demo_runs row. It stays
    INITIALIZING, never ACTIVE, so the synthetic ACTIVE template is still the one every workspace clones; a test that
    activates it puts the synthetic one back. Its per-run rows, and those of any workspace cloned from it, are deleted
    at teardown (05.2-14)."""
    from api import advance, sandbox
    run_id = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=public_dv)
        sandbox.init_worlds(conn, run_id, refdata.get(public_dv), refdata.truth(public_dv))
        advance.day_start(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id))
        row = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
    yield row
    with db.tx() as conn:
        clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = :r",
                                               r=run_id)]
        drop_runs(conn, [run_id, *clones])


def restore_the_designed_flag(conn, run: dict) -> list[str]:
    """Test-local setup on ONE scratch day-0 TEMPLATE of the tiny public set (never `public_run`, never the fixture
    files): the scheduled supplier deliveries of the designed lot's SKU due at its destinations from today to its
    expiry are deleted in every world of this run, so the designed lot qualifies on day 0 for the reason 05.2-15
    designed it (rule b: a destination short of stock). Since quick 261017-ofn the screen counts those deliveries, and
    the tiny set's day 0 flags no lot without this. Returns the deleted inbound ids."""
    from api import advance, inputs, overlay
    if (run["kind"], run["status"], run["day"], run.get("template_run_id")) != ("TEMPLATE", "INITIALIZING", 0, None):
        raise ValueError("restore_the_designed_flag works on a scratch day-0 template run only")
    lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND tag = 'designed'",
                 r=run["run_id"])
    dests = sorted({r.dest for r in inputs.build_input(conn, run, lot["lot_id"]).runs})
    read, p = overlay.everywhere(conn, run, "inbound")
    ids = [r["inbound_id"] for r in db.rows(
        conn, f"SELECT DISTINCT inbound_id FROM inbound WHERE {read} AND inbound.sku = :s AND inbound.branch = ANY(:b) "
              f"AND source_case_id IS NULL AND arrival_day >= :d AND arrival_day <= :e ORDER BY inbound_id",
        s=lot["sku"], b=dests, d=run["day"], e=lot["expiry_day"], **p)]
    assert ids, "the designed lot's destinations have no scheduled delivery in its window: nothing to set up"
    db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND world = ANY(:w) AND inbound_id = ANY(:i)",
           r=run["run_id"], w=list(advance.WORLDS), i=ids)
    return ids


@pytest.fixture(scope="module")
def public_template(template, public_dv):
    """The tiny public set built into a demo template through `sandbox.build_template` (the warm-up, the open day's
    catalogue and the checks; 05.2-16), ACTIVE for the module so every new session's workspace is cloned from it: its
    run_id. At teardown the synthetic template it retired is ACTIVE again and this one RETIRED, in one transaction;
    then its rows and those of every workspace cloned from it are deleted."""
    from api import sandbox
    with db.tx() as conn:
        before = sandbox.active_template(conn)["run_id"]
    run_id = sandbox.build_template(public_dv)
    try:
        yield run_id
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :p", p=run_id)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=before)
        with db.tx() as conn:
            clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = :r",
                                                   r=run_id)]
            drop_runs(conn, [run_id, *clones])


@pytest.fixture(scope="session")
def template(schema):
    """The Day-3 demo template built from data/reference (the warm-up runs through advance-day): its run_id."""
    from api import sandbox
    sandbox.clear_cache()  # the schema was rebuilt above: read its tables again
    return sandbox.build_template(schema)


@pytest.fixture
def run(template):
    """A fresh sandbox cloned from the ACTIVE template, as its demo_runs row."""
    from api import sandbox
    with db.tx() as conn:
        rid = sandbox.clone(conn, sandbox.active_template(conn))
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid)


def seat(branch: str | None) -> dict:
    """An actor: HQ when branch is None, else that branch's manager (the account is the store code, or "HQ")."""
    return {"account": "HQ", "role": "hq", "branch": None, "session_id": "t"} if branch is None else \
        {"account": branch, "role": "manager", "branch": branch, "session_id": "t"}


def take(run: dict | str, actor: dict, case_id: str) -> dict:
    """Take ownership of the case as `actor`, in its own transaction after the run lock, as the route makes it
    (06.1 D-67); the owner repeating it changes nothing."""
    from api import ownership
    run_id = run if isinstance(run, str) else run["run_id"]
    return db.in_tx(lambda conn: ownership.take_ownership(conn, db.lock_run(conn, run_id), actor, case_id))


def owned_send(run: dict | str, actor: dict, case_id: str, rec_version: str) -> dict:
    """Send as the owner (06.1 D-54): take ownership as `actor`, then send, each in its own transaction after the
    run lock, as the two routes make them. Any refusal (of either) is raised as the service raised it."""
    from api import workflow
    run_id = run if isinstance(run, str) else run["run_id"]
    take(run_id, actor, case_id)
    return db.in_tx(lambda conn: workflow.send(conn, db.lock_run(conn, run_id), actor, case_id, rec_version))


def by_tag(run: dict | str, tag: str) -> dict | None:
    """The sandbox's case for the lot with this seed tag. Cases are found by tag: only SUS-19 has a stable key."""
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = :t ORDER BY opened_day DESC LIMIT 1",
                      r=run_id, t=tag)


def fresh(run_id: str) -> dict:
    """The run's demo_runs row as it is now."""
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)


def world_lot(run: dict | str, lot_id: str, world: str = "shelfshift") -> dict | None:
    """The lot's record row as the workspace reads it: its own, else its template's (api/overlay.py, 05.2 D-09)."""
    from api import overlay
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        return overlay.lot(conn, fresh(run_id), world, lot_id)


def world_rows(run: dict | str, table: str, where: str = "", order: str = "", **params) -> list[dict]:
    """Rows of a series table (lots, inbound, outcomes) as the workspace reads them: its own rows of the series it
    owns, its template's of the others (overlay.everywhere). `where` is test SQL over that table's columns."""
    from api import overlay
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        read, p = overlay.everywhere(conn, fresh(run_id), table)
        return db.rows(conn, f"SELECT * FROM {table} WHERE {read}" + (f" AND {where}" if where else "") +
                       (f" ORDER BY {order}" if order else ""), **params, **p)


def physical(run: dict | str, lot_ids, world: str = "shelfshift") -> dict:
    """{lot_id: physical qty} as the workspace reads it (overlay.physical)."""
    from api import overlay
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        return overlay.physical(conn, fresh(run_id), world, lot_ids)


def own_world(run: dict | str) -> None:
    """A test that calls advance-day's internals directly (the morning screen, the store rule) first owns every
    series, as advance_day does before them (overlay.materialize_all under the run lock FOR UPDATE)."""
    from api import overlay
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        overlay.materialize_all(conn, db.lock_run(conn, run_id, mode="UPDATE"))


def own_series(run: dict | str, branch: str, sku: str, world: str = "shelfshift") -> None:
    """Before a test writes world rows of a workspace directly, the workspace takes its own copy of that series,
    as every write in api/ does first (overlay.materialize); its own rows are what it reads from then on."""
    from api import overlay
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        overlay.materialize(conn, fresh(run_id), world, branch, sku)


def first_store() -> str:
    """The store a new workspace starts at on the ACTIVE template (its catalogue's start seat, else its first store,
    as a new session's seat was before 06.1): the default store manager account of `sign_in`."""
    from api import catalogue, sandbox
    with db.tx() as conn:
        template = sandbox.active_template(conn)
        return catalogue.start_seat(conn, template) or refdata.get(template["dataset_version"]).branches[0]


@pytest.fixture
def store(template) -> str:
    return first_store()


def sign_in(c, account: str | None = None, passcode: str | None = None):
    """Sign `c` in as `account` (default: `first_store()`; "HQ" for HQ) with `passcode` (default: the store passcode,
    or HQ's own for "HQ"), with a fresh Idempotency-Key; asserts 200 and returns the response."""
    account = account or first_store()
    given = passcode or (HQ if account == "HQ" else PASSCODE)
    r = c.post("/api/session", json={"account": account, "passcode": given}, headers=key())
    assert r.status_code == 200, r.json()
    return r


@pytest.fixture
def client(template):
    """An HTTP client signed in as the start store's manager, with its own sandbox (the lifespan is not run: no
    worker)."""
    from fastapi.testclient import TestClient
    from api.app import app
    c = TestClient(app)
    sign_in(c)
    return c


def key() -> dict:
    """A fresh Idempotency-Key header, as the web app sends one per user action."""
    return {"Idempotency-Key": uuid.uuid4().hex}


def own(client, case_id: str):
    """Take ownership of the case over HTTP as the client's account (06.1 D-67); asserts 200 and returns the
    response."""
    r = client.post(f"/api/cases/{case_id}/take-ownership", json={}, headers=key())
    assert r.status_code == 200, r.json()
    return r


def take_and_send(client, case_id: str, rec_version: str, headers: dict | None = None):
    """Take ownership over HTTP, then send (06.1 D-54): the send's response. `headers` is the send's own (default:
    a fresh Idempotency-Key)."""
    own(client, case_id)
    return client.post(f"/api/cases/{case_id}/send", json={"rec_version": rec_version},
                       headers=key() if headers is None else headers)


def sign_out(c):
    """Sign `c` out (its workspace is kept, 06.1 D-02) with a fresh Idempotency-Key; asserts 200."""
    r = c.post("/api/session/sign-out", json={}, headers=key())
    assert r.status_code == 200, r.json()
    return r


def as_seat(client, branch: str | None) -> None:
    """Sign the client out, then in again as that branch's store manager, or as HQ (its own credential) when branch is
    None. The workspace is kept under a rotated session id. No route changes a seat after sign-in (06.1-06)."""
    sign_out(client)
    sign_in(client, "HQ" if branch is None else branch)
