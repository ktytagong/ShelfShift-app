"""The explicit release job uses real READY templates, services and SQL clones.

Only public provider builders are faked. No job runs against a provider.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from tests.api import conftest as api_fixtures  # Credentials precede api.config.
from tests.agent.test_baseline import all_ai_caps, inactive
from tests.agent.test_brief import business_state, rows
from tests.agent.test_model import response


@pytest.fixture
def release_template(schema, template, monkeypatch):
    from api import preload
    # The tiny synthetic set's only candidate is a judge's case. Select it in
    # this fixture as the upstream preload tests do, keeping real generation.
    monkeypatch.setattr(preload, "JUDGE_TAGS", ())
    monkeypatch.setattr(preload, "ROLES", ("photo_only",))
    run = inactive(schema)
    assert run["kind"] == "TEMPLATE" and run["status"] == "READY" and not run["failure"]
    return run


def reports(run_id):
    return [row for row in rows(run_id, "signals")
            if row["provenance"] == "simulated" and row["state"] == "READ" and row["report_id"]]


def simulated_answers(run_id):
    from api import db
    with db.tx() as conn:
        return db.rows(conn, "SELECT s.* FROM signals s JOIN evidence_requests q "
            "ON q.run_id=s.run_id AND q.answer_signal_id=s.signal_id "
            "WHERE s.run_id=:r AND s.provenance='simulated' ORDER BY s.signal_id", r=run_id)


def reading_rows(run_id):
    from api import db
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM readings WHERE run_id=:r ORDER BY created_at, reading_id", r=run_id)


def install_photo(monkeypatch, fake=None):
    from agent.reading import Reading
    from tests.agent.test_readings_http import FakeModels, install
    return install(monkeypatch, fake or FakeModels(Reading(kind="STOCK_COUNT", photo_observation="visible shelf")))


def install_release_model(fake_model, monkeypatch):
    from agent import jobs, model
    base = type(fake_model([]))

    class ReleaseModel(base):
        async def generate_content_async(self, llm_request, stream=False):
            declarations = {fn.name for tool in (llm_request.config.tools or [])
                            for fn in (tool.function_declarations or [])}
            returned = [part.function_response.response for content in llm_request.contents
                        for part in (content.parts or []) if part.function_response is not None
                        and part.function_response.name == "get_impact_preview"]
            if "get_impact_preview" in declarations and not returned:
                item = response(calls=[("get_impact_preview", {})])
            elif returned:
                item = response("If confirmed, this report could affect "
                    f"[[{returned[-1]['result_id']}:affected]] cases.")
            else:
                item = response("Review the recorded options.")
            self.script.append(item)
            async for event in super().generate_content_async(llm_request, stream=stream):
                yield event

    fake = ReleaseModel(script=[])
    monkeypatch.setattr(jobs, "ai_on", lambda: True)
    monkeypatch.setattr(model, "llm", lambda: fake)
    return fake


def clone_and_session(template):
    from api import db, sandbox
    with db.tx() as conn:
        rid = sandbox.clone(conn, api_fixtures.fresh(template["run_id"]))
        sid = "precompute-" + uuid.uuid4().hex
        db.run(conn, "INSERT INTO sessions (session_id, role, account, run_id) "
                     "VALUES (:s, 'hq', 'HQ', :r)", s=sid, r=rid)
    return api_fixtures.fresh(rid), dict(session_id=sid, run_id=rid, role="hq", account="HQ", branch=None)


async def summary(run_id, report, session=None, *, precomputed=False):
    from agent import impact
    final = None
    async for event in impact.summarize(run_id, report["signal_id"], session,
            revision=report["revision"], precomputed=precomputed):
        if event["type"] == "summary":
            final = event
    return final


def test_precomputed_summary_opens_free(release_template, fake_model, monkeypatch, capsys):
    from agent import briefs, impact
    from api import db
    run = release_template
    report_rows = reports(run["run_id"])
    assert any(row["kind"] == "CAPACITY" for row in report_rows)
    fake = install_release_model(fake_model, monkeypatch)
    install_photo(monkeypatch)
    caps = all_ai_caps()
    before = business_state(run["run_id"])
    assert asyncio.run(briefs.run(run["run_id"])) == 0
    after = business_state(run["run_id"])
    for table in ("cases", "decisions", "reservations", "reported_facts", "evidence_requests", "lots", "truth_lots"):
        assert after[table] == before[table], table
    # Only report preview JSON may change on existing signals. No answered
    # values, confirmation, request or stock record is part of release output.
    without_preview = lambda row: {key: value for key, value in row.items() if key != "preview"}
    old_signals = {row["signal_id"]: row for row in before["signals"]}
    new_signals = {row["signal_id"]: row for row in after["signals"]}
    assert {key: without_preview(row) for key, row in new_signals.items()} == {
        key: without_preview(row) for key, row in old_signals.items()}
    changed_previews = {key for key in new_signals if new_signals[key]["preview"] != old_signals[key]["preview"]}
    assert changed_previews and changed_previews <= {row["signal_id"] for row in report_rows}
    # Existing recommendations remain byte-for-byte; newly stored engine
    # what-ifs never replace the active recommendations checked above.
    identity = lambda row: (row["case_id"], row["rec_version"])
    old_recs = {identity(row): row for row in before["recommendations"]}
    new_recs = {identity(row): row for row in after["recommendations"]}
    assert all(new_recs.get(key) == row for key, row in old_recs.items())
    added = [row for key, row in new_recs.items() if key not in old_recs]
    assert added and all(row["is_whatif"] for row in added)
    outputs = [row for row in rows(run["run_id"], "ai_outputs") if row["kind"] == "impact"]
    assert len(outputs) == len(report_rows), "The explicit release job must precompute every eligible report summary"
    assert all(row["status"] == "CURRENT" and row["provenance"] == "PRECOMPUTED" for row in outputs)
    job = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]
    assert job["preloaded"]["written"] == len(report_rows) + len(simulated_answers(run["run_id"]))
    assert job["preloaded"]["failed"] == 0
    copied, session = clone_and_session(run)
    target = next(row for row in reports(copied["run_id"]) if row["kind"] == "CAPACITY")
    calls = len(fake.requests)
    audits = rows(copied["run_id"], "tool_results")
    retained = rows(copied["run_id"], "ai_outputs")
    opened = asyncio.run(summary(copied["run_id"], target, session))
    assert (opened["status"], opened["provenance"], opened["served"], opened["model_calls"]) == (
        "CURRENT", "PRECOMPUTED", "precomputed", 0)
    assert opened["preview_key"] and opened["rows_result_id"]
    with db.tx() as conn:
        view = impact.summary_view(conn, copied, target)
        assert view["provenance"] == "PRECOMPUTED" and view["served"] == "precomputed"
        assert db.one(conn, "SELECT result_id FROM tool_results WHERE run_id=:r AND result_id=:i",
                      r=copied["run_id"], i=opened["rows_result_id"])
        for ref in opened["refs"]:
            assert db.one(conn, "SELECT result_id FROM tool_results WHERE run_id=:r AND result_id=:i",
                          r=copied["run_id"], i=ref["result_id"])
    assert len(fake.requests) == calls and all_ai_caps() == caps
    assert rows(copied["run_id"], "ai_outputs") == retained
    assert rows(copied["run_id"], "tool_results") == audits
    original = [{k: v for k, v in row.items() if k != "run_id"} for row in rows(run["run_id"], "ai_outputs")]
    assert [{k: v for k, v in row.items() if k != "run_id"} for row in retained] == original


@pytest.mark.parametrize("failed_status", ["FAILED", "REFUSED", "INTERRUPTED"])
def test_answer_cache_excludes_failed_precompute(release_template, monkeypatch, failed_status):
    from agent import readings_job
    from api import db
    copied, session = clone_and_session(release_template)
    [answer] = simulated_answers(copied["run_id"])
    fake = install_photo(monkeypatch)
    live = asyncio.run(readings_job.read_answer(session, answer["signal_id"], answer["revision"]))
    assert live["status"] == "DONE" and len(fake.calls) == 1
    [original] = reading_rows(copied["run_id"])
    with db.tx() as conn:
        # Stored failed release attempts are fixture history, not a model or
        # engine bypass. Their later timestamp exercises the actual selectors.
        db.run(conn, "INSERT INTO readings (run_id, reading_id, input_key, purpose, signal_id, signal_revision, "
            "request_id, status, model_id, assistant_version, provenance, created_by) "
            "SELECT run_id, :new, input_key, purpose, signal_id, signal_revision, request_id, :status, "
            "model_id, assistant_version, 'PRECOMPUTED', 'preloaded-release' "
            "FROM readings WHERE run_id=:r AND reading_id=:id",
            new="failed-release-" + uuid.uuid4().hex, status=failed_status,
            r=copied["run_id"], id=original["reading_id"])
        selected = readings_job._answer_latest(conn, copied, answer, original["input_key"])
        assert selected["reading_id"] == original["reading_id"], "Failed precompute must not hide the saved LIVE answer"
        assert readings_job._latest(conn, copied, original["input_key"])["reading_id"] == original["reading_id"]
        assert readings_job.answer_latest(conn, copied, answer)["reading_id"] == original["reading_id"]
    caps = all_ai_caps()
    again = asyncio.run(readings_job.read_answer(session, answer["signal_id"], answer["revision"]))
    assert again["reading_id"] == original["reading_id"] and again["served"] == "reuse"
    assert len(fake.calls) == 1 and all_ai_caps() == caps
    for terminal in ("FAILED", "REFUSED"):
        with db.tx() as conn:
            db.run(conn, "UPDATE readings SET status=:status WHERE run_id=:r AND reading_id=:id",
                   status=terminal, r=copied["run_id"], id=original["reading_id"])
            assert readings_job.answer_latest(conn, copied, answer)["status"] == terminal
            assert readings_job._latest(conn, copied, original["input_key"])["status"] == terminal
        saved = asyncio.run(readings_job.read_answer(session, answer["signal_id"], answer["revision"]))
        assert saved["reading_id"] == original["reading_id"] and saved["status"] == terminal
        assert len(fake.calls) == 1 and all_ai_caps() == caps


def test_precomputed_answer_reading(release_template, fake_model, monkeypatch):
    from agent import preloaded
    from api.app import COOKIE, app
    from fastapi.testclient import TestClient
    install_release_model(fake_model, monkeypatch)
    photo = install_photo(monkeypatch)
    caps = all_ai_caps()
    result = asyncio.run(preloaded.precompute(release_template["run_id"]))
    stored = reading_rows(release_template["run_id"])
    assert len(stored) == 1, "The release pass must persist its simulated response reading"
    assert result["failed"] == 0 and stored[0]["provenance"] == "PRECOMPUTED"
    copied, session = clone_and_session(release_template)
    [answer] = simulated_answers(copied["run_id"])
    calls, retained = len(photo.calls), reading_rows(copied["run_id"])
    with TestClient(app) as client:
        client.cookies.set(COOKIE, session["session_id"])
        response = client.post(f"/api/signals/{answer['signal_id']}/reading", json={"revision": answer["revision"]})
    assert response.status_code == 200, response.text
    out = response.json()
    assert (out["status"], out["provenance"], out["served"]) == ("DONE", "PRECOMPUTED", "precomputed")
    assert out["pending_counts"] is None and out["observations"] == ["visible shelf"]
    assert out["not_checked"] == "Backroom stock not checked, so the total can't be confirmed"
    assert len(photo.calls) == calls and all_ai_caps() == caps
    assert reading_rows(copied["run_id"]) == retained
    assert [{k: v for k, v in row.items() if k != "run_id"} for row in retained] == [
        {k: v for k, v in row.items() if k != "run_id"} for row in stored]


@pytest.mark.parametrize("provider_fails", [False, True])
def test_activation_waits_for_precomputed_answer_then_allows_terminal_result(
        release_template, template, monkeypatch, barrier, provider_fails):
    from agent import readings_job
    from agent.reading import Reading
    from api import db, sandbox
    from tests.agent.test_readings_http import FakeModels

    run = release_template
    [answer] = simulated_answers(run["run_id"])
    gate = barrier()

    class PausedPhoto(FakeModels):
        async def generate_content(self, **kwargs):
            await gate.wait()
            if provider_fails:
                self.calls.append(kwargs)
                raise RuntimeError("provider unavailable")
            return await super().generate_content(**kwargs)

    photo = install_photo(monkeypatch, PausedPhoto(Reading(
        kind="STOCK_COUNT", photo_observation="visible shelf")))
    before_active = api_fixtures.fresh(template)
    before_ready = api_fixtures.fresh(run["run_id"])
    caps = all_ai_caps()

    async def exercise():
        task = asyncio.create_task(readings_job.precompute_answer(
            run["run_id"], answer["signal_id"], answer["revision"]))
        try:
            await asyncio.wait_for(gate.entered.wait(), 3)
            [pending] = reading_rows(run["run_id"])
            assert pending["status"] == "GENERATING" and pending["provenance"] == "PRECOMPUTED"
            with pytest.raises(sandbox.TemplateError, match="unfinished"):
                await asyncio.to_thread(db.in_tx, sandbox.activate_template, run["run_id"])
            assert api_fixtures.fresh(template) == before_active
            assert api_fixtures.fresh(run["run_id"]) == before_ready
            gate.release.set()
            final = await asyncio.wait_for(task, 3)
            assert final["status"] == ("FAILED" if provider_fails else "DONE")
            assert final["provenance"] == "PRECOMPUTED"
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(exercise())
        assert db.in_tx(sandbox.activate_template, run["run_id"]) == template
        assert api_fixtures.fresh(run["run_id"])["status"] == "ACTIVE"
        assert len(photo.calls) == 1 and all_ai_caps() == caps
    finally:
        db.in_tx(sandbox.restore, run["run_id"])
        sandbox.clear_cache()


def test_rerun_skips_unchanged_and_identity_changes_rewrite(release_template, fake_model, monkeypatch):
    from agent import config, preloaded
    fake = install_release_model(fake_model, monkeypatch)
    photo = install_photo(monkeypatch)
    caps = all_ai_caps()
    first = asyncio.run(preloaded.precompute(release_template["run_id"]))
    assert first["eligible"] == len(reports(release_template["run_id"])) + 1
    calls = len(fake.requests), len(photo.calls)
    again = asyncio.run(preloaded.precompute(release_template["run_id"]))
    assert again["written"] == again["failed"] == 0 and again["skipped"] == first["eligible"]
    assert again["usage"]["model_calls"] == 0 and all(item["model_calls"] == 0 for item in again["items"])
    assert (len(fake.requests), len(photo.calls)) == calls
    for field, value in (("ASSISTANT_VERSION", "release-version-changed"), ("model_id", lambda: "release-model-changed")):
        monkeypatch.setattr(config, field, value)
        changed = asyncio.run(preloaded.precompute(release_template["run_id"]))
        assert changed["written"] == first["eligible"] and changed["skipped"] == changed["failed"] == 0
        assert len(photo.calls) > calls[1]
        calls = len(fake.requests), len(photo.calls)
    assert all_ai_caps() == caps


def test_failure_never_fails_release_and_clone_reads_failed_item_live(release_template, fake_model, monkeypatch, capsys):
    from agent import briefs, readings_job
    from agent.reading import Reading
    from tests.agent.test_readings_http import FakeModels

    class FailedPhoto(FakeModels):
        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("private provider diagnostic")

    fake = install_release_model(fake_model, monkeypatch)
    photo = install_photo(monkeypatch, FailedPhoto(Reading(kind="STOCK_COUNT")))
    caps = all_ai_caps()
    assert asyncio.run(briefs.run(release_template["run_id"])) == 0
    job = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]
    assert job["preloaded"]["failed"] == 1, "Failed response generation must be attempted and reported"
    assert job["preloaded"]["written"] == len(reports(release_template["run_id"]))
    assert fake.requests and len(photo.calls) == 1 and all_ai_caps() == caps
    assert "private provider diagnostic" not in json.dumps(job)
    copied, session = clone_and_session(release_template)
    [answer] = simulated_answers(copied["run_id"])
    [failed] = reading_rows(copied["run_id"])
    assert failed["status"] == "FAILED" and failed["provenance"] == "PRECOMPUTED"
    recovered = install_photo(monkeypatch)
    opened = asyncio.run(readings_job.read_answer(session, answer["signal_id"], answer["revision"]))
    assert (opened["status"], opened["served"], opened["provenance"]) == ("DONE", "live", "LIVE")
    assert len(recovered.calls) == 1 and all_ai_caps() != caps
    assert len(reading_rows(copied["run_id"])) == 2


def test_budget_refuses_before_any_item_starts(release_template, fake_model, monkeypatch):
    from agent import preloaded
    fake = install_release_model(fake_model, monkeypatch)
    photo = install_photo(monkeypatch)
    caps, seen = all_ai_caps(), []

    def refuse(item):
        seen.append(item)
        return False

    result = asyncio.run(preloaded.precompute(release_template["run_id"], budget=refuse))
    assert len(seen) == 1 and result["stopped"] == result["eligible"]
    assert result["written"] == result["skipped"] == result["failed"] == 0
    assert not fake.requests and not photo.calls and all_ai_caps() == caps
    assert not reading_rows(release_template["run_id"])
    assert not [row for row in rows(release_template["run_id"], "ai_outputs") if row["kind"] == "impact"]


@pytest.mark.parametrize("failure_point", ["before_reader", "after_reader"])
def test_escaped_reading_error_preserves_known_zero_or_unknown_usage(release_template, fake_model, monkeypatch, failure_point):
    from agent import preloaded, reading, readings_job
    install_release_model(fake_model, monkeypatch)
    photo = install_photo(monkeypatch)
    caps = all_ai_caps()
    original = reading.read

    if failure_point == "before_reader":
        def fail_builder():
            raise RuntimeError("private construction diagnostic")
        monkeypatch.setattr(readings_job, "genai_client", fail_builder)
    else:
        async def fail_after_reader(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("private escaped diagnostic")
        monkeypatch.setattr(reading, "read", fail_after_reader)

    result = asyncio.run(preloaded.precompute(release_template["run_id"]))
    assert result["failed"] == 1, "An escaped response attempt must be retained as a failed release item"
    [failed] = reading_rows(release_template["run_id"])
    expected = 0 if failure_point == "before_reader" else None
    assert failed["status"] == "FAILED" and failed["usage"]["model_calls"] is expected
    assert len(photo.calls) == (0 if failure_point == "before_reader" else 1)
    answer = next(item for item in result["items"] if item.get("reading_id") == failed["reading_id"])
    assert answer["model_calls"] is expected
    if failure_point == "after_reader":
        assert result["usage"]["model_calls"] is None
        assert all(failed["usage"][key] is None for key in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens"))
    assert all_ai_caps() == caps
