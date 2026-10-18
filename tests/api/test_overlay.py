"""Workspaces share the dataset (spec §8 Sandboxes, template and reset, rev 6.6; 05.2-CONTEXT D-08..D-10, D-37).

A workspace is created with its template's workflow rows only and reads the world rows (recorded and physical stock,
deliveries, outcomes, world events) through its template until it writes a series. These tests re-prove what the
full copy gave for free: a fresh workspace answers exactly what its template answers; a write in one workspace (a
confirmed count, a committed transfer, Next day) changes nothing another workspace or the template reads; a reset
reads the template again; two transactions owning one series copy it once; and a reset stays inside the 2 s target
(the literal below, never raised). Cases and lots are found by tag and by the engine's own facts, never by a seed
number.
"""
import threading
import time

import pytest

from api import advance, cases, db, inputs, overlay, refdata, sandbox
from api.errors import ApiError
from tests.api.conftest import by_tag, fresh
from tests.api.test_evidence import full_loop, phantom
from tests.api.test_workflow import STORY, pick

WORLD = ("lots", "truth_lots", "inbound", "outcomes", "world_events")
RESET_TARGET_MS = 2000  # the existing 2 s target (FLOW-01, D-12), never raised


def own_rows(run_id: str, table: str) -> int:
    with db.tx() as conn:
        return db.one(conn, f"SELECT count(*) AS n FROM {table} WHERE run_id = :r", r=run_id)["n"]


def series_of(run_id: str) -> set[tuple]:
    with db.tx() as conn:
        return {(r["world"], r["branch"], r["sku"]) for r in db.rows(
            conn, "SELECT world, branch, sku FROM run_series WHERE run_id = :r", r=run_id)}


def new_workspace() -> dict:
    with db.tx() as conn:
        rid = sandbox.clone(conn, sandbox.active_template(conn))
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid)


def read_lot(run_id: str, lot_id: str) -> dict | None:
    with db.tx() as conn:
        return overlay.lot(conn, fresh(run_id), "shelfshift", lot_id)


def case_inputs(run_id: str) -> dict:
    """Every case's current raw input (or the reason it cannot be built), by case_id."""
    out = {}
    with db.tx() as conn:
        r = fresh(run_id)
        for case in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r ORDER BY case_id", r=run_id):
            try:
                out[case["case_id"]] = cases.current_input(conn, r, case)
            except Exception as exc:  # a gone lot or a missing day answers the same way in both runs
                out[case["case_id"]] = f"{type(exc).__name__}: {exc}"
        conn.rollback()
    return out


def views(run_id: str) -> dict:
    """What a visitor reads: the board and every case detail (rolled back, so the template is never written). A
    preloaded report's filed time is left out: a clone shifts it to its own creation (07-16, D-05, D-18)."""
    with db.tx() as conn:
        r = fresh(run_id)
        board = cases.board(conn, r)
        detail = {c["case_id"]: cases.detail(conn, r, c["case_id"]) for c in board}
        conn.rollback()
    for d in detail.values():
        d["reports"] = [{k: v for k, v in x.items() if not (k == "created_at" and x.get("provenance") == "simulated")}
                        for x in d.get("reports") or []]
    return {"board": board, "detail": detail}


# --- Task 1: reads resolve through the template; a count stays private; a reset reads the template again ------------

def test_every_world_read_in_a_workspace_resolves_through_the_overlay(run, template):
    for table in WORLD:
        assert own_rows(run["run_id"], table) == 0, table  # nothing of the world was copied
    assert series_of(run["run_id"]) == set()
    assert own_rows(template, "lots") and own_rows(template, "outcomes")  # the template holds the world
    assert views(run["run_id"]) == views(template)
    mine, theirs = case_inputs(run["run_id"]), case_inputs(template)
    assert mine == theirs and mine  # every case's EngineInput equals the template's


def test_a_count_in_one_workspace_changes_nothing_another_reads(run, template):
    a, b = run, new_workspace()
    case = by_tag(a, cases.STORY_TAG)
    fact = phantom(a, case["case_id"])
    lot_ids = list(fact["lot_ids"])
    before = {l: read_lot(template, l) for l in lot_ids}
    assert all(before[l]["qty"] > 0 for l in lot_ids)  # the phantom: on the record, physically empty
    b_input, t_input = case_inputs(b["run_id"]), case_inputs(template)

    q, _ = full_loop(a, case["case_id"], value=0.0)  # A counts the phantom lot(s): the record goes to 0

    for l in lot_ids:
        assert read_lot(a["run_id"], l)["qty"] == 0.0
        assert read_lot(b["run_id"], l) == before[l]  # B still reads the template's row
        assert read_lot(template, l) == before[l]  # the template's row is untouched
    sku = before[lot_ids[0]]["sku"]
    assert series_of(a["run_id"]) == {("shelfshift", q["to_branch"], sku)}  # exactly the counted series
    assert series_of(b["run_id"]) == set()
    assert own_rows(b["run_id"], "lots") == 0
    assert case_inputs(b["run_id"]) == b_input
    assert case_inputs(template) == t_input
    assert case_inputs(a["run_id"]) != b_input  # A's own input moved with its count


def test_after_a_reset_the_workspace_reads_the_template_again(run, template):
    case = by_tag(run, cases.STORY_TAG)
    lot_ids = list(phantom(run, case["case_id"])["lot_ids"])
    before = {l: read_lot(template, l) for l in lot_ids}
    full_loop(run, case["case_id"], value=0.0)
    assert series_of(run["run_id"])
    with db.tx() as conn:
        db.run(conn, "INSERT INTO sessions (session_id, role, branch, run_id) VALUES ('overlay-reset', 'manager', "
                     "NULL, :r)", r=run["run_id"])
    new, ms = db.in_tx(sandbox.reset, "overlay-reset")
    assert ms < RESET_TARGET_MS
    assert series_of(new) == set()  # owns nothing
    for table in WORLD:
        assert own_rows(new, table) == 0, table
    for l in lot_ids:
        assert read_lot(new, l) == before[l]  # the uncorrected record again
    assert read_lot(run["run_id"], l)["qty"] == 0.0  # the retired workspace kept its own change


# --- Task 2: committed deliveries, Next day (D-37), concurrency and the 2 s reset ------------------------------------

def deliveries(run_id: str, case_id: str) -> list[str]:
    """The case's pending deliveries as the run reads them (its own series and its template's)."""
    with db.tx() as conn:
        read, p = overlay.everywhere(conn, fresh(run_id), "inbound")
        return [r["inbound_id"] for r in db.rows(conn, f"SELECT inbound_id FROM inbound WHERE {read} "
                                                       f"AND inbound.source_case_id = :c ORDER BY 1", c=case_id, **p)]


def test_a_committed_transfer_stays_in_its_workspace_and_a_release_deletes_only_its_row(run, template):
    from tests.api.test_workflow import approve_transfer
    a, b = run, new_workspace()
    plan = pick(a, STORY)
    assert plan.kind == "TRANSFER", "D-15 hand-back: SUS-19's pick is not a transfer"
    sku = by_tag(a, cases.STORY_TAG)["sku"]
    assert deliveries(template, STORY) == [] and deliveries(b["run_id"], STORY) == []
    b_input = case_inputs(b["run_id"])
    assert approve_transfer(a)["state"] == "APPROVED"  # both sides approved: the delivery is booked
    assert len(deliveries(a["run_id"], STORY)) == 1
    assert ("shelfshift", plan.dest, sku) in series_of(a["run_id"])  # the destination's series is A's own now
    assert own_rows(a["run_id"], "inbound") >= 1
    assert deliveries(b["run_id"], STORY) == [] and deliveries(template, STORY) == []
    assert case_inputs(b["run_id"]) == b_input
    with db.tx() as conn:
        from api import bookings
        bookings.release(conn, a["run_id"], STORY)
    assert deliveries(a["run_id"], STORY) == []
    assert deliveries(b["run_id"], STORY) == [] and deliveries(template, STORY) == []


def scratch_template(template: str, case_id: str) -> tuple[dict, tuple]:
    """A RETIRED copy of one series of the template plus a pending delivery of `case_id` in it (test rows only), so
    a workspace can be cloned from a template that holds a delivery. Returns (its demo_runs row, the series)."""
    t = fresh(template)
    rid = f"tpl-ov-{case_id.lower()}"
    with db.tx() as conn:
        lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' ORDER BY lot_id LIMIT 1",
                     r=template)
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, version, next_case_no) "
                     "VALUES (:r, 'TEMPLATE', :v, 'RETIRED', :d, :ver, :n)", r=rid, v=t["dataset_version"],
               d=t["day"], ver=t["version"], n=t["next_case_no"])
        for table in ("lots", "inbound", "outcomes"):
            names = ", ".join(sandbox.columns(conn, table)[1:])
            db.run(conn, f"INSERT INTO {table} (run_id, {names}) SELECT :n, {names} FROM {table} WHERE run_id = :t "
                         f"AND world = 'shelfshift' AND branch = :b AND sku = :s", n=rid, t=template,
                   b=lot["branch"], s=lot["sku"])
        db.run(conn, "INSERT INTO inbound (run_id, world, inbound_id, branch, sku, qty, arrival_day, arrival_min, "
                     "expiry_day, received_seq, source_case_id, origin) VALUES (:r, 'shelfshift', :i, :b, :s, 5, :d, "
                     "0, :e, 1000000, :c, 'test')", r=rid, i=f"{case_id}>{lot['branch']}#1", b=lot["branch"],
               s=lot["sku"], d=t["day"] + 1, e=lot["expiry_day"], c=case_id)
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid), ("shelfshift", lot["branch"],
                                                                                 lot["sku"])


def test_releasing_a_delivery_the_template_holds_copies_the_series_first(template):
    tpl, series = scratch_template(template, "OV-REL")
    with db.tx() as conn:
        ws = sandbox.clone(conn, tpl)
    assert deliveries(ws, "OV-REL") == ["OV-REL>" + series[1] + "#1"]  # read through the template
    with db.tx() as conn:
        from api import bookings
        bookings.release(conn, ws, "OV-REL")
    assert deliveries(ws, "OV-REL") == []  # gone for the workspace
    assert series_of(ws) == {series}  # the series was copied in first, then its own row deleted
    assert deliveries(tpl["run_id"], "OV-REL") == ["OV-REL>" + series[1] + "#1"]  # the template keeps it


def world_snapshot(run_id: str) -> dict:
    from tests.api.test_advance import rows_without_run_id
    return {t: rows_without_run_id(run_id, t) for t in WORLD}


def test_next_day_owns_every_series_first_and_changes_nothing_another_reads(run, template):
    """D-37 B on the synthetic set: Next day behaves as A (owns every series first, then steps as before)."""
    from tests.api.test_advance import advance_once
    a, b = run, new_workspace()
    b_views, b_inputs, t_world = views(b["run_id"]), case_inputs(b["run_id"]), world_snapshot(template)
    out = advance_once(a)
    assert out == {"advanced": True, "day": a["day"] + 1, "version": a["version"] + 1}
    assert ("*", "*", "*") in series_of(a["run_id"])
    assert all(own_rows(a["run_id"], t) for t in ("lots", "truth_lots", "outcomes", "world_events"))
    assert views(b["run_id"]) == b_views and case_inputs(b["run_id"]) == b_inputs  # B still reads the template
    assert world_snapshot(template) == t_world  # the template's world never moved
    assert own_rows(b["run_id"], "lots") == 0 and series_of(b["run_id"]) == set()
    with db.tx() as conn:  # a second Next day copies nothing more: every series is A's own already
        assert overlay.materialize_all(conn, db.lock_run(conn, a["run_id"], mode="UPDATE")) == 0
    assert advance_once(a)["advanced"] is True
    assert world_snapshot(template) == t_world


def test_next_day_on_a_public_workspace_is_refused_and_changes_nothing(run, monkeypatch):
    """D-37 B (user): a public dataset's workspace has no Next day; the backend refuses it with 409 not_available
    before anything is written. 05.2-14 repeats this on the tiny public set."""
    from tests.api.test_advance import advance_once
    real = refdata.get

    class Public:  # the dataset's kind arrives with 05.2-12's reference tables; stood in here
        kind = "public"

        def __init__(self, ref):
            self._ref = ref

        def __getattr__(self, name):
            return getattr(self._ref, name)

    monkeypatch.setattr(refdata, "get", lambda dv: Public(real(dv)))
    before = fresh(run["run_id"])
    with pytest.raises(ApiError) as e:
        advance_once(run)
    assert (e.value.status, e.value.code, e.value.message) == (409, "not_available",
                                                               "Next day is not part of this dataset's workspaces")
    after = fresh(run["run_id"])
    assert (after["day"], after["version"]) == (before["day"], before["version"])
    assert series_of(run["run_id"]) == set() and all(own_rows(run["run_id"], t) == 0 for t in WORLD)


def test_two_transactions_owning_one_series_copy_it_once(run, template):
    case = by_tag(run, cases.STORY_TAG)
    lot = read_lot(template, case["lot_id"])
    series = ("shelfshift", lot["branch"], lot["sku"])
    with db.tx() as conn:
        want = db.one(conn, "SELECT count(*) AS n FROM lots WHERE run_id = :r AND world = 'shelfshift' AND branch = :b "
                            "AND sku = :s", r=template, b=lot["branch"], s=lot["sku"])["n"]
    claimed, second_waiting, results, errors = threading.Event(), threading.Event(), {}, []

    def first():
        try:
            with db.tx() as conn:
                results["first"] = overlay.materialize(conn, fresh(run["run_id"]), *series)
                claimed.set()
                second_waiting.wait(10)  # commit only once the second is blocked on this claim
        except Exception as exc:  # surfaced below
            errors.append(exc)
            claimed.set()

    def second():
        try:
            assert claimed.wait(10)
            with db.tx() as conn:
                results["second"] = overlay.materialize(conn, fresh(run["run_id"]), *series)
        except Exception as exc:
            errors.append(exc)

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start()
    t2.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not second_waiting.is_set():  # the second blocks on the first's claim
        with db.tx() as conn:
            if db.one(conn, "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                            "AND wait_event_type = 'Lock' AND query LIKE 'INSERT INTO run_series%'")["n"]:
                second_waiting.set()
    overlapped = second_waiting.is_set()
    second_waiting.set()  # never leave the first transaction waiting
    t1.join(15)
    t2.join(15)
    assert overlapped, "the second transaction never waited on the first one's claim"
    assert errors == []  # no duplicate-key error
    assert (results["first"], results["second"]) == (True, False)  # the first claim copied, the second found it
    assert series_of(run["run_id"]) == {series}
    with db.tx() as conn:
        got = db.one(conn, "SELECT count(*) AS n FROM lots WHERE run_id = :r AND world = 'shelfshift' AND branch = :b "
                           "AND sku = :s", r=run["run_id"], b=lot["branch"], s=lot["sku"])["n"]
    assert got == want > 0  # one copy, never doubled


def test_a_reset_of_an_owning_workspace_stays_within_2_s(run):
    """Edge DATA-15: a reset of a workspace that owns no series and of one that owns every series both finish inside
    the 2 s target on the synthetic template (the existing literal, never raised)."""
    with db.tx() as conn:
        db.run(conn, "INSERT INTO sessions (session_id, role, branch, run_id) VALUES ('overlay-2s', 'manager', NULL, "
                     ":r)", r=run["run_id"])
    started = time.perf_counter()
    owns_none, ms = db.in_tx(sandbox.reset, "overlay-2s")
    assert ms < RESET_TARGET_MS and time.perf_counter() - started < RESET_TARGET_MS / 1000
    with db.tx() as conn:
        assert overlay.materialize_all(conn, db.lock_run(conn, owns_none, mode="UPDATE")) > 0
    assert ("*", "*", "*") in series_of(owns_none)
    started = time.perf_counter()
    new, ms = db.in_tx(sandbox.reset, "overlay-2s")
    assert ms < RESET_TARGET_MS and time.perf_counter() - started < RESET_TARGET_MS / 1000
    assert new != owns_none and series_of(new) == set() and fresh(owns_none)["status"] == "RETIRED"


# --- overlay.lot_rows: lot's read rule for many lots at once (quick 261017-tuf) ----------------------------------------

def _lot_rows_match(run_id: str) -> dict:
    """overlay.lot_rows against overlay.lot for every shelfshift lot id the run or its template holds, plus an id no
    run has: asserts equal answers and returns {"own": n, "template": n, "none": n} as the run reads them."""
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        runs = [run_id, *([run["template_run_id"]] if run["template_run_id"] else [])]
        ids = sorted({r["lot_id"] for r in db.rows(conn, "SELECT lot_id FROM lots WHERE run_id = ANY(:r) "
                                                         "AND world = 'shelfshift'", r=runs)}) + ["no-such-lot"]
        got = overlay.lot_rows(conn, run, "shelfshift", ids)
        want = {i: overlay.lot(conn, run, "shelfshift", i) for i in ids}
    assert got == want
    assert got["no-such-lot"] is None
    return {"own": sum(1 for r in got.values() if r and r["run_id"] == run_id),
            "template": sum(1 for r in got.values() if r and r["run_id"] != run_id),
            "none": sum(1 for r in got.values() if r is None)}


def test_lot_rows_matches_lot(template, public_run):
    from tests.api.conftest import own_series, own_world
    seen = _lot_rows_match(template)  # a template reads its own rows
    assert seen["own"] > 0 and seen["template"] == 0
    bare = new_workspace()["run_id"]  # owns no series: every lot through the template
    seen = _lot_rows_match(bare)
    assert seen["own"] == 0 and seen["template"] > 0
    one = new_workspace()["run_id"]  # owns one series and deleted one of its own lots there (a gone lot)
    with db.tx() as conn:  # a series with two lots or more, so one stays after the delete
        series = db.one(conn, "SELECT branch, sku FROM lots WHERE run_id = :t AND world = 'shelfshift' GROUP BY "
                              "branch, sku HAVING count(*) >= 2 ORDER BY branch, sku LIMIT 1", t=template)
    own_series(one, series["branch"], series["sku"])
    with db.tx() as conn:
        gone = db.one(conn, "SELECT lot_id FROM lots WHERE run_id = :r AND world = 'shelfshift' AND branch = :b "
                            "AND sku = :s ORDER BY lot_id LIMIT 1", r=one, b=series["branch"], s=series["sku"])
        db.run(conn, "DELETE FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l", r=one,
               l=gone["lot_id"])
    seen = _lot_rows_match(one)
    assert seen["own"] > 0 and seen["template"] > 0 and seen["none"] >= 2  # the gone lot and the unknown id
    with db.tx() as conn:
        assert overlay.lot(conn, fresh(one), "shelfshift", gone["lot_id"]) is None
    every = new_workspace()["run_id"]  # after materialize_all it owns every series (synthetic only)
    own_world(every)
    seen = _lot_rows_match(every)
    assert seen["own"] > 0 and seen["template"] == 0
    with db.tx() as conn:  # a tiny public workspace
        pub = sandbox.clone(conn, public_run)
    assert refdata.get(fresh(pub)["dataset_version"]).kind == "public"
    seen = _lot_rows_match(pub)
    assert seen["template"] > 0
