"""The baseline rule (spec §6): what a simple rule would do, with no future data."""
from __future__ import annotations

import math

from .adjust import coerce_input, effective, validate_input
from .allocator import EPS, ShelfLot, allocate
from .limits import markdown_pcts, transfer_limit
from .model import KEEP, MARKDOWN, TRANSFER, EngineInput, Plan, scheduled_pct, totals
from .simulate import HorizonError
from .model import NEAR_EXPIRY_DAYS  # engine-2 name kept (IN-03, D-20); logic reads the Policy fields


def baseline_plan(inp: EngineInput) -> Plan:
    """If the case lot would still have stock left at its expiry, send that surplus to the short
    branch whose run arrives earliest and passes the same limits (spec §6 rev 6.2.2, D-21).
    The surplus is a FEFO walk over the source's unexpired lots that expire by the case lot,
    oldest first (expiry, then received_seq). Each lot, net of units set aside for other cases,
    sells at most the base demand left through its own expiry; the surplus is what is left of
    the case lot at its expiry. A lot that has already expired sells nothing and is not walked.
    Stock that outlives the case lot is not walked: this is engine-2's walk (01-CONTEXT D-21), kept
    verbatim, which assumes such stock sells after the case lot. The projection does too, unless
    that stock is recorded at a deeper markdown; then the projection sells it first, and this walk
    overstates the case lot's sales and understates its surplus (review WR-07; whether Phase 8
    discloses this is open for the user, 02-CONTEXT D-30). The surplus can never exceed the case
    lot's own net quantity. "Earliest" is arrival day and minute together
    (day x 1440 + arrives_min), ties to the lowest run_code (D-17). When the lot expires within
    policy.baseline_near_expiry_days days (1: today or tomorrow), mark down
    policy.baseline_markdown_pct whatever surplus the transfer doesn't take. Otherwise keep. It
    computes on the coerced input (WR-07). A source day from today through the case lot's expiry
    with no forecast is a HorizonError, as in evaluate, never zero demand (round-3 WR-03).

    With a markdown schedule (spec §6 rev 6.3.0, D-11, D-12) the store follows its own schedule, and
    its surplus counts the schedule's sales lift. The walk runs day by day from today through the
    case lot's expiry with the sales allocator, on base demand and the base markdown response (the
    base values of m20 and m30). Each walk lot sells in the tier of the deeper of its recorded
    markdown and its own schedule step that day, as in the projection and the world step (02-CONTEXT
    D-22), so the schedule can deepen a lot's price but never make it shallower. With no schedule
    the walk equals the first-expiry-first walk exactly (that path is kept verbatim). The deeper tier
    sells first and the next tier gets the rest of the day, first-expiry-first within a tier (then
    first received, then lot id). The walk also takes the source's unexpired lots that outlive the
    case lot: each sells in its own tier each day, so one recorded deeper sells first, as in the
    projection, and only the case lot's leftover at its expiry is the surplus (review WR-07). Like
    the walk without a schedule, it ignores inbound. The near-expiry step does not apply (the
    schedule is the store's markdown): the rest of a transfer is held, and with no passing run the
    rule keeps. The baseline rule never sees the waste price."""
    inp = coerce_input(inp)
    validate_input(inp)
    eff = effective(inp)
    lot = eff.case_lot()
    set_aside = totals(eff.bookings.lot_units)
    demand = {f.day: f.base for f in eff.forecasts if f.branch == eff.source}
    missing = [d for d in range(eff.today, lot.expiry_day + 1) if d not in demand]
    if missing:
        raise HorizonError(f"no forecast for {eff.source} on day {missing[0]}")
    walk = sorted((l for l in eff.lots
                   if l.branch == eff.source and eff.today <= l.expiry_day <= lot.expiry_day),
                  key=lambda l: (l.expiry_day, l.received_seq, l.lot_id))
    taken, leftover = 0.0, 0.0  # a case lot that has itself expired is not walked: no surplus
    if not eff.markdown_schedule:
        for l in walk:
            net = max(0.0, l.qty - set_aside.get(l.lot_id, 0.0))
            through = sum(demand.get(d, 0.0) for d in range(eff.today, l.expiry_day + 1))
            sells = min(net, max(0.0, through - taken))
            taken += sells
            if l.lot_id == lot.lot_id:
                leftover = net - sells
                break
    else:
        # review WR-07: the source's unexpired lots that outlive the case lot are on the shelf too, so one recorded
        # deeper sells first, as in the projection; the walk still stops at the case lot's expiry and reads only its
        # leftover. They expire after every walk lot, so walk + later stays in walk order.
        later = sorted((l for l in eff.lots
                        if l.branch == eff.source and eff.today <= l.expiry_day and l.expiry_day > lot.expiry_day),
                       key=lambda l: (l.expiry_day, l.received_seq, l.lot_id))
        # in walk order: allocate's FEFO sort is stable, so lot_id stays the last tie-break
        # each walk lot starts at its recorded markdown; the schedule only ever deepens it (D-22)
        shelf = [ShelfLot(l.lot_id, l.branch, max(0.0, l.qty - set_aside.get(l.lot_id, 0.0)), l.expiry_day,
                          l.received_seq, l.markdown_pct) for l in walk + later]
        recorded = {l.lot_id: l.markdown_pct for l in walk + later}
        m = {20: eff.markdown.m20[1], 30: eff.markdown.m30[1]}
        for d in range(eff.today, lot.expiry_day + 1):
            for s in shelf:
                s.pct = max(recorded[s.lot_id], scheduled_pct(eff.markdown_schedule, s.expiry_day, d))
            allocate(demand[d], [s for s in shelf if s.qty > EPS and d <= s.expiry_day], m, eff.product.price)
        leftover = next((s.qty for s in shelf if s.lot_id == lot.lot_id), 0.0)
    surplus = math.floor(leftover + 1e-9)
    if surplus < 1:
        return Plan(KEEP)
    pct = eff.policy.baseline_markdown_pct
    near = (not eff.markdown_schedule and lot.expiry_day - eff.today <= eff.policy.baseline_near_expiry_days
            and pct in markdown_pcts(eff) and eff.case_lot_id not in eff.bookings.lot_price)
    for run in sorted(eff.runs, key=lambda r: (r.day * 1440 + r.arrives_min, r.run_code)):
        qmax, reasons = transfer_limit(eff, run)
        if not reasons:
            q = min(surplus, qmax)
            return Plan(TRANSFER, pct if near and surplus - q >= 1 else 0, q, run.dest, run.run_code)
    return Plan(MARKDOWN, pct) if near else Plan(KEEP)
