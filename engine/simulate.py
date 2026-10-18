"""Day-by-day projection of one plan under one demand scenario (spec §6 Core).

Steps every lot of the product at the source and the candidate destinations
until the last affected expiry. Daily order: departures, arrivals, the plan's
markdown, schedule markdowns before sales, sales, expiry. The input must already
be effective (adjust.effective).

A transfer is committed today: its units are reserved (off sale) from today and
leave on the run's day. Trip costs follow `model.trip_cost` (the full quote if anything
ships, the run's cancel cost if nothing does). Moved units sell at full price at the
destination unless the markdown schedule marks them down; they follow the schedule only,
by the case lot's expiry (spec §6 Markdown schedule, rev 6.3.0).

A lot's price on a day is the deeper of its recorded markdown, the schedule's percent for
its own expiry (`model.scheduled_pct`) and, for the case lot from the plan markdown's
effective day, the plan's markdown. With no schedule nothing is done per lot (engine-2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .allocator import EPS, ShelfLot, allocate
from .model import KEEP, TRANSFER, EngineInput, Plan, scheduled_pct, totals, trip_cost
from .timing import first_sellable_day, markdown_effective_day


class HorizonError(ValueError):
    """A forecast is missing for a day the simulation must reach. Never clamp silently."""


@dataclass
class SimResult:
    net: float = 0.0
    revenue: float = 0.0
    transport: float = 0.0  # the run's fixed quote
    handling: float = 0.0  # per unit shipped
    disposal: float = 0.0
    sold: float = 0.0
    wasted: float = 0.0
    moved: float = 0.0
    short: float = 0.0  # planned minus shipped: 0 for every candidate plan on its own input
    initial: float = 0.0  # stock at the start plus external inbound
    on_hand_end: float = 0.0
    sold_by: dict[tuple[str, int], float] = field(default_factory=dict)  # (branch, day)
    lost_by: dict[tuple[str, int], float] = field(default_factory=dict)
    wasted_by: dict[str, float] = field(default_factory=dict)


def demand_table(inp: EngineInput, scenario: str) -> dict[tuple[str, int], float]:
    return {(f.branch, f.day): getattr(f, scenario) for f in inp.forecasts}


def horizon_end(inp: EngineInput) -> int:
    return max([inp.today] + [l.expiry_day for l in inp.lots] + [i.expiry_day for i in inp.inbound])


def simulate(inp: EngineInput, plan: Plan, scenario: str = "base",
             m20: float | None = None, m30: float | None = None,
             branches: set[str] | None = None) -> SimResult:
    """Project `plan`. `branches` limits the simulation to some branches (others are unaffected by the plan)."""
    p = inp.product
    m = {20: inp.markdown.m20[1] if m20 is None else m20, 30: inp.markdown.m30[1] if m30 is None else m30}
    demand = demand_table(inp, scenario)
    set_aside = totals(inp.bookings.lot_units)
    case_lot = inp.case_lot()
    names = branches or {f.branch for f in inp.forecasts} | {l.branch for l in inp.lots}
    res = SimResult()

    stock = [ShelfLot(l.lot_id, l.branch, max(0.0, l.qty - set_aside.get(l.lot_id, 0.0)), l.expiry_day,
                      l.received_seq, l.markdown_pct)
             for l in inp.lots if l.branch in names]
    pending = [ShelfLot(i.inbound_id, i.branch, i.qty, i.expiry_day, i.received_seq,
                        sellable_from=first_sellable_day(i.arrival_day, i.arrival_min, inp.policy))
               for i in inp.inbound if i.branch in names]
    res.initial = sum(l.qty for l in stock) + sum(l.qty for l in pending)
    run = next((r for r in inp.runs if r.run_code == plan.run_code), None) if plan.kind == TRANSFER else None
    md_day = markdown_effective_day(inp)
    by_id = {l.lot_id: l for l in stock}
    if run is not None and case_lot.lot_id in by_id:
        by_id[case_lot.lot_id].reserved = float(plan.qty)  # off sale from the commit
    # the schedule's percent by days left (expiry_day - day), computed once per call; none without a schedule.
    # More days left than the largest k: 0. Past expiry (negative days left): the deepest step.
    # The table stops at the most days left any lot can have on a simulated day (horizon end - today), so its
    # size never depends on how large a step's k is (review CR-01); a left past the table is past the largest k
    schedule = inp.markdown_schedule
    by_days_left = {}
    if schedule:
        top_k = max(k for k, _ in schedule)
        span = horizon_end(inp) - inp.today
        by_days_left = {left: scheduled_pct(schedule, left, 0) for left in range(0, min(top_k, span) + 1)}
        deepest = max(pct for _, pct in schedule)

    for day in range(inp.today, horizon_end(inp) + 1):
        # 1. departures leave before sales
        if run is not None and day == run.day and case_lot.lot_id in by_id:
            src = by_id[case_lot.lot_id]
            moved = min(float(plan.qty), src.qty)
            src.qty -= moved
            src.reserved = 0.0
            res.moved, res.short = moved, float(plan.qty) - moved
            res.transport, res.handling = trip_cost(run.fixed_cost, run.cancel_cost, run.per_unit_cost, moved)
            stock.append(ShelfLot(f"{case_lot.lot_id}>{plan.dest}", plan.dest, moved, case_lot.expiry_day,
                                  received_seq=10**6,
                                  sellable_from=first_sellable_day(run.day, run.arrives_min, inp.policy)))
        # 2. arrivals and inbound join the stock (sellable from their first sellable day)
        for lot in [l for l in pending if l.sellable_from <= day]:
            pending.remove(lot)
            stock.append(lot)
        if plan.kind != KEEP and plan.pct and day == md_day and case_lot.lot_id in by_id:
            lot = by_id[case_lot.lot_id]
            lot.pct = max(lot.pct, plan.pct)  # a plan never makes a price shallower
        # 2b. schedule markdowns before sales: every lot at its own expiry's step (moved units by the case lot's)
        if schedule:
            for lot in stock:
                left = lot.expiry_day - day
                pct = deepest if left < 0 else by_days_left.get(left, 0)
                if pct > lot.pct:
                    lot.pct = pct
        # 3. sales: once per branch for this SKU and day
        for b in sorted(names):
            if (b, day) not in demand:
                raise HorizonError(f"no forecast for {b} on day {day}")
            shelf = [l for l in stock if l.branch == b and l.qty > EPS and l.sellable_from <= day <= l.expiry_day]
            s = allocate(demand[(b, day)], shelf, m, p.price)
            res.sold += s.sold
            res.revenue += s.revenue
            res.sold_by[(b, day)] = s.sold
            res.lost_by[(b, day)] = s.lost
        # 4. expiry at the end of the day, including stock that arrived too late to sell
        #    and lots that were already past expiry on the first day
        for lot in stock + pending:
            if lot.expiry_day <= day and lot.qty > EPS:
                res.wasted += lot.qty
                res.wasted_by[lot.branch] = res.wasted_by.get(lot.branch, 0.0) + lot.qty
                lot.qty = 0.0

    res.on_hand_end = sum(l.qty for l in stock + pending)
    res.disposal = res.wasted * p.disposal_cost
    res.net = res.revenue - res.transport - res.handling - res.disposal
    return res
