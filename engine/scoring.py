"""Scoring and ranking of candidate plans (spec §6 Ranking)."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .model import MOVE_RANK, SCENARIOS, EngineInput, Plan
from .simulate import SimResult, simulate


def micro(x: float) -> int:
    """x in millionths, rounded half to even, computed exactly: the integer that round(x, 6) stands for.

    The ranking and the waste-price thresholds (crossover.waste_price_crossover) compare plans on each plan's net
    and kg wasted passed through this function (rev 6.3.1, D-24). Reported net revenue and every displayed figure
    are unchanged.
    """
    return round(Fraction(x) * 1_000_000)


def cents(x: float) -> float:
    """x rounded to 2 decimal places, never -0.0 (review IN-01).

    round(-0.001, 2) is -0.0, which JSON writes as "-0.0" and a naive formatter shows as "-$0.00"; adding 0.0 after
    rounding turns a negative zero into 0.0 and leaves every other value unchanged. Every reported money and kg
    figure goes through it.
    """
    return round(x, 2) + 0.0


@dataclass
class Scored:
    plan: Plan
    sims: dict[str, SimResult]  # by scenario
    waste_price: float = 0.0  # Policy.waste_price, money per kg wasted (rev 6.3.0, ENG-10)
    kg_per_unit: float = 0.0  # Product.kg_per_unit

    def net(self, scenario: str = "base") -> float:
        return self.sims[scenario].net

    def kg(self, scenario: str = "base") -> float:
        """Kilograms wasted in the scenario."""
        return self.sims[scenario].wasted * self.kg_per_unit

    def score(self, scenario: str = "base") -> float:
        """The float score: net minus the waste-policy amount (waste price x kg wasted), shown in the option rows and
        used by the transport crossover and the fact checks.

        The ranking does not compare this float: it compares exact_score through rank_key, built from net and kg
        rounded to 6 places (rev 6.3.1, D-24; review IN-05). The waste-policy amount is a retailer policy value, not
        cash; it never changes the net. At a waste price of 0 the score equals the net.
        """
        return self.net(scenario) - self.waste_price * self.kg(scenario)

    def exact_score(self, scenario: str = "base") -> Fraction:
        """The score the ranking compares (rev 6.3.1, D-24), exactly, in millionths:
        micro(net) - waste price x micro(kg), as a Fraction (the waste price is taken exactly as the float it is).

        Only rank_key and the waste-price thresholds use it; score() stays the float for the displayed rows, the
        transport crossover and the fact checks.
        """
        return micro(self.net(scenario)) - Fraction(self.waste_price) * micro(self.kg(scenario))


def rank_key(sc: Scored, scenario: str = "base") -> tuple:
    """Best score first, then less waste, then fewer moves (keep, markdown, transfer), then the smaller quantity,
    then the plan key.

    The score is Scored.exact_score (rev 6.3.1, D-24): each plan's net and kg wasted are rounded to 6 places
    (micro), and the score micro(net) - waste price x micro(kg) is compared exactly. So:
    - the comparison between any two plans is linear in the waste price, and one exact crossing decides it (the
      waste flip reads its thresholds from the same components, so the plan it reports is the plan ranked here);
    - float noise below a millionth in a net or a kg still ties, and the tie goes to the tie-breaks;
    - at a waste price of 0 the order is engine-2's: micro(net) orders exactly as round(net, 6) (both are the
      correctly rounded 6-place value of the same float, and nets are far below the 2**53 / 10**6 at which
      distinct 6-place values could share a float), and the waste term is 0.
    The ranking and the waste-price thresholds use these same six-decimal components; reported net revenue and
    every displayed figure are unchanged. "Exact" means exact over those rounded components,
    not exact demand or money estimates.
    """
    s = sc.sims[scenario]
    return (-sc.exact_score(scenario), round(s.wasted, 6), MOVE_RANK[sc.plan.kind], sc.plan.qty, sc.plan.key)


def score(inp: EngineInput, plans: list[Plan]) -> list[Scored]:
    """Score every plan under low, base and high, sorted best first (base). `inp` must be effective."""
    scored = [Scored(p, {s: simulate(inp, p, s) for s in SCENARIOS}, inp.policy.waste_price, inp.product.kg_per_unit)
              for p in plans]
    scored.sort(key=rank_key)
    return scored


def best_base(inp: EngineInput, plans: list[Plan]) -> Scored:
    """Best base-scenario plan from nonempty candidates on effective input.

    Alternative fact checks consume only the winning base simulation. Use the same ranking and first-on-tie
    behavior as score()[0], without computing unused scenarios or sorting the remaining plans. Full option rows
    still use score() and retain all three scenarios.
    """
    return min((Scored(p, {"base": simulate(inp, p, "base")}, inp.policy.waste_price, inp.product.kg_per_unit)
                for p in plans), key=rank_key)
