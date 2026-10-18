"""Short committed admission, bounded ADK production, then checked publication.

No transaction/lock crosses the model boundary. A disconnected consumer cancels
the producer and retains its terminal attempt and tool audit, never partial text.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import threading
import time
import uuid

import anyio
from google.genai import types

from api import accounts, caps, cases, commitment, config as api_config, db, refdata, selection, signals
from api.errors import ApiError
from . import config, model, numbers, prompts, tools
from . import usage
from .usage import _start_attempt, _finish_attempt

FALLBACK = "Fallback · Gemini unavailable · figures from the engine"
GENERATING = "Template summary · AI brief being written"
DAILY = "Today's AI limit is reached. It resets at 00:00 Bangkok time."
BRIEF_MAX_WORDS = 100
PING_SECONDS = 10


def ai_on():
    return api_config.env("GCP", "off") == "on"


def authorize_case(conn, run, case_id, session=None, *, lock=False):
    """Authorize the server identity before any assistant data/admission lookup.

    Internal release/test callers without a session are trusted HQ callers. HTTP
    routes always pass their authenticated session, and cannot supply an actor.
    """
    case = cases.case_row(conn, run["run_id"], case_id, lock=lock)
    rec = cases.active_rec(conn, case)
    if session is not None:
        fresh = db.one(conn, "SELECT * FROM sessions WHERE session_id = :s", s=session["session_id"])
        if fresh is None or not fresh.get("account") or fresh["run_id"] != run["run_id"]:
            raise ApiError(403, "forbidden", "This session no longer has access.")
        actor = accounts.logged_actor(fresh)
        if actor != accounts.logged_actor(session):
            raise ApiError(403, "forbidden", "This session no longer has access.")
        plan = cases.parse_plan(selection.plan_in_force(case, rec)[0]) if rec else None
        if actor != {"account": "HQ", "role": "hq", "branch": None} and not (
                actor["role"] == "manager" and actor["account"] == actor["branch"]
                and actor["branch"] in {case["source_branch"], plan.dest if plan else None}):
            raise ApiError(403, "forbidden", "This case is not available to this account.")
    else:
        actor = {"account": "HQ", "role": "hq", "branch": None}
    return case, rec, actor


def current_brief(conn, run_id, case_id, rec_version, plan_key):
    db.run(conn, "UPDATE ai_outputs SET status = 'HISTORICAL' WHERE run_id = :r AND kind = 'BRIEF' "
                 "AND case_id = :c AND status IN ('CURRENT', 'FALLBACK') AND (rec_version IS DISTINCT FROM :v "
                 "OR plan_key IS DISTINCT FROM :p)", r=run_id, c=case_id, v=rec_version, p=plan_key)
    return db.one(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND kind = 'BRIEF' AND case_id = :c "
                       "AND rec_version = :v AND plan_key = :p AND status = 'CURRENT' "
                       "ORDER BY created_at DESC, output_id DESC LIMIT 1",
                  r=run_id, c=case_id, v=rec_version, p=plan_key)


def brief_history(conn, run, session, case_id):
    """Authorized historical briefs in stable newest-first order; never generate."""
    case, rec, actor = authorize_case(conn, run, case_id, session)
    saved = db.rows(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND case_id = :c "
                         "AND kind = 'BRIEF' AND status IN ('CURRENT', 'HISTORICAL', 'FALLBACK') "
                         "ORDER BY created_at DESC, output_id DESC", r=run["run_id"], c=case_id)
    result = []
    for row in saved:
        if not _historical(case, rec, row):
            continue
        try:
            _authorize_output(conn, run, session, case, actor, row)
        except ApiError as exc:
            if exc.status != 404:
                raise
            continue
        result.append(_history_view(conn, case, rec, row, None))
    return result


def _spend(conn, session):
    try:
        caps.spend(conn, session["session_id"] if session else None, "ai")
    except ApiError as exc:
        if exc.code != "cap_reached":
            raise
        daily = exc.message.startswith(("The demo has reached", "The daily limit"))  # caps wording before/after 06.1
        raise ApiError(exc.status, exc.code, DAILY if daily else exc.message,
                       kind="ai", scope="daily" if daily else "session") from None


def prepare_brief(run_id, case_id, session=None, *, precomputed=False, baseline=False):
    precomputed = precomputed or baseline

    def work(conn):
        run = db.lock_run(conn, run_id, statuses=("READY",) if baseline else
                          ("ACTIVE", "INITIALIZING", "READY") if precomputed else ("ACTIVE", "INITIALIZING"))
        if run["status"] == "READY" and (run["kind"] != "TEMPLATE" or run["failure"]):
            raise ApiError(409, "not_available", "The baseline job requires a completed inactive template.")
        case, rec, actor = authorize_case(conn, run, case_id, session, lock=True)
        if rec is None:
            raise ApiError(409, "not_available", "This case has no calculated options yet.")
        plan = selection.plan_in_force(case, rec)[0]
        ctx = {"run_id": run_id, "scope": "CASE", "case_id": case_id, "rec_version": rec["rec_version"],
               "plan_key": plan, "session_id": session["session_id"] if session else None, "actor": actor,
               "snapshot_id": None, "model_id": config.model_id(), "assistant_version": config.ASSISTANT_VERSION,
               "precomputed": precomputed}
        summary = cases.detail(conn, run, case_id)["summary"]
        job = {"ctx": ctx, "kind": "BRIEF", "summary": summary, "location": config.location(),
               "dataset_version": run["dataset_version"], "precomputed": precomputed}
        saved = current_brief(conn, run_id, case_id, ctx["rec_version"], plan)
        if saved is None and not precomputed:
            # Baseline failure permits one live first-open admission. Once that
            # live attempt fails, later reads/polls reuse its terminal fallback.
            # Store the admission mode explicitly: final FALLBACK provenance
            # alone cannot distinguish a baseline attempt from a charged one.
            saved = db.one(conn, "SELECT o.* FROM ai_outputs o JOIN ai_usage u "
                "ON u.run_id = o.run_id AND u.output_id = o.output_id "
                "WHERE o.run_id = :r AND o.kind = 'BRIEF' AND o.case_id = :c "
                "AND o.rec_version = :v AND o.plan_key = :p AND o.status = 'FALLBACK' "
                "AND u.precomputed IS FALSE ORDER BY o.created_at DESC, o.output_id DESC LIMIT 1",
                r=run_id, c=case_id, v=ctx["rec_version"], p=plan)
        reusable = saved is not None and not precomputed
        if saved is not None and precomputed and config.baseline_matches(saved, ctx):
            reusable = db.one(conn, "SELECT 1 AS ok FROM ai_usage WHERE run_id = :r AND output_id = :o "
                                   "AND precomputed IS TRUE AND status = 'DONE'",
                              r=run_id, o=saved["output_id"]) is not None
        if reusable:
            job["saved"] = output_view(conn, saved, summary)
            return job
        # CASE is shared across tabs/people: its claim must not include the opener's session.
        output_id = "A" + uuid.uuid4().hex
        claim = db.one(conn, "INSERT INTO ai_outputs (run_id, output_id, kind, case_id, rec_version, plan_key, "
                            "model_id, assistant_version, actor, provenance, status) "
                            "VALUES (:r, :id, 'BRIEF', :c, :v, :p, :m, :av, CAST(:a AS jsonb), :pr, 'GENERATING') "
                            "ON CONFLICT DO NOTHING RETURNING output_id", r=run_id, id=output_id, c=case_id,
                       v=ctx["rec_version"], p=plan, m=ctx["model_id"], av=config.ASSISTANT_VERSION,
                       a=db.js(actor), pr="PRECOMPUTED" if precomputed else "LIVE")
        if claim is None:
            job["generating"] = True
            return job
        job["output_id"] = output_id
        ctx["request_id"] = output_id  # audit correlation, never an HTTP/model-supplied ID
        if not precomputed:
            _spend(conn, session)
        _start_attempt(conn, job)
        return job
    return db.in_tx(work)  # commit claim -> cap -> attempt BEFORE the producer exists


def prepare_answer(run_id, case_id, session, request_id, question, key, digest):
    """Personal ASK uses the same committed admission and terminal recorder."""
    def work(conn):
        run = db.lock_run(conn, run_id)
        case, rec, actor = authorize_case(conn, run, case_id, session, lock=True)
        if rec is None:
            raise ApiError(409, "not_available", "This case has no calculated options yet.")
        ctx = {"run_id": run_id, "scope": "ASK", "case_id": case_id, "rec_version": rec["rec_version"],
               "plan_key": selection.plan_in_force(case, rec)[0], "session_id": session["session_id"],
               "actor": actor, "request_id": request_id, "snapshot_id": None,
               "model_id": config.model_id(), "assistant_version": config.ASSISTANT_VERSION}
        summary = cases.detail(conn, run, case_id)["summary"]
        job = {"ctx": ctx, "kind": "ANSWER", "request_id": request_id, "question": question,
               "summary": summary, "location": config.location(), "dataset_version": run["dataset_version"],
               "precomputed": False}
        matches = db.rows(conn, "SELECT * FROM chat_requests WHERE run_id = :r AND "
            "(request_id = :q OR (session_id = :s AND case_id = :c AND idempotency_key = :k)) FOR UPDATE",
            r=run_id, q=request_id, s=session["session_id"], c=case_id, k=key)
        if len(matches) > 1:
            raise ApiError(409, "idempotency_conflict", "These request identities name different questions.")
        previous = matches[0] if matches else None
        if previous:
            _private({**previous, "scope": "ASK"}, session)
            if previous["case_id"] != case_id or previous["request_id"] != request_id or previous["body_hash"] != digest:
                raise ApiError(409, "idempotency_conflict", "This request identity was used with a different question.")
            saved = db.one(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND request_id = :q "
                                "AND kind = 'ANSWER'", r=run_id, q=request_id)
            if previous["status"] == "RUNNING":
                job["generating"] = True
            else:
                job["saved"] = output_view(conn, saved, summary)
            return job
        # The case row serializes personal-key admission before the unique claim.
        claim = db.one(conn, "INSERT INTO chat_requests (run_id, request_id, session_id, case_id, kind, status, "
            "rec_version, plan_key, actor, question, body_hash, idempotency_key) "
            "VALUES (:r, :q, :s, :c, 'ASK', 'RUNNING', :v, :p, CAST(:a AS jsonb), :text, :h, :k) "
            "ON CONFLICT DO NOTHING RETURNING request_id",
            r=run_id, q=request_id, s=session["session_id"], c=case_id, v=ctx["rec_version"], p=ctx["plan_key"],
            a=db.js(actor), text=question, h=digest, k=key)
        if claim is None:
            raise ApiError(409, "idempotency_conflict", "This request identity is already in use.")
        output_id = "A" + uuid.uuid4().hex
        db.run(conn, "INSERT INTO ai_outputs (run_id, output_id, kind, case_id, rec_version, plan_key, "
            "session_id, request_id, question, actor, model_id, assistant_version, provenance, status) "
            "VALUES (:r, :o, 'ANSWER', :c, :v, :p, :s, :q, :text, CAST(:a AS jsonb), :m, :av, 'LIVE', 'GENERATING')",
            r=run_id, o=output_id, c=case_id, v=ctx["rec_version"], p=ctx["plan_key"], s=session["session_id"],
            q=request_id, text=question, a=db.js(actor), m=ctx["model_id"], av=ctx["assistant_version"])
        job["output_id"] = output_id
        _spend(conn, session)
        _start_attempt(conn, job)
        # Only checked completed personal prose is quoted into a new Runner.
        job["history"] = [{"question": row["question"], "text": numbers.render(row["text"], row["refs"])}
                          for row in reversed(_history_rows(conn, run_id, case_id, session, limit=3))
                          if row["provenance"] != "FALLBACK" and row["text"].strip()]
        return job
    return db.in_tx(work)


def _history_scope(run_id, case_id, session):
    """Apply original permissions before counting, cursor lookup and LIMIT."""
    actor = accounts.logged_actor(session)
    params = dict(r=run_id, c=case_id, s=session["session_id"], a=db.js(actor),
                  hq=actor["role"] == "hq", branch=actor["branch"])
    scope = ("o.run_id = :r AND o.case_id = :c AND o.session_id = :s AND o.actor = CAST(:a AS jsonb) "
        "AND o.kind = 'ANSWER' AND o.status IN ('CURRENT', 'HISTORICAL', 'FALLBACK') "
        "AND o.signal_id IS NULL AND o.signal_revision IS NULL "
        "AND EXISTS (SELECT 1 FROM chat_requests q WHERE q.run_id = o.run_id AND q.request_id = o.request_id "
        "AND q.case_id = o.case_id AND q.rec_version = o.rec_version AND q.plan_key = o.plan_key "
        "AND q.session_id = o.session_id AND q.actor = o.actor AND q.kind = 'ASK' AND q.status = 'DONE') "
        "AND EXISTS (SELECT 1 FROM recommendations r JOIN cases c ON c.run_id = r.run_id AND c.case_id = r.case_id "
        "CROSS JOIN LATERAL jsonb_array_elements(r.result->'options') opt "
        "WHERE r.run_id = o.run_id AND r.case_id = o.case_id AND r.rec_version = o.rec_version "
        "AND opt->>'key' = o.plan_key AND (:hq OR :branch = c.source_branch OR opt->>'dest' = :branch))")
    return scope, params


def _history_rows(conn, run_id, case_id, session, *, limit=4, cursor=None):
    scope, params = _history_scope(run_id, case_id, session)
    boundary = ""
    if cursor:
        row = db.one(conn, f"SELECT o.created_at, o.output_id FROM ai_outputs o WHERE {scope} AND o.output_id = :id",
                     **params, id=cursor)
        if row is None:
            raise ApiError(400, "bad_request", "This history cursor is not available.")
        boundary = " AND (o.created_at, o.output_id) < (:at, :id)"
        params.update(at=row["created_at"], id=row["output_id"])
    return db.rows(conn, f"SELECT o.* FROM ai_outputs o WHERE {scope}{boundary} "
                        "ORDER BY o.created_at DESC, o.output_id DESC LIMIT :n", **params, n=limit)


def _historical(case, rec, row):
    return (row["status"] == "HISTORICAL" or rec is None or rec["rec_version"] != row["rec_version"]
            or selection.plan_in_force(case, rec)[0] != row["plan_key"] or case["state"] not in cases.LIVE_STATES)


def _history_view(conn, case, rec, row, summary):
    historical = _historical(case, rec, row)
    # A prior fallback's summary was not retained. Do not substitute current
    # figures or rerun a historical engine/tool/model to fill that missing data.
    out = output_view(conn, row, None if historical else summary)
    out.update(read_only=historical, status="HISTORICAL" if historical else row["status"])
    if historical:
        out["missing"] = ["historical_summary"]
        if row["provenance"] == "FALLBACK":
            out["text"] = row["text"]
    out["cards"] = [{**card, "read_only": historical or card.get("read_only", False),
                     "can_adopt": bool(not historical and not card.get("read_only") and card.get("adoptable"))}
                    for card in out["cards"]]
    return out


def history(conn, run, session, case_id, cursor=None):
    case, rec, actor = authorize_case(conn, run, case_id, session)
    found = _history_rows(conn, run["run_id"], case_id, session, cursor=cursor)
    summary = cases.detail(conn, run, case_id)["summary"]
    turns = [_history_view(conn, case, rec, _authorize_output(conn, run, session, case, actor, row), summary)
             for row in found[:3]]
    scope, params = _history_scope(run["run_id"], case_id, session)
    total = db.one(conn, f"SELECT count(*) AS n FROM ai_outputs o WHERE {scope}", **params)["n"]
    return {"turns": turns, "next_cursor": turns[-1]["output_id"] if len(found) > 3 else None, "total": int(total)}


def drafts(conn, run, session, case_id):
    """Authorized checked originals, with current eligibility derived on read.

    No generation, draft mutation, slot spend or deadline occurs here. Older
    tuples retain their original wording/context as read-only history. Open
    manual requests take precedence without rewriting the original draft.
    """
    case, rec, actor = authorize_case(conn, run, case_id, session)
    plan_key, _, _ = selection.plan_in_force(case, rec)
    version = rec["rec_version"] if rec else None
    left = max(0, api_config.MAX_REQUESTS_PER_VERSION - signals._groups(
        conn, run["run_id"], case_id, version)) if rec else 0
    ctx = {"run_id": run["run_id"], "scope": "CASE", "case_id": case_id,
           "rec_version": version, "plan_key": plan_key}
    checks = tools._fact_checks(conn, run, ctx) if rec else {"fact_checks": {}, "fact_order": []}
    eligible = {fid: checks["fact_checks"][fid] for fid in checks["fact_order"]
                if checks["fact_checks"][fid]["class"] and checks["fact_checks"][fid]["kind"] in commitment.ASKABLE}
    source = actor["role"] == "manager" and actor["branch"] == case["source_branch"]
    live = case["state"] in cases.LIVE_STATES
    saved = db.rows(conn, "SELECT * FROM evidence_drafts WHERE run_id = :r AND case_id = :c "
                         "ORDER BY created_at, draft_id", r=run["run_id"], c=case_id)
    result = []
    for row in saved:
        fact = eligible.get(row["fact_id"])
        current = (live and row["rec_version"] == version and row["plan_key"] == plan_key and fact is not None)
        linked = fact["request"] if current else None
        request_id = (linked["request_id"] if linked else None) or row["request_id"]
        can_send = bool(current and source and left and row["state"] == "DRAFT" and not request_id)
        reason = ("This draft belongs to an earlier version or plan, or the case no longer accepts requests."
                  if not current else "A request already exists for this fact." if request_id else
                  f"Only the {row['owner']} manager can send this request." if not source else
                  "No request slots remain for this version. Review current checks and the available case actions."
                  if not left else fact["reason"])
        action = ("review_current_checks" if not current else "view_request" if request_id else
                  "review_draft" if can_send else "review_current_checks")
        result.append({**row, "created_at": cases.utc(row["created_at"]),
                       "state": row["state"] if current else "HISTORICAL", "request_id": request_id,
                       "read_only": not can_send, "can_send": can_send, "reason": reason, "next_action": action})
    order = {fid: i for i, fid in enumerate(eligible)}
    result.sort(key=lambda row: (row["state"] == "HISTORICAL", row["class"] != "Required",
                                order.get(row["fact_id"], len(order)), row["created_at"], row["draft_id"]))
    by_fact = {row["fact_id"]: row for row in result if row["state"] != "HISTORICAL"}
    attention = []
    for fid, fact in eligible.items():
        draft = by_fact.get(fid)
        if draft:
            action, reason = draft["next_action"], draft["reason"]
        elif fact["request"]:
            action, reason = "view_request", "A request already exists for this fact."
        elif source and live and left:
            action, reason = "request_check", fact["reason"]
        else:
            action, reason = "review_current_checks", (
                f"Only the {case['source_branch']} manager can send a request." if not source else
                "Review current checks and the available case actions.")
        attention.append({**fact, "next_action": action, "reason": reason})
    reason = ("No engine-flagged stock or expiry checks need a draft for this plan." if not eligible else
              "No current request draft. The source manager can review the checks and request a check manually."
              if not by_fact else None)
    return {"rec_version": version, "plan_key": plan_key, "drafts": result, "requests_left": left,
            "attention": attention, "reason": reason, "next_action": "review_current_checks"}


def _identity(job):
    return {key: job["ctx"][key] for key in ("run_id", "case_id", "rec_version", "plan_key")}


def output_view(conn, row, summary):
    measured = usage.for_output(conn, row)
    out = {**_safe_detail(row), "created_at": cases.utc(row["created_at"]), "summary": summary,
           "usage": measured, "usage_state": "retained" if measured is not None else "missing"}
    out["label"] = FALLBACK if row["provenance"] == "FALLBACK" else (
        "Precomputed" if row["provenance"] == "PRECOMPUTED" else "Live")
    if row["status"] == "INTERRUPTED":
        out.update(label="Interrupted, ask again", text="")
    elif row["provenance"] == "FALLBACK":
        out["text"] = summary  # engine summary on the wire, never partial model prose
    audit = _output_audit(conn, row)
    out["sources"] = [_source_summary(r, row) for r in audit if _has_data(r)]
    present = {r["result_id"] for r in audit}
    missing = sum(1 for s in row["sources"] if s["result_id"] not in present)
    out.update(missing_sources=missing, sources_state=("partial" if out["sources"] else "missing") if missing
               else "available" if out["sources"] else "none")
    return out


class _Publication:
    """Share cancellation/deadline with short, shielded DB finalization work."""

    def __init__(self, seconds):
        self.started = time.monotonic()
        self.deadline = self.started + seconds
        self.cancelled = threading.Event()

    def stop_reason(self):
        if self.cancelled.is_set():
            return "interrupted"
        if time.monotonic() >= self.deadline:
            return "timeout"
        return None


def _finish(job, stats, candidate=None, *, interrupted=False, reason=None, publication=None):
    def work(conn):
        ctx = job["ctx"]
        # Retired runs still retain an audit, but can never publish current text.
        run = db.lock_run(conn, ctx["run_id"], statuses=("ACTIVE", "INITIALIZING", "READY", "RETIRED", "FAILED"))
        case = cases.case_row(conn, run["run_id"], ctx["case_id"], lock=True)
        summary = cases.detail(conn, run, ctx["case_id"])["summary"]
        attempt = db.one(conn, "SELECT status FROM ai_usage WHERE run_id = :r AND attempt_id = :id FOR UPDATE",
                         r=ctx["run_id"], id=job["output_id"])
        if attempt["status"] != "RUNNING":
            saved = db.one(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND output_id = :id",
                           r=ctx["run_id"], id=job["output_id"])
            return output_view(conn, saved, summary)
        rec = cases.active_rec(conn, case)
        publishable = (run["status"] in ("ACTIVE", "INITIALIZING") or
                       (run["status"] == "READY" and run["kind"] == "TEMPLATE" and job["precomputed"]))
        current = (publishable and rec is not None
                   and rec["rec_version"] == ctx["rec_version"]
                   and selection.plan_in_force(case, rec)[0] == ctx["plan_key"])
        audit = db.rows(conn, "SELECT * FROM tool_results WHERE run_id = :r AND request_id = :q "
                             "AND case_id = :c AND scope = :sc AND (:sc = 'CASE' OR session_id = :s) "
                             "AND rec_version IS NOT DISTINCT FROM :v AND plan_key IS NOT DISTINCT FROM :p "
                             "ORDER BY created_at, result_id", r=ctx["run_id"], q=ctx["request_id"],
                        c=ctx["case_id"], sc=ctx["scope"], s=ctx.get("session_id"),
                        v=ctx["rec_version"], p=ctx["plan_key"])
        results = {r["result_id"]: r["result"] for r in audit}
        stopped = "interrupted" if interrupted else publication.stop_reason() if publication else None
        text, failure_reason = (candidate or "").strip() if not stopped else "", stopped or reason
        if not text and not stopped and failure_reason is None:
            failure_reason = "empty"
        if job["kind"] == "BRIEF" and len(text.split()) > BRIEF_MAX_WORDS:
            text, failure_reason = "", "brief_too_long"
        checked = numbers.check(text, results, ref=refdata.get(job["dataset_version"])) if text else None
        # References and removed-number warnings can expand the displayed prose.
        # Check the whole answer at both boundaries; never make a shorter answer
        # by silently cutting away unexamined prose or its material warnings.
        if (checked and job["kind"] == "BRIEF"
                and len(numbers.render(checked["text"], checked["refs"]).split()) > BRIEF_MAX_WORDS):
            text, failure_reason, checked = "", "brief_too_long", None
        # Checking/lock waits may outlive a disconnect or the producer deadline.
        # The worker can retain audit, but must discard its uncommitted candidate.
        stopped = "interrupted" if interrupted else publication.stop_reason() if publication else None
        if stopped:
            text, failure_reason, checked = "", stopped, None
        was_interrupted = stopped == "interrupted"
        checked = checked or {"text": "", "refs": [], "claims": 0, "removed": 0}
        terminal = "INTERRUPTED" if was_interrupted else "DONE" if text else "FALLBACK"
        status = ("INTERRUPTED" if was_interrupted else "HISTORICAL" if not current
                  else "CURRENT" if text else "FALLBACK")
        provenance = "FALLBACK" if not text else "PRECOMPUTED" if job["precomputed"] else "LIVE"
        sources = [{"result_id": row["result_id"], "kind": row["tool"], "tool": row["tool"]} for row in audit]
        # Cards are exact successful engine results from this attempt's scoped
        # audit, never model-authored JSON or inferred from narrative wording.
        cards = [{**_identity(job), "result_id": row["result_id"], "result": row["result"],
                  "overrides": row["result"]["overrides"], "adoptable": row["result"]["adoptable"],
                  "simulated_rec_version": row["result"]["what_if_version"], "read_only": not current}
                 for row in audit if text and not was_interrupted and job["kind"] == "ANSWER"
                 and row["tool"] == "simulate_options" and "error" not in row["result"]]
        if job["kind"] == "BRIEF" and text and current:
            db.run(conn, "UPDATE ai_outputs SET status = 'HISTORICAL' WHERE run_id = :r AND case_id = :c "
                         "AND kind = 'BRIEF' AND status = 'CURRENT' AND output_id <> :o",
                   r=ctx["run_id"], c=ctx["case_id"], o=job["output_id"])
        db.run(conn, "UPDATE ai_outputs SET text = :text, refs = CAST(:refs AS jsonb), checks = CAST(:ck AS jsonb), "
                     "sources = CAST(:sources AS jsonb), cards = CAST(:cards AS jsonb), "
                     "status = :st, provenance = :pr, reason = :why "
                     "WHERE run_id = :r AND output_id = :o", text=checked["text"], refs=db.js(checked["refs"]),
               ck=db.js({k: checked[k] for k in ("claims", "removed")}), sources=db.js(sources), cards=db.js(cards), st=status,
               pr=provenance, why=failure_reason, r=ctx["run_id"], o=job["output_id"])
        measured = {**stats, "total_ms": (time.monotonic() - publication.started) * 1000} if publication else stats
        _finish_attempt(conn, job, measured, terminal, failure_reason)
        if job["kind"] == "ANSWER":
            db.run(conn, "UPDATE chat_requests SET status = :st WHERE run_id = :r AND request_id = :q",
                   st="INTERRUPTED" if was_interrupted else "DONE", r=ctx["run_id"], q=ctx["request_id"])
        row = db.one(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND output_id = :o",
                     r=ctx["run_id"], o=job["output_id"])
        return output_view(conn, row, summary)
    return db.in_tx(work)


async def run_job(job):
    """Queue progress while a separate producer owns the model's deadline."""
    identity = _identity(job)
    final_event = "answer" if job["kind"] == "ANSWER" else "brief"
    summary = {"type": "summary", **identity, "summary": job["summary"], "label": GENERATING}
    read_usage = {"load_model_calls": 0} if job["kind"] == "BRIEF" else {}
    if "saved" in job:
        yield {**summary, "label": job["saved"]["label"], **read_usage}
        yield {"type": "interrupted" if job["saved"]["status"] == "INTERRUPTED" else final_event,
               **job["saved"], **read_usage}
        return
    if job.get("generating"):
        yield summary
        yield {"type": "generating", **identity, "status": "GENERATING", "label": GENERATING, **read_usage}
        return
    queue = asyncio.Queue()
    stats = {}
    publication = _Publication(config.load().seconds)
    finished = False

    async def finalize(candidate=None, *, interrupted=False, reason=None):
        # Keep blocking pg8000/checking off the event loop so a disconnect can
        # set the fence while finalization is pending. Always await this worker;
        # unlike a cancelled tool thread it owns terminal request cleanup.
        worker = asyncio.create_task(asyncio.to_thread(_finish, job, dict(stats), candidate,
            interrupted=interrupted, reason=reason, publication=publication))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await worker
            raise

    async def produce():
        nonlocal finished
        try:
            async with asyncio.timeout_at(publication.deadline):
                if not ai_on():
                    raise model.ModelUnavailable("Gemini generation is disabled")
                ask = job["kind"] == "ANSWER"
                candidate = None
                async with aclosing(model.run(prompts.ask if ask else prompts.brief,
                        [types.Part(text=job["question"] if ask else "Explain this case and its checks.")],
                        tools=tools.ASK_TOOLS if ask else tools.CASE_TOOLS, ctx=job["ctx"], usage=stats,
                        history=job.get("history", ()))) as stream:
                    async for event in stream:
                        if event["type"] == "final":
                            queue.put_nowait({"type": "state", **identity, "seq": event["seq"],
                                              "state": "Checking numbers"})
                            candidate = event["text"]
                            break
                        queue.put_nowait({**identity, **event})
                # Runner cancellation/cleanup has reported usage before the
                # snapshot is finalized. Only complete prose reaches checking.
                final = await finalize(candidate)
                finished = True
                queue.put_nowait({"type": final_event, **final})
        except asyncio.CancelledError:
            publication.cancelled.set()
            with anyio.CancelScope(shield=True):
                await finalize(interrupted=True, reason="interrupted")
                finished = True
            raise
        except Exception as exc:
            # Provider diagnostics may contain credentials. Persist a safe category only.
            reason = "timeout" if isinstance(exc, TimeoutError) else "tool_limit" if isinstance(exc, model.LimitReached) else "unavailable"
            final = await finalize(reason=reason)
            finished = True
            queue.put_nowait({"type": final_event, **final})
        finally:
            queue.put_nowait(None)

    producer = asyncio.create_task(produce())
    try:
        yield summary
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                yield {"type": "ping"}
                continue
            if event is None:
                return
            yield event
    finally:
        if not finished:
            publication.cancelled.set()
            producer.cancel()
        # Starlette's disconnect uses level cancellation. Let model cleanup finish
        # and persist measured usage before returning from the ASGI stream.
        with anyio.CancelScope(shield=True):
            await asyncio.gather(producer, return_exceptions=True)
            if not finished:  # cancellation can precede the producer's first instruction
                await finalize(interrupted=True, reason="interrupted")


def abort_unfinished(job):
    """Response cleanup also covers a disconnect before the body is iterated."""
    if "output_id" in job and "saved" not in job:
        _finish(job, {}, interrupted=True, reason="interrupted")


async def brief(run_id, case_id, session=None, *, precomputed=False, provenance=None):
    # Retain the original internal baseline-call contract. HTTP never accepts
    # this argument; the compatibility spelling admits only a completed inactive
    # template without a reviewer session through the same checked pipeline.
    if provenance is not None:
        if provenance != "PRECOMPUTED" or session is not None:
            raise ApiError(409, "not_available", "Precomputed briefs require the inactive template job.")
        job = prepare_brief(run_id, case_id, baseline=True)
    else:
        job = prepare_brief(run_id, case_id, session, precomputed=precomputed)
    async with aclosing(run_job(job)) as stream:
        async for event in stream:
            yield event


def sse(job):
    async def stream():
        async with aclosing(run_job(job)) as events:
            async for event in events:
                if event["type"] == "ping":
                    yield ": ping\n\n"
                else:
                    name = event["type"]
                    yield f"event: {name}\ndata: {json.dumps({k: v for k, v in event.items() if k != 'type'}, default=str)}\n\n"
    return stream()


def _private(row, session):
    if row.get("scope") == "ASK" or row.get("kind") == "ANSWER":
        if row["session_id"] != session["session_id"] or row.get("actor") != accounts.logged_actor(session):
            raise ApiError(404, "not_found", "This source is not available.")


def _unavailable():
    return ApiError(404, "not_found", "This source is not available.")


def _authorize_output(conn, run, session, case, actor, row):
    """Current permission AND the saved original scope, including historical keys.

    Never bind an old result to today's version/selection. A destination must
    still read this case and must also have been entitled to the saved plan.
    """
    if (row is None or row["kind"] not in ("BRIEF", "ANSWER") or row["run_id"] != run["run_id"]
            or row["case_id"] != case["case_id"] or row.get("signal_id") is not None
            or row.get("signal_revision") is not None or not row.get("rec_version") or not row.get("plan_key")):
        raise _unavailable()
    _private(row, session)
    rec = db.one(conn, "SELECT result FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                 r=run["run_id"], c=case["case_id"], v=row["rec_version"])
    if rec is None or row["plan_key"] not in {o["key"] for o in rec["result"]["options"]}:
        raise _unavailable()
    if actor["role"] == "manager" and actor["branch"] not in {
            case["source_branch"], cases.parse_plan(row["plan_key"]).dest}:
        raise _unavailable()
    if row["kind"] == "ANSWER":
        attempt = db.one(conn, "SELECT * FROM chat_requests WHERE run_id = :r AND request_id = :q",
                         r=run["run_id"], q=row["request_id"])
        if attempt is None or attempt["kind"] != "ASK" or any(attempt[k] != row[k] for k in (
                "case_id", "rec_version", "plan_key", "session_id", "request_id", "actor")):
            raise _unavailable()
    return row


def _output_audit(conn, output):
    """Exact retained rows for one output/attempt; no model, tools or recompute."""
    scope = "ASK" if output["kind"] == "ANSWER" else "CASE"
    return db.rows(conn, "SELECT * FROM tool_results WHERE run_id = :r AND case_id = :c AND request_id = :q "
        "AND scope = :sc AND rec_version IS NOT DISTINCT FROM :v AND plan_key IS NOT DISTINCT FROM :p "
        "AND snapshot_id IS NOT DISTINCT FROM :snap AND signal_id IS NULL AND signal_revision IS NULL "
        "AND (:sc = 'CASE' OR (session_id = :s AND actor = CAST(:a AS jsonb))) "
        "ORDER BY created_at, result_id", r=output["run_id"], c=output["case_id"],
        q=output["request_id"] if scope == "ASK" else output["output_id"], sc=scope,
        v=output["rec_version"], p=output["plan_key"], snap=output.get("snapshot_id"),
        s=output.get("session_id"), a=db.js(output.get("actor")))


def _safe_detail(value):
    """Legacy/defense-in-depth projection; safe compact values stay exact."""
    if isinstance(value, dict):
        return {k: _safe_detail(v) for k, v in value.items() if not (
            any(part in k.lower() for part in ("thought", "credential", "hidden", "truth"))
            or k.lower() in {"api_key", "secret", "password", "authorization", "access_token", "refresh_token",
                             "cookie", "cookies", "private_key", "system_instruction"})}
    if isinstance(value, list):
        return [_safe_detail(v) for v in value]
    return value


def _has_data(row):
    return row.get("status") != "FAILED" and bool(row["result"]) and "error" not in row["result"]


def _source_summary(row, output):
    result, tool = _safe_detail(row["result"]), row["tool"]
    categories = []
    if tool in ("get_case", "simulate_options", "check_constraints", "get_transport_options", "get_fact_checks"):
        categories.append("Engine result · this version")
    if tool == "get_case" and result.get("lot") or tool == "get_inventory_and_inbound":
        categories.append("Stock records")
    if tool == "get_forecast":
        categories.append("Forecast")
    if tool in ("get_fact_checks", "draft_evidence_request"):
        categories.append("Requests and confirmed facts")
    out = {"result_id": row["result_id"], "kind": tool, "tool": tool, "categories": categories,
           "source_kind": result.get("source_kind"), "units": result.get("units"),
           "paths": [ref["path"] for ref in output["refs"] if ref["result_id"] == row["result_id"]
                     and numbers.lookup({row["result_id"]: result}, row["result_id"], ref["path"]) is not None]}
    if tool == "get_forecast":
        out.update(forecast_run_id=result.get("forecast_run_id"),
                   providers=sorted({day["provider"] for day in result.get("days", []) if day.get("provider")}))
    return out


def _source_view(row, output):
    # Do not serialize internal actor/session identity from shared CASE audits.
    fields = ("run_id", "result_id", "scope", "case_id", "rec_version", "plan_key", "snapshot_id",
              "tool", "inputs", "result", "duration_ms", "status", "purpose")
    return {**{k: _safe_detail(row.get(k)) for k in fields},
            "created_at": cases.utc(row["created_at"]), **_source_summary(row, output)}


def source(conn, run, session, case_id, result_id):
    case, _, actor = authorize_case(conn, run, case_id, session)
    row = db.one(conn, "SELECT * FROM tool_results WHERE run_id = :r AND case_id = :c AND result_id = :id",
                 r=run["run_id"], c=case_id, id=result_id)
    if row is None or row["scope"] not in ("CASE", "ASK"):
        raise _unavailable()
    _private(row, session)
    locator = "output_id" if row["scope"] == "CASE" else "request_id"
    output = db.one(conn, f"SELECT * FROM ai_outputs WHERE run_id = :r AND case_id = :c AND {locator} = :q "
                         "AND kind = :k", r=run["run_id"], c=case_id, q=row["request_id"],
                    k="BRIEF" if row["scope"] == "CASE" else "ANSWER")
    _authorize_output(conn, run, session, case, actor, output)
    if result_id not in {r["result_id"] for r in _output_audit(conn, output)}:
        raise _unavailable()
    return _source_view(row, output)


def trace(conn, run, session, case_id, output_id=None, request_id=None):
    if bool(output_id) == bool(request_id):
        raise ApiError(400, "bad_request", "Choose exactly one output_id or request_id.")
    case, _, actor = authorize_case(conn, run, case_id, session)
    if output_id:
        row = db.one(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND case_id = :c AND output_id = :id",
                     r=run["run_id"], c=case_id, id=output_id)
    else:
        row = db.one(conn, "SELECT * FROM ai_outputs WHERE run_id = :r AND case_id = :c "
                           "AND request_id = :id AND kind = 'ANSWER'", r=run["run_id"], c=case_id, id=request_id)
    _authorize_output(conn, run, session, case, actor, row)
    fields = ("result_id", "scope", "case_id", "rec_version", "plan_key", "tool", "duration_ms", "status", "purpose")
    return [{**{k: r.get(k) for k in fields}, "created_at": cases.utc(r["created_at"])}
            for r in _output_audit(conn, row)]
