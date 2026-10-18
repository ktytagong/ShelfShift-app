"""09-08 contracts: callable surface, transitive authority and scoped audit.

Source guards inspect reachable local helpers/import aliases, including SQL;
runtime cases prove that permitted reads remain useful and correctly scoped.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import re
from textwrap import dedent
from types import SimpleNamespace

import pytest


TOOL_PARAMS = {
    "get_case": (), "get_forecast": ("branch",), "get_inventory_and_inbound": ("branch",),
    "get_transport_options": (), "simulate_options": ("overrides",),
    "check_constraints": ("plan_key",), "get_fact_checks": (),
    "draft_evidence_request": ("fact_id", "ask_text"),
}


def test_only_the_eight_approved_async_capabilities_are_exposed():
    from agent import tools
    case = {tool.__name__: tool for tool in tools.CASE_TOOLS}
    assert set(case) == set(TOOL_PARAMS)
    assert {t.__name__ for t in tools.ASK_TOOLS} == set(case) - {"draft_evidence_request"}
    assert tools.SCOPE_TOOLS["SIGNAL"] == []
    for name, tool in case.items():
        assert inspect.iscoroutinefunction(tool), name
        assert set(inspect.signature(tool).parameters) == {*TOOL_PARAMS[name], "tool_context"}, name
        assert not any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                       for p in inspect.signature(tool).parameters.values()), name


def reachable_source(functions):
    """Follow local/API callables and import aliases; no runtime tool invocation."""
    queue, visited, sources = list(functions), set(), []
    while queue:
        fn = queue.pop()
        if fn in visited or not inspect.isfunction(fn):
            continue
        visited.add(fn)
        source = inspect.getsource(fn)
        tree = ast.parse(dedent(source))
        sources.append((fn.__module__ + "." + fn.__name__, tree))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            parts = []
            while isinstance(target, ast.Attribute):
                parts.insert(0, target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                obj = fn.__globals__.get(target.id)
                for part in parts:
                    obj = getattr(obj, part, None)
                if inspect.isfunction(obj) and obj.__module__.startswith(("agent.", "api.")):
                    # Existing pure readers/worker are trusted boundaries; scan
                    # every agent helper and explicitly forbid business APIs.
                    if obj.__module__.startswith("agent."):
                        queue.append(obj)
                    else:
                        sources.append((obj.__module__ + "." + obj.__name__, None))
    return sources


def test_reachable_tools_cannot_call_human_actions_or_read_hidden_truth():
    from agent import tools
    reached = reachable_source(tools.CASE_TOOLS)
    assert {"agent.tools." + n for n in TOOL_PARAMS}.issubset({n for n, _ in reached})
    forbidden = {"api.workflow." + n for n in ("send", "decide", "prepare", "wait", "reject", "adopt")}
    forbidden |= {"api.signals." + n for n in ("request_evidence", "confirm", "answer", "dismiss")}
    forbidden |= {"api.sandbox.reset", "api.refdata.truth"}
    for name, tree in reached:
        assert name not in forbidden and not name.startswith(("api.bookings.", "api.advance.")), name
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.search(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:truth_\w+|ref_truth)\b",
                                     node.value, re.I), (name, node.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "__import__"}, name


@pytest.mark.parametrize("field,foreign", [("run_id", "other-run"), ("scope", "ASK"),
    ("case_id", "other-case"), ("rec_version", "other-version"), ("plan_key", "other-plan")])
def test_case_reference_lookup_rejects_every_foreign_identity_component(run, field, foreign):
    from agent import tools
    from api import db
    from tests.api.test_evidence import active, story
    case = story(run)
    rec = active(run, case["case_id"])
    ctx = dict(scope="CASE", run_id=run["run_id"], case_id=case["case_id"],
               rec_version=rec["rec_version"], plan_key=rec["result"]["recommended"])
    with db.tx() as conn:
        own = tools.record(conn, ctx, "get_case", {}, {"qty": 82}, 3)
        bad = tools.record(conn, {**ctx, field: foreign}, "get_case", {}, {"qty": 9999}, 4)
        assert tools.scoped(conn, ctx, ctx) == {own: {"qty": 82}}
        assert bad != own


def test_ask_reference_lookup_adds_the_session_and_request_scope(run):
    from agent import tools
    from api import db
    from tests.api.test_evidence import active, story
    case = story(run)
    rec = active(run, case["case_id"])
    ctx = dict(scope="ASK", run_id=run["run_id"], case_id=case["case_id"], session_id="person-one",
               request_id="q-one", rec_version=rec["rec_version"], plan_key=rec["result"]["recommended"])
    with db.tx() as conn:
        own = tools.record(conn, ctx, "get_case", {}, {"qty": 82}, 3)
        tools.record(conn, {**ctx, "session_id": "person-two"}, "get_case", {}, {"qty": 9999}, 4)
        tools.record(conn, {**ctx, "request_id": "q-two"}, "get_case", {}, {"qty": 8888}, 4)
        assert tools.scoped(conn, ctx, ctx) == {own: {"qty": 82}}


def test_server_context_controls_case_read_and_stored_result_is_exact(run):
    from agent import tools
    from api import db
    from tests.api.conftest import seat
    from tests.api.test_evidence import active, story
    case = story(run)
    rec = active(run, case["case_id"])
    ctx = dict(scope="CASE", run_id=run["run_id"], case_id=case["case_id"], actor=seat(case["source_branch"]),
               rec_version=rec["rec_version"], plan_key=rec["result"]["recommended"])
    result = asyncio.run(tools.get_case(SimpleNamespace(state={"ctx": ctx})))
    assert result["case"] == case["key"] and result["recommended"] == rec["result"]["recommended"]
    assert result["lot"]["recorded_qty"] == next(l["qty"] for l in rec["engine_input"]["lots"]
                                                if l["lot_id"] == case["lot_id"])
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM tool_results WHERE run_id=:r AND result_id=:id",
                     r=run["run_id"], id=result["result_id"])
    assert row["result"] == {k: v for k, v in result.items() if k != "result_id"}
    assert (row["inputs"], row["scope"], row["case_id"], row["rec_version"], row["plan_key"]) == (
        {}, "CASE", case["case_id"], rec["rec_version"], ctx["plan_key"])
    assert row["duration_ms"] >= 0


# 09-08 executor additions; the complete original contract above is unchanged.
def bound_context(run):
    from tests.api.conftest import seat
    from tests.api.test_evidence import active, story
    case = story(run)
    rec = active(run, case["case_id"])
    ctx = dict(scope="CASE", run_id=run["run_id"], case_id=case["case_id"],
               actor=seat(case["source_branch"]), rec_version=rec["rec_version"],
               plan_key=rec["result"]["recommended"])
    return case, rec, ctx


@pytest.mark.parametrize("field,foreign", [("run_id", "foreign"), ("scope", "CASE"),
    ("signal_id", "foreign"), ("signal_revision", 2)])
def test_signal_audit_references_reject_every_foreign_axis(run, field, foreign):
    from agent import tools
    from api import db
    ctx = dict(scope="SIGNAL", run_id=run["run_id"], signal_id="signal-one", signal_revision=1)
    with db.tx() as conn:
        own = tools.record(conn, ctx, "get_case", {}, {"qty": 7}, 0)
        tools.record(conn, {**ctx, field: foreign}, "get_case", {}, {"qty": 900}, 0)
        assert tools.scoped(conn, ctx, ctx) == {own: {"qty": 7}}


@pytest.mark.parametrize("axis", ["run_id", "scope", "case_id", "rec_version", "plan_key", "snapshot_id"])
def test_reference_scope_cannot_replace_the_authorized_scope(run, axis):
    from agent import tools
    from api import db
    _, _, ctx = bound_context(run)
    foreign = {**ctx, axis: "foreign"}
    with db.tx() as conn:
        tools.record(conn, foreign, "get_case", {}, {"secret": "foreign"}, 0)
        assert tools.scoped(conn, ctx, foreign) == {}


@pytest.mark.parametrize("axis", ["account", "role", "branch"])
def test_personal_reference_identity_includes_the_server_actor(run, axis):
    from agent import tools
    from api import db
    _, _, ctx = bound_context(run)
    ctx.update(scope="ASK", session_id="one", request_id="question")
    foreign = {**ctx, "actor": {**ctx["actor"], axis: "foreign"}}
    with db.tx() as conn:
        own = tools.record(conn, ctx, "get_case", {}, {"qty": 7}, 0)
        tools.record(conn, foreign, "get_case", {}, {"secret": "foreign"}, 0)
        assert tools.scoped(conn, ctx, ctx) == {own: {"qty": 7}}
        assert tools.scoped(conn, ctx, foreign) == {}


def test_all_read_tools_use_real_frozen_data_and_have_no_business_effect(run):
    from agent import tools
    from api import db
    from tests.agent.test_brief import business_state
    case, rec, ctx = bound_context(run)
    before = business_state(run["run_id"])
    tc = SimpleNamespace(state={"ctx": ctx})
    async def read():
        return await asyncio.gather(tools.get_case(tc), tools.get_forecast(case["source_branch"], tc),
            tools.get_inventory_and_inbound(case["source_branch"], tc), tools.get_transport_options(tc),
            tools.check_constraints(ctx["plan_key"], tc), tools.get_fact_checks(tc))
    result = asyncio.run(read())
    assert all("error" not in r and r["result_id"] for r in result), result
    from api import refdata
    ref = refdata.get(run["dataset_version"])
    assert result[1]["days"] and all(f["provider"] == ref.provider_of(rec["day"], case["source_branch"],
        case["sku"], f["day"]) for f in result[1]["days"])
    assert [{k: f[k] for k in ("branch", "day", "low", "base", "high")} for f in result[1]["days"]] == [
        f for f in rec["engine_input"]["forecasts"] if f["branch"] == case["source_branch"]]
    assert result[2]["lots"] == [l for l in rec["engine_input"]["lots"] if l["branch"] == case["source_branch"]]
    assert [r["run_code"] for r in result[3]["runs"]] == [r["run_code"] for r in rec["engine_input"]["runs"]]
    assert result[4]["passes"] is True
    assert business_state(run["run_id"]) == before
    with db.tx() as conn:
        audits = tools.scoped(conn, ctx, ctx)
    assert audits == {r["result_id"]: {k: v for k, v in r.items() if k != "result_id"} for r in result}


def test_wrong_role_branch_or_scope_is_an_audited_refusal(run):
    from agent import tools
    from api import db
    _, _, ctx = bound_context(run)
    bad = {**ctx, "actor": {"account": "foreign", "role": "manager", "branch": "foreign"}}
    result = asyncio.run(tools.get_case(SimpleNamespace(state={"ctx": bad})))
    assert result["error"] == "forbidden" and "lot" not in result
    tc = SimpleNamespace(state={"ctx": ctx})
    branch = asyncio.run(tools.get_inventory_and_inbound("foreign", tc))
    assert branch["error"] == "forbidden" and "lots" not in branch
    signal = asyncio.run(tools.get_case(SimpleNamespace(state={"ctx": {**ctx, "scope": "SIGNAL"}})))
    assert signal["error"] == "forbidden"
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM tool_results WHERE run_id=:r", r=run["run_id"])
    assert len(rows) == 3 and all(r["status"] == "FAILED" and r["purpose"] for r in rows)


def test_manager_selection_checks_are_authoritative_without_relabelling_the_engine(run):
    from agent import tools
    from api import selection
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call
    case, rec, ctx = bound_context(run)
    key = feasible_others(run, case["case_id"])[0]
    call(run, selection.select, ctx["actor"], case["case_id"], ctx["rec_version"], key)
    stale = asyncio.run(tools.get_fact_checks(SimpleNamespace(state={"ctx": ctx})))
    assert stale["error"] == "stale_plan"
    ctx = {**ctx, "plan_key": key}
    async def read():
        tc = SimpleNamespace(state={"ctx": ctx})
        return await tools.get_case(tc), await tools.get_fact_checks(tc)
    detail, checks = asyncio.run(read())
    assert detail["recommended"] == rec["result"]["recommended"]
    assert detail["manager_selection"]["plan_key"] == key
    from api import cases, db
    with db.tx() as conn:
        selected = cases.case_row(conn, run["run_id"], case["case_id"])["selected"]
    assert set(checks["fact_checks"]) == {f["fact_id"] for f in selected["fact_checks"]}


def test_draft_is_checked_immutable_and_links_an_existing_human_request(run):
    from agent import tools
    from api import db
    from tests.agent.test_brief import business_state
    from tests.api.test_evidence import ask, phantom
    case, _, ctx = bound_context(run)
    fact = phantom(run, case["case_id"])
    tc = SimpleNamespace(state={"ctx": ctx})
    before = business_state(run["run_id"])
    made = asyncio.run(tools.draft_evidence_request(fact["fact_id"], "Please count USD 123456789.12 of stock.", tc))
    assert made["state"] == "DRAFT" and made["request_id"] is None
    assert made["original_text"] == "⚠ number removed: not traceable to a tool result"
    again = asyncio.run(tools.draft_evidence_request(fact["fact_id"], "Replacement wording.", tc))
    assert again["draft_id"] == made["draft_id"]
    assert business_state(run["run_id"]) == before
    with db.tx() as conn:
        saved = db.rows(conn, "SELECT * FROM evidence_drafts WHERE run_id=:r", r=run["run_id"])
    assert len(saved) == 1 and saved[0]["original_text"] == made["original_text"]
    assert saved[0]["request_context"]["lot_ids"] == fact["lot_ids"]
    assert saved[0].get("due_at") is None and saved[0].get("sent_at") is None
    # Clear only our draft test row to exercise the existing-request path.
    with db.tx() as conn:
        db.run(conn, "DELETE FROM evidence_drafts WHERE run_id=:r", r=run["run_id"])
    request = ask(run, case["case_id"], fact["fact_id"])
    linked = asyncio.run(tools.draft_evidence_request(fact["fact_id"], "Please count the recorded stock.", tc))
    assert linked["request_id"] == request["request_id"] and linked["draft_id"] is None


def test_real_adk_tool_context_returns_the_exact_audited_result(run, fake_model, monkeypatch):
    from agent import model, prompts, tools
    from google.genai import types
    from tests.agent.conftest import drain
    from tests.agent.test_model import response
    _, _, ctx = bound_context(run)
    fake = fake_model([response(calls=[("get_case", {})]), response("Review the engine's recorded options.")])
    monkeypatch.setattr(model, "llm", lambda: fake)
    events = asyncio.run(drain(model.run(prompts.brief, [types.Part(text="Explain this case.")],
                                         tools=tools.CASE_TOOLS, ctx=ctx)))
    assert events[-1]["type"] == "final" and len(fake.requests) == 2
    tool_result, = [e for e in events if e["type"] == "tool_result"]
    assert tool_result["result_id"]
    assert ctx["case_id"] not in str(fake.requests[0].config.tools)


def test_fact_checks_retain_an_open_request_deadline_as_json(run):
    from agent import tools
    from tests.api.test_evidence import ask, phantom
    case, _, ctx = bound_context(run)
    fact = phantom(run, case["case_id"])
    request = ask(run, case["case_id"], fact["fact_id"])
    encoding_error = None
    try:
        result = asyncio.run(tools.get_fact_checks(SimpleNamespace(state={"ctx": ctx})))
    except TypeError as exc:
        encoding_error = exc
    assert encoding_error is None, "A real request deadline must be JSON-safe in tool output and audit"
    linked = result["fact_checks"][fact["fact_id"]]["request"]
    assert linked["request_id"] == request["request_id"]
    assert isinstance(linked["due_at"], str) and linked["due_at"]
