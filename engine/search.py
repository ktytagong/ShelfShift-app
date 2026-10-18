"""Candidate search, ranking and the case result (spec §6 Search, Ranking and outputs)."""
from __future__ import annotations

from .adjust import coerce_input, effective, validate_input
from .canonical import rec_version
from .crossover import markdown_crossover, transport_crossover, waste_price_crossover
from .limits import candidates
from .model import ENGINE_VERSION, KEEP, SCENARIOS, TRANSFER, EngineInput, Plan, Run, scheduled_pct
from .scoring import Scored, cents, rank_key, score
from .timing import fmt_at, fmt_min, latest_decision_at


def _row(sc: Scored, keep: Scored, product) -> dict:
    p, base = sc.plan, sc.sims["base"]
    shortfall = 0.0
    if p.dest:
        keep_lost = sum(v for (b, _), v in keep.sims["base"].lost_by.items() if b == p.dest)
        plan_lost = sum(v for (b, _), v in base.lost_by.items() if b == p.dest)
        shortfall = keep_lost - plan_lost
    net = {s: cents(sc.net(s)) for s in SCENARIOS}  # review IN-01: never -0.0
    policy = {s: round(sc.waste_price * sc.kg(s), 2) for s in SCENARIOS}
    return {
        "key": p.key, "label": p.label, "kind": p.kind, "pct": p.pct, "qty": p.qty,
        "dest": p.dest, "run_code": p.run_code,
        "net": net,
        # rev 6.3.0 (ENG-10): the row's score beside the net, and the base-scenario waste-policy amount
        # (waste price x kg wasted), a policy value; net and delta_vs_keep never include it. The displayed score
        # is the rounded net minus the rounded waste-policy amount, so the row adds up to the cent (review WR-03).
        # The ranking compares Scored.exact_score through rank_key (D-24; review IN-05)
        "score": {s: cents(net[s] - policy[s]) for s in SCENARIOS},
        "waste_policy": policy["base"],
        "delta_vs_keep": cents(sc.net() - keep.net()),
        "sold": round(base.sold, 2),
        "wasted_units": round(base.wasted, 2),
        "wasted_kg": round(base.wasted * product.kg_per_unit, 2),
        "shortfall_filled": cents(shortfall),
    }


def _run_deadline(inp: EngineInput, run: Run) -> dict:
    """{day, latest_decision_min, text}: the last minute a transfer on `run` can be decided (approval and prepare
    done), from `latest_decision_at`. Shared by `wait` and the run part of `decide_by` (D-21)."""
    at = latest_decision_at(run, inp.policy)
    return {"day": at // 1440, "latest_decision_min": at % 1440,
            "text": (f"transfer becomes impossible after {fmt_at(at, inp.today)} (run {run.run_code} loading cutoff "
                     f"{fmt_min(run.loading_cutoff_min)} minus {_duration(inp.policy.packing_min)} packing)")}


def _wait(inp: EngineInput, scored: list[Scored]) -> dict | None:
    """Wait is not scored: it shows when transferring stops being possible."""
    runs = {s.plan.run_code for s in scored if s.plan.kind == TRANSFER}
    if not runs:
        return None
    # The latest deadline wins; on a tie the lowest run_code is named (spec §6 Wait, rev 6.2.1).
    run = min((r for r in inp.runs if r.run_code in runs),
              key=lambda r: (-latest_decision_at(r, inp.policy), r.run_code))
    return {"run_code": run.run_code, **_run_deadline(inp, run)}


def _markdown_part(eff: EngineInput, plan: Plan) -> dict | None:
    """The markdown part of `decide_by` for a markdown plan (or a transfer plan's rest) at `plan.pct` under the
    schedule, or None when no step reaches that percent (02-CONTEXT D-21, option A).

    F is the first day any schedule step (not only the next) reaches the percent: expiry minus the largest k among
    the steps whose pct is at least it. A markdown decided after markdown_same_day_by_min - relabel_min starts the
    next day (timing.markdown_effective_day), and check_plan refuses it once its effective day reaches F. So the
    last accepted minute is day F-1 at that cutoff, or day F-2 at 23:59 when the cutoff is negative. S is the
    schedule's percent on F, the percent check_plan's covered reason names.

    The text follows the plan's kind; day, minute and source "markdown" do not. A markdown plan reads "mark down by
    <at>: from day F the schedule already has it at S%". A transfer's rest reads "decide by <at>: from day F the
    schedule already has the rest at S%", because after that minute check_plan refuses the whole transfer, not only
    its markdown (review IN-06).
    """
    case = eff.case_lot()
    ks = [k for k, p in eff.markdown_schedule if p >= plan.pct]
    if not ks:
        return None
    first = case.expiry_day - max(ks)
    cutoff = eff.policy.markdown_same_day_by_min - eff.policy.relabel_min
    at = (first - 1) * 1440 + min(cutoff, 1439) if cutoff >= 0 else (first - 1) * 1440 - 1
    s = scheduled_pct(eff.markdown_schedule, case.expiry_day, first)
    head, what = ("decide by", "the rest") if plan.kind == TRANSFER else ("mark down by", "it")
    return {"day": at // 1440, "latest_decision_min": at % 1440,
            "text": f"{head} {fmt_at(at, eff.today)}: from day {first} the schedule already has {what} at {s}%",
            "source": "markdown"}


def _deadlines(eff: EngineInput, plan: Plan) -> dict:
    """The deadline keys to merge into the result (spec §6 Supported options, rev 6.3.0, D-09, D-21).

    Result keys only: the search is unchanged.
    - `markdown_step` {day, pct, days_until}: the case lot's next schedule step after today that is deeper than
      today's price (the deeper of its recorded markdown and the schedule's percent today; a step day's price
      applies from opening, so today's step is already the price). Absent with no schedule or no deeper step.
    - `act_before` {day, latest_decision_min, text}: minute 1439 of the day before that step, an inclusive latest
      minute like `wait`'s, the reminder that the scheduled price drop starts tomorrow. Present only with
      `markdown_step`. It is never a part of `decide_by`.
    - `decide_by` {day, latest_decision_min, text, source}: the recommended plan's own deadline. decide_by is the
      last minute check_plan accepts the recommended plan (02-CONTEXT D-21, option A). Its parts: for a transfer,
      its own run's latest decision time (source "run", with `wait`'s text for that run); for a markdown or a
      transfer's rest at P > 0 under a schedule, the markdown effective-day cutoff before the first day any step
      reaches P (source "markdown", `_markdown_part`; a markdown plan's text reads "mark down by ...: ... has it at
      S%", a transfer's "decide by ...: ... has the rest at S%", since after that minute the whole transfer is
      refused, review IN-06). The earliest part by absolute minute wins, a tie going to
      "run"; absent when nothing limits the plan (Keep, a markdown no step covers). `wait` (the last chance for
      any transfer) and `act_before` keep their meanings and are reported beside it; act_before is never a part.
      Consumers read `decide_by` and never recompute it.
    """
    out: dict = {}
    act = None
    if eff.markdown_schedule:
        case = eff.case_lot()
        today_pct = max(case.markdown_pct, scheduled_pct(eff.markdown_schedule, case.expiry_day, eff.today))
        steps = sorted((case.expiry_day - k, pct) for k, pct in eff.markdown_schedule
                       if case.expiry_day - k > eff.today and pct > today_pct)
        if steps:
            day, pct = steps[0]
            at = (day - 1) * 1440 + 1439
            out["markdown_step"] = {"day": day, "pct": pct, "days_until": day - eff.today}
            act = {"day": day - 1, "latest_decision_min": 1439,
                   "text": (f"act by {fmt_at(at, eff.today)}: the schedule takes the lot to {pct}% off "
                            f"at opening on day {day}")}
            out["act_before"] = act
    parts: list[tuple[int, int, dict]] = []  # (absolute minute, rank, part); rank 0 = run, so a tie goes to "run"
    if plan.kind == TRANSFER:
        run = next(r for r in eff.runs if r.run_code == plan.run_code and r.dest == plan.dest)
        part = {**_run_deadline(eff, run), "source": "run"}
        parts.append((part["day"] * 1440 + part["latest_decision_min"], 0, part))
    if plan.pct > 0 and eff.markdown_schedule and (part := _markdown_part(eff, plan)) is not None:
        parts.append((part["day"] * 1440 + part["latest_decision_min"], 1, part))
    if parts:
        out["decide_by"] = min(parts, key=lambda p: (p[0], p[1]))[2]
    return out


def _duration(minutes: int) -> str:
    return f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes} min"


def evaluate(inp: EngineInput, with_crossovers: bool = True) -> dict:
    """The full case result for one EngineInput. Deterministic: same input, same result.

    The input is validated first (InputError names the bad field), and the result carries the
    version of the code that computed it (WR-03, WR-09). It computes on the coerced input (WR-07),
    so a Decimal or int/float form of a number gives the same result as its version.

    `effective_version` is rec_version of the effective input (facts and overrides applied), the input every
    number here is computed on. Inputs that differ only in ways the engine never sees (adjustment order, an ended
    range, row order) share it; a what-if that changes anything does not. fact_checks refuses a result whose
    effective_version is not its own input's (review WR-02).
    """
    inp = coerce_input(inp)
    validate_input(inp)
    eff = effective(inp)
    plans, rejected = candidates(eff)
    scored = score(eff, plans)
    best = scored[0]
    keep = next(s for s in scored if s.plan.kind == KEEP)

    rows: dict[tuple, Scored] = {}
    for s in scored:  # sorted best first, so the first one per group is the group's best
        group = (s.plan.kind, s.plan.pct, s.plan.dest, s.plan.run_code)
        rows.setdefault(group, s)
    by_scenario = {sc: min(scored, key=lambda s: rank_key(s, sc)).plan.key for sc in SCENARIOS}

    result = {
        "engine_version": ENGINE_VERSION,
        "recommended": best.plan.key,
        "recommended_label": best.plan.label,
        "options": [_row(s, keep, eff.product) for s in sorted(rows.values(), key=rank_key)],
        "rejected": rejected,
        "scenario_wins": {"wins": sum(1 for k in by_scenario.values() if k == best.plan.key),
                          "by_scenario": by_scenario},
        "wait": _wait(eff, scored),
        "candidates_checked": len(scored),
        "effective_version": rec_version(eff),  # review WR-02: ties the result to the input it was computed on
    }
    result.update(_deadlines(eff, best.plan))  # markdown_step, act_before, decide_by (D-21; result keys only)
    if eff.policy.shortlist_step:  # rev 6.3.0 (ENG-09): only when the shortlist is on
        result["shortlist"] = {"step": eff.policy.shortlist_step,
                               "label": f"wins among the {len(scored)} plans checked"}
    if with_crossovers:
        result["crossovers"] = {"transport": transport_crossover(eff, scored),
                                "markdown_response": markdown_crossover(eff, scored),
                                "waste_price": waste_price_crossover(eff, scored)}
    return result
