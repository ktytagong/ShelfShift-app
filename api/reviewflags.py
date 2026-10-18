"""Review flags: new confirmed information on a committed case (07-CONTEXT D-17; 04-API-CONTRACT "Review flag").

When a confirmed fact's scope (api/scope.py) covers an APPROVED, PREPARED, EXECUTED or OBSERVED case, the case gets one
flag per (case, fact), "New confirmed information · review required", with the fact id. A flag means a person should
review the plan, not that the plan is invalid: nothing here re-plans, rewrites or blocks a committed case, and no
engine run decides a flag. On a PREPARED case the detail adds the informational line "Review before dispatch" (06.1's
dispatch check enforces it, api/execution.py).

"Reviewed · keep plan" (`keep`, 07-09): a person authorised to approve the case (its source manager, or the
destination manager of a transfer) clears exactly the named open flags with a required note; each gets cleared_at,
cleared_by and the note, logged `review_kept` with the fact ids. Only those flags clear: a later confirmed fact raises a
new flag. While an APPROVED case's Prepare checks would refuse (a lot hold or a Required check, api/commitment.py, or
check_plan on today's raw input) it is 409 cannot_continue and the flags stay open: the supported next step is to
cancel the approved plan (07-11). Keep never clears a hold, settles a Required check or makes a plan valid.
"""
from __future__ import annotations

import uuid

from . import accounts, db, realtime
from .errors import ApiError

REASON = "New confirmed information · review required"
CANNOT_CONTINUE = "This plan can't continue; cancel the approved plan instead"
BEFORE_DISPATCH = "Review before dispatch"  # informational on a PREPARED case (D-17); never says the work is blocked
STATES = ("APPROVED", "PREPARED", "EXECUTED", "OBSERVED")  # the committed states a confirmed fact flags (D-17)


def raise_for(conn, run: dict, cases: list[dict], fact_id: str) -> list[str]:
    """One flag for each case and this fact (a repeat does nothing: one_flag_per_case_fact), in the given order, each
    logged `review_flag_raised` in the caller's transaction; returns the case ids newly flagged."""
    out = []
    for case in cases:
        flag_id = f"FLAG-{uuid.uuid4().hex[:8]}"
        made = db.one(conn, "INSERT INTO review_flags (run_id, flag_id, case_id, fact_id, reason) VALUES (:r, :f, :c, "
                            ":x, :why) ON CONFLICT DO NOTHING RETURNING flag_id", r=run["run_id"], f=flag_id,
                      c=case["case_id"], x=fact_id, why=REASON)
        if made is not None:
            db.log(conn, run["run_id"], run["day"], "review_flag_raised", case["case_id"], flag_id=flag_id,
                   fact_id=fact_id, reason=REASON, state=case["state"], rec_version=case["active_rec_version"])
            out.append(case["case_id"])
    return out


def _rows(conn, run_id: str, case_id: str, only_open: bool) -> list[dict]:
    return db.rows(conn, "SELECT f.*, x.label AS fact_label FROM review_flags f LEFT JOIN reported_facts x "
                         "ON x.run_id = f.run_id AND x.fact_id = f.fact_id WHERE f.run_id = :r AND f.case_id = :c"
                   + (" AND f.cleared_at IS NULL" if only_open else "") + " ORDER BY f.created_at, f.flag_id",
                   r=run_id, c=case_id)


def open_for(conn, run_id: str, case_id: str) -> list[dict]:
    """The case's uncleared flags, oldest first, each with its fact's label (`fact_label`)."""
    return _rows(conn, run_id, case_id, True)


def open_for_many(conn, run_id: str, case_ids) -> dict[str, list[dict]]:
    """`open_for` for many cases in one read (the board's set-based cards, api/cases.py `cards`): {case_id: its
    uncleared flags, oldest first, each with its fact's label}; nothing is read when `case_ids` is empty."""
    out: dict[str, list[dict]] = {c: [] for c in case_ids}
    if not out:
        return out
    for row in db.rows(conn, "SELECT f.*, x.label AS fact_label FROM review_flags f LEFT JOIN reported_facts x "
                             "ON x.run_id = f.run_id AND x.fact_id = f.fact_id WHERE f.run_id = :r "
                             "AND f.case_id = ANY(:c) AND f.cleared_at IS NULL ORDER BY f.created_at, f.flag_id",
                       r=run_id, c=sorted(out)):
        out[row["case_id"]].append(row)
    return out


def for_case(conn, run_id: str, case_id: str) -> list[dict]:
    """Every flag of the case, cleared ones included, oldest first, each with its fact's label."""
    return _rows(conn, run_id, case_id, False)


def view(row: dict, state: str) -> dict:
    """A flag in the contract's shape (04-API-CONTRACT "Review flag"), plus `fact_label` (the confirmed fact's label)
    and `info`: "Review before dispatch" while the flag is open on a PREPARED case, else null."""
    return {"flag_id": row["flag_id"], "case_id": row["case_id"], "fact_id": row["fact_id"], "reason": row["reason"],
            "created_at": realtime.iso(row["created_at"]), "cleared_at": realtime.iso(row["cleared_at"]),
            "cleared_by": row["cleared_by"], "note": row["note"], "fact_label": row["fact_label"],
            "info": BEFORE_DISPATCH if state == "PREPARED" and row["cleared_at"] is None else None}


def keep(conn, run: dict, actor: dict, case_id: str, flag_ids, note) -> dict:
    """"Reviewed · keep plan" (07-CONTEXT D-17): {"cleared": [flag_id, ...]}. Order: the case locked (404 for another
    workspace's); the seat (the source manager, or a transfer's destination manager, from the session row: else 403);
    a note (blank 400); `flag_ids` a non-empty list of open flags of this case (else 400); on an APPROVED case the
    Prepare checks (the commitment rule's blocks, then the shipment and check_plan on today's raw input: 409
    cannot_continue with `blocks` or `reasons`, the flags left open). Then the flags clear and `review_kept` is logged
    in the caller's transaction."""
    from . import cases, commitment, selection, workflow  # they read this module at import
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    rec = cases.active_rec(conn, case)
    key, _, _ = selection.plan_in_force(case, rec)  # the plan in force's seats (07-CONTEXT D-23)
    plan = cases.parse_plan(key) if key else None
    seats = {case["source_branch"]} | ({plan.dest} if plan is not None and plan.dest else set())
    if actor.get("role") != "manager" or actor.get("branch") not in seats:
        raise ApiError(403, "forbidden", "Only a manager who approves this plan can review it")
    if not isinstance(note, str) or not note.strip():
        raise ApiError(400, "bad_request", "A review note is required")
    if (not isinstance(flag_ids, list) or not flag_ids or not all(isinstance(f, str) for f in flag_ids)
            or len(set(flag_ids)) != len(flag_ids)):
        raise ApiError(400, "bad_request", "Name the open review flags you reviewed")
    rows = db.rows(conn, "SELECT * FROM review_flags WHERE run_id = :r AND case_id = :c AND flag_id = ANY(:f) "
                         "AND cleared_at IS NULL ORDER BY created_at, flag_id FOR UPDATE",
                   r=run["run_id"], c=case_id, f=list(flag_ids))
    if len(rows) != len(flag_ids):
        raise ApiError(400, "bad_request", "Name the open review flags you reviewed")
    if case["state"] == "APPROVED" and plan is not None:
        found = commitment.blocks(conn, run, case, rec, plan, commitment.checks_in_force(case, rec))
        if found:
            raise ApiError(409, "cannot_continue", CANNOT_CONTINUE, blocks=found)
        try:
            raw = workflow._current_input(conn, run, case, plan)
            workflow._check_shipment(raw, case, plan)
            workflow._recheck(raw, plan)
        except ApiError as exc:
            raise ApiError(409, "cannot_continue", CANNOT_CONTINUE,
                           reasons=list(exc.extra.get("reasons") or [exc.message])) from None
    who = actor.get("account") or f"manager:{actor['branch']}"
    db.run(conn, "UPDATE review_flags SET cleared_at = now(), cleared_by = :by, note = :n WHERE run_id = :r "
                 "AND flag_id = ANY(:f)", by=who, n=note.strip(), r=run["run_id"], f=[r["flag_id"] for r in rows])
    db.log(conn, run["run_id"], run["day"], "review_kept", case_id, flag_ids=[r["flag_id"] for r in rows],
           fact_ids=[r["fact_id"] for r in rows], note=note.strip(), state=case["state"],
           rec_version=case["active_rec_version"], actor=accounts.logged_actor(actor))
    return {"cleared": [r["flag_id"] for r in rows]}
