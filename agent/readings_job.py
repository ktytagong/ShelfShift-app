"""Reading admission and persistence; no model call holds a DB transaction.

D-01/D-22: content plus context deduplicates within a workspace. The run lock
serializes admission (including cap refusals); an attempt fences late finishers.
Nothing in this module writes a report, confirmed fact or uploaded file.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from dataclasses import replace
import hashlib
import uuid

from api import caps, config, db, files, inputs, overlay, refdata, signals
from api.errors import ApiError
from . import config as assistant_config
from . import reading


# A failed release attempt is retained audit, not a final first-open result.
# Both content-key admission and revision read projections share this rule.
_ANSWER_CACHE_ELIGIBLE = ("NOT (purpose='ANSWER' AND provenance='PRECOMPUTED' "
                          "AND status IN ('FAILED','REFUSED','INTERRUPTED'))")


def genai_client():
    """Public Phase 9 builder, retaining identity, enterprise mode and retry."""
    from . import model
    return model.llm().api_client


def context(conn, run, actor, *, case_id=None, purpose="report"):
    ref = refdata.get(run["dataset_version"])
    store = actor.get("branch") if actor["role"] == "manager" else None
    case = None
    if case_id:
        case = db.one(conn, "SELECT * FROM cases WHERE run_id=:r AND case_id=:c", r=run["run_id"], c=case_id)
        if case is None:
            raise ApiError(404, "not_found", "No such case")
        if store is not None and store not in (case["source_branch"], case.get("plan_dest")):
            raise ApiError(403, "forbidden", "You can't read evidence for another branch's case")
    end = int(ref.calendar["horizon_end"])
    days = range(run["day"], end + 1)
    zones = tuple(sorted({p.zone for p in ref.products.values()}))
    where, params = overlay.everywhere(conn, run, "lots")
    lots = db.rows(conn, f"SELECT lot_id, branch FROM lots WHERE {where} AND world='shelfshift'", **params)
    codes = {r.run_code for branch in ref.branches for r in ref.runs_from(branch, run["day"])}
    prefill = {k: case[k] for k in ("case_id", "source_branch", "plan_dest", "sku", "lot_id") if k in case} if case else {}
    return reading.ReadingContext(purpose=purpose, store=store, case_prefill=prefill, day=run["day"],
        dataset_version=run["dataset_version"], model_id=assistant_config.model_id(),
        assistant_version=assistant_config.ASSISTANT_VERSION,
        calendar={ref.date(day).isoformat(): day for day in days}, horizon=(run["day"], end),
        skus=tuple(sorted(ref.products)), lot_ids=tuple(sorted({l["lot_id"] for l in lots if store in (None, l["branch"])})),
        run_codes=tuple(sorted(codes)), zones=zones,
        free_totes={(store, z, d): n for z in zones for d in days
                    if store is not None and (n := ref.free_totes(store, z, d)) is not None},
        adjustment_cost_max=inputs.policy(conn, run, ref).adjustment_cost_max)


def state_of(row):
    if row["status"] == "GENERATING" and row["lease_until"] <= datetime.now(timezone.utc):
        return "INTERRUPTED"
    return row["status"]


def _latest(conn, run, key):
    return db.one(conn, f"SELECT * FROM readings WHERE run_id=:r AND input_key=:k AND {_ANSWER_CACHE_ELIGIBLE} "
                  "ORDER BY created_at DESC, reading_id DESC LIMIT 1", r=run["run_id"], k=key)


def _saved(row):
    return {"row": row, "served": "generating" if state_of(row) == "GENERATING" else
            "precomputed" if row["provenance"] == "PRECOMPUTED" else "reuse"}


def claim(conn, run, actor, key, ctx):
    row = _latest(conn, run, key)
    if row:
        if state_of(row) != "INTERRUPTED" or row["attempt"] >= 2:
            return _saved(row)
        row = db.one(conn, "UPDATE readings SET status='GENERATING', attempt=attempt+1, "
            "lease_until=clock_timestamp()+make_interval(secs => :lease), finished_at=NULL "
            "WHERE run_id=:r AND reading_id=:id RETURNING *", r=run["run_id"], id=row["reading_id"],
            lease=config.READING["lease_s"])
    else:
        row = db.one(conn, "INSERT INTO readings (run_id, reading_id, input_key, purpose, status, attempt, "
            "model_id, assistant_version, provenance, lease_until, created_by) VALUES "
            "(:r,:id,:k,:p,'GENERATING',1,:m,:v,'LIVE',clock_timestamp()+make_interval(secs => :lease),:by) "
            "ON CONFLICT DO NOTHING RETURNING *", r=run["run_id"], id=uuid.uuid4().hex, k=key,
            p=ctx.purpose.upper(), m=ctx.model_id, v=ctx.assistant_version, lease=config.READING["lease_s"],
            by=actor["session_id"])
        if row is None:
            return _saved(_latest(conn, run, key))
    caps.spend(conn, actor["session_id"], "ai")
    return {"row": row, "served": "live"}


def refuse(conn, run, actor, key, ctx):
    """Own transaction after admission rollback; recheck under the same run lock."""
    row = _latest(conn, run, key)
    if row and state_of(row) != "INTERRUPTED":
        return row
    if row:
        return db.one(conn, "UPDATE readings SET status='REFUSED', reason='cap_reached', "
            "finished_at=clock_timestamp() WHERE run_id=:r AND reading_id=:id RETURNING *",
            r=run["run_id"], id=row["reading_id"])
    return db.one(conn, "INSERT INTO readings (run_id, reading_id, input_key, purpose, status, model_id, "
        "assistant_version, provenance, reason, created_by, finished_at) VALUES "
        "(:r,:id,:k,:p,'REFUSED',:m,:v,'LIVE','cap_reached',:by,clock_timestamp()) RETURNING *",
        r=run["run_id"], id=uuid.uuid4().hex, k=key, p=ctx.purpose.upper(), m=ctx.model_id,
        v=ctx.assistant_version, by=actor["session_id"])


def finish(conn, run, row, result, ctx, text):
    validator = reading.validate_answer if ctx.purpose == 'answer' else reading.validate
    validated = validator(result.reading, ctx, text) if result.reading is not None else {}
    historical, differs = _answer_history(conn, run, row, validated)
    usage = dict(result.usage)
    if row["purpose"] == "ANSWER":
        usage["differs_from_confirmed"] = differs
    updated = db.one(conn, "UPDATE readings SET status=:s, reason=:reason, kind=:kind, "
        "suggestions=CAST(:rows AS jsonb), needs_input=CAST(:needs AS jsonb), observations=CAST(:obs AS jsonb), "
        "note=:note, usage=CAST(:usage AS jsonb), after_confirmation=:historical, finished_at=clock_timestamp() "
        "WHERE run_id=:r AND reading_id=:id AND status='GENERATING' AND attempt=:attempt RETURNING *",
        r=run["run_id"], id=row["reading_id"], attempt=row["attempt"], s="FAILED" if result.reason else "DONE",
        reason=result.reason, kind=validated.get("kind"), rows=db.js(validated.get("rows", [])),
        needs=db.js(validated.get("needs_input", [])), obs=db.js(validated.get("observations", [])),
        note=validated.get("note", False), usage=db.js(usage), historical=historical)
    return updated or db.one(conn, "SELECT * FROM readings WHERE run_id=:r AND reading_id=:id",
                             r=run["run_id"], id=row["reading_id"])


def response(row, served):
    return {"reading_id": row["reading_id"], "status": state_of(row), "served": served, "kind": row["kind"],
            "attempt": row["attempt"],
            "suggestions": row["suggestions"], "needs_input": row["needs_input"], "note": row["note"],
            "provenance": row["provenance"], "model_id": row["model_id"], "reason": row["reason"]}


async def read_report(session, text, file_bytes=None, mime=None, *, case_id=None):
    ctx, key, parts = None, None, None

    def prepare(conn):
        nonlocal ctx, key, parts
        run = db.lock_run(conn, session["run_id"], mode="UPDATE")
        ctx = context(conn, run, session, case_id=case_id, purpose="report")
        parts = reading.build_parts(text, file_bytes, mime, ctx, uuid.uuid4().hex)
        key = reading.reuse_key(ctx, text, hashlib.sha256(file_bytes).hexdigest() if file_bytes else None)
        return claim(conn, run, session, key, ctx)

    try:
        admitted = await asyncio.to_thread(db.in_tx, prepare)
    except ApiError as exc:
        if exc.status != 429 or exc.code != "cap_reached":
            raise
        await asyncio.to_thread(db.in_tx, lambda conn: refuse(conn,
            db.lock_run(conn, session["run_id"], mode="UPDATE"), session, key, ctx))
        raise
    if admitted["served"] != "live":
        return response(admitted["row"], admitted["served"])
    client = None
    try:
        client = genai_client()
        result = await reading.read(parts, client=client, model_id=ctx.model_id, settings=config.READING)
    except Exception:
        result = reading.ReadResult(None, "error", dict.fromkeys(
            ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens")))
    finally:
        if client is not None and callable(getattr(client.aio, "aclose", None)):
            try:
                await client.aio.aclose()
            except Exception:
                # Cleanup must not discard the result/usage or strand its
                # claim. Never log exception text, credentials or evidence.
                logging.getLogger(__name__).warning("Reading client cleanup failed")
    row = await asyncio.to_thread(db.in_tx, lambda conn: finish(conn,
        db.lock_run(conn, session["run_id"], mode="UPDATE"), admitted["row"], result, ctx, text))
    return response(row, "live")


def _answer_record(conn, run, actor, signal_id, revision):
    signal = signals._signal(conn, run["run_id"], signal_id)
    if signal is None or not signal["request_id"] or signal["kind"] not in ("STOCK_COUNT", "EXPIRY"):
        raise ApiError(404, "not_found", f"No answer {signal_id}")
    request = signals._request(conn, run["run_id"], signal["request_id"])
    case = db.one(conn, "SELECT * FROM cases WHERE run_id=:r AND case_id=:c",
                  r=run["run_id"], c=request["case_id"])
    attachment = files.lookup(conn, run, signal_id) if signal["file_uri"] else None
    files.may_read(actor, signal, request, case)
    if isinstance(revision, bool) or revision != signal["revision"] or request["answer_signal_id"] != signal_id:
        raise ApiError(409, "stale_version", "The answer changed since you looked at it",
                       revision=signal["revision"])
    if not signal["text"].strip() and attachment is None:
        raise ApiError(409, "wrong_state", "Nothing to read")
    return signal, request, attachment


def answer_context(conn, run, actor, signal, request):
    """The addressed branch, SKU and requested lots, without any typed values."""
    ctx = context(conn, run, {**actor, "role": "manager", "branch": request["to_branch"]}, purpose="answer")
    lots = list(request["lot_ids"])
    prefill = {"store": request["to_branch"], "sku": request["sku"], "lot_ids": lots}
    if len(lots) == 1:
        prefill["lot"] = lots[0]
    return replace(ctx, case_prefill=prefill, skus=(request["sku"],), lot_ids=tuple(lots),
                   request_id=request["request_id"], signal_revision=signal["revision"])


def _bind_answer(conn, run, admitted, signal, request, actor, ctx):
    if admitted["served"] == "live":
        admitted["row"] = db.one(conn, "UPDATE readings SET signal_id=:s, signal_revision=:v, request_id=:q, "
            "model_id=:m, assistant_version=:av, provenance='LIVE', created_by=:by "
            "WHERE run_id=:r AND reading_id=:id RETURNING *", r=run["run_id"], id=admitted["row"]["reading_id"],
            s=signal["signal_id"], v=signal["revision"], q=request["request_id"],
            m=ctx.model_id, av=ctx.assistant_version, by=actor["session_id"])
    return admitted


def _answer_not_checked(signal, values):
    if signal["kind"] != "STOCK_COUNT":
        return None
    lots = signal["fields"]["lot_ids"]
    typed = signal["fields"]["counts"]
    pending = values["pending_counts"] or {}
    complete = lambda counts: all(counts.get(lot, {}).get(part) is not None
                                  for lot in lots for part in signals.PARTS)
    if complete(typed) or complete(pending):
        return None
    # A complete count must come from one source; complementary partials are
    # still incomplete. Describe the typed source when it contains any values.
    shown = typed if any(typed.get(lot, {}).get(part) is not None
                         for lot in lots for part in signals.PARTS) else pending
    if any(shown.get(lot, {}).get("backroom_units") is None for lot in lots):
        return "Backroom stock not checked, so the total can't be confirmed"
    if any(shown.get(lot, {}).get("shelf_units") is None for lot in lots):
        return "Shelf stock not checked, so the total can't be confirmed"
    return None


def answer_response(conn, run, row, served):
    signal = signals._signal(conn, run["run_id"], row["signal_id"])
    values = reading.answer_values(row["suggestions"], signal["fields"]["lot_ids"])
    return {**response(row, served), **values, "observations": row["observations"], "conflicts": _answer_conflicts(signal, values),
            "not_checked": _answer_not_checked(signal, values), "after_confirmation": row["after_confirmation"],
            "differs_from_confirmed": bool((row["usage"] or {}).get("differs_from_confirmed")), "typed_values": signal["fields"], "reply_text": signal["text"],
            "recorded_at": (row["finished_at"] or row["created_at"]).isoformat()}


async def read_answer(session, signal_id, revision):
    """A separate open request, after save; only readings and AI usage are written."""
    def inspect(conn):
        run = db.lock_run(conn, session["run_id"], mode="SHARE")
        signal, request, attachment = _answer_record(conn, run, session, signal_id, revision)
        ctx = answer_context(conn, run, session, signal, request)
        key = reading.reuse_key(ctx, signal["text"], signal["file_sha256"])
        old = _answer_latest(conn, run, signal, key)
        cached = None
        if old and (state_of(old) != "INTERRUPTED" or old["attempt"] >= 2):
            cached = answer_response(conn, run, old, _saved(old)["served"])
        return signal, request, attachment, cached

    signal, request, attachment, cached = await asyncio.to_thread(db.in_tx, inspect)
    if cached is not None:
        return cached
    # Permission was checked before the stored bytes; no run lock spans file I/O.
    data = await asyncio.to_thread(files.read, attachment["uri"]) if attachment else None
    ctx, key, parts = None, None, None

    def prepare(conn):
        nonlocal ctx, key, parts, signal, request
        run = db.lock_run(conn, session["run_id"], mode="UPDATE")
        signal, request, _ = _answer_record(conn, run, session, signal_id, revision)
        ctx = answer_context(conn, run, session, signal, request)
        parts = reading.build_parts(signal["text"], data, signal["file_type"], ctx, uuid.uuid4().hex)
        key = reading.reuse_key(ctx, signal["text"], signal["file_sha256"])
        previous = _answer_latest(conn, run, signal, key)
        if previous is not None:
            key = previous["input_key"]
        return _bind_answer(conn, run, claim(conn, run, session, key, ctx), signal, request, session, ctx)

    try:
        admitted = await asyncio.to_thread(db.in_tx, prepare)
    except ApiError as exc:
        if exc.status != 429 or exc.code != "cap_reached":
            raise

        def refused(conn):
            run = db.lock_run(conn, session["run_id"], mode="UPDATE")
            row = refuse(conn, run, session, key, ctx)
            return _bind_answer(conn, run, {"row": row, "served": "live"}, signal, request, session, ctx)

        await asyncio.to_thread(db.in_tx, refused)
        raise

    def render(conn, row, served):
        run = db.lock_run(conn, session["run_id"], mode="SHARE")
        return answer_response(conn, run, row, served)

    if admitted["served"] != "live":
        return await asyncio.to_thread(db.in_tx, lambda conn: render(conn, admitted["row"], admitted["served"]))
    client = None
    try:
        client = genai_client()
        result = await reading.read(parts, client=client, model_id=ctx.model_id, settings=config.READING)
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(db.in_tx, lambda conn: _interrupt_answer(conn,
            db.lock_run(conn, session["run_id"], mode="UPDATE"), admitted["row"])))
        raise
    except Exception:
        result = reading.ReadResult(None, "error", {"model_id": ctx.model_id, "model_calls": 0,
            "input_tokens": None, "output_tokens": None, "thinking_tokens": None, "total_tokens": None,
            "total_ms": 0})
    finally:
        if client is not None and callable(getattr(client.aio, "aclose", None)):
            try:
                await client.aio.aclose()
            except Exception:
                pass  # cleanup cannot undo a saved answer or hide a finished reading
    row = await asyncio.to_thread(db.in_tx, lambda conn: finish(conn,
        db.lock_run(conn, session["run_id"], mode="UPDATE"), admitted["row"], result, ctx, signal["text"]))
    return await asyncio.to_thread(db.in_tx, lambda conn: render(conn, row, "live"))


def answer_latest(conn, run, signal):
    """Read the current answer revision's visible attempt after caller authorization.

    Failed/interrupted release attempts permit a first live open. LIVE terminal
    attempts retain their existing finality, independent of model/key changes.
    """
    return db.one(conn, f"SELECT * FROM readings WHERE {_ANSWER_CACHE_ELIGIBLE} "
        "AND run_id=:r AND purpose='ANSWER' AND signal_id=:s "
        "AND signal_revision=:v ORDER BY created_at DESC, reading_id DESC LIMIT 1",
        r=run["run_id"], s=signal["signal_id"], v=signal["revision"])


def _answer_latest(conn, run, signal, key):
    return answer_latest(conn, run, signal)


def _release_answer(conn, run_id, signal_id, revision, *, mode="SHARE"):
    run = db.lock_run(conn, run_id, mode=mode, statuses=("READY",))
    if run["kind"] != "TEMPLATE" or run["failure"]:
        raise ValueError("Precompute requires a completed inactive template (READY).")
    signal = signals._signal(conn, run_id, signal_id)
    if signal is None or signal["provenance"] != signals.SIMULATED or not signal["request_id"]:
        raise ValueError("Only this template's simulated answers may be precomputed.")
    request = signals._request(conn, run_id, signal["request_id"])
    actor = dict(run_id=run_id, role="manager", account=request["to_branch"],
                 branch=request["to_branch"], session_id="preloaded-release")
    signal, request, attachment = _answer_record(conn, run, actor, signal_id, revision)
    return run, actor, signal, request, attachment


def _release_answer_claim(conn, run, actor, key, ctx, signal, request):
    old = _latest(conn, run, key)
    if old is not None and ((old["status"] == "DONE" and old["provenance"] == "PRECOMPUTED")
                            or state_of(old) == "GENERATING"):
        return dict(row=old, fresh=False)
    if old is not None and old["status"] == "GENERATING":
        if old["provenance"] != "PRECOMPUTED":
            return dict(row=old, fresh=False)
        # Retire the expired lease before inserting a new release attempt.
        # Its terminal status fences a late finish from the previous caller.
        db.run(conn, "UPDATE readings SET status='INTERRUPTED', reason='lease_expired', "
            "finished_at=clock_timestamp() WHERE run_id=:r AND reading_id=:id AND status='GENERATING'",
            r=run["run_id"], id=old["reading_id"])
    row = db.one(conn, "INSERT INTO readings (run_id, reading_id, input_key, purpose, signal_id, "
        "signal_revision, request_id, status, model_id, assistant_version, provenance, lease_until, created_by) "
        "VALUES (:r,:id,:k,'ANSWER',:s,:v,:q,'GENERATING',:m,:av,'PRECOMPUTED', "
        "clock_timestamp()+make_interval(secs => :lease),:by) ON CONFLICT DO NOTHING RETURNING *",
        r=run["run_id"], id=uuid.uuid4().hex, k=key, s=signal["signal_id"], v=signal["revision"],
        q=request["request_id"], m=ctx.model_id, av=ctx.assistant_version,
        lease=config.READING["lease_s"], by=actor["session_id"])
    # The release job retains usage without spending any reviewer allowance.
    return dict(row=row or _latest(conn, run, key), fresh=row is not None)


async def precompute_answer(template_run_id, signal_id, revision):
    """Prepare/call/finish one simulated answer on a completed READY template.

    This trusted release entry point shares live context, validation and key
    rules. Its invocation metadata is private to the release coordinator.
    """
    def inspect(conn):
        run, actor, signal, request, attachment = _release_answer(conn, template_run_id, signal_id, revision)
        ctx = answer_context(conn, run, actor, signal, request)
        key = reading.reuse_key(ctx, signal["text"], signal["file_sha256"])
        old = _latest(conn, run, key)
        cached = None
        if old is not None and ((old["status"] == "DONE" and old["provenance"] == "PRECOMPUTED")
                                or state_of(old) == "GENERATING"):
            cached = dict(answer_response(conn, run, old, _saved(old)["served"]), fresh=False, model_calls=0)
        return signal, attachment, cached

    signal, attachment, cached = await asyncio.to_thread(db.in_tx, inspect)
    if cached is not None:
        return cached
    data = await asyncio.to_thread(files.read, attachment["uri"]) if attachment else None
    ctx, parts = None, None

    def prepare(conn):
        nonlocal ctx, parts, signal
        run, actor, signal, request, _ = _release_answer(conn, template_run_id, signal_id, revision, mode="UPDATE")
        ctx = answer_context(conn, run, actor, signal, request)
        key = reading.reuse_key(ctx, signal["text"], signal["file_sha256"])
        parts = reading.build_parts(signal["text"], data, signal["file_type"], ctx, uuid.uuid4().hex)
        return _release_answer_claim(conn, run, actor, key, ctx, signal, request)

    admitted = await asyncio.to_thread(db.in_tx, prepare)

    def render(conn, row, fresh, model_calls=0):
        run = db.lock_run(conn, template_run_id, mode="SHARE", statuses=("READY",))
        return dict(answer_response(conn, run, row, _saved(row)["served"]),
                    fresh=fresh, model_calls=model_calls)

    if not admitted["fresh"]:
        return await asyncio.to_thread(db.in_tx, render, admitted["row"], False)
    client, reader_started = None, False
    started = time.monotonic()
    try:
        client = genai_client()
        reader_started = True
        result = await reading.read(parts, client=client, model_id=ctx.model_id, settings=config.READING)
    except asyncio.CancelledError:
        await asyncio.shield(asyncio.to_thread(db.in_tx, lambda conn: _interrupt_answer(conn,
            db.lock_run(conn, template_run_id, mode="UPDATE", statuses=("READY",)), admitted["row"])))
        raise
    except Exception:
        # A construction failure precedes any provider work. Once the reader
        # starts, an escaped failure has unknown usage rather than zero calls.
        result = reading.ReadResult(None, "error", dict(model_id=ctx.model_id,
            model_calls=None if reader_started else 0, input_tokens=None, output_tokens=None,
            thinking_tokens=None, total_tokens=None, total_ms=(time.monotonic() - started) * 1000))
    finally:
        if client is not None and callable(getattr(client.aio, "aclose", None)):
            try:
                await client.aio.aclose()
            except Exception:
                pass  # cleanup cannot discard the retained result or usage
    row = await asyncio.to_thread(db.in_tx, lambda conn: finish(conn,
        db.lock_run(conn, template_run_id, mode="UPDATE", statuses=("READY",)),
        admitted["row"], result, ctx, signal["text"]))
    return await asyncio.to_thread(db.in_tx, render, row, True, result.usage.get("model_calls"))


def _answer_conflicts(signal, values):
    out = []
    for lot, parts in (values["pending_counts"] or {}).items():
        for part, value in parts.items():
            typed = signal["fields"].get("counts", {}).get(lot, {}).get(part)
            if typed is not None and value is not None and typed != value:
                out.append({"field": f"counts.{lot}.{part}", "typed": typed, "read": value})
    for lot, value in (values["printed_dates"] or {}).items():
        typed = signal["fields"].get("dates", {}).get(lot)
        if typed is not None and typed != value:
            out.append({"field": f"dates.{lot}", "typed": typed, "read": value})
    return out


def _interrupt_answer(conn, run, row):
    db.run(conn, "UPDATE readings SET status='INTERRUPTED', reason='interrupted', finished_at=clock_timestamp() "
                "WHERE run_id=:r AND reading_id=:id AND status='GENERATING' AND attempt=:attempt",
           r=run["run_id"], id=row["reading_id"], attempt=row["attempt"])


def _answer_history(conn, run, row, validated):
    if row["purpose"] != "ANSWER":
        return False, False
    request = signals._request(conn, run["run_id"], row["request_id"])
    current = signals._signal(conn, run["run_id"], request["answer_signal_id"])
    historical = (request["state"] == "CONFIRMED" or request["answer_signal_id"] != row["signal_id"]
                  or current is None or current["revision"] != row["signal_revision"])
    if request["state"] != "CONFIRMED":
        return historical, False
    from api import cases, facts
    values = reading.answer_values(validated.get("rows", []), list(request["lot_ids"]))
    compared = []
    for lot, parts in (values["pending_counts"] or {}).items():
        if all(parts.get(part) is not None for part in signals.PARTS):
            compared.append((lot, "stock_count", sum(parts[part] for part in signals.PARTS)))
    for lot, date in (values["printed_dates"] or {}).items():
        compared.append((lot, "expiry", cases.day_of(run, date)))
    differs = False
    for lot, kind, value in compared:
        confirmed = facts.in_force(conn, run["run_id"], lot, kind)
        differs = differs or (confirmed is not None and confirmed["value"] != value)
    return historical, differs
