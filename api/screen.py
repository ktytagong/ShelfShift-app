"""The ESL-02 case-opening screen: which lots get a case at all (03-CONTEXT D-22; 04-CONTEXT D-18, D-19).

Every morning the screen looks at each never-cased lot whose window contains today. The window runs from
`days_before_first_step` before the lot's first markdown step to its expiry day: with F = expiry - the schedule's
largest days-before-expiry, it is [F - days_before_first_step, expiry]. The screen runs no search and no Gemini call.
A lot qualifies when there is still time to act and either branch holds:

(a) Keep under the schedule wastes at least `min_waste_units` of the LOT'S OWN units (reading A1), or
(b) the lot's Keep stock at the start of its next step day, or of its expiry day when no step remains, is at least
    `min_discounted_totes` totes, and some run today has no `transfer_caps` reasons and a demand cap of at least
    `min_short_totes` totes. A tote is `units_per_tote` units exactly (A2), compared on unrounded floats.

"Time left" means `candidates(effective)` holds a plan other than Keep (A3). The next step day comes from the
schedule alone, through `scheduled_pct`, not from the lot's recorded markdown (A4). Lots a delivered transfer
created are not screened; the caller leaves them out (A5).

Only public engine functions are used (D-03, D-18): Keep is stepped day by day with `engine.world.step_day`, the same
allocator, schedule rule and expiry order `simulate` uses, because `simulate` reports waste by branch only and branch
(b) needs the lot's own stock on a future day. The four thresholds come from reference/world.json `case_opening`
through `config.CASE_OPENING`; none is a code constant (DATA-06).
"""
from __future__ import annotations

from engine.adjust import checked_input, effective
from engine.canonical import from_dict
from engine.allocator import EPS, ShelfLot
from engine.limits import candidates, transfer_caps
from engine.model import EngineInput, scheduled_pct, totals
from engine.simulate import HorizonError, demand_table, horizon_end
from engine.timing import first_sellable_day
from engine.world import step_day


def window(expiry_day: int, schedule, days_before_first_step: int) -> tuple[int, int]:
    """(first day, last day) of the lot's screen window. An empty schedule is refused, never skipped silently."""
    if not schedule:
        raise ValueError("no markdown schedule: the ESL-02 window starts from the schedule's first step")
    first_step = expiry_day - max(k for k, _ in schedule)  # the first day any step applies
    return first_step - days_before_first_step, expiry_day


def keep_projection(eff: EngineInput, branch: str) -> tuple[dict[int, float], dict[str, float]]:
    """Keep at `branch` on the effective input, stepped with world.step_day on base demand and the base assumed
    response: (the case lot's stock at the start of each day from today to the horizon end, units wasted by lot id).

    Units set aside by other cases are off the shelf from the start, as in `simulate`; deliveries join on their
    arrival day and sell from their first sellable day. A day with no forecast raises HorizonError, as `simulate`
    does."""
    m = {20: eff.markdown.m20[1], 30: eff.markdown.m30[1]}
    demand = demand_table(eff, "base")
    aside = totals(eff.bookings.lot_units)
    lots = [ShelfLot(l.lot_id, l.branch, max(0.0, l.qty - aside.get(l.lot_id, 0.0)), l.expiry_day, l.received_seq,
                     l.markdown_pct, recorded=l.qty)
            for l in eff.lots if l.branch == branch]
    start: dict[int, float] = {}
    waste: dict[str, float] = {}
    for day in range(eff.today, horizon_end(eff) + 1):
        if (branch, day) not in demand:
            raise HorizonError(f"no forecast for {branch} on day {day}")
        start[day] = next((l.qty for l in lots if l.lot_id == eff.case_lot_id), 0.0)
        arrivals = tuple(ShelfLot(i.inbound_id, i.branch, i.qty, i.expiry_day, i.received_seq,
                                  sellable_from=first_sellable_day(i.arrival_day, i.arrival_min, eff.policy),
                                  recorded=i.qty)
                         for i in eff.inbound if i.branch == branch and i.arrival_day + i.arrival_min // 1440 == day)
        before = [*lots, *arrivals]
        step_day(lots, day, {branch: demand[(branch, day)]}, m, eff.product, arrivals=arrivals,
                 schedule=eff.markdown_schedule)
        left = {id(l) for l in lots}
        for l in before:  # a lot step_day removed was past its expiry: what it still held was wasted
            if id(l) not in left and l.qty > EPS:
                waste[l.lot_id] = waste.get(l.lot_id, 0.0) + l.qty
    return start, waste


def lot_keep_waste(engine_input: dict) -> float | None:
    """The case lot's own Keep waste on a stored engine input: the same projection as the screen's `keep_waste`.
    None when a day has no forecast (HorizonError)."""
    try:
        eff = effective(checked_input(from_dict(engine_input)))
        return keep_projection(eff, eff.case_lot().branch)[1].get(eff.case_lot_id, 0.0)
    except HorizonError:
        return None


def next_step_day(schedule, expiry_day: int, today: int) -> int:
    """The first day after today whose scheduled percent is deeper than today's, from the schedule alone (A4);
    the expiry day when no step remains."""
    today_pct = scheduled_pct(schedule, expiry_day, today)
    return next((d for d in range(today + 1, expiry_day + 1) if scheduled_pct(schedule, expiry_day, d) > today_pct),
                expiry_day)


def screen(raw_inp: EngineInput, thresholds: dict) -> dict:
    """The D-22 screen for the raw input's case lot (readings A1-A4). No search, no Gemini call.

    {qualifies, a, b, time_left, usable_runs, step_day, keep_waste, stock_at_step}; HorizonError propagates (the
    caller opens the case with "missing data", never a silent skip)."""
    inp = checked_input(raw_inp)
    eff = effective(inp)
    lot, upt = eff.case_lot(), eff.product.units_per_tote
    start, waste = keep_projection(eff, lot.branch)
    keep_waste = waste.get(lot.lot_id, 0.0)
    a = keep_waste >= thresholds["min_waste_units"]  # A1: the lot's own waste
    step = next_step_day(eff.markdown_schedule, lot.expiry_day, eff.today)  # A4
    stock_at_step = start.get(step, 0.0)
    stocked = stock_at_step >= thresholds["min_discounted_totes"] * upt  # A2: units, never rounded
    usable = []
    for run in eff.runs:  # today's runs only (03-CONTEXT D-24)
        caps, reasons = transfer_caps(eff, run)
        if not reasons and caps["demand"] >= thresholds["min_short_totes"] * upt:
            usable.append(run.run_code)
    b = stocked and bool(usable)
    time_left = len(candidates(eff)[0]) > 1  # A3: a plan other than Keep is still possible
    return {"qualifies": (a or b) and time_left, "a": a, "b": b, "time_left": time_left, "usable_runs": usable,
            "step_day": step, "keep_waste": keep_waste, "stock_at_step": stock_at_step}
