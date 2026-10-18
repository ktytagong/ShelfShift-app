"""Closing a plan never hides at-risk stock (07-CONTEXT D-24, D-25, D-28; 04-API-CONTRACT "Close and Reassess",
"Decisions recorded here" D-28; acceptance check 21).

Lots are found by tag (the story case by its tag), never by a pasted id; nothing sleeps. The D-28 rule, as the contract
writes it: "The fingerprint (a material change in stock, demand or transport) is sha256 of the date-invariant decision
inputs: the lot's expiry day and expiry source, a confirmed count's marker (verified_by COUNT and its day), the moved
lots' ids, the confirmed facts in effect for the lot's SKU at the branches the closed version covered (ids, replaced
ones excluded; demand events and transport reports included), the sandbox's policy rows, other cases' live bookings on
those branches and the lot, and the dataset version. It never includes today, days left, the day's forecast points,
today's runs or the daily sales, because each of those moves with every date increment."

Every close (reject, the reject or decline decision, cancel) stores the fingerprint and the lot's status then; the card
reads `lot_status` "Needs decision", or "Awaiting verification" while an active hold on the lot or a Required check of
the closed plan remains, and null once the lot is gone or no longer at risk.
"""
from __future__ import annotations

from api import cases, db, lotholds, scope, settings, workflow
from tests.api.conftest import fresh, own_series, seat
from tests.api.test_advance import advance_once
from tests.api.test_commitment import clone
from tests.api.test_selection import CANCEL_WHY, approved_story, card_of, case_now
from tests.api.test_workflow import add_booking, call
from tests.api.verify_helpers import required_fixture, story

NEEDS, AWAITING = "Needs decision", "Awaiting verification"


def closing():
    """api.closing (07-11)."""
    from api import closing as module
    return module


def lot_status(run, case_id):
    return card_of(run, case_id)["lot_status"]


def recomputed(run, case_id) -> str:
    """closing.fingerprint of the case's lot now, over the branches of its closed version."""
    with db.tx() as conn:
        r = fresh(run["run_id"])
        case = cases.case_row(conn, r["run_id"], case_id)
        return closing().fingerprint(conn, r, case["lot_id"], closing().branches(conn, case))


def reject_story(run) -> dict:
    case = story(run)
    assert call(run, workflow.reject, seat(case["source_branch"]), case["case_id"], "Not this week") == \
        {"state": "REJECTED"}
    return case_now(run, case["case_id"])


def gone(run, case: dict) -> None:
    """The case lot's stock gone, on the record and physically (the test writes its own copy of the series)."""
    own_series(run, case["source_branch"], case["sku"])
    with db.tx() as conn:
        db.run(conn, "UPDATE lots SET qty = 0 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               r=run["run_id"], l=case["lot_id"])
        db.run(conn, "UPDATE truth_lots SET qty = 0 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               r=run["run_id"], l=case["lot_id"])


def test_closing_never_hides_the_lot(run):
    """Acceptance check 21 (D-24): a rejected at-risk lot reads "Needs decision" (its fingerprint and status stored);
    with a CONDITION hold on the lot it reads "Awaiting verification" until the hold is released; a case rejected while
    a Required check blocked it reads "Awaiting verification"; a cancelled approved plan follows the same rule; once the
    lot's stock is gone it reads nothing."""
    row = reject_story(run)
    cid, src = row["case_id"], seat(row["source_branch"])
    assert (row["lot_status"], len(row["fingerprint"] or "")) == ("AT_RISK", 64)
    assert lot_status(run, cid) == NEEDS
    assert card_of(run, cid)["predecessor"] is None and card_of(run, cid)["successor"] is None
    hold = call(run, lotholds.place, src, row["lot_id"], "CONDITION", "Crushed crates in the chiller")
    assert lot_status(run, cid) == AWAITING
    call(run, lotholds.release, src, hold["hold_id"], "Inspected: crates are fine")
    assert lot_status(run, cid) == NEEDS
    gone(run, row)
    assert lot_status(run, cid) is None
    blocked = clone()  # rejected while a Required check blocked the plan
    required_fixture(blocked)
    assert reject_story(blocked)["lot_status"] == "AT_RISK"
    assert lot_status(blocked, story(blocked)["case_id"]) == AWAITING
    cancelled = clone()  # a cancelled approved plan
    case, _ = approved_story(cancelled)
    call(cancelled, closing().cancel, seat(case["source_branch"]), case["case_id"], CANCEL_WHY)
    row = case_now(cancelled, case["case_id"])
    assert (row["lot_status"], len(row["fingerprint"] or "")) == ("AT_RISK", 64)
    assert lot_status(cancelled, case["case_id"]) == NEEDS


def test_the_decline_decision_records_the_lot(run):
    """D-24: the reject or decline decision on a pending case stores the fingerprint and status too."""
    case = story(run)
    cid = case["case_id"]
    from tests.api.conftest import owned_send
    from tests.api.test_workflow import pick, version
    v = version(run, cid)
    owned_send(run, seat(case["source_branch"]), cid, v)
    plan = pick(run, cid)
    assert call(run, workflow.decide, seat(plan.dest), cid, v, "decline", "No room", None) == {"state": "REJECTED"}
    row = case_now(run, cid)
    assert row["lot_status"] == "AT_RISK" and row["fingerprint"] == recomputed(run, cid)
    assert lot_status(run, cid) == NEEDS


def test_the_fingerprint_is_date_invariant(run):
    """The D-28 rule: the fingerprint stored at reject equals one computed again after one Next day (advance_once:
    the day's sales, forecasts and runs moved, nothing else); it changes after a confirmed count on the lot, a settings
    write, another case's booking on the lot's branches and its release, and a confirmed fact in effect for the lot's
    SKU at those branches (a replaced one is out)."""
    row = reject_story(run)
    cid = row["case_id"]
    assert recomputed(run, cid) == row["fingerprint"]
    day = fresh(run["run_id"])["day"]
    assert advance_once(run)["advanced"] is True and fresh(run["run_id"])["day"] == day + 1
    assert case_now(run, cid)["state"] == "REJECTED"
    assert recomputed(run, cid) == row["fingerprint"]  # a date increment alone changes nothing
    seen = [row["fingerprint"]]

    def changed():
        now = recomputed(run, cid)
        assert now not in seen, "the fingerprint did not change"
        seen.append(now)

    own_series(run, row["source_branch"], row["sku"])
    r = fresh(run["run_id"])
    with db.tx() as conn:  # a confirmed count, as api/facts.py writes it
        db.run(conn, "UPDATE lots SET last_verified_day = :d, verified_by = 'COUNT' WHERE run_id = :r "
                     "AND world = 'shelfshift' AND lot_id = :l", d=r["day"], r=r["run_id"], l=row["lot_id"])
    changed()
    with db.tx() as conn:
        settings.write_settings(conn, fresh(run["run_id"]), seat(None), {"waste_price": 0.5},
                                fresh(run["run_id"])["version"])
    changed()
    with db.tx() as conn:
        branches = closing().branches(conn, case_now(run, cid))
    dest = next(b for b in branches if b != row["source_branch"])
    add_booking(run["run_id"], "TEST-OTHER", "DEMAND", f"{dest}|{row['sku']}", r["day"] + 1, 3.0)
    changed()
    with db.tx() as conn:
        db.run(conn, "UPDATE reservations SET state = 'RELEASED' WHERE run_id = :r AND case_id = 'TEST-OTHER'",
               r=run["run_id"])
    released = recomputed(run, cid)
    assert released != seen[-1]
    seen.append(released)
    with db.tx() as conn:
        db.run(conn, "INSERT INTO reported_facts (run_id, fact_id, signal_id, kind, adjustments, sku, valid_from, "
                     "valid_to, label, confirmed_by, confirmed_day, branch) VALUES (:r, 'TEST-FACT', 'TEST-SIG', "
                     "'DEMAND_EVENT', CAST('[]' AS jsonb), :s, :d, :d, 'test', 'test', :d, :b)",
               r=run["run_id"], s=row["sku"], d=r["day"], b=dest)
    changed()
    with db.tx() as conn:
        db.run(conn, "UPDATE reported_facts SET replaced_by = 'TEST-LATER' WHERE run_id = :r AND fact_id = 'TEST-FACT'",
               r=run["run_id"])
    assert recomputed(run, cid) == released  # a replaced fact is never in effect


def test_closed_scope_holds_rejected_and_cancelled_cases(run):
    """scope.affected's closed list: a REJECTED case and a CLOSED-as-CANCELLED case in scope are in it; a CLOSED case
    whose lot is gone (closed_as null) never is, in any list."""
    row = reject_story(run)
    lot_scope = scope.FactScope(lot_ids=frozenset({row["lot_id"]}))

    def closed_ids(r, s):
        with db.tx() as conn:
            live, committed, closed = scope.affected(conn, fresh(r["run_id"]), s)
        return [c["case_id"] for c in live], [c["case_id"] for c in committed], [c["case_id"] for c in closed]

    assert row["case_id"] in closed_ids(run, lot_scope)[2]
    other = clone()
    case, _ = approved_story(other)
    call(other, closing().cancel, seat(case["source_branch"]), case["case_id"], CANCEL_WHY)
    assert case["case_id"] in closed_ids(other, lot_scope)[2]
    with db.tx() as conn:  # the same case as a lot-gone close: closed_as null
        db.run(conn, "UPDATE cases SET closed_as = NULL WHERE run_id = :r AND case_id = :c", r=other["run_id"],
               c=case["case_id"])
    assert all(case["case_id"] not in ids for ids in closed_ids(other, lot_scope))


# --- 07-12 Task 1: Reassess and the linked successor (07-CONTEXT D-24; research F-19; acceptance check 22) ----------

def successor_of(run, case_id) -> dict | None:
    """The case whose predecessor is `case_id` (found by the link, never by a key)."""
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND predecessor = :c", r=run["run_id"], c=case_id)


def lot_cases(run, lot_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND lot_id = :l ORDER BY opened_day, case_id",
                       r=run["run_id"], l=lot_id)


def test_reassess_creates_one_linked_successor(run):
    """D-24, F-19: the rejected story case's source manager reassesses it: exactly one successor, OPEN, evaluated, with
    a counter key (never SUS-19) and `predecessor` set; both cards show the link and the rejected case no longer reads
    a lot status. Another seat is 403; a second Reassess opens no second case (it answers the active one, as the
    contract row says); an open case is 409 wrong_state. Before the successor, `reassess` is the closed case's next
    action; after it, nothing is."""
    from tests.api.test_selection import detail_of, logged, other_branch
    from tests.api.test_workflow import raises
    row = reject_story(run)
    cid, src = row["case_id"], seat(row["source_branch"])
    assert detail_of(run, cid)["commitment"]["next_actions"] == ["reassess"]
    for who in (seat(None), seat(other_branch(run, row["source_branch"]))):
        e = raises(call, run, closing().reassess, who, cid)
        assert (e.status, e.code) == (403, "forbidden")
    assert successor_of(run, cid) is None  # a refusal opens nothing
    out = call(run, closing().reassess, src, cid)
    succ = successor_of(run, cid)
    assert succ is not None and out == {"case_id": succ["case_id"], "key": succ["key"]}
    assert succ["key"] != cases.STORY_KEY and succ["key"].startswith("SUS-")
    assert (succ["state"], succ["predecessor"], succ["lot_id"], succ["tag"]) == ("OPEN", cid, row["lot_id"], row["tag"])
    assert succ["active_rec_version"] is not None  # evaluated: a recommendation
    assert card_of(run, cid)["successor"] == succ["key"] and card_of(run, cid)["predecessor"] is None
    assert card_of(run, succ["case_id"])["predecessor"] == cid and card_of(run, succ["case_id"])["successor"] is None
    assert detail_of(run, cid)["successor"] == succ["key"]
    assert lot_status(run, cid) is None  # the successor carries the lot now
    assert detail_of(run, cid)["commitment"]["next_actions"] == []
    assert call(run, closing().reassess, src, cid) == out  # no second case
    assert [c["case_id"] for c in lot_cases(run, row["lot_id"])] == [cid, succ["case_id"]]
    e = raises(call, run, closing().reassess, src, succ["case_id"])
    assert (e.status, e.code) == (409, "wrong_state")
    (entry,) = logged(run, cid, "reassessed")
    assert entry["payload"]["successor"] == succ["case_id"] and entry["payload"]["trigger"] == "reassess"
    (opened,) = logged(run, succ["case_id"], "opened")
    assert opened["payload"]["predecessor"] == cid and opened["payload"]["trigger"] == "reassess"


def test_a_hold_carries_over(run):
    """D-24 (closing bypasses nothing): with a Required phantom check and a CONDITION hold on the lot, the rejected
    story case's successor lists the hold and the Required check among its blocks, its commit is refused with 409
    commitment_blocked naming the lot hold, and its fact checks still flag the unconfirmed phantom fact."""
    from tests.api.conftest import owned_send
    from tests.api.test_selection import commit_with_acks, detail_of
    from tests.api.test_workflow import raises, version
    check = required_fixture(run)
    row = reject_story(run)
    src = seat(row["source_branch"])
    hold = call(run, lotholds.place, src, row["lot_id"], "CONDITION", "Crushed crates in the chiller")
    out = call(run, closing().reassess, src, row["case_id"])
    succ = successor_of(run, row["case_id"])
    assert out["case_id"] == succ["case_id"]
    blocks = detail_of(run, succ["case_id"])["commitment"]["blocks"]
    assert {"lot_hold", "required_check"} <= {b["kind"] for b in blocks}
    assert any(b["kind"] == "lot_hold" and b["hold_id"] == hold["hold_id"] for b in blocks)
    with db.tx() as conn:
        rec = cases.active_rec(conn, cases.case_row(conn, run["run_id"], succ["case_id"]))
    assert any(c["kind"] == "PHANTOM_STOCK" and c["branch"] == check["branch"] for c in rec["fact_checks"])
    plan = cases.parse_plan(rec["result"]["recommended"])
    owned_send(run, src, succ["case_id"], version(run, succ["case_id"]))
    e = raises(commit_with_acks, run, succ["case_id"], plan)
    assert (e.status, e.code) == (409, "commitment_blocked")
    assert any(b["kind"] == "lot_hold" for b in e.extra["blocks"])


def test_reassess_refuses_a_gone_lot(run, client):
    """A lot with no stock on the record or physically: Reassess is 409 not_at_risk "This lot no longer needs a
    decision", nothing opens and the card reads no lot status; over HTTP the stored lot_status becomes null too."""
    from tests.api.conftest import as_seat, key
    from tests.api.test_workflow import raises
    row = reject_story(run)
    gone(run, row)
    e = raises(call, run, closing().reassess, seat(row["source_branch"]), row["case_id"])
    assert (e.status, e.code, e.message) == (409, "not_at_risk", "This lot no longer needs a decision")
    assert successor_of(run, row["case_id"]) is None and lot_status(run, row["case_id"]) is None
    mine = fresh(client.get("/api/session").json()["run_id"])  # the HTTP route stores the null
    case = story(mine)
    as_seat(client, case["source_branch"])
    r = client.post(f"/api/cases/{case['case_id']}/reject", json={"reason": "Not this week"}, headers=key())
    assert r.status_code == 200, r.json()
    assert case_now(mine, case["case_id"])["lot_status"] == "AT_RISK"
    gone(mine, case)
    r = client.post(f"/api/cases/{case['case_id']}/reassess", json={}, headers=key())
    assert (r.status_code, r.json()["error"]) == (409, "not_at_risk"), r.json()
    assert case_now(mine, case["case_id"])["lot_status"] is None
    assert successor_of(mine, case["case_id"]) is None


# --- 07-12 Task 2: automatic reassessment on changed decision inputs only; the D-28 record and crossing rule ----------

def write_settings(run, changes: dict) -> dict:
    """HQ's settings write on the sandbox, as the route makes it."""
    with db.tx() as conn:
        r = fresh(run["run_id"])
        return settings.write_settings(conn, r, seat(None), changes, r["version"])


def current_settings(run) -> dict:
    with db.tx() as conn:
        return settings.read_settings(conn, fresh(run["run_id"]))["values"]


def confirm_count(run, lot_id: str) -> dict:
    """A confirmed count on the lot (the record's own quantity), written and applied as signals.confirm does it:
    facts.write, facts.correct_record, then facts.apply over the count's scope, under the run lock FOR UPDATE."""
    from api import facts, overlay
    from api import scope as scopes

    def work(conn):
        r = db.lock_run(conn, run["run_id"], mode="UPDATE")
        row = overlay.lot(conn, r, "shelfshift", lot_id)
        adj = [{"kind": "stock_count", "lot_id": lot_id, "units": float(row["qty"]), "counted_day": r["day"]}]
        fact_id = facts.write(conn, r, kind="STOCK_COUNT", adjustments=adj, sku=row["sku"], branch=row["branch"],
                              zone=None, valid_from=r["day"], valid_to=r["day"], label="test count",
                              confirmed_by=f"manager:{row['branch']}", observed_at=None, source="request",
                              signal_id="TEST-SIG")
        facts.correct_record(conn, r, lot_id, float(row["qty"]), r["day"])
        found = scopes.of_adjustments(adj, sku=row["sku"], branch=row["branch"], zone=None, valid_from=r["day"],
                                      valid_to=r["day"])
        return facts.apply(conn, r, fact_id, found, note=f"count confirmed ({fact_id})")
    return db.in_tx(work)


def reassess_lots(run, lot_id: str, trigger: str = "test") -> list[str]:
    return call(run, closing().reassess_lots, [lot_id], trigger)


def opened_entry(run, case_id) -> dict:
    from tests.api.test_selection import logged
    (entry,) = logged(run, case_id, "opened")
    return entry["payload"]


def active_cases(run, lot_id) -> list[dict]:
    return [c for c in lot_cases(run, lot_id) if c["state"] not in ("CLOSED", "REJECTED")]


def test_no_duplicate_loop(run):
    """Acceptance check 22 (D-24): a rejected at-risk lot; reassessing it with nothing changed opens nothing; a
    settings write that leaves every value as it was opens nothing; a confirmed count on the lot opens exactly one
    successor, linked, with the lot hold and the closed version's unconfirmed checks intact; the trigger again opens
    nothing more; once the successor is rejected and the lot's stock is gone, nothing opens and lot_status is null."""
    from tests.api.test_selection import detail_of
    row = reject_story(run)
    cid, lot_id, src = row["case_id"], row["lot_id"], seat(row["source_branch"])
    assert reassess_lots(run, lot_id) == [] and successor_of(run, cid) is None
    same = current_settings(run)
    write_settings(run, {"waste_price": same["waste_price"], "min_sellable_days": same["min_sellable_days"]})
    assert successor_of(run, cid) is None and recomputed(run, cid) == row["fingerprint"]
    hold = call(run, lotholds.place, src, lot_id, "CONDITION", "Crushed crates in the chiller")
    with db.tx() as conn:
        closed_ids = {c["fact_id"] for c in cases.active_rec(conn, case_now(run, cid))["fact_checks"]}
    done = confirm_count(run, lot_id)
    succ = successor_of(run, cid)
    assert succ is not None and done["reassessed"] == [succ["key"]] and succ["predecessor"] == cid
    assert opened_entry(run, succ["case_id"])["reasons"] == ["inputs"]
    assert opened_entry(run, succ["case_id"])["trigger"] == "fact"
    blocks = detail_of(run, succ["case_id"])["commitment"]["blocks"]
    assert any(b["kind"] == "lot_hold" and b["hold_id"] == hold["hold_id"] for b in blocks)
    with db.tx() as conn:
        now_ids = {c["fact_id"] for c in cases.active_rec(conn, case_now(run, succ["case_id"]))["fact_checks"]}
    assert closed_ids <= now_ids  # nothing about those facts was confirmed: they stay unverified
    assert reassess_lots(run, lot_id) == [] and confirm_count(run, lot_id)["reassessed"] == []
    assert len(lot_cases(run, lot_id)) == 2 and len(active_cases(run, lot_id)) == 1
    assert call(run, workflow.reject, src, succ["case_id"], "Still not this week") == {"state": "REJECTED"}
    assert reassess_lots(run, lot_id) == []  # its own close stored the inputs it was decided on
    gone(run, row)
    assert reassess_lots(run, lot_id) == [] and len(lot_cases(run, lot_id)) == 2
    assert case_now(run, succ["case_id"])["lot_status"] is None and lot_status(run, succ["case_id"]) is None


def test_a_settings_change_reassesses_closed_lots(run):
    """D-24: a settings write that changes a value (waste_price) changes the closed lot's inputs: one successor opens
    while it still qualifies, logged with the trigger "settings" and the reason "inputs"."""
    row = reject_story(run)
    before = current_settings(run)["waste_price"]
    write_settings(run, {"waste_price": before + 0.5})
    succ = successor_of(run, row["case_id"])
    assert succ is not None and succ["state"] == "OPEN" and succ["predecessor"] == row["case_id"]
    assert (opened_entry(run, succ["case_id"])["trigger"], opened_entry(run, succ["case_id"])["reasons"]) == \
        ("settings", ["inputs"])
    write_settings(run, {"waste_price": before + 1.0})
    assert len(lot_cases(run, row["lot_id"])) == 2  # the successor is active: no second case


def test_a_release_reassesses_neighbours(run):
    """D-24 (a released booking): case B (the bread-keep case) holds storage at a destination the story lot's closed
    version covered when the story case is rejected; rejecting B releases it, which changes the story lot's inputs:
    the story lot gets one successor (trigger "release")."""
    from tests.api.conftest import by_tag
    b = by_tag(run, "bread-keep")
    assert b["state"] == "OPEN"
    with db.tx() as conn:
        mine = closing().branches(conn, story(run))
        dest = next(x for x in closing().branches(conn, b) if x in mine and x != story(run)["source_branch"])
    add_booking(run["run_id"], b["case_id"], "STORAGE", f"{dest}|chilled", fresh(run["run_id"])["day"] + 1, 2.0)
    row = reject_story(run)
    assert successor_of(run, row["case_id"]) is None
    assert call(run, workflow.reject, seat(b["source_branch"]), b["case_id"], "Not needed") == {"state": "REJECTED"}
    succ = successor_of(run, row["case_id"])
    assert succ is not None and opened_entry(run, succ["case_id"])["trigger"] == "release"
    assert opened_entry(run, succ["case_id"])["reasons"] == ["inputs"]


def test_one_active_case_per_lot_across_close_and_reassess_loops(run):
    """The assumption-delta invariant: reject → Reassess → approve and cancel → a changed input → reject → Reassess:
    never two active cases for the lot, every case but the first names its predecessor, and no key repeats."""
    from tests.api.test_selection import commit_with_acks
    from tests.api.conftest import owned_send
    from tests.api.test_workflow import version

    def invariant():
        chain = lot_cases(run, lot_id)
        assert len(active_cases(run, lot_id)) <= 1
        assert [c["predecessor"] for c in chain] == [None] + [c["case_id"] for c in chain[:-1]]
        assert len({c["key"] for c in chain}) == len(chain)
        return chain

    row = reject_story(run)
    lot_id, src = row["lot_id"], seat(row["source_branch"])
    invariant()
    s1 = call(run, closing().reassess, src, row["case_id"])["case_id"]
    invariant()
    owned_send(run, src, s1, version(run, s1))
    with db.tx() as conn:
        plan = cases.parse_plan(cases.active_rec(conn, case_now(run, s1))["result"]["recommended"])
    assert commit_with_acks(run, s1, plan)["state"] == "APPROVED"
    call(run, closing().cancel, src, s1, "The destination's chiller failed")
    invariant()
    write_settings(run, {"waste_price": current_settings(run)["waste_price"] + 0.5})  # a changed input
    s2 = successor_of(run, s1)
    assert s2 is not None
    invariant()
    assert call(run, workflow.reject, src, s2["case_id"], "Not this week") == {"state": "REJECTED"}
    s3 = call(run, closing().reassess, src, s2["case_id"])["case_id"]
    chain = invariant()
    assert [c["case_id"] for c in chain] == [row["case_id"], s1, s2["case_id"], s3]
    assert cases.STORY_KEY not in (s1, s2["case_id"], s3)


def st(day=4, days_left=2, band=1, step_pct=20, clearance=False, transfer_open=True, actions=("md30", "tr:X")):
    """A built D-28 state (test values)."""
    return {"day": day, "days_left": days_left, "band": band, "step_pct": step_pct, "clearance": clearance,
            "transfer_open": transfer_open, "actions": sorted(actions)}


def test_crossings():
    """D-28, the crossing rule on its own: only clearance reached, an action not considered and a lost transfer window
    are reasons; a band, a routine step, a dropped action, one less day and the date never are; the merged record
    keeps the most urgent band, the deepest step, clearance once reached, a lost window lost and every action seen."""
    crossed = closing().crossed
    base = st()
    assert crossed(None, base) == ([], base)  # a case closed before 07-12 gets its record, never a crossing
    assert crossed(base, st(day=5, days_left=1))[0] == []  # one less day
    assert crossed(base, st(band=2))[0] == []  # a more urgent band alone
    assert crossed(st(step_pct=0), st(step_pct=20))[0] == []  # a routine markdown step below clearance
    assert crossed(base, st(actions=("tr:X",)))[0] == []  # an action dropped (a markdown the schedule now covers)
    assert crossed(base, st(clearance=True))[0] == ["clearance"]
    assert crossed(base, st(actions=("md20", "md30", "tr:X")))[0] == ["new_option"]
    assert crossed(base, st(transfer_open=False))[0] == ["transfer_window"]
    reasons, merged = crossed(base, st(band=3, transfer_open=False))
    assert reasons == ["transfer_window"]  # the band is never a reason
    assert crossed(merged, st(band=3, transfer_open=False))[0] == []  # the merged record fed back: no reason
    _, seen = crossed(base, st(actions=("md20", "md30", "tr:X")))
    _, dropped = crossed(seen, st(actions=("tr:X",)))
    assert crossed(dropped, st(actions=("md20", "tr:X")))[0] == []  # seen, dropped, seen again: no second reason
    _, lost = crossed(base, st(transfer_open=False))
    _, again = crossed(lost, st(transfer_open=True))
    assert crossed(again, st(transfer_open=False))[0] == []  # opened and closed again: no second reason
    _, cleared = crossed(base, st(clearance=True, step_pct=30))
    _, kept = crossed(cleared, st(clearance=False, step_pct=20))
    assert kept["clearance"] is True and kept["step_pct"] == 30  # clearance once reached stays reached
    _, m = crossed(st(band=2, step_pct=30, actions=("md30",)), st(band=1, step_pct=20, actions=("tr:Y",)))
    assert (m["band"], m["step_pct"], m["actions"]) == (2, 30, ["md30", "tr:Y"])


def test_thresholds_come_from_data(run, monkeypatch):
    """D-28, thresholds from data: for the story lot on its first morning, `closing.state` moves with
    config.URGENCY_BANDS (another band, never a reason), the product's schedule in reference data (another step and
    markdown actions), config.CLEARANCE_PCT (another clearance) and a sandbox run_policy row for min_sellable_days
    (another transfer_open), with no code change; `crossed` names the reason at the moved place. The close stores
    `considered` equal to `closing.state` then."""
    from api import config, overlay, refdata
    case = story(run)
    sku = case["sku"]
    ref = refdata.get(run["dataset_version"])

    def state_now():
        with db.tx() as conn:
            r = fresh(run["run_id"])
            return closing().state(conn, r, overlay.lot(conn, r, "shelfshift", case["lot_id"]))

    base = state_now()
    bands, clearance, seeded = config.URGENCY_BANDS, config.CLEARANCE_PCT, ref.schedules[sku]
    first = min(p for _, p in seeded)  # the schedule's first (shallowest) step
    assert base["step_pct"] == 0 and base["transfer_open"] and f"md{first}" in base["actions"]
    monkeypatch.setattr(config, "URGENCY_BANDS", tuple(e for e in bands if e < base["days_left"]))
    banded = state_now()
    assert banded["band"] < base["band"] and closing().crossed(banded, base)[0] == []
    monkeypatch.setattr(config, "URGENCY_BANDS", bands)
    monkeypatch.setitem(ref.schedules, sku, tuple((k + base["days_left"], p) if p == first else (k, p)
                                                  for k, p in seeded))
    stepped = state_now()  # the first step moved to today: the step is today's, its markdown is covered
    assert stepped["step_pct"] == first and f"md{first}" not in stepped["actions"] and not stepped["clearance"]
    assert closing().crossed(stepped, base)[0] == ["new_option"]  # the markdown back on the seeded schedule
    monkeypatch.setattr(config, "CLEARANCE_PCT", first)
    cleared = state_now()
    assert cleared["clearance"] and closing().crossed(stepped, cleared)[0] == ["clearance"]
    monkeypatch.setattr(config, "CLEARANCE_PCT", clearance)
    monkeypatch.setitem(ref.schedules, sku, seeded)
    assert state_now() == base
    with db.tx() as conn:
        db.run(conn, "INSERT INTO run_policy (run_id, field, value) VALUES (:r, 'min_sellable_days', "
                     "CAST(:v AS jsonb))", r=run["run_id"], v=db.js(base["days_left"] + 2))
    shut = state_now()
    assert not shut["transfer_open"] and closing().crossed(base, shut)[0] == ["transfer_window"]
    with db.tx() as conn:
        db.run(conn, "DELETE FROM run_policy WHERE run_id = :r AND field = 'min_sellable_days'", r=run["run_id"])
    row = reject_story(run)
    assert row["considered"] == state_now()


def test_the_reopen_check_fits_the_case_budget(run):
    """T-07-82: the morning reopen check (`reopen_allowed` with `next_day`: the screen, the fingerprint and one
    `candidates` call, no search) on the rejected story lot, median of 3 with the engine worker as the suite runs it,
    stays under api.bench.BUDGET_S (the spec's 3 s case budget, never raised). The time is printed for the SUMMARY."""
    import statistics
    import time
    from api import bench, overlay
    row = reject_story(run)
    times = []
    for _ in range(3):
        with db.tx() as conn:
            r = fresh(run["run_id"])
            prev = cases.case_row(conn, r["run_id"], row["case_id"])
            lot_row = overlay.lot(conn, r, "shelfshift", row["lot_id"])
            start = time.perf_counter()
            closing().reopen_allowed(conn, r, prev, lot_row, next_day=True)
            times.append(time.perf_counter() - start)
    median = statistics.median(times)
    print(f"\nreopen check on the story lot: median {median:.3f} s of {[round(t, 3) for t in times]} "
          f"(budget {bench.BUDGET_S:g} s)")
    assert median < bench.BUDGET_S


def test_a_malformed_reassess_threshold_is_refused(tmp_path):
    """T-07-81: config.load_settings refuses malformed D-28 leaves, naming the dotted path: band edges that are not
    distinct whole numbers of 0 or more in descending order, or a clearance percent that is not a whole number from 1
    to 100."""
    import copy
    import json
    import pytest
    from api import config
    from data.config import ConfigError
    base = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    for bands, pct, path in (([1, 3, 0], 30, "urgency_bands"), ([3, 3, 0], 30, "urgency_bands"),
                             ([3, -1], 30, "urgency_bands"), ([], 30, "urgency_bands"), ([3, 1.5], 30, "urgency_bands"),
                             ([3, 1, 0], 0, "clearance_pct"), ([3, 1, 0], 101, "clearance_pct"),
                             ([3, 1, 0], 2.5, "clearance_pct")):
        tree = copy.deepcopy(base)
        tree["reassess"]["urgency_bands"]["value"], tree["reassess"]["clearance_pct"]["value"] = bands, pct
        target = tmp_path / "settings.json"
        target.write_text(json.dumps(tree), encoding="utf-8")
        with pytest.raises(ConfigError, match=f"settings.reassess.{path}"):
            config.load_settings(target)


# --- 07-12 Task 3: the synthetic morning screen and the public flag screen follow the same rule (D-24, D-28) ---------

def morning_screen(run) -> list[str]:
    """Re-run the morning screen that morning (cases.open_cases, once the workspace owns every series)."""
    from tests.api.conftest import own_world
    own_world(run)
    with db.tx() as conn:
        return cases.open_cases(conn, fresh(run["run_id"]))


def lot_state(run, lot_id) -> dict:
    """closing.state of the lot on the workspace's day."""
    from api import overlay
    with db.tx() as conn:
        r = fresh(run["run_id"])
        return closing().state(conn, r, overlay.lot(conn, r, "shelfshift", lot_id))


def qualifies(run, lot_id) -> bool:
    from api import config, screen
    with db.tx() as conn:
        return screen.screen(inputs_of(conn, run, lot_id), config.CASE_OPENING)["qualifies"]


def inputs_of(conn, run, lot_id):
    from api import inputs
    return inputs.build_input(conn, fresh(run["run_id"]), lot_id)


def first_step_today(monkeypatch, run, case) -> int:
    """Move the product's first (shallowest) markdown step onto today in reference data (a monkeypatched schedule,
    never an edit of reference/): today's step is that percent and its markdown is covered. Returns the percent."""
    from api import refdata
    ref = refdata.get(run["dataset_version"])
    seeded = ref.schedules[case["sku"]]
    first = min(p for _, p in seeded)
    days_left = lot_state(run, case["lot_id"])["days_left"]
    monkeypatch.setitem(ref.schedules, case["sku"], tuple((k + days_left, p) if p == first else (k, p)
                                                          for k, p in seeded))
    return first


def test_a_routine_day_or_a_band_alone_never_reopens(run, monkeypatch):
    """D-28: the story case rejected on its first morning; the next morning (one less day, a deeper routine step
    below the clearance threshold, the same fingerprint, the lot still qualifying, no crossing) opens no successor;
    re-running that morning's screen with an urgency band edge on today's days left (the band up, still no crossing)
    opens none; then a confirmed count on the lot opens exactly one, with the reason "inputs"."""
    from api import config
    row = reject_story(run)
    cid, at_close = row["case_id"], row["considered"]
    assert advance_once(run)["advanced"] is True
    now = lot_state(run, row["lot_id"])
    assert now["days_left"] == at_close["days_left"] - 1
    assert at_close["step_pct"] < now["step_pct"] < config.CLEARANCE_PCT and not now["clearance"]
    assert closing().crossed(at_close, now)[0] == [] and recomputed(run, cid) == row["fingerprint"]
    assert qualifies(run, row["lot_id"]) and successor_of(run, cid) is None
    merged = case_now(run, cid)["considered"]
    monkeypatch.setattr(config, "URGENCY_BANDS", tuple(sorted({*config.URGENCY_BANDS, now["days_left"]},
                                                              reverse=True)))
    banded = lot_state(run, row["lot_id"])
    assert banded["band"] > merged["band"] and closing().crossed(merged, banded)[0] == []
    assert morning_screen(run) == [] and successor_of(run, cid) is None
    done = confirm_count(run, row["lot_id"])
    succ = successor_of(run, cid)
    assert succ is not None and done["reassessed"] == [succ["key"]]
    assert opened_entry(run, succ["case_id"])["reasons"] == ["inputs"]


def test_the_story_lot_routine_markdown_then_a_clearance_threshold(run, monkeypatch):
    """D-28, the user's story test: the story case rejected on its first morning; the next morning its schedule
    reaches the deeper routine step (no clearance, no crossing, the lot qualifying): no successor. That morning the
    clearance threshold is placed by data on the step it just reached (config.CLEARANCE_PCT, deeper than the step
    considered at the close): the re-run screen opens exactly one linked successor whose `opened` entry lists
    clearance; re-running opens nothing more. Then, on a fresh workspace with the seeded threshold, the first morning
    whose step reaches it is read and printed for the SUMMARY (nothing is changed to make it reopen there)."""
    from api import config
    from tests.api.test_commitment import clone
    row = reject_story(run)
    cid, at_close = row["case_id"], row["considered"]
    assert advance_once(run)["advanced"] is True
    now = lot_state(run, row["lot_id"])
    assert now["step_pct"] > at_close["step_pct"] and not now["clearance"]
    assert closing().crossed(at_close, now)[0] == [] and qualifies(run, row["lot_id"])
    assert successor_of(run, cid) is None
    monkeypatch.setattr(config, "CLEARANCE_PCT", now["step_pct"])
    assert lot_state(run, row["lot_id"])["clearance"] and not case_now(run, cid)["considered"]["clearance"]
    opened = morning_screen(run)
    succ = successor_of(run, cid)
    assert succ is not None and opened == [succ["key"]]
    entry = opened_entry(run, succ["case_id"])
    assert "clearance" in entry["reasons"] and entry["trigger"] == "next_day"
    assert morning_screen(run) == [] and len(lot_cases(run, row["lot_id"])) == 2
    monkeypatch.undo()
    seeded = clone()  # the seeded threshold: the first morning the story lot's step reaches it
    first = reject_story(seeded)
    reading = None
    for _ in range(first["considered"]["days_left"] + 1):
        before = case_now(seeded, first["case_id"])["considered"]
        assert advance_once(seeded)["advanced"] is True
        with db.tx() as conn:
            r = fresh(seeded["run_id"])
            from api import overlay
            lot = overlay.lot(conn, r, "shelfshift", first["lot_id"])
        if lot is None or float(lot["qty"]) <= 0:
            break
        now = lot_state(seeded, first["lot_id"])
        if now["step_pct"] >= config.CLEARANCE_PCT:
            from api import screen
            with db.tx() as conn:
                found = screen.screen(inputs_of(conn, seeded, first["lot_id"]), config.CASE_OPENING)
            reading = {"day": now["day"], "state": now, "crossed": closing().crossed(before, now)[0],
                       "a": found["a"], "b": found["b"], "time_left": found["time_left"],
                       "qualifies": found["qualifies"], "successor": successor_of(seeded, first["case_id"])}
            break
    assert reading is not None, "the seeded schedule never reached the clearance threshold before the lot went"
    print(f"\nseeded clearance morning for the story lot: {reading}")
    opened_there = reading["successor"] is not None
    assert opened_there == (reading["qualifies"] and bool(reading["crossed"]))  # the rule, as read that morning
    if opened_there:
        assert "clearance" in opened_entry(seeded, reading["successor"]["case_id"])["reasons"]


def test_a_newly_viable_option_reopens_once(run, monkeypatch):
    """D-28 (a markdown change that makes a previously infeasible option viable): the story case is closed on its
    first morning while its schedule (monkeypatched in reference data) already covers its first markdown percent, so
    that markdown is not in the considered actions; with the seeded schedule back, the re-run screen that morning
    (exactly ["new_option"], the fingerprint unchanged, the lot qualifying) opens exactly one successor whose reasons
    list new_option; re-running opens nothing more."""
    from api import refdata
    case = story(run)
    seeded = refdata.get(run["dataset_version"]).schedules[case["sku"]]
    first = first_step_today(monkeypatch, run, case)
    row = reject_story(run)
    assert f"md{first}" not in row["considered"]["actions"]
    monkeypatch.setitem(refdata.get(run["dataset_version"]).schedules, case["sku"], seeded)
    now = lot_state(run, row["lot_id"])
    assert closing().crossed(row["considered"], now)[0] == ["new_option"] and f"md{first}" in now["actions"]
    assert recomputed(run, row["case_id"]) == row["fingerprint"] and qualifies(run, row["lot_id"])
    opened = morning_screen(run)
    succ = successor_of(run, row["case_id"])
    assert succ is not None and opened == [succ["key"]]
    assert opened_entry(run, succ["case_id"])["reasons"] == ["new_option"]
    assert morning_screen(run) == [] and len(lot_cases(run, row["lot_id"])) == 2


def lost_window_setup(run) -> dict:
    """The story lot with the Policy's min_sellable_days written (an HQ settings write, before the close, so the
    stored fingerprint already holds it) at the most sellable days any run leaving its branch today gives: today's
    window is open, and a later day's run gives fewer days. Returns the rejected case row."""
    from api import refdata
    from engine.timing import first_sellable_day
    case = story(run)
    with db.tx() as conn:
        inp = inputs_of(conn, run, case["lot_id"])
    lot = next(l for l in inp.lots if l.lot_id == case["lot_id"])
    ref = refdata.get(run["dataset_version"])
    days = max(lot.expiry_day - first_sellable_day(r.day, r.arrives_min, inp.policy) + 1
               for r in ref.runs_from(case["source_branch"], inp.today))
    write_settings(run, {"min_sellable_days": days})
    return reject_story(run)


def test_a_lost_transfer_window_reopens_once(run):
    """D-28 (losing a transfer window that was feasible before): the story lot closed with its window open (the
    sandbox Policy's min_sellable_days at today's most); the next morning no run of the timetable from its branch
    passes the run-level limits while a markdown is still available and the lot qualifies: crossed() is exactly
    ["transfer_window"] and that morning's screen opened exactly one successor with that reason; re-running opens
    nothing more."""
    row = lost_window_setup(run)
    assert row["considered"]["transfer_open"] is True
    assert advance_once(run)["advanced"] is True
    now = lot_state(run, row["lot_id"])
    assert now["transfer_open"] is False and any(a.startswith("md") for a in now["actions"])
    assert closing().crossed(row["considered"], now)[0] == ["transfer_window"] and qualifies(run, row["lot_id"])
    succ = successor_of(run, row["case_id"])
    assert succ is not None and opened_entry(run, succ["case_id"])["reasons"] == ["transfer_window"]
    assert opened_entry(run, succ["case_id"])["trigger"] == "next_day"
    assert morning_screen(run) == [] and len(lot_cases(run, row["lot_id"])) == 2


def test_a_band_crossing_that_changes_the_action_reopens_once(run, monkeypatch):
    """D-28: the lost-window lot and morning, with an urgency band edge on that morning's days left as well (the
    band up): exactly one successor, its reasons transfer_window and no band reason, its `opened` entry carrying the
    more urgent band; re-running opens nothing more."""
    from api import config
    row = lost_window_setup(run)
    edge = row["considered"]["days_left"] - 1
    monkeypatch.setattr(config, "URGENCY_BANDS", tuple(sorted({*config.URGENCY_BANDS, edge}, reverse=True)))
    assert advance_once(run)["advanced"] is True
    now = lot_state(run, row["lot_id"])
    assert now["days_left"] == edge and now["band"] > row["considered"]["band"]
    assert closing().crossed(row["considered"], now)[0] == ["transfer_window"]
    succ = successor_of(run, row["case_id"])
    entry = opened_entry(run, succ["case_id"])
    assert entry["reasons"] == ["transfer_window"] and entry["band"] == now["band"]
    assert morning_screen(run) == [] and len(lot_cases(run, row["lot_id"])) == 2


def test_the_same_crossing_never_opens_twice(run, monkeypatch):
    """D-28: after the newly-viable-option successor, rejecting it the same day and re-running the screen opens
    nothing; the next morning, with no new reason and unchanged inputs, opens nothing; the first closed case's
    considered holds the merged record (the new action in its actions) and the rejected successor's holds the state
    at its close."""
    from api import refdata
    case = story(run)
    seeded = refdata.get(run["dataset_version"]).schedules[case["sku"]]
    first = first_step_today(monkeypatch, run, case)
    row = reject_story(run)
    monkeypatch.setitem(refdata.get(run["dataset_version"]).schedules, case["sku"], seeded)
    morning_screen(run)
    succ = successor_of(run, row["case_id"])
    assert succ is not None
    assert f"md{first}" in case_now(run, row["case_id"])["considered"]["actions"]  # merged that morning
    src = seat(row["source_branch"])
    assert call(run, workflow.reject, src, succ["case_id"], "Still not this week") == {"state": "REJECTED"}
    closed = case_now(run, succ["case_id"])
    assert closed["considered"] == lot_state(run, row["lot_id"])
    assert morning_screen(run) == [] and successor_of(run, succ["case_id"]) is None
    assert advance_once(run)["advanced"] is True
    now = lot_state(run, row["lot_id"])
    if qualifies(run, row["lot_id"]):
        assert closing().crossed(closed["considered"], now)[0] == []
    assert recomputed(run, succ["case_id"]) == closed["fingerprint"]
    assert successor_of(run, succ["case_id"]) is None and len(lot_cases(run, row["lot_id"])) == 2


def test_the_public_flag_screen_follows_the_same_rule(public_template, monkeypatch):
    """D-24 handoff to 05.2, applied here, on a workspace of the tiny public template (05.2-16's fixture; ACTIVE for
    the rest of this module, so this test stays last in it): a rejected flagged lot is looked at by a re-run of the
    flag screen (never excluded) and, with unchanged inputs, neither flagged again nor reopened; the screen's rule
    never counts the Next-day reasons (every reopen check it makes is without `next_day`); after a confirmed fact in
    scope the lot is reopened once, and re-running the screen opens nothing more."""
    from api import flags, overlay, sandbox
    with db.tx() as conn:
        rid = sandbox.clone(conn, sandbox.active_template(conn))
    run, template = fresh(rid), fresh(public_template)
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r AND day = :d AND missing_data IS NULL "
                             "ORDER BY key", r=template["run_id"], d=template["day"])
    assert rows, "the tiny public template flags at least one lot"
    row = None
    for flag in rows:  # the first flagged lot whose case opens OPEN in this workspace
        with db.tx() as conn:
            flags.open_from_flag(conn, fresh(rid), flag["key"])
            if cases.case_row(conn, rid, flag["key"])["state"] == "OPEN":
                row = flag
                break
    assert row is not None
    assert call(run, workflow.reject, seat(row["branch"]), row["key"], "Not this week") == {"state": "REJECTED"}
    assert case_now(run, row["key"])["lot_status"] == "AT_RISK"
    calls = []
    real = closing().reopen_allowed

    def spy(conn, r, prev, lot_row, next_day=False, seen=None):
        calls.append((prev["case_id"], next_day))
        return real(conn, r, prev, lot_row, next_day=next_day, seen=seen)
    monkeypatch.setattr(closing(), "reopen_allowed", spy)
    assert morning_screen(run) == []
    assert (row["key"], False) in calls and all(nd is False for _, nd in calls)
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM case_flags WHERE run_id = :r AND lot_id = :l",
                      r=rid, l=row["lot_id"])["n"] == 0  # not flagged again in the workspace
        assert overlay.lot(conn, fresh(rid), "shelfshift", row["lot_id"]) is not None
    assert successor_of(run, row["key"]) is None
    done = confirm_count(run, row["lot_id"])
    succ = successor_of(run, row["key"])
    assert succ is not None and done["reassessed"] == [succ["key"]]
    assert morning_screen(run) == []
    assert len(lot_cases(run, row["lot_id"])) == 2 and all(nd is False for _, nd in calls)
