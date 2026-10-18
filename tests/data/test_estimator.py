"""The store's in-stock estimate reads the record only (DATA-02).

Every expected value is derived by hand from the rule in data/estimator.py's docstring (same-weekday in-stock days,
then any in-stock day in the lookback, then the opening value, then "init_failed"), never pasted from output.
Order day d = 0 throughout; its same-weekday days for target x = 0 are −7, −14, −21, −28 (y = d − 1 − ((d − 1 − x) % 7)
= −1 − 6 = −7).
"""
import inspect

from data.estimator import TIERS, estimate

B, S = "Branch", "SKU"
START = -60  # the first simulated day in these records
KW = {"lookback": 28, "same_weekday": 4}


def record(values=None, stockouts=(), first=START, last=-1):
    """rec and stockout dicts for days first..last: recorded sales `values[day]` (default 0) and the stockout days."""
    values = values or {}
    rec = {(B, S, z): float(values.get(z, 0.0)) for z in range(first, last + 1)}
    so = {(B, S, z): z in stockouts for z in range(first, last + 1)}
    return rec, so


def everything_out_except(*days, first=START, last=-1):
    return {z for z in range(first, last + 1) if z not in days}


def test_the_tiers_are_the_five_named_ones():
    assert TIERS == ("four", "a", "b", "c", "init_failed")


def test_four_in_stock_same_weekdays_give_their_mean():
    # −7: 10, −14: 20, −21: 30, −28: 40, all in stock -> (10 + 20 + 30 + 40) / 4 = 25
    rec, so = record({-7: 10, -14: 20, -21: 30, -28: 40, -1: 999})
    assert estimate(rec, so, B, S, 0, 0, START, **KW) == (25.0, "four")


def test_two_of_four_in_stock_give_tier_a():
    # −14 and −28 were stockout days, so only −7 (10) and −21 (30) count: (10 + 30) / 2 = 20
    rec, so = record({-7: 10, -14: 99, -21: 30, -28: 99}, stockouts={-14, -28})
    assert estimate(rec, so, B, S, 0, 0, START, **KW) == (20.0, "a")


def test_no_same_weekday_falls_back_to_any_in_stock_day_in_28():
    # all four same-weekday days out; in stock only −3 (12) and −10 (18) -> (12 + 18) / 2 = 15
    rec, so = record({-3: 12, -10: 18}, stockouts=everything_out_except(-3, -10))
    assert estimate(rec, so, B, S, 0, 0, START, **KW) == (15.0, "b")


def test_the_opening_value_is_tier_c():
    rec, so = record(stockouts=everything_out_except())
    assert estimate(rec, so, B, S, 0, 0, START, 37.5, **KW) == (37.5, "c")


def test_no_data_and_no_opening_is_init_failed_and_zero():
    rec, so = record(stockouts=everything_out_except())
    assert estimate(rec, so, B, S, 0, 0, START, **KW) == (0.0, "init_failed")
    assert estimate(rec, so, B, S, 0, 0, START, None, **KW) == (0.0, "init_failed")


def test_nothing_before_start_is_read():
    # the record starts at −20: only −7 and −14 are on or after it -> (10 + 20) / 2 = 15, tier a
    rec, so = record({-7: 10, -14: 20}, first=-20)
    assert estimate(rec, so, B, S, 0, 0, -20, **KW) == (15.0, "a")


def test_the_order_day_and_later_are_never_read():
    rec, so = record({-7: 10, -14: 20, -21: 30, -28: 40})
    before = estimate(rec, so, B, S, 0, 0, START, **KW)
    rec[(B, S, 0)], so[(B, S, 0)] = 10_000.0, False
    rec[(B, S, 1)], so[(B, S, 1)] = 10_000.0, False
    assert estimate(rec, so, B, S, 0, 0, START, **KW) == before == (25.0, "four")
    # in the fallback tier too: only day 0 in stock beside out-of-stock history -> day 0 is not seen
    rec, so = record(stockouts=everything_out_except())
    rec[(B, S, 0)], so[(B, S, 0)] = 10_000.0, False
    assert estimate(rec, so, B, S, 1, 0, START, **KW) == (0.0, "init_failed")


def test_the_lookback_counts_day_d_minus_28_not_d_minus_29():
    # target x = 1 (weekday of d + 1): its same-weekday days are −6, −13, −20, −27, none of them −28 or −29.
    # In stock only on −28 (value 9): it is inside d − 28 .. d − 1, so tier b gives 9.
    rec, so = record({-28: 9}, stockouts=everything_out_except(-28))
    assert estimate(rec, so, B, S, 1, 0, START, **KW) == (9.0, "b")
    # In stock only on −29: outside the lookback, so nothing is known.
    rec, so = record({-29: 9}, stockouts=everything_out_except(-29))
    assert estimate(rec, so, B, S, 1, 0, START, **KW) == (0.0, "init_failed")


def test_at_most_four_same_weekday_observations_are_used():
    # −35 is the fifth same-weekday day back. With a 40-day lookback it is inside the window, so it only counts in
    # tier b, beside −3: (50 + 6) / 2 = 28. Were five observations used, it would give (50, "a").
    rec, so = record({-35: 50, -3: 6}, stockouts=everything_out_except(-35, -3))
    assert estimate(rec, so, B, S, 0, 0, START, lookback=40, same_weekday=4) == (28.0, "b")
    # with the 28-day lookback −35 is not read at all: only −3 -> 6
    assert estimate(rec, so, B, S, 0, 0, START, **KW) == (6.0, "b")


def test_the_estimator_takes_no_truth_parameter():
    names = list(inspect.signature(estimate).parameters)
    assert names == ["rec", "stockout", "branch", "sku", "x", "d", "start", "opening", "lookback", "same_weekday"]
    assert not {"demand", "mean", "truth", "latent"} & set(names)
