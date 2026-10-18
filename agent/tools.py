"""Server-bound application tools. The model may read, simulate and draft only.

Every result is the exact compact JSON retained in tool_results. No model tool
calls a human action, reads physical truth, or publishes an answer. The caller
still owns admission, numeric checking and the final current-key check.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
import json
import threading
import time
import uuid

from google.adk.tools import ToolContext
from sqlalchemy.exc import SQLAlchemyError

from api import accounts, cases, commitment, config as api_config, db, inputs, refdata, selection, workflow
from api.errors import ApiError, engine_error
from engine import from_dict
from engine.adjust import ADOPTABLE, AdjustmentError, InputError
from engine.limits import check_plan
from engine.model import KEEP
from engine.simulate import HorizonError
from engine.timing import latest_decision_min

from . import config, numbers

SCOPE_KEYS = {
    "CASE": ("case_id", "rec_version", "plan_key"),
    "ASK": ("case_id", "rec_version", "plan_key", "session_id", "request_id"),
    "SIGNAL": ("signal_id", "signal_revision"),
}
PURPOSES = {
    "get_case": "Reading the recorded case and engine options",
    "get_forecast": "Checking the frozen forecast",
    "get_inventory_and_inbound": "Checking recorded stock and inbound",
    "get_transport_options": "Checking transport",
    "simulate_options": "Comparing a what-if",
    "check_constraints": "Checking limits",
    "get_fact_checks": "Reading the plan's flagged facts",
    "draft_evidence_request": "Drafting a request for human review",
}


class _Runtime:
    """Ephemeral request coordination, never serialized into ADK session state."""

    def __init__(self, limit=None):
        self.loop = asyncio.get_running_loop()
        self.reads = asyncio.Semaphore(2)
        self.simulations = asyncio.Semaphore(1)
        self.limit = min(limit or 8, config.load().max_tool_calls)
        self.calls = 0
        self.cancelled = threading.Event()


_runtime_context: ContextVar[_Runtime | None] = ContextVar("shelfshift_tool_runtime", default=None)


def begin_request(limit=None):
    """The model producer owns one runtime shared by its ADK child tasks."""
    return _runtime_context.set(_Runtime(limit))


def end_request(token):
    runtime = _runtime_context.get()
    if runtime is not None:
        runtime.cancelled.set()
    _runtime_context.reset(token)


def _runtime(tool_context):
    runtime = _runtime_context.get()
    if runtime is None:
        # Direct server callers may reuse one ToolContext without a model
        # producer. The fallback belongs to that object and event loop only.
        runtime = getattr(tool_context, "_shelfshift_runtime", None)
        if runtime is None or runtime.loop is not asyncio.get_running_loop():
            runtime = _Runtime()
            tool_context._shelfshift_runtime = runtime
    return runtime


def _consume_finished(task):
    if not task.cancelled():
        task.exception()


def _interrupted():
    return {"error": "interrupted", "message": "This request was interrupted."}


def _actor(ctx):
    return accounts.logged_actor(ctx.get("actor") or {})


def record(conn, ctx: dict, tool: str, inputs_: dict, result: dict, ms: int) -> str:
    """Trusted audit primitive, also used for unsuccessful tool attempts."""
    rid = "R" + uuid.uuid4().hex
    db.run(conn, "INSERT INTO tool_results (run_id, result_id, scope, session_id, case_id, rec_version, plan_key, "
                 "signal_id, signal_revision, request_id, actor, snapshot_id, tool, inputs, result, duration_ms, "
                 "status, purpose) VALUES (:r, :id, :sc, :s, :c, :v, :p, :sig, :rev, :q, CAST(:a AS jsonb), "
                 ":snap, :t, CAST(:i AS jsonb), CAST(:res AS jsonb), :ms, :st, :why)",
           r=ctx["run_id"], id=rid, sc=ctx["scope"], s=ctx.get("session_id"), c=ctx.get("case_id"),
           v=ctx.get("rec_version"), p=ctx.get("plan_key"), sig=ctx.get("signal_id"),
           rev=ctx.get("signal_revision"), q=ctx.get("request_id"), a=db.js(_actor(ctx)),
           snap=ctx.get("snapshot_id"), t=tool, i=db.js(inputs_), res=db.js(result), ms=ms,
           st="FAILED" if "error" in result else "SUCCEEDED", why=PURPOSES[tool])
    return rid


def scoped(conn, ctx: dict, scope: dict) -> dict[str, dict]:
    """Resolve only the authenticated caller's exact original content scope.

    Historical readers supply an already reauthorized original tuple. A claimed
    result ID or scope cannot override that tuple. CASE is shared; ASK adds the
    session, request and actor. SIGNAL references never become CASE references.
    """
    kind = ctx.get("scope")
    if kind not in SCOPE_KEYS or scope.get("scope") != kind:
        return {}
    keys = ("run_id", "scope", *SCOPE_KEYS[kind], "snapshot_id")
    if any(ctx.get(k) != scope.get(k) for k in keys):
        return {}
    if not ctx.get("run_id") or any(ctx.get(k) is None for k in SCOPE_KEYS[kind]):
        return {}
    if kind == "ASK" and _actor(ctx) != _actor(scope):
        return {}
    where = " AND ".join(f"{k} IS NOT DISTINCT FROM :{k}" for k in keys)
    params = {k: ctx.get(k) for k in keys}
    if kind == "ASK":
        where += " AND actor = CAST(:actor AS jsonb)"
        params["actor"] = db.js(_actor(ctx))
    rows = db.rows(conn, f"SELECT result_id, result FROM tool_results WHERE {where}", **params)
    return {row["result_id"]: row["result"] for row in rows}


def _bound(conn, run, ctx):
    case = cases.case_row(conn, run["run_id"], ctx["case_id"])
    rec = cases.active_rec(conn, case)
    if rec is None or rec["rec_version"] != ctx["rec_version"]:
        raise ApiError(409, "stale_version", workflow.STALE)
    if selection.plan_in_force(case, rec)[0] != ctx["plan_key"]:
        raise ApiError(409, "stale_plan", workflow.PLAN_CHANGED)
    return case, rec


def _authorize(conn, run, ctx, tool):
    scope = ctx.get("scope")
    if scope not in ("CASE", "ASK") or (scope == "ASK" and tool == "draft_evidence_request"):
        raise ApiError(403, "forbidden", "This tool is not available in this scope")
    if any(not ctx.get(k) for k in SCOPE_KEYS[scope]):
        raise ApiError(403, "forbidden", "The server context is incomplete")
    ref = refdata.get(run["dataset_version"])
    actor = _actor(ctx)
    if (actor["role"], actor["branch"]) != ("hq", None) and not (
            actor["role"] == "manager" and actor["branch"] in ref.branches):
        raise ApiError(403, "forbidden", "This role cannot read case data")
    expected = "HQ" if actor["role"] == "hq" else actor["branch"]
    if actor["account"] != expected:
        raise ApiError(403, "forbidden", "The server actor is inconsistent")
    if ctx.get("session_id"):
        session = db.one(conn, "SELECT * FROM sessions WHERE session_id = :s", s=ctx["session_id"])
        if (session is None or session["run_id"] != run["run_id"]
                or accounts.logged_actor(session) != actor):
            raise ApiError(403, "forbidden", "This session no longer has access")
    if scope == "ASK":
        request = db.one(conn, "SELECT * FROM chat_requests WHERE run_id = :r AND request_id = :q "
                              "AND session_id = :s AND case_id = :c", r=run["run_id"],
                         q=ctx["request_id"], s=ctx["session_id"], c=ctx["case_id"])
        if request is None or request["status"] != "RUNNING":
            raise ApiError(409, "not_available", "This question is no longer running")
    case, rec = _bound(conn, run, ctx)
    plan = cases.parse_plan(ctx["plan_key"])
    if actor["role"] == "manager" and actor["branch"] not in {case["source_branch"], plan.dest}:
        raise ApiError(403, "forbidden", "This case is not available to this account")


def _error(exc):
    if isinstance(exc, ApiError):
        return {"error": exc.code, "message": exc.message}
    mapped = engine_error(exc)
    if mapped:
        return {"error": mapped.code, "message": mapped.message}
    return {"error": "db_busy", "message": "Case data is unavailable right now. Try again."}


def call(ctx: dict, tool: str, inputs_: dict, fn) -> dict:
    """Own one connection/transaction in the worker thread; errors are data.

    A savepoint rolls back a failed service before writing its failure audit.
    A pool checkout failure has no connection on which an audit can be written;
    its safe response therefore has no invented result ID.
    """
    started = time.perf_counter()
    if not ctx.get("run_id"):
        # There is no authorized run under which an audit could be stored.
        return _error(ApiError(403, "forbidden", "The server context is incomplete"))

    def work(conn):
        try:
            with conn.begin_nested():
                run = db.lock_run(conn, ctx["run_id"], statuses=("ACTIVE", "INITIALIZING", "READY")
                                  if ctx.get("precomputed") else ("ACTIVE", "INITIALIZING"))
                if run["status"] == "READY" and (run["kind"] != "TEMPLATE" or run["failure"]):
                    raise ApiError(409, "not_available", "The baseline job requires a completed inactive template.")
                _authorize(conn, run, ctx, tool)
                out = fn(conn, run, ctx)
        except (ApiError, InputError, AdjustmentError, HorizonError, SQLAlchemyError) as exc:
            out = _error(exc)
        # Normalize once: the model and the stored JSON have identical values.
        # JSONB does not preserve object order. Keep the engine order on the
        # wire and carry explicit fact_order for persisted/model consumers.
        out = json.loads(json.dumps(inputs.plain(out), ensure_ascii=False))
        rid = record(conn, ctx, tool, inputs_, out, int((time.perf_counter() - started) * 1000))
        return {"result_id": rid, **out}

    try:
        return db.in_tx(work)
    except SQLAlchemyError as exc:
        return _error(exc)


async def _go(tool_context, tool, inputs_, fn):
    runtime = _runtime(tool_context)
    if runtime.cancelled.is_set():
        return _interrupted()
    # Count before waiting for a worker slot: queued calls consume the same
    # request budget as running calls, and the ninth never opens a connection.
    if runtime.calls >= runtime.limit:
        from .model import LimitReached
        raise LimitReached(f"Request limit: {runtime.limit} tool calls")
    runtime.calls += 1
    ctx = deepcopy(tool_context.state["ctx"])
    inputs_ = deepcopy(inputs_)

    async def transaction():
        async with runtime.reads:
            if runtime.cancelled.is_set():
                return _interrupted()
            return await asyncio.to_thread(call, ctx, tool, inputs_, fn)

    async def execute():
        if tool == "simulate_options":
            async with runtime.simulations:
                return await transaction()
        return await transaction()

    worker = asyncio.create_task(execute())
    try:
        # Cancelling the await cannot stop a running DB thread. Keep its slot
        # until the transaction/audit finishes; queued work checks the fence.
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        runtime.cancelled.set()
        worker.add_done_callback(_consume_finished)
        raise
    return _interrupted() if runtime.cancelled.is_set() else result


def _options(result, plan_key=None):
    """Best rows per engine group; keep the engine's metrics and ordering."""
    selected, groups = {}, set()
    for row in result["options"]:
        group = (row["kind"], row.get("dest"))
        if row["key"] in (result["recommended"], plan_key) or row["kind"] == KEEP or group not in groups:
            selected[row["key"]] = {k: v for k, v in row.items() if k != "key"}
        groups.add(group)
    return selected


def _result(result, plan_key=None):
    wins = result["scenario_wins"]
    return {"recommended": result["recommended"], "recommended_label": result["recommended_label"],
            "options": _options(result, plan_key), "rejected": result["rejected"],
            "scenario_wins": {**wins, "tested": len(wins["by_scenario"])},
            "crossovers": result.get("crossovers"), "wait": cases.wait_view(result, None),
            "decide_by": result.get("decide_by")}


async def get_case(tool_context: ToolContext) -> dict:
    """Read the recorded lot, state and engine options. Recommendation and manager
    selection are separately labelled; stock is a record, never physical truth."""
    def read(conn, run, ctx):
        case, rec = _bound(conn, run, ctx)
        inp = rec["engine_input"]
        lot = next(l for l in inp["lots"] if l["lot_id"] == case["lot_id"])
        chosen = selection.bound(case, rec)
        return {"case": case["key"], "state": case["state"], "product": case["sku"],
                "source": case["source_branch"], "today": rec["day"], "zone": inp["product"]["zone"],
                "lot": {"lot_id": lot["lot_id"], "recorded_qty": lot["qty"], "expiry_day": lot["expiry_day"],
                        "expiry_source": lot["expiry_source"], "last_verified_day": lot["last_verified_day"]},
                **_result(rec["result"], ctx["plan_key"]), "plan_key": ctx["plan_key"], "rec_version": ctx["rec_version"],
                "manager_selection": selection.card_view(chosen), "source_kind": "Recorded stock and calculated options",
                "currency": refdata.get(run["dataset_version"]).currency, "missing": []}
    return await _go(tool_context, "get_case", {}, read)


def _branch(branch, inp):
    permitted = {l["branch"] for l in inp["lots"]} | {f["branch"] for f in inp["forecasts"]}
    permitted |= {r["dest"] for r in inp["runs"]} | {r["origin"] for r in inp["runs"]}
    if not isinstance(branch, str) or branch not in permitted:
        raise ApiError(403, "forbidden", "That branch is outside this case's data")


async def get_forecast(branch: str, tool_context: ToolContext) -> dict:
    """Read frozen low/base/high demand for an authorized case branch, with the
    actual per-point provider and forecast run. Missing data stays explicit."""
    def read(conn, run, ctx):
        case, rec = _bound(conn, run, ctx)
        _branch(branch, rec["engine_input"])
        ref = refdata.get(run["dataset_version"])
        meta = ref.forecast_runs[rec["day"]]
        days = [{**f, "provider": ref.provider_of(rec["day"], branch, case["sku"], f["day"])}
                for f in rec["engine_input"]["forecasts"] if f["branch"] == branch]
        return {"branch": branch, "days": days, "forecast_run_id": meta["forecast_run_id"],
                "providers": meta["providers"], "cutoff_day": meta["cutoff_day"],
                "source_kind": "Frozen forecast", "units": "units", "missing": [] if days else ["forecast"]}
    return await _go(tool_context, "get_forecast", {"branch": branch}, read)


async def get_inventory_and_inbound(branch: str, tool_context: ToolContext) -> dict:
    """Read recorded lots, inbound and capacity for a branch in this frozen case.
    This is the recorded scenario; no quantity is a confirmed physical count."""
    def read(conn, run, ctx):
        _, rec = _bound(conn, run, ctx)
        inp = rec["engine_input"]
        _branch(branch, inp)
        return {"branch": branch, "lots": [l for l in inp["lots"] if l["branch"] == branch],
                "inbound": [i for i in inp["inbound"] if i["branch"] == branch],
                "capacity": [c for c in inp["capacity"] if c["branch"] == branch],
                "source_kind": "Frozen stock records", "units": "units; capacity in totes", "missing": []}
    return await _go(tool_context, "get_inventory_and_inbound", {"branch": branch}, read)


async def get_transport_options(tool_context: ToolContext) -> dict:
    """Read case runs, cold chain, cutoffs, spare totes and quotes. Decision
    deadlines use this snapshot's policy; missing cancellation quotes stay null."""
    def read(conn, run, ctx):
        _, rec = _bound(conn, run, ctx)
        inp = from_dict(rec["engine_input"])
        rows = [{**r, "latest_decision_min": latest_decision_min(actual, inp.policy)}
                for r, actual in zip(rec["engine_input"]["runs"], inp.runs)]
        return {"runs": rows, "source_kind": "Frozen transport records",
                "currency": refdata.get(run["dataset_version"]).currency,
                "missing": [{"run_code": r["run_code"], "field": "cancel_cost"} for r in rows if r["cancel_cost"] is None]}
    return await _go(tool_context, "get_transport_options", {}, read)


async def simulate_options(overrides: list[dict], tool_context: ToolContext) -> dict:
    """Compare validated hypothetical overrides through the engine service.
    Use only engine-supported assumptions or what-if kinds. Never adopts them;
    stock counts and expiry facts require human confirmation outside this tool."""
    def simulate(conn, run, ctx):
        out = workflow.whatif(conn, run, ctx["case_id"], overrides, rec_version=ctx["rec_version"])
        return {**_result(out["result"]), "what_if_version": out["rec_version"],
                "source_rec_version": ctx["rec_version"], "plan_key": ctx["plan_key"], "overrides": out["overrides"],
                "adoptable": all(o["kind"] in ADOPTABLE for o in out["overrides"]),
                "source_kind": "Engine what-if; not adopted", "missing": []}
    return await _go(tool_context, "simulate_options", {"overrides": overrides}, simulate)


async def check_constraints(plan_key: str, tool_context: ToolContext) -> dict:
    """Check an engine-generated plan from this bound version against today's
    raw record and bookings. Returns feasibility and verbatim engine failures."""
    def read(conn, run, ctx):
        case, rec = _bound(conn, run, ctx)
        if not isinstance(plan_key, str) or plan_key not in {o["key"] for o in rec["result"]["options"]}:
            raise ApiError(400, "not_an_option", selection.NOT_AN_OPTION)
        failures = list(check_plan(cases.current_input(conn, run, case), cases.parse_plan(plan_key)))
        return {"plan_key": plan_key, "passes": not failures, "failures": failures, "missing": []}
    return await _go(tool_context, "check_constraints", {"plan_key": plan_key}, read)


def _fact_checks(conn, run, ctx):
    case, rec = _bound(conn, run, ctx)
    _, checks, _ = selection.plan_in_force(case, rec)
    requests = db.rows(conn, "SELECT request_id, fact_id, state, to_branch, due_at FROM evidence_requests "
                            "WHERE run_id = :r AND case_id = :c AND rec_version = :v AND state IN ('OPEN', 'ANSWERED') "
                            "ORDER BY created_at, request_id", r=run["run_id"], c=case["case_id"], v=ctx["rec_version"])
    requests = [{**q, "due_at": cases.utc(q["due_at"]) if q["due_at"] else None} for q in requests]
    drafts = db.rows(conn, "SELECT draft_id, fact_id, state, request_id FROM evidence_drafts "
                          "WHERE run_id = :r AND case_id = :c AND rec_version = :v AND plan_key = :p "
                          "ORDER BY created_at, draft_id", r=run["run_id"], c=case["case_id"],
                     v=ctx["rec_version"], p=ctx["plan_key"])
    required, advisory = commitment.classes(checks)
    ordered = required + advisory + [f for f in checks if f not in required and f not in advisory]
    facts = {}
    for fact in ordered:
        class_ = "Required" if fact in required else "Advisory" if fact in advisory else None
        request = next((q for q in requests if q["fact_id"] == fact["fact_id"]), None)
        draft = next((d for d in drafts if d["fact_id"] == fact["fact_id"]), None)
        reason = ("This fact must be confirmed before the plan can proceed." if class_ == "Required" else
                  "This fact could change the plan or its expected benefit." if class_ == "Advisory" else
                  "The engine did not flag this fact as changing the plan.")
        action = ("view_request" if request else "review_draft" if draft else
                  "request_check" if class_ and fact["kind"] in commitment.ASKABLE else "review_current_checks")
        facts[fact["fact_id"]] = {**cases.in_engine_order(fact), "class": class_, "owner": case["source_branch"],
                                 "to_branch": fact["branch"], "reason": reason, "next_action": action,
                                 "request": request, "draft": draft}
    return {"rec_version": ctx["rec_version"], "plan_key": ctx["plan_key"],
            "fact_checks": facts, "fact_order": list(facts),
            "reason": ("No engine-flagged stock or expiry checks need a draft for this plan." if not any(
                f["class"] and f["kind"] in commitment.ASKABLE for f in facts.values()) else None),
            "next_action": "review_current_checks",
            "source_kind": "Plan-in-force engine checks", "missing": []}


async def get_fact_checks(tool_context: ToolContext) -> dict:
    """Read the plan in force's Required then Advisory facts, engine values,
    regret and unserved units, with any existing open request or checked draft."""
    return await _go(tool_context, "get_fact_checks", {}, _fact_checks)


def _draft(conn, run, ctx, fact_id, ask_text):
    case, rec = _bound(conn, run, ctx)
    # Lock only for this short draft write; recheck the tuple after waiting.
    cases.case_row(conn, run["run_id"], case["case_id"], lock=True)
    case, rec = _bound(conn, run, ctx)
    if case["state"] not in cases.LIVE_STATES:
        raise ApiError(409, "wrong_state", "This case cannot receive a new draft")
    if (not isinstance(ask_text, str) or not ask_text.strip() or "\x00" in ask_text
            or len(ask_text) > api_config.SETTINGS["requests"]["max_message_chars"]):
        raise ApiError(400, "bad_request", "Give a nonempty request within the message limit")
    if not isinstance(fact_id, str) or not fact_id.strip() or "\x00" in fact_id:
        raise ApiError(400, "unknown_fact", "Choose an engine-flagged stock or expiry fact")
    checks = _fact_checks(conn, run, ctx)
    fact = checks["fact_checks"].get(fact_id)
    if fact is None or fact["class"] is None or fact["kind"] not in commitment.ASKABLE:
        raise ApiError(400, "unknown_fact", "Choose a stock or expiry fact flagged for the plan in force")
    if fact["request"]:
        return {"request_id": fact["request"]["request_id"],
                "draft_id": fact["draft"]["draft_id"] if fact["draft"] else None, "linked": True,
                "next_action": "view_request"}
    if fact["draft"]:
        return {"draft_id": fact["draft"]["draft_id"], "request_id": fact["draft"]["request_id"],
                "next_action": "review_draft"}
    checked = numbers.check(ask_text, scoped(conn, ctx, ctx), ref=refdata.get(run["dataset_version"]))
    did = "D" + uuid.uuid4().hex
    context = {"branch": fact["branch"], "sku": case["sku"], "lot_ids": fact["lot_ids"],
               "fact_id": fact_id, "class": fact["class"],
               "required_parts": ["printed date per lot"] if fact["kind"] == "ESTIMATED_EXPIRY"
               else ["shelf count per lot", "backroom count per lot"]}
    db.run(conn, "INSERT INTO evidence_drafts (run_id, draft_id, case_id, rec_version, plan_key, fact_id, class, "
                 "owner, to_branch, sku, lot_ids, original_text, refs, checks, model_id, assistant_version, "
                 "request_context, state) VALUES (:r, :id, :c, :v, :p, :f, :cl, :o, :b, :sku, CAST(:lots AS jsonb), "
                 ":text, CAST(:refs AS jsonb), CAST(:checks AS jsonb), :m, :av, CAST(:context AS jsonb), 'DRAFT')",
           r=run["run_id"], id=did, c=case["case_id"], v=ctx["rec_version"], p=ctx["plan_key"], f=fact_id,
           cl=fact["class"], o=case["source_branch"], b=fact["branch"], sku=case["sku"], lots=db.js(fact["lot_ids"]),
           text=checked["text"], refs=db.js(checked["refs"]),
           checks=db.js({k: checked[k] for k in ("claims", "removed")}),
           m=ctx.get("model_id") or config.model_id(), av=ctx.get("assistant_version") or config.ASSISTANT_VERSION,
           context=db.js(context))
    db.log(conn, run["run_id"], run["day"], "evidence_drafted", case["case_id"], draft_id=did,
           rec_version=ctx["rec_version"], plan_key=ctx["plan_key"], fact_id=fact_id)
    return {"draft_id": did, "fact_id": fact_id, "class": fact["class"], "request_id": None,
            "original_text": checked["text"], "refs": checked["refs"], "checks": {k: checked[k] for k in ("claims", "removed")},
            "request_context": context, "state": "DRAFT"}


async def draft_evidence_request(fact_id: str, ask_text: str, tool_context: ToolContext) -> dict:
    """Store a checked draft for an engine-flagged stock/expiry fact, CASE only.
    Required first. Existing requests/drafts are linked. No send, deadline,
    confirmed fact or request slot: the source manager must review and send."""
    return await _go(tool_context, "draft_evidence_request", {"fact_id": fact_id, "ask_text": ask_text},
                     lambda conn, run, ctx: _draft(conn, run, ctx, fact_id, ask_text))


CASE_TOOLS = [get_case, get_forecast, get_inventory_and_inbound, get_transport_options,
              simulate_options, check_constraints, get_fact_checks, draft_evidence_request]
ASK_TOOLS = [tool for tool in CASE_TOOLS if tool is not draft_evidence_request]
SCOPE_TOOLS = {"CASE": CASE_TOOLS, "ASK": ASK_TOOLS, "SIGNAL": []}
