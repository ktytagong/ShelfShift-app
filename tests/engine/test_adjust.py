import re
import sys
from dataclasses import replace
from decimal import Decimal

import pytest

from engine.adjust import (ADOPTABLE, FACT_ONLY, KINDS, REPORTABLE, WHAT_IF_ONLY, AdjustmentError, InputError, effective,
                           validate, validate_input)
from engine.adjust import coerce_input
from engine.model import MarkdownResponse
from engine.model import Bookings
from tests.engine.fixtures import LAT_PHRAO, f1, f6, with_facts, with_overrides
from tests.engine.fixtures import BANG_NA, FRI, SAT, THU
from tests.engine.fixtures import R02
from engine.search import evaluate
from engine.limits import check_plan
from engine.factchecks import fact_checks
from engine.baseline import baseline_plan
from engine.model import KEEP, Plan


@pytest.mark.parametrize("adj,message", [
    ({"kind": "teleport"}, "unknown kind"),
    ({"kind": "decide_at", "minute": 630, "extra": 1}, "unknown keys"),
    ({"kind": "decide_at"}, "missing keys"),
    ({"kind": "decide_at", "minute": "10:30"}, "must be int"),
    ({"kind": "decide_at", "minute": True}, "must be int"),
    ({"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 100_001.0}, "outside"),
    ({"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": 3, "day_to": 5, "value": 3.5}, "outside"),
    ({"kind": "departure_delay_h", "run_code": "R-02", "hours": 25.0}, "outside"),
    ({"kind": "transport_quote", "run_code": "R-99", "fixed_cost": 900.0}, "unknown run"),
    ({"kind": "capacity", "branch": "Nowhere", "day_from": 3, "day_to": 5, "free_totes": 1}, "unknown branch"),
    ({"kind": "capacity", "branch": LAT_PHRAO, "day_from": 5, "day_to": 3, "free_totes": 1}, "day_from after day_to"),
])
def test_invalid_adjustments_are_rejected(adj, message):
    with pytest.raises(AdjustmentError, match=message):
        validate(adj, f1())


def test_integers_are_accepted_where_floats_are_expected():
    validate({"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 900}, f1())


def test_every_kind_is_in_exactly_one_group():
    assert ADOPTABLE | WHAT_IF_ONLY | FACT_ONLY == set(KINDS)
    assert not (ADOPTABLE & WHAT_IF_ONLY) and not (ADOPTABLE & FACT_ONLY) and not (WHAT_IF_ONLY & FACT_ONLY)


def test_reported_facts_apply_before_overrides():
    inp = with_overrides(with_facts(f6(), {"kind": "stock_count", "lot_id": "MLK-038", "units": 0.0}),
                         {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 900.0})
    eff = effective(inp)
    assert next(l for l in eff.lots if l.lot_id == "MLK-038").qty == 0.0
    assert eff.runs[0].fixed_cost == 900.0
    assert (eff.reported_facts, eff.overrides) == ((), ())


def test_confirmed_count_marks_the_lot_verified_today():
    eff = effective(with_facts(f6(), {"kind": "stock_count", "lot_id": "MLK-038", "units": 0.0}))
    assert next(l for l in eff.lots if l.lot_id == "MLK-038").last_verified_day == eff.today


def test_decide_at_is_never_a_reported_fact():
    assert "decide_at" not in REPORTABLE and FACT_ONLY <= REPORTABLE
    with pytest.raises(AdjustmentError, match="cannot be a reported fact"):
        effective(with_facts(f1(), {"kind": "decide_at", "minute": 630}))


def test_a_stock_count_cannot_be_a_what_if():
    with pytest.raises(AdjustmentError, match="only come from a confirmed reported fact"):
        effective(with_overrides(f6(), {"kind": "stock_count", "lot_id": "MLK-038", "units": 0.0}))


def test_the_fixtures_pass_input_validation():
    assert validate_input(f1()) is None
    assert validate_input(f6()) is None


def _first_lot(x, **changes):
    return replace(x, lots=(replace(x.lots[0], **changes),) + x.lots[1:])


@pytest.mark.parametrize("change,message", [
    pytest.param(lambda x: replace(x, engine_version="engine-0"), "engine_version", id="engine-version"),
    pytest.param(lambda x: _first_lot(x, markdown_pct=10), "markdown_pct", id="markdown-pct-10"),
    pytest.param(lambda x: replace(x, markdown=MarkdownResponse(m20=(0.0, 43 / 30, 1.55), m30=x.markdown.m30)), "m20",
                 id="m20-low-zero"),
    pytest.param(lambda x: replace(x, markdown=MarkdownResponse(m20=x.markdown.m20, m30=(1.7, 1.6, 1.8))), "m30",
                 id="m30-low-above-base"),
    pytest.param(lambda x: _first_lot(x, qty=-1.0), "qty", id="lot-qty-negative"),
    pytest.param(lambda x: _first_lot(x, qty=float("nan")), "qty", id="lot-qty-nan"),
    pytest.param(lambda x: replace(x, forecasts=(replace(x.forecasts[0], base=-5.0),) + x.forecasts[1:]), "forecast",
                 id="forecast-base-negative"),
    pytest.param(lambda x: replace(x, product=replace(x.product, units_per_tote=0)), "units_per_tote",
                 id="units-per-tote-zero"),
    pytest.param(lambda x: replace(x, lots=x.lots + x.lots), "duplicate", id="duplicate-lot-id"),
    pytest.param(lambda x: replace(x, runs=x.runs + x.runs), "duplicate", id="duplicate-run-code"),
])
def test_invalid_inputs_are_rejected_with_the_field_named(change, message):
    with pytest.raises(InputError, match=message):
        validate_input(change(f1()))


@pytest.mark.parametrize("value,rejected", [(3_000.0, True), (2_000.0, False)])
def test_the_money_bound_comes_from_the_policy(value, rejected):
    usd = replace(f1(), policy=replace(f1().policy, adjustment_cost_max=2_777.78))
    adj = {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": value}
    if rejected:
        with pytest.raises(AdjustmentError, match="outside"):
            validate(adj, usd)
    else:
        assert validate(adj, usd) is None


@pytest.mark.parametrize("adj", [
    pytest.param({"kind": "expiry", "lot_id": "MLK-042", "expiry_day": 20261017}, id="expiry-full-date"),
    pytest.param({"kind": "expiry", "lot_id": "MLK-042", "expiry_day": 6}, id="expiry-past-last-forecast-day"),
    pytest.param({"kind": "expiry", "lot_id": "MLK-042", "expiry_day": 1}, id="expiry-before-yesterday"),
])
def test_day_fields_must_lie_in_the_horizon(adj):
    """F1: today is 3 and the last forecast day is 5."""
    with pytest.raises(AdjustmentError, match="outside"):
        validate(adj, f1())


def test_a_range_that_started_before_today_and_yesterdays_expiry_are_accepted():
    assert validate({"kind": "capacity", "branch": LAT_PHRAO, "day_from": 1, "day_to": 4, "free_totes": 1}, f1()) is None
    assert validate({"kind": "expiry", "lot_id": "MLK-042", "expiry_day": 2}, f1()) is None
    assert validate({"kind": "expiry", "lot_id": "MLK-042", "expiry_day": 5}, f1()) is None


def test_a_typo_in_a_confirmed_expiry_is_an_adjustment_error():
    with pytest.raises(AdjustmentError, match="expiry.expiry_day"):
        effective(with_facts(f1(), {"kind": "expiry", "lot_id": "MLK-042", "expiry_day": 20261017}))


@pytest.mark.parametrize("adj", [
    pytest.param({"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": 1, "day_to": 2, "value": 1.5},
                 id="ended-before-today"),
    pytest.param({"kind": "capacity", "branch": LAT_PHRAO, "day_from": 6, "day_to": 9, "free_totes": 0},
                 id="wholly-after-the-last-forecast-day"),
    pytest.param({"kind": "capacity", "branch": LAT_PHRAO, "day_from": 3, "day_to": 99, "free_totes": 1},
                 id="runs-past-the-last-forecast-day"),
    pytest.param({"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": 1, "day_to": 20261017, "value": 2.0},
                 id="covers-the-whole-horizon"),
])
def test_ended_future_and_overlapping_ranges_are_valid(adj):
    """D-16: a day range has no horizon bound (F1: today 3, last forecast day 5). It applies to the horizon days it
    covers, which may be none."""
    assert validate(adj, f1()) is None


def sat_f1():
    return replace(f1(), today=SAT)


def test_an_ended_or_future_range_applies_to_no_day():
    """D-16: a range changes the input's days max(day_from, today)..day_to.
    - Thu-only capacity on Fri: max(3, 4) = 4 > 3, so it covers no day.
    - An adopted Thu-Fri multiplier on Sat: max(3, 5) = 5 > 4, so it covers no day.
    - Capacity on days 6-9 in F1: F1 has capacity rows for days 3-5 only, so it covers no day."""
    fri = replace(f1(), today=FRI)
    thu_only = {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": THU, "free_totes": 0}
    assert effective(with_facts(fri, thu_only)) == effective(fri)
    thu_fri = {"kind": "demand_multiplier", "branch": LAT_PHRAO, "day_from": THU, "day_to": FRI, "value": 1.5}
    assert effective(with_overrides(sat_f1(), thu_fri)) == effective(sat_f1())
    later = {"kind": "capacity", "branch": LAT_PHRAO, "day_from": 6, "day_to": 9, "free_totes": 0}
    assert effective(with_facts(f1(), later)) == effective(f1())


def test_a_range_applies_to_exactly_the_horizon_days_it_covers():
    """D-16:
    - F1 (today Thu) with capacity Fri..99 at 1 tote: Thu keeps its 16, and Fri and Sat get 1.
    - F1 on Fri with an adopted Thu-Fri multiplier x0.5: Thu is before today, so it stays 40; Fri is 50 x 0.5 = 25;
      Sat is outside the range and stays 50."""
    cap = effective(with_facts(f1(), {"kind": "capacity", "branch": LAT_PHRAO, "day_from": FRI, "day_to": 99,
                                      "free_totes": 1}))
    assert {c.day: c.free_totes for c in cap.capacity if c.branch == LAT_PHRAO} == {THU: 16, FRI: 1, SAT: 1}
    mult = effective(with_overrides(replace(f1(), today=FRI), {"kind": "demand_multiplier", "branch": LAT_PHRAO,
                                                               "day_from": THU, "day_to": FRI, "value": 0.5}))
    assert {f.day: f.base for f in mult.forecasts if f.branch == LAT_PHRAO} == {THU: 40, FRI: 25.0, SAT: 50}


def test_an_adopted_markdown_response_widens_its_range():
    """F1: m20 (1.30, 43/30, 1.55), m30 (1.40, 1.6, 1.80). A value outside low..high moves that end; one inside does not."""
    above = effective(with_overrides(f1(), {"kind": "markdown_response", "m20": 2.5, "m30": 2.8}))
    assert above.markdown == MarkdownResponse((1.30, 2.5, 2.5), (1.40, 2.8, 2.8))
    inside = effective(with_overrides(f1(), {"kind": "markdown_response", "m20": 1.4, "m30": 1.5}))
    assert inside.markdown == MarkdownResponse((1.30, 1.4, 1.55), (1.40, 1.5, 1.80))
    assert validate_input(above) is None
    assert validate_input(inside) is None


def test_a_count_from_an_earlier_day_sets_only_its_day():
    """D-18 WR-02, D-21 choice 1: on F6 at Fri, MLK-038 is on record at 60, last verified 8 days before Thu.
    - A Thu count of 20 is already in the record as a stock correction (the record's 60 carries Thu's count and any
      sales since), so only its day applies: (60.0, Thu).
    - A Fri count is taken at this snapshot: (20.0, Fri).
    - No counted_day defaults to the evaluation day: (20.0, Fri)."""
    fri = replace(f6(), today=FRI)
    count = {"kind": "stock_count", "lot_id": "MLK-038", "units": 20.0}

    def mlk038(adj):
        lot = next(l for l in effective(with_facts(fri, adj)).lots if l.lot_id == "MLK-038")
        return (lot.qty, lot.last_verified_day)

    assert mlk038({**count, "counted_day": THU}) == (60.0, THU)
    assert mlk038({**count, "counted_day": FRI}) == (20.0, FRI)
    assert mlk038(count) == (20.0, FRI)


def test_a_count_cannot_come_from_a_later_day():
    """D-18 WR-02: F6's today is 3, so a count dated day 4 is in the future."""
    with pytest.raises(AdjustmentError, match="counted_day=4 outside"):
        validate({"kind": "stock_count", "lot_id": "MLK-038", "units": 0.0, "counted_day": FRI}, f6())


def test_numbers_are_coerced_to_their_declared_type():
    """D-18 WR-07: every value is F1's own written another way (500.0, 5, 1, 40.0, 10, 600.0, 22, 1.3, 100.0, 40.0,
    120, 1.0), so the coerced odd-typed input equals the coerced F1. F1's int forecasts become floats on both sides."""
    base = f1()
    odd = replace(
        base,
        decision_min=540.0,
        lots=(replace(base.lots[0], qty=Decimal("500"), expiry_day=5.0, received_seq=Decimal("1")),),
        product=replace(base.product, price=Decimal("40.00"), units_per_tote=10.0),
        runs=(replace(base.runs[0], fixed_cost=600, spare_totes=Decimal("22")),),
        markdown=MarkdownResponse((Decimal("1.30"), base.markdown.m20[1], base.markdown.m20[2]), base.markdown.m30),
        sales_yesterday=((BANG_NA, 100), (LAT_PHRAO, Decimal("40"))),
        policy=replace(base.policy, packing_min=120.0, min_regret=1),
    )
    c = coerce_input(odd)
    assert c == coerce_input(base)
    assert type(c.lots[0].qty) is float
    assert type(c.lots[0].expiry_day) is int
    assert type(c.decision_min) is int
    assert type(c.product.price) is float
    assert type(c.runs[0].spare_totes) is int


@pytest.mark.parametrize("change,match", [
    pytest.param(lambda x: _first_lot(x, qty=True), "qty", id="qty-bool"),
    pytest.param(lambda x: _first_lot(x, qty="500"), "qty", id="qty-str"),
    pytest.param(lambda x: _first_lot(x, qty=float("nan")), "qty", id="qty-nan"),
    pytest.param(lambda x: _first_lot(x, expiry_day=5.5), "expiry_day", id="expiry-day-not-whole"),
    pytest.param(lambda x: replace(x, capacity=(replace(x.capacity[0], free_totes=1.5),) + x.capacity[1:]),
                 "free_totes", id="free-totes-not-whole"),
    pytest.param(lambda x: replace(x, runs=(replace(x.runs[0], spare_totes=True),)), "spare_totes",
                 id="spare-totes-bool"),
    pytest.param(lambda x: replace(x, runs=(replace(x.runs[0], fixed_cost=None),)), "fixed_cost",
                 id="fixed-cost-none"),
    pytest.param(lambda x: replace(x, product=replace(x.product, price=Decimal("NaN"))), "price",
                 id="price-decimal-nan"),
    pytest.param(lambda x: replace(x, decision_min=540.5), "decision_min", id="decision-min-not-whole"),
])
def test_a_number_that_cannot_be_coerced_exactly_is_an_input_error(change, match):
    with pytest.raises(InputError, match=match):
        coerce_input(change(f1()))


def test_validate_input_rejects_a_number_it_cannot_coerce():
    """D-18 WR-07: before, a bool qty passed validation and counted as 1."""
    with pytest.raises(InputError, match="qty"):
        validate_input(_first_lot(f1(), qty=True))


def test_adjustment_numbers_are_converted_only_when_exact():
    """D-21 choice 4: an exact payload number takes its declared type; anything else is left for validate."""
    def first(adj):
        return coerce_input(with_overrides(f1(), adj)).overrides[0]

    quote = first({"kind": "transport_quote", "run_code": "R-02", "fixed_cost": Decimal("900")})
    assert quote["fixed_cost"] == 900.0 and type(quote["fixed_cost"]) is float
    minute = first({"kind": "decide_at", "minute": 630.0})["minute"]
    assert minute == 630 and type(minute) is int
    assert first({"kind": "decide_at", "minute": True})["minute"] is True
    assert first({"kind": "decide_at", "minute": 630.5})["minute"] == 630.5
    with pytest.raises(AdjustmentError, match="must be int"):
        effective(coerce_input(with_overrides(f1(), {"kind": "decide_at", "minute": 630.5})))


def _policy(x, **changes):
    return replace(x, policy=replace(x.policy, **changes))


def _product(x, **changes):
    return replace(x, product=replace(x.product, **changes))


def _run(x, **changes):
    return replace(x, runs=(replace(x.runs[0], **changes),) + x.runs[1:])


@pytest.mark.parametrize("change,match", [
    pytest.param(lambda x: _policy(x, min_regret=-1.0), "policy.min_regret", id="min-regret-negative"),
    pytest.param(lambda x: _policy(x, crossover_range_floor=float("inf")), "policy.crossover_range_floor",
                 id="crossover-range-floor-inf"),
    pytest.param(lambda x: _policy(x, adjustment_cost_max=-0.01), "policy.adjustment_cost_max",
                 id="adjustment-cost-max-negative"),
    pytest.param(lambda x: _policy(x, packing_min=-600), "policy.packing_min", id="packing-min-negative"),
    pytest.param(lambda x: _policy(x, min_sellable_days=-1), "policy.min_sellable_days",
                 id="min-sellable-days-negative"),
    pytest.param(lambda x: _policy(x, relabel_min=-60), "policy.relabel_min", id="relabel-min-negative"),
    pytest.param(lambda x: _policy(x, markdown_same_day_by_min=-1), "policy.markdown_same_day_by_min",
                 id="markdown-same-day-by-min-negative"),
    pytest.param(lambda x: _policy(x, sellable_same_day_before_min=-1), "policy.sellable_same_day_before_min",
                 id="sellable-same-day-before-min-negative"),
    pytest.param(lambda x: _product(x, price=-40.0), "product.price", id="price-negative"),
    pytest.param(lambda x: _product(x, disposal_cost=float("inf")), "product.disposal_cost", id="disposal-cost-inf"),
    pytest.param(lambda x: _product(x, kg_per_unit=-1.03), "product.kg_per_unit", id="kg-per-unit-negative"),
    pytest.param(lambda x: _run(x, fixed_cost=-600.0), "fixed_cost", id="run-fixed-cost-negative"),
    pytest.param(lambda x: _run(x, per_unit_cost=float("inf")), "per_unit_cost", id="run-per-unit-cost-inf"),
    pytest.param(lambda x: _run(x, cancel_cost=-150.0), "cancel_cost", id="run-cancel-cost-negative"),
    pytest.param(lambda x: replace(x, decision_min=-500), "decision_min", id="decision-min-negative"),
    pytest.param(lambda x: replace(x, decision_min=1440), "decision_min", id="decision-min-1440"),
    pytest.param(lambda x: replace(x, case_lot_id="MLK-999"), "case_lot_id", id="case-lot-not-in-lots"),
])
def test_the_policy_costs_times_and_case_lot_are_validated(change, match):
    """D-18 WR-06: each value is finite-and->=0-invalid or out of range, never NaN (coerce_input rejects NaN)."""
    with pytest.raises(InputError, match=match):
        validate_input(change(f1()))


def test_edge_values_of_the_new_checks_pass():
    """D-18 WR-06 guard: the bounds are inclusive. F1's own cancel_cost=None is covered by
    test_the_fixtures_pass_input_validation."""
    assert validate_input(replace(f1(), decision_min=0)) is None
    assert validate_input(replace(f1(), decision_min=1439)) is None
    assert validate_input(_run(f1(), cancel_cost=0.0, fixed_cost=0.0)) is None
    assert validate_input(_policy(f1(), min_regret=0.0, packing_min=0)) is None


@pytest.mark.parametrize("changes,field", [
    ({"markdown": MarkdownResponse(m20=(1.3, 1.5), m30=(1.4, 1.6, 1.8))}, "markdown.m20"),
    ({"bookings": Bookings(demand=((LAT_PHRAO, 3),))}, "bookings.demand"),
    ({"sales_yesterday": ((BANG_NA,),)}, "sales_yesterday"),
    ({"sales_yesterday": ((),)}, "sales_yesterday"),
], ids=["m20 two values", "booking demand two values", "sales row one value", "sales row empty"])
def test_a_row_of_the_wrong_length_is_an_input_error(changes, field):
    """Round-3 IN-02: a row with the wrong number of values (a markdown triple of 2, a booking demand row of 2
    instead of (branch, day, units), a sales row of 1 or 0 instead of (branch, units)) is an InputError naming
    its field, never a bare ValueError or IndexError from unpacking."""
    with pytest.raises(InputError, match=field):
        validate_input(replace(f1(), **changes))


@pytest.mark.parametrize("field,value", [
    ("stale_after_days", -1),
    ("stale_factor", 1.5),
    ("phantom_min_forecast", -1.0),
])
def test_an_invalid_stale_or_phantom_threshold_is_an_input_error(field, value):
    """Round-3 IN-03: the fact-check thresholds are Policy fields, validated like every other: the stale age and
    the phantom forecast floor are finite and >= 0, and the stale factor is a share in 0..1."""
    with pytest.raises(InputError, match=f"policy.{field}"):
        validate_input(replace(f1(), policy=replace(f1().policy, **{field: value})))


@pytest.mark.parametrize("field,value", [
    ("baseline_near_expiry_days", -1),
    ("baseline_markdown_pct", 25),
    ("baseline_markdown_pct", 0),
])
def test_an_invalid_baseline_step_value_is_an_input_error(field, value):
    """Round-3 IN-03: the baseline's near-expiry days are finite and >= 0, and its markdown is an allowed step,
    20 or 30 (0 would be no markdown at all)."""
    with pytest.raises(InputError, match=f"policy.{field}"):
        validate_input(replace(f1(), policy=replace(f1().policy, **{field: value})))


@pytest.mark.parametrize("value", [-1.0, float("inf"), float("nan")])
def test_an_invalid_waste_price_is_an_input_error(value):
    """Rev 6.3.0 (ENG-10): the waste price is money per kg, finite and >= 0, like every other policy money value."""
    with pytest.raises(InputError, match="policy.waste_price"):
        validate_input(replace(f1(), policy=replace(f1().policy, waste_price=value)))


@pytest.mark.parametrize("value", [-1.0, float("inf")])
def test_an_invalid_waste_flip_max_is_an_input_error(value):
    """Rev 6.3.0 (D-05): the waste flip cap is a money guard, finite and >= 0."""
    with pytest.raises(InputError, match="policy.waste_flip_max"):
        validate_input(replace(f1(), policy=replace(f1().policy, waste_flip_max=value)))


@pytest.mark.parametrize("value", [7, -5, 1])
def test_an_invalid_shortlist_step_is_an_input_error(value):
    """Rev 6.3.0 (ENG-09): the shortlist step is 0 (off), 5 or 10."""
    with pytest.raises(InputError, match="policy.shortlist_step"):
        validate_input(replace(f1(), policy=replace(f1().policy, shortlist_step=value)))


@pytest.mark.parametrize("schedule", [
    ((2, 25),),
    ((-1, 20),),
    ((1, 20), (1, 30)),
    ((2, 30), (1, 20)),
    ((2, 20.5),),
    ((2,),),
    None,  # review WR-05: a null schedule (e.g. a nullable reference column) is an InputError, not a TypeError
    "2:20 1:30",  # the setting's text form must be parsed into steps before it reaches the engine
    (5,),  # a step that is not a (k, pct) pair
])
def test_a_bad_schedule_is_an_input_error(schedule):
    with pytest.raises(InputError, match="markdown_schedule"):
        validate_input(replace(f1(), markdown_schedule=schedule))


def test_a_schedule_given_as_floats_is_coerced():
    out = coerce_input(replace(f1(), markdown_schedule=((2.0, 20.0), (1, 30)))).markdown_schedule
    assert out == ((2, 20), (1, 30))
    assert all(type(v) is int for step in out for v in step)


def test_a_number_too_large_for_the_engines_arithmetic_is_refused_at_every_entry_point():
    """Review WR-08 (D-30): a finite but huge value used to pass validate_input, which checked only finite and >= 0.
    F1 with product.price 1e306 makes a net of 440 x 1e306 = inf, and scoring.micro (Fraction(inf)) raised a bare
    OverflowError from evaluate, where every other bad input names its field.

    The rule: the engine multiplies at most three input magnitudes (waste price x kg per unit x units; price x units;
    per-unit cost x units) and sums them over rows, so each money value and quantity must have a fourth power that is
    a finite float. The largest float is about 1.797e308, so the limit is about 1.158e77, the float type's own range,
    not a chosen value. (1e306)^4 overflows, so 1e306 is an InputError naming product.price at all four entry points.
    """
    inp = _product(f1(), price=1e306)
    with pytest.raises(InputError, match="product.price"):
        evaluate(inp)
    with pytest.raises(InputError, match="product.price"):
        check_plan(inp, Plan(KEEP))
    with pytest.raises(InputError, match="product.price"):
        fact_checks(inp, {})
    with pytest.raises(InputError, match="product.price"):
        baseline_plan(inp)


@pytest.mark.parametrize("change,match", [
    pytest.param(lambda x: _product(x, kg_per_unit=1e306), "product.kg_per_unit", id="kg-per-unit-1e306"),
    pytest.param(lambda x: _policy(x, waste_price=1e306), "policy.waste_price", id="waste-price-1e306"),
    pytest.param(lambda x: _policy(x, min_regret=1e306), "policy.min_regret", id="min-regret-1e306"),
    pytest.param(lambda x: _first_lot(x, qty=1e306), "lot MLK-042 qty", id="lot-qty-1e306"),
    pytest.param(lambda x: replace(x, forecasts=(replace(x.forecasts[0], low=1e306, base=1e306, high=1e306),)
                                   + x.forecasts[1:]), "forecast Bang Na day 3 low", id="forecast-1e306"),
    pytest.param(lambda x: _run(x, fixed_cost=1e306), "run R-02 fixed_cost", id="run-fixed-cost-1e306"),
    pytest.param(lambda x: replace(x, sales_yesterday=((BANG_NA, 1e306),) + x.sales_yesterday[1:]),
                 "sales_yesterday Bang Na units", id="sales-yesterday-1e306"),
    pytest.param(lambda x: replace(x, capacity=(replace(x.capacity[0], free_totes=10 ** 5000),) + x.capacity[1:]),
                 "capacity Lat Phrao day 3 free_totes", id="free-totes-10-pow-5000"),
    pytest.param(lambda x: _product(x, units_per_tote=10 ** 5000), "product.units_per_tote",
                 id="units-per-tote-10-pow-5000"),
])
def test_a_number_too_large_for_the_engines_arithmetic_is_an_input_error(change, match):
    """Review WR-08 (D-30): every money value and quantity validate_input checks follows the float-range rule, its
    fourth power a finite float (limit about 1.158e77). (1e306)^4 overflows. A whole number of 5,001 digits
    (10**5000) does not even convert to a float: it is an InputError naming the field, never a bare OverflowError
    from the conversion, and never a ValueError from printing it (Python refuses to print an int past 4,300 digits).
    units_per_tote used to be checked only for >= 1, so 10**5000 passed."""
    with pytest.raises(InputError, match=match):
        validate_input(change(f1()))


def test_numbers_within_the_engines_float_range_are_accepted():
    """Review WR-08, the boundary either side. The largest float is about 1.797e308, so the limit is its fourth
    root, about 1.158e77. (1e77)^4 is about 1e308, finite: accepted. (1.2e77)^4 is about 2.07e308, past the maximum:
    refused.

    F1 at price 1e77: Transfer 140 hold still ranks first, with the same sales (300 at Bang Na plus 140 at Lat Phrao).
    Its net is 440 x 1e77 = 4.4e79; the costs (600 transport, 120 disposal) vanish in a float of that size. Finite.
    """
    inp = _product(f1(), price=1e77)
    assert validate_input(inp) is None
    r = evaluate(inp)
    assert r["recommended"] == "tr:Lat Phrao:R-02:140:hold"
    net = next(o["net"]["base"] for o in r["options"] if o["key"] == r["recommended"])
    assert net == pytest.approx(4.4e79, rel=1e-9) and net < float("inf")
    with pytest.raises(InputError, match="product.price"):
        validate_input(_product(f1(), price=1.2e77))


def test_a_stack_of_demand_multipliers_past_the_float_range_is_an_adjustment_error():
    """Review WR-08: one demand_multiplier is bounded at 3.0, but overrides stack. F1's Bang Na Thu forecast is 100
    (low = base = high). After k overrides of x3.0 it is 100 x 3^k; log10(3) = 0.47712.
    - k = 157: 100 x 10^74.908, about 8.09e76; its fourth power, about 4.29e307, is finite: accepted.
    - k = 158: about 2.43e77; its fourth power overflows: an AdjustmentError naming demand_multiplier, the error
      every bad adjustment raises, from effective() and from evaluate(). So effective() of a valid input stays inside
      the rule."""
    adj = {"kind": "demand_multiplier", "branch": BANG_NA, "day_from": THU, "day_to": THU, "value": 3.0}
    eff = effective(with_overrides(f1(), *[adj] * 157))
    thu = next(f for f in eff.forecasts if f.branch == BANG_NA and f.day == THU)
    assert thu.base == pytest.approx(100 * 3 ** 157, rel=1e-9)
    too_many = with_overrides(f1(), *[adj] * 158)
    with pytest.raises(AdjustmentError, match="demand_multiplier"):
        effective(too_many)
    with pytest.raises(AdjustmentError, match="demand_multiplier"):
        evaluate(too_many)


def _run_after_expiry(min_sellable_days):
    """F1 with R-02 moved to day 6, after the case lot's Sat (day 5) expiry (review WR-09, 02-CONTEXT D-33)."""
    x = replace(f1(), runs=(replace(R02, day=6),))
    return replace(x, policy=replace(x.policy, min_sellable_days=min_sellable_days))


def _estimated_sat_run(min_sellable_days):
    """F1 with the case lot's expiry ESTIMATED and R-02 on Sat (review WR-09, 02-CONTEXT D-33)."""
    x = f1()
    x = replace(x, lots=(replace(x.lots[0], expiry_source="ESTIMATED"),) + x.lots[1:], runs=(replace(R02, day=SAT),))
    return replace(x, policy=replace(x.policy, min_sellable_days=min_sellable_days))


@pytest.mark.parametrize("build,value,shown", [(_run_after_expiry, 0, "0"), (_estimated_sat_run, 0, "0"),
                                               (_run_after_expiry, -1, "-1"),
                                               (_run_after_expiry, -10 ** 5000, "a whole number too large to print")],
                         ids=["r02_day_6_at_0", "estimated_r02_sat_at_0", "r02_day_6_at_minus_1",
                              "r02_day_6_at_minus_10_to_5000"])
def test_min_sellable_days_below_1_is_refused_at_every_entry_point(build, value, shown):
    """Review WR-09, 02-CONTEXT D-33. validate_input used to accept policy.min_sellable_days = 0, because _quantity
    only checks >= 0. In limits.transfer_caps the sellable-days check reads expiry - sell_day + 1 < min_sellable_days.
    For a run arriving after the case lot's expiry (R-02 on day 6 against the Sat expiry, or R-02 on Sat against the
    Fri expiry of an ESTIMATED lot's alternative) at 0 that reads 0 < 0, which is false, so the run gets past the
    check, the storage window range(arrival_day, expiry_day + 1) is empty, and min() raises a bare ValueError, which
    the API would turn into a 500.

    D-33 refuses the value at the boundary: spec §6 Hard limits says sellable days at the destination >= 1, so
    validate_input refuses policy.min_sellable_days below 1 with an InputError naming the field and the value as
    given, and every entry point (evaluate, check_plan, fact_checks, baseline_plan) raises it, because each calls
    validate_input first. With a floor of 1 the window cannot be empty once transfer_caps gets past the sellable-days
    reason, so no crash-site guard is needed. -1 reads the same rule (it is checked before the float-range loop).
    fact_checks runs validate_input before it reads the result, so {} is enough, as in the WR-08 test.

    -10**5000 reads 'a whole number too large to print' through `_shown` (review WR-10): Python refuses to print an
    int past `sys.get_int_max_str_digits()` digits, and the f-string used to raise that refusal as a bare ValueError
    before the InputError existed (65387e1 put the floor ahead of the `_quantity` loop). The expected text comes from
    the shown column, never from formatting the value, which would raise that same refusal inside the test.
    """
    inp = build(value)
    expected = re.escape(f"policy.min_sellable_days={shown} must be >= 1")
    with pytest.raises(InputError, match=expected):
        evaluate(inp)
    with pytest.raises(InputError, match=expected):
        check_plan(inp, Plan(KEEP))
    with pytest.raises(InputError, match=expected):
        fact_checks(inp, {})
    with pytest.raises(InputError, match=expected):
        baseline_plan(inp)


def test_at_min_sellable_days_1_both_repros_complete():
    """02-CONTEXT D-33: at min_sellable_days 1, the Policy default (so F1-F6 never move), both WR-09 repros complete.

    F1, R-02 on day 6: R-02's first sellable day is day 6, and 5 - 6 + 1 = 0 < 1, so transfer_caps returns
    ["not enough sellable days at destination"] before any window; R-02 is rejected with exactly that reason (its
    deadline, day 6 10:00, is still open). The decision is at 09:00 Thu, so a markdown takes effect Thu. The pick is
    the best of Keep 12,000 - 400 = 11,600, Markdown 20% 13,760 - 140 = 13,620 and Markdown 30% 13,440 - 40 = 13,400:
    md20.

    F1 ESTIMATED, R-02 on Sat. Record (expiry Sat): 5 - 5 + 1 = 1 >= 1 and Lat Phrao is short Sat 50, so the cap is 50.
    Transfer q <= 50 with the rest at 20%: Bang Na sells 430 at 32 = 13,760 and wastes 70 - q; Lat Phrao sells q at
    40; net 13,760 - 2(70 - q) + 40q - 600 = 13,020 + 42q, so 15,120 at q = 50. The rest held gives 13,100, at 30%
    14,000, Markdown 20% 13,620. So p0 = tr:Lat Phrao:R-02:50:md20.
    The one fact is MLK-042:expiry (alternative Fri). 4 - 5 + 1 = 0 < 1, so every transfer is refused and check_plan
    gives exactly ["not enough sellable days at destination"] (no schedule, so no covered reason):
    pick_feasible_if_alt False. p1 = best of Keep 7,400, Markdown 20% 9,173.33 - 426.67 = 8,746.67 and Markdown 30%
    8,600: md20. p0 under the alternative: the projection ends Fri, before R-02's Sat departure, so nothing ships and
    Bang Na sells at 20% exactly as Markdown 20% does: every figure 0.0, changes_plan True (not allowed), regret 0.0.
    """
    r = evaluate(_run_after_expiry(1))
    assert r["recommended"] == "md20"
    r02 = [x for x in r["rejected"] if x["run_code"] == "R-02"]
    assert len(r02) == 1 and r02[0]["reasons"] == ["not enough sellable days at destination"]

    inp = _estimated_sat_run(1)
    r = evaluate(inp, with_crossovers=False)
    assert r["recommended"] == "tr:Lat Phrao:R-02:50:md20"
    cs = fact_checks(inp, r)
    assert [c["fact_id"] for c in cs] == ["MLK-042:expiry"]
    c = cs[0]
    assert c["pick_feasible_if_alt"] is False and c["changes_plan"] is True
    assert c["regret"] == pytest.approx(0.0, abs=0.01)
    assert c["decision_regret"] == pytest.approx(0.0, abs=0.01)
    assert c["net_revenue_difference"] == pytest.approx(0.0, abs=0.01)
    assert c["waste_kg_difference"] == pytest.approx(0.0, abs=0.01)
    assert c["best_plan_if_alt"] == "md20"


def test_a_float_range_message_prints_the_value_as_given_and_names_no_review():
    """Review IN-09, fixed per 02-CONTEXT D-34: these messages reach the API and, through what-if overrides, a manager,
    so they carry no planning-ledger review ID (it stays in the code comments) and print the value as given:
    free_totes=-1 reads "=-1", not "=-1.0", and price 1e306 reads "=1e+306". An int Python refuses to print (past
    `sys.get_int_max_str_digits()`, 4,300 digits by default) is the only value not printed: `_shown` catches that
    refusal (review IN-12) and the message reads 'a whole number too large for a float'. An int of 10**400 (about
    1,329 bits) is printed in full, all 401 digits.
    The demand_multiplier stack (158 overrides of x3.0, as in the WR-08 stack test) ends with the same float-range
    wording."""
    tail = " must be finite, >= 0 and at most about 1.158e77 (the engine's float range)"

    def message(inp):
        with pytest.raises(InputError) as e:
            validate_input(inp)
        return str(e.value)

    free = lambda x, v: replace(x, capacity=(replace(x.capacity[0], free_totes=v),) + x.capacity[1:])  # noqa: E731
    messages = [
        message(free(f1(), -1)),
        message(_product(f1(), price=1e306)),
        message(_product(f1(), units_per_tote=10 ** 5000)),
        message(free(f1(), 10 ** 400)),
    ]
    assert messages[0] == "capacity Lat Phrao day 3 free_totes=-1" + tail
    assert messages[1] == "product.price=1e+306" + tail
    assert messages[2] == "product.units_per_tote=a whole number too large for a float" + tail
    assert messages[3] == "capacity Lat Phrao day 3 free_totes=1" + "0" * 400 + tail

    adj = {"kind": "demand_multiplier", "branch": BANG_NA, "day_from": THU, "day_to": THU, "value": 3.0}
    with pytest.raises(AdjustmentError) as e:
        effective(with_overrides(f1(), *[adj] * 158))
    stack = str(e.value)
    assert stack.startswith("demand_multiplier on Bang Na day 3: the stacked multipliers take the low forecast to ")
    assert stack.endswith(", past the engine's float range (at most about 1.158e77)")
    for m in messages + [stack]:
        assert "review" not in m and "WR-0" not in m


def test_a_value_python_refuses_to_print_reads_as_a_phrase_at_any_int_print_limit():
    """Review IN-12 (user, UAT G-02-2 item 2). The 14,000-bit rule in _quantity assumed the default int-print limit
    of 4,300 digits; -X int_max_str_digits, PYTHONINTMAXSTRDIGITS and sys.set_int_max_str_digits lower it to as
    little as 640, and this test sets that same limit (restored afterwards). Hand values at 640: 10**400 has 401
    digits and prints in full; 10**700 (701 digits) and 10**1000 (1,001 digits) are refused, so _shown gives "a whole
    number too large for a float", which is always true of such an int: past 640 digits it is past the float range
    (about 1.8e308, 309 digits). min_sellable_days = -10**1000 meets the D-33 floor first (it runs before the
    float-range loop), so it reads "a whole number too large to print"."""
    tail = " must be finite, >= 0 and at most about 1.158e77 (the engine's float range)"

    def message(inp):
        with pytest.raises(InputError) as e:
            validate_input(inp)
        return str(e.value)

    free = lambda x, v: replace(x, capacity=(replace(x.capacity[0], free_totes=v),) + x.capacity[1:])  # noqa: E731
    saved = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        messages = [
            message(free(f1(), 10 ** 400)),
            message(free(f1(), 10 ** 700)),
            message(free(f1(), 10 ** 1000)),
            message(replace(f1(), policy=replace(f1().policy, min_sellable_days=-10 ** 1000))),
        ]
    finally:
        sys.set_int_max_str_digits(saved)
    assert messages[0] == "capacity Lat Phrao day 3 free_totes=1" + "0" * 400 + tail
    assert messages[1] == "capacity Lat Phrao day 3 free_totes=a whole number too large for a float" + tail
    assert messages[2] == "capacity Lat Phrao day 3 free_totes=a whole number too large for a float" + tail
    assert messages[3] == "policy.min_sellable_days=a whole number too large to print must be >= 1"
