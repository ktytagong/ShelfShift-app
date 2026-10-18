"""Crossover points (spec §6 Crossover).

Each returns {"result": "threshold" | "no_change_in_range" | "multiple_changes", ...}
with the range and method used.

Each crossover states its own boundary. At the transport threshold the scores tie and rank_key's tie-breaks decide;
the markdown response reports the first grid point whose winner differs. The waste flip's winner ranks first on the
half-open interval [falls_at, rises_at), so at exactly rises_at another plan already ranks first (D-24; review IN-05).
"""
from __future__ import annotations

import math
from dataclasses import replace
from fractions import Fraction

from .model import KEEP, MARKDOWN, TRANSFER, EngineInput
from .scoring import Scored, micro, rank_key
from .simulate import simulate


def transport_crossover(inp: EngineInput, scored: list[Scored]) -> dict:
    """Exact: compares scores (spec §6, rev 6.3.0; the net at a waste price of 0). A plan's net is linear in the
    run's quote with slope -1 (uses the run) or 0 (doesn't); its kg wasted doesn't depend on the quote, so its score
    is linear in the quote with the same slope."""
    winner = scored[0]
    transfers = [s for s in scored if s.plan.kind == TRANSFER]
    if winner.plan.kind == TRANSFER:
        run_code = winner.plan.run_code
    elif transfers:
        run_code = transfers[0].plan.run_code  # the best transfer plan's run
    else:
        return {"result": "no_change_in_range", "run_code": None, "method": "exact"}
    run = next(r for r in inp.runs if r.run_code == run_code)
    c0, hi = run.fixed_cost, max(5 * run.fixed_cost, inp.policy.crossover_range_floor)
    out = {"run_code": run_code, "quote": c0, "range": [0.0, hi], "method": "exact"}

    def uses(s: Scored) -> bool:
        return s.plan.kind == TRANSFER and s.plan.run_code == run_code

    w = winner.score()
    if uses(winner):  # winner can only lose as the quote rises
        gaps = [w - s.score() for s in scored if not uses(s)]
        threshold = c0 + min(gaps) if gaps else None
    else:  # winner can only lose as the quote falls
        gaps = [w - s.score() for s in scored if uses(s)]
        threshold = c0 - min(gaps) if gaps else None
    if threshold is None or not (0.0 <= threshold <= hi):
        return {"result": "no_change_in_range", **out}
    return {"result": "threshold", "value": round(threshold, 2), "direction": "rises" if uses(winner) else "falls", **out}


def markdown_crossover(inp: EngineInput, scored: list[Scored], points: int = 21) -> dict:
    """Approximate: re-simulate the top 20 base plans plus the best of each type on a grid from low to high.

    The regrade ranks by score: each regraded plan keeps its waste price and kg per unit (rev 6.3.0).
    """
    top = scored[:20]
    for kind in (KEEP, MARKDOWN, TRANSFER):
        best = next((s for s in scored if s.plan.kind == kind), None)
        if best is not None and best not in top:
            top.append(best)
    (lo20, _, hi20), (lo30, _, hi30) = inp.markdown.m20, inp.markdown.m30
    steps = points - 1
    winners: list[str] = []
    grid: list[tuple[float, float]] = []
    for i in range(points):
        m20 = lo20 + i * (hi20 - lo20) / steps
        m30 = lo30 + i * (hi30 - lo30) / steps
        regraded = [replace(s, sims={"base": simulate(inp, s.plan, "base", m20=m20, m30=m30)}) for s in top]
        winners.append(min(regraded, key=rank_key).plan.key)
        grid.append((round(m20, 4), round(m30, 4)))
    changes = [{"m20": grid[i][0], "m30": grid[i][1], "winner": winners[i]}
               for i in range(1, points) if winners[i] != winners[i - 1]]
    out = {"range": {"m20": [lo20, hi20], "m30": [lo30, hi30]}, "method": "approx",
           "label": f"approx., step = (high - low)/{steps}, among top plans"}
    if not changes:
        return {"result": "no_change_in_range", **out}
    if len(changes) == 1:
        return {"result": "threshold", "value": changes[0], **out}
    return {"result": "multiple_changes", "values": changes, **out}


def _flip(value: float, taker: Scored) -> dict:
    return {"result": "threshold", "value": value, "key": taker.plan.key, "label": taker.plan.label}


def _first_at(scored: list[Scored], w: float) -> Scored:
    """The plan the ranking puts first when every scored plan is repriced at waste price w."""
    return min((replace(s, waste_price=w) for s in scored), key=rank_key)


def _float_at_or_above(x: Fraction) -> float:
    """The smallest float at or above x (float() is the nearest float; step up one when that is below x)."""
    f = float(x)
    return f if Fraction(f) >= x else math.nextafter(f, math.inf)


def waste_price_crossover(inp: EngineInput, scored: list[Scored]) -> dict:
    """Exact, in both directions (spec §6 Crossover, "Waste price: exact, in both directions"; rev 6.3.1, D-24).

    It uses the ranking's own convention (scoring.rank_key): each plan's base net and kg wasted rounded to 6 places
    (N = micro(net), K = micro(kg)) and the exact score N - w * K, linear in the waste price w. So each crossing
    between the winner A (scored[0], at the price in force) and another plan is an exact Fraction, and every
    reported value and taker is read from the ranking itself: the plan reported on each side of a threshold is the
    plan rank_key picks there, near-ties included. It is computed over the plans actually scored, so it follows the
    shortlist's grid when that is on. The ranking and the waste-price thresholds use the same six-decimal
    components; reported net revenue and every displayed figure are unchanged. "Exact" means exact over those
    rounded components, not exact demand or money estimates.

    - rises_at: over plans with K below A's, the lowest crossing (N_A - N_i) / (K_A - K_i). If it is in range, the
      value is the smallest float at or above it at which the ranking's first plan is not A, and the taker is the
      ranking's first plan at that value.
    - falls_at: over plans with K above A's, the highest crossing (N_i - N_A) / (K_i - K_A), reported only when
      above 0, or at exactly 0 when the ranking at 0 already puts another plan first; its value is then the
      smallest positive float and its plan the ranking's first plan at 0 (the units tie-break can differ from
      micro kg; review WR-06). The value is the smallest float at or above it at which A still ranks first, and
      the taker is the ranking's first plan at the largest float below the value.
    - Plans with K equal to A's never flip by price (this covers 0 kg per unit): their order against A doesn't
      depend on w.
    - The range is [0, hi] with hi = max(Policy.waste_flip_max, the price in force), so it always holds the price
      in force. waste_flip_max is the default reporting ceiling of the flip range, not a maximum permitted waste
      price (D-05). A crossing above hi reads "no_change_in_range", in either direction; with the price at or below
      the cap, hi is the cap. With the price above the cap every falls crossing lies at or below the price, so a
      flip between the cap and the price is reported, never hidden (review WR-01).

    The winner ranks first on the half-open interval [falls_at, rises_at): at exactly rises_at another plan already
    ranks first; at exactly falls_at A still does. The value is never rounded; a display rounds rises_at up and
    falls_at down.
    """
    a = scored[0]
    hi = max(inp.policy.waste_flip_max, inp.policy.waste_price)
    hi_x = Fraction(hi)
    n_a, k_a = micro(a.net()), micro(a.kg())
    out = {"price": inp.policy.waste_price, "range": [0.0, hi], "method": "exact",
           "rises_at": {"result": "no_change_in_range"}, "falls_at": {"result": "no_change_in_range"}}

    def is_a(s: Scored) -> bool:
        return s.plan.key == a.plan.key

    rises = [Fraction(n_a - micro(s.net()), k_a - k) for s in scored if (k := micro(s.kg())) < k_a]
    if rises and (c := min(rises)) <= hi_x:
        w = _float_at_or_above(c)
        if is_a(_first_at(scored, w)):  # an exact tie at c that the tie-breaks gave to A
            w = math.nextafter(w, math.inf)
        if w <= hi:
            out["rises_at"] = _flip(w, _first_at(scored, w))

    falls = [Fraction(micro(s.net()) - n_a, k - k_a) for s in scored if (k := micro(s.kg())) > k_a]
    # Review WR-06: a crossing at exactly 0 counts when the ranking at 0 already puts another plan first (the units
    # tie-break can differ from micro kg); the bump below then gives the smallest positive float.
    if falls and (c := max(falls)) <= hi_x and (c > 0 or (c == 0 and not is_a(_first_at(scored, 0.0)))):
        w = _float_at_or_above(c)
        if not is_a(_first_at(scored, w)):  # an exact tie at c that the tie-breaks gave to the other plan
            w = math.nextafter(w, math.inf)
        if w <= hi:
            out["falls_at"] = _flip(w, _first_at(scored, math.nextafter(w, -math.inf)))
    return out
