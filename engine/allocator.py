"""The sales allocator: the only function that turns demand into sales (spec §6).

Called once per branch x SKU x day, never once per case, so two cases at the
same branch can never use the same demand twice.
"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9


@dataclass(slots=True)
class ShelfLot:
    lot_id: str
    branch: str
    qty: float
    expiry_day: int
    received_seq: int
    pct: int = 0  # 0, 20 or 30
    sellable_from: int = -(10**9)
    recorded: float = 0.0  # used by world.py only: the record, which can differ from qty
    reserved: float = 0.0  # set aside for a committed transfer: still here, off sale until it departs

    @property
    def on_sale(self) -> float:
        return max(0.0, self.qty - self.reserved)


@dataclass(frozen=True)
class DaySales:
    sold: float
    revenue: float
    lost: float  # lost sales, at full price only
    adjusted_demand: float
    sold_by_lot: dict[str, float]


def _fefo(lot: ShelfLot) -> tuple[int, int]:
    return (lot.expiry_day, lot.received_seq)


def _take(lots: list[ShelfLot], units: float, sold_by_lot: dict[str, float]) -> None:
    for lot in sorted(lots, key=_fefo):
        if units <= EPS:
            return
        take = min(lot.on_sale, units)
        lot.qty -= take
        units -= take
        sold_by_lot[lot.lot_id] = sold_by_lot.get(lot.lot_id, 0.0) + take


def allocate(demand: float, lots: list[ShelfLot], m: dict[int, float], price: float) -> DaySales:
    """Sell one day's full-price demand `demand` from `lots` (mutated in place). Reserved units never sell.

    Deepest discount first, FEFO within a tier. A discounted tier with stock S
    runs at demand*m_k for a share f_k = min(r, S/(demand*m_k)) of the day.
    """
    sold_by_lot: dict[str, float] = {}
    if demand <= 0:
        return DaySales(0.0, 0.0, 0.0, 0.0, sold_by_lot)
    r = 1.0
    sold = revenue = adjusted = 0.0
    for pct in (30, 20):
        tier = [l for l in lots if l.pct == pct and l.on_sale > EPS]
        stock = sum(l.on_sale for l in tier)
        if stock <= EPS or r <= EPS:
            continue
        rate = demand * m[pct]
        if rate <= EPS:  # a zero response rate sells nothing in this tier (spec §6: no divisions by zero)
            continue
        f = min(r, stock / rate)
        units = f * rate
        _take(tier, units, sold_by_lot)
        sold += units
        revenue += units * price * (100 - pct) / 100
        adjusted += units
        r -= f
    full = [l for l in lots if l.pct == 0 and l.on_sale > EPS]
    stock_full = sum(l.on_sale for l in full)
    units = min(stock_full, demand * r)
    _take(full, units, sold_by_lot)
    sold += units
    revenue += units * price
    adjusted += demand * r
    lost = max(0.0, demand * r - stock_full)
    return DaySales(sold, revenue, lost, adjusted, sold_by_lot)
