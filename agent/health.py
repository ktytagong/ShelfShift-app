"""Readiness access checks (11-CONTEXT D-01, D-13, D-18, D-19).

Model metadata and a one-object uploads listing show reachability and configuration only;
they do not prove inference, uploads or downloads work. Release smoke and the deployed
journey verify those separately. The running app never checks the data bucket.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait

from api import config

TTL_S = 60.0
TOTAL_DEADLINE_S = 5.0
CLIENT_TIMEOUT_S = 3.5
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="readyz-probe")
_cache = {"at": -TTL_S, "payload": None}
_in_flight: dict[str, Future] = {}


def _payload(vertex: str, uploads: str) -> dict:
    return {"vertex_ai": vertex, "cloud_storage": uploads, "data_bucket": "not_checked",
            "uploads_bucket": uploads, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _probe_vertex() -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "disabled"
    kwargs = {"vertexai": True, "location": config.VERTEX_LOCATION,
              "http_options": types.HttpOptions(timeout=int(CLIENT_TIMEOUT_S * 1000))}
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if project:
        kwargs["project"] = project
    client = genai.Client(**kwargs)
    try:
        client.models.get(model=config.GEMINI_MODEL)
    finally:
        if callable(getattr(client, "close", None)):
            client.close()
    return "ok"


def _probe_bucket(name: str) -> str:
    if not name:
        return "disabled"
    try:
        from google.cloud import storage
    except ImportError:
        return "disabled"
    client = storage.Client()
    try:
        next(iter(client.list_blobs(name, max_results=1, timeout=CLIENT_TIMEOUT_S, retry=None)), None)
    finally:
        if callable(getattr(client, "close", None)):
            client.close()
    return "ok"


def _submit_if_idle(key: str, fn, *args):
    previous = _in_flight.get(key)
    if previous is not None and not previous.done():
        return None
    future = _pool.submit(fn, *args)
    _in_flight[key] = future
    return future


def _refresh(uploads_bucket: str) -> dict:
    from api.ready import log_dependency_error
    probes = {"vertex_ai": _submit_if_idle("vertex_ai", _probe_vertex),
              "uploads_bucket": _submit_if_idle("uploads_bucket", _probe_bucket, uploads_bucket)}
    active = [f for f in probes.values() if f is not None]
    done, _ = wait(active, timeout=TOTAL_DEADLINE_S) if active else (set(), set())
    results = {}
    for key, fut in probes.items():
        event = None
        if fut is None:
            status, event = "degraded", "probe_busy"
        elif fut not in done:
            status, event = "degraded", "probe_timeout"
        else:
            try:
                status = fut.result()
            except Exception:
                status, event = "degraded", "probe_failed"
        results[key] = status
        if event:
            log_dependency_error("vertex_ai" if key == "vertex_ai" else "cloud_storage", event)
    return _payload(results["vertex_ai"], results["uploads_bucket"])


def check_ai_and_storage(gcp_enabled: bool, uploads_bucket: str) -> dict:
    if not gcp_enabled:
        return _payload("disabled", "disabled")
    if _cache["payload"] is not None and time.monotonic() - _cache["at"] < TTL_S:
        return dict(_cache["payload"])
    with _lock:
        if _cache["payload"] is not None and time.monotonic() - _cache["at"] < TTL_S:
            return dict(_cache["payload"])
        fresh = _refresh(uploads_bucket)
        _cache.update(at=time.monotonic(), payload=fresh)
        return dict(fresh)
