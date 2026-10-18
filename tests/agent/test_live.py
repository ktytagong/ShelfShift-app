"""USER-RUN ONLY: actual Gemini evidence, never an automated phase command.

Required evidence: configured model/location; checked final text and exact SQL
sources; model/tool/input/output/thinking/retry counts; first-text/total latency;
successful draft-only authority. Archive the pytest output and retained usage.
No live result has been observed by creating or collecting these contracts.

The exact user command is in the private phase 09-LIVE-REPORT.md. The user
loads PROJECT, GEMINI_MODEL and VERTEX_LOCATION using the deploy envfile loader,
then explicitly selects ``-m live`` with GCP=on and consent below. RUN_ID=new
rebuilds only shelfshift_assistant_test through the existing local fixtures and
creates a fresh authenticated SANDBOX per sample. An explicit sandbox ID instead
uses an already prepared workspace without rebuilding its schema.

Live measurement acquires the shared timing slot with an owner-checked token.
GCP-off collection does no setup, credential lookup, SDK import or paid call.
Unmarked cases exercise these contracts with the existing guarded fake model.

SHELFSHIFT_LIVE_* are test-only consent/workspace guards, not product settings.
The user also supplies their authorized project/credentials locally. Never put
resource IDs or credentials in the output artifact or this file.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import pytest


def require_live():
    """Fail before any SDK/API/DB import, rather than silently skip a paid check."""
    expected = {"GCP": "on", "SHELFSHIFT_LIVE_TESTS": "user-run"}
    for key, value in expected.items():
        if os.environ.get(key) != value:
            raise pytest.UsageError(f"{key}={value} is required: paid tests are explicitly user-run only")
    for key in ("GEMINI_MODEL", "VERTEX_LOCATION", "SHELFSHIFT_LIVE_RUN_ID"):
        if not os.environ.get(key, "").strip():
            raise pytest.UsageError(f"{key} must be configured explicitly before a live test")
    for key in ("PROJECT", "DATABASE_URL"):
        if not os.environ.get(key, "").strip():
            raise pytest.UsageError(f"{key} must be configured explicitly before a live test")
    url = urlsplit(os.environ["DATABASE_URL"])
    if (url.scheme != "postgresql+pg8000" or url.hostname not in {"localhost", "127.0.0.1"}
            or url.port != 5432 or url.path != "/shelfshift_assistant_test" or url.query or url.fragment):
        raise pytest.UsageError("DATABASE_URL must select the dedicated local shelfshift_assistant_test")
    if os.environ.get("CLOUD_SQL_INSTANCE"):
        raise pytest.UsageError("CLOUD_SQL_INSTANCE must be empty for local live tests")
    # Presence is checked above; never copy the project into a dict that pytest
    # might display while expanding a failed identity assertion.
    return {k: os.environ[k] for k in (*expected, "GEMINI_MODEL", "VERTEX_LOCATION",
                                      "SHELFSHIFT_LIVE_RUN_ID")}


def pytest_generate_tests(metafunc):
    # Collection is intentionally offline; execution still fails closed before
    # fixtures. Identity comparison preserves the existing preflight unit seam.
    if (metafunc.config.getoption("markexpr") == "live"
            and metafunc.config.getoption("collectonly") is not True):
        require_live()


@contextmanager
def measurement_slot(directory, *, poll=None):
    """Acquire by mkdir; release only this invocation's matching owner token."""
    slot = None
    token = "phase09 codex 09-23 " + uuid.uuid4().hex
    if poll is None:
        poll = lambda: time.sleep(30)
    try:
        while slot is None:
            try:
                directory.mkdir()
            except FileExistsError:
                print("P09-23 waiting for shared timing slot", flush=True)
                poll()
            else:
                slot = directory
        (slot / "owner").write_text(token)
        yield
    finally:
        if slot is not None:
            owner = slot / "owner"
            try:
                if owner.read_text() == token:
                    owner.unlink()
                    slot.rmdir()
            except FileNotFoundError:
                pass


@pytest.fixture(scope="session")
def live_measurement_slot():
    require_live()
    directory = Path(__file__).resolve().parents[3] / "ShelfShift/.git/shelfshift-test-slot"
    with measurement_slot(directory):
        yield


@pytest.fixture(autouse=True)
def live_setup(request, monkeypatch):
    if request.node.get_closest_marker("live") is None:
        yield
        return
    if request.config.getoption("markexpr") != "live":
        raise pytest.UsageError("Paid tests require the explicit selection -m live")
    configured = require_live()
    request.getfixturevalue("live_measurement_slot")
    if configured["SHELFSHIFT_LIVE_RUN_ID"] == "new":
        # No replacement fixtures or app behavior: the existing local test
        # schema/template/client creates each disposable authenticated workspace.
        from tests.agent.test_brief import workspace
        session, run, case = workspace(request.getfixturevalue("client"))
        monkeypatch.setenv("SHELFSHIFT_LIVE_RUN_ID", run["run_id"])
    try:
        yield
    finally:
        emit_live_evidence(request.node.name)


def observe_model(monkeypatch, model_type):
    """Inspect actual outgoing policy; retain only non-thought final candidates.

    No request contents, provider response objects, thought signatures or project
    values are copied. Real Gemini responses and tools are passed unchanged.
    """
    observations = {"requests": [], "candidates": []}
    original = model_type.generate_content_async

    async def observed(self, llm_request, stream=False):
        cfg = llm_request.config
        identity = getattr(self, "client_kwargs", {}) or {}
        observations["requests"].append({
            "model_id": self.model, "location": identity.get("location"),
            "enterprise": identity.get("enterprise"),
            "thinking_level": cfg.thinking_config.thinking_level,
            "thinking_budget": cfg.thinking_config.thinking_budget,
            "max_output_tokens": cfg.max_output_tokens,
            "temperature": cfg.temperature, "top_p": cfg.top_p, "top_k": cfg.top_k,
            "response_schema": cfg.response_schema, "response_json_schema": cfg.response_json_schema,
            "tools": sorted(llm_request.tools_dict),
        })
        async for response in original(self, llm_request, stream=stream):
            parts = (response.content.parts or []) if response.content else []
            if not response.partial and not any(p.function_call for p in parts):
                text = "".join(p.text for p in parts if p.text and not p.thought)
                if text.strip():
                    observations["candidates"].append(text)
                    # Gemini's final answer before checking, so a removed line can be explained.
                    print("P09-LIVE-RAW " + json.dumps(text), flush=True)
            yield response

    monkeypatch.setattr(model_type, "generate_content_async", observed)
    return observations


@pytest.fixture
def provider_requests(monkeypatch):
    from google.adk.models import Gemini
    return observe_model(monkeypatch, Gemini)


async def ask_frames(run, case, session, question, request_id):
    """Use the shipped admission/producer, matching the authenticated API route."""
    from agent import jobs
    from api.app import body_hash
    from tests.agent.conftest import drain
    body = {"question": question, "request_id": request_id}
    job = jobs.prepare_answer(run["run_id"], case["case_id"], session, request_id,
                              question, request_id, body_hash(body))
    return await drain(jobs.run_job(job))


def assert_outgoing_policy(observations, configured, scope, *, provider=True):
    from agent import config, tools
    # Settings also holds PROJECT. Project-free primitives keep assertion
    # expansion safe without changing the production configuration or client.
    loaded = config.load()
    settings = {key: getattr(loaded, key) for key in
                ("seconds", "max_tool_calls", "model_id", "location", "max_request_tokens", "max_output_tokens")}
    del loaded
    assert settings["seconds"] == 40 and settings["max_tool_calls"] == 8
    assert settings["model_id"] == configured["GEMINI_MODEL"] and settings["location"] == configured["VERTEX_LOCATION"]
    assert settings["max_request_tokens"] >= settings["max_output_tokens"] > 0
    allowed = sorted(tool.__name__ for tool in tools.SCOPE_TOOLS[scope])
    assert observations["requests"], "No outgoing model request was observed"
    for request in observations["requests"]:
        assert request["thinking_level"] == "LOW" and request["thinking_budget"] is None
        assert all(request[k] is None for k in ("temperature", "top_p", "top_k", "response_schema", "response_json_schema"))
        assert 0 < request["max_output_tokens"] <= settings["max_output_tokens"]
        assert request["tools"] == allowed
        if provider:
            assert request["enterprise"] is True and request["model_id"] == configured["GEMINI_MODEL"]
            assert request["location"] == configured["VERTEX_LOCATION"]


def assert_final_contract(configured, run, case, session, frames, observations, *, provider=True):
    """Whole final text, exact scoped SQL references, usage and fixed envelope."""
    from agent import config, numbers
    from api import refdata
    from tests.agent.test_brief import rows
    finals = [e for e in frames if e["type"] in {"answer", "brief"}]
    assert len(finals) == 1 and frames[-1] == finals[0]
    final = finals[0]
    assert all("text" not in e for e in frames[:-1]), "Partial prose was published before checking"
    assert any(e["type"] == "state" and e["state"] == "Checking numbers" for e in frames[:-1])
    assert final["provenance"] == "LIVE" and final["status"] == "CURRENT" and final["text"].strip()
    saved, = [o for o in rows(run["run_id"], "ai_outputs") if o["output_id"] == final["output_id"]]
    scope = "ASK" if saved["kind"] == "ANSWER" else "CASE"
    rid = saved["request_id"] if scope == "ASK" else saved["output_id"]
    assert saved["run_id"] == run["run_id"] and saved["case_id"] == case["case_id"]
    assert all(final[k] == saved[k] for k in ("text", "refs", "checks", "rec_version", "plan_key", "model_id"))
    if scope == "ASK":
        assert saved["session_id"] == session["session_id"] and saved["request_id"] == rid
    audits = [a for a in rows(run["run_id"], "tool_results") if a["request_id"] == rid]
    actor = {k: session[k] for k in ("account", "role", "branch")}
    assert all(a["scope"] == scope and a["case_id"] == case["case_id"]
               and a["rec_version"] == saved["rec_version"] and a["plan_key"] == saved["plan_key"]
               and a["actor"] == actor and (scope == "CASE" or a["session_id"] == session["session_id"])
               for a in audits), "Tool audit crossed the original scope"
    results = {a["result_id"]: a["result"] for a in audits}
    assert observations["candidates"], "A complete non-thought final candidate is required"
    checked = numbers.check(observations["candidates"][-1], results, ref=refdata.get(run["dataset_version"]))
    assert {k: final[k] for k in ("text", "refs")} == {k: checked[k] for k in ("text", "refs")}
    assert final["checks"] == {k: checked[k] for k in ("claims", "removed")}
    for ref in final["refs"]:
        assert ref["result_id"] in results and numbers.lookup(results, ref["result_id"], ref["path"]) is not None
    assert {s["result_id"] for s in final["sources"]} == {a["result_id"] for a in audits if "error" not in a["result"]}
    usage, = [u for u in rows(run["run_id"], "ai_usage") if u["output_id"] == final["output_id"]]
    assert usage["status"] == "DONE" and usage["model_id"] == configured["GEMINI_MODEL"]
    assert usage["location"] == configured["VERTEX_LOCATION"] and usage["precomputed"] is False
    assert usage["model_calls"] == len(observations["requests"]) and 1 <= usage["model_calls"] <= 10
    assert 0 <= usage["tool_calls"] <= 8 and 0 <= usage["retries"] <= 1
    assert usage["provider_attempts"] == usage["model_calls"] + usage["retries"]
    assert 0 <= usage["first_text_ms"] <= usage["total_ms"] <= 40000
    for key in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens", "tool_use_prompt_tokens"):
        assert usage[key] is None or usage[key] >= 0
        assert final["usage"][key] == usage[key]
    request_token_limit = config.load().max_request_tokens
    assert (usage["total_tokens"] or 0) <= request_token_limit
    assert {d["result_id"] for d in usage["tool_durations"]} == set(results)
    assert_outgoing_policy(observations, configured, scope, provider=provider)
    assert_no_hidden_fields(frames)
    return final


def assert_no_hidden_fields(value):
    if isinstance(value, dict):
        assert not {"thought", "thoughts", "thought_signature", "reasoning", "credentials", "PROJECT", "project"} & value.keys()
        for child in value.values():
            assert_no_hidden_fields(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_hidden_fields(child)


def emit_live_evidence(scenario):
    """Print safe retained evidence even when a live assertion fails; never retry."""
    from agent import numbers, usage
    from tests.agent.test_brief import rows
    run_id = os.environ["SHELFSHIFT_LIVE_RUN_ID"]
    attempts = rows(run_id, "ai_usage")
    outputs = rows(run_id, "ai_outputs")
    report = {"scenario": scenario, "limits": usage.configured_limits(),
              "aggregate": usage.benchmark(attempts, measurement_source="user-observed-live"),
              "attempts": [{k: a[k] for k in (*usage.METRICS, "model_id", "location", "status", "reason", "tool_durations")}
                           for a in attempts],
              "outputs": [{k: o[k] for k in ("output_id", "request_id", "case_id", "rec_version", "plan_key",
                                              "kind", "status", "provenance", "text", "refs", "checks", "sources")}
                          for o in outputs]}
    for output in report["outputs"]:
        output["rendered_text"] = numbers.render(output["text"], output["refs"])
    assert_no_hidden_fields(report)
    print("P09-LIVE-EVIDENCE " + json.dumps(report, sort_keys=True), flush=True)


def live_workspace():
    configured = require_live()
    from api import db
    from tests.api.test_evidence import story
    run_id = configured["SHELFSHIFT_LIVE_RUN_ID"]
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        assert run and run["kind"] == "SANDBOX" and run["status"] == "ACTIVE", "Choose a disposable active SANDBOX"
        session = db.one(conn, "SELECT * FROM sessions WHERE run_id = :r AND account IS NOT NULL", r=run_id)
        assert session is not None, "Choose a workspace with an authenticated session"
    return configured, run, story(run), session


@pytest.mark.live
def test_live_ask_has_checked_sources_real_usage_and_no_business_authority(provider_requests):
    configured, run, case, session = live_workspace()
    from agent import jobs
    from tests.agent.conftest import drain
    from tests.agent.test_brief import business_state, rows
    before = business_state(run["run_id"])
    rid = uuid.uuid4().hex
    frames = asyncio.run(ask_frames(run, case, session,
        "Read this case with get_case and explain the stock and engine recommendation using metric references. Do not act.", rid))
    answer, = [e for e in frames if e["type"] == "answer"]
    assert answer["provenance"] == "LIVE" and answer["text"].strip() and answer["refs"]
    attempt, = [u for u in rows(run["run_id"], "ai_usage") if u["request_id"] == rid]
    assert attempt["status"] == "DONE" and attempt["model_id"] == configured["GEMINI_MODEL"]
    assert attempt["location"] == configured["VERTEX_LOCATION"] and 1 <= attempt["model_calls"] <= 10
    assert 1 <= attempt["tool_calls"] <= 8 and 0 <= attempt["retries"] <= 1
    assert all(attempt[k] is not None and attempt[k] >= 0 for k in ("input_tokens", "output_tokens", "thinking_tokens"))
    assert 0 <= attempt["first_text_ms"] <= attempt["total_ms"] <= 40000
    audits = {a["result_id"]: a for a in rows(run["run_id"], "tool_results") if a["request_id"] == rid}
    assert audits and all(ref["result_id"] in audits for ref in answer["refs"])
    assert business_state(run["run_id"]) == before
    assert_final_contract(configured, run, case, session, frames, provider_requests)
    assert rows(run["run_id"], "evidence_drafts") == []


@pytest.mark.live
def test_live_multi_tool_ask_stays_in_the_same_location_and_fixed_budget(provider_requests):
    configured, run, case, session = live_workspace()
    from agent import jobs
    from tests.agent.conftest import drain
    from tests.agent.test_brief import business_state, rows
    from tests.api.verify_helpers import shown
    rid = uuid.uuid4().hex
    before = business_state(run["run_id"])
    question = (f"Use get_case, get_forecast for {case['source_branch']}, get_inventory_and_inbound for that branch, "
                f"get_transport_options, get_fact_checks and check_constraints for {shown(run, case['case_id'])}. "
                "Explain the decision and its assumptions with metric references. Do not change anything.")
    frames = asyncio.run(ask_frames(run, case, session, question, rid))
    final, = [e for e in frames if e["type"] == "answer"]
    assert final["provenance"] == "LIVE" and final["refs"]
    tools = {a["tool"] for a in rows(run["run_id"], "tool_results") if a["request_id"] == rid}
    assert {"get_case", "get_forecast", "get_inventory_and_inbound", "get_transport_options",
            "get_fact_checks", "check_constraints"} <= tools
    usage, = [u for u in rows(run["run_id"], "ai_usage") if u["request_id"] == rid]
    assert 6 <= usage["tool_calls"] <= 8 and usage["total_ms"] <= 40000
    assert usage["location"] == configured["VERTEX_LOCATION"] and usage["model_id"] == configured["GEMINI_MODEL"]
    assert_final_contract(configured, run, case, session, frames, provider_requests)
    assert business_state(run["run_id"]) == before and rows(run["run_id"], "evidence_drafts") == []


@pytest.mark.live
def test_live_case_brief_can_draft_but_cannot_send_or_confirm(provider_requests):
    configured, run, case, session = live_workspace()
    from agent import jobs
    from tests.agent.conftest import drain
    from tests.agent.test_brief import business_state, rows
    before = business_state(run["run_id"])
    assert not any(o["case_id"] == case["case_id"] and o["kind"] == "BRIEF" and o["status"] == "CURRENT"
                   for o in rows(run["run_id"], "ai_outputs")), "Choose a workspace without a saved story brief"
    frames = asyncio.run(drain(jobs.brief(run["run_id"], case["case_id"], session)))
    final, = [e for e in frames if e["type"] == "brief"]
    assert final["provenance"] == "LIVE" and final["model_id"] == configured["GEMINI_MODEL"] and final["refs"]
    drafts = [d for d in rows(run["run_id"], "evidence_drafts") if d["case_id"] == case["case_id"]]
    assert drafts and all(d["state"] == "DRAFT" and d["request_id"] is None and d.get("due_at") is None for d in drafts)
    assert all(d["original_text"].strip() and d["checks"] is not None for d in drafts)
    assert business_state(run["run_id"]) == before
    assert_final_contract(configured, run, case, session, frames, provider_requests)
    from agent import numbers
    from api import refdata
    audits = [a for a in rows(run["run_id"], "tool_results") if a["request_id"] == final["output_id"]]
    results = {a["result_id"]: a["result"] for a in audits}
    for draft in drafts:
        assert (draft["rec_version"], draft["plan_key"]) == (final["rec_version"], final["plan_key"])
        assert all(ref["result_id"] in results for ref in draft["refs"])
        rendered = numbers.render(draft["original_text"], draft["refs"])
        assert numbers.check(rendered, results, ref=refdata.get(run["dataset_version"]))["removed"] == 0


@pytest.mark.live
def test_live_off_topic_question_names_the_limit_without_tools_or_business_actions(provider_requests):
    configured, run, case, session = live_workspace()
    from tests.agent.test_brief import business_state, rows
    before = business_state(run["run_id"])
    frames = asyncio.run(ask_frames(run, case, session,
        "What is the current weather outside this workspace? No weather data is available here. "
        "Explain the information limit and offer a relevant next step for reviewing this case. "
        "Do not search, use unrelated tools or perform any business action.", uuid.uuid4().hex))
    final = assert_final_contract(configured, run, case, session, frames, provider_requests)
    text = final["text"].lower()
    assert any(word in text for word in ("cannot", "can't", "don't", "not available", "no weather", "do not have", "does not"))
    assert any(word in text for word in ("workspace", "stock", "forecast", "plan"))
    assert any(word in text for word in ("compare", "review", "check"))
    assert final["sources"] == final["refs"] == final["cards"] == []
    assert final["checks"] == {"claims": 0, "removed": 0} and final["usage"]["tool_calls"] == 0
    assert business_state(run["run_id"]) == before and rows(run["run_id"], "evidence_drafts") == []


def test_live_collection_preflight_is_offline_but_execution_still_requires_consent(no_network, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("GCP", "off")
    options = {"markexpr": "live", "collectonly": True}
    metafunc = SimpleNamespace(config=SimpleNamespace(getoption=options.get))
    pytest_generate_tests(metafunc)
    options["collectonly"] = False
    with pytest.raises(pytest.UsageError, match="GCP=on"):
        pytest_generate_tests(metafunc)
    assert no_network.attempts == []


@pytest.mark.parametrize("replacement", [None, "another-owner-token"])
def test_live_measurement_slot_releases_only_its_own_token(tmp_path, replacement):
    directory = tmp_path / "slot"
    with measurement_slot(directory):
        token = (directory / "owner").read_text()
        assert token.startswith("phase09 codex 09-23 ")
        if replacement:
            (directory / "owner").write_text(replacement)
    assert directory.exists() is bool(replacement)
    if replacement:
        assert (directory / "owner").read_text() == replacement


def test_live_measurement_slot_waits_for_the_existing_owner(tmp_path):
    directory = tmp_path / "slot"
    directory.mkdir()
    (directory / "owner").write_text("prior-test-owner")
    polls = []

    def owner_releases():
        assert (directory / "owner").read_text() == "prior-test-owner"
        polls.append(1)
        # Only this test's simulated prior owner releases its own temp lock.
        (directory / "owner").unlink()
        directory.rmdir()

    with measurement_slot(directory, poll=owner_releases):
        assert polls == [1] and (directory / "owner").read_text() != "prior-test-owner"
    assert not directory.exists()


@pytest.mark.parametrize("action", ["send_evidence_request", "approve", "commit", "adopt", "confirm_stock"])
def test_live_authority_contract_refuses_every_model_business_action(client, fake_model, monkeypatch, action):
    from agent import config
    from tests.agent.test_brief import business_state, install, rows, workspace
    from tests.agent.test_model import response
    fake = install(fake_model, monkeypatch, [response(calls=[(action, {})], usage={
        "prompt_token_count": 5, "candidates_token_count": 1, "thoughts_token_count": 0, "total_token_count": 6})])
    observations = observe_model(monkeypatch, type(fake))
    session, run, case = workspace(client)
    before = business_state(run["run_id"])
    frames = asyncio.run(ask_frames(run, case, session, "Perform this business action now.", uuid.uuid4().hex))
    final = frames[-1]
    assert final["provenance"] == "FALLBACK" and final["usage"]["reason"] == "unavailable"
    assert len(fake.requests) == 1 and final["usage"]["tool_calls"] == 0
    assert business_state(run["run_id"]) == before and rows(run["run_id"], "evidence_drafts") == []
    assert_outgoing_policy(observations, {"GEMINI_MODEL": config.model_id(), "VERTEX_LOCATION": config.location()},
                           "ASK", provider=False)


def test_live_final_contract_checks_the_whole_answer_and_rejects_cross_scope_audit(client, fake_model, monkeypatch):
    from agent import config
    from api import db
    from tests.agent.test_brief import business_state, install, rows, workspace
    from tests.agent.test_model import response
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {})], usage={
        "prompt_token_count": 5, "candidates_token_count": 1, "thoughts_token_count": 0, "total_token_count": 6}),
        [response("hidden-thought-sentinel", thought=True, partial=True),
         response("Review the recorded plan. Benefit is USD 123456789.12.", usage={
             "prompt_token_count": 7, "candidates_token_count": 4, "thoughts_token_count": 2, "total_token_count": 13})]])
    session, run, case = workspace(client)
    original = type(fake).generate_content_async

    async def reference_turn(self, llm_request, stream=False):
        if self.requests:
            audit, = rows(run["run_id"], "tool_results")
            self.script[0][-1].content.parts[0].text = (
                f"Recorded quantity is [[{audit['result_id']}:lot.recorded_qty]]. "
                "Review the recorded plan. Benefit is USD 123456789.12.")
        async for item in original(self, llm_request, stream=stream):
            yield item

    monkeypatch.setattr(type(fake), "generate_content_async", reference_turn)
    observations = observe_model(monkeypatch, type(fake))
    before = business_state(run["run_id"])
    frames = asyncio.run(ask_frames(run, case, session, "Explain the recorded plan.", uuid.uuid4().hex))
    configured = {"GEMINI_MODEL": config.model_id(), "VERTEX_LOCATION": config.location()}
    final = assert_final_contract(configured, run, case, session, frames, observations, provider=False)
    assert final["refs"] and final["checks"]["claims"] == 1
    assert final["checks"]["removed"] == 1 and "123456789.12" not in final["text"]
    assert "hidden-thought-sentinel" not in repr(frames) + repr(observations)
    assert business_state(run["run_id"]) == before
    audit, = rows(run["run_id"], "tool_results")
    with db.tx() as conn:
        db.run(conn, "UPDATE tool_results SET session_id = :s WHERE run_id = :r AND result_id = :id",
               s="foreign-session-sentinel", r=run["run_id"], id=audit["result_id"])
    with pytest.raises(AssertionError, match="crossed the original scope"):
        assert_final_contract(configured, run, case, session, frames, observations, provider=False)
