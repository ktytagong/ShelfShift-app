"""Bounded database liveness and outage responses (11-CONTEXT D-01..D-03).

The dated contract section '## Judge-ready release (11)' defines these bodies.
Only the dedicated probe engine is timed out; long application transactions are untouched.
"""
from __future__ import annotations

import logging
import importlib
import json
import sys
from datetime import datetime, timezone
from typing import Literal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from fastapi import Request
from fastapi.responses import JSONResponse

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from pg8000.exceptions import InterfaceError as DriverInterfaceError

from . import config, db
from .errors import DB_BUSY

UNAVAILABLE = "ShelfShift is temporarily unavailable."
INTERNAL = "Something went wrong on our side."
RETRYABLE = {"40001", "40P01", "55P03"}
UNAVAILABLE_STATES = {"57P01", "57P02", "57P03"}
DB_DEADLINE_S = 3.5
DB_CACHE_S = 2.0
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-alive")
_lock = threading.Lock()
_state: dict = {"at": -1e9, "ok": None, "fut": None}


class Paused(Exception):
    """The database liveness probe could not reach Postgres."""


def paused_body() -> dict:
    return {"ok": False, "db": "unreachable", "error": "paused", "message": UNAVAILABLE,
            "contact": config.CONTACT_EMAIL}


def _select1() -> bool:
    with db.probe_engine().connect() as conn:
        db.one(conn, "SELECT 1 AS ok")
    return True


def db_alive() -> bool:
    """Answer within 3.5 s; reuse a still-running probe and cache answers for strictly less than 2 s."""
    with _lock:
        if _state["ok"] is not None and time.monotonic() - _state["at"] < DB_CACHE_S:
            return _state["ok"]
        fut = _state["fut"]
        if fut is None or fut.done():
            fut = _state["fut"] = _pool.submit(_select1)
    try:
        done, _ = wait([fut], timeout=DB_DEADLINE_S)
        ok = bool(fut in done and fut.result())
    except Exception:
        ok = False
    with _lock:
        _state.update(at=time.monotonic(), ok=ok)
    return ok


class ReadyOk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True]
    db: Literal["ok"]
    vertex_ai: Literal["ok", "degraded", "disabled"]
    cloud_storage: Literal["ok", "degraded", "disabled"]
    data_bucket: Literal["not_checked"]
    uploads_bucket: Literal["ok", "degraded", "disabled"]
    model_id: str
    checked_at: str


_DEPENDENCY_MESSAGES = {
    "health_module_missing": "Dependency readiness is unavailable in this build.",
    "probe_timeout": "A dependency access check exceeded its deadline.",
    "probe_failed": "A dependency access check failed.",
    "probe_busy": "A previous dependency access check is still running.",
    "upload_failed": "An upload could not be completed.",
    "download_failed": "A download could not be completed.",
    "call_failed": "A dependency request could not be completed.",
    "request_failed": "A dependency request could not be completed.",
    "dependency_failed": "A dependency operation could not be completed.",
}


def log_dependency_error(component: str, event: str) -> None:
    """One privacy-safe ERROR writer for readiness, uploads and model call sites (D-13)."""
    if component not in {"vertex_ai", "cloud_storage", "uploads", "readiness"}:
        component = "readiness"
    if event not in _DEPENDENCY_MESSAGES:
        event = "dependency_failed"
    print(json.dumps({"severity": "ERROR", "component": component, "event": event,
                      "message": _DEPENDENCY_MESSAGES[event]}), file=sys.stderr, flush=True)


def readyz():
    if not db_alive():
        return JSONResponse(paused_body(), status_code=503)
    status = {"vertex_ai": "disabled", "uploads_bucket": "disabled",
              "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if config.GCP:
        try:
            health = importlib.import_module("agent.health")
        except ModuleNotFoundError:
            status.update(vertex_ai="degraded", uploads_bucket="degraded")
            log_dependency_error("readiness", "health_module_missing")
        else:
            status = health.check_ai_and_storage(True, config.UPLOADS_BUCKET)
    # These are access checks only. They do not prove inference or uploads/downloads work.
    # D-19: the running app never checks the data bucket; storage follows uploads only.
    return ReadyOk(ok=True, db="ok", vertex_ai=status["vertex_ai"],
                   cloud_storage=status["uploads_bucket"], data_bucket="not_checked",
                   uploads_bucket=status["uploads_bucket"], model_id=config.GEMINI_MODEL,
                   checked_at=status["checked_at"]).model_dump()


def session_or_paused(request: Request) -> dict:
    if not request.cookies.get("ss_session") and not db_alive():
        raise Paused()
    from .app import current_session
    return current_session(request)


def classify(exc: Exception) -> str:
    """Classify by SQLSTATE and connectivity, never by a driver's message (11-CONTEXT D-02)."""
    if isinstance(exc, sa.exc.TimeoutError):
        return "busy"
    state = db.sql_code(exc)
    if state in RETRYABLE:
        return "retry"
    if (state and (state.startswith("08") or state in UNAVAILABLE_STATES)
            or isinstance(exc, (sa.exc.InterfaceError, DriverInterfaceError, ConnectionError, TimeoutError,
                                socket.gaierror))
            or isinstance(exc, sa.exc.DBAPIError) and exc.connection_invalidated):
        return "paused"
    return "internal"


def install(app) -> None:
    async def failure(_, exc: Exception):
        kind = classify(exc)
        if kind in ("busy", "retry"):
            return JSONResponse({"error": "db_busy", "message": DB_BUSY}, status_code=503)
        if kind == "paused":
            return JSONResponse(paused_body(), status_code=503)
        logging.getLogger("shelfshift.api").exception("Unhandled request failure", exc_info=exc)
        return JSONResponse({"error": "internal", "message": INTERNAL}, status_code=500)
    for cls in (sa.exc.DBAPIError, OSError, Exception):
        app.add_exception_handler(cls, failure)


def startup_problem() -> str | None:
    if config.GCP and not config.UPLOADS_BUCKET.strip():
        return "UPLOADS_BUCKET is not set; refusing to start with GCP on"
    return None
