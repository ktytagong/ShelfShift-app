"""Real evaluation bindings with context-local provider reservations and audits.

Every binding drains the complete application service. Public observation seams
retain unchecked candidates and provider attempts without replacing safeguards.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
from threading import Lock
from types import SimpleNamespace
import uuid

from api import config, db
from agent import config as assistant_config
from .budget import BudgetReached
from .runner import StepResult

_current = ContextVar("eval_provider_observation", default=None)
_inside_transport = ContextVar("eval_inside_transport", default=False)
_verified_request = ContextVar("eval_verified_generation_request", default=None)
_install_lock = Lock()
_installed = False


class _Meter:
    def __init__(self, ctx, *, reading=None):
        if reading is None:
            reading = ctx.step["action"] in ("ai:reading", "ai:answer_reading")
        self.ctx, self.calls, self.raw_text, self.blocked, self.reading = ctx, 0, "", None, reading
        self.settled = False
        settings = assistant_config.load()
        if ctx.step["action"] == "ai:summary":
            from agent import impact
            settings = impact.limits()
        self.input_limit = config.READING["max_input_tokens"] if reading else settings.max_request_tokens
        self.output_limit = config.READING["max_output_tokens"] if reading else settings.max_output_tokens

    def admit(self):
        try:
            self.ctx.budget.allow(input_tokens=self.input_limit, output_tokens=self.output_limit)
        except BudgetReached as exc:
            self.blocked = exc
            raise
        self.calls += 1

    def settle(self, usage=None):
        if self.settled or not self.calls:
            return
        self.settled = True
        measured = _usage(usage)
        turns = (usage or {}).get("model_calls")
        if type(turns) is int and 0 < turns < self.calls:
            # A retried HTTP attempt has no independent usage metadata. Its
            # reservation remains spent; reported successful turns cannot
            # refund it. Close it now so a later step cannot settle it by FIFO.
            try:
                self.ctx.budget.charge(measured, calls=turns)
            finally:
                self.ctx.budget.charge(_usage(None), calls=self.calls - turns)
        else:
            self.ctx.budget.charge(measured, calls=self.calls)


async def _prove_prompt(transport, request, meter):
    """Count the serialized Vertex prompt before any generation admission.

    Vertex countTokens accepts contents (including history and inline media),
    systemInstruction, tools and generationConfig. Forward those exact fields,
    including the response schema; a count of only the user's text is unsafe.
    Cached or externally referenced content is refused because it can change
    between counting and generation. Unknown prompt fields also fail closed.
    """
    import httpx
    from agent import impact, model

    response = None
    try:
        payload = json.loads(await request.aread())
        count_fields = {"contents", "systemInstruction", "tools", "generationConfig", "model"}
        if not isinstance(payload, dict) or set(payload) - count_fields - {"safetySettings", "labels"}:
            raise ValueError("Unsupported evaluation prompt fields")
        if not isinstance(payload.get("contents"), list) or not payload["contents"]:
            raise ValueError("A complete evaluation prompt is required")

        def external(value):
            if isinstance(value, dict):
                return "fileData" in value or "cachedContent" in value or any(external(v) for v in value.values())
            return isinstance(value, list) and any(external(v) for v in value)

        if external(payload):
            raise ValueError("External prompt content cannot be bounded")
        maximum = payload.get("generationConfig", {}).get("maxOutputTokens")
        if type(maximum) is not int or not 0 < maximum <= meter.output_limit:
            raise ValueError("The generation output must fit its reservation")
        # Bypass both service meters: counting is not a generation attempt.
        raw = transport
        while isinstance(raw, (model.AttemptTransport, impact.MeteredTransport)):
            if raw.inner is None:
                raw.inner = httpx.AsyncHTTPTransport()
            raw = raw.inner
        url = request.url.copy_with(path=request.url.path.rsplit(":", 1)[0] + ":countTokens")
        url = url.copy_remove_param("alt")
        headers = [(key, value) for key, value in request.headers.multi_items() if key.lower() != "content-length"]
        counted = httpx.Request("POST", url, headers=headers,
            json={key: value for key, value in payload.items() if key in count_fields},
            extensions=dict(request.extensions))
        response = await raw.handle_async_request(counted)
        await response.aread()
        if response.status_code != 200:
            raise ValueError("Prompt measurement did not succeed")
        total = response.json().get("totalTokens")
        if type(total) is not int or not 0 <= total <= meter.input_limit:
            raise ValueError("Prompt measurement cannot fit its reservation")
    except (ValueError, TypeError, AttributeError, httpx.HTTPError) as exc:
        # Service runners may convert exceptions to a fallback. Keep this
        # sticky so the evaluation still stops as CAPPED without another call.
        meter.blocked = BudgetReached("The complete evaluation prompt could not be bounded before generation")
        raise meter.blocked from exc
    finally:
        if response is not None:
            await response.aclose()


def _install_observers():
    """Install inert wrappers once; only a runner-local context activates them.

    The real HTTP transport counts each generation attempt, including retries.
    ADK's public attempt observer counts scripted BaseLlm fakes without HTTP.
    count_tokens requests are not generation attempts. An actual client factory
    is still the same model.llm seam used by the production services.
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        from agent import impact, model, readings_job
        original_attempt = model.record_provider_attempt
        original_run = model.run
        original_signal_run = impact.run_agent
        original_client = readings_job.genai_client

        @wraps(original_attempt)
        def record_attempt():
            original_attempt()
            meter = _current.get()
            if meter is not None and not _inside_transport.get():
                meter.admit()

        def observe_transport(original):
            @wraps(original)
            async def transport(self, request):
                meter = _current.get()
                path = request.url.path.lower()
                verified = None
                if (meter is not None and _verified_request.get() is not request
                    and path.endswith((":generatecontent", ":streamgeneratecontent"))):
                    await _prove_prompt(self, request, meter)
                    meter.admit()
                    verified = _verified_request.set(request)
                token = _inside_transport.set(True)
                try:
                    return await original(self, request)
                finally:
                    _inside_transport.reset(token)
                    if verified is not None:
                        _verified_request.reset(verified)
            return transport

        def observe_runner(original):
            @wraps(original)
            async def observed_run(*args, **kwargs):
                async with aclosing(original(*args, **kwargs)) as stream:
                    async for event in stream:
                        meter = _current.get()
                        if meter is not None and event.get("type") in ("final", "candidate"):
                            meter.raw_text = event.get("text", "")
                        yield event
            return observed_run

        @wraps(original_client)
        def observed_client():
            from google import genai
            client = original_client()
            if _current.get() is None:
                return client
            underlying = client.aio.models

            async def generate_content(**kwargs):
                meter = _current.get()
                # Real SDK attempts are metered at its configured transport.
                # Scripted clients have no transport and get one real fake call.
                if meter is not None and not isinstance(client, genai.Client):
                    meter.admit()
                result = await underlying.generate_content(**kwargs)
                if meter is not None:
                    try:
                        raw = getattr(result, "text", None)
                    except (AttributeError, ValueError):
                        raw = None
                    if isinstance(raw, str):
                        meter.raw_text = raw
                    else:
                        parsed = getattr(result, "parsed", None)
                        if hasattr(parsed, "model_dump"):
                            meter.raw_text = json.dumps(parsed.model_dump(mode="json"), ensure_ascii=False)
                return result

            class Models:
                def __getattr__(self, name):
                    return getattr(underlying, name)

            models = Models()
            models.generate_content = generate_content

            class Client:
                def __getattr__(self, name):
                    return getattr(client, name)

            wrapped = Client()
            wrapped.aio = SimpleNamespace(models=models, aclose=client.aio.aclose)
            return wrapped

        model.record_provider_attempt = record_attempt
        model.AttemptTransport.handle_async_request = observe_transport(model.AttemptTransport.handle_async_request)
        impact.MeteredTransport.handle_async_request = observe_transport(impact.MeteredTransport.handle_async_request)
        model.run = observe_runner(original_run)
        impact.run_agent = observe_runner(original_signal_run)
        readings_job.genai_client = observed_client
        _installed = True


def maximum_calls(action):
    # The assistant permits a single provider retry per request. A tool-free
    # structured reading inherits the same bounded one-retry SDK configuration.
    if action in ("reading", "answer_reading"):
        return 2
    if action == "summary":
        from agent import impact
        return impact.limits().max_tool_calls + 3
    return assistant_config.load().max_llm_calls + 1


def precheck(ctx):
    action = ctx.step["action"].split(":", 1)[1]
    if ctx.budget.totals()["calls"] + maximum_calls(action) > ctx.budget.caps["max_calls"]:
        raise BudgetReached("Evaluation budget cannot fit this service's maximum model calls")


def _usage(value):
    value = value or {}
    return dict(input=value.get("input_tokens"), output=value.get("output_tokens"),
                thoughts=value.get("thinking_tokens"))


@contextmanager
def _observation(ctx, *, reading=False):
    _install_observers()
    meter = _Meter(ctx, reading=reading)
    token = _current.set(meter)
    try:
        yield meter
    except BaseException:
        meter.settle()
        raise
    finally:
        _current.reset(token)


def _session(ctx, seat):
    from api import accounts
    sessions = ctx.state.setdefault("eval_sessions", {})
    if seat not in sessions:
        actor = ctx.actors[seat]
        with db.tx() as conn:
            session_id = accounts.open_row(conn, actor["account"], actor["role"], actor["branch"])
            db.run(conn, "UPDATE sessions SET run_id=:r WHERE session_id=:s",
                   r=ctx.workspace["run_id"], s=session_id)
            sessions[seat] = db.one(conn, "SELECT * FROM sessions WHERE session_id=:s", s=session_id)
    return sessions[seat]


def _reading_seat(ctx):
    return {"destination": "destination", "plan_run": "hq"}.get(
        ctx.scenario["selector"].get("target"), "source")


def _attachment(ctx):
    raw = ctx.step.get("attachment") or ctx.inputs.get(ctx.step.get("attachment_ref", ""))
    if not raw:
        return None, None
    if isinstance(raw, dict) and ctx.step.get("attachment_ref") == "quote_pdf":
        from api import preload
        quote, data = preload.quote_for_run(ctx.workspace, raw["run_code"], ambiguous=True)
        if (any(raw.get(key) != value for key, value in quote.items())
                or hashlib.sha256(data).hexdigest() != raw["sha256"]):
            raise ValueError("Evaluation quote differs from the approved reference")
        return data, "application/pdf"
    path = (ctx.eval_dir / raw).resolve()
    if not path.is_relative_to(ctx.eval_dir.resolve()) or not path.is_file():
        raise ValueError("Evaluation attachment is missing")
    from api import files
    data = path.read_bytes()
    return data, files.sniff(data)


def _audit(ctx, *, request_id=None, signal_id=None, revision=None):
    clauses, bindings = ["run_id=:r"], {"r": ctx.workspace["run_id"]}
    for column, value in (("request_id", request_id), ("signal_id", signal_id), ("signal_revision", revision)):
        if value is not None:
            clauses.append(column + "=:" + column)
            bindings[column] = value
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT tool, inputs, result, result_id FROM tool_results WHERE " +
            " AND ".join(clauses) + " ORDER BY created_at, result_id", **bindings)
    return ([dict(name=row["tool"], args=row["inputs"], result_id=row["result_id"]) for row in rows],
            {row["result_id"]: row["result"] for row in rows})


async def _drain(stream):
    events = []
    async with aclosing(stream):
        async for event in stream:
            events.append(event)
    return events


def _result(ctx, meter, response, usage, *, outputs=None, served="live", tool_calls=None, tool_results=None):
    from agent import numbers
    measured = _usage(usage)
    meter.settle(usage)
    if meter.blocked is not None:
        raise meter.blocked
    return StepResult(served=served, model_calls=meter.calls,
        cache_hits=int(served in ("reuse", "precomputed")), raw_text=meter.raw_text,
        displayed_text=numbers.render(response.get("text", ""), response.get("refs") or []),
        captured_prompt=ctx.step.get("prompt"),
        tool_calls=tool_calls or [], tool_results=tool_results or {}, usage=measured,
        outputs=outputs if outputs is not None else response,
        note=response.get("note", False), numbers_removed=response.get("numbers_removed", (response.get("checks") or {}).get("removed")),
        preview_rows=ctx.state.get("preview_rows", []),
        error=None if response.get("status") in ("DONE", "CURRENT") or response.get("outcome") == "no_change"
        else response.get("reason") or "Service did not complete")


def _reading(ctx):
    from agent import readings_job
    precheck(ctx)
    session = _session(ctx, _reading_seat(ctx))
    text, case_id = ctx.step.get("prompt", ""), ctx.inputs.get("case_id")
    data, mime = _attachment(ctx)
    with _observation(ctx, reading=True) as meter:
        response = asyncio.run(readings_job.read_report(session, text, data, mime, case_id=case_id))
        with db.tx() as conn:
            saved = db.one(conn, "SELECT usage FROM readings WHERE run_id=:r AND reading_id=:id",
                           r=ctx.workspace["run_id"], id=response["reading_id"])
    ctx.state["reading"] = dict(response=response, text=text, file=data, mime=mime,
                                case_id=case_id, session=session)
    output = {**{row["field"]: row["value"] for row in response["suggestions"]}, "kind": response["kind"]}
    return _result(ctx, meter, {**response, "text": json.dumps(response["suggestions"], ensure_ascii=False)},
                   saved["usage"], outputs=output, served=response["served"])


def _answer_reading(ctx):
    from agent import readings_job
    from api import signals
    precheck(ctx)
    answer = ctx.state["answer"]
    session = _session(ctx, "source")
    with _observation(ctx, reading=True) as meter:
        response = asyncio.run(readings_job.read_answer(session, answer["signal_id"], answer["revision"]))
        with db.tx() as conn:
            saved = db.one(conn, "SELECT usage FROM readings WHERE run_id=:r AND reading_id=:id",
                           r=ctx.workspace["run_id"], id=response["reading_id"])
            signal = db.one(conn, "SELECT * FROM signals WHERE run_id=:r AND signal_id=:s",
                            r=ctx.workspace["run_id"], s=answer["signal_id"])
            request = db.one(conn, "SELECT * FROM evidence_requests WHERE run_id=:r AND request_id=:q",
                             r=ctx.workspace["run_id"], q=signal["request_id"])
            status = signals.request_view(conn, ctx.workspace, request, request["case_id"])["status"]
    lot_id = ctx.inputs["lot_id"]
    pending = (response.get("pending_counts") or {}).get(lot_id, {})
    typed = response.get("typed_values") or {}
    output = {**response, "shelf_units": pending.get("shelf_units"), "backroom_units": pending.get("backroom_units"),
              "photo_observation": " ".join(response.get("observations") or []), "request_status": status,
              "typed_counts": (typed.get("counts") or {}).get(lot_id),
              "reply_counts_state": "pending manager check" if response.get("pending_counts") else None,
              "conflict": bool(response.get("conflicts"))}
    return _result(ctx, meter, {**response, "text": json.dumps(response["suggestions"], ensure_ascii=False)},
                   saved["usage"], outputs=output, served=response["served"])


def _assistant(ctx, *, ask=False):
    from agent import jobs
    from api.app import body_hash
    precheck(ctx)
    session = _session(ctx, "source")
    run_id, case_id = ctx.workspace["run_id"], ctx.inputs["case_id"]
    with _observation(ctx) as meter:
        if ask:
            request_id, key = uuid.uuid4().hex, uuid.uuid4().hex
            body = dict(question=ctx.step["prompt"], request_id=request_id)
            job = jobs.prepare_answer(run_id, case_id, session, request_id, body["question"], key, body_hash(body))
        else:
            job = jobs.prepare_brief(run_id, case_id, session)
        events = asyncio.run(_drain(jobs.run_job(job)))
    final_type = "answer" if ask else "brief"
    response = next((event for event in reversed(events) if event.get("type") == final_type), None)
    if response is None:
        raise ValueError("Assistant service returned no terminal output")
    served = ("precomputed" if response.get("provenance") == "PRECOMPUTED" else "reuse") if "saved" in job else "live"
    calls, results = _audit(ctx, request_id=job["ctx"].get("request_id") or response["output_id"])
    outputs = dict(response)
    if not ask:
        from api import cases, selection, signals
        with db.tx() as conn:
            outputs["drafts"] = jobs.drafts(conn, ctx.workspace, session, case_id)
            ids = [results[call["result_id"]].get("draft_id") for call in calls
                   if call["name"] == "draft_evidence_request"]
            if ids:
                draft = db.one(conn, "SELECT * FROM evidence_drafts WHERE run_id=:r AND draft_id=:d", r=run_id, d=ids[0])
                case = cases.case_row(conn, run_id, case_id)
                checks = selection.plan_in_force(case, cases.active_rec(conn, case))[1]
                fact = next(row for row in checks if row["fact_id"] == draft["fact_id"])
                outputs["draft"] = {name: fact[name] for name in ("fact_id", "kind", "branch", "lot_ids")}
                outputs["draft"]["lot_ids"] = sorted(outputs["draft"]["lot_ids"])
                outputs["draft"]["part"] = signals.part_of(fact["kind"])
    return _result(ctx, meter, response, response.get("usage"), served=served,
                   outputs=outputs, tool_calls=calls, tool_results=results)


def _brief(ctx):
    return _assistant(ctx)


def _ask(ctx):
    return _assistant(ctx, ask=True)


def _summary(ctx):
    from agent import impact
    precheck(ctx)
    report = ctx.state["report"]
    session = _session(ctx, "hq" if report["kind"] == "TRANSPORT" else "destination")
    with _observation(ctx) as meter:
        events = asyncio.run(_drain(impact.summarize(ctx.workspace["run_id"], report["signal_id"], session,
                                                   revision=report["revision"])))
    response = next((event for event in reversed(events) if event.get("type") == "summary"), None)
    if response is None:
        raise ValueError("Summary service returned no terminal output")
    calls, results = _audit(ctx, request_id=response["output_id"],
                           signal_id=report["signal_id"], revision=report["revision"])
    result = _result(ctx, meter, response, response.get("usage"), served=response["served"],
                     tool_calls=calls, tool_results=results)
    return result


REAL_STEPS = dict(brief=_brief, ask=_ask, reading=_reading,
                  answer_reading=_answer_reading, summary=_summary)
