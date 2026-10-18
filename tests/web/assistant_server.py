"""Test-only launcher: the bound API with a bounded local BaseLlm, no fake API.

Only pytest's command fixture invokes this file. There is no application env,
route or query switch enabling scripts, and remote/credential clients are denied.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys
import tempfile
import time

sys.path.insert(0, str(Path.cwd()))  # actual bound assistant worktree, not this web checkout

import pytest
import uvicorn
from google.adk.models import BaseLlm
from pydantic import Field
from tests.agent.conftest import ConnectionGuard
from tests.agent.test_model import response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    patch = pytest.MonkeyPatch()
    local_ipc = {name: getattr(socket.socket, name) for name in ("connect", "connect_ex")}
    guard = ConnectionGuard(patch, allow_local=True)
    denials = args.control / "denials.json"
    denials.write_text("[]")
    original_deny = guard.deny
    def deny(name):
        failure = original_deny(name)
        def recorded(*a, **kw):
            try:
                return failure(*a, **kw)
            finally:
                # Flush even swallowed failures before uvicorn re-raises SIGTERM.
                denials.write_text(json.dumps(guard.attempts))
        return recorded
    guard.deny = deny
    # The real engine pool uses Python's local forkserver Unix socket. Permit
    # only that temporary IPC path, retaining the remote/provider sentinels.
    for name, original in local_ipc.items():
        guarded = getattr(socket.socket, name)
        def connect(sock, address, *a, original=original, guarded=guarded, **kw):
            if (sock.family == socket.AF_UNIX and isinstance(address, str)
                    and Path(address).parent.name.startswith("pymp-")
                    and Path(address).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())):
                return original(sock, address, *a, **kw)
            return guarded(sock, address, *a, **kw)
        patch.setattr(socket.socket, name, connect)
    import google.auth
    import google.auth._default
    from google import genai
    patch.setattr(google.auth, "default", guard.deny("credentials"))
    patch.setattr(google.auth._default, "default", guard.deny("credentials"))
    patch.setattr(genai.Client, "__init__", guard.deny("provider client"))
    from agent import jobs, model, numbers
    from api.app import app

    check = numbers.check

    def gated_check(*a, **kw):
        # Test-owned barrier exposes the real server's checking state without
        # delaying the browser with timing guesses or publishing partial prose.
        if json.loads((args.control / "script.json").read_text()).get("block_check"):
            (args.control / "check-entered").touch()
            deadline = time.monotonic() + 15
            while not (args.control / "check-release").exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("Browser did not release the number checker")
                time.sleep(.01)
        return check(*a, **kw)

    patch.setattr(numbers, "check", gated_check)

    class Script(BaseLlm):
        model: str = "offline-browser-model"
        turn: int = 0
        settings: dict = Field(default_factory=dict)

        async def generate_content_async(self, llm_request, stream=False):
            self.turn += 1
            with (args.control / "calls.jsonl").open("a") as out:
                out.write(json.dumps({"turn": self.turn, "tools": [f.name for t in llm_request.config.tools
                    for f in (t.function_declarations or [])]}) + "\n")
            if self.turn > 2:
                raise AssertionError("Unexpected model turn")
            if self.turn == 1:
                mode = self.settings.get("mode", "success")
                if mode == "error":
                    raise RuntimeError("offline provider failure")
                if mode == "timeout":
                    raise TimeoutError("offline deadline")
                if mode == "limit":
                    raise model.LimitReached("8 tool calls")
                if mode == "empty":
                    yield response("")
                    return
                if mode == "whatif":
                    yield response(calls=[("simulate_options", {"overrides": overrides})
                        for overrides in self.settings["simulations"]],
                        usage={"prompt_token_count": 11, "candidates_token_count": 3})
                    return
                yield response(calls=[("get_case", {})], usage={"prompt_token_count": 11, "candidates_token_count": 3})
            else:
                (args.control / "entered").touch()
                if self.settings.get("block"):
                    async with asyncio.timeout(15):
                        while not (args.control / "release").exists():
                            await asyncio.sleep(.01)
                if self.settings.get("mode") == "whatif":
                    results = [p.function_response.response for c in llm_request.contents for p in c.parts
                               if p.function_response and p.function_response.name == "simulate_options"]
                    assert len(results) == len(self.settings["simulations"])
                    assert all("result_id" in result for result in results)
                    yield response("These engine comparisons are hypotheses. A manager must review assumptions "
                        "and authorize business actions.", usage={"prompt_token_count": 17, "candidates_token_count": 7})
                    return
                results = [p.function_response.response for c in llm_request.contents for p in c.parts
                           if p.function_response and p.function_response.name == "get_case"]
                assert len(results) == 1
                rid = results[0]["result_id"]
                yield response("Hidden thought sentinel", thought=True, partial=True)
                yield response(f"Recorded stock is [[{rid}:lot.recorded_qty]] units. "
                    "A manager must send requests and authorize business actions. The benefit is USD 123456789.12.",
                    usage={"prompt_token_count": 17, "candidates_token_count": 7})

    def fake():
        return Script(settings=json.loads((args.control / "script.json").read_text()))

    jobs.ai_on = lambda: True
    model.llm = fake
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        denials.write_text(json.dumps(guard.attempts))
        patch.undo()


if __name__ == "__main__":
    main()
