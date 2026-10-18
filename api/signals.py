"""The count path of "verify before you move": count requests, answers and confirmations (spec §7; 04-API-CONTRACT
"Count path"; 04-CONTEXT D-05, D-12, D-20).

Only the count path lands in Phase 4 (D-05). Field signals (create, patch, dismiss, the other kinds, quotes, demand
events), uploads and the signal amount bound are Phase 7's; the only signal this module writes is the STOCK_COUNT
answer to a request.

- **Ask** (`request_evidence`): the case's source-branch manager asks about a fact the engine flagged
  (`changes_plan`) for the plan in force (a selected plan's own checks; 07 review E) on the active version of a live
  case (OPEN, WAITING or PENDING_APPROVAL; any other state is 409
  wrong_state, since a count re-plans nothing there). No model is called and no cap is spent (D-20). The request goes to the
  branch that holds the lots and covers every lot in the fact's `lot_ids` (round-2 WR-03). Its real deadline is
  `due_at` = the database's now() + `requests.response_min` minutes (07-CONTEXT D-05), and the ask ends "Respond by
  {t}" in the display time zone (D-09). The legacy `due_day`/`due_min` (`requests.lead_min` before the engine's
  `decide_by`, else before the end of the decision day; research A12) stay filled and are never read for state. One
  live (OPEN or ANSWERED) request per (case, version, fact); at most `requests.max_per_version` requests per
  version, the limit the agent's own requests share in Phase 9. Asking again (`recount_of`, 07-CONTEXT D-07) names
  the fact's latest request on the active version once it is ANSWERED, Unanswered (OPEN past `due_at` with no
  complete answer, partial replies included), CANT_CHECK or HELD: a new request that counts toward that limit; the
  original becomes SUPERSEDED with its answer kept, read-only and never confirmable.
- **Can't check** (`cant_check`, D-07): the addressed branch's manager closes an OPEN or ANSWERED request as
  CANT_CHECK with `closed_at` and `closed_by`; the fact stays unverified, and the source manager may ask again.
- **Tasks** (`tasks_for`, `waiting`, 07-CONTEXT D-04): the seat's addressed requests with the counts the Tasks tiles
  show (to answer: AWAITING, UNANSWERED and the partials; to confirm: ANSWERED; lot holds: the ACTIVE holds the
  branch owns, 07-08; advisory tasks: the open display tasks, 07-14). The session's `tasks_waiting` is the same
  counts' sum, derived through the same status rule.
- **Answer** (`answer`): only the addressed branch's manager, with per-lot shelf and backroom counts, while the
  request is OPEN, ANSWERED, EXPIRED or SUPERSEDED (a late answer is kept; spec §7 rev 6.7, 07-CONTEXT D-08; a
  SUPERSEDED request's answer is stored for the record and never confirmable, D-07). A blank is null
  and never 0. A reply that leaves a count blank keeps the request OPEN (EXPIRED if it had expired) with a partial
  note and corrects nothing; a complete one moves it to ANSWERED. A second answer replaces the first (the earlier
  signal is DISMISSED, the new one has the next revision). Every answer states when the branch checked
  (`observed`, stored as `observed_at` apart from the submission time; no default, 07-CONTEXT D-22). An answer may
  carry one file (PNG, JPEG or PDF by content; 07-CONTEXT D-20, api/files.py): it spends only the cross-workspace
  upload cap (`caps.spend(conn, None, "upload")`, never a workspace allowance), is stored under a content name, and
  never counts as a count: a photo without every count keeps the request OPEN as "Photo received · count missing"
  (VER-04). The view shows the file's type and size, never where it is stored.
- **Confirm** (`confirm`): only the addressed branch's manager, on the answer's current revision of an OPEN or
  ANSWERED request (the answer an EXPIRED request held from before Next day is not confirmed: the manager sends
  today's count), with the scope tick and both counts for every lot, under the run lock FOR UPDATE (WR-01). A count
  is confirmed only on the sandbox day it was given (05-REVIEW CR-02): an answer from an earlier day is 409
  stale_version with `answer_day`, whether or not its request expired. Applicability comes first (D-22): the
  answer's lots are the request's, each still on the record at the addressed branch with the request's SKU (else 409
  count_mismatch naming the lot); SUPERSEDED and CANT_CHECK requests are 409 wrong_state. There is no active-version
  check and no case-state check (07-CONTEXT D-08, D-17: the fact is about the lot, and a late answer confirmed after
  its case was decided still applies; a committed case gets a review flag). The answer's check time is compared with
  the confirmed fact in force for each lot (api/facts.py, D-22): checked earlier is 409 older_evidence, the same time
  with another value 409 review_required, both kept as they were; the same time and value confirms and changes
  nothing. Otherwise it writes one reported fact holding one stock_count adjustment per changed lot, always with
  `counted_day` = the workspace's business day (contract-diff row 10; D-21), with the check time, branch, source and
  a label carrying the real confirmation time (D-09), sets each lot's record to its count through
  `facts.correct_record`, marks the request CONFIRMED and applies the fact by case state (`facts.apply`, D-17): every
  live case in its scope is re-evaluated (a version change cancels a pending approval), every committed case in it is
  flagged "New confirmed information · review required", never re-planned. It approves, sends and holds nothing, and
  never moves the business day or the run version.

- **Shared with field reports** (07-14): the newer-evidence step and the effects of a count (`count_newer`,
  `count_apply`) and of a printed date (`expiry_newer`, `expiry_apply`, `resolve_holds`) are one implementation that a
  STOCK_COUNT or EXPIRY report's confirmation (api/reports.py) calls too, so an unsolicited count or printed date is
  held to exactly the standard of a requested one (D-11, D-22).

- **Expiry checks** (07-CONTEXT D-11; research F-23): "Check printed expiry date" is asked on an ESTIMATED_EXPIRY
  fact through the same `request_evidence` (the part "expiry"); the answer gives a printed date per lot (a business
  date in the dataset calendar, inside the engine's bound) with the tick "I checked the specified lot"; the lot's
  branch manager confirms (`_confirm_expiry`), which writes an `expiry` reported fact and never rewrites
  lots.expiry_day. Business dates go through `cases.day_of` and `cases.iso_of` only.

- **Read-time state** (`request_view`, 07-CONTEXT D-06): every view reads the database's now() once and derives
  "Awaiting response · respond by {t}" or "Unanswered · Unverified" (and the partial forms with " · overdue") through
  `realtime.request_status`, with `server_now` and `minutes_left` so a client never trusts its own clock. Nothing
  stores "Unanswered" and no job sweeps requests; Next day expires none (legacy EXPIRED rows still read and answer).

Every step writes its activity entry in the caller's transaction (D-12): `evidence_requested` (the flag with its
decision regret and parts), `evidence_answered`, `count_confirmed` (the counts and the resulting versions), and the
`new_version` entries `cases.evaluate_case` writes after it. Every query is keyed by run_id, so two sandboxes never
see each other's requests, answers or facts (FLOW-02).
"""
from __future__ import annotations

import hashlib
import math
import uuid
from datetime import timedelta

from engine.adjust import UNITS, AdjustmentError, InputError, validate
from engine.simulate import HorizonError

from . import accounts, bookings, cases, config, db, facts, inputs, overlay, realtime, refdata, scope
from . import caps, files  # an answer's file and its upload spend (07-CONTEXT D-20)
from . import lotholds  # Mixed dates places a lot hold; a confirmed printed date resolves one (07-CONTEXT D-11, D-13)
from .errors import ApiError, engine_error

COUNT_KINDS = ("PHANTOM_STOCK", "STALE_COUNT")  # facts a shelf-and-backroom count settles (D-05): "Count stock"
EXPIRY_KINDS = ("ESTIMATED_EXPIRY",)  # facts a printed date settles: "Check printed expiry date" (07-CONTEXT D-11)
NOT_ASKABLE = "Only a stock count or a printed expiry date can be requested"
MIXED = "MIXED"  # "Mixed dates in this lot", chosen instead of a date: places a lot hold (D-11, D-13)
PARTS = ("shelf_units", "backroom_units")
# a late answer is kept (spec §7 rev 6.7; 07-CONTEXT D-08), a SUPERSEDED request's too: stored, never confirmable (D-07)
ANSWERABLE = ("OPEN", "ANSWERED", "EXPIRED", "SUPERSEDED")
STORED_ONLY = ("SUPERSEDED",)  # takes an answer for the record only; _sync_request never changes its state
ASK_AGAIN = ("ANSWERED", "CANT_CHECK", "HELD")  # stored states Ask again may name; an Unanswered OPEN one too (D-07)
# The answer an EXPIRED request held when Next day ended its due day is not confirmed: it was given on an earlier
# sandbox day and the lot record has moved a day since, so the addressed manager sends today's count (a late answer)
# and confirms that. The state alone does not carry the rule: a request due on a later day survives Next day as
# ANSWERED, so confirm() also compares the answer's day with today (05-REVIEW CR-02). Phase 7 D-05 replaces the
# day-based due and the Next-day expiry with a real 60-minute deadline.
CONFIRMABLE = ("OPEN", "ANSWERED")
EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it."
PARTIAL = "reply received · count missing"
PARTIAL_DATE = "reply received · date missing"  # an expiry part's text reply without every printed date (D-11)
SIMULATED = "simulated"  # provenance of what the template build preloaded (07-CONTEXT D-18; api/preload.py)
REQUEST_LABEL, RESPONSE_LABEL = "Simulated request", "Simulated response"  # its labels on the views (D-18, SIG-05)


def provenance(row: dict, label: str) -> dict:
    """{provenance, simulated_label} of a stored request or answer: "simulated" with `label` (REQUEST_LABEL or
    RESPONSE_LABEL) for what the template preloaded, else "person" and null (07-CONTEXT D-18, SIG-05)."""
    simulated = row.get("provenance") == SIMULATED
    return {"provenance": SIMULATED if simulated else "person", "simulated_label": label if simulated else None}


def part_of(kind: str) -> str:
    """The check a fact kind asks for: "count" (PHANTOM_STOCK, STALE_COUNT) or "expiry" (ESTIMATED_EXPIRY); any
    other kind is 400 bad_request (07-CONTEXT D-11)."""
    if kind in COUNT_KINDS:
        return "count"
    if kind in EXPIRY_KINDS:
        return "expiry"
    raise ApiError(400, "bad_request", NOT_ASKABLE)


def _part(r: dict) -> str:
    """A stored request's part; a row from before 07-08 (no part) is a count."""
    return r.get("part") or "count"


def due(result: dict, day: int) -> tuple[int, int]:
    """(due_day, due_min): the engine's decide_by as an absolute minute minus the lead; without a decide_by, the end
    of the decision day (23:59) minus the lead (04-API-CONTRACT Count path 5; research A12)."""
    by = (result or {}).get("decide_by")
    end = by["day"] * 1440 + by["latest_decision_min"] if by else day * 1440 + 1439
    at = end - config.REQUEST_LEAD_MIN
    return at // 1440, at % 1440


def product_label(ref: refdata.Ref, sku: str) -> str:
    """The product as its code, or "Name · Code" where a name is mapped (07-CONTEXT D-03; a public set's name is the
    code when it has none, never an invented one)."""
    name = ref.names.get(sku) or sku
    return sku if name == sku else f"{name} · {sku}"


def _ask_text(ref: refdata.Ref, sku: str, branch: str, lot_ids: list[str], respond_by: str,
              part: str = "count") -> str:
    """A plain English ask naming the product code, every lot and the branch, with the real respond-by time (no model
    call; D-03, D-05); an expiry part asks for the printed date (D-11)."""
    lots = f"lot{'s' if len(lot_ids) > 1 else ''} {', '.join(lot_ids)}"
    if part == "expiry":
        return (f"Please check the printed expiry date of {product_label(ref, sku)} {lots} at {branch}. "
                f"Respond by {respond_by}.")
    return (f"Please count {product_label(ref, sku)} {lots} at {branch}: the shelf and the backroom. "
            f"Respond by {respond_by}.")


def _signal_view(s: dict | None) -> dict | None:
    if s is None:
        return None
    return {"signal_id": s["signal_id"], "revision": s["revision"], "kind": s["kind"], "state": s["state"],
            "request_id": s["request_id"], "branch": s["branch"], "text": s["text"], "fields": s["fields"],
            "day": s["created_day"], "reporter_role": s["reporter_role"], "reporter_branch": s["reporter_branch"],
            "created_at": s["created_at"].isoformat(),
            "observed_at": realtime.iso(s.get("observed_at")),  # the stated check time (D-22); null before Phase 7
            "file": _file_view(s), **provenance(s, RESPONSE_LABEL)}


def _file_view(s: dict) -> dict | None:
    """The answer's file as a client sees it: its stored (sniffed) type and size, never where it is (D-20); read
    through GET /api/signals/{id}/file."""
    if not s.get("file_uri"):
        return None
    return {"type": s["file_type"], "size": s.get("file_size")}


def _signal(conn, run_id: str, signal_id: str | None) -> dict | None:
    if not signal_id:
        return None
    return db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s", r=run_id, s=signal_id)


def _inapplicable(conn, run: dict, q: dict, lot_ids: list[str]) -> tuple[str, str] | None:
    """confirm()'s applicability check (07-CONTEXT D-22; Verify path 7), as the workspace reads the record
    (api/overlay.py): (lot_id, message) of the first lot that fails, else None. The answer's lots must be the
    request's, and every lot still on the record at the addressed branch with the request's SKU. A case version change
    or the case's state never enters it: the fact is about the lot (D-17)."""
    asked = list(q["lot_ids"])
    odd = [l for l in lot_ids if l not in asked] + [l for l in asked if l not in lot_ids]
    if odd:
        return odd[0], "The answer's lots are not the request's lots"
    for lot_id in lot_ids:
        lot = overlay.lot(conn, run, "shelfshift", lot_id)
        if lot is None or lot["branch"] != q["to_branch"]:
            return lot_id, f"Lot {lot_id} is no longer on the record at {q['to_branch']}"
        if lot["sku"] != q["sku"]:
            return lot_id, f"Lot {lot_id} is no longer recorded as {q['sku']}"
    return None


def _values(run: dict, s: dict) -> tuple[str, dict]:
    """(adjustment kind, {lot_id: value}) a complete answer would confirm: a count's units (shelf + backroom) or a
    printed date's business day (07-CONTEXT D-11)."""
    f = s["fields"]
    if s["kind"] == "EXPIRY":
        return "expiry", {l: cases.day_of(run, f["dates"][l]) for l in f["lot_ids"]}
    return "stock_count", {l: float(f["counts"][l]["shelf_units"] + f["counts"][l]["backroom_units"])
                           for l in f["lot_ids"]}


def _newer_ok(conn, run: dict, s: dict) -> bool:
    """confirm()'s check-time rule as a view reads it (D-22): no lot of the complete answer was checked earlier than its
    confirmed fact in force, at the same time with another value, or at its time when that fact was confirmed on an
    earlier business day (07 review H)."""
    kind, values = _values(run, s)
    observed = s.get("observed_at") or s["created_at"]
    found = facts.compare(conn, run["run_id"], kind, {l: (observed, v) for l, v in values.items()}, run["day"])
    return not found["older"] and not found["conflict"]


def _complete(s: dict) -> bool:
    """The answer gives every value its part asks for: both counts for every lot, or a printed date for every lot with
    the tick (an expiry answer with a date always carries the tick: answer() refuses one without it)."""
    if s["fields"].get("needs_input"):
        return False
    return s["kind"] != "EXPIRY" or s["fields"].get("checked_lot") is True


def _status(r: dict, s: dict | None, now) -> dict:
    """The request's read-time status at `now` (realtime.request_status), from its current answer, if any."""
    answer = None if s is None else {"complete": not s["fields"].get("needs_input"), "photo": bool(s["file_uri"]),
                                     "note": r["partial_note"], "answered_at": s["created_at"]}
    return realtime.request_status(r["state"], r.get("due_at"), now, answer, _part(r))


def _askable(r: dict, now) -> bool:
    """Ask again may name this request (07-CONTEXT D-07): ANSWERED, CANT_CHECK or HELD, or Unanswered: OPEN with its
    deadline passed at `now` (no complete answer; a partial reply included). Before the deadline, never."""
    if r["state"] in ASK_AGAIN:
        return True
    return r["state"] == "OPEN" and r.get("due_at") is not None and r["due_at"] <= now


def request_view(conn, run: dict, r: dict, case_key: str, now=None) -> dict:
    """A count request in the contract's shape, with its answer signal at its current revision (null before one).
    `fact_id` is the engine's (a replaced row's stored replaced_fact_id); `case_source` is the case's source branch;
    `askable_again` (and `recountable`, kept equal to it) is true when the source manager may Ask again now: the
    request is askable (`_askable`: ANSWERED, Unanswered, CANT_CHECK or HELD; a SUPERSEDED one never is, so the
    request is the fact's latest), on the case's active version while the case is live and the version holds fewer
    than requests.max_per_version requests (07-CONTEXT D-07). The seat check is the route's.

    `confirmable` mirrors every check confirm() makes on the request's current answer (05-REVIEW WR-10): the request
    OPEN or ANSWERED, its answer READ with both counts for every lot, given today (CR-02), applicable
    (`_inapplicable`: the request's lots, each still on the record at the addressed branch with the request's SKU),
    and no lot checked earlier than its confirmed fact in force or at its time with another value (`_newer_ok`); for a
    printed date, each date inside the engine's bound on its lot's own input today (`_dates_ok`, the helper confirm
    uses; 07 review K).
    confirm() holds no active-version check and no case-state check (07-CONTEXT D-17, D-22: the fact is about the
    lot), and takes the answer's current revision, which is the one `answer` shows. `answerable`: an answer sent today
    could be confirmed today (the request takes answers and is applicable), whatever the case's state.

    The Phase 7 keys (04-API-CONTRACT Request): `asked_at`, `due_at` and `server_now` (ISO UTC), `minutes_left`
    (floored, 0 once due), `overdue`, `status_kind`, `status` and `late`, all derived from `now`, the database's now()
    read once per read (a list passes one `now` to every view; 07-CONTEXT D-06)."""
    run_id, today = run["run_id"], run["day"]
    now = now if now is not None else realtime.db_now(conn)
    case = db.one(conn, "SELECT source_branch, state, active_rec_version FROM cases WHERE run_id = :r "
                        "AND case_id = :c", r=run_id, c=r["case_id"])
    n = _groups(conn, run_id, r["case_id"], r["rec_version"])
    live = case["state"] in cases.LIVE_STATES
    askable = (_askable(r, now) and r["rec_version"] == case["active_rec_version"]
               and live and n < config.MAX_REQUESTS_PER_VERSION)
    s = _signal(conn, run_id, r["answer_signal_id"])
    lot_ids = list(r["lot_ids"])
    takes = r["state"] in ANSWERABLE and r["state"] not in STORED_ONLY
    answerable = takes and _inapplicable(conn, run, r, lot_ids) is None
    confirmable = (r["state"] in CONFIRMABLE and s is not None and s["state"] == "READ" and _complete(s)
                   and (s["kind"] == "EXPIRY" or s["created_day"] == today)  # a printed date has no record day
                   and _inapplicable(conn, run, r, s["fields"]["lot_ids"]) is None
                   and _newer_ok(conn, run, s)
                   and (s["kind"] != "EXPIRY" or _dates_ok(conn, run, r, s)))  # confirm's date bound (07 review K)
    status = _status(r, s, now)
    draft = db.one(conn, "SELECT original_text FROM evidence_drafts WHERE run_id = :r AND draft_id = :d",
                   r=run_id, d=r["draft_id"]) if r.get("draft_id") else None
    answer = _signal_view(s)
    if answer is not None:
        from agent import readings_job  # read-only public projection; it imports signals at module load
        row = readings_job.answer_latest(conn, run, s)
        answer["reading"] = None
        if row is not None:
            served = ("generating" if readings_job.state_of(row) == "GENERATING" else
                      "precomputed" if row["provenance"] == "PRECOMPUTED" else "reuse")
            answer["reading"] = readings_job.answer_response(conn, run, row, served)
    return {"request_id": r["request_id"], "case_id": r["case_id"], "case_key": case_key,
            "rec_version": r["rec_version"], "fact_id": r["replaced_fact_id"] or r["fact_id"],
            "fact_kind": r["fact_kind"],
            "lot_ids": r["lot_ids"], "to_branch": r["to_branch"], "ask_text": r["ask_text"], "due_day": r["due_day"],
            "due_min": r["due_min"], "state": r["state"], "partial_note": r["partial_note"],
            "answer_signal_id": r["answer_signal_id"], "replaced_by": r["replaced_by"],
            "case_source": case["source_branch"], "recountable": askable, "confirmable": confirmable,
            "answerable": answerable, "answer": answer,
            "asked_at": realtime.iso(r["created_at"]), "due_at": realtime.iso(r.get("due_at")),
            "server_now": realtime.iso(now), "minutes_left": realtime.minutes_left(r.get("due_at"), now),
            "overdue": status["overdue"], "status_kind": status["kind"], "status": status["sentence"],
            "late": status["late"], "askable_again": askable,
            "answered_at": realtime.iso(s["created_at"]) if s else None,
            "observed_at": realtime.iso(s.get("observed_at")) if s else None,
            "closed_at": realtime.iso(r.get("closed_at")), "closed_by": r.get("closed_by"),
            "part": _part(r), "request_group": r.get("request_group") or r["request_id"],
            "group_parts": _group_parts(conn, run, r, status, now),
            "draft_id": r.get("draft_id"), "message": r.get("message"),
            "sent_by": r.get("sent_by"), "sent_at": realtime.iso(r.get("sent_at")),
            "plan_key": r.get("plan_key"), "request_context": r.get("request_context"),
            "original_text": draft["original_text"] if draft else None, **provenance(r, REQUEST_LABEL)}


def _group_parts(conn, run: dict, r: dict, status: dict, now) -> list[dict]:
    """`group_parts`: [{request_id, part, fact_id, status_kind}] for every part of r's group (the count part first),
    each status read at the same `now`; a single request lists itself (07-CONTEXT D-11, D-22)."""
    own = {"request_id": r["request_id"], "part": _part(r), "fact_id": r["replaced_fact_id"] or r["fact_id"],
           "status_kind": status["kind"]}
    if not r.get("request_group"):
        return [own]
    rows = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND request_group = :g",
                   r=run["run_id"], g=r["request_group"])
    out = [own if p["request_id"] == r["request_id"] else
           {"request_id": p["request_id"], "part": _part(p), "fact_id": p["replaced_fact_id"] or p["fact_id"],
            "status_kind": _status(p, _signal(conn, run["run_id"], p["answer_signal_id"]), now)["kind"]}
           for p in rows]
    return sorted(out, key=lambda p: (p["part"] != "count", p["request_id"]))


def _groups(conn, run_id: str, case_id: str, rec_version: str) -> int:
    """The request groups on the case version, superseded ones included: what requests.max_per_version limits (a
    combined request counts once; 07-CONTEXT D-07, D-11)."""
    return db.one(conn, "SELECT count(DISTINCT coalesce(request_group, request_id)) AS n FROM evidence_requests "
                        "WHERE run_id = :r AND case_id = :c AND rec_version = :v", r=run_id, c=case_id,
                  v=rec_version)["n"]


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


def request_evidence(conn, run: dict, actor: dict, case_id: str, rec_version: str, fact_id: str | None,
                     recount_of: str | None = None, fact_ids: list[str] | None = None, *,
                     draft_id: str | None = None, message: str | None = None, plan_key: str | None = None) -> dict:
    """Ask the branch holding the lots for a count of a fact the engine flagged on the active version (D-20).

    Asking twice returns the existing request (the newest for the fact on this version). With `recount_of` it is
    Ask again (G-05-2 item 12; 07-CONTEXT D-07): the named request of this fact on this version must be askable
    (`_askable`: ANSWERED, Unanswered, CANT_CHECK or HELD; else 409 wrong_state "Request is <STATE>", SUPERSEDED
    included, so only the fact's latest request qualifies); a new request, and the named one becomes SUPERSEDED with
    `replaced_by` and its answer untouched. The case row lock serialises two asks or two ask-agains: the second sees
    the first's request, or the SUPERSEDED row. The replaced row's fact_id moves aside to
    `<fact_id>#<request_id>` (the engine's id kept in replaced_fact_id), so the UNIQUE (run, case, version, fact)
    still holds the one live request. A recount counts toward requests.max_per_version.

    The fact's kind names the part (07-CONTEXT D-11, `part_of`): "Count stock" on PHANTOM_STOCK and STALE_COUNT,
    "Check printed expiry date" on ESTIMATED_EXPIRY, through this one function (VER-02's one path); any other kind is
    400 bad_request. `fact_ids` with one fact is the same as `fact_id`; with two it is a combined request
    (`_request_pair`). Optional human wording is kept verbatim apart from server context. A draft must match
    this locked case/version/plan and supplies its own fact. No model is called, and no AI cap is spent (09 D-23)."""
    if fact_ids is not None:
        if not 1 <= len(fact_ids) <= 2:
            raise ApiError(400, "bad_request", "fact_ids lists one or two facts")
        fact_id = fact_ids[0]
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
    from . import selection  # selection imports cases/signals at startup
    current_key, _, _ = selection.plan_in_force(case, rec)
    if plan_key is not None and (not isinstance(plan_key, str) or not plan_key.strip() or "\x00" in plan_key):
        raise ApiError(400, "bad_request", "Provide a valid plan_key")
    if plan_key is not None and plan_key != current_key:
        raise ApiError(409, "stale_plan", "The plan changed. Review the current checks before sending.")
    draft = None
    if draft_id is not None:
        if (not isinstance(draft_id, str) or not draft_id.strip() or "\x00" in draft_id or plan_key is None
                or fact_id is not None or fact_ids is not None or recount_of is not None):
            raise ApiError(400, "bad_request", "Send the draft binding and final message without changing its context")
        draft = db.one(conn, "SELECT * FROM evidence_drafts WHERE run_id = :r AND draft_id = :d AND case_id = :c",
                       r=run["run_id"], d=draft_id, c=case_id)
        if draft is None:
            raise ApiError(404, "not_found", "No draft for this case")
        if draft["rec_version"] != rec_version:
            raise ApiError(409, "stale_version", "This draft belongs to an older recommendation. Review current checks.")
        if draft["plan_key"] != current_key:
            raise ApiError(409, "stale_plan", "The plan changed. Review the current checks before sending.")
        fact_id = draft["fact_id"]
    if message is not None or draft is not None:
        if (not isinstance(message, str) or not message.strip() or "\x00" in message
                or len(message) > config.SETTINGS["requests"]["max_message_chars"]):
            raise ApiError(400, "bad_request", "Give a nonempty request within the message limit")
    if fact_ids is not None and len(fact_ids) == 2:
        return _request_pair(conn, run, actor, case, rec, fact_ids, recount_of, message=message, plan_key=current_key)
    fact = _flagged(case, rec, fact_id)
    if draft is not None and (draft["owner"] != case["source_branch"] or draft["to_branch"] != fact["branch"]
            or draft["sku"] != case["sku"] or draft["lot_ids"] != fact["lot_ids"]
            or draft["request_context"] != _request_context(case, fact)):
        raise ApiError(400, "bad_request", "The draft context does not match the flagged fact")
    if draft is not None and draft["request_id"] is not None:
        # A manual recount may supersede this request; it never rebinds the sent draft or its human wording.
        return request_view(conn, run, _request(conn, run["run_id"], draft["request_id"]), case["key"])
    original = None
    if recount_of is None:
        existing = db.one(conn, "SELECT request_id FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                                "AND rec_version = :v AND fact_id = :f ORDER BY created_at DESC, request_id DESC "
                                "LIMIT 1", r=run["run_id"], c=case_id, v=rec_version, f=fact_id)
        if existing:
            return _request_result(conn, run, case, existing["request_id"], draft)
    else:
        original = db.one(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND request_id = :q FOR UPDATE",
                          r=run["run_id"], q=recount_of)
        if original is None:
            raise ApiError(404, "not_found", f"No request {recount_of}")
        named = original["replaced_fact_id"] or original["fact_id"]  # a SUPERSEDED row's engine id was moved aside
        if (original["case_id"], original["rec_version"], named) != (case_id, rec_version, fact_id):
            raise ApiError(400, "bad_request", "A recount must name this fact's request on this version")
        if not _askable(original, realtime.db_now(conn)):  # awaiting its answer, confirmed or superseded
            raise ApiError(409, "wrong_state", f"Request is {original['state']}", state=original["state"])
    part = part_of(fact["kind"])  # a count or a printed-date check (D-11); any other kind is 400
    _within_limit(conn, run, case_id, rec_version)
    rid = f"REQ-{uuid.uuid4().hex[:8]}"
    if original is not None:  # moved aside first, so the UNIQUE never sees two live rows for the fact
        db.run(conn, "UPDATE evidence_requests SET state = 'SUPERSEDED', replaced_by = :n, replaced_fact_id = fact_id, "
                     "fact_id = fact_id || '#' || request_id WHERE run_id = :r AND request_id = :q",
               n=rid, r=run["run_id"], q=original["request_id"])
    _insert_request(conn, run, actor, case, rec, fact, rid, recount_of, part,
                    message=message, plan_key=current_key, draft=draft)
    return _request_result(conn, run, case, rid, draft)


def _request_context(case: dict, fact: dict) -> dict:
    """Immutable server instructions, separate from human wording and the Send-time deadline (09 D-23)."""
    return {"branch": fact["branch"], "sku": case["sku"], "lot_ids": fact["lot_ids"], "fact_id": fact["fact_id"],
            "class": "Advisory" if fact["pick_feasible_if_alt"] else "Required",
            "required_parts": ["printed date per lot"] if part_of(fact["kind"]) == "expiry"
            else ["shelf count per lot", "backroom count per lot"]}


def _request_result(conn, run: dict, case: dict, request_id: str, draft: dict | None) -> dict:
    """Link the checked original atomically, including an existing manual request, without resending it."""
    if draft is not None:
        db.run(conn, "UPDATE evidence_drafts SET state = 'SENT', request_id = :q WHERE run_id = :r AND draft_id = :d",
               q=request_id, r=run["run_id"], d=draft["draft_id"])
    return request_view(conn, run, _request(conn, run["run_id"], request_id), case["key"])


PAIR = "Ask together one count fact and one expiry fact at one branch, the expiry lots within the count lots"


def _flagged(case: dict, rec: dict, fact_id: str) -> dict:
    """The fact check with this id that changes the plan, among the plan in force's checks on the active version (a
    selected plan's own checks, else the version's stored ones: api/selection.py `plan_in_force`, so a Required
    check the case shows can be requested; 07 review E, D-32); else 400 unknown_fact."""
    from . import selection  # it reads cases, which reads this module, at import
    _, checks, _ = selection.plan_in_force(case, rec)
    fact = next((f for f in checks if f["fact_id"] == fact_id and f["changes_plan"]), None)
    if fact is None:
        raise ApiError(400, "unknown_fact", f"{fact_id} is not a fact the engine flagged for this version")
    return fact


def _within_limit(conn, run: dict, case_id: str, rec_version: str) -> None:
    """409 too_many_requests once the version holds requests.max_per_version request groups (`_groups`)."""
    limit = config.MAX_REQUESTS_PER_VERSION
    if _groups(conn, run["run_id"], case_id, rec_version) >= limit:
        raise ApiError(409, "too_many_requests", f"At most {limit} count requests per case version")


def _request_pair(conn, run: dict, actor: dict, case: dict, rec: dict, fact_ids: list[str],
                  recount_of: str | None, *, message: str | None = None, plan_key: str | None = None) -> dict:
    """A combined request (07-CONTEXT D-11, D-22): one count fact and one expiry fact at one branch, the expiry lots
    within the count lots (any other pairing, or Ask again with two facts, is 400 bad_request), asked once. Two rows
    sharing `request_group` (the count part's request_id) and `due_at` (one now() in the transaction), each with its
    own part, state, answer and confirm; one slot of the per-version limit. Asking for the same pair again returns
    the count part; a fact already asked about on its own on this version is 400 bad_request. Returns the count part's
    view, whose `group_parts` names both."""
    if recount_of is not None:
        raise ApiError(400, "bad_request", "Ask again names one fact's request")
    found = [_flagged(case, rec, f) for f in fact_ids]
    by_part = {}
    for fact in found:
        by_part.setdefault(part_of(fact["kind"]), []).append(fact)
    if sorted(by_part) != ["count", "expiry"] or any(len(v) != 1 for v in by_part.values()):
        raise ApiError(400, "bad_request", PAIR)
    count, expiry = by_part["count"][0], by_part["expiry"][0]
    if count["branch"] != expiry["branch"] or not set(expiry["lot_ids"]) <= set(count["lot_ids"]):
        raise ApiError(400, "bad_request", PAIR)
    asked = db.rows(conn, "SELECT request_id, request_group FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                          "AND rec_version = :v AND fact_id = ANY(:f)", r=run["run_id"], c=case["case_id"],
                    v=rec["rec_version"], f=[count["fact_id"], expiry["fact_id"]])
    if asked:
        groups = {a["request_group"] for a in asked}
        if len(asked) == 2 and len(groups) == 1 and None not in groups:  # this pair, asked before
            (group,) = groups
            return request_view(conn, run, _request(conn, run["run_id"], group), case["key"])
        raise ApiError(400, "bad_request", "One of these facts was already asked about on this version")
    _within_limit(conn, run, case["case_id"], rec["rec_version"])
    gid = f"REQ-{uuid.uuid4().hex[:8]}"
    _insert_request(conn, run, actor, case, rec, count, gid, None, "count", gid, message=message, plan_key=plan_key)
    _insert_request(conn, run, actor, case, rec, expiry, f"REQ-{uuid.uuid4().hex[:8]}", None, "expiry", gid,
                    message=message, plan_key=plan_key)
    return request_view(conn, run, _request(conn, run["run_id"], gid), case["key"])


def _insert_request(conn, run: dict, actor: dict, case: dict, rec: dict, fact: dict, rid: str,
                    recount_of: str | None, part: str = "count", group: str | None = None, *,
                    message: str | None = None, plan_key: str | None = None, draft: dict | None = None) -> None:
    """The new OPEN request for the fact and its evidence_requested entry (an ask and a recount alike). Its deadline
    is the database's now() + requests.response_min minutes, stored UTC (D-05); the ask's "Respond by" is the same
    instant, since now() is fixed in the transaction. The legacy due_day/due_min stay filled, never read for state.
    `part` is the check it asks ("count" or "expiry", D-11); `group` the combined request it belongs to, else null."""
    ref = refdata.get(run["dataset_version"])
    lot_ids = list(fact["lot_ids"])
    due_day, due_min = due(rec["result"], run["day"])
    minutes = config.REQUEST_RESPONSE_MIN
    respond_by = realtime.label(realtime.db_now(conn) + timedelta(minutes=minutes))
    db.run(conn, "INSERT INTO evidence_requests (run_id, request_id, case_id, rec_version, fact_id, fact_kind, sku, "
                 "lot_id, lot_ids, to_branch, ask_text, due_day, due_min, due_at, state, regret, created_by, "
                 "created_day, part, request_group, draft_id, message, sent_by, sent_at, asked_at, plan_key, "
                 "request_context) VALUES (:r, :q, :c, :v, :f, :k, :s, :l, CAST(:ls AS jsonb), :b, "
                 ":t, :dd, :dm, now() + make_interval(mins => CAST(:rm AS int)), 'OPEN', :rg, :by, :d, :part, :grp, "
                 ":draft, :message, :sender, now(), now(), :plan, CAST(:context AS jsonb))",
           r=run["run_id"], q=rid, c=case["case_id"], v=rec["rec_version"], f=fact["fact_id"], k=fact["kind"],
           s=case["sku"], l=lot_ids[0], ls=db.js(lot_ids), b=fact["branch"],
           t=_ask_text(ref, case["sku"], fact["branch"], lot_ids, respond_by, part), dd=due_day, dm=due_min,
           rm=minutes, rg=fact["regret"], by=f"manager:{actor['branch']}", d=run["day"], part=part, grp=group,
           draft=draft["draft_id"] if draft else None, message=message, sender=actor.get("account"), plan=plan_key,
           context=db.js(draft["request_context"] if draft else _request_context(case, fact)))
    extra = {"recount_of": recount_of} if recount_of else {}
    if group:
        extra["request_group"] = group
    db.log(conn, run["run_id"], run["day"], "evidence_requested", case["case_id"], request_id=rid,
           rec_version=rec["rec_version"], fact_id=fact["fact_id"], fact_kind=fact["kind"], lot_ids=lot_ids,
           to_branch=fact["branch"], due_day=due_day, due_min=due_min, regret=fact["regret"],
           decision_regret=fact["decision_regret"], net_revenue_difference=fact["net_revenue_difference"],
           waste_policy_difference=fact["waste_policy_difference"], actor=accounts.logged_actor(actor), part=part,
           **extra)


def requests_for_case(conn, run: dict, case: dict) -> list[dict]:
    """Every count request for this case, oldest first, each with its answer, all read at one now()."""
    rows = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND case_id = :c "
                         "ORDER BY created_at, request_id", r=run["run_id"], c=case["case_id"])
    now = realtime.db_now(conn)
    return [request_view(conn, run, r, case["key"], now) for r in rows]


def inbox(conn, run: dict, actor: dict, now=None) -> list[dict]:
    """The requests addressed to the seat's branch, newest first, all read at one now(); HQ gets none."""
    if actor.get("role") != "manager":
        return []
    rows = db.rows(conn, "SELECT q.*, c.key AS case_key FROM evidence_requests q JOIN cases c USING (run_id, case_id) "
                         "WHERE q.run_id = :r AND q.to_branch = :b ORDER BY q.created_at DESC, q.request_id DESC",
                   r=run["run_id"], b=actor["branch"])
    now = now if now is not None else realtime.db_now(conn)
    return [request_view(conn, run, r, r["case_key"], now) for r in rows]


# What each Tasks tile counts, by the request's read-time status_kind (04-API-CONTRACT GET /api/tasks; D-04). A legacy
# OPEN row (no due_at) still waits for its answer; legacy EXPIRED rows are not counted.
TO_ANSWER = ("AWAITING", "UNANSWERED", "PHOTO_PARTIAL", "TEXT_PARTIAL", "OPEN")
TO_CONFIRM = ("ANSWERED",)


def _task_counts(kinds, lot_holds: int = 0, advisory: int = 0) -> dict:
    kinds = list(kinds)
    return {"to_answer": sum(k in TO_ANSWER for k in kinds), "to_confirm": sum(k in TO_CONFIRM for k in kinds),
            "lot_holds": lot_holds, "advisory": advisory}  # advisory: open display tasks (07-14, D-13)


def tasks_for(conn, run: dict, actor: dict) -> dict:
    """GET /api/tasks (07-CONTEXT D-04, D-13): the requests addressed to the seat's branch (newest first, every state),
    the ACTIVE lot holds it owns (oldest first; lotholds.owned_by), its advisory tasks (confirmed DISPLAY condition
    reports, open first, then the done ones with "Done by"; api/reports.py `advisory`, 07-14) and the tile counts
    (`advisory` counts the open ones), every view and `server_now` read at one now(). HQ gets empty lists and zero
    counts."""
    from . import reports  # it imports this module at import
    now = realtime.db_now(conn)
    requests = inbox(conn, run, actor, now)
    mine = actor.get("branch") if actor.get("role") == "manager" else None
    holds = [lotholds.view(conn, run, h) for h in lotholds.owned_by(conn, run["run_id"], mine)]
    tasks = reports.advisory(conn, run, mine)
    open_tasks = sum(t["task"]["done_at"] is None for t in tasks)
    return {"requests": requests, "holds": holds, "advisory": tasks,
            "counts": _task_counts((r["status_kind"] for r in requests), len(holds), open_tasks),
            "server_now": realtime.iso(now)}


def waiting(conn, run: dict, actor: dict) -> int:
    """The session's `tasks_waiting`: the sum of tasks_for's counts for the seat, read in one query (the session is
    read on every page) and derived through the same status rule (`_status`); 0 for HQ."""
    if actor.get("role") != "manager":
        return 0
    now = realtime.db_now(conn)
    rows = db.rows(conn, "SELECT q.state, q.due_at, q.partial_note, q.part, s.signal_id AS sid, s.fields, s.file_uri, "
                         "s.created_at FROM evidence_requests q LEFT JOIN signals s ON s.run_id = q.run_id "
                         "AND s.signal_id = q.answer_signal_id WHERE q.run_id = :r AND q.to_branch = :b "
                         "AND q.state IN ('OPEN', 'ANSWERED')", r=run["run_id"], b=actor["branch"])
    from . import reports  # it imports this module at import
    kinds = [_status(r, r if r["sid"] else None, now)["kind"] for r in rows]
    return sum(_task_counts(kinds, len(lotholds.owned_by(conn, run["run_id"], actor["branch"])),
                            reports.open_tasks(conn, run, actor["branch"])).values())


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


def lot_input(conn, run: dict, q: dict, lot_id: str):
    """The requested lot's own raw input today (`inputs.build_input` on that lot: the record, never physical stock):
    what its printed date is validated against, so a check outlives its case's own lot (07 review F, D-32). A lot no
    longer on the record is 409 count_mismatch naming it; an input that can't be built (missing data) is the engine
    error's plain 4xx."""
    try:
        return inputs.build_input(conn, run, lot_id)
    except (InputError, AdjustmentError, HorizonError) as exc:
        raise engine_error(exc) from None
    except LookupError:
        raise ApiError(409, "count_mismatch", f"Lot {lot_id} is no longer on the record at {q['to_branch']}",
                       lot_id=lot_id) from None


def check_dates(conn, run: dict, q: dict, days: dict[str, int]) -> None:
    """Every printed date {lot_id: business day} inside the engine's bound on its own lot's input today
    (`lot_input`, `check_printed`): the one check answer and confirm make, and the one `confirmable` reads (07 review
    F, K)."""
    for lot_id, day in days.items():
        check_printed(run, lot_input(conn, run, q, lot_id), q, lot_id, day)


def _dates_ok(conn, run: dict, q: dict, s: dict) -> bool:
    """confirm()'s printed-date bound as a view reads it (`check_dates`; 07 review K): an answer it would refuse is
    not confirmable."""
    try:
        check_dates(conn, run, q, _values(run, s)[1])
    except ApiError:
        return False
    return True


def check_printed(run: dict, inp, q: dict, lot_id: str, day: int) -> None:
    """A printed date is a business day inside the engine's own expiry bound (engine.adjust KINDS["expiry"],
    EXPIRY_DAY: yesterday to the input's last forecast day), checked by `engine.adjust.validate` on the lot's own
    input (`lot_input`): outside it is 400 out_of_bounds with field "dates.<lot_id>" and the bound as business dates; a
    lot the input no longer holds is 409 count_mismatch (07-CONTEXT D-11; T-07-27)."""
    try:
        validate({"kind": "expiry", "lot_id": lot_id, "expiry_day": day}, inp)
    except AdjustmentError:
        if lot_id not in {l.lot_id for l in inp.lots}:
            raise ApiError(409, "count_mismatch", f"Lot {lot_id} is no longer on the record at {q['to_branch']}",
                           lot_id=lot_id) from None
        low, high = inp.today - 1, max((f.day for f in inp.forecasts), default=inp.today)  # the message only
        bound = [cases.iso_of(run, low), cases.iso_of(run, high)]
        raise ApiError(400, "out_of_bounds", f"The printed date for lot {lot_id} must be between {bound[0]} and "
                       f"{bound[1]}", field=f"dates.{lot_id}", bound=bound) from None


def _clean_dates(conn, run: dict, q: dict, dates) -> dict:
    """{lot_id: "YYYY-MM-DD" | "MIXED" | None} for every requested lot, a blank as None (07-CONTEXT D-11). A lot outside
    the request is 409 count_mismatch; a value that is not a business date in the dataset calendar is 400 bad_request
    (cases.day_of); a date outside the engine's bound is 400 out_of_bounds (check_printed)."""
    lot_ids = list(q["lot_ids"])
    if dates is None:
        dates = {}
    if not isinstance(dates, dict):
        raise ApiError(400, "bad_request", "dates must map each lot to its printed date")
    for lot_id in dates:
        if lot_id not in lot_ids:
            raise ApiError(409, "count_mismatch", "The date is for a lot that is not in this request", lot_id=lot_id)
    out = {}
    for lot_id in lot_ids:
        value = dates.get(lot_id)
        if value is None or value == "" or value == MIXED:
            out[lot_id] = value or None
            continue
        check_dates(conn, run, q, {lot_id: cases.day_of(run, value)})  # on the lot's own input (07 review F)
        out[lot_id] = value
    return out


def _sync_request(conn, run: dict, request: dict, signal: dict) -> None:
    """Every lot with both counts (or a printed date) moves the request to ANSWERED (a late answer too); otherwise it
    keeps the partial note and stays EXPIRED if it had expired, else OPEN. A SUPERSEDED request keeps its state and
    note (D-07): only its answer_signal_id moves. The request points at its current answer either way."""
    complete = not signal["fields"]["needs_input"]
    note = PARTIAL_DATE if signal["kind"] == "EXPIRY" else PARTIAL
    db.run(conn, "UPDATE evidence_requests SET state = CASE WHEN state = 'SUPERSEDED' THEN 'SUPERSEDED' "
                 "WHEN CAST(:done AS boolean) THEN 'ANSWERED' WHEN state = 'EXPIRED' THEN 'EXPIRED' ELSE 'OPEN' END, "
                 "partial_note = CASE WHEN state = 'SUPERSEDED' THEN partial_note ELSE :n END, answer_signal_id = :s "
                 "WHERE run_id = :r AND request_id = :q AND state = ANY(:open)",
           done=complete, n=None if complete else note, s=signal["signal_id"],
           r=run["run_id"], q=request["request_id"], open=list(ANSWERABLE))


def answer(conn, run: dict, actor: dict, request_id: str, text: str | None, counts, observed,
           file: bytes | None = None, dates=None, checked_lot: bool | None = None, *, asset: str | None = None) -> dict:
    """The addressed branch answers with per-lot shelf and backroom counts, a reply and/or one file, stating when it
    checked (`observed`: "now" or an ISO-8601 time with its offset, no later than now; realtime.parse_observed,
    07-CONTEXT D-22). Changes no stock, plan, booking or price; a second answer replaces the first. The check time is
    parsed before anything is written, so a refused one stores nothing; it is stored apart from the submission time.
    A file is checked by content, spends the cross-workspace upload cap only (never a workspace allowance, D-20) and
    is stored under `<run_id>/<sha256>.<ext>` after every other check (validate, spend, store, insert: research Q4);
    alone it never completes the answer (VER-04).

    An expiry part (07-CONTEXT D-11) takes `dates` instead of counts: per lot a printed date "YYYY-MM-DD" (a business
    date in the dataset calendar, inside the engine's bound) or "MIXED", with `checked_lot` true ("I checked the
    specified lot"; a date or MIXED without it is 409 scope_tick naming the lot). It is stored as an EXPIRY signal
    whose fields hold `dates`, `checked_lot` and `needs_input` (the lots without a date); a photo or a reply without
    every date keeps the part OPEN ("Photo received · date missing", or the reply note). A count part refuses dates and
    an expiry part refuses counts (400).

    `asset` (never a client's input: no route passes it) is a packaged file's `asset://<name>` (api/files.py
    `asset_uri`) carried instead of an upload: the template's preloaded photo-only answer (07-16; 07-CONTEXT D-18,
    D-19). It is read where it is, checked by content, never stored again and spends nothing."""
    q = _request(conn, run["run_id"], request_id, lock=True)
    _addressed(actor, q["to_branch"], "answer")
    if q["state"] not in ANSWERABLE:
        raise ApiError(409, "wrong_state", f"Request is {q['state']}", state=q["state"])
    observed_at = realtime.parse_observed(observed, realtime.db_now(conn))  # never defaulted (D-22)
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "bad_request", "text must be a string")
    lot_ids = list(q["lot_ids"])
    reply = (text or "").strip()
    if _part(q) == "expiry":
        if counts:
            raise ApiError(400, "bad_request", "An expiry check takes printed dates, not counts")
        clean = _clean_dates(conn, run, q, dates)
        given = [l for l in lot_ids if clean[l] is not None]
        if given and checked_lot is not True:
            raise ApiError(409, "scope_tick", f"Tick the box to confirm you checked lot {given[0]}.", lot_id=given[0])
        if not reply and not file and not asset and not given:
            raise ApiError(400, "bad_request", "Enter a printed date or write a reply")
        kind = "EXPIRY"
        fields = {"lot_ids": lot_ids, "dates": clean, "checked_lot": bool(given) and checked_lot is True,
                  "needs_input": [{"lot_id": l, "field": "date"} for l in lot_ids if clean[l] is None]}
        logged = {"dates": clean}
    else:
        if dates:
            raise ApiError(400, "bad_request", "A count request takes counts, not dates")
        clean = _clean_counts(counts, lot_ids)
        if not reply and not file and not asset and not any(v is not None for c in clean.values() for v in c.values()):
            raise ApiError(400, "bad_request", "Enter a count or write a reply")
        kind = "STOCK_COUNT"
        fields = {"lot_ids": lot_ids, "counts": clean, "needs_input": _missing(clean, lot_ids)}
        logged = {"counts": clean}
    stored = None
    if file:
        files.sniff(file)  # 413 or 415 before anything is spent
        caps.spend(conn, None, "upload")  # the infrastructure cap only (D-20); rolled back with a failed answer
        stored = files.save(run["run_id"], file)
    revision = 1
    earlier = _signal(conn, run["run_id"], q["answer_signal_id"])
    if earlier is not None:  # the earlier answer is replaced, never edited
        revision = earlier["revision"] + 1
        db.run(conn, "UPDATE signals SET state = 'DISMISSED' WHERE run_id = :r AND signal_id = :s AND state = 'READ'",
               r=run["run_id"], s=earlier["signal_id"])
    sid = f"SIG-{uuid.uuid4().hex[:8]}"
    db.run(conn, "INSERT INTO signals (run_id, signal_id, revision, reporter_role, reporter_branch, branch, text, kind, "
                 "fields, state, request_id, provenance, created_day, observed_at) VALUES (:r, :s, :n, :role, :rb, "
                 ":b, :t, :k, CAST(:f AS jsonb), 'READ', :q, 'typed', :d, :o)", r=run["run_id"], s=sid, k=kind,
           n=revision, role=actor["role"], rb=actor["branch"], b=q["to_branch"], t=reply, f=db.js(fields),
           q=request_id, d=run["day"], o=observed_at)  # the reporter is the seat _addressed just checked (decision 4)
    if stored is None and asset:  # a packaged file, read where it is: nothing spent or stored (07-16, D-18)
        data = files.read(asset)
        stored = files.Stored(uri=asset, mime=files.sniff(data), sha256=hashlib.sha256(data).hexdigest(),
                              size=len(data))
    if stored is not None:  # an answer photo spent no workspace allowance (file_counted false; D-20)
        db.run(conn, "UPDATE signals SET file_uri = :u, file_type = :m, file_sha256 = :h, file_size = :z, "
                     "file_counted = false WHERE run_id = :r AND signal_id = :s", u=stored.uri, m=stored.mime,
               h=stored.sha256, z=stored.size, r=run["run_id"], s=sid)
    signal = _signal(conn, run["run_id"], sid)
    _sync_request(conn, run, q, signal)
    mixed = [l for l in lot_ids if kind == "EXPIRY" and clean[l] == MIXED]
    held = []
    if mixed and q["state"] not in STORED_ONLY:  # a superseded part's answer is for the record only
        held = _hold_mixed(conn, run, actor, q, sid, mixed)
    db.log(conn, run["run_id"], run["day"], "evidence_answered", q["case_id"], request_id=request_id, signal_id=sid,
           revision=revision, needs_input=fields["needs_input"], replaced=earlier["signal_id"]
           if earlier else None, observed_at=realtime.iso(observed_at), actor=accounts.logged_actor(actor),
           file=_file_view(signal), part=_part(q), holds=[h["hold_id"] for h in held], **logged)
    view = _signal_view(signal)
    return {**view, "holds": held} if kind == "EXPIRY" else view


def _hold_mixed(conn, run: dict, actor: dict, q: dict, signal_id: str, lot_ids: list[str]) -> list[dict]:
    """"Mixed dates in this lot" (07-CONTEXT D-11, D-13): one MIXED_DATES hold "Separate stock and verify before
    moving" per such lot, placed by the answering seat (the lot's branch manager) at now() through lotholds.place, and
    the part closed as HELD with closed_at and closed_by. The fact stays unverified; the part is askable again."""
    held = [lotholds.place(conn, run, actor, l, "MIXED_DATES", lotholds.MIXED_TEXT, signal_id=signal_id,
                           request_id=q["request_id"], case_id=q["case_id"]) for l in lot_ids]
    db.run(conn, "UPDATE evidence_requests SET state = 'HELD', closed_at = now(), closed_by = :by, partial_note = NULL "
                 "WHERE run_id = :r AND request_id = :q", by=f"manager:{actor['branch']}", r=run["run_id"],
           q=q["request_id"])
    return held


CANT_CHECK_FROM = ("OPEN", "ANSWERED")  # the parts Can't check closes (04-API-CONTRACT Verify path 10)


def cant_check(conn, run: dict, actor: dict, request_id: str) -> dict:
    """The addressed branch's manager closes the response attempt (07-CONTEXT D-07): every OPEN or ANSWERED part of
    the request's group becomes CANT_CHECK with closed_at (the database's now()) and closed_by "manager:<branch>",
    logged evidence_cant_check in the same transaction. The fact stays unverified (Can't check never satisfies a
    check), an answer already given is kept, and the source manager may ask again. Another seat is 403 (the seat
    comes from the session row, FLOW-10); any other state is 409 wrong_state. The group is the combined request's
    parts (`request_group`, 07-08), else the request alone; a part already CONFIRMED (or otherwise closed) keeps its
    state."""
    q = _request(conn, run["run_id"], request_id, lock=True)
    _addressed(actor, q["to_branch"], "close")
    if q["state"] not in CANT_CHECK_FROM:
        raise ApiError(409, "wrong_state", f"Request is {q['state']}", state=q["state"])
    group = [q["request_id"]]
    if q.get("request_group"):
        group = [p["request_id"] for p in db.rows(
            conn, "SELECT request_id FROM evidence_requests WHERE run_id = :r AND request_group = :g "
                  "AND state = ANY(:open) ORDER BY request_id FOR UPDATE", r=run["run_id"], g=q["request_group"],
            open=list(CANT_CHECK_FROM))]
    db.run(conn, "UPDATE evidence_requests SET state = 'CANT_CHECK', closed_at = now(), closed_by = :by "
                 "WHERE run_id = :r AND request_id = ANY(:g) AND state = ANY(:open)", by=f"manager:{actor['branch']}",
           r=run["run_id"], g=group, open=list(CANT_CHECK_FROM))
    db.log(conn, run["run_id"], run["day"], "evidence_cant_check", q["case_id"], request_id=request_id, parts=group,
           fact_id=q["replaced_fact_id"] or q["fact_id"], from_state=q["state"], actor=accounts.logged_actor(actor))
    return request_view(conn, run, _request(conn, run["run_id"], request_id), q["case_key"])


# --- the count and printed-date steps an answer and a field report share (07-14; D-11, D-22) -------------------------
# One implementation of the newer-evidence rule, the record correction and the expiry fact: a request's answer
# (`confirm`, `_confirm_expiry`) and a STOCK_COUNT or EXPIRY field report (api/reports.py) both call these, each
# around its own seat, state, revision, tick and applicability checks and its own activity entry.

def count_newer(conn, run: dict, lot_ids: list[str], units: dict[str, float], observed) -> list[str]:
    """The newer-evidence rule for a count (facts.check_newer, D-22): 409 older_evidence or review_required, else the
    lots whose check time and value equal the fact in force (they change nothing). An equal time from an earlier
    business day is older (07 review H). Then expires lapsed booking holds, as every confirmation does."""
    same = facts.check_newer(conn, run["run_id"], "stock_count", {l: (observed, units[l]) for l in lot_ids},
                             run["day"])
    bookings.expire_holds(conn, run)
    return same


def count_apply(conn, run: dict, changed: list[str], units: dict[str, float], *, sku: str, branch: str, observed,
                label: str, source: str, signal_id: str, note: str, reading_id=None) -> tuple[str, int, dict]:
    """The count's effects for the changed lots: one STOCK_COUNT reported fact (one stock_count adjustment per lot,
    `counted_day` the business day, D-21), each lot's record corrected (`facts.correct_record`, spec §8), the caller's
    activity seq reserved before what the fact causes, then `facts.apply` (D-17). Returns (fact_id, seq, applied)."""
    day = run["day"]
    adjustments = [{"kind": "stock_count", "lot_id": l, "units": units[l], "counted_day": day} for l in changed]
    fact_id = facts.write(conn, run, kind="STOCK_COUNT", adjustments=adjustments, sku=sku, branch=branch, zone=None,
                          valid_from=day, valid_to=day, label=label, confirmed_by=f"manager:{branch}",
                          observed_at=observed, source=source, signal_id=signal_id)
    _fact_reading(conn, run, fact_id, reading_id)
    for l in changed:  # the record comes into line from now on (spec §8 Corrected records), through one helper
        facts.correct_record(conn, run, l, units[l], day)
    # the count is logged before the new versions and flags it causes, so Activity reads flag, count, new version
    seq = db.one(conn, "SELECT nextval('activity_seq') AS n")["n"]
    fact_scope = scope.of_adjustments(adjustments, sku=sku, branch=branch, zone=None, valid_from=day, valid_to=day)
    return fact_id, seq, facts.apply(conn, run, fact_id, fact_scope, note=f"{note} ({fact_id})")


def expiry_newer(conn, run: dict, branch: str, days: dict[str, int], observed) -> list[str]:
    """A printed date's checks before anything is written: each date inside the engine's bound on its lot's own input
    today (`check_dates`; 400 out_of_bounds, T-07-27), then the newer-evidence rule for `expiry` (D-22), then lapsed
    booking holds expire. Returns the lots equal in check time and date to the fact in force."""
    check_dates(conn, run, {"to_branch": branch}, days)
    same = facts.check_newer(conn, run["run_id"], "expiry", {l: (observed, d) for l, d in days.items()}, run["day"])
    bookings.expire_holds(conn, run)
    return same


def expiry_apply(conn, run: dict, actor: dict, changed: list[str], days: dict[str, int], lot_ids: list[str], *,
                 sku: str, branch: str, observed, label: str, source: str, signal_id: str, note: str,
                 case_id: str | None, reading_id=None) -> tuple[str, int, dict, list]:
    """The printed date's effects: one EXPIRY reported fact (one `expiry` adjustment per changed lot) valid from the
    business day to the later of the printed and recorded dates; lots.expiry_day is read, never written (F-23). The
    caller's activity seq is reserved, the MIXED_DATES holds the date resolves are resolved (`resolve_holds`, every
    lot of the check), then `facts.apply`. Returns (fact_id, seq, applied, resolved hold ids)."""
    day = run["day"]
    adjustments = [{"kind": "expiry", "lot_id": l, "expiry_day": days[l]} for l in changed]
    recorded = [overlay.lot(conn, run, "shelfshift", l)["expiry_day"] for l in changed]  # read, never written
    valid_to = max([day, *(days[l] for l in changed), *recorded])
    fact_id = facts.write(conn, run, kind="EXPIRY", adjustments=adjustments, sku=sku, branch=branch, zone=None,
                          valid_from=day, valid_to=valid_to, label=label, confirmed_by=f"manager:{branch}",
                          observed_at=observed, source=source, signal_id=signal_id)
    _fact_reading(conn, run, fact_id, reading_id)
    seq = db.one(conn, "SELECT nextval('activity_seq') AS n")["n"]  # logged before the versions and flags it causes
    resolved = resolve_holds(conn, run, actor, lot_ids, observed, fact_id, case_id)
    fact_scope = scope.of_adjustments(adjustments, sku=sku, branch=branch, zone=None, valid_from=day,
                                      valid_to=valid_to)
    return fact_id, seq, facts.apply(conn, run, fact_id, fact_scope, note=f"{note} ({fact_id})"), resolved


def confirm(conn, run: dict, actor: dict, signal_id: str, revision, checked_both, *, statuses=("ACTIVE",),
            reading_id=None, value_source=None, checked_date=None) -> dict:
    """The addressed manager confirms a complete, applicable count that is not older than the evidence in force: one
    reported fact, the record corrected, the request CONFIRMED, every live case in scope re-evaluated and every
    committed one flagged (`facts.apply`). Approves, sends and holds nothing; returns {fact_id, labels, cases,
    flagged, unchanged}.

    It locks the run FOR UPDATE first (WR-01): every other sandbox write holds the run FOR SHARE, so a commit or
    prepare rechecking on the record either finishes before the count (the contract's order: confirm re-evaluates
    live cases only) or starts after it and sees the counted record, never in between. Two confirms for one lot
    serialise on the same lock, so the second is judged against the first one's fact (D-22)."""
    run = db.lock_run(conn, run["run_id"], mode="UPDATE", statuses=statuses)
    s = _signal(conn, run["run_id"], signal_id)
    if s is None or s["kind"] not in ("STOCK_COUNT", "EXPIRY") or not s["request_id"]:  # field reports: 07-13
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
    s, selected = _confirm_reading(conn, run, s, q, reading_id, value_source, checked_date)
    selected_id = selected["reading_id"] if selected else None
    if s["kind"] == "EXPIRY":
        return _confirm_expiry(conn, run, actor, s, q, reading_id=selected_id,
                               value_source="reading" if selected else "typed")
    if s["created_day"] != run["day"]:  # the record moved since this count was taken (CR-02; see CONFIRMABLE)
        raise ApiError(409, "stale_version", EARLIER_DAY, answer_day=s["created_day"])
    if checked_both is not True:
        raise ApiError(409, "scope_tick", "Tick 'I checked the shelf and the backroom for this lot'")
    lot_ids, counts = s["fields"]["lot_ids"], s["fields"]["counts"]
    missing = _missing(counts, lot_ids)
    if missing:
        raise ApiError(409, "needs_input", "Fill in every count before confirming", fields=missing)
    day = run["day"]
    bad = _inapplicable(conn, run, q, lot_ids)  # applicability before anything is written (D-22)
    if bad is not None:
        raise ApiError(409, "count_mismatch", bad[1], lot_id=bad[0])
    units = {l: float(counts[l]["shelf_units"] + counts[l]["backroom_units"]) for l in lot_ids}
    observed = s.get("observed_at") or s["created_at"]  # an answer from before Phase 7 states none
    same = set(count_newer(conn, run, lot_ids, units, observed))  # the step a STOCK_COUNT report shares (07-14)
    db.run(conn, "UPDATE signals SET state = 'CONFIRMED' WHERE run_id = :r AND signal_id = :s", r=run["run_id"],
           s=signal_id)
    db.run(conn, "UPDATE evidence_requests SET state = 'CONFIRMED', partial_note = NULL WHERE run_id = :r "
                 "AND request_id = :q", r=run["run_id"], q=q["request_id"])
    entry = {"request_id": q["request_id"], "signal_id": signal_id, "revision": s["revision"], "counts": counts,
             "units": units, "counted_day": day, "observed_at": realtime.iso(observed),
             "actor": accounts.logged_actor(actor), "value_source": "reading" if selected else "typed",
             "reading_id": selected_id}
    changed = [l for l in lot_ids if l not in same]
    if not changed:  # the same check time and value as the evidence in force: nothing changes (D-22)
        db.log(conn, run["run_id"], day, "count_confirmed", q["case_id"], fact_id=None, labels=[], cases=[],
               unchanged=True, **entry)
        return {"fact_id": None, "labels": [], "cases": [], "flagged": [], "unchanged": True}
    at = realtime.db_now(conn)  # the confirmation time the fact row stores (now(), fixed in the transaction)
    labels = [facts.label_count(q["to_branch"], at, counts[l], photo=bool(s["file_uri"])) for l in changed]
    if selected:
        labels = [label + " · count stated in the reply, checked by " + q["to_branch"] + " manager"
                  for label in labels]
    fact_id, seq, done = count_apply(conn, run, changed, units, sku=q["sku"], branch=q["to_branch"],
                                     observed=observed, label="; ".join(labels), source="reading" if selected else "request",
                                     signal_id=signal_id, note="count confirmed", reading_id=selected_id)
    db.run(conn, "INSERT INTO activity_log (run_id, seq, day, type, case_id, payload) VALUES (:r, :n, :d, "
                 "'count_confirmed', :c, CAST(:p AS jsonb))", r=run["run_id"], n=seq, d=day, c=q["case_id"],
           p=db.js({**entry, "fact_id": fact_id, "labels": labels, "cases": done["cases"],
                    "flagged": done["flagged"], "unchanged": False}))
    return {"fact_id": fact_id, "labels": labels, "unchanged": False, "flagged": done["flagged"],
            "cases": [{"case_id": c["case_id"], "key": c["key"], "rec_version": c["rec_version"]}
                      for c in done["cases"]]}


def _confirm_expiry(conn, run: dict, actor: dict, s: dict, q: dict, *, reading_id=None, value_source=None) -> dict:
    """confirm() for an expiry part (07-CONTEXT D-11; research F-23), after the shared seat, state and revision checks:
    the tick and a printed date for every lot (409 scope_tick, 409 needs_input), applicability (409 count_mismatch),
    each date inside the engine's bound on today's input (400 out_of_bounds, T-07-27), then the newer-evidence rule
    for the `expiry` adjustment (facts.check_newer: 409 older_evidence, review_required). It writes one EXPIRY reported
    fact with one `expiry` adjustment per changed lot, valid from the business day to the later of the printed and
    recorded dates (and never before the business day), labelled per lot "Printed date confirmed by {branch} manager ·
    {t} · {Thu 15 Oct}"; marks the answer and the part CONFIRMED and applies the fact by case state (facts.apply).
    lots.expiry_day is never rewritten: the printed date stays a reported fact the engine applies (F-23). A printed
    date is not tied to the sandbox day it was given (no earlier-day refusal: the label does not change overnight)."""
    run_id, day = run["run_id"], run["day"]
    lot_ids, dates = s["fields"]["lot_ids"], s["fields"]["dates"]
    missing = s["fields"]["needs_input"]
    if missing:
        raise ApiError(409, "needs_input", "Fill in every printed date before confirming", fields=missing)
    if s["fields"].get("checked_lot") is not True:
        raise ApiError(409, "scope_tick", f"Tick the box to confirm you checked lot {lot_ids[0]}.", lot_id=lot_ids[0])
    bad = _inapplicable(conn, run, q, lot_ids)
    if bad is not None:
        raise ApiError(409, "count_mismatch", bad[1], lot_id=bad[0])
    days = {l: cases.day_of(run, dates[l]) for l in lot_ids}
    observed = s.get("observed_at") or s["created_at"]
    same = set(expiry_newer(conn, run, q["to_branch"], days, observed))  # shared with an EXPIRY report (07-14)
    db.run(conn, "UPDATE signals SET state = 'CONFIRMED' WHERE run_id = :r AND signal_id = :s", r=run_id,
           s=s["signal_id"])
    db.run(conn, "UPDATE evidence_requests SET state = 'CONFIRMED', partial_note = NULL WHERE run_id = :r "
                 "AND request_id = :q", r=run_id, q=q["request_id"])
    entry = {"request_id": q["request_id"], "signal_id": s["signal_id"], "revision": s["revision"], "dates": dates,
             "expiry_days": days, "observed_at": realtime.iso(observed), "actor": accounts.logged_actor(actor),
             "value_source": value_source or "typed", "reading_id": reading_id}
    changed = [l for l in lot_ids if l not in same]
    if not changed:  # the same check time and date as the evidence in force: nothing changes (D-22)
        resolved = resolve_holds(conn, run, actor, lot_ids, observed, None, q["case_id"])
        db.log(conn, run_id, day, "expiry_confirmed", q["case_id"], fact_id=None, labels=[], cases=[],
               unchanged=True, holds_resolved=resolved, **entry)
        return {"fact_id": None, "labels": [], "cases": [], "flagged": [], "unchanged": True}
    at = realtime.db_now(conn)
    ref = refdata.get(run["dataset_version"])
    labels = [facts.label_expiry(q["to_branch"], at, cases.date_label(days[l], ref), photo=bool(s["file_uri"]))
              for l in changed]
    if reading_id:
        labels = [label + " · printed date read from the answer, checked by " + q["to_branch"] + " manager"
                  for label in labels]
    fact_id, seq, done, resolved = expiry_apply(conn, run, actor, changed, days, lot_ids, sku=q["sku"],
                                                branch=q["to_branch"], observed=observed, label="; ".join(labels),
                                                source="reading" if reading_id else "request", signal_id=s["signal_id"],
                                                note="printed date confirmed", case_id=q["case_id"],
                                                reading_id=reading_id)
    db.run(conn, "INSERT INTO activity_log (run_id, seq, day, type, case_id, payload) VALUES (:r, :n, :d, "
                 "'expiry_confirmed', :c, CAST(:p AS jsonb))", r=run_id, n=seq, d=day, c=q["case_id"],
           p=db.js({**entry, "fact_id": fact_id, "labels": labels, "cases": done["cases"],
                    "flagged": done["flagged"], "unchanged": False, "holds_resolved": resolved}))
    return {"fact_id": fact_id, "labels": labels, "unchanged": False, "flagged": done["flagged"],
            "cases": [{"case_id": c["case_id"], "key": c["key"], "rec_version": c["rec_version"]}
                      for c in done["cases"]]}


def resolve_holds(conn, run: dict, actor: dict, lot_ids: list[str], observed, fact_id: str | None,
                  case_id: str | None) -> list:
    """A confirmed printed date for each lot resolves its MIXED_DATES hold placed before the date's check time
    (lotholds.resolve_by_fact, D-11); the fact in force stands in when the date changed nothing. An answer and an
    EXPIRY report (07-14) share it."""
    out = []
    for l in lot_ids:
        held = fact_id or (facts.in_force(conn, run["run_id"], l, "expiry") or {}).get("fact_id")
        out += lotholds.resolve_by_fact(conn, run, actor, l, observed, held, case_id=case_id)
    return out


def _confirm_reading(conn, run, signal, request, reading_id, value_source, checked_date):
    """Choose one source under confirm's existing run lock; never edit the answer."""
    if value_source not in (None, "typed", "reading"):
        raise ApiError(400, "bad_request", "value_source must be typed or reading")
    latest = db.one(conn, "SELECT * FROM readings WHERE run_id=:r AND purpose='ANSWER' AND signal_id=:s "
        "AND signal_revision=:v AND status='DONE' ORDER BY created_at DESC, reading_id DESC LIMIT 1",
        r=run["run_id"], s=signal["signal_id"], v=signal["revision"])
    if value_source == "reading":
        if (not isinstance(reading_id, str) or latest is None or latest["reading_id"] != reading_id
                or latest["after_confirmation"] or latest["request_id"] != request["request_id"]
                or request["answer_signal_id"] != signal["signal_id"]):
            raise ApiError(409, "stale_version", "The reading changed since you looked at it")
    elif latest is None or latest["after_confirmation"]:
        return signal, None
    from agent.reading import answer_values
    suggested = answer_values(latest["suggestions"], signal["fields"]["lot_ids"])
    lot_ids = signal["fields"]["lot_ids"]
    if signal["kind"] == "STOCK_COUNT":
        counts = _clean_counts(suggested["pending_counts"], lot_ids)
        typed = signal["fields"]["counts"]
        if (value_source is None and not _missing(typed, lot_ids) and not _missing(counts, lot_ids)
                and typed != counts):
            raise ApiError(400, "bad_request", "Choose which count to confirm.")
        if value_source == "reading":
            return {**signal, "fields": {**signal["fields"], "counts": counts,
                    "needs_input": _missing(counts, lot_ids)}}, latest
    elif value_source == "reading":
        dates = _clean_dates(conn, run, request, suggested["printed_dates"])
        missing = [{"lot_id": lot, "field": "date"} for lot in lot_ids if dates[lot] is None]
        return {**signal, "fields": {**signal["fields"], "dates": dates,
                "needs_input": missing, "checked_lot": checked_date is True}}, latest
    return signal, None


def _fact_reading(conn, run, fact_id, reading_id):
    if reading_id is not None:
        db.run(conn, "UPDATE reported_facts SET reading_id=:id WHERE run_id=:r AND fact_id=:f",
               r=run["run_id"], f=fact_id, id=reading_id)
