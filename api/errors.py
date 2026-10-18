"""The one error the API raises for a client to see (04-API-CONTRACT "Errors").

Every non-2xx response is `{"error": code, "message": plain text, ...extra}`. CODES is the contract's error table,
code -> status: the closed set of `error` values the API returns. A code outside it, or a status that differs from
the table's, is a programming error and is refused when the ApiError is built, so the contract cannot drift silently.
A change to the table is a dated change-log row in 04-API-CONTRACT.md first (04-CONTEXT D-15).
"""
from __future__ import annotations

from engine.adjust import AdjustmentError, InputError
from engine.simulate import HorizonError

CODES = {
    "idempotency_key": 400, "bad_request": 400, "bad_input": 400, "bad_override": 400, "unknown_fact": 400,
    "out_of_bounds": 400, "not_writable": 400, "unknown_account": 400,
    "check_time": 400,  # Phase 7: "When did you check?" (07-CONTEXT D-22)
    "not_an_option": 400,  # Phase 7: Select another plan takes only an engine option (07-CONTEXT D-23)
    "locked": 401, "bad_passcode": 401,
    "forbidden": 403,
    "not_found": 404,
    "stale_version": 409, "sandbox_reset": 409, "conflict": 409, "incomplete_shipment": 409,
    "idempotency_conflict": 409, "hold_expired": 409, "wrong_state": 409, "already_decided": 409,
    "missing_data": 409, "demo_complete": 409, "too_many_requests": 409, "needs_input": 409, "count_mismatch": 409,
    "scope_tick": 409, "engine_version": 409, "not_available": 409, "gone": 409,
    "already_owned": 409, "not_owned": 409, "dispatch_blocked": 409, "discrepancy_open": 409,  # 06.1, all at once
    "older_evidence": 409, "review_required": 409,  # Phase 7: newer confirmed evidence wins (07-CONTEXT D-22)
    "commitment_blocked": 409, "ack_required": 409, "cannot_continue": 409,  # Phase 7: the commitment rule (D-12, D-17)
    "stale_plan": 409,  # Phase 7: approvals are bound to the plan key (07-CONTEXT D-23)
    "not_at_risk": 409,  # Phase 7: Reassess on a lot that no longer needs a decision (07-CONTEXT D-24)
    "already_dispatched": 409,  # Phase 7: cancel refuses work whose stock has left (07 review A; D-23, D-32)
    "quote_expired": 409,  # Phase 7: "This quote expired on {date}" (confirming a TRANSPORT quote; D-14)
    "too_large": 413,  # Phase 7: "Files can be at most 5 MB" (the number from uploads.max_bytes; D-20)
    "bad_file": 415,  # "Only PNG, JPEG or PDF files can be attached" (by content; api/files.py BAD_FILE)
    "cap_reached": 429,
    # 11-CONTEXT D-01, D-02; contract Judge-ready release (11).
    "paused": 503, "internal": 500,
    "preparing": 503, "engine_busy": 503, "engine_timeout": 503, "engine_unavailable": 503, "db_busy": 503,
}

# The one db_busy sentence (503): the pool-timeout handler (app.py), the series route (views.py) and the stock reads
# (stock.py) say the same words. No "demo": the store app shows it word for word (06.1 copy rule).
DB_BUSY = "The server is busy. Try again in a moment."  # (proposed)


class ApiError(Exception):
    """An error the client sees as JSON: {"error": code, "message": ..., **extra}."""

    def __init__(self, status: int, code: str, message: str, **extra):
        if CODES.get(code) != status:
            raise ValueError(f"{status} {code!r} is not in the contract's error table")
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def engine_error(exc: BaseException) -> ApiError | None:
    """An engine error as the contract's plain 4xx, by class, with the engine's own message (no traceback); None for
    anything else. Never a limit conflict: only reasons returned by check_plan become 409 conflict (04-API-CONTRACT
    "Errors"; contract-diff rows 4, 5, 23)."""
    if isinstance(exc, InputError):
        return ApiError(400, "bad_input", str(exc))
    if isinstance(exc, AdjustmentError):
        return ApiError(400, "bad_override", str(exc))
    if isinstance(exc, HorizonError):
        return ApiError(409, "missing_data", str(exc))
    return None
