"""Fresh, disposable evaluation workspaces and a service-binding step seam.

Injected deterministic steps prove harness behavior only. CLI dry runs remain
DRY and cannot be published. Real model/service bindings belong to 10-15.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from api import config, db, sandbox
from agent import config as agent_config
from agent.config import ASSISTANT_VERSION
from engine.model import ENGINE_VERSION
from . import fingerprint, score, store
from .evidence import Writer
from .budget import BudgetReached, EvalBudget
from . import probe

AI_TABLES = ("readings", "ai_outputs", "tool_results", "evidence_drafts", "chat_requests", "ai_usage")


@dataclass
class StepContext:
    workspace: dict
    actors: dict
    scenario: dict
    inputs: dict
    references: dict
    step: dict
    budget: object = None
    probe: object = None
    state: dict = field(default_factory=dict)
    eval_dir: Path = fingerprint.HERE


@dataclass
class StepResult:
    served: str
    model_calls: int
    cache_hits: int = 0
    raw_text: str = ""
    displayed_text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: dict(input=None, output=None, thoughts=None))
    outputs: dict = field(default_factory=dict)
    error: str | None = None
    numbers_removed: int | None = None
    tool_results: dict = field(default_factory=dict)
    captured_prompt: str | None = None
    preview_rows: list = field(default_factory=list)
    note: bool = False
    test_double: bool = False


def _actors(inputs):
    def manager(branch):
        return dict(account=branch, role="manager", branch=branch)
    return dict(source=manager(inputs.get("source_store")), destination=manager(inputs.get("dest_store")),
                hq=dict(account="HQ", role="hq", branch=None))


def _human(ctx):
    """Person actions use application services; no model can invoke this seam."""
    from api import cases, signals
    action = ctx.step["action"].split(":", 1)[1]
    with db.tx() as conn:
        workspace = db.lock_run(conn, ctx.workspace["run_id"], mode="UPDATE")
        fields = ctx.inputs.get(ctx.step.get("fields_ref"), {})
        if action == "request_evidence":
            case = cases.case_row(conn, workspace["run_id"], ctx.inputs["case_id"])
            request = signals.request_evidence(conn, workspace, ctx.actors["source"], case["case_id"],
                                               case["active_rec_version"], fields["fact_id"])
            ctx.state["request"] = request
            return request
        if action == "answer":
            request = ctx.state["request"]
            actor = dict(role="manager", branch=request["to_branch"], account=request["to_branch"])
            counts = {lot: dict(fields) for lot in request["lot_ids"]} if fields else None
            # Packaged evidence is already private local material; never upload it
            # through a reviewer route or spend its upload allowance.
            result = signals.answer(conn, workspace, actor, request["request_id"],
                                    ctx.step.get("reply", "Evidence attached for review."), counts, "now")
            if ctx.step.get("attachment"):
                # Packaged photos are immutable fixture material, not uploads.
                attachment = (ctx.eval_dir / ctx.step["attachment"]).resolve()
                if not attachment.is_relative_to(ctx.eval_dir.resolve()) or not attachment.is_file():
                    raise ValueError("Evaluation attachment is missing")
                from api import files
                if config.UPLOADS_BUCKET and config.env("GCP", "off") == "off":
                    raise ValueError("Offline evaluation attachments require local storage")
                stored = files.save(workspace["run_id"], attachment.read_bytes())
                db.run(conn, "UPDATE signals SET file_uri=:u, file_type=:m, file_sha256=:h, file_size=:z, "
                       "file_counted=false WHERE run_id=:r AND signal_id=:s",
                       u=stored.uri, m=stored.mime, h=stored.sha256, z=stored.size,
                       r=workspace["run_id"], s=result["signal_id"])
                result = signals._signal_view(signals._signal(conn, workspace["run_id"], result["signal_id"]))
            ctx.state["answer"] = result
            return result
        if action == "submit_report":
            from api import reports
            kind = ctx.references["expected"]["reading_fields"]["kind"]
            actor = ctx.actors["hq" if kind == "TRANSPORT" else "destination"]
            reading = ctx.state.get("reading")
            if reading is None:
                result = reports.create(conn, workspace, actor, kind, fields, "", ctx.inputs["case_id"], None)
            else:
                suggested = {row["field"]: row["value"] for row in reading["response"]["suggestions"]
                             if row["value"] is not None and row["state"] in ("suggested", "calculated")}
                if reading["response"]["kind"] == "NOT_RELEVANT":
                    suggested["kind"] = "NOT_RELEVANT"
                values = {"kind": kind, **fields}
                origins = {name: ("cleared" if value is None else "typed") if name not in suggested else
                           "gemini_accepted" if value == suggested[name] else "gemini_edited"
                           for name, value in values.items()}
                result = reports.create(conn, workspace, reading["session"], kind, fields, reading["text"],
                    reading["case_id"], None, file=reading["file"], reading_id=reading["response"]["reading_id"],
                    field_sources=origins)
            ctx.state["report"] = result
            return result
        if action == "preview":
            from api import impact
            report = ctx.state["report"]
            result = impact.preview(conn, workspace, ctx.actors["destination"], report["signal_id"], report["revision"])
            ctx.state["preview_rows"] = result["cases"]
            return result
    raise ValueError("Unknown scripted human action")


def _load(root, name):
    return json.loads((Path(root) / (name + ".json")).read_text(encoding="utf-8"))


def _items(trace, rubric, references, numbers_check):
    scored = score.score_trace(trace, rubric, references, numbers_check=numbers_check)
    return [dict(item_id=item["item_id"], grp=item["group"], gate=item["gate"], facet=item["facet"],
                 scorer=item["scorer"], result=result["result"], note=result["reason"],
                 item=item["title"], scenario_title=trace["scenario_title"], reference=item["check"],
                 actual=trace["steps"]) for item, result in zip(rubric, scored)]


def _observed_step(ctx, invoke, trace):
    with db.tx() as conn:
        before = probe.business_snapshot(conn, ctx.workspace["run_id"])
    hits, writes = [], []
    try:
        with probe.truth_probe(writes) as hits:
            ctx.probe = hits
            return invoke(ctx)
    finally:
        # Only the invocation is an AI window. Observe even a failed invocation.
        with db.tx() as conn:
            after = probe.business_snapshot(conn, ctx.workspace["run_id"])
        changes = probe.diff(before, after)
        if changes.get("activity_log"):
            trace.setdefault("authority_windows", []).append({
                "before": {table: before[table] for table in ("activity_log", "evidence_drafts")},
                "after": {table: after[table] for table in ("activity_log", "evidence_drafts")}})
        for write in writes:
            table = write["table"]
            if table not in before and table != "usage_counts":
                continue
            if table == "recommendations":
                # What-if rows are allowed. A net-zero business write must not
                # disappear merely because it was undone before the snapshot.
                delta = probe.diff({"rows": before[table]}, {"rows": after[table]}).get("rows", [])
                if delta and all(row.get("is_whatif") is True for row in delta):
                    continue
            if table not in changes:
                changes[table] = [dict(executed_write=True, statement=write["statement"])]
        for table, rows in changes.items():
            trace["db_diff"].setdefault(table, []).extend(rows)
        trace["sql_probe_hits"].extend(hits)


def default_budget(**overrides):
    settings = config.EVAL
    return EvalBudget(overrides.get("max_calls", settings["max_calls"]),
                      overrides.get("max_tokens", settings["max_tokens"]),
                      overrides.get("max_cost_usd", settings["max_cost_usd"]),
                      dict(input=settings["price_input_usd_per_mtok"],
                           output=settings["price_output_usd_per_mtok"]))


@contextmanager
def _exclusive(eval_id):
    # A session lock survives short metadata transactions, but not process death.
    # NOWAIT below also detects another writer holding the eval row. Never hold a
    # business-row transaction open over a model request.
    with db.engine().connect() as conn:
        held = db.one(conn, "SELECT pg_try_advisory_lock(hashtextextended(:e, 1007)) AS held", e=eval_id)["held"]
        conn.commit()
        if not held:
            raise ValueError("Evaluation is already running")
        try:
            yield
        finally:
            db.one(conn, "SELECT pg_advisory_unlock(hashtextextended(:e, 1007))", e=eval_id)
            conn.commit()


def run(eval_id, template_run_id, **kwargs):
    """One owner per eval ID; a dead owner's recorded attempts are never replayed."""
    with _exclusive(eval_id):
        return _run(eval_id, template_run_id, **kwargs)


def _run(eval_id, template_run_id, *, scenarios=None, runs=3, steps=None, evidence, budget=None, dry_run=False,
         eval_dir=fingerprint.HERE, numbers_check=None, human_steps=None):
    if steps is None:
        from .steps import REAL_STEPS
        steps = REAL_STEPS
    if type(runs) is not int or not 1 <= runs <= 3:
        raise ValueError("Runs must be from 1 to 3")
    definitions, references, rubric = (_load(eval_dir, n) for n in ("scenarios", "references", "rubric"))
    selected = definitions if scenarios is None else [s for s in definitions if s["id"] in scenarios]
    if not selected or scenarios is not None and {s["id"] for s in selected} != set(scenarios):
        raise ValueError("Unknown evaluation scenario")
    if any("pending" in references["scenarios"].get(s["id"], {"pending": []}) for s in selected):
        raise ValueError("Selected references are PENDING; complete their named upstream plans first")
    writer = Writer(evidence)
    computed = fingerprint.compute(eval_dir, references["dataset_version"], references["engine_version"])
    if not dry_run:
        approval = Path(eval_dir) / "FINGERPRINT"
        locked = approval.read_text().strip() if approval.is_file() else "missing"
        if locked != computed:
            raise ValueError(f"Reference fingerprint mismatch: committed {locked}; computed {computed}")
    budget = budget or default_budget()
    model_id = agent_config.model_id()
    configuration = dict(scenarios=[s["id"] for s in selected], runs=runs, dry_run=dry_run,
                         assistant_policy=config.SETTINGS["assistant"], reading_policy=config.READING,
                         model_id=model_id, location=agent_config.location(),
                         evidence_hash=hashlib.sha256(str(evidence).encode()).hexdigest())
    caps = dict(**budget.caps, configuration=configuration)
    with db.tx() as conn:
        template = sandbox.active_template(conn) if template_run_id == "active" else db.one(
            conn, "SELECT * FROM demo_runs WHERE run_id=:r AND kind='TEMPLATE'", r=template_run_id)
        if not template:
            raise ValueError("No such evaluation template")
        if template["dataset_version"] != references["dataset_version"] or references["engine_version"] != ENGINE_VERSION:
            raise ValueError("Evaluation template/reference versions differ")
        try:
            before = db.one(conn, "SELECT * FROM eval_runs WHERE eval_id=:e FOR UPDATE NOWAIT", e=eval_id)
        except Exception as exc:
            if db.sql_code(exc) == "55P03":
                raise ValueError("Evaluation is already running") from None
            raise
        if before:
            if (before["caps"] != caps or before["fingerprint"] != computed or
                    before["template_ref"] != template["run_id"] or before["build_sha"] != config.GIT_COMMIT or
                    before["model_id"] != model_id or
                    before["assistant_version"] != ASSISTANT_VERSION):
                raise ValueError("Resume configuration differs; use a new evaluation ID")
            if before["publish_state"] == "PUBLISHED":
                raise ValueError("A published evaluation cannot be resumed")
            if before["status"] == "CAPPED":
                return "CAPPED"
            budget.restore(before["usage"])
        store.record_run(conn, eval_id, fingerprint=computed, template_ref=template["run_id"],
                         engine_version=ENGINE_VERSION, dataset_version=template["dataset_version"],
                         build_sha=config.GIT_COMMIT, model_id=model_id,
                         assistant_version=ASSISTANT_VERSION,
                         caps=caps, usage=budget.totals(), status="RUNNING", finished_at=None)
        recorded = db.rows(conn, "SELECT * FROM eval_attempts WHERE eval_id=:e", e=eval_id)
    # A hard-killed process may leave an in-flight clone. Retain that failed
    # attempt; do not repeat a possibly already-spent provider request.
    for attempt in recorded:
        if attempt["status"] == "RUNNING":
            trace_ref = None
            try:
                trace_ref = writer.trace(f"s{attempt['scenario_id']:02}", attempt["run_no"],
                                         dict(invalid_reason="Interrupted before completion", error=dict(type="Interrupted")))
            finally:
                with db.tx() as conn:
                    db.run(conn, "UPDATE eval_attempts SET status='FAILED', valid=false, invalid_reason=:reason, "
                           "evidence_ref=COALESCE(:ref,evidence_ref) WHERE eval_id=:e AND scenario_id=:s AND run_no=:n",
                           reason="Interrupted before completion", ref=trace_ref, e=eval_id,
                           s=attempt["scenario_id"], n=attempt["run_no"])
                    db.run(conn, "UPDATE demo_runs SET status='RETIRED', retired_at=now() WHERE run_id=:r",
                           r=attempt["workspace_run_id"])
                    if trace_ref is None:
                        store.record_run(conn, eval_id, status="FAILED")
    completed = {(a["scenario_id"], a["run_no"]) for a in recorded}

    def checkpoint(totals):
        with db.tx() as conn:
            store.record_run(conn, eval_id, usage=totals)

    budget.checkpoint = checkpoint
    checker = numbers_check
    if checker is None and not dry_run:
        from agent import numbers
        from api import refdata
        reference = refdata.get(references["dataset_version"])
        checker = lambda text, results: numbers.check(text, results, ref=reference)
    if checker is None:
        checker = lambda text, results: {"removed": None}
    status = "DRY" if dry_run else "COMPLETE"
    for scenario in selected:
        for run_no in range(1, runs + 1):
            sid = int(scenario["id"][1:])
            if (sid, run_no) in completed:
                continue
            if budget.exhausted():
                status = "CAPPED"
                break
            started = time.perf_counter()
            trace = dict(scenario=scenario["id"], scenario_title=scenario["title"], steps={},
                         raw_text="", displayed_text="", tool_calls=[], usage={}, db_diff={}, sql_probe_hits=[],
                         preview_rows=[], summary_rows=[], tool_results={})
            with db.tx() as conn:
                workspace_id = sandbox.clone(conn, template)
                for table in reversed(AI_TABLES):
                    if table in sandbox.per_run_tables(conn):
                        db.run(conn, f"DELETE FROM {table} WHERE run_id=:r", r=workspace_id)
                workspace = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=workspace_id)
                store.record_attempt(conn, eval_id, sid, run_no, workspace_run_id=workspace_id,
                                     valid=False, invalid_reason="Attempt in progress", status="RUNNING", model_calls=0)
            attempt_status, invalid, interrupted = "COMPLETE", None, None
            initial_calls = budget.totals()["calls"]
            cache_hits = 0
            try:
                ref = references["scenarios"][scenario["id"]]
                inputs = ref["resolved_inputs"]
                trace["report_text"] = next((s.get("prompt", "") for s in inputs["steps"]
                                             if s["action"] == "ai:reading"), "")
                state = {}
                for step in inputs["steps"]:
                    ctx = StepContext(workspace, _actors(inputs), scenario, inputs, ref, step, budget,
                                      state=state, eval_dir=Path(eval_dir))
                    kind, action = step["action"].split(":", 1)
                    if kind == "human":
                        (human_steps or _human)(ctx)
                        continue
                    before_calls = budget.totals()["calls"]
                    result = _observed_step(ctx, steps[action], trace)
                    payload = asdict(result)
                    payload["output"] = payload.pop("outputs")
                    if action == "summary" and not payload["preview_rows"]:
                        payload["preview_rows"] = state.get("preview_rows", [])
                    trace["steps"][step["id"]] = payload
                    trace["raw_text"] += result.raw_text
                    trace["displayed_text"] += result.displayed_text
                    trace["tool_calls"].extend(result.tool_calls)
                    trace["tool_results"][step["id"]] = result.tool_results
                    trace["preview_rows"] = state.get("preview_rows", result.preview_rows)
                    cache_hits += result.cache_hits
                    if result.served != "live" or result.model_calls <= 0 or result.cache_hits:
                        invalid = "A step was cache-served or made no model call"
                    if result.model_calls != budget.totals()["calls"] - before_calls:
                        raise ValueError("Model calls bypassed budget admission")
                    if result.error:
                        raise ValueError(result.error)
            except BaseException as exc:
                attempt_status = "CAPPED" if isinstance(exc, BudgetReached) else "FAILED"
                status = attempt_status
                invalid = str(exc) or type(exc).__name__
                trace["error"] = dict(type=type(exc).__name__, message=str(exc))
                if not isinstance(exc, Exception):
                    interrupted = exc
            finally:
                try:
                    trace.update(invalid_reason=invalid, usage=budget.totals())
                    if scenario["id"] == "s05" and invalid is None:
                        with db.tx() as conn:
                            trace["truth_probe"] = probe.truth_matches(conn, workspace, ref["expected"]["truth_probe"],
                                                                      trace["raw_text"], trace["displayed_text"],
                                                                      trace["tool_results"])
                    evidence_ref = writer.trace(scenario["id"], run_no, trace)
                    with db.tx() as conn:
                        db.run(conn, "UPDATE eval_attempts SET valid=:v, invalid_reason=:reason, model_calls=:c, "
                               "cache_hits=:h, status=:status, evidence_ref=:ref, duration_ms=:ms "
                               "WHERE eval_id=:e AND scenario_id=:s AND run_no=:n",
                               v=invalid is None, reason=invalid, c=budget.totals()["calls"] - initial_calls,
                               h=cache_hits, status=attempt_status, ref=evidence_ref,
                               ms=int((time.perf_counter() - started) * 1000), e=eval_id, s=sid, n=run_no)
                        if invalid is None:
                            items = [i for i in rubric if i["scenario"] == scenario["id"]]
                            store.record_items(conn, eval_id, sid, run_no, _items(trace, items, references, checker))
                except Exception:
                    # A storage/scoring failure must not leave an apparently
                    # complete evaluation. The clone is retired even here.
                    status = "FAILED"
                    with db.tx() as conn:
                        db.run(conn, "UPDATE eval_attempts SET status='FAILED', valid=false, invalid_reason=:reason "
                               "WHERE eval_id=:e AND scenario_id=:s AND run_no=:n",
                               reason="Evidence or scoring could not be completed", e=eval_id, s=sid, n=run_no)
                    raise
                finally:
                    with db.tx() as conn:
                        db.run(conn, "UPDATE demo_runs SET status='RETIRED', retired_at=now() WHERE run_id=:r", r=workspace_id)
                        store.record_run(conn, eval_id, status=status if status in ("FAILED", "CAPPED") else "RUNNING",
                                         usage=budget.totals())
            if interrupted:
                raise interrupted
            if status in ("CAPPED", "FAILED"):
                break
        if status in ("CAPPED", "FAILED"):
            break
    with db.tx() as conn:
        store.record_run(conn, eval_id, status=status, usage=budget.totals(), finished_at=datetime.now(timezone.utc))
        writer.checklist(eval_id, computed, store.checklist_rows(conn, eval_id))
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent.eval run")
    parser.add_argument("--template", required=True)
    parser.add_argument("--runs", type=int, default=config.EVAL["runs_per_scenario"])
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--eval-id", default=None)
    parser.add_argument("--evidence")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    eval_id = args.eval_id or "eval-" + uuid.uuid4().hex
    evidence = args.evidence or (f"gs://{config.EVAL_BUCKET}/eval/{eval_id}" if config.EVAL_BUCKET else None)
    if not evidence:
        parser.error("--evidence or EVAL_BUCKET is required")
    from .dryrun import DRY_STEPS
    from .steps import REAL_STEPS
    from api.caps import EVAL_ADMISSION
    caps = {k: getattr(args, k) for k in ("max_calls", "max_tokens", "max_cost_usd") if getattr(args, k) is not None}
    token = EVAL_ADMISSION.set(True) if not args.dry_run else None
    try:
        status = run(eval_id, args.template, runs=args.runs, budget=default_budget(**caps),
                     steps=DRY_STEPS if args.dry_run else REAL_STEPS, evidence=evidence, dry_run=args.dry_run)
    finally:
        if token is not None:
            EVAL_ADMISSION.reset(token)
    print("Evaluation " + status)
    return 0 if status in ("DRY", "COMPLETE") else 1


run_main = main
