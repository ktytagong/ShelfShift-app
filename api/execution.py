"""The dispatch check on the server, before any stock leaves (06.1-CONTEXT D-35, D-48, D-49; 06.1-RESEARCH Pattern 5;
04-API-CONTRACT "Product mode (06.1)", Execution).

Named dispatch_check, never eligibility: 05.2 uses that word for stage-1 transfer runs (the case detail's
`eligibility`), so 06.1's concept is `dispatch_check` / `dispatch_blocked`.

`dispatch_check(conn, run, case, action)` returns every block that applies, each `{kind, id, lot_ids, owner_branch,
reason, resolution}`, in the order the contract lists the kinds; an empty list means the server confirms the work
order may leave. Hiding a button is not enough (D-49): the dispatch route (06.1-09) calls this in its transaction and
refuses with 409 dispatch_blocked, writing nothing, when any block comes back.

- Support present first: `lot_holds` and `review_flags` are Phase 7's tables, created by 06.1 with Phase 7's DDL
  (api/schema.sql, 06.1-04). When either is missing the only answer is the checks_unavailable block. Missing support
  never reads as "no hold" (D-49).
- wrong_state: the case is not PREPARED, or the work order is not a PLANNED transfer of this case.
- cancelled: the work order is CANCELLED, or the case is CLOSED as CANCELLED (07-11's cancel; D-35).
- lot_hold: an ACTIVE `lot_holds` row on an involved lot (`involved_lots`).
- review_flag: an uncleared `review_flags` row of this case, while its work order has not left. Phase 7's "Review
  before dispatch" note becomes a check here (D-35); once the work order is DISPATCHED or RECEIVED the same flag is
  listed by `after_dispatch` instead and blocks nothing.
- recheck: the Prepare recheck again (`workflow._locked_input` and `_recheck`, today's raw input without this case's
  own bookings). Each reason comes back word for word as its own block. It runs only on a PREPARED case with a PLANNED
  transfer, the only state a recheck describes.

`after_dispatch(conn, run, case)`: once the case's transfer is DISPATCHED or RECEIVED, an uncleared review flag is an
operational exception (D-35). It keeps the commitment and blocks nothing already moved; before dispatch the same flag
is a review_flag block instead.

Nothing here writes stock or workflow rows. The recheck takes the same transaction-scoped advisory locks Prepare
takes, released at commit or rollback.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timezone

from data.config import ConfigError
from engine.model import MARKDOWN, TRANSFER

from . import accounts, advance, bookings, cases, config, db, overlay, ownership, workflow
from .errors import ApiError

WORLD = "shelfshift"
SUPPORT = ("lot_holds", "review_flags")  # Phase 7's tables (07-08-PLAN.md step 2, 07-06-PLAN.md step 2)
MOVED = ("DISPATCHED", "RECEIVED")  # a transfer whose stock has left the source

# Server sentences. "(proposed)" marks wording the contract or UI-SPEC proposes and a later review may change.
BLOCKED = "Dispatch is blocked; see the reasons."  # the 409 dispatch_blocked message (proposed)
UNAVAILABLE = "Dispatch checks unavailable"
UNAVAILABLE_RESOLUTION = "Dispatch stays blocked until the checks can run."  # (proposed)
CANCELLED_REASON = "This work order was cancelled. Nothing can be dispatched."  # (proposed)
CANCELLED_RESOLUTION = "Start a new decision if the stock still needs to move."  # (proposed)
WRONG_STATE_RESOLUTION = "Only a prepared transfer with a planned work order can be dispatched."  # (proposed)
HOLD_RESOLUTION = "Release the hold or resolve its check"  # (proposed)
FLAG_REASON = "New confirmed information · review required"
FLAG_RESOLUTION = "Review before dispatch"
RECHECK_RESOLUTION = "Check the plan against the current records before anything moves."  # (proposed)
AFTER_DISPATCH_LABEL = "New confirmed information · operational exception · review"  # (proposed)
AFTER_DISPATCH_KEY = "operational_exception"


def _block(kind: str, id_: str | None, lot_ids, owner_branch: str | None, reason: str, resolution: str) -> dict:
    return {"kind": kind, "id": id_, "lot_ids": list(lot_ids), "owner_branch": owner_branch, "reason": reason,
            "resolution": resolution}


def _regclass(conn, name: str) -> str | None:
    """`to_regclass(name)` as text: None when the table does not exist in the search path."""
    return db.one(conn, "SELECT CAST(to_regclass(:t) AS text) AS t", t=name)["t"]


def support_present(conn) -> bool:
    """Both of Phase 7's tables exist; otherwise no check can say a lot is clear (D-49)."""
    return all(_regclass(conn, t) is not None for t in SUPPORT)


def involved_lots(conn, run: dict, case: dict) -> list[str]:
    """The lots a hold on which stops this case's dispatch: the case lot, the lots moved from it (`moved_from`), and the
    lot it was moved from (07-01 step 6). Read as the workspace reads them (api/overlay.py)."""
    lot_id = case["lot_id"]
    out = {lot_id}
    row = overlay.lot(conn, run, WORLD, lot_id)
    if row is not None and row["moved_from"]:
        out.add(row["moved_from"])
    read, p = overlay.everywhere(conn, run, "lots")
    out |= {r["lot_id"] for r in db.rows(
        conn, f"SELECT lots.lot_id FROM lots WHERE {read} AND lots.world = :w AND lots.sku = :s "
              "AND lots.moved_from = :l", w=WORLD, s=case["sku"], l=lot_id, **p)}
    return sorted(out)


def _state_blocks(case: dict, action: dict | None) -> list[dict]:
    """cancelled when the work order or the case was cancelled; else wrong_state per fact that does not hold."""
    lots, owner = [case["lot_id"]], case["source_branch"]
    ok_action = action is not None and action["case_id"] == case["case_id"]
    if (ok_action and action["state"] == "CANCELLED") or case.get("closed_as") == "CANCELLED":
        return [_block("cancelled", action["action_id"] if ok_action else None,
                       [action["lot_id"]] if ok_action else lots, owner, CANCELLED_REASON, CANCELLED_RESOLUTION)]
    out = []
    if case["state"] != "PREPARED":
        out.append(_block("wrong_state", None, lots, owner, f"Case is {case['state']}", WRONG_STATE_RESOLUTION))
    if not ok_action:
        out.append(_block("wrong_state", None, lots, owner, "This case has no such work order",
                          WRONG_STATE_RESOLUTION))
        return out
    if action["kind"] != TRANSFER:
        out.append(_block("wrong_state", action["action_id"], [action["lot_id"]], owner,
                          f"Work order is a {action['kind']}, not a transfer", WRONG_STATE_RESOLUTION))
    if action["state"] != "PLANNED":
        out.append(_block("wrong_state", action["action_id"], [action["lot_id"]], owner,
                          f"Work order is {action['state']}", WRONG_STATE_RESOLUTION))
    return out


def _hold_blocks(conn, run: dict, case: dict) -> list[dict]:
    rows = db.rows(conn, "SELECT hold_id, lot_id, branch, reason FROM lot_holds WHERE run_id = :r AND state = 'ACTIVE' "
                         "AND lot_id = ANY(:l) ORDER BY placed_at, hold_id",
                   r=run["run_id"], l=involved_lots(conn, run, case))
    return [_block("lot_hold", h["hold_id"], [h["lot_id"]], h["branch"], h["reason"], HOLD_RESOLUTION) for h in rows]


def _open_flags(conn, run: dict, case: dict) -> list[dict]:
    return db.rows(conn, "SELECT flag_id, fact_id, reason FROM review_flags WHERE run_id = :r AND case_id = :c "
                         "AND cleared_at IS NULL ORDER BY created_at, flag_id", r=run["run_id"], c=case["case_id"])


def _flag_blocks(conn, run: dict, case: dict) -> list[dict]:
    return [_block("review_flag", f["flag_id"], [case["lot_id"]], case["source_branch"], FLAG_REASON,
                   FLAG_RESOLUTION) for f in _open_flags(conn, run, case)]


def _price_done(conn, run: dict, case: dict) -> bool:
    """The current round's MARKDOWN work order is DONE: its price change was recorded (06.1-11 price_applied) or the
    demo's Next day carried it out."""
    return db.one(conn, "SELECT 1 AS x FROM world_actions WHERE run_id = :r AND world = :w AND case_id = :c "
                        "AND kind = 'MARKDOWN' AND state = 'DONE' "
                        "AND CAST(substring(action_id FROM '#([0-9]+):') AS int) = :n LIMIT 1",
                  r=run["run_id"], w=WORLD, c=case["case_id"], n=case["round"]) is not None


def _recheck_blocks(conn, run: dict, case: dict) -> list[dict]:
    """Prepare's recheck on today's raw input; a refusal's reasons word for word, one block each. A refusal without
    engine reasons (no verifiable source, missing data, an engine input error) is one block with its message.

    Only the work still to do is rechecked: once the plan's markdown rest is DONE (the price change recorded first,
    or carried out by Next day), the lot already carries that percent, so the transfer is rechecked without its rest
    (pct 0) instead of being refused as "lot already N% off" (06.1-11)."""
    try:
        plan = workflow._plan(conn, case)
        if plan.pct and plan.kind == TRANSFER and _price_done(conn, run, case):
            plan = replace(plan, pct=0)
        workflow._recheck(workflow._locked_input(conn, run, case, plan), plan)
    except ApiError as exc:
        reasons = exc.extra.get("reasons") or [exc.message]
        return [_block("recheck", None, [case["lot_id"]], case["source_branch"], r, RECHECK_RESOLUTION)
                for r in reasons]
    return []


def dispatch_check(conn, run: dict, case: dict, action: dict | None) -> list[dict]:
    """Every block that stops this work order leaving, in the contract's order; [] means dispatch may proceed. `case`
    is the case row (locked by the caller), `action` the work order's world_actions row (None when there is none)."""
    if not support_present(conn):
        return [_block("checks_unavailable", None, [case["lot_id"]], None, UNAVAILABLE, UNAVAILABLE_RESOLUTION)]
    states = _state_blocks(case, action)
    blocks = states + _hold_blocks(conn, run, case)
    if action is None or action["state"] not in MOVED:  # after dispatch a flag is an exception, never a block (D-35)
        blocks += _flag_blocks(conn, run, case)
    if not states:
        blocks += _recheck_blocks(conn, run, case)
    return blocks


def after_dispatch(conn, run: dict, case: dict) -> list[dict]:
    """The case's uncleared review flags once its transfer has left (DISPATCHED or RECEIVED), as operational
    exceptions `{flag_id, fact_id, reason, label_key}`: the commitment stands and nothing already moved is blocked
    (D-35). Before dispatch, and without the review_flags table, the list is empty (the dispatch check blocks then)."""
    if _regclass(conn, "review_flags") is None:
        return []
    moved = db.one(conn, "SELECT 1 AS x FROM world_actions WHERE run_id = :r AND world = :w AND case_id = :c "
                         "AND kind = :k AND state = ANY(:s) LIMIT 1",
                   r=run["run_id"], w=WORLD, c=case["case_id"], k=TRANSFER, s=list(MOVED))
    if moved is None:
        return []
    return [{"flag_id": f["flag_id"], "fact_id": f["fact_id"], "reason": f["reason"], "label_key": AFTER_DISPATCH_KEY}
            for f in _open_flags(conn, run, case)]


# --- dispatch, recorded by the source manager (06.1-09; D-34, D-48, D-49) -------------------------------------------

DISPATCH, RECEIPT = "DISPATCH", "RECEIPT"  # movement kinds (06.1-11 adds PRICE_APPLIED and RECONCILE)
CONDITIONS = "conditions"  # settings execution.conditions: the keys a dispatch or receipt condition may take
MOVEMENT_KEYS = ("movement_id", "case_id", "action_id", "kind", "lot_id", "qty", "damaged_qty", "condition", "reason",
                 "recorded_by", "role", "branch", "recorded_at", "scenario_day")


def _iso(value) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def movement_row(row: dict) -> dict:
    """A movements row as the API answers it: every column but run_id, `recorded_at` ISO UTC."""
    return {k: (_iso(row[k]) if k == "recorded_at" else row[k]) for k in MOVEMENT_KEYS}


def _work_order(conn, run: dict, case: dict, action_id: str) -> dict | None:
    """The work order row, locked; None when there is none or it belongs to another case."""
    row = db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = :w AND action_id = :a FOR UPDATE",
                 r=run["run_id"], w=WORLD, a=action_id)
    return row if row is not None and row["case_id"] == case["case_id"] else None


def _condition(condition: str) -> None:
    keys = [c["key"] for c in settings_section()[CONDITIONS]]
    if condition not in keys:
        raise ApiError(400, "bad_request", f"condition must be one of {', '.join(keys)}")


def _movement(conn, run: dict, actor: dict, movement_id: str, case_id: str, action_id: str, kind: str, lot_id: str,
              qty: float, damaged: float, condition: str, reason: str | None) -> dict:
    row = db.one(conn, "INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, lot_id, qty, "
                       "damaged_qty, condition, reason, recorded_by, role, branch, scenario_day) VALUES (:r, :m, :c, "
                       ":a, :k, :l, :q, :dq, :cond, :why, :by, :role, :b, :d) RETURNING *",
                 r=run["run_id"], m=movement_id, c=case_id, a=action_id, k=kind, l=lot_id, q=float(qty),
                 dq=float(damaged), cond=condition, why=reason, by=actor.get("account"), role=actor.get("role"),
                 b=actor.get("branch"), d=run["day"])
    return movement_row(row)


def dispatch(conn, run: dict, actor: dict, case_id: str, action_id: str, qty: int, lot_id: str,
             condition: str) -> dict:
    """The source manager records that `qty` units of the work order's lot left (06.1 D-48, D-49), in the caller's
    transaction under the run lock FOR UPDATE. Order: hold expiry; the case locked; the source seat (403); the work
    order locked; the fields (400 naming the field: a whole qty 1..committed, the work order's own lot, a condition
    key of settings execution.conditions); the dispatch check (any block: 409 dispatch_blocked with every block, and
    nothing written). Then the source series is materialized (the template's rows are never written), the source
    lot's record and physical quantity go down by qty (physical floored at 0), the work order is DISPATCHED, its
    SOURCE_LOT and TRUCK bookings DONE, a DISPATCH movement is written with the actor and the scenario day, and
    `dispatched` is logged. A short dispatch (qty below the committed units) also brings the destination's pending
    delivery (the inbound row `new_lot_id` bookings.commit wrote) down to qty, its arrival, expiry and origin unchanged,
    after materializing the destination series, so Incoming and every engine input read the units actually sent. The
    case stays PREPARED (step Execute) until the destination records the receipt (D-34)."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    workflow.require_source(actor, case)
    action = _work_order(conn, run, case, action_id)
    top = int(action["qty"]) if action is not None and action["qty"] is not None else None  # the committed units
    if qty < 1 or (top is not None and qty > top):
        raise ApiError(400, "bad_request", "qty must be a whole number from 1" + (f" to {top}" if top else ""))
    if action is not None and lot_id != action["lot_id"]:
        raise ApiError(400, "bad_request", f"lot_id must be the work order's lot {action['lot_id']}")
    _condition(condition)
    blocks = dispatch_check(conn, run, case, action)
    if blocks:
        raise ApiError(409, "dispatch_blocked", BLOCKED, blocks=blocks)
    source, sku, keys = case["source_branch"], action["sku"], {"r": run["run_id"], "w": WORLD, "l": action["lot_id"]}
    overlay.materialize(conn, run, WORLD, source, sku)  # the workspace's own copy of the source series (05.2 D-09)
    db.run(conn, "UPDATE lots SET qty = qty - :q WHERE run_id = :r AND world = :w AND lot_id = :l", q=float(qty),
           **keys)
    db.run(conn, "UPDATE truth_lots SET qty = GREATEST(qty - :q, 0) WHERE run_id = :r AND world = :w AND lot_id = :l",
           q=float(qty), **keys)
    db.run(conn, "UPDATE world_actions SET state = 'DISPATCHED' WHERE run_id = :r AND world = :w AND action_id = :a",
           r=run["run_id"], w=WORLD, a=action_id)
    if top is not None and qty < top:  # the destination expects what left, not what was committed (D-34, D-48)
        overlay.materialize(conn, run, WORLD, action["dest"], sku)  # a template delivery becomes the workspace's own
        db.run(conn, "UPDATE inbound SET qty = :q WHERE run_id = :r AND world = :w AND inbound_id = :i "
                     "AND source_case_id = :c", q=float(qty), r=run["run_id"], w=WORLD, i=action["new_lot_id"],
               c=case_id)
    db.run(conn, "UPDATE reservations SET state = 'DONE' WHERE run_id = :r AND case_id = :c AND round = :n "
                 "AND kind = ANY(:k) AND state = 'COMMITTED'",
           r=run["run_id"], c=case_id, n=case["round"], k=["SOURCE_LOT", "TRUCK"])  # the set-aside left on the truck
    movement = _movement(conn, run, actor, f"{action_id}:D", case_id, action_id, DISPATCH, action["lot_id"], qty, 0,
                         condition, None)
    db.log(conn, run["run_id"], run["day"], "dispatched", case_id, actor=accounts.logged_actor(actor), qty=qty,
           lot_id=action["lot_id"], action_id=action_id, condition=condition)
    return {"state": case["state"], "movement": movement}


# --- receipt, recorded by the destination manager (06.1-09; D-48, D-65) ---------------------------------------------

SHORT, DAMAGED = "SHORT", "DAMAGED"  # discrepancy kinds: units missing, units arrived damaged
DISCREPANCY_KEYS = ("discrepancy_id", "case_id", "action_id", "kind", "units", "open_units", "state", "opened_at",
                    "resolution", "resolved_by", "resolved_at", "reason")


def discrepancy_row(row: dict) -> dict:
    """A discrepancies row as the API answers it: every column but run_id, times ISO UTC."""
    return {k: (_iso(row[k]) if k in ("opened_at", "resolved_at") else row[k]) for k in DISCREPANCY_KEYS}


def _units(name: str, value: int) -> None:
    if value < 0:
        raise ApiError(400, "bad_request", f"{name} must be a whole number of at least 0")


def _arriving_lot(conn, run: dict, action: dict) -> dict:
    """The attributes of the stock arriving at the destination: the pending delivery row (bookings.commit wrote it
    from the source lot), else the source lot itself; as advance.step_world builds a moved lot."""
    keys = {"r": run["run_id"], "w": WORLD}
    pending = db.one(conn, "SELECT * FROM inbound WHERE run_id = :r AND world = :w AND inbound_id = :i",
                     i=action["new_lot_id"], **keys)
    src = pending or overlay.lot(conn, run, WORLD, action["lot_id"])
    if src is None:
        raise ApiError(409, "wrong_state", "The dispatched lot is no longer on the record", state="DISPATCHED")
    return {"expiry_day": src["expiry_day"], "expiry_source": src["expiry_source"]}


def receipt(conn, run: dict, actor: dict, case_id: str, action_id: str, received_qty: int, damaged_qty: int,
            condition: str, reason: str | None) -> dict:
    """The destination manager records what arrived (06.1 D-48, D-65; BUSINESS-FLOW-BRIEF §5), in the caller's
    transaction under the run lock FOR UPDATE. Order: hold expiry; the case locked; the work order locked (404 when
    the case has none by that id); the destination seat (403); a DISPATCHED transfer of a PREPARED case (409
    wrong_state); the fields (400 naming the field: whole numbers of at least 0, received + damaged at most the units
    dispatched, a condition key of settings execution.conditions, a reason when anything is damaged or missing).

    Then the destination series is materialized; the moved lot `new_lot_id` is inserted with the received quantity,
    record and physical, `moved_from` the source lot; the pending delivery row is removed; a RECEIPT movement; a SHORT
    discrepancy for the missing units and a DAMAGED one for the damaged units, each OPEN; the work order RECEIVED; the
    case EXECUTED only when no other work order of it is left to do (`_executed_when_done`: a markdown rest still
    PLANNED keeps it PREPARED until its price change is recorded, 06.1-11); `received` and each `discrepancy_opened`
    logged with the actor. The dispatch movement is never rewritten, and nothing is ever received without this
    call."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    action = _work_order(conn, run, case, action_id)
    if action is None:
        raise ApiError(404, "not_found", f"This case has no work order {action_id}")
    dest = action["dest"]
    workflow.require_manager(actor, dest, f"Only the {dest} manager can record this receipt" if dest else
                             "Only a transfer's destination manager can record a receipt")
    sent = db.one(conn, "SELECT qty FROM movements WHERE run_id = :r AND movement_id = :m AND kind = :k",
                  r=run["run_id"], m=f"{action_id}:D", k=DISPATCH)
    if action["kind"] != TRANSFER or action["state"] != "DISPATCHED" or sent is None:
        raise ApiError(409, "wrong_state", f"Work order is {action['state']}", state=action["state"])
    if case["state"] != "PREPARED":
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    _units("received_qty", received_qty)
    _units("damaged_qty", damaged_qty)
    sent = sent["qty"]
    if received_qty + damaged_qty > sent:
        raise ApiError(400, "bad_request", f"received_qty plus damaged_qty must not exceed the {sent:g} units "
                                           "dispatched")
    _condition(condition)
    missing = sent - received_qty - damaged_qty
    why = reason.strip() if isinstance(reason, str) else ""
    if (damaged_qty > 0 or missing > 0) and not why:
        raise ApiError(400, "bad_request", "reason is required for a short or damaged receipt")
    sku, new_lot = action["sku"], action["new_lot_id"]
    overlay.materialize(conn, run, WORLD, dest, sku)  # the workspace's own copy of the destination series (05.2 D-09)
    arriving = _arriving_lot(conn, run, action)
    advance.insert_lot(conn, run["run_id"], WORLD, {
        "lot_id": new_lot, "branch": dest, "sku": sku, "qty": float(received_qty), "expiry_day": arriving["expiry_day"],
        "received_seq": advance.MOVED_SEQ, "markdown_pct": 0, "expiry_source": arriving["expiry_source"],
        "last_verified_day": run["day"], "verified_by": "COUNT", "tag": None, "origin": advance.MOVED,
        "moved_from": action["lot_id"]}, float(received_qty))
    db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND world = :w AND inbound_id = :i", r=run["run_id"], w=WORLD,
           i=new_lot)
    movement = _movement(conn, run, actor, f"{action_id}:R", case_id, action_id, RECEIPT, new_lot, received_qty,
                         damaged_qty, condition, why or None)
    opened = []
    for kind, units in ((SHORT, missing), (DAMAGED, damaged_qty)):
        if units <= 0:
            continue
        row = db.one(conn, "INSERT INTO discrepancies (run_id, discrepancy_id, case_id, action_id, kind, units, "
                           "open_units, state, reason) VALUES (:r, :d, :c, :a, :k, :u, :u, 'OPEN', :why) RETURNING *",
                     r=run["run_id"], d=f"{action_id}:{kind}", c=case_id, a=action_id, k=kind, u=float(units), why=why)
        opened.append(discrepancy_row(row))
    db.run(conn, "UPDATE world_actions SET state = 'RECEIVED' WHERE run_id = :r AND world = :w AND action_id = :a",
           r=run["run_id"], w=WORLD, a=action_id)
    state = _executed_when_done(conn, run, case)
    db.log(conn, run["run_id"], run["day"], "received", case_id, actor=accounts.logged_actor(actor),
           action_id=action_id, lot_id=new_lot, dispatched_qty=sent, received_qty=received_qty, damaged_qty=damaged_qty,
           missing_qty=missing, condition=condition, reason=why or None)
    for d in opened:
        db.log(conn, run["run_id"], run["day"], "discrepancy_opened", case_id, actor=accounts.logged_actor(actor),
               discrepancy_id=d["discrepancy_id"], kind=d["kind"], units=d["units"], action_id=action_id)
    return {"state": state, "movement": movement, "discrepancies": opened}


def _executed_when_done(conn, run: dict, case: dict) -> str:
    """A stock decision is EXECUTED only when all of its work orders are done (06.1-11, orchestrator decision on
    06.1-09's open item): with none of them PLANNED or DISPATCHED any more, the PREPARED case becomes EXECUTED, as
    advance._lifecycle_after decides at the end of a demo day. Returns the case's state after the check."""
    left = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND world = :w AND case_id = :c "
                        "AND state IN ('PLANNED', 'DISPATCHED')", r=run["run_id"], w=WORLD, c=case["case_id"])["n"]
    if left or case["state"] != "PREPARED":
        return case["state"]
    db.run(conn, "UPDATE cases SET state = 'EXECUTED' WHERE run_id = :r AND case_id = :c", r=run["run_id"],
           c=case["case_id"])
    return "EXECUTED"


# --- the price change, recorded by the source manager (06.1-11; D-34, D-48; OQ-2 option A) ------------------------

PRICE_APPLIED = "PRICE_APPLIED"  # movement kind: a person changed the shelf price of the lot
NOT_CONFIRMED = "confirmed must be true: record the price change only once the shelf price was changed"  # (proposed)


def price_applied(conn, run: dict, actor: dict, case_id: str, action_id: str, confirmed) -> dict:
    """The source manager records that the shelf price was changed for a markdown work order (06.1 D-34, D-48), in
    the caller's transaction under the run lock FOR UPDATE. No price changes without this person's confirmation.
    Order: hold expiry; the case locked; the source seat (403); the work order locked (404 when the case has none by
    that id); a PLANNED MARKDOWN of a PREPARED case (409 wrong_state); `confirmed` exactly true (400).

    Then the lot's series is materialized; the lot's markdown becomes the deeper of its own and the work order's
    percent (record); the work order is DONE and the round's LOT_PRICE booking DONE; a PRICE_APPLIED movement
    `{action_id}:P` (qty: the lot's recorded units the new price applies to) with the actor; the case EXECUTED when
    no other work order is left to do, else it stays PREPARED (a transfer still to dispatch or receive);
    `price_applied` is logged with the actor."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    workflow.require_source(actor, case)
    action = _work_order(conn, run, case, action_id)
    if action is None:
        raise ApiError(404, "not_found", f"This case has no work order {action_id}")
    if action["kind"] != MARKDOWN:
        raise ApiError(409, "wrong_state", f"Work order is a {action['kind']}, not a markdown", state=action["state"])
    if action["state"] != "PLANNED":
        raise ApiError(409, "wrong_state", f"Work order is {action['state']}", state=action["state"])
    if case["state"] != "PREPARED":
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    if confirmed is not True:
        raise ApiError(400, "bad_request", NOT_CONFIRMED)
    lot = overlay.lot(conn, run, WORLD, action["lot_id"])
    if lot is None:
        raise ApiError(409, "wrong_state", f"Lot {action['lot_id']} is no longer on the record", state=action["state"])
    overlay.materialize(conn, run, WORLD, lot["branch"], lot["sku"])  # the workspace's own copy (05.2 D-09)
    before, pct = int(lot["markdown_pct"]), max(int(lot["markdown_pct"]), int(action["pct"]))
    keys = {"r": run["run_id"], "w": WORLD}
    db.run(conn, "UPDATE lots SET markdown_pct = :p WHERE run_id = :r AND world = :w AND lot_id = :l", p=pct,
           l=action["lot_id"], **keys)
    db.run(conn, "UPDATE world_actions SET state = 'DONE' WHERE run_id = :r AND world = :w AND action_id = :a",
           a=action_id, **keys)
    db.run(conn, "UPDATE reservations SET state = 'DONE' WHERE run_id = :r AND case_id = :c AND round = :n "
                 "AND kind = 'LOT_PRICE' AND state = 'COMMITTED'", r=run["run_id"], c=case_id, n=case["round"])
    movement = _movement(conn, run, actor, f"{action_id}:P", case_id, action_id, PRICE_APPLIED, action["lot_id"],
                         lot["qty"], 0, None, None)
    state = _executed_when_done(conn, run, case)
    db.log(conn, run["run_id"], run["day"], "price_applied", case_id, actor=accounts.logged_actor(actor),
           action_id=action_id, lot_id=action["lot_id"], pct=int(action["pct"]), from_pct=before, markdown_pct=pct,
           state=state)
    return {"state": state, "movement": movement}


# --- the execution view (06.1-09; GET /api/cases/{id}/execution) ----------------------------------------------------

WORK_ORDER_KEYS = ("action_id", "kind", "state", "lot_id", "new_lot_id", "sku", "origin", "dest", "qty", "run_code",
                   "run_day", "pct", "effective_day")
ROUND_ORDER = "CAST(substring(action_id FROM '#([0-9]+):') AS int) NULLS FIRST, action_id"  # a case's rounds in order


def execution_view(conn, run: dict, case_id: str) -> dict:
    """What was planned, dispatched and received for a case, in one read (06.1 D-34, D-48, D-65): `dispatch`
    {allowed, blocks} for its first PLANNED transfer work order (else its latest transfer, or none: wrong_state);
    `work_orders` (each world action of the case, with the units its movements recorded); `movements` by recorded
    time; `discrepancies` by opening time; `after_dispatch`; and `options`, the settings' conditions and resolutions
    a form offers. Hold expiry applies, as on any read that touches a case; the engine worker is never called (the
    dispatch check's recheck is the engine's limit check in this process). 404 for a case not in this workspace."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id)
    keys = {"r": run["run_id"], "c": case_id}
    actions = db.rows(conn, f"SELECT * FROM world_actions WHERE run_id = :r AND world = :w AND case_id = :c "
                            f"ORDER BY {ROUND_ORDER}", w=WORLD, **keys)
    moves = [movement_row(m) for m in db.rows(conn, "SELECT * FROM movements WHERE run_id = :r AND case_id = :c "
                                                    "ORDER BY recorded_at, movement_id", **keys)]
    gaps = [discrepancy_row(d) for d in db.rows(conn, "SELECT * FROM discrepancies WHERE run_id = :r AND case_id = :c "
                                                      "ORDER BY opened_at, discrepancy_id", **keys)]
    recorded = {(m["action_id"], m["kind"]): m for m in moves}

    def units(action_id: str, kind: str, key: str):
        m = recorded.get((action_id, kind))
        return None if m is None else m[key]
    work_orders = [{**{k: a[k] for k in WORK_ORDER_KEYS},
                    "dispatched_qty": units(a["action_id"], DISPATCH, "qty"),
                    "received_qty": units(a["action_id"], RECEIPT, "qty"),
                    "damaged_qty": units(a["action_id"], RECEIPT, "damaged_qty")} for a in actions]
    transfers = [a for a in actions if a["kind"] == TRANSFER]
    target = next((a for a in transfers if a["state"] == "PLANNED"), transfers[-1] if transfers else None)
    blocks = dispatch_check(conn, run, case, target)
    return {"dispatch": {"allowed": not blocks, "blocks": blocks}, "work_orders": work_orders, "movements": moves,
            "discrepancies": gaps, "after_dispatch": after_dispatch(conn, run, case), "options": settings_section()}


# --- close by the owner (06.1-11; D-34, D-65, D-67) -----------------------------------------------------------------

CLOSABLE = ("EXECUTED", "OBSERVED")  # the plan's work is recorded
COMPLETED = "COMPLETED"  # closed_as of a decision whose plan ran (07-11 adds CANCELLED)
RECEIPT_DISCREPANCY = "Receipt discrepancy—reconciliation required."  # the 409 discrepancy_open message (D-65)
NOT_CLOSER = "Only the owner of this stock decision can close it."  # (proposed)


def _account(actor: dict) -> str | None:
    """The acting account as a case stores it ("HQ" for HQ), or None without one."""
    account = actor.get("account")
    if not account:
        return None
    return accounts.HQ if accounts.is_hq(account) else account


def _is_owner(actor: dict, case: dict) -> bool:
    """The acting account is the case's owner (cases.owner_taken_by), in the owner's own role."""
    owner = ownership.owner_view(case)
    return owner is not None and _account(actor) == owner["account"] and actor.get("role") == owner["role"]


def _reason(reason, what: str) -> str:
    why = reason.strip() if isinstance(reason, str) else ""
    if not why:
        raise ApiError(400, "bad_request", f"reason is required to {what}")
    return why


def _open_discrepancies(conn, run: dict, case_id: str) -> int:
    return db.one(conn, "SELECT count(*) AS n FROM discrepancies WHERE run_id = :r AND case_id = :c AND state = 'OPEN'",
                  r=run["run_id"], c=case_id)["n"]


def close_decision(conn, run: dict, actor: dict, case_id: str, reason: str | None) -> dict:
    """The owner closes a stock decision whose work is recorded (06.1 D-34, D-67), in the caller's transaction under
    the run lock FOR UPDATE. Order: hold expiry; the case locked; the owner (403); EXECUTED or OBSERVED (409
    wrong_state); a reason (400); no OPEN receipt discrepancy of the case (409 discrepancy_open, D-65). Any refusal
    writes nothing.

    Then what the plan still occupies ends DONE (bookings.done, as the observed path ends it: the plan ran); the case
    is CLOSED with closed_as COMPLETED, closed_at now(), closed_by the owner's account and the reason; `closed` is
    logged with the actor."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    if not _is_owner(actor, case):
        raise ApiError(403, "forbidden", NOT_CLOSER)
    if case["state"] not in CLOSABLE:
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    why = _reason(reason, "close a stock decision")
    if _open_discrepancies(conn, run, case_id):
        raise ApiError(409, "discrepancy_open", RECEIPT_DISCREPANCY)
    bookings.done(conn, run["run_id"], case_id)
    db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = :a, closed_at = now(), closed_by = :by, reason = :why "
                 "WHERE run_id = :r AND case_id = :c", a=COMPLETED, by=_account(actor), why=why, r=run["run_id"],
           c=case_id)
    db.log(conn, run["run_id"], run["day"], "closed", case_id, actor=accounts.logged_actor(actor),
           closed_as=COMPLETED, reason=why, from_state=case["state"], lot_id=case["lot_id"])
    return {"state": "CLOSED", "closed_as": COMPLETED}


# --- reconcile a receipt discrepancy (06.1-11; D-65; UI-SPEC open question 2, recommended default) ------------------

RECONCILE = "RECONCILE"  # movement kind: one part of a discrepancy settled, with its resolution and units
RESOLUTIONS = "resolutions"  # settings execution.resolutions: the keys a reconciliation may take
# The stock adjustment a resolution makes (04-API-CONTRACT Execution): the units go back onto the moved lot at the
# destination, or back onto the source lot. Any other resolution key settings lists (lost_or_written_off included)
# changes no stock: the units are not in any store's stock.
ADJUSTS = {"found_at_destination": "destination", "source_count_error": "source"}
NOT_RECONCILER = "Only the owner of this stock decision or HQ can reconcile a discrepancy."  # (proposed)
LOT_NOT_ON_RECORD = "Lot {lot} is no longer on the record. Choose another resolution."  # (proposed)


def _is_hq(actor: dict) -> bool:
    return actor.get("role") == accounts.HQ_ROLE and _account(actor) == accounts.HQ


def _adjust(conn, run: dict, action: dict, target: str, units: int) -> str:
    """Add `units` to the lot `target` names (the moved lot at the destination, or the source lot), on the record and
    physically, after the workspace owns its series (05.2 D-09); the lot id. 409 wrong_state when the lot is gone."""
    branch, lot_id = (action["dest"], action["new_lot_id"]) if target == "destination" else \
        (action["origin"], action["lot_id"])
    overlay.materialize(conn, run, WORLD, branch, action["sku"])
    row = overlay.lot(conn, run, WORLD, lot_id)
    if row is None or row["run_id"] != run["run_id"]:
        raise ApiError(409, "wrong_state", LOT_NOT_ON_RECORD.format(lot=lot_id))
    keys = {"r": run["run_id"], "w": WORLD, "l": lot_id, "q": float(units)}
    db.run(conn, "UPDATE lots SET qty = qty + :q WHERE run_id = :r AND world = :w AND lot_id = :l", **keys)
    db.run(conn, "UPDATE truth_lots SET qty = qty + :q WHERE run_id = :r AND world = :w AND lot_id = :l", **keys)
    return lot_id


def reconcile(conn, run: dict, actor: dict, case_id: str, discrepancy_id: str, resolution: str, units: int,
              reason: str | None) -> dict:
    """The owner or HQ settles some or all of a receipt discrepancy (06.1 D-65; UI-SPEC open question 2), in the
    caller's transaction under the run lock FOR UPDATE. Order: hold expiry; the case locked; the owner or HQ (403);
    the discrepancy locked (404 when the case has none by that id); OPEN (409 wrong_state); the fields (400 naming
    the field: a resolution key of settings execution.resolutions, whole units 1..open units, a reason).

    Then the resolution's stock adjustment (ADJUSTS; the series materialized first); a RECONCILE movement
    `{discrepancy_id}:{n}` (n counts this discrepancy's parts) carrying the resolution key as its `condition`, the
    units, the adjusted lot (null when no stock changes), the reason and the actor; open units go down by the units,
    and at 0 the discrepancy is RECONCILED with this resolution, resolved_by, resolved_at and the reason;
    `discrepancy_reconciled` is logged with the actor. The dispatch and receipt movements are never rewritten."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    if not (_is_owner(actor, case) or _is_hq(actor)):
        raise ApiError(403, "forbidden", NOT_RECONCILER)
    gap = db.one(conn, "SELECT * FROM discrepancies WHERE run_id = :r AND discrepancy_id = :d FOR UPDATE",
                 r=run["run_id"], d=discrepancy_id)
    if gap is None or gap["case_id"] != case_id:
        raise ApiError(404, "not_found", f"This case has no discrepancy {discrepancy_id}")
    if gap["state"] != "OPEN":
        raise ApiError(409, "wrong_state", f"Discrepancy is {gap['state']}", state=gap["state"])
    keys = [r["key"] for r in settings_section()[RESOLUTIONS]]
    if resolution not in keys:
        raise ApiError(400, "bad_request", f"resolution must be one of {', '.join(keys)}")
    left = float(gap["open_units"])
    if units < 1 or units > left:
        raise ApiError(400, "bad_request", f"units must be a whole number from 1 to {left:g}")
    why = _reason(reason, "reconcile a discrepancy")
    action = _work_order(conn, run, case, gap["action_id"])
    target = ADJUSTS.get(resolution)
    lot_id = _adjust(conn, run, action, target, units) if target else None
    n = db.one(conn, "SELECT count(*) AS n FROM movements WHERE run_id = :r AND kind = :k "
                     "AND starts_with(movement_id, :p)", r=run["run_id"], k=RECONCILE, p=f"{discrepancy_id}:")["n"]
    movement = _movement(conn, run, actor, f"{discrepancy_id}:{n + 1}", case_id, gap["action_id"], RECONCILE, lot_id,
                         units, 0, resolution, why)
    rest = left - units
    if rest <= 0:
        row = db.one(conn, "UPDATE discrepancies SET open_units = 0, state = 'RECONCILED', resolution = :res, "
                           "resolved_by = :by, resolved_at = now(), reason = :why WHERE run_id = :r "
                           "AND discrepancy_id = :d RETURNING *", res=resolution, by=_account(actor), why=why,
                     r=run["run_id"], d=discrepancy_id)
    else:
        row = db.one(conn, "UPDATE discrepancies SET open_units = :o WHERE run_id = :r AND discrepancy_id = :d "
                           "RETURNING *", o=rest, r=run["run_id"], d=discrepancy_id)
    db.log(conn, run["run_id"], run["day"], "discrepancy_reconciled", case_id, actor=accounts.logged_actor(actor),
           discrepancy_id=discrepancy_id, kind=gap["kind"], resolution=resolution, units=units,
           open_units=row["open_units"], state=row["state"], lot_id=lot_id, action_id=gap["action_id"], reason=why)
    return {"state": case["state"], "discrepancy": discrepancy_row(row), "movement": movement}


# --- settings execution.conditions and execution.resolutions (06.1-UI-SPEC open questions 2 and 5) ------------------

def settings_section() -> dict:
    """settings.json `execution` as {"conditions": [{key, label}], "resolutions": [{key, label}]}, checked here (config
    reads every leaf's format; this checks the shape): each a non-empty list of {key, label} with non-empty text and
    unique keys. A malformed one is a ConfigError naming its dotted path."""
    section = config.SETTINGS.get("execution")
    if not isinstance(section, dict):
        raise ConfigError("settings.execution: required section is missing")
    out = {}
    for name in ("conditions", "resolutions"):
        where, items = f"settings.execution.{name}", section.get(name)
        if not isinstance(items, list) or not items:
            raise ConfigError(f"{where}: must be a non-empty list of {{key, label}}")
        for i, item in enumerate(items):
            ok = isinstance(item, dict) and set(item) == {"key", "label"} and all(
                isinstance(item[k], str) and item[k].strip() for k in ("key", "label"))
            if not ok:
                raise ConfigError(f"{where}[{i}]: must be {{key, label}} with non-empty text")
        keys = [item["key"] for item in items]
        if len(set(keys)) != len(keys):
            raise ConfigError(f"{where}: keys must be unique")
        out[name] = [dict(item) for item in items]
    return out
