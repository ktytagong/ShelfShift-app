"""DB-free tests of outage behaviour, 11-CONTEXT D-01..D-03; never drops API schemas."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from api import config, db, ready
from api.app import app


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(ready, "_state", {"at": -1e9, "ok": None, "fut": None})
    monkeypatch.setattr(config, "CONTACT_EMAIL", "")


def unavailable():
    raise ConnectionRefusedError("private connection detail")


def test_readyz_is_the_paused_body_when_postgres_is_unreachable(monkeypatch):
    monkeypatch.setattr(ready, "_select1", unavailable)
    client = TestClient(app)
    for contact in ("", "help@example.org"):
        monkeypatch.setattr(config, "CONTACT_EMAIL", contact)
        r = client.get("/api/readyz")
        assert r.status_code == 503
        assert r.json() == {"ok": False, "db": "unreachable", "error": "paused",
                            "message": "ShelfShift is temporarily unavailable.", "contact": contact}


def test_signed_out_session_answers_paused_then_locked(monkeypatch):
    monkeypatch.setattr(ready, "_select1", unavailable)
    client = TestClient(app)
    r = client.get("/api/session")
    assert r.status_code == 503 and r.json() == ready.paused_body()
    ready._state.update(at=-1e9, ok=None, fut=None)
    monkeypatch.setattr(ready, "_select1", lambda: True)
    r = client.get("/api/session")
    assert r.status_code == 401
    assert r.json() == {"error": "locked", "message": "Sign in to continue."}


def test_postgres_check_is_bounded_and_single_flight(monkeypatch):
    event = threading.Event()
    calls = []
    def blocked():
        calls.append(1)
        event.wait(10)
        return True
    monkeypatch.setattr(ready, "_select1", blocked)
    start = time.monotonic()
    try:
        assert ready.db_alive() is False
        assert time.monotonic() - start < 3.7
        assert ready.db_alive() is False
        assert calls == [1]
    finally:
        event.set()
        ready._state["fut"].result(timeout=2)


def test_result_cache_boundary(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ready, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    select = Mock(return_value=True)
    monkeypatch.setattr(ready, "_select1", select)
    assert ready.db_alive() is True
    clock[0] = 11.99
    assert ready.db_alive() is True and select.call_count == 1
    clock[0] = 12.0
    assert ready.db_alive() is True and select.call_count == 2


def test_probe_engine_does_not_change_main_engine_settings(monkeypatch):
    made = []
    monkeypatch.setattr(db, "_probe_engine", None)
    monkeypatch.setattr(config, "CLOUD_SQL_INSTANCE", "")
    monkeypatch.setattr(sa, "create_engine", lambda *args, **kwargs: made.append((args, kwargs)) or object())
    first = db.probe_engine()
    assert db.probe_engine() is first
    assert made[0][1] == {"poolclass": sa.pool.NullPool, "connect_args": {"timeout": 3}}
    assert len(made) == 1


def sql_failure(code):
    return sa.exc.DBAPIError(None, None, Exception({"C": code}))


@pytest.mark.parametrize("exc, expected", [
    (sa.exc.TimeoutError("private-marker"), "busy"),
    *[(sql_failure(c), "retry") for c in ("40001", "40P01", "55P03")],
    *[(sql_failure(c), "paused") for c in ("08006", "08001", "57P01", "57P02", "57P03")],
    (sa.exc.InterfaceError(None, None, Exception("private-marker")), "paused"),
    (sa.exc.DBAPIError(None, None, Exception("private-marker"), connection_invalidated=True), "paused"),
    (ConnectionRefusedError("private-marker"), "paused"),
    (TimeoutError("private-marker"), "paused"),
    *[(sql_failure(c), "internal") for c in ("23505", "57014")],
    (FileNotFoundError("private-marker"), "internal"),
    (PermissionError("private-marker"), "internal"),
    (ValueError("private-marker"), "internal"),
])
def test_classify_table(exc, expected):
    assert ready.classify(exc) == expected


def test_handlers_answer_by_class(monkeypatch):
    import asyncio
    import json
    from api.errors import DB_BUSY
    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/api/healthz", "headers": []})
    for exc, expected, status in [
        (sa.exc.TimeoutError("private-marker"), {"error": "db_busy", "message": DB_BUSY}, 503),
        (sql_failure("40P01"), {"error": "db_busy", "message": DB_BUSY}, 503),
        (ConnectionRefusedError("private-marker"), ready.paused_body(), 503),
        (ValueError("private-marker"), {"error": "internal", "message": "Something went wrong on our side."}, 500),
    ]:
        r = asyncio.run(app.exception_handlers[Exception](request, exc))
        assert (r.status_code, json.loads(r.body)) == (status, expected)
        assert b"private-marker" not in r.body
    def fail():
        raise ValueError("private-marker")
    monkeypatch.setattr(ready, "readyz", fail)
    r = TestClient(app, raise_server_exceptions=False).get("/api/readyz")
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "message": "Something went wrong on our side."}


def test_connector_failure_is_wrapped(monkeypatch):
    from google.cloud.sql import connector
    failure = RuntimeError("private-marker")
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(connector, "Connector", lambda **kwargs: SimpleNamespace(connect=fail))
    monkeypatch.setattr(config, "CLOUD_SQL_INSTANCE", "configured-instance")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_probe_engine", None)
    for engine in (db.engine(), db.probe_engine()):
        with pytest.raises(db.Unreachable) as caught:
            engine.raw_connection()
        assert str(caught.value) == "database unreachable"
        assert caught.value.__cause__ is failure


def test_unreachable_and_raw_interface_errors_are_classified():
    from pg8000.exceptions import InterfaceError
    assert ready.classify(db.Unreachable("private-marker")) == "paused"
    assert ready.classify(InterfaceError("private-marker")) == "paused"


def test_new_server_sentences_are_clean():
    from tests.api.test_server_words import clean
    clean(ready.UNAVAILABLE)
    clean(ready.INTERNAL)


def test_gcp_without_uploads_bucket_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "DEMO_PASSCODE", "unit-test-store")
    monkeypatch.setattr(config, "HQ_PASSCODE", "unit-test-head-office")
    monkeypatch.setattr(config, "ENGINE_WORKER", False)
    monkeypatch.setattr(config, "GCP", True)
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    with pytest.raises(RuntimeError, match="^UPLOADS_BUCKET is not set; refusing to start with GCP on$"):
        with TestClient(app):
            pass
    monkeypatch.setattr(config, "GCP", False)
    with TestClient(app) as client:
        assert client.get("/api/healthz").json() == {"ok": True}
