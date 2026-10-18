"""Select another plan (07-CONTEXT D-23, D-12; 04-API-CONTRACT "Select another plan", "Approvals are bound to the plan
key"; research F-21, Pattern 4, Pitfall 11).

**The plan in force** (`plan_in_force`) is the selection bound to the active version, else the active version's
recommended plan. Every workflow consumer reads it: Send holds it, each decision records its key, the commit and
Prepare commit and recheck it, the card suggests it, the approvals list its resources and the commitment rule tests
its own fact checks (api/commitment.py `checks_in_force`). Nothing reads `result["recommended"]` for the workflow.

**Selecting** (`select`; the source manager; OPEN, WAITING or PENDING_APPROVAL; the active version):
- Only an engine-generated option can be selected: a row key of the active version's `result["options"]` (Keep, a
  markdown or another transfer). Anything else, a hand-made key with another quantity included, is 400
  not_an_option.
- The option is rechecked with `check_plan` on today's raw input, exactly as Send rechecks (409 conflict with the
  engine's reasons, verbatim).
- Its own fact checks are computed by the engine, unchanged: `fact_checks(inp, {**result, "recommended": key})` on the
  version's stored input, through the one worker (`_checks`, module-level so the worker unpickles it by name). The
  Required class is therefore evaluated against the selected plan (D-12), and with the recommended key it returns the
  stored checks (the identity test). It costs one search per suspect fact; the 3 s case budget is measured
  (tests/api/test_selection_budget.py), never raised.
- The selection stores the option's own `decide_by` as the engine computes it for one plan (engine.search
  `_deadlines` on the version's stored input: its run's latest decision time, or the markdown cutoff, the earlier
  one; no search), and the card's `due` and `decide_by` show the plan in force's (`decide_by`; 07 review G, D-32).
- The selection is stored on the case bound to the version, with who and when; a pending round is cancelled (its
  holds released, `approval_cancelled` with reason "plan selected", the case OPEN, and the closed lots those holds can
  affect rechecked at once, D-38.2), so the selected plan needs a fresh Send and every approval again. Selecting the
  recommended key clears the selection (the same round rule applies); selecting the plan already in force changes
  nothing.
- It is logged `plan_selected` with who, when and which option (and the plan it replaced).
- It never touches a lot hold (D-13): a hold on an involved lot still refuses the selected plan's commit.

A new version clears a selection (`clear`, called by cases.evaluate_case; Pitfall 11), and a selection whose
`rec_version` is not the active one is never in force even before that.

**The option list** (`options`, the case detail's `options` while a selection is possible): every row of the active
version's `result["options"]` with `feasible` and `reasons` from `check_plan` on today's raw input, `recommended`,
`selected`, and `required_fact_ids` (`option_dependencies`): the suspect facts under whose alternative that option
would break a hard limit, exactly as engine/factchecks.py classes a check Required (`suspect_facts` for the option
with the result's best transfer, `_with_alt`, `check_plan`; the covered-markdown reason is no broken limit). That needs
no search: one `check_plan` per option and fact. An option that is feasible and depends on no unverified fact is
the commitment rule's `select_plan` exit (api/commitment.py `next_actions`; D-12).
"""
from __future__ import annotations

import json
from dataclasses import replace

from engine import fact_checks, from_dict
from engine.adjust import AdjustmentError, InputError, effective
from engine.factchecks import _with_alt, suspect_facts
from engine.limits import check_plan, covered_reason
from engine.model import TRANSFER
from engine.search import _deadlines  # one plan's deadline keys, as evaluate gives the recommended plan's (no search)
from engine.simulate import HorizonError

from . import accounts, bookings, cases, db, realtime, worker
from .errors import ApiError, engine_error

SELECTABLE = ("OPEN", "WAITING", "PENDING_APPROVAL")
NOT_AN_OPTION = "Choose one of the plans the engine generated for this version"
CANCELLED = "plan selected"  # the approval_cancelled reason when a selection cancels a pending round
PUBLIC_KEYS = ("plan_key", "label", "rec_version")  # the select answer's `selected`
CARD_KEYS = ("plan_key", "label", "rec_version", "by", "at")  # the card's `selected_plan`
ENGINE_ERRORS = (InputError, AdjustmentError, HorizonError)


def _checks(inp, result: dict, key: str) -> list[dict]:
    """The engine's fact checks for the option `key` of this stored result: `recommended` replaced by the key, no
    engine change (research F-21, A10). Module-level, so the worker can unpickle it by name."""
    return fact_checks(inp, {**result, "recommended": key})


def bound(case: dict, rec: dict | None = None) -> dict | None:
    """The case's selection while it is bound to the active version (and to `rec`, when given), else None."""
    sel = case.get("selected")
    if not sel or not case.get("active_rec_version") or sel.get("rec_version") != case["active_rec_version"]:
        return None
    if rec is not None and sel.get("rec_version") != rec["rec_version"]:
        return None
    return sel


def plan_in_force(case: dict, rec: dict | None) -> tuple[str | None, list[dict], dict | None]:
    """(plan key, its fact checks, the selection or None) for the active version `rec`: the selection bound to it,
    else the recommended plan with the version's stored checks; (None, [], None) without a version."""
    if rec is None:
        return None, [], None
    sel = bound(case, rec)
    if sel is not None:
        return sel["plan_key"], list(sel["fact_checks"]), sel
    return rec["result"]["recommended"], list(rec["fact_checks"]), None


def _decide_by(engine_input: dict, plan) -> dict | None:
    """The engine's own `decide_by` for `plan` on the version's stored raw input made effective (engine.search
    `_deadlines`, as `evaluate` computes it for the recommended plan): the earlier of its run's latest decision time
    and its markdown cutoff, None when nothing limits the plan. No search, no database. An engine error becomes the
    contract's plain 4xx (07 review G)."""
    try:
        return json.loads(db.js(_deadlines(effective(from_dict(engine_input)), plan).get("decide_by")))
    except ENGINE_ERRORS as exc:
        raise engine_error(exc) from None


def decide_by(case: dict, rec: dict | None) -> dict | None:
    """The plan in force's `decide_by` (07 review G, D-32): a bound selection's own, stored at selection (computed
    from the stored input for a selection stored before it kept one), else the active version's result's; None
    without a version."""
    if rec is None:
        return None
    sel = bound(case, rec)
    if sel is None:
        return (rec["result"] or {}).get("decide_by")
    if "decide_by" in sel:
        return sel["decide_by"]
    return _decide_by(rec["engine_input"], cases.parse_plan(sel["plan_key"]))


def card_view(sel: dict | None) -> dict | None:
    """The card's `selected_plan`: {plan_key, label, rec_version, by, at}, or None."""
    return {k: sel.get(k) for k in CARD_KEYS} if sel else None


def _public(sel: dict | None) -> dict | None:
    return {k: sel[k] for k in PUBLIC_KEYS} if sel else None


def clear(conn, run_id: str, case_id: str) -> None:
    """Forget the case's selection: the recommended plan is in force again."""
    db.run(conn, "UPDATE cases SET selected = NULL WHERE run_id = :r AND case_id = :c AND selected IS NOT NULL",
           r=run_id, c=case_id)


def _cancel_round(conn, run: dict, case: dict) -> str:
    """A pending round ends as a new version ends it (cases.evaluate_case): holds released, approval_cancelled
    logged, the case OPEN; then the closed lots the released holds can affect are rechecked at once, through the
    helper a close uses (`closing.recheck_neighbours`, trigger "release"; D-38.2). Returns the case's state
    afterwards."""
    from . import closing  # it reads cases at import
    if case["state"] != "PENDING_APPROVAL":
        return case["state"]
    bookings.release(conn, run["run_id"], case["case_id"])
    db.log(conn, run["run_id"], run["day"], "approval_cancelled", case["case_id"], round=case["round"],
           rec_version=case["active_rec_version"], reason=CANCELLED)
    closing.recheck_neighbours(conn, run, case, "release")
    return "OPEN"


def select(conn, run: dict, actor: dict, case_id: str, rec_version: str, plan_key) -> dict:
    """POST /api/cases/{id}/select: {"state", "selected": {plan_key, label, rec_version} | None}. Order: expired holds
    applied, the case locked, the seat (source manager, 403), the state (409 wrong_state), the version (409
    stale_version), the option (400 not_an_option), then for another option than the recommended one the recheck (409
    conflict) and its own fact checks; then the round is cancelled, the selection stored and `plan_selected` logged,
    all in the caller's transaction."""
    from . import workflow  # workflow reads this module at import
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    workflow.require_source(actor, case)
    if case["state"] not in SELECTABLE:
        raise workflow._wrong_state(case)
    workflow._version(case, rec_version)
    rec = cases.active_rec(conn, case)
    if rec is None:
        raise ApiError(409, "missing_data", "This case has no recommendation")
    rows = {r["key"]: r for r in rec["result"]["options"]}
    if not isinstance(plan_key, str) or plan_key not in rows:
        raise ApiError(400, "not_an_option", NOT_AN_OPTION)
    current, _, sel = plan_in_force(case, rec)
    if plan_key == current:  # already the plan in force: nothing changes, nothing is logged
        return {"state": case["state"], "selected": _public(sel)}
    plan = cases.parse_plan(plan_key)
    chosen = None
    if plan_key != rec["result"]["recommended"]:
        workflow._recheck(workflow._current_input(conn, run, case, plan), plan)  # 409 conflict with the reasons
        checks = workflow._engine(worker.run, _checks, from_dict(rec["engine_input"]), rec["result"], plan_key)
        chosen = {"rec_version": rec["rec_version"], "plan_key": plan_key, "label": rows[plan_key]["label"],
                  "fact_checks": json.loads(db.js(checks)), "by": actor.get("account"),
                  "at": cases.utc(realtime.db_now(conn)),
                  "decide_by": _decide_by(rec["engine_input"], plan)}  # its own deadline (07 review G)
    state = _cancel_round(conn, run, case)
    dest = plan.dest if plan.kind == TRANSFER else None  # the board's scope follows the plan in force (05.2 D-26)
    # an expired hold described the earlier plan's round: the plan in force now has none yet
    db.run(conn, "UPDATE cases SET selected = CAST(:s AS jsonb), plan_dest = :p, state = :st, hold_expired = false "
                 "WHERE run_id = :r AND case_id = :c",
           s=db.js(chosen) if chosen is not None else None, p=dest, st=state, r=run["run_id"], c=case_id)
    db.log(conn, run["run_id"], run["day"], "plan_selected", case_id, rec_version=rec["rec_version"],
           plan_key=plan_key, label=rows[plan_key]["label"], previous=current, cleared=chosen is None,
           round=case["round"], actor=accounts.logged_actor(actor))
    return {"state": state, "selected": _public(chosen)}


def option_dependencies(engine_input: dict, result: dict) -> dict[str, list[str]]:
    """{option key: the fact ids that would be Required for that option, sorted}: for each row of `result["options"]`,
    the suspect facts for that option (`suspect_facts` with the result's best transfer, as fact_checks reads it) whose
    alternative makes `check_plan` refuse it for a reason other than the covered-markdown one (02-CONTEXT D-29, D-35).
    The stored input made effective, the alternative applied, applied_overrides cleared on that internal copy, as
    engine/factchecks.py does. No search. An engine error becomes the contract's plain 4xx."""
    try:
        eff = effective(from_dict(engine_input))
        plans = {r["key"]: cases.parse_plan(r["key"]) for r in result["options"]}
        first = next((r["key"] for r in result["options"] if r["kind"] == TRANSFER), None)
        best_transfer = plans[first] if first is not None else None
        out = {}
        for key, plan in plans.items():
            required = []
            for fact in suspect_facts(eff, plan, best_transfer):
                alt = _with_alt(eff, fact)
                covered = covered_reason(alt, plan.pct) if plan.pct else None
                if any(r != covered for r in check_plan(replace(alt, applied_overrides=()), plan)):
                    required.append(fact["fact_id"])
            out[key] = sorted(required)
        return out
    except ENGINE_ERRORS as exc:
        raise engine_error(exc) from None


def options(conn, run: dict, case: dict, rec: dict | None) -> list[dict]:
    """The case detail's `options` (04-API-CONTRACT Detail): while a selection is possible (OPEN, WAITING,
    PENDING_APPROVAL), every engine option of the active version in the result's order as {key, label, kind,
    feasible, reasons, required_fact_ids, recommended, selected}; `feasible` and `reasons` are `check_plan` on today's
    raw input (what select and Send recheck; a gone lot or a missing day makes every row infeasible with that reason).
    Empty in any other state or without a version."""
    if rec is None or case["state"] not in SELECTABLE:
        return []
    result = rec["result"]
    deps = option_dependencies(rec["engine_input"], result)
    sel = bound(case, rec)
    try:
        raw, gone = cases.current_input(conn, run, case), None
    except HorizonError as exc:
        raw, gone = None, str(exc)
    except LookupError:
        raw, gone = None, f"Lot {case['lot_id']} is no longer on the record"
    out = []
    for row in result["options"]:
        if raw is None:
            reasons = [gone]
        else:
            try:
                reasons = list(check_plan(raw, cases.parse_plan(row["key"])))
            except ENGINE_ERRORS as exc:
                reasons = [str(exc)]
        out.append({"key": row["key"], "label": row["label"], "kind": row["kind"], "feasible": not reasons,
                    "reasons": reasons, "required_fact_ids": deps.get(row["key"], []),
                    "recommended": row["key"] == result["recommended"],
                    "selected": sel is not None and sel["plan_key"] == row["key"]})
    return out
