"""Where a stock decision's stock is going, and what people recorded about it: the case's transport details panel and
the Branch transfers list (06.1-CONTEXT D-17, D-30, D-33, D-52, D-55; 04-API-CONTRACT "Product mode (06.1)", Read
views).

Read-only; times are planned values from the data or recorded times, never invented. Nothing here writes a row
(Dispatch and Receipt stay in Execute, api/execution.py) and nothing calls the engine worker (OPS-06). Expired holds
are applied first, as every read that touches a case does (spec §8 Hold expiry).

- **The run.** A case's Scheduled truck run is its TRANSFER work order's run (the current round's, written by Prepare),
  else, before Prepare, the run the active version's recommended transfer names, read from that version's stored
  engine input. A Keep or markdown case has no run (`run` null: the screen says "No transport assigned."). The
  timetable (loading cutoff, departure, arrival, cold chain, spare totes) is the reference run's; the quote is the
  work order's fixed cost plus its per-unit cost times the committed quantity (the costs as quoted at Prepare), or,
  before Prepare, the stored run's costs times the planned quantity.
- **Milestones** (D-30): pack from (the loading cutoff less the workspace policy's `packing_min`), ready (nothing
  records it: null), loading cutoff, departure, arrival, received (the receipt's scenario day and recorded time, else
  null). A planned time is `{day, min}` with `min` inside the day (an arrival past midnight names the next day);
  nothing is ever "missed" or overdue (D-18).
- **Decision and movement stay apart** (D-33). `movement_state` is derived from recorded rows only: the work order's
  state, its `movements` (DISPATCH, RECEIPT) and its `discrepancies`. A work order the demo's Next day carried out
  has no recorded movement, so its movement state is null (not recorded), never "received".
- **Scope** (D-17): the transfers list is the signed-in store's (a manager's `branch` parameter is ignored); HQ sees
  every store or names one. Every query binds the workspace run; nothing reads a reference series.
- `assumed` names the values the data model lacks (carrier, coordinator); they are labelled assumed, never guessed.
"""
from __future__ import annotations

from engine.model import TRANSFER

from . import bookings, cases, db, inputs, refdata, stepkeys
from .errors import ApiError

WORLD = "shelfshift"
DAY_MIN = 1440
ASSUMED = {"carrier": "own_fleet", "coordinator": "represented"}  # the contract's assumed values (D-30)
MILESTONES = ("pack_from", "ready", "loading_cutoff", "departure", "arrival", "received")
MOVEMENT_STATES = ("planned", "dispatched", "partially_received", "received", "discrepancy", "expired", "cancelled")
DIRECTIONS = ("outgoing", "incoming", "all")
DISPATCH, RECEIPT = "DISPATCH", "RECEIPT"  # movements.kind (04-API-CONTRACT Execution)
OPEN = "OPEN"  # discrepancies.state while units are unreconciled


def _num(value) -> float | None:
    return None if value is None else float(value)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def movement_state(action: dict | None, movements: list[dict], discrepancies: list[dict]) -> str | None:
    """The movement state of one work order from what was recorded (D-33): cancelled or expired as the work order
    says; discrepancy while any of its discrepancies is OPEN; received (or partially_received, fewer units received
    than dispatched) once a receipt is recorded; dispatched once a dispatch is recorded; planned while it waits.
    None without a work order, or for one carried out without a recorded movement (the demo's Next day)."""
    if action is None:
        return None
    state = action["state"]
    if state == "CANCELLED":
        return "cancelled"
    if state == "EXPIRED":
        return "expired"
    if any(d["state"] == OPEN for d in discrepancies):
        return "discrepancy"
    sent = sum(float(m["qty"]) for m in movements if m["kind"] == DISPATCH)
    receipts = [m for m in movements if m["kind"] == RECEIPT]
    if receipts or state == "RECEIVED":
        return "received" if sum(float(m["qty"]) for m in receipts) >= sent else "partially_received"
    if sent or state == "DISPATCHED":
        return "dispatched"
    return "planned" if state == "PLANNED" else None


def _records(conn, run: dict, action_ids: list[str]) -> tuple[dict, dict]:
    """({action_id: movements by recorded time}, {action_id: discrepancies by opening time}) for these work orders."""
    moves, gaps = {a: [] for a in action_ids}, {a: [] for a in action_ids}
    if not action_ids:
        return moves, gaps
    for m in db.rows(conn, "SELECT * FROM movements WHERE run_id = :r AND action_id = ANY(:a) "
                           "ORDER BY recorded_at, movement_id", r=run["run_id"], a=action_ids):
        moves[m["action_id"]].append(m)
    for d in db.rows(conn, "SELECT * FROM discrepancies WHERE run_id = :r AND action_id = ANY(:a) "
                           "ORDER BY opened_at, discrepancy_id", r=run["run_id"], a=action_ids):
        gaps[d["action_id"]].append(d)
    return moves, gaps


def _quantities(movements: list[dict]) -> dict:
    """Recorded quantities and times; null where nothing was recorded (never the committed quantity in their place)."""
    sent = [m for m in movements if m["kind"] == DISPATCH]
    got = [m for m in movements if m["kind"] == RECEIPT]
    return {"dispatched_qty": sum(float(m["qty"]) for m in sent) if sent else None,
            "received_qty": sum(float(m["qty"]) for m in got) if got else None,
            "damaged_qty": sum(float(m["damaged_qty"]) for m in got) if got else None,
            "dispatched_at": _iso(sent[-1]["recorded_at"]) if sent else None,
            "received_at": _iso(got[-1]["recorded_at"]) if got else None}


class _Timetables:
    """Reference runs looked up by (origin, day), each pair read once per request (both dataset kinds answer
    `runs_from`)."""

    def __init__(self, ref: refdata.Ref):
        self.ref, self.seen = ref, {}

    def find(self, origin: str, code: str | None, day: int | None, dest: str | None):
        if code is None or day is None:
            return None
        if (origin, day) not in self.seen:
            self.seen[(origin, day)] = self.ref.runs_from(origin, day)
        return next((r for r in self.seen[(origin, day)] if r.run_code == code and (dest is None or r.dest == dest)),
                    None)


def _work_order(conn, run: dict, case: dict) -> dict | None:
    """The case's TRANSFER work order of its current round (Prepare's action id), else None."""
    return db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = :w AND case_id = :c AND kind = :k "
                        "AND action_id = :a", r=run["run_id"], w=WORLD, c=case["case_id"], k=TRANSFER,
                  a=f"{case['case_id']}#{case['round']}:transfer")


def _at(day: int, minute: int) -> dict:
    t = day * DAY_MIN + minute
    return {"day": t // DAY_MIN, "min": t % DAY_MIN}


def _milestones(trip: dict, packing_min: int, receipts: list[dict]) -> list[dict]:
    day, none = trip["day"], {"day": None, "min": None}

    def planned(minute):
        return none if minute is None or day is None else _at(day, minute)
    cutoff = trip["loading_cutoff_min"]
    last = receipts[-1] if receipts else None
    times = {"pack_from": planned(None if cutoff is None else cutoff - packing_min), "ready": none,
             "loading_cutoff": planned(cutoff), "departure": planned(trip["departs_min"]),
             "arrival": planned(trip["arrives_min"]),
             "received": {"day": last["scenario_day"], "min": None} if last else none}
    return [{"key": k, **times[k], "recorded_at": _iso(last["recorded_at"]) if k == "received" and last else None}
            for k in MILESTONES]


def _trip(code: str, day: int, timetable, fixed, per_unit, qty) -> dict:
    """The run object: the reference timetable (null fields when the reference lacks the run) and the quote."""
    quote = None if fixed is None or per_unit is None or qty is None else round(float(fixed) + float(per_unit) * qty, 2)
    return {"code": code, "day": day,
            "loading_cutoff_min": timetable.loading_cutoff_min if timetable else None,
            "departs_min": timetable.departs_min if timetable else None,
            "arrives_min": timetable.arrives_min if timetable else None,
            "cold_chain": timetable.cold_chain if timetable else None,
            "spare_totes": timetable.spare_totes if timetable else None,
            "quote": quote}


def step_of(conn, run: dict, case: dict) -> str:
    """The case's step as the board shows it, through api/stepkeys.py (the one place a state becomes a step), fed from
    the case's stock checks and its OPEN receipt discrepancies."""
    requests = db.rows(conn, "SELECT case_id, state, rec_version FROM evidence_requests WHERE run_id = :r "
                             "AND case_id = :c", r=run["run_id"], c=case["case_id"])
    gap = db.one(conn, "SELECT 1 AS x FROM discrepancies WHERE run_id = :r AND case_id = :c AND state = :s LIMIT 1",
                 r=run["run_id"], c=case["case_id"], s=OPEN)
    return stepkeys.step_of(case, stepkeys.has_open_check(case, requests), gap is not None)


def transport_view(conn, run: dict, case_id: str) -> dict:
    """GET /api/cases/{id}/transport (04-API-CONTRACT Read views; D-30, D-33, D-55)."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id)
    ref = refdata.get(run["dataset_version"])
    packing = inputs.sandbox_policy(conn, run, ref).packing_min
    times = _Timetables(ref)
    action = _work_order(conn, run, case)
    source, dest, committed, trip = case["source_branch"], None, None, None
    if action is not None:
        source, dest, committed = action["origin"], action["dest"], _num(action["qty"])
        trip = _trip(action["run_code"], action["run_day"],
                     times.find(source, action["run_code"], action["run_day"], dest),
                     action["fixed_cost"], action["per_unit_cost"], committed)
    else:
        rec = cases.active_rec(conn, case)
        plan = cases.parse_plan(rec["result"]["recommended"]) if rec else None
        if plan is not None and plan.kind == TRANSFER:
            stored = next((r for r in rec["engine_input"]["runs"]
                           if r["run_code"] == plan.run_code and r["dest"] == plan.dest), None)
            if stored is not None:
                dest = plan.dest
                committed = float(plan.qty) if case["state"] == "APPROVED" else None  # approved = committed
                trip = _trip(plan.run_code, stored["day"], times.find(source, plan.run_code, stored["day"], dest),
                             stored["fixed_cost"], stored["per_unit_cost"], float(plan.qty))
    ids = [action["action_id"]] if action is not None else []
    moves, gaps = _records(conn, run, ids)
    movements, discrepancies = moves.get(ids[0], []) if ids else [], gaps.get(ids[0], []) if ids else []
    return {
        "run": trip, "source": source, "destination": dest, "packing_min": packing,
        "milestones": _milestones(trip, packing, [m for m in movements if m["kind"] == RECEIPT]) if trip else [],
        "decision": {"state": case["state"], "step": step_of(conn, run, case)},
        "movement": {"state": movement_state(action, movements, discrepancies), "committed_qty": committed,
                     **_quantities(movements)},
        "assumed": dict(ASSUMED),
    }


# --- the Branch transfers list (D-17, D-33, D-52) --------------------------------------------------------------------

def _store(ref: refdata.Ref, actor: dict | None, branch: str | None) -> str | None:
    """The store the list is bound to: a manager's own (any `branch` parameter is ignored, T-06.1-41); HQ's named
    store (400 for one the reference data lacks) or None for every store."""
    if actor is None or actor["role"] != "hq":
        return actor["branch"] if actor is not None else None
    if branch is None:
        return None
    if branch not in ref.branches:
        raise ApiError(400, "bad_request", f"No store {branch}")
    return branch


def _page_size(ref: refdata.Ref, page_size: int | None, total: int) -> int:
    """The reference list page size (`board.page_size`, 05.2 D-26) is the default and the largest page (400 above
    it). A set without one (the synthetic set) answers one page by default, as the board does there."""
    largest = (ref.settings.get("board") or {}).get("page_size")
    if largest is None:
        return page_size or max(total, 1)
    size = int(largest) if page_size is None else page_size
    if not 1 <= size <= int(largest):
        raise ApiError(400, "bad_request", f"page_size runs from 1 to {int(largest)}")
    return size


def _who(role: str, branch: str | None) -> list[dict]:
    return [{"role": role, "branch": branch}]


def _next_action(item: dict, owner: str | None) -> dict:
    """What the list offers next (each is done inside the stock decision's Execute step, never on this page): the
    source records the dispatch of a planned transfer, the destination its receipt once dispatched, the owner or HQ
    reconciles an open discrepancy; anything else is view only."""
    state = item["movement_state"]
    if state == "planned":
        return {"key": "record_dispatch", "who": _who("manager", item["source"])}
    if state == "dispatched":
        return {"key": "record_receipt", "who": _who("manager", item["dest"])}
    if state == "discrepancy":
        who = [] if owner in (None, stepkeys.HQ) else _who("manager", owner)
        return {"key": "reconcile", "who": who + _who("hq", None)}
    return {"key": "view", "who": []}


def transfers(conn, run: dict, actor: dict | None, direction: str = "outgoing", branch: str | None = None,
              state: str | None = None, page: int = 1, page_size: int | None = None) -> dict:
    """GET /api/transfers: the workspace's TRANSFER work orders of stock decisions, bound to one store (outgoing: it is
    the source; incoming: the destination; all: either) or, for HQ without a store, every store; filtered by
    movement state on the server and paged in one stable order (run day, departure minute, action id). Each row keeps
    the decision state and the movement state apart (D-33). Expired holds are applied first."""
    if direction not in DIRECTIONS:
        raise ApiError(400, "bad_request", f"direction is one of {', '.join(DIRECTIONS)}")
    if state is not None and state not in MOVEMENT_STATES:
        raise ApiError(400, "bad_request", f"state is one of {', '.join(MOVEMENT_STATES)}")
    ref = refdata.get(run["dataset_version"])
    store = _store(ref, actor, branch)
    if store is None:
        direction = "all"  # every store: both ends are in scope
    bookings.expire_holds(conn, run)
    where, p = "a.run_id = :r AND a.world = :w AND a.kind = :k", {"r": run["run_id"], "w": WORLD, "k": TRANSFER}
    if store is not None:
        where += {"outgoing": " AND a.origin = :b", "incoming": " AND a.dest = :b",
                  "all": " AND (a.origin = :b OR a.dest = :b)"}[direction]
        p["b"] = store
    rows = db.rows(conn, "SELECT a.*, c.key, c.state AS decision_state, c.owner_taken_by FROM world_actions a "
                         f"JOIN cases c ON c.run_id = a.run_id AND c.case_id = a.case_id WHERE {where} "
                         "ORDER BY a.action_id", **p)
    moves, gaps = _records(conn, run, [a["action_id"] for a in rows])
    times = _Timetables(ref)
    items = []
    for a in rows:
        mine = moves[a["action_id"]]
        trip = times.find(a["origin"], a["run_code"], a["run_day"], a["dest"])
        q = _quantities(mine)
        item = {"action_id": a["action_id"], "case_id": a["case_id"], "key": a["key"], "sku": a["sku"],
                "lot_id": a["lot_id"], "new_lot_id": a["new_lot_id"], "source": a["origin"], "dest": a["dest"],
                "committed_qty": _num(a["qty"]), "dispatched_qty": q["dispatched_qty"],
                "received_qty": q["received_qty"], "damaged_qty": q["damaged_qty"], "run_code": a["run_code"],
                "run_day": a["run_day"], "departs_min": trip.departs_min if trip else None,
                "arrives_min": trip.arrives_min if trip else None, "decision_state": a["decision_state"],
                "movement_state": movement_state(a, mine, gaps[a["action_id"]])}
        if state is None or item["movement_state"] == state:
            items.append({**item, "next_action": _next_action(item, a["owner_taken_by"])})
    items.sort(key=lambda i: (i["run_day"] is None, i["run_day"] or 0, i["departs_min"] is None,
                              i["departs_min"] or 0, i["action_id"]))
    size = _page_size(ref, page_size, len(items))
    first = (page - 1) * size
    return {"items": items[first:first + size], "page": page, "page_size": size, "total": len(items),
            "scope": {"branch": store, "direction": direction}}
