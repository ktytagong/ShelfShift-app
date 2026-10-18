"""The case's transport details and the Branch transfers list, read-only (06.1 D-17, D-30, D-33, D-52, D-55;
04-API-CONTRACT "Product mode (06.1)", Read views).

The story transfer (SUS-19, the milk-story tag) is driven to PREPARED at service level in the HTTP client's own
workspace (ownership set in the database, then send, accept, approve and prepare as the routes make them). The
dispatch, receipt and discrepancy rows are then written straight into the test database in the shape the contract's
Execution rows define: a documented test shortcut (06.1-12 execution rules), so these reads are tested without the
dispatch plan beside them; 06.1-31's journey proves the real flow.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import cases, db, inputs, refdata, workflow, worker
from api.app import app
from engine.model import KEEP, TRANSFER
from tests.api.conftest import HQ, fresh, seat, sign_in
from tests.api.verify_helpers import ack, shown  # approve and accept acknowledge (D-26) and name the plan (07 review C)

STORY = cases.STORY_KEY
KEYS = ("pack_from", "ready", "loading_cutoff", "departure", "arrival", "received")


# --- helpers ---------------------------------------------------------------------------------------------------------

def call(run_id: str, fn, *args):
    """One service call in its own transaction after the run lock, as a route makes it."""
    return db.in_tx(lambda conn: fn(conn, db.lock_run(conn, run_id), *args))


def scratch(run_id: str, sql: str, **params) -> None:
    """A test-only write in the test's own workspace."""
    with db.tx() as conn:
        db.run(conn, sql, r=run_id, **params)


def case_of(run_id: str, case_id: str = STORY) -> dict:
    with db.tx() as conn:
        return cases.case_row(conn, run_id, case_id)


def plan_of(run_id: str, case_id: str = STORY):
    with db.tx() as conn:
        return cases.parse_plan(cases.active_rec(conn, cases.case_row(conn, run_id, case_id))["result"]["recommended"])


def prepare_story(run_id: str) -> dict:
    """SUS-19 owned by its source, sent, accepted, approved and prepared: its TRANSFER work order row."""
    case, plan = case_of(run_id), plan_of(run_id)
    assert plan.kind == TRANSFER, "D-15 hand-back: SUS-19's pick is not a transfer"
    src, dest = seat(case["source_branch"]), seat(plan.dest)
    scratch(run_id, "UPDATE cases SET owner_taken_by = :o, owner_taken_at = now() WHERE run_id = :r AND case_id = :c",
            o=case["source_branch"], c=STORY)
    v = case["active_rec_version"]
    call(run_id, workflow.send, src, STORY, v)
    call(run_id, workflow.decide, dest, STORY, v, "accept", None, ack(run_id, STORY), shown(run_id, STORY))
    assert call(run_id, workflow.decide, src, STORY, v, "approve", None, ack(run_id, STORY),
                shown(run_id, STORY))["state"] == "APPROVED"
    assert call(run_id, workflow.prepare, src, STORY) == {"state": "PREPARED"}
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                            "AND kind = 'TRANSFER'", r=run_id, c=STORY)


def session(account: str | None = None) -> tuple[TestClient, str]:
    """A new signed-in client with its own workspace (as `account`, default the start store): (client, run_id)."""
    c = TestClient(app)
    sign_in(c)
    if account is not None:
        sign_in(c, account)  # rotates the session, keeps the workspace
    return c, c.get("/api/session").json()["run_id"]


def as_account(c: TestClient, account: str) -> TestClient:
    sign_in(c, account)
    return c


def movement_row(run_id: str, action: dict, movement_id: str, kind: str, qty: float, damaged: float = 0.0,
                 by: str | None = None) -> None:
    """A DISPATCH or RECEIPT movement as 04-API-CONTRACT's Execution rows record one (test shortcut)."""
    scratch(run_id, "INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, lot_id, qty, damaged_qty, "
                    "condition, recorded_by, role, branch, scenario_day) VALUES (:r, :m, :c, :a, :k, :l, :q, :d, "
                    "'as_expected', :b, 'manager', :b, (SELECT day FROM demo_runs WHERE run_id = :r))",
            m=movement_id, c=action["case_id"], a=action["action_id"], k=kind,
            l=action["lot_id"] if kind == "DISPATCH" else action["new_lot_id"], q=float(qty), d=float(damaged),
            b=by or (action["origin"] if kind == "DISPATCH" else action["dest"]))


def set_action(run_id: str, action_id: str, state: str) -> None:
    scratch(run_id, "UPDATE world_actions SET state = :s WHERE run_id = :r AND world = 'shelfshift' AND action_id = :a",
            s=state, a=action_id)


def dispatch(run_id: str, action: dict, qty: float) -> None:
    """The source's recorded dispatch: the work order DISPATCHED and a DISPATCH movement (test shortcut)."""
    set_action(run_id, action["action_id"], "DISPATCHED")
    movement_row(run_id, action, f"{action['action_id']}:D", "DISPATCH", qty)


def receive(run_id: str, action: dict, qty: float, short: float = 0.0) -> None:
    """The destination's recorded receipt: RECEIPT movement, an OPEN SHORT discrepancy for `short` units, the work
    order RECEIVED and the case EXECUTED (test shortcut)."""
    movement_row(run_id, action, f"{action['action_id']}:R", "RECEIPT", qty)
    if short:
        scratch(run_id, "INSERT INTO discrepancies (run_id, discrepancy_id, case_id, action_id, kind, units, "
                        "open_units, state) VALUES (:r, :i, :c, :a, 'SHORT', :u, :u, 'OPEN')",
                i=f"{action['action_id']}:SHORT", c=action["case_id"], a=action["action_id"], u=float(short))
    set_action(run_id, action["action_id"], "RECEIVED")
    scratch(run_id, "UPDATE cases SET state = 'EXECUTED' WHERE run_id = :r AND case_id = :c", c=action["case_id"])


def transport(c: TestClient, case_id: str = STORY) -> dict:
    r = c.get(f"/api/cases/{case_id}/transport")
    assert r.status_code == 200, r.json()
    return r.json()


def by_key(view: dict) -> dict:
    return {m["key"]: m for m in view["milestones"]}


@pytest.fixture
def no_worker(monkeypatch):
    """Both reads never call the engine worker (OPS-06)."""
    monkeypatch.setattr(worker, "run", lambda *a, **k: pytest.fail("a read called the engine worker"))


# --- Task 1: the transport panel -------------------------------------------------------------------------------------

def test_the_prepared_story_transfer_shows_its_run_milestones_and_a_planned_movement(template, no_worker):
    c, run_id = session()
    action = prepare_story(run_id)
    as_account(c, action["origin"])
    view = transport(c)
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    trip = next(r for r in ref.runs_from(action["origin"], action["run_day"])
                if r.run_code == action["run_code"] and r.dest == action["dest"])
    committed = float(action["qty"])
    with db.tx() as conn:
        packing = inputs.sandbox_policy(conn, run, ref).packing_min
    # the run: the reference timetable; the quote = the work order's fixed cost + per-unit cost x committed units
    assert view["run"] == {
        "code": trip.run_code, "day": trip.day, "loading_cutoff_min": trip.loading_cutoff_min,
        "departs_min": trip.departs_min, "arrives_min": trip.arrives_min, "cold_chain": trip.cold_chain,
        "spare_totes": trip.spare_totes,
        "quote": round(action["fixed_cost"] + action["per_unit_cost"] * committed, 2)}
    assert (view["source"], view["destination"], view["packing_min"]) == (action["origin"], action["dest"], packing)
    ms = by_key(view)
    assert [m["key"] for m in view["milestones"]] == list(KEYS)
    # pack from = loading cutoff - packing time, as an absolute minute of the run's day (may fall on the day before)
    assert ms["pack_from"]["day"] * 1440 + ms["pack_from"]["min"] == trip.day * 1440 + trip.loading_cutoff_min - packing
    assert (ms["loading_cutoff"]["day"], ms["loading_cutoff"]["min"]) == (trip.day, trip.loading_cutoff_min)
    assert (ms["departure"]["day"], ms["departure"]["min"]) == (trip.day, trip.departs_min)
    assert ms["arrival"]["day"] * 1440 + ms["arrival"]["min"] == trip.day * 1440 + trip.arrives_min
    assert 0 <= ms["arrival"]["min"] < 1440
    for k in ("ready", "received"):  # nothing records them yet: never a guessed time
        assert (ms[k]["day"], ms[k]["min"], ms[k]["recorded_at"]) == (None, None, None)
    assert all(m["recorded_at"] is None for m in view["milestones"])
    assert view["decision"] == {"state": "PREPARED", "step": "EXECUTE"}
    assert view["movement"] == {"state": "planned", "committed_qty": committed, "dispatched_qty": None,
                                "received_qty": None, "damaged_qty": None, "dispatched_at": None, "received_at": None}
    assert view["assumed"] == {"carrier": "own_fleet", "coordinator": "represented"}


def test_dispatch_and_a_short_receipt_are_shown_as_recorded(template, no_worker):
    c, run_id = session()
    action = prepare_story(run_id)
    committed = float(action["qty"])
    sent = committed - 6  # the source sends 6 fewer than committed
    dispatch(run_id, action, sent)
    view = transport(c)
    assert view["movement"]["state"] == "dispatched"
    assert view["movement"]["dispatched_qty"] == sent and view["movement"]["committed_qty"] == committed
    assert view["movement"]["dispatched_at"] is not None and view["movement"]["received_at"] is None
    assert view["decision"]["state"] == "PREPARED"  # a dispatch is a movement, never a decision (D-33)
    assert by_key(view)["received"]["recorded_at"] is None
    receive(run_id, action, sent - 4, short=4)  # 4 of the dispatched units missing: an OPEN SHORT discrepancy
    view = transport(c)
    assert view["movement"]["state"] == "discrepancy"
    assert (view["movement"]["received_qty"], view["movement"]["damaged_qty"]) == (sent - 4, 0.0)
    assert view["movement"]["received_at"] is not None
    received = by_key(view)["received"]
    assert received["recorded_at"] == view["movement"]["received_at"]
    assert received["day"] == fresh(run_id)["day"] and received["min"] is None  # recorded, never a planned minute
    assert view["decision"] == {"state": "EXECUTED", "step": "EXECUTE"}  # Execute until reconciled (D-65)


def test_a_keep_case_has_no_run_and_an_unknown_case_is_not_found(template, no_worker):
    c, run_id = session()
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND active_rec_version IS NOT NULL "
                             "ORDER BY case_id", r=run_id)
        keep = next((r for r in rows
                     if cases.parse_plan(cases.active_rec(conn, r)["result"]["recommended"]).kind == KEEP), None)
    assert keep is not None, "the synthetic template has a case whose recommendation is Keep"
    view = transport(c, keep["case_id"])
    assert view["run"] is None and view["destination"] is None and view["milestones"] == []
    assert view["source"] == keep["source_branch"]
    assert view["movement"]["state"] is None and view["movement"]["committed_qty"] is None
    r = c.get("/api/cases/no-such-case/transport")
    assert (r.status_code, r.json()["error"]) == (404, "not_found")


# --- Task 2: the Branch transfers list -------------------------------------------------------------------------------

def other_cases(run_id: str, n: int) -> list[dict]:
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id <> :c ORDER BY case_id",
                       r=run_id, c=STORY)
    assert len(rows) >= n, "the synthetic template has enough other cases for the test's work orders"
    return rows[:n]


def add_work_order(run_id: str, like: dict, case: dict, action_id: str, origin: str, dest: str) -> None:
    """A TRANSFER work order for another case, on the story's run and costs (test shortcut: Prepare's row shape)."""
    scratch(run_id, "INSERT INTO world_actions (run_id, world, action_id, case_id, kind, lot_id, sku, qty, dest, "
                    "run_code, run_day, fixed_cost, per_unit_cost, new_lot_id, origin) VALUES (:r, 'shelfshift', :a, "
                    ":c, 'TRANSFER', :l, :s, 5, :d, :rc, :rd, :fc, :pu, :nl, :o)",
            a=action_id, c=case["case_id"], l=case["lot_id"], s=case["sku"], d=dest, rc=like["run_code"],
            rd=like["run_day"], fc=like["fixed_cost"], pu=like["per_unit_cost"], nl=f"{action_id}>new", o=origin)


def listing(c: TestClient, **params) -> dict:
    r = c.get("/api/transfers", params=params)
    assert r.status_code == 200, r.json()
    return r.json()


ITEM_KEYS = {"action_id", "case_id", "key", "sku", "lot_id", "new_lot_id", "source", "dest", "committed_qty",
             "dispatched_qty", "received_qty", "damaged_qty", "run_code", "run_day", "departs_min", "arrives_min",
             "decision_state", "movement_state", "next_action"}


def test_each_store_sees_its_outgoing_or_incoming_transfer_and_a_third_store_none(template, no_worker):
    c, run_id = session()
    action = prepare_story(run_id)
    source, dest = action["origin"], action["dest"]
    out = listing(as_account(c, source))  # direction defaults to outgoing
    assert out["total"] == 1 and [i["action_id"] for i in out["items"]] == [action["action_id"]]
    item = out["items"][0]
    assert set(item) == ITEM_KEYS
    assert (item["key"], item["source"], item["dest"], item["committed_qty"]) == (STORY, source, dest,
                                                                                  float(action["qty"]))
    assert (item["dispatched_qty"], item["received_qty"], item["damaged_qty"]) == (None, None, None)
    assert (item["decision_state"], item["movement_state"]) == ("PREPARED", "planned")
    assert item["next_action"] == {"key": "record_dispatch", "who": [{"role": "manager", "branch": source}]}
    assert listing(c, direction="incoming")["total"] == 0  # the source receives nothing
    inc = listing(as_account(c, dest), direction="incoming")
    assert [i["action_id"] for i in inc["items"]] == [action["action_id"]]
    assert listing(c)["total"] == 0  # the destination sends nothing
    dispatch(run_id, action, float(action["qty"]))
    item = listing(c, direction="incoming")["items"][0]
    assert (item["movement_state"], item["dispatched_qty"]) == ("dispatched", float(action["qty"]))
    assert item["decision_state"] == "PREPARED"  # decision and movement stay apart (D-33)
    assert item["next_action"] == {"key": "record_receipt", "who": [{"role": "manager", "branch": dest}]}
    third = next(b for b in refdata.get(fresh(run_id)["dataset_version"]).branches if b not in (source, dest))
    for direction in ("outgoing", "incoming", "all"):
        assert listing(as_account(c, third), direction=direction) == {
            "items": [], "page": 1, "page_size": 1, "total": 0, "scope": {"branch": third, "direction": direction}}


def test_a_manager_sees_only_their_store_and_hq_may_name_one(template, no_worker):
    c, run_id = session()
    action = prepare_story(run_id)
    source, dest = action["origin"], action["dest"]
    back = other_cases(run_id, 1)[0]
    add_work_order(run_id, action, back, "test#1:transfer", dest, source)  # an incoming transfer for the source
    as_account(c, dest)
    mine = listing(c, branch=source, direction="all")  # another store named: ignored, the manager's own is used
    assert mine["scope"] == {"branch": dest, "direction": "all"}
    assert {i["action_id"] for i in mine["items"]} == {action["action_id"], "test#1:transfer"}
    assert [i["action_id"] for i in listing(c, branch=source)["items"]] == ["test#1:transfer"]  # dest's outgoing
    as_account(c, "HQ")
    both = listing(c, branch=source, direction="all")
    assert both["scope"] == {"branch": source, "direction": "all"}
    assert {(i["action_id"], i["source"], i["dest"]) for i in both["items"]} == {
        (action["action_id"], source, dest), ("test#1:transfer", dest, source)}
    assert [i["action_id"] for i in listing(c, branch=source)["items"]] == [action["action_id"]]
    assert listing(c)["total"] == 2 and listing(c)["scope"] == {"branch": None, "direction": "all"}
    r = c.get("/api/transfers", params={"branch": "NO-SUCH-STORE"})
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")


def test_state_filters_on_the_server_and_pages_walk_the_list_once(template, no_worker):
    c, run_id = session()
    action = prepare_story(run_id)
    source, dest = action["origin"], action["dest"]
    for n, case in enumerate(other_cases(run_id, 2), 1):
        add_work_order(run_id, action, case, f"test#{n}:transfer", source, dest)
    as_account(c, source)
    everything = listing(c)
    assert everything["total"] == 3
    # one stable order: run day, departure minute, action id (the three share the story's run)
    assert [i["action_id"] for i in everything["items"]] == sorted(i["action_id"] for i in everything["items"])
    dispatch(run_id, action, float(action["qty"]))
    shipped = listing(c, state="dispatched")
    assert [i["action_id"] for i in shipped["items"]] == [action["action_id"]] and shipped["total"] == 1
    assert [i["movement_state"] for i in listing(c, state="planned")["items"]] == ["planned", "planned"]
    for bad in ({"state": "lost"}, {"direction": "sideways"}, {"page": "0"}, {"page_size": "0"}):
        r = c.get("/api/transfers", params=bad)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), bad
    order = [i["action_id"] for i in listing(c)["items"]]
    walked = []
    for page in (1, 2, 3):
        p = listing(c, page=page, page_size=1)
        assert (p["page"], p["page_size"], p["total"]) == (page, 1, 3)
        walked += [i["action_id"] for i in p["items"]]
    assert walked == order  # every transfer once, in the server's order
    assert listing(c, page=4, page_size=1)["items"] == []
