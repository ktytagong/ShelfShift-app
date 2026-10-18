"""Retained usage, cap refusals and the fail-closed paid-test entry guard."""
from __future__ import annotations

from datetime import timedelta

import pytest

from tests.agent.test_ask import ask
from tests.agent.test_brief import events, install, make_brief, rows, terminal, workspace
from tests.agent.test_model import response


def own_ai_count(session):
    from api import db
    with db.tx() as conn:
        return db.one(conn, "SELECT coalesce(sum(count), 0) AS n FROM usage_counts "
                           "WHERE scope = :s AND kind = 'ai'", s=session["session_id"])["n"]


def test_usage_totals_are_exact_and_a_successful_attempt_is_retained_once(client, fake_model, monkeypatch):
    from agent import model
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})], usage={
        "prompt_token_count": 11, "candidates_token_count": 3, "thoughts_token_count": 2, "total_token_count": 16}),
        response("Review the recorded case.", usage={"prompt_token_count": 17, "candidates_token_count": 7,
            "thoughts_token_count": 1, "total_token_count": 25})])
    _, run, case = workspace(client)
    answer = terminal(events(ask(client, case)), "answer")
    usage, = rows(run["run_id"], "ai_usage")
    wanted = {"model_calls": 2, "tool_calls": 1, "input_tokens": 28, "output_tokens": 10,
              "thinking_tokens": 3, "total_tokens": 41, "retries": 0}
    assert {k: usage[k] for k in wanted} == wanted
    assert {k: answer["usage"][k] for k in wanted} == wanted
    assert usage["status"] == "DONE" and usage["model_id"] == model.model_id()
    assert usage["location"] and 0 <= usage["first_text_ms"] <= usage["total_ms"]
    assert usage["request_id"] == rows(run["run_id"], "chat_requests")[0]["request_id"]
    assert len(fake.requests) == 2
    client.get(f"/api/cases/{case['case_id']}/chat")
    assert rows(run["run_id"], "ai_usage") == [usage] and len(fake.requests) == 2


@pytest.mark.parametrize("outcome", ["success", "empty", "provider_error", "timeout", "tool_limit"])
def test_missing_provider_metrics_stay_null_in_every_retained_attempt(client, fake_model, monkeypatch, outcome):
    from agent import model
    script = {"success": response("No numeric claims."), "empty": response(""),
              "provider_error": RuntimeError("offline failure"), "timeout": TimeoutError("offline deadline"),
              "tool_limit": model.LimitReached("8 tool calls")}
    fake = install(fake_model, monkeypatch, [script[outcome]])
    _, run, case = workspace(client)
    final = terminal(events(ask(client, case)), "answer")
    usage, = rows(run["run_id"], "ai_usage")
    assert [usage[k] for k in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens")] == [None]*4
    assert usage["model_calls"] == 1 and usage["tool_calls"] == 0 and len(fake.requests) == 1
    assert usage["status"] == ("DONE" if outcome == "success" else "FALLBACK")
    assert usage["total_ms"] >= 0 and usage["location"]
    assert final["provenance"] == ("LIVE" if outcome == "success" else "FALLBACK")
    if outcome != "success":
        assert usage["reason"] and final["label"] == "Fallback · Gemini unavailable · figures from the engine"


def test_hourly_cap_blocks_only_new_ai_and_manual_requests_still_work(client, fake_model, monkeypatch):
    from api import caps, config, db
    from tests.api.conftest import key
    from tests.api.test_evidence import phantom
    from tests.api.test_workflow import version
    fake = install(fake_model, monkeypatch, [response("Saved explanation.")])
    session, run, case = workspace(client)
    monkeypatch.setitem(config.SESSION_CAPS, "ai", (1, "hour"))
    saved = make_brief(client, case)
    refused = ask(client, case)
    assert (refused.status_code, refused.json()["error"], refused.json()["kind"], refused.json()["scope"]) == (
        429, "cap_reached", "ai", "session")
    reset = caps.window_start("hour") + timedelta(hours=1)
    assert refused.json()["message"] == ("You have reached this session's limit (1 AI requests per hour). "
                                         f"It resets at {reset:%H:%M} Bangkok time.")
    assert make_brief(client, case)["output_id"] == saved["output_id"] and len(fake.requests) == 1
    assert len(rows(run["run_id"], "ai_usage")) == 1 and rows(run["run_id"], "chat_requests") == []
    fact = phantom(run, case["case_id"])
    sent = client.post(f"/api/cases/{case['case_id']}/requests", headers=key(), json={
        "rec_version": version(run, case["case_id"]), "fact_id": fact["fact_id"], "message": "Human manual check."})
    assert sent.status_code == 200 and len(rows(run["run_id"], "evidence_requests")) == 1
    with db.tx() as conn:
        own = db.one(conn, "SELECT count FROM usage_counts WHERE scope = :s AND kind = 'ai' AND window_start = :w",
                     s=session["session_id"], w=caps.window_start("hour"))
    assert own["count"] == 1 and len(fake.requests) == 1


def test_readiness_reports_configured_model_without_constructing_or_calling_it(client, fake_model, monkeypatch):
    from agent import model
    fake = install(fake_model, monkeypatch, [])
    from api import config as api_config  # the deploy sets one GEMINI_MODEL for both (11-HANDOFF H-12)
    monkeypatch.setattr(api_config, "GEMINI_MODEL", model.model_id())
    ready = client.get("/api/readyz")
    assert ready.status_code == 200
    assert ready.json()["model_id"] == model.model_id() and ready.json()["db"] == "ok"
    assert fake.requests == []


@pytest.mark.parametrize("missing", ["GCP", "SHELFSHIFT_LIVE_TESTS", "GEMINI_MODEL", "VERTEX_LOCATION", "SHELFSHIFT_LIVE_RUN_ID"])
def test_live_entry_guard_rejects_missing_authorization_or_configuration_before_any_io(monkeypatch, no_network, missing):
    from types import SimpleNamespace
    from tests.agent.test_live import pytest_generate_tests as live_preflight, require_live
    env = {"GCP": "on", "SHELFSHIFT_LIVE_TESTS": "user-run", "GEMINI_MODEL": "configured-model-sentinel",
           "VERTEX_LOCATION": "configured-location-sentinel", "SHELFSHIFT_LIVE_RUN_ID": "disposable-workspace-sentinel"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(pytest.UsageError, match=missing):
        require_live()
    with pytest.raises(pytest.UsageError, match=missing):
        live_preflight(SimpleNamespace(config=SimpleNamespace(getoption=lambda name: "live")))
    assert no_network.attempts == []


def test_brief_attempt_records_observed_calls_and_keeps_unreported_tokens_null(client, fake_model, monkeypatch):
    fake = install(fake_model, monkeypatch, [response("An observed explanation.")])
    _, run, case = workspace(client)
    final = make_brief(client, case)
    usage, = rows(run["run_id"], "ai_usage")
    assert (usage["model_calls"], usage["provider_attempts"], usage["retries"]) == (1, 1, 0)
    assert final["usage"]["provider_attempts"] == 1 and len(fake.requests) == 1
    assert usage["input_tokens"] is None and usage["total_tokens"] is None


def test_precomputed_brief_measures_usage_without_spending_reviewer_counters(client, fake_model, monkeypatch):
    import asyncio
    from agent import jobs
    from api import caps
    from tests.agent.conftest import drain
    from tests.api.test_caps import counts
    session, run, case = workspace(client)
    before = counts(caps.GLOBAL, "ai"), own_ai_count(session)
    fake = install(fake_model, monkeypatch, [response("Saved baseline explanation.", usage={
        "prompt_token_count": 11, "candidates_token_count": 7, "total_token_count": 18})])
    final = asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))[-1]
    usage, = rows(run["run_id"], "ai_usage")
    assert usage["status"] == "DONE" and usage["total_tokens"] == 18 and len(fake.requests) == 1
    assert usage["provider_attempts"] == 1 and final["provenance"] == "PRECOMPUTED"
    assert (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == before
    assert make_brief(client, case)["output_id"] == final["output_id"] and len(fake.requests) == 1


def test_caps_use_one_database_clock_sample_and_reset_at_bangkok_midnight(client, monkeypatch):
    from datetime import datetime
    from api import caps, config, db
    from api.errors import ApiError
    session, _, _ = workspace(client)
    moment = datetime.fromisoformat("2032-05-01T23:59:59+07:00")
    sampled = []
    original = db.one
    def database_clock(conn, sql, **params):
        if sql == "SELECT clock_timestamp() AS now":
            sampled.append(moment)
            return {"now": moment}
        return original(conn, sql, **params)
    monkeypatch.setattr(db, "one", database_clock)
    monkeypatch.setitem(config.GLOBAL_CAPS, "ai", 1)
    db.in_tx(lambda conn: caps.spend(conn, session["session_id"], "ai"))
    with pytest.raises(ApiError, match="today's limit|daily limit"):
        db.in_tx(lambda conn: caps.spend(conn, session["session_id"], "ai"))
    moment = datetime.fromisoformat("2032-05-02T00:00:00+07:00")
    db.in_tx(lambda conn: caps.spend(conn, session["session_id"], "ai"))
    assert len(sampled) == 3, "Both cap windows must use one database-clock sample per spend"
    with db.tx() as conn:
        days = db.rows(conn, "SELECT window_start, count FROM usage_counts WHERE scope = :s AND kind = 'ai' "
                            "AND window_start >= :start ORDER BY window_start", s=caps.GLOBAL,
                       start=datetime.fromisoformat("2032-05-01T00:00:00+07:00"))
    assert [(row["window_start"].astimezone(config.CAP_TZ).isoformat(), row["count"]) for row in days] == [
        ("2032-05-01T00:00:00+07:00", 1), ("2032-05-02T00:00:00+07:00", 1)]


@pytest.mark.parametrize("turns", [1, 2])
def test_observed_sdk_retry_stays_inside_one_admission_and_one_cap_spend(client, monkeypatch, turns):
    import httpx
    from agent import jobs, model
    from api import caps
    from google.adk.models import BaseLlm
    from google.genai import _api_client, errors
    from tenacity import AsyncRetrying
    from tests.api.test_caps import counts
    attempts, delays = [], []
    def provider(request):
        turn = request.headers["x-model-turn"]
        attempts.append(turn)
        return httpx.Response(429 if attempts.count(turn) == 1 else 200, json={})
    class RetryingModel(BaseLlm):
        model: str = "offline-retry-script"
        observes_provider_attempts: bool = True
        turn: int = 0
        async def generate_content_async(self, llm_request, stream=False):
            self.turn += 1
            transport = model.AttemptTransport(httpx.MockTransport(provider))
            async with httpx.AsyncClient(transport=transport) as http:
                async def attempt():
                    result = await http.post("https://offline.invalid/model:generateContent", json={},
                                             headers={"x-model-turn": str(self.turn)})
                    if result.status_code != 200:
                        raise errors.ClientError(result.status_code, {"error": {"code": result.status_code}})
                async def delay(seconds):
                    delays.append(seconds)
                options = llm_request.config.http_options
                policy = options.retry_options if options else model.build_model("offline", "offline", None).retry_options
                await AsyncRetrying(**_api_client.retry_args(policy), sleep=delay)(attempt)
            if self.turn < turns:
                yield response(calls=[("get_case", {})], usage={"prompt_token_count": 11,
                    "candidates_token_count": 3, "total_token_count": 14})
            else:
                yield response("One checked response after a bounded retry.")
    monkeypatch.setattr(jobs, "ai_on", lambda: True)
    monkeypatch.setattr(model, "llm", RetryingModel)
    session, run, case = workspace(client)
    before = counts(caps.GLOBAL, "ai"), own_ai_count(session)
    result = make_brief(client, case)
    usage, = rows(run["run_id"], "ai_usage")
    assert len(attempts) == turns + 1 and len(delays) == 1 and .5 <= delays[0] <= 1
    assert (usage["model_calls"], usage["provider_attempts"], usage["retries"]) == (turns, turns + 1, 1)
    assert result["usage"]["provider_attempts"] == turns + 1
    assert usage["status"] == ("DONE" if turns == 1 else "FALLBACK")
    assert (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == (before[0] + 1, before[1] + 1)
    assert len(rows(run["run_id"], "ai_outputs")) == 1
    if turns == 2:
        assert usage["input_tokens"] == 11 and usage["output_tokens"] == 3 and usage["total_tokens"] == 14
        assert usage["tool_calls"] == 1 and len(usage["tool_durations"]) == 1
        assert rows(run["run_id"], "ai_outputs")[0]["text"] == "" and result["provenance"] == "FALLBACK"


def test_explicit_baseline_reuse_requires_model_and_assistant_revision_but_page_reads_do_not(
        client, fake_model, monkeypatch):
    import asyncio
    from agent import config, jobs
    from api import caps
    from tests.agent.conftest import drain
    from tests.api.test_caps import counts
    session, run, case = workspace(client)
    before = counts(caps.GLOBAL, "ai"), own_ai_count(session)
    fake = install(fake_model, monkeypatch, [response("Original baseline."), response("Revised baseline.")])
    async def baseline():
        return (await drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))[-1]
    first = asyncio.run(baseline())
    monkeypatch.setattr(config, "ASSISTANT_VERSION", "new-assistant-revision")
    assert make_brief(client, case)["output_id"] == first["output_id"] and len(fake.requests) == 1
    revised = asyncio.run(baseline())
    assert revised["output_id"] != first["output_id"] and len(fake.requests) == 2
    assert revised["assistant_version"] == "new-assistant-revision" and revised["status"] == "CURRENT"
    assert make_brief(client, case)["output_id"] == revised["output_id"] and len(fake.requests) == 2
    assert {row["status"] for row in rows(run["run_id"], "ai_outputs")} == {"CURRENT", "HISTORICAL"}
    assert len(rows(run["run_id"], "ai_usage")) == 2
    assert (counts(caps.GLOBAL, "ai"), own_ai_count(session)) == before


def test_opening_metrics_are_separate_from_retained_precomputed_generation(client, fake_model, monkeypatch):
    import asyncio
    from agent import jobs
    from tests.agent.conftest import drain
    fake = install(fake_model, monkeypatch, [response("Stored baseline explanation.", usage={
        "prompt_token_count": 19, "candidates_token_count": 7, "total_token_count": 26})])
    session, run, case = workspace(client)
    generated = asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))[-1]
    retained = rows(run["run_id"], "ai_usage")
    opened = make_brief(client, case)
    assert opened.get("load_total_ms") is not None, "The opening needs its own measured latency"
    assert opened["load_total_ms"] >= 0 and opened["load_model_calls"] == 0
    assert opened["usage"] == generated["usage"] and opened["usage"]["model_calls"] == 1
    assert opened.get("usage_state") == "retained"
    assert opened["usage"].get("missing") == ["thinking_tokens", "tool_use_prompt_tokens"]
    assert opened["usage"]["first_text_ms"] <= opened["usage"]["total_ms"]
    assert rows(run["run_id"], "ai_usage") == retained and len(fake.requests) == 1


@pytest.mark.parametrize("outcome,reason", [("success", None), ("provider_error", "unavailable"),
    ("timeout", "timeout"), ("tool_limit", "tool_limit")])
def test_output_reads_keep_exact_terminal_metrics_and_safe_categories(client, fake_model, monkeypatch, outcome, reason):
    from agent import model
    from tests.agent.test_brief import business_state
    final = {"success": response("Checked explanation."), "provider_error": RuntimeError("private provider diagnostic"),
             "timeout": TimeoutError("private endpoint"), "tool_limit": model.LimitReached("8 tool calls")}[outcome]
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})], usage={
        "prompt_token_count": 11, "candidates_token_count": 3, "thoughts_token_count": 2, "total_token_count": 16}), final])
    _, run, case = workspace(client)
    before = business_state(run["run_id"])
    result = terminal(events(ask(client, case)), "answer")
    retained, = rows(run["run_id"], "ai_usage")
    audit, = rows(run["run_id"], "tool_results")
    read = client.get(f"/api/cases/{case['case_id']}/chat").json()["turns"][0]
    assert read.get("usage_state") == "retained", "History must identify retained, rather than newly generated, metrics"
    assert read["usage"] == result["usage"]
    assert read["usage"]["status"] == ("DONE" if outcome == "success" else "FALLBACK")
    assert read["usage"]["reason"] == reason
    assert read["usage"]["model_calls"] == read["usage"]["provider_attempts"] == 2
    assert read["usage"]["retries"] == 0 and read["usage"]["tool_calls"] == 1
    assert read["usage"]["tool_durations"] == [{"result_id": audit["result_id"], "tool": "get_case",
                                               "duration_ms": audit["duration_ms"]}]
    assert read["usage"]["tool_durations_missing"] == 0
    assert read["usage"]["missing"] == [k for k in ("model_calls", "provider_attempts", "tool_calls", "retries",
        "input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "tool_use_prompt_tokens",
        "first_text_ms", "total_ms") if retained[k] is None]
    assert "private" not in str(read["usage"]) and business_state(run["run_id"]) == before
    assert rows(run["run_id"], "ai_usage") == [retained] and len(fake.requests) == 2


def test_interrupted_request_replay_exposes_retained_usage_without_partial_text_or_calls(
        client, fake_model, monkeypatch, barrier):
    import asyncio
    import uuid
    from agent import jobs
    from tests.api.conftest import key
    from tests.agent.test_brief import business_state
    session, run, case = workspace(client)
    gate = barrier()
    fake = install(fake_model, monkeypatch, [response("Private incomplete candidate.")], gate=gate)
    request_id, headers = uuid.uuid4().hex, key()
    from api.app import body_hash
    job = jobs.prepare_answer(run["run_id"], case["case_id"], session, request_id, "Private question.",
                              headers["Idempotency-Key"], body_hash({"question": "Private question.", "request_id": request_id}))
    before = business_state(run["run_id"])
    async def interrupt():
        stream = jobs.run_job(job)
        assert (await anext(stream))["type"] == "summary"
        pending = asyncio.create_task(anext(stream))
        await asyncio.wait_for(gate.entered.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
    asyncio.run(interrupt())
    retained, = rows(run["run_id"], "ai_usage")
    replay = terminal(events(ask(client, case, "Private question.", request_id=request_id, headers=headers)), "interrupted")
    assert replay.get("usage_state") == "retained"
    assert replay["text"] == "" and replay["status"] == replay["usage"]["status"] == "INTERRUPTED"
    assert replay["usage"]["reason"] == "interrupted" and replay["usage"]["model_calls"] == 1
    assert replay["usage"]["first_text_ms"] is None and "first_text_ms" in replay["usage"]["missing"]
    assert replay["usage"]["total_ms"] >= 0 and replay["usage"]["total_tokens"] is None
    assert rows(run["run_id"], "ai_usage") == [retained] and len(fake.requests) == 1
    assert business_state(run["run_id"]) == before and not gate.release.is_set()


def test_legacy_missing_usage_is_explicit_and_does_not_regenerate(client, fake_model, monkeypatch):
    from api import db
    fake = install(fake_model, monkeypatch, [response("Stored explanation.")])
    _, run, case = workspace(client)
    final = make_brief(client, case)
    with db.tx() as conn:
        db.run(conn, "DELETE FROM ai_usage WHERE run_id = :r AND output_id = :o", r=run["run_id"], o=final["output_id"])
    opened = make_brief(client, case)
    assert opened.get("usage_state") == "missing", "Legacy absence must be explicit, never manufactured zero usage"
    assert opened["usage"] is None and opened["load_model_calls"] == 0 and len(fake.requests) == 1
    assert rows(run["run_id"], "ai_usage") == []


def test_usage_projection_cannot_cross_saved_scope_or_expose_extra_fields(client, fake_model, monkeypatch):
    from api import db
    from tests.api.conftest import as_seat
    fake = install(fake_model, monkeypatch, [response("Private explanation.")])
    _, run, case = workspace(client)
    answer = terminal(events(ask(client, case, "Private question sentinel.")), "answer")
    path = f"/api/cases/{case['case_id']}"
    with db.tx() as conn:
        db.run(conn, "UPDATE ai_usage SET plan_key = 'foreign-plan' WHERE run_id = :r AND output_id = :o",
               r=run["run_id"], o=answer["output_id"])
    own = client.get(path + "/chat").json()["turns"][0]
    assert own.get("usage_state") == "missing" and own["usage"] is None
    as_seat(client, case["source_branch"])
    assert client.get(path + "/chat").json()["turns"] == []
    denied = client.get(path + "/trace", params={"output_id": answer["output_id"]})
    assert denied.status_code == 404 and "Private" not in denied.text and len(fake.requests) == 1


def test_aggregate_percentiles_preserve_missing_counts_and_never_export_private_text():
    from agent import usage
    attempts = [{"kind": "ANSWER", "precomputed": False, "status": "DONE", "model_calls": 2,
                 "first_text_ms": value, "total_ms": value * 2, "tool_calls": 1,
                 "tool_durations": [{"tool": "get_case", "duration_ms": value / 2,
                                     "result_id": "private-source-id", "result": "private stock"}],
                 "question": "private question", "model_id": "private-resource", "location": "private-endpoint",
                 "run_id": "private-workspace", "session_id": "private-person"} for value in range(1, 21)]
    attempts.append({"kind": "ANSWER", "precomputed": False, "status": "INTERRUPTED", "reason": "interrupted",
                     "total_ms": 100, "first_text_ms": None, "tool_calls": 1, "tool_durations": []})
    report_fn = getattr(usage, "benchmark", None)
    assert callable(report_fn), "Retained attempt metrics need a metadata-only percentile report"
    report = report_fn(attempts, measurement_source="local-fake")
    assert report["measurement_source"] == "local-fake" and report["sample_count"] == 21
    assert report["percentile_method"] == "nearest-rank"
    group, = report["groups"]
    assert group["kind"] == "ANSWER" and group["precomputed"] is False
    assert group["statuses"] == {"DONE": 20, "INTERRUPTED": 1}
    assert group["metrics"]["first_text_ms"] == {"samples": 20, "missing": 1, "p50": 10, "p95": 19,
                                                "known_total": 210}
    assert group["metrics"]["total_ms"]["p95"] == 40
    assert group["metrics"]["input_tokens"] == {"samples": 0, "missing": 21, "p50": None, "p95": None,
                                               "known_total": None}
    assert group["tool_duration_ms"]["samples"] == 20 and group["tool_durations_missing"] == 1
    assert "private" not in str(report) and "question" not in str(report)
    empty = report_fn([], measurement_source="user-observed-live")
    assert empty["sample_count"] == 0 and empty["groups"] == []
    assert empty["measurement_source"] == "user-observed-live"
    with pytest.raises(ValueError, match="measurement_source"):
        report_fn(attempts, measurement_source="mixed-or-unspecified")


@pytest.mark.parametrize("action", ["request_evidence", "approve", "commit", "adopt", "confirm_stock"])
def test_measured_model_attempt_cannot_perform_human_business_actions(client, fake_model, monkeypatch, action):
    from tests.agent.test_brief import business_state
    fake = install(fake_model, monkeypatch, [response(calls=[(action, {})])])
    _, run, case = workspace(client)
    before = business_state(run["run_id"])
    result = terminal(events(ask(client, case, "Send, approve, commit, adopt and confirm stock for me.")), "answer")
    assert result["provenance"] == "FALLBACK" and result["usage"]["status"] == "FALLBACK"
    assert business_state(run["run_id"]) == before and rows(run["run_id"], "evidence_drafts") == []
    assert len(fake.requests) == 1 and len(rows(run["run_id"], "ai_usage")) == 1


def test_usage_metadata_projection_removes_diagnostics_and_nested_private_fields(client, fake_model, monkeypatch):
    from api import db
    fake = install(fake_model, monkeypatch, [response("A checked answer.")])
    _, run, case = workspace(client)
    answer = terminal(events(ask(client, case)), "answer")
    with db.tx() as conn:
        db.run(conn, "UPDATE ai_usage SET tool_calls = 1, tool_durations = CAST(:dur AS jsonb), "
                     "reason = :why, location = :loc WHERE run_id = :r AND output_id = :o",
               r=run["run_id"], o=answer["output_id"], loc="projects/private-resource/locations/private-location",
               why="private-provider-diagnostic", dur=db.js([{"tool": "get_case", "result_id": "Rabc", "duration_ms": 3,
                    "question": "private question", "result": {"credential": "private-secret"}}]))
    measured = client.get(f"/api/cases/{case['case_id']}/chat").json()["turns"][0]["usage"]
    assert "private" not in str(measured) and measured["location"] is None
    assert measured["reason"] == "unavailable" and "location" in measured["missing"]
    assert measured["tool_durations"] == [{"tool": "get_case", "result_id": "Rabc", "duration_ms": 3}]
    assert len(fake.requests) == 1


def test_readme_records_fake_measurements_pending_live_and_release_order():
    from pathlib import Path
    import re
    from agent import config
    full = Path("README.md").read_text()
    # The store app's dataset section (05.2) ends the README with its required attribution link; the assistant guide's
    # own wording rules apply to everything before it (09-17 integration).
    readme = full.split("\n## Data\n")[0]
    assert "Public sales dataset · Simulated operational scenario" in readme, "The public assistant guide must retain the data honesty mark"
    assert "local fake" in readme.lower() and "paid vertex rerun: pending" in readme.lower()
    assert "p50" in readme and "p95" in readme and "nearest-rank" in readme
    assert "Gemini drafts evidence requests; managers send them" in readme
    assert "physical stock, food safety or reasoning" in readme
    assert "8 tool calls" in readme and "40 s" in readme and "LOW" in readme
    assert "user decision" in readme and "first-open" in readme
    assert "InMemorySessionService" in readme and "FastAPI" in readme and "Vertex" in readme and "Cloud Run" in readme
    assert readme.index("--no-activate") < readme.index("-m agent.briefs") < readme.index("-m api.sandbox activate")
    settings = config.load()
    for value in (settings.max_output_tokens, settings.max_request_tokens):
        assert f"{value:,}" in readme
    assert "8,192" in readme and "65,536" in readme and "safety margin" in readme.lower()
    assert not re.search(r"\blive\b", full, re.I)  # the store app's word rule (05.2 D-04); "deliveries" is fine
    assert "2 passed, 2 failed" in readme and "16,959" in readme
    assert "user-run" in readme.lower() and "09-23" in readme
    assert "--measurement-source" in readme and "-m agent.usage" in readme
    assert not re.search(r"https?://|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:US)?\$\s?\d|\bThai\b|\.run\.app", readme, re.I)


def test_usage_report_cli_reads_retained_rows_without_generation_or_private_exports(
        client, fake_model, monkeypatch, tmp_path, capsys):
    import json
    from agent import config, usage
    from api import config as api_config
    from tests.agent.test_brief import business_state
    fake = install(fake_model, monkeypatch, [response("Private saved answer.")])
    session, run, case = workspace(client)
    terminal(events(ask(client, case, "Private saved question.")), "answer")
    before, paid = business_state(run["run_id"]), own_ai_count(session)
    monkeypatch.setitem(api_config.SETTINGS["assistant"], "max_output_tokens", 512)
    monkeypatch.setitem(api_config.SETTINGS["assistant"], "max_request_tokens", 2048)
    main = getattr(usage, "main", None)
    assert callable(main), "A local read-only report must consume retained rows"
    output = tmp_path / "usage-report.json"
    assert main([run["run_id"], "--measurement-source", "local-fake", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["measurement_source"] == "local-fake" and report["sample_count"] == 1
    assert report["limits"] == {"max_output_tokens": 512, "max_request_tokens": 2048, "max_tool_calls": 8,
        "max_llm_calls": 10, "seconds": 40, "thinking_level": "LOW",
        "session_ai_requests": {"limit": api_config.SESSION_CAPS["ai"][0], "window": api_config.SESSION_CAPS["ai"][1]}}
    assert report["groups"][0]["metrics"]["input_tokens"]["missing"] == 1
    assert "private" not in output.read_text().lower() and run["run_id"] not in output.read_text()
    assert session["session_id"] not in output.read_text() and "global" not in report["limits"]
    assert business_state(run["run_id"]) == before and own_ai_count(session) == paid and len(fake.requests) == 1
    assert main([run["run_id"], "--measurement-source", "user-observed-live"]) == 0
    live_report = json.loads(capsys.readouterr().out.strip())
    assert live_report["measurement_source"] == "user-observed-live"  # explicit operator label, never inferred
    assert live_report["sample_count"] == report["sample_count"]


def test_local_usage_benchmark(client, fake_model, monkeypatch, no_paid_calls):
    import asyncio
    import json
    import os
    import platform
    from datetime import datetime, timezone
    from pathlib import Path
    from agent import jobs, usage
    from tests.agent.conftest import drain
    destination = os.environ.get("P09_USAGE_BENCHMARK_REPORT")
    if not destination:
        pytest.skip("Local timing measurements require the owner-checked shared slot wrapper")
    sample_count = 20
    script = [response("Stored baseline explanation.")]
    for _ in range(sample_count):
        script += [response(calls=[("get_case", {})]), response("Review the recorded engine options.")]
    fake = install(fake_model, monkeypatch, script)
    session, run, case = workspace(client)
    generated = asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session, precomputed=True)))[-1]
    loads = []
    for _ in range(sample_count):
        opened = make_brief(client, case)
        assert opened["output_id"] == generated["output_id"] and opened["load_model_calls"] == 0
        loads.append(opened["load_total_ms"])
    for _ in range(sample_count):
        result = terminal(events(ask(client, case)), "answer")
        assert result["usage"]["status"] == "DONE" and result["provenance"] == "LIVE"
    report = usage.benchmark(rows(run["run_id"], "ai_usage"), measurement_source="local-fake")
    report.update(limits=usage.configured_limits(), observed_at=datetime.now(timezone.utc).isoformat(),
        machine={"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version(),
                 "database": "local PostgreSQL"},
        precomputed_openings={"sample_count": sample_count, "load_model_calls": 0,
                              "read_preparation_ms": usage.distribution(loads, samples=sample_count)},
        scripted_model_invocations=len(fake.requests), paid_or_remote_calls=len(no_paid_calls.attempts))
    assert report["sample_count"] == sample_count + 1 and len(fake.requests) == 2 * sample_count + 1
    assert no_paid_calls.attempts == [] and fake.script == []
    assert all(group["statuses"] == {"DONE": group["sample_count"]} for group in report["groups"])
    assert all(group["metrics"]["thinking_tokens"]["samples"] == 0 for group in report["groups"])
    Path(destination).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("LOCAL_FAKE_USAGE_BENCHMARK", json.dumps(report, sort_keys=True))
