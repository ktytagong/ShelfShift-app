"""Real reading evaluations through the CLI boundary, public services and local fakes.

Reference locks are temporary test fixtures. Nothing writes release approvals.
"""
from __future__ import annotations

from contextvars import Context
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from tests.api import conftest as api_fixtures  # Credentials precede API imports.
from tests.agent.test_eval_runner import eval_id, public_dv, public_template, eval_dir  # noqa: F401
from tests.agent.test_readings_http import FakeModels


def reading_provider(monkeypatch):
    from agent import model
    from agent.reading import Reading
    clients = []

    async def close():
        pass

    def factory():
        models = FakeModels(Reading(kind="NOT_RELEVANT"))
        clients.append(models)
        return SimpleNamespace(api_client=SimpleNamespace(aio=SimpleNamespace(models=models, aclose=close)))

    monkeypatch.setattr(model, "llm", factory)
    return clients


def counters():
    from api import db
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope, kind, window_start")


def run_s09_cli(monkeypatch, eval_id, template, root, evidence, *, during_eval=None):
    """Select a test-local input set while retaining the real CLI and runner."""
    from agent.eval import fingerprint, runner
    from agent.eval.budget import EvalBudget
    refs = json.loads((root / "references.json").read_text())
    (root / "FINGERPRINT").write_text(fingerprint.compute(root, refs["dataset_version"], refs["engine_version"]))
    budget = EvalBudget(20, 1_000_000, 100, {"input": 1, "output": 1})
    original = runner.run

    def local_inputs(eval_key, template_key, **kwargs):
        if during_eval is not None:
            during_eval()
        kwargs.update(scenarios=["s09"], eval_dir=root, budget=budget)
        return original(eval_key, template_key, **kwargs)

    monkeypatch.setattr(runner, "run", local_inputs)
    try:
        result = runner.main(["--eval-id", eval_id, "--template", template, "--runs", "3",
                              "--evidence", str(evidence)])
    except ValueError as exc:
        # The existing public CLI deliberately refuses real bindings. Capture
        # that behavior as the RED assertion, never an import/setup failure.
        if str(exc) != "Real evaluation step bindings require plan 10-15":
            raise
        result = str(exc)
    assert result == 0, f"A real reading evaluation must complete: {result}"
    return budget


def test_s09_through_the_real_reading_service(eval_id, public_template, eval_dir, tmp_path, monkeypatch):
    from api import db
    providers = reading_provider(monkeypatch)
    before = counters()
    evidence = tmp_path / "evidence"
    budget = run_s09_cli(monkeypatch, eval_id, public_template, eval_dir, evidence)
    assert counters() == before
    assert budget.totals()["calls"] == 3 and sum(len(client.calls) for client in providers) == 3
    with db.tx() as conn:
        attempts = db.rows(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e ORDER BY run_no", e=eval_id)
        items = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e", e=eval_id)
        result = db.one(conn, "SELECT * FROM eval_runs WHERE eval_id=:e", e=eval_id)
        readings = [db.rows(conn, "SELECT * FROM readings WHERE run_id=:r", r=row["workspace_run_id"])
                    for row in attempts]
    assert result["status"] == "COMPLETE" and len(attempts) == 3
    assert len({row["workspace_run_id"] for row in attempts}) == 3
    assert all(row["valid"] and row["model_calls"] == 1 and row["cache_hits"] == 0 for row in attempts)
    assert items and all(row["result"] == "PASS" for row in items)
    assert all(len(found) == 1 and found[0]["status"] == "DONE" for found in readings)
    for number in range(1, 4):
        trace = json.loads((evidence / "s09" / f"{number}.json").read_text())
        step = trace["steps"]["reading"]
        assert step["output"]["kind"] == "NOT_RELEVANT" and not step["test_double"]


def test_admission_context_is_eval_only(eval_id, public_template, eval_dir, client, tmp_path, monkeypatch):
    from api import caps
    from api.app import COOKIE
    providers = reading_provider(monkeypatch)
    # The cookie names this HTTP identity exactly; a workspace may contain
    # other stored sessions, so its first database row is not authoritative.
    session_id = client.cookies.get(COOKIE)
    assert session_id
    def spent(scope):
        # Session AI is hourly; global AI is daily. Compare actual rows so
        # crossing a policy window cannot make the spend look absent.
        return sum(row["count"] for row in counters() if row["scope"] == scope and row["kind"] == "ai")

    before = {scope: spent(scope) for scope in (caps.GLOBAL, session_id)}
    independent = []

    def during_eval():
        assert caps.EVAL_ADMISSION.get() is True
        assert Context().run(caps.EVAL_ADMISSION.get) is False
        response = Context().run(client.post, "/api/readings", files={"text": (None, "Staff party Friday.")})
        assert response.status_code == 200, response.text
        assert response.json()["served"] == "live"
        for scope, count in before.items():
            assert spent(scope) == count + 1
        independent.append(counters())

    budget = run_s09_cli(monkeypatch, eval_id, public_template, eval_dir, tmp_path / "isolated",
                         during_eval=during_eval)
    assert len(independent) == 1 and counters() == independent[0]
    assert caps.EVAL_ADMISSION.get() is False
    assert budget.totals()["calls"] == 3 and sum(len(client.calls) for client in providers) == 4


def lock_fixture(root):
    from agent.eval import fingerprint
    refs = json.loads((root / "references.json").read_text())
    (root / "FINGERPRINT").write_text(fingerprint.compute(root, refs["dataset_version"], refs["engine_version"]))


@pytest.fixture
def preflight_fixture(eval_dir, monkeypatch):
    """Only the single-scenario test inputs are approved in this fixture."""
    from agent import model
    from agent.eval import cli
    from api import config
    lock_fixture(eval_dir)
    monkeypatch.setattr(config, "env", lambda name, default=None: "isolated-test-key" if name in
                        ("EVAL_BUCKET", "GEMINI_MODEL") else default)
    monkeypatch.setattr(cli, "_committed_fingerprint", lambda root, value: True, raising=False)
    calls = []

    def forbidden_model():
        calls.append(True)
        raise AssertionError("Preflight must never create a model client")

    monkeypatch.setattr(model, "llm", forbidden_model)
    yield eval_dir, calls
    assert not calls


def preflight(template, root):
    # Exercise the real public dispatcher even before its command is built.
    from agent.eval.__main__ import main
    return main(["preflight", "--template", template, "--eval-dir", str(root)])


def test_preflight_accepts_checked_test_inputs(public_template, preflight_fixture, capsys):
    root, _ = preflight_fixture
    assert preflight(public_template, root) == 0
    assert capsys.readouterr().out.strip() == "PREFLIGHT-OK"


@pytest.mark.parametrize("fault, expected", [
    ("missing_lock", "FINGERPRINT"), ("mismatch", "match"), ("uncommitted", "committed"),
    ("dataset", "versions differ"), ("engine", "versions differ"),
    ("selector", "selector does not resolve"), ("pending", "PENDING"),
    ("unbound", "unbound"), ("caps", "caps/prices"),
    ("EVAL_BUCKET", "EVAL_BUCKET"), ("GEMINI_MODEL", "GEMINI_MODEL"),
    ("template", "Template does not exist"), ("malformed", "Evaluation inputs"),
])
def test_preflight_refusals(public_template, preflight_fixture, monkeypatch, capsys, fault, expected):
    from agent.eval import cli, steps
    from api import config
    root, _ = preflight_fixture
    template = public_template
    if fault == "missing_lock":
        (root / "FINGERPRINT").unlink()
    elif fault == "mismatch":
        (root / "FINGERPRINT").write_text("0" * 64)
    elif fault == "uncommitted":
        monkeypatch.setattr(cli, "_committed_fingerprint", lambda *args: False)
    elif fault in ("dataset", "engine", "pending"):
        path = root / "references.json"
        doc = json.loads(path.read_text())
        if fault == "pending":
            doc["scenarios"]["s09"]["pending"] = "PENDING test dependency"
        else:
            doc[fault + "_version"] = "different-test-version"
        path.write_text(json.dumps(doc))
        lock_fixture(root)
    elif fault == "selector":
        path = root / "scenarios.json"
        doc = json.loads(path.read_text())
        doc[0]["selector"] = {"tag": "no-matching-eval-case"}
        path.write_text(json.dumps(doc))
        lock_fixture(root)
    elif fault == "unbound":
        monkeypatch.setattr(steps, "REAL_STEPS", {})
    elif fault == "caps":
        monkeypatch.setattr(config, "EVAL", {**config.EVAL, "max_calls": 0})
    elif fault in ("EVAL_BUCKET", "GEMINI_MODEL"):
        original = config.env
        monkeypatch.setattr(config, "env", lambda key, default=None: " " if key == fault else original(key, default))
    elif fault == "template":
        template = "missing-test-template"
    elif fault == "malformed":
        (root / "references.json").write_text("[]")
    assert preflight(template, root) == 1
    output = capsys.readouterr().out
    assert expected in output and "PREFLIGHT-OK" not in output


def test_preflight_reports_independent_failures(public_template, preflight_fixture, monkeypatch, capsys):
    from agent.eval import steps
    from api import config
    root, _ = preflight_fixture
    (root / "FINGERPRINT").unlink()
    monkeypatch.setattr(steps, "REAL_STEPS", {})
    monkeypatch.setattr(config, "EVAL", {**config.EVAL, "max_tokens": 0})
    monkeypatch.setattr(config, "env", lambda *args: None)
    assert preflight(public_template, root) == 1
    output = capsys.readouterr().out
    assert all(label in output for label in ("FINGERPRINT", "unbound", "caps/prices", "EVAL_BUCKET", "GEMINI_MODEL"))


def test_preflight_refuses_missing_binding_in_input_set(public_template, preflight_fixture, monkeypatch, capsys):
    from agent.eval import steps
    root, _ = preflight_fixture
    path = root / "scenarios.json"
    doc = json.loads(path.read_text())
    doc[0]["steps"].append({"action": "ai:summary"})
    path.write_text(json.dumps(doc))
    lock_fixture(root)
    # Explicit incomplete map stays refused even after Task 2 binds summary.
    monkeypatch.setattr(steps, "REAL_STEPS", {"reading": steps.REAL_STEPS["reading"]})
    assert preflight(public_template, root) == 1
    assert "unbound" in capsys.readouterr().out


def test_runbook_is_public_safe():
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    header = "## AI evaluation (user-run)"
    assert header in readme, "The user needs the ordered evaluation runbook"
    text = readme.split(header, 1)[1].split("\n## ", 1)[0]
    assert "Preliminary evaluation · 10 scenarios × 3 runs" in text
    assert all(key in text for key in ("EVAL_JOB", "EVAL_BUCKET", "GEMINI_MODEL"))
    assert "http" not in text.lower() and "@" not in text and not re.search(r"\$\d|gs://[^\s\"']+", text)
    assert "accuracy" not in text.lower()
    commands = ["agent.eval approval-sheet", "agent.eval preflight", "jobs execute", "agent.eval export", "spreadsheet",
                "agent.eval import", "agent.eval publish"]
    assert [text.index(command) for command in commands] == sorted(text.index(command) for command in commands)
    assert all(flag in text for flag in ("--max-calls", "--max-tokens", "--max-cost-usd"))
    assert "About" in text


@pytest.fixture(scope="module")
def real_eval_template(public_template):
    """A separate real template where halving capacity changes the transfer.

    Required totes are ceil(units / units_per_tote). Confirming precisely that
    capacity retains the current feasible transfer; halving it cannot fit the
    same quantity. This creates a service precondition, not an expected result.
    No checked-in data or reference fixture is changed.
    """
    import math
    from agent.eval import refs
    from api import cases, db, refdata, reports, sandbox, selection
    from api.simplerule import parse_plan
    with db.tx() as conn:
        original = sandbox.active_template(conn)["run_id"]
        template = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=public_template)
    run_id = sandbox.build_template(template["dataset_version"])
    try:
        with db.tx() as conn:
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=run_id)
            case = refs.select_case(conn, run, {"tag": "transfer"})
            rec = cases.active_rec(conn, case)
            plan = parse_plan(selection.plan_in_force(case, rec)[0])
            product = refdata.get(run["dataset_version"]).products[case["sku"]]
            capacity = math.ceil(plan.qty / product.units_per_tote)
            assert capacity > 0
            fields = {"zone": product.zone, "free_totes": capacity,
                      "valid_from": cases.iso_of(run, run["day"]),
                      "valid_to": cases.iso_of(run, run["day"] + 1)}
            actor = {"role": "manager", "branch": plan.dest, "account": plan.dest}
            report = reports.create(conn, run, actor, "CAPACITY", fields, "Checked test capacity.")
            reports.confirm(conn, run, actor, report["signal_id"], report["revision"])
        yield run_id
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status='RETIRED' WHERE run_id=:r", r=run_id)
            db.run(conn, "UPDATE demo_runs SET status='ACTIVE' WHERE run_id=:r", r=original)
            children = [row["run_id"] for row in db.rows(conn,
                "SELECT run_id FROM demo_runs WHERE template_run_id=:r", r=run_id)]
            api_fixtures.drop_runs(conn, [run_id, *children])


@pytest.fixture
def real_eval_dir(real_eval_template, tmp_path):
    """References come from real deterministic services on the local template."""
    import shutil
    from agent.eval import refs
    from api import db
    root = tmp_path / "real-inputs"
    root.mkdir()
    source = Path(__file__).resolve().parents[2] / "agent/eval"
    scenarios = json.loads((source / "scenarios.json").read_text())
    with db.tx() as conn:
        references = refs.build_refs(conn, real_eval_template, scenarios)
    assert not any("pending" in row for row in references["scenarios"].values())
    assert any(row["changed"] for row in references["scenarios"]["s06"]["expected"]["preview"]["rows"])
    shutil.copytree(source / "attachments", root / "attachments")
    for name, document in (("scenarios", scenarios), ("references", references),
                            ("rubric", json.loads((source / "rubric.json").read_text()))):
        (root / (name + ".json")).write_text(json.dumps(document))
    lock_fixture(root)
    return root


def run_real_cli(monkeypatch, eval_id, template, root, evidence, *, calls=100, **options):
    from agent.eval import runner
    from agent.eval.budget import EvalBudget
    budget = EvalBudget(calls, 10_000_000, 100, {"input": 1, "output": 1})
    original, results = runner.run, []

    def local_inputs(eval_key, template_key, **kwargs):
        kwargs.update(eval_dir=root, budget=budget, **options)
        result = original(eval_key, template_key, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(runner, "run", local_inputs)
    exit_code = runner.main(["--eval-id", eval_id, "--template", template,
                             "--evidence", str(evidence)])
    return results[0], budget, exit_code


def all_service_providers(monkeypatch, fake_model, *, final_text=None):
    from agent import jobs, model, reading
    from agent.eval import steps
    from google.adk.models import LlmResponse
    from google.genai import types
    clients, agents = [], []

    def structured(ctx):
        scenario = ctx.scenario["id"]
        if scenario == "s06":
            fields = ctx.references["expected"]["reading_fields"]
            return reading.Reading(kind="CAPACITY", zone=fields["zone"], capacity_share=.5,
                valid_from=fields["valid_from"], valid_to=fields["valid_to"], evidence=[
                    reading.Evidence(field=name, source="message", excerpt=quote, confidence=.9)
                    for name, quote in (("kind", "broken"), ("zone", "chilled"), ("capacity_share", "half"),
                        ("valid_from", "until tomorrow"), ("valid_to", "until tomorrow"))])
        if scenario == "s07":
            fields = ctx.references["expected"]["reading_fields"]
            return reading.Reading(kind="TRANSPORT", **{k: v for k, v in fields.items() if k != "kind"},
                evidence=[reading.Evidence(field=name, source="file", excerpt="Carrier quote", confidence=.9)
                          for name in ("kind", "what", "run_code", "amount")])
        if scenario == "s08":
            if ctx.step["id"] == "reading1":
                return reading.Reading(kind="STOCK_COUNT", photo_observation="A milk slot is visible.")
            return reading.Reading(kind="STOCK_COUNT", lot_id=ctx.inputs["lot_id"], shelf_units=0, backroom_units=0,
                evidence=[reading.Evidence(field=name, source="message", excerpt=quote, confidence=.9)
                          for name, quote in (("kind", "None on the shelf"), ("lot_id", "None on the shelf"),
                              ("shelf_units", "None on the shelf"), ("backroom_units", "none in the backroom"))])
        return reading.Reading(kind="NOT_RELEVANT", contains_instructions=scenario == "s10")

    def response(*parts):
        return LlmResponse(content=types.Content(role="model", parts=list(parts)),
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=100,
                candidates_token_count=30, thoughts_token_count=5, total_token_count=135))

    async def close():
        pass

    def factory():
        current = steps._current.get()
        assert current is not None, "Only actual evaluation steps request these fake providers"
        ctx = current.ctx
        if ctx.step["action"] in ("ai:reading", "ai:answer_reading"):
            provider = FakeModels(structured(ctx))
            clients.append(provider)
            return SimpleNamespace(api_client=SimpleNamespace(aio=SimpleNamespace(models=provider, aclose=close)))
        final = response(types.Part(text=final_text or
                                     "If these fields were applied, the affected plans could be reviewed."))
        turns = [final]
        if ctx.step["action"] == "ai:summary":
            turns = [response(types.Part(function_call=types.FunctionCall(name="get_impact_preview", args={}))), final]
        elif ctx.scenario["id"] == "s01" and ctx.step["action"] == "ai:brief":
            fact = ctx.references["expected"]["fact"]
            turns = [response(types.Part(function_call=types.FunctionCall(name="draft_evidence_request",
                args={"fact_id": fact["fact_id"], "ask_text": "Please check this stock record before confirming."}))), final]
        agent = fake_model(turns)
        agents.append(agent)
        return agent

    monkeypatch.setattr(model, "llm", factory)
    monkeypatch.setattr(jobs, "ai_on", lambda: True)
    return clients, agents


def test_full_eval_through_real_services(eval_id, real_eval_template, real_eval_dir, tmp_path, monkeypatch, fake_model):
    from api import config, db
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    clients, agents = all_service_providers(monkeypatch, fake_model)
    before = counters()
    evidence = tmp_path / "full-evidence"
    status, budget, exit_code = run_real_cli(monkeypatch, eval_id, real_eval_template, real_eval_dir, evidence, runs=3)
    assert status == "COMPLETE" and exit_code == 0, "Every scenario must finish through its real service"
    assert counters() == before
    with db.tx() as conn:
        attempts = db.rows(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
        items = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e", e=eval_id)
        first = [row["workspace_run_id"] for row in attempts if row["scenario_id"] == 1]
        requests = sum(db.one(conn, "SELECT count(*) AS n FROM evidence_requests WHERE run_id=:r", r=run)["n"]
                       for run in first)
        drafts = [db.rows(conn, "SELECT * FROM evidence_drafts WHERE run_id=:r", r=run) for run in first]
        draft_audits = [db.rows(conn, "SELECT scope FROM tool_results WHERE run_id=:r "
                               "AND tool='draft_evidence_request'", r=run) for run in first]
        reports = [db.rows(conn, "SELECT s.reading_id, s.field_sources, s.text, s.fields, q.suggestions "
                           "FROM signals s JOIN readings q "
                           "ON q.run_id=s.run_id AND q.reading_id=s.reading_id WHERE s.run_id=:r "
                           "AND s.report_id IS NOT NULL", r=row["workspace_run_id"])
                   for row in attempts if row["scenario_id"] == 6]
        answers = [db.rows(conn, "SELECT signal_id, signal_revision, status FROM readings "
                           "WHERE run_id=:r AND purpose='ANSWER' ORDER BY signal_revision", r=row["workspace_run_id"])
                   for row in attempts if row["scenario_id"] == 8]
    assert len(attempts) == 30 and len({row["workspace_run_id"] for row in attempts}) == 30
    assert all(row["valid"] and row["model_calls"] > 0 and row["cache_hits"] == 0 for row in attempts)
    assert len(drafts) == 3 and all(len(rows) == 1 for rows in drafts) and requests == 0
    assert all(rows == [{"scope": "CASE"}] for rows in draft_audits)
    assert len(reports) == 3 and all(len(rows) == 1 for rows in reports)
    for rows in reports:
        submitted = rows[0]
        suggestion = next(row["value"] for row in submitted["suggestions"] if row["field"] == "free_totes")
        expected_origin = "gemini_accepted" if suggestion == submitted["fields"]["free_totes"] else "gemini_edited"
        assert submitted["field_sources"]["free_totes"] == expected_origin
    assert len(answers) == 3 and all(len(rows) == 2 and rows[0]["signal_revision"] < rows[1]["signal_revision"]
                                    and all(row["status"] == "DONE" for row in rows) for rows in answers)
    protections = [row for row in items if row["facet"] == "protection"]
    assert protections and all(row["result"] == "PASS" for row in protections)
    assert budget.totals()["calls"] == sum(len(client.calls) for client in clients) + sum(len(a.requests) for a in agents)
    for number in range(1, 4):
        trace = json.loads((evidence / "s01" / f"{number}.json").read_text())
        assert any(call["name"] == "draft_evidence_request" for call in trace["steps"]["brief"]["tool_calls"])
        assert trace["steps"]["ask"]["raw_text"] and trace["steps"]["ask"]["displayed_text"]
        summary = json.loads((evidence / "s06" / f"{number}.json").read_text())["steps"]["summary"]
        assert summary["preview_rows"] and summary["tool_results"] and summary["raw_text"]


def test_real_preview_uses_cases_contract(real_eval_template, real_eval_dir):
    from api import db, sandbox
    from agent.eval import runner
    references = json.loads((real_eval_dir / "references.json").read_text())["scenarios"]["s06"]
    scenario = next(row for row in json.loads((real_eval_dir / "scenarios.json").read_text()) if row["id"] == "s06")
    with db.tx() as conn:
        template = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=real_eval_template)
        run_id = sandbox.clone(conn, template)
        workspace = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=run_id)
    inputs = references["resolved_inputs"]
    ctx = runner.StepContext(workspace, runner._actors(inputs), scenario, inputs, references,
                             {"action": "human:submit_report", "fields_ref": "report_fields"})
    try:
        runner._human(ctx)
        ctx.step = {"action": "human:preview"}
        try:
            preview = runner._human(ctx)
        except KeyError as exc:
            if exc.args != ("rows",):
                raise
            preview = None
        assert preview is not None, "Evaluation must consume the real preview's cases, not obsolete rows"
        assert ctx.state["preview_rows"] == preview["cases"] and preview["cases"]
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status='RETIRED', retired_at=now() WHERE run_id=:r", r=run_id)


def test_budget_refuses_a_step_that_could_overflow(eval_id, real_eval_template, real_eval_dir, tmp_path, monkeypatch, fake_model):
    from api import db
    clients, agents = all_service_providers(monkeypatch, fake_model)
    status, budget, exit_code = run_real_cli(monkeypatch, eval_id, real_eval_template, real_eval_dir,
        tmp_path / "capped", calls=5, scenarios=["s01"], runs=1)
    assert status == "CAPPED" and exit_code == 1
    assert budget.totals()["calls"] == 0 and not clients and not agents
    with db.tx() as conn:
        attempt = db.one(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
    assert "maximum model calls" in attempt["invalid_reason"]


def test_reuse_makes_a_run_invalid(eval_id, public_template, eval_dir, tmp_path, monkeypatch):
    import asyncio
    from agent import readings_job
    from agent.eval import steps
    from api import db
    clients = reading_provider(monkeypatch)
    lock_fixture(eval_dir)

    def reused(ctx):
        session = steps._session(ctx, steps._reading_seat(ctx))
        asyncio.run(readings_job.read_report(session, ctx.step["prompt"], case_id=ctx.inputs["case_id"]))
        return steps.REAL_STEPS["reading"](ctx)

    status, budget, exit_code = run_real_cli(monkeypatch, eval_id, public_template, eval_dir,
        tmp_path / "cache", scenarios=["s09"], runs=1, steps={**steps.REAL_STEPS, "reading": reused})
    with db.tx() as conn:
        attempt = db.one(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
        items = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e", e=eval_id)
    assert status == "COMPLETE" and exit_code == 0 and not attempt["valid"]
    assert "cache" in attempt["invalid_reason"].lower() and not items
    assert budget.totals()["calls"] == 0 and sum(len(client.calls) for client in clients) == 1


def test_summary_trace_keeps_unchecked_candidate(eval_id, real_eval_template, real_eval_dir, tmp_path, monkeypatch, fake_model):
    from api import db
    all_service_providers(monkeypatch, fake_model,
                          final_text="If confirmed, there would be 987654321 affected units.")
    evidence = tmp_path / "unchecked-summary"
    status, _, exit_code = run_real_cli(monkeypatch, eval_id, real_eval_template, real_eval_dir,
                                       evidence, scenarios=["s06"], runs=1)
    assert status == "COMPLETE" and exit_code == 0
    summary = json.loads((evidence / "s06/1.json").read_text())["steps"]["summary"]
    assert "987654321" in summary["raw_text"] and "987654321" not in summary["displayed_text"]
    own = summary["output"]["output_id"]
    with db.tx() as conn:
        result_ids = {row["result_id"] for row in db.rows(conn,
            "SELECT result_id FROM tool_results WHERE request_id=:q AND scope='SIGNAL'", q=own)}
    assert set(summary["tool_results"]) == result_ids and result_ids


def test_summary_reserves_its_own_policy(monkeypatch):
    from agent import impact
    from agent.eval import steps
    from api import config
    monkeypatch.setattr(config, "SETTINGS", {**config.SETTINGS, "impact": {
        **config.SETTINGS["impact"], "max_tool_calls": 16, "max_output_tokens": 2048}})
    try:
        maximum = steps.maximum_calls("summary")
    except ValueError as exc:
        if str(exc) != "The evaluation service binding is not available":
            raise
        maximum = None
    assert maximum == impact.limits().max_tool_calls + 3
    meter = steps._Meter(SimpleNamespace(step={"action": "ai:summary"}))
    assert meter.input_limit == impact.limits().max_request_tokens
    assert meter.output_limit == impact.limits().max_output_tokens


def generation_payload():
    return {"contents": [{"role": "user", "parts": [{"text": "Check these fields."}]}],
            "systemInstruction": {"parts": [{"text": "Use only checked records."}]},
            "tools": [{"functionDeclarations": [{"name": "get_case", "parameters": {
                "type": "OBJECT", "properties": {"case": {"type": "STRING"}}}}]}],
            "generationConfig": {"maxOutputTokens": 8, "responseMimeType": "application/json",
                                 "responseSchema": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}}}}


def transport_fixture(monkeypatch, token_counts, *, success=False):
    import httpx
    from agent import impact, model
    from agent.eval import steps
    from agent.eval.budget import EvalBudget
    from api import config
    monkeypatch.setattr(config, "READING", {**config.READING, "max_input_tokens": 100, "max_output_tokens": 8})
    operations, counted = [], []
    counts = iter(token_counts)

    async def send(request):
        if request.url.path.endswith(":countTokens"):
            operations.append("count")
            counted.append(json.loads(await request.aread()))
            return httpx.Response(200, json={"totalTokens": next(counts)}, request=request)
        operations.append("generation")
        if success:
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [{"text": "Checked."}]},
                                                               "finishReason": "STOP"}]}, request=request)
        return httpx.Response(429, json={"error": "test retry"}, request=request)

    steps._install_observers()
    budget = EvalBudget(20, 10_000, 100, {"input": 1, "output": 1})
    meter = steps._Meter(SimpleNamespace(step={"action": "ai:reading"}, budget=budget))
    stats = {"model_calls": 1, "provider_attempts": None, "retries": None}
    transport = impact.MeteredTransport(model.AttemptTransport(httpx.MockTransport(send)), stats)
    return transport, meter, stats, operations, counted


@pytest.mark.parametrize("measured", [101, None, -1, True])
def test_oversized_or_unmeasurable_prompt_never_generates(monkeypatch, no_network, measured):
    import asyncio
    import httpx
    from agent.eval import steps
    transport, meter, stats, operations, counted = transport_fixture(monkeypatch, [measured])
    payload = generation_payload()

    async def invoke():
        token = steps._current.set(meter)
        try:
            try:
                await transport.handle_async_request(httpx.Request("POST",
                    "https://example.invalid/v1/projects/test/locations/test/publishers/google/models/test:generateContent",
                    json=payload))
            except ValueError:
                pass
        finally:
            steps._current.reset(token)
            await transport.aclose()

    asyncio.run(invoke())
    assert operations == ["count"], "Input size must be proven before the first generation transport"
    assert counted == [payload]
    assert meter.calls == 0 and meter.ctx.budget.totals()["calls"] == 0
    assert stats["provider_attempts"] is None and stats["retries"] is None


@pytest.mark.parametrize("retry", [False, True])
def test_grown_history_and_retry_are_rechecked_before_generation(monkeypatch, no_network, retry):
    import asyncio
    import httpx
    from agent.eval import steps
    transport, meter, stats, operations, counted = transport_fixture(monkeypatch, [50, 101])
    first = generation_payload()
    second = json.loads(json.dumps(first))
    if not retry:
        second["contents"].append({"role": "user", "parts": [{"functionResponse": {
            "name": "get_case", "response": {"history": "long tool history " * 100}}}]})

    async def invoke():
        token = steps._current.set(meter)
        try:
            response = await transport.handle_async_request(httpx.Request("POST",
                "https://example.invalid/v1/projects/test/locations/test/publishers/google/models/test:streamGenerateContent?alt=sse",
                json=first))
            await response.aclose()
            stats["model_calls"] += int(not retry)
            try:
                await transport.handle_async_request(httpx.Request("POST",
                    "https://example.invalid/v1/projects/test/locations/test/publishers/google/models/test:streamGenerateContent?alt=sse",
                    json=second))
            except ValueError:
                pass
        finally:
            steps._current.reset(token)
            await transport.aclose()

    asyncio.run(invoke())
    assert operations == ["count", "generation", "count"], "No next generation may precede its own complete prompt check"
    assert counted == [first, second]
    assert meter.calls == 1 and meter.ctx.budget.totals()["calls"] == 1
    assert stats["provider_attempts"] == 1 and stats["retries"] == 0


def test_real_sdk_serializes_the_complete_counted_prompt(monkeypatch, no_network):
    import asyncio
    import httpx
    from google.genai import _common, models, types
    from agent.eval import steps
    transport, meter, stats, operations, counted = transport_fixture(monkeypatch, [50], success=True)
    declaration = {"name": "get_case", "parameters": {"type": "OBJECT", "properties": {"case": {"type": "STRING"}}}}
    schema = {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}}
    # Exercise the pinned SDK's pure wire converter, without constructing an
    # SDK client or relaxing the offline client's constructor sentinel.
    payload = models._GenerateContentParameters_to_vertex(SimpleNamespace(vertexai=True), {
        "model": "gemini-test", "contents": "Check these fields.",
        "config": types.GenerateContentConfig(system_instruction="Use only checked records.",
            tools=[types.Tool(function_declarations=[declaration])], max_output_tokens=8,
            response_mime_type="application/json", response_schema=schema)})
    payload.pop("_url")  # The SDK moves this routing metadata into the URL.
    payload = _common.encode_unserializable_types(_common.convert_to_dict(payload))

    async def invoke():
        token = steps._current.set(meter)
        try:
            response = await transport.handle_async_request(httpx.Request("POST",
                "https://example.invalid/v1/projects/test/locations/test/publishers/google/models/gemini-test:generateContent",
                json=payload))
            assert response.json()["candidates"][0]["content"]["parts"][0]["text"] == "Checked."
            await response.aclose()
        finally:
            steps._current.reset(token)
            await transport.aclose()

    asyncio.run(invoke())
    assert operations == ["count", "generation"] and len(counted) == 1
    assert counted[0]["contents"][0]["parts"][0]["text"] == "Check these fields."
    assert counted[0]["systemInstruction"]["parts"][0]["text"] == "Use only checked records."
    assert counted[0]["tools"][0]["functionDeclarations"][0]["name"] == "get_case"
    assert counted[0]["generationConfig"]["responseSchema"] == schema
    assert counted[0]["generationConfig"]["maxOutputTokens"] == 8
    assert meter.calls == 1 and stats["provider_attempts"] == 1
