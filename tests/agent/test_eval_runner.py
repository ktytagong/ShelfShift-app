"""Local runner integration; injected steps are test doubles, never live evidence."""
from __future__ import annotations

import importlib.util
import asyncio
import csv
from copy import deepcopy
import io
import json
from pathlib import Path
import uuid
import time

import pytest

from tests.api import conftest as api_fixtures  # noqa: F401
from api import db, sandbox

HERE = Path(__file__).resolve().parents[2] / "agent" / "eval"


@pytest.fixture(scope="session")
def public_dv(schema):
    return api_fixtures.public_dv.__wrapped__(schema)


@pytest.fixture(scope="module")
def public_template(template, public_dv):
    yield from api_fixtures.public_template.__wrapped__(template, public_dv)


@pytest.fixture(scope="module")
def capacity_eval_template(public_template):
    # Full ten-scenario references now require a real changed-capacity case.
    # Keep raw-template tests on public_template; only full-input consumers use
    # this separate template with an existing real report/confirm precondition.
    from tests.agent.test_eval_steps import real_eval_template
    yield from real_eval_template.__wrapped__(public_template)


def runner_module():
    assert importlib.util.find_spec("agent.eval.runner") is not None, "Fresh isolated eval runner must exist"
    from agent.eval import runner
    return runner


@pytest.fixture
def eval_id(schema):
    value = "test-" + uuid.uuid4().hex
    yield value
    with db.tx() as conn:
        for table in ("eval_publications", "eval_items", "eval_attempts", "eval_runs"):
            db.run(conn, f"DELETE FROM {table} WHERE eval_id=:e", e=value)


@pytest.fixture
def eval_dir(public_template, tmp_path):
    """A private unlocked fixture; no production references or approval file."""
    from agent.eval import refs
    root = tmp_path / "inputs"
    root.mkdir()
    scenarios = json.loads((HERE / "scenarios.json").read_text())
    selected = [s for s in scenarios if s["id"] == "s09"]
    with db.tx() as conn:
        references = refs.build_refs(conn, public_template, selected)
    for name, value in (("scenarios", selected), ("references", references),
                        ("rubric", json.loads((HERE / "rubric.json").read_text()))):
        (root / (name + ".json")).write_text(json.dumps(value))
    return root


def test_one_run_end_to_end(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()
    from agent.eval.dryrun import DRY_STEPS
    from agent.eval import store
    seen = []

    def reading(ctx):
        seen.append(ctx.workspace["run_id"])
        with db.tx() as conn:
            for table in runner.AI_TABLES:
                if table in sandbox.per_run_tables(conn):
                    assert db.one(conn, f"SELECT count(*) AS n FROM {table} WHERE run_id=:r",
                                  r=ctx.workspace["run_id"])["n"] == 0
        return DRY_STEPS["reading"](ctx)

    status = runner.run(eval_id, public_template, scenarios=["s09"], runs=1,
                        steps={**DRY_STEPS, "reading": reading}, evidence=tmp_path / "evidence",
                        eval_dir=eval_dir, dry_run=True)
    assert status == "DRY"
    assert len(seen) == 1
    trace = json.loads((tmp_path / "evidence/s09/1.json").read_text())
    assert trace["steps"]["reading"]["output"]["kind"] == "NOT_RELEVANT"
    assert trace["steps"]["reading"]["test_double"] is True
    assert set(trace) >= {"raw_text", "displayed_text", "tool_calls", "usage", "db_diff", "sql_probe_hits"}
    with db.tx() as conn:
        attempt = db.one(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
        assert attempt["workspace_run_id"] == seen[0] and attempt["valid"]
        assert db.one(conn, "SELECT status FROM demo_runs WHERE run_id=:r", r=seen[0])["status"] == "RETIRED"
        items = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e", e=eval_id)
        assert items and all(i["result"] == "PASS" for i in items)
        assert db.one(conn, "SELECT status FROM eval_runs WHERE eval_id=:e", e=eval_id)["status"] == "DRY"
        with pytest.raises(store.Blocked):
            store.publish(conn, eval_id, "test-fingerprint")


def test_evidence_writer_local_and_fake_storage(tmp_path, monkeypatch):
    runner_module()
    from agent.eval import evidence
    saved = {}

    class Blob:
        def upload_from_string(self, data, content_type):
            saved["data"], saved["type"] = data, content_type

    class Fake:
        def bucket(self, name):
            assert name == "isolated-test-bucket"
            return self

        def blob(self, key):
            saved["key"] = key
            return Blob()

    monkeypatch.setattr(evidence, "_client", lambda: Fake())
    writer = evidence.Writer("gs://isolated-test-bucket/eval/test")
    writer.trace("s09", 1, {"raw_text": "test"})
    assert saved["key"] == "eval/test/s09/1.json"
    assert json.loads(saved["data"]) == {"raw_text": "test"}
    assert saved["type"] == "application/json"
    with pytest.raises(ValueError):
        evidence.Writer(tmp_path).trace("../escape", 1, {})


def budget_module():
    assert importlib.util.find_spec("agent.eval.budget") is not None, "Per-model-call eval budget must exist"
    from agent.eval import budget
    return budget


def options(eval_dir, tmp_path):
    from agent.eval.dryrun import DRY_STEPS
    return dict(scenarios=["s09"], runs=3, steps=DRY_STEPS, evidence=tmp_path / "evidence",
                eval_dir=eval_dir, dry_run=True)


def test_budget_caps_the_run(eval_id, public_template, eval_dir, tmp_path):
    module = budget_module()
    runner = runner_module()
    b = module.EvalBudget(max_calls=4, max_tokens=1000, max_cost_usd=1,
                         prices={"input": 1.5, "output": 9})
    calls = []

    def retry(ctx):
        for _ in range(2):  # first provider attempt is a 429, then one retry
            ctx.budget.allow(input_tokens=10, output_tokens=5)
            calls.append("provider-test-double")
            ctx.budget.charge({"input": 10, "output": 2, "thoughts": 3}, calls=1)
        return runner.StepResult("live", 2, usage={"input": 20, "output": 4, "thoughts": 6})

    args = options(eval_dir, tmp_path)
    args.update(steps={"reading": retry}, budget=b)
    assert runner.run(eval_id, public_template, **args) == "CAPPED"
    assert len(calls) == 4 and b.totals()["calls"] == 4
    assert b.totals()["tokens"] == 60
    assert b.totals()["cost_usd"] == pytest.approx((40 * 1.5 + 20 * 9) / 1_000_000)
    with db.tx() as conn:
        assert len(db.rows(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)) == 2
        assert db.one(conn, "SELECT status FROM eval_runs WHERE eval_id=:e", e=eval_id)["status"] == "CAPPED"


@pytest.mark.parametrize(("max_tokens", "max_cost"), [(2, 1), (1000, 0.000001)])
def test_budget_admission_precedes_provider(max_tokens, max_cost):
    module = budget_module()
    b = module.EvalBudget(10, max_tokens, max_cost, {"input": 1.5, "output": 9})
    calls = []
    with pytest.raises(module.BudgetReached):
        b.allow(input_tokens=10, output_tokens=5)
        calls.append("must never happen")
    assert calls == [] and b.totals()["calls"] == 0


def test_hidden_numbers_fail_without_a_sql_read(eval_id, capacity_eval_template, full_eval_dir, tmp_path):
    public_template = capacity_eval_template
    from agent.eval import runner
    from agent.eval.dryrun import DRY_STEPS
    ref = json.loads((full_eval_dir / "references.json").read_text())["scenarios"]["s05"]
    keys = ref["expected"]["truth_probe"]["keys"]
    with db.tx() as conn:
        value = db.one(conn, "SELECT demand FROM ref_truth_demand WHERE dataset_version=:v AND store=:s "
                       "AND sku=:sku AND day=:d", v=keys["dataset_version"], s=keys["store"],
                       sku=keys["sku"], d=keys["day"])["demand"]

    def leak(ctx):
        result = DRY_STEPS["ask"](ctx)
        result.raw_text = result.displayed_text = "Hidden demand is " + str(value)
        return result

    assert runner.run(eval_id, public_template, scenarios=["s05"], runs=1, steps={"ask": leak},
                       evidence=tmp_path / "leak", eval_dir=full_eval_dir, dry_run=True) == "DRY"
    trace = json.loads((tmp_path / "leak/s05/1.json").read_text())
    assert trace["sql_probe_hits"] == []  # lookup was outside the AI window
    assert set(trace["truth_probe"]) == {"complete", "raw_matches", "displayed_matches"}
    assert trace["truth_probe"]["complete"] and trace["truth_probe"]["raw_matches"] > 0
    assert trace["truth_probe"]["displayed_matches"] > 0
    with db.tx() as conn:
        results = db.rows(conn, "SELECT result FROM eval_items WHERE eval_id=:e AND gate='hidden_truth'", e=eval_id)
        assert results and all(i["result"] == "FAIL" for i in results)

@pytest.mark.parametrize(("served", "calls", "hits"), [("reuse", 0, 1), ("precomputed", 0, 0),
                                                          ("live", 0, 0)])
def test_cache_served_run_is_invalid(eval_id, public_template, eval_dir, tmp_path, served, calls, hits):
    runner = runner_module()
    args = options(eval_dir, tmp_path)
    args.update(runs=1, steps={"reading": lambda ctx: runner.StepResult(served, calls, hits)})
    assert runner.run(eval_id, public_template, **args) == "DRY"
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
        assert not row["valid"] and row["invalid_reason"]
        assert db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e", e=eval_id) == []
    assert json.loads((tmp_path / "evidence/s09/1.json").read_text())["invalid_reason"]


def test_resume_and_refusal(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()
    from agent.eval.dryrun import DRY_STEPS
    calls = []
    args = options(eval_dir, tmp_path)

    def interrupt(ctx):
        calls.append(ctx.workspace["run_id"])
        with pytest.raises(ValueError, match="already running"):
            runner.run(eval_id, public_template, **args)
        if len(calls) == 2:
            raise KeyboardInterrupt("test interruption")
        return DRY_STEPS["reading"](ctx)

    args["steps"] = {"reading": interrupt}
    with pytest.raises(KeyboardInterrupt):
        runner.run(eval_id, public_template, **args)
    with db.tx() as conn:
        before = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e ORDER BY item_id", e=eval_id)
    args["steps"] = DRY_STEPS
    assert runner.run(eval_id, public_template, **args) == "DRY"
    assert runner.run(eval_id, public_template, **args) == "DRY"
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e ORDER BY run_no", e=eval_id)
        assert len(rows) == 3 and len({r["workspace_run_id"] for r in rows}) == 3
        assert rows[1]["status"] == "FAILED" and not rows[1]["valid"]
        assert db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e AND run_no=1 ORDER BY item_id",
                       e=eval_id) == before
        assert all(db.one(conn, "SELECT status FROM demo_runs WHERE run_id=:r",
                          r=r["workspace_run_id"])["status"] == "RETIRED" for r in rows)
    changed = {**args, "runs": 2}
    with pytest.raises(ValueError, match="configuration"):
        runner.run(eval_id, public_template, **changed)


def test_reviewer_counters_unchanged(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()
    with db.tx() as conn:
        before = db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope, kind, window_start")
    runner.run(eval_id, public_template, **options(eval_dir, tmp_path))
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope, kind, window_start") == before


def test_fingerprint_guard(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()
    from agent.eval.fingerprint import compute
    (eval_dir / "FINGERPRINT").write_text("mismatch")
    args = options(eval_dir, tmp_path)
    args["dry_run"] = False
    computed = compute(eval_dir, json.loads((eval_dir / "references.json").read_text())["dataset_version"],
                       json.loads((eval_dir / "references.json").read_text())["engine_version"])
    with pytest.raises(ValueError) as error:
        runner.run(eval_id, public_template, **args)
    assert "mismatch" in str(error.value) and computed in str(error.value)
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM eval_runs WHERE eval_id=:e", e=eval_id) == []


def test_failed_attempt_keeps_evidence(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()
    args = options(eval_dir, tmp_path)

    def fail(ctx):
        raise RuntimeError("private provider failure")

    args.update(runs=1, steps={"reading": fail})
    assert runner.run(eval_id, public_template, **args) == "FAILED"
    trace = json.loads((tmp_path / "evidence/s09/1.json").read_text())
    assert trace["error"]["type"] == "RuntimeError"
    with db.tx() as conn:
        assert not db.one(conn, "SELECT valid FROM eval_attempts WHERE eval_id=:e", e=eval_id)["valid"]


def test_eval_settings_validated(tmp_path):
    from api import config
    assert "eval" in config.SETTINGS, "Evaluation caps and prices must be sourced settings"
    assert config.EVAL["runs_per_scenario"] == 3
    tree = json.loads(config.SETTINGS_FILE.read_text())
    tree["eval"]["max_tokens"]["value"] = float("inf")
    path = tmp_path / "settings.json"
    # A syntactically valid JSON exponent overflows Python's float; exercise
    # settings validation rather than the parser's non-JSON Infinity rejection.
    path.write_text(json.dumps(tree).replace("Infinity", "1e309"))
    with pytest.raises(ValueError, match="settings.eval.max_tokens"):
        config.load_settings(path)


def probe_module():
    assert importlib.util.find_spec("agent.eval.probe") is not None, "AI-window SQL and business-write probe must exist"
    from agent.eval import probe
    return probe


@pytest.mark.parametrize("threaded", [False, True])
def test_probe_catches_truth_reads(public_template, threaded):
    probe = probe_module()

    def read():
        with db.tx() as conn:
            db.one(conn, 'SELECT count(*) FROM "public"."truth_lots" WHERE run_id=:r', r=public_template)

    with probe.truth_probe() as hits:
        if threaded:
            asyncio.run(asyncio.to_thread(read))
        else:
            read()
    assert len(hits) == 1 and "truth_lots" in hits[0]["statement"]
    read()
    assert len(hits) == 1  # scoring and human actions run outside the AI window


def test_diff_catches_executed_writes(eval_id, public_template, eval_dir, tmp_path):
    probe = probe_module()
    runner = runner_module()
    from agent.eval.dryrun import DRY_STEPS

    def write(ctx):
        with db.tx() as conn:
            db.run(conn, "INSERT INTO decisions(run_id,case_id,rec_version,round,role,decision) "
                   "VALUES(:r,'test-case','test-version',0,'manager','APPROVE')", r=ctx.workspace["run_id"])
            db.run(conn, "INSERT INTO ai_outputs(run_id,output_id,kind,provenance,status) "
                   "VALUES(:r,'test-output','BRIEF','LIVE','COMPLETE')", r=ctx.workspace["run_id"])
            db.run(conn, "INSERT INTO recommendations(run_id,case_id,rec_version,day,engine_input,result,"
                   "fact_checks,is_whatif) VALUES(:r,'test-case','what-if',0,'{}','{}','[]',true)",
                   r=ctx.workspace["run_id"])
        return DRY_STEPS["reading"](ctx)

    args = options(eval_dir, tmp_path)
    args.update(runs=1, steps={"reading": write})
    assert runner.run(eval_id, public_template, **args) == "DRY"
    trace = json.loads((tmp_path / "evidence/s09/1.json").read_text())
    assert trace["db_diff"]["decisions"][0]["decision"] == "APPROVE"
    assert not trace["db_diff"].get("ai_outputs") and not trace["db_diff"].get("recommendations")
    # Deletes and updates count too, including disguising a business row as a what-if.
    before = {"decisions": [{"decision": "APPROVE"}],
              "recommendations": [{"is_whatif": False, "result": {}}]}
    after = {"decisions": [], "recommendations": [{"is_whatif": True, "result": {}}]}
    changes = probe.diff(before, after)
    assert changes["decisions"] and changes["recommendations"]
    assert probe.diff({}, {t: [{"anything": True}] for t in runner.AI_TABLES}) == {}


@pytest.fixture
def full_eval_dir(capacity_eval_template, tmp_path, monkeypatch):
    """Isolated references; unavailable Phase 7 contracts are explicit doubles."""
    from agent.eval import refs
    from api import config, preload
    from types import SimpleNamespace
    public_template = capacity_eval_template
    definitions = json.loads((HERE / "scenarios.json").read_text())

    def create(conn, run, actor, kind, fields, text, case_id, corrects):
        return {"signal_id": "fixture-report", "revision": 1}

    def preview(conn, run, actor, signal_id, revision):
        return {"cases": [{"case_id": "fixture-case", "plan_now": "keep", "plan_if": "md30", "changed": True}],
                "more": 0}

    modules = {"api.reports": SimpleNamespace(create=create), "api.impact": SimpleNamespace(preview=preview),
               "api.preload": preload}
    monkeypatch.setattr(refs, "_upstream", modules.get)
    monkeypatch.setitem(config.SETTINGS, "impact", {"summary_max_words": 90})
    with db.tx() as conn:
        references = refs.build_refs(conn, public_template, definitions)
    root = tmp_path / "full-inputs"
    root.mkdir()
    import shutil
    shutil.copytree(HERE / "attachments", root / "attachments")
    for name, doc in (("scenarios", definitions), ("references", references),
                      ("rubric", json.loads((HERE / "rubric.json").read_text()))):
        (root / (name + ".json")).write_text(json.dumps(doc))
    # Temporary test lock, never the release approval file.
    from agent.eval.fingerprint import compute
    (root / "FINGERPRINT").write_text(compute(root, references["dataset_version"], references["engine_version"]))
    return root


def fixture_human(ctx):
    from agent.eval import runner
    if ctx.scenario["id"] != "s06":
        return runner._human(ctx)
    # Pending hand-report/preview contracts; no owned upstream modules created.
    # The business write is deliberately OUTSIDE an AI window.
    if ctx.step["action"] == "human:submit_report":
        with db.tx() as conn:
            db.run(conn, "INSERT INTO signals(run_id,signal_id,reporter_role,text,kind,fields,state,created_day) "
                   "VALUES(:r,'fixture-report','manager','fixture','CAPACITY',CAST(:f AS jsonb),'READ',0)",
                   r=ctx.workspace["run_id"], f=db.js(ctx.inputs["report_fields"]))
    else:
        ctx.state["preview_rows"] = deepcopy(ctx.references["expected"]["preview"]["rows"])


@pytest.mark.parametrize("dry", [True, False])
def test_full_dry_eval(eval_id, capacity_eval_template, full_eval_dir, tmp_path, monkeypatch, dry, record_property):
    public_template = capacity_eval_template
    probe_module()
    runner = runner_module()
    from agent.eval import cli
    from agent.eval.dryrun import DRY_STEPS
    seen = set()
    steps_seen = []

    def step(ctx):
        seen.add(ctx.workspace["run_id"])
        steps_seen.append((ctx.scenario["id"], ctx.step["id"]))
        return DRY_STEPS[ctx.step["action"].split(":")[1]](ctx)

    with db.tx() as conn:
        counters = db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope,kind,window_start")
    started = time.perf_counter()
    # Non-CLI COMPLETE is a store integration fixture, labelled test_double in
    # every trace. Actual CLI --dry-run always stays DRY.
    result = runner.run(eval_id, public_template, runs=3, steps=dict.fromkeys(DRY_STEPS, step),
                        evidence=tmp_path / "full-evidence", eval_dir=full_eval_dir, dry_run=dry,
                        human_steps=fixture_human, numbers_check=lambda text, results: {"removed": 0})
    record_property("30_clone_seconds", round(time.perf_counter() - started, 3))
    assert result == ("DRY" if dry else "COMPLETE")
    assert len(seen) == 30
    assert steps_seen.count(("s08", "reading1")) == steps_seen.count(("s08", "reading2")) == 3
    with db.tx() as conn:
        attempts = db.rows(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
        assert len(attempts) == 30 and all(a["valid"] for a in attempts)
        assert all(db.one(conn, "SELECT status FROM demo_runs WHERE run_id=:r", r=r)["status"] == "RETIRED" for r in seen)
        assert db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope,kind,window_start") == counters
        for attempt in attempts:
            trace = json.loads(Path(attempt["evidence_ref"]).read_text())
            assert trace["sql_probe_hits"] == [] and not any(trace["db_diff"].values())
            assert all(s["test_double"] for s in trace["steps"].values())
        items = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e", e=eval_id)
        assert len({(i["scenario_id"], i["run_no"]) for i in items if i["scorer"] == "script"}) == 30
        assert all(i["result"] == "PASS" for i in items if i["facet"] == "protection")
    checklist = tmp_path / "review.csv"
    assert cli.export_main(["--eval-id", eval_id, "--out", str(checklist)]) == 0
    rows = list(csv.reader(io.StringIO(checklist.read_text())))
    for row in rows[2:]:
        if not row[7]:
            row[8] = "Pass"  # Simulated human judgments, not model grading.
    stream = io.StringIO(newline="")
    csv.writer(stream).writerows(rows)
    checklist.write_text(stream.getvalue())
    assert cli.import_main(["--eval-id", eval_id, "--file", str(checklist), "--reviewer", "test-reviewer"]) == 0
    monkeypatch.setattr(cli, "FINGERPRINT_PATH", full_eval_dir / "FINGERPRINT")
    assert cli.publish_main(["--eval-id", eval_id]) == (2 if dry else 0)


def test_probe_and_write_fail_protection(eval_id, capacity_eval_template, full_eval_dir, tmp_path):
    public_template = capacity_eval_template
    probe_module()
    runner = runner_module()
    from agent.eval.dryrun import DRY_STEPS

    def unsafe(ctx):
        with db.tx() as conn:
            db.one(conn, "SELECT count(*) FROM truth_lots WHERE run_id=:r", r=ctx.workspace["run_id"])
            db.run(conn, "INSERT INTO decisions(run_id,case_id,rec_version,round,role,decision) "
                   "VALUES(:r,'fixture','fixture',0,'manager','APPROVE')", r=ctx.workspace["run_id"])
        return DRY_STEPS["ask"](ctx)

    assert runner.run(eval_id, public_template, scenarios=["s04", "s05"], runs=1, steps={"ask": unsafe},
                       evidence=tmp_path / "unsafe", eval_dir=full_eval_dir, dry_run=True) == "DRY"
    with db.tx() as conn:
        items = db.rows(conn, "SELECT * FROM eval_items WHERE eval_id=:e AND facet='protection'", e=eval_id)
        assert {i["gate"] for i in items if i["result"] == "FAIL"} >= {"self_approval", "hidden_truth"}


def test_undone_business_write_still_recorded(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()
    from agent.eval.dryrun import DRY_STEPS

    def transient(ctx):
        with db.tx() as conn:
            db.run(conn, "INSERT INTO decisions(run_id,case_id,rec_version,round,role,decision) "
                   "VALUES(:r,'temporary','temporary',0,'manager','APPROVE')", r=ctx.workspace["run_id"])
            db.run(conn, "DELETE FROM decisions WHERE run_id=:r AND case_id='temporary'", r=ctx.workspace["run_id"])
        return DRY_STEPS["reading"](ctx)

    args = options(eval_dir, tmp_path)
    args.update(runs=1, steps={"reading": transient})
    assert runner.run(eval_id, public_template, **args) == "DRY"
    trace = json.loads((tmp_path / "evidence/s09/1.json").read_text())
    assert trace["db_diff"]["decisions"][0]["executed_write"] is True


def test_failed_step_keeps_observation(eval_id, public_template, eval_dir, tmp_path):
    runner = runner_module()

    def failure(ctx):
        with db.tx() as conn:
            db.one(conn, "SELECT count(*) FROM truth_lots WHERE run_id=:r", r=ctx.workspace["run_id"])
        raise RuntimeError("provider test failure")

    args = options(eval_dir, tmp_path)
    args.update(runs=1, steps={"reading": failure})
    assert runner.run(eval_id, public_template, **args) == "FAILED"
    trace = json.loads((tmp_path / "evidence/s09/1.json").read_text())
    assert len(trace["sql_probe_hits"]) == 1


def test_evidence_failure_retires_clone(eval_id, public_template, eval_dir, tmp_path, monkeypatch):
    runner = runner_module()
    seen = []

    def unavailable(self, scenario, number, trace):
        with db.tx() as conn:
            seen.append(db.one(conn, "SELECT workspace_run_id FROM eval_attempts WHERE eval_id=:e", e=eval_id)[
                "workspace_run_id"])
        raise ValueError("test storage unavailable")

    monkeypatch.setattr(runner.Writer, "trace", unavailable)
    args = options(eval_dir, tmp_path)
    args["runs"] = 1
    with pytest.raises(ValueError, match="storage unavailable"):
        runner.run(eval_id, public_template, **args)
    with db.tx() as conn:
        assert db.one(conn, "SELECT status FROM demo_runs WHERE run_id=:r", r=seen[0])["status"] == "RETIRED"
        assert db.one(conn, "SELECT status FROM eval_runs WHERE eval_id=:e", e=eval_id)["status"] == "FAILED"
        assert db.one(conn, "SELECT status FROM eval_attempts WHERE eval_id=:e", e=eval_id)["status"] == "FAILED"


def test_budget_unreported_usage_keeps_reservation():
    module = budget_module()
    b = module.EvalBudget(5, 20, 1, {"input": 1.5, "output": 9})
    b.allow(input_tokens=10, output_tokens=5)
    b.charge({"input": None, "output": None, "thoughts": None})
    assert b.totals()["tokens"] == 15 and b.totals()["usage_incomplete"]
    with pytest.raises(module.BudgetReached):
        b.allow(input_tokens=10, output_tokens=5)


def test_budget_records_usage_above_admitted_ceiling():
    module = budget_module()
    b = module.EvalBudget(5, 20, 1, {"input": 1.5, "output": 9})
    b.allow(input_tokens=1, output_tokens=1)
    with pytest.raises(ValueError, match="ceiling"):
        b.charge({"input": 2, "output": 0, "thoughts": 2})
    assert b.totals()["tokens"] == 4
    assert b.totals()["cost_usd"] == pytest.approx((2 * 1.5 + 2 * 9) / 1_000_000)

