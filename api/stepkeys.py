"""The nine steps, the four stages, the first-stage label, the owner, the next action and the blocked flag of a stock
decision (06.1-CONTEXT D-23, D-24, D-54, D-65; 04-API-CONTRACT "Product mode (06.1)", Card and detail keys).

This is the only place a case state becomes a step. The board, the table, Today and the schedule read these keys from
the server; the client never derives one, and nothing here moves a case: every key is read from the stored state, the
owner column, the active version's stock checks, the work orders and the discrepancies, so a step changes only after
a recorded business action (C-21; no client field). The Decision board's drag and Move controls invoke those actions
(06.1-CONTEXT D-69); they never write a stage (C-21).

- `step_of`: OPEN/WAITING/INVALIDATED not owned → TRIGGER; OPEN owned with a stock check OPEN or ANSWERED on the
  active version → CONTEXT; OPEN owned otherwise, WAITING or INVALIDATED owned → COMPARE; PENDING_APPROVAL → APPROVE;
  APPROVED → COMMIT; PREPARED → EXECUTE; EXECUTED or OBSERVED with an OPEN receipt discrepancy → EXECUTE (D-65: the
  decision stays in Execute until it is reconciled, and Close refuses it in either state); EXECUTED or OBSERVED
  otherwise → VERIFY; CLOSED, REJECTED → ARCHIVE. OPTIONS (3) is always done once a recommendation exists: the
  morning pass ran it, so no case stands on it. A flag (an unevaluated lot, api/flags.py) is TRIGGER.
- `first_stage` (D-54): a flag, or a case in the first stage with no recommendation, "needs_evaluation"; an
  evaluated case in the first stage "needs_action"; null outside the first stage.
- `owner_view`: `cases.owner_taken_by` as {account, role, label} ("{code} manager" or "HQ planner"), read here so
  this module needs no other 06.1 module.
- `next_action` {key, who}: the contract's keys. Who is a list of {role, branch} (HQ's branch is null).
- `blocked` (C-7, the server's report; Phase 7 replaces it with its own commitment rule later): missing data, an
  expired capacity hold that needs Check availability, or a PREPARED case whose planned transfer the dispatch check
  (api/execution.py) blocks. A decision behind us (executed, observed, closed, rejected) is never blocked.
"""
from __future__ import annotations

from engine.model import MARKDOWN, TRANSFER

STEPS = ("TRIGGER", "CONTEXT", "OPTIONS", "COMPARE", "APPROVE", "COMMIT", "EXECUTE", "VERIFY", "ARCHIVE")
STEP_NUMBER = {s: n for n, s in enumerate(STEPS, 1)}
STAGES = ("decide", "authorise", "do", "check")
STAGE = {"TRIGGER": "decide", "CONTEXT": "decide", "OPTIONS": "decide", "COMPARE": "decide", "APPROVE": "authorise",
         "COMMIT": "authorise", "EXECUTE": "do", "VERIFY": "check", "ARCHIVE": "check"}
STAGE_COUNTS = (*STAGES, "closed")  # the board's counts: "closed" is the archived cards, "check" the ones before it
FIRST = ("OPEN", "WAITING", "INVALIDATED")  # the states of the first stage (Trigger to Compare)
DONE = ("EXECUTED", "OBSERVED")  # the plan ran: Verify, or Execute while a receipt discrepancy is open
ARCHIVED = ("CLOSED", "REJECTED")
CHECK_STATES = ("OPEN", "ANSWERED")  # a stock check still in progress on the active version
NEEDS_EVALUATION, NEEDS_ACTION = "needs_evaluation", "needs_action"
HQ = "HQ"  # the HQ account (api/accounts.py)


def owner_view(case: dict) -> dict | None:
    """The owner as {account, role, label}, or None while nobody has taken ownership."""
    account = case.get("owner_taken_by")
    if not account:
        return None
    if account == HQ:
        return {"account": HQ, "role": "hq", "label": "HQ planner"}
    return {"account": account, "role": "manager", "label": f"{account} manager"}


def has_open_check(case: dict, requests: list[dict]) -> bool:
    """A stock check of this case OPEN or ANSWERED on its active version."""
    version = case.get("active_rec_version")
    return version is not None and any(
        r["case_id"] == case["case_id"] and r["state"] in CHECK_STATES and r["rec_version"] == version
        for r in requests)


def step_of(case: dict, has_open_check: bool, open_discrepancy: bool = False) -> str:
    """The case's step from its stored state, owner, stock checks and receipt discrepancies (D-24, D-65)."""
    state = case["state"]
    if state in ARCHIVED:
        return "ARCHIVE"
    if state in DONE:
        return "EXECUTE" if open_discrepancy else "VERIFY"
    if state == "PENDING_APPROVAL":
        return "APPROVE"
    if state == "APPROVED":
        return "COMMIT"
    if state == "PREPARED":
        return "EXECUTE"
    if not case.get("owner_taken_by"):
        return "TRIGGER"
    if state == "OPEN" and has_open_check:
        return "CONTEXT"
    return "COMPARE"


def stage_of(step: str) -> str:
    return STAGE[step]


def first_stage(item: dict) -> str | None:
    """D-54: a flag row (no state) or a first-stage case without a recommendation needs evaluation; an evaluated
    first-stage case needs action; any other case is past the first stage."""
    if "state" not in item:
        return NEEDS_EVALUATION
    if item["state"] not in FIRST:
        return None
    return NEEDS_ACTION if item.get("active_rec_version") else NEEDS_EVALUATION


def closed_as(case: dict) -> str | None:
    """COMPLETED once closed through Close (Phase 7 adds CANCELLED); null otherwise, a lot-gone close included."""
    return case.get("closed_as") if case["state"] == "CLOSED" else None


def _source(case: dict) -> dict:
    return {"role": "manager", "branch": case["source_branch"]}


def _owner_who(case: dict) -> list[dict]:
    """The owner as an actor; the source manager when nobody has taken ownership (a decision made before 06.1)."""
    owner = owner_view(case)
    if owner is None:
        return [_source(case)]
    return [{"role": owner["role"], "branch": None if owner["role"] == "hq" else owner["account"]}]


def _action(key: str | None, who: list[dict]) -> dict:
    return {"key": key, "who": who}


def planned_transfer(actions: list[dict]) -> dict | None:
    """The case's TRANSFER work order still to leave (PLANNED), else None."""
    return next((a for a in actions if a["kind"] == TRANSFER and a["state"] == "PLANNED"), None)


def next_action(case: dict, plan, decided: dict, execution: dict | None = None) -> dict:
    """What happens next and who does it (04-API-CONTRACT next_action). `plan` is the active version's Plan (or
    None), `decided` the current round's decisions by role, `execution` {"actions": the case's work orders,
    "open_discrepancy": bool}.

    First stage: Take ownership (the source manager) until someone owns it; then, by the owner, Evaluate while there
    is no recommendation, Check availability after a capacity hold expired, else Send (a stock check does not stop
    Send, C-7). An INVALIDATED case waits for the morning's re-evaluation (no key). Approval: the destination's
    Accept first, then the source's Approve (the judge's journey order; either order commits). APPROVED: Prepare
    (the source manager). PREPARED: Record dispatch (the transfer's origin) for a PLANNED transfer, Record receipt
    (the destination) once it is DISPATCHED, else Record price for a PLANNED markdown. EXECUTED or OBSERVED:
    Reconcile while a receipt discrepancy is open, else Close, both by the owner. Archived: no key, nobody."""
    state = case["state"]
    execution = execution or {}
    if state in ARCHIVED:
        return _action(None, [])
    if state in FIRST:
        if not case.get("owner_taken_by"):
            return _action("take_ownership", [_source(case)])
        if state == "INVALIDATED":
            return _action(None, _owner_who(case))
        if not case.get("active_rec_version"):
            return _action("evaluate", _owner_who(case))
        if state == "OPEN" and case.get("hold_expired"):
            return _action("check_availability", _owner_who(case))
        return _action("send", _owner_who(case))
    if state == "PENDING_APPROVAL":
        if plan is not None and plan.kind == TRANSFER and "destination" not in decided:
            return _action("accept", [{"role": "manager", "branch": plan.dest}])
        if "source" not in decided:
            return _action("approve", [_source(case)])
        return _action(None, [])
    if state == "APPROVED":
        return _action("prepare", [_source(case)])
    actions = execution.get("actions") or []
    if state == "PREPARED":
        moving = planned_transfer(actions)
        if moving is not None:
            origin = moving["origin"] or case["source_branch"]
            return _action("record_dispatch", [{"role": "manager", "branch": origin}])
        sent = next((a for a in actions if a["kind"] == TRANSFER and a["state"] == "DISPATCHED"), None)
        if sent is not None:
            return _action("record_receipt", [{"role": "manager", "branch": sent["dest"]}])
        if any(a["kind"] == MARKDOWN and a["state"] == "PLANNED" for a in actions):
            return _action("record_price", [_source(case)])
        return _action(None, [])
    if execution.get("open_discrepancy"):
        return _action("reconcile", _owner_who(case))
    return _action("close", _owner_who(case))


def blocked(conn, run: dict, case: dict, execution: dict | None = None) -> tuple[bool, list[str]]:
    """(blocked, reasons) in the contract's order: missing_data, hold_expired, dispatch_blocked. The dispatch check
    runs only for a PREPARED case with a PLANNED transfer (the one state it describes; a markdown or a transfer on its
    way is not dispatch work)."""
    state = case["state"]
    if state in ARCHIVED or state in DONE:
        return False, []
    reasons = []
    if case.get("missing_data"):
        reasons.append("missing_data")
    if state == "OPEN" and case.get("hold_expired"):
        reasons.append("hold_expired")
    if state == "PREPARED":
        moving = planned_transfer((execution or {}).get("actions") or [])
        if moving is not None:
            # imported here: execution reads workflow, which reads cases, which reads this module
            from .execution import dispatch_check
            if dispatch_check(conn, run, case, moving):
                reasons.append("dispatch_blocked")
    return bool(reasons), reasons


def flag_keys(row: dict) -> dict:
    """A flag's keys: Trigger, needs evaluation, nobody owns it, Evaluate by the store's manager, never blocked (the
    board's owner and blocked filters leave flags out)."""
    return {"step": "TRIGGER", "step_number": STEP_NUMBER["TRIGGER"], "stage": STAGE["TRIGGER"],
            "first_stage": first_stage(row), "owner": None,
            "next_action": _action("evaluate", [{"role": "manager", "branch": row["branch"]}]),
            "blocked": False, "blocked_reasons": [], "closed_as": None}


def keys(conn, run: dict, case: dict, plan, decided: dict, requests: list[dict], execution: dict) -> dict:
    """Every derived key of a case's card and detail, computed once here."""
    step = step_of(case, has_open_check(case, requests), bool(execution.get("open_discrepancy")))
    is_blocked, reasons = blocked(conn, run, case, execution)
    return {"step": step, "step_number": STEP_NUMBER[step], "stage": stage_of(step), "first_stage": first_stage(case),
            "owner": owner_view(case), "next_action": next_action(case, plan, decided, execution),
            "blocked": is_blocked, "blocked_reasons": reasons, "closed_as": closed_as(case)}
