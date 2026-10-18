from dataclasses import replace

import pytest

from engine.adjust import effective
from engine.crossover import markdown_crossover, transport_crossover
from engine.crossover import waste_price_crossover
from engine.limits import candidates
from engine.model import MarkdownResponse
from engine.scoring import score
from engine.adjust import validate_input
from tests.engine.fixtures import f1, with_overrides
from tests.engine.fixtures import MILK, f6
import math
from fractions import Fraction
from engine.scoring import Scored, rank_key
from engine.simulate import SimResult
from engine.model import Plan, KEEP, MARKDOWN
from engine.model import TRANSFER
from tests.engine.fixtures import LAT_PHRAO
from engine.crossover import _first_at
from engine.scoring import micro
import engine.crossover

QUOTE_4500 = {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 4_500.0}


def crossovers(inp):
    eff = effective(inp)
    scored = score(eff, candidates(eff)[0])
    return transport_crossover(eff, scored), markdown_crossover(eff, scored)


def test_f1_transport_threshold_quote_rising():
    t, _ = crossovers(f1())
    assert (t["result"], t["value"], t["direction"], t["run_code"]) == ("threshold", 3_860, "rises", "R-02")


def test_f2_transport_threshold_quote_falling():
    t, _ = crossovers(with_overrides(f1(), QUOTE_4500))
    assert (t["result"], t["value"], t["direction"]) == ("threshold", 3_860, "falls")


def test_threshold_does_not_depend_on_the_current_quote():
    t, _ = crossovers(with_overrides(f1(), {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 0.0}))
    assert (t["result"], t["value"]) == ("threshold", 3_860)


def test_no_feasible_transfer_is_no_change_in_range():
    t, _ = crossovers(with_overrides(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}))
    assert t["result"] == "no_change_in_range"


def test_threshold_below_zero_is_no_change_in_range():
    # handling at 100 per unit makes every transfer lose even if the trip were free
    t, _ = crossovers(with_overrides(f1(), {"kind": "handling_cost", "run_code": "R-02", "per_unit_cost": 100.0}))
    assert t["result"] == "no_change_in_range"


def test_markdown_grid_no_change_when_transfer_dominates():
    _, m = crossovers(f1())
    assert m["result"] == "no_change_in_range"


def test_markdown_grid_single_threshold():
    # m30 capped at 1.6 so Markdown 30% never wins; transfer + hold (12,980) loses to Markdown 20% once m20 > 1.37
    inp = replace(with_overrides(f1(), QUOTE_4500),
                  markdown=MarkdownResponse(m20=(1.30, 43 / 30, 1.55), m30=(1.40, 1.6, 1.6)))
    _, m = crossovers(inp)
    assert m["result"] == "threshold"
    assert (m["value"]["m20"], m["value"]["winner"]) == (pytest.approx(1.375), "md20")


def test_markdown_grid_multiple_changes():
    # transfer -> Markdown 20% -> Markdown 30% -> Markdown 20% across the default grid
    _, m = crossovers(with_overrides(f1(), QUOTE_4500))
    assert m["result"] == "multiple_changes"
    assert [c["winner"] for c in m["values"]] == ["md20", "md30", "md20"]
    assert m["label"] == "approx., step = (high - low)/20, among top plans"


def test_transport_range_floor_comes_from_the_policy():
    """The range is [0, max(5 x 600, 1,000)] = [0, 3,000], so F1's 3,860 threshold is out of range."""
    inp = replace(f1(), policy=replace(f1().policy, crossover_range_floor=1_000.0))
    t, _ = crossovers(inp)
    assert (t["result"], t["range"]) == ("no_change_in_range", [0.0, 3_000.0])


def test_markdown_grid_brackets_the_adopted_response():
    """F1 ends are m20 1.30-1.55 and m30 1.40-1.80; the adopted 2.5 / 2.8 become the high ends."""
    _, m = crossovers(with_overrides(f1(), {"kind": "markdown_response", "m20": 2.5, "m30": 2.8}))
    assert m["range"] == {"m20": [1.30, 2.5], "m30": [1.40, 2.8]}


def at_waste_price(inp, price):
    return replace(inp, policy=replace(inp.policy, waste_price=price))


def test_the_transport_crossover_compares_scores():
    """Rev 6.3.0 (ENG-10), spec §6 Crossover: at 10.08 F1's winner is Transfer 140, markdown rest 20% (score 16,520,
    uses R-02); the best plan without the run is Markdown 30% at 13,400 - 207.648 = 13,192.35; the threshold is
    600 + (16,520 - 13,192.35) = 3,927.65, rising."""
    t, _ = crossovers(at_waste_price(f1(), 10.08))
    assert (t["result"], t["value"], t["direction"], t["run_code"]) == ("threshold", 3_927.65, "rises", "R-02")


def test_the_markdown_grid_ranks_by_score():
    """Rev 6.3.0 (ENG-10): the regraded plans keep their waste price. F2 (quote 4,500) at 10.08, on the grid
    m20 = 1.30 + 0.0125 i, m30 = 1.40 + 0.02 i (i = 0..20), hand-derived:
    - md20 scores 10,200 m20 - 1,000 - 10.3824 (500 - 300 m20) = 13,314.72 m20 - 6,191.2;
    - md30 scores 12,114.72 m30 - 6,191.2, or 14,000 once m30 >= 5/3 (all 500 sold);
    - Transfer 140, markdown rest 20% stays 12,620 (16,520 - 3,900);
    - md30 first beats 12,620 at i = 8 (12,707.76 against md20's 12,449.41);
    - md20 overtakes md30 at i = 18 (14,113.75 > 14,000).
    Ranking by net instead gives md20 at i = 3, md30 at 13 and md20 at 14, so this catches a regrade that drops
    the waste price."""
    _, m = crossovers(at_waste_price(with_overrides(f1(), QUOTE_4500), 10.08))
    assert m["result"] == "multiple_changes"
    assert [(c["m20"], c["m30"], c["winner"]) for c in m["values"]] == \
        [(pytest.approx(1.4), pytest.approx(1.56), "md30"), (pytest.approx(1.525), pytest.approx(1.76), "md20")]


def waste(inp):
    eff = effective(inp)
    scored = score(eff, candidates(eff)[0])
    return waste_price_crossover(eff, scored)


def test_f1_waste_flip_rises_at_360_over_61_8():
    """Rev 6.3.0 (ENG-10, D-02...D-05), spec §6 Crossover. Hand crossings (kg = units x 1.03; Transfer q hold: net
    11,000 + 42q, waste 200 - q; Transfer q md20: net 15,400 + 8q, waste 0 for q >= 70, else net 13,020 + 42q,
    waste 70 - q; Transfer q md30: net 13,400 + 12q, waste 0). At 0 the winner is Transfer 140 hold (16,880;
    61.8 kg). Its crossings with lower-waste plans: md20 140 at 360/61.8 = 5.8252 (the minimum); md20 139 at
    368/61.8 = 5.95; md20 69 at 962/60.77 = 15.83; md30 at 3,480/41.2 = 84.47. No plan wastes more with a crossing
    above 0, so falls_at is no change in range at a price of 0."""
    w = waste(f1())
    assert (w["price"], w["range"], w["method"]) == (0.0, [0.0, 180.0], "exact")
    assert w["rises_at"]["result"] == "threshold"
    assert w["rises_at"]["value"] == pytest.approx(360 / 61.8, abs=1e-9)
    assert w["rises_at"]["key"] == "tr:Lat Phrao:R-02:140:md20"
    assert w["rises_at"]["label"] == "Transfer 140 to Lat Phrao (R-02), markdown rest 20%"
    assert w["falls_at"] == {"result": "no_change_in_range"}


def at(inp, w):
    return replace(inp, policy=replace(inp.policy, waste_price=w))


NO_CHANGE = {"result": "no_change_in_range"}


def test_f1_at_10_08_the_flip_falls_at_360_over_61_8():
    """Rev 6.3.0 (ENG-10): at 10.08 the winner is Transfer 140 md20 (16,520; 0 kg), so no plan wastes less. Its
    crossings with higher-waste plans: hold 140 at 360/61.8 = 5.8252 (the maximum); hold 139 at 318/62.83 = 5.06."""
    w = waste(at(f1(), 10.08))
    assert w["price"] == 10.08
    assert w["rises_at"] == NO_CHANGE
    assert w["falls_at"]["result"] == "threshold"
    assert w["falls_at"]["value"] == pytest.approx(360 / 61.8, abs=1e-9)
    assert w["falls_at"]["key"] == "tr:Lat Phrao:R-02:140:hold"
    assert w["falls_at"]["label"] == "Transfer 140 to Lat Phrao (R-02), hold rest"


def test_at_the_flip_price_the_lower_waste_plan_already_ranks_first():
    """Rev 6.3.1 (D-24) boundary: the float 360/61.8 lies just above the exact crossing 1800/309, so at it Transfer
    140 hold's exact score is below 16,520 and the lower-waste plan ranks first; at 5.82 hold scores 16,880 -
    359.676 = 16,520.32 > 16,520; so the winner ranks first on [falls_at, rises_at)."""
    def top(w):
        eff = effective(at(f1(), w))
        return score(eff, candidates(eff)[0])[0].plan.key

    assert top(360 / 61.8) == "tr:Lat Phrao:R-02:140:md20"
    assert top(5.82) == "tr:Lat Phrao:R-02:140:hold"
    falls = waste(at(f1(), 360 / 61.8))["falls_at"]
    assert falls["value"] == pytest.approx(360 / 61.8, abs=1e-9)
    assert falls["key"] == "tr:Lat Phrao:R-02:140:hold"


def test_f6_waste_flip_is_no_change_both_ways():
    """Rev 6.3.0: F6's winner, Transfer 80 md20 (18,440), wastes 0 and has the highest net, so no plan wastes less
    and every crossing with a plan wasting more is <= 0."""
    for w in (0.0, 10.08):
        flip = waste(at(f6(), w))
        assert flip["rises_at"] == NO_CHANGE
        assert flip["falls_at"] == NO_CHANGE


def test_a_flip_above_the_cap_is_no_change_but_the_range_holds_the_price_in_force():
    """Rev 6.3.0 (D-05), corrected by review WR-01. F1 at 0 with a cap of 5: the range is [0, max(5, 0)] = [0, 5]
    and the only rises crossing that matters, md20 140's 360/61.8 = 5.8252, is above it: no change in range.

    At 10.08 (a price above the cap is allowed) the range is [0, max(5, 10.08)] = [0, 10.08]. The winner is
    Transfer 140 md20 (net 15,400 + 8 x 140 = 16,520, 0 kg), so no plan wastes less: rises is no change. Its falls
    crossings, by hand: Transfer q hold has net 11,000 + 42q and waste (200 - q) x 1.03 kg, so its crossing is
    (42q - 5,520) / (1.03 (200 - q)), which grows with q; at the largest q, 140 (Lat Phrao's demand 40 + 50 + 50),
    it is 360 / 61.8 = 5.8252. Keep (11,600, 206 kg), Markdown 20 (13,620, 72.1 kg) and Transfer q md20 below
    q = 70 ((42q - 3,500) / (1.03 (70 - q)) < 0) cross at or below 0. So falls_at is 360/61.8 by hold 140, inside
    the range: every price below 5.8252, the whole [0, 5] the cap alone would show, picks hold 140, and the old
    "no change in range" there was false."""
    capped = replace(f1(), policy=replace(f1().policy, waste_flip_max=5.0))
    w = waste(capped)
    assert w["rises_at"] == NO_CHANGE
    assert w["range"] == [0.0, 5.0]
    above = at(capped, 10.08)
    assert validate_input(above) is None
    w = waste(above)
    assert w["range"] == [0.0, 10.08]
    assert w["rises_at"] == NO_CHANGE
    assert w["falls_at"]["result"] == "threshold"
    assert w["falls_at"]["value"] == pytest.approx(360 / 61.8, abs=1e-9)
    assert w["falls_at"]["key"] == "tr:Lat Phrao:R-02:140:hold"
    for price in (0.0, 2.5, 5.0):  # every price in the cap's own range picks the falls_at plan
        eff = effective(at(capped, price))
        assert score(eff, candidates(eff)[0])[0].plan.key == "tr:Lat Phrao:R-02:140:hold"


def test_at_a_waste_price_equal_to_the_cap_the_flip_range_is_the_cap():
    """D-05 at the cap (02-CONTEXT D-05, confirmed by the user 2026-10-13): Policy.waste_flip_max is the default
    reporting ceiling of the flip range, not a maximum permitted waste price. F1 with a cap of 5 at a waste price of
    exactly 5 is a valid input, and the range is [0, max(5, 5)] = [0, 5], the cap itself.

    Hand derivation: Transfer 140 hold ranks first at 5: 16,880 - 5 x 61.8 = 16,571 against Transfer 140 md20's
    16,520 (0 kg).
    - Rising: the lowest crossing with a lower-waste plan is md20 140's 1800/309 = 5.8252 (md20 139 at 368/61.8 =
      5.95, Markdown 30% at 3,480/41.2 = 84.47, the md30 transfers higher), above 5: no change in range.
    - Falling: every plan wasting more than 61.8 kg crosses at or below 0. Keep (11,600, 206 kg) and Markdown 20%
      (13,620, 72.1 kg) net less; Transfer q hold for q < 140 crosses at (42q - 5,880) / (1.03 (140 - q)) =
      -42/1.03; Transfer q md20 for q < 10 nets 13,020 + 42q < 16,880. No change in range.

    This completes the D-05 coverage: below the cap, test_f1_waste_flip_rises_at_360_over_61_8 (price 0, default
    cap 180: [0, 180]) and the price-0 half of
    test_a_flip_above_the_cap_is_no_change_but_the_range_holds_the_price_in_force ([0, 5]); above the cap, that
    test's 10.08 half ([0, 10.08]); at the cap, this test."""
    capped = replace(f1(), policy=replace(f1().policy, waste_flip_max=5.0))
    at_cap = at(capped, 5.0)
    assert validate_input(at_cap) is None
    w = waste(at_cap)
    assert (w["price"], w["range"], w["method"]) == (5.0, [0.0, 5.0], "exact")
    assert w["rises_at"] == NO_CHANGE
    assert w["falls_at"] == NO_CHANGE
    eff = effective(at_cap)
    assert score(eff, candidates(eff)[0])[0].plan.key == "tr:Lat Phrao:R-02:140:hold"


def test_without_kg_per_unit_no_plan_flips_by_price():
    """Rev 6.3.0 (1e-9 rule): with 0 kg per unit every plan's kg is 0, so the score equals the net and no plan
    flips by price."""
    inp = at(replace(f1(), product=replace(MILK, kg_per_unit=0.0)), 10.08)
    w = waste(inp)
    assert w["rises_at"] == w["falls_at"] == NO_CHANGE
    eff = effective(inp)
    assert score(eff, candidates(eff)[0])[0].plan.key == "tr:Lat Phrao:R-02:140:hold"


def _near_tie(md20_net, w):
    """Keep {net 10.0, wasted 1.0} and Markdown 20% {net md20_net, wasted 0.0}, 1 kg per unit, at waste price w,
    sorted by rank_key."""
    plans = [Scored(Plan(KEEP), {"base": SimResult(net=10.0, wasted=1.0)}, w, 1.0),
             Scored(Plan(MARKDOWN, pct=20), {"base": SimResult(net=md20_net, wasted=0.0)}, w, 1.0)]
    return sorted(plans, key=rank_key)


def test_a_near_tie_never_reports_a_falling_threshold_at_price_0():
    """Rev 6.3.1 (D-24, review IN-04), the reproduced near-tie: Keep {net 10.0, wasted 1.0} and Markdown 20%
    {net 9.9999996, wasted 0.0}, 1 kg per unit. In millionths: N = 10,000,000 for both (9,999,999.6 rounds to
    10,000,000); K = 1,000,000 against 0.
    - At any price w >= 0 Markdown's exact score 10,000,000 >= Keep's 10,000,000 - w x 1,000,000; at 0 it is a
      tie, which goes to less waste. So Markdown 20% ranks first.
    - Keep (the only plan wasting more) crosses at (10,000,000 - 10,000,000) / (1,000,000 - 0) = 0, which is not
      above 0: no falling threshold. Nothing wastes less than 0 kg: no rising threshold.
    Before: falls_at 4e-7 at price 0 (the crossing came from unrounded nets while the ranking rounded each score)."""
    for w in (0.0, 2e-7, 4e-7, 1e-6):
        scored = _near_tie(9.9999996, w)
        assert scored[0].plan.key == "md20"
        flip = waste_price_crossover(at(f1(), w), scored)
        assert flip["rises_at"] == NO_CHANGE
        assert flip["falls_at"] == NO_CHANGE


def test_the_ranking_picks_the_reported_plan_on_each_side_of_every_threshold():
    """Rev 6.3.1 (D-24, review IN-04): before, at and after each reported threshold the ranking picks the reported
    plan.

    The flipping near-tie: Keep {10.0, 1.0} against Markdown 20% {9.999999, 0.0}. In millionths N = 10,000,000
    against 9,999,999, so Keep ranks first at 0. The rising crossing is exactly 1/1,000,000; the float 1e-6 is just
    below it (Fraction(1e-6) < 1/10**6), so rises_at is math.nextafter(1e-6, inf), taken by md20. At 1e-6 Keep
    still ranks first; at the value Markdown 20% does. Repriced at that value (Markdown first), falls_at is the same
    float, taken by keep.

    F1: the exact crossing of Transfer 140 hold (16,880; 60 units = 61.8 kg) and Transfer 140 md20 (16,520; 0 kg)
    is (16,880,000,000 - 16,520,000,000) / (61,800,000 - 0) = 1800/309; Transfer 139 md20 is next at 368/61.8 =
    5.95. The float of 1800/309 (5.825242718446602) lies just above it, so rises_at at price 0 and falls_at at 10.08
    both have that value, by Transfer 140 md20 and Transfer 140 hold. At the float just below it Transfer 140 hold
    ranks first; at the value Transfer 140 md20 does. Before: 5.825242718446601, below the exact crossing."""
    v = math.nextafter(1e-6, math.inf)
    flip = waste_price_crossover(at(f1(), 0.0), _near_tie(9.999999, 0.0))
    assert flip["rises_at"] == {"result": "threshold", "value": v, "key": "md20", "label": "Markdown 20%"}
    assert flip["falls_at"] == NO_CHANGE
    assert _near_tie(9.999999, 1e-6)[0].plan.key == "keep"
    assert _near_tie(9.999999, v)[0].plan.key == "md20"
    repriced = waste_price_crossover(at(f1(), v), _near_tie(9.999999, v))
    assert repriced["falls_at"] == {"result": "threshold", "value": v, "key": "keep", "label": "Keep"}
    assert repriced["rises_at"] == NO_CHANGE

    def top(w):
        eff = effective(at(f1(), w))
        return score(eff, candidates(eff)[0])[0].plan.key

    r = waste(f1())["rises_at"]["value"]
    below = math.nextafter(r, -math.inf)
    assert Fraction(r) >= Fraction(1800, 309) > Fraction(below)
    assert waste(f1())["rises_at"]["key"] == "tr:Lat Phrao:R-02:140:md20"
    at_r, below_r = top(r), top(below)
    assert at_r == "tr:Lat Phrao:R-02:140:md20"
    assert below_r == "tr:Lat Phrao:R-02:140:hold"
    falls = waste(at(f1(), 10.08))["falls_at"]
    assert (falls["value"], falls["key"]) == (r, "tr:Lat Phrao:R-02:140:hold")


def _units_tie(w):
    """Keep {net 100.0, wasted 1.0000002} and Transfer 1 to Lat Phrao (R-02), hold rest {net 100.0, wasted 1.0},
    5 kg per unit, at waste price w, sorted by rank_key (review WR-06)."""
    plans = [Scored(Plan(KEEP), {"base": SimResult(net=100.0, wasted=1.0000002)}, w, 5.0),
             Scored(Plan(TRANSFER, pct=0, qty=1, dest=LAT_PHRAO, run_code="R-02"),
                    {"base": SimResult(net=100.0, wasted=1.0)}, w, 5.0)]
    return sorted(plans, key=rank_key)


def test_a_falling_threshold_at_exactly_0_is_reported_when_the_ranking_at_0_picks_another_plan():
    """Review WR-06 (02-CONTEXT D-30), the reproduced boundary. 5 kg per unit:
    - Keep {net 100.0, wasted 1.0000002}: N = 100,000,000 millionths, K = micro(5.000001) = 5,000,001.
    - Transfer 1 to Lat Phrao (R-02), hold rest {net 100.0, wasted 1.0}: N = 100,000,000, K = 5,000,000.
    At w = 5 the Transfer's exact score is 100,000,000 - 25,000,000 = 75,000,000, above Keep's 100,000,000 -
    25,000,005 = 74,999,995, so the Transfer is the winner A. Keep wastes more and crosses at (N_keep - N_A) /
    (K_keep - K_A) = 0 / 1 = 0. At w = 0 the scores tie; the units tie-break ties too (round(1.0000002, 6) = 1.0 =
    1.0, while the score compares kg in millionths, 5,000,001 against 5,000,000); the move rank gives Keep (0)
    before the Transfer (2). For every w > 0 the Transfer scores higher by w x 1 millionth. So the winner changes
    between 0 and the smallest positive float: falls_at = math.nextafter(0.0, inf), taken by Keep.
    Before: falls_at no change in range (only a crossing above 0 was admitted), which claimed the Transfer holds
    all the way down to 0. D-24: the plan reported on each side of a threshold is the plan rank_key picks there.

    At a price in force of 0 the same plans give A = Keep, no falling threshold, and rises_at =
    math.nextafter(0.0, inf) by the Transfer (this half already held)."""
    tiny = math.nextafter(0.0, math.inf)
    tr_key, tr_label = "tr:Lat Phrao:R-02:1:hold", "Transfer 1 to Lat Phrao (R-02), hold rest"
    plans = _units_tie(5.0)
    keep, tr = (next(s for s in plans if s.plan.key == k) for k in ("keep", tr_key))
    assert (micro(keep.net()), micro(keep.kg())) == (100_000_000, 5_000_001)
    assert (micro(tr.net()), micro(tr.kg())) == (100_000_000, 5_000_000)
    assert round(keep.sims["base"].wasted, 6) == round(tr.sims["base"].wasted, 6) == 1.0
    assert plans[0].plan.key == tr_key
    flip = waste_price_crossover(at(f1(), 5.0), plans)
    assert flip["rises_at"] == NO_CHANGE
    assert flip["falls_at"] == {"result": "threshold", "value": tiny, "key": "keep", "label": "Keep"}
    assert _first_at(plans, 0.0).plan.key == "keep"
    assert _first_at(plans, tiny).plan.key == tr_key

    at_0 = _units_tie(0.0)
    assert at_0[0].plan.key == "keep"
    flip = waste_price_crossover(at(f1(), 0.0), at_0)
    assert flip["falls_at"] == NO_CHANGE
    assert flip["rises_at"] == {"result": "threshold", "value": tiny, "key": tr_key, "label": tr_label}


def test_the_ranking_docs_name_the_exact_score_and_the_half_open_interval():
    """Review IN-05 (D-30): the words D-24 made stale describe the ranking the code runs. Scored.score is the float
    score shown in the rows and used by the transport crossover and the fact checks; the ranking compares
    Scored.exact_score through rank_key (D-24). The crossover module states each crossover's own boundary: the waste
    flip's winner ranks first on [falls_at, rises_at), so at exactly rises_at the tie does not go to the current
    winner."""
    score_doc = Scored.score.__doc__
    assert "exact_score" in score_doc
    assert "rank_key" in score_doc
    assert "The ranking score:" not in score_doc
    module_doc = engine.crossover.__doc__
    assert "[falls_at, rises_at)" in module_doc
    assert "Ties go to the current winner" not in module_doc
