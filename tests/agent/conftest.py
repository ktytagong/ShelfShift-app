"""Shared agent test seams, with no SDK, database or credential setup on import.

``fake_model(script, barrier=None)`` builds a real ADK BaseLlm. Each script item
is one model turn: an LlmResponse, a list of streaming responses, or an exception.
Use ``asyncio.run(drain(...))`` without an async pytest plugin. ``sse`` parses
split frames, and ``asgi_client`` drives the app's real disconnect handling.
The API fixtures below delegate lazily to the existing fixtures, rather than
giving DB-free tests a schema dependency (tests/api/test_evidence.py's pattern).
"""
from __future__ import annotations

import asyncio
import codecs
from contextlib import suppress
from dataclasses import dataclass, field
import ipaddress
import json
import os
import socket

import pytest


def pytest_collection_modifyitems(items):
    if os.environ.get("GCP", "off") == "off":
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(pytest.mark.skip(reason="Live tests need GCP=on and are user-run only"))


def _loopback(address):
    host = address[0] if isinstance(address, tuple) else address
    if host in ("localhost", b"localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ConnectionGuard:
    """Record even swallowed failures; allow local Postgres only when requested."""

    def __init__(self, monkeypatch, *, allow_local=False):
        self.attempts = []
        self.patches = []

        def patch(target, name, replacement):
            monkeypatch.setattr(target, name, replacement)
            self.patches.append((target, name, replacement))

        def network(name, original, address_of):
            def checked(*args, **kwargs):
                if allow_local and _loopback(address_of(args, kwargs)):
                    return original(*args, **kwargs)
                return self.deny(name)(*args, **kwargs)
            return checked

        for name in ("connect", "connect_ex"):
            patch(socket.socket, name, network(f"socket.{name}", getattr(socket.socket, name),
                                               lambda args, kwargs: args[1]))
        patch(socket.socket, "sendto", network("socket.sendto", socket.socket.sendto,
                                               lambda args, kwargs: args[-1]))
        for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            patch(socket, name, network(f"socket.{name}", getattr(socket, name),
                                       lambda args, kwargs: args[0] if args else kwargs.get("host")))

    def deny(self, name):
        def fail(*args, **kwargs):
            self.attempts.append(name)
            raise AssertionError(f"Offline agent harness forbids {name}")
        return fail

    def assert_installed(self):
        assert self.patches
        for target, name, replacement in self.patches:
            assert getattr(target, name) is replacement, name


@pytest.fixture(autouse=True)
def _previews_out_of_reach(monkeypatch):
    """Phase 10's preview tests preview many times a minute, so the release's `preview` cap (api/settings.json; 07-20
    review) is out of reach here, as in tests/api/conftest.py; tests/api/test_signals tests the cap itself."""
    from api import config
    monkeypatch.setitem(config.WORKSPACE_CAPS, "preview", (10**6, "minute"))
    monkeypatch.setitem(config.GLOBAL_CAPS, "preview", 10**6)


@pytest.fixture(autouse=True)
def no_paid_calls(monkeypatch):
    """GCP-off tests may use local DB/HTTP, but cannot initialize a paid client."""
    if os.environ.get("GCP", "off") != "off":
        yield
        return
    guard = ConnectionGuard(monkeypatch, allow_local=True)
    # The socket guard precedes these imports, and SDK clients are never built.
    import google.auth
    import google.auth._default
    from google import genai
    monkeypatch.setattr(google.auth, "default", guard.deny("google.auth.default"))
    monkeypatch.setattr(google.auth._default, "default", guard.deny("google.auth._default.default"))
    monkeypatch.setattr(genai.Client, "__init__", guard.deny("google.genai.Client"))
    yield guard
    assert not guard.attempts, f"Paid/remote calls attempted: {guard.attempts}"


@pytest.fixture
def no_network(monkeypatch):
    """Stricter opt-in sentinel: no local sockets, credentials or DB engines."""
    guard = ConnectionGuard(monkeypatch)
    import google.auth
    import google.auth._default
    import sqlalchemy as sa
    import sqlalchemy.engine.create
    import pg8000
    import pg8000.dbapi
    for target, name in ((google.auth, "default"), (google.auth._default, "default"),
                         (sa, "create_engine"), (sa.engine, "create_engine"),
                         (sa.engine.create, "create_engine"),
                         (sa.Engine, "connect"), (sa.Engine, "raw_connection"),
                         (pg8000, "connect"), (pg8000.dbapi, "connect")):
        replacement = guard.deny(f"{target.__name__}.{name}")
        monkeypatch.setattr(target, name, replacement)
        guard.patches.append((target, name, replacement))
    guard.assert_installed()
    yield guard
    assert not guard.attempts, f"Network/credential/DB attempts: {guard.attempts}"


@pytest.fixture
def fake_model(no_paid_calls):
    # Deferred until the test requests the fixture, so sentinels can cover even
    # ADK imports and BaseLlm construction. No Gemini fallback or SDK Client.
    from google.adk.models import BaseLlm
    from pydantic import Field

    class FakeModel(BaseLlm):
        model: str = "offline-scripted-model"
        script: list = Field(default_factory=list)
        requests: list = Field(default_factory=list)
        barrier: object | None = None

        async def generate_content_async(self, llm_request, stream=False):
            self.requests.append(llm_request.model_copy(deep=True))
            if not self.script:
                raise AssertionError("Fake model script exhausted: unexpected model call")
            turn = self.script.pop(0)
            if self.barrier is not None:
                await self.barrier.wait()
            if isinstance(turn, BaseException):
                raise turn
            for response in turn if isinstance(turn, (list, tuple)) else [turn]:
                yield response.model_copy(deep=True)

    def build(script, *, barrier=None):
        return FakeModel(script=list(script), barrier=barrier)
    return build


async def drain(events):
    """Consume the real async iterator, propagating errors/cancellation."""
    return [event async for event in events]


class SSEParser:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.buffer = ""

    def feed(self, chunk):
        self.buffer += self.decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        events = []
        while "\n\n" in self.buffer.replace("\r\n", "\n"):
            self.buffer = self.buffer.replace("\r\n", "\n")
            frame, self.buffer = self.buffer.split("\n\n", 1)
            event, data = "message", []
            for line in frame.split("\n"):
                if not line or line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if key == "event":
                    event = value
                elif key == "data":
                    data.append(value)
            if data:
                payload = "\n".join(data)
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    pass
                events.append({"event": event, "data": payload})
        return events

    def finish(self):
        # An interrupted/incomplete frame never becomes a final answer.
        return self.feed(self.decoder.decode(b"", final=True))


def sse(chunks):
    parser = SSEParser()
    if isinstance(chunks, (bytes, str)):
        chunks = [chunks]
    return [event for chunk in chunks for event in parser.feed(chunk)] + parser.finish()


@dataclass
class ConcurrencyBarrier:
    parties: int = 1
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    arrivals: int = 0

    async def wait(self):
        self.arrivals += 1
        if self.arrivals >= self.parties:
            self.entered.set()
        await self.release.wait()


@pytest.fixture
def barrier():
    return ConcurrencyBarrier


class ASGIStream:
    """In-process streaming client with an actual http.disconnect receive event.

    Unlike buffered httpx ASGITransport, it returns frames before completion.
    ASGI 2.3 matches uvicorn's disconnect listener path. No lifespan is run.
    """

    def __init__(self, app, path="/", *, method="GET", body=b"", headers=(), timeout=2):
        self.app, self.body, self.timeout = app, body, timeout
        self.scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                      "http_version": "1.1", "method": method, "scheme": "http", "path": path,
                      "raw_path": path.encode(), "query_string": b"", "headers": list(headers),
                      "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.messages = []
        self.task = None

    async def __aenter__(self):
        self.incoming.put_nowait({"type": "http.request", "body": self.body, "more_body": False})
        self.task = asyncio.create_task(self.app(self.scope, self.incoming.get, self._send))
        return self

    async def _send(self, message):
        self.messages.append(dict(message))
        await self.outgoing.put(message)

    async def next_message(self):
        return await asyncio.wait_for(self.outgoing.get(), self.timeout)

    async def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(asyncio.shield(self.task), self.timeout)

    async def __aexit__(self, *exc):
        try:
            if not self.task.done():
                await self.disconnect()
        finally:
            if not self.task.done():
                self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


@pytest.fixture
def asgi_client():
    return ASGIStream


# Explicit reuse, requested only by DB-backed agent tests. Importing the sibling
# conftest as a global pytest plugin would leak its setup into DB-free tests.
@pytest.fixture(scope="session")
def schema():
    from tests.api.conftest import schema as api_schema
    return api_schema.__wrapped__()


@pytest.fixture(scope="session")
def template(schema):
    from tests.api.conftest import template as api_template
    return api_template.__wrapped__(schema)


@pytest.fixture
def run(template):
    from tests.api.conftest import run as api_run
    return api_run.__wrapped__(template)


@pytest.fixture
def client(template):
    from tests.api.conftest import client as api_client
    return api_client.__wrapped__(template)


@pytest.fixture
def store(template):
    from tests.api.conftest import store as api_store
    return api_store.__wrapped__(template)
