"""Units add up across random small networks, and a realistic case fits the 3 s budget."""
import random
import statistics
import time
from dataclasses import replace

import pytest

from engine.factchecks import fact_checks
from engine.model import ENGINE_VERSION, Capacity, EngineInput, Forecast, Inbound, Lot, MarkdownResponse
from engine.model import KEEP, MARKDOWN, Policy
from engine.limits import candidates
from engine.search import evaluate
from engine.simulate import simulate
from tests.engine.fixtures import MILK, R02, f6
from engine.baseline import baseline_plan
from engine.canonical import rec_version
from tests.engine.fixtures import f1

BRANCHES = ["A", "B", "C", "D"]


def random_network(rng: random.Random) -> EngineInput:
    today = 3
    n = rng.randint(2, 4)
    names = BRANCHES[:n]
    lots, inbound, runs, fc, cap = [], [], [], [], []
    for i, b in enumerate(names):
        for j in range(rng.randint(0 if i else 1, 2)):
            lots.append(Lot(f"{b}{j}", b, float(rng.randint(1, 400)), today + rng.randint(0, 3), rng.randint(0, 9),
                            markdown_pct=rng.choice([0, 0, 20, 30])))
        if rng.random() < 0.5:
            inbound.append(Inbound(f"in-{b}", b, float(rng.randint(1, 100)), today + rng.randint(0, 2),
                                   rng.choice([600, 900]), today + 4, 99))
        for d in range(today, today + 6):
            base = float(rng.randint(0, 120))
            fc.append(Forecast(b, d, base * 0.8, base, base * 1.2))
            cap.append(Capacity(b, d, rng.randint(0, 30)))
        if i:
            runs.append(replace(R02, run_code=f"R-{b}", dest=b, arrives_min=rng.choice([780, 900]),
                                spare_totes=rng.randint(0, 30), fixed_cost=float(rng.randint(0, 2000))))
    return EngineInput(ENGINE_VERSION, today, 540, lots[0].lot_id, MILK, tuple(lots), tuple(inbound), tuple(runs),
                       tuple(fc), tuple(cap), tuple((b, 10.0) for b in names),
                       MarkdownResponse((1.3, 43 / 30, 1.55), (1.4, 1.6, 1.8)))


@pytest.mark.parametrize("seed", range(25))
def test_units_add_up_and_transfers_add_no_stock(seed):
    rng = random.Random(seed)
    inp = random_network(rng)
    plans, _ = candidates(inp)
    for plan in rng.sample(plans, min(5, len(plans))):
        for scenario in ("low", "base", "high"):
            res = simulate(inp, plan, scenario)
            assert res.short == 0  # a plan that passed the limits never ships short on its own input
            assert res.initial == pytest.approx(res.sold + res.wasted + res.on_hand_end, abs=1e-6)
            assert res.on_hand_end == pytest.approx(0, abs=1e-6)  # the horizon reaches the last expiry


def realistic_case() -> EngineInput:
    base = f6()
    runs, fc, cap = [], list(base.forecasts), list(base.capacity)
    for i, dest in enumerate(["Lat Phrao", "Rangsit", "Bang Kapi", "Thonburi"]):
        runs.append(replace(R02, run_code=f"R-0{i + 2}", dest=dest, arrives_min=800 + 10 * i))
        if dest != "Lat Phrao":
            fc += [Forecast(dest, d, 36, 45, 54) for d in (3, 4, 5)]
            cap += [Capacity(dest, d, 16) for d in (3, 4, 5)]
    return replace(base, runs=tuple(runs), forecasts=tuple(fc), capacity=tuple(cap))


def test_case_evaluation_fits_the_3_second_budget():
    inp = realistic_case()
    start = time.perf_counter()
    result = evaluate(inp)
    fact_checks(inp, result)
    elapsed = time.perf_counter() - start
    assert result["candidates_checked"] > 1_000
    assert elapsed < 3.0, f"case evaluation took {elapsed:.2f}s"


def sus19_sized_case(shortlist_step: int = 0) -> EngineInput:
    """A case the size of the demo's milk case (SUS-19, research Q9; the plan Task 14 world shape).

    Milk (fixture money): 10-day life, 3-day delivery cycle. Today is Thu (day 3), the decision at 09:00, the case
    lot MLK-042 at Bang Na expires Sun (day 6). Lat Phrao records a 60-unit lot with zero sales yesterday, so one
    phantom fact is priced. Inbound arrives at all three branches, so the horizon runs to day 16. Schedule A
    (20% at D-2, 30% at D-1) and a waste price of 10.08 are on.

    Both destinations are short by more than 160 before the case lot expires (Lat Phrao 260, Thonburi 270), so
    storage (16 totes) binds at 160 on each run. The full search then checks 3 + 3 x (160 + 160) = 963 plans, the
    measured SUS-19 shape, and the step-10 shortlist checks 3 + 3 x (16 + 16) = 99.
    """
    bn, lp, tb = "Bang Na", "Lat Phrao", "Thonburi"
    lots = (
        Lot("MLK-042", bn, 450.0, 6, 1, last_verified_day=3),
        Lot("MLK-046", bn, 300.0, 11, 2, last_verified_day=3),
        Lot("MLK-038", lp, 60.0, 6, 0, last_verified_day=-5),  # zero sales yesterday: the phantom fact
        Lot("MLK-047", lp, 40.0, 12, 3, last_verified_day=3),
        Lot("MLK-060", tb, 80.0, 4, 0, last_verified_day=3),
        Lot("MLK-061", tb, 20.0, 9, 1, last_verified_day=-5),  # stale
    )
    inbound = (
        Inbound("IN-BN-4", bn, 280.0, 4, 360, 15, 5),
        Inbound("IN-LP-7", lp, 130.0, 7, 360, 16, 6),
        Inbound("IN-TB-7", tb, 150.0, 7, 360, 16, 7),
    )
    r03 = replace(R02, run_code="R-03", dest=tb, loading_cutoff_min=780, departs_min=810, arrives_min=900,
                  spare_totes=20, fixed_cost=700.0)
    days = range(3, 17)
    forecasts = tuple([Forecast(bn, d, 90.0, 100.0, 110.0) for d in days]
                      + [Forecast(b, d, 81.0, 90.0, 99.0) for b in (lp, tb) for d in days])
    capacity = tuple(Capacity(b, d, 16) for b in (lp, tb) for d in days)
    return EngineInput(ENGINE_VERSION, 3, 540, "MLK-042", MILK, lots, inbound, (R02, r03), forecasts, capacity,
                       ((bn, 100.0), (lp, 0.0), (tb, 90.0)),
                       MarkdownResponse((1.30, 43 / 30, 1.55), (1.40, 1.60, 1.80)),
                       policy=Policy(waste_price=10.08, shortlist_step=shortlist_step),
                       markdown_schedule=((2, 20), (1, 30)))


def median_seconds(inp: EngineInput, runs: int = 3):
    """Median wall time of `runs` runs of evaluate (both crossovers) plus fact_checks, with the last result."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        result = evaluate(inp)
        checks = fact_checks(inp, result)
        times.append(time.perf_counter() - start)
    return statistics.median(times), result, checks


def test_the_sus19_sized_case_fits_the_3_second_budget():
    """ENG-09, ROADMAP Phase 2 criterion 4: the full search on a SUS-19-sized case, median of 3 runs."""
    median, result, checks = median_seconds(sus19_sized_case())
    assert result["candidates_checked"] >= 900
    assert len(checks) >= 1
    assert median < 3.0, f"SUS-19-sized case took {median:.2f}s (median of 3)"


def test_the_shortlist_path_fits_the_budget_and_names_the_plans_checked():
    """ENG-09: the same case at shortlist step 10 checks fewer plans, says how many, and picks a plan on the grid."""
    median, result, checks = median_seconds(sus19_sized_case(shortlist_step=10))
    assert result["candidates_checked"] < 150
    assert result["shortlist"] == {"step": 10,
                                   "label": f"wins among the {result['candidates_checked']} plans checked"}
    rec = next(o for o in result["options"] if o["key"] == result["recommended"])
    assert rec["kind"] in (KEEP, MARKDOWN) or rec["qty"] % 10 == 0
    assert median < 3.0, f"SUS-19-sized case with the shortlist took {median:.2f}s (median of 3)"


def test_a_schedule_step_far_before_expiry_costs_no_more_than_an_in_range_one():
    """Review CR-01: a step's days-before-expiry value never sets the cost of a case.

    F6's horizon is Thu..Sat (today 3, every lot expires Sat = 5), so on any simulated day a lot has at most 2 days
    left. A 20% step at k = 100,000 and one at k = 2 are therefore both in force on every day of the horizon, and
    ((100_000, 20), (1, 30)) prices every day exactly as schedule A ((2, 20), (1, 30)) does. Every result key and
    every fact check must match; only the covered markdowns' "from day" differs: 5 - 100,000 = -99,995 against
    5 - 2 = 3, and the effective_version, which hashes the two different schedules. Before the fix the simulation
    built a 100,001-entry table per call (about a minute per case).
    """
    far = replace(f6(), markdown_schedule=((100_000, 20), (1, 30)))
    near = replace(f6(), markdown_schedule=((2, 20), (1, 30)))
    start = time.perf_counter()
    result = evaluate(far)
    checks = fact_checks(far, result)
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"a k = 100,000 schedule step took {elapsed:.2f}s"
    expected = evaluate(near)
    differs = ("rejected", "effective_version")
    assert {k: v for k, v in result.items() if k not in differs} == \
        {k: v for k, v in expected.items() if k not in differs}
    assert checks == fact_checks(near, expected)
    for got, want in zip(result["rejected"], expected["rejected"], strict=True):
        assert got["reasons"] == [r.replace("from day 3", "from day -99995") for r in want["reasons"]]
        assert {k: v for k, v in got.items() if k != "reasons"} == {k: v for k, v in want.items() if k != "reasons"}


def test_a_schedule_with_many_steps_costs_no_more_than_its_reduced_form():
    """D-26 (and its 2026-10-13 clarification): a schedule costs no more than its effective steps.

    F6's horizon is Thu..Sat (today 3, every lot expires Sat = 5). A 20% step at every k from 2 to 10,001 plus 30% at
    k = 1 is valid, and only its deepest-reaching step per percent can change a price: it reduces to
    ((10_001, 20), (1, 30)), which prices every simulated day alike; the covered reason's "from day" is
    5 - 10,001 = -9,996 for both. The reduction happens once, in effective(), after validation, so:
    - behavior: the full evaluate result (crossovers and effective_version included), the fact checks and
      baseline_plan equal those of the reduced schedule (the effective inputs are equal);
    - fingerprint: rec_version of the input as entered differs, since the record keeps what the user entered;
    - existing hashes: an input already in reduced form (F1 + schedule A, no adjustments) keeps
      effective_version == rec_version(input).
    Before the fix this schedule took 7.42 s on F6 with crossovers and fact checks (verification gap 5).
    """
    long = replace(f6(), markdown_schedule=tuple((k, 20) for k in range(2, 10_002)) + ((1, 30),))
    short = replace(f6(), markdown_schedule=((10_001, 20), (1, 30)))
    start = time.perf_counter()
    result = evaluate(long)
    checks = fact_checks(long, result)
    elapsed = time.perf_counter() - start
    assert elapsed < 3.0, f"a 10,001-step schedule took {elapsed:.2f}s"
    expected = evaluate(short)
    assert result == expected
    assert checks == fact_checks(short, expected)
    assert baseline_plan(long) == baseline_plan(short)
    assert rec_version(long) != rec_version(short)
    reduced_already = replace(f1(), markdown_schedule=((2, 20), (1, 30)))
    assert evaluate(reduced_already)["effective_version"] == rec_version(reduced_already)
