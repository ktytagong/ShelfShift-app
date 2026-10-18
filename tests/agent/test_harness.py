"""Executable harness contracts, independent of Postgres and cloud credentials."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import textwrap

import pytest

from tests.agent.conftest import drain, sse


def test_fake_model_zero_network(no_network, request):
    """The shared fake must work inside ADK's real tool-calling/session loop."""
    no_network.assert_installed()
    assert not {"schema", "template", "run", "client"}.intersection(request.fixturenames)
    # Request the fixture only AFTER the failing connection/credential/DB guards.
    fake_model = request.getfixturevalue("fake_model")
    from google.adk.agents import LlmAgent
    from google.adk.agents.run_config import RunConfig
    from google.adk.models import BaseLlm, LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    async def exercise():
        tool_calls = []

        async def get_fact_checks() -> dict:
            """Read a deterministic local check; no business action is exposed."""
            tool_calls.append("get_fact_checks")
            return {"result_id": "local-check", "required": True, "authority": "human"}

        usage = types.GenerateContentResponseUsageMetadata
        fake = fake_model([
            LlmResponse(content=types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(name="get_fact_checks", args={}))]),
                usage_metadata=usage(prompt_token_count=11, candidates_token_count=3,
                                     thoughts_token_count=2, total_token_count=16)),
            LlmResponse(content=types.Content(role="model", parts=[
                types.Part(text="Private thought", thought=True),
                types.Part(text="A manager must review and send any evidence request.")]),
                usage_metadata=usage(prompt_token_count=17, candidates_token_count=7,
                                     thoughts_token_count=1, total_token_count=25)),
        ])
        assert isinstance(fake, BaseLlm)
        agent = LlmAgent(name="offline_harness", model=fake, tools=[get_fact_checks],
                         instruction=lambda ctx: "Read checks. A human owns every business action.")
        assert [tool.__name__ for tool in agent.tools] == ["get_fact_checks"]
        async with Runner(app_name="offline_harness", agent=agent,
                          session_service=InMemorySessionService(), auto_create_session=True) as runner:
            events = await drain(runner.run_async(user_id="local-user", session_id="local-session",
                new_message=types.Content(role="user", parts=[types.Part(text="What needs checking?")]),
                run_config=RunConfig(max_llm_calls=3)))
        assert tool_calls == ["get_fact_checks"]
        assert len(fake.requests) == 2 and not fake.script
        assert set(fake.requests[0].tools_dict) == {"get_fact_checks"}
        replies = [part.function_response for content in fake.requests[1].contents
                   for part in content.parts or [] if part.function_response]
        assert len(replies) == 1
        assert replies[0].name == "get_fact_checks"
        assert replies[0].response == {"result_id": "local-check", "required": True, "authority": "human"}
        calls = [p.function_call.name for ev in events for p in (ev.content.parts if ev.content else []) or []
                 if p.function_call]
        responses = [p.function_response.name for ev in events for p in (ev.content.parts if ev.content else []) or []
                     if p.function_response]
        assert calls == responses == ["get_fact_checks"]
        final = "".join(p.text for ev in events if ev.is_final_response()
                        for p in (ev.content.parts if ev.content else []) or [] if p.text and not p.thought)
        assert final == "A manager must review and send any evidence request."
        metadata = [ev.usage_metadata for ev in events if ev.usage_metadata]
        assert len(metadata) == 2
        assert [sum(getattr(u, field) for u in metadata) for field in
                ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count")] == [28, 10, 3, 41]

    asyncio.run(exercise())
    no_network.assert_installed()
    assert no_network.attempts == []


# Loaded by the CHILD interpreter before pytest, plugins, or fixture imports.
CHILD_GUARDS = """
import socket
ATTEMPTS = []
READY = False
def deny(name):
    def fail(*args, **kwargs):
        ATTEMPTS.append(name)
        raise AssertionError('Discovery must be connection-free: ' + name)
    return fail
for name in ('connect', 'connect_ex', 'sendto'):
    setattr(socket.socket, name, deny('socket.' + name))
for name in ('create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex'):
    setattr(socket, name, deny('socket.' + name))
import google.auth
import google.auth._default
google.auth.default = deny('google.auth.default')
google.auth._default.default = deny('google.auth._default.default')
import sqlalchemy as sa
import sqlalchemy.engine.create
import pg8000
import pg8000.dbapi
for target, name in ((sa, 'create_engine'), (sa.engine, 'create_engine'),
                     (sa.engine.create, 'create_engine'),
                     (sa.Engine, 'connect'), (sa.Engine, 'raw_connection'),
                     (pg8000, 'connect'), (pg8000.dbapi, 'connect')):
    setattr(target, name, deny(target.__name__ + '.' + name))
READY = True
"""

CHILD_PLUGIN = """
import json
import os
from pathlib import Path
import sitecustomize
def pytest_sessionfinish(session, exitstatus):
    report = {'ready': sitecustomize.READY, 'attempts': sitecustomize.ATTEMPTS,
              'nodes': [item.nodeid for item in session.items]}
    Path(os.environ['P09_DISCOVERY_REPORT']).write_text(json.dumps(report))
    if not report['ready'] or report['attempts']:
        session.exitstatus = 1
"""


def test_db_free_make_discovery(tmp_path):
    """Exercise the tracked recipe: either missing exclusion imports a bomb."""
    root = Path(__file__).resolve().parents[2]
    makefile = root / "Makefile"
    guards = tmp_path / "guards"
    guards.mkdir()
    (guards / "sitecustomize.py").write_text(textwrap.dedent(CHILD_GUARDS))
    (guards / "discovery_probe.py").write_text(textwrap.dedent(CHILD_PLUGIN))
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_db_free.py").write_text(
        "def test_collects_without_connections():\n"
        "    import sitecustomize\n"
        "    assert sitecustomize.READY and not sitecustomize.ATTEMPTS\n")
    (tests / "test_live.py").write_text(
        "import pytest\n@pytest.mark.live\n"
        "def test_paid_work_is_not_selected():\n"
        "    raise AssertionError('Live checks are user-run only')\n")
    for suite in ("api", "agent"):
        (tests / suite).mkdir()
        bomb = f"raise AssertionError('{suite} import sentinel reached by DB-free make test')\n"
        (tests / suite / "test_sentinel.py").write_text(bomb)
        (tests / suite / "conftest.py").write_text(bomb)
    (tmp_path / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())
    report = tmp_path / "discovery.json"
    env = dict(os.environ, GCP="off", PYTHONPATH=str(guards),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               PYTEST_ADDOPTS="--collect-only -p discovery_probe", P09_DISCOVERY_REPORT=str(report))
    result = subprocess.run(["make", "--no-print-directory", "-f", str(makefile), "test",
                             f"PY={shlex.quote(sys.executable)}", "TESTS=tests/api"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report.is_file(), result.stdout + result.stderr
    observed = json.loads(report.read_text())
    assert observed == {"ready": True, "attempts": [],
                        "nodes": ["tests/test_db_free.py::test_collects_without_connections"]}


def test_gcp_off_blocks_even_paid_client_construction(no_paid_calls):
    from google import genai
    with pytest.raises(AssertionError, match="google.genai.Client"):
        genai.Client(api_key="offline-sentinel-only")
    # Consume this deliberate sentinel probe; no actual connection is made.
    assert no_paid_calls.attempts.pop() == "google.genai.Client"
    assert no_paid_calls.attempts == []


def test_offline_sentinels_reject_connection_credentials_and_database(no_network):
    import google.auth
    import sqlalchemy as sa
    probes = [
        ("socket.create_connection", lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.01)),
        ("google.auth.default", google.auth.default),
        ("sqlalchemy.create_engine", lambda: sa.create_engine("postgresql+pg8000://")),
    ]
    for name, probe in probes:
        with pytest.raises(AssertionError, match="Offline agent harness forbids"):
            probe()
        # Only consume this test's intentional denial; swallowed extra attempts fail.
        assert no_network.attempts.pop() == name
        assert no_network.attempts == []


def test_fake_script_streams_errors_and_waits_at_a_barrier(no_network, request, barrier):
    fake_model = request.getfixturevalue("fake_model")
    from google.adk.models import LlmRequest, LlmResponse
    from google.genai import types

    async def exercise():
        gate = barrier()
        chunks = [LlmResponse(content=types.Content(parts=[types.Part(text="fragment")]), partial=True),
                  LlmResponse(content=types.Content(parts=[types.Part(text="final")]), partial=False)]
        fake = fake_model([chunks, RuntimeError("scripted failure")], barrier=gate)
        task = asyncio.create_task(drain(fake.generate_content_async(LlmRequest(), stream=True)))
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not task.done()
        gate.release.set()
        assert [response.content.parts[0].text for response in await task] == ["fragment", "final"]
        with pytest.raises(RuntimeError, match="scripted failure"):
            await drain(fake.generate_content_async(LlmRequest()))
        with pytest.raises(AssertionError, match="script exhausted"):
            await drain(fake.generate_content_async(LlmRequest()))
    asyncio.run(exercise())


def test_fake_cancellation_propagates(no_network, request, barrier):
    fake_model = request.getfixturevalue("fake_model")
    from google.adk.models import LlmRequest, LlmResponse

    async def exercise():
        gate = barrier()
        fake = fake_model([LlmResponse()], barrier=gate)
        task = asyncio.create_task(drain(fake.generate_content_async(LlmRequest())))
        await asyncio.wait_for(gate.entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(fake.requests) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
def test_sse_split_utf8_multiline_ping_and_incomplete_frames(chunk_size):
    wire = (': ping\r\n\r\nevent: tool\r\ndata: {"name":"get_fact_checks"}\r\n\r\n'
            'event: answer\ndata: {"text":"café"}\n\n'
            'data: first\ndata: second\n\nevent: answer\ndata: incomplete').encode()
    assert sse([wire[i:i + chunk_size] for i in range(0, len(wire), chunk_size)]) == [
        {"event": "tool", "data": {"name": "get_fact_checks"}},
        {"event": "answer", "data": {"text": "café"}},
        {"event": "message", "data": "first\nsecond"},
    ]


def test_asgi_client_disconnect_cancels_the_real_stream(no_network, asgi_client, barrier):
    from fastapi import FastAPI
    from starlette.responses import StreamingResponse

    async def exercise():
        app = FastAPI()
        gate = barrier()
        stopped = asyncio.Event()

        @app.middleware("http")
        async def through_middleware(request, call_next):
            return await call_next(request)

        @app.get("/stream")
        async def stream():
            async def frames():
                try:
                    yield b'event: tool\ndata: {"name":"get_fact_checks"}\n\n'
                    await gate.wait()
                    yield b'event: answer\ndata: {"text":"should never arrive"}\n\n'
                finally:
                    stopped.set()
            return StreamingResponse(frames(), media_type="text/event-stream")

        async with asgi_client(app, "/stream") as client:
            start = await client.next_message()
            assert start["type"] == "http.response.start" and start["status"] == 200
            first = await client.next_message()
            assert first["type"] == "http.response.body" and first["more_body"]
            assert sse(first["body"]) == [{"event": "tool", "data": {"name": "get_fact_checks"}}]
            await asyncio.wait_for(gate.entered.wait(), 2)
            await client.disconnect()
            assert stopped.is_set() and not gate.release.is_set()
            assert not any(b"should never arrive" in message.get("body", b"") for message in client.messages)
    asyncio.run(exercise())
