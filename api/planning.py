"""Today and the planned schedule: read-only views composed from the workspace's own records (06.1-CONTEXT D-11,
D-17, D-18, D-22, D-29, D-33; 04-API-CONTRACT "Product mode (06.1)", Read views).

Planned times from the data, recorded times from people, nothing invented. A time of day here is one the data plans:
the engine's `decide_by` as stored, a stock check's due time as the request recorded it, a scheduled truck run's
loading cutoff, departure and arrival from the reference timetable, the pick-and-pack window (the cutoff less the
workspace policy's `packing_min`), a supplier delivery's planned arrival and a markdown step's day. Anything else has
no time ("Unscheduled": null). There is no current time, no countdown, no overdue state and no "missed" cutoff: the
views never read a clock (D-18). Statuses come from recorded states only, and an expected outcome is never shown as
actual (D-22, D-33).

The case keys (step, stage, owner, next action, blocked) are the board's own (`cases.card`, through api/stepkeys.py,
the one place a state becomes a step), so Today, the schedule and the board read the same records the same way.

- **Scope** (D-17): a manager reads their own store, whatever `branch` they send (T-06.1-44); HQ names one store, or,
  for Today, reads the network form. Every query binds the workspace run; a reference read is bound to a store.
- **Today** (D-11, D-22): the scenario date, the four summary counts and the action cards for the account. A store's
  counts are attributed so that HQ's network counts are their sum: decisions due on the scenario date and transfers at
  risk to the deciding (source) store, an approval to each side still to decide, a stock check to the store it is
  addressed to.
- Nothing here writes a business row or calls the engine worker (OPS-06); expired holds are applied first, as every
  read that touches a case does (spec §8 Hold expiry).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from engine.model import MARKDOWN, TRANSFER

from . import bookings, cases, db, inputs, movement, overlay, refdata, stepkeys, stock
from .errors import ApiError

DAY_MIN = 1440
COUNTS = ("decisions_due", "approvals_waiting", "stock_checks", "transfers_at_risk")
CHECK_STATES = stepkeys.CHECK_STATES  # a stock check still waiting on its store: OPEN or ANSWERED
CHECK_ACTIONS = {"OPEN": "answer_stock_check", "ANSWERED": "confirm_stock_check"}  # what the addressed store does next
BEFORE_PREPARE = ("OPEN", "WAITING", "INVALIDATED", "PENDING_APPROVAL", "APPROVED")  # no work order yet
GONE_ORDERS = ("CANCELLED", "VOIDED", "EXPIRED")  # a work order whose planned work no longer stands


# --- shared ----------------------------------------------------------------------------------------------------------

def scenario_date(ref: refdata.Ref, run: dict) -> str:
    """The session's scenario date (`data_as_of`, 05.2 D-04); on the synthetic set, the calendar date of the run day."""
    return ref.data_as_of or ref.date(run["day"]).isoformat()


def _at(day: int, minute: int) -> dict:
    """An absolute planned minute as {day, min} with `min` inside the day (a time past midnight names the next day)."""
    t = day * DAY_MIN + minute
    return {"day": t // DAY_MIN, "min": t % DAY_MIN}


def _abs(t: dict) -> int:
    return t["day"] * DAY_MIN + t["min"]


def _store(ref: refdata.Ref, actor: dict, branch: str | None) -> str | None:
    """The store a view reads: a manager's own (any `branch` they send is ignored, T-06.1-44); HQ's named store (400
    for one the reference data lacks), or None for HQ's network form."""
    if actor["role"] != "hq":
        return actor["branch"]
    if branch is None:
        return None
    if branch not in ref.branches:
        raise ApiError(400, "bad_request", f"No store {branch}")
    return branch


def _manager(branch: str) -> dict:
    return {"role": "manager", "branch": branch}


def _store_owner(branch: str) -> dict:
    """A store manager as the owner shape the cards use ({account, role, label})."""
    return stepkeys.owner_view({"owner_taken_by": branch})


def _in_scope(run: dict, store: str | None) -> tuple[str, dict]:
    """(the clause over alias c selecting the stock decisions a view reads, its parameters): every case for HQ's
    network form, else the cases whose source or recommended destination is the store, or whose work order leaves
    from or arrives at it."""
    where, p = "c.run_id = :r", {"r": run["run_id"]}
    if store is not None:
        where += (" AND (c.source_branch = :b OR c.plan_dest = :b OR EXISTS (SELECT 1 FROM world_actions a "
                  "WHERE a.run_id = c.run_id AND a.world = 'shelfshift' AND a.case_id = c.case_id "
                  "AND (a.origin = :b OR a.dest = :b)))")
        p["b"] = store
    return where, p


def _entries(conn, run: dict, store: str | None) -> tuple[list[dict], list[dict]]:
    """(the stock decisions in scope, each {"case", "card"}, in case order; the workspace's stock checks still waiting
    on a store). In scope: every case for HQ's network form, else the cases whose source or recommended destination is
    the store, or whose work order leaves from or arrives at it. Expired holds are applied first."""
    bookings.expire_holds(conn, run)
    where, p = _in_scope(run, store)
    rows = db.rows(conn, f"SELECT c.* FROM cases c WHERE {where} ORDER BY c.case_id", **p)
    requests = db.rows(conn, "SELECT q.*, c.key AS case_key FROM evidence_requests q "
                             "JOIN cases c USING (run_id, case_id) WHERE q.run_id = :r "
                             "ORDER BY q.created_at, q.request_id", r=run["run_id"])
    entries = [{"case": c, "card": k} for c, k in zip(rows, cases.cards(conn, run, rows, requests))]
    waiting = [q for q in requests if q["state"] in CHECK_STATES]
    return entries, waiting


# --- the four counts -------------------------------------------------------------------------------------------------

def _due_on(card: dict, day: int) -> bool:
    return card["due"] is not None and card["due"]["day"] == day


def _transfer_at_risk(card: dict, day: int) -> bool:
    """A stock decision that moves stock and still needs a decision by a deadline on the scenario date, or that the
    server reports blocked (missing data, an expired capacity hold, a blocked dispatch)."""
    return card["dest"] is not None and (_due_on(card, day) or card["blocked"])


def _waits_on(card: dict, branch: str) -> bool:
    """An approval waiting for this store: a side still to decide during PENDING_APPROVAL (either order commits)."""
    return card["state"] == "PENDING_APPROVAL" and any(w.get("branch") == branch for w in card["next_owner"])


def _tiles(card: dict, day: int, branch: str | None) -> list[str]:
    """The summary tiles a case card falls under for this store (HQ's network form: any store), so pressing a tile
    filters the cards under it (D-22)."""
    source = branch is None or card["source"] == branch
    out = []
    if source and _due_on(card, day):
        out.append("decisions_due")
    if card["state"] == "PENDING_APPROVAL" and (branch is None or _waits_on(card, branch)):
        out.append("approvals_waiting")
    if source and _transfer_at_risk(card, day):
        out.append("transfers_at_risk")
    return out


def _counts(entries: list[dict], waiting: list[dict], day: int, branch: str) -> dict:
    """One store's four counts and its first deadline on the scenario date: decisions due and transfers at risk where
    it decides (the source), approvals where it is a side still to decide, stock checks addressed to it."""
    mine = [e["card"] for e in entries if e["card"]["source"] == branch]
    due = sorted((c["due"] for c in mine if _due_on(c, day)), key=_abs)
    return {"decisions_due": len(due),
            "approvals_waiting": sum(1 for e in entries if _waits_on(e["card"], branch)),
            "stock_checks": sum(1 for q in waiting if q["to_branch"] == branch),
            "transfers_at_risk": sum(1 for c in mine if _transfer_at_risk(c, day)),
            "first_deadline": {"day": due[0]["day"], "min": due[0]["min"], "date": due[0]["date"]} if due else None}


# --- action cards (D-11) ---------------------------------------------------------------------------------------------

def _cause(card: dict) -> dict:
    """Why the decision deadline is when it is, as the engine stored it: `decide_by.source` "run" (the scheduled truck
    run's loading cutoff less the packing time; `run_code` names the recommended transfer's run) or "markdown" (the
    markdown schedule's step)."""
    by = card["decide_by"] or {}
    cause = {"source": by.get("source"), "run_code": None}
    if by.get("source") == "run" and card["suggested_key"]:
        plan = cases.parse_plan(card["suggested_key"])
        cause["run_code"] = plan.run_code if plan.kind == TRANSFER else None
    return cause


def _case_card(entry: dict, ref: refdata.Ref, run: dict, branch: str | None) -> dict:
    """A stock decision's action card: what, where, owner, deadline with its cause, status, next action and the
    linked stock decision, read from the board card's own server keys. Its `date` is the deadline's scenario date, or
    the day the decision opened when it has no planned deadline."""
    card = entry["card"]
    deadline = None
    if card["due"] is not None:
        deadline = {"day": card["due"]["day"], "min": card["due"]["min"], "date": card["due"]["date"],
                    "cause": _cause(card)}
    return {
        "key": f"case:{card['case_id']}", "kind": "stock_decision",
        "what": {"sku": card["sku"], "product": card["product"], "lot_id": card["lot_id"],
                 "plan": card["suggested_key"]},
        "where": {"source": card["source"], "dest": card["dest"]},
        "owner": card["owner"], "deadline": deadline,
        "date": deadline["date"] if deadline else ref.date(entry["case"]["opened_day"]).isoformat(),
        "status": {"state": card["state"], "step": card["step"], "stage": card["stage"],
                   "first_stage": card["first_stage"], "blocked": card["blocked"],
                   "blocked_reasons": card["blocked_reasons"]},
        "next_action": card["next_action"], "case_key": card["key"], "case_id": card["case_id"],
        "tiles": _tiles(card, run["day"], branch),
    }


def _check_card(q: dict, ref: refdata.Ref) -> dict:
    """A stock check addressed to the store: answer it while OPEN, confirm the answer once ANSWERED. Its deadline is
    the due time the request recorded (decide-by less the request lead), cause "stock_check_due"."""
    return {
        "key": f"request:{q['request_id']}", "kind": "stock_check",
        "what": {"sku": q["sku"], "product": ref.names.get(q["sku"], q["sku"]), "lot_id": q["lot_id"],
                 "fact_kind": q["fact_kind"]},
        "where": {"source": q["to_branch"], "dest": None},
        "owner": _store_owner(q["to_branch"]),
        "deadline": {"day": q["due_day"], "min": q["due_min"], "date": ref.date(q["due_day"]).isoformat(),
                     "cause": {"source": "stock_check_due", "request_id": q["request_id"]}},
        "date": ref.date(q["due_day"]).isoformat(),
        "status": {"state": q["state"], "step": None, "stage": None, "first_stage": None, "blocked": False,
                   "blocked_reasons": []},
        "next_action": {"key": CHECK_ACTIONS[q["state"]], "who": [_manager(q["to_branch"])]},
        "case_key": q["case_key"], "case_id": q["case_id"], "tiles": ["stock_checks"],
    }


def _names(card: dict, actor: dict, branch: str | None) -> bool:
    """Whether the stock decision waits on this account: its next action names the store's manager (HQ: HQ), or it is
    an approval still waiting for this store."""
    who = card["next_action"]["who"]
    if branch is None:
        return any(w["role"] == "hq" for w in who)
    return _manager(branch) in who or _waits_on(card, branch)


def card_order(card: dict) -> tuple:
    """Planned deadline first (by absolute minute), then the unscheduled ones by date, then key: one stable order."""
    d = card["deadline"]
    return (d is None, _abs(d) if d else 0, card["date"], card["key"])


def _cards(entries: list[dict], waiting: list[dict], ref: refdata.Ref, run: dict, actor: dict,
           branch: str | None) -> list[dict]:
    """The account's action cards, in `card_order`: the stock decisions that wait on it and, for a store, the stock
    checks addressed to it."""
    out = [_case_card(e, ref, run, branch) for e in entries if _names(e["card"], actor, branch)]
    if branch is not None:
        out += [_check_card(q, ref) for q in waiting if q["to_branch"] == branch]
    return sorted(out, key=card_order)


# --- work orders and scheduled truck runs ---------------------------------------------------------------------------

def _actions(conn, run: dict, entries: list[dict]) -> None:
    """Each entry's work orders of its current round (Prepare's ids "{case}#{round}:..."), with the movement state
    derived from what was recorded (api/movement.py, D-33), as entry["actions"]."""
    ids = [e["case"]["case_id"] for e in entries]
    rows = db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = :w AND case_id = ANY(:c) "
                         "ORDER BY action_id", r=run["run_id"], w=movement.WORLD, c=ids) if ids else []
    moves, gaps = movement._records(conn, run, [a["action_id"] for a in rows])
    by_case: dict[str, list[dict]] = {}
    for a in rows:
        by_case.setdefault(a["case_id"], []).append(
            {**a, "movement_state": movement.movement_state(a, moves[a["action_id"]], gaps[a["action_id"]])})
    for e in entries:
        head = f"{e['case']['case_id']}#{e['case']['round']}:"
        e["actions"] = [a for a in by_case.get(e["case"]["case_id"], []) if a["action_id"].startswith(head)]


def _trip(conn, entry: dict, times, packing: int) -> dict | None:
    """Where the decision's stock goes and on which scheduled truck run, with the run's planned times: the current
    round's TRANSFER work order (written by Prepare), else, before Prepare, the run the active version's recommended
    transfer names in its stored engine input. None for a Keep or markdown decision, or one behind us without a work
    order. The times are the reference timetable's (null where the reference lacks the run)."""
    case, card = entry["case"], entry["card"]
    action = next((a for a in entry["actions"] if a["kind"] == TRANSFER), None)
    if action is not None:
        origin, dest, code, day, qty = (action["origin"], action["dest"], action["run_code"], action["run_day"],
                                        movement._num(action["qty"]))
    elif case["state"] in BEFORE_PREPARE and card["suggested_key"]:
        plan = cases.parse_plan(card["suggested_key"])
        rec = cases.active_rec(conn, case) if plan.kind == TRANSFER else None
        stored = next((r for r in (rec or {}).get("engine_input", {}).get("runs", ())
                       if r["run_code"] == plan.run_code and r["dest"] == plan.dest), None)
        if stored is None:
            return None
        origin, dest, code, day, qty = case["source_branch"], plan.dest, plan.run_code, stored["day"], float(plan.qty)
    else:
        return None
    tt = times.find(origin, code, day, dest)
    cutoff = tt.loading_cutoff_min if tt else None
    return {"action": action, "origin": origin, "dest": dest, "run_code": code, "run_day": day, "qty": qty,
            "pack_from": _at(day, cutoff - packing) if tt else None,
            "loading_cutoff": _at(day, cutoff) if tt else None,
            "departure": _at(day, tt.departs_min) if tt else None,
            "arrival": _at(day, tt.arrives_min) if tt else None,
            "cold_chain": tt.cold_chain if tt else None, "spare_totes": tt.spare_totes if tt else None,
            "movement_state": action["movement_state"] if action else None}


def _trips(conn, run: dict, ref: refdata.Ref, entries: list[dict]) -> int:
    """Attach each entry's work orders and trip; returns the workspace policy's packing time (D-30)."""
    packing = inputs.sandbox_policy(conn, run, ref).packing_min
    times = movement._Timetables(ref)
    _actions(conn, run, entries)
    for e in entries:
        e["trip"] = _trip(conn, e, times, packing)
    return packing


def _deliveries(conn, run: dict, ref: refdata.Ref, store: str, first: int, last: int) -> list[dict]:
    """The store's supplier deliveries planned to arrive from day `first` through `last` (the data's planned arrival),
    in arrival order: from the dataset's delivery schedule (a public set's read bound to the store), each "scheduled"
    while the workspace still holds it as a pending delivery and "received" once the day start recorded it as a lot
    (D-21). Incoming branch transfers are work orders, not here."""
    if ref.kind == "public":
        plan = stock._bounded(conn, ref, "SELECT inbound_id, store AS branch, sku, qty, arrival_day, arrival_min "
                                         "FROM ref_inbound0 WHERE dataset_version = :v AND store = :s "
                                         "AND arrival_day BETWEEN :a AND :b", v=ref.dataset_version, s=store,
                              a=first, b=last)
    else:
        plan = [i for i in ref.inbound0(store) if first <= i["arrival_day"] <= last]
    if not plan:
        return []
    read, p = overlay.everywhere(conn, run, "i")
    pending = {r["inbound_id"] for r in db.rows(
        conn, f"SELECT i.inbound_id FROM inbound i WHERE {read} AND i.world = :w AND i.branch = :b "
              f"AND i.inbound_id = ANY(:ids)", w=movement.WORLD, b=store, ids=[i["inbound_id"] for i in plan], **p)}
    out = []
    for i in sorted(plan, key=lambda i: (i["arrival_day"], i["arrival_min"], i["inbound_id"])):
        status = "scheduled" if i["inbound_id"] in pending else ("received" if i["arrival_day"] <= run["day"] else None)
        out.append({"kind": "delivery", "inbound_id": i["inbound_id"], "branch": store, "sku": i["sku"],
                    "qty": float(i["qty"]), "arrival": _at(i["arrival_day"], i["arrival_min"]), "status": status})
    return out


def _decision(entry: dict) -> dict:
    return {"state": entry["card"]["state"], "step": entry["card"]["step"]}


def _moving(entry: dict) -> dict:
    """A transfer on a run, with the decision and the movement kept apart (D-33)."""
    trip = entry["trip"]
    return {"case_key": entry["card"]["key"], "case_id": entry["card"]["case_id"],
            "action_id": trip["action"]["action_id"] if trip["action"] else None, "sku": entry["card"]["sku"],
            "source": trip["origin"], "dest": trip["dest"], "run_code": trip["run_code"], "run_day": trip["run_day"],
            "qty": trip["qty"], "arrival": trip["arrival"], "decision": _decision(entry),
            "movement": {"state": trip["movement_state"]}}


def _live_trip(entry: dict) -> bool:
    """A trip whose work still stands: a decision before Prepare, or a work order that was not cancelled, voided or
    expired."""
    trip = entry.get("trip")
    if trip is None:
        return False
    return trip["action"] is None or trip["action"]["state"] not in GONE_ORDERS


# --- Today's lanes, movements, outcomes, coming steps and exceptions (D-22, D-33) ------------------------------------

def _on(t: dict | None, day: int) -> bool:
    return t is not None and t["day"] == day


def _lanes(entries: list[dict], waiting: list[dict], deliveries: list[dict], runs, store: str, day: int) -> list[dict]:
    """The planned day strip: decision deadlines, stock-check due times, the store floor's pick-and-pack bars (the
    loading cutoff less the packing time; conditional while no work order exists, solid once prepared) and supplier
    deliveries, then one lane per outgoing scheduled truck run with its cutoff and travel. Only planned values: no
    lane holds the current time (D-18)."""
    decisions = sorted(({"kind": "decision_deadline", "case_key": e["card"]["key"], "case_id": e["card"]["case_id"],
                         "day": e["card"]["due"]["day"], "min": e["card"]["due"]["min"]}
                        for e in entries if e["card"]["source"] == store and _due_on(e["card"], day)),
                       key=lambda m: (_abs(m), m["case_key"]))
    checks = sorted(({"kind": "stock_check_due", "request_id": q["request_id"], "case_key": q["case_key"],
                      "case_id": q["case_id"], "day": q["due_day"], "min": q["due_min"]}
                     for q in waiting if q["to_branch"] == store and q["due_day"] == day),
                    key=lambda m: (_abs(m), m["request_id"]))
    floor = []
    for e in entries:
        trip = e.get("trip")
        if not _live_trip(e) or trip["origin"] != store or trip["pack_from"] is None:
            continue
        if not (_on(trip["pack_from"], day) or _on(trip["loading_cutoff"], day)):
            continue
        floor.append({"kind": "pick_and_pack", "case_key": e["card"]["key"], "case_id": e["card"]["case_id"],
                      "action_id": trip["action"]["action_id"] if trip["action"] else None,
                      "run_code": trip["run_code"], "qty": trip["qty"], "start": trip["pack_from"],
                      "end": trip["loading_cutoff"], "conditional": trip["action"] is None})
    floor.sort(key=lambda b: (_abs(b["start"]), b["case_key"]))
    floor += [{"kind": "delivery", "inbound_id": d["inbound_id"], "sku": d["sku"], "qty": d["qty"],
               "day": d["arrival"]["day"], "min": d["arrival"]["min"], "status": d["status"]} for d in deliveries]
    out = [{"key": "decisions", "kind": "decisions", "branch": store, "items": decisions},
           {"key": "stock_checks", "kind": "stock_checks", "branch": store, "items": checks},
           {"key": "floor", "kind": "floor", "branch": store, "items": floor}]
    for r in runs:
        out.append({"key": f"run:{r.run_code}", "kind": "run", "branch": store, "run_code": r.run_code,
                    "dest": r.dest, "cold_chain": r.cold_chain,
                    "items": [{"kind": "loading_cutoff", **_at(r.day, r.loading_cutoff_min)},
                              {"kind": "travel", "start": _at(r.day, r.departs_min),
                               "end": _at(r.day, r.arrives_min)}]})
    return out


def _movements(entries: list[dict], deliveries: list[dict], runs, store: str, day: int) -> dict:
    """Outgoing: one row per scheduled truck run leaving the store on the scenario date with its planned times and the
    transfers on it (none: an empty list, never an invented one). Incoming: supplier deliveries and branch transfers
    arriving on the scenario date or on their way. Each transfer keeps its decision and movement apart (D-33)."""
    live = [e for e in entries if _live_trip(e)]
    outgoing = []
    for r in runs:
        on = [e for e in live if e["trip"]["origin"] == store and e["trip"]["run_code"] == r.run_code
              and e["trip"]["run_day"] == r.day and e["trip"]["dest"] == r.dest]
        outgoing.append({"run_code": r.run_code, "day": r.day, "source": store, "dest": r.dest,
                         "cold_chain": r.cold_chain, "spare_totes": r.spare_totes,
                         "loading_cutoff": _at(r.day, r.loading_cutoff_min), "departure": _at(r.day, r.departs_min),
                         "arrival": _at(r.day, r.arrives_min),
                         "transfers": [_moving(e) for e in sorted(on, key=lambda e: e["card"]["key"])]})
    arriving = [e for e in live if e["trip"]["dest"] == store and
                (e["trip"]["run_day"] == day or e["trip"]["movement_state"] == "dispatched")]
    incoming = list(deliveries) + [{"kind": "transfer", **_moving(e)} for e in
                                   sorted(arriving, key=lambda e: (e["trip"]["run_day"], e["card"]["key"]))]
    return {"outgoing": outgoing, "incoming": incoming}


def _outcomes(entries: list[dict]) -> list[dict]:
    """The decisions being verified: the engine's expected net against keeping (simulated) beside an actual that stays
    null until someone records it; never the expected figure as actual (PROD-12)."""
    return [{"case_key": e["card"]["key"], "case_id": e["card"]["case_id"], "sku": e["card"]["sku"],
             "lot_id": e["card"]["lot_id"], "plan": e["card"]["suggested"],
             "expected": {"net_vs_keep": e["card"]["est_benefit"], "kind": "simulated"}, "actual": None}
            for e in entries if e["card"]["step"] == "VERIFY"]


def _coming_up(conn, run: dict, ref: refdata.Ref, entries: list[dict], store: str) -> list[dict]:
    """Markdown steps after the scenario date: each live decision's lot at the store, the schedule's next step deeper
    than the price in force (api/stock.py), and the planned price changes its work orders hold. A day, never a clock
    time (a step applies at opening)."""
    out = []
    for e in entries:
        case = e["case"]
        if case["source_branch"] != store or case["state"] in stepkeys.ARCHIVED:
            continue
        lot = cases.lot_meta(conn, run, case)
        step = stock.next_markdown(ref, run["day"], {**lot, "sku": case["sku"]}) if lot["qty"] is not None else None
        if step is not None:
            out.append({"kind": "markdown_step", "case_key": case["key"], "lot_id": case["lot_id"],
                        "sku": case["sku"], "day": step["day"], "date": step["date"], "min": None, "pct": step["pct"],
                        "source": "schedule"})
        for a in e["actions"]:
            if a["kind"] == MARKDOWN and a["state"] == "PLANNED" and (a["effective_day"] or 0) > run["day"]:
                out.append({"kind": "price_change", "case_key": case["key"], "lot_id": a["lot_id"],
                            "sku": a["sku"], "day": a["effective_day"],
                            "date": ref.date(a["effective_day"]).isoformat(), "min": None, "pct": a["pct"],
                            "source": "work_order"})
    return sorted(out, key=lambda x: (x["day"], x["case_key"], x["kind"]))


def _exceptions(conn, run: dict, entries: list[dict]) -> list[dict]:
    """What needs someone apart from the plan: unverified facts on a live decision, open receipt discrepancies,
    blocked dispatches and expired capacity holds, each with the owner and the next action (the board's own keys)."""
    ids = [e["case"]["case_id"] for e in entries]
    gaps: dict[str, list[dict]] = {}
    rows = db.rows(conn, "SELECT * FROM discrepancies WHERE run_id = :r AND case_id = ANY(:c) AND state = :s "
                         "ORDER BY opened_at, discrepancy_id", r=run["run_id"], c=ids, s=movement.OPEN) if ids else []
    for d in rows:
        gaps.setdefault(d["case_id"], []).append(d)
    out = []
    for e in entries:
        card = e["card"]
        head = {"case_key": card["key"], "case_id": card["case_id"], "owner": card["owner"],
                "next_action": card["next_action"]}
        unverified = next((b for b in card["badges"] if b["kind"] == "unverified"), None)
        if unverified is not None:
            out.append({"kind": "unverified_fact", "id": card["case_id"], **head, "fact_ids": unverified["fact_ids"]})
        for d in gaps.get(card["case_id"], []):
            out.append({"kind": "discrepancy", "id": d["discrepancy_id"], **head, "action_id": d["action_id"],
                        "discrepancy_kind": d["kind"], "units": float(d["units"]),
                        "open_units": float(d["open_units"])})
        for reason in ("dispatch_blocked", "hold_expired"):
            if reason in card["blocked_reasons"]:
                out.append({"kind": reason, "id": card["case_id"], **head})
    return out


# --- Today -----------------------------------------------------------------------------------------------------------

def _store_day(conn, run: dict, ref: refdata.Ref, actor: dict, store: str, chosen: bool) -> dict:
    """Today for one store (D-22): summary, lanes, cards, movements, outcomes, coming steps and exceptions."""
    day = run["day"]
    entries, waiting = _entries(conn, run, store)
    packing = _trips(conn, run, ref, entries)
    deliveries = _deliveries(conn, run, ref, store, day, day)
    runs = ref.runs_from(store, day)
    return {"scenario_date": scenario_date(ref, run), "day": day, "branch": store,
            "scope": {"branch": store, "default": not chosen}, "packing_min": packing,
            "summary": _counts(entries, waiting, day, store),
            "lanes": _lanes(entries, waiting, deliveries, runs, store, day),
            "cards": _cards(entries, waiting, ref, run, actor, store),
            "movements": _movements(entries, deliveries, runs, store, day),
            "outcomes": _outcomes(entries), "coming_up": _coming_up(conn, run, ref, entries, store),
            "exceptions": _exceptions(conn, run, entries)}


def _network(conn, run: dict, ref: refdata.Ref, actor: dict, page: int, page_size: int | None) -> dict:
    """HQ's network form (D-22, D-33): the four counts summed over every store, 05.2 D-38's `evaluated` and `at_risk`
    from the board's own rule for HQ's unfiltered scope, the stores needing attention (any count above 0) ordered by
    decisions due, descending, then store code and paged, and the cards that wait on HQ."""
    day = run["day"]
    entries, waiting = _entries(conn, run, None)
    stores = sorted({e["card"]["source"] for e in entries} | {q["to_branch"] for q in waiting} |
                    {w["branch"] for e in entries for w in e["card"]["next_owner"] if w.get("branch")})
    rows = []
    for b in stores:
        counts = _counts(entries, waiting, day, b)
        if any(counts[k] for k in COUNTS):
            rows.append({"branch": b, **counts})
    rows.sort(key=lambda r: (-r["decisions_due"], r["branch"]))
    firsts = [r["first_deadline"] for r in rows if r["first_deadline"]]
    board = cases.board_counts(conn, run, actor)  # the board's own counts for HQ's unfiltered scope
    network = {**{k: sum(r[k] for r in rows) for k in COUNTS},
               "first_deadline": min(firsts, key=_abs) if firsts else None,
               "evaluated": board["cases"], "at_risk": board["cases"] + board["flags"]}
    size = movement._page_size(ref, page_size, len(rows))
    first = (page - 1) * size
    return {"scenario_date": scenario_date(ref, run), "day": day, "branch": None,
            "scope": {"branch": None, "default": True}, "network": network,
            "branches": {"items": rows[first:first + size], "page": page, "page_size": size, "total": len(rows)},
            "cards": _cards(entries, waiting, ref, run, actor, None)}


def today(conn, run: dict, actor: dict, branch: str | None = None, page: int = 1,
          page_size: int | None = None) -> dict:
    """GET /api/today: a store's day (a manager's own; HQ's named one), or HQ's network form without a store."""
    ref = refdata.get(run["dataset_version"])
    store = _store(ref, actor, branch)
    if store is None:
        return _network(conn, run, ref, actor, page, page_size)
    return _store_day(conn, run, ref, actor, store, chosen=actor["role"] == "hq")


# --- the shell's counts ----------------------------------------------------------------------------------------------

def today_counts(conn, run: dict, actor: dict) -> dict:
    """Today's action cards counted without building one: {"cards": the stock decisions that wait on the account,
    "checks": the stock checks waiting on its store (0 for HQ's network form)}, so `cards` + `checks` is
    len(today()["cards"]) and `checks` is today()["summary"]["stock_checks"].

    The scope is `_entries`' own (`_in_scope`), after hold expiry, in a fixed number of statements: the cases with
    their active version's recommended key, the current round's decisions on it, and the work orders and open
    discrepancy of the cases that are prepared or ran (cases.decided_many, cases.execution_many). Whether a case waits
    on the account is `_names` over the three keys it reads, each computed by the rule a card uses on the same
    inputs: stepkeys.next_action (api/stepkeys.py stays the only place a state becomes a step or an owner),
    cases.next_owner and the case's state. The plan is the card's, the plan in force: the selection bound to the
    active version when there is one, else the recommended key (api/selection.py `plan_in_force`; 07-CONTEXT D-23)."""
    from . import selection  # it reads cases at import: imported here
    ref = refdata.get(run["dataset_version"])
    store = _store(ref, actor, None)
    bookings.expire_holds(conn, run)
    where, p = _in_scope(run, store)
    rows = db.rows(conn, f"SELECT c.*, rec.result->>'recommended' AS recommended_key FROM cases c "
                         f"LEFT JOIN recommendations rec ON rec.run_id = c.run_id AND rec.case_id = c.case_id "
                         f"AND rec.rec_version = c.active_rec_version WHERE {where} ORDER BY c.case_id", **p)
    keys = {c["case_id"]: c.pop("recommended_key") for c in rows}
    decided = cases.decided_many(conn, run["run_id"], rows)
    execution = cases.execution_many(conn, run["run_id"], rows)
    n = 0
    for c in rows:
        key = keys[c["case_id"]]  # None without a version row, as plan_in_force gives without one
        sel = selection.bound(c) if key is not None else None
        key = sel["plan_key"] if sel is not None else key
        plan = cases.parse_plan(key) if key is not None else None
        d = decided[c["case_id"]]
        view = {"next_action": stepkeys.next_action(c, plan, d, execution[c["case_id"]]), "state": c["state"],
                "next_owner": cases.next_owner(c, plan, d)}
        n += _names(view, actor, store)
    checks = 0
    if store is not None:
        checks = int(db.one(conn, "SELECT count(*) AS n FROM evidence_requests q JOIN cases c USING (run_id, case_id) "
                                  "WHERE q.run_id = :r AND q.state = ANY(:s) AND q.to_branch = :b",
                            r=run["run_id"], s=list(CHECK_STATES), b=store)["n"])
    return {"cards": n, "checks": checks}


def nav_counts(conn, run: dict, session: dict) -> dict:
    """GET /api/session `nav_counts` for the signed-in account: `today` (Today's action cards), `board` (the board's
    open stock decisions in the account's default scope: every stage but closed, flags included) and `tasks` (a
    store's stock checks waiting on it, transfers to dispatch from it and transfers to receive at it, and since 07-19
    the lot holds it owns and its open advisory tasks, so it is GET /api/tasks' six tiles summed; HQ none).

    Counted with set-based reads, never by building the pages they count. The equalities: today = len(today()
    ["cards"]) = today_counts' cards + checks; board = the board's stage_counts summed over stepkeys.STAGES =
    cases.board_counts' open + flags; a store's tasks = today()["summary"]["stock_checks"] (= checks) plus the two
    transfer totals, unchanged. tests/api/test_view_identity.py pins them against the pages, byte for byte."""
    if not session.get("account"):
        return {"today": 0, "board": 0, "tasks": 0}
    actor = {"account": session["account"], "role": session["role"], "branch": session["branch"]}
    mine = today_counts(conn, run, actor)
    board = cases.board_counts(conn, run, actor)
    tasks = 0
    if actor["role"] != "hq":
        from . import tasklist  # it reads movement and execution at import
        tasks = mine["checks"] + sum(
            movement.transfers(conn, run, actor, direction=d, state=s)["total"]
            for d, s in (("outgoing", "planned"), ("incoming", "dispatched")))
        tasks += tasklist.open_beyond_checks(conn, run, actor["branch"])  # lot holds, advisory tasks (07-19)
    return {"today": mine["cards"] + mine["checks"], "board": board["open"] + board["flags"], "tasks": tasks}


# --- the planned schedule (D-18, D-29) -------------------------------------------------------------------------------

VIEWS = ("day", "week", "month")
GROUPS = ("work_order", "branch", "run")
ACTIVITIES = ("decision_deadline", "stock_check", "pick_and_pack", "loading_cutoff", "transport", "receive",
              "markdown", "delivery")
STATUSES = ("open", "not_requested", "answered", "confirmed", "expired", "superseded", "not_started", "scheduled",
            "dispatched", "partially_received", "received", "discrepancy", "done", "rejected", "closed", "cancelled",
            "voided")
DECIDED = ("APPROVED", "PREPARED", "EXECUTED", "OBSERVED")  # the decision was made and committed
LEFT = ("dispatched", "partially_received", "received", "discrepancy")  # the stock left the source
ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
CONTAINERS = ("work_order", "run", "branch")  # parent rows; every other row is a leaf
RUN_ACTIVITIES = ("decision_deadline", "pick_and_pack", "loading_cutoff", "transport", "receive")  # under a run


def check_schedule(view: str, group: str, activity: str | None, status: str | None) -> None:
    """The enumerated schedule parameters; any other value is 400 bad_request."""
    for name, value, allowed in (("view", view, VIEWS), ("group", group, GROUPS), ("activity", activity, ACTIVITIES),
                                 ("status", status, STATUSES)):
        if value is not None and value not in allowed:
            raise ApiError(400, "bad_request", f"{name} is one of {', '.join(allowed)}")


def window(ref: refdata.Ref, run: dict, on: str | None, view: str) -> tuple[date, date, date]:
    """(the anchor date, the window's first and last date): the day of `on` (default the scenario date), its
    Monday-to-Sunday week, or its calendar month, never longer (T-06.1-45). A date that is not YYYY-MM-DD, or no such
    day, is 400 bad_request."""
    if on is None:
        anchor = date.fromisoformat(scenario_date(ref, run))
    else:
        try:
            if not ISO_DATE.match(on):
                raise ValueError(on)
            anchor = date.fromisoformat(on)
        except ValueError:
            raise ApiError(400, "bad_request", "date must be a day as YYYY-MM-DD") from None
    if view == "day":
        return anchor, anchor, anchor
    if view == "week":
        monday = anchor - timedelta(days=anchor.weekday())
        return anchor, monday, monday + timedelta(days=6)
    first = anchor.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return anchor, first, last


def _index(ref: refdata.Ref, d: date) -> int:
    """The day index of a calendar date in the dataset calendar (the inverse of `ref.date`)."""
    return (d - ref.date(0)).days


def _row(rid: str, kind: str, title: str, branch: str | None, start: dict | None, end: dict | None,
         status: str | None, owner: dict | None, **extra) -> dict:
    return {"id": rid, "parent_id": None, "kind": kind, "title": title, "sub": extra.get("sub"), "branch": branch,
            "start": start, "end": end, "status": status, "owner": owner, "qty": extra.get("qty"),
            "run_code": extra.get("run_code"), "case_key": extra.get("case_key"),
            "depends_on": list(extra.get("depends_on", ()))}


def _decision_status(state: str) -> str:
    if state == "REJECTED":
        return "rejected"
    if state == "CLOSED":
        return "closed"
    return "done" if state in DECIDED else "open"


def _transfer_status(kind: str, trip: dict) -> str:
    """A transfer activity's status from the recorded work order and movements: not started before Prepare;
    cancelled, voided or expired as the work order says; then by what was recorded (a work order Next day carried out
    with no recorded movement is done)."""
    action = trip["action"]
    if action is None:
        return "not_started"
    if action["state"] in GONE_ORDERS:
        return action["state"].lower()
    m = trip["movement_state"]
    if m is None:
        return "done"
    gone = m in LEFT
    if kind == "pick_and_pack":
        return "done" if gone else "scheduled"
    if kind == "loading_cutoff":
        return "dispatched" if gone else "scheduled"
    if kind == "transport":
        return {"planned": "scheduled", "dispatched": "dispatched"}.get(m, "received" if m != "discrepancy" else m)
    return {"planned": "not_started", "dispatched": "not_started"}.get(m, m)


def _sort_key(t: dict | None, end: bool = False) -> tuple:
    """A row time's position: undated last; a day-only time (a markdown step at opening) at the start of its day, or at
    its end for an end time."""
    if t is None:
        return (1, 0)
    return (0, t["day"] * DAY_MIN + (t["min"] if t["min"] is not None else (DAY_MIN - 1 if end else 0)))


def _span(kids: list[dict]) -> tuple[dict | None, dict | None]:
    """A parent's planned start and end from its children's (None when no child has a time)."""
    starts = [k["start"] for k in kids if k["start"] is not None]
    ends = [k["end"] for k in kids if k["end"] is not None]
    return (min(starts, key=_sort_key) if starts else None,
            max(ends, key=lambda t: _sort_key(t, end=True)) if ends else None)


def _activities(conn, run: dict, ref: refdata.Ref, entry: dict, checks: list[dict]) -> list[dict]:
    """A stock decision's activities, each one row with only the times the data plans: the decision deadline (the
    stored decide_by), the stock check (no planned time), the transfer's pick and pack, loading cutoff, transport and
    receive (no planned time), and the markdown (its day: a price change's effective day, or the lot's next schedule
    step; no clock time). Ids are stable: "wo:{case}:{activity}". Pick and pack -> loading cutoff -> transport ->
    receive are linked by `depends_on`."""
    case, card, trip = entry["case"], entry["card"], entry["trip"]
    base = f"wo:{case['case_id']}"
    owner = card["owner"] or _store_owner(case["source_branch"])
    common = {"case_key": case["key"]}
    out = []
    by = card["decide_by"]
    at = {"day": by["day"], "min": by["latest_decision_min"]} if by else None
    out.append(_row(f"{base}:decision_deadline", "decision_deadline", "Decision deadline", case["source_branch"], at,
                    at, _decision_status(case["state"]), owner, **common))
    mine = [q for q in checks if q["case_id"] == case["case_id"]]
    asked = next((q for q in reversed(mine) if q["rec_version"] == case["active_rec_version"]), None) or \
        (mine[-1] if mine else None)
    if asked is not None or any(b["kind"] == "unverified" for b in card["badges"]):
        where = asked["to_branch"] if asked is not None else case["source_branch"]
        out.append(_row(f"{base}:stock_check", "stock_check", f"Stock check · {where}", where, None, None,
                        asked["state"].lower() if asked is not None else "not_requested", _store_owner(where),
                        **common))
    if trip is not None and _live_trip(entry):
        ids = {k: f"{base}:{k}" for k in ("pick_and_pack", "loading_cutoff", "transport", "receive")}
        move = {"qty": trip["qty"], "run_code": trip["run_code"], **common}
        src, dst = trip["origin"], trip["dest"]
        out += [_row(ids["pick_and_pack"], "pick_and_pack", "Pick and pack", src, trip["pack_from"],
                     trip["loading_cutoff"], _transfer_status("pick_and_pack", trip), _store_owner(src), **move),
                _row(ids["loading_cutoff"], "loading_cutoff", f"Loading cutoff · {trip['run_code']}", src,
                     trip["loading_cutoff"], trip["loading_cutoff"], _transfer_status("loading_cutoff", trip),
                     _store_owner(src), depends_on=[ids["pick_and_pack"]], **move),
                _row(ids["transport"], "transport", f"{trip['run_code']} · {src} → {dst}", src, trip["departure"],
                     trip["arrival"], _transfer_status("transport", trip), _store_owner(src),
                     depends_on=[ids["loading_cutoff"]], **move),
                _row(ids["receive"], "receive", f"Receive · {dst}", dst, None, None,
                     _transfer_status("receive", trip), _store_owner(dst), depends_on=[ids["transport"]], **move)]
    price = next((a for a in entry["actions"] if a["kind"] == MARKDOWN), None)
    step = None
    if price is not None and price["effective_day"] is not None and price["state"] not in GONE_ORDERS:
        step = {"day": price["effective_day"], "pct": price["pct"],
                "status": "done" if price["state"] == "DONE" else "scheduled"}
    elif case["state"] not in stepkeys.ARCHIVED:
        lot = cases.lot_meta(conn, run, case)
        nxt = stock.next_markdown(ref, run["day"], {**lot, "sku": case["sku"]}) if lot["qty"] is not None else None
        if nxt is not None:
            step = {"day": nxt["day"], "pct": nxt["pct"], "status": "scheduled"}
    if step is not None:
        at = {"day": step["day"], "min": None}  # a markdown applies at opening: a day, no clock time
        out.append(_row(f"{base}:markdown", "markdown", f"Markdown {step['pct']}%", case["source_branch"], at, at,
                        step["status"], _store_owner(case["source_branch"]), **common))
    return out


def _plan_word(card: dict) -> str:
    if not card["suggested_key"]:
        return "decision"
    return cases.parse_plan(card["suggested_key"]).kind.lower()


def _work_order(entry: dict, kids: list[dict]) -> dict:
    """A stock decision as a work-order parent: its title and sub line, its span from its activities, its status (the
    transfer's transport, a price change's markdown, else the decision's), owner and quantity."""
    case, card, trip = entry["case"], entry["card"], entry["trip"]
    start, end = _span(kids)
    where = f"{case['source_branch']} → {card['dest']}" if card["dest"] else case["source_branch"]
    qty = trip["qty"] if trip else None
    by_kind = {k["kind"]: k for k in kids}
    status = (by_kind["transport"]["status"] if "transport" in by_kind and trip["action"] is not None else
              by_kind["markdown"]["status"] if "markdown" in by_kind and any(
                  a["kind"] == MARKDOWN for a in entry["actions"]) else _decision_status(case["state"]))
    sub = f"{case['key']} · lot {case['lot_id']}" + (f" · {qty:g} units" if qty is not None else "")
    return _row(f"wo:{case['case_id']}", "work_order", f"{case['sku']} {_plan_word(card)} · {where}",
                case["source_branch"], start, end, status, card["owner"] or _store_owner(case["source_branch"]),
                sub=sub, qty=qty, run_code=trip["run_code"] if trip else None, case_key=case["key"])


def _touches(row: dict, first: int, last: int) -> bool:
    """A row with a planned time inside the window (a span overlapping it)."""
    if row["start"] is None:
        return False
    end = row["end"] or row["start"]
    return row["start"]["day"] <= last and end["day"] >= first


def _by_start(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (_sort_key(r["start"]), r["id"]))


def _leaves(rows: list[dict]) -> list[tuple]:
    return [(r, []) for r in _by_start(rows)]


def _run_rows(ref: refdata.Ref, store: str | None, orders: list[tuple], first: int, last: int) -> list[tuple]:
    """Grouped by scheduled truck run: every run leaving the store in the window (from the reference timetable) and
    every run a decision in scope rides (an incoming transfer's, or, for HQ's network, the only runs read), in the
    order day, loading cutoff, run code. Each run's children are the decision deadline and transfer activities of the
    decisions on it; a run with no transfer has none."""
    found: dict[tuple, dict] = {}
    if store is not None:
        for d in range(first, last + 1):
            for r in ref.runs_from(store, d):
                found[(r.run_code, r.day)] = {"code": r.run_code, "day": r.day, "origin": r.origin, "dest": r.dest,
                                              "cutoff": _at(r.day, r.loading_cutoff_min),
                                              "arrival": _at(r.day, r.arrives_min), "kids": []}
    for _, kids, e in orders:
        trip = e["trip"]
        if trip is None or not _live_trip(e) or not first <= trip["run_day"] <= last:
            continue
        run = found.setdefault((trip["run_code"], trip["run_day"]), {
            "code": trip["run_code"], "day": trip["run_day"], "origin": trip["origin"], "dest": trip["dest"],
            "cutoff": trip["loading_cutoff"], "arrival": trip["arrival"], "kids": []})
        run["kids"] += [k for k in kids if k["kind"] in RUN_ACTIVITIES]
    out = []
    for r in sorted(found.values(), key=lambda r: (r["day"], _sort_key(r["cutoff"]), r["code"])):
        moving = next((k for k in _by_start(r["kids"]) if k["kind"] == "transport"), None)
        parent = _row(f"run:{r['code']}:{r['day']}", "run", r["code"], r["origin"], r["cutoff"], r["arrival"],
                      moving["status"] if moving else None, _store_owner(r["origin"]),
                      sub=f"{r['origin']} → {r['dest']}", run_code=r["code"])
        out.append((parent, _leaves(r["kids"])))
    return out


def _grouped(ref: refdata.Ref, group: str, store: str | None, orders: list[tuple], loose: list[dict], first: int,
             last: int) -> list[tuple]:
    """The schedule as a tree of (row, children) nodes in the server's order, for the chosen grouping."""
    if group == "run":
        return _run_rows(ref, store, orders, first, last)
    nodes = [(wo, _leaves(kids)) for wo, kids, _ in orders] + [(d, []) for d in loose]
    nodes.sort(key=lambda n: (_sort_key(n[0]["start"]), n[0]["id"]))
    if group == "work_order":
        return nodes
    stores: dict[str, list[tuple]] = {}
    for n in nodes:
        stores.setdefault(n[0]["branch"], []).append(n)
    out = []
    for b in sorted(stores):
        start, end = _span([n[0] for n in stores[b]])
        out.append((_row(f"branch:{b}", "branch", b, b, start, end, None, _store_owner(b)), stores[b]))
    return out


def _filtered(node: tuple, activity: str | None, status: str | None) -> tuple | None:
    """A node with only the leaves that match `activity` and `status` (a zero-length row is a leaf); a container left
    with nothing under a filter is dropped. Without a filter, everything stays (an empty run included)."""
    row, children = node
    if row["kind"] not in CONTAINERS:
        return node if _keep([row], activity, status) else None
    kept = [n for n in (_filtered(c, activity, status) for c in children) if n is not None]
    if (activity is not None or status is not None) and not kept:
        return None
    return row, kept


def _flat(node: tuple, parent_id: str | None = None) -> list[dict]:
    row, children = node
    out = [{**row, "parent_id": parent_id}]
    for c in children:
        out += _flat(c, row["id"])
    return out


def _keep(rows: list[dict], activity: str | None, status: str | None) -> list[dict]:
    return [r for r in rows
            if (activity is None or r["kind"] == activity) and (status is None or r["status"] == status)]


def schedule(conn, run: dict, actor: dict, on: str | None = None, view: str = "day", group: str = "work_order",
             branch: str | None = None, activity: str | None = None, status: str | None = None, page: int = 1,
             page_size: int | None = None) -> dict:
    """GET /api/schedule (D-18, D-29): read-only planned rows for a date window, parents with their children.

    - **Rows.** A stock decision is a work-order parent whose children are its activities (`_activities`); a store's
      supplier delivery is a zero-length row of its own; grouped by scheduled truck run, each run is a parent of the
      transfer activities on it (a run with no transfer has no children, never an invented one); grouped by branch,
      each store is a parent of its work orders (by source store) and deliveries.
    - **Window.** A decision is listed when one of its activities has a planned time in the window, or when it is
      still open and the window holds the scenario date (its unscheduled work is that day's).
    - **Scope** (D-17): a manager's own store, HQ's named one; HQ with no store reads every store's work orders, and
      runs only as those name them (no per-store reference read across the network, T-06.1-45).
    - **Filters and pages.** `activity` and `status` keep the matching leaves and drop a parent left with none; pages
      walk the top-level parents in one stable order, each with all its kept descendants.
    Nothing reads a clock: a status is a recorded state, never "overdue" or "missed"."""
    check_schedule(view, group, activity, status)
    ref = refdata.get(run["dataset_version"])
    store = _store(ref, actor, branch)
    anchor, lo, hi = window(ref, run, on, view)
    first, last = _index(ref, lo), _index(ref, hi)
    entries, _ = _entries(conn, run, store)
    _trips(conn, run, ref, entries)
    checks = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r ORDER BY created_at, request_id",
                     r=run["run_id"])
    holds_today = first <= run["day"] <= last
    orders = []
    for e in entries:
        kids = _activities(conn, run, ref, e, checks)
        if any(_touches(k, first, last) for k in kids) or (e["card"]["step"] != "ARCHIVE" and holds_today):
            orders.append((_work_order(e, kids), kids, e))
    loose = [_row(f"delivery:{d['inbound_id']}", "delivery", f"Delivery · {d['sku']}", d["branch"], d["arrival"],
                  d["arrival"], d["status"], _store_owner(d["branch"]), qty=d["qty"])
             for d in (_deliveries(conn, run, ref, store, first, last) if store is not None else [])]
    nodes = [n for n in (_filtered(n, activity, status) for n in _grouped(ref, group, store, orders, loose, first,
                                                                           last)) if n is not None]
    size = movement._page_size(ref, page_size, len(nodes))
    begin = (page - 1) * size
    return {"date": anchor.isoformat(), "view": view, "group": group, "branch": store,
            "scenario_date": scenario_date(ref, run),
            "window": {"from": lo.isoformat(), "to": hi.isoformat(), "from_day": first, "to_day": last},
            "rows": [r for n in nodes[begin:begin + size] for r in _flat(n)],
            "page": page, "page_size": size, "total": len(nodes)}
