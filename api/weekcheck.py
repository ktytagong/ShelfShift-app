"""The careful-day evaluation driver and the trip check the second-look tests need (04-CONTEXT D-06).

This module is an evaluation driver, not part of the app: it stands in for honest counters by reading PHYSICAL stock
(`truth_lots`), which no route may ever do (FLOW-08). No module under api/ other than this one imports it, and no route
may (a static test checks it). Phase 8 adds the five-seed weekcheck run and the metrics (`api/metrics.py`,
`python -m api.weekcheck`, `test_metrics.py`); there is no command-line entry here.

- `careful_day`: for each OPEN case with a version, the first fact on its active version that changes the plan and
  that a count settles (PHANTOM_STOCK, STALE_COUNT) is asked about by the case's source manager, the addressed branch
  answers every lot in the fact's lot_ids with its physical quantity on the shelf and 0 in the backroom, and confirms
  with the scope tick, all through `signals`; then the warm-up autopilot sends, accepts, approves and prepares every
  open case through the service layer (`sandbox.autopilot`).
- `trips_paid_once`: the transport in the shelfshift outcomes equals one `engine.model.trip_cost` per dispatched
  transfer (a short shipment at the quantity it shipped, a failed trip at its cancel cost), within $0.01, and no
  dispatched round still holds its source set-aside.
"""
from __future__ import annotations

from engine.model import trip_cost

from . import cases, db, overlay, sandbox, signals

TOLERANCE = 0.01  # money compared to the cent


def careful_day(conn, run: dict) -> None:
    """Ask about every plan-changing count fact, count honestly from physical stock, confirm, then the autopilot."""
    for listed in db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r AND state = 'OPEN' "
                                "AND active_rec_version IS NOT NULL ORDER BY case_id", r=run["run_id"]):
        case = cases.case_row(conn, run["run_id"], listed["case_id"])  # an earlier confirm may have replanned it
        rec = cases.active_rec(conn, case)
        if case["state"] != "OPEN" or rec is None:
            continue
        fact = next((f for f in rec["fact_checks"] if f["changes_plan"] and f["kind"] in signals.COUNT_KINDS), None)
        if fact is None:
            continue
        asker = {"role": "manager", "branch": case["source_branch"], "session_id": "weekcheck"}
        q = signals.request_evidence(conn, run, asker, case["case_id"], rec["rec_version"], fact["fact_id"])
        if q["state"] not in signals.ANSWERABLE:
            continue  # already settled on this version
        counter = {"role": "manager", "branch": fact["branch"], "session_id": "weekcheck"}
        physical = overlay.physical(conn, run, "shelfshift", fact["lot_ids"])  # through the template (05.2 D-09)
        counts = {lot_id: {"shelf_units": physical[lot_id], "backroom_units": 0.0} for lot_id in fact["lot_ids"]}
        s = signals.answer(conn, run, counter, q["request_id"], "counted", counts)
        signals.confirm(conn, run, counter, s["signal_id"], s["revision"], True)
    sandbox.autopilot(conn, run)


def trips_paid_once(conn, run_id: str) -> list[str]:
    """[] when every dispatched trip was charged exactly once and no dispatched round still holds its set-aside;
    else one line per problem."""
    shipped = {(e["day"], e["payload"]["lot_id"]): float(e["payload"]["shipped"]) for e in db.rows(
        conn, "SELECT day, payload FROM world_events WHERE run_id = :r AND world = 'shelfshift' "
              "AND kind = 'SHORT_SHIPMENT'", r=overlay.world_events_run(conn, run_id))}  # through the template
    due = sum(trip_cost(a["fixed_cost"], a["cancel_cost"], a["per_unit_cost"],
                        shipped.get((a["run_day"], a["lot_id"]), float(a["qty"])))[0]
              for a in db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                                     "AND kind = 'TRANSFER' AND state = 'DONE' ORDER BY action_id", r=run_id))
    read, p = overlay.everywhere(conn, run_id, "outcomes")  # every series, own or the template's (05.2 D-09)
    paid = float(db.one(conn, f"SELECT coalesce(sum(transport), 0) AS t FROM outcomes WHERE {read} "
                              f"AND outcomes.world = 'shelfshift'", **p)["t"])
    # a round's own transfer is the action prepare wrote for it: "<case>#<round>:transfer"
    left = db.one(conn, "SELECT count(*) AS n FROM reservations s JOIN world_actions a ON a.run_id = s.run_id "
                        "AND a.world = 'shelfshift' AND a.case_id = s.case_id AND a.kind = 'TRANSFER' "
                        "AND a.state = 'DONE' AND a.action_id = s.case_id || '#' || s.round || :suffix "
                        "WHERE s.run_id = :r AND s.kind = 'SOURCE_LOT' AND s.state = 'COMMITTED'", r=run_id,
                  suffix=":transfer")["n"]
    return ([f"transport paid {paid:.2f} != {due:.2f} due"] if abs(paid - due) > TOLERANCE else []) + \
           ([f"{left} shipped set-aside(s) still booked"] if left else [])
