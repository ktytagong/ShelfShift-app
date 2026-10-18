"""09-14/09-15: explicit baseline jobs, exact reuse and baseline-only copies.

The CLI is exercised in-process so the 09-03 BaseLlm/client sentinels apply.
All template builds use only the dedicated local test DB and GCP=off.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from tests.agent.conftest import drain
from tests.agent.test_brief import business_state, install, rows
from tests.agent.test_model import response


def inactive(dataset_version):
    from api import db, sandbox
    with db.tx() as conn:
        previous = sandbox.active_template(conn)["run_id"]
    run_id = sandbox.build_template(dataset_version, activate=False)
    with db.tx() as conn:
        built = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        assert sandbox.active_template(conn)["run_id"] == previous
    # The release owner owns the inactive-state spelling. Assert the lifecycle
    # boundary without inventing a new public status enum here.
    assert built["kind"] == "TEMPLATE" and built["status"] not in ("ACTIVE", "FAILED")
    return built


def eligible(run):
    return sorted((c["case_id"], c["active_rec_version"]) for c in rows(run["run_id"], "cases")
                  if c["active_rec_version"] is not None)


def baseline(run_id):
    from agent import briefs
    # Same entry point as `python -m agent.briefs RUN_ID`, without a subprocess
    # that would lose the approved fake model and offline client sentinel.
    assert briefs.main([run_id]) == 0


def successful(run_id):
    return [o for o in rows(run_id, "ai_outputs")
            if o["kind"] == "BRIEF" and o["status"] == "CURRENT" and o["provenance"] == "PRECOMPUTED"]


def all_ai_caps():
    from api import db
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM usage_counts WHERE kind = 'ai' ORDER BY scope, window_start")


def test_explicit_job_visits_every_evaluated_case_and_repeated_job_reuses_exact_outputs(schema, template, fake_model, monkeypatch):
    from agent import briefs
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    expected = eligible(run)
    assert len(expected) > 1
    fake.script.extend(response("Review the engine's recorded options.") for _ in expected)
    before, caps = business_state(run["run_id"]), all_ai_caps()
    baseline(run["run_id"])
    outputs = successful(run["run_id"])
    assert sorted((o["case_id"], o["rec_version"]) for o in outputs) == expected
    assert len(fake.requests) == len(expected) and fake.script == []
    assert len(rows(run["run_id"], "ai_usage")) == len(expected)
    baseline(run["run_id"])
    assert successful(run["run_id"]) == outputs and len(fake.requests) == len(expected)
    assert all_ai_caps() == caps and business_state(run["run_id"]) == before


@pytest.mark.parametrize("field", ["rec_version", "plan_key", "model_id", "assistant_version"])
def test_job_reuse_requires_every_revision_key_component(schema, template, fake_model, monkeypatch, field):
    from agent import briefs
    from api import db
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    expected = eligible(run)
    fake.script.extend(response("Original checked explanation.") for _ in expected)
    baseline(run["run_id"])
    old = {o["case_id"]: o for o in successful(run["run_id"])}
    target = expected[0][0]
    with db.tx() as conn:
        # An older saved output is fixture data. The case/engine input stays
        # intact; reuse must compare it with all five current identity fields.
        assert field in {"rec_version", "plan_key", "model_id", "assistant_version"}
        db.run(conn, f"UPDATE ai_outputs SET {field} = :x WHERE run_id = :r AND output_id = :o",
               x="older-key-sentinel", r=run["run_id"], o=old[target]["output_id"])
    fake.script.append(response("Regenerated checked explanation."))
    caps = all_ai_caps()
    baseline(run["run_id"])
    current = {o["case_id"]: o for o in successful(run["run_id"])}
    assert set(current) == set(old) and len(fake.requests) == len(expected) + 1 and fake.script == []
    assert current[target]["output_id"] != old[target]["output_id"]
    assert current[target]["text"] == "Regenerated checked explanation."
    assert all(current[cid] == old[cid] for cid in old if cid != target)
    assert all_ai_caps() == caps


def test_one_provider_failure_does_not_abort_job_or_become_precomputed_success(schema, template, fake_model, monkeypatch, capsys):
    from agent import briefs
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    expected = eligible(run)
    fake.script.extend([RuntimeError("offline baseline failure")] + [response("Checked explanation.") for _ in expected[1:]])
    caps = all_ai_caps()
    baseline(run["run_id"])
    successes = successful(run["run_id"])
    assert len(successes) == len(expected) - 1 and len(fake.requests) == len(expected)
    usage = rows(run["run_id"], "ai_usage")
    failures = [u for u in usage if u["status"] == "FALLBACK"]
    assert len(usage) == len(expected) and len(failures) == 1 and failures[0]["reason"]
    assert failures[0]["case_id"] not in {o["case_id"] for o in successes}
    report = capsys.readouterr().out
    assert failures[0]["case_id"] in report and "failure" in report.lower()
    assert all_ai_caps() == caps
    # Baseline failures are release-nonfatal; the original active template was
    # never replaced by the job. A user may explicitly activate this build.
    from api import db, sandbox
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template


def test_unevaluated_case_is_reported_without_fabricating_a_brief(schema, template, fake_model, monkeypatch, capsys):
    from agent import briefs
    from api import db
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    missing = eligible(run)[0][0]
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET active_rec_version = NULL WHERE run_id = :r AND case_id = :c",
               r=run["run_id"], c=missing)
    expected = eligible(run)
    fake.script.extend(response("Checked explanation.") for _ in expected)
    baseline(run["run_id"])
    assert len(successful(run["run_id"])) == len(fake.requests) == len(expected)
    assert missing not in {o["case_id"] for o in rows(run["run_id"], "ai_outputs")}
    report = capsys.readouterr().out
    assert missing in report and "recommendation" in report.lower()


def test_inactive_build_and_explicit_activation_never_generate_and_preserve_predecessor(schema, template, fake_model, monkeypatch):
    from agent import briefs
    from api import db, sandbox
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    caps = all_ai_caps()
    try:
        assert sandbox.main(["activate", run["run_id"]]) == 0
        with db.tx() as conn:
            assert sandbox.active_template(conn)["run_id"] == run["run_id"]
            old = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=template)
        assert old["status"] == "RETIRED" and old["retired_at"] is not None
        assert old["retired_at"] <= datetime.now(timezone.utc)
        assert fake.requests == [] and all_ai_caps() == caps
    finally:
        # Restore only this test's active-template pointer, preserving the
        # session-scoped API fixture for later tests. Never touches another DB.
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :r", r=run["run_id"])
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE', retired_at = NULL WHERE run_id = :r", r=template)
        sandbox.clear_cache()


def test_clone_copies_checked_brief_original_draft_exact_sources_and_usage_with_only_run_id_changed(schema, template, fake_model, monkeypatch, barrier):
    from agent import jobs, numbers
    from api import db, sandbox
    from google.genai import types
    from tests.api.test_evidence import phantom, story
    run = inactive(schema)
    case = story(run)
    fact = phantom(run, case["case_id"])
    before = business_state(run["run_id"])

    async def generate():
        gate, turn = barrier(), 0
        wait = gate.wait

        async def second_model_boundary():
            nonlocal turn
            turn += 1
            if turn == 2:
                await wait()

        monkeypatch.setattr(gate, "wait", second_model_boundary)
        last = response("Pending reference.")
        fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {}),
            ("draft_evidence_request", {"fact_id": fact["fact_id"], "ask_text": "Please count shelf and backroom stock."})]), last], gate=gate)
        task = asyncio.create_task(drain(jobs.brief(run["run_id"], case["case_id"], provenance="PRECOMPUTED")))
        try:
            await asyncio.wait_for(gate.entered.wait(), 3)
            audit, = [r for r in rows(run["run_id"], "tool_results") if r["tool"] == "get_case"]
            last.content.parts = [types.Part(text=f"Recorded stock is [[{audit['result_id']}:lot.recorded_qty]] units.")]
            gate.release.set()
            completed = await asyncio.wait_for(task, 3)
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(fake.requests) == 2 and completed[-1]["type"] == "brief"
        return fake, audit

    fake, cited = asyncio.run(generate())
    saved, = successful(run["run_id"])
    assert saved["refs"] and saved["refs"][0]["result_id"] == cited["result_id"]
    assert numbers.render(saved["text"], saved["refs"]) == f"Recorded stock is {cited['result']['lot']['recorded_qty']:g} units."
    caps = all_ai_caps()
    clone = db.in_tx(lambda conn: sandbox.clone(conn, run))
    for table in ("ai_outputs", "evidence_drafts", "tool_results", "ai_usage"):
        original, copied = rows(run["run_id"], table), rows(clone, table)
        assert original and len(copied) == len(original), table
        assert [{k: v for k, v in row.items() if k != "run_id"} for row in copied] == [
            {k: v for k, v in row.items() if k != "run_id"} for row in original], table
    copied, = rows(clone, "evidence_drafts")
    assert copied["state"] == "DRAFT" and copied["request_id"] is None
    assert copied.get("due_at") is None and copied.get("sent_at") is None
    assert rows(clone, "evidence_requests") == rows(run["run_id"], "evidence_requests") == []
    assert business_state(run["run_id"]) == before and all_ai_caps() == caps
    with db.tx() as conn:
        assert jobs.current_brief(conn, clone, case["case_id"], saved["rec_version"], saved["plan_key"])["output_id"] == saved["output_id"]
        for table in ("evidence_drafts", "ai_usage"):
            assert sandbox.columns(conn, table)[0] == "run_id" and table in sandbox.per_run_tables(conn)
    assert len(fake.requests) == 2


# 09-14 additions. The original 09-04 contract above stays byte-for-byte intact.
def test_job_reports_actual_counts_and_nullable_usage_and_retries_only_failures(schema, template, fake_model, monkeypatch, capsys, record_property):
    import json
    from agent import briefs, config
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    expected = eligible(run)
    fake.script.extend([RuntimeError("private provider diagnostic")] + [response("Checked explanation.") for _ in expected[1:]])
    capsys.readouterr()  # the template build's step lines (api/buildsteps.py) are not the job's report
    baseline(run["run_id"])
    report = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    summary = report[-1]
    record_property("first_job_summary", json.dumps(summary))
    assert summary["type"] == "summary" and summary["eligible"] == len(expected)
    assert (summary["attempted"], summary["success"], summary["failure"], summary["reused"]) == (len(expected), len(expected) - 1, 1, 0)
    assert summary["model_id"] == config.model_id() and summary["location"] == config.location()
    assert summary["assistant_version"] == config.ASSISTANT_VERSION
    assert summary["usage"]["model_calls"]["known_total"] == len(expected)
    assert summary["usage"]["input_tokens"] == {"known_total": None, "known_attempts": 0, "unknown_attempts": len(expected)}
    assert len([row for row in report if row["type"] == "case"]) == len(rows(run["run_id"], "cases"))
    assert "private provider diagnostic" not in json.dumps(report)
    fake.script.append(response("Recovered checked explanation."))
    baseline(run["run_id"])
    again = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]
    record_property("retry_job_summary", json.dumps(again))
    assert (again["attempted"], again["success"], again["failure"], again["reused"]) == (1, 1, 0, len(expected) - 1)
    assert len(rows(run["run_id"], "ai_usage")) == len(expected) + 1
    assert again["baseline_attempts"] == len(expected) + 1
    assert len(successful(run["run_id"])) == len(expected) and fake.script == []


@pytest.mark.parametrize("kind,status", [("TEMPLATE", "ACTIVE"), ("TEMPLATE", "INITIALIZING"),
                                         ("TEMPLATE", "FAILED"), ("TEMPLATE", "RETIRED"), ("SANDBOX", "ACTIVE")])
def test_job_refuses_anything_except_completed_inactive_template(schema, template, fake_model, monkeypatch, capsys, kind, status):
    from agent import briefs
    from api import db
    fake = install(fake_model, monkeypatch, [])
    run = inactive(schema)
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET kind = :k, status = :s WHERE run_id = :r", k=kind, s=status, r=run["run_id"])
    before, caps = business_state(run["run_id"]), all_ai_caps()
    assert briefs.main([run["run_id"]]) == 1
    assert "completed inactive template" in capsys.readouterr().err.lower()
    assert fake.requests == [] and rows(run["run_id"], "ai_usage") == []
    assert business_state(run["run_id"]) == before and all_ai_caps() == caps
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :r", r=run["run_id"])


def test_baseline_uses_scoped_tools_checks_numbers_and_stores_only_unsent_drafts(schema, template, fake_model, monkeypatch):
    from api import db
    from tests.api.test_evidence import phantom, story
    run = inactive(schema)
    case = story(run)
    fact = phantom(run, case["case_id"])
    expected = eligible(run)
    script = []
    for cid, _ in expected:
        if cid == case["case_id"]:
            script.extend([response(calls=[("get_case", {}), ("get_fact_checks", {}),
                ("draft_evidence_request", {"fact_id": fact["fact_id"], "ask_text": "Please count shelf and backroom stock."})]),
                response("The benefit is USD 123456789.12.")])
        else:
            script.append(response("Review the engine's recorded options."))
    fake = install(fake_model, monkeypatch, script)
    before, caps = business_state(run["run_id"]), all_ai_caps()
    baseline(run["run_id"])
    saved, = [o for o in successful(run["run_id"]) if o["case_id"] == case["case_id"]]
    assert saved["text"] == "⚠ number removed: not traceable to a tool result" and saved["checks"]["removed"] == 1
    audit = [r for r in rows(run["run_id"], "tool_results") if r["case_id"] == case["case_id"]]
    assert {r["tool"] for r in audit} == {"get_case", "get_fact_checks", "draft_evidence_request"}
    assert all("error" not in r["result"] for r in audit)
    assert {s["result_id"] for s in saved["sources"]} == {r["result_id"] for r in audit}
    assert all((r["scope"], r["rec_version"], r["plan_key"], r["request_id"]) ==
               ("CASE", saved["rec_version"], saved["plan_key"], saved["output_id"]) for r in audit)
    draft, = rows(run["run_id"], "evidence_drafts")
    assert draft["state"] == "DRAFT" and draft["request_id"] is None
    assert draft.get("due_at") is None and draft.get("sent_at") is None
    assert business_state(run["run_id"]) == before and all_ai_caps() == caps
    assert len(fake.requests) == len(expected) + 1 and fake.script == []


def test_activation_refuses_an_inflight_baseline_without_retiring_the_active_template(schema, template, fake_model, monkeypatch, barrier):
    from agent import jobs
    from api import db, sandbox
    run = inactive(schema)
    case_id = eligible(run)[0][0]
    gate = barrier()
    fake = install(fake_model, monkeypatch, [response("Checked explanation.")], gate=gate)

    async def exercise():
        job = jobs.prepare_brief(run["run_id"], case_id, baseline=True)
        task = asyncio.create_task(drain(jobs.run_job(job)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 3)
            with pytest.raises(sandbox.TemplateError, match="unfinished baseline"):
                db.in_tx(sandbox.activate_template, run["run_id"])
            with db.tx() as conn:
                assert sandbox.active_template(conn)["run_id"] == template
            assert rows(run["run_id"], "ai_usage")[0]["status"] == "RUNNING"
            gate.release.set()
            final = (await asyncio.wait_for(task, 3))[-1]
            assert final["status"] == "CURRENT" and final["provenance"] == "PRECOMPUTED"
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    try:
        assert sandbox.main(["activate", run["run_id"]]) == 0
        assert len(fake.requests) == 1 and all(u["status"] == "DONE" for u in rows(run["run_id"], "ai_usage"))
    finally:
        db.in_tx(sandbox.restore, run["run_id"])
        sandbox.clear_cache()


# 09-15 additions. Every pre-existing assertion and call above is retained.
@pytest.fixture
def checked_copy_source(schema, template, fake_model, monkeypatch):
    from agent import jobs
    from tests.api.test_evidence import phantom, story
    run = inactive(schema)
    case = story(run)
    fact = phantom(run, case["case_id"])
    fake = install(fake_model, monkeypatch, [response(calls=[("get_case", {}),
        ("get_fact_checks", {}), ("draft_evidence_request", {"fact_id": fact["fact_id"],
            "ask_text": "Please count shelf and backroom stock separately."})]),
        response("Review the recorded options and the unresolved stock check.")])
    job = jobs.prepare_brief(run["run_id"], case["case_id"], baseline=True)
    frames = asyncio.run(drain(jobs.run_job(job)))
    assert frames[-1]["status"] == "CURRENT" and frames[-1]["provenance"] == "PRECOMPUTED"
    return run, case, fake


def test_reset_restores_exact_baseline_after_human_send_without_sharing_private_changes(checked_copy_source, template):
    from agent import jobs
    from api import db, sandbox, signals
    from tests.api.conftest import fresh, seat
    run, case, fake = checked_copy_source
    rid = run["run_id"]
    originals = {t: rows(rid, t) for t in ("ai_outputs", "tool_results", "evidence_drafts", "ai_usage")}
    before, caps = business_state(rid), all_ai_caps()
    db.in_tx(sandbox.activate_template, rid)
    try:
        with db.tx() as conn:
            first = sandbox.clone(conn, sandbox.active_template(conn))
            other = sandbox.clone(conn, sandbox.active_template(conn))
            db.run(conn, "INSERT INTO sessions (session_id, role, branch, account, run_id) "
                         "VALUES ('p09-copy-reset', 'manager', :b, :b, :r)", b=case["source_branch"], r=first)
        draft, = rows(first, "evidence_drafts")
        message = "  Manager wording: please count 987654321 units?\nReport what you actually see.  "
        with db.tx() as conn:
            signals.request_evidence(conn, db.lock_run(conn, first), seat(case["source_branch"]),
                case["case_id"], draft["rec_version"], None, message=message,
                draft_id=draft["draft_id"], plan_key=draft["plan_key"])
        sent, = rows(first, "evidence_requests")
        retained, = rows(first, "evidence_drafts")
        assert sent["message"] == message and sent["sent_at"] is not None
        assert retained["original_text"] == originals["evidence_drafts"][0]["original_text"]
        assert retained["checks"] == originals["evidence_drafts"][0]["checks"]
        assert "987654321" not in retained["original_text"]  # human text never becomes checked Gemini wording
        assert sent.get("checks") is None and sent.get("refs") is None
        new, _ = db.in_tx(sandbox.reset, 'p09-copy-reset')
        assert new != first and fresh(first)["status"] == "RETIRED"
        for dest in (new, other):
            for table, original in originals.items():
                copied = rows(dest, table)
                assert copied and all(row["run_id"] == dest for row in copied)
                assert [{k: v for k, v in r.items() if k != "run_id"} for r in copied] == [
                    {k: v for k, v in r.items() if k != "run_id"} for r in original], table
            assert rows(dest, "evidence_requests") == []
            with db.tx() as conn:
                listing = jobs.drafts(conn, fresh(dest), None, case["case_id"])
            assert listing["requests_left"] == 2 and listing["drafts"][0]["state"] == "DRAFT"
            for output in rows(dest, "ai_outputs"):
                assert {s["result_id"] for s in output["sources"]} <= {
                    r["result_id"] for r in rows(dest, "tool_results")}
        assert rows(first, "evidence_requests") == [sent]
        assert all_ai_caps() == caps and business_state(rid) == before and len(fake.requests) == 2
    finally:
        db.in_tx(sandbox.restore, rid)
        sandbox.clear_cache()
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template


def test_every_assistant_table_is_run_first_and_copied_without_overlay(checked_copy_source):
    from api import db, overlay, sandbox
    run, _, fake = checked_copy_source
    tables = {"ai_outputs", "tool_results", "evidence_drafts", "ai_usage", "chat_requests"}
    with db.tx() as conn:
        assert tables <= set(sandbox.per_run_tables(conn))
        assert not tables.intersection(sandbox.NOT_CLONED) and not tables.intersection(overlay.OVERLAID)
        assert all(sandbox.columns(conn, table)[0] == "run_id" for table in tables)
        clone = sandbox.clone(conn, run)
    assert len(rows(clone, "ai_usage")) == 1
    assert rows(clone, "ai_usage")[0]["precomputed"] is True
    assert len(fake.requests) == 2


def test_baseline_benchmark_refuses_unprepared_rows_instead_of_reporting_a_small_success(template, tmp_path, monkeypatch):
    from api import bench, config, worker
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    monkeypatch.setattr(worker, "run", lambda *args: None)  # no real timing in this error-path contract
    report = tmp_path / "unprepared.json"
    assert bench.main(["--baseline", "--runs", "2", "--report", str(report)]) == 2
    import json
    saved = json.loads(report.read_text())
    assert saved["baseline_complete"] is False and saved["eligible_cases"] > 0
    assert saved["prepared_cases"] == 0 and saved["case_open"] is None and saved["reset"] is None


@pytest.mark.parametrize("case_s,reset_s,passed", [(.749, .499, True), (.75, .5, True),
                                                  (.751, .5, False), (.75, .501, False)])
def test_baseline_budget_keeps_the_fixed_fourfold_margin_at_both_boundaries(case_s, reset_s, passed):
    from api import bench
    verdict = bench.assess_baseline_budget([case_s] * 10, [reset_s] * 10)
    assert verdict["production_targets_s"] == {"case_open": 3.0, "reset": 2.0}
    assert verdict["local_targets_s"] == {"case_open": .75, "reset": .5}
    assert verdict["passed"] is passed
    assert verdict["case_open"]["p50_s"] == verdict["case_open"]["p95_s"] == case_s
    assert verdict["reset"]["p50_s"] == verdict["reset"]["p95_s"] == reset_s


def _budget_fixture(schema, fake_model, monkeypatch, count=200):
    """Real checked tool shapes for every tiny-template case, scaled to 200 snapshots.

    Extra case identities are CLOSED historical snapshots of the actual checked
    story case (same immutable engine input). This avoids inventing engine
    outputs or violating the one-live-case-per-lot index. It is a volume fixture,
    not an observation of 200 distinct public-data cases or paid model output.
    """
    from api import cases, commitment, db, sandbox
    run = inactive(schema)
    rid = run["run_id"]
    prose = ("The engine recommendation compares keeping this stock under its recorded markdown schedule "
        "with the available alternatives. Review the calculated options, their operating constraints and "
        "the unresolved fact checks before deciding. Recorded quantities describe the stock record; they "
        "do not confirm what is on the shelf or in the backroom. A flagged check can change feasibility "
        "or the expected result. Any evidence request remains a draft until the source manager reviews "
        "and sends it. The manager owns the decision, and the calculated comparison remains available "
        "when the assistant is unavailable.")
    script = []
    with db.tx() as conn:
        for cid, _ in eligible(run):
            case = cases.case_row(conn, rid, cid)
            rec = cases.active_rec(conn, case)
            calls = [("get_case", {}), ("get_forecast", {"branch": case["source_branch"]}),
                ("get_inventory_and_inbound", {"branch": case["source_branch"]}),
                ("get_transport_options", {}), ("get_fact_checks", {})]
            if case["state"] in cases.LIVE_STATES:
                fact = next((f for f in rec["fact_checks"] if f["kind"] in commitment.ASKABLE
                    and (not f["pick_feasible_if_alt"] or f["changes_plan"])), None)
                if fact:
                    calls.append(("draft_evidence_request", {"fact_id": fact["fact_id"], "ask_text":
                        "Please check the recorded stock for the attached lots. Count shelf and backroom "
                        "stock separately and report what you observe, including any difference from the "
                        "record. Keep the lot identifiers with each count so the manager can review the "
                        "evidence before deciding. This draft does not confirm stock or authorize movement."}))
            script.extend([response(calls=calls), response(prose)])
    fake = install(fake_model, monkeypatch, script)
    caps = all_ai_caps()
    baseline(rid)
    assert len(successful(rid)) == len(eligible(run)) and fake.script == []
    originals = {table: rows(rid, table) for table in ("cases", "recommendations", "ai_outputs",
        "tool_results", "evidence_drafts", "ai_usage")}
    original_count = len(originals["cases"])
    drafted = originals["evidence_drafts"][0]
    source = next(c for c in originals["cases"] if c["case_id"] == drafted["case_id"])
    output = next(o for o in originals["ai_outputs"] if o["case_id"] == source["case_id"])

    def insert(conn, table, row):
        values = {f"v{i}": db.js(v) if isinstance(v, (dict, list)) else v for i, v in enumerate(row.values())}
        params = [f"CAST(:v{i} AS jsonb)" if isinstance(v, (dict, list)) else f":v{i}"
                  for i, v in enumerate(row.values())]
        db.run(conn, f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join(params)})", **values)

    for i in range(original_count, count):
        cid, key = f"bench-case-{i:04d}", f"BENCH-{i:04d}"
        mapping = {source["case_id"]: cid, source["key"]: key,
                   output["output_id"]: f"bench-output-{i:04d}"}
        for table, id_key in (("tool_results", "result_id"), ("evidence_drafts", "draft_id")):
            for row in originals[table]:
                if row["case_id"] == source["case_id"]:
                    mapping[row[id_key]] = f"bench-{i:04d}-{row[id_key]}"

        def remap(value):
            if isinstance(value, dict):
                return {k: remap(v) for k, v in value.items()}
            if isinstance(value, list):
                return [remap(v) for v in value]
            return mapping.get(value, value) if isinstance(value, str) else value

        with db.tx() as conn:
            for table, saved in originals.items():
                for row in saved:
                    if row["case_id"] != source["case_id"]:
                        continue
                    copied = remap(row)
                    if table == "cases":
                        copied.update(state="CLOSED", closed_as="REJECTED", tag=None)
                    insert(conn, table, copied)
    assert len(rows(rid, "cases")) == count and len(successful(rid)) == count
    assert all_ai_caps() == caps
    return run, fake, {"kind": "scaled checked synthetic snapshots", "cases": count,
        "original_cases": original_count, "note": "Additional identities repeat a checked immutable story snapshot; not a production public-data measurement."}


def test_baseline_copy_budget(schema, template, fake_model, monkeypatch, record_property):
    import json
    import os
    from pathlib import Path
    from api import bench, config, db, sandbox, worker
    if os.environ.get("P09_MEASURE_BASELINE") != "1":
        pytest.skip("Budget measurement runs explicitly under the shared timing slot")
    slot = Path('/Users/kt/Desktop/AI Builder/ShelfShift/.git/shelfshift-test-slot')
    token = os.environ.get("P09_OWNER", "")
    assert token and '09-15' in token and (slot / 'owner').read_text() == token
    run, fake, fixture = _budget_fixture(schema, fake_model, monkeypatch)
    calls = len(fake.requests)
    caps = all_ai_caps()
    before = business_state(run["run_id"])
    db.in_tx(sandbox.activate_template, run["run_id"])
    path = Path('/tmp/p09-15-budget.json')
    monkeypatch.setattr(config, "ENGINE_WORKER", True)  # same warm process path as existing api.bench
    import multiprocessing as mp
    # The existing offline guard forbids Unix-domain forkserver sockets. Use
    # the worker's supported spawn context: identical warm submit/result path,
    # with the original external-network and paid-client guards left intact.
    monkeypatch.setattr(worker, "_context", lambda: mp.get_context("spawn"))
    try:
        code = bench.main(["--baseline", "--runs", "10", "--report", str(path)])
        report = json.loads(path.read_text())
        report["fixture"] = fixture
        path.write_text(json.dumps(report, indent=2) + '\n')
        record_property("budget_report", json.dumps(report))
        assert code == (0 if report["budget"]["passed"] else 1)
        assert report["baseline_complete"] is True
        assert report["eligible_cases"] == report["prepared_cases"] == 200
        assert report["rows"]["ai_outputs"]["rows"] == 200
        assert report["rows"]["tool_results"]["rows"] >= 1000
        assert report["rows"]["evidence_drafts"]["rows"] >= 190
        assert report["rows"]["tool_results"]["logical_bytes"] > 1_000_000
        assert report["case_open"]["count"] >= 200 and report["reset"]["count"] == 10
        assert report["creation"]["count"] == 1 and report["machine"]["load_before"]
        assert report["machine"]["engine_worker"] is True and worker.pid() is None
        assert report["machine"]["worker_start_method"] == "spawn"
        assert report["rows"]["chat_requests"]["rows"] == 0
        assert len(fake.requests) == calls and business_state(run["run_id"]) == before and all_ai_caps() == caps
    finally:
        db.in_tx(sandbox.restore, run["run_id"])
        sandbox.clear_cache()
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template
