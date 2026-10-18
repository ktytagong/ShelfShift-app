import pytest

from engine.allocator import ShelfLot, allocate

M = {20: 43 / 30, 30: 1.6}


def lot(lot_id, qty, pct=0, expiry=5, seq=0):
    return ShelfLot(lot_id, "B", float(qty), expiry, seq, pct)


def test_zero_demand_sells_nothing_and_divides_by_nothing():
    lots = [lot("a", 10, pct=20)]
    s = allocate(0.0, lots, M, 40.0)
    assert (s.sold, s.lost, s.adjusted_demand) == (0.0, 0.0, 0.0)
    assert lots[0].qty == 10


def test_deepest_discount_first_then_20_then_full():
    lots = [lot("full", 100), lot("d20", 10, pct=20), lot("d30", 16, pct=30)]
    s = allocate(100.0, lots, M, 40.0)
    # 30% tier: rate 160, 16 units use f=0.1. 20% tier: rate 143.33, 10 units use f=0.0698. Full: D*r.
    r = 1 - 0.1 - 10 / (100 * 43 / 30)
    assert s.sold_by_lot["d30"] == pytest.approx(16)
    assert s.sold_by_lot["d20"] == pytest.approx(10)
    assert s.sold_by_lot["full"] == pytest.approx(100 * r)
    assert s.revenue == pytest.approx(16 * 28 + 10 * 32 + 100 * r * 40)
    assert s.lost == 0.0


def test_fefo_within_a_tier():
    lots = [lot("late", 50, expiry=6, seq=0), lot("early", 50, expiry=5, seq=1)]
    s = allocate(60.0, lots, M, 40.0)
    assert s.sold_by_lot == {"early": pytest.approx(50), "late": pytest.approx(10)}


def test_fefo_tie_goes_to_earliest_received():
    lots = [lot("second", 50, seq=2), lot("first", 50, seq=1)]
    s = allocate(60.0, lots, M, 40.0)
    assert s.sold_by_lot["first"] == pytest.approx(50)


def test_stock_runs_out_partway_and_lost_sales_are_full_price_only():
    lots = [lot("d20", 43, pct=20)]  # rate 143.33, so the 20% tier lasts 0.3 of the day
    s = allocate(100.0, lots, M, 40.0)
    assert s.sold == pytest.approx(43)
    assert s.lost == pytest.approx(70)  # D * r = 100 * 0.7, nothing at full price
    assert s.adjusted_demand == pytest.approx(43 + 70)


def test_single_markdown_lot_reduces_to_f_formula():
    lots = [lot("d20", 500, pct=20)]
    s = allocate(100.0, lots, M, 40.0)
    assert s.sold == pytest.approx(min(500, 100 * 43 / 30))


def test_reserved_units_never_sell():
    lots = [ShelfLot("a", "B", 500.0, 5, 0, reserved=100.0)]
    s = allocate(450.0, lots, M, 40.0)
    assert s.sold == pytest.approx(400)
    assert (lots[0].qty, lots[0].reserved) == (pytest.approx(100), 100.0)


def test_a_zero_response_rate_sells_nothing_and_divides_by_nothing():
    lots = [ShelfLot("a", "X", 5.0, 5, 0, pct=20)]
    s = allocate(10.0, lots, {20: 0.0, 30: 0.0}, 40.0)
    assert (s.sold, s.lost) == (0.0, 10.0)
    assert lots[0].qty == 5.0
