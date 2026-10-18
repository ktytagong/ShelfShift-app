"""Bookings with holds (spec §8 Bookings, Hold expiry, Recheck contract, Locking rule).

Five kinds, each a (kind, bucket) row in `reservations` that carries its case_id and approval round:
- DEMAND (bucket "dest|sku", per day, units): the destination's unmet demand the transfer will sell into;
- STORAGE (bucket "dest|zone", per day from arrival through expiry, totes);
- TRUCK (bucket "run_code|run day", totes on that one run);
- SOURCE_LOT (bucket = the case lot, units set aside at the source);
- LOT_PRICE (bucket = the case lot, the exclusive right to change its price, 1 per lot).

A hold lasts HOLD_TTL_MIN real minutes (api/settings.json, 60), computed by Postgres as now() plus that interval, never
demo time. Availability counts COMMITTED rows plus HELD rows with expires_at > now() (api/inputs.LIVE), so a hold at
or past its expiry never counts, even before `expire_holds` marks it EXPIRED. Nothing here ever moves expires_at.

On commit the DEMAND hold becomes an inbound delivery at the destination (state CONVERTED, so it is counted once, on
the destination's timeline), SOURCE_LOT a set-aside and LOT_PRICE a committed price right (both COMMITTED), and the
storage and truck holds COMMITTED. `buckets`, `holds_for` and `resources` read an EFFECTIVE input (engine.adjust
.effective of the raw input that excludes this case's own bookings); check_plan itself is never given one (row 6).

In a workspace the delivery is written to its own copy of the destination's series, and a release first copies any
series in which the template holds the case's delivery (api/overlay.py, 05.2 D-09).
"""
from __future__ import annotations

import math
import uuid

from engine.limits import CAP_REASON, transfer_caps, unmet_by_day
from engine.model import KEEP, TRANSFER, EngineInput, Plan, Run, totals
from engine.timing import first_sellable_day

from . import config, db, overlay

OPEN_STATES = ("HELD", "COMMITTED", "CONVERTED")  # what a release ends
MOVED_SEQ = 10**6  # a moved lot sells after the destination's own lots (as advance numbers it)


class Shortfall(Exception):
    """The holds cannot cover the plan's quantity; never booked silently short (the engine's reason)."""

    def __init__(self, kind: str):
        super().__init__(CAP_REASON[kind])
        self.reason = CAP_REASON[kind]


def plan_run(inp: EngineInput, plan: Plan) -> Run | None:
    """Today's run the transfer names: its code to its destination, or None."""
    return next((r for r in inp.runs if r.run_code == plan.run_code and r.dest == plan.dest), None)


def buckets(eff: EngineInput, plan: Plan) -> list[tuple[str, str]]:
    """(kind, bucket) for every resource this plan books, in lock order (spec §8 Locking rule 2: kind, then key)."""
    lot = eff.case_lot()
    out = []
    if plan.kind == TRANSFER:
        run = plan_run(eff, plan)
        out += [("DEMAND", f"{plan.dest}|{eff.product.sku}"), ("STORAGE", f"{plan.dest}|{eff.product.zone}"),
                ("TRUCK", f"{run.run_code}|{run.day}"), ("SOURCE_LOT", lot.lot_id)]
    if plan.pct:
        out.append(("LOT_PRICE", lot.lot_id))
    return sorted(out)


def lock(conn, run_id: str, keys: list[tuple[str, str]]) -> None:
    """Serialize competing bookings of one resource inside one sandbox: a transaction-scoped advisory lock per
    (kind, bucket), always taken in sorted (kind, bucket) order. Released at commit or rollback."""
    for kind, bucket in sorted(keys):
        db.run(conn, "SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))", k=f"{run_id}:{kind}:{bucket}")


def holds_for(eff: EngineInput, plan: Plan) -> list[tuple[str, str, int | None, float]]:
    """(kind, bucket, day, qty) for every resource the plan's action type needs (spec §8 Lifecycle by action type):
    a transfer holds DEMAND, STORAGE, TRUCK and SOURCE_LOT, plus LOT_PRICE with a markdown rest; a markdown holds
    LOT_PRICE; Keep holds nothing. `eff` excludes this case's own bookings.

    DEMAND is taken from the destination's unmet demand per day, earliest sellable day first, net of other cases'
    held demand, exactly as transfer_caps counts it; Shortfall if it cannot cover the quantity."""
    lot, p = eff.case_lot(), eff.product
    out: list[tuple[str, str, int | None, float]] = []
    if plan.kind == TRANSFER:
        run = plan_run(eff, plan)
        sell_day = first_sellable_day(run.day, run.arrives_min, eff.policy)
        arrival = run.day + run.arrives_min // 1440
        held = totals((d, u) for b, d, u in eff.bookings.demand if b == plan.dest)
        lost = unmet_by_day(eff, plan.dest)
        left = float(plan.qty)
        for d in range(sell_day, lot.expiry_day + 1):
            take = min(left, max(0.0, lost.get(d, 0.0) - held.get(d, 0.0)))
            if take > 1e-9:
                out.append(("DEMAND", f"{plan.dest}|{p.sku}", d, take))
                left -= take
        if left > 1e-6:
            raise Shortfall("demand")
        totes = math.ceil(plan.qty / p.units_per_tote)
        out += [("STORAGE", f"{plan.dest}|{p.zone}", d, float(totes)) for d in range(arrival, lot.expiry_day + 1)]
        out += [("TRUCK", f"{run.run_code}|{run.day}", run.day, float(totes)),
                ("SOURCE_LOT", lot.lot_id, None, float(plan.qty))]
    if plan.pct:
        out.append(("LOT_PRICE", lot.lot_id, None, 1.0))
    return out


def hold(conn, run_id: str, case_id: str, round_: int, rows: list[tuple]) -> None:
    """Insert every hold of the round; expires_at is Postgres's now() plus HOLD_TTL_MIN minutes (real time)."""
    for kind, bucket, day, qty in rows:
        db.run(conn, "INSERT INTO reservations (run_id, res_id, case_id, round, kind, bucket, day, qty, state, "
                     "expires_at) VALUES (:r, :id, :c, :n, :k, :b, :d, :q, 'HELD', "
                     "now() + make_interval(secs => CAST(:ttl AS float8) * 60))",
               r=run_id, id=uuid.uuid4().hex, c=case_id, n=round_, k=kind, b=bucket, d=day, q=float(qty),
               ttl=float(config.HOLD_TTL_MIN))


def held_ok(conn, run_id: str, case_id: str, round_: int) -> bool:
    """Every booking of this round is still HELD and unexpired (Keep has none, so it is trivially true)."""
    r = db.one(conn, "SELECT count(*) FILTER (WHERE state = 'HELD' AND expires_at > now()) AS ok, count(*) AS n "
                     "FROM reservations WHERE run_id = :r AND case_id = :c AND round = :n",
               r=run_id, c=case_id, n=round_)
    return r["ok"] == r["n"]


def moved_lot_id(case_id: str, round_: int, dest: str) -> str:
    """The lot a transfer creates at the destination; one per round, so a second look never reuses an id."""
    return f"{case_id}>{dest}#{round_}"


def commit(conn, run_id: str, case_id: str, round_: int, plan: Plan, eff: EngineInput) -> None:
    """The round's holds become bookings in the caller's transaction: DEMAND → an inbound delivery at the destination
    carrying source_case_id and the source branch as origin (the row turns CONVERTED), every other kind COMMITTED."""
    db.run(conn, "UPDATE reservations SET state = CASE WHEN kind = 'DEMAND' THEN 'CONVERTED' ELSE 'COMMITTED' END, "
                 "expires_at = NULL WHERE run_id = :r AND case_id = :c AND round = :n AND state = 'HELD'",
           r=run_id, c=case_id, n=round_)
    if plan.kind != TRANSFER:
        return
    run, lot = plan_run(eff, plan), eff.case_lot()
    overlay.materialize(conn, run_id, "shelfshift", plan.dest, eff.product.sku)  # the workspace's own series (05.2 D-09)
    db.run(conn, "INSERT INTO inbound (run_id, world, inbound_id, branch, sku, qty, arrival_day, arrival_min, "
                 "expiry_day, received_seq, expiry_source, last_verified_day, source_case_id, origin) VALUES "
                 "(:r, 'shelfshift', :i, :b, :s, :q, :d, :m, :e, :seq, :es, :lv, :c, :o)",
           r=run_id, i=moved_lot_id(case_id, round_, plan.dest), b=plan.dest, s=eff.product.sku, q=float(plan.qty),
           d=run.day + run.arrives_min // 1440, m=run.arrives_min % 1440, e=lot.expiry_day, seq=MOVED_SEQ,
           es=lot.expiry_source, lv=run.day, c=case_id, o=lot.branch)


def release(conn, run_id: str, case_id: str) -> None:
    """Every arrow into REJECTED, CLOSED or a cancelled approval releases what the current look holds or booked: its
    held and committed rows, its pending inbound delivery and its planned work orders (spec §8 Case states). On a
    second look an earlier look's plan already ran, so rounds up to look_round stay booked."""
    look = db.one(conn, "SELECT look_round FROM cases WHERE run_id = :r AND case_id = :c", r=run_id, c=case_id)
    n = look["look_round"] if look else 0
    db.run(conn, "UPDATE reservations SET state = 'RELEASED' WHERE run_id = :r AND case_id = :c AND state = ANY(:s) "
                 "AND round > :n", r=run_id, c=case_id, s=list(OPEN_STATES), n=n)
    overlay.materialize_deliveries(conn, run_id, case_id)  # a delivery the template holds becomes this workspace's
    db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND world = 'shelfshift' AND source_case_id = :c "
                 "AND CAST(substring(inbound_id FROM '#([0-9]+)$') AS int) > :n", r=run_id, c=case_id, n=n)
    db.run(conn, "DELETE FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                 "AND state = 'PLANNED'", r=run_id, c=case_id)


def done(conn, run_id: str, case_id: str) -> None:
    """A case whose lot and moved lots are gone ends what its plans still occupy: every HELD, COMMITTED or CONVERTED
    row becomes DONE (spec §8 Lifecycle). Used on the observed path (advance._lifecycle_after) and, after `release`
    has ended the current look, by cases.close_gone for the earlier looks' rows a second look kept booked."""
    db.run(conn, "UPDATE reservations SET state = 'DONE' WHERE run_id = :r AND case_id = :c AND state = ANY(:s)",
           r=run_id, c=case_id, s=list(OPEN_STATES))


def expire_holds(conn, run: dict, case_id: str | None = None) -> list[str]:
    """Hold expiry without a background job (spec §8): every read and write that touches a case, and advance-day,
    calls this first. HELD rows with expires_at <= now() become EXPIRED; their PENDING_APPROVAL cases return to OPEN
    on the same version with hold_expired set and a hold_expired log entry. Only the bookings and the approval round
    end: the version, the adopted assumptions and the history stay. Returns the case ids whose holds expired."""
    rs = db.rows(conn, "UPDATE reservations SET state = 'EXPIRED' WHERE run_id = :r AND state = 'HELD' "
                       "AND expires_at <= now() AND (CAST(:c AS text) = '' OR case_id = :c) RETURNING case_id, round",
                 r=run["run_id"], c=case_id or "")
    rounds = {}
    for r in rs:
        rounds[r["case_id"]] = max(rounds.get(r["case_id"], 0), r["round"])
    for cid in sorted(rounds):
        n = db.run(conn, "UPDATE cases SET state = 'OPEN', hold_expired = true WHERE run_id = :r AND case_id = :c "
                         "AND state = 'PENDING_APPROVAL' AND round = :n", r=run["run_id"], c=cid, n=rounds[cid])
        if n:
            db.log(conn, run["run_id"], run["day"], "hold_expired", cid, round=rounds[cid])
    return sorted(rounds)


def resources(eff: EngineInput, plan: Plan, caps: dict[str, int]) -> list[dict]:
    """'Resources to hold' (04-API-CONTRACT Approvals): needed against available for what this action type needs.
    `eff` and `caps` exclude this case's own bookings; Keep holds nothing."""
    if plan.kind == KEEP:
        return []
    rows = []
    if plan.kind == TRANSFER:
        p = eff.product
        totes = math.ceil(plan.qty / p.units_per_tote)
        rows += [{"kind": "DEMAND", "label": f"Unmet demand at {plan.dest}", "needed": plan.qty,
                  "available": caps.get("demand"), "unit": "units"},
                 {"kind": "STORAGE", "label": f"{p.zone.title()} storage at {plan.dest}", "needed": totes,
                  "available": caps["storage"] // p.units_per_tote if "storage" in caps else None, "unit": "totes"},
                 {"kind": "TRUCK", "label": f"Truck space on {plan.run_code}", "needed": totes,
                  "available": caps["truck"] // p.units_per_tote if "truck" in caps else None, "unit": "totes"},
                 {"kind": "SOURCE_LOT", "label": f"Units set aside from {eff.case_lot_id}", "needed": plan.qty,
                  "available": caps.get("stock"), "unit": "units"}]
    if plan.pct:
        rows.append({"kind": "LOT_PRICE", "label": f"Price change on {eff.case_lot_id}", "needed": 1,
                     "available": 0 if eff.case_lot_id in eff.bookings.lot_price else 1, "unit": "right"})
    return rows


def plan_resources(eff: EngineInput, plan: Plan) -> list[dict]:
    """`resources` with the transfer caps of the plan's run; [] when the run is not in today's input. A missing
    capacity day raises the engine's HorizonError for the caller to handle."""
    caps: dict[str, int] = {}
    if plan.kind == TRANSFER:
        run = plan_run(eff, plan)
        if run is None:
            return []
        caps, _ = transfer_caps(eff, run)
    return resources(eff, plan, caps)
