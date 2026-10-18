"""Persisted brief contracts (09-07/09-10): real Runner, SQL and SSE.

Helpers here are ordinary test functions, not replacement jobs or a second
fixture framework. Production imports stay inside functions for RED collection.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.agent.conftest import drain, sse
from tests.agent.test_model import response

FALLBACK = "Fallback · Gemini unavailable · figures from the engine"


def install(fake_model, monkeypatch, script, *, gate=None):
    from agent import jobs, model
    fake = fake_model(script, barrier=gate)
    # ai_on is the job's availability seam; GCP stays off and the socket/client
    # sentinels from 09-03 remain installed throughout the real job/Runner.
    monkeypatch.setattr(jobs, "ai_on", lambda: True)
    monkeypatch.setattr(model, "llm", lambda: fake)
    return fake


def workspace(client):
    from tests.api.conftest import as_seat, fresh
    from tests.api.test_caps import session_of
    from tests.api.test_evidence import story
    session = session_of(client)
    run = fresh(session["run_id"])
    case = story(run)
    if session["branch"] != case["source_branch"]:
        as_seat(client, case["source_branch"])
        session = session_of(client)
    return session, run, case


def rows(run_id, table):
    from api import db
    assert table in {"ai_outputs", "ai_usage", "chat_requests", "tool_results", "evidence_drafts",
                     "evidence_requests", "reported_facts", "signals", "reservations", "decisions",
                     "cases", "recommendations", "activity_log", "lots", "truth_lots"}
    with db.tx() as conn:
        found = db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r", r=run_id)
    return sorted(found, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def business_state(run_id):
    return {table: rows(run_id, table) for table in ("cases", "recommendations", "evidence_requests",
        "reported_facts", "signals", "reservations", "decisions", "lots", "truth_lots")}


def events(result):
    assert result.status_code == 200, result.text
    assert result.headers["content-type"].startswith("text/event-stream")
    return sse(result.content)


def terminal(frames, name):
    found = [e["data"] for e in frames if e["event"] == name]
    assert len(found) == 1, frames
    return found[0]


def make_brief(client, case):
    return terminal(events(client.get(f"/api/cases/{case['case_id']}/brief")), "brief")


def test_first_brief_is_checked_persisted_and_second_read_makes_zero_calls(client, fake_model, monkeypatch):
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Review the recorded options.")])
    _, run, case = workspace(client)
    before = business_state(run["run_id"])
    first = make_brief(client, case)
    second = make_brief(client, case)
    from tests.api.test_workflow import version
    from tests.api.verify_helpers import shown
    assert first["output_id"] == second["output_id"] and first["text"] == "Review the recorded options."
    assert (first["provenance"], first["rec_version"], first["plan_key"]) == (
        "LIVE", version(run, case["case_id"]), shown(run, case["case_id"]))
    assert len(fake.requests) == 2 and fake.script == []
    stored, = rows(run["run_id"], "ai_outputs")
    assert (stored["status"], stored["text"], stored["output_id"]) == ("CURRENT", first["text"], first["output_id"])
    audit, = rows(run["run_id"], "tool_results")
    assert (audit["scope"], audit["case_id"], audit["rec_version"], audit["plan_key"], audit["tool"]) == (
        "CASE", case["case_id"], first["rec_version"], first["plan_key"], "get_case")
    assert audit["duration_ms"] >= 0 and audit["result"]
    assert business_state(run["run_id"]) == before


def test_numbers_are_checked_before_any_complete_brief_is_published_or_stored(client, fake_model, monkeypatch):
    install(fake_model, monkeypatch, [response("The benefit is USD 123456789.12.")])
    _, run, case = workspace(client)
    brief = make_brief(client, case)
    assert brief["text"] == "⚠ number removed: not traceable to a tool result"
    assert "123456789.12" not in json.dumps(brief)
    saved, = rows(run["run_id"], "ai_outputs")
    assert saved["text"] == brief["text"] and saved["checks"] == brief["checks"]


@pytest.mark.parametrize("last", ["empty", "error", "timeout", "tool_limit"])
def test_failed_generations_use_engine_fallback_and_retain_attempt(client, fake_model, monkeypatch, last):
    from agent import model
    error = {"empty": response(""), "error": RuntimeError("offline provider failure"),
             "timeout": TimeoutError("offline deadline"), "tool_limit": model.LimitReached("8 tool calls")}[last]
    fake = install(fake_model, monkeypatch, [error])
    _, run, case = workspace(client)
    from api import cases, db
    with db.tx() as conn:
        summary = cases.detail(conn, run, case["case_id"])["summary"]
    frames = events(client.get(f"/api/cases/{case['case_id']}/brief"))
    initial = terminal(frames, "summary")
    final = terminal(frames, "brief")
    assert initial["summary"] == summary
    assert final["provenance"] == "FALLBACK" and final["label"] == FALLBACK
    assert final["summary"] == summary and len(fake.requests) == 1
    usage, = rows(run["run_id"], "ai_usage")
    assert usage["status"] == "FALLBACK" and usage["reason"]
    assert not any(r["status"] == "GENERATING" for r in rows(run["run_id"], "ai_outputs"))


def test_assistant_revision_alone_does_not_invalidate_a_saved_brief(client, fake_model, monkeypatch):
    from api import db
    fake = install(fake_model, monkeypatch, [response("Saved explanation.")])
    _, run, case = workspace(client)
    first = make_brief(client, case)
    with db.tx() as conn:
        db.run(conn, "UPDATE ai_outputs SET assistant_version = :v WHERE run_id = :r AND output_id = :o",
               r=run["run_id"], o=first["output_id"], v="older-release-sentinel")
    second = make_brief(client, case)
    assert second["output_id"] == first["output_id"] and second["text"] == "Saved explanation."
    assert second["assistant_version"] == "older-release-sentinel" and len(fake.requests) == 1


@pytest.mark.parametrize("change", ["selection", "version", "reset"])
def test_late_completion_is_historical_after_identity_changes(client, fake_model, monkeypatch, barrier, change):
    from agent import jobs
    from api import cases, db, selection, workflow
    from tests.api.conftest import fresh, key, seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call, version
    from tests.api.verify_helpers import shown
    _, run, case = workspace(client)
    cid = case["case_id"]
    old_version, old_plan = version(run, cid), shown(run, cid)

    async def exercise():
        gate = barrier()
        fake = install(fake_model, monkeypatch, [response("Late explanation.")], gate=gate)
        task = asyncio.create_task(drain(jobs.brief(run["run_id"], cid)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 2)
            if change == "selection":
                chosen = feasible_others(run, cid)[0]
                call(run, selection.select, seat(case["source_branch"]), cid, old_version, chosen)
                assert version(run, cid) == old_version and shown(run, cid) == chosen != old_plan
            elif change == "version":
                # Real re-evaluation via the human assumption service, not a
                # fabricated recommendation/version row.
                call(run, workflow.adopt, seat(case["source_branch"]), cid,
                     [{"kind": "demand_multiplier", "branch": case["source_branch"],
                       "day_from": run["day"], "day_to": run["day"], "value": .9}], "Manager scenario")
                assert version(run, cid) != old_version
            else:
                reset = client.post("/api/demo/reset", json={}, headers=key())
                assert reset.status_code == 200 and client.get("/api/session").json()["run_id"] != run["run_id"]
            gate.release.set()
            completed = await asyncio.wait_for(task, 3)
            assert len(fake.requests) == 1
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        saved, = rows(run["run_id"], "ai_outputs")
        assert (saved["rec_version"], saved["plan_key"], saved["status"]) == (old_version, old_plan, "HISTORICAL")
        assert not any(e["type"] == "brief" and e.get("status") == "CURRENT" for e in completed)
        if change != "reset":
            with db.tx() as conn:
                current = jobs.current_brief(conn, run["run_id"], cid, version(run, cid), shown(run, cid))
            assert current is None
        else:
            assert fresh(run["run_id"])["status"] == "RETIRED"
    asyncio.run(exercise())


@pytest.mark.parametrize("word_count", [99, 100, 101])
def test_whole_brief_word_bound_never_truncates_an_overlong_candidate(client, fake_model, monkeypatch, word_count):
    text = " ".join(["Review"] * word_count)
    fake = install(fake_model, monkeypatch, [response(text)])
    _, run, case = workspace(client)
    final = make_brief(client, case)
    saved, = rows(run["run_id"], "ai_outputs")
    usage, = rows(run["run_id"], "ai_usage")
    if word_count <= 100:
        assert final["text"] == text and saved["text"] == text
        assert final["status"] == "CURRENT" and usage["status"] == "DONE"
    else:
        assert final["provenance"] == "FALLBACK" and final["label"] == FALLBACK
        assert final["text"] == final["summary"] and saved["text"] == ""
        assert usage["status"] == "FALLBACK" and usage["reason"] == "brief_too_long"
    assert len(fake.requests) == 1 and usage["model_calls"] == 1


@pytest.mark.parametrize("field", ["rec_version", "plan_key"])
def test_final_brief_check_rejects_a_source_from_another_tuple_even_with_its_attempt_id(
        client, fake_model, monkeypatch, field):
    from agent import jobs, tools
    from api import db
    session, run, case = workspace(client)
    job = jobs.prepare_brief(run["run_id"], case["case_id"], session)
    foreign = {**job["ctx"], field: "foreign-tuple-sentinel"}
    rid = db.in_tx(lambda conn: tools.record(conn, foreign, "get_case", {}, {"net": 123456789.12}, 0))
    fake = install(fake_model, monkeypatch, [response(f"The benefit is [[{rid}:net]].")])
    final = asyncio.run(drain(jobs.run_job(job)))[-1]
    assert final["text"] == "⚠ number removed: not traceable to a tool result"
    assert final["checks"] == {"claims": 0, "removed": 1}
    assert final["refs"] == [] and final["sources"] == []
    assert len(rows(run["run_id"], "tool_results")) == 1 and len(fake.requests) == 1


def test_live_brief_runs_fact_checks_and_checked_draft_without_business_authority(client, fake_model, monkeypatch):
    from tests.api.test_evidence import phantom
    session, run, case = workspace(client)
    fact = phantom(run, case["case_id"])
    before = business_state(run["run_id"])
    text = ("Engine recommendation: review the recorded plan. Manager selection: none. "
            "A stock check draft is ready for the source manager to review.")
    fake = install(fake_model, monkeypatch, [
        response(calls=[("get_case", {}), ("get_fact_checks", {})]),
        response(calls=[("draft_evidence_request", {"fact_id": fact["fact_id"],
                   "ask_text": "Please check shelf and backroom stock."})]),
        response(text),
    ])
    final = make_brief(client, case)
    assert final["text"] == text and final["status"] == "CURRENT" and final["provenance"] == "LIVE"
    assert final["model_id"] and final["assistant_version"] and final["checks"] == {"claims": 0, "removed": 0}
    assert {s["tool"] for s in final["sources"]} == {"get_case", "get_fact_checks", "draft_evidence_request"}
    audit = rows(run["run_id"], "tool_results")
    assert {s["result_id"] for s in final["sources"]} == {a["result_id"] for a in audit}
    assert all((a["rec_version"], a["plan_key"]) == (final["rec_version"], final["plan_key"]) for a in audit)
    draft, = rows(run["run_id"], "evidence_drafts")
    assert draft["state"] == "DRAFT" and draft["request_id"] is None
    assert draft["original_text"] == "Please check shelf and backroom stock."
    assert business_state(run["run_id"]) == before
    assert final["usage"]["model_calls"] == 3 and final["usage"]["tool_calls"] == 3
    assert 0 <= final["usage"]["first_text_ms"] <= final["usage"]["total_ms"]
    assert make_brief(client, case)["output_id"] == final["output_id"] and len(fake.requests) == 3


@pytest.mark.parametrize("prefix_words", [91, 92])
def test_brief_word_bound_also_checks_the_complete_checked_text(client, fake_model, monkeypatch, prefix_words):
    text = "Review. " * prefix_words + "123456789.12."
    install(fake_model, monkeypatch, [response(text)])
    _, run, case = workspace(client)
    final = make_brief(client, case)
    checked = "Review. " * prefix_words + "⚠ number removed: not traceable to a tool result"
    if len(checked.split()) <= 100:
        assert final["text"] == checked and final["status"] == "CURRENT"
    else:
        assert final["provenance"] == "FALLBACK" and final["text"] == final["summary"]
        saved, = rows(run["run_id"], "ai_outputs")
        assert saved["text"] == "" and saved["reason"] == "brief_too_long"


def test_failed_live_brief_reads_reuse_exact_fallback_without_implicit_retry(client, fake_model, monkeypatch):
    from api import caps, cases, db
    from tests.agent.test_usage import own_ai_count
    from tests.api.test_caps import counts
    fake = install(fake_model, monkeypatch, [RuntimeError("offline failure"), RuntimeError("unwanted second attempt")])
    session, run, case = workspace(client)
    before = counts(caps.GLOBAL, "ai"), own_ai_count(session)
    with db.tx() as conn:
        summary = cases.detail(conn, run, case["case_id"])["summary"]
    first = make_brief(client, case)
    second = make_brief(client, case)
    assert second["output_id"] == first["output_id"], "Reading Fallback must not start another paid attempt"
    assert first["text"] == second["text"] == first["summary"] == second["summary"] == summary
    assert first["label"] == second["label"] == FALLBACK
    assert len(fake.requests) == 1 and len(rows(run["run_id"], "ai_usage")) == 1
    assert (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == (before[0] + 1, before[1] + 1)
    assert second["load_model_calls"] == 0 and second["usage"]["model_calls"] == 1


def test_opening_successful_baseline_reports_zero_load_calls_and_preserves_generation_usage(client, fake_model, monkeypatch):
    from agent import jobs
    from api import caps
    from tests.agent.test_usage import own_ai_count
    from tests.api.test_caps import counts
    session, run, case = workspace(client)
    fake = install(fake_model, monkeypatch, [response("A checked baseline.")])
    before = counts(caps.GLOBAL, "ai"), own_ai_count(session)
    made = asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))[-1]
    opened = make_brief(client, case)
    assert opened["output_id"] == made["output_id"] and opened["provenance"] == "PRECOMPUTED"
    assert opened["load_model_calls"] == 0 and opened["usage"]["model_calls"] == 1
    assert len(fake.requests) == 1 and (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == before
    usage, = rows(run["run_id"], "ai_usage")
    assert usage["precomputed"] is True


@pytest.mark.parametrize("live_fails", [False, True])
def test_failed_baseline_admits_one_live_attempt_and_tabs_then_read_its_terminal_result(
        client, fake_model, monkeypatch, barrier, live_fails):
    from agent import jobs
    from api import caps
    from tests.agent.test_usage import own_ai_count
    from tests.api.test_caps import counts
    session, run, case = workspace(client)
    before = counts(caps.GLOBAL, "ai"), own_ai_count(session)
    baseline = install(fake_model, monkeypatch, [RuntimeError("offline baseline failure")])
    failed = asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))[-1]
    assert failed["provenance"] == "FALLBACK" and len(baseline.requests) == 1
    assert (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == before

    async def exercise():
        gate = barrier()
        turn = RuntimeError("offline live failure") if live_fails else response("Recovered live explanation.")
        fake = install(fake_model, monkeypatch, [turn, turn], gate=gate)
        task = asyncio.create_task(drain(jobs.brief(run["run_id"], case["case_id"], session)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 2)
            second = await drain(jobs.brief(run["run_id"], case["case_id"], session))
            assert second[-1]["type"] == "generating" and len(fake.requests) == 1
            assert (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == (before[0] + 1, before[1] + 1)
            gate.release.set()
            completed = (await asyncio.wait_for(task, 3))[-1]
            reopened = (await drain(jobs.brief(run["run_id"], case["case_id"], session)))[-1]
            assert reopened["output_id"] == completed["output_id"] != failed["output_id"]
            assert reopened["provenance"] == ("FALLBACK" if live_fails else "LIVE")
            assert len(fake.requests) == 1 and reopened["load_model_calls"] == 0
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
    usage = rows(run["run_id"], "ai_usage")
    assert len(usage) == 2 and sorted(u["precomputed"] for u in usage) == [False, True]
    assert all(u["status"] != "RUNNING" for u in usage)


@pytest.mark.parametrize("outcome", ["whitespace", "ninth_tool", "producer_timeout"])
def test_real_producer_failures_never_publish_partial_brief_and_keep_exact_summary(
        client, fake_model, monkeypatch, barrier, outcome):
    from api import cases, config, db
    if outcome == "producer_timeout":
        monkeypatch.setitem(config.SETTINGS["assistant"], "seconds", .05)
    script = ([response(" \n\t ")] if outcome == "whitespace" else
              [[response("Unpublishable partial prose.", partial=True),
                response(calls=[("get_case", {})] * 9)]] if outcome == "ninth_tool" else
              [response("Unpublishable late prose.")])
    fake = install(fake_model, monkeypatch, script, gate=barrier() if outcome == "producer_timeout" else None)
    _, run, case = workspace(client)
    with db.tx() as conn:
        summary = cases.detail(conn, run, case["case_id"])["summary"]
    frames = events(client.get(f"/api/cases/{case['case_id']}/brief"))
    final = terminal(frames, "brief")
    assert final["provenance"] == "FALLBACK" and final["label"] == FALLBACK
    assert final["summary"] == final["text"] == summary
    assert not any("Unpublishable" in json.dumps(e) for e in frames)
    output, = rows(run["run_id"], "ai_outputs")
    usage, = rows(run["run_id"], "ai_usage")
    assert output["status"] == usage["status"] == "FALLBACK" and output["text"] == "" and usage["reason"]
    assert len(fake.requests) == usage["model_calls"] == 1
    if outcome == "ninth_tool":
        assert usage["tool_calls"] == 8 and usage["reason"] == "tool_limit"
    if outcome == "producer_timeout":
        assert usage["reason"] == "timeout"


def test_stale_failure_uses_the_final_locked_engine_summary_and_retains_historical_attempt(
        client, fake_model, monkeypatch, barrier):
    from agent import jobs
    from api import cases, db, selection
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call, version
    session, run, case = workspace(client)
    cid = case["case_id"]

    async def exercise():
        gate = barrier()
        install(fake_model, monkeypatch, [RuntimeError("offline stale failure")], gate=gate)
        task = asyncio.create_task(drain(jobs.brief(run["run_id"], cid, session)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 2)
            call(run, selection.select, seat(case["source_branch"]), cid, version(run, cid), feasible_others(run, cid)[0])
            with db.tx() as conn:
                summary = cases.detail(conn, run, cid)["summary"]
            gate.release.set()
            completed = await asyncio.wait_for(task, 3)
            final = completed[-1]
            assert final["summary"] == final["text"] == summary, "Fallback must use the final authoritative engine summary"
            assert final["status"] == "HISTORICAL" and final["label"] == FALLBACK
            saved, = rows(run["run_id"], "ai_outputs")
            usage, = rows(run["run_id"], "ai_usage")
            assert saved["status"] == "HISTORICAL" and saved["text"] == "" and usage["status"] == "FALLBACK"
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_late_older_completion_cannot_replace_a_newer_selected_plan_brief(client, fake_model, monkeypatch, barrier):
    from agent import jobs
    from api import db, selection
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call, version
    from tests.api.verify_helpers import shown
    session, run, case = workspace(client)
    cid = case["case_id"]

    async def exercise():
        gate = barrier()
        older = install(fake_model, monkeypatch, [response("Older explanation.")], gate=gate)
        task = asyncio.create_task(drain(jobs.brief(run["run_id"], cid, session)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 2)
            call(run, selection.select, seat(case["source_branch"]), cid, version(run, cid), feasible_others(run, cid)[0])
            newer = install(fake_model, monkeypatch, [response("Newer selected explanation.")])
            current = (await drain(jobs.brief(run["run_id"], cid, session)))[-1]
            assert current["status"] == "CURRENT" and current["plan_key"] == shown(run, cid)
            gate.release.set()
            late = (await asyncio.wait_for(task, 3))[-1]
            assert late["status"] == "HISTORICAL" and late["output_id"] != current["output_id"]
            with db.tx() as conn:
                saved = jobs.current_brief(conn, run["run_id"], cid, version(run, cid), shown(run, cid))
            assert saved["output_id"] == current["output_id"] and saved["text"] == "Newer selected explanation."
            assert len(older.requests) == len(newer.requests) == 1
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_interrupted_brief_can_retry_and_late_cleanup_never_overwrites_the_replacement(client, fake_model, monkeypatch):
    from agent import jobs
    session, run, case = workspace(client)
    fake = install(fake_model, monkeypatch, [response("Replacement explanation.")])
    first = jobs.prepare_brief(run["run_id"], case["case_id"], session)

    async def close_before_production():
        stream = jobs.run_job(first)
        assert (await anext(stream))["type"] == "summary"
        await stream.aclose()
    asyncio.run(close_before_production())
    interrupted, = rows(run["run_id"], "ai_outputs")
    assert interrupted["status"] == "INTERRUPTED" and interrupted["text"] == ""
    replacement = make_brief(client, case)
    jobs.abort_unfinished(first)
    saved = {r["output_id"]: r for r in rows(run["run_id"], "ai_outputs")}
    assert saved[replacement["output_id"]]["status"] == "CURRENT" and replacement["text"] == "Replacement explanation."
    assert saved[first["output_id"]] == interrupted and len(fake.requests) == 1
    assert sorted(u["status"] for u in rows(run["run_id"], "ai_usage")) == ["DONE", "INTERRUPTED"]


def test_brief_history_timestamp_ties_use_output_id_and_remain_read_only(client, fake_model, monkeypatch):
    from agent import config, jobs
    from api import db
    session, run, case = workspace(client)
    fake = install(fake_model, monkeypatch, [response("Earlier brief."), response("Middle brief."), response("Latest brief.")])
    for release in ("history-release-first", "history-release-second", "history-release-third"):
        monkeypatch.setattr(config, "ASSISTANT_VERSION", release)
        asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))
    with db.tx() as conn:
        db.run(conn, "UPDATE ai_outputs SET created_at = '2030-01-01T00:00:00Z' WHERE run_id = :r", r=run["run_id"])
    before = rows(run["run_id"], "ai_outputs"), rows(run["run_id"], "ai_usage")
    assert callable(getattr(jobs, "brief_history", None)), "Historical brief reads need the same stable tie ordering as current reads"
    with db.tx() as conn:
        historical = jobs.brief_history(conn, db.lock_run(conn, run["run_id"]), session, case["case_id"])
    expected = sorted((r["output_id"] for r in before[0] if r["status"] == "HISTORICAL"), reverse=True)
    assert len(historical) == 2 and [r["output_id"] for r in historical] == expected
    assert all(r["status"] == "HISTORICAL" and r["read_only"] is True for r in historical)
    assert (rows(run["run_id"], "ai_outputs"), rows(run["run_id"], "ai_usage")) == before and len(fake.requests) == 3


def test_usage_tool_durations_retain_only_the_final_attempts_exact_tuple(client, fake_model, monkeypatch):
    from agent import jobs, tools
    from api import db
    session, run, case = workspace(client)
    job = jobs.prepare_brief(run["run_id"], case["case_id"], session)
    foreign = {**job["ctx"], "plan_key": "foreign-plan-sentinel"}
    db.in_tx(lambda conn: tools.record(conn, foreign, "get_case", {}, {"net": 123456789.12}, 7))
    install(fake_model, monkeypatch, [response("No source data was used.")])
    final = asyncio.run(drain(jobs.run_job(job)))[-1]
    assert final["sources"] == [] and final["usage"]["tool_durations"] == []
    usage, = rows(run["run_id"], "ai_usage")
    assert usage["tool_durations"] == [] and len(rows(run["run_id"], "tool_results")) == 1


def test_final_key_is_read_after_the_finalizer_waits_for_the_case_lock(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agent import jobs
    from api import cases, db, selection
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import version
    session, run, case = workspace(client)
    job = jobs.prepare_brief(run["run_id"], case["case_id"], session)
    chosen = feasible_others(run, case["case_id"])[0]
    main_thread, waiting = threading.get_ident(), threading.Event()
    original = cases.case_row

    def watched(conn, run_id, case_id, lock=False):
        if lock and threading.get_ident() != main_thread:
            waiting.set()
        return original(conn, run_id, case_id, lock=lock)
    monkeypatch.setattr(cases, "case_row", watched)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.tx() as conn:
            locked = db.lock_run(conn, run["run_id"])
            selection.select(conn, locked, seat(case["source_branch"]), case["case_id"], version(run, case["case_id"]), chosen)
            pending = pool.submit(jobs._finish, job, {"model_calls": 0, "tool_calls": 0}, "Queued explanation.")
            assert waiting.wait(timeout=5), "Finalization never reached its case lock"
            assert not pending.done()
        final = pending.result(timeout=5)
    assert final["status"] == "HISTORICAL" and final["plan_key"] == job["ctx"]["plan_key"] != chosen
    saved, = rows(run["run_id"], "ai_outputs")
    assert saved["status"] == "HISTORICAL" and saved["text"] == "Queued explanation."
