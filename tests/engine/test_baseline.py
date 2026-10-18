from dataclasses import replace

import pytest

from engine.adjust import InputError, effective
from engine.baseline import baseline_plan
from engine.model import KEEP, MARKDOWN, TRANSFER, Bookings, Forecast, Lot, Plan
from engine.simulate import simulate
from engine.simulate import HorizonError
from engine.search import evaluate
from tests.engine.fixtures import BANG_NA, FRI, LAT_PHRAO, SAT, THU, f1, f6, with_overrides
from tests.engine.fixtures import R02, with_facts
from engine.baseline import NEAR_EXPIRY_DAYS

TOL = 0.01


def test_surplus_goes_to_the_short_branch_up_to_the_limits():
    assert baseline_plan(f1()) == Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")


def test_baseline_acts_on_the_record():
    # on the record Lat Phrao is short only 80, so the rule sends 80 (it never verifies)
    assert baseline_plan(f6()) == Plan(TRANSFER, 0, 80, LAT_PHRAO, "R-02")


def test_no_surplus_keeps():
    inp = f1()
    small = replace(inp, lots=(replace(inp.lots[0], qty=250.0),))
    assert baseline_plan(small) == Plan(KEEP)


def near_expiry(**lot_changes):
    inp = f1()
    return replace(inp, lots=(replace(inp.lots[0], expiry_day=FRI, **lot_changes),))


NO_COLD_CHAIN = {"kind": "cold_chain", "run_code": "R-02", "value": False}


def test_near_expiry_the_surplus_the_transfer_cannot_take_is_marked_down():
    # surplus 500 - 200 = 300; Lat Phrao can take 40 + 50 = 90 before Fri; the other 210 go 30% off
    assert baseline_plan(near_expiry()) == Plan(TRANSFER, 30, 90, LAT_PHRAO, "R-02")


def test_near_expiry_with_no_transfer_marks_down_30():
    assert baseline_plan(with_overrides(near_expiry(), NO_COLD_CHAIN)) == Plan(MARKDOWN, 30)


def test_near_expiry_lot_already_at_30_keeps():
    assert baseline_plan(with_overrides(near_expiry(markdown_pct=30), NO_COLD_CHAIN)) == Plan(KEEP)


def test_baseline_rejects_an_input_from_another_engine_version():
    with pytest.raises(InputError, match="engine_version"):
        baseline_plan(replace(f1(), engine_version="engine-0"))


def test_stock_that_outlives_the_case_lot_is_not_its_surplus():
    """Case lot 250 (Sat) plus 400 expiring day 9 at Bang Na, demand 100/day. Only stock expiring
    by Sat counts: 250 on hand < 300 demand through Sat, so Keep. The old rule counted all 650
    and sent 140 (WR-05)."""
    inp = f1()
    later = Lot("MLK-050", BANG_NA, 400.0, 9, received_seq=2, last_verified_day=THU)
    forecasts = tuple(Forecast(BANG_NA, d, 100.0, 100.0, 100.0) for d in range(THU, 10)) + tuple(
        Forecast(LAT_PHRAO, d, v, v, v) for d, v in {THU: 40.0, FRI: 50.0, SAT: 50.0, 6: 50.0, 7: 50.0, 8: 50.0, 9: 50.0}.items())
    inp = replace(inp, lots=(replace(inp.lots[0], qty=250.0), later), forecasts=forecasts)
    assert baseline_plan(inp) == Plan(KEEP)


def test_units_set_aside_for_other_cases_are_not_surplus():
    """F1 with 150 of MLK-042 set aside for other cases: 500 - 150 = 350 on hand, 350 - 300 = 50
    surplus, under the run's cap of 140. Sat is not near expiry, so the rest is held (WR-05)."""
    inp = replace(f1(), bookings=Bookings(lot_units=(("MLK-042", 150.0),)))
    assert baseline_plan(inp) == Plan(TRANSFER, 0, 50, LAT_PHRAO, "R-02")


def test_surplus_is_capped_at_the_case_lots_own_quantity():
    """Case lot 90 (Fri) beside MLK-040, 500 expiring Thu. FEFO walk (D-21): MLK-040 sells min(500, 100)
    = 100 by Thu (its other 400 are waste whatever happens); the case lot sells min(90, 200 - 100) = 90
    by Fri, leaving 0, so Keep. Keep: 190 x 40 - 400 x 2 = 6,800. The old count (590 - 200 = 390, capped
    at 90) sent Transfer 90: Bang Na 100 + Lat Phrao 90 = 190 x 40 - 600 - 800 = 6,200."""
    inp = f1()
    other = Lot("MLK-040", BANG_NA, 500.0, THU, received_seq=0, last_verified_day=THU)
    inp = replace(inp, lots=(replace(inp.lots[0], qty=90.0, expiry_day=FRI), other))
    assert baseline_plan(inp) == Plan(KEEP)


def test_stock_that_expired_before_today_is_not_surplus():
    """The reviewer's repro (round-2 WR-04). F1 with the case lot at 250 and MLK-041 (200) confirmed as
    expiring Wed. MLK-041 has expired, so it is out of the FEFO walk; the case lot sells min(250, 300)
    = 250, leaving 0: Keep. Keep: Bang Na sells 100 + 100 + 50 = 250 x 40 = 10,000, MLK-041's 200 are
    waste on Thu (400): 9,600. Transfer 140: Bang Na keeps 110 and sells 100 + 10 = 4,400, Lat Phrao
    sells 140 x 40 = 5,600, transport 600, disposal 400: 9,000. The old rule counted 450 - 300 = 150
    and sent 140."""
    inp = f1()
    sibling = Lot("MLK-041", BANG_NA, 200.0, SAT, received_seq=0, last_verified_day=THU)
    inp = replace(inp, lots=(replace(inp.lots[0], qty=250.0), sibling))
    inp = with_facts(inp, {"kind": "expiry", "lot_id": "MLK-041", "expiry_day": THU - 1})
    assert baseline_plan(inp) == Plan(KEEP)
    eff = effective(inp)
    assert simulate(eff, Plan(KEEP)).net == pytest.approx(9_600, abs=TOL)
    assert simulate(eff, Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")).net == pytest.approx(9_000, abs=TOL)


def test_an_earlier_lot_takes_only_the_demand_before_its_own_expiry():
    """Case lot 250 (Sat, seq 1) beside MLK-041 (Thu, seq 0), demand 100/day (D-21).
    Sibling 200, R-02 as in F1: the sibling sells min(200, 100) = 100; the case lot sells
    min(250, 300 - 100) = 200, leaving 50, under the run's cap of 140; Sat - Thu = 2, so the rest is
    held: Transfer 50. The old rule counted 450 - 300 = 150 and sent 140.
    Sibling 100, R-02 moved to Fri: the sibling sells 100, the case lot 200, leaving 50; the run sells
    from Fri, so its cap is 50 + 50 = 100: Transfer 50. An unexpired earlier lot is never dropped for
    expiring before the run arrives (D-17's dropped wording would have kept, wasting the 50)."""
    inp = f1()
    case = replace(inp.lots[0], qty=250.0)
    big = Lot("MLK-041", BANG_NA, 200.0, THU, received_seq=0, last_verified_day=THU)
    assert baseline_plan(replace(inp, lots=(case, big))) == Plan(TRANSFER, 0, 50, LAT_PHRAO, "R-02")
    small = replace(big, qty=100.0)
    fri_run = replace(inp, lots=(case, small), runs=(replace(R02, day=FRI),))
    assert baseline_plan(fri_run) == Plan(TRANSFER, 0, 50, LAT_PHRAO, "R-02")


# R-A: Thu, cutoff 22:00 (decide by 20:00), arrives Fri 02:00 = 3 x 1440 + 1560 = 5,880.
R_A = replace(R02, run_code="R-A", loading_cutoff_min=1320, departs_min=1350, arrives_min=1560)
# R-B: Fri, cutoff 00:15 (decide by Thu 22:15), arrives Fri 01:30 = 4 x 1440 + 90 = 5,850.
R_B = replace(R02, run_code="R-B", day=FRI, loading_cutoff_min=15, departs_min=30, arrives_min=90)


def test_the_earliest_arrival_counts_day_and_minute_together():
    """D-17 / round-2 WR-05. F1 surplus 500 - 300 = 200. Both runs pass at Thu 09:00 and both sell from
    Fri, so each cap is 50 + 50 = 100 (storage 160, truck 220). R-B arrives first (5,850 < 5,880):
    Transfer 100 via R-B, hold. The old key (day, arrives_min) put (3, 1560) before (4, 90)."""
    assert baseline_plan(replace(f1(), runs=(R_A, R_B))) == Plan(TRANSFER, 0, 100, LAT_PHRAO, "R-B")


def test_runs_arriving_together_go_to_the_lowest_run_code():
    """Two runs arriving at the same minute (5,850): the tie goes to the lowest run_code, R-B."""
    runs = (replace(R_B, run_code="R-C"), R_B)
    assert baseline_plan(replace(f1(), runs=runs)) == Plan(TRANSFER, 0, 100, LAT_PHRAO, "R-B")


def test_a_missing_source_forecast_day_raises_like_evaluate():
    """Round-3 WR-03 (the review's repro): F1 with the case lot at 250 keeps (Bang Na sells 100 a day Thu-Sat, 300
    through Sat, so all 250 sell). Remove Bang Na's Fri forecast: the old rule read the missing day as 0 demand,
    so only 200 sold through Sat, a surplus of 50 appeared, and it sent Transfer 50 to Lat Phrao (R-02). evaluate
    raises HorizonError on the same input; the baseline must too, never reading a missing day as zero demand."""
    inp = f1()
    x = replace(inp, lots=(replace(inp.lots[0], qty=250.0),),
                forecasts=tuple(f for f in inp.forecasts if not (f.branch == BANG_NA and f.day == FRI)))
    with pytest.raises(HorizonError, match="Bang Na on day 4"):
        baseline_plan(x)
    with pytest.raises(HorizonError, match="Bang Na on day 4"):
        evaluate(x)


def test_the_near_expiry_step_comes_from_the_policy():
    """Round-3 IN-03: surplus 500 - 200 = 300; Lat Phrao takes 40 + 50 = 90 before Fri; Fri - Thu = 1 is not <= 0,
    so it is not near and the rest is held; at 20% the rest goes 20% off. The engine-2 constant still imports from
    engine.baseline with its value (D-20)."""
    assert NEAR_EXPIRY_DAYS == 1
    pol = lambda inp, **kw: replace(inp, policy=replace(inp.policy, **kw))  # noqa: E731
    assert baseline_plan(pol(near_expiry(), baseline_near_expiry_days=0)) == Plan(TRANSFER, 0, 90, LAT_PHRAO, "R-02")
    assert baseline_plan(pol(near_expiry(), baseline_markdown_pct=20)) == Plan(TRANSFER, 20, 90, LAT_PHRAO, "R-02")
    assert baseline_plan(pol(with_overrides(near_expiry(), NO_COLD_CHAIN), baseline_markdown_pct=20)) == \
        Plan(MARKDOWN, 20)


def test_the_baseline_never_sees_the_waste_price():
    """Rev 6.3.0 (ENG-10): the store rule is the fair comparison, so a waste price changes nothing in it: F1 gives
    Transfer 140 hold and F6 Transfer 80 hold at 10.08, as at 0."""
    at = lambda inp: replace(inp, policy=replace(inp.policy, waste_price=10.08))  # noqa: E731
    assert baseline_plan(at(f1())) == Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")
    assert baseline_plan(at(f6())) == Plan(TRANSFER, 0, 80, LAT_PHRAO, "R-02")


def test_the_store_rule_counts_the_schedules_lift():
    """Spec §6 rev 6.3.0 (D-11, D-12): with schedule A = ((2, 20), (1, 30)) the case lot (500, Sat) is 20% off on Thu
    and 30% off on Fri and Sat. On base demand 100/day with m20 = 43/30 and m30 = 1.6 it sells 143.33 + 160 + 160 =
    463.33, leaving 36.67, so the surplus is 36. The cap is 140, and with a schedule the rest is held: Transfer 36.
    Without the schedule the rule is D-21 exactly: Transfer 140 hold."""
    assert baseline_plan(replace(f1(), markdown_schedule=((2, 20), (1, 30)))) == Plan(TRANSFER, 0, 36, LAT_PHRAO, "R-02")
    assert baseline_plan(f1()) == Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")


A = ((2, 20), (1, 30))


def test_with_a_schedule_the_deeper_tier_sells_first():
    """Two tiers on one day (spec §6 rev 6.3.0, D-12). The case lot is 400, expiring Sat, received second (seq 1);
    MLK-041 is 80, expiring Fri, received first (seq 0); Bang Na demand 100/day. Under A on Thu, MLK-041 is at D - 1 =
    30%: rate 160, f = 80/160 = 0.5, so it sells 80. The case lot at 20% gets the other 0.5 of the day at 143.33/day:
    71.67. Fri sells 160 (30%) and Sat 160. 400 - 391.67 = 8.33, so Transfer 8 hold. Without the schedule (D-21):
    MLK-041 sells min(80, 200) = 80, the case lot min(400, 300 - 80) = 220, leaving 180, capped at 140: Transfer 140
    hold."""
    inp = f1()
    sibling = Lot("MLK-041", BANG_NA, 80.0, FRI, received_seq=0, last_verified_day=THU)
    inp = replace(inp, lots=(replace(inp.lots[0], qty=400.0), sibling))
    assert baseline_plan(replace(inp, markdown_schedule=A)) == Plan(TRANSFER, 0, 8, LAT_PHRAO, "R-02")
    assert baseline_plan(inp) == Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")


def test_the_near_expiry_step_applies_only_without_a_schedule():
    """near_expiry() (the lot expires Fri) under A: Thu and Fri are at 30% (D - 1 and D), 160 + 160 = 320 sold, 180
    left; the cap is 40 + 50 = 90, and the schedule is the store's markdown, so the rest is held: Transfer 90 hold.
    With no cold chain no run passes and the rule keeps. Without the schedule these stay Transfer 90 with the rest 30%
    off and Markdown 30 (the existing tests)."""
    assert baseline_plan(replace(near_expiry(), markdown_schedule=A)) == Plan(TRANSFER, 0, 90, LAT_PHRAO, "R-02")
    assert baseline_plan(with_overrides(replace(near_expiry(), markdown_schedule=A), NO_COLD_CHAIN)) == Plan(KEEP)


def test_with_a_schedule_the_store_rule_still_ignores_the_waste_price():
    """ENG-10 under a schedule: F1 + A gives Transfer 36 hold at waste price 10.08, as at 0."""
    inp = replace(f1(), markdown_schedule=A)
    at = replace(inp, policy=replace(inp.policy, waste_price=10.08))
    assert baseline_plan(at) == Plan(TRANSFER, 0, 36, LAT_PHRAO, "R-02")
    assert baseline_plan(inp) == Plan(TRANSFER, 0, 36, LAT_PHRAO, "R-02")


def test_a_missing_source_forecast_still_raises_with_a_schedule():
    """Round-3 WR-03 holds under a schedule: the day-by-day walk needs every source day, so F1 + A with Bang Na's Fri
    forecast removed raises HorizonError before the walk, never reading the day as zero demand."""
    inp = f1()
    x = replace(inp, markdown_schedule=A,
                forecasts=tuple(f for f in inp.forecasts if not (f.branch == BANG_NA and f.day == FRI)))
    with pytest.raises(HorizonError, match="Bang Na on day 4"):
        baseline_plan(x)


def test_the_store_rule_keeps_a_lots_recorded_markdown_under_a_schedule():
    """D-22: under a schedule each walk lot sells each day at the deeper of its recorded markdown and its own schedule
    step, as in the projection and the world. F1: Bang Na demand 100/day Thu-Sat, D = Sat; base m20 = 43/30, so
    143.33/day; base m30 = 1.6, so 160/day; R-02's cap is 140.
    The reproduced case: the case lot is 470, recorded at 30%, schedule ((0, 20),). Each day the lot is at
    max(30, 0 or 20) = 30%: it sells 160 Thu, 160 Fri and the remaining 150 Sat (f = 150/160). Nothing is left, so the
    surplus is 0 and the rule keeps. The Keep projection also prices the lot at its recorded 30%, so Keep wastes 0.
    (Pricing at the schedule only gave Thu and Fri at full price, 100 each, 270 left, and Sat at 20%, 143.33, 126.67
    left: Transfer 126 hold.)
    Recorded earlier than the schedule: the case lot is 500, recorded at 20%, schedule ((0, 30),). Thu and Fri are at
    max(20, 0) = 20%: 143.33 each, 213.33 left. Sat is at max(20, 30) = 30%: 160, 53.33 left. The surplus is 53, under
    the cap 140: Transfer 53 hold. (Schedule only: Thu and Fri at full price, 300 left, Sat 160, 140 left: Transfer 140
    hold.)"""
    inp = replace(f1(), lots=(replace(f1().lots[0], qty=470.0, markdown_pct=30),), markdown_schedule=((0, 20),))
    assert baseline_plan(inp) == Plan(KEEP)
    keep = next(o for o in evaluate(inp, with_crossovers=False)["options"] if o["key"] == "keep")
    assert keep["wasted_units"] == 0.0
    earlier = replace(f1(), lots=(replace(f1().lots[0], markdown_pct=20),), markdown_schedule=((0, 30),))
    assert baseline_plan(earlier) == Plan(TRANSFER, 0, 53, LAT_PHRAO, "R-02")


def test_under_a_schedule_the_store_rule_sells_stock_that_outlives_the_case_lot():
    """Review WR-07 (02-CONTEXT D-30). F1: Bang Na demand 100/day, D = Sat; base m20 = 43/30, so 143.33/day; base
    m30 = 1.6, so 160/day; R-02 arrives Thu 13:20 with cap 140 = min(Lat Phrao unmet Thu 40 + Fri 50 + Sat 50, storage
    160, truck 220, stock 330). The case lot MLK-042 is 330, expiring Sat, recorded at 0%. MLK-050 is 300 at Bang Na,
    expiring Sun (received second, seq 2), recorded at 30%. Sun forecasts are Bang Na 100 and Lat Phrao 50 (the
    projection runs to the last lot expiry). Schedule ((0, 20),): 20% on each lot's own expiry day.
    With the fix the scheduled walk also takes MLK-050. Thu: MLK-050 is at max(30, 0) = 30%, sells 160/day and has 300,
    so it takes the whole day; the case lot at 0% sells 0, and MLK-050 has 140 left. Fri: MLK-050's 140 at 30% last
    f = 140/160 = 0.875 of the day; the other 0.125 x 100 = 12.5 sells from the case lot at full price, leaving 317.5.
    Sat: the case lot is at max(0, 20) = 20%, 143.33/day, leaving 317.5 - 143.33 = 174.17. The surplus is 174, so
    Transfer min(174, 140) = 140, the rest held (the schedule is the store's markdown). The Keep projection sells the
    same way, so Keep wastes 174.17, all of it the case lot (MLK-050 sells out by Fri). Before the fix the walk had the
    case lot alone: 100 Thu, 100 Fri, the remaining 130 Sat at 20% (143.33 > 130), nothing left: Keep.
    MLK-050 recorded at 0% (control): Thu and Fri both lots are in the 0% tier and FEFO puts the case lot first,
    100 + 100, so 130 is left; Sat the case lot's 20% tier sells first, 130 of 143.33 (f = 0.907), and the rest of the
    day goes to MLK-050. The case lot ends at 0: Keep, before and after the fix.
    No schedule: engine-2's walk (01-CONTEXT D-21), kept by D-30, takes the case lot alone; through Sat it sells
    min(330, 300) = 300, so 30 is left; the lot expires in 2 > 1 days, so there is no near-expiry step: Transfer 30
    hold. (This understates the surplus here; the projection wastes 217.5. The gap is left to the user, D-30.)"""
    later = Lot("MLK-050", BANG_NA, 300.0, 6, received_seq=2, markdown_pct=30, last_verified_day=THU)
    inp = replace(f1(), lots=(replace(f1().lots[0], qty=330.0), later),
                  forecasts=f1().forecasts + (Forecast(BANG_NA, 6, 100.0, 100.0, 100.0),
                                              Forecast(LAT_PHRAO, 6, 50.0, 50.0, 50.0)),
                  markdown_schedule=((0, 20),))
    assert baseline_plan(inp) == Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")
    keep = next(o for o in evaluate(inp, with_crossovers=False)["options"] if o["key"] == "keep")
    assert keep["wasted_units"] == pytest.approx(174.17, abs=TOL)
    assert baseline_plan(replace(inp, lots=(inp.lots[0], replace(later, markdown_pct=0)))) == Plan(KEEP)
    assert baseline_plan(replace(inp, markdown_schedule=())) == Plan(TRANSFER, 0, 30, LAT_PHRAO, "R-02")
