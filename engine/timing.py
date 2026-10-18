"""Timing terms shared by the engine and the UI (spec §6 Timing)."""
from __future__ import annotations

from .model import EngineInput, Policy, Run


def fmt_min(minute: int) -> str:
    """A minute of the day as HH:MM. Values outside 0-1439 wrap modulo 1440, so no time renders negative."""
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def fmt_at(at: int, today: int) -> str:
    """An absolute minute as "HH:MM" when it falls on `today`, otherwise "day N HH:MM"."""
    day = at // 1440
    return fmt_min(at) if day == today else f"day {day} {fmt_min(at)}"


def latest_decision_at(run: Run, policy: Policy) -> int:
    """`latest_decision_at` in absolute minutes (day * 1440 + minute): the run's (day, loading cutoff) minus
    packing; approval and prepare must both be done by then. Spec §6 Timing rev 6.2.1: it is a timestamp,
    so it can fall on the previous day (a Fri 01:00 cutoff with 2 h packing gives Thu 23:00)."""
    return run.day * 1440 + run.loading_cutoff_min - policy.packing_min


def latest_decision_min(run: Run, policy: Policy) -> int:
    """The minute (0-1439) of `latest_decision_at` on the deadline's own day, which can be the day before the run."""
    return latest_decision_at(run, policy) % 1440


def first_sellable_day(day: int, minute: int, policy: Policy) -> int:
    """Arrival before 14:00 sells that day, otherwise the next day. Minutes past 1440 roll over."""
    day += minute // 1440
    return day + (1 if minute % 1440 >= policy.sellable_same_day_before_min else 0)


def markdown_effective_day(inp: EngineInput) -> int:
    """A price change applies all day if prepare + relabel is done by 12:00, else from tomorrow."""
    p = inp.policy
    same_day = inp.decision_min + p.relabel_min <= p.markdown_same_day_by_min
    return inp.today if same_day else inp.today + 1
