"""Authenticated assistant admission, separate from the model/stream lifetime."""
import time

from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from agent import jobs
from .app import Body, KEY_MAX, Session, _query_text, _read, _request_key, _text, app, body_hash
from .errors import ApiError


@app.get("/api/cases/{case_id}/brief")
def brief(case_id: str, s: dict = Session):
    started = time.monotonic()
    job = jobs.prepare_brief(s["run_id"], case_id, s)
    if "saved" in job:
        # Read preparation only: delivery/browser time is not observed here.
        # Retained generation timings and calls remain under usage, unchanged.
        job["saved"]["load_total_ms"] = (time.monotonic() - started) * 1000
    return StreamingResponse(jobs.sse(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
                             background=BackgroundTask(jobs.abort_unfinished, job))


@app.post("/api/cases/{case_id}/chat")
def ask(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    key = _request_key(request)
    question, request_id = _text(body, "question"), _text(body, "request_id")
    if (set(body) != {"question", "request_id"} or not question.strip() or not request_id.strip()
            or len(request_id) > KEY_MAX or "\x00" in question or "\x00" in request_id):
        raise ApiError(400, "bad_request", "Provide a question and a valid request_id.")
    job = jobs.prepare_answer(s["run_id"], case_id, s, request_id, question, key, body_hash(body))
    return StreamingResponse(jobs.sse(job), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
                             background=BackgroundTask(jobs.abort_unfinished, job))


@app.get("/api/cases/{case_id}/chat")
def history(request: Request, response: Response, case_id: str, s: dict = Session):
    response.headers["Cache-Control"] = "no-store"
    cursor = _query_text(request, "cursor")
    return _read(s, lambda conn, run, actor, session: jobs.history(conn, run, session, case_id, cursor))


@app.get("/api/cases/{case_id}/drafts")
def drafts(case_id: str, response: Response, s: dict = Session):
    response.headers["Cache-Control"] = "no-store"
    return _read(s, lambda conn, run, actor, session: jobs.drafts(conn, run, session, case_id))


@app.get("/api/cases/{case_id}/trace")
def trace(request: Request, response: Response, case_id: str, s: dict = Session):
    response.headers["Cache-Control"] = "no-store"
    output_id, request_id = _query_text(request, "output_id"), _query_text(request, "request_id")
    return _read(s, lambda conn, run, actor, session: jobs.trace(conn, run, session, case_id, output_id, request_id))


@app.get("/api/cases/{case_id}/tool-results/{result_id}")
def source(case_id: str, result_id: str, response: Response, s: dict = Session):
    response.headers["Cache-Control"] = "no-store"
    return _read(s, lambda conn, run, actor, session: jobs.source(conn, run, session, case_id, result_id))
