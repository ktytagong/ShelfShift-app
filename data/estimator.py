"""The store's in-stock estimate (DATA-02; spec §5 Demand).

E(x) for target day x, for a delivery arriving on day d, from the store's record only (DATA-02). It receives
recorded sales and recorded stockout flags for days before d, never demand, the model mean or truth. `opening` is
passed only during the burn-in (d < −90).

The rule, in order:
1. the same-weekday days before d (the most recent day y < d with x's weekday, then y − 7, y − 14, ...), up to
   `same_weekday` of them, kept when they are on or after `start` and in stock: their mean ("four" when all of them
   are kept, else "a");
2. else every in-stock day in the `lookback` days before d (d − lookback .. d − 1): their mean ("b");
3. else the opening value, when one is given ("c");
4. else 0 units, "init_failed".

With `same_weekday` 4 and `lookback` 28 every same-weekday day lies in d − 28 .. d − 1 (y − 21 ≥ d − 28), so the
estimator never reads a day at or after d, nor one more than `lookback` days back.
"""
from __future__ import annotations

TIERS = ("four", "a", "b", "c", "init_failed")


def estimate(rec: dict, stockout: dict, branch: str, sku: str, x: int, d: int, start: int,
             opening: float | None = None, *, lookback: int, same_weekday: int) -> tuple[float, str]:
    """(E(x), tier) for target day `x` of a delivery arriving on day `d`.

    `rec` and `stockout` are keyed (branch, sku, day) and hold the store's recorded sales and recorded stockout
    flags; only days in `start` .. d − 1 are read. `start` is the first simulated day (nothing is recorded before it).
    """
    y = d - 1 - ((d - 1 - x) % 7)  # the most recent day before d with x's weekday
    keep = [z for z in (y - 7 * k for k in range(same_weekday)) if z >= start and not stockout[(branch, sku, z)]]
    if keep:
        return sum(rec[(branch, sku, z)] for z in keep) / len(keep), ("four" if len(keep) == same_weekday else "a")
    zs = [z for z in range(max(start, d - lookback), d) if not stockout[(branch, sku, z)]]
    if zs:
        return sum(rec[(branch, sku, z)] for z in zs) / len(zs), "b"
    if opening is not None:
        return opening, "c"
    return 0.0, "init_failed"
