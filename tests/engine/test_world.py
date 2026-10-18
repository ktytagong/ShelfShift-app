from dataclasses import replace

import pytest

from engine.allocator import ShelfLot
from engine.model import TRANSFER, Plan
from engine.model import KEEP
from engine.simulate import simulate
from engine.world import PriceChange, Shipment, apply_stock_correction, step_day
from tests.engine.fixtures import BANG_NA, FRI, LAT_PHRAO, MILK, SAT, THU, f1

M = {20: 43 / 30, 30: 1.6}


def wl(lot_id, branch, physical, recorded=None, expiry=SAT, seq=0, reserved=0.0):
    return ShelfLot(lot_id, branch, float(physical), expiry, seq,
                    recorded=float(physical if recorded is None else recorded), reserved=reserved)


def ship(qty, lot_id="MLK-042", sellable_from=THU, fixed=600.0, per_unit=0.0, cancel=None, origin=BANG_NA):
    return Shipment(lot_id=lot_id, qty=float(qty), dest=LAT_PHRAO, new_lot_id=f"{lot_id}>{LAT_PHRAO}",
                    sellable_from=sellable_from, fixed_cost=fixed, per_unit_cost=per_unit, cancel_cost=cancel,
                    origin=origin)


def test_phantom_lot_sells_nothing_and_is_a_record_correction_not_waste():
    lots = [wl("MLK-038", LAT_PHRAO, physical=0, recorded=60, expiry=THU)]
    out = step_day(lots, THU, {LAT_PHRAO: 40.0}, M, MILK)
    assert out.sold[LAT_PHRAO] == 0
    assert out.lost[LAT_PHRAO] == pytest.approx(40)
    assert out.wasted == {}
    assert out.record_corrections == [{"lot_id": "MLK-038", "recorded": 60.0, "physical": 0.0}]


def test_dispatch_ships_what_is_physically_there_and_never_leaves_a_negative_record():
    lots = [wl("MLK-042", BANG_NA, physical=50, recorded=50)]  # the record is below the planned 80
    out = step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 40.0}, M, MILK, shipments=(ship(80),))
    assert out.short_shipments == [{"lot_id": "MLK-042", "planned": 80.0, "shipped": 50.0, "transport_paid": 600.0}]
    assert out.sold[LAT_PHRAO] == pytest.approx(40)
    assert lots[0].recorded == 0.0  # not -30


def test_short_shipment_reconciles_the_record_to_the_physical_count():
    lots = [wl("MLK-042", BANG_NA, physical=30, recorded=50, reserved=50.0)]  # physical is below the record
    out = step_day(lots, THU, {BANG_NA: 0.0}, M, MILK, shipments=(ship(50),))
    assert out.short_shipments == [{"lot_id": "MLK-042", "planned": 50.0, "shipped": 30.0, "transport_paid": 600.0}]
    assert out.record_corrections == [{"lot_id": "MLK-042", "recorded": 20.0, "physical": 0.0}]
    assert (lots[0].qty, lots[0].recorded, lots[0].reserved) == (0.0, 0.0, 0.0)


def test_sales_never_push_the_record_below_zero():
    lots = [wl("a", BANG_NA, physical=30, recorded=10)]
    step_day(lots, THU, {BANG_NA: 30.0}, M, MILK)
    assert lots[0].recorded == 0.0


def test_reserved_units_stay_off_sale_until_they_ship():
    lots = [wl("MLK-042", BANG_NA, 500, reserved=100.0)]
    out = step_day(lots, THU, {BANG_NA: 450.0}, M, MILK)
    assert out.sold[BANG_NA] == pytest.approx(400)
    out = step_day(lots, FRI, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(100, sellable_from=FRI),))
    assert out.short_shipments == [] and lots[0].reserved == 0.0


def test_costs_and_net_for_the_day():
    lots = [wl("MLK-042", BANG_NA, 500, expiry=THU)]
    out = step_day(lots, THU, {BANG_NA: 100.0, LAT_PHRAO: 40.0}, M, MILK, shipments=(ship(140, per_unit=1.0),))
    assert out.transport == {BANG_NA: 600.0} and out.handling == {BANG_NA: 140.0}
    assert out.wasted == {BANG_NA: pytest.approx(260), LAT_PHRAO: pytest.approx(100)}  # all expire tonight
    assert out.disposal == {BANG_NA: pytest.approx(520), LAT_PHRAO: pytest.approx(200)}
    assert out.net == pytest.approx(140 * 40 - 600 - 140 - 720)


@pytest.mark.parametrize("cancel,paid", [(None, 600.0), (150.0, 150.0)])
def test_a_failed_trip_pays_the_cancel_cost_and_is_reported(cancel, paid):
    lots = [wl("MLK-042", BANG_NA, physical=0, recorded=80, reserved=80.0)]
    out = step_day(lots, THU, {BANG_NA: 0.0}, M, MILK, shipments=(ship(80, per_unit=1.0, cancel=cancel),))
    assert (out.transport, out.handling) == ({BANG_NA: paid}, {BANG_NA: 0.0})
    assert out.short_shipments == [{"lot_id": "MLK-042", "planned": 80.0, "shipped": 0.0, "transport_paid": paid}]


@pytest.mark.parametrize("on_hand", [0.0, 1.0, 50.0])
def test_forecast_and_world_charge_a_trip_the_same_way(on_hand):
    """All three worlds use step_day, and step_day and simulate share trip_cost: same shipment, same cost."""
    inp = f1()
    run = replace(inp.runs[0], per_unit_cost=1.0, cancel_cost=150.0)
    forecast = simulate(replace(inp, lots=(replace(inp.lots[0], qty=on_hand),), runs=(run,)),
                        Plan(TRANSFER, 0, 50, LAT_PHRAO, "R-02"), "base")
    lots = [wl("MLK-042", BANG_NA, on_hand, reserved=50.0)]
    out = step_day(lots, THU, {BANG_NA: 0.0}, M, MILK, shipments=(ship(50, per_unit=1.0, cancel=150.0),))
    assert (out.transport[BANG_NA], out.handling[BANG_NA]) == (forecast.transport, forecast.handling)


def test_a_lot_past_expiry_never_sells():
    lots = [wl("a", BANG_NA, 50, expiry=THU - 1)]
    out = step_day(lots, THU, {BANG_NA: 100.0}, M, MILK)
    assert out.sold[BANG_NA] == 0 and out.wasted == {BANG_NA: 50.0} and lots == []


def test_demand_is_allocated_once_per_branch():
    lots = [wl("a", BANG_NA, 500, seq=0), wl("b", BANG_NA, 500, seq=1)]
    out = step_day(lots, THU, {BANG_NA: 100.0}, M, MILK)
    assert out.sold[BANG_NA] == pytest.approx(100)


def test_price_change_applies_on_its_effective_day():
    lots = [wl("a", BANG_NA, 500)]
    out = step_day(lots, THU, {BANG_NA: 100.0}, M, MILK, price_changes=(PriceChange("a", 20, THU),))
    assert out.sold[BANG_NA] == pytest.approx(100 * 43 / 30)
    assert out.revenue[BANG_NA] == pytest.approx(100 * 43 / 30 * 32)


def test_units_add_up_with_reserved_and_in_transit_stock():
    lots = [wl("a", BANG_NA, 500, reserved=140.0), wl("p", LAT_PHRAO, 0, recorded=60)]
    start = sum(l.qty for l in lots)
    sold = wasted = 0.0
    for day in (THU, FRI, SAT):
        sh = (ship(140, lot_id="a", sellable_from=SAT),) if day == FRI else ()  # in transit Fri, sellable Sat
        out = step_day(lots, day, {BANG_NA: 100.0, LAT_PHRAO: 45.0}, M, MILK, shipments=sh)
        sold += sum(out.sold.values())
        wasted += sum(out.wasted.values())
        assert start == pytest.approx(sold + wasted + sum(l.qty for l in lots))


def test_world_matches_the_forecast_when_reality_equals_the_record():
    """The forecast (simulate) and execution (step_day) follow one policy: same plan, same demand, same result."""
    inp = f1()
    friday = replace(inp, runs=(replace(inp.runs[0], day=FRI, per_unit_cost=1.0),))
    forecast = simulate(friday, Plan(TRANSFER, 0, 100, LAT_PHRAO, "R-02"), "base")

    lots = [wl("MLK-042", BANG_NA, 500, seq=1, reserved=100.0)]  # the backend reserves at commit (Thu)
    sold = wasted = net = 0.0
    for day in (THU, FRI, SAT):
        sh = (ship(100, sellable_from=FRI, per_unit=1.0),) if day == FRI else ()
        demand = {f.branch: f.base for f in inp.forecasts if f.day == day}
        out = step_day(lots, day, demand, {20: inp.markdown.m20[1], 30: inp.markdown.m30[1]}, MILK, shipments=sh)
        sold += sum(out.sold.values())
        wasted += sum(out.wasted.values())
        net += out.net
    assert (sold, wasted) == (pytest.approx(forecast.sold), pytest.approx(forecast.wasted))
    assert net == pytest.approx(forecast.net)


def test_stock_correction_sets_the_record():
    lots = [wl("p", LAT_PHRAO, 0, recorded=60)]
    apply_stock_correction(lots, "p", 0.0)
    assert lots[0].recorded == 0.0


@pytest.mark.parametrize("qty", [0.0, -50.0, float("nan"), float("inf")])
def test_a_malformed_shipment_is_rejected_before_anything_moves(qty):
    """A shipment quantity must be a finite number above 0 (D-11); the world is untouched after the error."""
    lots = [wl("MLK-042", BANG_NA, 500, reserved=80.0)]
    with pytest.raises(ValueError, match="quantity"):
        step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(qty),))
    assert (len(lots), lots[0].qty, lots[0].recorded, lots[0].reserved) == (1, 500.0, 500.0, 80.0)


def test_a_shipment_whose_source_lot_has_expired_is_a_failed_dispatch():
    """MLK-042 expires Thu night and leaves the world; Friday's shipment of it ships nothing. The trip rule
    charges the cancel cost 150 plus handling 1.0 x 0 = 0 to the origin; no revenue, no disposal: net -150 (WR-08)."""
    lots = [wl("MLK-042", BANG_NA, 50, expiry=THU)]
    step_day(lots, THU, {BANG_NA: 0.0}, M, MILK)
    assert lots == []
    out = step_day(lots, FRI, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK,
                   shipments=(ship(50, sellable_from=FRI, per_unit=1.0, cancel=150.0),))
    assert (out.transport, out.handling) == ({BANG_NA: 150.0}, {BANG_NA: 0.0})
    assert out.short_shipments == [{"lot_id": "MLK-042", "planned": 50.0, "shipped": 0.0, "transport_paid": 150.0}]
    assert out.net == -150.0 and lots == []


def test_a_shipment_whose_source_lot_is_missing_is_a_failed_dispatch():
    """No source lot at execution: a failed delivery. With no cancel cost (None) the trip rule charges the
    full quote 600 to the origin, handling 0; net -600 (WR-08)."""
    lots = []
    out = step_day(lots, FRI, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(50, sellable_from=FRI),))
    assert (out.transport, out.handling) == ({BANG_NA: 600.0}, {BANG_NA: 0.0})
    assert out.short_shipments == [{"lot_id": "MLK-042", "planned": 50.0, "shipped": 0.0, "transport_paid": 600.0}]
    assert out.net == -600.0 and lots == []


def test_a_shipment_from_a_lot_past_its_expiry_is_a_failed_dispatch():
    """D-25: a source lot that is still listed but past its expiry day ships nothing. Day 0; lot A1 at A holds 10
    (record 10) and expired on day -1; ship 5 to B, quote 10, cancel cost 2, handling 0 per unit.

    The lot's last selling day (-1) is before day 0, so it is a failed delivery: trip_cost(10, 2, 0, 0) = (2, 0)
    is charged to A, the short shipment is {A1, planned 5, shipped 0, transport_paid 2}, and nothing moves (no B1).
    Nothing sells (expired; demand 0). Expiry writes off all 10 at A: disposal 10 x 2 = 20. The record (10) equals
    the physical count (10), so no correction; `lots` ends empty. Net = 0 - 2 - 0 - 20 = -22.
    Before the fix the full quote 10 was paid and 5 units moved to B to be wasted there (02-CONTEXT D-25)."""
    lots = [wl("A1", "A", 10, expiry=-1)]
    sh = Shipment(lot_id="A1", qty=5.0, dest="B", new_lot_id="B1", sellable_from=0, fixed_cost=10.0,
                  per_unit_cost=0.0, cancel_cost=2.0, origin="A")
    out = step_day(lots, 0, {"A": 0.0, "B": 0.0}, M, MILK, shipments=(sh,))
    assert out.transport == {"A": 2.0} and out.handling == {"A": 0.0}
    assert out.short_shipments == [{"lot_id": "A1", "planned": 5.0, "shipped": 0.0, "transport_paid": 2.0}]
    assert out.wasted == {"A": 10.0} and out.disposal == {"A": 20.0}
    assert out.record_corrections == [] and lots == []
    assert out.net == -22.0


def test_a_shipment_must_be_built_by_keyword_with_an_origin():
    """The old positional form and a keyword form without origin both fail at once; fields never shift (WR-08)."""
    with pytest.raises(TypeError):
        Shipment("MLK-042", 50.0, LAT_PHRAO, "MLK-042>Lat Phrao", THU, 600.0, 0.0, None)
    with pytest.raises(TypeError):
        Shipment(lot_id="MLK-042", qty=50.0, dest=LAT_PHRAO, new_lot_id="MLK-042>Lat Phrao", sellable_from=THU,
                 fixed_cost=600.0, per_unit_cost=0.0, cancel_cost=None)


@pytest.mark.parametrize("origin", ["", None])
def test_an_empty_origin_is_rejected_before_anything_moves(origin):
    lots = [wl("MLK-042", BANG_NA, 500, reserved=80.0)]
    with pytest.raises(ValueError, match="origin"):
        step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(80, origin=origin),))
    assert (len(lots), lots[0].qty, lots[0].recorded, lots[0].reserved) == (1, 500.0, 500.0, 80.0)


def test_an_origin_that_is_not_the_lots_branch_is_rejected_before_anything_moves():
    """The engine never guesses the source branch: an origin that differs from the lot's branch is refused."""
    lots = [wl("MLK-042", BANG_NA, 500, reserved=80.0)]
    with pytest.raises(ValueError, match="origin"):
        step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(80, origin=LAT_PHRAO),))
    assert (len(lots), lots[0].qty, lots[0].recorded, lots[0].reserved) == (1, 500.0, 500.0, 80.0)


@pytest.mark.parametrize("lot_pct, pct, match", [
    (0, 25, "20 or 30"),
    (0, 0, "20 or 30"),
    (30, 20, "does not lower"),
    (20, 20, "does not lower"),
])
def test_a_malformed_price_change_is_rejected_before_anything_moves(lot_pct, pct, match):
    """A price change must be 20% or 30% and lower the lot's price (D-11, WR-08). It is checked with the
    shipments in step 0, so after the error the lot, and the shipment of 80 in the same call, are untouched."""
    lots = [wl("MLK-042", BANG_NA, 500, reserved=80.0)]
    lots[0].pct = lot_pct
    with pytest.raises(ValueError, match=match):
        step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(80),),
                 price_changes=(PriceChange("MLK-042", pct, THU),))
    assert (len(lots), lots[0].qty, lots[0].recorded, lots[0].reserved, lots[0].pct) == (1, 500.0, 500.0, 80.0, lot_pct)


def test_a_price_change_from_20_to_30_applies():
    """A lot at 20% off goes to 30%: m30 = 1.6, so demand 100 sells 160 at 40 x 0.7 = 28, revenue 4,480."""
    lots = [wl("a", BANG_NA, 500)]
    lots[0].pct = 20
    out = step_day(lots, THU, {BANG_NA: 100.0}, M, MILK, price_changes=(PriceChange("a", 30, THU),))
    assert out.sold[BANG_NA] == pytest.approx(160)
    assert out.revenue[BANG_NA] == pytest.approx(160 * 28)


def test_two_price_changes_on_one_day_are_checked_in_order():
    """Two changes on Thu for a 0% lot: 30% passes (30 > 0, running pct now 30), then 20% does not lower
    the price (20 <= 30), so the pair would raise the price from 30% off to 20% off (round-3 WR-02). It is
    refused in step 0, so the lot, and the shipment of 80 in the same call, are untouched."""
    lots = [wl("MLK-042", BANG_NA, 500, reserved=80.0)]
    with pytest.raises(ValueError, match="does not lower"):
        step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(80),),
                 price_changes=(PriceChange("MLK-042", 30, THU), PriceChange("MLK-042", 20, THU)))
    assert (len(lots), lots[0].qty, lots[0].recorded, lots[0].reserved, lots[0].pct) == (1, 500.0, 500.0, 80.0, 0)


def test_a_price_change_for_an_earlier_day_is_neither_checked_nor_applied():
    """On Fri a lot already at 20% is given its whole schedule: (20% Thu, 30% Fri). The Thu change is for
    another day, so it is skipped; the Fri change lowers 20 -> 30. m30 = 1.6, so demand 100 sells 160 at
    40 x 0.7 = 28, revenue 160 x 28 = 4,480, and no error (round-3 WR-02)."""
    lots = [wl("a", BANG_NA, 500)]
    lots[0].pct = 20
    out = step_day(lots, FRI, {BANG_NA: 100.0}, M, MILK,
                   price_changes=(PriceChange("a", 20, THU), PriceChange("a", 30, FRI)))
    assert out.sold[BANG_NA] == pytest.approx(160)
    assert out.revenue[BANG_NA] == pytest.approx(160 * 28)
    assert lots[0].pct == 30


def test_a_price_change_for_a_later_day_is_a_no_op_today():
    """On Thu a lot at 30% is given a 20% change for Fri: not today's, so neither checked nor applied. The
    lot stays at 30%: m30 = 1.6, demand 100 sells 160 at 40 x 0.7 = 28, revenue 4,480, no error (round-3 WR-02)."""
    lots = [wl("a", BANG_NA, 500)]
    lots[0].pct = 30
    out = step_day(lots, THU, {BANG_NA: 100.0}, M, MILK, price_changes=(PriceChange("a", 20, FRI),))
    assert out.sold[BANG_NA] == pytest.approx(160)
    assert out.revenue[BANG_NA] == pytest.approx(160 * 28)
    assert lots[0].pct == 30


def test_the_world_follows_the_schedule_like_the_forecast():
    """Under schedule A (20% on D - 2, 30% on D - 1; D = Sat) the world steps Keep over Thu-Sat with the same
    schedule keyword and agrees with simulate: same sold, wasted and net, the hand net 13,473.33 (rev 6.3.0, ESL-01)."""
    inp = replace(f1(), markdown_schedule=((2, 20), (1, 30)))
    forecast = simulate(inp, Plan(KEEP), "base")
    lots = [wl("MLK-042", BANG_NA, 500, seq=1)]
    sold = wasted = net = 0.0
    for day in (THU, FRI, SAT):
        out = step_day(lots, day, {f.branch: f.base for f in inp.forecasts if f.day == day}, M, MILK,
                       schedule=inp.markdown_schedule)
        sold += sum(out.sold.values())
        wasted += sum(out.wasted.values())
        net += out.net
    assert (sold, wasted) == (pytest.approx(forecast.sold), pytest.approx(forecast.wasted))
    assert net == pytest.approx(forecast.net)
    assert net == pytest.approx(13_473.33, abs=0.01)


A = ((2, 20), (1, 30))


def test_moved_units_follow_the_schedule_in_the_world():
    """Transfer 140 hold under schedule A, shipped Thu and sellable Thu at Lat Phrao. The shipped lot keeps the source
    lot's expiry (Sat), so the schedule follows it: 20% on Thu, 30% on Fri. World equals forecast, net 14,202.67."""
    inp = replace(f1(), markdown_schedule=A)
    forecast = simulate(inp, Plan(TRANSFER, 0, 140, LAT_PHRAO, "R-02"), "base")
    lots = [wl("MLK-042", BANG_NA, 500, seq=1, reserved=140.0)]  # the backend reserves at commit (Thu)
    sold = wasted = net = 0.0
    for day in (THU, FRI, SAT):
        sh = (ship(140, sellable_from=THU),) if day == THU else ()
        demand = {f.branch: f.base for f in inp.forecasts if f.day == day}
        out = step_day(lots, day, demand, M, MILK, shipments=sh, schedule=A)
        sold += sum(out.sold.values())
        wasted += sum(out.wasted.values())
        net += out.net
    assert (sold, wasted) == (pytest.approx(forecast.sold), pytest.approx(forecast.wasted))
    assert net == pytest.approx(forecast.net)
    assert net == pytest.approx(14_202.67, abs=0.01)


def test_a_schedule_step_never_fails_the_price_check():
    """On Fri (D - 1) schedule A has a Sat lot at 30%: demand 100 sells 160 (m30 = 1.6) at 40 x 0.7 = 28. A
    manager's 20% change the same day passes the check (20 lowers 0), then the schedule takes the lot to the
    deeper 30%: nothing raises and the lot sells at 30%."""
    lots = [wl("a", BANG_NA, 500)]
    out = step_day(lots, FRI, {BANG_NA: 100.0}, M, MILK, schedule=A)
    assert out.sold[BANG_NA] == pytest.approx(160)
    lots = [wl("a", BANG_NA, 500)]
    out = step_day(lots, FRI, {BANG_NA: 100.0}, M, MILK, price_changes=(PriceChange("a", 20, FRI),), schedule=A)
    assert out.sold[BANG_NA] == pytest.approx(160)
    assert out.revenue[BANG_NA] == pytest.approx(160 * 28)
    assert lots[0].pct == 30


def test_a_manager_change_that_repeats_a_step_in_force_is_refused():
    """Schedule A took the lot to 20% on Thu (demand 100 sells 100 x 43/30 = 143.33, 356.67 left). A manager's 20%
    change on Fri doesn't lower the price, so it is refused before anything moves and the lot is untouched.
    Phase 4 drops or voids such a change before the step."""
    lots = [wl("a", BANG_NA, 500)]
    step_day(lots, THU, {BANG_NA: 100.0}, M, MILK, schedule=A)
    assert lots[0].pct == 20 and lots[0].qty == pytest.approx(356.67, abs=0.01)
    with pytest.raises(ValueError, match="does not lower"):
        step_day(lots, FRI, {BANG_NA: 100.0}, M, MILK, price_changes=(PriceChange("a", 20, FRI),), schedule=A)
    assert lots[0].qty == pytest.approx(356.67, abs=0.01) and lots[0].pct == 20


def test_a_change_equal_to_todays_schedule_step_is_accepted_and_changes_nothing():
    """IN-02: lot a (500, expires Sat) on Thu, where schedule A is at 20%. Before today's changes the lot is at 0%,
    so a manager's 20% change lowers the price and passes the check; the schedule's step today is also 20%, so the
    lot sells at 20% either way: 100 x 43/30 = 143.33 at 32 = 4,586.67, with or without the change. This pins the
    behavior spec §8 already states (only a step in force from an earlier day is refused); it passes before and
    after the IN-02 commit, which is a documentation fix, not a behavior change."""
    lots = [wl("a", BANG_NA, 500)]
    out = step_day(lots, THU, {BANG_NA: 100.0}, M, MILK, price_changes=(PriceChange("a", 20, THU),), schedule=A)
    assert lots[0].pct == 20
    assert out.sold[BANG_NA] == pytest.approx(100 * 43 / 30)
    assert out.revenue[BANG_NA] == pytest.approx(100 * 43 / 30 * 32)
    plain = [wl("a", BANG_NA, 500)]
    without = step_day(plain, THU, {BANG_NA: 100.0}, M, MILK, schedule=A)
    assert (out.sold, out.revenue) == (without.sold, without.revenue)


@pytest.mark.parametrize("s", [((2, 25),), ((1, 20), (1, 30)), None])  # None: a reason, not a TypeError (WR-05)
def test_a_bad_schedule_is_refused_before_anything_moves(s):
    """A schedule that breaks its rules (a pct other than 20 or 30; a days-before-expiry value twice) is refused
    first in step 0, so the lot, and the shipment of 80 in the same call, are untouched (rev 6.3.0)."""
    lots = [wl("MLK-042", BANG_NA, 500, reserved=80.0)]
    with pytest.raises(ValueError, match="schedule"):
        step_day(lots, THU, {BANG_NA: 0.0, LAT_PHRAO: 0.0}, M, MILK, shipments=(ship(80),), schedule=s)
    assert (len(lots), lots[0].qty, lots[0].recorded, lots[0].reserved, lots[0].pct) == (1, 500.0, 500.0, 80.0, 0)
