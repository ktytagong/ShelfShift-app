import pytest

from engine.model import ENGINE_VERSION, KEEP, MARKDOWN, TRANSFER, Plan, Policy
from tests.engine.fixtures import BANG_NA, f1
from engine.model import NEAR_EXPIRY_DAYS, PHANTOM_MIN_FORECAST, STALE_AFTER_DAYS, STALE_FACTOR
from engine.model import scheduled_pct, schedule_problem
from engine.model import reduced_schedule


def test_plan_keys_and_labels():
    assert (Plan(KEEP).key, Plan(KEEP).label) == ("keep", "Keep")
    assert (Plan(MARKDOWN, 20).key, Plan(MARKDOWN, 20).label) == ("md20", "Markdown 20%")
    hold = Plan(TRANSFER, 0, 140, "Lat Phrao", "R-02")
    assert hold.key == "tr:Lat Phrao:R-02:140:hold"
    assert hold.label == "Transfer 140 to Lat Phrao (R-02), hold rest"
    assert Plan(TRANSFER, 20, 80, "Lat Phrao", "R-02").label == "Transfer 80 to Lat Phrao (R-02), markdown rest 20%"


def test_case_lot_and_source():
    inp = f1()
    assert inp.case_lot().lot_id == "MLK-042"
    assert inp.source == BANG_NA


def test_a_colon_in_a_name_would_break_plan_keys():
    with pytest.raises(ValueError, match="':' is not allowed"):
        Plan(TRANSFER, 0, 10, "Lat:Phrao", "R-02").key


def test_money_guards_default_to_the_fixture_values():
    p = Policy()
    assert (p.min_regret, p.crossover_range_floor, p.adjustment_cost_max) == (1.0, 20_000.0, 100_000.0)


def test_engine_version_is_engine_3():
    assert ENGINE_VERSION == "engine-3"


def test_in03_policy_fields_default_to_todays_values():
    """Round-3 IN-03: the engine-2 constants keep their names and values under the D-20 freeze and are the Policy
    defaults; the engine reads the fields. Their old import paths are checked in test_factchecks and test_baseline."""
    p = Policy()
    assert (p.stale_after_days, p.stale_factor, p.phantom_min_forecast, p.baseline_near_expiry_days,
            p.baseline_markdown_pct) == (7, 0.7, 10.0, 1, 30)
    assert (STALE_AFTER_DAYS, STALE_FACTOR, PHANTOM_MIN_FORECAST, NEAR_EXPIRY_DAYS) == (7, 0.7, 10.0, 1)
    assert (p.stale_after_days, p.stale_factor, p.phantom_min_forecast, p.baseline_near_expiry_days) == \
        (STALE_AFTER_DAYS, STALE_FACTOR, PHANTOM_MIN_FORECAST, NEAR_EXPIRY_DAYS)


def test_the_waste_price_defaults_to_zero():
    """Rev 6.3.0 (ENG-10): with no waste price the score is the net, so the ranking is engine-2's."""
    assert Policy().waste_price == 0.0


def test_the_waste_flip_cap_defaults_to_180():
    """Rev 6.3.0 (D-05): 180 = $5/kg x 36 in fixture money."""
    assert Policy().waste_flip_max == 180.0


SCHEDULE_A, SCHEDULE_B, SCHEDULE_C = ((2, 20), (1, 30)), ((1, 20), (0, 30)), ((0, 30),)


def test_scheduled_pct_steps_from_opening_and_never_compounds():
    """D = 5 (Sat). A step (k, pct) applies from opening on day D - k; the deepest step in force wins, never added."""
    assert [scheduled_pct(SCHEDULE_A, 5, d) for d in (2, 3, 4, 5)] == [0, 20, 30, 30]
    assert [scheduled_pct(SCHEDULE_B, 5, d) for d in (3, 4, 5)] == [0, 20, 30]
    assert [scheduled_pct(SCHEDULE_C, 5, d) for d in (4, 5)] == [0, 30]
    assert scheduled_pct((), 5, 5) == 0


def test_schedule_problem_names_the_broken_rule():
    for ok in (SCHEDULE_A, SCHEDULE_B, SCHEDULE_C, ()):
        assert schedule_problem(ok) is None
    for bad in (((2, 25),), ((-1, 20),), ((1, 20), (1, 30)), ((2, 30), (1, 20))):
        assert schedule_problem(bad) is not None


def test_engine_input_has_no_schedule_by_default():
    assert f1().markdown_schedule == ()


def test_a_schedule_reduces_to_its_deepest_reaching_step_per_percent():
    """D-26: a step (k, P) applies from opening on day D - k, so among the steps of one percent the largest k starts
    first and is in force on every day the others are. Percents never compound (scheduled_pct takes the largest
    percent in force), so only the largest k of each percent can change the price: the others are redundant.
    Validation keeps levels to 20/30 and never lets the pct fall toward expiry, so the reduction is exact.
    Hand reductions: A reduces to itself; (3, 20), (2, 20), (1, 30), (0, 30) to (3, 20), (1, 30); (1, 20), (2, 20) to
    (2, 20) whatever the order; C and () to themselves; the 10,001-step schedule to (10_001, 20), (1, 30).
    """
    long = tuple((k, 20) for k in range(2, 10_002)) + ((1, 30),)
    cases = [
        (((2, 20), (1, 30)), ((2, 20), (1, 30))),
        (((3, 20), (2, 20), (1, 30), (0, 30)), ((3, 20), (1, 30))),
        (((1, 20), (2, 20)), ((2, 20),)),
        (((0, 30),), ((0, 30),)),
        ((), ()),
        (long, ((10_001, 20), (1, 30))),
    ]
    for full, reduced in cases:
        assert reduced_schedule(full) == reduced
    for full, _ in cases[:5]:
        for expiry in (3, 5, 6):
            for day in range(-2, 9):
                assert scheduled_pct(full, expiry, day) == scheduled_pct(reduced_schedule(full), expiry, day)
