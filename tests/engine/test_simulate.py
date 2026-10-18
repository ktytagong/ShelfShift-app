from dataclasses import replace

import pytest

from engine.adjust import effective
from engine.model import KEEP, MARKDOWN, TRANSFER, Bookings, Forecast, Lot, Plan
from engine.simulate import HorizonError, simulate
from tests.engine.fixtures import BANG_NA, FRI, LAT_PHRAO, SAT, THU, f1, f6, with_overrides

TOL = 0.01


def daily(res, branch):
    return [round(res.sold_by[(branch, d)], 2) for d in (THU, FRI, SAT)]


F1_ROWS = [
    # plan, Bang Na sold Thu/Fri/Sat, Bang Na wasted, Lat Phrao sold Thu/Fri/Sat, net
    (Plan(KEEP), [100, 100, 100], 200, [0, 0, 0], 11_600),
    (Plan(MARKDOWN, 20), [143.33, 143.33, 143.33], 70, [0, 0, 0], 13_620),
    (Plan(MARKDOWN, 30), [160, 160, 160], 20, [0, 0, 0], 13_400),
    (Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02"), [100, 100, 100], 60, [40, 50, 50], 16_880),
    (Plan(TRANSFER, 20, 140, LAT_PHRAO, "R-02"), [143.33, 143.33, 73.33], 0, [40, 50, 50], 16_520),
    (Plan(TRANSFER, 30, 140, LAT_PHRAO, "R-02"), [160, 160, 40], 0, [40, 50, 50], 15_080),
]


@pytest.mark.parametrize("plan,bn_sold,bn_wasted,lp_sold,net", F1_ROWS, ids=[r[0].key for r in F1_ROWS])
def test_f1_rows(plan, bn_sold, bn_wasted, lp_sold, net):
    res = simulate(f1(), plan, "base")
    assert daily(res, BANG_NA) == pytest.approx(bn_sold, abs=TOL)
    assert res.wasted_by.get(BANG_NA, 0.0) == pytest.approx(bn_wasted, abs=TOL)
    assert daily(res, LAT_PHRAO) == pytest.approx(lp_sold, abs=TOL)
    assert res.net == pytest.approx(net, abs=TOL)


def test_f1_units_add_up_for_transfer_hold():
    res = simulate(f1(), Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02"), "base")
    # 500 initial + 0 external inbound = 440 sold + 60 wasted + 0 on hand + 0 in transit
    assert res.initial == pytest.approx(res.sold + res.wasted + res.on_hand_end, abs=TOL)
    assert (res.sold, res.wasted) == (pytest.approx(440, abs=TOL), pytest.approx(60, abs=TOL))


@pytest.mark.parametrize("q", [60, 69, 70, 75, 80])
def test_f6_transfer_md20_formula(q):
    expected = 17_800 + 8 * q if q >= 70 else 15_420 + 42 * q
    res = simulate(effective(f6()), Plan(TRANSFER, 20, q, LAT_PHRAO, "R-02"), "base")
    assert res.net == pytest.approx(expected, abs=TOL)


def test_expiry_means_sellable_until_end_of_that_day():
    res = simulate(f1(), Plan(KEEP), "base")
    assert res.sold_by[(BANG_NA, SAT)] == pytest.approx(100)
    assert res.wasted_by[BANG_NA] == pytest.approx(200)


def test_arrival_after_1400_sells_next_day():
    late = effective(with_overrides(f1(), {"kind": "departure_delay_h", "run_code": "R-02", "hours": 1.0}))
    res = simulate(late, Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02"), "base")  # arrives 14:20
    assert res.sold_by[(LAT_PHRAO, THU)] == 0
    assert res.sold_by[(LAT_PHRAO, FRI)] == pytest.approx(50)


def test_destination_stock_pushed_out_is_counted_as_waste():
    inp = f1()
    sun = 6
    lp_lot = Lot("MLK-050", LAT_PHRAO, 200.0, sun, received_seq=0)  # 190 demand Thu-Sun: 10 wasted if nothing moves
    fc = inp.forecasts + (Forecast(BANG_NA, sun, 100, 100, 100), Forecast(LAT_PHRAO, sun, 50, 50, 50))
    inp = replace(inp, lots=inp.lots + (lp_lot,), forecasts=fc)
    keep = simulate(inp, Plan(KEEP), "base")
    moved = simulate(inp, Plan(TRANSFER, 0, 60, LAT_PHRAO, "R-02"), "base")
    # the transferred units expire Sat, so FEFO sells them before the Sunday lot, which then wastes more
    assert moved.wasted_by.get(LAT_PHRAO, 0) > keep.wasted_by.get(LAT_PHRAO, 0)


def test_markdown_same_day_when_prepared_by_1100():
    res = simulate(effective(with_overrides(f1(), {"kind": "decide_at", "minute": 11 * 60})), Plan(MARKDOWN, 20))
    assert res.net == pytest.approx(13_620, abs=TOL)


def test_markdown_next_day_when_prepared_after_1100():
    res = simulate(effective(with_overrides(f1(), {"kind": "decide_at", "minute": 11 * 60 + 1})), Plan(MARKDOWN, 20))
    # Thu at full price (100 sold), Fri and Sat at 20% off (143.33 each), the rest wasted
    assert res.sold_by[(BANG_NA, THU)] == pytest.approx(100)
    sold_md = 2 * 100 * 43 / 30
    assert res.net == pytest.approx(100 * 40 + sold_md * 32 - (500 - 100 - sold_md) * 2, abs=TOL)


def test_missing_forecast_fails_loudly():
    inp = f1()
    gap = replace(inp, forecasts=tuple(f for f in inp.forecasts if not (f.branch == LAT_PHRAO and f.day == SAT)))
    with pytest.raises(HorizonError):
        simulate(gap, Plan(KEEP))


def test_two_lots_at_one_branch_never_share_demand():
    inp = f1()
    second = Lot("MLK-043", BANG_NA, 500.0, SAT, received_seq=2)
    res = simulate(replace(inp, lots=inp.lots + (second,)), Plan(KEEP), "base")
    for day in (THU, FRI, SAT):
        assert res.sold_by[(BANG_NA, day)] == pytest.approx(100)  # demand is used once, not once per lot


def test_a_run_on_a_later_day_departs_that_day():
    inp = f1()
    friday = replace(inp, runs=(replace(inp.runs[0], day=FRI),))
    res = simulate(friday, Plan(TRANSFER, 0, 100, LAT_PHRAO, "R-02"), "base")
    assert res.sold_by[(LAT_PHRAO, THU)] == 0
    assert res.sold_by[(LAT_PHRAO, FRI)] == pytest.approx(50)
    assert res.sold_by[(BANG_NA, THU)] == pytest.approx(100)


def test_set_aside_units_are_off_sale():
    inp = replace(f1(), bookings=Bookings(lot_units=(("MLK-042", 450.0),)))
    res = simulate(inp, Plan(KEEP), "base")
    assert res.sold == pytest.approx(50)  # only 50 of the 500 are on sale


def test_units_promised_to_a_later_run_are_not_sold_first():
    inp = f1()
    busy_thu = tuple(replace(f, low=450.0, base=450.0, high=450.0) if (f.branch, f.day) == (BANG_NA, THU) else f
                     for f in inp.forecasts)
    friday = replace(inp, runs=(replace(inp.runs[0], day=FRI),), forecasts=busy_thu)
    res = simulate(friday, Plan(TRANSFER, 0, 100, LAT_PHRAO, "R-02"), "base")
    assert res.sold_by[(BANG_NA, THU)] == pytest.approx(400)  # the 100 promised units are off sale from today
    assert (res.moved, res.short) == (100.0, 0.0)


def test_a_lot_already_past_expiry_never_sells_and_is_wasted():
    inp = f1()
    expired = replace(inp, lots=(replace(inp.lots[0], expiry_day=THU - 1),))
    res = simulate(expired, Plan(KEEP), "base")
    assert (res.sold, res.wasted, res.on_hand_end) == (0.0, 500.0, 0.0)


def test_a_short_shipment_is_reported_not_hidden():
    inp = effective(with_overrides(f1(), {"kind": "handling_cost", "run_code": "R-02", "per_unit_cost": 1.0}))
    small = replace(inp, lots=(replace(inp.lots[0], qty=50.0),))
    res = simulate(small, Plan(TRANSFER, 0, 80, LAT_PHRAO, "R-02"), "base")
    assert (res.moved, res.short) == (50.0, 30.0)
    assert (res.transport, res.handling) == (600.0, 50.0)  # something shipped: the full quote, handling on 50


def test_two_set_aside_bookings_on_one_lot_add_up():
    inp = replace(f1(), bookings=Bookings(lot_units=(("MLK-042", 225.0), ("MLK-042", 225.0))))
    assert simulate(inp, Plan(KEEP), "base").sold == pytest.approx(50)


@pytest.mark.parametrize("cancel_cost,paid", [(None, 600.0), (150.0, 150.0)])
def test_a_trip_that_ships_nothing_pays_the_cancel_cost(cancel_cost, paid):
    inp = f1()
    empty = replace(inp, lots=(replace(inp.lots[0], qty=0.0),), runs=(replace(inp.runs[0], cancel_cost=cancel_cost),))
    res = simulate(empty, Plan(TRANSFER, 0, 80, LAT_PHRAO, "R-02"), "base")
    assert (res.moved, res.transport, res.handling) == (0.0, paid, 0.0)


def test_a_trip_that_ships_one_unit_pays_the_full_quote():
    inp = f1()
    one = replace(inp, lots=(replace(inp.lots[0], qty=1.0),), runs=(replace(inp.runs[0], cancel_cost=150.0),))
    assert simulate(one, Plan(TRANSFER, 0, 80, LAT_PHRAO, "R-02"), "base").transport == 600.0


SCHEDULE_A = ((2, 20), (1, 30))


def test_keep_follows_the_schedule():
    """F1 + schedule A (D = Sat = 5): 20% Thu, 30% Fri and Sat; m20 = 43/30, m30 = 1.6 (spec §6 Markdown schedule).

    Thu: 100 x 43/30 = 143.33 at 32 = 4,586.67. Fri and Sat: 160 at 28 = 4,480 each.
    Sold 463.33, wasted 500 - 463.33 = 36.67 x 2 = 73.33. Net 4,586.67 + 8,960 - 73.33 = 13,473.33.
    """
    res = simulate(replace(f1(), markdown_schedule=SCHEDULE_A), Plan(KEEP))
    assert res.net == pytest.approx(13_473.33, abs=0.01)
    assert res.sold == pytest.approx(463.33, abs=0.01)
    assert res.wasted == pytest.approx(36.67, abs=0.01)


def test_a_transfer_and_its_moved_units_follow_the_schedule():
    """F1 + schedule A, Transfer 140 hold on R-02 (Thu); the moved lot is sellable Thu and both lots follow A.

    Bang Na 360: Thu 143.33 at 32, Fri 160 at 28, Sat 56.67 at 28 = 10,653.33, waste 0.
    Lat Phrao 140 (by the case lot's expiry, Sat): Thu 40 x 43/30 = 57.33 at 32 = 1,834.67; Fri 50 x 1.6 = 80 at 28
    = 2,240; Sat 2.67 at 28 = 74.67; total 4,149.33. Net 14,802.67 - 600 = 14,202.67.
    """
    res = simulate(replace(f1(), markdown_schedule=SCHEDULE_A), Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02"))
    assert res.net == pytest.approx(14_202.67, abs=0.01)
    assert res.wasted == pytest.approx(0, abs=0.01)
    assert res.sold_by[(LAT_PHRAO, THU)] == pytest.approx(57.33, abs=0.01)


def test_a_plan_markdown_is_never_shallower_than_the_schedule():
    """A 20% plan on a day the schedule is at 30% sells at 30%: the price is the deeper of the two.

    Markdown 30 from Thu: 160 x 3 = 480 at 28 - 20 x 2 = 13,400. Markdown 20: the schedule deepens it to 30 on Fri
    and Sat, so it equals Keep, 13,473.33, also when decided at 11:30 so that it starts Fri.
    """
    a = replace(f1(), markdown_schedule=SCHEDULE_A)
    assert simulate(a, Plan(MARKDOWN, 30)).net == pytest.approx(13_400, abs=0.01)
    assert simulate(a, Plan(MARKDOWN, 20)).net == pytest.approx(13_473.33, abs=0.01)
    assert simulate(replace(a, decision_min=690), Plan(MARKDOWN, 20)).net == pytest.approx(13_473.33, abs=0.01)
