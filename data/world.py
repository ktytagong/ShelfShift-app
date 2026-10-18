"""Calendar helpers for the synthetic world (spec §5).

Everything here is synthetic. The values come from `reference/` (world.json and scenarios.json) through
`data.config`; this module holds no retailer value of its own. Day 0 is the first demo day; history runs from
`first_history_day()` (−history_days) to day −1, and the generator simulates a burn-in of `burn_in_days` before it
that is discarded (03-CONTEXT D-03).
"""
from __future__ import annotations

from datetime import date, timedelta

from . import config

_CFG = config.load()

DAY0: date = date.fromisoformat(_CFG["world"]["calendar"]["day0"])
SEED: int = _CFG["scenarios"]["demo"]["seed"]


def demo_date(day: int) -> date:
    """The calendar date of day index `day` (day 0 = DAY0)."""
    return DAY0 + timedelta(days=day)


def shelf_life(sku: str, cfg: dict | None = None) -> int:
    """Days from receipt to expiry: expiry = received + shelf life (03-CONTEXT D-13)."""
    return (cfg or _CFG)["world"]["products"][sku]["shelf_life_days"]


def horizon_end(cfg: dict | None = None) -> int:
    """The last day any demo-week lot can still sell: the last demo day plus the longest shelf life."""
    return config.horizon_end(cfg or _CFG)


def first_history_day(cfg: dict | None = None) -> int:
    """The first kept history day: −history_days (−90)."""
    return -(cfg or _CFG)["world"]["calendar"]["history_days"]


def burn_in_start(cfg: dict | None = None) -> int:
    """The first simulated day: burn_in_days before the first history day (−146; 03-CONTEXT D-03)."""
    cfg = cfg or _CFG
    return first_history_day(cfg) - cfg["world"]["calendar"]["burn_in_days"]
