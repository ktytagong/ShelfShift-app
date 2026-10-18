"""Dispatch and receipt recorded by people, and the execution view (06.1 D-34, D-48, D-49, D-65; 04-API-CONTRACT
"Product mode (06.1)", Execution).

Every call goes through the real app. The story case (SUS-19, the milk-story tag) is driven to PREPARED over HTTP:
the source manager signs in, `cases.owner_taken_by` is set to the source store directly in the database before Send
(a documented test shortcut, so these tests pass whether or not 06.1-08's owner rule has landed; 06.1-08's own tests
prove that rule), then send as the source, accept as the destination, approve and prepare as the source. Quantities
are derived from the work order (its committed quantity), never pasted from the seed.

A receipt executes the case only when no other work order of it is left to do (06.1-11): the story's transfer has a
marked-down rest, whose MARKDOWN work order stays PLANNED until its price change is recorded, so `after_receipt`
reads the expected state from the work orders before the receipt.
"""
from __future__ import annotations

import pytest

from api import cases, config, db, execution, worker
from api.app import COOKIE
from tests.api.conftest import fresh, key, sign_in, world_lot, world_rows
from tests.api.verify_helpers import ack, shown

STORY_TAG = cases.STORY_TAG


def post(c, path, body=None, headers=None):
    return c.post(path, json={} if body is None else body, headers=headers or key())


def run_of(c) -> dict:
    """The demo_runs row of the client's workspace (from its session row)."""
    with db.tx() as conn:
        s = db.one(conn, "SELECT run_id FROM sessions WHERE session_id = :s", s=c.cookies.get(COOKIE))
    return fresh(s["run_id"])


def story_case(run_id: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = :t ORDER BY opened_day DESC LIMIT 1",
                      r=run_id, t=STORY_TAG)


def work_order(run_id: str, case_id: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                            "AND kind = 'TRANSFER' ORDER BY action_id DESC LIMIT 1", r=run_id, c=case_id)


def prepared(c) -> tuple[dict, dict, str, str]:
    """SUS-19 driven to PREPARED over HTTP; (run, transfer work order, source, destination). Leaves `c` signed in as
    the source manager."""
    run = run_of(c)
    case = story_case(run["run_id"])
    cid, src, v = case["case_id"], case["source_branch"], case["active_rec_version"]
    with db.tx() as conn:
        plan = cases.parse_plan(cases.active_rec(conn, case)["result"]["recommended"])
    assert plan.kind == "TRANSFER", "D-15 hand-back: SUS-19's pick is not a transfer"
    sign_in(c, src)
    with db.tx() as conn:  # the documented shortcut: ownership taken by the source store (06.1-08 owns the route)
        db.run(conn, "UPDATE cases SET owner_taken_by = :a, owner_taken_at = now() WHERE run_id = :r AND case_id = :c",
               a=src, r=run["run_id"], c=cid)
    assert post(c, f"/api/cases/{cid}/send", {"rec_version": v}).json()["state"] == "PENDING_APPROVAL"
    sign_in(c, plan.dest)
    given = ack(run["run_id"], cid)  # approve and accept acknowledge the Advisory facts (07-CONTEXT D-26)
    page = shown(run, cid)  # and name the plan the page shows (07 review C)
    assert post(c, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "accept", "plan_key": page,
                                                  "acknowledged": given}).status_code == 200
    sign_in(c, src)
    assert post(c, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "approve", "plan_key": page,
                                                  "acknowledged": given}).json() == {"state": "APPROVED"}
    assert post(c, f"/api/cases/{cid}/prepare").json() == {"state": "PREPARED"}
    action = work_order(run["run_id"], cid)
    assert action["state"] == "PLANNED"
    return fresh(run["run_id"]), action, src, plan.dest


def after_receipt(run_id: str, case_id: str) -> str:
    """The case's state a receipt of its transfer leaves (06.1-11): PREPARED while another work order of it (a
    markdown rest) is still PLANNED, else EXECUTED. Read before the receipt."""
    with db.tx() as conn:
        left = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                            "AND case_id = :c AND kind <> 'TRANSFER' AND state = 'PLANNED'", r=run_id, c=case_id)["n"]
    return "PREPARED" if left else "EXECUTED"


def dispatch_body(action: dict, qty: int, **over) -> dict:
    return {"action_id": action["action_id"], "qty": qty, "lot_id": action["lot_id"], "condition": "as_expected",
            **over}


def committed(action: dict) -> int:
    return int(action["qty"])


def bookings_of(run_id: str, case_id: str) -> dict:
    with db.tx() as conn:
        out: dict = {}
        for r in db.rows(conn, "SELECT kind, state FROM reservations WHERE run_id = :r AND case_id = :c",
                         r=run_id, c=case_id):
            out.setdefault(r["kind"], set()).add(r["state"])
        return out


def logged(run_id: str, case_id: str, type_: str) -> list[dict]:
    with db.tx() as conn:
        return [r["payload"] for r in db.rows(conn, "SELECT payload FROM activity_log WHERE run_id = :r "
                                                    "AND case_id = :c AND type = :t ORDER BY seq",
                                              r=run_id, c=case_id, t=type_)]


def template_lot(run: dict, lot_id: str) -> dict | None:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                      r=run["template_run_id"], l=lot_id)


def physical_of(run_id: str, lot_id: str) -> float | None:
    with db.tx() as conn:
        row = db.one(conn, "SELECT qty FROM truth_lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                     r=run_id, l=lot_id)
    return None if row is None else float(row["qty"])


# --- Task 1 (tracer): dispatch over HTTP by the source manager -------------------------------------------------------

def test_the_source_manager_dispatches_the_prepared_story_transfer(client):
    run, action, src, _ = prepared(client)
    cid, lot_id = action["case_id"], action["lot_id"]
    before, tpl_before = world_lot(run, lot_id), template_lot(run, lot_id)
    qty = committed(action) - 6
    r = post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, qty))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["state"] == "PREPARED"
    m = body["movement"]
    assert (m["kind"], m["qty"], m["lot_id"], m["recorded_by"], m["scenario_day"]) == \
        ("DISPATCH", qty, lot_id, src, run["day"])
    assert m["recorded_at"] and m["condition"] == "as_expected" and m["action_id"] == action["action_id"]
    # the workspace's record and physical stock went down by qty; the template's row is untouched (05.2 D-09)
    after = world_lot(run, lot_id)
    assert after["run_id"] == run["run_id"] and after["qty"] == pytest.approx(before["qty"] - qty)
    assert template_lot(run, lot_id) == tpl_before
    assert physical_of(run["run_id"], lot_id) == pytest.approx(
        max(physical_of(run["template_run_id"], lot_id) - qty, 0.0))
    assert work_order(run["run_id"], cid)["state"] == "DISPATCHED"
    booked = bookings_of(run["run_id"], cid)
    assert booked["SOURCE_LOT"] == booked["TRUCK"] == {"DONE"}
    assert booked["STORAGE"] == {"COMMITTED"} and booked["DEMAND"] == {"CONVERTED"}  # the arrival still needs them
    [entry] = logged(run["run_id"], cid, "dispatched")
    assert entry["actor"] == {"account": src, "role": "manager", "branch": src}
    assert (entry["qty"], entry["lot_id"], entry["action_id"], entry["condition"]) == \
        (qty, lot_id, action["action_id"], "as_expected")
    assert fresh(run["run_id"])["day"] == run["day"]  # nothing moved the clock
    assert story_case(run["run_id"])["state"] == "PREPARED"  # the step stays Execute until the receipt (D-34)


def test_a_retried_dispatch_with_its_key_records_once(client):
    run, action, _, _ = prepared(client)
    cid, lot_id = action["case_id"], action["lot_id"]
    before = world_lot(run, lot_id)["qty"]
    h, body = key(), dispatch_body(action, committed(action) - 6)
    first = post(client, f"/api/cases/{cid}/dispatch", body, headers=h)
    again = post(client, f"/api/cases/{cid}/dispatch", body, headers=h)
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert world_lot(run, lot_id)["qty"] == pytest.approx(before - body["qty"])  # once, not twice
    assert len(logged(run["run_id"], cid, "dispatched")) == 1
    with db.tx() as conn:
        n = db.one(conn, "SELECT count(*) AS n FROM movements WHERE run_id = :r AND case_id = :c",
                   r=run["run_id"], c=cid)["n"]
    assert n == 1


# --- Task 2: receipt by the destination, discrepancies, the field rules ----------------------------------------------

def dispatched(c, short: int = 6) -> tuple[dict, dict, str, str, int]:
    """The story prepared and dispatched (committed − `short` units) by its source manager; `c` is then signed in as
    the destination manager. (run, work order, source, destination, units dispatched)."""
    run, action, src, dest = prepared(c)
    qty = committed(action) - short
    r = post(c, f"/api/cases/{action['case_id']}/dispatch", dispatch_body(action, qty))
    assert r.status_code == 200, r.json()
    sign_in(c, dest)
    return run, action, src, dest, qty


def receipt_body(action: dict, received: int, damaged: int = 0, condition: str = "as_expected",
                 reason: str | None = None) -> dict:
    body = {"action_id": action["action_id"], "received_qty": received, "damaged_qty": damaged, "condition": condition}
    return body if reason is None else {**body, "reason": reason}


def inbound_row(run_id: str, inbound_id: str) -> list[dict]:
    return world_rows(run_id, "inbound", "world = 'shelfshift' AND inbound_id = :i", i=inbound_id)


KEPT = ("branch", "sku", "arrival_day", "arrival_min", "expiry_day", "expiry_source", "origin", "source_case_id")


def incoming_of(c, sku: str) -> float | None:
    """The signed-in store's Incoming figure for `sku` from GET /api/lots; None when it lists no lot of it."""
    items = c.get("/api/lots", params={"product": sku}).json()["items"]
    return items[0]["incoming"] if items else None


def test_a_short_dispatch_brings_the_pending_delivery_down_to_the_units_sent(client):
    """The destination expects what left, not what was committed (06.1 D-34, D-48; quick 261017-wzs finding 2)."""
    run, action, src, dest = prepared(client)
    new_lot, sku = action["new_lot_id"], action["sku"]
    [before] = inbound_row(run["run_id"], new_lot)
    assert before["qty"] == committed(action)
    sign_in(client, dest)
    incoming_before = incoming_of(client, sku)
    sign_in(client, src)
    sent = committed(action) - 6
    r = post(client, f"/api/cases/{action['case_id']}/dispatch", dispatch_body(action, sent))
    assert r.status_code == 200, r.json()
    [after] = inbound_row(run["run_id"], new_lot)
    assert after["qty"] == sent
    assert {k: after[k] for k in KEPT} == {k: before[k] for k in KEPT}  # only the quantity changes
    sign_in(client, dest)
    if incoming_before is not None:
        assert incoming_of(client, sku) == pytest.approx(incoming_before - 6)
    # else: the destination lists no lot of this product, so GET /api/lots shows no Incoming figure to compare and
    # the pending delivery row (what Incoming and the engine input read) is the whole check.


def test_a_full_dispatch_leaves_the_pending_delivery_as_committed(client):
    run, action, _, _ = prepared(client)
    [before] = inbound_row(run["run_id"], action["new_lot_id"])
    r = post(client, f"/api/cases/{action['case_id']}/dispatch", dispatch_body(action, committed(action)))
    assert r.status_code == 200, r.json()
    assert inbound_row(run["run_id"], action["new_lot_id"]) == [before]


def test_a_short_and_damaged_receipt_keeps_received_apart_and_opens_two_discrepancies(client):
    run, action, _, dest, sent = dispatched(client)
    cid, new_lot = action["case_id"], action["new_lot_id"]
    assert len(inbound_row(run["run_id"], new_lot)) == 1  # the pending delivery, until a person receives it
    received, expected = sent - 4, after_receipt(run["run_id"], cid)
    r = post(client, f"/api/cases/{cid}/receipt",
             receipt_body(action, received, 2, "damaged", "two crates crushed"))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["state"] == expected
    m = body["movement"]
    assert (m["kind"], m["qty"], m["damaged_qty"], m["lot_id"], m["recorded_by"], m["condition"], m["reason"]) == \
        ("RECEIPT", received, 2, new_lot, dest, "damaged", "two crates crushed")
    assert sorted((d["kind"], d["units"], d["open_units"], d["state"]) for d in body["discrepancies"]) == \
        [("DAMAGED", 2, 2, "OPEN"), ("SHORT", 2, 2, "OPEN")]
    lot = world_lot(run, new_lot)
    assert (lot["branch"], lot["qty"], lot["moved_from"], lot["origin"]) == (dest, received, action["lot_id"], "moved")
    assert physical_of(run["run_id"], new_lot) == received  # record and physical: what the person counted in
    assert inbound_row(run["run_id"], new_lot) == []
    assert work_order(run["run_id"], cid)["state"] == "RECEIVED"
    assert story_case(run["run_id"])["state"] == expected
    with db.tx() as conn:  # dispatched and received stay two separate records (D-48)
        kinds = {m["kind"]: (m["qty"], m["damaged_qty"]) for m in db.rows(
            conn, "SELECT kind, qty, damaged_qty FROM movements WHERE run_id = :r AND case_id = :c",
            r=run["run_id"], c=cid)}
    assert kinds == {"DISPATCH": (sent, 0), "RECEIPT": (received, 2)}
    [entry] = logged(run["run_id"], cid, "received")
    assert entry["actor"] == {"account": dest, "role": "manager", "branch": dest}
    assert (entry["dispatched_qty"], entry["received_qty"], entry["damaged_qty"]) == (sent, received, 2)
    assert sorted((e["kind"], e["units"]) for e in logged(run["run_id"], cid, "discrepancy_opened")) == \
        [("DAMAGED", 2), ("SHORT", 2)]


def test_receipt_rules_reason_bounds_seat_and_state(client):
    run, action, src, dest = prepared(client)
    cid, path = action["case_id"], f"/api/cases/{action['case_id']}/receipt"
    sign_in(client, dest)
    r = post(client, path, receipt_body(action, 1))
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")  # nothing dispatched yet
    sign_in(client, src)
    sent = committed(action) - 6
    assert post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, sent)).status_code == 200
    r = post(client, path, receipt_body(action, sent))
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")  # the source never receives for the destination
    sign_in(client, dest)
    r = post(client, path, receipt_body(action, sent - 3, 1, "damaged"))
    assert (r.status_code, r.json()["error"]) == (400, "bad_request") and "reason" in r.json()["message"]
    r = post(client, path, receipt_body(action, sent - 1, 0, reason="  "))
    assert (r.status_code, r.json()["error"]) == (400, "bad_request") and "reason" in r.json()["message"]
    r = post(client, path, receipt_body(action, sent, 1, "damaged", "one extra?"))
    assert (r.status_code, r.json()["error"]) == (400, "bad_request") and "received_qty" in r.json()["message"]
    r = post(client, path, receipt_body(action, -1, 0, reason="x"))
    assert (r.status_code, r.json()["error"]) == (400, "bad_request") and "received_qty" in r.json()["message"]
    r = post(client, path, receipt_body(action, 1.5))
    assert (r.status_code, r.json()["error"]) == (400, "bad_request") and "received_qty" in r.json()["message"]
    r = post(client, path, receipt_body(action, sent, condition="lost"))
    assert (r.status_code, r.json()["error"]) == (400, "bad_request") and "condition" in r.json()["message"]
    assert work_order(run["run_id"], cid)["state"] == "DISPATCHED"  # every refusal wrote nothing
    assert world_lot(run, action["new_lot_id"]) is None
    assert story_case(run["run_id"])["state"] == "PREPARED"


def test_a_full_receipt_in_good_condition_opens_no_discrepancy(client):
    run, action, _, _, sent = dispatched(client, short=0)
    expected = after_receipt(run["run_id"], action["case_id"])
    r = post(client, f"/api/cases/{action['case_id']}/receipt", receipt_body(action, sent))
    assert r.status_code == 200, r.json()
    assert (r.json()["state"], r.json()["discrepancies"]) == (expected, [])
    assert world_lot(run, action["new_lot_id"])["qty"] == sent
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM discrepancies WHERE run_id = :r", r=run["run_id"])["n"] == 0


def test_dispatch_field_rules_and_a_lot_hold_refuse_before_any_write(client):
    run, action, _, _ = prepared(client)
    cid, lot_id = action["case_id"], action["lot_id"]
    path, before = f"/api/cases/{cid}/dispatch", world_lot(run, lot_id)
    for body, field in ((dispatch_body(action, committed(action) + 1), "qty"),
                        (dispatch_body(action, 0), "qty"),
                        (dispatch_body(action, 2.5), "qty"),
                        (dispatch_body(action, 1, lot_id=f"{lot_id}-other"), "lot_id"),
                        (dispatch_body(action, 1, condition="dented"), "condition")):
        r = post(client, path, body)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), (field, r.json())
        assert field in r.json()["message"], (field, r.json())
    with db.tx() as conn:  # an ACTIVE hold on the lot (Phase 7's table; test scratch in this workspace)
        db.run(conn, "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, state) VALUES "
                     "(:r, 'h-1', :l, :b, 'MIXED_DATES', 'Mixed dates found on the shelf', :b, 'ACTIVE')",
               r=run["run_id"], l=lot_id, b=action["origin"])
    r = post(client, path, dispatch_body(action, committed(action)))
    assert (r.status_code, r.json()["error"]) == (409, "dispatch_blocked")
    assert [(b["kind"], b["id"], b["reason"]) for b in r.json()["blocks"]] == \
        [("lot_hold", "h-1", "Mixed dates found on the shelf")]
    assert world_lot(run, lot_id) == before  # nothing written: the record is the template's, unchanged
    assert work_order(run["run_id"], cid)["state"] == "PLANNED"
    assert logged(run["run_id"], cid, "dispatched") == []


# --- Task 3: the execution view, and Next day never executes or receives a dispatched work order --------------------

def view(c, case_id: str) -> dict:
    r = c.get(f"/api/cases/{case_id}/execution")
    assert r.status_code == 200, r.json()
    return r.json()


VIEW_KEYS = {"dispatch", "work_orders", "movements", "discrepancies", "after_dispatch", "options"}


def test_the_execution_view_follows_dispatch_and_receipt(client):
    run, action, src, dest = prepared(client)
    cid = action["case_id"]
    v = view(client, cid)
    assert set(v) == VIEW_KEYS
    assert v["dispatch"] == {"allowed": True, "blocks": []}
    [wo] = [w for w in v["work_orders"] if w["kind"] == "TRANSFER"]
    assert (wo["action_id"], wo["state"], wo["qty"], wo["lot_id"], wo["new_lot_id"], wo["origin"], wo["dest"]) == \
        (action["action_id"], "PLANNED", action["qty"], action["lot_id"], action["new_lot_id"], src, dest)
    assert (wo["dispatched_qty"], wo["received_qty"], wo["damaged_qty"]) == (None, None, None)
    assert v["movements"] == v["discrepancies"] == v["after_dispatch"] == []
    assert v["options"] == execution.settings_section()
    sent = committed(action) - 6
    assert post(client, f"/api/cases/{cid}/dispatch", dispatch_body(action, sent)).status_code == 200
    v = view(client, cid)
    [m] = v["movements"]
    assert (m["kind"], m["qty"], m["recorded_by"], m["role"], m["branch"], m["scenario_day"]) == \
        ("DISPATCH", sent, src, "manager", src, run["day"])
    assert m["recorded_at"]
    assert v["dispatch"]["allowed"] is False
    assert [(b["kind"], b["reason"]) for b in v["dispatch"]["blocks"]] == [("wrong_state", "Work order is DISPATCHED")]
    [wo] = [w for w in v["work_orders"] if w["kind"] == "TRANSFER"]
    assert (wo["state"], wo["dispatched_qty"], wo["received_qty"]) == ("DISPATCHED", sent, None)
    sign_in(client, dest)
    r = post(client, f"/api/cases/{cid}/receipt", receipt_body(action, sent - 5, 0, reason="five missing"))
    assert r.status_code == 200, r.json()
    v = view(client, cid)
    assert [m["kind"] for m in v["movements"]] == ["DISPATCH", "RECEIPT"]  # by recorded time
    assert [(d["kind"], d["units"], d["open_units"], d["state"]) for d in v["discrepancies"]] == \
        [("SHORT", 5, 5, "OPEN")]
    [wo] = [w for w in v["work_orders"] if w["kind"] == "TRANSFER"]
    assert (wo["state"], wo["dispatched_qty"], wo["received_qty"], wo["damaged_qty"]) == \
        ("RECEIVED", sent, sent - 5, 0)


def test_the_execution_view_never_calls_the_engine_worker(client, monkeypatch):
    _, action, _, _ = prepared(client)

    def refuse(*args, **kwargs):
        raise AssertionError("a GET called the engine worker")
    monkeypatch.setattr(worker, "run", refuse)
    assert view(client, action["case_id"])["dispatch"]["allowed"] is True
    r = client.get("/api/cases/no-such-case/execution")
    assert (r.status_code, r.json()["error"]) == (404, "not_found")


def test_next_day_leaves_a_dispatched_work_order_for_its_receipt(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)  # Next day is a demo control (06.1 D-62)
    run, action, _, dest, sent = dispatched(client)
    cid, new_lot = action["case_id"], action["new_lot_id"]
    version = client.get("/api/session").json()["version"]
    r = post(client, "/api/demo/advance-day", {"expected_version": version})
    assert r.status_code == 200 and r.json()["advanced"], r.json()
    assert story_case(run["run_id"])["state"] == "PREPARED"  # nothing executes or receives it but a person (D-34)
    assert work_order(run["run_id"], cid)["state"] == "DISPATCHED"
    assert world_lot(run, new_lot) is None
    assert logged(run["run_id"], cid, "executed") == []
    assert len(inbound_row(run["run_id"], new_lot)) == 1  # still on its way
    expected = after_receipt(run["run_id"], cid)  # Next day may have carried out the rest's price change
    r = post(client, f"/api/cases/{cid}/receipt", receipt_body(action, sent))
    assert r.status_code == 200 and r.json()["state"] == expected, r.json()
    moved = world_rows(run["run_id"], "lots", "world = 'shelfshift' AND lot_id = :l", l=new_lot)
    assert [(m["branch"], m["qty"]) for m in moved] == [(dest, sent)]  # once, never also delivered by the day
    assert inbound_row(run["run_id"], new_lot) == []
    assert world_rows(run["run_id"], "lots", "world = 'shelfshift' AND moved_from = :l", l=action["lot_id"]) == moved


def test_settings_section_names_a_malformed_path(monkeypatch):
    from data.config import ConfigError
    good = config.SETTINGS["execution"]
    for bad, path in (({**good, "conditions": []}, "settings.execution.conditions"),
                      ({**good, "resolutions": [{"key": "x"}]}, "settings.execution.resolutions[0]"),
                      ({**good, "conditions": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}]},
                       "settings.execution.conditions")):
        monkeypatch.setitem(config.SETTINGS, "execution", bad)
        with pytest.raises(ConfigError) as err:
            execution.settings_section()
        assert str(err.value).startswith(path + ":")
