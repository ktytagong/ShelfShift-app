"""The count path of "verify before you move": count requests, answers and confirmations (spec §7; 04-API-CONTRACT
"Count path"; 04-CONTEXT D-05, D-12, D-20).

Only the count path lands in Phase 4 (D-05). Field signals (create, patch, dismiss, the other kinds, quotes, demand
events), uploads and the signal amount bound are Phase 7's; the only signal this module writes is the STOCK_COUNT
answer to a request.

- **Ask** (`request_evidence`): the case's source-branch manager asks about a fact the engine flagged
  (`changes_plan`) on the active version of a live case (OPEN, WAITING or PENDING_APPROVAL; any other state is 409
  wrong_state, since a count re-plans nothing there). No model is called and no cap is spent (D-20). The request goes to the
  branch that holds the lots and covers every lot in the fact's `lot_ids` (round-2 WR-03), due
  `requests.lead_min` before the engine's `decide_by`, else before the end of the decision day (research A12). One
  live (OPEN or ANSWERED) request per (case, version, fact); at most `requests.max_per_version` requests per
  version, the limit the agent's own requests share in Phase 9. A recount of an ANSWERED request is a new request
  that counts toward that limit; the original becomes SUPERSEDED with its answer kept (G-05-2 item 12), and
  answering or confirming it is refused.
- **Answer** (`answer`): only the addressed branch's manager, with per-lot shelf and backroom counts, while the
  request is OPEN, ANSWERED or EXPIRED (a late answer is kept; spec §7 rev 6.7, 07-CONTEXT D-08). A blank is null
  and never 0. A reply that leaves a count blank keeps the request OPEN (EXPIRED if it had expired) with a partial
  note and corrects nothing; a complete one moves it to ANSWERED. A second answer replaces the first (the earlier
  signal is DISMISSED, the new one has the next revision).
- **Confirm** (`confirm`): only the addressed branch's manager, on the answer's current revision of an OPEN or
  ANSWERED request (the answer an EXPIRED request held from before Next day is not confirmed: the manager sends
  today's count), with the scope tick and both counts for every lot, under the run lock FOR UPDATE (WR-01). A count
  is confirmed only on the sandbox day it was given (05-REVIEW CR-02): an answer from an earlier day is 409
  stale_version with `answer_day`, whether or not its request expired. A late answer (given after the due day) is confirmed only while its case is OPEN, WAITING or PENDING_APPROVAL; there is
  no active-version check (07-CONTEXT D-08: current means not superseded). It writes one reported fact holding one
  stock_count adjustment per lot, always with `counted_day` (contract-diff row 10), sets each lot's record to its
  count, marks the request CONFIRMED and re-evaluates every live case whose input holds those lots (a version change
  cancels a pending approval). It approves, sends and holds nothing.

Every step writes its activity entry in the caller's transaction (D-12): `evidence_requested` (the flag with its
decision regret and parts), `evidence_answered`, `count_confirmed` (the counts and the resulting versions), and the
`new_version` entries `cases.evaluate_case` writes after it. Every query is keyed by run_id, so two sandboxes never
see each other's requests, answers or facts (FLOW-02).
"""
from __future__ import annotations

import math
import uuid

from engine.adjust import UNITS, AdjustmentError, InputError
from engine.simulate import HorizonError
from engine.timing import fmt_min

from . import accounts, bookings, cases, config, db, overlay, refdata
from .errors import ApiError, engine_error

COUNT_KINDS = ("PHANTOM_STOCK", "STALE_COUNT")  # facts a shelf-and-backroom count settles (D-05)
PARTS = ("shelf_units", "backroom_units")
ANSWERABLE = ("OPEN", "ANSWERED", "EXPIRED")  # a late answer is kept (spec §7 rev 6.7; 07-CONTEXT D-08)
# The answer an EXPIRED request held when Next day ended its due day is not confirmed: it was given on an earlier
# sandbox day and the lot record has moved a day since, so the addressed manager sends today's count (a late answer)
# and confirms that. The state alone does not carry the rule: a request due on a later day survives Next day as
# ANSWERED, so confirm() also compares the answer's day with today (05-REVIEW CR-02). Phase 7 D-05 replaces the
# day-based due and the Next-day expiry with a real 60-minute deadline.
CONFIRMABLE = ("OPEN", "ANSWERED")
EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it."
PARTIAL = "reply received · count missing"
ENGINE_ERRORS = (InputError, AdjustmentError, HorizonError)


def due(result: dict, day: int) -> tuple[int, int]:
    """(due_day, due_min): the engine's decide_by as an absolute minute minus the lead; without a decide_by, the end
    of the decision day (23:59) minus the lead (04-API-CONTRACT Count path 5; research A12)."""
    by = (result or {}).get("decide_by")
    end = by["day"] * 1440 + by["latest_decision_min"] if by else day * 1440 + 1439
    at = end - config.REQUEST_LEAD_MIN
    return at // 1440, at % 1440


def _ask_text(ref: refdata.Ref, sku: str, branch: str, lot_ids: list[str], due_day: int, due_min: int) -> str:
    """A plain English ask naming every lot (no model call)."""
    lots = ", ".join(lot_ids)
    return (f"Please count {ref.names[sku]} lot{'s' if len(lot_ids) > 1 else ''} {lots} at {branch}: the shelf and "
            f"the backroom, by {cases.weekday(due_day, ref)} {fmt_min(due_min)}.")


def _signal_view(s: dict | None) -> dict | None:
    if s is None:
        return None
    return {"signal_id": s["signal_id"], "revision": s["revision"], "kind": s["kind"], "state": s["state"],
            "request_id": s["request_id"], "branch": s["branch"], "text": s["text"], "fields": s["fields"],
            "day": s["created_day"], "reporter_role": s["reporter_role"], "reporter_branch": s["reporter_branch"],
            "created_at": s["created_at"].isoformat()}


def _signal(conn, run_id: str, signal_id: str | None) -> dict | None:
    if not signal_id:
        return None
    return db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s", r=run_id, s=signal_id)


def _lots_on_record(conn, run: dict, lot_ids: list[str], branch: str) -> bool:
    """True when every lot is still on the record at `branch` (confirm's count_mismatch check), as the workspace
    reads the record (api/overlay.py)."""
    rows = overlay.lots(conn, run, "shelfshift", lot_ids, branch)
    return {x["lot_id"] for x in rows} == set(lot_ids)


def request_view(conn, run: dict, r: dict, case_key: str) -> dict:
    """A count request in the contract's shape, with its answer signal at its current revision (null before one).
    `fact_id` is the engine's (a replaced row's stored replaced_fact_id); `case_source` is the case's source branch;
    `recountable` is true only for an ANSWERED request on the case's active version while the case is live and the
    version holds fewer than requests.max_per_version requests (G-05-2 item 12). The seat check is the route's.

    `confirmable` mirrors every check confirm() makes on the request's current answer (05-REVIEW WR-10): the request
    OPEN or ANSWERED, its answer READ with both counts for every lot, given today (CR-02), a late answer (given after
    the due day) only on a live case, and every lot still on the record at the addressed branch. confirm() holds no
    active-version check and takes the answer's current revision, which is the one `answer` shows. `answerable`: an
    answer sent today could be confirmed today (the request takes answers, today is on or before its due day or the
    case is live, and every lot is still on the record); answer() still keeps a late answer on a finished case for the
    record."""
    run_id, today = run["run_id"], run["day"]
    case = db.one(conn, "SELECT source_branch, state, active_rec_version FROM cases WHERE run_id = :r "
                        "AND case_id = :c", r=run_id, c=r["case_id"])
    n = db.one(conn, "SELECT count(*) AS n FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                     "AND rec_version = :v", r=run_id, c=r["case_id"], v=r["rec_version"])["n"]
    live = case["state"] in cases.LIVE_STATES
    recountable = (r["state"] == "ANSWERED" and r["rec_version"] == case["active_rec_version"]
                   and live and n < config.MAX_REQUESTS_PER_VERSION)
    s = _signal(conn, run_id, r["answer_signal_id"])
    lot_ids = list(r["lot_ids"])
    takes = r["state"] in ANSWERABLE and (today <= r["due_day"] or live)
    answerable = takes and _lots_on_record(conn, run, lot_ids, r["to_branch"])
    confirmable = (r["state"] in CONFIRMABLE and s is not None and s["state"] == "READ"
                   and not _missing(s["fields"]["counts"], s["fields"]["lot_ids"])
                   and s["created_day"] == today and (s["created_day"] <= r["due_day"] or live)
                   and _lots_on_record(conn, run, s["fields"]["lot_ids"], r["to_branch"]))
    return {"request_id": r["request_id"], "case_id": r["case_id"], "case_key": case_key,
            "rec_version": r["rec_version"], "fact_id": r["replaced_fact_id"] or r["fact_id"],
            "fact_kind": r["fact_kind"],
            "lot_ids": r["lot_ids"], "to_branch": r["to_branch"], "ask_text": r["ask_text"], "due_day": r["due_day"],
            "due_min": r["due_min"], "state": r["state"], "partial_note": r["partial_note"],
            "answer_signal_id": r["answer_signal_id"], "replaced_by": r["replaced_by"],
            "case_source": case["source_branch"], "recountable": recountable, "confirmable": confirmable,
            "answerable": answerable, "answer": _signal_view(s)}


def _request(conn, run_id: str, request_id: str, lock: bool = False) -> dict:
    q = db.one(conn, "SELECT q.*, c.key AS case_key FROM evidence_requests q JOIN cases c USING (run_id, case_id) "
                     "WHERE q.run_id = :r AND q.request_id = :q" + (" FOR UPDATE OF q" if lock else ""),
               r=run_id, q=request_id)
    if q is None:
        raise ApiError(404, "not_found", f"No request {request_id}")
    return q


def _addressed(actor: dict, branch: str, what: str) -> None:
    if actor.get("role") != "manager" or actor.get("branch") != branch:
        raise ApiError(403, "forbidden", f"Only the {branch} manager can {what} this request")


def request_evidence(conn, run: dict, actor: dict, case_id: str, rec_version: str, fact_id: str,
                     recount_of: str | None = None) -> dict:
    """Ask the branch holding the lots for a count of a fact the engine flagged on the active version (D-20).

    Asking again returns the existing request (the newest for the fact on this version). With `recount_of`, an
    ANSWERED request of this fact on this version, it is a recount (G-05-2 item 12): a new request, and the named
    one becomes SUPERSEDED with `replaced_by` and its answer untouched. The replaced row's fact_id moves aside to
    `<fact_id>#<request_id>` (the engine's id kept in replaced_fact_id), so the UNIQUE (run, case, version, fact)
    still holds the one live request. A recount counts toward requests.max_per_version."""
    case = cases.case_row(conn, run["run_id"], case_id, lock=True)  # serialises the per-version limit
    if actor.get("role") != "manager" or actor.get("branch") != case["source_branch"]:
        raise ApiError(403, "forbidden", f"Only the {case['source_branch']} manager can do this")
    if case["state"] not in cases.LIVE_STATES:  # a count re-plans only a live case (G-05-2 item 11); state first,
        raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])  # as decide() orders it
    if not case["active_rec_version"] or rec_version != case["active_rec_version"]:
        raise ApiError(409, "stale_version",  # workflow.STALE's sentence (06.1 D-61); signals does not import workflow
                       "This recommendation was replaced by a newer version. Reload the stock decision.",
                       active_rec_version=case["active_rec_version"])
    rec = cases.active_rec(conn, case)
    fact = next((f for f in rec["fact_checks"] if f["fact_id"] == fact_id and f["changes_plan"]), None)
    if fact is None:
        raise ApiError(400, "unknown_fact", f"{fact_id} is not a fact the engine flagged for this version")
    original = None
    if recount_of is None:
        existing = db.one(conn, "SELECT request_id FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                                "AND rec_version = :v AND fact_id = :f ORDER BY created_at DESC, request_id DESC "
                                "LIMIT 1", r=run["run_id"], c=case_id, v=rec_version, f=fact_id)
        if existing:
            return request_view(conn, run, _request(conn, run["run_id"], existing["request_id"]),
                                case["key"])
    else:
        original = db.one(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND request_id = :q FOR UPDATE",
                          r=run["run_id"], q=recount_of)
        if original is None:
            raise ApiError(404, "not_found", f"No request {recount_of}")
        if (original["case_id"], original["rec_version"], original["fact_id"]) != (case_id, rec_version, fact_id):
            raise ApiError(400, "bad_request", "A recount must name this fact's request on this version")
        if original["state"] != "ANSWERED":
            raise ApiError(409, "wrong_state", f"Request is {original['state']}", state=original["state"])
    if fact["kind"] not in COUNT_KINDS:  # an expiry check needs a label answer, which is not on the count path (D-05)
        raise ApiError(400, "bad_request", "Only a stock count can be requested in this version")
    n = db.one(conn, "SELECT count(*) AS n FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                     "AND rec_version = :v", r=run["run_id"], c=case_id, v=rec_version)["n"]
    limit = config.MAX_REQUESTS_PER_VERSION
    if n >= limit:
        raise ApiError(409, "too_many_requests", f"At most {limit} count requests per case version")
    rid = f"REQ-{uuid.uuid4().hex[:8]}"
    if original is not None:  # moved aside first, so the UNIQUE never sees two live rows for the fact
        db.run(conn, "UPDATE evidence_requests SET state = 'SUPERSEDED', replaced_by = :n, replaced_fact_id = fact_id, "
                     "fact_id = fact_id || '#' || request_id WHERE run_id = :r AND request_id = :q",
               n=rid, r=run["run_id"], q=original["request_id"])
    _insert_request(conn, run, actor, case, rec, fact, rid, recount_of)
    return request_view(conn, run, _request(conn, run["run_id"], rid), case["key"])


def _insert_request(conn, run: dict, actor: dict, case: dict, rec: dict, fact: dict, rid: str,
                    recount_of: str | None) -> None:
    """The new OPEN request for the fact and its evidence_requested entry (an ask and a recount alike)."""
    ref = refdata.get(run["dataset_version"])
    lot_ids = list(fact["lot_ids"])
    due_day, due_min = due(rec["result"], run["day"])
    db.run(conn, "INSERT INTO evidence_requests (run_id, request_id, case_id, rec_version, fact_id, fact_kind, sku, "
                 "lot_id, lot_ids, to_branch, ask_text, due_day, due_min, state, regret, created_by, created_day) "
                 "VALUES (:r, :q, :c, :v, :f, :k, :s, :l, CAST(:ls AS jsonb), :b, :t, :dd, :dm, 'OPEN', :rg, :by, :d)",
           r=run["run_id"], q=rid, c=case["case_id"], v=rec["rec_version"], f=fact["fact_id"], k=fact["kind"],
           s=case["sku"], l=lot_ids[0], ls=db.js(lot_ids), b=fact["branch"],
           t=_ask_text(ref, case["sku"], fact["branch"], lot_ids, due_day, due_min), dd=due_day, dm=due_min,
           rg=fact["regret"], by=f"manager:{actor['branch']}", d=run["day"])
    extra = {"recount_of": recount_of} if recount_of else {}
    db.log(conn, run["run_id"], run["day"], "evidence_requested", case["case_id"], request_id=rid,
           rec_version=rec["rec_version"], fact_id=fact["fact_id"], fact_kind=fact["kind"], lot_ids=lot_ids,
           to_branch=fact["branch"], due_day=due_day, due_min=due_min, regret=fact["regret"],
           decision_regret=fact["decision_regret"], net_revenue_difference=fact["net_revenue_difference"],
           waste_policy_difference=fact["waste_policy_difference"], actor=accounts.logged_actor(actor), **extra)


def requests_for_case(conn, run: dict, case: dict) -> list[dict]:
    """Every count request for this case, oldest first, each with its answer."""
    rows = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                         "ORDER BY created_at, request_id", r=run["run_id"], c=case["case_id"])
    return [request_view(conn, run, r, case["key"]) for r in rows]


def inbox(conn, run: dict, actor: dict) -> list[dict]:
    """The requests addressed to the seat's branch, newest first; HQ gets none."""
    if actor.get("role") != "manager":
        return []
    rows = db.rows(conn, "SELECT q.*, c.key AS case_key FROM evidence_requests q JOIN cases c USING (run_id, case_id) "
                         "WHERE q.run_id = :r AND q.to_branch = :b ORDER BY q.created_at DESC, q.request_id DESC",
                   r=run["run_id"], b=actor["branch"])
    return [request_view(conn, run, r, r["case_key"]) for r in rows]


def _count(value):
    """A count part: None (blank) or a finite number of units, zero or more; else 400 bad_request."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ApiError(400, "bad_request", "A count is a number of units, zero or more, or left blank")
    return float(value)


def _clean_counts(counts, lot_ids: list[str]) -> dict:
    """{lot_id: {shelf_units, backroom_units}} for every requested lot, a blank as None (never 0). A lot outside the
    request is 409 count_mismatch; anything malformed is 400 bad_request."""
    if counts is None:
        counts = {}
    if not isinstance(counts, dict):
        raise ApiError(400, "bad_request", "counts must map each lot to its shelf and backroom units")
    for lot_id, parts in counts.items():
        if lot_id not in lot_ids:
            raise ApiError(409, "count_mismatch", "The count is for a lot that is not in this request", lot_id=lot_id)
        if parts is not None and (not isinstance(parts, dict) or set(parts) - set(PARTS)):
            raise ApiError(400, "bad_request", "Each lot's count has shelf_units and backroom_units")
    out = {}
    for lot_id in lot_ids:
        parts = counts.get(lot_id) or {}
        out[lot_id] = {p: _count(parts.get(p)) for p in PARTS}
        total = sum(v for v in out[lot_id].values() if v is not None)
        if total > UNITS[1]:
            raise ApiError(400, "bad_request", f"A lot's count is at most {UNITS[1]:,.0f} units")
    return out


def _missing(counts: dict, lot_ids: list[str]) -> list[dict]:
    return [{"lot_id": l, "field": p} for l in lot_ids for p in PARTS if counts[l][p] is None]


def _sync_request(conn, run: dict, request: dict, signal: dict) -> None:
    """Every lot with both counts moves the request to ANSWERED (a late answer too); otherwise it keeps the partial
    note and stays EXPIRED if it had expired, else OPEN. The request points at its current answer either way."""
    complete = not signal["fields"]["needs_input"]
    db.run(conn, "UPDATE evidence_requests SET state = CASE WHEN CAST(:done AS boolean) THEN 'ANSWERED' "
                 "WHEN state = 'EXPIRED' THEN 'EXPIRED' ELSE 'OPEN' END, partial_note = :n, answer_signal_id = :s "
                 "WHERE run_id = :r AND request_id = :q AND state = ANY(:open)",
           done=complete, n=None if complete else PARTIAL, s=signal["signal_id"],
           r=run["run_id"], q=request["request_id"], open=list(ANSWERABLE))


def answer(conn, run: dict, actor: dict, request_id: str, text: str | None, counts) -> dict:
    """The addressed branch answers with per-lot shelf and backroom counts and/or a reply. Changes no stock, plan,
    booking or price; a second answer replaces the first."""
    q = _request(conn, run["run_id"], request_id, lock=True)
    _addressed(actor, q["to_branch"], "answer")
    if q["state"] not in ANSWERABLE:
        raise ApiError(409, "wrong_state", f"Request is {q['state']}", state=q["state"])
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "bad_request", "text must be a string")
    lot_ids = list(q["lot_ids"])
    clean = _clean_counts(counts, lot_ids)
    reply = (text or "").strip()
    if not reply and not any(v is not None for c in clean.values() for v in c.values()):
        raise ApiError(400, "bad_request", "Enter a count or write a reply")
    revision = 1
    earlier = _signal(conn, run["run_id"], q["answer_signal_id"])
    if earlier is not None:  # the earlier answer is replaced, never edited
        revision = earlier["revision"] + 1
        db.run(conn, "UPDATE signals SET state = 'DISMISSED' WHERE run_id = :r AND signal_id = :s AND state = 'READ'",
               r=run["run_id"], s=earlier["signal_id"])
    sid = f"SIG-{uuid.uuid4().hex[:8]}"
    fields = {"lot_ids": lot_ids, "counts": clean, "needs_input": _missing(clean, lot_ids)}
    db.run(conn, "INSERT INTO signals (run_id, signal_id, revision, reporter_role, reporter_branch, branch, text, kind, "
                 "fields, state, request_id, provenance, created_day) VALUES (:r, :s, :n, :role, :rb, :b, :t, "
                 "'STOCK_COUNT', CAST(:f AS jsonb), 'READ', :q, 'typed', :d)", r=run["run_id"], s=sid, n=revision,
           role=actor["role"], rb=actor["branch"], b=q["to_branch"], t=reply, f=db.js(fields), q=request_id,
           d=run["day"])  # the reporter is the seat _addressed just checked, as recorded (user decision 4)
    signal = _signal(conn, run["run_id"], sid)
    _sync_request(conn, run, q, signal)
    db.log(conn, run["run_id"], run["day"], "evidence_answered", q["case_id"], request_id=request_id, signal_id=sid,
           revision=revision, counts=clean, needs_input=fields["needs_input"], replaced=earlier["signal_id"]
           if earlier else None, actor=accounts.logged_actor(actor))
    return _signal_view(signal)


def _label(branch: str, parts: dict) -> str:
    shelf, back = parts["shelf_units"], parts["backroom_units"]
    return f"Count confirmed by {branch} manager · shelf {shelf:g} + backroom {back:g} = {shelf + back:g}"


def _affected(conn, run: dict, lot_ids: set[str]) -> list[dict]:
    """Live cases whose active input holds any of these lots, or whose own lot is one of them, by case_id."""
    out = []
    for case in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = ANY(:s) ORDER BY case_id FOR UPDATE",
                        r=run["run_id"], s=list(cases.LIVE_STATES)):
        rec = cases.active_rec(conn, case)
        held = {l["lot_id"] for l in rec["engine_input"]["lots"]} if rec else set()
        if case["lot_id"] in lot_ids or held & lot_ids:
            out.append(case)
    return out


def confirm(conn, run: dict, actor: dict, signal_id: str, revision, checked_both) -> dict:
    """The addressed manager confirms a complete count: one reported fact, the record corrected, the request
    CONFIRMED, every live case holding those lots re-evaluated. Approves, sends and holds nothing.

    It locks the run FOR UPDATE first (WR-01): every other sandbox write holds the run FOR SHARE, so a commit or
    prepare rechecking on the record either finishes before the count (the contract's order: confirm re-evaluates
    live cases only) or starts after it and sees the counted record, never in between."""
    run = db.lock_run(conn, run["run_id"], mode="UPDATE")
    s = _signal(conn, run["run_id"], signal_id)
    if s is None or s["kind"] != "STOCK_COUNT" or not s["request_id"]:  # field signals are Phase 7's (D-05)
        raise ApiError(404, "not_found", f"No signal {signal_id}")
    q = _request(conn, run["run_id"], s["request_id"], lock=True)
    s = db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s FOR UPDATE", r=run["run_id"],
               s=signal_id)
    _addressed(actor, q["to_branch"], "confirm")
    if s["state"] != "READ":
        raise ApiError(409, "wrong_state", f"Answer is {s['state']}", state=s["state"])
    if q["state"] not in CONFIRMABLE:  # an EXPIRED request's earlier-day answer stays refused (see CONFIRMABLE)
        raise ApiError(409, "wrong_state", f"Request is {q['state']}", state=q["state"])
    if isinstance(revision, bool) or revision != s["revision"]:
        raise ApiError(409, "stale_version", "The answer changed since you looked at it", revision=s["revision"])
    if s["created_day"] != run["day"]:  # the record moved since this count was taken (CR-02; see CONFIRMABLE)
        raise ApiError(409, "stale_version", EARLIER_DAY, answer_day=s["created_day"])
    if s["created_day"] > q["due_day"]:  # a late answer is confirmed only on a live case (spec §7 rev 6.7)
        case = cases.case_row(conn, run["run_id"], q["case_id"])
        if case["state"] not in cases.LIVE_STATES:
            raise ApiError(409, "wrong_state", f"Case is {case['state']}", state=case["state"])
    if checked_both is not True:
        raise ApiError(409, "scope_tick", "Tick 'I checked the shelf and the backroom for this lot'")
    lot_ids, counts = s["fields"]["lot_ids"], s["fields"]["counts"]
    missing = _missing(counts, lot_ids)
    if missing:
        raise ApiError(409, "needs_input", "Fill in every count before confirming", fields=missing)
    day = run["day"]
    found = {}
    for lot_id in lot_ids:
        lot = found[lot_id] = overlay.lot(conn, run, "shelfshift", lot_id)
        if lot is None or lot["branch"] != q["to_branch"]:
            raise ApiError(409, "count_mismatch", f"Lot {lot_id} is no longer on the record at {q['to_branch']}",
                           lot_id=lot_id)
    bookings.expire_holds(conn, run)
    units = {l: float(counts[l]["shelf_units"] + counts[l]["backroom_units"]) for l in lot_ids}
    adjustments = [{"kind": "stock_count", "lot_id": l, "units": units[l], "counted_day": day} for l in lot_ids]
    labels = [_label(q["to_branch"], counts[l]) for l in lot_ids]
    fact_id = f"FACT-{uuid.uuid4().hex[:8]}"
    db.run(conn, "INSERT INTO reported_facts (run_id, fact_id, signal_id, kind, adjustments, sku, valid_from, valid_to, "
                 "label, confirmed_by, confirmed_day) VALUES (:r, :f, :s, 'STOCK_COUNT', CAST(:a AS jsonb), :sku, :d, "
                 ":d, :lab, :by, :d)", r=run["run_id"], f=fact_id, s=signal_id, a=db.js(adjustments), sku=q["sku"],
           d=day, lab="; ".join(labels), by=f"manager:{q['to_branch']}")
    for l in lot_ids:  # the record comes into line from now on (spec §8 Corrected records)
        overlay.materialize(conn, run, "shelfshift", found[l]["branch"], found[l]["sku"])  # the workspace's own row
        db.run(conn, "UPDATE lots SET qty = :q, last_verified_day = :d, verified_by = 'COUNT' WHERE run_id = :r "
                     "AND world = 'shelfshift' AND lot_id = :l", q=units[l], d=day, r=run["run_id"], l=l)
    db.run(conn, "UPDATE signals SET state = 'CONFIRMED' WHERE run_id = :r AND signal_id = :s", r=run["run_id"],
           s=signal_id)
    db.run(conn, "UPDATE evidence_requests SET state = 'CONFIRMED', partial_note = NULL WHERE run_id = :r "
                 "AND request_id = :q", r=run["run_id"], q=q["request_id"])
    # the count is logged before the new versions it causes, so Activity reads flag, count, new version (D-12)
    seq = db.one(conn, "SELECT nextval('activity_seq') AS n")["n"]
    out = []
    for case in _affected(conn, run, set(lot_ids)):
        try:
            version = cases.evaluate_case(conn, run, case, note=f"count confirmed ({fact_id})")
        except ENGINE_ERRORS as exc:
            raise engine_error(exc) from None
        rec = cases.active_rec(conn, cases.case_row(conn, run["run_id"], case["case_id"]))
        out.append({"case_id": case["case_id"], "key": case["key"], "rec_version": version,
                    "from_version": case["active_rec_version"],
                    "recommended": rec["result"]["recommended"] if rec else None})
    db.run(conn, "INSERT INTO activity_log (run_id, seq, day, type, case_id, payload) VALUES (:r, :n, :d, "
                 "'count_confirmed', :c, CAST(:p AS jsonb))", r=run["run_id"], n=seq, d=day, c=q["case_id"],
           p=db.js({"request_id": q["request_id"], "signal_id": signal_id, "revision": s["revision"],
                    "fact_id": fact_id, "counts": counts, "units": units, "counted_day": day, "labels": labels,
                    "cases": out, "actor": accounts.logged_actor(actor)}))
    return {"fact_id": fact_id, "labels": labels,
            "cases": [{"case_id": c["case_id"], "key": c["key"], "rec_version": c["rec_version"]} for c in out]}
