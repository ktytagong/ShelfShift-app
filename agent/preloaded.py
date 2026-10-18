"""Explicit release preparation for simulated material; no work at import/read.

Each item uses its complete public service. Model work has no open transaction;
only a completed inactive template can supply rows to this shared baseline.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing

from api import db, signals
from . import impact, readings_job


def _items(conn, run_id):
    run = db.lock_run(conn, run_id, mode="SHARE", statuses=("READY",))
    if run["kind"] != "TEMPLATE" or run["failure"]:
        raise ValueError("The preloaded pass requires a completed inactive template (READY).")
    reports = [dict(row, purpose="summary") for row in db.rows(conn,
        "SELECT signal_id, revision, kind, report_id, request_id FROM signals "
        "WHERE run_id=:r AND provenance=:p AND state='READ' AND report_id IS NOT NULL "
        "AND kind NOT IN ('CONDITION', 'NOT_RELEVANT') ORDER BY created_at, signal_id",
        r=run_id, p=signals.SIMULATED)]
    answers = [dict(row, purpose="answer") for row in db.rows(conn,
        "SELECT s.signal_id, s.revision, s.kind, s.report_id, s.request_id FROM signals s "
        "JOIN evidence_requests q ON q.run_id=s.run_id AND q.answer_signal_id=s.signal_id "
        "WHERE s.run_id=:r AND s.provenance=:p AND s.kind IN ('STOCK_COUNT', 'EXPIRY') "
        "AND (length(trim(s.text))>0 OR s.file_uri IS NOT NULL) ORDER BY s.created_at, s.signal_id",
        r=run_id, p=signals.SIMULATED)]
    return reports + answers


def _record(conn, run_id, item, result):
    run = db.lock_run(conn, run_id, mode="UPDATE", statuses=("READY",))
    db.log(conn, run_id, run["day"], "preloaded_precompute", None,
        signal_id=item["signal_id"], revision=item["revision"], purpose=item["purpose"], **result)


async def _write(run_id, item):
    if item["purpose"] == "answer":
        final = await readings_job.precompute_answer(run_id, item["signal_id"], item["revision"])
        # The public answer response stays unchanged. Retained metrics belong
        # to this run's persisted reading, even when the invocation reused it.
        row = await asyncio.to_thread(db.in_tx, lambda conn: db.one(conn,
            "SELECT usage FROM readings WHERE run_id=:r AND reading_id=:id",
            r=run_id, id=final["reading_id"]))
        return dict(final, usage=row["usage"])
    final = None
    # summarize refreshes the deterministic preview before claiming its key.
    async with aclosing(impact.summarize(run_id, item["signal_id"],
            revision=item["revision"], precomputed=True)) as stream:
        async for event in stream:
            if event["type"] == "summary":
                final = event
    if final is None:
        raise ValueError("No terminal impact summary was published")
    return final


def _usage(rows):
    names = ("model_calls", "tool_calls", "provider_attempts", "retries", "input_tokens",
             "output_tokens", "thinking_tokens", "total_tokens")
    # Original usage remains on each item. Aggregate only this invocation's
    # new attempts; unknown provider work remains null, never fabricated zero.
    return {name: sum(row[name] for row in rows)
            if rows and all(row.get(name) is not None for row in rows) else (0 if not rows else None)
            for name in names}


async def precompute(template_run_id, *, budget=None):
    """Await the release pass; return counts, retained item usage and new usage.

    Optional synchronous budget admission receives a copy of the item before
    any preview, service admission or provider work. False/exception stops the
    remaining items. Per-item generation failures are retained and nonfatal.
    """
    items = await asyncio.to_thread(db.in_tx, _items, template_run_id)
    counts = dict(eligible=len(items), written=0, skipped=0, failed=0, stopped=0)
    attempts, records = [], []
    for index, item in enumerate(items):
        if budget is not None:
            try:
                permitted = budget(dict(item))
            except Exception as exc:
                permitted, reason = False, type(exc).__name__
            else:
                reason = "budget_refused"
            if permitted is False:
                counts["stopped"] = len(items) - index
                records.append(dict(signal_id=item["signal_id"], outcome="stopped", reason=reason))
                break
        try:
            final = await _write(template_run_id, item)
            ok = final["provenance"] == "PRECOMPUTED" and final["status"] in ("DONE", "CURRENT")
            reused = ok and final["served"] in ("precomputed", "reuse") and not final.get("fresh", False)
            outcome = "skipped" if reused else "written" if ok else "failed"
            retained = final.get("usage") or {}
            current_calls = final["model_calls"]
            if not reused:
                attempts.append({**retained, "model_calls": current_calls})
            result = dict(outcome=outcome, reading_id=final.get("reading_id"),
                output_id=final.get("output_id"), reason=final.get("reason"),
                model_calls=current_calls, usage=retained)
        except Exception as exc:
            outcome = "failed"
            attempts.append({"model_calls": None})
            # Keep only a diagnostic class; no raw provider/user strings.
            result = dict(outcome=outcome, reason=type(exc).__name__, model_calls=None)
        counts[outcome] += 1
        await asyncio.to_thread(db.in_tx, _record, template_run_id, item, result)
        records.append(dict(signal_id=item["signal_id"], **result))
    return dict(**counts, usage=_usage(attempts), items=records)
