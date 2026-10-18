"""Send, decide and commit with all-or-nothing holds, expiry, roles and versions (spec §8; FLOW-03/04/05/06/10).

Cases are found by tag or by their engine pick (`case_with_pick`): SUS-19 (the milk-story tag) for a transfer, else
the first open case whose pick has the kind, else a synthetic lot and case inserted into the cloned sandbox. Branches,
runs and products come from the seed through the sandbox; no test names another case key, a seed quantity or an
amount. Quantities a test inserts itself (another case's bookings that fill a resource) are derived from the input
the engine sees. Hold expiry is tested by moving expires_at, never by sleeping.
"""
import math
import threading
from dataclasses import replace

import pytest

from api import advance, cases, config, db, inputs, refdata, workflow
from api.errors import ApiError
from engine.adjust import effective
from engine.limits import CAP_REASON, PRICE_BOOKED, transfer_caps, unmet_by_day
from engine.model import KEEP, MARKDOWN, TRANSFER, totals
from engine.simulate import HorizonError
from engine.timing import first_sellable_day
from tests.api.conftest import by_tag, fresh, own_series, owned_send, seat, take, world_lot
from tests.api.verify_helpers import ack, shown  # approve and accept acknowledge (D-26) and name the plan (07 review C)

STORY = cases.STORY_KEY
TRANSFER_HOLDS = {"DEMAND", "STORAGE", "TRUCK", "SOURCE_LOT"}  # spec §8 Lifecycle: a transfer with a hold rest


def call(run, fn, *args):
    """One service call in its own transaction after the run lock, as a route makes it."""
    return db.in_tx(lambda conn: fn(conn, db.lock_run(conn, run["run_id"]), *args))


def version(run, case_id):
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)["active_rec_version"]


def state(run, case_id):
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)["state"]


def holds(run, case_id, states=("HELD",)):
    """(kind, bucket) of the case's bookings in these states; a hold at or past its expiry never counts."""
    with db.tx() as conn:
        return sorted((r["kind"], r["bucket"]) for r in db.rows(
            conn, "SELECT DISTINCT kind, bucket FROM reservations WHERE run_id = :r AND case_id = :c "
                  "AND state = ANY(:s) AND (state <> 'HELD' OR expires_at > now())",
            r=run["run_id"], c=case_id, s=list(states)))


def pick(run, case_id):
    with db.tx() as conn:
        rec = cases.active_rec(conn, cases.case_row(conn, run["run_id"], case_id))
    return cases.parse_plan(rec["result"]["recommended"])


def eff_input(run, case_id):
    """The case's effective input today without its own bookings, as the recheck reads it."""
    with db.tx() as conn:
        r = fresh(run["run_id"])
        return effective(cases.current_input(conn, r, cases.case_row(conn, r["run_id"], case_id)))


def source_of(run, case_id):
    with db.tx() as conn:
        return seat(cases.case_row(conn, run["run_id"], case_id)["source_branch"])


def add_booking(run_id, case_id, kind, bucket, day, qty, state="COMMITTED"):
    """Another case's booking, written directly (a test value, not a seed value)."""
    with db.tx() as conn:
        n = db.one(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = :r", r=run_id)["n"]
        db.run(conn, "INSERT INTO reservations (run_id, res_id, case_id, round, kind, bucket, day, qty, state, "
                     "expires_at) VALUES (:r, :id, :c, 1, :k, :b, :d, :q, :s, NULL)",
               r=run_id, id=f"test-{n}", c=case_id, k=kind, b=bucket, d=day, q=qty, s=state)


def insert_case(run, lot: dict, case_id: str) -> dict:
    """A synthetic lot (record and physical alike) and an OPEN case on it, evaluated by the engine."""
    own_series(run, lot["branch"], lot["sku"])  # the test writes the series directly
    with db.tx() as conn:
        r = fresh(run["run_id"])
        db.run(conn, "INSERT INTO lots (run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, markdown_pct, "
                     "expiry_source, last_verified_day, verified_by) VALUES (:r, 'shelfshift', :l, :b, :s, :q, :e, "
                     ":seq, :pct, :es, :lv, 'COUNT')", r=r["run_id"], l=lot["lot_id"], b=lot["branch"], s=lot["sku"],
               q=lot["qty"], e=lot["expiry_day"], seq=lot["received_seq"], pct=lot["markdown_pct"],
               es=lot["expiry_source"], lv=r["day"])
        db.run(conn, "INSERT INTO truth_lots (run_id, world, lot_id, qty) VALUES (:r, 'shelfshift', :l, :q)",
               r=r["run_id"], l=lot["lot_id"], q=lot["qty"])
        db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, state, opened_day) VALUES "
                     "(:r, :c, :c, :l, :b, :s, 'OPEN', :d)", r=r["run_id"], c=case_id, l=lot["lot_id"],
               b=lot["branch"], s=lot["sku"], d=r["day"])
        cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], case_id))
        return cases.case_row(conn, r["run_id"], case_id)


def case_with_pick(run, kind) -> dict:
    """A case whose engine pick has this kind (research Pitfall 5): SUS-19 for a transfer; else the first open case
    (by lot id) with that pick; else a synthetic one. The pick is asserted before the case is returned."""
    if kind == TRANSFER:
        case = by_tag(run, cases.STORY_TAG)
        assert pick(run, case["case_id"]).kind == TRANSFER, "D-15 hand-back: SUS-19's pick is not a transfer"
        return case
    with db.tx() as conn:
        for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' AND tag IS DISTINCT FROM :t "
                               "ORDER BY lot_id", r=run["run_id"], t=cases.STORY_TAG):
            rec = cases.active_rec(conn, c)
            if rec and cases.parse_plan(rec["result"]["recommended"]).kind == kind:
                return c
    ref = refdata.get(run["dataset_version"])
    story = by_tag(run, cases.STORY_TAG)
    base = world_lot(run, story["lot_id"])  # as the workspace reads it (api/overlay.py)
    if kind == KEEP:  # one unit expiring on the last demo day: nothing worth moving
        lot = {**base, "lot_id": "TEST-KEEP", "qty": 1.0, "expiry_day": ref.calendar["last_demo_day"]}
    else:  # a large lot at a branch with no run today, expiring two days after the run day
        idle = next(b for b in ref.branches if not any(r.origin == b and r.day == run["day"] for r in ref.runs))
        lot = {**base, "lot_id": "TEST-MD", "branch": idle, "expiry_day": run["day"] + 2}
    case = insert_case(run, lot, f"TEST-{kind}")
    assert pick(run, case["case_id"]).kind == kind
    return case


def approve_transfer(run, case_id=STORY):
    """Send, accept and approve a transfer case on its active version."""
    src, dest, v = source_of(run, case_id), seat(pick(run, case_id).dest), version(run, case_id)
    owned_send(run, src, case_id, v)
    call(run, workflow.decide, dest, case_id, v, "accept", None, ack(run, case_id), shown(run, case_id))
    return call(run, workflow.decide, src, case_id, v, "approve", None, ack(run, case_id), shown(run, case_id))


def held_kinds(plan):
    return TRANSFER_HOLDS | ({"LOT_PRICE"} if plan.pct else set())


def test_a_transfer_holds_every_resource_at_send_and_commits_after_both_sides(run):
    case = case_with_pick(run, TRANSFER)
    cid, plan = case["case_id"], pick(run, STORY)
    src, dest = seat(case["source_branch"]), seat(plan.dest)
    v = version(run, cid)
    assert owned_send(run, src, cid, v) == {"state": "PENDING_APPROVAL", "round": 1}
    assert {k for k, _ in holds(run, cid)} == held_kinds(plan)  # every resource the plan's rest needs, at send
    assert call(run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), shown(run, cid)) == \
        {"state": "PENDING_APPROVAL"}
    assert holds(run, cid, ("COMMITTED", "CONVERTED")) == []  # one side is not enough
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "APPROVED"}
    assert state(run, cid) == "APPROVED" and holds(run, cid) == []  # nothing is still HELD
    assert [k for k, _ in holds(run, cid, ("CONVERTED",))] == ["DEMAND"]
    assert {k for k, _ in holds(run, cid, ("COMMITTED",))} == held_kinds(plan) - {"DEMAND"}
    with db.tx() as conn:
        inbound = db.rows(conn, "SELECT * FROM inbound WHERE run_id = :r AND source_case_id = :c",
                          r=run["run_id"], c=cid)
        log = [a["type"] for a in db.rows(conn, "SELECT type FROM activity_log WHERE run_id = :r AND case_id = :c "
                                                "AND seq > (SELECT max(seq) FROM activity_log WHERE run_id = :r "
                                                "AND case_id = :c AND type = 'opened') ORDER BY seq",
                                          r=run["run_id"], c=cid)]
    assert len(inbound) == 1
    moved = inbound[0]
    assert (moved["branch"], moved["origin"], moved["qty"]) == (plan.dest, case["source_branch"], plan.qty)
    assert moved["inbound_id"] == f"{cid}>{plan.dest}#1"
    assert log[-4:] == ["sent", "decision", "decision", "committed"]


def test_either_order_of_decisions_commits(run):
    case = case_with_pick(run, TRANSFER)
    cid, plan = case["case_id"], pick(run, STORY)
    src, dest = seat(case["source_branch"]), seat(plan.dest)
    v = version(run, cid)
    owned_send(run, src, cid, v)
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "PENDING_APPROVAL"}
    with db.tx() as conn:  # the destination is the one still to act
        card = next(c for c in cases.board(conn, fresh(run["run_id"])) if c["case_id"] == cid)
    assert card["next_owner"] == [{"role": "manager", "branch": plan.dest}]
    assert call(run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), shown(run, cid)) == \
        {"state": "APPROVED"}
    assert [k for k, _ in holds(run, cid, ("CONVERTED",))] == ["DEMAND"]


def test_approval_passes_with_all_the_demand_held_by_this_case(run):
    """FLOW-03 adjacency: this case's own holds take exactly all of the destination's unmet demand on the run, and
    the commit (which leaves this case's own bookings out) still passes."""
    case = case_with_pick(run, TRANSFER)
    cid, plan = case["case_id"], pick(run, STORY)
    eff = eff_input(run, cid)
    trip = next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest)
    caps, _ = transfer_caps(eff, trip)
    if caps["demand"] > plan.qty:  # another case takes the difference, so this plan needs all that is left
        lost = unmet_by_day(eff, plan.dest)
        held = totals((d, u) for b, d, u in eff.bookings.demand if b == plan.dest)
        extra = caps["demand"] - plan.qty
        for d in range(first_sellable_day(trip.day, trip.arrives_min, eff.policy), eff.case_lot().expiry_day + 1):
            take = min(extra, max(0.0, lost.get(d, 0.0) - held.get(d, 0.0)))
            if take > 0:
                add_booking(run["run_id"], "OTHER", "DEMAND", f"{plan.dest}|{eff.product.sku}", d, take)
                extra -= take
        caps, _ = transfer_caps(eff_input(run, cid), trip)
    assert caps["demand"] == plan.qty  # the edge: available demand equals the plan's quantity exactly
    owned_send(run, seat(case["source_branch"]), cid, version(run, cid))
    with db.tx() as conn:
        held_units = db.one(conn, "SELECT sum(qty) AS q FROM reservations WHERE run_id = :r AND case_id = :c "
                                  "AND kind = 'DEMAND' AND state = 'HELD'", r=run["run_id"], c=cid)["q"]
    assert math.isclose(held_units, plan.qty, abs_tol=1e-6)
    # its own holds are left out of its own availability, so the recheck at commit still sees all of it
    assert transfer_caps(eff_input(run, cid), trip)[0]["demand"] == plan.qty
    v = version(run, cid)
    call(run, workflow.decide, seat(plan.dest), cid, v, "accept", None, ack(run, cid), shown(run, cid))
    assert call(run, workflow.decide, seat(case["source_branch"]), cid, v, "approve", None, ack(run, cid),
                shown(run, cid)) == {"state": "APPROVED"}


# --- conflicts, competing sends, keep and markdown, roles, versions, expiry and shipment checks (Task 2) ------------

def lot_row(run, lot_id):
    return world_lot(run, lot_id)  # as the workspace reads it (api/overlay.py)


def competing_case(run) -> dict:
    """A twin of the story lot at its branch (same SKU, record quantity and expiry): its own engine pick also wants
    the story transfer's destination demand on the same run (asserted), so the two compete for one resource."""
    story = case_with_pick(run, TRANSFER)
    base = lot_row(run, story["lot_id"])
    twin = insert_case(run, {**base, "lot_id": f"{base['lot_id']}-TWIN", "received_seq": base["received_seq"] + 1},
                       "TEST-TWIN")
    mine, theirs = pick(run, STORY), pick(run, twin["case_id"])
    assert theirs.kind == TRANSFER and (theirs.dest, theirs.run_code) == (mine.dest, mine.run_code), theirs
    return twin


def raises(fn, *args) -> ApiError:
    with pytest.raises(ApiError) as e:
        fn(*args)
    return e.value


def booked_rows(run, case_id):
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM reservations WHERE run_id = :r AND case_id = :c ORDER BY res_id",
                       r=run["run_id"], c=case_id)


def logged(run, case_id, type_):
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM activity_log WHERE run_id = :r AND case_id = :c AND type = :t ORDER BY seq",
                       r=run["run_id"], c=case_id, t=type_)


def run_out_the_clock(run):
    """Every hold's hour is up (set directly, so a busy machine can't make a test flaky)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE reservations SET expires_at = now() - interval '1 second' WHERE run_id = :r "
                     "AND state = 'HELD'", r=run["run_id"])


def other_branch(run, *not_these):
    return next(b for b in refdata.get(run["dataset_version"]).branches if b not in not_these)


def test_a_second_case_asking_for_the_same_demand_fails(run):
    twin = competing_case(run)
    tid, src = twin["case_id"], seat(twin["source_branch"])
    owned_send(run, src, STORY, version(run, STORY))
    e = raises(owned_send, run, src, tid, version(run, tid))
    assert (e.status, e.code, e.message) == (409, "conflict", "This plan can't go ahead now; see the reasons.")
    assert CAP_REASON["demand"] in e.extra["reasons"]  # the engine's own wording
    demand = next(r for r in e.extra["resources"] if r["kind"] == "DEMAND")
    assert demand["available"] < demand["needed"]
    assert booked_rows(run, tid) == [] and state(run, tid) == "OPEN"
    # once the story transfer is committed it is counted once, as an inbound delivery: still no demand left
    v = version(run, STORY)
    call(run, workflow.decide, seat(pick(run, STORY).dest), STORY, v, "accept", None, ack(run, STORY),
         shown(run, STORY))
    call(run, workflow.decide, src, STORY, v, "approve", None, ack(run, STORY), shown(run, STORY))
    e = raises(owned_send, run, src, tid, version(run, tid))
    assert e.code == "conflict" and CAP_REASON["demand"] in e.extra["reasons"] and booked_rows(run, tid) == []


def test_competing_sends_exactly_one_holds(run):
    twin = competing_case(run)
    plan = pick(run, STORY)
    eff = eff_input(run, STORY)
    cap = transfer_caps(eff, next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest))[0]
    src = seat(twin["source_branch"])
    todo = {STORY: version(run, STORY), twin["case_id"]: version(run, twin["case_id"])}
    out = {}
    start = threading.Barrier(len(todo))
    for case_id in todo:  # owned first, so the threads race on the send alone (06.1 D-54)
        take(run, src, case_id)

    def go(case_id, v):
        start.wait()
        try:
            out[case_id] = call(run, workflow.send, src, case_id, v)["state"]
        except ApiError as e:
            out[case_id] = e.code

    threads = [threading.Thread(target=go, args=item) for item in todo.items()]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out.values()) == ["PENDING_APPROVAL", "conflict"], out
    with db.tx() as conn:
        held = db.rows(conn, "SELECT case_id, sum(qty) AS q FROM reservations WHERE run_id = :r AND kind = 'DEMAND' "
                             "AND state = 'HELD' GROUP BY case_id", r=run["run_id"])
    assert len(held) == 1 and out[held[0]["case_id"]] == "PENDING_APPROVAL"
    assert held[0]["q"] <= cap["demand"] + 1e-6  # nothing over-booked


def test_send_holds_everything_or_nothing(run):
    case = case_with_pick(run, TRANSFER)
    plan, eff = pick(run, STORY), eff_input(run, STORY)
    trip = next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest)
    taken = sum(t for code, t in eff.bookings.run_totes if code == trip.run_code)
    add_booking(run["run_id"], "OTHER", "TRUCK", f"{trip.run_code}|{trip.day}", trip.day, trip.spare_totes - taken)
    e = raises(owned_send, run, seat(case["source_branch"]), STORY, version(run, STORY))
    assert e.code == "conflict" and CAP_REASON["truck"] in e.extra["reasons"]
    assert booked_rows(run, STORY) == []  # the other resources were free, yet none of them was held
    with db.tx() as conn:
        row = cases.case_row(conn, run["run_id"], STORY)
    assert (row["state"], row["round"]) == ("OPEN", 0) and logged(run, STORY, "sent") == []


def test_keep_holds_nothing_and_goes_straight_to_executed(run):
    case = case_with_pick(run, KEEP)
    cid, src = case["case_id"], seat(case["source_branch"])
    v = version(run, cid)
    with db.tx() as conn:
        approvals = cases.detail(conn, fresh(run["run_id"]), cid)["approvals"]
    assert approvals["required"] == ["source"] and approvals["resources"] == []
    assert owned_send(run, src, cid, v) == {"state": "PENDING_APPROVAL", "round": 1}
    assert booked_rows(run, cid) == []
    assert call(run, workflow.decide, src, cid, v, "approve", None, None, shown(run, cid)) == {"state": "EXECUTED"}
    assert state(run, cid) == "EXECUTED"


def test_markdown_takes_the_lot_price_right(run):
    case = case_with_pick(run, MARKDOWN)
    cid, lot, src = case["case_id"], case["lot_id"], seat(case["source_branch"])
    v = version(run, cid)
    with db.tx() as conn:
        resources = cases.detail(conn, fresh(run["run_id"]), cid)["approvals"]["resources"]
    assert [(r["kind"], r["needed"], r["available"], r["unit"]) for r in resources] == [("LOT_PRICE", 1, 1, "right")]
    owned_send(run, src, cid, v)
    assert holds(run, cid) == [("LOT_PRICE", lot)]
    assert call(run, workflow.decide, src, cid, v, "approve", None, None, shown(run, cid)) == \
        {"state": "APPROVED"}  # the source alone
    assert holds(run, cid, ("COMMITTED",)) == [("LOT_PRICE", lot)] and holds(run, cid) == []


def test_two_cases_never_take_the_price_of_one_lot(run):
    case = case_with_pick(run, MARKDOWN)
    cid, lot = case["case_id"], case["lot_id"]
    add_booking(run["run_id"], "OTHER", "LOT_PRICE", lot, None, 1)
    e = raises(owned_send, run, seat(case["source_branch"]), cid, version(run, cid))
    assert e.code == "conflict" and PRICE_BOOKED in e.extra["reasons"]
    assert [(r["kind"], r["available"]) for r in e.extra["resources"]] == [("LOT_PRICE", 0)]
    assert booked_rows(run, cid) == []


def test_a_stale_version_is_rejected(run):
    case = case_with_pick(run, TRANSFER)
    src, v = seat(case["source_branch"]), version(run, STORY)
    e = raises(owned_send, run, src, STORY, "0" * len(v))
    assert (e.status, e.code) == (409, "stale_version")
    assert e.message == "This recommendation was replaced by a newer version. Reload the stock decision."  # D-61
    assert e.extra == {"active_rec_version": v}
    owned_send(run, src, STORY, v)
    e = raises(call, run, workflow.decide, src, STORY, "0" * len(v), "approve")
    assert e.code == "stale_version" and e.extra == {"active_rec_version": v}


def test_the_destination_cannot_approve_as_the_source(run):
    case = case_with_pick(run, TRANSFER)
    src, dest, v = seat(case["source_branch"]), seat(pick(run, STORY).dest), version(run, STORY)
    owned_send(run, src, STORY, v)
    e = raises(call, run, workflow.decide, dest, STORY, v, "approve")
    assert (e.status, e.code) == (403, "forbidden") and e.message == f"Only the {case['source_branch']} manager can do this"
    e = raises(call, run, workflow.decide, src, STORY, v, "accept")  # nor the source accept for the destination
    assert e.status == 403 and e.message == "Only the destination manager can accept or decline this transfer"
    with db.tx() as conn:
        assert cases.decided_roles(conn, cases.case_row(conn, run["run_id"], STORY)) == {}


def test_a_manager_cannot_act_on_another_branchs_case(run):
    case = case_with_pick(run, TRANSFER)
    plan, v = pick(run, STORY), version(run, STORY)
    stranger = seat(other_branch(run, case["source_branch"], plan.dest))
    assert raises(take, run, stranger, STORY).status == 403  # only the source store or HQ takes it (06.1 D-67)
    take(run, seat(case["source_branch"]), STORY)
    e = raises(call, run, workflow.send, stranger, STORY, v)  # only the owner sends (06.1 D-54)
    assert (e.status, e.message) == (403, "Only the owner of this stock decision can send it.")
    owned_send(run, seat(case["source_branch"]), STORY, v)
    for decision in ("approve", "reject", "accept", "decline"):
        assert raises(call, run, workflow.decide, stranger, STORY, v, decision, "why").status == 403
    assert state(run, STORY) == "PENDING_APPROVAL"


def test_hq_cannot_send_anothers_decision_or_decide(run):
    """06.1 D-54, D-67: HQ may own a decision and then send it (test_ownership.py), but never send one it does not
    own, and owning grants no approval."""
    case = case_with_pick(run, TRANSFER)
    hq, v = seat(None), version(run, STORY)
    assert raises(call, run, workflow.send, hq, STORY, v).code == "not_owned"  # nobody owns it yet
    take(run, seat(case["source_branch"]), STORY)
    assert raises(call, run, workflow.send, hq, STORY, v).code == "forbidden"  # the source store owns it
    owned_send(run, seat(case["source_branch"]), STORY, v)
    for decision in ("approve", "accept"):
        assert raises(call, run, workflow.decide, hq, STORY, v, decision).code == "forbidden"


def test_decline_releases_every_hold(run):
    case = case_with_pick(run, TRANSFER)
    dest, v = seat(pick(run, STORY).dest), version(run, STORY)
    owned_send(run, seat(case["source_branch"]), STORY, v)
    sent = booked_rows(run, STORY)
    e = raises(call, run, workflow.decide, dest, STORY, v, "decline", " ")
    assert (e.code, e.message) == ("bad_request", "A reason is required")
    assert call(run, workflow.decide, dest, STORY, v, "decline", "no space") == {"state": "REJECTED"}
    assert state(run, STORY) == "REJECTED"
    assert holds(run, STORY, ("HELD", "COMMITTED", "CONVERTED")) == []
    assert {r["state"] for r in booked_rows(run, STORY)} == {"RELEASED"} and len(booked_rows(run, STORY)) == len(sent)


def test_an_expired_hold_returns_the_case_to_open_on_the_same_version(run):
    case = case_with_pick(run, TRANSFER)
    src, dest, v = seat(case["source_branch"]), seat(pick(run, STORY).dest), version(run, STORY)
    owned_send(run, src, STORY, v)
    call(run, workflow.decide, dest, STORY, v, "accept", None, ack(run, STORY), shown(run, STORY))
    run_out_the_clock(run)
    e = raises(call, run, workflow.decide, src, STORY, v, "approve")
    assert (e.code, e.message) == ("hold_expired", "Capacity hold expired. Check availability and book again.")  # D-57
    with db.tx() as conn:
        d = cases.detail(conn, fresh(run["run_id"]), STORY)  # any read applies the expiry first
    assert (d["state"], d["rec_version"]) == ("OPEN", v)
    assert {"kind": "hold_expired", "text": "Hold expired"} in d["badges"]
    assert {r["state"] for r in booked_rows(run, STORY)} == {"EXPIRED"} and len(logged(run, STORY, "hold_expired")) == 1
    e = raises(call, run, workflow.decide, src, STORY, v, "approve")  # still refused after the expiry was applied
    assert e.code == "hold_expired"


def test_hold_expired_and_re_review_badges_end_with_the_state_they_describe(run):
    """WR-08: "Hold expired" shows only while the case is OPEN (the client then offers Recheck and resend), and
    "Re-review" only while the re-reviewed decision is still ahead (OPEN, WAITING, PENDING_APPROVAL, APPROVED)."""
    flags = {"missing_data": None, "hold_expired": True, "re_review": True, "look_round": 0}
    for state_ in cases.COLUMNS:
        kinds = [b["kind"] for b in cases.badges({**flags, "state": state_}, None, None, [])]
        assert ("hold_expired" in kinds) == (state_ == "OPEN"), state_
        assert ("re_review" in kinds) == (state_ in ("OPEN", "WAITING", "PENDING_APPROVAL", "APPROVED")), state_
    # the path the review found: a hold expires, then the manager rejects the case
    case = case_with_pick(run, TRANSFER)
    src, v = seat(case["source_branch"]), version(run, STORY)
    owned_send(run, src, STORY, v)
    run_out_the_clock(run)
    with db.tx() as conn:
        assert {"kind": "hold_expired", "text": "Hold expired"} in cases.detail(conn, fresh(run["run_id"]), STORY)["badges"]
    call(run, workflow.reject, src, STORY, "the shelf is full")
    with db.tx() as conn:
        d = cases.detail(conn, fresh(run["run_id"]), STORY)
    assert d["state"] == "REJECTED" and "hold_expired" not in [b["kind"] for b in d["badges"]]


def test_a_hold_at_or_past_its_expiry_never_counts(run):
    """FLOW-05 boundary: a hold counts only while expires_at > now(), even before any cleanup marks it EXPIRED."""
    case = case_with_pick(run, TRANSFER)

    def booked(conn):  # the story lot's input with nobody's bookings left out: the story case's own would count
        return inputs.build_input(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run["run_id"]),
                                  case["lot_id"], exclude_case=None).bookings

    with db.tx() as conn:
        before = booked(conn)
    owned_send(run, seat(case["source_branch"]), STORY, version(run, STORY))
    with db.tx() as conn:
        assert booked(conn) != before  # a live hold counts
    for at in ("now()", "now() - interval '1 second'"):
        with db.tx() as conn:  # one transaction, so now() is the same instant in the update and the read
            db.run(conn, f"UPDATE reservations SET expires_at = {at} WHERE run_id = :r AND case_id = :c",
                   r=run["run_id"], c=STORY)
            assert booked(conn) == before
            assert {r["state"] for r in db.rows(conn, "SELECT state FROM reservations WHERE run_id = :r "
                                                      "AND case_id = :c", r=run["run_id"], c=STORY)} == {"HELD"}


def test_a_decision_from_an_expired_round_does_not_count(run):
    case = case_with_pick(run, TRANSFER)
    src, dest, v = seat(case["source_branch"]), seat(pick(run, STORY).dest), version(run, STORY)
    owned_send(run, src, STORY, v)
    call(run, workflow.decide, dest, STORY, v, "accept", None, ack(run, STORY), shown(run, STORY))
    run_out_the_clock(run)
    with db.tx() as conn:
        cases.board(conn, fresh(run["run_id"]))  # any read applies the expiry
    assert owned_send(run, src, STORY, v) == {"state": "PENDING_APPROVAL", "round": 2}  # Recheck and resend
    assert call(run, workflow.decide, src, STORY, v, "approve", None, ack(run, STORY), shown(run, STORY)) == \
        {"state": "PENDING_APPROVAL"}  # round 1's yes is gone
    assert call(run, workflow.decide, dest, STORY, v, "accept", None, ack(run, STORY), shown(run, STORY)) == \
        {"state": "APPROVED"}


def test_resend_holds_everything_again_or_nothing(run):
    case = case_with_pick(run, TRANSFER)
    src, plan, v = seat(case["source_branch"]), pick(run, STORY), version(run, STORY)
    owned_send(run, src, STORY, v)
    run_out_the_clock(run)
    eff = eff_input(run, STORY)  # meanwhile another case commits the destination's whole free storage
    trip = next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest)
    free = {c.day: c.free_totes for c in eff.capacity if c.branch == plan.dest}
    taken = totals((d, t) for b, d, t in eff.bookings.totes if b == plan.dest)
    for d in range(trip.day + trip.arrives_min // 1440, eff.case_lot().expiry_day + 1):
        add_booking(run["run_id"], "OTHER", "STORAGE", f"{plan.dest}|{eff.product.zone}", d, free[d] - taken.get(d, 0))
    e = raises(owned_send, run, src, STORY, v)
    assert e.code == "conflict" and CAP_REASON["storage"] in e.extra["reasons"]
    assert holds(run, STORY) == [] and {r["round"] for r in booked_rows(run, STORY)} == {1}  # no round 2 at all
    with db.tx() as conn:
        assert cases.detail(conn, fresh(run["run_id"]), STORY)["state"] == "OPEN"


def test_nothing_extends_a_hold(run):
    case = case_with_pick(run, TRANSFER)
    src, dest, v = seat(case["source_branch"]), seat(pick(run, STORY).dest), version(run, STORY)

    def expiries():
        return [(r["res_id"], r["expires_at"]) for r in booked_rows(run, STORY) if r["state"] == "HELD"]
    owned_send(run, src, STORY, v)
    sent = expiries()
    call(run, workflow.decide, dest, STORY, v, "accept", None, ack(run, STORY), shown(run, STORY))
    with db.tx() as conn:  # reads neither
        cases.board(conn, fresh(run["run_id"]))
        cases.detail(conn, fresh(run["run_id"]), STORY)
    assert sent and expiries() == sent  # the destination's yes and the reads bought the bookings no time


def test_a_hold_lasts_hold_ttl_min(run, monkeypatch):
    """FLOW-05 precision: Postgres computes expires_at as now() plus HOLD_TTL_MIN minutes from the settings."""
    def lasts(case_id):
        with db.tx() as conn:
            return [float(r["s"]) for r in db.rows(conn, "SELECT extract(epoch FROM expires_at - now()) AS s "
                                                         "FROM reservations WHERE run_id = :r AND case_id = :c",
                                                   r=run["run_id"], c=case_id)]
    case = case_with_pick(run, TRANSFER)
    owned_send(run, seat(case["source_branch"]), STORY, version(run, STORY))
    assert config.HOLD_TTL_MIN == config.SETTINGS["holds"]["hold_ttl_min"]
    assert lasts(STORY) and all(abs(s - config.HOLD_TTL_MIN * 60) <= 5 for s in lasts(STORY))
    monkeypatch.setattr(config, "HOLD_TTL_MIN", config.HOLD_TTL_MIN / 2)  # the value is read at send time
    md = case_with_pick(run, MARKDOWN)
    owned_send(run, seat(md["source_branch"]), md["case_id"], version(run, md["case_id"]))
    assert lasts(md["case_id"]) and all(abs(s - config.HOLD_TTL_MIN * 60) <= 5 for s in lasts(md["case_id"]))


def test_advance_day_expires_holds_first(run):
    case = case_with_pick(run, TRANSFER)
    owned_send(run, seat(case["source_branch"]), STORY, version(run, STORY))
    run_out_the_clock(run)
    r = fresh(run["run_id"])
    db.in_tx(lambda conn: advance.advance_day(conn, r["run_id"], r["version"]))
    assert {x["state"] for x in booked_rows(run, STORY)} == {"EXPIRED"}
    with db.tx() as conn:
        log = db.rows(conn, "SELECT seq, type, day FROM activity_log WHERE run_id = :r AND type IN ('hold_expired', "
                            "'day_advanced') AND day = :d ORDER BY seq", r=run["run_id"], d=run["day"])
    assert [x["type"] for x in log] == ["hold_expired", "day_advanced"]  # before the worlds moved
    assert state(run, STORY) == "OPEN"


def test_a_transfer_without_a_verifiable_source_is_refused_before_booking(run, monkeypatch):
    case = case_with_pick(run, TRANSFER)
    src, plan, v = seat(case["source_branch"]), pick(run, STORY), version(run, STORY)
    elsewhere = other_branch(run, case["source_branch"], plan.dest)

    def move_lot(branch):
        for b in {case["source_branch"], elsewhere}:  # the test moves the lot between two series directly
            own_series(run, b, case["sku"])
        with db.tx() as conn:
            db.run(conn, "UPDATE lots SET branch = :b WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   b=branch, r=run["run_id"], l=case["lot_id"])

    def refused(fn, *args):
        e = raises(call, run, fn, *args)
        assert (e.code, e.message) == ("incomplete_shipment",
                                       "This transfer has no verifiable source; nothing was booked.")

    take(run, src, STORY)  # only the owner sends (06.1 D-54)
    move_lot(elsewhere)  # the case lot is on the record at another branch
    refused(workflow.send, src, STORY, v)
    assert booked_rows(run, STORY) == [] and logged(run, STORY, "sent") == []
    move_lot(case["source_branch"])
    real = inputs.case_runs
    monkeypatch.setattr(inputs, "case_runs", lambda ref, source, day, **stage1: [  # the run now leaves elsewhere
        replace(r, origin=elsewhere) if r.run_code == plan.run_code else r for r in real(ref, source, day, **stage1)])
    refused(workflow.send, src, STORY, v)
    assert booked_rows(run, STORY) == [] and logged(run, STORY, "sent") == []
    monkeypatch.setattr(inputs, "case_runs", real)
    owned_send(run, src, STORY, v)  # and at commit: the lot moves after the send
    call(run, workflow.decide, seat(plan.dest), STORY, v, "accept", None, ack(run, STORY), shown(run, STORY))
    move_lot(elsewhere)
    refused(workflow.decide, src, STORY, v, "approve", None, ack(run, STORY), shown(run, STORY))
    with db.tx() as conn:
        assert db.rows(conn, "SELECT 1 FROM inbound WHERE run_id = :r AND source_case_id = :c",
                       r=run["run_id"], c=STORY) == []
    assert {x["state"] for x in booked_rows(run, STORY)} == {"HELD"} and logged(run, STORY, "committed") == []


def test_engine_errors_are_plain_4xx_not_conflicts(run, monkeypatch):
    case = case_with_pick(run, TRANSFER)
    src, plan, v = seat(case["source_branch"]), pick(run, STORY), version(run, STORY)
    monkeypatch.setattr(inputs, "ENGINE_VERSION", "engine-0")  # an input stamped with another engine version
    e = raises(owned_send, run, src, STORY, v)
    assert (e.status, e.code) == (400, "bad_input") and "engine" in e.message
    monkeypatch.undo()
    trip = {"run_code": plan.run_code, "day": run["day"]}  # a what-if-only kind can never reach check_plan
    item = {"override": {"kind": "cold_chain", "run_code": plan.run_code, "value": True}, "trip": trip,
            "branch": None, "note": "test", "adopted_day": run["day"]}
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET adopted = CAST(:a AS jsonb) WHERE run_id = :r AND case_id = :c",
               a=db.js([item]), r=run["run_id"], c=STORY)
    e = raises(owned_send, run, src, STORY, v)
    assert (e.status, e.code) == (400, "bad_override")
    assert booked_rows(run, STORY) == [] and logged(run, STORY, "sent") == []


# --- wait, reject, what-if and adopt (Task 3) --------------------------------------------------------------------

def story_trip(run):
    """Today's run the story transfer names, from the stored input the engine saw."""
    plan = pick(run, STORY)
    with db.tx() as conn:
        rec = cases.active_rec(conn, cases.case_row(conn, run["run_id"], STORY))
    return next(r for r in rec["engine_input"]["runs"] if r["run_code"] == plan.run_code and r["dest"] == plan.dest)


def test_adopting_needs_a_source_note_and_cancels_a_pending_approval(run):
    case = case_with_pick(run, TRANSFER)
    src, v, trip = seat(case["source_branch"]), version(run, STORY), story_trip(run)
    owned_send(run, src, STORY, v)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    for note in ("", "   "):
        e = raises(call, run, workflow.adopt, src, STORY, quote, note)
        assert (e.code, e.message) == ("bad_request", "Say where these assumptions come from")
    assert raises(call, run, workflow.adopt, seat(pick(run, STORY).dest), STORY, quote, "carrier").status == 403
    out = call(run, workflow.adopt, src, STORY, quote, "carrier quote by phone")
    assert out["rec_version"] != v and out["rec_version"] == version(run, STORY)
    assert state(run, STORY) == "OPEN" and holds(run, STORY, ("HELD", "COMMITTED", "CONVERTED")) == []
    assert len(logged(run, STORY, "approval_cancelled")) == 1 and len(logged(run, STORY, "assumptions_adopted")) == 1
    with db.tx() as conn:
        d = cases.detail(conn, fresh(run["run_id"]), STORY)
    assert d["adopted"] == [{"override": quote[0], "trip": {"run_code": trip["run_code"], "day": trip["day"]},
                             "branch": None, "note": "carrier quote by phone", "adopted_day": run["day"]}]
    assert d["adopted_ended"] == [] and quote[0] in d["engine_input"]["overrides"]
    assert raises(owned_send, run, src, STORY, v).code == "stale_version"  # the old page is stale now


def test_operational_facts_cannot_be_adopted(run):
    case = case_with_pick(run, TRANSFER)
    src, v, trip = seat(case["source_branch"]), version(run, STORY), story_trip(run)
    for adj in ({"kind": "stock_count", "lot_id": case["lot_id"], "units": 1.0},  # only from a confirmed count
                {"kind": "cold_chain", "run_code": trip["run_code"], "value": False}):  # what-if only
        e = raises(call, run, workflow.adopt, src, STORY, [adj], "a note")
        assert (e.status, e.code, e.message) == (400, "bad_override", f"{adj['kind']} is not allowed here")
    e = raises(call, run, workflow.adopt, src, STORY, [{"kind": "transport_quote", "run_code": trip["run_code"],
                                                        "fixed_cost": -1.0}], "a note")
    assert e.code == "bad_override"  # the engine's own bound, in its own words
    assert version(run, STORY) == v
    with db.tx() as conn:
        assert cases.case_row(conn, run["run_id"], STORY)["adopted"] == []


def test_a_what_if_never_changes_the_active_plan(run):
    case = case_with_pick(run, TRANSFER)
    plan, v = pick(run, STORY), version(run, STORY)
    with db.tx() as conn:
        by = cases.active_rec(conn, cases.case_row(conn, run["run_id"], STORY))["result"]["decide_by"]
    later = [{"kind": "decide_at", "minute": by["latest_decision_min"] + 1}]  # one minute past the story deadline
    out = call(run, workflow.whatif, STORY, later)
    on_run = [r for r in out["result"]["rejected"] if r.get("run_code") == plan.run_code]
    assert on_run and all(any(x.startswith("past decision deadline") for x in r["reasons"]) for r in on_run)
    assert out["rec_version"] != v and out["overrides"] == later and "text" not in (out["result"]["wait"] or {})
    assert version(run, STORY) == v and state(run, STORY) == "OPEN" and booked_rows(run, STORY) == []
    with db.tx() as conn:
        row = db.one(conn, "SELECT is_whatif, overrides FROM recommendations WHERE run_id = :r AND case_id = :c "
                           "AND rec_version = :v", r=run["run_id"], c=STORY, v=out["rec_version"])
    assert row == {"is_whatif": True, "overrides": later}
    # a what-if on a pending case leaves its holds and approval round alone
    owned_send(run, seat(case["source_branch"]), STORY, v)
    sent = booked_rows(run, STORY)
    call(run, workflow.whatif, STORY, [{"kind": "run_available", "run_code": plan.run_code, "value": False}])
    assert state(run, STORY) == "PENDING_APPROVAL" and booked_rows(run, STORY) == sent and version(run, STORY) == v
    assert raises(call, run, workflow.whatif, STORY, [{"kind": "stock_count", "lot_id": case["lot_id"],
                                                       "units": 1.0}]).code == "bad_override"


def test_adopting_what_a_what_if_tried_makes_a_real_version_with_its_note(run):
    """WR-06: a what-if and then "Use these assumptions" with the same overrides give the same version label; the
    active version is then the adopted one (is_whatif false, the source note stored), never a row still marked as a
    simulation."""
    case = case_with_pick(run, TRANSFER)
    trip = story_trip(run)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    tried = call(run, workflow.whatif, STORY, quote)
    adopted = call(run, workflow.adopt, seat(case["source_branch"]), STORY, quote, "carrier quote by phone")
    assert adopted["rec_version"] == tried["rec_version"] == version(run, STORY)  # one input, one label
    with db.tx() as conn:
        row = db.one(conn, "SELECT is_whatif, source_note, overrides FROM recommendations WHERE run_id = :r "
                           "AND case_id = :c AND rec_version = :v", r=run["run_id"], c=STORY, v=adopted["rec_version"])
    assert row == {"is_whatif": False, "source_note": "carrier quote by phone", "overrides": quote}
    # a later what-if on the same input never turns the active version back into a simulation
    call(run, workflow.whatif, STORY, quote, adopted["rec_version"])
    with db.tx() as conn:
        again = db.one(conn, "SELECT is_whatif, source_note FROM recommendations WHERE run_id = :r AND case_id = :c "
                             "AND rec_version = :v", r=run["run_id"], c=STORY, v=adopted["rec_version"])
    assert again == {"is_whatif": False, "source_note": "carrier quote by phone"}


def test_wait_reopens_on_the_review_day(run):
    case = case_with_pick(run, TRANSFER)
    src = seat(case["source_branch"])
    for until, why in ((run["day"], "later"), (run["day"] + 1, " ")):
        assert raises(call, run, workflow.wait, src, STORY, until, why).code == "bad_request"
    assert raises(call, run, workflow.wait, seat(None), STORY, run["day"] + 1, "later").status == 403
    assert call(run, workflow.wait, src, STORY, run["day"] + 1, "waiting for the count") == {"state": "WAITING"}
    with db.tx() as conn:
        row = cases.case_row(conn, run["run_id"], STORY)
    assert (row["state"], row["waiting_until_day"], row["reason"]) == ("WAITING", run["day"] + 1,
                                                                       "waiting for the count")
    r = fresh(run["run_id"])
    db.in_tx(lambda conn: advance.advance_day(conn, r["run_id"], r["version"]))
    assert state(run, STORY) == "OPEN"


def test_a_waiting_case_keeps_the_managers_reason_and_missing_data_is_its_own_state(run, monkeypatch):
    """WR-05: re-evaluation never erases the manager's wait reason, and the engine's missing-data state has its own
    column: a reason that happens to start with "missing data" shows no badge, and a real missing forecast shows the
    badge without touching the manager's text."""
    case = case_with_pick(run, TRANSFER)
    why = "missing data from the supplier"  # a manager's own words that read like the engine's state

    def card():
        with db.tx() as conn:
            c = cases.detail(conn, fresh(run["run_id"]), STORY)
        return c["state"], c["reason"], [b["kind"] for b in c["badges"]]
    call(run, workflow.wait, seat(case["source_branch"]), STORY, run["day"] + 2, why)
    state_, reason, kinds = card()
    assert (state_, reason) == ("WAITING", why) and "missing_data" not in kinds
    r = fresh(run["run_id"])
    db.in_tx(lambda conn: advance.advance_day(conn, r["run_id"], r["version"]))  # the morning re-evaluates it
    state_, reason, kinds = card()
    assert (state_, reason) == ("WAITING", why) and "missing_data" not in kinds

    def no_forecast(*args):
        raise HorizonError("no forecast for a day the case needs")
    monkeypatch.setattr(cases, "current_input", no_forecast)
    db.in_tx(lambda conn: cases.evaluate_case(conn, fresh(run["run_id"]), cases.case_row(conn, run["run_id"], STORY)))
    state_, reason, kinds = card()
    assert (state_, reason) == ("WAITING", why) and "missing_data" in kinds


def test_reject_needs_a_reason_and_keeps_the_history(run):
    case = case_with_pick(run, TRANSFER)
    src, v = seat(case["source_branch"]), version(run, STORY)
    with db.tx() as conn:
        versions = db.one(conn, "SELECT count(*) AS n FROM recommendations WHERE run_id = :r AND case_id = :c",
                          r=run["run_id"], c=STORY)["n"]
    assert raises(call, run, workflow.reject, src, STORY, "").message == "A reason is required"
    assert raises(call, run, workflow.reject, seat(pick(run, STORY).dest), STORY, "no").status == 403
    assert call(run, workflow.reject, src, STORY, "the shelf is full") == {"state": "REJECTED"}
    with db.tx() as conn:
        row = cases.case_row(conn, run["run_id"], STORY)
        kept = db.one(conn, "SELECT count(*) AS n FROM recommendations WHERE run_id = :r AND case_id = :c",
                      r=run["run_id"], c=STORY)["n"]
    assert (row["state"], row["reason"], row["active_rec_version"]) == ("REJECTED", "Rejected: the shelf is full", v)
    assert kept == versions and len(logged(run, STORY, "opened")) == 1 and len(logged(run, STORY, "rejected")) == 1
    assert raises(call, run, workflow.send, src, STORY, v).code == "wrong_state"  # the state first, then the owner


def test_commit_recheck_refuses_when_availability_changed(run):
    """FLOW-04 negative path: after send and accept, another case takes all the destination's unmet demand on the run
    (a COMMITTED OTHER booking, which the recheck reads from today's raw input). Approve is 409 conflict with the
    engine's reasons and nothing commits: the holds stay HELD, no booking, no inbound, no `committed` entry."""
    case = case_with_pick(run, TRANSFER)
    cid, plan = case["case_id"], pick(run, STORY)
    src, dest, v = seat(case["source_branch"]), seat(plan.dest), version(run, cid)
    owned_send(run, src, cid, v)
    call(run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), shown(run, cid))
    held_before = holds(run, cid)
    eff = eff_input(run, cid)
    trip = next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest)
    lost = unmet_by_day(eff, plan.dest)
    for d in range(first_sellable_day(trip.day, trip.arrives_min, eff.policy), eff.case_lot().expiry_day + 1):
        if lost.get(d, 0.0) > 0:
            add_booking(run["run_id"], "OTHER", "DEMAND", f"{plan.dest}|{eff.product.sku}", d, lost[d])
    assert transfer_caps(eff_input(run, cid), trip)[0]["demand"] < plan.qty  # availability really shrank
    e = raises(call, run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid))
    assert (e.status, e.code) == (409, "conflict")
    assert CAP_REASON["demand"] in e.extra["reasons"]
    assert state(run, cid) == "PENDING_APPROVAL"
    assert holds(run, cid) == held_before and held_before
    assert holds(run, cid, ("COMMITTED", "CONVERTED")) == []
    assert logged(run, cid, "committed") == []
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM inbound WHERE run_id = :r AND source_case_id = :c",
                       r=run["run_id"], c=cid) == []
