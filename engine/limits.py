"""Hard limits on a transfer (spec §6 Hard limits). Each rejection records its reason."""
from __future__ import annotations

import math

from .adjust import WHAT_IF_ONLY, AdjustmentError, coerce_input, effective, validate_input
from .model import KEEP, MARKDOWN, MOVE_RANK, TRANSFER, EngineInput, Plan, Run, scheduled_pct, totals
from .simulate import HorizonError, simulate
from .timing import first_sellable_day, fmt_at, latest_decision_at, markdown_effective_day

CAP_REASON = {
    "demand": "no unmet demand at destination",
    "storage": "no free storage at destination",
    "truck": "no truck space",
    "stock": "no stock to move",
}
PRICE_BOOKED = "price change already booked by another case"


def unmet_by_day(inp: EngineInput, dest: str) -> dict[int, float]:
    """Lost sales at `dest` in the base scenario if nothing is moved: its stock timeline, committed inbound included."""
    keep = simulate(inp, Plan(KEEP), "base", branches={dest})
    return {day: lost for (_, day), lost in keep.lost_by.items()}


def transfer_caps(inp: EngineInput, run: Run) -> tuple[dict[str, int], list[str]]:
    """The quantity caps on `run` (demand, storage, truck, stock), or the reasons it is rejected outright."""
    pol, lot, product = inp.policy, inp.case_lot(), inp.product
    reasons: list[str] = []
    if not run.available:
        reasons.append("run unavailable")
    if product.zone == "chilled" and not run.cold_chain:
        reasons.append("cold chain")
    deadline = latest_decision_at(run, pol)
    if inp.today * 1440 + inp.decision_min > deadline:
        reasons.append(f"past decision deadline {fmt_at(deadline, inp.today)}")
    sell_day = first_sellable_day(run.day, run.arrives_min, pol)
    if lot.expiry_day - sell_day + 1 < pol.min_sellable_days:
        reasons.append("not enough sellable days at destination")
    if reasons:
        return {}, reasons

    held_demand = totals((day, u) for b, day, u in inp.bookings.demand if b == run.dest)
    lost = unmet_by_day(inp, run.dest)
    demand_cap = sum(max(0.0, lost.get(d, 0.0) - held_demand.get(d, 0.0))
                     for d in range(sell_day, lot.expiry_day + 1))

    arrival_day = run.day + run.arrives_min // 1440
    free = {c.day: c.free_totes for c in inp.capacity if c.branch == run.dest}
    missing = [d for d in range(arrival_day, lot.expiry_day + 1) if d not in free]
    if missing:
        raise HorizonError(f"no capacity for {run.dest} on day {missing[0]}")
    held_totes = totals((day, t) for b, day, t in inp.bookings.totes if b == run.dest)
    storage_totes = min(free[d] - held_totes.get(d, 0) for d in range(arrival_day, lot.expiry_day + 1))
    truck_totes = run.spare_totes - sum(t for code, t in inp.bookings.run_totes if code == run.run_code)
    set_aside = sum(u for lot_id, u in inp.bookings.lot_units if lot_id == lot.lot_id)
    return {
        "demand": math.floor(demand_cap + 1e-9),
        "storage": max(0, storage_totes) * product.units_per_tote,  # one lot per tote, so ceil(q/upt) <= totes
        "truck": max(0, truck_totes) * product.units_per_tote,
        "stock": math.floor(lot.qty - set_aside + 1e-9),
    }, []


def transfer_limit(inp: EngineInput, run: Run) -> tuple[int, list[str]]:
    """Largest whole-unit quantity allowed on `run`, and the reasons if none is.

    Quantity-dependent limits (demand, storage, truck, stock) become a cap, so
    every quantity from 1 to the cap passes every limit.
    """
    caps, reasons = transfer_caps(inp, run)
    if reasons:
        return 0, reasons
    binding = min(caps, key=lambda k: caps[k])
    if caps[binding] < 1:
        return 0, [CAP_REASON[binding]]
    return caps[binding], []


def markdown_pcts(inp: EngineInput) -> tuple[int, ...]:
    """Markdowns that lower the price: a lot already at 20% off can only go to 30%."""
    return tuple(p for p in (20, 30) if p > inp.case_lot().markdown_pct)


def covered_reason(inp: EngineInput, pct: int) -> str | None:
    """Why a markdown to `pct` (or a transfer's rest at `pct`) is covered by the markdown schedule, or None.

    Covered means the schedule's percent on the plan markdown's effective day is at least `pct`, so the option
    equals Keep (or hold) on every remaining day (spec §6 Markdown schedule, rev 6.3.0, D-07). The reason names
    that percent S and the first day F of the step that gives it. With no schedule nothing is covered.
    """
    if not inp.markdown_schedule:
        return None
    case = inp.case_lot()
    s = scheduled_pct(inp.markdown_schedule, case.expiry_day, markdown_effective_day(inp))
    if s < pct:
        return None
    first = case.expiry_day - max(k for k, p in inp.markdown_schedule if p >= s)
    return f"the schedule already has it at {s}% from day {first}"


def check_plan(inp: EngineInput, plan: Plan) -> list[str]:
    """Why this exact plan breaks a hard limit, or [] if it passes. Send, approve and prepare call it (spec §8).

    Callers pass the stored, raw input of the active version (spec §8 Recheck contract, D-10).
    Confirmed facts, then adopted assumptions, are applied here exactly as in `evaluate`. A what-if
    that was never adopted must not change the active plan, so an input that carries a what-if-only
    override, or on which effective() already applied one (EngineInput.applied_overrides), is
    refused with AdjustmentError (D-18 WR-01). An invalid input, or an input made by another
    engine version, is refused with InputError (WR-03, WR-09). Everything here, the what-if
    refusal included, runs on the coerced input (WR-07).
    """
    inp = coerce_input(inp)
    validate_input(inp)
    what_ifs = sorted(({adj.get("kind") for adj in inp.overrides} | set(inp.applied_overrides)) & WHAT_IF_ONLY)
    if what_ifs:
        raise AdjustmentError(
            "check_plan takes the active version's raw input; what-if-only overrides, carried or already applied, "
            f"are not allowed: {what_ifs}")
    inp = effective(inp)
    # A malformed plan is named and returned at once, never clamped or repaired (D-11).
    if plan.kind not in MOVE_RANK:
        return [f"unknown plan kind {plan.kind!r}"]
    shape: list[str] = []
    if plan.kind == KEEP and plan.pct != 0:
        shape.append("keep cannot carry a markdown")
    if plan.kind == MARKDOWN and plan.pct not in (20, 30):
        shape.append("markdown must be 20% or 30%")
    if plan.kind == TRANSFER:
        if plan.pct not in (0, 20, 30):
            shape.append("rest of a transfer must be held or marked down 20% or 30%")
        if isinstance(plan.qty, bool) or not isinstance(plan.qty, int) or plan.qty < 1:
            shape.append("transfer quantity must be a whole number of at least 1")
    if shape:
        return shape
    reasons: list[str] = []
    if plan.pct:
        if inp.case_lot_id in inp.bookings.lot_price:
            reasons.append(PRICE_BOOKED)
        if plan.pct not in markdown_pcts(inp):
            reasons.append(f"lot already {inp.case_lot().markdown_pct}% off")
        elif (covered := covered_reason(inp, plan.pct)) is not None:  # rev 6.3.0 D-07: the same reason as candidates
            reasons.append(covered)
    if plan.kind != TRANSFER:
        return reasons
    run = next((r for r in inp.runs if r.run_code == plan.run_code and r.dest == plan.dest), None)
    if run is None:
        return reasons + [f"no run {plan.run_code} to {plan.dest}"]
    caps, run_reasons = transfer_caps(inp, run)
    return reasons + run_reasons + [CAP_REASON[k] for k in caps if caps[k] < plan.qty]


def candidates(inp: EngineInput) -> tuple[list[Plan], list[dict]]:
    """Every plan that passes the hard limits, plus rejected options with reasons.

    With the shortlist on (Policy.shortlist_step 5 or 10, rev 6.3.0 ENG-09), a run's transfer quantities are the
    multiples of the step up to its cap plus the cap itself (the cap alone when it is below the step). The shortlist
    keeps every destination, run and remainder; only transfer quantities coarsen. Off (0) is the full search.

    With a markdown schedule (rev 6.3.0, D-07), a markdown or a transfer's remainder percent that the schedule
    already has on the plan markdown's effective day is not a candidate: it is listed in `rejected` once per
    option group with `covered_reason`. With no schedule nothing is covered and the output is engine-2's.
    """
    step = inp.policy.shortlist_step
    plans = [Plan(KEEP)]
    rejected: list[dict] = []
    price_free = inp.case_lot_id not in inp.bookings.lot_price
    pcts = markdown_pcts(inp) if price_free else ()
    covered = {p: r for p in pcts if (r := covered_reason(inp, p)) is not None}
    offered = tuple(p for p in pcts if p not in covered)
    plans += [Plan(MARKDOWN, p) for p in offered]
    if not price_free:
        rejected.append({"label": "Markdown", "dest": None, "run_code": None, "reasons": [PRICE_BOOKED]})
    rejected += [{"label": Plan(MARKDOWN, p).label, "dest": None, "run_code": None, "reasons": [r]}
                 for p, r in covered.items()]
    remainders = (0, *offered)
    for run in sorted(inp.runs, key=lambda r: r.run_code):
        qmax, reasons = transfer_limit(inp, run)
        if reasons:
            rejected.append({"label": f"Transfer to {run.dest} ({run.run_code})", "dest": run.dest,
                             "run_code": run.run_code, "reasons": reasons})
            continue
        rejected += [{"label": f"Transfer to {run.dest} ({run.run_code}), markdown rest {p}%", "dest": run.dest,
                      "run_code": run.run_code, "reasons": [r]} for p, r in covered.items()]
        qtys = range(1, qmax + 1) if step == 0 else sorted({*range(step, qmax + 1, step), qmax})
        plans += [Plan(TRANSFER, pct, q, run.dest, run.run_code)
                  for q in qtys for pct in remainders]
    return plans, rejected
