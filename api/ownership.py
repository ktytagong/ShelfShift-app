"""Take ownership, and Send only by the owner (06.1 D-25, D-54, D-67; 04-API-CONTRACT "Ownership and the steps").

A stock decision waits at 1 · Trigger until someone owns it (D-25). Taking ownership is a write like any other: one
transaction after the idempotency key and the run lock, the case row locked FOR UPDATE, the activity entry in the same
transaction.

- **Who may take it** (D-67). The case's source store manager (the account equal to `source_branch`), or HQ across
  stores. Anyone else is 403 forbidden. For a branch transfer the source store owns the decision; the destination
  store handles its receipt tasks.
- **When**. While the case is OPEN, WAITING or INVALIDATED; otherwise 409 wrong_state.
- **Atomic**. The case row is locked FOR UPDATE before the owner is read, so of two accounts racing, the second waits
  and then sees the first one's owner: 409 already_owned with `owner`. An owned decision is never taken over by
  another account (no self-service takeover). The owner repeating it gets the same answer and no second log entry.
- **Send** (D-54). Only the owner sends: not owned is 409 not_owned, owned by another account is 403 forbidden.
- **No approval rights** (D-67). Ownership decides who sends, nothing else: approve, accept, decline and reject keep
  their seats in `workflow.decide`, and policy writes stay HQ's.

The account is read from the session row (`app._actor`), never from a request body (FLOW-10; 06.1 D-47).
"""
from __future__ import annotations

from . import accounts, bookings, cases, db
from .errors import ApiError

TAKEABLE = ("OPEN", "WAITING", "INVALIDATED")
NOT_OWNED = "Take ownership before sending this stock decision."
NOT_OWNER = "Only the owner of this stock decision can send it."
ALREADY_OWNED = "This stock decision is already owned by the {label}."
NOT_YOURS = "Only the {source} manager or HQ can take ownership of this stock decision."  # (proposed)
HQ_LABEL = "HQ planner"


def owner_view(case: dict) -> dict | None:
    """The case's owner as {account, role, label}, or None when nobody owns it. The label is "<store code> manager"
    or "HQ planner"."""
    account = case.get("owner_taken_by")
    if not account:
        return None
    if accounts.is_hq(account):
        return {"account": accounts.HQ, "role": accounts.HQ_ROLE, "label": HQ_LABEL}
    return {"account": account, "role": accounts.MANAGER, "label": f"{account} manager"}


def _answer(case: dict) -> dict:
    return {"owner": owner_view(case), "taken_at": case["owner_taken_at"]}


def _account(actor: dict) -> str | None:
    """The acting account as stored on a case ("HQ" for HQ), or None without one."""
    account = actor.get("account")
    if not account:
        return None
    return accounts.HQ if accounts.is_hq(account) else account


def _may_take(actor: dict, case: dict) -> bool:
    """The case's source store manager (account, role and branch all the source), or HQ."""
    account, source = _account(actor), case["source_branch"]
    if actor.get("role") == accounts.HQ_ROLE:
        return account == accounts.HQ
    return actor.get("role") == accounts.MANAGER and actor.get("branch") == source and account == source


def take_ownership(conn, run: dict, actor: dict, case_id: str) -> dict:
    """Make the acting account the case's owner, atomically (the case row FOR UPDATE), and log `ownership_taken`
    with the actor; {owner, taken_at}. The owner repeating it gets the stored answer and no second entry."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    if not _may_take(actor, case):
        raise ApiError(403, "forbidden", NOT_YOURS.format(source=case["source_branch"]))
    if case["state"] not in TAKEABLE:
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    owner = owner_view(case)
    account = _account(actor)
    if owner is not None:
        if owner["account"] == account:
            return _answer(case)
        raise ApiError(409, "already_owned", ALREADY_OWNED.format(label=owner["label"]), owner=owner)
    row = db.one(conn, "UPDATE cases SET owner_taken_by = :a, owner_taken_at = now() WHERE run_id = :r "
                       "AND case_id = :c RETURNING *", a=account, r=run["run_id"], c=case_id)
    db.log(conn, run["run_id"], run["day"], "ownership_taken", case_id,
           actor={"account": account, "role": actor["role"], "branch": actor.get("branch")})
    return _answer(row)


def require_owner(actor: dict, case: dict) -> None:
    """Send's gate (D-54): 409 not_owned when nobody owns the case, 403 forbidden when another account does."""
    owner = owner_view(case)
    if owner is None:
        raise ApiError(409, "not_owned", NOT_OWNED)
    if _account(actor) != owner["account"]:
        raise ApiError(403, "forbidden", NOT_OWNER)
