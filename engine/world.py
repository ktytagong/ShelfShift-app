"""One demo day in one world (spec §8 Demo clock and advance day).

Sales, dispatch and expiry act on PHYSICAL stock (ShelfLot.qty). The record
(ShelfLot.recorded) is what the app sees, and it never goes below 0. A phantom
lot sells nothing and is written off as a record correction at expiry, never as
waste. Units set aside for a committed transfer (ShelfLot.reserved, from the
commit until the run departs) are off sale, as in `simulate`. Money is computed
here too, with the same rules as `simulate`: trip costs from `model.trip_cost`,
disposal per unit wasted. A shipment names its `origin`, the branch it leaves from and
that pays for the trip; the engine checks it against the source lot and never guesses
it. A shipment whose source lot has gone (expired earlier, or missing) or is past its
expiry day though still listed is a failed delivery: nothing moves, the trip rule charges
the cancel cost to `origin`, it is reported as a short shipment, and the day goes on; a
listed expired lot is written off by expiry as usual (02-CONTEXT D-25). A price change must be 20% or 30%. Only
the changes effective that day are checked, in order, each against the lot's running percent,
so together they can only lower the price; a change for another day is neither checked nor
applied. The check runs with the shipments before anything moves (D-11, WR-08, round-3 WR-02).

With a markdown schedule (rev 6.3.0, spec §8), every lot, moved and arrived lots included, is raised to the
schedule's percent for its own expiry after the day's price changes and before sales, through the same
`model.scheduled_pct` as `simulate`, so a lot sells at the deeper of the two and the world and the projection agree.
A schedule step is never a price change, so it never fails the price-change check; a manager's change that repeats
a step already in force from an earlier day doesn't lower the price and is refused; a change equal to today's own
step is accepted and changes nothing, because the schedule applies that step anyway (spec §8; review IN-02).
A bad schedule is refused first, before anything moves. With no schedule the world step is engine-2's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .allocator import EPS, ShelfLot, allocate
from .model import Product, schedule_problem, scheduled_pct, trip_cost


@dataclass(frozen=True, kw_only=True)
class Shipment:
    lot_id: str
    qty: float  # planned
    dest: str
    new_lot_id: str
    sellable_from: int
    fixed_cost: float  # the run's quote, as committed
    per_unit_cost: float  # handling per unit shipped
    cancel_cost: float | None = None  # paid if nothing ships; None = the full quote
    origin: str  # the source branch the transfer leaves from, charged for the trip; never guessed


@dataclass(frozen=True)
class PriceChange:
    lot_id: str
    pct: int
    effective_day: int


@dataclass
class DayOutcome:
    """Everything by branch. Transport and handling are charged to the source branch."""

    sold: dict[str, float] = field(default_factory=dict)
    revenue: dict[str, float] = field(default_factory=dict)
    wasted: dict[str, float] = field(default_factory=dict)
    lost: dict[str, float] = field(default_factory=dict)
    transport: dict[str, float] = field(default_factory=dict)
    handling: dict[str, float] = field(default_factory=dict)
    disposal: dict[str, float] = field(default_factory=dict)
    short_shipments: list[dict] = field(default_factory=list)
    record_corrections: list[dict] = field(default_factory=list)

    @property
    def net(self) -> float:
        """Network net contribution for the day: revenue - transport - handling - disposal."""
        costs = (self.transport, self.handling, self.disposal)
        return sum(self.revenue.values()) - sum(sum(c.values()) for c in costs)


def _add(d: dict[str, float], key: str, value: float) -> None:
    d[key] = d.get(key, 0.0) + value


def step_day(lots: list[ShelfLot], day: int, demand: dict[str, float], m: dict[int, float], product: Product,
             shipments: tuple[Shipment, ...] = (), price_changes: tuple[PriceChange, ...] = (),
             arrivals: tuple[ShelfLot, ...] = (), schedule: tuple[tuple[int, int], ...] = ()) -> DayOutcome:
    """Advance one SKU in one world by one day. `lots` is mutated: sold and expired stock leave it.

    `schedule` is the product's markdown schedule (rev 6.3.0). It is applied after the day's price changes and
    before sales, so a lot sells at the deeper of the two. A schedule step is never a price change and never fails
    the check; a manager's change that repeats a step already in force from an earlier day doesn't lower the price
    and is refused; a change equal to today's own step is accepted and changes nothing, because the schedule applies
    that step anyway (spec §8; review IN-02). With no schedule nothing changes.
    """
    # 0. a bad schedule is refused first, before any other check or move (rev 6.3.0)
    reason = schedule_problem(schedule)
    if reason is not None:
        raise ValueError(f"markdown schedule {schedule!r}: {reason}")
    by_id = {l.lot_id: l for l in lots}
    # 0. validate every shipment and price change before anything moves, so a rejection leaves the world untouched (D-11, WR-08)
    for sh in shipments:
        if not (isinstance(sh.qty, (int, float)) and math.isfinite(sh.qty) and sh.qty > 0):
            raise ValueError(f"shipment {sh.lot_id}: quantity must be a finite number above 0, got {sh.qty!r}")
        if not (isinstance(sh.origin, str) and sh.origin):
            raise ValueError(f"shipment {sh.lot_id}: origin must be the source branch, got {sh.origin!r}")
        src = by_id.get(sh.lot_id)
        if src is not None and sh.origin != src.branch:
            raise ValueError(f"shipment {sh.lot_id}: origin {sh.origin!r} is not the source lot's branch {src.branch!r}")
    # only today's changes are checked, in order, against a running pct, so together they can only lower the price (round-3 WR-02)
    running = {l.lot_id: l.pct for l in lots}
    for pc in price_changes:
        if pc.pct not in (20, 30):
            raise ValueError(f"price change {pc.lot_id}: pct must be 20 or 30, got {pc.pct!r}")
        # a change for another day, or for a lot no longer in `lots`, is not applied in this call: a no-op (WR-08)
        if pc.effective_day != day or pc.lot_id not in running:
            continue
        if pc.pct <= running[pc.lot_id]:
            raise ValueError(f"price change {pc.lot_id}: {pc.pct}% does not lower the price "
                             f"(the lot is already {running[pc.lot_id]}% off)")
        running[pc.lot_id] = pc.pct
    out = DayOutcome()
    # 1. departures: ship what is physically there, record the shortfall
    for sh in shipments:
        src = by_id.get(sh.lot_id)
        # the source lot expired earlier or is missing (WR-08), or is still listed but past its expiry day (D-25):
        # a failed delivery, nothing moves; a listed lot is left untouched and step 4 writes it off
        if src is None or src.expiry_day < day:
            transport, handling = trip_cost(sh.fixed_cost, sh.cancel_cost, sh.per_unit_cost, 0.0)
            _add(out.transport, sh.origin, transport)
            _add(out.handling, sh.origin, handling)
            out.short_shipments.append({"lot_id": sh.lot_id, "planned": sh.qty, "shipped": 0.0,
                                        "transport_paid": transport})
            continue
        shipped = min(sh.qty, src.qty)
        src.qty -= shipped
        src.reserved = max(0.0, src.reserved - sh.qty)
        src.recorded = max(0.0, src.recorded - shipped)
        transport, handling = trip_cost(sh.fixed_cost, sh.cancel_cost, sh.per_unit_cost, shipped)
        _add(out.transport, sh.origin, transport)
        _add(out.handling, sh.origin, handling)
        if shipped < sh.qty - EPS:  # shipped == 0 is a failed trip: its transport_paid bought nothing
            out.short_shipments.append({"lot_id": sh.lot_id, "planned": sh.qty, "shipped": shipped,
                                        "transport_paid": transport})
            # packing found the lot short, so the record is reconciled to what is physically left (0)
            if abs(src.recorded - src.qty) > EPS:
                out.record_corrections.append({"lot_id": sh.lot_id, "recorded": src.recorded, "physical": src.qty})
                src.recorded = src.qty
        lots.append(ShelfLot(sh.new_lot_id, sh.dest, shipped, src.expiry_day, 10**6,
                             sellable_from=sh.sellable_from, recorded=shipped))
    # 2. external inbound
    lots.extend(arrivals)
    # price changes take effect
    for pc in price_changes:
        if pc.effective_day == day and pc.lot_id in by_id:
            by_id[pc.lot_id].pct = pc.pct
    # the schedule takes effect for every lot (moved and arrived included): the deeper of its pct and the step (rev 6.3.0)
    if schedule:
        for lot in lots:
            lot.pct = max(lot.pct, scheduled_pct(schedule, lot.expiry_day, day))
    # 3. sales on physical stock, once per branch; expired lots never sell
    for branch in sorted(demand):
        shelf = [l for l in lots if l.branch == branch and l.qty > EPS and l.sellable_from <= day <= l.expiry_day]
        s = allocate(demand[branch], shelf, m, product.price)
        for lot in shelf:
            lot.recorded = max(0.0, lot.recorded - s.sold_by_lot.get(lot.lot_id, 0.0))
        out.sold[branch] = s.sold
        out.revenue[branch] = s.revenue
        out.lost[branch] = s.lost
    # 4. expiry (including lots already past it): physical stock left is waste; a record that differs is corrected
    for lot in [l for l in lots if l.expiry_day <= day]:
        if lot.qty > EPS:
            _add(out.wasted, lot.branch, lot.qty)
            _add(out.disposal, lot.branch, lot.qty * product.disposal_cost)
        if abs(lot.recorded - lot.qty) > EPS:
            out.record_corrections.append({"lot_id": lot.lot_id, "recorded": lot.recorded, "physical": lot.qty})
        lots.remove(lot)
    return out


def apply_stock_correction(lots: list[ShelfLot], lot_id: str, units: float) -> None:
    """A confirmed count brings the record into line with what was counted."""
    next(l for l in lots if l.lot_id == lot_id).recorded = units
