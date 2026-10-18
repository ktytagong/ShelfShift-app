"""The forecast input (03-CONTEXT D-05): estimated full-price-equivalent sales. Every expected value is derived by
hand from the formula sold_full + sold_20 / m20 + sold_30 / m30 with m20 = 43/30 and m30 = 1.6."""
import copy
import inspect

from data.preprocess import forecast_input

M20 = 43 / 30
M30 = 1.6


def row(day, full=0.0, s20=0.0, s30=0.0, stockout=False, markdown=None):
    return {"day": day, "sold_full": full, "sold_20": s20, "sold_30": s30,
            "recorded": full + s20 + s30, "stockout_flag": stockout,
            "markdown_flag": (s20 + s30 > 0) if markdown is None else markdown}


def fi(rows, cutoff):
    return forecast_input(rows, cutoff, M20, M30)


def test_a_day_without_markdown_is_recorded_as_is():
    assert fi([row(0, 12)], 0) == [(0, 12.0, "recorded")]


def test_discounted_units_are_converted_by_the_assumed_response():
    # 5 + 16 / 1.6 = 15; 10 + (43/3) / (43/30) = 10 + 10 = 20
    assert fi([row(0, 5, s30=16)], 0) == [(0, 15.0, "converted")]
    assert fi([row(0, 10, s20=43 / 3)], 0) == [(0, 20.0, "converted")]


def test_a_stockout_day_is_filled_from_converted_same_weekday_days():
    # day -7 converts to 15.0, day 7 is 25.0 as recorded; day 0 (same weekday) is their mean, 20.0
    rows = [row(-7, 5, s30=16), row(0, 3, stockout=True), row(7, 25)]
    assert fi(rows, 7) == [(-7, 15.0, "converted"), (0, 20.0, "imputed"), (7, 25.0, "recorded")]


def test_a_stockout_day_with_a_markdown_is_still_filled_in():
    # converting day 0 would give 3 + 10 = 13.0; a stockout day is filled in whatever its markdown, so 20.0
    rows = [row(-7, 5, s30=16), row(0, 3, s20=43 / 3, stockout=True, markdown=True), row(7, 25)]
    assert fi(rows, 7)[1] == (0, 20.0, "imputed")


def test_fewer_than_two_same_weekdays_use_the_last_14_converted_days():
    # day 0 is a stockout and so are -7 and -14, its only same-weekday days in range: no clean same weekday.
    # Clean days -20..-17 hold 100; the last 14 clean days (-16, -15, -13..-8, -6..-1) hold 10, day -1 as
    # 5 + 8 / 1.6 = 10 converted. Their mean is 10.0; the older 100s are outside the last 14.
    rows = [row(d, 100) for d in range(-20, -16)]
    rows += [row(d, 0, stockout=True) if d in (-14, -7) else row(d, 10) for d in range(-16, -1)]
    rows += [row(-1, 5, s30=8), row(0, 0, stockout=True)]
    out = fi(rows, 0)
    assert out[-1] == (0, 10.0, "imputed")
    assert (-1, 10.0, "converted") in out


def test_no_clean_day_leaves_its_own_converted_sales_unfilled():
    # 2 + 1.6 / 1.6 = 3.0
    assert fi([row(0, 2, s30=1.6, stockout=True)], 0) == [(0, 3.0, "unfilled")]


def test_recorded_sales_are_never_overwritten():
    rows = [row(-7, 5, s30=16), row(0, 3, s20=2, stockout=True), row(7, 25)]
    before = copy.deepcopy(rows)
    fi(rows, 7)
    assert rows == before


def test_changing_sales_after_the_cutoff_does_not_change_the_input():
    rows = [row(d, 10 + d % 7, s20=1 if d % 3 == 0 else 0, stockout=d % 5 == 0) for d in range(-28, 11)]
    before = fi(rows, 2)
    for r in rows:
        if r["day"] > 2:
            r["sold_full"] = r["recorded"] = 10_000
            r["stockout_flag"] = False
    assert fi(rows, 2) == before
    assert max(d for d, _, _ in before) == 2


def test_preprocessing_takes_no_truth_parameter():
    assert list(inspect.signature(forecast_input).parameters) == ["rows", "cutoff", "m20", "m30"]


def test_converted_values_are_rounded_once_to_4_places():
    # 1 / (43/30) = 30/43 = 0.69767... -> 0.6977; a rounded multiplier 1.43 would give 0.6993
    assert fi([row(0, 0, s20=1)], 0) == [(0, 0.6977, "converted")]
    # the mean of 30/43 and 90/43 is 60/43 = 1.395348... -> 1.3953; averaging the rounded 0.6977 and 2.0930
    # would give 1.39535 -> 1.3954
    rows = [row(-7, 0, s20=1), row(0, 0, stockout=True), row(7, 0, s20=3)]
    assert fi(rows, 7) == [(-7, 0.6977, "converted"), (0, 1.3953, "imputed"), (7, 2.093, "converted")]
