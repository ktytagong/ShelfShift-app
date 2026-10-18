from dataclasses import replace
from decimal import Decimal

import pytest

from engine.baseline import baseline_plan
from engine.canonical import canonical_json, from_dict, rec_version, to_dict
from engine.factchecks import fact_checks
from engine.adjust import InputError, effective
from engine.limits import check_plan
from engine.model import Bookings
from engine.model import Plan, TRANSFER
from engine.search import evaluate
from tests.engine.fixtures import LAT_PHRAO, THU, f1, f6, with_overrides
from tests.engine.fixtures import FRI, R02, with_facts


def test_order_of_lists_does_not_change_the_version():
    inp = f6()
    shuffled = replace(inp, lots=tuple(reversed(inp.lots)), forecasts=tuple(reversed(inp.forecasts)))
    assert rec_version(shuffled) == rec_version(inp)


def test_float_noise_does_not_change_the_version():
    inp = f1()
    noisy = replace(inp, runs=(replace(inp.runs[0], fixed_cost=0.1 + 0.2 + 599.7),))
    assert rec_version(noisy) == rec_version(inp)


def test_a_different_booking_quantity_changes_the_version():
    inp = f1()
    booked = replace(inp, bookings=Bookings(demand=((LAT_PHRAO, THU, 10.0),)))
    assert rec_version(booked) != rec_version(inp)


def test_an_override_changes_the_version():
    inp = f1()
    what_if = replace(inp, overrides=({"kind": "decide_at", "minute": 630},))
    assert rec_version(what_if) != rec_version(inp)


def test_canonical_json_is_sorted_and_compact():
    text = canonical_json(f1())
    assert text.startswith('{"bookings":') and ": " not in text


def test_export_round_trip_gives_same_version_and_result():
    inp = f6()
    back = from_dict(to_dict(inp))
    assert rec_version(back) == rec_version(inp)
    assert evaluate(back) == evaluate(inp)



def test_thai_names_are_stable_in_the_version():
    inp = f1()
    thai = replace(inp, lots=(replace(inp.lots[0], branch="บางนา"),))
    text = canonical_json(thai)
    assert "บางนา" in text  # stored as UTF-8, not escaped
    assert rec_version(from_dict(to_dict(thai))) == rec_version(thai)


def test_int_and_float_give_the_same_version():
    inp = f1()
    as_ints = replace(inp, lots=(replace(inp.lots[0], qty=500),), runs=(replace(inp.runs[0], fixed_cost=600),))
    assert rec_version(as_ints) == rec_version(inp)


def test_override_numbers_are_coerced_by_their_declared_type():
    as_int = with_overrides(f1(), {"kind": "departure_delay_h", "run_code": "R-02", "hours": 1})
    as_float = with_overrides(f1(), {"kind": "departure_delay_h", "run_code": "R-02", "hours": 1.0})
    assert rec_version(as_int) == rec_version(as_float)


def test_decimal_numbers_hash_like_floats():
    inp = f1()
    from_db = replace(inp, lots=(replace(inp.lots[0], qty=Decimal("500")),),
                      runs=(replace(inp.runs[0], fixed_cost=Decimal("600.00")),))
    assert rec_version(from_db) == rec_version(inp)
    assert rec_version(from_dict(to_dict(from_db))) == rec_version(inp)
    quote = {"kind": "transport_quote", "run_code": "R-02"}
    assert (rec_version(with_overrides(inp, {**quote, "fixed_cost": Decimal("900")}))
            == rec_version(with_overrides(inp, {**quote, "fixed_cost": 900.0})))


def test_an_ended_fact_stays_in_the_version_and_changes_no_result():
    """D-16: F1 on Fri (R-02 on Fri) with a confirmed Thu-only capacity fact. The fact covers no day
    (max(3, 4) = 4 > 3), so every result equals the one without it, but it is not pruned: it stays in the input,
    in rec_version and through the export round trip."""
    fri = replace(f1(), today=FRI, runs=(replace(R02, day=FRI),))
    x = with_facts(fri, {"kind": "capacity", "branch": LAT_PHRAO, "day_from": THU, "day_to": THU, "free_totes": 0})
    assert rec_version(x) != rec_version(fri)
    back = from_dict(to_dict(x))
    assert rec_version(back) == rec_version(x)
    assert back.reported_facts == x.reported_facts
    r = evaluate(x)
    assert r == evaluate(fri) == evaluate(back)
    assert fact_checks(x, r) == fact_checks(fri, evaluate(fri))
    assert baseline_plan(x) == baseline_plan(fri)


def test_effective_records_the_override_kinds_it_applied():
    """D-18 WR-01, D-21 choice 2: the marker holds override kinds (adopted and what-if) in order, never a reported
    fact's kind, and a second pass adds nothing because effective() leaves no adjustments."""
    assert effective(f1()).applied_overrides == ()
    x = with_overrides(with_facts(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}),
                       {"kind": "transport_quote", "run_code": "R-02", "fixed_cost": 900.0},
                       {"kind": "decide_at", "minute": 630})
    assert effective(x).applied_overrides == ("transport_quote", "decide_at")
    assert effective(effective(x)).applied_overrides == ("transport_quote", "decide_at")


def test_the_override_marker_is_neither_compared_nor_hashed():
    """D-18 WR-01: the marker records how an input was processed, not business data."""
    e = effective(with_overrides(f1(), {"kind": "cold_chain", "run_code": "R-02", "value": False}))
    bare = replace(e, applied_overrides=())
    assert e.applied_overrides == ("cold_chain",)
    assert e == bare
    assert canonical_json(e) == canonical_json(bare)
    assert "applied_overrides" not in canonical_json(e)
    assert from_dict(to_dict(e)).applied_overrides == ()


def test_the_version_uses_the_engines_number_boundary():
    """D-18 WR-07: rec_version hashes through coerce_input, so 5.0 in an int field hashes like 5, and a value the
    engine can't coerce exactly has no version."""
    inp = f1()
    assert rec_version(replace(inp, lots=(replace(inp.lots[0], expiry_day=5.0),))) == rec_version(inp)
    with pytest.raises(InputError, match="qty"):
        rec_version(replace(inp, lots=(replace(inp.lots[0], qty=True),)))


def test_every_entry_point_computes_on_the_coerced_input():
    """D-18 WR-07: each value coerces to F6's own (500.0, 60.0, 5, 40.0, 600.0), so every entry point sees F6:
    Transfer 80 + md20 at 18,440, phantom regret 840, and 80 is its cap."""
    f = f6()
    bn, lp = f.lots
    x = replace(f, lots=(replace(bn, qty=Decimal("500")), replace(lp, qty=Decimal("60"), expiry_day=5.0)),
                product=replace(f.product, price=Decimal("40.00")),
                runs=(replace(f.runs[0], fixed_cost=Decimal("600.00")),))
    r = evaluate(x)
    assert r == evaluate(f)
    assert fact_checks(x, r) == fact_checks(f, evaluate(f))
    assert baseline_plan(x) == baseline_plan(f)
    plan = Plan(TRANSFER, 20, 80, LAT_PHRAO, "R-02")
    assert check_plan(x, plan) == check_plan(f, plan) == []
    assert rec_version(x) == rec_version(f)


@pytest.mark.parametrize("change,match", [
    pytest.param(lambda x: replace(x, lots=(replace(x.lots[0], qty=True),)), "qty", id="qty-bool"),
    pytest.param(lambda x: replace(x, lots=(replace(x.lots[0], expiry_day=5.5),)), "expiry_day",
                 id="expiry-day-not-whole"),
    pytest.param(lambda x: replace(x, product=replace(x.product, price=Decimal("NaN"))), "price",
                 id="price-decimal-nan"),
])
def test_an_entry_point_rejects_a_number_it_cannot_coerce_exactly(change, match):
    with pytest.raises(InputError, match=match):
        evaluate(change(f1()))


def test_in03_policy_fields_hash_alike_as_int_or_float():
    """Round-3 IN-03: the new threshold fields are in the number-type tables, so 7 and 7.0 give one rec_version."""
    pol = replace(f1().policy, stale_after_days=7.0, baseline_near_expiry_days=1.0, baseline_markdown_pct=30.0,
                  phantom_min_forecast=10)
    assert rec_version(replace(f1(), policy=pol)) == rec_version(f1())


def test_the_shortlist_step_is_part_of_the_version():
    """Rev 6.3.0 (ENG-09): shortlist_step is hashed, and as an int field 10 and 10.0 give one rec_version."""
    def step(value):
        return replace(f1(), policy=replace(f1().policy, shortlist_step=value))
    assert rec_version(step(0)) != rec_version(step(10))
    assert rec_version(step(10)) == rec_version(step(10.0))


def test_the_schedule_is_part_of_the_version_in_any_order():
    a = replace(f1(), markdown_schedule=((2, 20), (1, 30)))
    assert rec_version(a) != rec_version(f1())
    assert rec_version(replace(f1(), markdown_schedule=((1, 30), (2, 20)))) == rec_version(a)
    assert rec_version(replace(f1(), markdown_schedule=((2.0, 20.0), (1, 30)))) == rec_version(a)
    assert rec_version(from_dict(to_dict(a))) == rec_version(a)
    assert evaluate(from_dict(to_dict(a))) == evaluate(a)


def test_a_null_schedule_in_an_export_is_an_input_error_naming_the_field():
    """Review WR-05: from_dict passes a JSON null (or a str, or a step that is not a list) through, so evaluate
    raises an InputError naming markdown_schedule instead of a bare TypeError; a missing key is still () (no
    schedule), and a list of lists still round-trips."""
    d = to_dict(f1())
    for bad in (None, "2:20 1:30", [5]):
        with pytest.raises(InputError, match="markdown_schedule"):
            evaluate(from_dict({**d, "markdown_schedule": bad}))
    assert from_dict({k: v for k, v in d.items() if k != "markdown_schedule"}).markdown_schedule == ()
    assert from_dict({**d, "markdown_schedule": [[2, 20], [1, 30]]}).markdown_schedule == ((2, 20), (1, 30))
