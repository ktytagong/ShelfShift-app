"""The benchmark's automatic commit loop (08-CONTEXT D-26, D-29; research Q5, Q6).

This is the benchmark's automatic path only. The app's approval workflow and its protections stay exactly as they are;
`sandbox.autopilot` (the template warm-up, 04 D-14: stop at the first refusal) is unchanged.

`commit_all` commits ShelfShift's recommendation for every OPEN stock decision with an active recommendation, in
case_id order (recorded as `order`, D-29):

- **Transfer or markdown:** through the app's own service calls, as the warm-up makes them: the source manager takes
  ownership (`ownership.take_ownership`) and sends (`workflow.send`); a transfer's destination manager accepts; the
  source manager approves (both name the plan and send `commitment.expected_ack`); an APPROVED plan is prepared
  (`workflow.prepare`). Bookings, rechecks, holds and the commitment rule run exactly as the app enforces them.
- **Keep (D-26 fast path):** no approval round trip. The checks the app's round makes for Keep come first: the hold
  expiry and state check, the active version and the plan in force (409 stale_version, stale_plan), the commitment
  rule (`commitment.refuse_if_blocked`: a lot hold or a Required check is 409 commitment_blocked), and the recheck on
  today's raw input (`workflow._current_input`, then `workflow._recheck`: a gone lot or a missing forecast day is the
  app's 409 missing_data, an invalid input its 400). Keep books nothing and locks no bucket, so the app's second read
  inside the locks would read the same input; it is read once (D-26 "read each input once"). Then exactly what the
  app's round writes on the case row for Keep: round + 1, hold_expired false, reason NULL (send), state EXECUTED (the
  commit), and the commit's `committed` log entry. No ownership, decision row or sent/decision entry is written:
  nobody decided, so `decided_ms` (time to decision) stays unrecorded.

Every attempt runs inside its own savepoint: ownership, send, accept, approve, prepare and every hold or booking they
write. A refusal (`ApiError`) rolls the attempt back to the savepoint before anything about it is recorded, so a
refused attempt leaves no hold, booking, decision, work order or ownership behind, the case is OPEN as before, and the
earlier cases' commits in the same day stay.

D-29 on a refusal:

- `conflict`, `hold_expired`, `stale_version` (a capacity clash): after the refused attempt is rolled back, the stock
  decision is re-evaluated against the current reservations (`cases.evaluate_case`, the app's recheck) and its new
  recommendation is tried through the same calls in a new savepoint, never with altered values, at most
  `retry_limit` times per stock decision per day (api/evaluation.json; the benchmark runs one commit loop a day).
  A retry starts from the rolled-back state, so it never inherits its own failed hold or accept. After that the
  stock decision stays OPEN with the reason `conflict_after_retries`.
- `commitment_blocked`: never worked around. The reason is `commitment_blocked:` plus each block's kind in the app's
  block order (lot holds first, then Required checks in the engine's order), a Required check followed by its fact
  kind (`commitment_blocked:required_check:ESTIMATED_EXPIRY`); several distinct blocks are joined with `+`.
- Any other code: the reason is the code.

Every reason carries the planned transfer units of the plans it left open (D-34). Nothing is invented or bypassed and
no fact is written here. Uncommitted stock stays at its branch under the automatic markdown schedule.
"""
from __future__ import annotations

from engine.model import KEEP, TRANSFER

from . import accounts, bookings, cases, commitment, db, ownership, selection, workflow
from .errors import ApiError

SESSION = "benchmark"  # the session id on the benchmark seats' writes
RETRY_CODES = ("conflict", "hold_expired", "stale_version")  # a capacity clash: recheck and retry (D-29)


def seat(branch: str) -> dict:
    """The branch manager's seat the benchmark acts as."""
    return {"account": branch, "role": "manager", "branch": branch, "session_id": SESSION}


def reason_of(exc: ApiError, checks: list[dict]) -> str:
    """The recorded reason for a refusal that leaves the stock decision open (D-29, D-34)."""
    if exc.code != "commitment_blocked":
        return exc.code
    kinds = {c["fact_id"]: c["kind"] for c in checks}
    parts: list[str] = []
    for block in exc.extra.get("blocks") or []:
        part = "commitment_blocked:" + block["kind"]
        if block["kind"] == "required_check":
            part += ":" + kinds.get(block.get("fact_id"), "unknown")
        if part not in parts:
            parts.append(part)
    return "+".join(parts) or "commitment_blocked"


def _keep(conn, run: dict, case_id: str, rec: dict, plan, actor: dict) -> None:
    """D-26: Keep without the approval round trip, after the checks the app's round makes for Keep."""
    run_id = run["run_id"]
    bookings.expire_holds(conn, run, case_id)
    case = cases.case_row(conn, run_id, case_id, lock=True)
    if case["state"] != "OPEN":
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    if case["active_rec_version"] != rec["rec_version"]:
        raise ApiError(409, "stale_version", workflow.STALE, active_rec_version=case["active_rec_version"])
    key, checks, _ = selection.plan_in_force(case, rec)
    if key != plan.key:
        raise ApiError(409, "stale_plan", workflow.PLAN_CHANGED, plan_key=key)
    commitment.refuse_if_blocked(conn, run, case, rec, plan, checks)
    workflow._recheck(workflow._current_input(conn, run, case, plan), plan)  # Keep breaks no limit; input checked
    n = case["round"] + 1
    db.run(conn, "UPDATE cases SET state = 'EXECUTED', round = :n, hold_expired = false, reason = NULL "
                 "WHERE run_id = :r AND case_id = :c", n=n, r=run_id, c=case_id)
    db.log(conn, run_id, run["day"], "committed", case_id, plan=plan.key, state="EXECUTED", round=n,
           actor=accounts.logged_actor(actor))


def _through_app(conn, run: dict, case: dict, rec: dict, plan, checks: list[dict]) -> None:
    """A transfer or markdown through the app's own calls, exactly as `sandbox.autopilot` makes them."""
    cid, version = case["case_id"], rec["rec_version"]
    src = seat(case["source_branch"])
    ack = commitment.expected_ack(rec, plan, checks)
    ownership.take_ownership(conn, run, src, cid)
    workflow.send(conn, run, src, cid, version)
    if plan.kind == TRANSFER:
        workflow.decide(conn, run, seat(plan.dest), cid, version, "accept", None, ack, plan.key)
    if workflow.decide(conn, run, src, cid, version, "approve", None, ack, plan.key)["state"] == "APPROVED":
        workflow.prepare(conn, run, src, cid)


def _attempt(conn, run: dict, case: dict, rec: dict, plan, checks: list[dict], keep_fast_path: bool) -> None:
    """One attempt in its own savepoint: every write it made is rolled back when anything refuses or fails."""
    with conn.begin_nested():
        if plan.kind == KEEP and keep_fast_path:
            _keep(conn, run, case["case_id"], rec, plan, seat(case["source_branch"]))
        else:
            _through_app(conn, run, case, rec, plan, checks)


def commit_all(conn, run: dict, *, retry_limit: int, keep_fast_path: bool = True) -> dict:
    """Commit every OPEN stock decision's recommendation in case_id order, in the caller's (day's) transaction.

    Returns {committed: [case ids], left_open: {reason: [case ids]}, retries: {case_id: n}, retry_codes: {code: n}
    (each recheck by the refusal code that caused it), order: [case ids], transfer_units: {reason: planned transfer
    units}}. `keep_fast_path` is the evaluation setting of that name
    (benchmark.settings_for); False sends Keep through the approval round trip as well."""
    if isinstance(retry_limit, bool) or not isinstance(retry_limit, int) or retry_limit < 0:
        raise ValueError("retry_limit must be a whole number of at least 0")
    run_id = run["run_id"]
    out: dict = {"committed": [], "left_open": {}, "retries": {}, "retry_codes": {}, "order": [], "transfer_units": {}}
    for listed in db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r AND state = 'OPEN' "
                                "AND active_rec_version IS NOT NULL ORDER BY case_id", r=run_id):
        cid = listed["case_id"]
        case = cases.case_row(conn, run_id, cid)
        rec = cases.active_rec(conn, case)
        if case["state"] != "OPEN" or rec is None:
            continue  # no recommendation: nothing to commit (the warm-up skips it too)
        out["order"].append(cid)
        reason = _commit_one(conn, run, case, rec, retry_limit, keep_fast_path, out)
        if reason is None:
            out["committed"].append(cid)
    return out


def _commit_one(conn, run: dict, case: dict, rec: dict, retry_limit: int, keep_fast_path: bool, out: dict):
    """Commit one stock decision under D-29: None when committed, else the reason it was left open (recorded)."""
    run_id, cid, tried = run["run_id"], case["case_id"], 0
    while True:
        plan = cases.parse_plan(rec["result"]["recommended"])
        checks = commitment.checks_in_force(case, rec)
        try:
            _attempt(conn, run, case, rec, plan, checks, keep_fast_path)
            return None
        except ApiError as exc:
            if exc.code not in RETRY_CODES:
                reason = reason_of(exc, checks)
            elif tried >= retry_limit:
                reason = "conflict_after_retries"
            else:
                tried += 1
                out["retries"][cid] = tried
                out["retry_codes"][exc.code] = out["retry_codes"].get(exc.code, 0) + 1
                # the refused attempt is rolled back; the app's recheck runs on the current reservations
                cases.evaluate_case(conn, run, cases.case_row(conn, run_id, cid, lock=True))
                case = cases.case_row(conn, run_id, cid)
                rec = cases.active_rec(conn, case)
                if case["state"] == "OPEN" and rec is not None:
                    continue
                reason = "missing_data" if case["missing_data"] else "no_recommendation"
        out["left_open"].setdefault(reason, []).append(cid)
        units = plan.qty if plan.kind == TRANSFER else 0
        out["transfer_units"][reason] = out["transfer_units"].get(reason, 0) + int(units)
        return reason
