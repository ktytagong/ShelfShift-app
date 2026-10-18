"""Accounts: who a session is (06.1 D-02, D-47, D-64, D-68; FLOW-10; 04-API-CONTRACT "Product mode (06.1)").

The account is the session's identity; the seat (role and branch) is derived from it at sign-in and is never set by
a client. Accounts are derived, never stored as a table: every store code of the workspace's reference data
(`Ref.branches`) is a store manager account, and "HQ" is the HQ account.

- **Credential** (`check_credential`). HQ compares only against `HQ_PASSCODE`; any other account only against
  `DEMO_PASSCODE` (the shared store passcode). Both sides are trimmed and compared with `secrets.compare_digest`; a
  blank expected value never matches, and HQ never falls back to the store passcode: while `hq_config_problem`
  names a problem, no HQ sign-in succeeds (D-47, D-64).
- **Account** (`resolve_account`). Checked after the credential, so a guess at a store code learns nothing; an
  unknown code is 400 unknown_account and spends nothing. The answer is the canonical code (a store code typed in
  another case resolves to the reference data's own spelling when exactly one store matches).
- **Rotation** (`rotate`). Sign-in on a browser that already has a session gives it a new session id that carries
  the old run_id, last_reset_ms and per-session cap counts; the old row is deleted in the same transaction (ASVS V3,
  session fixation).
- **Sign-out** (`sign_out`). Clears the account and keeps the workspace, so signing in again in that browser returns
  to it (D-02); every private route is then 401 locked.
- **Startup** (`hq_config_problem`). The app refuses to start when HQ_PASSCODE is missing, shorter than
  `config.HQ_MIN_LENGTH`, or equal to DEMO_PASSCODE. The message names the variable and the rule, never a value
  (D-64, D-68).

Nothing here prints, logs or stores a credential: the sign-in key row hashes the account only (app.open_session).
"""
from __future__ import annotations

import secrets

from . import config, db, refdata
from .errors import ApiError

HQ = "HQ"
MANAGER = "manager"
HQ_ROLE = "hq"
UNKNOWN = "No store with that code. Check the code and try again."


def _account(account) -> str:
    """The account as typed, trimmed ("" when it is not text)."""
    return account.strip() if isinstance(account, str) else ""


def is_hq(account) -> bool:
    return _account(account).upper() == HQ


def logged_actor(actor: dict) -> dict:
    """The acting account as an activity payload's `actor` {account, role, branch}, which views.actor reads back
    (06.1 D-47, D-58). Taken from the session actor (app._actor); the session id is never copied."""
    return {"account": actor.get("account"), "role": actor.get("role"), "branch": actor.get("branch")}


def _bytes(value) -> bytes:
    return value.strip().encode("utf-8") if isinstance(value, str) else b""


def hq_config_problem() -> str | None:
    """Why HQ_PASSCODE cannot be used, as the startup error (names the variable and the rule, never a value), or
    None when it is sound."""
    hq = _bytes(config.HQ_PASSCODE)
    if not hq:
        return "HQ_PASSCODE is not set; refusing to start"
    if len(config.HQ_PASSCODE.strip()) < config.HQ_MIN_LENGTH:
        return f"HQ_PASSCODE is shorter than {config.HQ_MIN_LENGTH} characters; refusing to start"
    if secrets.compare_digest(hq, _bytes(config.DEMO_PASSCODE)):
        return "HQ_PASSCODE equals DEMO_PASSCODE; refusing to start"
    return None


def check_credential(account, passcode) -> bool:
    """True when `passcode` is the credential of `account`'s kind: HQ_PASSCODE for HQ (and only while it is sound),
    DEMO_PASSCODE for any other account. Never raises and never reveals which part was wrong."""
    given = _bytes(passcode)
    if is_hq(account):
        if hq_config_problem() is not None:  # fail closed: a blank, short or duplicate HQ value opens nothing
            return False
        expected = _bytes(config.HQ_PASSCODE)
    else:
        expected = _bytes(config.DEMO_PASSCODE)
    return bool(expected) and secrets.compare_digest(given, expected)


def resolve_account(ref: refdata.Ref, account) -> tuple[str, str, str | None]:
    """(account, role, branch) of a signed-in account on this reference data; 400 unknown_account otherwise."""
    typed = _account(account)
    if is_hq(typed):
        return HQ, HQ_ROLE, None
    if typed in ref.branches:
        return typed, MANAGER, typed
    folded = [b for b in ref.branches if b.casefold() == typed.casefold()] if typed else []
    if len(folded) == 1:
        return folded[0], MANAGER, folded[0]
    raise ApiError(400, "unknown_account", UNKNOWN)


SIGN_IN = "sign-in"  # the endpoint of a sign-in key row: (key, the old session id, run '', "sign-in")


def claim(conn, key: str, session_id: str, digest: str) -> dict | None:
    """Insert the sign-in's key row first (as app._write does): None for a new request; the stored
    {"session", "answer"} for a repeat (a simultaneous duplicate waits on the row until the first commits); 409
    idempotency_conflict for the same key with another account. `digest` hashes the account only."""
    scope = {"k": key, "s": session_id, "e": SIGN_IN}
    if db.one(conn, "INSERT INTO idempotency_keys (key, session_id, run_id, endpoint, body_hash) "
                    "VALUES (:k, :s, '', :e, :h) ON CONFLICT DO NOTHING RETURNING key", h=digest, **scope):
        return None
    prev = db.one(conn, "SELECT body_hash, response FROM idempotency_keys WHERE key = :k AND session_id = :s "
                        "AND run_id = '' AND endpoint = :e", **scope)
    if prev["body_hash"] != digest:
        raise ApiError(409, "idempotency_conflict", "This Idempotency-Key was used with a different request")
    return prev["response"]


def remember(conn, key: str, session_id: str, stored: dict) -> None:
    """Store the sign-in's answer and its rotated session id on its key row (never the passcode)."""
    db.run(conn, "UPDATE idempotency_keys SET status = 200, response = CAST(:p AS jsonb) WHERE key = :k "
                 "AND session_id = :s AND run_id = '' AND endpoint = :e", p=db.js(stored), k=key, s=session_id,
           e=SIGN_IN)


def open_row(conn, account: str, role: str, branch: str | None) -> str:
    """A new session row for a browser without one (its workspace is created next); returns its id."""
    new = secrets.token_urlsafe(24)
    db.run(conn, "INSERT INTO sessions (session_id, role, branch, account) VALUES (:s, :role, :b, :a)", s=new,
           role=role, b=branch, a=account)
    return new


def rotate(conn, old: dict, account: str, role: str, branch: str | None) -> str:
    """A new session id for the signed-in account, carrying the old row's run_id, last_reset_ms and per-session cap
    counts; the old row is deleted in the caller's transaction. Returns the new id."""
    new = secrets.token_urlsafe(24)
    db.run(conn, "INSERT INTO sessions (session_id, role, branch, account, run_id, last_reset_ms) "
                 "VALUES (:n, :role, :b, :a, :r, :ms)", n=new, role=role, b=branch, a=account, r=old["run_id"],
           ms=old["last_reset_ms"])
    db.run(conn, "UPDATE usage_counts SET scope = :n WHERE scope = :o", n=new, o=old["session_id"])
    db.run(conn, "DELETE FROM sessions WHERE session_id = :o", o=old["session_id"])
    return new


def sign_out(conn, session: dict) -> dict:
    """Clear the session's account and keep its workspace (D-02); the caller holds the session row lock."""
    db.run(conn, "UPDATE sessions SET account = NULL WHERE session_id = :s", s=session["session_id"])
    return {"signed_out": True}
