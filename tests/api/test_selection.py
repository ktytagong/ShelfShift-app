"""Select another plan (07-CONTEXT D-23, D-12; 04-API-CONTRACT "Select another plan", "Approvals are bound to the
plan key"; acceptance check 19).

Options are found from the stored result of the active version (`result.options`) and checked with `check_plan` on
today's raw input, never pasted keys; the story case is found by its tag. The selected plan's fact checks are the
engine's own (`engine.fact_checks` with `recommended` replaced by the selected key), never hand-built. A Required check
comes from the record (`verify_helpers.required_fixture`), never an injected fact. Nothing sleeps.
"""
import json
from dataclasses import replace

import pytest

from api import bookings, cases, db, lotholds, settings, workflow
from engine import fact_checks, from_dict
from engine.adjust import effective
from engine.limits import CAP_REASON, check_plan
from engine.model import KEEP, TRANSFER
from tests.api.conftest import fresh, owned_send, seat
from tests.api.test_cases import CHECK_KEYS_07
from tests.api.test_workflow import add_booking, call, raises, state, version
from tests.api.verify_helpers import required_fixture
from tests.api.verify_helpers import ack, shown, story

TRANSFER_HOLDS = {"DEMAND", "STORAGE", "TRUCK", "SOURCE_LOT"}  # spec §8 Lifecycle


def selection():
    """api.selection, imported where it is used (07-10 adds the module)."""
    from api import selection as module
    return module


def normal(value):
    """A value in the form a jsonb round trip gives it (the stored checks' form)."""
    return json.loads(db.js(value))


def active(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run["run_id"], case_id))


def raw_today(run, case_id):
    """The case's raw input today, without its own bookings, as Send's recheck reads it."""
    with db.tx() as conn:
        r = fresh(run["run_id"])
        return cases.current_input(conn, r, cases.case_row(conn, r["run_id"], case_id))


def feasible_others(run, case_id, kinds=None) -> list[str]:
    """The active version's option keys other than the recommended one that check_plan accepts on today's raw input,
    in the result's order (optionally only these kinds)."""
    rec, raw = active(run, case_id), raw_today(run, case_id)
    return [o["key"] for o in rec["result"]["options"]
            if o["key"] != rec["result"]["recommended"] and (kinds is None or o["kind"] in kinds)
            and not check_plan(raw, cases.parse_plan(o["key"]))]


def option_row(run, case_id, key) -> dict:
    return next(o for o in active(run, case_id)["result"]["options"] if o["key"] == key)


def engine_checks(run, case_id, key) -> list[dict]:
    """The engine's fact checks for that option of the active version: fact_checks on the stored input with
    `recommended` replaced by the key (research F-21; no engine change)."""
    rec = active(run, case_id)
    return normal(fact_checks(from_dict(rec["engine_input"]), {**rec["result"], "recommended": key}))


def card_of(run, case_id) -> dict:
    with db.tx() as conn:
        r = fresh(run["run_id"])
        return cases.card(conn, r, cases.case_row(conn, r["run_id"], case_id), [])


def detail_of(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.detail(conn, fresh(run["run_id"]), case_id)


def logged(run, case_id, type_) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM activity_log WHERE run_id = :r AND case_id = :c AND type = :t ORDER BY seq",
                       r=run["run_id"], c=case_id, t=type_)


def decision_rows(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM decisions WHERE run_id = :r AND case_id = :c ORDER BY decided_at, role",
                       r=run["run_id"], c=case_id)


def booked(run, case_id, states=("COMMITTED",)) -> set[str]:
    with db.tx() as conn:
        return {r["kind"] for r in db.rows(conn, "SELECT kind FROM reservations WHERE run_id = :r AND case_id = :c "
                                                 "AND state = ANY(:s)", r=run["run_id"], c=case_id, s=list(states))}


def resources_for(run, case_id, plan) -> list[dict]:
    """What the plan holds, read as the detail's approvals read it (today's effective input, no own bookings)."""
    return bookings.plan_resources(effective(raw_today(run, case_id)), plan)


def commit_with_acks(run, case_id, plan) -> dict:
    """Accept (a transfer) and approve the plan in force on the active version, each with its acknowledgement."""
    v, src = version(run, case_id), seat(story(run)["source_branch"])
    if plan.kind == TRANSFER:
        call(run, workflow.decide, seat(plan.dest), case_id, v, "accept", None, ack(run, case_id), shown(run, case_id))
    return call(run, workflow.decide, src, case_id, v, "approve", None, ack(run, case_id), shown(run, case_id))


# --- Task 1: the tracer (acceptance check 19, core) ------------------------------------------------------------------

def test_select_another_plan(run):
    """Acceptance check 19 (D-23): the source manager selects an engine option other than the recommended one (the
    first other row of result.options that check_plan accepts today). The answer names it; the card suggests it and
    names it as the selected plan; the detail's fact checks are that option's own; Send holds its resources; the
    accept and the approve (each acknowledging that plan's Advisory facts) record its key; the commit commits it."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v = version(run, cid)
    recommended = active(run, cid)["result"]["recommended"]
    key = feasible_others(run, cid)[0]
    plan, row = cases.parse_plan(key), option_row(run, cid, key)
    out = call(run, selection().select, src, cid, v, key)
    assert out == {"state": "OPEN", "selected": {"plan_key": key, "label": row["label"], "rec_version": v}}
    card = card_of(run, cid)
    assert (card["suggested_key"], card["suggested"]) == (key, row["label"])
    assert {k: card["selected_plan"][k] for k in ("plan_key", "label", "rec_version")} == \
        {"plan_key": key, "label": row["label"], "rec_version": v}
    assert card["selected_plan"]["by"] == src["account"] and card["selected_plan"]["at"]
    assert card["dest"] == (plan.dest if plan.kind == TRANSFER else None)
    d = detail_of(run, cid)
    assert [{k: x for k, x in c.items() if k not in CHECK_KEYS_07} for c in d["fact_checks"]] == \
        engine_checks(run, cid, key)  # the selected option's own checks, in the engine's order
    (entry,) = logged(run, cid, "plan_selected")
    assert (entry["payload"]["plan_key"], entry["payload"]["previous"], entry["payload"]["actor"]["account"]) == \
        (key, recommended, src["account"])
    assert owned_send(run, src, cid, v)["state"] == "PENDING_APPROVAL"
    (sent,) = logged(run, cid, "sent")
    assert sent["payload"]["plan"] == key  # Send held the plan in force
    assert detail_of(run, cid)["approvals"]["resources"] == resources_for(run, cid, plan)
    done = commit_with_acks(run, cid, plan)
    assert done["state"] == ("EXECUTED" if plan.kind == KEEP else "APPROVED")
    assert {r["plan_key"] for r in decision_rows(run, cid)} == {key}
    (committed,) = logged(run, cid, "committed")
    assert committed["payload"]["plan"] == key
    if plan.kind == TRANSFER:
        assert booked(run, cid, ("CONVERTED",)) == {"DEMAND"}  # the demand hold converts at commit (spec §8)
        assert booked(run, cid) == TRANSFER_HOLDS - {"DEMAND"} | ({"LOT_PRICE"} if plan.pct else set())
    assert state(run, cid) == done["state"]


def test_fact_checks_identity(run):
    """F-21's identity: the selected plan's check function, given the recommended key itself, returns the stored
    fact checks of the active version, so a selected option's checks are the engine's own computation."""
    case = story(run)
    rec = active(run, case["case_id"])
    got = selection()._checks(from_dict(rec["engine_input"]), rec["result"], rec["result"]["recommended"])
    assert normal(got) == rec["fact_checks"]


# --- Task 2: the stale approval, the selection rules, the exit from a Required block, the invariant ------------------

STALE_PLAN = "The approvals in this round were for another plan. Send it for approval again."
NOT_AN_OPTION = "Choose one of the plans the engine generated for this version"
BLOCKED = "This plan can't be committed yet; see what blocks it."


def selected_column(run, case_id):
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)["selected"]


def held(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM reservations WHERE run_id = :r AND case_id = :c AND state = 'HELD'",
                       r=run["run_id"], c=case_id)


def round_of(run, case_id) -> int:
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)["round"]


def test_a_stale_approval_cannot_commit(run):
    """D-23 (the user's test): the source approves the recommended transfer on round n; the owner then selects another
    transfer option, which cancels the round (holds released, approval_cancelled "plan selected", OPEN). Send opens
    round n + 1; the destination's accept leaves it PENDING_APPROVAL, because the old approval belongs to round n and
    never counts. A decision of the current round forced to the old plan key makes the commit 409 stale_plan, with
    nothing booked and the refused approve leaving no row."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v = version(run, cid)
    recommended = active(run, cid)["result"]["recommended"]
    assert cases.parse_plan(recommended).kind == TRANSFER, "hand-back: the story's pick is not a transfer"
    other = feasible_others(run, cid, (TRANSFER,))[0]
    plan = cases.parse_plan(other)
    owned_send(run, src, cid, v)
    n = round_of(run, cid)
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "PENDING_APPROVAL"}
    assert call(run, selection().select, src, cid, v, other)["state"] == "OPEN"
    assert held(run, cid) == [] and state(run, cid) == "OPEN"
    (cancelled,) = logged(run, cid, "approval_cancelled")
    assert (cancelled["payload"]["reason"], cancelled["payload"]["round"]) == ("plan selected", n)
    assert owned_send(run, src, cid, v) == {"state": "PENDING_APPROVAL", "round": n + 1}
    assert call(run, workflow.decide, seat(plan.dest), cid, v, "accept", None, ack(run, cid), shown(run, cid)) == \
        {"state": "PENDING_APPROVAL"}  # the source's approval of round n does not count
    with db.tx() as conn:  # a decision of the current round given for the old plan (forced: the test's own write)
        db.run(conn, "UPDATE decisions SET plan_key = :k WHERE run_id = :r AND case_id = :c AND round = :n",
               k=recommended, r=run["run_id"], c=cid, n=n + 1)
    e = raises(call, run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid))
    assert (e.status, e.code, e.message) == (409, "stale_plan", STALE_PLAN)
    assert state(run, cid) == "PENDING_APPROVAL" and booked(run, cid, ("COMMITTED", "CONVERTED")) == set()
    assert {(d["round"], d["role"]) for d in decision_rows(run, cid)} == {(n, "source"), (n + 1, "destination")}


def test_a_decision_from_before_phase_7_counts_as_the_recommended_plan(run):
    """A decision row with no plan key (stored before Phase 7) counts as the recommended plan's: with no selection the
    commit goes ahead (D-23, the NULL rule)."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v, plan = version(run, cid), cases.parse_plan(active(run, cid)["result"]["recommended"])
    owned_send(run, src, cid, v)
    call(run, workflow.decide, seat(plan.dest), cid, v, "accept", None, ack(run, cid), shown(run, cid))
    with db.tx() as conn:
        db.run(conn, "UPDATE decisions SET plan_key = NULL WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=cid)
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "APPROVED"}


def test_selection_rules(run):
    """D-23 and the contract row: a key outside result.options (a hand-made one with another quantity included) is
    400 not_an_option; an option check_plan refuses today is 409 conflict with the engine's reasons; selecting the
    recommended key clears the selection; a new version (a settings write) clears it; another seat is 403; an old
    version is 409 stale_version; a REJECTED case is 409 wrong_state."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v = version(run, cid)
    result = active(run, cid)["result"]
    recommended, keys = result["recommended"], {o["key"] for o in result["options"]}
    rplan = cases.parse_plan(recommended)
    hand_made = replace(rplan, qty=rplan.qty + 1).key
    assert hand_made not in keys
    for bad in (hand_made, "tr:Nowhere:R-99:1:hold", 7, None):
        e = raises(call, run, selection().select, src, cid, v, bad)
        assert (e.status, e.code, e.message) == (400, "not_an_option", NOT_AN_OPTION), bad
    for other_seat in (seat(rplan.dest), seat(None)):
        assert raises(call, run, selection().select, other_seat, cid, v, "keep").status == 403
    e = raises(call, run, selection().select, src, cid, "not-a-version", "keep")
    assert (e.status, e.code) == (409, "stale_version")
    # an option check_plan refuses today: its run's truck is full (another case's booking, a test value)
    other = feasible_others(run, cid, (TRANSFER,))[0]
    oplan = cases.parse_plan(other)
    eff = effective(raw_today(run, cid))
    trip = next(r for r in eff.runs if r.run_code == oplan.run_code and r.dest == oplan.dest)
    taken = sum(t for code, t in eff.bookings.run_totes if code == trip.run_code)
    add_booking(run["run_id"], "OTHER", "TRUCK", f"{trip.run_code}|{trip.day}", trip.day, trip.spare_totes - taken)
    e = raises(call, run, selection().select, src, cid, v, other)
    assert (e.status, e.code) == (409, "conflict") and CAP_REASON["truck"] in e.extra["reasons"]
    assert selected_column(run, cid) is None and logged(run, cid, "plan_selected") == []
    # Keep moves nothing: selected, then the recommended key clears it
    assert call(run, selection().select, src, cid, v, "keep")["selected"]["plan_key"] == "keep"
    assert card_of(run, cid)["suggested_key"] == "keep"
    assert call(run, selection().select, src, cid, v, recommended) == {"state": "OPEN", "selected": None}
    assert selected_column(run, cid) is None
    assert (card_of(run, cid)["suggested_key"], card_of(run, cid)["selected_plan"]) == (recommended, None)
    assert [e["payload"]["cleared"] for e in logged(run, cid, "plan_selected")] == [False, True]
    # a new version clears a selection
    call(run, selection().select, src, cid, v, "keep")
    with db.tx() as conn:
        r = fresh(run["run_id"])
        settings.write_settings(conn, r, seat(None), {"waste_price": 0.5}, r["version"])
    v2 = version(run, cid)
    assert v2 != v and selected_column(run, cid) is None
    assert card_of(run, cid)["suggested_key"] == active(run, cid)["result"]["recommended"]
    # a REJECTED case
    call(run, workflow.reject, src, cid, "Not this week")
    e = raises(call, run, selection().select, src, cid, v2, "keep")
    assert (e.status, e.code) == (409, "wrong_state")


def test_a_selected_plan_escapes_a_required_block(run):
    """D-12's "another plan" exit (acceptance check 19 with check 7): with the Required fixture blocking the
    recommended transfer, the detail's options mark it with the fact and Keep with none (both as the engine's own
    checks for those options classify them); next_actions lists select_plan. Selecting Keep never clears a lot hold:
    the hold placed before stays ACTIVE and refuses Keep's commit; once its manager releases it, approving Keep
    executes it."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    check = required_fixture(run)
    v = version(run, cid)
    recommended = active(run, cid)["result"]["recommended"]
    d = detail_of(run, cid)
    options = {o["key"]: o for o in d["options"]}
    assert set(options) == {o["key"] for o in active(run, cid)["result"]["options"]}
    assert check["fact_id"] in options[recommended]["required_fact_ids"] and options[recommended]["recommended"]
    assert options["keep"]["feasible"] and options["keep"]["required_fact_ids"] == []
    for key in (recommended, "keep"):  # the option list's class is the engine's own for that option
        required = sorted(c["fact_id"] for c in engine_checks(run, cid, key) if not c["pick_feasible_if_alt"])
        assert options[key]["required_fact_ids"] == required, key  # sorted by fact id
    assert "select_plan" in d["commitment"]["next_actions"]
    hold = call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    assert call(run, selection().select, src, cid, v, "keep")["selected"]["plan_key"] == "keep"
    d = detail_of(run, cid)
    assert [h["state"] for h in d["holds"]] == ["ACTIVE"]  # the selection never touches a hold (D-13)
    assert [b["kind"] for b in d["commitment"]["blocks"]] == ["lot_hold"]  # Keep depends on no unverified fact
    assert {o["key"]: o["selected"] for o in d["options"]}["keep"] is True
    owned_send(run, src, cid, v)
    e = raises(call, run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid))
    assert (e.status, e.code, e.message) == (409, "commitment_blocked", BLOCKED)
    assert [b["kind"] for b in e.extra["blocks"]] == ["lot_hold"]
    call(run, lotholds.release, src, hold["hold_id"], "Inspected: crates are fine")
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "EXECUTED"}
    (committed,) = logged(run, cid, "committed")
    assert committed["payload"]["plan"] == "keep"


@pytest.mark.parametrize("with_selection", [False, True])
def test_every_consumer_agrees_on_the_plan_in_force(run, with_selection):
    """The assumption-delta invariant (07-10): with and without a selection, the card's suggested_key, the detail's
    acknowledgement and fact checks, Send's held plan and resources, each decision's plan_key and the committed plan
    all name one key: the plan in force."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    v = version(run, cid)
    key = active(run, cid)["result"]["recommended"]
    if with_selection:
        key = feasible_others(run, cid, (TRANSFER,))[0]
        call(run, selection().select, src, cid, v, key)
    plan = cases.parse_plan(key)
    card, d = card_of(run, cid), detail_of(run, cid)
    assert card["suggested_key"] == d["suggested_key"] == key
    assert card["dest"] == d["dest"] == plan.dest
    assert [{k: x for k, x in c.items() if k not in CHECK_KEYS_07} for c in d["fact_checks"]] == \
        engine_checks(run, cid, key)
    assert d["commitment"]["ack"] is None or d["commitment"]["ack"]["plan_key"] == key
    owned_send(run, src, cid, v)
    assert logged(run, cid, "sent")[-1]["payload"]["plan"] == key
    assert detail_of(run, cid)["approvals"]["resources"] == resources_for(run, cid, plan)
    assert commit_with_acks(run, cid, plan)["state"] == "APPROVED"
    assert {r["plan_key"] for r in decision_rows(run, cid)} == {key}
    assert logged(run, cid, "committed")[-1]["payload"]["plan"] == key


# --- 07-11: Cancel approved plan, stored CLOSED with closed_as CANCELLED (07-CONTEXT D-23, D-25; check 20) -----------

CANCEL_WHY = "The destination's chiller failed"


def closing():
    """api.closing, imported where it is used (07-11 adds the module)."""
    from api import closing as module
    return module


def approved_story(run) -> tuple[dict, object]:
    """The story case sent and approved on its recommended plan (a transfer: the destination accepts and the source
    approves, each acknowledging the plan's Advisory facts): (its cases row, the plan)."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    plan = cases.parse_plan(active(run, cid)["result"]["recommended"])
    assert plan.kind == TRANSFER, "hand-back: the story case's recommended plan is no transfer"
    owned_send(run, src, cid, version(run, cid))
    assert commit_with_acks(run, cid, plan)["state"] == "APPROVED"
    return story(run), plan


def case_now(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)


def pending_inbound(run, case_id) -> list[dict]:
    """The case's pending delivery at the destination, as the workspace reads it."""
    from tests.api.conftest import world_rows
    return world_rows(run, "inbound", "source_case_id = :c", c=case_id)


def other_branch(run, *not_these) -> str:
    from api import refdata
    return next(b for b in refdata.get(run["dataset_version"]).branches if b not in not_these)


def test_cancel_approved_plan(run):
    """Acceptance check 20 (D-23, D-25), APPROVED: only a person authorised to approve the plan cancels it (HQ and an
    unrelated store are 403), with a reason (blank is 400). The answer is {state: CLOSED, closed_as: CANCELLED}; the
    case row keeps who, when and why; its bookings are RELEASED and its pending delivery gone; the round's decisions
    stay, marked cancelled; the log has `cancelled` with the reason, who and the state it left. Prepare then refuses
    (409 wrong_state), and the card reads state CLOSED, closed_as CANCELLED, column Resolved."""
    case, plan = approved_story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    assert booked(run, cid, ("COMMITTED", "CONVERTED")) >= TRANSFER_HOLDS and pending_inbound(run, cid)
    for who in (seat(None), seat(other_branch(run, case["source_branch"], plan.dest))):
        e = raises(call, run, closing().cancel, who, cid, CANCEL_WHY)
        assert (e.status, e.code) == (403, "forbidden")
    e = raises(call, run, closing().cancel, src, cid, "  ")
    assert (e.status, e.code, e.message) == (400, "bad_request", "A reason is required")
    assert state(run, cid) == "APPROVED"  # a refusal writes nothing
    assert call(run, closing().cancel, src, cid, CANCEL_WHY) == {"state": "CLOSED", "closed_as": "CANCELLED"}
    row = case_now(run, cid)
    assert (row["state"], row["closed_as"], row["closed_by"], row["cancel_reason"], row["reason"]) == (
        "CLOSED", "CANCELLED", src["account"], CANCEL_WHY, f"Cancelled: {CANCEL_WHY}")
    assert row["closed_at"] is not None
    assert booked(run, cid, ("HELD", "COMMITTED", "CONVERTED")) == set()
    assert booked(run, cid, ("RELEASED",)) >= TRANSFER_HOLDS
    assert pending_inbound(run, cid) == []
    rows = [d for d in decision_rows(run, cid) if d["round"] == case["round"]]
    assert {d["role"] for d in rows} == {"source", "destination"}  # still there
    assert all(d["cancelled_at"] is not None for d in rows)
    (entry,) = logged(run, cid, "cancelled")
    p = entry["payload"]
    assert (p["reason"], p["by"], p["from_state"], p["closed_as"], p["plan"]) == (
        CANCEL_WHY, src["account"], "APPROVED", "CANCELLED", plan.key)
    assert p["actor"]["account"] == src["account"]
    e = raises(call, run, workflow.prepare, src, cid)
    assert (e.status, e.code) == (409, "wrong_state")
    e = raises(call, run, closing().cancel, src, cid, CANCEL_WHY)  # a cancelled case is no longer approved work
    assert (e.status, e.code) == (409, "wrong_state")
    card = card_of(run, cid)
    assert (card["state"], card["closed_as"], card["column"]) == ("CLOSED", "CANCELLED", "Resolved")


def test_the_destination_manager_cancels_a_transfer(run):
    """D-23: the destination manager of a transfer is authorised to approve it, so may cancel it too."""
    case, plan = approved_story(run)
    out = call(run, closing().cancel, seat(plan.dest), case["case_id"], "No room after all")
    assert out == {"state": "CLOSED", "closed_as": "CANCELLED"}
    assert case_now(run, case["case_id"])["closed_by"] == plan.dest


def insert_open_case(run, case: dict, case_id: str) -> None:
    with db.tx() as conn:
        db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, tag, state, opened_day) "
                     "VALUES (:r, :c, :c, :l, :b, :s, NULL, 'OPEN', :d)", r=run["run_id"], c=case_id,
               l=case["lot_id"], b=case["source_branch"], s=case["sku"], d=fresh(run["run_id"])["day"])


def test_cancel_frees_the_lot_but_one_open_case_stays(run):
    """The D-25 user test: while the approved case is live, no other case for its lot can open; after the cancel one
    can (the index one_open_case_per_lot, unchanged, frees a CLOSED case's lot), and a second open case for that lot
    is still impossible."""
    case, _ = approved_story(run)
    with pytest.raises(Exception, match="one_open_case_per_lot"):
        insert_open_case(run, case, "TEST-NEXT")
    call(run, closing().cancel, seat(case["source_branch"]), case["case_id"], CANCEL_WHY)
    insert_open_case(run, case, "TEST-NEXT")
    with pytest.raises(Exception, match="one_open_case_per_lot"):
        insert_open_case(run, case, "TEST-NEXT-2")


def work_orders(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                             "ORDER BY action_id", r=run["run_id"], c=case_id)


def test_cancel_prepared_work(run):
    """Acceptance check 20, PREPARED (D-17, D-23; research F-18): cancelling prepared work keeps its work orders, marked
    CANCELLED; the world step really runs once (advance_once) and never executes them: no moved lot on the record, no
    TRANSFER_RECEIVED event names the case's moved lot, and the case stays CLOSED (never EXECUTED)."""
    from tests.api.conftest import world_lot
    from tests.api.test_advance import advance_once
    case, plan = approved_story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    assert call(run, workflow.prepare, src, cid) == {"state": "PREPARED"}
    before = work_orders(run, cid)
    assert before and {a["state"] for a in before} == {"PLANNED"}
    moved = bookings.moved_lot_id(cid, case["round"], plan.dest)
    assert call(run, closing().cancel, src, cid, CANCEL_WHY) == {"state": "CLOSED", "closed_as": "CANCELLED"}
    after = work_orders(run, cid)
    assert [a["action_id"] for a in after] == [a["action_id"] for a in before]  # kept in history
    assert {a["state"] for a in after} == {"CANCELLED"}
    assert logged(run, cid, "cancelled")[0]["payload"]["from_state"] == "PREPARED"
    assert advance_once(run)["advanced"] is True
    assert world_lot(run, moved) is None  # nothing was shipped
    with db.tx() as conn:
        received = db.rows(conn, "SELECT * FROM world_events WHERE run_id = :r AND kind = 'TRANSFER_RECEIVED' "
                                 "AND payload->>'lot_id' LIKE :m", r=run["run_id"], m=f"{cid}>%")
    assert received == []
    assert {a["state"] for a in work_orders(run, cid)} == {"CANCELLED"}  # never DONE
    row = case_now(run, cid)
    assert (row["state"], row["closed_as"]) == ("CLOSED", "CANCELLED")
    assert logged(run, cid, "executed") == []


def test_cancel_is_offered_when_the_plan_cannot_continue(run):
    """D-17, D-23: an OPEN case blocked by a lot hold lists close_proposal and not cancel_approved; an APPROVED case
    whose Reviewed · keep plan is refused (cannot_continue: a hold on its lot) lists cancel_approved; so does a
    PREPARED case. The list states what exists; the seat check stays the route's."""
    from api import reviewflags
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    hold = call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    acts = detail_of(run, cid)["commitment"]["next_actions"]
    assert "close_proposal" in acts and "cancel_approved" not in acts
    call(run, lotholds.release, src, hold["hold_id"], "Inspected: crates are fine")
    case, _ = approved_story(run)
    assert detail_of(run, cid)["commitment"]["next_actions"] == ["cancel_approved"]  # nothing blocks: it still exists
    hold = call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Leaking cartons")
    with db.tx() as conn:
        (flagged,) = reviewflags.raise_for(conn, fresh(run["run_id"]), [cases.case_row(conn, run["run_id"], cid)],
                                           "TEST-FACT")
    flag_ids = [f["flag_id"] for f in detail_of(run, cid)["review_flags"]]
    e = raises(call, run, reviewflags.keep, src, cid, flag_ids, "Looked at it")
    assert (e.status, e.code) == (409, "cannot_continue")
    acts = detail_of(run, cid)["commitment"]["next_actions"]
    assert acts[-1] == "cancel_approved" and "close_proposal" not in acts
    call(run, lotholds.release, src, hold["hold_id"], "Cartons replaced")
    assert call(run, workflow.prepare, src, cid) == {"state": "PREPARED"}
    assert detail_of(run, cid)["commitment"]["next_actions"] == ["cancel_approved"]


# 09-10: expected plan bindings on human evidence Sends, including combined parts.
@pytest.mark.parametrize("combined", [False, True])
def test_evidence_send_rechecks_the_selected_plan_binding(run, combined):
    from api import signals
    from api.errors import ApiError
    from tests.api.test_evidence import phantom, table
    from tests.api.test_requests import pair_fixture

    case = story(run)
    if combined:
        case, count, expiry = pair_fixture(run)
        kwargs = {"fact_ids": [count["fact_id"], expiry["fact_id"]]}
        fact_id = None
    else:
        fact_id, kwargs = phantom(run, case["case_id"])["fact_id"], {}
    cid, actor = case["case_id"], seat(case["source_branch"])
    rec = active(run, cid)
    selected = feasible_others(run, cid)[0]
    call(run, selection().select, actor, cid, rec["rec_version"], selected)
    with pytest.raises(ApiError) as caught:
        db.in_tx(lambda conn: signals.request_evidence(conn, db.lock_run(conn, run["run_id"]), actor,
            cid, rec["rec_version"], fact_id, **kwargs, message="Keep the human binding.",
            plan_key=rec["result"]["recommended"]))
    assert (caught.value.status, caught.value.code) == (409, "stale_plan")
    assert table(run["run_id"], "evidence_requests") == []


@pytest.mark.parametrize("combined", [False, True])
def test_selected_only_flagged_facts_can_be_sent_with_their_exact_context(run, combined):
    from api import signals
    from tests.api.test_evidence import table
    from tests.api.test_requests import pair_fixture

    case, count, expiry = pair_fixture(run)
    cid, actor = case["case_id"], seat(case["source_branch"])
    rec = active(run, cid)
    # Isolate the stored-plan lookup using real engine checks, as the existing
    # pair fixture derives its count part from the engine's expiry check.
    alternatives = [(key, engine_checks(run, cid, key)) for key in feasible_others(run, cid, (TRANSFER,))]
    candidate, checks = next((key, fs) for key, fs in alternatives
                             if any(f["fact_id"] == expiry["fact_id"] and f["changes_plan"] for f in fs))
    call(run, selection().select, actor, cid, rec["rec_version"], candidate)
    selected_expiry = next(f for f in checks if f["fact_id"] == expiry["fact_id"])
    selected_count = {**selected_expiry, "fact_id": count["fact_id"], "kind": "STALE_COUNT"}
    with db.tx() as conn:
        # Test-owned stored-check differences: selection has both parts,
        # recommendation has neither. No seed, fixture or engine is changed.
        chosen = selected_column(run, cid)
        db.run(conn, "UPDATE cases SET selected = CAST(:s AS jsonb) WHERE run_id = :r AND case_id = :c",
               s=db.js({**chosen, "fact_checks": [selected_count, selected_expiry]}), r=run["run_id"], c=cid)
        db.run(conn, "UPDATE recommendations SET fact_checks = '[]' WHERE run_id = :r AND case_id = :c "
                     "AND rec_version = :v", r=run["run_id"], c=cid, v=rec["rec_version"])
    message = " Exact selected-plan request.\nPlease keep the parts separate. "
    q = db.in_tx(lambda conn: signals.request_evidence(conn, db.lock_run(conn, run["run_id"]), actor, cid,
        rec["rec_version"], None if combined else selected_expiry["fact_id"],
        fact_ids=[selected_count["fact_id"], selected_expiry["fact_id"]] if combined else None,
        message=message, plan_key=candidate))
    stored = table(run["run_id"], "evidence_requests")
    assert len(stored) == (2 if combined else 1)
    assert all(r["message"] == message and r["plan_key"] == candidate for r in stored)
    assert len({r["request_group"] or r["request_id"] for r in stored}) == 1
    assert all(r["request_context"]["branch"] == selected_expiry["branch"]
               and r["request_context"]["lot_ids"] == selected_expiry["lot_ids"] for r in stored)
    assert q["message"] == message and q["plan_key"] == candidate
    d = detail_of(run, cid)
    assert d["selected_plan"]["plan_key"] == candidate and d["result"]["recommended"] == rec["result"]["recommended"]
