"""The commitment rule (07-CONTEXT D-12, D-13; 04-API-CONTRACT "The commitment rule and the acknowledgement").

It reads stored checks and runs no search: the class of each fact check is already stored with the version
(`recommendations.fact_checks`), so the rule costs one indexed hold query plus, only for a Required check's reason, one
`check_plan` call under that fact's alternative (research Q3, F-3).

- **The plan in force** is the selection bound to the active version, else its recommended plan, and its checks are
  that plan's own fact checks (`checks_in_force`, read through api/selection.py `plan_in_force`; 07-CONTEXT D-23), so
  the Required class is evaluated against the plan that would be committed.
- **Required** (`classes`): a check under whose alternative the plan would break a hard limit (`pick_feasible_if_alt`
  false, 02-CONTEXT D-29). **Advisory**: a check that changes which plan wins and leaves the plan allowed
  (`changes_plan` and `pick_feasible_if_alt`), the badge's own rule.
- **What blocks** (`blocks`): every ACTIVE lot hold on a lot the case involves (the case lot, the lots moved from it,
  the lot it was moved from; api/scope.py `involved_lots`), then every Required check in the engine's order. Each block
  carries its kind, its id, its lots, its owner (the hold's branch, or the fact's branch), its reason (the hold's own,
  or `check_plan`'s reasons under the fact's alternative, word for word) and its resolution path.
- **The acknowledgement** (`expected_ack`, `check_ack`; D-12, D-26, research F-27): every approve and accept on a plan
  with Advisory facts carries {rec_version, plan_key, fact_ids} equal to the active version, the plan in force's key
  and its Advisory fact ids, sorted. None sent is 409 ack_required; one that no longer matches (another version, plan
  or fact set) is 409 ack_required with `stale` true, so changed facts need a fresh acknowledgement. With no Advisory
  fact nothing is needed.
- **What the case shows** (`summary`, `check_keys`): while a commit or Prepare is still ahead (OPEN, WAITING,
  PENDING_APPROVAL, APPROVED) the case lists the blocks, the Advisory facts, the acknowledgement to send and the next
  actions that are open (`request_check`, `ask_again`, `select_plan`, `close_proposal`, `cancel_approved`; and
  `reassess` on a closed plan whose lot is still at risk, 07-12). A blocked case always lists at least one before
  approval. A Required check reads "Blocked · stock/expiry verification required" after Can't check, or once no
  request is left, while no request still stands for it. "Plan as if it's true" (`adoptable`) is offered only for kinds the engine adopts, never for a
  stock or expiry fact (engine/adjust.py ADOPTABLE, FACT_ONLY). A PREPARED case is past Prepare: the rule claims no
  block on it (a hold there is informational, "Review before dispatch").
- **Where it bites** (`refuse_if_blocked`): the decision that commits and Prepare refuse with 409 commitment_blocked,
  nothing booked or written. Send is never refused for it (D-12), and nothing else blocks: an impact preview, an
  unconfirmed report, an unanswered or Can't check request never settle or add a block; only a check absent from the
  plan in force's checks (a confirmed fact removes it from the next version) or a released or resolved hold clears one.

Facts are tested one at a time, as the engine does (`_with_alt` applies one alternative): a plan that survives each
fact alone but fails when two hold together is not detected, and nothing here claims it is (D-12 known limitation).
"""
from __future__ import annotations

from dataclasses import replace

from engine import from_dict
from engine.adjust import ADOPTABLE, AdjustmentError, InputError, effective
from engine.factchecks import _with_alt
from engine.limits import check_plan, covered_reason
from engine.model import Plan
from engine.simulate import HorizonError

from . import config, lotholds, scope, selection
from .errors import ApiError, engine_error

BLOCKED = "This plan can't be committed yet; see what blocks it."
ACK_FIRST = "Acknowledge the unverified facts before approving"
ACK_AGAIN = "These facts changed · acknowledge again"
ACK_KEYS = ("rec_version", "plan_key", "fact_ids")
HOLD_RESOLUTION = "Release the hold or resolve its check"
COUNT_KINDS = ("PHANTOM_STOCK", "STALE_COUNT")  # settled by a confirmed count; ESTIMATED_EXPIRY by a printed date
ENGINE_ERRORS = (InputError, AdjustmentError, HorizonError)
BLOCKED_LABEL = "Blocked · stock/expiry verification required"
COMMIT_AHEAD = ("OPEN", "WAITING", "PENDING_APPROVAL", "APPROVED")  # a commit or Prepare is still ahead
CLOSABLE = ("OPEN", "WAITING", "PENDING_APPROVAL")  # the owner closes the proposal: reject, or the reject decision
CANCELLABLE = ("APPROVED", "PREPARED")  # Cancel approved plan (api/closing.py; 07-CONTEXT D-17, D-23)
ASKABLE = ("PHANTOM_STOCK", "STALE_COUNT", "ESTIMATED_EXPIRY")  # a count or a printed date can be requested
# the engine adjustment a confirmed answer to each fact kind becomes: none of them is ADOPTABLE (engine/adjust.py)
ADJUSTMENT_OF = {"PHANTOM_STOCK": "stock_count", "STALE_COUNT": "stock_count", "ESTIMATED_EXPIRY": "expiry"}


def checks_in_force(case: dict, rec: dict | None) -> list[dict]:
    """The fact checks of the plan in force, in the engine's order: a bound selection's own checks, else the active
    version's stored checks (api/selection.py `plan_in_force`)."""
    return selection.plan_in_force(case, rec)[1]


def classes(checks: list[dict]) -> tuple[list[dict], list[dict]]:
    """(required, advisory), each in the given (engine) order: Required = not pick_feasible_if_alt; Advisory =
    changes_plan and pick_feasible_if_alt. A check that is neither changes nothing about the plan."""
    required = [c for c in checks if not c["pick_feasible_if_alt"]]
    advisory = [c for c in checks if c["changes_plan"] and c["pick_feasible_if_alt"]]
    return required, advisory


def required_reasons(engine_input: dict, check: dict, plan: Plan) -> list[str]:
    """check_plan's reasons for `plan` if the fact were true, word for word, as engine/factchecks.py computes them:
    the stored raw input made effective, the fact's alternative applied (`_with_alt`), applied_overrides cleared on
    that internal copy. The covered-markdown reason (D-35) is no broken limit and is left out. An engine error becomes
    the contract's plain 4xx (as workflow._engine maps it)."""
    try:
        alt = _with_alt(effective(from_dict(engine_input)), check)
        reasons = check_plan(replace(alt, applied_overrides=()), plan)
        covered = covered_reason(alt, plan.pct) if plan.pct else None
    except ENGINE_ERRORS as exc:
        raise engine_error(exc) from None
    return [r for r in reasons if r != covered]


def resolution(kind: str, branch: str) -> str:
    """The path that settles a Required check: a confirmed count, or a confirmed printed date, from the fact's
    branch (the exits when that can't happen are the case's `next_actions`)."""
    what = "a count" if kind in COUNT_KINDS else "a printed date"
    return f"Confirm {what} from {branch}"


def holds_on(conn, run: dict, case: dict) -> list[dict]:
    """The ACTIVE lot holds on the lots the case involves (the case lot, the lots moved from it, the lot it was moved
    from), oldest first."""
    return lotholds.active_on(conn, run["run_id"], scope.involved_lots(conn, run, case))


def holds_on_many(conn, run: dict, rows: list[dict], lots: dict | None = None) -> dict[str, list[dict]]:
    """`holds_on` for many cases (the board's set-based cards, api/cases.py `cards`): {case_id: the ACTIVE lot holds on
    the lots it involves, oldest first}. One read of the run's ACTIVE holds; only when there is one are the involved
    lots read, for every case at once (scope.involved_many; `lots`: the case lots' rows already read). Each case's list
    is the run's holds in their order, kept when the hold's lot is one the case involves: `lotholds.active_on`'s rows
    and order."""
    out: dict[str, list[dict]] = {c["case_id"]: [] for c in rows}
    if not rows:
        return out
    active = lotholds.active_in_run(conn, run["run_id"])
    if not active:
        return out
    involved = scope.involved_many(conn, run, rows, lots)
    for c in rows:
        mine = set(involved[c["case_id"]])
        out[c["case_id"]] = [h for h in active if h["lot_id"] in mine]
    return out


def hold_blocks(conn, run: dict, case: dict, holds: list[dict] | None = None) -> list[dict]:
    """A block for every ACTIVE lot hold on a lot the case involves, oldest first."""
    return [{"kind": "lot_hold", "hold_id": h["hold_id"], "lot_ids": [h["lot_id"]], "owner_branch": h["branch"],
             "reason": h["reason"], "resolution": HOLD_RESOLUTION}
            for h in (holds if holds is not None else holds_on(conn, run, case))]


def reasons_of(rec: dict, plan: Plan, required: list[dict]) -> dict[str, list[str]]:
    """{fact_id: its Required reasons} (one check_plan call each, no search)."""
    return {c["fact_id"]: required_reasons(rec["engine_input"], c, plan) for c in required}


def required_ids(case: dict, rec: dict | None) -> list[str]:
    """The Required checks' fact ids while a commit or Prepare is still ahead (the board's badge and blocked flag:
    the stored class only, no check_plan call)."""
    if case["state"] not in COMMIT_AHEAD:
        return []
    required, _ = classes(checks_in_force(case, rec))
    return [c["fact_id"] for c in required]


def blocks(conn, run: dict, case: dict, rec: dict | None, plan: Plan | None, checks: list[dict],
           reasons: dict[str, list[str]] | None = None, holds: list[dict] | None = None) -> list[dict]:
    """What blocks committing `plan` now: the lot holds first, then the Required checks in the engine's order
    (04-API-CONTRACT block shape). `reasons` and `holds` may carry what was already read (`reasons_of`, `holds_on`)."""
    out = hold_blocks(conn, run, case, holds)
    required, _ = classes(checks)
    if not required or rec is None or plan is None:
        return out
    reasons = reasons if reasons is not None else reasons_of(rec, plan, required)
    return out + [{"kind": "required_check", "fact_id": c["fact_id"], "lot_ids": list(c["lot_ids"]),
                   "owner_branch": c["branch"], "reason": "; ".join(reasons[c["fact_id"]]),
                   "resolution": resolution(c["kind"], c["branch"])} for c in required]


def refuse_if_blocked(conn, run: dict, case: dict, rec: dict | None, plan: Plan | None,
                      checks: list[dict]) -> None:
    """409 commitment_blocked with the blocks while anything blocks committing `plan` (the decision that commits,
    and Prepare; never Send)."""
    found = blocks(conn, run, case, rec, plan, checks)
    if found:
        raise ApiError(409, "commitment_blocked", BLOCKED, blocks=found)


def advisory_view(checks: list[dict]) -> list[dict]:
    """The Advisory facts as the client lists them: [{fact_id, kind, lot_ids, branch, owner_branch}]."""
    _, advisory = classes(checks)
    return [{"fact_id": c["fact_id"], "kind": c["kind"], "lot_ids": list(c["lot_ids"]), "branch": c["branch"],
             "owner_branch": c["branch"]} for c in advisory]


def expected_ack(rec: dict | None, plan: Plan | None, checks: list[dict]) -> dict | None:
    """The exact acknowledgement an approve or accept must send: {rec_version, plan_key, fact_ids (sorted)} of the
    plan in force's Advisory facts; None when it has none."""
    _, advisory = classes(checks)
    if not advisory or rec is None or plan is None:
        return None
    return {"rec_version": rec["rec_version"], "plan_key": plan.key,
            "fact_ids": sorted(c["fact_id"] for c in advisory)}


def _same(given, expected: dict) -> bool:
    if not isinstance(given, dict):
        return False
    ids = given.get("fact_ids")
    return (given.get("rec_version") == expected["rec_version"] and given.get("plan_key") == expected["plan_key"]
            and isinstance(ids, list) and all(isinstance(i, str) for i in ids) and sorted(ids) == expected["fact_ids"])


def check_ack(given, expected: dict | None, checks: list[dict]) -> dict | None:
    """The acknowledgement to store: None when nothing is to be acknowledged (whatever was sent), else `expected` when
    `given` equals it exactly (the three keys; the fact ids as a set, in any order, no repeat). Otherwise 409 ack_required: "Acknowledge
    the unverified facts before approving" when none was sent, "These facts changed · acknowledge again" when one was
    sent that no longer matches, with `advisory`, `rec_version`, `plan_key` and `stale`."""
    if expected is None:
        return None
    if given is not None and _same(given, expected):
        return {k: expected[k] for k in ACK_KEYS}
    stale = given is not None
    raise ApiError(409, "ack_required", ACK_AGAIN if stale else ACK_FIRST, advisory=advisory_view(checks),
                   rec_version=expected["rec_version"], plan_key=expected["plan_key"], stale=stale)


def requests_left(rec: dict | None, requests: list[dict]) -> int:
    """Request groups the active version still allows (requests.max_per_version less the groups asked on it, a
    combined request counted once, superseded ones included); `requests` are the case's request views."""
    if rec is None:
        return 0
    used = {r["request_group"] for r in requests if r["rec_version"] == rec["rec_version"]}
    return max(0, config.MAX_REQUESTS_PER_VERSION - len(used))


def _latest(requests: list[dict], rec_version: str, fact_id: str) -> dict | None:
    """The newest request view for this fact on this version (views come oldest first), or None."""
    mine = [r for r in requests if r["rec_version"] == rec_version and r["fact_id"] == fact_id]
    return mine[-1] if mine else None


def _standing(r: dict) -> bool:
    """A request that still stands for its fact: ANSWERED, or OPEN before its deadline (as the badge reads it)."""
    return r["state"] == "ANSWERED" or (r["state"] == "OPEN" and not r.get("overdue"))


def class_of(check: dict) -> str | None:
    """"REQUIRED", "ADVISORY" or None, by the stored class rule (`classes`)."""
    if not check["pick_feasible_if_alt"]:
        return "REQUIRED"
    return "ADVISORY" if check["changes_plan"] else None


def blocked_label(check: dict, rec: dict | None, requests: list[dict], left: int) -> str | None:
    """"Blocked · stock/expiry verification required" on a Required check whose latest request on the version is
    Can't check, or with no request left, while no request still stands for it; else None."""
    if class_of(check) != "REQUIRED" or rec is None:
        return None
    latest = _latest(requests, rec["rec_version"], check["fact_id"])
    if latest is not None and _standing(latest):
        return None
    if (latest is not None and latest["state"] == "CANT_CHECK") or left == 0:
        return BLOCKED_LABEL
    return None


def check_keys(check: dict, rec: dict | None, requests: list[dict], left: int,
               reasons: dict[str, list[str]]) -> dict:
    """A fact check's Phase 7 keys for the plan in force: class, owner_branch (the fact's branch), requests_left,
    adoptable (its adjustment kind is ADOPTABLE: never a stock or expiry fact), blocked_label, and reasons (check_plan's
    reasons under the alternative, Required checks only)."""
    return {"class": class_of(check), "owner_branch": check["branch"], "requests_left": left,
            "adoptable": ADJUSTMENT_OF.get(check["kind"], check["kind"]) in ADOPTABLE,
            "blocked_label": blocked_label(check, rec, requests, left),
            "reasons": list(reasons.get(check["fact_id"], [])) if class_of(check) == "REQUIRED" else []}


def next_actions(case: dict, rec: dict | None, checks: list[dict], requests: list[dict], found: list[dict],
                 options: list[dict] | None = None, plan_key: str | None = None) -> list[str]:
    """The exits open to a blocked case, in this order, every one that applies: `request_check` (a Required stock or
    expiry check with no request on the version yet, and a request left), `ask_again` (a Required check whose latest
    request may be asked again), `select_plan` (a Required check blocks, and an engine option other than the plan in
    force `plan_key` is feasible today and depends on no unverified fact: api/selection.py `options`; D-12, D-23),
    `close_proposal` (OPEN, WAITING or PENDING_APPROVAL), `cancel_approved` (APPROVED or PREPARED, whether or not
    anything blocks: Cancel approved plan is the supported next step when the plan can't continue, D-17, D-23; the list
    states what exists, the seat check stays the route's). Otherwise none while nothing blocks."""
    tail = ["cancel_approved"] if case["state"] in CANCELLABLE else []
    if not found or rec is None:
        return tail
    required, _ = classes(checks)
    out = []
    live = case["state"] in CLOSABLE
    latest = {c["fact_id"]: _latest(requests, rec["rec_version"], c["fact_id"]) for c in required}
    if live and requests_left(rec, requests) > 0 and any(
            c["kind"] in ASKABLE and latest[c["fact_id"]] is None for c in required):
        out.append("request_check")
    if any(r is not None and r.get("askable_again") for r in latest.values()):
        out.append("ask_again")
    if live and required and any(o["key"] != plan_key and o["feasible"] and not o["required_fact_ids"]
                                 for o in options or ()):
        out.append("select_plan")
    if live:
        out.append("close_proposal")
    return out + tail


def summary(conn, run: dict, case: dict, rec: dict | None, plan: Plan | None, checks: list[dict],
            requests: list[dict], reasons: dict[str, list[str]] | None = None,
            holds: list[dict] | None = None, options: list[dict] | None = None) -> dict:
    """The case detail's `commitment`: {blocks (as the refusal would list them now), advisory, ack (the exact
    acknowledgement an approve or accept must send, None with no Advisory fact), next_actions}. Past Prepare (or on a
    closed case) the rule no longer applies: nothing blocks and nothing is to be acknowledged; a PREPARED case still
    lists `cancel_approved` (07-11) until its transfer is dispatched (api/closing.py `cancellable`; 07 review A), and a
    plan a person closed lists `reassess` while its lot still has an at-risk status (api/closing.py `label`: none once
    a successor carries the lot; 07-12, D-24)."""
    if case["state"] not in COMMIT_AHEAD:
        from . import closing  # it reads this module at import
        reassess = closing.is_closed_by_person(case) and closing.label(conn, run, case) is not None
        return {"blocks": [], "advisory": [], "ack": None,  # cancel only while nothing has left (07 review A)
                "next_actions": ["cancel_approved"] if closing.cancellable(conn, case) else
                ["reassess"] if reassess else []}
    found = blocks(conn, run, case, rec, plan, checks, reasons, holds)
    return {"blocks": found, "advisory": advisory_view(checks), "ack": expected_ack(rec, plan, checks),
            "next_actions": next_actions(case, rec, checks, requests, found, options,
                                         plan.key if plan is not None else None)}
