"""Admission and streaming races use 09-03's real ASGI disconnect client."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.agent.conftest import SSEParser
from tests.agent.test_brief import events, install, make_brief, rows, terminal, workspace
from tests.agent.test_model import response


def cookie_headers(client):
    return [(b"cookie", "; ".join(f"{k}={v}" for k, v in client.cookies.items()).encode())]


async def receive_until(stream, name):
    parser = SSEParser()
    seen = []
    while True:
        message = await stream.next_message()
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        elif message["type"] == "http.response.body":
            seen.extend(parser.feed(message.get("body", b"")))
            if any(e["event"] == name for e in seen):
                return seen
            assert message.get("more_body", False), (name, seen)


def test_claim_and_cap_are_committed_before_model_and_second_tab_does_not_spend(client, fake_model, monkeypatch, barrier, asgi_client):
    from api import caps, db
    from api.app import app
    from tests.api.test_caps import counts
    _, run, case = workspace(client)
    paid = counts(caps.GLOBAL, "ai")

    async def exercise():
        gate = barrier()
        fake = install(fake_model, monkeypatch, [response("Only admitted answer.")], gate=gate)
        async with asgi_client(app, f"/api/cases/{case['case_id']}/brief", headers=cookie_headers(client)) as first:
            await asyncio.wait_for(gate.entered.wait(), 2)
            claims = rows(run["run_id"], "ai_outputs")
            assert len(claims) == 1 and claims[0]["status"] == "GENERATING"
            assert counts(caps.GLOBAL, "ai") == paid + 1 and len(rows(run["run_id"], "ai_usage")) == 1
            with db.tx() as conn:
                db.run(conn, "SET LOCAL lock_timeout = '200ms'")
                assert db.lock_run(conn, run["run_id"])["run_id"] == run["run_id"]
            # Buffered TestClient is in its own loop/thread, while the first
            # producer is paused at the real model boundary.
            other = events(client.get(f"/api/cases/{case['case_id']}/brief"))
            generating = terminal(other, "generating")
            assert generating["rec_version"] == claims[0]["rec_version"] and generating["plan_key"] == claims[0]["plan_key"]
            assert len(fake.requests) == 1 and counts(caps.GLOBAL, "ai") == paid + 1
            gate.release.set()
            final = terminal(await receive_until(first, "brief"), "brief")
            assert final["text"] == "Only admitted answer."
    asyncio.run(exercise())


def test_refused_cap_rolls_back_claim_and_retry_can_claim_once(client, fake_model, monkeypatch):
    from api import caps, config
    from tests.api.test_caps import counts
    fake = install(fake_model, monkeypatch, [response("After the limit is restored.")])
    _, run, case = workspace(client)
    today = counts(caps.GLOBAL, "ai")
    monkeypatch.setitem(config.GLOBAL_CAPS, "ai", today)
    denied = client.get(f"/api/cases/{case['case_id']}/brief")
    assert denied.status_code == 429 and denied.json() == {"error": "cap_reached", "kind": "ai", "scope": "daily",
        "message": "Today's AI limit is reached. It resets at 00:00 Bangkok time."}
    assert fake.requests == [] and rows(run["run_id"], "ai_usage") == []
    assert not any(r["status"] == "GENERATING" for r in rows(run["run_id"], "ai_outputs"))
    assert counts(caps.GLOBAL, "ai") == today
    monkeypatch.setitem(config.GLOBAL_CAPS, "ai", today + 1)
    assert make_brief(client, case)["text"] == "After the limit is restored."
    assert len(fake.requests) == 1 and counts(caps.GLOBAL, "ai") == today + 1


def test_disconnect_retains_interrupted_attempt_and_tool_audit_without_partial_answer(client, fake_model, monkeypatch, barrier, asgi_client):
    from api.app import app
    from tests.api.conftest import key
    _, run, case = workspace(client)

    async def exercise():
        gate = barrier()
        original_wait, turn = gate.wait, 0

        async def pause_second_turn():
            nonlocal turn
            turn += 1
            if turn == 2:
                await original_wait()

        # Keep the real fake BaseLlm and real Runner: only choose which model
        # boundary the proven harness barrier pauses.
        monkeypatch.setattr(gate, "wait", pause_second_turn)
        fake = install(fake_model, monkeypatch, [[response("Unpublishable partial sentinel", partial=True),
            response(calls=[("get_case", {})])], response("Complete answer sentinel")], gate=gate)
        rid = uuid.uuid4().hex
        headers = cookie_headers(client) + [(b"content-type", b"application/json"),
            (b"idempotency-key", key()["Idempotency-Key"].encode())]
        async with asgi_client(app, f"/api/cases/{case['case_id']}/chat", method="POST",
            body=json.dumps({"request_id": rid, "question": "Investigate this case."}).encode(), headers=headers) as stream:
            seen = await receive_until(stream, "tool_result")
            await asyncio.wait_for(gate.entered.wait(), 2)
            assert len(fake.requests) == 2 and not gate.release.is_set()
            progress = [e["data"] for e in seen if e["event"] in ("tool", "tool_result")]
            assert [e["tool"] for e in progress] == ["get_case", "get_case"]
            assert [e["seq"] for e in progress] == sorted(e["seq"] for e in progress)
            assert all(isinstance(e["seq"], int) and e["seq"] >= 0 for e in progress)
            assert progress[-1]["duration_ms"] >= 0
            await stream.disconnect()
        attempt, = rows(run["run_id"], "chat_requests")
        assert (attempt["request_id"], attempt["status"]) == (rid, "INTERRUPTED")
        audit, = rows(run["run_id"], "tool_results")
        assert (audit["request_id"], audit["tool"]) == (rid, "get_case")
        assert not any(e["event"] == "answer" for e in seen)
        assert all("sentinel" not in r["text"] for r in rows(run["run_id"], "ai_outputs"))
        usage, = rows(run["run_id"], "ai_usage")
        assert usage["status"] == "INTERRUPTED" and usage["tool_calls"] == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("params", [{}, {"output_id": "x", "request_id": "y"}])
def test_trace_requires_exactly_one_locator_and_never_calls_model(client, fake_model, monkeypatch, params):
    fake = install(fake_model, monkeypatch, [])
    _, run, case = workspace(client)
    result = client.get(f"/api/cases/{case['case_id']}/trace", params=params)
    assert (result.status_code, result.json()["error"]) == (400, "bad_request")
    assert fake.requests == [] and rows(run["run_id"], "ai_usage") == []


def test_source_read_returns_exact_saved_compact_result_and_no_model_or_thoughts(client, fake_model, monkeypatch):
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Read the recorded case.")])
    _, run, case = workspace(client)
    saved = make_brief(client, case)
    audit, = rows(run["run_id"], "tool_results")
    result = client.get(f"/api/cases/{case['case_id']}/tool-results/{audit['result_id']}")
    assert result.status_code == 200
    body = result.json()
    for field in ("result_id", "scope", "case_id", "rec_version", "plan_key", "tool", "inputs", "result", "duration_ms"):
        assert body[field] == audit[field]
    assert body["result"] == audit["result"] and body["created_at"]
    assert "thought" not in json.dumps(body).lower() and "credential" not in json.dumps(body).lower()
    trace = client.get(f"/api/cases/{case['case_id']}/trace", params={"output_id": saved["output_id"]})
    assert trace.status_code == 200 and audit["result_id"] in trace.text and len(fake.requests) == 2


def test_readiness_adds_configured_identity_without_constructing_model(schema, monkeypatch):
    from agent import model
    from api.app import app
    from fastapi.testclient import TestClient
    from api import config as api_config
    # Phase 11's readiness body wins (11-HANDOFF H-12): model_id is the deploy's GEMINI_MODEL, read at startup
    monkeypatch.setattr(api_config, "GEMINI_MODEL", "configured-readiness-sentinel")

    def forbidden():
        pytest.fail("Readiness must not construct a model")

    monkeypatch.setattr(model, "llm", forbidden)
    ready = TestClient(app).get("/api/readyz")
    assert ready.status_code == 200
    body = ready.json()
    assert (body["ok"], body["db"], body["model_id"]) == (True, "ok", "configured-readiness-sentinel")


def test_unreachable_database_readiness_is_the_paused_body_without_error_details(monkeypatch):
    """Phase 11's paused body wins (11-CONTEXT D-02; 11-HANDOFF H-12): it carries no model_id."""
    from api import ready as readiness
    from api.app import app
    from fastapi.testclient import TestClient

    def unreachable():
        raise RuntimeError("private-credential-sentinel")

    monkeypatch.setattr(readiness, "_select1", unreachable)
    monkeypatch.setitem(readiness._state, "ok", None)  # no cached answer from an earlier test
    monkeypatch.setitem(readiness._state, "fut", None)
    ready = TestClient(app).get("/api/readyz")
    assert ready.status_code == 503
    body = ready.json()
    assert (body["ok"], body["db"], body["error"]) == (False, "unreachable", "paused")
    assert "private-credential-sentinel" not in ready.text


def test_app_setup_rejects_missing_budget_before_worker_or_model(monkeypatch, no_network):
    import secrets
    from api import config
    from api.app import app, lifespan
    from data.config import ConfigError
    monkeypatch.setattr(config, "DEMO_PASSCODE", secrets.token_urlsafe(16))
    monkeypatch.setattr(config, "HQ_PASSCODE", secrets.token_urlsafe(16))
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    monkeypatch.delitem(config.SETTINGS["assistant"], "max_request_tokens")

    async def start():
        async with lifespan(app):
            pytest.fail("Invalid required assistant settings must fail setup")

    with pytest.raises(ConfigError, match="max_request_tokens"):
        asyncio.run(start())
    assert no_network.attempts == []


def test_brief_route_opens_with_engine_fallback_when_generation_is_disabled(client):
    _, run, case = workspace(client)
    result = client.get(f"/api/cases/{case['case_id']}/brief")
    assert result.status_code == 200, "First open must reach the assistant API"
    final = terminal(events(result), "brief")
    assert final["provenance"] == "FALLBACK"
    assert final["label"] == "Fallback · Gemini unavailable · figures from the engine"
    assert final["summary"] and final["checks"] == {"claims": 0, "removed": 0}


def test_brief_model_boundary_holds_no_run_lock_or_database_transaction(client, fake_model, monkeypatch, barrier):
    from agent import jobs
    from api import db
    from tests.agent.conftest import drain
    _, run, case = workspace(client)

    async def exercise():
        gate = barrier()
        install(fake_model, monkeypatch, [response("An unlocked explanation.")], gate=gate)
        pending = asyncio.create_task(drain(jobs.brief(run["run_id"], case["case_id"])))
        try:
            await asyncio.wait_for(gate.entered.wait(), 2)
            with db.tx() as conn:
                db.run(conn, "SET LOCAL lock_timeout = '200ms'")
                assert db.lock_run(conn, run["run_id"], mode="UPDATE")["run_id"] == run["run_id"]
                attempt = db.one(conn, "SELECT * FROM ai_usage WHERE run_id = :r", r=run["run_id"])
                assert attempt["status"] == "RUNNING"
            gate.release.set()
            assert (await asyncio.wait_for(pending, 3))[-1]["text"] == "An unlocked explanation."
        finally:
            gate.release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    asyncio.run(exercise())


def test_brief_scope_denial_precedes_claim_cap_and_model(client, fake_model, monkeypatch):
    from api import caps, db, refdata
    from tests.api.conftest import as_seat
    from tests.api.test_caps import counts
    fake = install(fake_model, monkeypatch, [])
    _, run, case = workspace(client)
    from tests.api.verify_helpers import shown
    from api.cases import parse_plan
    plan = parse_plan(shown(run, case["case_id"]))
    branch = next(b for b in refdata.get(run["dataset_version"]).branches if b not in {case["source_branch"], plan.dest})
    as_seat(client, branch)
    paid = counts(caps.GLOBAL, "ai")
    denied = client.get(f"/api/cases/{case['case_id']}/brief")
    assert (denied.status_code, denied.json()["error"]) == (403, "forbidden")
    assert fake.requests == [] and rows(run["run_id"], "ai_outputs") == [] and rows(run["run_id"], "ai_usage") == []
    assert rows(run["run_id"], "tool_results") == [] and counts(caps.GLOBAL, "ai") == paid


def test_closing_brief_after_initial_summary_releases_claim_and_retains_interruption(client, fake_model, monkeypatch):
    from agent import jobs
    fake = install(fake_model, monkeypatch, [])
    _, run, case = workspace(client)

    async def exercise():
        stream = jobs.brief(run["run_id"], case["case_id"])
        assert (await anext(stream))["type"] == "summary"
        await stream.aclose()
    asyncio.run(exercise())
    attempt, = rows(run["run_id"], "ai_usage")
    assert attempt["status"] == "INTERRUPTED", "An early close must not strand an admitted attempt"
    claim, = rows(run["run_id"], "ai_outputs")
    assert claim["status"] == "INTERRUPTED" and claim["text"] == "" and fake.requests == []


def test_ask_request_id_cannot_mix_case_and_personal_tool_audits(client, fake_model, monkeypatch):
    from tests.agent.test_ask import ask
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Case explanation.")])
    _, run, case = workspace(client)
    brief = make_brief(client, case)
    audit, = rows(run["run_id"], "tool_results")
    fake.script.append(response(f"Recorded stock is [[{audit['result_id']}:lot.recorded_qty]] units."))
    answer = terminal(events(ask(client, case, request_id=brief["output_id"])), "answer")
    assert answer["checks"] == {"claims": 0, "removed": 1} and answer["refs"] == [] and answer["sources"] == []
    path = f"/api/cases/{case['case_id']}/trace"
    assert client.get(path, params={"output_id": answer["output_id"]}).json() == []
    assert client.get(path, params={"output_id": brief["output_id"]}).json()[0]["result_id"] == audit["result_id"]
    retained = next(row for row in rows(run["run_id"], "ai_usage") if row["kind"] == "ANSWER")
    assert retained["tool_durations"] == [] and retained["tool_calls"] == 0


def test_ask_streams_failed_tool_state_then_checks_and_emits_one_complete_answer(
        client, fake_model, monkeypatch, barrier, asgi_client):
    from api.app import app
    from tests.agent.conftest import sse
    from tests.api.conftest import key
    _, run, case = workspace(client)

    async def exercise():
        gate = barrier()
        original_wait, turns = gate.wait, 0

        async def pause_final_turn():
            nonlocal turns
            turns += 1
            if turns == 2:
                await original_wait()

        monkeypatch.setattr(gate, "wait", pause_final_turn)
        candidate = "The expected benefit is USD 123456789.12."
        fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {}),
            ("get_forecast", {"branch": "outside-this-case"})]), [response("Partial sentinel", partial=True),
            response(candidate), response(candidate)]], gate=gate)
        rid = uuid.uuid4().hex
        headers = cookie_headers(client) + [(b"content-type", b"application/json"),
            (b"idempotency-key", key()["Idempotency-Key"].encode())]
        async with asgi_client(app, f"/api/cases/{case['case_id']}/chat", method="POST",
                body=json.dumps({"request_id": rid, "question": "Explain this case."}).encode(), headers=headers) as stream:
            first = await receive_until(stream, "tool_result")
            await asyncio.wait_for(gate.entered.wait(), 2)
            output, = rows(run["run_id"], "ai_outputs")
            assert output["status"] == "GENERATING" and output["text"] == ""
            assert rows(run["run_id"], "ai_usage")[0]["status"] == "RUNNING"
            assert not any(e["event"] == "answer" for e in first)
            gate.release.set()
            await receive_until(stream, "answer")
            await asyncio.wait_for(asyncio.shield(stream.task), 2)
        raw = b"".join(m.get("body", b"") for m in stream.messages if m["type"] == "http.response.body")
        # Parse the real wire one byte at a time: adjacent frames and the UTF-8
        # warning cannot change event order or create a second final answer.
        frames = sse([raw[i:i+1] for i in range(len(raw))])
        final = terminal(frames, "answer")
        assert final["text"] == "⚠ number removed: not traceable to a tool result"
        assert final["checks"] == {"claims": 0, "removed": 1} and final["cards"] == []
        assert b"Partial sentinel" not in raw and b"123456789.12" not in raw
        progress = [e["data"] for e in frames if e["event"] in ("tool", "tool_result")]
        failed, = [e for e in progress if e["tool"] == "get_forecast" and e.get("result_id")]
        assert failed["state"] == "Failed", "An audited tool error must never be shown as Complete"
        assert all(e["state"] == "Working" for e in progress if "result_id" not in e)
        assert all(e["state"] in ("Complete", "Failed") and e["duration_ms"] >= 0
                   for e in progress if "result_id" in e)
        checking = terminal(frames, "state")
        assert checking["state"] == "Checking numbers" and "text" not in checking
        numbered = [e["data"]["seq"] for e in frames if e["event"] in ("tool", "tool_result", "state")]
        assert numbered == sorted(set(numbered))
        assert [e["event"] for e in frames][-2:] == ["state", "answer"]
        assert final["text"] == rows(run["run_id"], "ai_outputs")[0]["text"]
        assert len(fake.requests) == 2 and rows(run["run_id"], "chat_requests")[0]["status"] == "DONE"

    asyncio.run(exercise())


def test_ask_heartbeat_changes_no_claim_spend_or_audit_and_never_reconnects(
        client, fake_model, monkeypatch, barrier, asgi_client):
    from agent import jobs
    from api import caps
    from api.app import app
    from tests.api.conftest import key
    from tests.api.test_caps import counts
    _, run, case = workspace(client)
    monkeypatch.setattr(jobs, "PING_SECONDS", .01, raising=False)

    async def exercise():
        gate = barrier()
        fake = install(fake_model, monkeypatch, [response("One complete answer.")], gate=gate)
        rid = uuid.uuid4().hex
        headers = cookie_headers(client) + [(b"content-type", b"application/json"),
            (b"idempotency-key", key()["Idempotency-Key"].encode()), (b"last-event-id", b"old-frame")]
        async with asgi_client(app, f"/api/cases/{case['case_id']}/chat", method="POST",
                body=json.dumps({"request_id": rid, "question": "Explain this case."}).encode(), headers=headers,
                timeout=.2) as stream:
            await asyncio.wait_for(gate.entered.wait(), 2)
            before = {t: rows(run["run_id"], t) for t in ("ai_outputs", "chat_requests", "ai_usage", "tool_results")}
            paid = counts(caps.GLOBAL, "ai")
            ping = False
            try:
                while not ping:
                    message = await stream.next_message()
                    ping = b": ping\n\n" in message.get("body", b"")
            except TimeoutError:
                pass
            gate.release.set()
            assert ping, "An idle live Ask must emit a comment heartbeat"
            assert {t: rows(run["run_id"], t) for t in before} == before
            assert counts(caps.GLOBAL, "ai") == paid and len(fake.requests) == 1
            await receive_until(stream, "answer")
            await asyncio.wait_for(asyncio.shield(stream.task), 2)
        start, = [m for m in stream.messages if m["type"] == "http.response.start"]
        response_headers = dict(start["headers"])
        assert b"no-store" in response_headers[b"cache-control"] and response_headers[b"x-accel-buffering"] == b"no"
        assert all(b"retry:" not in m.get("body", b"") and b"\nid:" not in m.get("body", b"")
                   for m in stream.messages)

    asyncio.run(exercise())


def test_parallel_personal_ask_tabs_share_one_committed_admission(client, fake_model, monkeypatch, barrier, asgi_client):
    from api import caps
    from api.app import app
    from tests.agent.test_ask import ask
    from tests.api.conftest import key
    from tests.api.test_caps import counts
    _, run, case = workspace(client)
    paid = counts(caps.GLOBAL, "ai")

    async def exercise():
        gate = barrier()
        fake = install(fake_model, monkeypatch, [response("Personal answer.")], gate=gate)
        rid, identity = uuid.uuid4().hex, key()
        headers = cookie_headers(client) + [(b"content-type", b"application/json"),
            (b"idempotency-key", identity["Idempotency-Key"].encode())]
        async with asgi_client(app, f"/api/cases/{case['case_id']}/chat", method="POST",
                body=json.dumps({"request_id": rid, "question": "Explain this case."}).encode(), headers=headers) as stream:
            await asyncio.wait_for(gate.entered.wait(), 2)
            retained = rows(run["run_id"], "ai_usage")
            other = events(ask(client, case, "Explain this case.", request_id=rid, headers=identity))
            assert terminal(other, "generating")["status"] == "GENERATING"
            assert rows(run["run_id"], "ai_usage") == retained
            assert len(fake.requests) == 1 and counts(caps.GLOBAL, "ai") == paid + 1
            conflict = ask(client, case, "Changed question.", request_id=rid, headers=identity)
            assert (conflict.status_code, conflict.json()["error"]) == (409, "idempotency_conflict")
            gate.release.set()
            first = terminal(await receive_until(stream, "answer"), "answer")
            await asyncio.wait_for(asyncio.shield(stream.task), 2)
        replay = terminal(events(ask(client, case, "Explain this case.", request_id=rid, headers=identity)), "answer")
        assert replay == first and len(fake.requests) == 1 and counts(caps.GLOBAL, "ai") == paid + 1
        assert len(rows(run["run_id"], "chat_requests")) == len(rows(run["run_id"], "ai_usage")) == 1

    asyncio.run(exercise())


# 09-12 cancellation probes use real ASGI cancellation and DB finalization.
@pytest.mark.parametrize("stage,change_plan", [("finalization", False), ("checking", False), ("finalization", True)])
def test_disconnect_during_finalization_keeps_reported_usage_but_publishes_no_text(
        client, fake_model, monkeypatch, asgi_client, stage, change_plan):
    import threading
    from agent import jobs, numbers
    from api import caps, selection
    from api.app import app
    from tests.agent.test_ask import ask
    from tests.api.conftest import key, seat
    from tests.api.test_caps import counts
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call
    _, run, case = workspace(client)
    entered, release = threading.Event(), threading.Event()
    if stage == "finalization":
        original = jobs._finish

        def pause(*args, **kwargs):
            if len(args) > 2 and args[2]:
                entered.set()
                release.wait(.35)
            return original(*args, **kwargs)

        monkeypatch.setattr(jobs, "_finish", pause)
    else:
        original = numbers.check

        def pause(*args, **kwargs):
            entered.set()
            release.wait(.35)
            return original(*args, **kwargs)

        monkeypatch.setattr(numbers, "check", pause)
    fake = install(fake_model, monkeypatch, [response("Unpublished candidate sentinel.", usage={
        "prompt_token_count": 17, "candidates_token_count": 7, "thoughts_token_count": 1,
        "total_token_count": 25}), response("A new explicit question can complete.")])
    paid = counts(caps.GLOBAL, "ai")

    async def exercise():
        rid, identity = uuid.uuid4().hex, key()
        headers = cookie_headers(client) + [(b"content-type", b"application/json"),
            (b"idempotency-key", identity["Idempotency-Key"].encode())]
        try:
            async with asgi_client(app, f"/api/cases/{case['case_id']}/chat", method="POST",
                    body=json.dumps({"request_id": rid, "question": "Explain this case."}).encode(), headers=headers) as stream:
                assert await asyncio.to_thread(entered.wait, 2)
                if change_plan:
                    pending = rows(run["run_id"], "ai_outputs")[0]
                    call(run, selection.select, seat(case["source_branch"]), case["case_id"],
                         pending["rec_version"], feasible_others(run, case["case_id"])[0])
                disconnect = asyncio.create_task(stream.disconnect())
                await asyncio.sleep(.02)
                release.set()
                await disconnect
        finally:
            release.set()
        output, = rows(run["run_id"], "ai_outputs")
        attempt, = rows(run["run_id"], "chat_requests")
        usage, = rows(run["run_id"], "ai_usage")
        assert (output["status"], attempt["status"], usage["status"]) == ("INTERRUPTED",)*3, (
            "Disconnect while finalization is pending must fence off candidate publication")
        assert output["text"] == "" and output["cards"] == output["refs"] == []
        assert usage["reason"] == "interrupted" and usage["finished_at"]
        assert {k: usage[k] for k in ("model_calls", "tool_calls", "input_tokens", "output_tokens",
            "thinking_tokens", "total_tokens")} == {"model_calls": 1, "tool_calls": 0, "input_tokens": 17,
                "output_tokens": 7, "thinking_tokens": 1, "total_tokens": 25}
        assert all(b"Unpublished candidate sentinel" not in m.get("body", b"") for m in stream.messages)
        assert client.get(f"/api/cases/{case['case_id']}/chat").json()["turns"] == []
        replay = events(ask(client, case, "Explain this case.", request_id=rid, headers=identity))
        interrupted = terminal(replay, "interrupted")
        assert interrupted["label"] == "Interrupted, ask again" and interrupted["text"] == ""
        assert len(fake.requests) == 1 and counts(caps.GLOBAL, "ai") == paid + 1
        retry = terminal(events(ask(client, case, "Explain this case.")), "answer")
        assert retry["request_id"] != rid and retry["text"] == "A new explicit question can complete."
        assert len(fake.requests) == 2 and counts(caps.GLOBAL, "ai") == paid + 2

    asyncio.run(exercise())


@pytest.mark.parametrize("during_tool", [False, True])
def test_disconnect_before_first_text_or_during_tool_terminalizes_before_late_audit(
        client, fake_model, monkeypatch, barrier, asgi_client, during_tool):
    import threading
    import time
    from agent import tools
    from api.app import app
    from tests.api.conftest import key
    _, run, case = workspace(client)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = tools.call

    def work(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(2), "Test must release its own late tool thread"
            return original(*args, **kwargs)
        finally:
            finished.set()

    if during_tool:
        monkeypatch.setattr(tools, "call", work)

    async def exercise():
        gate = barrier()
        first = response(calls=[("get_case", {})], usage={"prompt_token_count": 11,
            "candidates_token_count": 3, "thoughts_token_count": 2, "total_token_count": 16})
        fake = install(fake_model, monkeypatch, [first, response("Never a late answer.")],
                       gate=None if during_tool else gate)
        rid = uuid.uuid4().hex
        headers = cookie_headers(client) + [(b"content-type", b"application/json"),
            (b"idempotency-key", key()["Idempotency-Key"].encode())]
        try:
            async with asgi_client(app, f"/api/cases/{case['case_id']}/chat", method="POST",
                    body=json.dumps({"request_id": rid, "question": "Explain this case."}).encode(), headers=headers) as stream:
                if during_tool:
                    assert await asyncio.to_thread(entered.wait, 2)
                else:
                    await asyncio.wait_for(gate.entered.wait(), 2)
                began = time.monotonic()
                await stream.disconnect()
                assert time.monotonic() - began < .5, "Disconnect must not wait for an unfinished tool thread"
                attempt, = rows(run["run_id"], "chat_requests")
                usage, = rows(run["run_id"], "ai_usage")
                assert attempt["status"] == usage["status"] == "INTERRUPTED"
                assert usage["model_calls"] == 1 and usage["tool_calls"] == int(during_tool)
                assert usage["input_tokens"] == (11 if during_tool else None)
                assert rows(run["run_id"], "ai_outputs")[0]["text"] == ""
                assert rows(run["run_id"], "tool_results") == []
        finally:
            release.set()
            if during_tool:
                assert await asyncio.to_thread(finished.wait, 2)
                await asyncio.sleep(.01)
        audit = rows(run["run_id"], "tool_results")
        assert len(audit) == int(during_tool) and len(fake.requests) == 1
        if audit:
            assert audit[0]["request_id"] == rid and audit[0]["scope"] == "ASK"
            assert audit[0]["status"] == "FAILED" and audit[0]["result"]["error"] == "not_available"
        assert rows(run["run_id"], "ai_usage")[0]["status"] == "INTERRUPTED"
        assert rows(run["run_id"], "ai_outputs")[0]["text"] == ""

    asyncio.run(exercise())


def test_real_ask_deadline_is_fallback_while_response_iteration_is_paused(client, fake_model, monkeypatch, barrier):
    from contextlib import aclosing
    from agent import jobs
    from api import config
    from api.app import body_hash
    from tests.agent.conftest import drain
    from tests.api.conftest import key
    session, run, case = workspace(client)
    monkeypatch.setitem(config.SETTINGS["assistant"], "seconds", .08)

    async def exercise():
        gate = barrier()
        fake = install(fake_model, monkeypatch, [response("Unpublished late text.")], gate=gate)
        body = {"request_id": uuid.uuid4().hex, "question": "Explain this case."}
        job = jobs.prepare_answer(run["run_id"], case["case_id"], session, body["request_id"], body["question"],
                                  key()["Idempotency-Key"], body_hash(body))
        async with aclosing(jobs.run_job(job)) as stream:
            assert (await anext(stream))["type"] == "summary"
            await asyncio.wait_for(gate.entered.wait(), 2)
            await asyncio.sleep(.15)  # No response iteration or socket write owns the deadline.
            # DB finalization now runs off the loop. Await its bounded cleanup
            # without ever consuming another response event.
            async with asyncio.timeout(1):
                while rows(run["run_id"], "ai_usage")[0]["status"] == "RUNNING":
                    await asyncio.sleep(.01)
            usage, = rows(run["run_id"], "ai_usage")
            assert usage["status"] == "FALLBACK" and usage["reason"] == "timeout"
            final, = [e for e in await drain(stream) if e["type"] == "answer"]
        assert final["provenance"] == "FALLBACK" and final["label"] == jobs.FALLBACK
        assert rows(run["run_id"], "chat_requests")[0]["status"] == "DONE"
        assert rows(run["run_id"], "ai_outputs")[0]["text"] == "" and len(fake.requests) == 1

    asyncio.run(exercise())


def test_deadline_during_number_checking_cannot_publish_a_late_candidate(client, fake_model, monkeypatch):
    import time
    from agent import jobs, numbers
    from api import config
    from tests.agent.test_ask import ask
    _, run, case = workspace(client)
    monkeypatch.setitem(config.SETTINGS["assistant"], "seconds", 2)  # room to reach the check once the store app's reads are in (09-17)
    original = numbers.check
    checked = []

    def slow_check(*args, **kwargs):
        checked.append(True)
        time.sleep(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(numbers, "check", slow_check)
    fake = install(fake_model, monkeypatch, [response("Unpublished slow candidate.")])
    final = terminal(events(ask(client, case, "Explain this case.")), "answer")
    assert checked and len(fake.requests) == 1
    assert final["provenance"] == "FALLBACK" and final["label"] == jobs.FALLBACK
    output, = rows(run["run_id"], "ai_outputs")
    usage, = rows(run["run_id"], "ai_usage")
    assert output["text"] == "" and output["cards"] == output["refs"] == []
    assert usage["status"] == "FALLBACK" and usage["reason"] == "timeout"
    assert rows(run["run_id"], "chat_requests")[0]["status"] == "DONE"


# 09-13: every read resolves the retained original tuple, never a guessed ID or
# a tool invocation from a different scope that happens to share a request ID.
@pytest.mark.parametrize("field,value", [
    ("rec_version", "foreign-version"), ("plan_key", "foreign-plan"),
    ("snapshot_id", "foreign-snapshot"), ("scope", "SIGNAL"),
    ("signal_id", "foreign-signal"), ("signal_revision", 7),
    ("request_id", "foreign-request"), ("session_id", "foreign-session"),
    ("actor", {"account": "HQ", "role": "hq", "branch": None}),
])
def test_source_and_trace_refuse_audit_outside_saved_answer_tuple(client, fake_model, monkeypatch, field, value):
    from api import db
    from tests.agent.test_ask import ask
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Recorded answer.")])
    _, run, case = workspace(client)
    answer = terminal(events(ask(client, case)), "answer")
    audit, = rows(run["run_id"], "tool_results")
    with db.tx() as conn:
        assignment = "CAST(:value AS jsonb)" if field == "actor" else ":value"
        db.run(conn, f"UPDATE tool_results SET {field} = {assignment} WHERE run_id = :r AND result_id = :id",
               value=db.js(value) if field == "actor" else value, r=run["run_id"], id=audit["result_id"])
    path = f"/api/cases/{case['case_id']}"
    denied = client.get(path + "/tool-results/" + audit["result_id"])
    assert (denied.status_code, denied.json().get("error")) == (404, "not_found"), (
        "A result ID must not authorize a different saved answer tuple", field, denied.text)
    for locator in ({"output_id": answer["output_id"]}, {"request_id": answer["request_id"]}):
        trace = client.get(path + "/trace", params=locator)
        assert trace.status_code == 200 and trace.json() == [], (field, trace.text)
    assert len(fake.requests) == 2


def test_sources_redact_sensitive_legacy_fields_without_recomputing_saved_metrics(client, fake_model, monkeypatch):
    from api import db
    from tests.agent.test_brief import business_state
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Recorded case.")])
    _, run, case = workspace(client)
    make_brief(client, case)
    audit, = rows(run["run_id"], "tool_results")
    polluted = {**audit["result"], "thought_signature": "private-thought-sentinel",
                "credentials": {"api_key": "private-credential-sentinel"},
                "legacy": [{"truth_qty": 999, "hidden_demand": 888, "units": 12}]}
    with db.tx() as conn:
        db.run(conn, "UPDATE tool_results SET result = CAST(:res AS jsonb), inputs = CAST(:inputs AS jsonb) "
                     "WHERE run_id = :r AND result_id = :id", r=run["run_id"], id=audit["result_id"],
               res=db.js(polluted), inputs=db.js({"authorization": "private-auth-sentinel", "branch": case["source_branch"]}))
    before = business_state(run["run_id"]), rows(run["run_id"], "tool_results"), rows(run["run_id"], "ai_usage")
    opened = client.get(f"/api/cases/{case['case_id']}/tool-results/{audit['result_id']}")
    assert opened.status_code == 200
    body = opened.json()
    assert all(secret not in opened.text for secret in (
        "private-thought-sentinel", "private-credential-sentinel", "private-auth-sentinel", "truth_qty", "hidden_demand")), (
            "Technical detail must remove sensitive fields on the server")
    assert body["result"] == {**audit["result"], "legacy": [{"units": 12}]}
    assert body["inputs"] == {"branch": case["source_branch"]}
    assert "no-store" in opened.headers.get("cache-control", "")
    assert (business_state(run["run_id"]), rows(run["run_id"], "tool_results"), rows(run["run_id"], "ai_usage")) == before
    assert len(fake.requests) == 2


def test_source_categories_use_only_successful_recorded_data_and_keep_missing_distinct(client, fake_model, monkeypatch):
    from api import db
    from tests.agent.test_ask import ask
    _, run, case = workspace(client)
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {}),
        ("get_forecast", {"branch": case["source_branch"]}),
        ("get_inventory_and_inbound", {"branch": "outside-this-case"})]),
        response("Review the recorded case and forecast."), response("No workspace data is needed.")])
    answer = terminal(events(ask(client, case)), "answer")
    path = f"/api/cases/{case['case_id']}"
    categories = {category for item in answer["sources"] for category in item.get("categories", [])}
    assert categories == {"Engine result · this version", "Stock records", "Forecast"}, (
        "Source categories must describe successful data actually supplied", answer["sources"])
    audits = {a["tool"]: a for a in rows(run["run_id"], "tool_results")}
    forecast = next(s for s in answer["sources"] if s["tool"] == "get_forecast")
    assert forecast["forecast_run_id"] == audits["get_forecast"]["result"]["forecast_run_id"]
    assert forecast["providers"] == sorted({day["provider"] for day in audits["get_forecast"]["result"]["days"]})
    assert audits["get_inventory_and_inbound"]["result_id"] not in json.dumps(answer["sources"])
    zero = terminal(events(ask(client, case, "A question needing no workspace data.")), "answer")
    assert zero["sources"] == [] and zero["checks"] == {"claims": 0, "removed": 0}
    assert zero["sources_state"] == "none"
    missing_id = audits["get_case"]["result_id"]
    with db.tx() as conn:
        db.run(conn, "DELETE FROM tool_results WHERE run_id = :r AND result_id = :id", r=run["run_id"], id=missing_id)
    missing = client.get(path + "/tool-results/" + missing_id)
    assert (missing.status_code, missing.json()["error"]) == (404, "not_found")
    retained = next(t for t in client.get(path + "/chat").json()["turns"] if t["output_id"] == answer["output_id"])
    assert retained["sources_state"] == "partial" and retained["missing_sources"] == 1
    assert len(fake.requests) == 3


def test_history_pages_complete_checked_groups_with_stable_ties_and_read_only_old_plan(client, fake_model, monkeypatch):
    from api import db, selection
    from tests.agent.test_ask import ask
    from tests.agent.test_brief import business_state
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call
    _, run, case = workspace(client)
    changes = [{"kind": "demand_multiplier", "branch": case["source_branch"],
                "day_from": run["day"], "day_to": run["day"], "value": .9}]
    fake = install(fake_model, monkeypatch, [response(calls=[("simulate_options", {"overrides": changes})]),
        response("Earlier checked what-if."), *[response(f"Checked answer {word}.") for word in (
            "beta", "gamma", "delta", "epsilon", "zeta", "eta")]])
    first = terminal(events(ask(client, case, "Earlier question?")), "answer")
    assert len(first["cards"]) == 1
    call(run, selection.select, seat(case["source_branch"]), case["case_id"], first["rec_version"],
         feasible_others(run, case["case_id"])[0])
    saved = [first] + [terminal(events(ask(client, case, f"Question {word}?")), "answer") for word in (
        "beta", "gamma", "delta", "epsilon", "zeta", "eta")]
    with db.tx() as conn:
        db.run(conn, "UPDATE ai_outputs SET created_at = '2031-01-01T00:00:00Z' WHERE run_id = :r", r=run["run_id"])
    before = business_state(run["run_id"]), rows(run["run_id"], "ai_outputs"), rows(run["run_id"], "ai_usage")
    path, groups, pages, cursor = f"/api/cases/{case['case_id']}/chat", [], [], None
    while True:
        page = client.get(path, params={"cursor": cursor} if cursor else {})
        assert page.status_code == 200 and "no-store" in page.headers.get("cache-control", "")
        body = page.json()
        assert body["total"] == 7, "Supply the count of currently authorized completed groups"
        pages.append(len(body["turns"]))
        groups.extend(body["turns"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert len(pages) < 4, "A cursor must make progress across tied timestamps"
    ids = [g["output_id"] for g in groups]
    assert pages == [3, 3, 1] and ids == sorted(ids, reverse=True) and len(set(ids)) == 7
    by_id = {g["output_id"]: g for g in groups}
    for original in saved:
        restored = by_id[original["output_id"]]
        for field in ("text", "question", "refs", "checks", "usage", "sources", "rec_version", "plan_key"):
            assert restored[field] == original[field], field
    historical = by_id[first["output_id"]]
    assert historical["status"] == "HISTORICAL" and historical["read_only"] is True
    card, = historical["cards"]
    assert card["result"] == first["cards"][0]["result"] and card["overrides"] == changes
    assert card["read_only"] is True and card["can_adopt"] is False
    assert (business_state(run["run_id"]), rows(run["run_id"], "ai_outputs"), rows(run["run_id"], "ai_usage")) == before
    invalid = client.get(path, params={"cursor": "unknown-or-inaccessible-output"})
    assert (invalid.status_code, invalid.json()["error"]) == (400, "bad_request")
    assert len(fake.requests) == 8


def test_prior_version_source_keeps_original_metrics_and_history_never_borrows_new_summary(client, fake_model, monkeypatch):
    from api import workflow
    from tests.agent.test_ask import ask
    from tests.api.conftest import seat
    from tests.api.test_workflow import call, version
    _, run, case = workspace(client)
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Earlier checked answer.")])
    first = terminal(events(ask(client, case)), "answer")
    audit, = rows(run["run_id"], "tool_results")
    call(run, workflow.adopt, seat(case["source_branch"]), case["case_id"],
         [{"kind": "demand_multiplier", "branch": case["source_branch"], "day_from": run["day"],
           "day_to": run["day"], "value": .9}], "Human supplied scenario")
    assert version(run, case["case_id"]) != first["rec_version"]
    path = f"/api/cases/{case['case_id']}"
    prior, = client.get(path + "/chat").json()["turns"]
    assert prior["status"] == "HISTORICAL" and prior["read_only"] is True
    assert prior["text"] == first["text"] and prior["summary"] is None
    opened = client.get(path + "/tool-results/" + audit["result_id"])
    assert opened.status_code == 200 and opened.json()["result"] == audit["result"]
    assert opened.json()["rec_version"] == first["rec_version"]
    assert client.get(path + "/trace", params={"request_id": first["request_id"]}).json()[0]["result_id"] == audit["result_id"]
    assert len(fake.requests) == 2


def test_role_change_revokes_personal_history_cursors_sources_and_trace_on_every_read(client, fake_model, monkeypatch):
    from api import db
    from tests.agent.test_ask import ask
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})]), response("Role-private sentinel.")])
    session, run, case = workspace(client)
    first = terminal(events(ask(client, case)), "answer")
    audit, = rows(run["run_id"], "tool_results")
    path = f"/api/cases/{case['case_id']}"
    assert client.get(path + "/chat").json()["turns"][0]["text"] == first["text"]
    assert client.get(path + "/tool-results/" + audit["result_id"]).status_code == 200
    with db.tx() as conn:
        db.run(conn, "UPDATE sessions SET account = 'HQ', role = 'hq', branch = NULL WHERE session_id = :s",
               s=session["session_id"])
    cleared = client.get(path + "/chat")
    assert cleared.status_code == 200 and cleared.json()["turns"] == []
    assert cleared.json()["total"] == 0 and "Role-private sentinel" not in cleared.text
    cursor = client.get(path + "/chat", params={"cursor": first["output_id"]})
    assert (cursor.status_code, cursor.json()["error"]) == (400, "bad_request")
    for suffix, params in (("/tool-results/" + audit["result_id"], {}),
                           ("/trace", {"output_id": first["output_id"]}),
                           ("/trace", {"request_id": first["request_id"]})):
        denied = client.get(path + suffix, params=params)
        assert (denied.status_code, denied.json()["error"]) == (404, "not_found")
        assert "Role-private sentinel" not in denied.text and audit["result_id"] not in denied.text
    assert len(fake.requests) == 2


def test_history_omits_wrong_kind_and_mismatched_attempt_without_counting_them(client, fake_model, monkeypatch):
    from api import db
    from tests.agent.test_ask import ask
    fake = install(fake_model, monkeypatch, [response("Checked answer.")])
    _, run, case = workspace(client)
    first = terminal(events(ask(client, case)), "answer")
    path = f"/api/cases/{case['case_id']}"
    with db.tx() as conn:
        db.run(conn, "UPDATE chat_requests SET plan_key = 'foreign-plan' WHERE run_id = :r AND request_id = :q",
               r=run["run_id"], q=first["request_id"])
    history = client.get(path + "/chat")
    assert history.status_code == 200 and history.json()["turns"] == [], (
        "A completed output without its exact authorized attempt is not personal history")
    assert history.json()["total"] == 0
    for locator in ({"output_id": first["output_id"]}, {"request_id": first["request_id"]}):
        denied = client.get(path + "/trace", params=locator)
        assert (denied.status_code, denied.json()["error"]) == (404, "not_found")
    with db.tx() as conn:
        db.run(conn, "UPDATE ai_outputs SET kind = 'SIGNAL' WHERE run_id = :r AND output_id = :o",
               r=run["run_id"], o=first["output_id"])
    assert client.get(path + "/trace", params={"output_id": first["output_id"]}).status_code == 404
    assert len(fake.requests) == 1


def test_shared_activity_keeps_business_events_but_never_expands_raw_private_tool_detail(client):
    from api import db
    from tests.agent.test_drafts import setup_draft
    from tests.api.test_evidence import ask as request_evidence
    _, run, case, fact, draft = setup_draft(client)
    sent = request_evidence(run, case["case_id"], fact["fact_id"])
    with db.tx() as conn:
        db.log(conn, run["run_id"], run["day"], "future_business_event", case["case_id"],
               actor={"account": "HQ", "role": "hq", "branch": None}, note="Safe business note.")
        for nested in ({"tool_result": {"result_id": "private-source-sentinel", "result": {"units": 13}}},
                       {"detail": {"result_id": "private-source-sentinel", "inputs": {}, "result": {"units": 13}}},
                       {"thought_signature": "private-thought-sentinel"}):
            db.log(conn, run["run_id"], run["day"], "legacy_assistant_event", case["case_id"], metadata=nested)
    shared = client.get(f"/api/cases/{case['case_id']}/activity")
    assert shared.status_code == 200
    assert "private-source-sentinel" not in shared.text and "private-thought-sentinel" not in shared.text, (
        "Expanded shared metadata must not serialize raw tool results or thoughts")
    drafted, = [e for e in shared.json() if e["type"] == "evidence_drafted"]
    requested, = [e for e in shared.json() if e["type"] == "evidence_requested"]
    future, = [e for e in shared.json() if e["type"] == "future_business_event"]
    assert drafted["group"] == "Assistant" and drafted["payload"]["draft_id"] == draft["draft_id"]
    assert "original_text" not in drafted["payload"] and "request_context" not in drafted["payload"]
    assert requested["group"] == "Data" and requested["payload"]["request_id"] == sent["request_id"]
    assert future["payload"]["note"] == "Safe business note." and future["actor"]["account"] == "HQ"
