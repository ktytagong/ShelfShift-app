"""Close by the owner, reconcile a receipt discrepancy, and record a price change (06.1 D-34, D-48, D-65, D-67;
04-API-CONTRACT "Product mode (06.1)", Execution).

Every call goes through the real app. The story case (SUS-19, the milk-story tag) is driven to PREPARED, dispatched
and received with test_execution's helpers (ownership by the source store set in the database before Send, as those
helpers document). Quantities come from the work order (its committed quantity), never pasted from the seed.

A stock decision is EXECUTED only when all of its work orders are done (orchestrator decision on 06.1-09's open
item): the story's plan is a transfer with a marked-down rest, so a receipt leaves it PREPARED while its MARKDOWN work
order is PLANNED, and recording the price change executes it (or the other way round). Tests read whether the plan
has a markdown rest from the prepared work orders, never assume it.
"""
from __future__ import annotations

import pytest

from api import db, refdata
from tests.api.conftest import fresh, sign_in, take_and_send, world_lot
from tests.api.test_execution import (committed, dispatch_body, dispatched, logged, physical_of, post, prepared,
                                      receipt_body, run_of, story_case, work_order)
from tests.api.verify_helpers import shown  # approve names the plan the page shows (07 review C)

CLOSED_COLUMNS = {"closed_as": "text", "closed_at": "timestamp with time zone", "closed_by": "text"}  # 07-11 step 2


def case_row(run_id: str, case_id: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", r=run_id, c=case_id)


def markdown_order(run_id: str, case_id: str) -> dict | None:
    """The case's MARKDOWN work order (a markdown plan, or a transfer's marked-down rest), or None."""
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                            "AND kind = 'MARKDOWN' ORDER BY action_id DESC LIMIT 1", r=run_id, c=case_id)


def record_price(c, case_id: str, action_id: str, **over) -> object:
    return post(c, f"/api/cases/{case_id}/price-applied", {"action_id": action_id, "confirmed": True, **over})


def received(c, short: int = 0, missing: int = 0) -> tuple[dict, dict, str, str, int]:
    """The story dispatched (committed − `short`) and received with `missing` units short (a SHORT discrepancy of
    `missing` when it is above 0), then its markdown rest's price change recorded by the source manager when the plan
    has one, so the case is EXECUTED; `c` is then signed in as the source manager, the owner. (run, work order,
    source, destination, units dispatched)."""
    run, action, src, dest, sent = dispatched(c, short=short)
    body = receipt_body(action, sent - missing, 0, reason="missing at the door" if missing else None)
    r = post(c, f"/api/cases/{action['case_id']}/receipt", body)
    assert r.status_code == 200, r.json()
    sign_in(c, src)
    rest = markdown_order(run["run_id"], action["case_id"])
    if rest is not None:
        assert r.json()["state"] == "PREPARED"  # the rest's price is still to change
        r = record_price(c, action["case_id"], rest["action_id"])
        assert r.status_code == 200, r.json()
    assert r.json()["state"] == "EXECUTED"
    return run, action, src, dest, sent


# --- Task 1 (tracer): the owner closes a received transfer; an open discrepancy blocks it ----------------------------

def test_the_closed_columns_match_phase_7s_names_and_types(client):
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'cases' "
                             "AND column_name = ANY(:n)", n=list(CLOSED_COLUMNS))
    assert {r["column_name"]: r["data_type"] for r in rows} == CLOSED_COLUMNS


def test_the_owner_closes_a_fully_received_transfer(client):
    run, action, src, _, _ = received(client)
    cid = action["case_id"]
    assert story_case(run["run_id"])["state"] == "EXECUTED"
    r = post(client, f"/api/cases/{cid}/close", {"reason": "Received as planned"})
    assert r.status_code == 200, r.json()
    assert r.json() == {"state": "CLOSED", "closed_as": "COMPLETED"}
    row = case_row(run["run_id"], cid)
    assert (row["state"], row["closed_as"], row["closed_by"], row["reason"]) == \
        ("CLOSED", "COMPLETED", src, "Received as planned")
    assert row["closed_at"] is not None
    [entry] = logged(run["run_id"], cid, "closed")
    assert entry["actor"] == {"account": src, "role": "manager", "branch": src}
    assert (entry["closed_as"], entry["reason"], entry["from_state"]) == ("COMPLETED", "Received as planned", "EXECUTED")
    with db.tx() as conn:  # the plan ran: what it still occupied ends DONE, as the observed path ends it
        open_rows = db.one(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = :r AND case_id = :c "
                                 "AND state IN ('HELD', 'COMMITTED', 'CONVERTED')", r=run["run_id"], c=cid)["n"]
    assert open_rows == 0


def test_an_open_receipt_discrepancy_blocks_close(client):
    run, action, _, _, _ = received(client, missing=6)
    cid = action["case_id"]
    r = post(client, f"/api/cases/{cid}/close", {"reason": "Received"})
    assert (r.status_code, r.json()["error"], r.json()["message"]) == \
        (409, "discrepancy_open", "Receipt discrepancy—reconciliation required.")
    row = case_row(run["run_id"], cid)
    assert (row["state"], row["closed_as"], row["closed_at"], row["closed_by"]) == ("EXECUTED", None, None, None)
    assert logged(run["run_id"], cid, "closed") == []


def test_close_needs_a_reason_the_owner_and_a_finished_plan(client):
    run, action, src, dest, sent = dispatched(client, short=0)
    cid, path = action["case_id"], f"/api/cases/{action['case_id']}/close"
    sign_in(client, src)
    r = post(client, path, {"reason": "too early"})
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")  # still PREPARED: nothing received yet
    sign_in(client, dest)
    assert post(client, f"/api/cases/{cid}/receipt", receipt_body(action, sent)).status_code == 200
    r = post(client, path, {"reason": "Received as planned"})
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")  # the destination is not the owner
    sign_in(client, src)
    rest = markdown_order(run["run_id"], cid)
    if rest is not None:  # received, but the rest's price change is still to record: not done yet
        r = post(client, path, {"reason": "Received as planned"})
        assert (r.status_code, r.json()["error"]) == (409, "wrong_state")
        assert record_price(client, cid, rest["action_id"]).json()["state"] == "EXECUTED"
    for body in ({}, {"reason": ""}, {"reason": "   "}):
        r = post(client, path, body)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), body
        assert "reason" in r.json()["message"]
    assert case_row(run["run_id"], cid)["state"] == "EXECUTED"
    assert logged(run["run_id"], cid, "closed") == []


# --- Task 2: reconcile a discrepancy with a resolution, its units and a reason ---------------------------------------

def reconcile_body(gap_id: str, resolution: str, units: int, reason: str | None = "counted again") -> dict:
    body = {"discrepancy_id": gap_id, "resolution": resolution, "units": units}
    return body if reason is None else {**body, "reason": reason}


def discrepancy(run_id: str, gap_id: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM discrepancies WHERE run_id = :r AND discrepancy_id = :d", r=run_id, d=gap_id)


def movements_of(run_id: str, case_id: str) -> list[dict]:
    """The case's stock movements (dispatch, receipt, reconciliations) by recorded time; a price change is no stock
    movement."""
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM movements WHERE run_id = :r AND case_id = :c AND kind <> 'PRICE_APPLIED' "
                             "ORDER BY recorded_at, movement_id", r=run_id, c=case_id)


def test_the_owner_reconciles_a_short_receipt_in_two_parts_then_closes(client):
    run, action, src, _, sent = received(client, missing=6)  # a SHORT discrepancy of 6
    cid, new_lot, gap_id = action["case_id"], action["new_lot_id"], f"{action['action_id']}:SHORT"
    path = f"/api/cases/{cid}/reconcile"
    lot_before, phys_before = world_lot(run, new_lot)["qty"], physical_of(run["run_id"], new_lot)
    assert lot_before == sent - 6
    r = post(client, path, reconcile_body(gap_id, "found_at_destination", 4, "found in the chiller"))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert (body["discrepancy"]["open_units"], body["discrepancy"]["state"]) == (2, "OPEN")
    m = body["movement"]
    assert (m["movement_id"], m["kind"], m["qty"], m["lot_id"], m["condition"], m["reason"], m["recorded_by"]) == \
        (f"{gap_id}:1", "RECONCILE", 4, new_lot, "found_at_destination", "found in the chiller", src)
    # the moved lot gains 4, on the record and physically
    assert world_lot(run, new_lot)["qty"] == pytest.approx(lot_before + 4)
    assert physical_of(run["run_id"], new_lot) == pytest.approx(phys_before + 4)
    r = post(client, f"/api/cases/{cid}/close", {"reason": "Received"})
    assert (r.status_code, r.json()["error"]) == (409, "discrepancy_open")  # 2 units still open
    r = post(client, path, reconcile_body(gap_id, "lost_or_written_off", 2, "not on the truck"))
    assert r.status_code == 200, r.json()
    gap = discrepancy(run["run_id"], gap_id)
    assert (gap["state"], gap["open_units"], gap["units"], gap["resolution"], gap["resolved_by"], gap["reason"]) == \
        ("RECONCILED", 0, 6, "lost_or_written_off", src, "not on the truck")
    assert gap["resolved_at"] is not None
    assert r.json()["movement"]["lot_id"] is None  # written off: no stock changed
    assert world_lot(run, new_lot)["qty"] == pytest.approx(lot_before + 4)
    assert physical_of(run["run_id"], new_lot) == pytest.approx(phys_before + 4)
    # dispatched and received stay as recorded (D-65): only RECONCILE rows were added
    kinds = [(m["kind"], m["qty"]) for m in movements_of(run["run_id"], cid)]
    assert kinds == [("DISPATCH", sent), ("RECEIPT", sent - 6), ("RECONCILE", 4), ("RECONCILE", 2)]
    entries = logged(run["run_id"], cid, "discrepancy_reconciled")
    assert [(e["resolution"], e["units"], e["open_units"], e["state"]) for e in entries] == \
        [("found_at_destination", 4, 2, "OPEN"), ("lost_or_written_off", 2, 0, "RECONCILED")]
    assert all(e["actor"] == {"account": src, "role": "manager", "branch": src} for e in entries)
    r = post(client, f"/api/cases/{cid}/close", {"reason": "Reconciled and received"})
    assert r.status_code == 200 and r.json()["state"] == "CLOSED", r.json()


def test_a_source_count_error_returns_the_units_to_the_source_lot(client):
    run, action, _, _, _ = received(client, short=6, missing=3)
    cid, src_lot, gap_id = action["case_id"], action["lot_id"], f"{action['action_id']}:SHORT"
    lot_before, phys_before = world_lot(run, src_lot)["qty"], physical_of(run["run_id"], src_lot)
    new_before = world_lot(run, action["new_lot_id"])["qty"]
    r = post(client, f"/api/cases/{cid}/reconcile", reconcile_body(gap_id, "source_count_error", 3))
    assert r.status_code == 200, r.json()
    assert r.json()["discrepancy"]["state"] == "RECONCILED"
    assert world_lot(run, src_lot)["qty"] == pytest.approx(lot_before + 3)
    assert physical_of(run["run_id"], src_lot) == pytest.approx(phys_before + 3)
    assert world_lot(run, action["new_lot_id"])["qty"] == pytest.approx(new_before)  # the destination is unchanged


def test_reconcile_rules_units_reason_resolution_and_seats(client):
    run, action, src, dest, _ = received(client, missing=6)
    cid, gap_id = action["case_id"], f"{action['action_id']}:SHORT"
    path = f"/api/cases/{cid}/reconcile"
    for body, field in ((reconcile_body(gap_id, "found_at_destination", 7), "units"),
                        (reconcile_body(gap_id, "found_at_destination", 0), "units"),
                        (reconcile_body(gap_id, "found_at_destination", 2.5), "units"),
                        (reconcile_body(gap_id, "found_at_destination", 2, None), "reason"),
                        (reconcile_body(gap_id, "found_at_destination", 2, "  "), "reason"),
                        (reconcile_body(gap_id, "stolen", 2), "resolution")):
        r = post(client, path, body)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), (field, r.json())
        assert field in r.json()["message"], (field, r.json())
    r = post(client, path, reconcile_body(f"{action['action_id']}:LOST", "found_at_destination", 1))
    assert (r.status_code, r.json()["error"]) == (404, "not_found")
    sign_in(client, dest)  # the destination records receipts only; it is neither the owner nor HQ
    r = post(client, path, reconcile_body(gap_id, "found_at_destination", 1))
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    gap = discrepancy(run["run_id"], gap_id)
    assert (gap["state"], gap["open_units"]) == ("OPEN", 6)  # every refusal wrote nothing
    assert [m["kind"] for m in movements_of(run["run_id"], cid)] == ["DISPATCH", "RECEIPT"]  # stock movements only
    assert logged(run["run_id"], cid, "discrepancy_reconciled") == []
    sign_in(client, "HQ")  # HQ may reconcile (UI-SPEC open question 2, recommended default)
    r = post(client, path, reconcile_body(gap_id, "lost_or_written_off", 6, "written off by HQ"))
    assert r.status_code == 200, r.json()
    gap = discrepancy(run["run_id"], gap_id)
    assert (gap["state"], gap["resolved_by"]) == ("RECONCILED", "HQ")
    [entry] = logged(run["run_id"], cid, "discrepancy_reconciled")
    assert entry["actor"] == {"account": "HQ", "role": "hq", "branch": None}
    r = post(client, path, reconcile_body(gap_id, "lost_or_written_off", 1))
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")  # nothing left to reconcile
    sign_in(client, src)
    assert post(client, f"/api/cases/{cid}/close", {"reason": "Written off"}).status_code == 200


# --- Task 3: record the price change of a markdown; a decision executes when all of its work orders are done -------

def markdown_prepared(c) -> tuple[dict, dict, dict, str]:
    """A case whose pick is a markdown (test_workflow.case_with_pick: the first open one by lot id, else a synthetic
    lot it inserts) driven to PREPARED over HTTP by its source manager: take ownership, send, approve (the source
    alone for a markdown), prepare. (run, case, markdown work order, source); `c` stays signed in as the source."""
    from tests.api.test_workflow import MARKDOWN, case_with_pick
    run = run_of(c)
    case = case_with_pick(run, MARKDOWN)
    cid, src = case["case_id"], case["source_branch"]
    sign_in(c, src)
    v = case_row(run["run_id"], cid)["active_rec_version"]
    assert take_and_send(c, cid, v).json()["state"] == "PENDING_APPROVAL"
    assert post(c, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "approve",  # names the plan (07 C)
                                                  "plan_key": shown(run, cid)}).json() == {"state": "APPROVED"}
    r = post(c, f"/api/cases/{cid}/prepare")
    assert r.json() == {"state": "PREPARED"}, r.json()
    return fresh(run["run_id"]), case, markdown_order(run["run_id"], cid), src


def lot_price_states(run_id: str, case_id: str) -> set:
    with db.tx() as conn:
        return {r["state"] for r in db.rows(conn, "SELECT state FROM reservations WHERE run_id = :r AND case_id = :c "
                                                  "AND kind = 'LOT_PRICE'", r=run_id, c=case_id)}


def test_a_recorded_price_change_executes_a_markdown_then_the_owner_closes_it(client):
    run, case, action, src = markdown_prepared(client)
    cid, lot_id = case["case_id"], action["lot_id"]
    before = world_lot(run, lot_id)
    assert action["state"] == "PLANNED" and before["markdown_pct"] < action["pct"]  # unchanged until a person says so
    r = record_price(client, cid, action["action_id"])
    assert r.status_code == 200, r.json()
    assert r.json()["state"] == "EXECUTED"
    m = r.json()["movement"]
    assert (m["movement_id"], m["kind"], m["lot_id"], m["qty"], m["recorded_by"], m["scenario_day"]) == \
        (f"{action['action_id']}:P", "PRICE_APPLIED", lot_id, before["qty"], src, run["day"])
    after = world_lot(run, lot_id)
    assert after["run_id"] == run["run_id"]  # written to the workspace's own copy (05.2 D-09)
    assert after["markdown_pct"] == max(before["markdown_pct"], action["pct"])
    assert markdown_order(run["run_id"], cid)["state"] == "DONE"
    assert lot_price_states(run["run_id"], cid) == {"DONE"}
    assert case_row(run["run_id"], cid)["state"] == "EXECUTED"
    [entry] = logged(run["run_id"], cid, "price_applied")
    assert entry["actor"] == {"account": src, "role": "manager", "branch": src}
    assert (entry["action_id"], entry["pct"], entry["markdown_pct"], entry["state"]) == \
        (action["action_id"], action["pct"], after["markdown_pct"], "EXECUTED")
    r = post(client, f"/api/cases/{cid}/close", {"reason": "Price changed on the shelf"})
    assert r.status_code == 200 and r.json() == {"state": "CLOSED", "closed_as": "COMPLETED"}, r.json()


def test_price_applied_needs_the_confirmation_the_source_seat_and_a_markdown(client):
    run, case, action, src = markdown_prepared(client)
    cid, path = case["case_id"], f"/api/cases/{case['case_id']}/price-applied"
    before = world_lot(run, action["lot_id"])
    for body in ({"action_id": action["action_id"]}, {"action_id": action["action_id"], "confirmed": False},
                 {"action_id": action["action_id"], "confirmed": "yes"}):
        r = post(client, path, body)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), body
        assert "confirmed" in r.json()["message"]
    r = record_price(client, cid, f"{cid}#9:price")
    assert (r.status_code, r.json()["error"]) == (404, "not_found")
    other = next(b for b in refdata.get(run["dataset_version"]).branches if b != src)
    sign_in(client, other)
    r = record_price(client, cid, action["action_id"])
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    assert world_lot(run, action["lot_id"]) == before  # no refusal changed a price
    assert markdown_order(run["run_id"], cid)["state"] == "PLANNED"
    assert logged(run["run_id"], cid, "price_applied") == []
    # on a transfer work order: 409 wrong_state
    t_run, transfer, t_src, _ = prepared(client)
    r = record_price(client, transfer["case_id"], transfer["action_id"])
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")
    assert work_order(t_run["run_id"], transfer["case_id"])["state"] == "PLANNED"


def test_a_transfer_only_decision_executes_at_its_receipt(client):
    run, action, src, dest = prepared(client)
    cid = action["case_id"]
    rest = markdown_order(run["run_id"], cid)
    if rest is not None:  # the story plan marks its rest down: void that work order (test scratch, as Next day voids
        with db.tx() as conn:  # a price change that is not deeper), so the transfer is the decision's only work
            db.run(conn, "UPDATE world_actions SET state = 'VOIDED' WHERE run_id = :r AND world = 'shelfshift' "
                         "AND action_id = :a", r=run["run_id"], a=rest["action_id"])
    sent = committed(action)
    assert post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, sent)).status_code == 200
    sign_in(client, dest)
    r = post(client, f"/api/cases/{cid}/receipt", receipt_body(action, sent))
    assert r.status_code == 200 and r.json()["state"] == "EXECUTED", r.json()
    assert case_row(run["run_id"], cid)["state"] == "EXECUTED"


def test_transfer_and_markdown_receipt_first_then_the_price(client):
    run, action, src, dest = prepared(client)
    cid = action["case_id"]
    rest = markdown_order(run["run_id"], cid)
    if rest is None:
        pytest.skip("the story plan has no marked-down rest on this template")  # never on the synthetic set
    sent = committed(action)
    assert post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, sent)).status_code == 200
    sign_in(client, dest)
    r = post(client, f"/api/cases/{cid}/receipt", receipt_body(action, sent))
    assert r.status_code == 200 and r.json()["state"] == "PREPARED", r.json()  # the rest's price is still to change
    assert case_row(run["run_id"], cid)["state"] == "PREPARED"
    sign_in(client, src)
    r = record_price(client, cid, rest["action_id"])  # after the transfer was received
    assert r.status_code == 200 and r.json()["state"] == "EXECUTED", r.json()
    assert case_row(run["run_id"], cid)["state"] == "EXECUTED"
    assert work_order(run["run_id"], cid)["state"] == "RECEIVED"


def test_transfer_and_markdown_price_first_then_dispatch_and_receipt(client):
    run, action, src, dest = prepared(client)
    cid = action["case_id"]
    rest = markdown_order(run["run_id"], cid)
    if rest is None:
        pytest.skip("the story plan has no marked-down rest on this template")  # never on the synthetic set
    r = record_price(client, cid, rest["action_id"])
    assert r.status_code == 200 and r.json()["state"] == "PREPARED", r.json()  # the transfer is still to do
    assert world_lot(run, action["lot_id"])["markdown_pct"] >= rest["pct"]
    # the dispatch check rechecks only the work still to do: the lot already carries the rest's percent, so the
    # transfer is rechecked without its rest instead of being refused as "lot already N% off"
    v = client.get(f"/api/cases/{cid}/execution").json()
    assert v["dispatch"] == {"allowed": True, "blocks": []}, v["dispatch"]
    sent = committed(action)
    r = post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, sent))
    assert r.status_code == 200 and r.json()["state"] == "PREPARED", r.json()
    sign_in(client, dest)
    r = post(client, f"/api/cases/{cid}/receipt", receipt_body(action, sent))
    assert r.status_code == 200 and r.json()["state"] == "EXECUTED", r.json()
    assert case_row(run["run_id"], cid)["state"] == "EXECUTED"
