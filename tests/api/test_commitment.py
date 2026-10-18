"""The commitment rule (07-CONTEXT D-12, D-13, D-17, D-26; 04-API-CONTRACT "The commitment rule and the
acknowledgement"; acceptance checks 7, 8, 10, 11 and 18).

Cases are found by tag (the story case) and facts by kind, never by a key or a lot id from the seed. A Required check
comes from the record (`verify_helpers.required_fixture`: an explicit zero sales row yesterday at the source), never
from an injected fact (`add_facts`). Holds are placed through api/lotholds.py by the lot's manager. Nothing sleeps.
"""
from dataclasses import replace

import pytest

from api import cases, config, db, lotholds, sandbox, settings, signals, workflow
from engine import from_dict
from engine.adjust import effective
from engine.factchecks import _with_alt
from engine.limits import check_plan
from engine.model import MARKDOWN, TRANSFER
from tests.api.conftest import fresh, owned_send, seat, world_lot
from tests.api.test_workflow import call, case_with_pick, insert_case, pick, raises, state, version
from tests.api.verify_helpers import ack, approve_with_ack, required_fixture, shown, story

BLOCKED = "This plan can't be committed yet; see what blocks it."
ACK_FIRST = "Acknowledge the unverified facts before approving"
ACK_AGAIN = "These facts changed · acknowledge again"


def active(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run["run_id"], case_id))


def engine_reasons(rec: dict, check: dict, plan) -> list[str]:
    """check_plan's reasons for the plan under the fact's alternative, as the engine's fact check computes them
    (engine/factchecks.py: the effective input, the alternative applied, applied_overrides cleared)."""
    alt = _with_alt(effective(from_dict(rec["engine_input"])), check)
    return list(check_plan(replace(alt, applied_overrides=()), plan))


def rows(run, table, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=case_id)


def decided(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.decided_roles(conn, cases.case_row(conn, run["run_id"], case_id))


def set_state(run, case_id, state_) -> None:
    """A state set directly: the test's own setup (an approval made before the rule existed)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = :s WHERE run_id = :r AND case_id = :c", s=state_, r=run["run_id"],
               c=case_id)


def test_required_blocks_commit_and_prepare(run):
    """Acceptance check 7 (D-12): a Required check (pick_feasible_if_alt false on the active version) lets Send through
    and refuses the final approval with 409 commitment_blocked: one block with the fact, its lots, the fact's branch
    as owner, check_plan's reason under the alternative word for word and the resolution; nothing is committed and the
    refused call leaves no decision. An approval made before the rule (set directly) is refused at Prepare the same
    way."""
    case = story(run)
    cid, src_branch = case["case_id"], case["source_branch"]
    check = required_fixture(run)
    plan, v = pick(run, cid), version(run, cid)
    assert plan.kind == TRANSFER
    src, dest = seat(src_branch), seat(plan.dest)
    assert owned_send(run, src, cid, v)["state"] == "PENDING_APPROVAL"  # Send is never blocked
    call(run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), shown(run, cid))
    reasons = engine_reasons(active(run, cid), check, plan)
    assert reasons  # the alternative breaks a hard limit
    expected = [{"kind": "required_check", "fact_id": check["fact_id"], "lot_ids": check["lot_ids"],
                 "owner_branch": src_branch, "reason": "; ".join(reasons),
                 "resolution": f"Confirm a count from {src_branch}"}]
    e = raises(call, run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid))
    assert (e.status, e.code, e.message) == (409, "commitment_blocked", BLOCKED)
    assert e.extra["blocks"] == expected
    assert state(run, cid) == "PENDING_APPROVAL" and decided(run, cid) == {"destination": "accept"}
    assert {r["state"] for r in rows(run, "reservations", cid)} == {"HELD"}  # nothing turned into a booking
    set_state(run, cid, "APPROVED")  # an approval made before the rule existed
    e = raises(call, run, workflow.prepare, src, cid)
    assert (e.status, e.code, e.message, e.extra["blocks"]) == (409, "commitment_blocked", BLOCKED, expected)
    assert state(run, cid) == "APPROVED" and rows(run, "world_actions", cid) == []


def test_send_is_never_blocked(run):
    """D-12: an active hold on the case lot, and then a Required check as well, never refuse Send."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    assert owned_send(run, src, cid, version(run, cid))["state"] == "PENDING_APPROVAL"
    required_fixture(run)  # a new version: the pending round is cancelled, the case is OPEN again
    assert state(run, cid) == "OPEN"
    assert owned_send(run, src, cid, version(run, cid))["state"] == "PENDING_APPROVAL"


# --- the acknowledgement (Task 2; D-12, D-26, acceptance check 11) ---------------------------------------------------

def advisory(run, case_id) -> list[dict]:
    """The active version's Advisory checks (changes_plan and pick_feasible_if_alt), in the engine's order."""
    return [c for c in active(run, case_id)["fact_checks"] if c["changes_plan"] and c["pick_feasible_if_alt"]]


def decision_rows(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM decisions WHERE run_id = :r AND case_id = :c ORDER BY decided_at, role",
                       r=run["run_id"], c=case_id)


def logged(run, case_id, type_) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM activity_log WHERE run_id = :r AND case_id = :c AND type = :t ORDER BY seq",
                       r=run["run_id"], c=case_id, t=type_)


def test_advisory_ack(run):
    """Acceptance check 11 and D-26's tests: on the story case (its destination phantom fact is Advisory) an accept
    without the acknowledgement is 409 ack_required with the expected set; with it the decision row and its log entry
    hold it and the plan key; an acknowledgement for another version (a settings write made a new one) is stale; a
    valid one commits, subject to every other check (a lot hold still refuses it)."""
    case = story(run)
    cid, src_branch = case["case_id"], case["source_branch"]
    plan, v = pick(run, cid), version(run, cid)
    (fact,) = advisory(run, cid)
    assert (fact["kind"], fact["branch"]) == ("PHANTOM_STOCK", plan.dest), "hand-back: the story's Advisory fact moved"
    src, dest = seat(src_branch), seat(plan.dest)
    expected = {"rec_version": v, "plan_key": plan.key, "fact_ids": [fact["fact_id"]]}
    assert ack(run, cid) == expected
    owned_send(run, src, cid, v)
    e = raises(call, run, workflow.decide, dest, cid, v, "accept", None, None, plan.key)
    assert (e.status, e.code, e.message) == (409, "ack_required", ACK_FIRST)
    assert e.extra == {"advisory": [{"fact_id": fact["fact_id"], "kind": fact["kind"], "lot_ids": fact["lot_ids"],
                                     "branch": fact["branch"], "owner_branch": fact["branch"]}],
                       "rec_version": v, "plan_key": plan.key, "stale": False}
    assert decision_rows(run, cid) == []
    assert call(run, workflow.decide, dest, cid, v, "accept", None, expected, plan.key) == \
        {"state": "PENDING_APPROVAL"}
    (row,) = decision_rows(run, cid)
    assert (row["role"], row["acknowledged"], row["plan_key"]) == ("destination", expected, plan.key)
    (entry,) = logged(run, cid, "decision")
    assert (entry["payload"]["acknowledged"], entry["payload"]["plan_key"]) == (expected, plan.key)
    with db.tx() as conn:  # a settings write makes a new version: the round is cancelled
        r = fresh(run["run_id"])
        settings.write_settings(conn, r, seat(None), {"waste_price": 0.5}, r["version"])
    v2 = version(run, cid)
    assert v2 != v and state(run, cid) == "OPEN"
    assert advisory(run, cid), "hand-back: the new version has no Advisory fact"
    fresh_ack, plan2 = ack(run, cid), pick(run, cid)  # the new version's plan in force (its key may differ)
    assert (fresh_ack["rec_version"], fresh_ack["plan_key"]) == (v2, plan2.key)
    owned_send(run, src, cid, v2)
    call(run, workflow.decide, dest, cid, v2, "accept", None, fresh_ack, plan2.key)
    e = raises(call, run, workflow.decide, src, cid, v2, "approve", None, expected, plan2.key)  # the old version's
    assert (e.status, e.code, e.message, e.extra["stale"], e.extra["rec_version"]) == (
        409, "ack_required", ACK_AGAIN, True, v2)
    for wrong in ({**fresh_ack, "plan_key": "keep"}, {**fresh_ack, "fact_ids": []},
                  {**fresh_ack, "fact_ids": [*fresh_ack["fact_ids"], "Nowhere:phantom"]}, "yes"):
        e = raises(call, run, workflow.decide, src, cid, v2, "approve", None, wrong, plan2.key)
        assert (e.code, e.extra["stale"]) == ("ack_required", True), wrong
    hold = call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    e = raises(call, run, workflow.decide, src, cid, v2, "approve", None, fresh_ack, plan2.key)  # a hold refuses it
    assert (e.code, [b["kind"] for b in e.extra["blocks"]]) == ("commitment_blocked", ["lot_hold"])
    call(run, lotholds.release, src, hold["hold_id"], "Crates inspected, stock fine")
    assert call(run, workflow.decide, src, cid, v2, "approve", None, fresh_ack, plan2.key) == {"state": "APPROVED"}
    source_row = next(r for r in decision_rows(run, cid) if r["rec_version"] == v2 and r["role"] == "source")
    assert (source_row["acknowledged"], source_row["plan_key"]) == (fresh_ack, plan2.key)


def test_no_advisory_needs_no_ack(run):
    """D-26 boundary: a plan in force with no Advisory fact approves without `acknowledged`; the decision still
    records its plan key, and no acknowledgement."""
    case = case_with_pick(run, MARKDOWN)
    cid, src = case["case_id"], seat(case["source_branch"])
    assert advisory(run, cid) == [] and ack(run, cid) is None, "hand-back: the markdown case has an Advisory fact"
    v, plan = version(run, cid), pick(run, cid)
    owned_send(run, src, cid, v)
    assert call(run, workflow.decide, src, cid, v, "approve", None, None, plan.key)["state"] == "APPROVED"
    (row,) = decision_rows(run, cid)
    assert (row["acknowledged"], row["plan_key"]) == (None, plan.key)


def clone() -> dict:
    """Another fresh workspace cloned from the ACTIVE template (as the `run` fixture makes one)."""
    with db.tx() as conn:
        rid = sandbox.clone(conn, sandbox.active_template(conn))
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid)


def only_story_open(run) -> str:
    """The test's own setup: every other OPEN case waits, so the warm-up acts on the story case alone."""
    cid = story(run)["case_id"]
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = 'WAITING' WHERE run_id = :r AND state = 'OPEN' AND case_id <> :c",
               r=run["run_id"], c=cid)
    return cid


def warm_up(run) -> list[str]:
    with db.tx() as conn:
        return sandbox.autopilot(conn, db.lock_run(conn, run["run_id"]))


def test_the_warm_up_acknowledges(run):
    """Research F-11: the template warm-up acknowledges each version's Advisory set, logged on the warm-up's own
    decisions; on a Required check or a lot hold it stops with a TemplateError naming the case (D-14: nothing is
    tuned to pass)."""
    cid = only_story_open(run)
    expected = ack(run, cid)
    assert expected is not None, "hand-back: the story case has no Advisory fact"
    assert warm_up(run) == [cid] and state(run, cid) == "PREPARED"
    rows_ = decision_rows(run, cid)
    assert sorted(r["role"] for r in rows_) == ["destination", "source"]
    assert all(r["acknowledged"] == expected and r["session_id"] == "warmup" for r in rows_)
    assert [e["payload"]["acknowledged"] for e in logged(run, cid, "decision")] == [expected, expected]
    blocked = clone()
    bid = only_story_open(blocked)
    required_fixture(blocked)
    with pytest.raises(sandbox.TemplateError) as e:
        warm_up(blocked)
    assert bid in str(e.value) and "commitment_blocked" in str(e.value)
    held = clone()
    hid = only_story_open(held)
    case = story(held)
    call(held, lotholds.place, seat(case["source_branch"]), case["lot_id"], "CONDITION", "Crushed crates")
    with pytest.raises(sandbox.TemplateError) as e:
        warm_up(held)
    assert hid in str(e.value) and "commitment_blocked" in str(e.value)


# --- the case's commitment state, next actions and holds (Task 3; D-12, D-13, acceptance checks 8 and 10) -----------

BLOCKED_LABEL = "Blocked · stock/expiry verification required"


def detail_of(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.detail(conn, fresh(run["run_id"]), case_id)


def card_of(run, case_id) -> dict:
    with db.tx() as conn:
        return next(c for c in cases.board(conn, fresh(run["run_id"])) if c["case_id"] == case_id)


def check_in(detail: dict, fact_id: str) -> dict:
    return next(c for c in detail["fact_checks"] if c["fact_id"] == fact_id)


def kinds(blocks) -> list[str]:
    return [b["kind"] for b in blocks]


def test_the_case_shows_its_commitment_state(run):
    """The card carries `lot_hold` and `required_check` badges and `blocked` while blocked; the detail's commitment
    is what the refusal would list now, its `ack` the expected acknowledgement (null with no Advisory fact), and each
    fact check carries its class, owner, requests left, adoptable, and (Required only) check_plan's reasons. A case
    with no fact check and no hold reads no blocks and needs no acknowledgement (VER-06 empty)."""
    case = story(run)
    cid, src_branch = case["case_id"], case["source_branch"]
    plain = detail_of(run, cid)
    assert plain["commitment"]["blocks"] == [] and plain["commitment"]["next_actions"] == []
    expected = {"rec_version": version(run, cid), "plan_key": pick(run, cid).key,
                "fact_ids": sorted(c["fact_id"] for c in advisory(run, cid))}
    assert expected["fact_ids"] and plain["commitment"]["ack"] == expected
    assert [a["fact_id"] for a in plain["commitment"]["advisory"]] == expected["fact_ids"]
    assert {c["fact_id"]: c["class"] for c in plain["fact_checks"]} == {
        c["fact_id"]: "ADVISORY" if c["changes_plan"] else None for c in active(run, cid)["fact_checks"]}
    assert not card_of(run, cid)["blocked"]
    check = required_fixture(run)
    plan = pick(run, cid)
    hold = call(run, lotholds.place, seat(src_branch), case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    d, card = detail_of(run, cid), card_of(run, cid)
    reasons = engine_reasons(active(run, cid), check, plan)
    assert kinds(d["commitment"]["blocks"]) == ["lot_hold", "required_check"]  # holds first, then Required
    (held, required) = d["commitment"]["blocks"]
    assert (held["hold_id"], held["owner_branch"], held["reason"]) == (hold["hold_id"], src_branch, hold["reason"])
    assert (required["fact_id"], required["reason"]) == (check["fact_id"], "; ".join(reasons))
    row = check_in(d, check["fact_id"])
    assert (row["class"], row["owner_branch"], row["reasons"], row["adoptable"], row["blocked_label"]) == (
        "REQUIRED", src_branch, reasons, False, None)
    assert row["requests_left"] == config.MAX_REQUESTS_PER_VERSION
    for other in d["fact_checks"]:
        if other["fact_id"] != check["fact_id"]:
            assert other["class"] in ("ADVISORY", None) and other["reasons"] == []
    assert d["commitment"]["ack"] == {"rec_version": version(run, cid), "plan_key": plan.key,
                                      "fact_ids": sorted(c["fact_id"] for c in advisory(run, cid))}
    by_kind = {b["kind"]: b for b in card["badges"]}
    assert by_kind["lot_hold"] == {"kind": "lot_hold", "text": "Lot hold", "hold_ids": [hold["hold_id"]],
                                   "owner_branches": [src_branch]}  # 07-22: who resolves it
    assert by_kind["required_check"] == {"kind": "required_check", "text": "Required check",
                                         "fact_ids": [check["fact_id"]], "owner_branches": [check["branch"]]}
    assert card["blocked"] and {"required_check", "lot_hold"} <= set(card["blocked_reasons"])
    keep = case_with_pick(run, MARKDOWN)  # VER-06 empty: no fact check that changes the plan, no hold
    k = detail_of(run, keep["case_id"])
    assert k["commitment"]["blocks"] == [] and k["commitment"]["ack"] is None
    assert not card_of(run, keep["case_id"])["blocked"]


def test_blocked_case_has_a_next_action(run):
    """Acceptance check 8, first exits (D-12): Unanswered or Can't check never settle a Required check. With no request
    yet the case offers request_check; after Can't check the row reads "Blocked · stock/expiry verification required"
    with its branch and the case offers ask_again and close_proposal; with the version's requests used it still offers
    close_proposal. Keep stays feasible and depends on no unverified fact, so select_plan is offered throughout
    (07-10, D-23). No stock or expiry check is adoptable, adopting stock_count stays 400 bad_override, and nothing
    here writes a reported fact."""
    case = story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    check = required_fixture(run)
    d = detail_of(run, cid)
    assert d["commitment"]["next_actions"] == ["request_check", "select_plan", "close_proposal"]  # 07-10: select_plan
    q = call(run, signals.request_evidence, src, cid, version(run, cid), check["fact_id"])
    assert check_in(detail_of(run, cid), check["fact_id"])["blocked_label"] is None  # asked, before its deadline
    with db.tx() as conn:  # the deadline passes: Unanswered never settles it
        db.run(conn, "UPDATE evidence_requests SET due_at = now() - interval '1 second' WHERE run_id = :r "
                     "AND request_id = :q", r=run["run_id"], q=q["request_id"])
    d = detail_of(run, cid)
    assert "required_check" in kinds(d["commitment"]["blocks"]) and "ask_again" in d["commitment"]["next_actions"]
    call(run, signals.cant_check, seat(q["to_branch"]), q["request_id"])
    d = detail_of(run, cid)
    row = check_in(d, check["fact_id"])
    assert (row["blocked_label"], row["owner_branch"], row["requests_left"]) == (
        BLOCKED_LABEL, case["source_branch"], config.MAX_REQUESTS_PER_VERSION - 1)
    assert kinds(d["commitment"]["blocks"]) == ["required_check"]  # Can't check settles nothing
    assert d["commitment"]["next_actions"] == ["ask_again", "select_plan", "close_proposal"]
    again = call(run, signals.request_evidence, src, cid, version(run, cid), check["fact_id"], q["request_id"])
    call(run, signals.cant_check, seat(again["to_branch"]), again["request_id"])
    d = detail_of(run, cid)
    row = check_in(d, check["fact_id"])
    assert (row["blocked_label"], row["requests_left"]) == (BLOCKED_LABEL, 0)
    assert d["commitment"]["next_actions"] == ["select_plan", "close_proposal"]  # the limit is used: both exits stay
    assert all(not c["adoptable"] for c in d["fact_checks"] if c["kind"] in
               ("PHANTOM_STOCK", "STALE_COUNT", "ESTIMATED_EXPIRY"))
    e = raises(call, run, workflow.adopt, src, cid, [{"kind": "stock_count", "lot_id": case["lot_id"], "units": 1.0}],
               "the shelf looked full")
    assert (e.status, e.code) == (400, "bad_override")
    with db.tx() as conn:
        assert db.rows(conn, "SELECT 1 FROM reported_facts WHERE run_id = :r", r=run["run_id"]) == []


def test_lot_holds(run):
    """Acceptance check 10, enforcement (D-13): a CONDITION hold on the story lot refuses its final approval and its
    Prepare with a lot_hold block naming the owner; a case whose lot was moved from the held lot is refused too; a
    PREPARED case involving the lot shows the hold as "Review before dispatch" and nothing claims the work is blocked by
    the commitment rule; after the lot's manager releases it the approval commits."""
    case = story(run)
    cid, src_branch = case["case_id"], case["source_branch"]
    src = seat(src_branch)
    plan, v = pick(run, cid), version(run, cid)
    owned_send(run, src, cid, v)
    call(run, workflow.decide, seat(plan.dest), cid, v, "accept", None, ack(run, cid), shown(run, cid))
    hold = call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    e = raises(call, run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid))
    assert (e.code, e.extra["blocks"]) == ("commitment_blocked", [
        {"kind": "lot_hold", "hold_id": hold["hold_id"], "lot_ids": [case["lot_id"]], "owner_branch": src_branch,
         "reason": hold["reason"], "resolution": "Release the hold or resolve its check"}])
    set_state(run, cid, "APPROVED")  # an approval made before the hold: Prepare refuses it
    e = raises(call, run, workflow.prepare, src, cid)
    assert (e.code, kinds(e.extra["blocks"])) == ("commitment_blocked", ["lot_hold"])
    set_state(run, cid, "PENDING_APPROVAL")
    base = world_lot(run, case["lot_id"])  # a lot moved from the held lot, with its own case
    moved = insert_case(run, {**base, "lot_id": f"{base['lot_id']}-M1", "received_seq": base["received_seq"] + 1},
                        "TEST-M1")
    with db.tx() as conn:
        db.run(conn, "UPDATE lots SET moved_from = :f WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               f=case["lot_id"], r=run["run_id"], l=moved["lot_id"])
    set_state(run, moved["case_id"], "APPROVED")  # its own approval, given before the hold (the test's setup)
    e = raises(call, run, workflow.prepare, src, moved["case_id"])
    assert (e.code, [b.get("hold_id") for b in e.extra["blocks"]]) == ("commitment_blocked", [hold["hold_id"]])
    assert [b.get("hold_id") for b in detail_of(run, moved["case_id"])["commitment"]["blocks"]] == [hold["hold_id"]]
    call(run, lotholds.release, src, hold["hold_id"], "Crates inspected, the stock is sound")
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "APPROVED"}
    assert call(run, workflow.prepare, src, cid) == {"state": "PREPARED"}
    again = call(run, lotholds.place, src, case["lot_id"], "CONDITION", "Leaking cartons")
    d = detail_of(run, cid)
    (listed,) = d["holds"]
    assert (listed["hold_id"], listed["info"]) == (again["hold_id"], "Review before dispatch")
    assert d["commitment"]["blocks"] == [] and d["commitment"]["next_actions"] == ["cancel_approved"]  # 07-11
    card = card_of(run, cid)
    assert "lot_hold" in [b["kind"] for b in card["badges"]]
    assert not {"required_check", "lot_hold"} & set(card["blocked_reasons"])  # the rule claims no block on PREPARED
