from dataclasses import replace

import pytest

from engine.adjust import AdjustmentError, InputError, effective
from engine.limits import candidates, check_plan, transfer_limit, unmet_by_day
from engine.model import KEEP, MARKDOWN, TRANSFER, Bookings, Plan, Policy
from engine.simulate import HorizonError
from engine.timing import fmt_min, latest_decision_at, latest_decision_min
from tests.engine.fixtures import FRI, LAT_PHRAO, R02, SAT, THU, f1, f6, with_facts, with_overrides
from tests.engine.fixtures import BANG_NA, _flat

# A Fri 01:00 loading cutoff with 2 h packing means deciding by Thu 23:00.
OVERNIGHT = replace(R02, day=FRI, loading_cutoff_min=60, departs_min=90, arrives_min=140)
PLAN140 = Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")


def limit(inp):
    eff = effective(inp)
    return transfer_limit(eff, eff.runs[0])


def overnight(minute):
    return with_overrides(replace(f1(), runs=(OVERNIGHT,)), {"kind": "decide_at", "minute": minute})


def test_f1_cap_is_unmet_demand_140():
    assert limit(f1()) == (140, [])  # 14 totes <= 16 storage and <= 22 truck


def test_f6_cap_on_the_record_is_80():
    assert unmet_by_day(effective(f6()), LAT_PHRAO) == {THU: 0.0, FRI: 30.0, SAT: 50.0}
    assert limit(f6()) == (80, [])


def test_cold_chain():
    assert limit(with_overrides(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False})) == (0, ["cold chain"])


def test_run_unavailable():
    assert limit(with_overrides(f1(), {"kind": "run_available", "run_code": "R-02", "value": False})) == \
        (0, ["run unavailable"])


def test_decide_at_1030_misses_the_1000_deadline():
    assert limit(with_overrides(f1(), {"kind": "decide_at", "minute": 630})) == (0, ["past decision deadline 10:00"])


def test_decide_at_exactly_1000_is_allowed():
    assert limit(with_overrides(f1(), {"kind": "decide_at", "minute": 600}))[0] == 140


def test_min_sellable_days():
    inp = f1()
    short = replace(inp, lots=(replace(inp.lots[0], expiry_day=THU),))
    late = with_overrides(short, {"kind": "departure_delay_h", "run_code": "R-02", "hours": 1.0})
    assert limit(late) == (0, ["not enough sellable days at destination"])


def test_totes_use_ceil_and_one_lot_per_tote():
    one = with_overrides(f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": SAT,
                                "free_totes": 1})
    assert limit(one) == (10, [])


def test_storage_must_be_free_every_day_until_expiry():
    full_fri = with_overrides(f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": FRI, "day_to": FRI,
                                     "free_totes": 0})
    assert limit(full_fri) == (0, ["no free storage at destination"])


def test_other_cases_bookings_reduce_the_caps():
    held = replace(f1(), bookings=Bookings(demand=((LAT_PHRAO, SAT, 50.0),), run_totes=(("R-02", 20),)))
    assert limit(held) == (20, [])  # truck: 22 - 20 = 2 totes = 20 units (demand alone would allow 90)


def test_candidates_cover_every_quantity_and_remainder():
    plans, rejected = candidates(effective(f1()))
    transfers = [p for p in plans if p.kind == TRANSFER]
    assert len(plans) == 3 + 140 * 3 and rejected == []
    assert {p.qty for p in transfers} == set(range(1, 141))


def test_price_right_taken_blocks_markdowns():
    inp = replace(f1(), bookings=Bookings(lot_price=("MLK-042",)))
    plans, rejected = candidates(effective(inp))
    assert {p.pct for p in plans} == {0}
    assert rejected[0]["reasons"] == ["price change already booked by another case"]


def test_set_aside_larger_than_the_lot_leaves_nothing_to_move():
    inp = replace(f1(), bookings=Bookings(lot_units=(("MLK-042", 600.0),)))
    assert limit(inp) == (0, ["no stock to move"])


def test_destination_with_no_demand_is_rejected():
    inp = f1()
    no_demand = replace(inp, forecasts=tuple(replace(f, low=0.0, base=0.0, high=0.0) if f.branch == LAT_PHRAO else f
                                             for f in inp.forecasts))
    assert limit(no_demand) == (0, ["no unmet demand at destination"])


def test_run_on_a_later_day_keeps_its_own_deadline():
    inp = f1()
    friday = replace(inp, runs=(replace(inp.runs[0], day=FRI),))
    late = with_overrides(friday, {"kind": "decide_at", "minute": 630})  # 10:30 Thu is before 10:00 Fri
    assert limit(late) == (100, [])  # sellable from Fri: 50 + 50


def test_two_bookings_in_one_bucket_add_up():
    one = replace(f1(), bookings=Bookings(demand=((LAT_PHRAO, SAT, 50.0),), totes=((LAT_PHRAO, FRI, 10),)))
    two = replace(f1(), bookings=Bookings(demand=((LAT_PHRAO, SAT, 25.0), (LAT_PHRAO, SAT, 25.0)),
                                          totes=((LAT_PHRAO, FRI, 5), (LAT_PHRAO, FRI, 5))))
    assert limit(two) == limit(one) == (60, [])  # storage: 16 - 10 = 6 totes on Fri


def test_missing_capacity_row_fails_loudly():
    inp = f1()
    gap = replace(inp, capacity=tuple(c for c in inp.capacity if c.day != SAT))
    with pytest.raises(HorizonError, match="no capacity for Lat Phrao on day 5"):
        limit(gap)


def test_markdowns_never_raise_the_price():
    inp = f1()
    at20 = replace(inp, lots=(replace(inp.lots[0], markdown_pct=20),))
    plans, _ = candidates(effective(at20))
    assert {p.key for p in plans if p.kind == MARKDOWN} == {"md30"}
    assert {p.pct for p in plans if p.kind == TRANSFER} == {0, 30}
    at30 = replace(inp, lots=(replace(inp.lots[0], markdown_pct=30),))
    assert {p.pct for p in candidates(effective(at30))[0]} == {0}


def test_check_plan_names_every_broken_limit():
    eff = effective(f1())
    assert check_plan(eff, Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02")) == []
    assert check_plan(eff, Plan(TRANSFER, 0, 141, LAT_PHRAO, "R-02")) == ["no unmet demand at destination"]
    assert check_plan(eff, Plan(TRANSFER, 0, 10, LAT_PHRAO, "R-99")) == ["no run R-99 to Lat Phrao"]
    cold = effective(with_facts(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}))
    assert check_plan(cold, Plan(TRANSFER, 0, 10, LAT_PHRAO, "R-02")) == ["cold chain"]
    booked = replace(eff, bookings=Bookings(lot_price=("MLK-042",)))
    assert check_plan(booked, Plan(MARKDOWN, 20)) == ["price change already booked by another case"]
    at30 = replace(eff, lots=(replace(eff.lots[0], markdown_pct=30),))
    assert check_plan(at30, Plan(MARKDOWN, 20)) == ["lot already 30% off"]
    assert check_plan(eff, Plan(KEEP)) == []


@pytest.mark.parametrize("minute, expected", [
    (1350, (100, [])),  # Thu 22:30, before the Thu 23:00 deadline; sellable Fri: 50 + 50
    (1380, (100, [])),  # exactly Thu 23:00 is allowed
    (1410, (0, ["past decision deadline 23:00"])),  # Thu 23:30 is too late
])
def test_overnight_run_deadline_before_at_and_after(minute, expected):
    assert limit(overnight(minute)) == expected


def test_a_deadline_on_an_earlier_day_names_its_day():
    inp = replace(f1(), today=FRI, decision_min=30, runs=(OVERNIGHT,))
    assert limit(inp) == (0, ["past decision deadline day 3 23:00"])


def test_latest_decision_is_a_timestamp_that_can_fall_on_the_previous_day():
    assert latest_decision_at(OVERNIGHT, Policy()) == THU * 1440 + 23 * 60
    assert latest_decision_min(OVERNIGHT, Policy()) == 23 * 60
    assert latest_decision_at(R02, Policy()) == THU * 1440 + 600  # F1 unchanged: Thu 10:00
    assert fmt_min(-60) == "23:00"


@pytest.mark.parametrize("inp, reasons", [
    (with_facts(f1(), {"kind": "run_available", "run_code": "R-02", "value": False}), ["run unavailable"]),
    (with_facts(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}), ["cold chain"]),
    (with_facts(f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": SAT, "free_totes": 0}),
     ["no free storage at destination"]),
    (with_facts(f1(), {"kind": "stock_count", "lot_id": "MLK-042", "units": 10.0}), ["no stock to move"]),
    # adopted: Lat Phrao then wants 20 + 25 + 25 = 70 < 140
    (with_overrides(f1(), {"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": THU, "day_to": SAT,
                           "value": 0.5}), ["no unmet demand at destination"]),
], ids=["run_available", "cold_chain", "capacity", "stock_count", "demand_multiplier"])
def test_check_plan_applies_confirmed_facts_and_adopted_assumptions(inp, reasons):
    assert check_plan(inp, PLAN140) == reasons


@pytest.mark.parametrize("adj", [
    {"kind": "run_available", "run_code": "R-02", "value": False},
    {"kind": "cold_chain", "run_code": "R-02", "value": False},
    {"kind": "decide_at", "minute": 630},
    {"kind": "departure_delay_h", "run_code": "R-02", "hours": 1.0},
    {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": SAT, "free_totes": 0},
], ids=lambda adj: adj["kind"])
def test_check_plan_rejects_what_if_only_overrides(adj):
    with pytest.raises(AdjustmentError, match="what-if"):
        check_plan(with_overrides(f1(), adj), PLAN140)


QTY = ["transfer quantity must be a whole number of at least 1"]
MD = ["markdown must be 20% or 30%"]


@pytest.mark.parametrize("plan, reasons", [
    (Plan(TRANSFER, 0, 0, LAT_PHRAO, "R-02"), QTY),
    (Plan(TRANSFER, 0, -50, LAT_PHRAO, "R-02"), QTY),
    (Plan(TRANSFER, 0, float("nan"), LAT_PHRAO, "R-02"), QTY),
    (Plan(TRANSFER, 0, float("inf"), LAT_PHRAO, "R-02"), QTY),
    (Plan(TRANSFER, 0, 10.5, LAT_PHRAO, "R-02"), QTY),
    (Plan(MARKDOWN, 0), MD),
    (Plan(MARKDOWN, 25), MD),
    (Plan(TRANSFER, 25, 10, LAT_PHRAO, "R-02"), ["rest of a transfer must be held or marked down 20% or 30%"]),
    (Plan(KEEP, 20), ["keep cannot carry a markdown"]),
    (Plan("BOGUS"), ["unknown plan kind 'BOGUS'"]),
], ids=["qty-0", "qty-neg50", "qty-nan", "qty-inf", "qty-10.5", "md-0", "md-25", "transfer-rest-25", "keep-md20",
        "bogus-kind"])
def test_check_plan_rejects_malformed_plans(plan, reasons):
    assert check_plan(f1(), plan) == reasons


def test_check_plan_rejects_an_input_from_another_engine_version():
    with pytest.raises(InputError, match="engine_version"):
        check_plan(replace(f1(), engine_version="engine-0"), Plan(KEEP))


def fri_f1():
    return replace(f1(), today=FRI, runs=(replace(R02, day=FRI),))


def sat_f1():
    return replace(f1(), today=SAT)


def test_check_plan_accepts_ranges_that_have_ended():
    """D-16: F1 on Fri with R-02 on Fri. A confirmed Thu-only capacity fact covers no day (max(3, 4) = 4 > 3).
    - The deadline is Fri 10:00 >= the Fri 09:00 decision; the run arrives Fri 13:20, so it sells from Fri.
    - Lat Phrao's unmet demand is 50 + 50 = 100. Storage 16 totes = 160, truck 22 totes = 220 and stock 500 don't
      bind, so the cap is 100: Transfer 100 passes and 101 does not.
    - F1 on Sat with an adopted Thu-Fri multiplier (covers no day): Markdown 20 on a lot at 0% with no price booking
      passes."""
    thu = with_facts(fri_f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": THU,
                                "free_totes": 0})
    for p in (Plan(KEEP), Plan(TRANSFER, 0, 100, LAT_PHRAO, "R-02")):
        assert check_plan(thu, p) == check_plan(fri_f1(), p) == []
    assert check_plan(thu, Plan(TRANSFER, 0, 101, LAT_PHRAO, "R-02")) == ["no unmet demand at destination"]
    ended = with_overrides(sat_f1(), {"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": THU,
                                      "day_to": FRI, "value": 1.5})
    assert check_plan(ended, Plan(MARKDOWN, 20)) == []


def test_a_range_past_the_last_forecast_day_limits_only_the_days_it_covers():
    """D-16: F1 with confirmed capacity Fri..99 at 1 tote. The run arrives Thu, and free totes are Thu 16, Fri 1,
    Sat 1, so the minimum is 1 tote = 10 units. Demand 140, truck 220 and stock 500 don't bind."""
    x = with_facts(f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": FRI, "day_to": 99, "free_totes": 1})
    assert check_plan(x, Plan(TRANSFER, 0, 10, LAT_PHRAO, "R-02")) == []
    assert check_plan(x, Plan(TRANSFER, 0, 11, LAT_PHRAO, "R-02")) == ["no free storage at destination"]


@pytest.mark.parametrize("adj", [
    {"kind": "run_available", "run_code": "R-02", "value": False},
    {"kind": "cold_chain", "run_code": "R-02", "value": False},
    {"kind": "decide_at", "minute": 630},
    {"kind": "departure_delay_h", "run_code": "R-02", "hours": 1.0},
    {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": SAT, "free_totes": 0},
], ids=lambda adj: adj["kind"])
def test_check_plan_refuses_an_input_with_a_what_if_already_applied(adj):
    """D-18 WR-01: effective() records the override kinds it applied, so a what-if can't reach the gate by being
    applied first."""
    with pytest.raises(AdjustmentError, match="what-if"):
        check_plan(effective(with_overrides(f1(), adj)), PLAN140)


def test_check_plan_refuses_the_reviewers_bypass():
    """D-18 WR-01, the review's repro: a run confirmed as cancelled (a fact) undone by a what-if 'available'. Raw, the
    carried what-if is refused; after effective(), the applied what-if is refused."""
    x = with_overrides(with_facts(f1(), {"kind": "run_available", "run_code": "R-02", "value": False}),
                       {"kind": "run_available", "run_code": "R-02", "value": True})
    for inp in (x, effective(x)):
        with pytest.raises(AdjustmentError, match="what-if"):
            check_plan(inp, PLAN140)


def test_check_plan_refuses_a_negative_packing_time():
    """D-18 WR-06, the reviewer's bypass: 21:00 is after 12:00 - 2 h = 10:00. With packing -600 the deadline would be
    12:00 + 10 h = 22:00 and the 21:00 decision would pass the gate."""
    late = replace(f1(), decision_min=21 * 60)
    assert check_plan(late, PLAN140) == ["past decision deadline 10:00"]
    with pytest.raises(InputError, match="packing_min"):
        check_plan(replace(late, policy=replace(late.policy, packing_min=-600)), PLAN140)


def _shortlist(inp, step):
    return replace(inp, policy=replace(inp.policy, shortlist_step=step))


@pytest.mark.parametrize("fixture,step,count,cap", [
    (f1, 10, 45, 140),
    (f1, 5, 87, 140),
    (f6, 10, 27, 80),
    (f6, 5, 51, 80),
])
def test_the_shortlist_grid_keeps_the_cap_and_every_remainder(fixture, step, count, cap):
    """Rev 6.3.0 (ENG-09): the shortlist coarsens transfer quantities to multiples of the step plus the cap; every
    remainder stays. F1: 14 x 3 + 3 = 45 at step 10, 28 x 3 + 3 = 87 at step 5; F6: 8 x 3 + 3, 16 x 3 + 3."""
    plans, _ = candidates(effective(_shortlist(fixture(), step)))
    transfers = [p for p in plans if p.kind == TRANSFER]
    assert len(plans) == count
    assert {p.qty for p in transfers} == set(range(step, cap + 1, step))
    for q in {p.qty for p in transfers}:
        assert {p.pct for p in transfers if p.qty == q} == {0, 20, 30}


def test_a_cap_below_the_step_is_used_alone():
    """ENG-09 boundary at step 10: a demand cap of 7 gives {7}, 10 gives {10} once, 25 gives {10, 20, 25}.
    Storage 160 and truck 220 don't bind; Bang Na's forecasts stay 100/100/100."""
    for (thu, fri, sat), qtys in (((3, 2, 2), {7}), ((4, 3, 3), {10}), ((10, 10, 5), {10, 20, 25})):
        inp = replace(f1(), forecasts=tuple(_flat(BANG_NA, {THU: 100, FRI: 100, SAT: 100})
                                            + _flat(LAT_PHRAO, {THU: thu, FRI: fri, SAT: sat})))
        plans, _ = candidates(effective(_shortlist(inp, 10)))
        assert {p.qty for p in plans if p.kind == TRANSFER} == qtys
        assert [p.qty for p in plans if p.kind == TRANSFER and p.pct == 0] == sorted(qtys)  # each quantity once


def test_the_shortlist_is_off_by_default():
    """ENG-09: the shortlist is an explicit setting, off by default; off is the full search (3 + 140 x 3)."""
    assert Policy().shortlist_step == 0
    plans, _ = candidates(effective(f1()))
    assert len(plans) == 423
    explicit, _ = candidates(effective(_shortlist(f1(), 0)))
    assert explicit == plans


def test_a_rejected_run_is_rejected_the_same_with_the_shortlist_on():
    """ENG-09 empty grid: a run a hard limit rejects (no cold chain) is rejected with the same entry at step 10
    as at step 0, leaving Keep, md20 and md30."""
    inp = with_overrides(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False})
    off_plans, off_rejected = candidates(effective(inp))
    on_plans, on_rejected = candidates(effective(_shortlist(inp, 10)))
    expected = [{"label": "Transfer to Lat Phrao (R-02)", "dest": "Lat Phrao", "run_code": "R-02",
                 "reasons": ["cold chain"]}]
    assert off_rejected == on_rejected == expected
    assert len(on_plans) == len(off_plans) == 3


# rev 6.3.0 (ESL-01, D-07): markdowns the schedule already has.
A = ((2, 20), (1, 30))
B = ((1, 20), (0, 30))
COVERED_THU = "the schedule already has it at 20% from day 3"


def sched(inp, schedule):
    return replace(inp, markdown_schedule=schedule)


def test_a_markdown_the_schedule_already_has_is_rejected_with_a_reason():
    """F1 + A decided Thu 09:00: the markdown applies Thu, when A is already at 20% (D-2 = day 3), so md20 and every
    md20 rest are covered. Candidates: Keep + md30 + 140 x (hold, md30 rest) = 1 + 1 + 140 x 2 = 282. Each covered
    option group is listed once in rejected, never per quantity."""
    plans, rejected = candidates(effective(sched(f1(), A)))
    assert len(plans) == 282
    assert all(p.pct != 20 for p in plans)
    assert rejected == [
        {"label": "Markdown 20%", "dest": None, "run_code": None, "reasons": [COVERED_THU]},
        {"label": "Transfer to Lat Phrao (R-02), markdown rest 20%", "dest": "Lat Phrao", "run_code": "R-02",
         "reasons": [COVERED_THU]},
    ]


def test_check_plan_refuses_a_markdown_the_schedule_already_has():
    """Send, approve and prepare refuse a covered markdown or rest with the same reason as candidates."""
    inp = sched(f1(), A)
    assert check_plan(inp, Plan(MARKDOWN, 20)) == [COVERED_THU]
    assert COVERED_THU in check_plan(inp, Plan(TRANSFER, 20, 140, LAT_PHRAO, "R-02"))
    assert check_plan(inp, Plan(MARKDOWN, 30)) == []


def test_a_markdown_before_the_first_step_is_offered():
    """F1 + B: the schedule is at 0% on Thu (its first step is day 4), so marking down earlier is allowed and every
    option is offered: 3 + 140 x 3 = 423, nothing rejected."""
    plans, rejected = candidates(effective(sched(f1(), B)))
    assert len(plans) == 423
    assert rejected == []


def test_covered_is_judged_on_the_markdown_effective_day():
    """F1 + B decided at 11:30: 11:30 + 1 h relabel is past 12:00, so the markdown starts Fri (day 4), where B is
    already at 20%. md20 is covered from day 4; md30 is offered; the run is past its 10:00 deadline."""
    plans, rejected = candidates(effective(replace(sched(f1(), B), decision_min=690)))
    assert plans == [Plan(KEEP), Plan(MARKDOWN, 30)]
    assert rejected == [
        {"label": "Markdown 20%", "dest": None, "run_code": None,
         "reasons": ["the schedule already has it at 20% from day 4"]},
        {"label": "Transfer to Lat Phrao (R-02)", "dest": "Lat Phrao", "run_code": "R-02",
         "reasons": ["past decision deadline 10:00"]},
    ]
