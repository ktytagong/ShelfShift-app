"""Advance day: one transaction under FOR UPDATE that moves all three worlds forward (spec §8 Demo clock and advance day).

shelfshift: the prepared actions. baseline_rule: what the store rule does each morning on its own records (04-08).
do_nothing: nothing. Every world sells the same hidden demand with the true price response, from PHYSICAL stock, and
each product's markdown schedule applies in every world. Money comes from `engine.world.step_day`; this module
stores what it returns and computes none itself (spec §8; contract-diff row 35).

The day start (03-CONTEXT D-21): at the 09:00 screen the decision day's 06:00 deliveries are lots, not inbound. One
function, `day_start`, moves them into `lots` (the record) and `truth_lots` (physical, from truth) in every world; the
template calls it on day 0 and `advance_day` calls it after the day moves on, so the world step never adds them again
(04-RESEARCH Pitfall 3).

A planned price change that is not deeper than the lot's running percent would make `step_day` refuse the whole day
(a repeated schedule step). It is VOIDED and logged before the step instead (Pitfall 4; contract-diff rows 17, 46).
A change equal to today's own schedule step is still passed: `step_day` accepts it and changes nothing.

Truth (physical stock, hidden demand, the hidden price response) is read here and by the template's checks only.
Worlds step in WORLDS order, products in sku order and rows in id order, so two sandboxes advanced from the same state
write identical rows.
"""
from __future__ import annotations

from engine import baseline_plan
from engine.adjust import effective
from engine.allocator import ShelfLot
from engine.model import KEEP, TRANSFER, Bookings, Inbound, Plan, Policy, Run
from engine.simulate import HorizonError
from engine.timing import first_sellable_day, markdown_effective_day
from engine.world import PriceChange, Shipment, step_day

from . import bookings, buildsteps, cases, config, db, inputs, overlay, refdata, screen
from .errors import ApiError

WORLDS = ("shelfshift", "baseline_rule", "do_nothing")
MOVED = "moved"  # the origin of a lot a delivered transfer created at its destination
MOVED_SEQ = 10**6  # a moved lot sells after the destination's own lots (as step_day numbers it)
LOT_COLUMNS = ("run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, markdown_pct, expiry_source, "
               "last_verified_day, verified_by, tag, origin, moved_from")
LOT_VALUES = ":r, :w, :l, :b, :s, :q, :e, :seq, :pct, :es, :lv, :vb, :tag, :o, :mf"
OUTCOME_COLUMNS = ("run_id", "world", "day", "branch", "sku", "demand", "sold", "wasted", "lost", "revenue",
                   "transport", "handling", "disposal")
OUTCOME_FIELDS = ("sold", "wasted", "lost", "revenue", "transport", "handling", "disposal")  # DayOutcome's dicts
NOT_PREPARED = "Approved but not prepared in time"
TODAY_ACTIONS = "((kind = 'TRANSFER' AND run_day = :d) OR (kind = 'MARKDOWN' AND effective_day = :d))"
# 05.2-06 D-37 B (user): owning every series of a public dataset would cost about 130 s on Cloud SQL, so a public
# workspace has no Next day; the synthetic set and the test fixture keep it
NEXT_DAY_PUBLIC = "Next day is not part of this dataset's workspaces"


def insert_lot(conn, run_id: str, world: str, lot: dict, physical: float) -> None:
    db.run(conn, f"INSERT INTO lots ({LOT_COLUMNS}) VALUES ({LOT_VALUES})", r=run_id, w=world, l=lot["lot_id"],
           b=lot["branch"], s=lot["sku"], q=lot["qty"], e=lot["expiry_day"], seq=lot["received_seq"],
           pct=lot["markdown_pct"], es=lot["expiry_source"], lv=lot["last_verified_day"], vb=lot.get("verified_by"),
           tag=lot.get("tag"), o=lot.get("origin"), mf=lot.get("moved_from"))
    db.run(conn, "INSERT INTO truth_lots (run_id, world, lot_id, qty) VALUES (:r, :w, :l, :q)",
           r=run_id, w=world, l=lot["lot_id"], q=physical)


LOT_FIELDS = tuple(LOT_COLUMNS.split(", "))


def insert_lots(conn, run_id: str, world: str, lots: list[tuple[dict, float]]) -> None:
    """insert_lot for many (lot, physical) pairs, in order, with the same values: lots, then truth_lots, through
    db.insert_rows (one multi-row INSERT per chunk; quick 261017-p52)."""
    db.insert_rows(conn, "lots", LOT_FIELDS, [
        (run_id, world, lot["lot_id"], lot["branch"], lot["sku"], lot["qty"], lot["expiry_day"], lot["received_seq"],
         lot["markdown_pct"], lot["expiry_source"], lot["last_verified_day"], lot.get("verified_by"), lot.get("tag"),
         lot.get("origin"), lot.get("moved_from")) for lot, _ in lots])
    db.insert_rows(conn, "truth_lots", ("run_id", "world", "lot_id", "qty"),
                   [(run_id, world, lot["lot_id"], physical) for lot, physical in lots])


def day_start(conn, run: dict) -> int:
    """Today's deliveries with no source case become lots in every world before anything reads the day (D-21).

    The record keeps the delivery's quantity; physical stock is truth's figure when it lists the id, else the record.
    Returns the number of rows moved (all worlds)."""
    run_id, day = run["run_id"], run["day"]
    truth = refdata.truth(run["dataset_version"])
    moved = 0
    for world in WORLDS:
        arriving = db.rows(conn, "SELECT * FROM inbound WHERE run_id = :r AND world = :w AND arrival_day = :d "
                                 "AND source_case_id IS NULL ORDER BY inbound_id", r=run_id, w=world, d=day)
        physical = truth.physical_of([i["inbound_id"] for i in arriving])  # these deliveries only (05.2 D-11)
        insert_lots(conn, run_id, world, [
            ({**i, "lot_id": i["inbound_id"], "markdown_pct": 0, "verified_by": "COUNT", "moved_from": None},
             physical.get(i["inbound_id"], i["qty"])) for i in arriving])
        db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND world = :w AND arrival_day = :d "
                     "AND source_case_id IS NULL", r=run_id, w=world, d=day)
        moved += len(arriving)
    return moved


def add_transfer(conn, run_id: str, world: str, action_id: str, case_id: str | None, lot_id: str, sku: str,
                 plan: Plan, trip: Run, policy: Policy, new_lot_id: str, origin: str) -> None:
    """A TRANSFER work order: the run's day and costs as quoted, the moved lot's first sellable day under `policy`,
    and `origin`, the branch the stock leaves from (never guessed; a CHECK constraint requires it)."""
    db.run(conn, "INSERT INTO world_actions (run_id, world, action_id, case_id, kind, lot_id, sku, qty, dest, run_code, "
                 "run_day, sellable_from, fixed_cost, per_unit_cost, cancel_cost, new_lot_id, origin) VALUES (:r, :w, "
                 ":a, :c, 'TRANSFER', :l, :s, :q, :d, :rc, :rd, :sf, :fc, :pu, :cc, :nl, :o)",
           r=run_id, w=world, a=action_id, c=case_id, l=lot_id, s=sku, q=float(plan.qty), d=plan.dest,
           rc=trip.run_code, rd=trip.day, sf=first_sellable_day(trip.day, trip.arrives_min, policy),
           fc=trip.fixed_cost, pu=trip.per_unit_cost, cc=trip.cancel_cost, nl=new_lot_id, o=origin)


def add_markdown(conn, run_id: str, world: str, action_id: str, case_id: str | None, lot_id: str, sku: str, pct: int,
                 effective_day: int) -> None:
    """A MARKDOWN work order (a price instruction) effective from `effective_day`."""
    db.run(conn, "INSERT INTO world_actions (run_id, world, action_id, case_id, kind, lot_id, sku, pct, effective_day) "
                 "VALUES (:r, :w, :a, :c, 'MARKDOWN', :l, :s, :p, :e)", r=run_id, w=world, a=action_id, c=case_id,
           l=lot_id, s=sku, p=pct, e=effective_day)


def baseline_candidates(conn, run: dict) -> list[dict]:
    """The store rule's lots this morning: baseline_rule lots with stock on its own record, never moved, with no
    baseline work order yet, whose ESL-02 window contains today; in the screen's order (expiry day, seed branch order,
    sku, lot_id). Stateless: nothing but the world's own rows decides (research A6)."""
    ref = refdata.get(run["dataset_version"])
    before = config.CASE_OPENING["days_before_first_step"]
    lots = db.rows(conn, "SELECT l.* FROM lots l WHERE l.run_id = :r AND l.world = 'baseline_rule' AND l.qty > 0 "
                         "AND l.moved_from IS NULL AND NOT EXISTS (SELECT 1 FROM world_actions a WHERE a.run_id = "
                         "l.run_id AND a.world = l.world AND a.lot_id = l.lot_id)", r=run["run_id"])
    order = {b: i for i, b in enumerate(ref.branches)}
    inside = [l for l in lots if screen.window(l["expiry_day"], ref.schedules[l["sku"]], before)[0] <= run["day"]
              <= l["expiry_day"]]
    return sorted(inside, key=lambda l: (l["expiry_day"], order[l["branch"]], l["sku"], l["lot_id"]))


def baseline_morning(conn, run: dict) -> list[str]:
    """What the store rule does this morning in the baseline_rule world (spec §8; research A6, A17): the same ESL-02
    screen on its own records, then, for each lot that qualifies, the engine's `baseline_plan` with the sandbox Policy,
    the same limits and today's snapshot, and no future data (a HorizonError skips the lot). Its transfers and
    markdowns become baseline work orders with the same field rules as prepare (origin = the lot's branch). Each
    transfer's set-aside, storage and truck space and its pending delivery count for the lots screened after it, as
    bookings and inbound would in the shelfshift world; storage within its zone and the delivery within its SKU.
    Returns the lot ids acted on."""
    ref = refdata.get(run["dataset_version"])
    run_id, day = run["run_id"], run["day"]
    policy = inputs.sandbox_policy(conn, run, ref)  # the sandbox Policy, as in the ShelfShift input (A17)
    storage: dict[str, list] = {}  # zone -> (dest, day, totes)
    trucks: list = []  # (run_code, totes) on today's runs
    set_aside: list = []  # (lot_id, units)
    priced: list = []  # lot ids whose price the rule changes
    pending: dict[str, list[Inbound]] = {}  # sku -> deliveries the rule sent today
    acted = []
    reads = inputs.ScreenReads(conn, run)  # safe: the loop writes only world_actions, which build_input never reads
    for lot in baseline_candidates(conn, run):
        sku, branch = lot["sku"], lot["branch"]
        product = ref.products[sku]
        reach = {branch} | {r.dest for r in inputs.case_runs(ref, branch, day, lot=lot, product=product, policy=policy)}
        extra = Bookings((), tuple(storage.get(product.zone, ())), tuple(trucks), tuple(set_aside), tuple(priced))
        arriving = tuple(i for i in pending.get(sku, ()) if i.branch in reach)
        try:
            inp = inputs.build_input(conn, run, lot["lot_id"], world="baseline_rule", extra=extra,
                                     extra_inbound=arriving, reads=reads)
            if not screen.screen(inp, config.CASE_OPENING)["qualifies"]:
                continue
            plan = baseline_plan(inp)
        except HorizonError:  # no future data: the rule cannot judge this lot today
            continue
        if plan.kind == KEEP:
            continue
        eff = effective(inp)
        if plan.kind == TRANSFER:
            trip = bookings.plan_run(eff, plan)
            held = bookings.holds_for(eff, plan)
            storage.setdefault(product.zone, []).extend(
                (plan.dest, d, int(q)) for k, _, d, q in held if k == "STORAGE")
            trucks.extend((plan.run_code, int(q)) for k, _, _, q in held if k == "TRUCK")
            set_aside.append((lot["lot_id"], float(plan.qty)))
            new_id = f"{lot['lot_id']}@{day}>{plan.dest}"
            pending.setdefault(sku, []).append(Inbound(new_id, plan.dest, float(plan.qty), trip.day, trip.arrives_min,
                                                       lot["expiry_day"], MOVED_SEQ))
            add_transfer(conn, run_id, "baseline_rule", f"d{day}:{lot['lot_id']}:transfer", None, lot["lot_id"], sku,
                         plan, trip, policy, new_id, branch)
        if plan.pct:
            priced.append(lot["lot_id"])
            add_markdown(conn, run_id, "baseline_rule", f"d{day}:{lot['lot_id']}:price", None, lot["lot_id"], sku,
                         plan.pct, markdown_effective_day(eff))
        acted.append(lot["lot_id"])
    return acted


def _void_repeated_price_changes(conn, run: dict, world: str, actions: list[dict], pct_by_lot: dict) -> list[dict]:
    """Today's price changes in order against each lot's running percent, as step_day checks them. One that does not
    lower the price is set VOIDED and logged instead of being passed (Pitfall 4). Returns the changes to pass."""
    running = dict(pct_by_lot)
    kept = []
    for a in actions:
        lot_id = a["lot_id"]
        if lot_id in running and a["pct"] <= running[lot_id]:
            db.run(conn, "UPDATE world_actions SET state = 'VOIDED' WHERE run_id = :r AND world = :w AND action_id = :a",
                   r=run["run_id"], w=world, a=a["action_id"])
            db.log(conn, run["run_id"], run["day"], "price_change_voided", a["case_id"], world=world,
                   action_id=a["action_id"], lot_id=lot_id, pct=a["pct"], lot_pct=running[lot_id])
            continue
        if lot_id in running:
            running[lot_id] = a["pct"]
        kept.append(a)
    return kept


def _by_sku(skus, rows) -> dict[str, list[dict]]:
    """Rows read for a product batch, grouped by sku in their read order (every sku of the batch, empty or not)."""
    out: dict[str, list[dict]] = {s: [] for s in skus}
    for r in rows:
        out[r["sku"]].append(r)
    return out


def step_world(conn, run: dict, world: str, truth: dict) -> None:
    """Step one world one day, product by product, through engine.world.step_day; store its output as returned.

    Hidden demand covers only the stores that carry the product (a public set's real series, 05.2 D-11). step_day
    still gets a demand for every store, 0.0 for a store without the product (it sells nothing there), and an
    outcome row is written only for the stores that carry the product that day: hidden demand, the product's lots,
    today's arrivals and every shipment's destination and origin. A nonzero outcome anywhere else raises; it is never
    dropped. On the synthetic set every store carries every product, so the rows are as before.

    Read and written per product batch (quick 261018-4nj; inputs.product_batches, in sku order): one read of each
    record table, physical stock, hidden demand (truth.demand_of_products, shared by the three worlds) and the batch's
    arrivals' physical stock, then every product steps in sku order exactly as it did alone, then one write per table
    (insert_rows for outcomes, world events and the rewritten lots). No product's writes touch another product's
    rows, and every SQL read keeps the per-product order (ORDER BY sku, then the old order), so the rows are the ones
    the per-product step wrote (tests/api/test_build_batching.py holds it to them)."""
    ref = refdata.get(run["dataset_version"])
    run_id, day = run["run_id"], run["day"]
    policy = inputs.sandbox_policy(conn, run, ref)  # one Policy per sandbox in every world (D-09; research A17)
    m = {20: truth.price_response["m20"], 30: truth.price_response["m30"]}
    seq = 0
    for skus in inputs.product_batches(ref):
        batch = list(skus)
        lot_rows = _by_sku(skus, db.rows(conn, "SELECT * FROM lots WHERE run_id = :r AND world = :w "
                                               "AND sku = ANY(:skus) ORDER BY sku, lot_id",
                                         r=run_id, w=world, skus=batch))
        ids = [l["lot_id"] for s in skus for l in lot_rows[s]]
        phys = {t["lot_id"]: t["qty"] for t in db.rows(conn, "SELECT lot_id, qty FROM truth_lots WHERE run_id = :r "
                                                             "AND world = :w AND lot_id = ANY(:ids)",
                                                       r=run_id, w=world, ids=ids)}
        actions_of = _by_sku(skus, db.rows(conn, f"SELECT * FROM world_actions WHERE run_id = :r AND world = :w "
                                                 f"AND sku = ANY(:skus) AND state = 'PLANNED' AND {TODAY_ACTIONS} "
                                                 f"ORDER BY sku, action_id", r=run_id, w=world, skus=batch, d=day))
        later_of: dict[str, dict] = {s: {} for s in skus}
        for a in db.rows(conn, "SELECT sku, lot_id, sum(qty) AS qty FROM world_actions WHERE run_id = :r "
                               "AND world = :w AND sku = ANY(:skus) AND state = 'PLANNED' AND kind = 'TRANSFER' "
                               "AND run_day > :d GROUP BY sku, lot_id", r=run_id, w=world, skus=batch, d=day):
            later_of[a["sku"]][a["lot_id"]] = a["qty"]
        # deliveries the day start did not move (none of today's 06:00 ones): sellable per the sandbox Policy
        arriving_of = _by_sku(skus, db.rows(conn, "SELECT * FROM inbound WHERE run_id = :r AND world = :w "
                                                  "AND sku = ANY(:skus) AND arrival_day = :d "
                                                  "AND source_case_id IS NULL ORDER BY sku, inbound_id",
                                            r=run_id, w=world, skus=batch, d=day))
        physical = truth.physical_of([i["inbound_id"] for s in skus for i in arriving_of[s]])
        truth.demand_of_products(skus, day)  # one read per (batch, day); each product's demand_for below reads none
        outcome_rows: list[tuple] = []
        event_rows: list[tuple] = []
        new_lots: list[tuple[dict, float]] = []
        moved: list[tuple[str, str]] = []  # (sku, new_lot_id) of every shipment
        transfer_cases: list[str] = []
        markdown_cases: list[str] = []
        for sku in skus:
            product = ref.products[sku]
            rows = {l["lot_id"]: l for l in lot_rows[sku]}
            actions = actions_of[sku]
            later = later_of[sku]
            lots = [ShelfLot(l["lot_id"], l["branch"], phys.get(l["lot_id"], l["qty"]), l["expiry_day"],
                             l["received_seq"], l["markdown_pct"], recorded=l["qty"],
                             reserved=later.get(l["lot_id"], 0.0))
                    for l in rows.values()]
            arriving = arriving_of[sku]
            arrivals = tuple(ShelfLot(i["inbound_id"], i["branch"], physical.get(i["inbound_id"], i["qty"]),
                                      i["expiry_day"], i["received_seq"],
                                      sellable_from=first_sellable_day(day, i["arrival_min"], policy),
                                      recorded=i["qty"]) for i in arriving)
            meta = {**rows, **{i["inbound_id"]: {**i, "lot_id": i["inbound_id"], "markdown_pct": 0,
                                                 "verified_by": "COUNT", "moved_from": None} for i in arriving}}
            shipments = []
            for a in (a for a in actions if a["kind"] == "TRANSFER"):
                shipments.append(Shipment(lot_id=a["lot_id"], qty=a["qty"], dest=a["dest"], new_lot_id=a["new_lot_id"],
                                          sellable_from=a["sellable_from"], fixed_cost=a["fixed_cost"],
                                          per_unit_cost=a["per_unit_cost"], cancel_cost=a["cancel_cost"],
                                          origin=a["origin"]))
                src = meta.get(a["lot_id"])  # a gone source lot is a failed delivery inside step_day: nothing arrives
                if src is not None:
                    meta[a["new_lot_id"]] = {**src, "lot_id": a["new_lot_id"], "branch": a["dest"],
                                             "received_seq": MOVED_SEQ, "markdown_pct": 0, "last_verified_day": day,
                                             "tag": None, "origin": MOVED, "moved_from": a["lot_id"]}
            markdowns = [a for a in actions if a["kind"] == "MARKDOWN"]
            markdowns = _void_repeated_price_changes(conn, run, world, markdowns, {l.lot_id: l.pct for l in lots})
            prices = tuple(PriceChange(a["lot_id"], a["pct"], day) for a in markdowns)
            hidden = truth.demand_for(sku, day)  # one product's day: the stores that carry it (05.2 D-11)
            demand = {b: hidden.get(b, 0.0) for b in ref.branches}  # a store without the product sells nothing
            # the stores that carry the product today, taken before step_day mutates lots
            carrying = (set(hidden) | {l.branch for l in lots} | {a.branch for a in arrivals}
                        | {sh.dest for sh in shipments} | {sh.origin for sh in shipments})
            tracked = [l for l in lots if ">" in l.lot_id]
            out = step_day(lots, day, demand, m, product, tuple(shipments), prices, arrivals,
                           schedule=ref.schedules[sku])
            for name in OUTCOME_FIELDS:  # an outcome outside the carrying set is a bug, never dropped silently
                for b, value in getattr(out, name).items():
                    if value and b not in carrying:
                        raise RuntimeError(f"{world} day {day} {sku}: {name} {value!r} at {b}, a store without the "
                                           f"product that day")
            outcome_rows.extend(
                (run_id, world, day, b, sku, demand[b], out.sold.get(b, 0.0), out.wasted.get(b, 0.0),
                 out.lost.get(b, 0.0), out.revenue.get(b, 0.0), out.transport.get(b, 0.0), out.handling.get(b, 0.0),
                 out.disposal.get(b, 0.0))
                for b in ref.branches if b in carrying)
            events = [("SHORT_SHIPMENT", s) for s in out.short_shipments]
            events += [("RECORD_CORRECTION", c) for c in out.record_corrections]
            shipped = {s["lot_id"]: s["shipped"] for s in out.short_shipments}
            events += [("TRANSFER_RECEIVED", {"lot_id": sh.new_lot_id, "branch": sh.dest,
                                              "qty": shipped.get(sh.lot_id, sh.qty)})
                       for sh in shipments if shipped.get(sh.lot_id, sh.qty) > 0]
            events += [("TRANSFER_EXPIRED", {"lot_id": l.lot_id, "branch": l.branch, "wasted": l.qty})
                       for l in tracked if l not in lots]
            for kind, payload in events:
                event_rows.append((run_id, world, day, seq, kind, db.js({**payload, "sku": sku})))
                seq += 1
            new_lots.extend(
                ({**meta[l.lot_id], "lot_id": l.lot_id, "branch": l.branch, "sku": sku, "qty": round(l.recorded, 6),
                  "expiry_day": l.expiry_day, "received_seq": l.received_seq, "markdown_pct": l.pct}, round(l.qty, 6))
                for l in lots)
            moved.extend((sku, sh.new_lot_id) for sh in shipments)
            for a in (a for a in actions if a["case_id"]):  # the set-aside left with the truck; the price is on the lot
                (transfer_cases if a["kind"] == "TRANSFER" else markdown_cases).append(a["case_id"])
        db.insert_rows(conn, "outcomes", OUTCOME_COLUMNS, outcome_rows)
        db.insert_rows(conn, "world_events", ("run_id", "world", "day", "seq", "kind", "payload"), event_rows,
                       casts={"payload": "jsonb"})
        db.run(conn, "DELETE FROM truth_lots WHERE run_id = :r AND world = :w AND lot_id = ANY(:ids)",
               r=run_id, w=world, ids=ids)
        db.run(conn, "DELETE FROM lots WHERE run_id = :r AND world = :w AND sku = ANY(:skus)",
               r=run_id, w=world, skus=batch)
        insert_lots(conn, run_id, world, new_lots)
        # a shipment became its moved lot today, whatever its arrival day: its pending delivery goes with it, so
        # the stock is never counted twice at the destination (WR-09); each moved id is paired with its own sku. A
        # dispatched transfer's delivery stays until its receipt removes it (06.1 D-34): it is still on its way, and
        # nothing has received it
        db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND world = :w AND sku = ANY(:skus) "
                     "AND (arrival_day <= :d OR (sku, inbound_id) IN "
                     "(SELECT * FROM unnest(CAST(:ms AS text[]), CAST(:mi AS text[])))) AND inbound_id NOT IN "
                     "(SELECT a.new_lot_id FROM world_actions a WHERE a.run_id = :r AND a.world = :w "
                     "AND a.state = 'DISPATCHED' AND a.new_lot_id IS NOT NULL)",
               r=run_id, w=world, skus=batch, d=day, ms=[s for s, _ in moved], mi=[i for _, i in moved])
        db.run(conn, f"UPDATE world_actions SET state = 'DONE' WHERE run_id = :r AND world = :w AND sku = ANY(:skus) "
                     f"AND state = 'PLANNED' AND {TODAY_ACTIONS}", r=run_id, w=world, skus=batch, d=day)
        for cases_done, kinds in ((transfer_cases, ["SOURCE_LOT", "TRUCK"]), (markdown_cases, ["LOT_PRICE"])):
            if cases_done:
                db.run(conn, "UPDATE reservations SET state = 'DONE' WHERE run_id = :r AND case_id = ANY(:cs) "
                             "AND kind = ANY(:k) AND state = 'COMMITTED'", r=run_id, cs=sorted(set(cases_done)),
                       k=kinds)


def _lifecycle_before(conn, run: dict) -> list[str]:
    """Only PREPARED plans execute (spec §8 Prepare and dispatch). At advance-day every APPROVED case has missed its
    prepare deadline: a transfer's latest_decision_at is on its run day (today's runs only), and a markdown's is the
    end of the demo day. It becomes INVALIDATED, and everything it holds or booked (its bookings, its inbound delivery,
    its set-aside and its price right) is released; the next morning it returns to OPEN with re_review and a new
    version (cases_morning). Returns the invalidated case ids."""
    out = []
    for case in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'APPROVED' ORDER BY case_id",
                        r=run["run_id"]):
        rec = cases.active_rec(conn, case)
        bookings.release(conn, run["run_id"], case["case_id"])
        db.run(conn, "UPDATE cases SET state = 'INVALIDATED', reason = :why WHERE run_id = :r AND case_id = :c",
               why=NOT_PREPARED, r=run["run_id"], c=case["case_id"])
        db.log(conn, run["run_id"], run["day"], "invalidated", case["case_id"], reason=NOT_PREPARED,
               plan=rec["result"]["recommended"] if rec else None, round=case["round"])
        out.append(case["case_id"])
    return out


def _lifecycle_after(conn, run: dict) -> None:
    """After the worlds stepped and the outcomes are written (spec §8 Lifecycle by action type): a PREPARED case with
    no PLANNED or DISPATCHED work order left is EXECUTED (06.1 D-34); an EXECUTED case whose lot and moved lots have
    no stock left, on the record or physically, is OBSERVED and then CLOSED at the end of this day, its remaining
    bookings (the storage the moved stock took) DONE; so are the earlier look's bookings of a REJECTED second look
    once its lot is gone (WR-07). A case that never executed and whose lot is gone closes through cases.close_gone,
    which releases what it held (also on the last demo day, when no morning follows)."""
    run_id, day = run["run_id"], run["day"]
    for case in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'PREPARED' ORDER BY case_id",
                        r=run_id):
        # 06.1 D-34: a dispatched transfer waits for the receipt a person records (api/execution.receipt); no day
        # passing executes or receives it
        left = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                            "AND case_id = :c AND state IN ('PLANNED', 'DISPATCHED')", r=run_id, c=case["case_id"])["n"]
        if left == 0:
            db.run(conn, "UPDATE cases SET state = 'EXECUTED' WHERE run_id = :r AND case_id = :c",
                   r=run_id, c=case["case_id"])
            db.log(conn, run_id, day, "executed", case["case_id"], round=case["round"], simulated=True)
    for case in db.rows(conn, f"SELECT * FROM cases c WHERE c.run_id = :r AND c.state = 'EXECUTED' "
                              f"AND {cases.LOT_GONE} ORDER BY c.case_id", r=run_id):
        db.run(conn, "UPDATE cases SET state = 'OBSERVED' WHERE run_id = :r AND case_id = :c", r=run_id, c=case["case_id"])
        db.log(conn, run_id, day, "observed", case["case_id"], lot_id=case["lot_id"], simulated=True)
        bookings.done(conn, run_id, case["case_id"])
        db.run(conn, "UPDATE cases SET state = 'CLOSED' WHERE run_id = :r AND case_id = :c", r=run_id, c=case["case_id"])
        db.log(conn, run_id, day, "closed", case["case_id"], lot_id=case["lot_id"], from_state="OBSERVED")
    # a second look that was rejected kept what the earlier look's plan still occupies (bookings.release); those
    # rows end DONE once the lot and the stock it moved are gone, as on the observed path (WR-07)
    for case in db.rows(conn, f"SELECT * FROM cases c WHERE c.run_id = :r AND c.state = 'REJECTED' "
                              f"AND c.look_round > 0 AND {cases.LOT_GONE} AND EXISTS (SELECT 1 FROM reservations x "
                              f"WHERE x.run_id = c.run_id AND x.case_id = c.case_id AND x.state = ANY(:s)) "
                              f"ORDER BY c.case_id", r=run_id, s=list(bookings.OPEN_STATES)):
        bookings.done(conn, run_id, case["case_id"])
    cases.close_gone(conn, run)  # a case that never executed closes at the end of the day its lot went, released


def advance_day(conn, run_id: str, expected_version: int) -> dict:
    """Spec §8 order, in the caller's transaction. A stale expected_version answers the current state unchanged.

    A workspace (SANDBOX) first takes its own copy of every series it still reads through its template
    (overlay.materialize_all), so everything below reads and writes its own rows; a workspace of a public dataset is
    refused with 409 not_available before anything is written (05.2 D-09, D-37 B)."""
    run = db.lock_run(conn, run_id, mode="UPDATE", statuses=("ACTIVE", "INITIALIZING"))
    if run["version"] != expected_version:
        return {"advanced": False, "day": run["day"], "version": run["version"]}
    ref = refdata.get(run["dataset_version"])
    if run["kind"] == "SANDBOX" and getattr(ref, "kind", "synthetic") == "public":  # 05.2 D-37 B (user)
        raise ApiError(409, "not_available", NEXT_DAY_PUBLIC)
    if run["day"] > ref.calendar["last_demo_day"]:
        raise ApiError(409, "demo_complete", "Demo complete · Reset to start again")
    # a template build's run prints one line per step (api/buildsteps.py, quick 261018-4nj); any other run prints none
    with buildsteps.step(run, "lifecycle_before"):
        overlay.materialize_all(conn, run)  # a workspace owns every series before its day steps (05.2 D-09)
        truth = refdata.truth(run["dataset_version"])
        bookings.expire_holds(conn, run)  # first, so a hold whose hour is up never rides into the new day (spec §8)
        _lifecycle_before(conn, run)  # only PREPARED plans execute
    with buildsteps.step(run, "baseline_morning"):
        baseline_morning(conn, run)  # the store rule acts on its own records before its world steps
    for world in WORLDS:
        with buildsteps.step(run, f"step_world {world}"):
            step_world(conn, run, world, truth)
    with buildsteps.step(run, "lifecycle_after"):
        _lifecycle_after(conn, run)  # executed, observed and closed, after the outcomes are written
    # an unanswered or unconfirmed count request due by today ends with the day; it never blocked anything (04-09)
    db.run(conn, "UPDATE evidence_requests SET state = 'EXPIRED' WHERE run_id = :r AND state IN ('OPEN', 'ANSWERED') "
                 "AND due_day <= :d", r=run_id, d=run["day"])
    db.log(conn, run_id, run["day"], "day_advanced", to_day=run["day"] + 1)
    run = db.one(conn, "UPDATE demo_runs SET day = day + 1, version = version + 1 WHERE run_id = :r RETURNING *",
                 r=run_id)
    with buildsteps.step(run, "day_start"):  # labelled with the new day
        day_start(conn, run)
    if run["day"] <= ref.last_review_day:  # the only morning that reopens a waiting case (G-05-13)
        with buildsteps.step(run, "cases_morning"):
            cases_morning(conn, run)
    return {"advanced": True, "day": run["day"], "version": run["version"]}


def cases_morning(conn, run: dict) -> None:
    """The case part of a new demo day, after the day start (spec §8 Advance day): WAITING cases due today reopen,
    a case whose lot is gone closes before anything reads it, live cases re-evaluate (an INVALIDATED one first
    returns to OPEN with re_review), executed cases still at risk get their second look, then the ESL-02 screen opens
    new cases."""
    run_id = run["run_id"]
    # a case that stops waiting no longer has a wait reason (WR-05: evaluate_case never writes reason)
    db.run(conn, "UPDATE cases SET state = 'OPEN', reason = NULL WHERE run_id = :r AND state = 'WAITING' "
                 "AND waiting_until_day <= :d", r=run_id, d=run["day"])
    cases.close_gone(conn, run)
    for case in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = ANY(:s) ORDER BY case_id",
                        r=run_id, s=list(cases.REEVALUATED)):
        if case["state"] == "INVALIDATED":
            db.run(conn, "UPDATE cases SET state = 'OPEN', re_review = true, reason = NULL WHERE run_id = :r "
                         "AND case_id = :c", r=run_id, c=case["case_id"])
            case = {**case, "state": "OPEN", "re_review": True, "reason": None}
        cases.evaluate_case(conn, run, case)
    cases.second_looks(conn, run)  # an executed case still at risk reopens from the data (spec §8 Second look)
    cases.open_cases(conn, run)
