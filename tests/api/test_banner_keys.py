"""The keys the screens' banners, board badges and Stock & lots rows read (07-22; 04-API-CONTRACT row 2026-10-18
"Banner, badge and lot-row keys"; 07-CONTEXT D-13, D-15, D-24; 07-UI-SPEC "Holds, flags and reports", "Board badges",
surface 6 Stock & lots).

- A successor's detail names how the plan it follows was closed (`predecessor_closed` {key, closed_as, at}) so the
  page reads "Follows {key} · Rejected {t}" (or Cancelled) without a second read.
- The `lot_hold` and `required_check` badges name the stores that own them (`owner_branches`), so a blocked card's
  Next line says who resolves it ("Resolve hold · {store} manager").
- A Stock & lots row carries its active hold (`lot_hold`) and the label of the confirmed fact in force for the lot
  (`fact_label`), so the row shows the Lot hold chip and the label without opening the drawer.

Cases are found by tag (the story case by its tag); nothing sleeps.
"""
from __future__ import annotations

import datetime as dt

from api import cases, db, lotholds, stock
from tests.api.conftest import fresh, seat
from tests.api.test_reassess import confirm_count, reject_story
from tests.api.test_selection import CANCEL_WHY, approved_story, card_of, detail_of
from tests.api.test_workflow import call
from tests.api.verify_helpers import required_fixture, story


def closing():
    from api import closing as module
    return module


def lot_row(run, branch: str, lot_id: str) -> dict:
    with db.tx() as conn:
        r = fresh(run["run_id"])
        page = stock.lots(conn, r, {"role": "manager", "branch": branch, "account": branch}, page_size=None)
    return next(x for x in page["items"] if x["lot_id"] == lot_id)


def test_a_successor_names_how_its_predecessor_closed(run):
    """D-24: after a reject, the successor Reassess opens names the rejected case's key, "REJECTED" and the reject's
    recorded time (ISO-8601 UTC); after a cancel, "CANCELLED" and the cancel's time. A case with no predecessor reads
    null."""
    row = reject_story(run)
    cid, src = row["case_id"], seat(row["source_branch"])
    assert detail_of(run, cid)["predecessor_closed"] is None
    succ = call(run, closing().reassess, src, cid)
    closed = detail_of(run, succ["case_id"])["predecessor_closed"]
    assert (closed["key"], closed["closed_as"]) == (row["key"], "REJECTED")
    assert dt.datetime.fromisoformat(closed["at"]).utcoffset() == dt.timedelta(0)


def test_a_cancelled_predecessor_reads_cancelled(run):
    case, _ = approved_story(run)
    cid, src = case["case_id"], seat(case["source_branch"])
    call(run, closing().cancel, src, cid, CANCEL_WHY)
    succ = call(run, closing().reassess, src, cid)
    closed = detail_of(run, succ["case_id"])["predecessor_closed"]
    assert (closed["key"], closed["closed_as"]) == (case["key"], "CANCELLED")
    assert dt.datetime.fromisoformat(closed["at"]).utcoffset() == dt.timedelta(0)


def test_hold_and_required_badges_name_their_owner(run):
    """D-12, D-13: the lot_hold badge names the held lot's store and the required_check badge the stores whose facts
    are Required, each once, so the card's Next line names who resolves it."""
    check = required_fixture(run)
    case = story(run)
    call(run, lotholds.place, seat(case["source_branch"]), case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    badges = {b["kind"]: b for b in card_of(run, case["case_id"])["badges"]}
    assert badges["lot_hold"]["owner_branches"] == [case["source_branch"]]
    assert badges["required_check"]["owner_branches"] == [check["branch"]]


def test_a_lot_row_carries_its_hold_and_fact_label(run):
    """Surface 6: the story lot's row reads no hold and no fact label; with a CONDITION hold it carries {hold_id, kind,
    reason}; with a confirmed count it carries that fact's label. Released, the hold leaves the row."""
    case = story(run)
    lot_id, branch = case["lot_id"], case["source_branch"]
    before = lot_row(run, branch, lot_id)
    assert (before["lot_hold"], before["fact_label"]) == (None, None)
    hold = call(run, lotholds.place, seat(branch), lot_id, "CONDITION", "Crushed crates in the chiller")
    assert lot_row(run, branch, lot_id)["lot_hold"] == {
        "hold_id": hold["hold_id"], "kind": "CONDITION", "reason": "Crushed crates in the chiller"}
    confirm_count(run, lot_id)
    assert lot_row(run, branch, lot_id)["fact_label"] == "test count"
    call(run, lotholds.release, seat(branch), hold["hold_id"], "Inspected: crates are fine")
    assert lot_row(run, branch, lot_id)["lot_hold"] is None


def test_the_keys_never_reach_a_flag_or_another_workspace(run):
    """A case with no predecessor and no hold keeps every new key empty; the cases row is untouched by the reads."""
    case = story(run)
    with db.tx() as conn:
        before = cases.case_row(conn, run["run_id"], case["case_id"])
    d = detail_of(run, case["case_id"])
    assert d["predecessor_closed"] is None
    assert not any(b["kind"] in ("lot_hold", "required_check") and "owner_branches" not in b for b in d["badges"])
    with db.tx() as conn:
        assert cases.case_row(conn, run["run_id"], case["case_id"]) == before
