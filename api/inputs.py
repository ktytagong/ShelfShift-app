"""Build the raw EngineInput for one case from Postgres (spec §6 Engine input, spec §8 Recheck contract).

The input holds the RECORD (table `lots`), never `truth_lots`: physical stock, hidden demand and the hidden price
response never enter an EngineInput. Bookings are other cases' quantities, COMMITTED plus HELD that has not expired,
so an expired hold never counts even before anything cleaned it up (spec §8 Availability). `exclude_case` leaves out
that case's own bookings, its own committed delivery and, on a second look, only the current look's rounds: storage
still taken by stock an earlier look moved keeps counting.

What goes in, and from where (contract-diff rows 6, 13, 15, 24, 29-34; IN-01):
- runs: only the runs leaving the source today (03-CONTEXT D-24, intentional and recorded); for a public set only
  those that pass stage 1 of the local candidate network for this lot (api/eligibility.py; 05.2 D-27, D-29..D-31);
- lots: the case lot's SKU at the source and at today's destinations, from the record; today's 06:00 deliveries are
  already lots after the day start (D-21), so inbound holds later arrivals and committed transfers only;
- policy: the seeded Policy (every field of seed.json `policy`) overlaid with this sandbox's `run_policy` rows, every
  field passed by keyword, so a missing or unknown field is an error, never a silent default;
- markdown_schedule: the product's schedule, as seeded;
- sales_yesterday: the reference history before the first day, the outcome rows afterwards; a branch with no row is
  left out (unknown is not zero, row 15);
- reported facts: confirmed facts valid today that apply to this input, with every number a float or int (row 11); a
  fact whose adjustments carry their own days (capacity, a demand multiplier) from the day it is confirmed until its
  range ends, so a demand event next week changes today's plan on those days (07-13 review T1); a confirmed printed
  expiry date older than yesterday is clamped to yesterday, the engine's floor, so the lot still reads expired
  (`facts_in_effect`; 07 review B); a replaced fact never, and a zone fact only on its zone (07-13);
- overrides: what the caller passes (the adopted assumptions still in effect, `adopted_in_effect`);
- an unconfirmed report (`Unconfirmed`, the impact preview only): the input as the report's confirmation would leave
  today's data, read only: its fact in effect after every confirmed one, the fact a correction replaces left out, the
  record a count corrects, and the bookings of the pending approvals the same confirmation cancels first left out
  (07-15 review P1, P2, P4).

In a workspace the lots, deliveries and outcomes are read through api/overlay.py: its own rows for a series it has
written, its template's otherwise (05.2 D-08..D-10).

Only the raw input is returned: callers apply `engine.adjust.effective` themselves where they read effective values,
and store, cache and export only this raw form (rows 6, 26).
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal

from engine.adjust import KINDS, AdjustmentError, validate
from engine.model import (ENGINE_VERSION, Bookings, Capacity, EngineInput, Forecast, Inbound, Lot, Policy, Run)

from . import db, eligibility, overlay, refdata

LIVE = "(state = 'COMMITTED' OR (state = 'HELD' AND expires_at > now()))"  # a booking that counts right now
# a case's input leaves out only the deliveries that case created; with no case, every delivery counts, scheduled
# supplier deliveries (no source case) included (quick 261017-ofn)
INBOUND_NOT_OWN = "(CAST(:c AS text) IS NULL OR source_case_id IS DISTINCT FROM :c)"
# adjustment kinds that carry their own business days (engine.adjust.KINDS: day_from..day_to; capacity and
# demand_multiplier), which the engine applies to those days only (D-16)
DAY_RANGED = frozenset(kind for kind, spec in KINDS.items() if "day_from" in spec)


@dataclass(frozen=True)
class Unconfirmed:
    """A report as its confirmation would leave today's data, for the impact preview (api/impact.py; 07-CONTEXT D-16,
    07-15 review P1, P2, P4). Nothing is written: `build_input` only reads it.

    - `fact`: the report's fact {adjustments, sku, zone, valid_from, valid_to}, in effect after every confirmed fact
      (it would be confirmed last) by `adjustments_in_effect`'s rule;
    - `replaced`: the fact a correction replaces (facts.replace sets its replaced_by), never in effect;
    - `counted`: ((lot_id, units), ...), the record a count corrects (facts.correct_record: the lot's qty, and
      last_verified_day the business day), read as corrected, so a lot counted at zero leaves the input as it would;
    - `released`: ((case_id, look_round), ...), the cases whose pending approval the same confirmation cancels before
      this input is built: their current look's bookings read as released (bookings.release)."""
    fact: dict | None = None
    replaced: str | None = None
    counted: tuple[tuple[str, float], ...] = ()
    released: tuple[tuple[str, int], ...] = ()

# The template build's batched reads and writes (quick 261018-4nj): products are read and written PRODUCT_BATCH at a
# time, in sku order, so a read holds at most one batch's rows (05.2 D-11's bound) and the round trips no longer grow
# with the number of products. An infrastructure constant, like publicref.SERIES_CACHE, never a retailer value.
PRODUCT_BATCH = 64


def product_batches(ref) -> list[tuple[str, ...]]:
    """sorted(ref.products) in consecutive tuples of at most PRODUCT_BATCH skus, in order."""
    skus = sorted(ref.products)
    size = max(1, int(PRODUCT_BATCH))
    return [tuple(skus[i:i + size]) for i in range(0, len(skus), size)]


def plain(value):
    """A value from the database with every Decimal turned into a float, at any depth (contract-diff row 11).

    pg8000 returns numeric (and aggregates over it) as Decimal; the engine's direct `validate` needs a real float
    or int, so nothing numeric goes into a fact or an override before it passes through here."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(plain(v) for v in value)
    return value


def sandbox_policy(conn, run: dict, ref: refdata.Ref | None = None) -> Policy:
    """The sandbox's Policy: the seeded Policy with this run's run_policy rows applied, every field by keyword.

    The one Policy of a sandbox (04-CONTEXT D-09; research A17): the ShelfShift input, the world step and the store
    rule's world all read it here, so a settings write reaches all three alike."""
    ref = ref or refdata.get(run["dataset_version"])
    values = {f.name: getattr(ref.policy, f.name) for f in fields(Policy)}
    for row in db.rows(conn, "SELECT field, value FROM run_policy WHERE run_id = :r ORDER BY field", r=run["run_id"]):
        if row["field"] not in values:
            raise ValueError(f"run_policy field {row['field']!r} is not an engine Policy field")
        values[row["field"]] = plain(row["value"])
    return Policy(**values)


policy = sandbox_policy  # the earlier name, kept for callers that read it


def case_runs(ref: refdata.Ref, source: str, day: int, lot: dict | None = None, product=None,
              policy: Policy | None = None) -> list[Run]:
    """The runs a case input holds, in reference order. The synthetic set: every run leaving `source` today
    (03-CONTEXT D-24: today's runs only). A public set: those that pass stage 1 for this lot (`lot` its record row,
    `product` and the sandbox `policy`; api/eligibility.py), so an unmapped store gets none (05.2 D-27, D-29..D-31)."""
    if ref.kind != "public":
        return ref.runs_from(source, day)
    if lot is None or product is None or policy is None:
        raise ValueError("a public set's case runs pass stage 1, which needs the lot, its product and the policy")
    return eligibility.eligible_runs(ref, {**lot, "branch": source}, product, day, policy)[0]


def booking_rows(conn, run_id: str, exclude_case: str | None, look_round: int = 0,
                 released: tuple[tuple[str, int], ...] = ()) -> list[dict]:
    """The run's live bookings summed by (kind, bucket, day), without this case's rounds above its look_round and
    without each `released` (case_id, look_round)'s rounds above its look_round (an impact preview's, `bookings`)."""
    gone = "".join(f"AND NOT (case_id = :gc{i} AND round > :gn{i}) " for i in range(len(released)))
    extra = {**{f"gc{i}": c for i, (c, _) in enumerate(released)}, **{f"gn{i}": n for i, (_, n) in enumerate(released)}}
    return db.rows(conn, f"SELECT kind, bucket, day, sum(qty) AS qty FROM reservations WHERE run_id = :r "
                         f"AND NOT (case_id = :c AND round > :n) {gone}AND {LIVE} "
                         f"GROUP BY kind, bucket, day ORDER BY kind, bucket, day",
                   r=run_id, c=exclude_case or "", n=look_round, **extra)


def bookings(conn, run_id: str, exclude_case: str | None, sku: str, zone: str, day: int, lot_ids: set[str],
             look_round: int = 0, rows: list[dict] | None = None,
             released: tuple[tuple[str, int], ...] = ()) -> Bookings:
    """Other cases' live bookings that touch this input; this case's rounds above its look_round are left out, and so
    are those of each `released` (case_id, look_round): a pending approval an unconfirmed report's confirmation would
    cancel first (`Unconfirmed`; bookings.release ends the current look's rounds).
    `rows`: `booking_rows` already read for the same run, case and look_round (a morning screen's ScreenReads, which
    never carries `released`: a ScreenReads and an unconfirmed report never meet, build_input)."""
    if rows is not None and released:
        raise ValueError("shared booking rows are read without released rounds: pass one or the other")
    rs = booking_rows(conn, run_id, exclude_case, look_round, released) if rows is None else rows
    demand, totes, run_totes, lot_units, lot_price = [], [], [], [], []
    for r in rs:
        a, _, b = r["bucket"].partition("|")
        qty = float(r["qty"])
        if r["kind"] == "DEMAND" and b == sku:
            demand.append((a, r["day"], qty))
        elif r["kind"] == "STORAGE" and b == zone:
            totes.append((a, r["day"], int(round(qty))))
        elif r["kind"] == "TRUCK" and b.isdigit() and int(b) == day:
            run_totes.append((a, int(round(qty))))
        elif r["kind"] == "SOURCE_LOT" and a in lot_ids:
            lot_units.append((a, qty))
        elif r["kind"] == "LOT_PRICE" and a in lot_ids:
            lot_price.append(a)
    return Bookings(tuple(demand), tuple(totes), tuple(run_totes), tuple(lot_units), tuple(lot_price))


def _expired_floor(adj: dict, probe: EngineInput) -> dict:
    """A confirmed printed expiry date (`expiry`) older than yesterday, clamped to yesterday: the engine's own floor
    for an expiry adjustment (engine/adjust.py EXPIRY_DAY), which still reads the lot as expired. Dropped instead, the
    lot would read its recorded estimate again once the printed date is two days old (07 review B; 07-CONTEXT D-11,
    D-21). Only the input changes: the stored fact keeps the printed date, and the engine is untouched. Anything else
    is returned as it is."""
    floor = probe.today - 1
    day = adj.get("expiry_day")
    if adj.get("kind") == "expiry" and isinstance(day, int) and not isinstance(day, bool) and day < floor:
        return {**adj, "expiry_day": floor}
    return adj


def fact_rows(conn, run_id: str, day: int, replaced: str | None = None) -> list[dict]:
    """The run's confirmed reported facts whose range has not ended by `day` and that no later correction replaced
    (adjustments, sku, zone, valid_from, valid_to), in confirmation order: what `facts_in_effect` judges, one read per
    run and day (a morning screen's ScreenReads shares it across lots; quick 261017-nhi, 07-13). `replaced` names a
    fact an unconfirmed correction would replace: it is left out as if replaced already (the impact preview, 07-15
    review P1)."""
    left_out = " AND fact_id <> :x" if replaced is not None else ""
    return db.rows(conn, "SELECT adjustments, sku, zone, valid_from, valid_to FROM reported_facts WHERE run_id = :r "
                         "AND valid_to >= :d AND replaced_by IS NULL" + left_out + " ORDER BY confirmed_at, fact_id",
                   r=run_id, d=day, **({"x": replaced} if replaced is not None else {}))


def facts_in_effect(conn, run_id: str, day: int, sku: str, probe: EngineInput,
                    rows: list[dict] | None = None, replaced: str | None = None) -> tuple[dict, ...]:
    """Confirmed reported facts that apply to this input today (other branches, runs or lots are skipped): a fact
    valid today, and an adjustment with its own days (`DAY_RANGED`: capacity, a demand multiplier) of a fact whose
    range has not ended, even when it starts later, since the engine applies it to those days only and they may lie
    inside today's horizon (07-13 review T1). Facts without their own days (counts, printed dates, transport on a run)
    keep the valid-today rule: a run code names no day. Every number is converted from Decimal first, so the engine's
    direct `validate` sees a float (row 11). A printed expiry date older than yesterday is clamped to yesterday
    (`_expired_floor`), never skipped (07 review B). A fact a later correction replaced (`replaced_by`) is never in
    effect, so two corrections never stack, and a fact about one storage zone (`zone`, a CAPACITY report) applies only
    to an input whose product is in that zone (07-CONTEXT D-15; research F-9). `replaced` names a fact an unconfirmed
    correction would replace: it is left out as if replaced already (the impact preview, 07-15 review P1).
    `rows`: `fact_rows` already read for the same run and day (a morning screen's ScreenReads, which never carries
    `replaced`)."""
    if rows is not None and replaced is not None:
        raise ValueError("shared fact rows are read without a replaced fact: pass one or the other")
    out = []
    for f in fact_rows(conn, run_id, day, replaced) if rows is None else rows:
        out += adjustments_in_effect(f, day, sku, probe)
    return tuple(out)


def adjustments_in_effect(fact: dict, day: int, sku: str, probe: EngineInput) -> list[dict]:
    """The adjustments of one fact ({adjustments, sku, zone, valid_from, valid_to}) that reach this input today, by
    `facts_in_effect`'s rule: none when its range has ended or its SKU or zone is another; an adjustment without its
    own days only once the fact is valid; a printed date older than yesterday clamped (`_expired_floor`); one the
    engine refuses on this input skipped. The impact preview judges a report's adjustments by this same rule, so the
    preview and the applied result agree (07-CONTEXT D-16; 07-15)."""
    if fact["valid_to"] < day or fact["sku"] not in (None, sku) or fact["zone"] not in (None, probe.product.zone):
        return []
    out = []
    for adj in plain(fact["adjustments"]):
        if fact["valid_from"] > day and adj.get("kind") not in DAY_RANGED:
            continue  # not valid yet, and nothing in it names the days it is about
        adj = _expired_floor(adj, probe)
        try:
            validate(adj, probe)
        except AdjustmentError:
            continue
        out.append(adj)
    return out


def adopted_in_effect(adopted, runs, branches) -> tuple[tuple[dict, ...], list[dict]]:
    """(the overrides still in effect, the adopted items that ended) for today's input (round-3 CR-01, row 9).

    Each stored item is {override, trip: {run_code, day} | null, branch | null, note, adopted_day}. An item with a
    trip stays only while a run with that code on that day is in today's input; one with a branch only while that
    branch is in today's input; a product-level item (markdown_response, disposal_cost: neither) always stays."""
    on_runs = {(r.run_code, r.day) for r in runs}
    branches = set(branches)
    kept, ended = [], []
    for item in adopted or ():
        trip, branch = item.get("trip"), item.get("branch")
        if trip is not None:
            live = (trip["run_code"], trip["day"]) in on_runs
        elif branch is not None:
            live = branch in branches
        else:
            live = True
        (kept if live else ended).append(item)
    return tuple(plain(i["override"]) for i in kept), ended


class ScreenReads:
    """The reads one morning screen shares across its lots: one query per (table, world, product) for the record
    tables and, on a public set, one per product for the forecast and yesterday's sales, instead of about 14 round
    trips per lot (quick 261017-nhi).

    The safety rule: one ScreenReads serves ONE morning screen, inside ONE transaction, whose loop writes nothing
    `build_input` reads when exclude_case is None (lots, inbound, outcomes, run_policy, the look_round of case '',
    reservations, reported_facts). That holds for cases.open_cases (it writes cases, the log, recommendations, the
    engine cache and the case counter) and advance.baseline_morning (it writes only world_actions), and only for them.
    Never second_looks (it rewrites look_round before each build), evaluate_case or a route. A live hold's
    `expires_at > now()` reads the transaction's start time, so a memo inside one transaction is exact.

    Every per-product read goes through overlay.everywhere (the one read rule, 05.2 D-09), and build_input filters
    branches in Python; the SQL keeps the row order and Python never re-sorts. Only the record tables are read here
    (lots, inbound, outcomes): physical stock (truth_lots) never is. Everything is freed with the object."""

    ORDER = {"lots": "lot_id", "inbound": "inbound_id", "outcomes": "branch"}  # each read's order, as build_input's

    def __init__(self, conn, run: dict):
        self.conn, self.run = conn, run
        self.ref = refdata.get(run["dataset_version"])
        self.memo: dict = {}
        self.lot_rows: dict[tuple[str, str], dict] = {}  # (world, lot_id) -> the record row the run reads

    def get(self, key, load):
        """`load()` once per key for this screen."""
        if key not in self.memo:
            self.memo[key] = load()
        return self.memo[key]

    def product(self, table: str, world: str, sku: str, where: str = "", **params) -> list[dict]:
        """Every row of `table` for one product in `world`, at every branch, as the run reads them; `where` is a code
        literal (never a request's text) over the table's columns, its values bound from `params`."""
        if table not in overlay.SERIES_TABLES:
            raise ValueError(f"{table!r} is not a record series table")

        def load():
            read, p = self.get(("everywhere", table), lambda: overlay.everywhere(self.conn, self.run, table))
            rows = db.rows(self.conn, f"SELECT * FROM {table} WHERE {read} AND {table}.world = :pw "
                                      f"AND {table}.sku = :ps {where} ORDER BY {self.ORDER[table]}",
                           pw=world, ps=sku, **p, **params)
            if table == "lots":
                self.lot_rows.update(((world, r["lot_id"]), r) for r in rows)
            return rows
        return self.get((table, world, sku, where, tuple(sorted(params.items()))), load)

    def lot(self, world: str, lot_id: str) -> dict | None:
        """overlay.lot through the product reads: the first lot of a product reads that product's lots."""
        found = self.lot_rows.get((world, lot_id))
        if found is not None:
            return found
        row = overlay.lot(self.conn, self.run, world, lot_id)
        if row is not None:
            self.product("lots", world, row["sku"])
        return row

    def forecast(self, day: int, store: str, sku: str) -> dict:
        """ref.forecast(day, store, sku); a public set reads the whole product once (a new dict each call)."""
        if self.ref.kind != "public":
            return self.ref.forecast(day, store, sku)
        return dict(self.get(("fc", day, sku), lambda: self.ref.forecast_of_product(day, sku)).get(store, {}))

    def sale(self, store: str, sku: str, day: int):
        """ref.sale(store, sku, day); a public set reads the whole product once."""
        if self.ref.kind != "public":
            return self.ref.sale(store, sku, day)
        return self.get(("sale", sku, day), lambda: self.ref.sales_of_product(sku, day)).get(store)


def build_input(conn, run: dict, lot_id: str, world: str = "shelfshift", exclude_case: str | None = None,
                overrides: tuple[dict, ...] = (), extra: Bookings | None = None,
                extra_inbound: tuple[Inbound, ...] = (), reads: ScreenReads | None = None,
                unconfirmed: Unconfirmed | None = None) -> EngineInput:
    """The raw EngineInput for `lot_id` on the run's day. LookupError when the lot is not on the record.

    `reads`: a morning screen's ScreenReads (cases.open_cases and advance.baseline_morning only); the input is the
    same with or without it (tests/api/test_inputs.py). exclude_case None (a morning screen, the store rule, casebench)
    leaves nothing out, so every delivery in the window counts.
    `unconfirmed` (the impact preview only): the input as that report's confirmation would leave today's data. The two
    never meet: a morning screen previews nothing, and a preview reads today's data on its own (ValueError if both)."""
    pending = unconfirmed or Unconfirmed()
    if reads is not None and pending != Unconfirmed():
        raise ValueError("a ScreenReads serves a morning screen, never an impact preview")
    ref = refdata.get(run["dataset_version"])
    day, run_id = run["day"], run["run_id"]
    if reads is not None and (reads.run["run_id"], reads.run["day"]) != (run_id, day):
        raise ValueError("a ScreenReads serves one run on one day: this one was made for another")
    if reads is None:
        case_lot = overlay.lot(conn, run, world, lot_id)  # world rows read through the template (05.2 D-09)
    else:
        case_lot = reads.lot(world, lot_id)
    if case_lot is None:
        raise LookupError(f"lot {lot_id} is gone")
    sku, source = case_lot["sku"], case_lot["branch"]
    product = ref.products[sku]
    if reads is None:
        policy = sandbox_policy(conn, run, ref)
    else:
        policy = reads.get("policy", lambda: sandbox_policy(conn, run, ref))
    runs = case_runs(ref, source, day, lot=case_lot, product=product, policy=policy)
    branches = sorted({source} | {r.dest for r in runs})
    inside = set(branches)
    counted = dict(pending.counted)
    if reads is None:
        lots_in, p = overlay.series_filter(conn, run, world, sku, branches, "lots")
        also = " OR lot_id = ANY(:counted_ids)" if counted else ""
        lot_rows = db.rows(conn, f"SELECT * FROM lots WHERE {lots_in} AND (qty > 0 OR lot_id = :l{also}) ORDER BY lot_id",
                           l=lot_id, **({"counted_ids": sorted(counted)} if counted else {}), **p)
    else:  # the same rows the series query returns, in its order (a ScreenReads carries no counted lot)
        lot_rows = [l for l in reads.product("lots", world, sku)
                    if l["branch"] in inside and (l["qty"] > 0 or l["lot_id"] == lot_id)]
    if counted:  # the record as the count's confirmation corrects it (facts.correct_record), read only
        lot_rows = [{**l, "qty": counted[l["lot_id"]], "last_verified_day": day} if l["lot_id"] in counted else l
                    for l in lot_rows]
        lot_rows = [l for l in lot_rows if l["qty"] > 0 or l["lot_id"] == lot_id]
    lots = tuple(Lot(l["lot_id"], l["branch"], float(l["qty"]), l["expiry_day"], l["received_seq"],
                     l["markdown_pct"], l["expiry_source"], l["last_verified_day"]) for l in lot_rows)
    if reads is None:
        inbound_in, p = overlay.series_filter(conn, run, world, sku, branches, "inbound")
        inb_rows = db.rows(conn, f"SELECT * FROM inbound WHERE {inbound_in} AND arrival_day >= :d "
                                 f"AND arrival_day <= :e AND {INBOUND_NOT_OWN} ORDER BY inbound_id",
                           d=day, e=case_lot["expiry_day"], c=exclude_case, **p)
    else:
        inb_rows = [i for i in reads.product("inbound", world, sku, "AND arrival_day >= :d AND " + INBOUND_NOT_OWN,
                                             d=day, c=exclude_case)
                    if i["branch"] in inside and i["arrival_day"] <= case_lot["expiry_day"]]
    inbound = tuple(Inbound(i["inbound_id"], i["branch"], float(i["qty"]), i["arrival_day"], i["arrival_min"],
                            i["expiry_day"], i["received_seq"]) for i in inb_rows) + tuple(extra_inbound)
    end = max([day] + [l.expiry_day for l in lots] + [i.expiry_day for i in inbound])
    forecast, sale = (ref.forecast, ref.sale) if reads is None else (reads.forecast, reads.sale)
    points = {b: forecast(day, b, sku) for b in branches}  # one series each (05.2 D-11)
    forecasts = tuple(Forecast(b, d, *points[b][d]) for b in branches for d in range(day, end + 1) if d in points[b])
    free = {(dest, d): ref.free_totes(dest, product.zone, d) for dest in sorted({r.dest for r in runs})
            for d in range(day, end + 1)}
    capacity = tuple(Capacity(dest, d, n) for (dest, d), n in free.items() if n is not None)
    sales = []
    if day - 1 >= 0:
        if reads is None:
            outcomes_in, p = overlay.series_filter(conn, run, world, sku, branches, "outcomes")
            sold = {o["branch"]: o["sold"] for o in db.rows(conn, f"SELECT branch, sold FROM outcomes "
                                                                  f"WHERE {outcomes_in} AND day = :d", d=day - 1, **p)}
        else:
            sold = {o["branch"]: o["sold"] for o in reads.product("outcomes", world, sku, "AND day = :d", d=day - 1)
                    if o["branch"] in inside}
    for b in branches:  # a branch with no row is unknown, not zero: it is left out (row 15)
        if day - 1 < 0:
            recorded = sale(b, sku, day - 1)
        else:
            recorded = round(float(sold[b]), 2) if b in sold else None
        if recorded is not None:
            sales.append((b, float(recorded)))
    lot_ids = {l.lot_id for l in lots}

    def look_row():
        return db.one(conn, "SELECT look_round FROM cases WHERE run_id = :r AND case_id = :c", r=run_id,
                      c=exclude_case or "")
    look = look_row() if reads is None else reads.get(("look", exclude_case), look_row)
    look_round = look["look_round"] if look else 0
    if world == "shelfshift":
        rows = None if reads is None else reads.get(
            ("res", exclude_case, look_round), lambda: booking_rows(conn, run_id, exclude_case, look_round))
        booked = bookings(conn, run_id, exclude_case, sku, product.zone, day, lot_ids, look_round, rows=rows,
                          released=pending.released)
    else:
        booked = Bookings()
    if extra is not None:
        booked = Bookings(booked.demand + extra.demand, booked.totes + extra.totes, booked.run_totes + extra.run_totes,
                          booked.lot_units + extra.lot_units, booked.lot_price + extra.lot_price)
    inp = EngineInput(engine_version=ENGINE_VERSION, today=day, decision_min=ref.calendar["open_min"],
                      case_lot_id=lot_id, product=product, lots=lots, inbound=inbound, runs=tuple(runs),
                      forecasts=forecasts, capacity=capacity, sales_yesterday=tuple(sales), markdown=ref.markdown,
                      policy=policy, bookings=booked, markdown_schedule=ref.schedules[sku])
    if world == "shelfshift":
        rows = None if reads is None else reads.get(("facts", day), lambda: fact_rows(conn, run_id, day))
        facts = facts_in_effect(conn, run_id, day, sku, inp, rows=rows, replaced=pending.replaced)
        if pending.fact is not None:  # confirmed last, so in effect after every other fact
            facts += tuple(adjustments_in_effect(pending.fact, day, sku, inp))
    else:
        facts = ()
    return replace(inp, reported_facts=facts, overrides=tuple(plain(o) for o in overrides))
