"""The forecast input: estimated full-price-equivalent sales (03-CONTEXT D-05).

Never recovered true demand. Hidden demand and the hidden response are never used: the only inputs are the
recorded history rows, the cutoff and the store's assumed markdown response (m20, m30), which the caller reads
from the seed's `markdown_assumption` base values.

Order of operations, per branch x SKU series:
1. keep only days <= cutoff (nothing after the cutoff is ever read);
2. every non-stockout day is converted: sold_full + sold_20 / m20 + sold_30 / m30, tagged `converted` when it sold
   any discounted unit, else `recorded`;
3. every stockout day, with or without a markdown, is filled in from the converted non-stockout days: their mean
   over the same weekday when there are at least 2, else over the last 14; tagged `imputed`. With no
   non-stockout day at all it keeps its own converted sales, tagged `unfilled`.

A day with both flags is filled in, not converted: a price adjustment cannot recover sales lost to a stockout.

Stockout-day fills are not floored (Phase 3 UAT test 6, code review WR-02; the user kept the rule, 2026-10-15). A
fill is the mean of converted non-stockout days and is not floored at the day's own sales, so it can sit below that
day's converted sales and below the units recorded that day, although on a stockout day recorded sales are already a
lower bound on demand. On the release d0 run (cutoff −1), 73 of 178 imputed days do: 1,803.6 units below their
converted sales in total (1,854.1 below the recorded units). The rule therefore understates demand on stockout days,
which are the high-demand days. The backtest bias is −0.048 (the baseline's is −0.038); how much of it comes from
this rule, and its effect on transfer sizes, is not measured. Known limitation, to fix after the competition: floor
the fill at the day's own converted sales and rerun the forecast (`forecast.json` restales and needs a new BigQuery
run).

Values are computed from the unrounded multipliers and rounded once, to 4 places.
"""
from __future__ import annotations

PLACES = 4
SAME_WEEKDAY_MIN = 2
LAST_DAYS = 14


def _converted(row: dict, m20: float, m30: float) -> float:
    return row["sold_full"] + row["sold_20"] / m20 + row["sold_30"] / m30


def forecast_input(rows: list[dict], cutoff: int, m20: float, m30: float) -> list[tuple[int, float, str]]:
    """(day, value, tag) for every row with day <= cutoff, sorted by day. tag: recorded | converted | imputed |
    unfilled. The input rows are read, never written."""
    known = sorted((r for r in rows if r["day"] <= cutoff), key=lambda r: r["day"])
    clean = [(r["day"], _converted(r, m20, m30)) for r in known if not r["stockout_flag"]]
    out = []
    for r in known:
        if not r["stockout_flag"]:
            tag = "converted" if r["sold_20"] + r["sold_30"] > 0 else "recorded"
            out.append((r["day"], round(_converted(r, m20, m30), PLACES), tag))
            continue
        same = [v for d, v in clean if d % 7 == r["day"] % 7]
        pool = same if len(same) >= SAME_WEEKDAY_MIN else [v for _, v in clean[-LAST_DAYS:]]
        if pool:
            out.append((r["day"], round(sum(pool) / len(pool), PLACES), "imputed"))
        else:
            out.append((r["day"], round(_converted(r, m20, m30), PLACES), "unfilled"))
    return out
