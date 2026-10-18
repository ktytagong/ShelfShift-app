"""One fresh ADK Runner per request; emits progress and final candidate text.

The caller checks candidate numbers before storing or publishing any prose.
Only server-supplied function tools exist in the agent. No client is built on
import, on readiness, or by the default GCP-off path.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing, suppress
from contextvars import ContextVar
import time
import uuid

import httpx
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import config, prompts


class LimitReached(RuntimeError):
    """A request has exhausted a server-owned generation bound."""


class ModelUnavailable(RuntimeError):
    """Generation is disabled or has no usable final text."""


_attempt_meter = ContextVar("assistant_attempt_meter", default=None)


def record_provider_attempt():
    """Observe a transport invocation, including the SDK's bounded retry."""
    meter = _attempt_meter.get()
    if meter is None:
        return
    stats = meter["stats"]
    if meter["last_turn"] == stats["model_calls"] and (stats["retries"] or 0) >= 1:
        raise LimitReached("Provider retry limit: one per request")
    stats["provider_attempts"] = (stats["provider_attempts"] or 0) + 1
    stats["retries"] = (stats["retries"] or 0) + int(meter["last_turn"] == stats["model_calls"])
    meter["last_turn"] = stats["model_calls"]


class AttemptTransport(httpx.AsyncBaseTransport):
    """Public SDK transport seam; construction opens no client or connection.

    Supplying an explicit transport selects the SDK's httpx backend, so a retry
    is measured at the same boundary with either optional HTTP backend installed.
    """
    def __init__(self, inner=None):
        self.inner = inner

    async def handle_async_request(self, request):
        record_provider_attempt()
        if self.inner is None:
            self.inner = httpx.AsyncHTTPTransport()
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        if self.inner is not None:
            await self.inner.aclose()


def build_model(model_id: str, location: str, project: str | None) -> Gemini:
    """Configure the approved SDK without accessing its lazy api_client."""
    kwargs = {"enterprise": True, "location": location}
    if project:
        kwargs["project"] = project
    retry = types.HttpRetryOptions(attempts=2, initial_delay=.5, max_delay=1., jitter=.2, http_status_codes=[429])
    kwargs["http_options"] = types.HttpOptions(retry_options=retry,
        async_client_args={"transport": AttemptTransport()})
    return Gemini(model=model_id, client_kwargs=kwargs, retry_options=retry)


def model_id() -> str:
    return config.model_id()


def llm():
    """The single injection seam; production generation requires explicit GCP on."""
    if config.api_config.env("GCP", "off") != "on":
        raise ModelUnavailable("Gemini generation is disabled")
    settings = config.load()
    return build_model(settings.model_id, settings.location, settings.project)


async def run(instruction, parts, *, tools=(), ctx=None, max_calls=8, seconds=40, history=(), usage=None):
    """Stream tool progress, then one complete non-thought candidate.

    The producer owns the deadline, including SDK retries and tool execution;
    yielding to a slow consumer cannot turn a timeout into a disconnect. A caller
    may retain the supplied usage dict even when the iterator raises or closes.
    max_calls/seconds are server/test arguments, never model or HTTP inputs.
    """
    settings = config.load()
    if not callable(instruction):
        raise TypeError("Instructions must be callable so literal metric braces are preserved")
    config._positive("max_calls", max_calls)
    config._positive("seconds", seconds, integer=False, ceiling=40)
    max_calls = settings.max_tool_calls if max_calls == 8 else max_calls
    seconds = min(seconds, settings.seconds)
    stats = usage if usage is not None else {}
    stats.update(model_id=settings.model_id, location=settings.location, model_calls=0, tool_calls=0,
                 input_tokens=None, output_tokens=None, thinking_tokens=None, total_tokens=None,
                 tool_use_prompt_tokens=None, provider_attempts=None, retries=None, first_text_ms=None, total_ms=0)
    token_fields = {"input_tokens": "prompt_token_count", "output_tokens": "candidates_token_count",
                    "thinking_tokens": "thoughts_token_count", "total_tokens": "total_token_count",
                    "tool_use_prompt_tokens": "tool_use_prompt_token_count"}
    turns = []
    token_floors = []
    q = asyncio.Queue()
    started = time.monotonic()
    active_tools = {}
    seq = 0
    stop = object()
    callback_failure = None
    active_model = None

    def refuse(message):
        # ADK's workflow may convert a callback exception to an error Event.
        # Preserve our typed refusal so that the producer still exposes it.
        nonlocal callback_failure
        callback_failure = LimitReached(message)
        raise callback_failure

    def emit(event):
        nonlocal seq
        q.put_nowait({**event, "seq": seq})
        seq += 1

    def known_tokens():
        # A missing provider metric stays null in the audit. Known components
        # still give a lower bound for enforcing the budget without inventing
        # a total or double-counting tool-use tokens inside prompt tokens.
        return sum(token_floors)

    def before_model(callback_context, llm_request):
        if stats["model_calls"] >= settings.max_llm_calls:
            refuse(f"Request limit: {settings.max_llm_calls} model calls")
        remaining = settings.max_request_tokens - known_tokens()
        if remaining <= 0:
            refuse(f"Request token limit: {settings.max_request_tokens}")
        # Apply policy on the actual outgoing request, after ADK preprocessing.
        # Neither SDK defaults nor environment sampling knobs may change it.
        llm_request.config.thinking_config = types.ThinkingConfig(thinking_level="LOW")
        llm_request.config.max_output_tokens = min(settings.max_output_tokens, remaining)
        for field in ("temperature", "top_p", "top_k", "response_schema", "response_json_schema"):
            setattr(llm_request.config, field, None)
        if stats["retries"]:
            # The SDK policy is per HTTP call; the application budget is per
            # admitted request, even when ADK follows a tool with another call.
            llm_request.config.http_options = types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1))
        stats["model_calls"] += 1
        # The injection seam's local BaseLlm is one scripted invocation, with
        # no SDK underneath. An explicitly metered fake can exercise retries.
        if not isinstance(active_model, Gemini) and not getattr(active_model, "observes_provider_attempts", False):
            record_provider_attempt()
        turns.append(dict.fromkeys(token_fields))
        token_floors.append(0)

    def after_model(callback_context, llm_response):
        metadata = llm_response.usage_metadata
        reported = dict.fromkeys(token_fields)
        if metadata is not None:
            for key, field in token_fields.items():
                value = getattr(metadata, field, None)
                if value is not None:
                    if value < 0:
                        refuse("Provider returned invalid token usage")
                    reported[key] = value
        token_floors[-1] = max(token_floors[-1], reported["total_tokens"] or 0,
                              sum(reported[key] or 0 for key in ("input_tokens", "output_tokens", "thinking_tokens")))
        if llm_response.partial:
            turns[-1].update({key: value for key, value in reported.items() if value is not None})
        else:
            # Partial counters remain a conservative budget floor. They do not
            # establish a complete total when the final response omits usage.
            turns[-1] = reported
        for key in token_fields:
            values = [turn[key] for turn in turns]
            stats[key] = sum(values) if all(v is not None for v in values) else None
        if known_tokens() > settings.max_request_tokens:
            refuse(f"Request token limit: {settings.max_request_tokens}")
        # Unknown function names have no authority. Refuse before ADK can feed
        # a tool-not-found response into another paid turn; retain this turn's
        # observed usage first, including when the provider omits token fields.
        permitted = {tool.__name__ for tool in tools}
        if any(part.function_call and part.function_call.name not in permitted
               for part in ((llm_response.content.parts or []) if llm_response.content else [])):
            raise ModelUnavailable("Gemini requested an unavailable tool")


    def on_model_error(callback_context, llm_request, error):
        # ADK's workflow wrapper can turn a later failed turn into an error
        # Event, dropping the Python exception type. Preserve our safe terminal
        # category before that conversion; never recover with another call.
        nonlocal callback_failure
        if isinstance(error, (TimeoutError, LimitReached)):
            callback_failure = error


    def before_tool(tool, args, tool_context):
        if stats["tool_calls"] >= max_calls:
            refuse(f"Request limit: {max_calls} tool calls")
        stats["tool_calls"] += 1
        active_tools[tool_context.function_call_id] = time.monotonic()
        emit({"type": "tool", "tool": tool.name, "state": "Working",
              "call_id": tool_context.function_call_id})

    def after_tool(tool, args, tool_context, tool_response):
        began = active_tools.pop(tool_context.function_call_id)
        result_id = tool_response.get("result_id") if isinstance(tool_response, dict) else None
        failed = isinstance(tool_response, dict) and "error" in tool_response
        emit({"type": "tool_result", "tool": tool.name, "result_id": result_id,
              "state": "Failed" if failed else "Complete", "call_id": tool_context.function_call_id,
              "duration_ms": (time.monotonic() - began) * 1000})

    async def produce():
        nonlocal active_model
        from .tools import begin_request, end_request
        runtime_token = begin_request(max_calls)
        meter_token = _attempt_meter.set({"stats": stats, "last_turn": None})
        try:
            async with asyncio.timeout(seconds):
                active_model = llm()
                agent = LlmAgent(name="shelfshift", model=active_model, instruction=instruction, tools=list(tools),
                    generate_content_config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                        max_output_tokens=settings.max_output_tokens),
                    before_model_callback=before_model, after_model_callback=after_model,
                    on_model_error_callback=on_model_error,
                    before_tool_callback=before_tool, after_tool_callback=after_tool)
                sessions = InMemorySessionService()
                runner = Runner(app_name="shelfshift", agent=agent, session_service=sessions, auto_create_session=True)
                message_parts = list(parts)
                if history:
                    message_parts.insert(0, types.Part(text=prompts.quoted_history(history)))
                stream = runner.run_async(user_id="shelfshift", session_id=uuid.uuid4().hex,
                    new_message=types.Content(role="user", parts=message_parts),
                    state_delta={"ctx": dict(ctx or {})}, run_config=RunConfig(max_llm_calls=settings.max_llm_calls))
                final_text = None
                async with runner, aclosing(stream):
                    async for event in stream:
                        content = event.content
                        text = "".join(part.text for part in (content.parts or []) if part.text and not part.thought) if content else ""
                        if text and stats["first_text_ms"] is None:
                            stats["first_text_ms"] = (time.monotonic() - started) * 1000
                        if event.is_final_response() and not event.partial:
                            if event.error_code:
                                if callback_failure is not None:
                                    raise callback_failure
                                raise ModelUnavailable("Gemini returned no usable response")
                            final_text = text
                if not final_text or not final_text.strip():
                    raise ModelUnavailable("Gemini returned empty final text")
                stats["total_ms"] = (time.monotonic() - started) * 1000
                emit({"type": "final", "text": final_text, "model_id": settings.model_id,
                      "location": settings.location, "usage": dict(stats)})
        except Exception as exc:
            q.put_nowait(exc)
        finally:
            end_request(runtime_token)
            _attempt_meter.reset(meter_token)
            stats["total_ms"] = (time.monotonic() - started) * 1000
            q.put_nowait(stop)

    producer = asyncio.create_task(produce())
    try:
        while (event := await q.get()) is not stop:
            if isinstance(event, Exception):
                raise event
            yield event
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
