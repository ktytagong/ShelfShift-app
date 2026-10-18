"""Closing a plan: Cancel approved plan (07-CONTEXT D-23, D-25; research F-17, F-18; 04-API-CONTRACT "Cancel approved
plan; cancelled is stored CLOSED").

**Cancel approved plan** (`cancel`): a person authorised to approve the plan in force (its source manager, or the
destination manager of a transfer; the seat from the session row, else 403) stops APPROVED or PREPARED work with a
required reason, only while none of its current round's work orders has left: once the 06.1 Execute step records a
dispatch (a work order DISPATCHED, or RECEIVED after it) cancel is 409 already_dispatched and is no longer offered
(`cancellable`), since releasing would drop the pending delivery of stock already on its way (07 review A; D-23
"work that hasn't dispatched", D-32).

- **Stored as D-25 decided:** state `CLOSED` with `closed_as` `CANCELLED`, `closed_at` now(), `closed_by` (the
  account) and `cancel_reason`; `reason` reads "Cancelled: <reason>". No new database state and no index change: the
  unchanged `one_open_case_per_lot` (`WHERE state NOT IN ('CLOSED', 'REJECTED')`) frees the lot for a successor, and
  two open cases for one lot stay impossible.
- **History is kept** (D-23, research F-18): the case's PLANNED work orders become `CANCELLED` first, so the release
  that follows (which deletes PLANNED rows) finds none and they stay; the world step reads PLANNED rows only, so a
  cancelled work order never executes. The current round's decisions stay, marked `cancelled_at`. The `cancelled`
  log entry records the reason, who, the state it left and the plan.
- **Released:** every reservation the current look holds or booked and its pending delivery (`bookings.release`).
  Prepare then refuses (409 wrong_state: the case is no longer APPROVED).

**Closing never hides at-risk stock** (D-24; the D-28 rule in 04-API-CONTRACT "Decisions recorded here"). Every close
of a plan by a person (`workflow.reject`, the reject or decline decision, `cancel`) calls `on_close` after its release,
which stores on the case:

- `fingerprint` (`fingerprint`): sha256 over a canonical JSON of exactly the date-invariant decision inputs that can
  affect this lot (D-38.1, `_reach`): the lot's expiry day and expiry source; a confirmed count's marker (verified_by
  COUNT with last_verified_day, else null); the ids of the lots moved from it; the not-replaced confirmed facts for
  the lot's SKU (or none named) and its storage zone (or none named) at the branches the closed version covered (or
  naming no branch), a fact naming runs only when one of them leaves the lot's source on the fact's days, each with
  its kind and valid range; the sandbox Policy as it reads (the seeded Policy with its run_policy rows applied, so a
  write that changes no value changes nothing); other cases' bookings by their full state (every one not RELEASED or
  EXPIRED: HELD and unexpired, COMMITTED, CONVERTED, DONE; so an approval or an executed transfer is no change, a
  release or an expired hold is; 07-12 review R3) that can affect the lot (`_touches`: DEMAND for its own SKU and
  STORAGE in its own zone, from any product, at those branches; TRUCK on a run leaving its source, from any product;
  SOURCE_LOT and LOT_PRICE on the lot), summed per kind and bucket; and the dataset version. Bread taking ambient
  space never touches chilled milk; another chilled product's storage there does, and so does any product's truck
  space on the milk's runs. Today, days left, the record's quantity (the daily sales), the forecasts and today's runs
  are left out: each moves with every date increment, and a date increment alone never reopens a lot (D-28). The
  shelf-life thresholds and the available actions count through 07-12's separate considered record.
- `lot_status` (`lot_status`): AT_RISK while the lot is on the record with stock and the ESL-02 screen
  (`screen.screen`, no search) still qualifies it, else null.

`label` turns that into the card's `lot_status` on a REJECTED or CLOSED-as-CANCELLED case: "Awaiting verification"
while an ACTIVE hold on a lot the case involves or a Required check of the closed plan in force remains, else "Needs
decision"; null once the lot has no stock on the record (or the close found it not at risk). It reads the record only,
never physical stock (FLOW-08: a route never reads truth), through the workspace's overlay.

**When a closed lot comes back** (07-12; D-24, and D-28 as the user refined it on 2026-10-17: "only a change that
creates a materially different decision counts"; 04-API-CONTRACT "Decisions recorded here", D-28):

- **Reassess** (`reassess`): the owner asks; one successor opens whatever the fingerprint says, while the lot is on
  the record with stock, still qualifies and has no active case.
- **Automatic** (`reassess_lots`, called by a confirmed fact in scope, a settings write and a release or an expired
  hold of bookings that can affect the lot, `recheck_neighbours`): a successor opens only when the fingerprint now
  differs from the stored one (`inputs`: a material change in stock, demand or transport, a booking or a policy), the
  lot qualifies and no case is active. Unchanged inputs never create a case: a relevant change triggers a recheck,
  never a reopen by itself (D-38.1).
- **The synthetic Next-day morning** (`cases.screen_candidates`, the only caller with `next_day`): also a reason not
  already considered (`crossed` against `cases.considered`): `clearance`, the product's scheduled markdown reaches
  the configured clearance threshold (`config.CLEARANCE_PCT`, settings `reassess.clearance_pct`); `new_option`, an
  available action not in the record (the plans `engine.limits.candidates` passes on today's effective input, grouped
  `md20`, `md30`, `tr:<destination>`), so a previously infeasible option became viable; `transfer_window`, the lot's
  last transfer window closed (no run of the seeded timetable from its branch, today or later, passes the run-level
  limits). Never on its own: a routine markdown step (in this engine a step only removes markdown options), one less
  day of shelf life, the date advancing, or a more urgent band (`config.URGENCY_BANDS`, settings
  `reassess.urgency_bands`): the band is recorded and logged beside the reasons and reopens a lot only together with
  an action change. Escalation is not modelled (`next_owner` names only the source and destination managers).
- **What was considered** (`state`, `crossed`): every close stores the state at the close; each Next-day morning the
  lot qualifies with no active case merges that morning's state in (the most urgent band, the deepest step,
  clearance once reached stays reached, a lost window stays lost, actions the union), so one crossing opens at most
  one successor; a crossing on a morning the lot does not qualify stays unconsidered. A close while the data to judge
  the lot is missing, or of a case without a version or with the missing-data flag even when the state reads
  (`closed_for_missing_data`), stores the empty record (`empty_state`: no actions, the window closed, no clearance),
  so the first morning with the data reopens the lot through `new_option` if it still has a real option (D-38.3).
- **Thresholds are data**: the two settings leaves (pending retailer), the product's markdown schedule (reference
  data, the input's `markdown_schedule`), the engine's hard limits with the sandbox Policy, and the seeded run
  timetable. The morning check runs no search: one `candidates` call on today's effective input (the hard limits
  only, one Keep simulation per run leaving today, no scoring).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json

from engine.adjust import coerce_input, effective
from engine.limits import candidates
from engine.model import MARKDOWN, TRANSFER, scheduled_pct
from engine.simulate import HorizonError
from engine.timing import first_sellable_day, latest_decision_at

from . import accounts, bookings, cases, config, db, inputs, overlay, realtime, refdata, screen, selection
from .errors import ApiError

CANCELLED = "CANCELLED"  # closed_as of a cancelled approved plan (D-25); the UI label is "Cancelled"
CANCELLABLE = ("APPROVED", "PREPARED")  # approved work, while nothing has left (`moved_orders`; D-23)
MOVED = ("DISPATCHED", "RECEIVED")  # a work order whose stock has left the source (06.1 Execute; api/execution.py)
ALREADY_DISPATCHED = "Stock for this plan has already been dispatched, so it can't be cancelled."  # 07 review A
NOT_CANCELLER = "Only a manager who approves this plan can cancel it"
REASON_REQUIRED = "A reason is required"
AT_RISK = "AT_RISK"  # cases.lot_status as stored: the lot still qualified when the plan was closed
NEEDS_DECISION, AWAITING_VERIFICATION = "Needs decision", "Awaiting verification"  # the card's labels (D-24)
WORLD = "shelfshift"
# a booking the fingerprint counts: every one not RELEASED or EXPIRED, by its full state (07-12 review R3, D-38.1):
# HELD while unexpired, COMMITTED, CONVERTED (an approved transfer's demand) and DONE (work that ran), so an approval
# or an executed transfer is no change, while a release or an expired hold (marked or not) still is
KEPT_BOOKING = "(state IN ('COMMITTED', 'CONVERTED', 'DONE') OR (state = 'HELD' AND expires_at > now()))"
CLOSED_BY_PERSON = "(c.state = 'REJECTED' OR (c.state = 'CLOSED' AND c.closed_as = 'CANCELLED'))"  # alias c


def _closer(actor: dict) -> str:
    """The acting account as `closed_by` stores it."""
    return actor.get("account") or f"manager:{actor.get('branch')}"


def moved_orders(conn, run_id: str, case: dict) -> list[dict]:
    """[{action_id, state}] of the case's current-round work orders whose stock has left (DISPATCHED or RECEIVED), in
    action order: an earlier look's finished work never counts (07 review A)."""
    return db.rows(conn, "SELECT action_id, state FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                         "AND case_id = :c AND state = ANY(:s) "
                         "AND CAST(substring(action_id FROM '#([0-9]+):') AS int) = :n ORDER BY action_id",
                   r=run_id, c=case["case_id"], s=list(MOVED), n=case["round"])


def cancellable(conn, case: dict) -> bool:
    """Cancel approved plan applies: APPROVED or PREPARED, with no current-round work order dispatched or received
    (07 review A; D-23)."""
    return case["state"] in CANCELLABLE and not moved_orders(conn, case["run_id"], case)


def cancel(conn, run: dict, actor: dict, case_id: str, reason) -> dict:
    """Cancel approved plan (D-23, D-25), in the caller's transaction after the run lock. Order: hold expiry; the case
    locked (404 for another workspace's); the seat (the source manager, or the plan in force's destination manager
    for a transfer; else 403); APPROVED or PREPARED (else 409 wrong_state); no work order of the round dispatched or
    received (else 409 already_dispatched with `work_orders`, 07 review A); a reason (blank 400). Any refusal writes
    nothing. Then: the PLANNED work orders become CANCELLED, the release, the round's decisions marked cancelled, the
    case stored CLOSED as CANCELLED with who, when and why, and `cancelled` logged."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    rec = cases.active_rec(conn, case)
    key, _, _ = selection.plan_in_force(case, rec)  # the approved plan is the plan in force (07-10)
    plan = cases.parse_plan(key) if key else None
    seats = {case["source_branch"]} | ({plan.dest} if plan is not None and plan.kind == TRANSFER else set())
    if actor.get("role") != "manager" or actor.get("branch") not in seats:
        raise ApiError(403, "forbidden", NOT_CANCELLER)
    if case["state"] not in CANCELLABLE:
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    moved = moved_orders(conn, run["run_id"], case)
    if moved:  # the stock is on its way: releasing would drop its pending delivery (07 review A; D-23)
        raise ApiError(409, "already_dispatched", ALREADY_DISPATCHED, work_orders=moved)
    why = reason.strip() if isinstance(reason, str) else ""
    if not why:
        raise ApiError(400, "bad_request", REASON_REQUIRED)
    run_id, by = run["run_id"], _closer(actor)
    # the work orders stay in history, marked cancelled, before the release deletes PLANNED rows (research F-18)
    orders = db.run(conn, "UPDATE world_actions SET state = 'CANCELLED' WHERE run_id = :r AND world = 'shelfshift' "
                          "AND case_id = :c AND state = 'PLANNED'", r=run_id, c=case_id)
    bookings.release(conn, run_id, case_id)
    db.run(conn, "UPDATE decisions SET cancelled_at = now() WHERE run_id = :r AND case_id = :c AND rec_version = :v "
                 "AND round = :n AND cancelled_at IS NULL", r=run_id, c=case_id, v=case["active_rec_version"] or "",
           n=case["round"])
    db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = :a, closed_at = now(), closed_by = :by, "
                 "cancel_reason = :why, reason = :text WHERE run_id = :r AND case_id = :c",
           a=CANCELLED, by=by, why=why, text=f"Cancelled: {why}", r=run_id, c=case_id)
    db.log(conn, run_id, run["day"], "cancelled", case_id, reason=why, by=by, from_state=case["state"],
           closed_as=CANCELLED, plan=key, round=case["round"], rec_version=case["active_rec_version"],
           work_orders_cancelled=orders, lot_id=case["lot_id"], actor=accounts.logged_actor(actor))
    on_close(conn, run, case, "cancelled")
    return {"state": "CLOSED", "closed_as": CANCELLED}


# --- the close side of D-24: the fingerprint and the lot's status ---------------------------------------------------

def is_closed_by_person(case: dict) -> bool:
    """A plan a person closed: REJECTED (reject, or the reject or decline decision), or CLOSED as CANCELLED. A case
    closed because its lot is gone (closed_as null) or completed is not one."""
    return case["state"] == "REJECTED" or (case["state"] == "CLOSED" and case.get("closed_as") == CANCELLED)


def branches(conn, case: dict) -> list[str]:
    """The source and the destinations of the closed version's stored input (the source alone without a version)."""
    rec = cases.active_rec(conn, case)
    dests = {r["dest"] for r in rec["engine_input"]["runs"]} if rec else set()
    return sorted({case["source_branch"], *dests})


def _reach(ref, lot_id: str, sku: str, source: str, branches) -> dict:
    """What can affect a lot (D-38.1): its id, its SKU, its storage zone, its source (the runs it could use leave
    there) and the branches its closed version covered."""
    return {"lot_id": lot_id, "sku": sku, "zone": ref.products[sku].zone, "source": source,
            "branches": set(branches)}


def _leaves(ref, source: str, codes, first: int, last: int) -> bool:
    """Whether a run with one of `codes` leaves `source` on a day from `first` to `last` (the reference timetable,
    either set kind)."""
    return any(r.run_code in codes for d in range(first, last + 1) for r in ref.runs_from(source, d))


def _touches(kind: str, bucket: str, reach: dict, ref) -> bool:
    """A booking that can affect the lot `reach` describes (D-38.1): DEMAND for its own SKU and STORAGE in its own zone
    (any product's: cold storage is shared) at one of its branches; TRUCK on a run leaving its source (any product's:
    the runs the lot could use); SOURCE_LOT and LOT_PRICE on the lot."""
    head, _, tail = bucket.partition("|")
    if kind == "DEMAND":
        return head in reach["branches"] and tail == reach["sku"]
    if kind == "STORAGE":
        return head in reach["branches"] and tail == reach["zone"]
    if kind == "TRUCK":  # bucket "run_code|day"
        return tail.lstrip("-").isdigit() and _leaves(ref, reach["source"], {head}, int(tail), int(tail))
    return kind in ("SOURCE_LOT", "LOT_PRICE") and bucket == reach["lot_id"]


def _fact_touches(fact: dict, reach: dict, ref) -> bool:
    """A confirmed fact (already filtered by SKU, zone and branch) that can affect the lot: one naming runs only when
    one of them leaves the lot's source on one of the fact's days (a transport report on a run it could use)."""
    adjustments = inputs.plain(fact["adjustments"]) or ()
    codes = {a["run_code"] for a in adjustments if isinstance(a, dict) and a.get("run_code")}
    return not codes or _leaves(ref, reach["source"], codes, fact["valid_from"], fact["valid_to"])


def fingerprint(conn, run: dict, lot_id: str, branches, case_id: str | None = None) -> str:
    """sha256 of a canonical JSON of the lot's date-invariant decision inputs over `branches` that can affect it (the
    D-28 rule with D-38.1; the module docstring lists them). `case_id` (the closed case) leaves its own bookings out;
    they are released by then anyway. Reads the record through the workspace's overlay; no engine call."""
    run_id, branches = run["run_id"], sorted(set(branches))
    ref = refdata.get(run["dataset_version"])
    row = overlay.lot(conn, run, WORLD, lot_id)
    if row is not None:
        sku, source = row["sku"], row["branch"]
    else:  # a lot gone from the record: its case still names its product and source
        gone = db.one(conn, "SELECT sku, source_branch FROM cases WHERE run_id = :r AND lot_id = :l LIMIT 1", r=run_id,
                      l=lot_id)
        sku, source = gone["sku"], gone["source_branch"]
    reach = _reach(ref, lot_id, sku, source, branches)
    read, p = overlay.everywhere(conn, run, "lots")
    moved = sorted(r["lot_id"] for r in db.rows(
        conn, f"SELECT lots.lot_id FROM lots WHERE {read} AND lots.world = :w AND lots.sku = :s "
              "AND lots.moved_from = :l", w=WORLD, s=sku, l=lot_id, **p))
    facts = [[f["fact_id"], f["kind"], f["valid_from"], f["valid_to"]] for f in db.rows(
        conn, "SELECT fact_id, kind, valid_from, valid_to, adjustments FROM reported_facts WHERE run_id = :r "
              "AND replaced_by IS NULL AND (sku IS NULL OR sku = :s) AND (zone IS NULL OR zone = :z) "
              "AND (branch IS NULL OR branch = ANY(:b)) ORDER BY fact_id", r=run_id, s=sku, z=reach["zone"], b=branches)
             if _fact_touches(f, reach, ref)]
    # the sandbox Policy as it reads (the seeded one with the run_policy rows applied), numbers as floats, so a
    # settings write that leaves every value as it was changes nothing (07-12, acceptance check 22)
    policy = [[f.name, _num(getattr(pol, f.name))] for pol in [inputs.sandbox_policy(conn, run, ref)]
              for f in dataclasses.fields(pol)]
    held = [[r["kind"], r["bucket"], round(float(r["qty"]), 6)] for r in db.rows(
        conn, f"SELECT kind, bucket, sum(qty) AS qty FROM reservations WHERE run_id = :r AND case_id <> :c "
              f"AND {KEPT_BOOKING} GROUP BY kind, bucket ORDER BY kind, bucket", r=run_id, c=case_id or "")
            if _touches(r["kind"], r["bucket"], reach, ref)]
    counted = row is not None and row.get("verified_by") == "COUNT"
    data = {"lot": lot_id, "expiry_day": row["expiry_day"] if row else None,
            "expiry_source": row["expiry_source"] if row else None,
            "count": ["COUNT", row["last_verified_day"]] if counted else None, "moved": moved,
            "branches": branches, "facts": facts, "policy": policy, "bookings": held,
            "dataset_version": run["dataset_version"]}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _num(value):
    """A policy value as the fingerprint hashes it: a number as a float (0 and 0.0 alike), anything else as it is."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def case_fingerprint(conn, run: dict, case: dict) -> str:
    """`fingerprint` of the case's lot over its closed version's branches, without its own bookings."""
    return fingerprint(conn, run, case["lot_id"], branches(conn, case), case["case_id"])


def _has_stock(conn, run: dict, lot_id: str) -> bool:
    """The lot is on the record (as the workspace reads it) with stock."""
    row = overlay.lot(conn, run, WORLD, lot_id)
    return row is not None and float(row["qty"]) > 0


def lot_status(conn, run: dict, case: dict) -> str | None:
    """AT_RISK when the lot is on the record with stock and the ESL-02 screen still qualifies it today (no search; a
    screen that cannot judge for lack of data counts as at risk, as the morning screen opens such a lot); else None."""
    if not _has_stock(conn, run, case["lot_id"]):
        return None
    try:
        found = screen.screen(inputs.build_input(conn, run, case["lot_id"]), config.CASE_OPENING)
    except HorizonError:
        return AT_RISK  # never a silent drop: the screen opens such a lot with "missing data"
    except LookupError:
        return None
    return AT_RISK if found["qualifies"] else None


def on_close(conn, run: dict, case: dict, how: str) -> dict:
    """After a close's release (`how`: rejected, declined or cancelled): store the lot's fingerprint, its status and
    what is considered now (`state` at the close, a fresh assessment, so a crossing before the close never counts;
    the empty record for a case closed for missing data, D-38.3) on the case (D-24, D-28); then recheck the other
    closed lots the release can affect (`recheck_neighbours`, trigger "release"). Returns them, with `reassessed`
    (the successors opened)."""
    out = {"fingerprint": case_fingerprint(conn, run, case), "lot_status": lot_status(conn, run, case),
           "considered": considered_now(conn, run, case["lot_id"], missing=closed_for_missing_data(case))}
    db.run(conn, "UPDATE cases SET fingerprint = :f, lot_status = :s, considered = CAST(:k AS jsonb) WHERE run_id = :r "
                 "AND case_id = :c", f=out["fingerprint"], s=out["lot_status"], k=db.js(out["considered"]),
           r=run["run_id"], c=case["case_id"])
    out["reassessed"] = recheck_neighbours(conn, run, case, "release")
    return out


def recheck_neighbours(conn, run: dict, case: dict, trigger: str = "release") -> list[str]:
    """After `case`'s bookings were released (a close, or a pending approval a confirmed fact or a selection cancelled;
    D-38.2): recheck every other lot whose latest case a person closed and that one of the case's RELEASED or EXPIRED
    bookings can affect (`_touches` against that lot's SKU, zone, source and its closed version's branches, D-38.1),
    through `reassess_lots` (a successor only when its fingerprint changed and it still qualifies). An expired hold
    counts as a release does (D-38.1): a close that ends a round whose hour ran out finds its holds EXPIRED, marked by
    its own hold expiry or by an earlier read, and releases nothing (fix-pass review U1). Bread's released ambient
    storage never rechecks chilled milk; another chilled product's storage, or any product's truck space on a run the
    milk could use, does. One helper for every such release, on both dataset kinds. Returns the successors' keys."""
    released = db.rows(conn, "SELECT DISTINCT kind, bucket FROM reservations WHERE run_id = :r AND case_id = :c "
                             "AND state IN ('RELEASED', 'EXPIRED') ORDER BY kind, bucket", r=run["run_id"],
                       c=case["case_id"])
    if not released:
        return []
    ref = refdata.get(run["dataset_version"])
    near = []
    for c in db.rows(conn, f"SELECT * FROM cases c WHERE c.run_id = :r AND c.lot_id <> :l AND {CLOSED_BY_PERSON} "
                           f"AND NOT EXISTS (SELECT 1 FROM cases s WHERE s.run_id = c.run_id AND s.predecessor = "
                           f"c.case_id) ORDER BY c.case_id", r=run["run_id"], l=case["lot_id"]):  # each lot's latest
        reach = _reach(ref, c["lot_id"], c["sku"], c["source_branch"], branches(conn, c))
        if any(_touches(b["kind"], b["bucket"], reach, ref) for b in released):
            near.append(c)
    return reassess_lots(conn, run, near, trigger)


def label(conn, run: dict, case: dict) -> str | None:
    """The card's `lot_status` (04-API-CONTRACT Card): on a plan a person closed whose lot was at risk at the close and
    still has stock on the record, "Awaiting verification" while an ACTIVE hold on a lot the case involves or a
    Required check of the closed plan in force remains, else "Needs decision"; None otherwise, and None once a successor
    carries the lot (07-12)."""
    if not is_closed_by_person(case) or case.get("lot_status") != AT_RISK:
        return None
    if successor(conn, case) is not None or not _has_stock(conn, run, case["lot_id"]):
        return None
    from . import commitment  # it reads cases at import
    if commitment.holds_on(conn, run, case):
        return AWAITING_VERIFICATION
    required, _ = commitment.classes(commitment.checks_in_force(case, cases.active_rec(conn, case)))
    return AWAITING_VERIFICATION if required else NEEDS_DECISION


def label_of(case: dict, successor_key: str | None, lot_row: dict | None, holds: list[dict],
             rec: dict | None) -> str | None:
    """`label`'s rule on records already read (the board's set-based cards, api/cases.py `cards`): the key of the case
    that follows this one (`successors`), the case lot's record row as the workspace reads it (overlay.lot_rows,
    `overlay.lot`'s rule; None once gone), the ACTIVE holds on the lots the case involves (commitment.holds_on_many)
    and its active version's row. The same conditions in the same order, so the two agree on every case."""
    if not is_closed_by_person(case) or case.get("lot_status") != AT_RISK:
        return None
    if successor_key is not None or lot_row is None or not float(lot_row["qty"]) > 0:
        return None
    if holds:
        return AWAITING_VERIFICATION
    from . import commitment  # it reads cases at import
    required, _ = commitment.classes(commitment.checks_in_force(case, rec))
    return AWAITING_VERIFICATION if required else NEEDS_DECISION


# --- Reassess and the linked successor (07-CONTEXT D-24; research F-19, Pattern 5) ----------------------------------

NOT_REASSESSOR = "Only the source manager can reassess this lot"
NOT_AT_RISK = "This lot no longer needs a decision"
ACTIVE_SQL = "state NOT IN ('CLOSED', 'REJECTED')"  # the one-open-case index's own condition


def successor(conn, case: dict) -> str | None:
    """The key of the case that follows this one (its `predecessor` names it), or None."""
    row = db.one(conn, "SELECT key FROM cases WHERE run_id = :r AND predecessor = :c ORDER BY opened_day, case_id "
                       "LIMIT 1", r=case["run_id"], c=case["case_id"])
    return row["key"] if row else None


def successors(conn, run_id: str, case_ids) -> dict[str, str | None]:
    """`successor` for many cases in one read (the board's set-based cards): {case_id: the key of the case that
    follows it, the first by opened_day then case_id, or None}; nothing is read when `case_ids` is empty."""
    out: dict[str, str | None] = {c: None for c in case_ids}
    if not out:
        return out
    for row in db.rows(conn, "SELECT DISTINCT ON (predecessor) predecessor, key FROM cases WHERE run_id = :r "
                             "AND predecessor = ANY(:c) ORDER BY predecessor, opened_day, case_id",
                       r=run_id, c=sorted(out)):
        out[row["predecessor"]] = row["key"]
    return out


def active_case(conn, run_id: str, lot_id: str) -> dict | None:
    """The lot's active case (any state but CLOSED and REJECTED; the unique index allows one), or None."""
    return db.one(conn, f"SELECT * FROM cases WHERE run_id = :r AND lot_id = :l AND {ACTIVE_SQL}", r=run_id, l=lot_id)


def latest(conn, run_id: str, lot_id: str) -> dict | None:
    """The lot's latest case: the one no other case follows (a lot's cases form one chain through `predecessor`)."""
    return db.one(conn, "SELECT * FROM cases c WHERE c.run_id = :r AND c.lot_id = :l AND NOT EXISTS (SELECT 1 FROM "
                        "cases s WHERE s.run_id = c.run_id AND s.predecessor = c.case_id) "
                        "ORDER BY c.opened_day DESC, c.case_id DESC LIMIT 1", r=run_id, l=lot_id)


SCREEN_FIGURES = ("a", "b", "usable_runs", "step_day")  # the screen's figures every `opened` entry carries


def open_successor(conn, run: dict, prev: dict, lot_row: dict | None, why=(), state: dict | None = None,
                   trigger: str = "reassess", found: dict | None = None) -> str | None:
    """Open the one successor of the closed case `prev` for its lot (D-24): None while the lot has an active case.
    The successor takes the run's counter key (never SUS-19, F-19), names `prev` as its predecessor, carries the lot's
    tag and is evaluated fresh (`cases.evaluate_case`), so the holds on the lot and every unconfirmed fact carry over
    by being on the lot. `reassessed` is logged on `prev` and `opened` on the successor, each with the trigger, the
    reasons `why` and, when given, the morning state's band, step and actions (D-28: the band is recorded, never a
    reason). `opened` also carries the ESL-02 screen's a, b, usable_runs and step_day, as an ordinary opening logs
    them (cases.open_cases), so the board's `risk` filter reads a successor too (07-12 review R2): `found`, the screen
    the caller already ran, else run here (a lot it cannot judge for lack of data logs them null). Returns the
    successor's key."""
    run_id = run["run_id"]
    if active_case(conn, run_id, prev["lot_id"]) is not None:
        return None
    if found is None:
        found = _screen(inputs.build_input(conn, run, prev["lot_id"]))
    tag = (lot_row or {}).get("tag", prev["tag"])
    key = cases.next_key(conn, run, {"tag": tag}, successor=True)
    db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, tag, state, opened_day, "
                 "predecessor) VALUES (:r, :c, :c, :l, :b, :s, :t, 'OPEN', :d, :p)", r=run_id, c=key,
           l=prev["lot_id"], b=prev["source_branch"], s=prev["sku"], t=tag, d=run["day"], p=prev["case_id"])
    seen = {k: state[k] for k in ("band", "days_left", "step_pct", "actions")} if state else {}
    db.log(conn, run_id, run["day"], "reassessed", prev["case_id"], lot_id=prev["lot_id"], successor=key,
           trigger=trigger, reasons=list(why), **seen)
    db.log(conn, run_id, run["day"], "opened", key, lot_id=prev["lot_id"], predecessor=prev["case_id"],
           trigger=trigger, reasons=list(why), **seen, **{k: found.get(k) for k in SCREEN_FIGURES})
    cases.evaluate_case(conn, run, cases.case_row(conn, run_id, key))
    return key


def reassess(conn, run: dict, actor: dict, case_id: str) -> dict:
    """Reassess (D-24), in the caller's transaction after the run lock: the owner asks for the lot to be decided
    again. Order: hold expiry; the case locked (404); the seat (the case's source manager, else 403); REJECTED or
    CLOSED as CANCELLED (else 409 wrong_state); an active case for the lot already: its key is returned (no second
    case); a later closed case for the lot: 409 wrong_state (reassess the latest); the lot on the record with stock
    that the ESL-02 screen still qualifies (else 409 not_at_risk; the route then stores lot_status null, `forget`).
    Then one successor opens whatever the fingerprint says: the owner asked. Returns {case_id, key}."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    if actor.get("role") != "manager" or actor.get("branch") != case["source_branch"]:
        raise ApiError(403, "forbidden", NOT_REASSESSOR)
    if not is_closed_by_person(case):
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    active = active_case(conn, run["run_id"], case["lot_id"])
    if active is not None:
        return {"case_id": active["case_id"], "key": active["key"]}
    last = latest(conn, run["run_id"], case["lot_id"])
    if last is not None and last["case_id"] != case_id:
        raise ApiError(409, "wrong_state", f"This lot has a later case {last['key']}", state=case["state"])
    if lot_status(conn, run, case) != AT_RISK:
        raise ApiError(409, "not_at_risk", NOT_AT_RISK)
    key = open_successor(conn, run, case, overlay.lot(conn, run, WORLD, case["lot_id"]), trigger="reassess")
    return {"case_id": key, "key": key}


def forget(conn, run: dict, case_id: str) -> bool:
    """After a 409 not_at_risk (which rolls its own transaction back): store lot_status null on the closed case when
    its lot is still not at risk, in a transaction of its own after the run lock. Returns whether it wrote."""
    case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c FOR UPDATE", r=run["run_id"],
                  c=case_id)
    if case is None or not is_closed_by_person(case) or case["lot_status"] is None:
        return False
    if lot_status(conn, run, case) is not None:
        return False
    db.run(conn, "UPDATE cases SET lot_status = NULL WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=case_id)
    return True


# --- D-28: the morning state, the available actions, the crossing rule and the reopen check -------------------------

def actions(eff) -> list[str]:
    """The sorted available actions on an effective input: for each plan `engine.limits.candidates` passes, a
    markdown's `Plan.key` (`md20`, `md30`) or "tr:" + the destination for a transfer (any run, quantity or
    remainder); Keep left out. The hard limits only (one Keep simulation per run leaving today): no scoring, no
    search, no database."""
    plans, _ = candidates(eff)
    return sorted({p.key if p.kind == MARKDOWN else f"tr:{p.dest}" for p in plans if p.kind in (MARKDOWN, TRANSFER)})


def _band(days_left: int) -> int:
    """How many urgency band edges (`config.URGENCY_BANDS`, settings reassess.urgency_bands) are at or above the
    days left: 0 is the least urgent band."""
    return sum(1 for edge in config.URGENCY_BANDS if edge >= days_left)


def _window_open(ref, eff) -> bool:
    """Whether some run of the seeded timetable leaving the case lot's branch today or later (up to its expiry day)
    still passes the run-level limits `engine.limits.transfer_caps` names: available; cold chain for a chilled
    product; the decision deadline (`latest_decision_at`) not yet past at today's decision minute; enough sellable
    days at the destination (`first_sellable_day` against the Policy's `min_sellable_days`). The quantity caps are left
    out: they move with forecasts and bookings, which `actions` and the fingerprint cover."""
    lot, pol = eff.case_lot(), eff.policy
    now = eff.today * 1440 + eff.decision_min
    for day in range(eff.today, lot.expiry_day + 1):
        for r in ref.runs_from(lot.branch, day):
            if not r.available or (eff.product.zone == "chilled" and not r.cold_chain):
                continue
            if now > latest_decision_at(r, pol):
                continue
            if lot.expiry_day - first_sellable_day(r.day, r.arrives_min, pol) + 1 >= pol.min_sellable_days:
                return True
    return False


def state(conn, run: dict, lot_row: dict, inp=None) -> dict:
    """{day, days_left, band, step_pct, clearance, transfer_open, actions} for the case lot on the run's day, read from
    the effective input (a confirmed printed date and confirmed transport facts count); `inp` is the lot's raw input
    (`inputs.build_input`: the record, never physical stock), built here when not given. `step_pct` is the product's
    scheduled markdown today (`engine.model.scheduled_pct` on the input's `markdown_schedule`, from reference data;
    0 with no schedule); `clearance` is that step at or above `config.CLEARANCE_PCT` (false with no schedule). A
    HorizonError propagates, as it does from `screen.screen`."""
    if inp is None:
        inp = inputs.build_input(conn, run, lot_row["lot_id"])
    eff = effective(coerce_input(inp))
    lot = eff.case_lot()
    days_left = lot.expiry_day - eff.today
    schedule = eff.markdown_schedule
    step = scheduled_pct(schedule, lot.expiry_day, eff.today) if schedule else 0
    return {"day": eff.today, "days_left": days_left, "band": _band(days_left), "step_pct": step,
            "clearance": bool(schedule) and step >= config.CLEARANCE_PCT,
            "transfer_open": _window_open(refdata.get(run["dataset_version"]), eff), "actions": actions(eff)}


def closed_for_missing_data(case: dict) -> bool:
    """The case was decided without the data to judge it: no active recommendation version, or the engine's
    missing-data flag (cases.evaluate_case) (D-38.3)."""
    return not case.get("active_rec_version") or bool(case.get("missing_data"))


def considered_now(conn, run: dict, lot_id: str, missing: bool = False) -> dict | None:
    """`state` of the lot now; None when the lot is not on the record. For a case closed for missing data (`missing`,
    `closed_for_missing_data`), or when the data to judge it is missing now (a HorizonError), it is the empty record
    (`empty_state`), never None, so the first Next-day morning with the data reopens the lot through `new_option` if
    it still has a real option (D-38.3). `missing` wins even when `state` reads: the state can read from the
    destinations' data alone (Keep is simulated there only) while the source's gap left the case without a version,
    and recording those actions would mean the data arriving never reopens the lot (fix-pass review U2)."""
    row = overlay.lot(conn, run, WORLD, lot_id)
    if row is None:
        return None
    if missing:
        return empty_state(run, row)
    try:
        return state(conn, run, row)
    except HorizonError:
        return empty_state(run, row)
    except LookupError:
        return None


def empty_state(run: dict, lot_row: dict) -> dict:
    """What a close considered while the data to judge the lot was missing (D-38.3): the day, the days left and their
    band from the record, and nothing else considered: no step, no clearance, the transfer window closed (so its
    loss is never a reason) and no actions (so every real option is new)."""
    days_left = lot_row["expiry_day"] - run["day"]
    return {"day": run["day"], "days_left": days_left, "band": _band(days_left), "step_pct": 0, "clearance": False,
            "transfer_open": False, "actions": []}


def crossed(considered: dict | None, now: dict) -> tuple[list[str], dict]:
    """(reasons, merged): the D-28 reasons `now` gives that `considered` did not hold, in this order: "clearance"
    (now at clearance, not before), "new_option" (an action not in the record), "transfer_window" (the window was
    open, now lost). Never a reason for a band, a step below the clearance threshold, a dropped action, one less day
    or the date. `merged` is now with the band and step at the larger of the two, clearance either, transfer_open
    both (a lost window stays lost) and actions the sorted union. A case closed before 07-12 (considered None) gets
    its record and no reason. Pure: no database, no clock."""
    if considered is None:
        return [], dict(now)
    was = set(considered.get("actions") or [])
    reasons = []
    if now["clearance"] and not considered.get("clearance"):
        reasons.append("clearance")
    if set(now["actions"]) - was:
        reasons.append("new_option")
    if considered.get("transfer_open") and not now["transfer_open"]:
        reasons.append("transfer_window")
    merged = {**now, "band": max(now["band"], considered.get("band", now["band"])),
              "step_pct": max(now["step_pct"], considered.get("step_pct", now["step_pct"])),
              "clearance": bool(now["clearance"] or considered.get("clearance")),
              "transfer_open": bool(now["transfer_open"] and considered.get("transfer_open", now["transfer_open"])),
              "actions": sorted(set(now["actions"]) | was)}
    return reasons, merged


def _screen(inp) -> dict:
    """The ESL-02 screen (no search); a lot it cannot judge for lack of data counts as qualifying with `missing`, as
    the morning screen opens such a lot."""
    try:
        return screen.screen(inp, config.CASE_OPENING)
    except HorizonError as exc:
        return {"qualifies": True, "a": None, "b": None, "usable_runs": [], "step_day": None, "keep_waste": None,
                "stock_at_step": None, "missing": f"missing data: {exc}"}


def reopen_allowed(conn, run: dict, prev: dict, lot_row: dict | None, next_day: bool = False,
                   seen: dict | None = None) -> list[str]:
    """The reasons the closed case `prev`'s lot may get a successor, empty meaning no (D-24, D-28): "inputs" when the
    fingerprint now differs from the stored one; with `next_day` (the synthetic Next-day morning screen only) also
    `crossed`'s reasons against `prev.considered`. The list is non-empty only when the lot is on the record with
    stock, the screen qualifies it and no case for the lot is active. With `next_day`, when the lot qualifies with no
    active case, the merged record is stored on `prev` whether or not a case opens; on a morning the lot does not
    qualify `considered` is left as it was. A case closed before 07-11 (no stored fingerprint) gets today's as its
    record, never a reason. `seen`, when given, receives `judged`, `status` (AT_RISK or None), `screen` and `state`."""
    seen = {} if seen is None else seen
    seen.update(judged=False, status=None, screen=None, state=None)
    if lot_row is None or float(lot_row["qty"]) <= 0:
        seen["judged"] = True
        return []
    if active_case(conn, run["run_id"], prev["lot_id"]) is not None:
        return []
    inp = inputs.build_input(conn, run, prev["lot_id"])
    found = _screen(inp)
    seen.update(judged=True, screen=found)
    if not found["qualifies"]:
        return []
    seen["status"] = AT_RISK
    reasons = []
    now_fp = case_fingerprint(conn, run, prev)
    if prev.get("fingerprint") is None:
        db.run(conn, "UPDATE cases SET fingerprint = :f WHERE run_id = :r AND case_id = :c", f=now_fp,
               r=run["run_id"], c=prev["case_id"])
    elif now_fp != prev["fingerprint"]:
        reasons.append("inputs")
    if next_day and found.get("missing") is None:
        now = state(conn, run, lot_row, inp)
        crossings, merged = crossed(prev.get("considered"), now)
        reasons += crossings
        seen["state"] = now
        db.run(conn, "UPDATE cases SET considered = CAST(:k AS jsonb) WHERE run_id = :r AND case_id = :c",
               k=db.js(merged), r=run["run_id"], c=prev["case_id"])
    return reasons


def reassess_lots(conn, run: dict, items, trigger: str) -> list[str]:
    """Automatic reassessment (D-24, D-28) of the lots of `items` (case rows or lot ids), in lot order: the lot's
    latest case, when a person closed it (REJECTED, or CLOSED as CANCELLED), gets one successor when
    `reopen_allowed` (no `next_day`: the fingerprint) gives reasons; otherwise its stored lot_status is refreshed
    (AT_RISK, or null once the lot is gone or no longer qualifies), unless a case for the lot is active. Returns the
    successors' keys."""
    opened = []
    for lot_id in sorted({i if isinstance(i, str) else i["lot_id"] for i in items}):
        prev = latest(conn, run["run_id"], lot_id)
        if prev is None or not is_closed_by_person(prev):
            continue
        lot_row = overlay.lot(conn, run, WORLD, lot_id)
        seen: dict = {}
        why = reopen_allowed(conn, run, prev, lot_row, seen=seen)
        if why:
            key = open_successor(conn, run, prev, lot_row, why=why, trigger=trigger, found=seen["screen"])
            if key is not None:
                opened.append(key)
        else:
            keep_status(conn, run, prev, seen)
    return opened


def keep_status(conn, run: dict, prev: dict, seen: dict) -> None:
    """After a reopen check that opened nothing: store the closed case's lot_status as the check judged it (AT_RISK,
    or null once the lot is gone or no longer qualifies) when it differs, so the card's label and Reassess follow it
    (D-24). A check that did not judge the lot (a case for it is active) changes nothing. Shared by `reassess_lots`
    and the morning screen (`cases.screen_candidates`; 07-12 review R1)."""
    if seen.get("judged") and seen.get("status") != prev.get("lot_status"):
        db.run(conn, "UPDATE cases SET lot_status = :s WHERE run_id = :r AND case_id = :c", s=seen["status"],
               r=run["run_id"], c=prev["case_id"])


def predecessor_closed(conn, run: dict, case: dict) -> dict | None:
    """How the plan this case follows was closed, for "Follows {key} · Rejected {t}" (07-UI-SPEC "Successor link";
    07-22): {key, closed_as ("REJECTED" or "CANCELLED"), at (ISO-8601 UTC: the reject's or decline's logged time, or
    the cancel's `closed_at`)}; None without a predecessor, or when it was closed some other way."""
    pred = case.get("predecessor")
    if not pred:
        return None
    row = db.one(conn, "SELECT key, state, closed_as, closed_at FROM cases WHERE run_id = :r AND case_id = :c",
                 r=run["run_id"], c=pred)
    if row is None:
        return None
    if row["state"] == "REJECTED":
        at = db.one(conn, "SELECT max(at) AS at FROM activity_log WHERE run_id = :r AND case_id = :c AND (type = "
                          "'rejected' OR (type = 'decision' AND payload->>'decision' IN ('reject', 'decline')))",
                    r=run["run_id"], c=pred)["at"]
        return {"key": row["key"], "closed_as": "REJECTED", "at": realtime.iso(at)}
    if row["state"] == "CLOSED" and row["closed_as"] == "CANCELLED":
        return {"key": row["key"], "closed_as": "CANCELLED", "at": realtime.iso(row["closed_at"])}
    return None
