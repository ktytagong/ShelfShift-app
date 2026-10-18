"""Authenticated evidence readings and checked impact-summary streams."""
import asyncio
from contextlib import suppress
import json

from fastapi import Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from agent import impact, readings_job
from agent.reading import EmptyEvidence, TooLong
from . import config, files
from .app import Body, Session, _whole, _request_key, answer_form, app, body_hash, bounded_body, TOO_LARGE
from .errors import ApiError


@app.post("/api/readings")
async def report_reading(request: Request, response: Response, s: dict = Session):
    response.headers["Cache-Control"] = "no-store"
    # Keep the shared bounded parser, with the reading contract's 413 for an
    # oversized multipart body, including streams without Content-Length.
    try:
        await bounded_body(request, config.UPLOAD_MAX_BYTES + config.UPLOAD_FORM_BYTES)
    except ApiError as exc:
        if exc.code == "bad_request" and exc.message == TOO_LARGE:
            raise files.too_large() from None
        raise
    form = await answer_form(request)
    raw = await request.form()
    for field in ("case_id", "request_id"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 200 or "\x00" in value):
            raise ApiError(400, "bad_request", f"Provide a valid {field}")
    case_id = raw.get("case_id") or None
    try:
        return await readings_job.read_report(s, form["text"], form["file"], case_id=case_id)
    except (EmptyEvidence, TooLong) as exc:
        raise ApiError(400, "bad_request", str(exc)) from None


@app.post("/api/signals/{signal_id}/reading")
async def answer_reading(signal_id: str, response: Response, s: dict = Session, body: dict = Body):
    response.headers["Cache-Control"] = "no-store"
    revision = _whole(body, "revision")
    try:
        return await readings_job.read_answer(s, signal_id, revision)
    except TooLong as exc:
        raise ApiError(400, "bad_request", str(exc)) from None


@app.post("/api/signals/{signal_id}/summary")
async def impact_summary(request: Request, signal_id: str, s: dict = Session,
                         key: str = Depends(_request_key), body: dict = Body):
    revision = body.get("revision")
    if set(body) != {"revision"} or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ApiError(400, "bad_request", "Provide the report revision.")
    source = impact.request_summary(s, signal_id, revision=revision, key=key, digest=body_hash(body))
    first = await anext(source)  # authenticate/admit before sending HTTP 200

    async def frames():
        pending = None
        try:
            event = first
            while True:
                kind = event["type"]
                yield ("event: " + kind + "\ndata: " + json.dumps(
                    {key: value for key, value in event.items() if key != "type"}, default=str) + "\n\n")
                pending = asyncio.create_task(anext(source))
                while not pending.done():
                    done, _ = await asyncio.wait({pending}, timeout=5)
                    if not done:
                        yield ": ping\n\n"
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await source.aclose()

    return StreamingResponse(frames(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        background=BackgroundTask(source.aclose))
