"""The confirmed outside-review findings on Phase 7's server (07-CONTEXT D-32; letters as in the verified list).

Each test names its letter. Cases are found by their tag (the story case, SUS-19) or by the stored result's own
options, never pasted keys; facts come from the record or a real confirmed answer. The one exception is H's further
request for the same lots, a test fact with the phantom fact's own scope (`add_facts`, as 07-06's check-time tests
ask one). Nothing sleeps.
"""
from __future__ import annotations

from api import cases, db, workflow
from engine.model import TRANSFER
from tests.api.conftest import as_seat, by_tag, fresh, key, owned_send, seat, take_and_send
from tests.api.test_selection import active, decision_rows, feasible_others
from tests.api.test_workflow import call, pick, raises, version
from tests.api.verify_helpers import ack, story


def decided(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.decided_roles(conn, cases.case_row(conn, run["run_id"], case_id))


def post(client, path, body=None):
    return client.post(path, json={} if body is None else body, headers=key())


def story_card(client) -> dict:
    return next(c for c in client.get("/api/cases").json()["cases"] if c["tag"] == cases.STORY_TAG)


def run_of(client) -> dict:
    return fresh(client.get("/api/session").json()["run_id"])


# --- C: approve and accept name the plan the page shows (07-CONTEXT D-23) ---------------------------------------------

def test_c_a_decision_names_the_plan_the_page_shows(run):
    """C: approve and accept send the plan key the page shows. None sent, or another option's key, is 409 stale_plan
    naming the plan in force, and records nothing; the plan in force's key records the decision, which stores it. A
    reject or decline is checked only when it names a plan."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v, plan = version(run, cid), pick(run, cid)
    assert plan.kind == TRANSFER, "hand-back: the story's pick is not a transfer"
    dest = seat(plan.dest)
    owned_send(run, src, cid, v)
    other = next(o["key"] for o in active(run, cid)["result"]["options"] if o["key"] != plan.key)
    for given in (None, other):
        e = raises(call, run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), given)
        assert (e.status, e.code, e.extra["plan_key"]) == (409, "stale_plan", plan.key)
    assert decided(run, cid) == {}
    assert call(run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), plan.key) == \
        {"state": "PENDING_APPROVAL"}
    e = raises(call, run, workflow.decide, src, cid, v, "approve", None, ack(run, cid))  # names no plan
    assert (e.code, decided(run, cid)) == ("stale_plan", {"destination": "accept"})
    e = raises(call, run, workflow.decide, src, cid, v, "reject", "not this one", None, other)
    assert e.code == "stale_plan"  # a reject that names another plan is refused too
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), plan.key) == {"state": "APPROVED"}
    assert {d["plan_key"] for d in decision_rows(run, cid)} == {plan.key}


def test_c_a_reject_or_decline_that_names_no_plan_still_closes(run):
    """C: a decline that names no plan is recorded as before (only approve and accept must name one)."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v, plan = version(run, cid), pick(run, cid)
    owned_send(run, src, cid, v)
    assert call(run, workflow.decide, seat(plan.dest), cid, v, "decline", "no room") == {"state": "REJECTED"}


def test_c_a_stale_page_cannot_accept_a_newly_selected_plan(client):
    """C (the reviewers' scenario): the destination's page shows plan A; the source then selects another engine option
    B to the same destination and sends it on the same version. The old page's accept, naming A (with A's
    acknowledgement), is 409 stale_plan and records nothing; the reloaded page's accept, naming B, is recorded. A
    plan_key that is not text is 400."""
    card = story_card(client)
    cid, src, dest, v, page = card["case_id"], card["source"], card["dest"], card["rec_version"], card["suggested_key"]
    run = run_of(client)
    seen = ack(run, cid)  # what the destination's page holds before the selection
    same = [k for k in feasible_others(run, cid, (TRANSFER,)) if cases.parse_plan(k).dest == dest]
    assert same, "hand-back: the story case has no other feasible transfer to its destination"
    as_seat(client, src)
    r = post(client,f"/api/cases/{cid}/select", {"rec_version": v, "plan_key": same[0]})
    assert r.status_code == 200, r.json()
    assert take_and_send(client, cid, v).json()["state"] == "PENDING_APPROVAL"
    as_seat(client, dest)
    r = post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "accept", "plan_key": page,
                                                    "acknowledged": seen})
    assert (r.status_code, r.json()["error"], r.json()["plan_key"]) == (409, "stale_plan", same[0])
    assert story_card(client)["decisions"] == {}
    r = post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "accept", "plan_key": 7})
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")
    now = story_card(client)
    assert now["suggested_key"] == same[0]
    r = post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "accept",
                                                    "plan_key": now["suggested_key"], "acknowledged": ack(run, cid)})
    assert r.json() == {"state": "PENDING_APPROVAL"}


# --- A: Cancel approved plan stops only work that has not left (07-CONTEXT D-23) --------------------------------------

def test_a_cancel_refuses_work_that_has_left(client):
    """A: Cancel approved plan is offered on prepared work until its transfer is dispatched. Once the stock has left,
    cancel is 409 already_dispatched for either approving manager, and nothing changes: the case stays PREPARED, the
    work order DISPATCHED, every booking and the pending delivery as they were, and the receipt still lands. It is no
    longer offered then, nor after the receipt."""
    from tests.api.test_execution import (bookings_of, committed, dispatch_body, inbound_row, prepared, receipt_body,
                                          story_case, work_order)
    from tests.api.conftest import sign_in
    run, action, src, dest = prepared(client)  # signed in as the source manager
    cid, rid = action["case_id"], run["run_id"]
    assert detail_of(client, cid)["commitment"]["next_actions"] == ["cancel_approved"]  # before dispatch: offered
    r = post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, committed(action)))
    assert r.status_code == 200, r.json()
    booked, pending = bookings_of(rid, cid), inbound_row(rid, action["new_lot_id"])
    assert len(pending) == 1
    for who in (src, dest):
        sign_in(client, who)
        r = post(client, f"/api/cases/{cid}/cancel", {"reason": "changed our mind"})
        assert (r.status_code, r.json().get("error")) == (409, "already_dispatched"), (who, r.json())
        assert r.json()["work_orders"] == [{"action_id": action["action_id"], "state": "DISPATCHED"}]
        assert detail_of(client, cid)["commitment"]["next_actions"] == []  # no longer offered
    assert (story_case(rid)["state"], story_case(rid)["closed_as"]) == ("PREPARED", None)
    assert work_order(rid, cid)["state"] == "DISPATCHED"
    assert bookings_of(rid, cid) == booked and inbound_row(rid, action["new_lot_id"]) == pending
    r = post(client, f"/api/cases/{cid}/receipt", receipt_body(action, committed(action)))  # signed in as dest
    assert r.status_code == 200, r.json()
    sign_in(client, src)
    r = post(client, f"/api/cases/{cid}/cancel", {"reason": "after the receipt"})
    if story_case(rid)["state"] == "PREPARED":  # a markdown rest still to apply keeps it PREPARED (06.1-11)
        assert (r.status_code, r.json()["error"]) == (409, "already_dispatched")
        assert detail_of(client, cid)["commitment"]["next_actions"] == []
    else:
        assert (r.status_code, r.json()["error"]) == (409, "wrong_state")


def detail_of(client, case_id) -> dict:
    r = client.get(f"/api/cases/{case_id}")
    assert r.status_code == 200, r.json()
    return r.json()


# --- E and J: requests and the unverified badge follow the plan in force (07-CONTEXT D-12, D-23) ---------------------

STRAWBERRY = "strawberry-cold-chain"  # a seed tag: its recommended markdown leaves the printed-date fact unflagged


def select_flagging(run, case_id: str, fact_ids: set[str]) -> str:
    """Select (as the source manager) the first feasible engine option whose own fact checks flag every fact of
    `fact_ids` (changes_plan) while the recommended plan's stored checks flag none of them; returns its key."""
    from api import selection
    from tests.api.test_selection import engine_checks
    rec = active(run, case_id)
    assert not any(c["changes_plan"] for c in rec["fact_checks"] if c["fact_id"] in fact_ids), \
        "hand-back: the recommended plan already flags the fact"
    key = next(k for k in feasible_others(run, case_id)
               if fact_ids <= {c["fact_id"] for c in engine_checks(run, case_id, k) if c["changes_plan"]})
    with db.tx() as conn:
        src = seat(cases.case_row(conn, run["run_id"], case_id)["source_branch"])
    call(run, selection.select, src, case_id, version(run, case_id), key)
    return key


def make_stale(run, case: dict, days_back: int) -> None:
    """The case lot's last count moved `days_back` days before today on the workspace's own copy of its series (a
    record change), then the case re-evaluated."""
    from tests.api.conftest import own_series
    own_series(run, case["source_branch"], case["sku"])
    with db.tx() as conn:
        r = fresh(run["run_id"])
        db.run(conn, "UPDATE lots SET last_verified_day = :d WHERE run_id = :r AND world = 'shelfshift' "
                     "AND lot_id = :l", d=r["day"] - days_back, r=r["run_id"], l=case["lot_id"])
        cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], case["case_id"]))


def test_e_a_selected_plans_own_fact_can_be_requested(run):
    """E: after Select another plan, the source manager can request the check the selected plan flags although the
    recommended plan does not (the plan in force's checks, `commitment.checks_in_force`); a fact only the replaced
    recommended plan flags is no longer askable (400 unknown_fact)."""
    from api import signals
    case = by_tag(run, STRAWBERRY)
    cid, src = case["case_id"], seat(case["source_branch"])
    (fact,) = [c for c in active(run, cid)["fact_checks"] if c["kind"] == "ESTIMATED_EXPIRY"]
    select_flagging(run, cid, {fact["fact_id"]})
    out = call(run, signals.request_evidence, src, cid, version(run, cid), fact["fact_id"])
    assert (out["fact_id"], out["part"], out["state"]) == (fact["fact_id"], "expiry", "OPEN")
    story_case = story(run)
    scid, ssrc = story_case["case_id"], seat(story_case["source_branch"])
    flagged = {c["fact_id"] for c in active(run, scid)["fact_checks"] if c["changes_plan"]}
    from api import selection
    from tests.api.test_selection import engine_checks
    bare = next(k for k in feasible_others(run, scid)
                if not flagged & {c["fact_id"] for c in engine_checks(run, scid, k) if c["changes_plan"]})
    call(run, selection.select, ssrc, scid, version(run, scid), bare)
    e = raises(call, run, signals.request_evidence, ssrc, scid, version(run, scid), sorted(flagged)[0])
    assert (e.status, e.code) == (400, "unknown_fact")


def test_e_a_combined_request_follows_the_selected_plan(run):
    """E: a combined count and printed-date request is checked against the plan in force's checks too: with the
    strawberry lot's last count well back, the selected markdown flags both its stale count and its printed date
    (the recommended one flags neither), and the pair is asked once, as two parts."""
    from api import signals
    case = by_tag(run, STRAWBERRY)
    cid, src = case["case_id"], seat(case["source_branch"])
    make_stale(run, case, 8)
    kinds = {c["kind"]: c["fact_id"] for c in active(run, cid)["fact_checks"]}
    pair = [kinds["STALE_COUNT"], kinds["ESTIMATED_EXPIRY"]]
    select_flagging(run, cid, set(pair))
    out = call(run, signals.request_evidence, src, cid, version(run, cid), None, None, pair)
    assert sorted((p["part"], p["fact_id"]) for p in out["group_parts"]) == [("count", pair[0]), ("expiry", pair[1])]


def test_j_the_unverified_badge_reads_the_plan_in_force(run):
    """J: the card's unverified badge counts the plan in force's checks: once the source selects the markdown that
    flags the strawberry lot's printed date, the badge names that fact, which the recommended plan did not flag."""
    from tests.api.test_selection import card_of
    case = by_tag(run, STRAWBERRY)
    cid = case["case_id"]
    (fact,) = [c for c in active(run, cid)["fact_checks"] if c["kind"] == "ESTIMATED_EXPIRY"]
    assert not [b for b in card_of(run, cid)["badges"] if b["kind"] == "unverified"]
    select_flagging(run, cid, {fact["fact_id"]})
    (badge,) = [b for b in card_of(run, cid)["badges"] if b["kind"] == "unverified"]
    assert badge["fact_ids"] == [fact["fact_id"]] and badge["not_allowed_fact_ids"] == []


# --- B: a confirmed printed date older than yesterday still reads as expired (07-CONTEXT D-11, D-21) -----------------

def confirmed_printed(run, printed_back: int = 1) -> tuple[dict, dict, int]:
    """The story lot made ESTIMATED (test_requests.expiry_fixture), its printed-date check asked, answered with the
    date `printed_back` days before today and confirmed by the lot's manager; (case, fact row, printed day)."""
    from tests.api.test_evidence import confirm, table
    from tests.api.test_requests import answer_dates, ask, expiry_fixture, in_both, iso_day
    case, fact = expiry_fixture(run)
    q = ask(run, case["case_id"], fact["fact_id"])
    printed = fresh(run["run_id"])["day"] - printed_back
    answer_dates(run, q, {fact["lot_id"]: iso_day(run, printed)})
    seen = in_both(run, q)
    confirm(run, seat(q["to_branch"]), seen["answer_signal_id"], seen["answer"]["revision"], None)
    (row,) = table(run["run_id"], "reported_facts")
    return case, row, printed


def test_b_a_printed_date_older_than_yesterday_still_reads_expired(run):
    """B: a printed date confirmed as yesterday stays in force after Next day (the fact's validity runs to the later
    of the printed and recorded dates); once it is older than the engine's yesterday floor, the input clamps it to
    yesterday instead of dropping it, so the lot still reads expired and the recorded estimate never comes back. The
    stored fact and the record are untouched (no engine change)."""
    from api import inputs
    from engine.adjust import effective
    from tests.api.test_advance import advance_once
    case, row, printed = confirmed_printed(run, 1)
    lot_id = row["adjustments"][0]["lot_id"]
    with db.tx() as conn:
        recorded = cases.lot_meta(conn, fresh(run["run_id"]), case)["expiry_day"]
    assert recorded > printed + 1 and row["valid_to"] >= printed + 2, "hand-back: the fact ends before the test day"
    assert advance_once(run)["advanced"] is True
    r = fresh(run["run_id"])
    assert r["day"] - 1 > printed  # the printed date is now older than yesterday
    with db.tx() as conn:
        inp = inputs.build_input(conn, r, lot_id)
    assert {"kind": "expiry", "lot_id": lot_id, "expiry_day": r["day"] - 1} in inp.reported_facts
    lot = next(l for l in effective(inp).lots if l.lot_id == lot_id)
    assert (lot.expiry_day, lot.expiry_source) == (r["day"] - 1, "PRINTED")  # expired, never the estimate again
    from tests.api.test_evidence import table
    (still,) = table(run["run_id"], "reported_facts")
    assert still["adjustments"] == row["adjustments"]  # the confirmed date itself is kept as it was
    with db.tx() as conn:
        assert cases.lot_meta(conn, r, case)["expiry_day"] == recorded  # the record is never rewritten (F-23)


# --- H: an equal check time from an earlier business day is older evidence (07-CONTEXT D-22) -------------------------

def test_h_an_equal_check_time_from_an_earlier_day_is_older_evidence(run, monkeypatch):
    """H: a count confirmed on one business day, then the same check (the same check time and value) answered to a
    request on the next business day: it is no new evidence for that day, so confirm is 409 older_evidence naming the
    lots, the view says it is not confirmable, the request stays ANSWERED and nothing is written; a new check, with
    its own time, then confirms and corrects the record for today. On the same day an equal time and value still
    confirms and changes nothing (test_requests.test_observed_time_rules_at_confirm)."""
    from datetime import timedelta
    from api import config, realtime, signals
    from tests.api.test_advance import advance_once
    from tests.api.test_evidence import (add_facts, answer, ask, confirm, counted, in_both, lot_row, phantom,
                                         refused, request_row, table)
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_VERSION", 10)  # the test's own value: a further request
    cid = story(run)["case_id"]
    fact = phantom(run, cid)
    with db.tx() as conn:
        at = realtime.db_now(conn) - timedelta(minutes=30)
    a = ask(run, cid)
    to = seat(a["to_branch"])
    first = answer(run, a, counted(a, 2.0), observed=at.isoformat())
    confirm(run, to, first["signal_id"], first["revision"])
    (fact_a,) = table(run["run_id"], "reported_facts")
    assert fact_a["confirmed_day"] == fresh(run["run_id"])["day"]
    assert advance_once(run)["advanced"] is True
    today = fresh(run["run_id"])["day"]
    add_facts(run, cid, [{**fact, "fact_id": "TEST:again"}])  # a further request for the same lots, as 07-06 asks one
    q = ask(run, cid, "TEST:again")
    s = answer(run, q, counted(q, 2.0), observed=at.isoformat())  # the same check, given again today
    assert in_both(run, q)["confirmable"] is False
    e = refused(run, signals.confirm, to, s["signal_id"], s["revision"], True)
    assert (e.status, e.code, e.extra["lot_ids"]) == (409, "older_evidence", q["lot_ids"])
    assert request_row(run, q)["state"] == "ANSWERED" and table(run["run_id"], "reported_facts") == [fact_a]
    again = answer(run, q, counted(q, 2.0))  # "Checked just now": a new check
    out = confirm(run, to, again["signal_id"], again["revision"])
    assert out["unchanged"] is False and out["fact_id"] is not None
    assert {lot_row(run, l)["last_verified_day"] for l in q["lot_ids"]} == {today}


# --- F and K: a printed date is checked on the requested lot's own input, and `confirmable` runs that check ---------

def test_f_a_printed_date_is_checked_on_the_requested_lot(run):
    """F: a printed-date check for a lot other than its case's lot (a test fact with the destination phantom fact's
    scope, on the destination's lot) is answered and confirmed after the case's own lot has gone from the record and
    the morning closed the case: the date is validated on the requested lot's own input today, so nothing refuses it
    as a count mismatch, the view reads it confirmable, and the confirm writes the printed-date fact for that lot."""
    from tests.api.conftest import own_world
    from tests.api.test_evidence import add_facts, ask, confirm, in_both, phantom, table
    from tests.api.test_requests import answer_dates, iso_day
    case = story(run)
    cid = case["case_id"]
    dest = phantom(run, cid)
    other = dest["lot_ids"][0]
    assert other != case["lot_id"]
    add_facts(run, cid, [{**dest, "fact_id": "TEST:dest-expiry", "kind": "ESTIMATED_EXPIRY", "lot_ids": [other],
                          "lot_id": other}])
    q = ask(run, cid, "TEST:dest-expiry")
    assert (q["part"], q["lot_ids"], q["to_branch"]) == ("expiry", [other], dest["branch"])
    own_world(run)  # as the morning does, then the case lot leaves the record and its case closes (close_gone)
    with db.tx() as conn:
        for table_name in ("lots", "truth_lots"):
            db.run(conn, f"DELETE FROM {table_name} WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   r=run["run_id"], l=case["lot_id"])
        assert cid in cases.close_gone(conn, fresh(run["run_id"]))
    day = fresh(run["run_id"])["day"]
    a = answer_dates(run, q, {other: iso_day(run, day)})
    assert a["fields"]["dates"] == {other: iso_day(run, day)}
    seen = in_both(run, q)
    assert seen["confirmable"] is True
    out = confirm(run, seat(q["to_branch"]), seen["answer_signal_id"], seen["answer"]["revision"], None)
    (row,) = [f for f in table(run["run_id"], "reported_facts") if f["fact_id"] == out["fact_id"]]
    assert row["adjustments"] == [{"kind": "expiry", "lot_id": other, "expiry_day": day}]


def test_k_confirmable_runs_the_printed_date_bound(run):
    """K: an expiry answer whose printed date was yesterday is confirmable on the day it was given; after Next day
    that date is before the engine's bound, so confirm refuses it with 400 out_of_bounds, and the view says so
    beforehand: `confirmable` is false (one helper shared with confirm)."""
    from api import signals
    from tests.api.test_advance import advance_once
    from tests.api.test_evidence import ask, in_both, refused, request_row
    from tests.api.test_requests import answer_dates, expiry_fixture, iso_day
    case, fact = expiry_fixture(run)
    q = ask(run, case["case_id"], fact["fact_id"])
    answer_dates(run, q, {fact["lot_id"]: iso_day(run, fresh(run["run_id"])["day"] - 1)})
    assert in_both(run, q)["confirmable"] is True
    assert advance_once(run)["advanced"] is True
    seen = in_both(run, q)
    assert (request_row(run, q)["state"], seen["confirmable"]) == ("ANSWERED", False)
    e = refused(run, signals.confirm, seat(q["to_branch"]), seen["answer_signal_id"], seen["answer"]["revision"],
                None)
    assert (e.status, e.code, e.extra["field"]) == (400, "out_of_bounds", f"dates.{fact['lot_id']}")


# --- G: Select another plan shows the selected plan's own deadline (07-CONTEXT D-23) ----------------------------------

def test_g_a_selected_plan_shows_its_own_deadline(run):
    """G: after the source selects another transfer on another run, the selection stores that plan's own decide_by
    (its run's latest decision time under the stored input's Policy, engine.timing.latest_decision_at), and the card's
    `due` and `decide_by`, the detail, Today's action card and the Schedule's decision deadline all show it, never the
    recommended plan's."""
    from api import planning, selection
    from engine import from_dict
    from engine.adjust import effective
    from engine.timing import latest_decision_at
    from tests.api.test_selection import card_of, detail_of
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    rec = active(run, cid)
    before = rec["result"]["decide_by"]
    eff = effective(from_dict(rec["engine_input"]))
    recommended = cases.parse_plan(rec["result"]["recommended"])

    def run_deadline(plan) -> int:
        trip = next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest)
        return latest_decision_at(trip, eff.policy)

    others = [cases.parse_plan(k) for k in feasible_others(run, cid, (TRANSFER,))]
    plan = next(p for p in others if p.pct == 0 and run_deadline(p) != run_deadline(recommended))
    at = run_deadline(plan)
    assert at != before["day"] * 1440 + before["latest_decision_min"], "hand-back: the deadlines are the same"
    call(run, selection.select, src, cid, version(run, cid), plan.key)
    want = {"day": at // 1440, "latest_decision_min": at % 1440, "source": "run"}
    for view in (card_of(run, cid), detail_of(run, cid)):
        assert {k: view["decide_by"][k] for k in want} == want
        assert (view["due"]["day"], view["due"]["min"]) == (want["day"], want["latest_decision_min"])
    with db.tx() as conn:
        stored = cases.case_row(conn, run["run_id"], cid)["selected"]["decide_by"]
    assert {k: stored[k] for k in want} == want
    with db.tx() as conn:
        r = fresh(run["run_id"])
        today = planning.today(conn, r, src)
        rows = planning.schedule(conn, r, src, on=cases.iso_of(r, r["day"]))["rows"]
    (mine,) = [c for c in today["cards"] if c["case_key"] == case["key"]]
    assert (mine["deadline"]["day"], mine["deadline"]["min"]) == (want["day"], want["latest_decision_min"])
    assert mine["deadline"]["cause"] == {"source": "run", "run_code": plan.run_code}
    (deadline,) = [x for x in rows if x["id"] == f"wo:{cid}:decision_deadline"]
    assert deadline["start"] == {"day": want["day"], "min": want["latest_decision_min"]}


# --- M (D-36): dispatch is blocked on the server by a lot hold, a review flag, or both --------------------------------

def test_m_dispatch_is_refused_for_a_flag_a_hold_and_both(client):
    """M (07-CONTEXT D-36 point 1; guards api/execution.py): on the prepared story transfer, an open review flag alone
    refuses the dispatch (409 dispatch_blocked, kind review_flag); with an active lot hold as well both are listed;
    "Reviewed · keep plan" clears the flag and never the hold, which alone still refuses the dispatch; only releasing
    the hold lets it through. Every refusal writes nothing: the work order stays PLANNED and the lot's record keeps
    its quantity. The flag is raised by the service a confirmed fact calls (reviewflags.raise_for)."""
    from api import reviewflags
    from tests.api.conftest import sign_in, world_lot
    from tests.api.test_execution import committed, dispatch_body, prepared, work_order
    run, action, src, _ = prepared(client)  # signed in as the source manager, the lot's own
    cid, lot_id = action["case_id"], action["lot_id"]
    before = world_lot(run, lot_id)["qty"]

    def dispatch() -> list[str]:
        r = post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, committed(action)))
        if r.status_code == 200:
            return []
        assert (r.status_code, r.json()["error"]) == (409, "dispatch_blocked"), r.json()
        assert work_order(run["run_id"], cid)["state"] == "PLANNED" and world_lot(run, lot_id)["qty"] == before
        return [b["kind"] for b in r.json()["blocks"]]

    with db.tx() as conn:
        r = fresh(run["run_id"])
        assert reviewflags.raise_for(conn, r, [cases.case_row(conn, r["run_id"], cid)], "TEST:new-count") == [cid]
    assert dispatch() == ["review_flag"]
    hold = post(client, f"/api/lots/{lot_id}/holds", {"reason": "Crushed crates in the chiller"})
    assert hold.status_code == 200, hold.json()
    assert dispatch() == ["lot_hold", "review_flag"]
    flags = [f["flag_id"] for f in detail_of(client, cid)["review_flags"] if f["cleared_at"] is None]
    r = post(client, f"/api/cases/{cid}/review", {"flag_ids": flags, "note": "Checked the count: keep the plan"})
    assert r.json() == {"cleared": flags}
    held = detail_of(client, cid)["holds"]
    assert [(h["hold_id"], h["state"]) for h in held] == [(hold.json()["hold_id"], "ACTIVE")]  # never cleared
    assert dispatch() == ["lot_hold"]
    r = post(client, f"/api/holds/{hold.json()['hold_id']}/release", {"note": "Inspected: crates are fine"})
    assert r.status_code == 200, r.json()
    sign_in(client, src)
    assert dispatch() == []
    assert work_order(run["run_id"], cid)["state"] == "DISPATCHED"


# --- I: a closed case's "Awaiting verification" follows a confirmed fact (07-12; D-24, D-28) --------------------------

def test_i_awaiting_verification_changes_once_the_fact_is_confirmed(run):
    """I: the story case rejected while its Required count check blocked it reads "Awaiting verification". Its count,
    asked before the close, is answered with what the record holds and confirmed afterwards (a request outlives its
    case's state, D-17, D-22): the confirmed fact in the lot's scope reassesses the closed lot (07-12), so the closed
    card no longer reads "Awaiting verification": a successor carries the lot (lot_status null, `successor` set) and
    has no Required check on that fact."""
    from tests.api.conftest import world_lot
    from tests.api.test_evidence import answer, ask, confirm
    from tests.api.test_reassess import AWAITING, card_of, case_now, lot_status, reject_story
    from tests.api.verify_helpers import required_fixture
    check = required_fixture(run)
    cid = story(run)["case_id"]
    q = ask(run, cid, check["fact_id"])
    assert reject_story(run)["lot_status"] == "AT_RISK"
    assert lot_status(run, cid) == AWAITING
    on_record = {l: {"shelf_units": float(world_lot(run, l)["qty"]), "backroom_units": 0.0} for l in q["lot_ids"]}
    a = answer(run, q, on_record)  # the count finds what the record holds: the lot is still at risk
    out = confirm(run, seat(q["to_branch"]), a["signal_id"], a["revision"])
    assert out["fact_id"] is not None
    assert lot_status(run, cid) != AWAITING
    successor = card_of(run, cid)["successor"]
    assert (lot_status(run, cid), successor is not None) == (None, True)
    now = case_now(run, successor)
    assert (now["predecessor"], now["state"]) == (cid, "OPEN")
    from tests.api.test_selection import active
    assert check["fact_id"] not in {c["fact_id"] for c in active(run, successor)["fact_checks"]
                                    if not c["pick_feasible_if_alt"]}
