"""FLOW-11: one counter for every capped kind (04-CONTEXT D-08; 04-API-CONTRACT "Caps"; spec §4 Caps).

`usage_counts(scope, kind, window_start, count)` holds every count: scope is a session id for a per-session cap, or
'global' for the daily cap across sessions. Each spend is one atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING
count`, so concurrent spends never undercount, and it runs in the request's own transaction: a refused spend (and
anything else that fails) rolls back with the request, so a 429 leaves no count behind.

`spend` checks the per-session cap first (when there is a session and the kind has one), then the daily cap across
sessions (when the kind has one). Every limit, window and the time zone come from api/settings.json through
`config.SESSION_CAPS`, `config.GLOBAL_CAPS` and `config.CAP_TZ`; a kind with no configured cap is never counted and
never refused. A day window starts at local midnight in the cap time zone, an hour window on the hour, and a minute
window on the local clock minute: a refusal under a minute window (the wrong-passcode block, 06-CONTEXT D-24) lasts
until that clock minute ends, at most 60 s, and is never permanent.

The 429 `cap_reached` carries the kind and a plain message that names the limit and when it resets. The per-session
message shows the session's own limit (the published per-session caps); the daily message never shows the global
number. Wired in Phase 4: `reset` (per session) and `sandbox` (daily, a new session's sandbox and every reset). The
`ai`, `signal` and `upload` kinds are wired by Phases 9 and 7 when their endpoints arrive; a count request spends
nothing (D-20).

A per-client cap (`config.CLIENT_CAPS`, settings `caps.client`) is the same counter with the client address as the
scope, hashed by `client_scope` so no address is stored (WR-03: wrong passcodes, before any session exists). The
address is the one `app.client_address` returns (06-CONTEXT D-24): the rightmost X-Forwarded-For entry on Cloud Run,
the TCP peer elsewhere. Its message names "this address's" limit; `reached` refuses once the limit is reached without
counting anything, and `spend` returns how many are left under the kind's own cap (the 401's attempts_left).

A per-workspace cap (`config.WORKSPACE_CAPS`, settings `caps.workspace`; 07-CONTEXT D-20) is the same counter with
the scope `run:<run_id>`, which the caller passes as `spend`'s first argument: the reviewer allowance of field reports
(`signal`) and their first files (`upload`), shared by every seat of the workspace, so signing in again or switching
seat never refills it. Its message names "this workspace's" limit. A report spends the workspace cap first, then the
daily cap across sessions (when the kind has one); a refusal at either rolls both back with the request.
"""
from __future__ import annotations

from contextvars import ContextVar

import hashlib
from datetime import datetime, timedelta, timezone

from . import config, db
from .errors import ApiError

GLOBAL = "global"  # the scope of a daily cap across sessions
# how a message names each kind (wording, not a limit)
WHAT = {"ai": "AI requests", "signal": "field reports", "upload": "uploads", "reset": "resets",
        "sandbox": "new workspaces", "passcode": "wrong passcodes", "preview": "impact previews"}
PER = {"day": "daily", "hour": "hourly", "minute": "per-minute"}  # a global cap's window in its refusal


class DailyCapReached(ApiError):
    """Internal daily-cap identity; the public error envelope remains unchanged."""


def window_start(window: str, now: datetime | None = None) -> datetime:
    """The start of the window holding `now` (default: the current time), in the cap time zone: local midnight for
    "day", the top of the local hour for "hour", the local clock minute for "minute". Timezone-aware, stored as
    timestamptz."""
    if window not in config.WINDOWS:
        raise ValueError(f"cap window {window!r} is not one of {', '.join(config.WINDOWS)}")
    local = (now or datetime.now(timezone.utc)).astimezone(config.CAP_TZ)
    if window == "day":
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "minute":
        return local.replace(second=0, microsecond=0)
    return local.replace(minute=0, second=0, microsecond=0)


def _city() -> str:
    """The cap time zone's city, from its IANA name ("Asia/Bangkok" -> "Bangkok")."""
    return str(config.CAP_TZ.key).rsplit("/", 1)[-1].replace("_", " ")


def _resets(window: str, start: datetime) -> str:
    if window == "day":
        return f"midnight {_city()} time"
    nxt = (start + (timedelta(minutes=1) if window == "minute" else timedelta(hours=1))).astimezone(config.CAP_TZ)
    return f"{nxt:%H:%M} {_city()} time"


def _count(conn, scope: str, kind: str, start: datetime, n: int) -> int:
    """Add n to the counter for (scope, kind, window) atomically and return the new count."""
    return db.one(conn, "INSERT INTO usage_counts (scope, kind, window_start, count) VALUES (:s, :k, :w, :n) "
                        "ON CONFLICT (scope, kind, window_start) DO UPDATE SET count = usage_counts.count + :n "
                        "RETURNING count", s=scope, k=kind, w=start, n=n)["count"]


def client_scope(address: str | None) -> str:
    """The scope of a per-client cap: a hash of the client address, so the address itself is never stored."""
    return "client:" + hashlib.sha256((address or "").encode("utf-8")).hexdigest()[:32]


def _own_cap(kind: str) -> tuple[tuple[int, str], str] | None:
    """((limit, window), whose limit a message names) of the kind's per-session, per-client or per-workspace cap, or
    None."""
    if kind in config.CLIENT_CAPS:
        return config.CLIENT_CAPS[kind], "this address's"
    if kind in config.WORKSPACE_CAPS:  # scope run:<run_id>, every seat of the workspace (07-CONTEXT D-20)
        return config.WORKSPACE_CAPS[kind], "this workspace's"
    if kind in config.SESSION_CAPS:
        return config.SESSION_CAPS[kind], "this session's"
    return None


def _refusal(kind: str, limit: int, window: str, whose: str, start: datetime) -> ApiError:
    return ApiError(429, "cap_reached", f"You have reached {whose} limit ({limit} {WHAT.get(kind, kind)} per "
                                        f"{window}). It resets at {_resets(window, start)}.", kind=kind)


def reached(conn, scope: str, kind: str, now: datetime | None = None) -> None:
    """429 cap_reached when the scope's count of `kind` in this window is at its limit already; counts nothing."""
    own = _own_cap(kind)
    if own is None:
        return
    (limit, window), whose = own
    moment = now if now is not None else db.one(conn, "SELECT clock_timestamp() AS now")["now"]
    start = window_start(window, moment)
    row = db.one(conn, "SELECT count FROM usage_counts WHERE scope = :s AND kind = :k AND window_start = :w",
                 s=scope, k=kind, w=start)
    if row and row["count"] >= limit:
        raise _refusal(kind, limit, window, whose, start)


EVAL_ADMISSION = ContextVar("eval_admission", default=False)


def spend(conn, session_id: str | None, kind: str, n: int = 1, now: datetime | None = None) -> int | None:
    """Spend n of `kind`: the session's (or, for a per-client kind, the client scope's) cap first, then the daily cap
    across sessions. Reaching either is 429 cap_reached with the kind and a plain message; the caller's transaction
    then rolls the counts back. Returns the number left under the kind's own cap after this spend (its limit minus the
    new count) when it counted one, else None."""
    if EVAL_ADMISSION.get():
        return None
    what = WHAT.get(kind, kind)
    own = _own_cap(kind)
    left = None
    # Sample once after admission locks, including a wait that crosses midnight.
    # Both windows use the database wall clock, never the API host's clock.
    moment = now
    if moment is None and ((session_id and own is not None) or kind in config.GLOBAL_CAPS):
        moment = db.one(conn, "SELECT clock_timestamp() AS now")["now"]
    if session_id and own is not None:
        (limit, window), whose = own
        start = window_start(window, moment)
        count = _count(conn, session_id, kind, start, n)
        if count > limit:
            raise _refusal(kind, limit, window, whose, start)
        left = limit - count
    if kind in config.GLOBAL_CAPS:
        window = config.GLOBAL_WINDOWS.get(kind, "day")
        start = window_start(window, moment)
        if _count(conn, GLOBAL, kind, start, n) > config.GLOBAL_CAPS[kind]:
            # (proposed) no "demo" or "today" (06.1 copy rule)
            raise DailyCapReached(429, "cap_reached", f"The {PER[window]} limit for {what} has been reached; it "
                                                       f"resets at {_resets(window, start)}.", kind=kind)
    return left
