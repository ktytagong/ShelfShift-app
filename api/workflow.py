"""The human actions' service layer: send, decide and commit, wait, reject, adopt and what-if (spec §7 Overrides,
spec §8 Send for approval, Decisions and approval, Recheck contract, Locking rule; 04-API-CONTRACT routes).

Each function runs inside the caller's transaction, after the run lock (`db.lock_run`), and checks the seat on the
server whatever the client shows (FLOW-10): only the case's owner sends (06.1 D-54, D-67: api/ownership.py; not
owned is 409 not_owned, another account's is 403); only the source-branch manager approves, rejects and prepares;
only the transfer's destination manager accepts or declines; ownership grants no approval, and other seats get 403.

The recheck (spec §8 Recheck contract, contract-diff row 6): `check_plan` always gets the RAW input of today's data
(`cases.current_input`: this case's own bookings and committed delivery left out, the adopted assumptions still in
effect), never an effective one; `engine.adjust.effective` is applied only where effective values are read (the
buckets to lock, the holds, the resource rows). Its reasons go back verbatim as 409 conflict with the resource rows;
an engine InputError, AdjustmentError or HorizonError is a plain 4xx (400 bad_input, 400 bad_override, 409
missing_data), never a conflict (rows 4, 5, 20).

A transfer whose source cannot be verified (the case lot not on the record at the case's source branch, no run today
with the plan's code from that branch to the destination, a quantity that is not a whole number of at least 1, or a
run cost that is not finite and non-negative) is refused with 409 incomplete_shipment at send and at commit, before
anything is locked, held or logged (Carried from Phase 1, round-2 IN-03).

Adopting an assumption (source manager, adoptable kinds only, with a note saying where it comes from) stores it with
its trip (the run code and that run's day in the active input) for transport_quote and handling_cost, its branch for
demand_multiplier, and neither for markdown_response and disposal_cost; the case re-evaluates with only the assumptions
still in effect today, so an assumption adopted for Thursday's trip never re-prices a later one (round-3 CR-01,
contract-diff row 9). A what-if (any seat) runs on a stored version plus its overrides and never changes the active
plan, its bookings or its approval round.

Every send, decision, commit, wait, rejection and adoption writes its activity entry in the same transaction
(T-04-24).

The commitment rule (Phase 7, api/commitment.py; 07-CONTEXT D-12, D-13): the decision that commits and Prepare refuse
with 409 commitment_blocked while a lot hold on a lot the case involves or a Required check of the plan in force
remains, before anything is locked, booked or written. Send is never refused for it.

Every close by a person (reject, the reject or decline decision; Cancel approved plan in api/closing.py) stores the
lot's decision-inputs fingerprint and its status after the release (closing.on_close; 07-CONTEXT D-24, D-28).

The plan in force (Phase 7, api/selection.py; 07-CONTEXT D-23) is what every action here works on: the plan the source
manager selected among the active version's engine options, while that selection is bound to the active version, else
the recommended plan.

Prepare (spec §8 Prepare and dispatch; source manager, APPROVED only) verifies the shipment and rechecks on the raw
input again, then writes the simulated work order for the shelfshift world: a TRANSFER action whose origin is the
case's source branch (the booked transfer's, never guessed), with the run's day and its costs as approved (the
effective run, so an adopted transport or handling quote is what the world pays) and the moved lot's id and first
sellable day under the sandbox Policy; and, for a markdown or a transfer's marked-down rest, a MARKDOWN action
effective from `markdown_effective_day`. A markdown the schedule now covers comes back as the engine's own reason in
a 409 conflict, and nothing is written (contract-diff row 42). Only PREPARED plans execute on Next day.
"""
from __future__ import annotations

import math
from dataclasses import replace

from engine import from_dict
from engine.adjust import ADOPTABLE, WHAT_IF_ONLY, AdjustmentError, InputError, effective, validate
from engine.limits import check_plan
from engine.model import KEEP, TRANSFER, EngineInput, Plan
from engine.simulate import HorizonError
from engine.timing import markdown_effective_day

from . import accounts, advance, bookings, cases, db, evaluate, inputs, ownership, refdata
from . import commitment  # Phase 7: Required checks and lot holds refuse commit and Prepare (07-CONTEXT D-12, D-13)
from . import selection  # Phase 7: the plan in force, the selection bound to the active version (07-CONTEXT D-23)
from . import closing  # Phase 7: every close records the lot's fingerprint and status (07-CONTEXT D-24, D-28)
from .errors import ApiError, engine_error

ENGINE_ERRORS = (InputError, AdjustmentError, HorizonError)
STALE = "This recommendation was replaced by a newer version. Reload the stock decision."  # 06.1 D-61
CONFLICT = "This plan can't go ahead now; see the reasons."
NO_SOURCE = "This transfer has no verifiable source; nothing was booked."
EXPIRED = "Capacity hold expired. Check availability and book again."  # 06.1 D-57, word for word
STALE_PLAN = "The approvals in this round were for another plan. Send it for approval again."  # 07-CONTEXT D-23
# a decision naming another plan than the one in force: the page is stale (07 review C; 07-CONTEXT D-23, D-32)
PLAN_CHANGED = "The plan changed since this page was loaded. Reload the stock decision."
NAMES_PLAN = ("approve", "accept")  # these must name the plan key the page shows; reject and decline may
DEST_ONLY = "Only the destination manager can accept or decline this transfer"
ROLE_OF = {"approve": "source", "reject": "source", "accept": "destination", "decline": "destination"}
TRIP_KINDS = ("transport_quote", "handling_cost")  # adopted for one trip: the run code and that run's day
BRANCH_KINDS = ("demand_multiplier",)  # adopted for one branch; markdown_response and disposal_cost for neither


def _engine(fn, *args):
    """An engine call whose InputError, AdjustmentError or HorizonError becomes the contract's plain 4xx."""
    try:
        return fn(*args)
    except ENGINE_ERRORS as exc:
        raise engine_error(exc) from None


def require_manager(actor: dict, branch: str | None, message: str) -> None:
    """403 unless the seat is that branch's manager."""
    if actor.get("role") != "manager" or branch is None or actor.get("branch") != branch:
        raise ApiError(403, "forbidden", message)


def require_source(actor: dict, case: dict) -> None:
    require_manager(actor, case["source_branch"], f"Only the {case['source_branch']} manager can do this")


def _wrong_state(case: dict) -> ApiError:
    return ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])


def _version(case: dict, rec_version) -> None:
    if not case["active_rec_version"] or rec_version != case["active_rec_version"]:
        raise ApiError(409, "stale_version", STALE, active_rec_version=case["active_rec_version"])


def _plan(conn, case: dict) -> Plan:
    """The plan in force (api/selection.py): the selection bound to the active version, else the recommended plan.
    Send holds it, each decision records its key, the commit and Prepare commit it (07-CONTEXT D-23)."""
    rec = cases.active_rec(conn, case)
    if rec is None:
        raise ApiError(409, "missing_data", "This case has no recommendation")
    key, _, _ = selection.plan_in_force(case, rec)
    return cases.parse_plan(key)


def _refuse_if_blocked(conn, run: dict, case: dict, plan: Plan) -> None:
    """The commitment rule on the plan in force (api/commitment.py): 409 commitment_blocked while a lot hold on an
    involved lot or a Required check remains (07-CONTEXT D-12, D-13)."""
    rec = cases.active_rec(conn, case)
    commitment.refuse_if_blocked(conn, run, case, rec, plan, commitment.checks_in_force(case, rec))


def _current_input(conn, run: dict, case: dict, plan: Plan) -> EngineInput:
    """The RAW input of today's data for this case (row 6). A gone case lot is no verifiable source for a transfer."""
    try:
        return cases.current_input(conn, run, case)
    except HorizonError as exc:
        raise ApiError(409, "missing_data", str(exc)) from None
    except LookupError:
        if plan.kind == TRANSFER:
            raise ApiError(409, "incomplete_shipment", NO_SOURCE) from None
        raise ApiError(409, "missing_data", f"Lot {case['lot_id']} is no longer on the record") from None


def _cost_ok(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _check_shipment(raw: EngineInput, case: dict, plan: Plan) -> None:
    """A transfer must have a verifiable source on the record and a real run today (Carried; round-2 IN-03)."""
    if plan.kind != TRANSFER:
        return
    source = case["source_branch"]
    lot = next((l for l in raw.lots if l.lot_id == case["lot_id"]), None)
    run = bookings.plan_run(raw, plan)
    ok = (raw.case_lot_id == case["lot_id"] and lot is not None and lot.branch == source
          and run is not None and run.origin == source and run.day == raw.today
          and isinstance(plan.qty, int) and not isinstance(plan.qty, bool) and plan.qty >= 1
          and _cost_ok(run.fixed_cost) and _cost_ok(run.per_unit_cost)
          and (run.cancel_cost is None or _cost_ok(run.cancel_cost)))
    if not ok:
        raise ApiError(409, "incomplete_shipment", NO_SOURCE)


def _resources(raw: EngineInput, plan: Plan) -> list[dict]:
    try:
        return bookings.plan_resources(effective(raw), plan)
    except ENGINE_ERRORS:
        return []


def _recheck(raw: EngineInput, plan: Plan) -> EngineInput:
    """check_plan on the raw input (other cases' resources only); its reasons verbatim as 409 conflict. Returns the
    effective input the holds are read from."""
    reasons = _engine(check_plan, raw, plan)
    if reasons:
        raise ApiError(409, "conflict", CONFLICT, reasons=list(reasons), resources=_resources(raw, plan))
    return _engine(effective, raw)


def _locked_input(conn, run: dict, case: dict, plan: Plan) -> EngineInput:
    """Verify the shipment, take the advisory locks in (kind, bucket) order, then read the raw input again inside
    them, so the recheck sees every competing booking committed before the locks were granted."""
    raw = _current_input(conn, run, case, plan)
    _check_shipment(raw, case, plan)  # before any lock, hold or log entry
    bookings.lock(conn, run["run_id"], bookings.buckets(_engine(effective, raw), plan))
    raw = _current_input(conn, run, case, plan)
    _check_shipment(raw, case, plan)
    return raw


def send(conn, run: dict, actor: dict, case_id: str, rec_version: str) -> dict:
    """Send for approval, and Recheck and resend after a hold expired: one transaction checks the version, locks the
    buckets, rechecks and holds every resource the action type needs in a new round, or none (spec §8)."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    if case["state"] not in ("OPEN", "WAITING"):
        raise _wrong_state(case)
    ownership.require_owner(actor, case)  # only the owner sends (06.1 D-54, D-67)
    _version(case, rec_version)
    plan = _plan(conn, case)
    raw = _locked_input(conn, run, case, plan)
    eff = _recheck(raw, plan)
    try:
        rows = bookings.holds_for(eff, plan)
    except bookings.Shortfall as exc:
        raise ApiError(409, "conflict", CONFLICT, reasons=[exc.reason], resources=_resources(raw, plan)) from None
    round_ = case["round"] + 1
    bookings.hold(conn, run["run_id"], case_id, round_, rows)
    db.run(conn, "UPDATE cases SET state = 'PENDING_APPROVAL', round = :n, hold_expired = false, reason = NULL "
                 "WHERE run_id = :r AND case_id = :c", n=round_, r=run["run_id"], c=case_id)  # no longer waiting
    db.log(conn, run["run_id"], run["day"], "sent", case_id, rec_version=rec_version, plan=plan.key, round=round_,
           held=sorted({k for k, *_ in rows}), actor=accounts.logged_actor(actor))
    return {"state": "PENDING_APPROVAL", "round": round_}


def _refuse_other_plan(plan: Plan, decision: str, plan_key) -> None:
    """409 stale_plan (with the plan in force's `plan_key`) when the decision names another plan than the plan in
    force: an approve or accept must name it (`plan_key`, the key the page shows; none named is refused too), a
    reject or decline only when it names one (07 review C; 07-CONTEXT D-23, D-32). A page loaded before a selection
    therefore never approves the newly selected plan."""
    if plan_key is None and decision not in NAMES_PLAN:
        return
    if plan_key != plan.key:
        raise ApiError(409, "stale_plan", PLAN_CHANGED, plan_key=plan.key)


def decide(conn, run: dict, actor: dict, case_id: str, rec_version: str, decision: str,
           reason: str | None = None, acknowledged=None, plan_key: str | None = None) -> dict:
    """Record one side's decision on the current round. A transfer needs the source's approval and the destination's
    acceptance, in either order; Keep and markdown need the source only. The last required decision commits.

    An approve or accept names the plan the page shows (`plan_key`), checked after the seat, version and reason
    checks: none named, or another key than the plan in force's, is 409 stale_plan and records nothing; a reject or
    decline is checked only when it names one (`_refuse_other_plan`; 07 review C, D-32). An approve or accept on a
    plan with Advisory facts then carries `acknowledged` {rec_version, plan_key, fact_ids} (api/commitment.py
    check_ack: 409 ack_required, missing or stale; 07-CONTEXT D-12, D-26). Every decision stores the plan in force's
    key, and the acknowledgement when one was needed, on its row and in its `decision` entry."""
    expired = bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    role = ROLE_OF.get(decision)
    if role is None:
        raise ApiError(400, "bad_request", "Pick approve, reject, accept or decline")
    if case_id in expired or (case["state"] == "OPEN" and case["hold_expired"]):
        raise ApiError(409, "hold_expired", EXPIRED)
    if case["state"] != "PENDING_APPROVAL":
        raise _wrong_state(case)
    _version(case, rec_version)
    plan = _plan(conn, case)
    if role == "source":
        require_source(actor, case)
    else:
        require_manager(actor, plan.dest if plan.kind == TRANSFER else None, DEST_ONLY)
    if decision in ("reject", "decline") and not (isinstance(reason, str) and reason.strip()):
        raise ApiError(400, "bad_request", "A reason is required")
    _refuse_other_plan(plan, decision, plan_key)  # the page's plan is the plan in force (07 review C; D-23, D-32)
    ack = None
    if decision in ("approve", "accept"):  # the Advisory facts of the plan in force are acknowledged (D-12, D-26)
        rec = cases.active_rec(conn, case)
        checks = commitment.checks_in_force(case, rec)
        ack = commitment.check_ack(acknowledged, commitment.expected_ack(rec, plan, checks), checks)
    n = db.run(conn, "INSERT INTO decisions (run_id, case_id, rec_version, round, role, decision, session_id, reason, "
                     "acknowledged, plan_key) VALUES (:r, :c, :v, :n, :role, :d, :s, :why, CAST(:ack AS jsonb), :key) "
                     "ON CONFLICT DO NOTHING",
               r=run["run_id"], c=case_id, v=rec_version, n=case["round"], role=role, d=decision,
               s=actor.get("session_id"), why=reason, ack=db.js(ack) if ack is not None else None, key=plan.key)
    if not n:
        raise ApiError(409, "already_decided", f"The {role} has already decided in this round")
    db.log(conn, run["run_id"], run["day"], "decision", case_id, role=role, decision=decision, reason=reason,
           round=case["round"], rec_version=rec_version, plan_key=plan.key, acknowledged=ack,
           actor=accounts.logged_actor(actor))
    if decision in ("reject", "decline"):
        bookings.release(conn, run["run_id"], case_id)
        why = f"{'Rejected' if decision == 'reject' else 'Declined'}: {reason.strip()}"
        db.run(conn, "UPDATE cases SET state = 'REJECTED', reason = :why WHERE run_id = :r AND case_id = :c",
               why=why, r=run["run_id"], c=case_id)
        closing.on_close(conn, run, case, "rejected" if decision == "reject" else "declined")  # D-24, D-28
        return {"state": "REJECTED"}
    needed = {"source", "destination"} if plan.kind == TRANSFER else {"source"}
    if needed - set(cases.decided_roles(conn, case)):
        return {"state": "PENDING_APPROVAL"}
    return _commit(conn, run, actor, case, plan)


def _refuse_stale_plan(conn, case: dict, plan: Plan) -> None:
    """409 stale_plan when a decision of the current round on the active version was given for another plan key than
    the plan in force's (07-CONTEXT D-23): an approval belongs to one exact plan. A decision with no key (stored before
    Phase 7) counts as the recommended plan's."""
    rec = cases.active_rec(conn, case)
    recommended = rec["result"]["recommended"] if rec else None
    keys = {d["plan_key"] or recommended for d in db.rows(
        conn, "SELECT plan_key FROM decisions WHERE run_id = :r AND case_id = :c AND rec_version = :v AND round = :n",
        r=case["run_id"], c=case["case_id"], v=case["active_rec_version"] or "", n=case["round"])}
    if keys - {plan.key}:
        raise ApiError(409, "stale_plan", STALE_PLAN)


def _commit(conn, run: dict, actor: dict, case: dict, plan: Plan) -> dict:
    """Every required decision is in: one transaction checks the round's holds, that every decision of the round was
    given for the plan in force (409 stale_plan), the commitment rule, locks again, rechecks on today's data without
    this case's own bookings, and turns the holds into bookings (spec §8 Commit). All or nothing."""
    run_id, case_id = run["run_id"], case["case_id"]
    if not bookings.held_ok(conn, run_id, case_id, case["round"]):
        raise ApiError(409, "hold_expired", EXPIRED)
    _refuse_stale_plan(conn, case, plan)  # an approval for another plan never commits this one (D-23)
    _refuse_if_blocked(conn, run, case, plan)  # a Required check or a lot hold: nothing is booked (D-12, D-13)
    raw = _locked_input(conn, run, case, plan)
    eff = _recheck(raw, plan)
    bookings.commit(conn, run_id, case_id, case["round"], plan, eff)
    state = "EXECUTED" if plan.kind == KEEP else "APPROVED"
    db.run(conn, "UPDATE cases SET state = :s, decided_ms = (extract(epoch FROM now() - (SELECT min(a.at) "
                 "FROM activity_log a WHERE a.run_id = :r AND a.case_id = :c AND a.type = 'sent' "
                 "AND (a.payload->>'round')::int = :n)) * 1000)::int WHERE run_id = :r AND case_id = :c",
           s=state, r=run_id, c=case_id, n=case["round"])
    db.log(conn, run_id, run["day"], "committed", case_id, plan=plan.key, state=state, round=case["round"],
           actor=accounts.logged_actor(actor))  # the account whose decision completed the round
    return {"state": state}


def prepare(conn, run: dict, actor: dict, case_id: str) -> dict:
    """Create the simulated work order for an APPROVED case (spec §8 Prepare): verify the shipment, recheck on today's
    raw input without this case's own bookings, write the TRANSFER and/or MARKDOWN world action, and mark it PREPARED.
    A recheck that fails is 409 conflict with the engine's reasons, and nothing is written."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    require_source(actor, case)
    if case["state"] != "APPROVED":
        raise _wrong_state(case)
    plan = _plan(conn, case)
    _refuse_if_blocked(conn, run, case, plan)  # before the recheck: nothing is written (D-12, D-13)
    raw = _locked_input(conn, run, case, plan)
    eff = _recheck(raw, plan)
    lot, n, sku = eff.case_lot(), case["round"], eff.product.sku
    if plan.kind == TRANSFER:  # the run as approved: adopted transport and handling quotes applied (CR-02)
        advance.add_transfer(conn, run["run_id"], "shelfshift", f"{case_id}#{n}:transfer", case_id, lot.lot_id, sku,
                             plan, bookings.plan_run(eff, plan), eff.policy,
                             bookings.moved_lot_id(case_id, n, plan.dest), case["source_branch"])
    if plan.pct:
        advance.add_markdown(conn, run["run_id"], "shelfshift", f"{case_id}#{n}:price", case_id, lot.lot_id, sku,
                             plan.pct, markdown_effective_day(eff))
    db.run(conn, "UPDATE cases SET state = 'PREPARED' WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=case_id)
    db.log(conn, run["run_id"], run["day"], "prepared", case_id, plan=plan.key, round=n,
           rec_version=case["active_rec_version"], actor=accounts.logged_actor(actor))
    return {"state": "PREPARED"}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def wait(conn, run: dict, actor: dict, case_id: str, until_day: int, reason: str) -> dict:
    """Wait until a later review day (source manager, case OPEN); the case reopens on the morning of that day, so the
    day must be one whose morning still runs: no later than the sandbox's last review day (G-05-13)."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    require_source(actor, case)
    if case["state"] != "OPEN":
        raise _wrong_state(case)
    if isinstance(until_day, bool) or not isinstance(until_day, int) or until_day <= run["day"] or not _text(reason):
        raise ApiError(400, "bad_request", "Pick a later review day and give a reason")
    ref = refdata.get(run["dataset_version"])
    last = ref.last_review_day  # no later morning reopens a waiting case (G-05-13)
    if until_day > last:
        raise ApiError(400, "bad_request",
                       f"Pick a review day no later than {cases.date_label(last, ref)}, the last day this demo reviews "
                       "cases")
    db.run(conn, "UPDATE cases SET state = 'WAITING', waiting_until_day = :d, reason = :why WHERE run_id = :r "
                 "AND case_id = :c", d=until_day, why=_text(reason), r=run["run_id"], c=case_id)
    db.log(conn, run["run_id"], run["day"], "waiting", case_id, until_day=until_day, reason=_text(reason),
           rec_version=case["active_rec_version"], actor=accounts.logged_actor(actor))
    return {"state": "WAITING"}


def reject(conn, run: dict, actor: dict, case_id: str, reason: str) -> dict:
    """Reject the recommendation (source manager, case OPEN or WAITING; a pending case is rejected through decide).
    A reason is required; versions, decisions and the activity history stay, and the case itself never reopens: the
    close records the lot's fingerprint and status (api/closing.py on_close; 07-CONTEXT D-24, D-28), so a lot still at
    risk stays on the board and a successor may open under the D-28 rule (07-12)."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    require_source(actor, case)
    if case["state"] not in ("OPEN", "WAITING"):
        raise _wrong_state(case)
    if not _text(reason):
        raise ApiError(400, "bad_request", "A reason is required")
    bookings.release(conn, run["run_id"], case_id)
    db.run(conn, "UPDATE cases SET state = 'REJECTED', reason = :why WHERE run_id = :r AND case_id = :c",
           why=f"Rejected: {_text(reason)}", r=run["run_id"], c=case_id)
    db.log(conn, run["run_id"], run["day"], "rejected", case_id, reason=_text(reason),
           rec_version=case["active_rec_version"], actor=accounts.logged_actor(actor))
    closing.on_close(conn, run, case, "rejected")  # D-24, D-28: closing a plan never hides the lot
    return {"state": "REJECTED"}


def _validated(overrides, allowed: set[str], inp: EngineInput) -> list[dict]:
    """Each override is an object of an allowed kind that the engine validates on `inp`; else 400 bad_override."""
    if not isinstance(overrides, list) or not overrides:
        raise ApiError(400, "bad_request", "Give at least one assumption")
    out = []
    for adj in overrides:
        if not isinstance(adj, dict):
            raise ApiError(400, "bad_request", "Each assumption must be an object with a kind")
        adj = inputs.plain(adj)
        if adj.get("kind") not in allowed:
            raise ApiError(400, "bad_override", f"{adj.get('kind')} is not allowed here")
        _engine(validate, adj, inp)
        out.append(adj)
    return out


def _scope(adj: dict, inp: EngineInput) -> tuple[dict | None, str | None]:
    """(trip, branch) an adopted assumption is kept for (row 9): a run's trip, a branch, or neither."""
    if adj["kind"] in TRIP_KINDS:
        run = next(r for r in inp.runs if r.run_code == adj["run_code"])  # validate checked it is a known run
        return {"run_code": run.run_code, "day": run.day}, None
    if adj["kind"] in BRANCH_KINDS:
        return None, adj["branch"]
    return None, None


def adopt(conn, run: dict, actor: dict, case_id: str, overrides, note: str,
          rec_version: str | None = None, plan_key: str | None = None) -> dict:
    """Use these assumptions (spec §7 Overrides): adoptable kinds only, with a source note; the case re-evaluates
    and a version change cancels a pending approval (cases.evaluate_case). Returns the new active version."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)
    require_source(actor, case)
    if rec_version is not None:
        _version(case, rec_version)
    if plan_key is not None:
        _refuse_other_plan(_plan(conn, case), "adopt", plan_key)
    if not _text(note):
        raise ApiError(400, "bad_request", "Say where these assumptions come from")
    if case["state"] not in ("OPEN", "WAITING", "PENDING_APPROVAL"):
        raise _wrong_state(case)
    rec = cases.active_rec(conn, case)
    if rec is None:
        raise ApiError(409, "missing_data", "This case has no recommendation")
    base = from_dict(rec["engine_input"])
    items = []
    for adj in _validated(overrides, ADOPTABLE, base):
        trip, branch = _scope(adj, base)
        items.append({"override": adj, "trip": trip, "branch": branch, "note": _text(note), "adopted_day": run["day"]})
    case = {**case, "adopted": list(case["adopted"]) + items}
    db.run(conn, "UPDATE cases SET adopted = CAST(:a AS jsonb) WHERE run_id = :r AND case_id = :c",
           a=db.js(case["adopted"]), r=run["run_id"], c=case_id)
    version = _engine(cases.evaluate_case, conn, run, case, _text(note))
    db.log(conn, run["run_id"], run["day"], "assumptions_adopted", case_id, overrides=[i["override"] for i in items],
           trips=[i["trip"] for i in items], note=_text(note), rec_version=version, actor=accounts.logged_actor(actor))
    return {"rec_version": version}


def whatif(conn, run: dict, case_id: str, overrides, rec_version: str | None = None) -> dict:
    """Re-run the engine on a stored version's raw input (the active one when none is given) plus the overrides
    (adoptable and what-if-only kinds); the what-if is stored with is_whatif true. Any seat may ask. It never changes
    the active version, the case's state, its bookings or its approval round."""
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run["run_id"], case_id)
    if rec_version:
        rec = db.one(conn, "SELECT * FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                     r=run["run_id"], c=case_id, v=rec_version)
        if rec is None:
            raise ApiError(400, "bad_request", f"No version {rec_version} for this case")
    else:
        rec = cases.active_rec(conn, case)
        if rec is None:
            raise ApiError(409, "missing_data", "This case has no recommendation")
    base = from_dict(rec["engine_input"])
    added = _validated(overrides, ADOPTABLE | WHAT_IF_ONLY, base)
    inp = replace(base, overrides=base.overrides + tuple(added))
    version, result, checks = _engine(evaluate.store, conn, run["run_id"], case_id, run["day"], inp, True,
                                      tuple(added))
    ref = refdata.get(run["dataset_version"])
    return {"rec_version": version, "result": cases.without_wait_text(result),
            "fact_checks": [cases.in_engine_order(c) for c in checks], "overrides": added,
            "summary": cases.summary(result, checks, ref, waste_price=inp.policy.waste_price)}
