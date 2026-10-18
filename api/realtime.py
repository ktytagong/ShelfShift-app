"""People's real action times (07-CONTEXT D-05, D-06, D-09): one clock, the database's now().

A request's deadline, whether it is overdue, the minutes left and every label of a person's action come from Postgres
now() read in the transaction that reads or acts on the request, never from Python's clock or the device's. Inside one
transaction now() is fixed, so the stored time, the state check and the label agree. Nothing here stores a state:
"Unanswered" is derived on every read (no background job, no sweeper, no simulated clock; research anti-patterns).

Labels read "16 Oct 2026, 20:30" in the display time zone (`config.CAP_TZ`, the settings leaf caps.time_zone), built
from parts: no `%b` (locale-dependent) and no `%-d` (platform-specific). Business days stay day indices and go through
the dataset calendar (`Ref.date`, api/cases.py), never through here. This module imports nothing from signals or
cases: the caller passes the stored partial note in.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import config, db
from .errors import ApiError

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
CHECK_TIME_MISSING = "Choose when you checked: Checked just now, or a date and time."
CHECK_TIME_LATER = "The check time can't be later than now."
UNANSWERED = "Unanswered · Unverified"
OVERDUE = " · overdue"
PHOTO_MISSING = {"count": "Photo received · count missing", "expiry": "Photo received · date missing"}
# the label a request in any other state carries (04-API-CONTRACT Request `status`); the client composes the rest
STATE_LABELS = {"ANSWERED": "Answered", "CONFIRMED": "Confirmed", "CANT_CHECK": "Can't check",
                "SUPERSEDED": "Superseded", "HELD": "Held"}


def db_now(conn) -> datetime:
    """The database's now(): the one clock (fixed for the rest of the caller's transaction)."""
    return db.one(conn, "SELECT now() AS now")["now"]


def parse_observed(value, now: datetime) -> datetime:
    """The stated check time ("When did you check?", 07-CONTEXT D-22): "now" ("Checked just now") is `now`, the
    database's now() at submission; otherwise an ISO-8601 date-time with its offset, no later than `now` (equal is
    accepted). Missing, blank, naive (no offset) or anything else is 400 check_time with the first message; later than
    now with the second. There is no default: a submission time is never taken for a check time (a late answer would
    be dated as new)."""
    if isinstance(value, str) and value.strip() == "now":
        return now
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "check_time", CHECK_TIME_MISSING)
    try:
        at = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ApiError(400, "check_time", CHECK_TIME_MISSING) from None
    if at.tzinfo is None or at.utcoffset() is None:
        raise ApiError(400, "check_time", CHECK_TIME_MISSING)
    if at > now:
        raise ApiError(400, "check_time", CHECK_TIME_LATER)
    return at


def label(ts: datetime) -> str:
    """A real action time as "16 Oct 2026, 20:30" in the display time zone (D-09)."""
    t = ts.astimezone(config.CAP_TZ)
    return f"{t.day} {MONTHS[t.month - 1]} {t.year}, {t.hour:02d}:{t.minute:02d}"


def iso(ts: datetime | None) -> str | None:
    """A stored time as ISO-8601 UTC; None stays None."""
    return None if ts is None else ts.astimezone(timezone.utc).isoformat()


def minutes_left(due_at: datetime | None, now: datetime) -> int | None:
    """Whole minutes from `now` to `due_at`, floored, 0 once due; None without a deadline (a legacy row)."""
    if due_at is None:
        return None
    return max(0, math.floor((due_at - now).total_seconds() / 60))


def request_status(state: str, due_at: datetime | None, now: datetime, answer: dict | None = None,
                   part: str = "count") -> dict:
    """{kind, sentence, overdue, late} of a request read at `now` (D-06; 04-API-CONTRACT Request).

    `answer` is the request's current answer, or None before one: {complete, photo, note, answered_at}, where `note`
    is the stored partial note (signals.PARTIAL for a text reply without every count). Overdue is `due_at <= now`.
    - OPEN with no answer: AWAITING "Awaiting response · respond by {t}" before the deadline, UNANSWERED
      "Unanswered · Unverified" from it on.
    - OPEN with an incomplete answer: PHOTO_PARTIAL "Photo received · count missing" ("· date missing" for an expiry
      part) when it holds a photo, else TEXT_PARTIAL with the partial note; either ends " · overdue" once due, so
      the note never hides the deadline.
    - Any other state: its own kind and label; `late` when the complete answer arrived after `due_at` ("Answered
      after deadline"), kept after confirmation.
    - A legacy row (no `due_at`) or a legacy EXPIRED one: its stored state and no sentence."""
    if due_at is None or state == "EXPIRED":
        return {"kind": state, "sentence": None, "overdue": False, "late": False}
    overdue = due_at <= now
    complete = answer is not None and answer["complete"]
    late = complete and answer["answered_at"] > due_at
    if state == "OPEN" and not complete:
        if answer is None:
            if overdue:
                return {"kind": "UNANSWERED", "sentence": UNANSWERED, "overdue": True, "late": False}
            return {"kind": "AWAITING", "sentence": f"Awaiting response · respond by {label(due_at)}",
                    "overdue": False, "late": False}
        kind, note = ("PHOTO_PARTIAL", PHOTO_MISSING[part]) if answer["photo"] else ("TEXT_PARTIAL",
                                                                                     answer["note"] or "")
        return {"kind": kind, "sentence": note + (OVERDUE if overdue else ""), "overdue": overdue, "late": False}
    return {"kind": state, "sentence": STATE_LABELS.get(state), "overdue": overdue, "late": late}
