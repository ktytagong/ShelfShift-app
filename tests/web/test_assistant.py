"""09-16 browser transport entry points, before assistant UI / API integration.

The opt-in fixtures below intercept only this visitor's assistant reads/Ask. Identity and engine figures come from
the real local API. No product flag, URL parameter, role/time control or live model enables these responses.
These passing cases establish the harness, not the assistant UI. 09-18–09-21 add their named behavior assertions
here: brief/fallback, sources/focus, personal Ask/interrupt, human adoption, preserved edits, human Send/Tasks,
Approve backlink and authorized history. There are no skipped/xfail placeholder feature tests.
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.parse
import uuid

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.web.conftest import (
    ROOT, TEST_MODEL, _api_binding, _api_ready, case_id_by_tag, case_json, config, db_tx, session_json,
    take_ownership,
)
from tests.web.test_case import STORY
from tests.web.test_tasks import expand, recorded_time

pytestmark = pytest.mark.web

FALLBACK = "Fallback · Gemini unavailable · figures from the engine"
DAILY = "Today's AI limit is reached. It resets at 00:00 Bangkok time."
CHECKED_TEXT = "Review the engine recommendation and unresolved checks. A manager must send requests and authorize business actions."
STATES = ("generating", "current", "fallback", "cap", "interrupted", "source-denied", "stale-selection",
          "edited-draft", "supplied-simulation")


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@dataclasses.dataclass
class ScriptedAssistant:
    page: Page = dataclasses.field(repr=False)
    state: str
    binding: dict
    current_binding: dict
    summary: str
    output: dict
    draft: dict
    tool_result: dict
    requests_left: int
    edited_message: str = "Manager wording\nPlease keep these line breaks."
    calls: list[dict] = dataclasses.field(default_factory=list)

    @property
    def path(self) -> str:
        return "/api/cases/" + urllib.parse.quote(self.binding["case_id"], safe="")

    def install(self) -> None:
        # Page-local route handlers are installed only when a test asks for one of the explicit fixtures.
        for suffix in ("brief*", "chat*", "drafts*", "trace*", "tool-results/*"):
            self.page.route("**" + self.path + "/" + suffix, self._respond)

    def _respond(self, route: Route) -> None:
        request = route.request
        path = urllib.parse.urlsplit(request.url).path
        self.calls.append({"method": request.method, "path": path, "body": request.post_data,
                           "key": request.headers.get("idempotency-key")})
        if path == self.path + "/chat" and request.method == "POST":
            if self.state == "cap":
                route.fulfill(status=429, json={"error": "cap_reached", "message": DAILY, "kind": "ai", "scope": "daily"})
                return
            asked = json.loads(request.post_data or "{}")
            assert request.headers.get("idempotency-key") == asked["request_id"]
            if self.state == "interrupted":
                content = sse("tool", {"seq": 1, "tool": "get_case", "state": "Working", "duration_ms": None})
                content += sse("interrupted", {"request_id": asked["request_id"], "status": "INTERRUPTED"})
            else:
                content = sse("tool", {"seq": 1, "tool": "get_case", "state": "Working", "duration_ms": None})
                content += sse("tool_result", {"seq": 2, "tool": "get_case", "state": "Complete", "duration_ms": None})
                content += sse("answer", self.output | {"request_id": asked["request_id"], "kind": "ANSWER"})
            route.fulfill(content_type="text/event-stream", body=content)
        elif path == self.path + "/brief" and request.method == "GET":
            content = sse("summary", self.current_binding | {"summary": self.summary}) + ": ping\n\n"
            if self.state == "generating":
                content += sse("state", self.current_binding | {"status": "GENERATING"})
            elif self.state == "fallback":
                content += sse("fallback", self.current_binding | {"status": "FALLBACK", "label": FALLBACK,
                                                                    "summary": self.summary})
            else:
                content += sse("brief", self.output)
            route.fulfill(content_type="text/event-stream", body=content)
        elif path == self.path + "/chat" and request.method == "GET":
            route.fulfill(json={"turns": [self.output | {"question": "Explain the recorded options."}],
                                "next_cursor": None, "total": 1})
        elif path == self.path + "/drafts" and request.method == "GET":
            route.fulfill(json={"rec_version": self.current_binding["rec_version"],
                                "plan_key": self.current_binding["plan_key"], "drafts": [self.draft],
                                "requests_left": self.requests_left})
        elif path == self.path + "/tool-results/" + self.tool_result["result_id"] and request.method == "GET":
            if self.state == "source-denied":
                route.fulfill(status=404, json={"error": "not_found", "message": "This source is not available."})
            else:
                route.fulfill(json=self.tool_result)
        elif path == self.path + "/trace" and request.method == "GET":
            route.fulfill(json=[{k: self.tool_result[k] for k in (
                "result_id", "scope", "tool", "duration_ms", "created_at", "case_id", "rec_version", "plan_key")}])
        else:
            # Do not silently fall through to a live assistant endpoint if a test supplies a wrong source/method.
            route.fulfill(status=404, json={"error": "not_found", "message": "This source is not available."})


@pytest.fixture
def assistant_states(visitor):
    """Factory shared by the nine explicit state fixtures; no module/global app configuration is changed."""
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    detail, session = case_json(page, cid), session_json(page)
    assert detail["result"] and detail["fact_checks"], "the story fixture needs actual engine checks"
    binding = {"run_id": session["run_id"], "case_id": cid, "rec_version": detail["rec_version"],
               "plan_key": detail["result"]["recommended"]}
    fact = detail["fact_checks"][0]
    recorded = dt.datetime.now(dt.timezone.utc).isoformat()

    def make(state: str) -> ScriptedAssistant:
        assert state in STATES
        current = copy.deepcopy(binding)
        if state == "stale-selection":
            alternatives = [o["key"] for o in detail["result"]["options"] if o["key"] != binding["plan_key"]]
            assert alternatives, "stale selection needs a second real option"
            current["plan_key"] = alternatives[0]
        result_id = "test-result-" + uuid.uuid4().hex
        scope = {"scope": "CASE", "session_id": None}
        tool_result = binding | scope | {"result_id": result_id, "tool": "get_case", "inputs": {},
            "result": copy.deepcopy(detail["result"]), "duration_ms": None, "created_at": recorded}
        output = binding | {"output_id": "test-output-" + uuid.uuid4().hex, "request_id": None, "kind": "BRIEF",
            "status": "HISTORICAL" if state == "stale-selection" else "CURRENT", "text": CHECKED_TEXT,
            "refs": [], "checks": {"claims": 0, "removed": 0}, "sources": [{"kind": "engine_result", "result_id": result_id}],
            "cards": [], "provenance": "PRECOMPUTED", "model_id": TEST_MODEL, "assistant_version": "offline-test",
            "reason": None, "created_at": recorded, "usage": {"model_calls": 0, "tool_calls": 0, "retries": 0,
            "input_tokens": None, "output_tokens": None, "thinking_tokens": None, "total_tokens": None,
            "first_text_ms": None, "total_ms": None, "location": None, "model_id": TEST_MODEL}}
        required = not fact["pick_feasible_if_alt"]
        draft = binding | {"draft_id": "test-draft-" + uuid.uuid4().hex, "fact_id": fact["fact_id"],
            "class": "Required" if required else "Advisory", "owner": {"role": "manager", "branch": detail["source"]},
            "to_branch": fact["branch"], "sku": detail["sku"], "lot_ids": fact["lot_ids"],
            "original_text": "Please check this flagged fact and report the required information.",
            "refs": [], "checks": {"claims": 0, "removed": 0}, "model_id": TEST_MODEL, "assistant_version": "offline-test",
            "created_at": recorded, "state": "HISTORICAL" if state == "stale-selection" else "DRAFT", "request_id": None,
            "request_context": {"branch": fact["branch"], "sku": detail["sku"], "lot_ids": fact["lot_ids"],
                "required_parts": ["Printed date per lot"] if fact["kind"] == "ESTIMATED_EXPIRY" else ["Shelf", "Backroom"]}}
        if state == "supplied-simulation":
            # The existing simulate endpoint is real and GCP-off; we never invent replacement engine arithmetic.
            overrides = [{"kind": "demand_multiplier", "branch": detail["source"], "day_from": session["day"],
                          "day_to": session["day"], "value": 1.1}]
            response = page.request.post("/api/cases/" + urllib.parse.quote(cid, safe="") + "/simulate",
                data={"rec_version": binding["rec_version"], "overrides": overrides},
                headers={"Idempotency-Key": str(uuid.uuid4())})
            assert response.status == 200, response.status
            simulation = response.json()
            tool_result["tool"], tool_result["result"] = "simulate_options", simulation
            output["cards"] = [binding | {"result_id": result_id, "overrides": simulation["overrides"],
                "result": simulation["result"], "simulated_rec_version": simulation["rec_version"]}]
        scripted = ScriptedAssistant(page, state, copy.deepcopy(binding), current, detail["summary"], output, draft,
            tool_result, config.SETTINGS["requests"]["max_per_version"] - len(detail["requests"]))
        scripted.install()
        return scripted
    return make


@pytest.fixture
def assistant_generating(assistant_states):
    return assistant_states("generating")


@pytest.fixture
def assistant_current(assistant_states):
    return assistant_states("current")


@pytest.fixture
def assistant_fallback(assistant_states):
    return assistant_states("fallback")


@pytest.fixture
def assistant_cap(assistant_states):
    return assistant_states("cap")


@pytest.fixture
def assistant_interrupted(assistant_states):
    return assistant_states("interrupted")


@pytest.fixture
def assistant_source_denied(assistant_states):
    return assistant_states("source-denied")


@pytest.fixture
def assistant_stale_selection(assistant_states):
    return assistant_states("stale-selection")


@pytest.fixture
def assistant_edited_draft(assistant_states):
    return assistant_states("edited-draft")


@pytest.fixture
def assistant_supplied_simulation(assistant_states):
    return assistant_states("supplied-simulation")


def business_state(run_id: str) -> dict:
    """Read the exact scoped rows; a displayed scripted output must not change any business record."""
    from api import db
    with db_tx() as conn:
        return {table: sorted(db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r", r=run_id),
                              key=lambda row: json.dumps(row, sort_keys=True, default=str))
                for table in ("cases", "recommendations", "evidence_requests", "reported_facts", "signals",
                              "reservations", "decisions", "lots", "truth_lots")}


@pytest.mark.parametrize("state", STATES)
def test_scripted_assistant_transport_is_scoped_and_has_no_business_authority(request, state):
    fixture = "assistant_" + state.replace("-", "_")
    script = request.getfixturevalue(fixture)
    before = business_state(script.binding["run_id"])
    writes = []
    script.page.on("request", lambda r: writes.append(urllib.parse.urlsplit(r.url).path)
                   if r.method != "GET" and "/api/cases/" in r.url else None)
    route = "chat" if state in ("cap", "interrupted") else "brief"
    if state in ("edited-draft", "stale-selection"):
        route = "drafts"
    elif state == "source-denied":
        route = "tool-results/" + script.tool_result["result_id"]
    action_key = uuid.uuid4().hex
    response = script.page.evaluate('''async ([path, post, key]) => {
      const response = await fetch(path, {credentials: "same-origin", ...(post ? {method: "POST",
        headers: {"Content-Type": "application/json", "Idempotency-Key": key},
        body: JSON.stringify({question: "Explain the options.", request_id: key})} : {})});
      return {status: response.status, text: await response.text()};
    }''', [script.path + "/" + route, route == "chat", action_key])
    assert len(script.calls) == 1
    assert script.calls[0]["path"] == script.path + "/" + route
    if state == "cap":
        assert response["status"] == 429 and json.loads(response["text"]) == {
            "error": "cap_reached", "message": DAILY, "kind": "ai", "scope": "daily"}
    elif state == "source-denied":
        assert response["status"] == 404
        assert json.loads(response["text"]) == {"error": "not_found", "message": "This source is not available."}
        assert script.tool_result["result_id"] not in response["text"]
    elif state in ("edited-draft", "stale-selection"):
        data = json.loads(response["text"])
        assert data["drafts"] == [script.draft]
        assert script.draft["request_id"] is None and "due_at" not in script.draft and "sent_at" not in script.draft
        assert script.edited_message != script.draft["original_text"] and "message" not in script.draft
        if state == "stale-selection":
            assert data["rec_version"] == script.draft["rec_version"]
            assert data["plan_key"] != script.draft["plan_key"] and script.draft["state"] == "HISTORICAL"
    elif state == "generating":
        assert "event: state\n" in response["text"] and '"GENERATING"' in response["text"]
        assert "event: brief\n" not in response["text"]
    elif state == "fallback":
        assert FALLBACK in response["text"] and json.dumps(script.summary, ensure_ascii=False) in response["text"]
    elif state == "interrupted":
        assert "event: tool\n" in response["text"] and "event: interrupted\n" in response["text"]
        assert "event: answer\n" not in response["text"]
    else:
        assert "event: brief\n" in response["text"] and CHECKED_TEXT in response["text"]
        assert script.output["usage"]["input_tokens"] is None
        if state == "supplied-simulation":
            assert script.output["cards"][0]["result"] == script.tool_result["result"]["result"]
            assert script.output["cards"][0]["rec_version"] == script.binding["rec_version"]
    assert response["status"] == (429 if state == "cap" else 404 if state == "source-denied" else 200)
    assert writes == ([script.path + "/chat"] if route == "chat" else [])
    assert business_state(script.binding["run_id"]) == before


def test_assistant_fake_is_test_only_and_product_has_no_fake_control(visitor):
    sources = "\n".join(p.read_text() for p in (ROOT / "web" / "src").rglob("*.*")
                        if p.is_file() and not p.name.endswith(".test.ts"))
    for marker in ("ASSISTANT_API_WORKTREE", "ScriptedAssistant", "assistant_states", "offline-browser-model"):
        assert marker not in sources
    expect(visitor.page.get_by_role("navigation", name="Main")).to_be_visible()
    expect(visitor.page.get_by_role("button", name="Fake assistant", exact=True)).to_have_count(0)


def test_assistant_readiness_accepts_additive_model_identity_without_a_model_ping():
    check = _api_ready(TEST_MODEL)
    assert check(httpx.Response(200, json={"ok": True, "db": "ok", "model_id": TEST_MODEL, "ai": "degraded"}))
    for body in ({"ok": True, "db": "ok"}, {"ok": True, "db": "ok", "model_id": "wrong-model"},
                 {"ok": True, "db": "unreachable", "model_id": TEST_MODEL}):
        assert not check(httpx.Response(200, json=body))
    assert not check(httpx.Response(503, json={"ok": False, "db": "unreachable", "model_id": TEST_MODEL}))
    assert not check(httpx.Response(200, text="proxy page"))


def test_explicit_assistant_api_binding_uses_own_tracked_root_and_venv_and_refuses_unbound_roots():
    binding = _api_binding(str(ROOT))
    assert binding.root == ROOT and binding.python == ROOT / ".venv" / "bin" / "python"
    for denied in (ROOT.parent / "ShelfShift-verify", ROOT.parent / "ShelfShift-product", pathlib.Path("/tmp/runtime-mirror")):
        with pytest.raises(RuntimeError, match="bound ShelfShift-assistant"):
            _api_binding(str(denied))


@pytest.mark.parametrize("gcp", [None, "off", "on"], ids=["unset", "off", "explicit-cloud"])
def test_browser_database_accepts_default_off_and_refuses_explicit_cloud_mode(monkeypatch, gcp):
    from tests.web.conftest import web_database

    monkeypatch.delenv("GCP", raising=False)
    if gcp is not None:
        monkeypatch.setenv("GCP", gcp)
    if gcp in (None, "off"):
        web_database.__wrapped__()
    else:
        with pytest.raises(pytest.fail.Exception, match="GCP=off"):
            web_database.__wrapped__()


def test_default_api_binding_uses_current_interpreter_without_git_or_local_venv(monkeypatch, tmp_path):
    from tests.web import conftest as harness

    def no_git(*args, **kwargs):
        pytest.fail("the unset default must not run git subprocess checks")

    # A source archive has neither repository metadata nor a sibling .venv; the original runner supports it.
    monkeypatch.delenv("ASSISTANT_API_WORKTREE", raising=False)
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    monkeypatch.setattr(harness.subprocess, "check_output", no_git)
    assert not (tmp_path / ".git").exists() and not (tmp_path / ".venv").exists()
    binding = harness.assistant_api_binding.__wrapped__()
    assert binding.root == tmp_path and str(binding.python) == sys.executable


@pytest.mark.parametrize("source", ["default", "default-with-model", "alternate"])
def test_api_readiness_preserves_default_check_until_the_root_has_a_model(monkeypatch, tmp_path, source):
    from tests.web import conftest as harness

    monkeypatch.setattr(harness, "ROOT", tmp_path)
    api_root = tmp_path
    if source == "default-with-model":
        (tmp_path / "agent").mkdir()
        (tmp_path / "agent" / "model.py").write_text("# test-only model capability marker\n")
    elif source == "alternate":
        api_root = tmp_path / "alternate"
    check = harness._api_readiness(harness.ApiBinding(api_root, pathlib.Path(sys.executable)))
    legacy = httpx.Response(200, json={"ok": True, "db": "ok"})
    model = httpx.Response(200, json={"ok": True, "db": "ok", "model_id": TEST_MODEL, "ai": "degraded"})
    assert check(legacy) == (source == "default")
    assert check(model) == (source != "default")
    assert not check(httpx.Response(503, json={"ok": True, "db": "ok", "model_id": TEST_MODEL}))
    assert not check(httpx.Response(200, json={"ok": True, "db": "ok", "model_id": "wrong-model"}))


@dataclasses.dataclass
class RealAssistant:
    directory: pathlib.Path

    def configure(self, *, mode="success", block=False, block_check=False, simulations=None):
        for name in ("entered", "release", "check-entered", "check-release"):
            (self.directory / name).unlink(missing_ok=True)
        (self.directory / "script.json").write_text(json.dumps({"mode": mode, "block": block,
            "block_check": block_check, "simulations": simulations or []}))

    def calls(self):
        path = self.directory / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def release(self):
        (self.directory / "release").touch()

    def release_check(self):
        (self.directory / "check-release").touch()


@pytest.fixture(scope="session")
def real_assistant(tmp_path_factory):
    control = RealAssistant(tmp_path_factory.mktemp("real-assistant"))
    control.configure(mode="error")  # other modules' pages see the GCP-off fallback, never a leftover script (09-17)
    yield control
    if os.environ.get("ASSISTANT_API_WORKTREE"):
        assert json.loads((control.directory / "denials.json").read_text()) == [], "A paid/remote call was attempted"


@pytest.fixture(autouse=True)
def assistant_script(real_assistant):
    """Each assistant test starts from the default successful script and leaves the fallback behind (09-17)."""
    real_assistant.configure()
    yield
    real_assistant.configure(mode="error")


@pytest.fixture(scope="session")
def assistant_api_command(assistant_api_binding, real_assistant):
    if os.environ.get("ASSISTANT_API_WORKTREE"):
        return [str(assistant_api_binding.python), str(ROOT / "tests/web/assistant_server.py"),
                "--control", str(real_assistant.directory)]
    return [str(assistant_api_binding.python), "-m", "uvicorn", "api.app:app"]


def assistant_rows(run, table):
    from api import db
    assert table in {"ai_outputs", "ai_usage", "tool_results"}
    with db_tx() as conn:
        return db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r ORDER BY created_at, output_id"
                       if table == "ai_outputs" else f"SELECT * FROM {table} WHERE run_id = :r", r=run)


def test_first_open_brief_tracer(visitor, real_assistant):
    from tests.web.test_compare import owned_at_compare
    from tests.web.test_case import decision_href
    from api import db, refdata
    assert os.environ.get("ASSISTANT_API_WORKTREE"), "The real tracer requires the bound assistant API"
    real_assistant.configure(block=True)
    calls_before = len(real_assistant.calls())
    page = visitor.page
    detail = owned_at_compare(page)
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief).to_be_visible()
    expect(brief).not_to_be_empty()
    expect(brief.get_by_role("status")).to_have_text("Checking figures against the recorded stock decision")
    run = session_json(page)["run_id"]
    before = business_state(run)
    claim, = assistant_rows(run, "ai_outputs")
    assert claim["status"] == "GENERATING" and claim["text"] == ""
    attempt, = assistant_rows(run, "ai_usage")
    assert attempt["status"] == "RUNNING"
    assert len(real_assistant.calls()) == calls_before + 2
    other = visitor.context.new_page()
    try:
        other.goto(decision_href(detail["case_id"]))
        expect(other.get_by_role("region", name="AI brief", exact=True)).to_contain_text(
            "Template summary · AI brief being written")
        # Authenticated second-tab read observes the committed claim, without a new spend/attempt.
        response = other.request.get(f"/api/cases/{detail['case_id']}/brief")
        assert response.status == 200 and 'event: generating\n' in response.text()
        assert len(assistant_rows(run, "ai_usage")) == 1 and len(real_assistant.calls()) == calls_before + 2
        real_assistant.release()
        expect(brief.get_by_test_id("checked-brief")).to_contain_text("A manager must send requests")
        expect(brief).to_contain_text("⚠ number removed: not traceable to a tool result")
        expect(brief).to_contain_text("1 number checked · 1 removed")
        expect(brief).not_to_contain_text("123456789.12")
        expect(brief).not_to_contain_text("Hidden thought sentinel")
        stored, = assistant_rows(run, "ai_outputs")
        audit, = assistant_rows(run, "tool_results")
        assert stored["status"] == "CURRENT" and stored["checks"] == {"claims": 1, "removed": 1}
        assert stored["refs"][0]["result_id"] == audit["result_id"]
        assert audit["tool"] == "get_case" and audit["scope"] == "CASE" and audit["duration_ms"] >= 0
        assert (audit["case_id"], audit["rec_version"], audit["plan_key"]) == (
            stored["case_id"], stored["rec_version"], stored["plan_key"])
        expect(brief.get_by_test_id("checked-brief")).to_contain_text(stored["refs"][0]["text"])
        source = page.request.get(f"/api/cases/{detail['case_id']}/tool-results/{audit['result_id']}")
        assert source.status == 200 and source.json()["result"] == audit["result"]
        assert business_state(run) == before
        assert real_assistant.calls()[-1]["tools"] == ["get_case", "get_forecast", "get_inventory_and_inbound",
            "get_transport_options", "simulate_options", "check_constraints", "get_fact_checks", "draft_evidence_request"]
        # Turn this checked fixture into a saved baseline result. Its actual original trace remains intact.
        with db_tx() as conn:
            db.run(conn, "UPDATE ai_outputs SET provenance = 'PRECOMPUTED' WHERE run_id = :r AND output_id = :o",
                   r=run, o=stored["output_id"])
        page.reload()
        expect(page.get_by_role("region", name="AI brief", exact=True)).to_contain_text("0 Gemini calls on page load")
        assert len(real_assistant.calls()) == calls_before + 2 and len(assistant_rows(run, "ai_usage")) == 1
        # An unrelated branch in this same workspace cannot read/model this case, including sources.
        with db_tx() as conn:
            dataset = db.one(conn, "SELECT dataset_version FROM demo_runs WHERE run_id = :r", r=run)["dataset_version"]
        branches = refdata.get(dataset).branches
        from api.cases import parse_plan
        destination = parse_plan(stored["plan_key"]).dest
        denied_branch = next(b for b in branches if b not in {detail["source"], destination})
        from tests.web.conftest import switch_seat
        switch_seat(page, denied_branch)
        denied = page.request.get(f"/api/cases/{detail['case_id']}/brief")
        assert denied.status == 403 and denied.json()["error"] == "forbidden"
        assert len(real_assistant.calls()) == calls_before + 2 and business_state(run) == before
        expect(page.get_by_role("button", name="Send request", exact=True)).to_have_count(0)
    finally:
        real_assistant.release()
        other.close()


@pytest.mark.parametrize("mode", ["empty", "error", "timeout", "limit"])
def test_real_assistant_failure_keeps_engine_figures_and_human_controls(visitor, real_assistant, mode):
    from tests.web.test_compare import owned_at_compare, grid
    assert os.environ.get("ASSISTANT_API_WORKTREE"), "Requires the bound assistant API"
    real_assistant.configure(mode=mode)
    before_calls = len(real_assistant.calls())
    page = visitor.page
    detail = owned_at_compare(page)
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief).to_contain_text(FALLBACK)
    expect(brief.get_by_test_id("template-summary")).to_have_text(detail["summary"])
    expect(grid(page)).to_be_visible()
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    run = session_json(page)["run_id"]
    usage, = assistant_rows(run, "ai_usage")
    assert usage["status"] == "FALLBACK" and usage["reason"] and usage["model_calls"] == 1
    assert usage["input_tokens"] is None and len(real_assistant.calls()) == before_calls + 1
    assert assistant_rows(run, "ai_outputs")[0]["status"] == "FALLBACK"


def test_brief_current_generating_fallback(assistant_current):
    """One seeded decision: a saved brief, another-tab poll, then quiet fallback; people retain authority."""
    from tests.web.test_case import decision_href, panel, step_button
    from tests.web.test_compare import grid, money, row_cells

    script, page = assistant_current, assistant_current.page
    script.output["assistant_version"] = "earlier-instructions"
    script.output["usage"].update(model_calls=2, tool_calls=1, total_ms=5800, status="DONE")
    before = business_state(script.binding["run_id"])
    writes = []
    page.on("request", lambda r: writes.append(r.url) if r.method != "GET" and "/api/" in r.url else None)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief.get_by_role("heading", name="Decision brief", exact=True)).to_be_visible()
    expect(brief.get_by_text("Precomputed", exact=True)).to_be_visible()
    expect(brief.get_by_test_id("checked-brief")).to_have_text(CHECKED_TEXT)
    expect(brief).to_contain_text("Engine recommendation")
    expect(brief).not_to_contain_text("Manager selection")
    expect(brief).to_contain_text("0 Gemini calls on page load")
    expect(brief).to_contain_text("0 numbers checked")
    brief.get_by_text("Stored generation details", exact=True).click()
    expect(brief).to_contain_text("earlier-instructions")
    expect(brief).to_contain_text("2 Gemini calls · 1 tool call · 5.8 s")
    assert len(script.calls) == 1, "a saved older assistant revision must not regenerate or poll"
    detail, session = case_json(page, script.binding["case_id"]), session_json(page)
    for i, column in enumerate(detail["comparison"]["columns"]):
        expect(row_cells(page, "net").nth(i)).to_contain_text(money(page, column["net"]["base"], session["currency"]))

    script.state = "generating"
    page.reload()
    expect(brief.get_by_text("Template summary · AI brief being written", exact=True)).to_be_visible()
    expect(brief.get_by_test_id("template-summary")).to_have_text(detail["summary"])
    expect(grid(page)).to_be_visible()
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    script.state = "fallback"
    expect(brief).to_contain_text(FALLBACK)
    expect(brief.get_by_test_id("template-summary")).to_have_text(detail["summary"])
    expect(brief.get_by_test_id("checked-brief")).to_have_count(0)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    step_button(page, 2, "Context").click()
    expect(panel(page)).to_have_attribute("data-step", "CONTEXT")
    expect(panel(page).get_by_role("button", name="Count stock", exact=True).first).to_be_enabled()
    step_button(page, 3, "Options").click()
    expect(panel(page)).to_have_attribute("data-step", "OPTIONS")
    assert all(call["method"] == "GET" and call["path"] == script.path + "/brief" for call in script.calls)
    assert writes == [] and business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("width", [375, 390, 1280, 1600])
def test_brief_long_text_uses_content_width_and_local_comparison_scroll(assistant_current, width):
    from tests.web.test_case import decision_href, stepper
    from tests.web.test_compare import grid

    script, page = assistant_current, assistant_current.page
    page.set_viewport_size({"width": width, "height": 844 if width < 768 else 800})
    script.output["text"] = CHECKED_TEXT + " " + "UnbrokenDatasetIdentifier" * 30 + " End of the complete checked brief."
    script.output["model_id"] = "model-" + "LongStoredModelIdentifier" * 25
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief.get_by_test_id("checked-brief")).to_have_text(script.output["text"])
    expect(brief).to_contain_text(script.output["model_id"])
    expect(stepper(page).get_by_role("button")).to_have_count(9)
    layout = page.get_by_test_id("compare-layout")
    bounds, comparison, explanation = layout.bounding_box(), grid(page).bounding_box(), page.get_by_test_id("explanation").bounding_box()
    assert bounds and comparison and explanation
    if bounds["width"] >= 960:
        assert abs(explanation["width"] - 320) <= 1
        assert abs(comparison["y"] - explanation["y"]) <= 1
        assert abs(explanation["x"] - comparison["x"] - comparison["width"] - 16) <= 1
    else:
        assert explanation["y"] >= comparison["y"] + comparison["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    scroll = page.get_by_test_id("compare-scroll")
    expect(grid(page).get_by_role("rowheader").first).to_have_css("position", "sticky")
    if width < 768:
        assert scroll.evaluate("e => e.scrollWidth > e.clientWidth")
    expect(brief.get_by_test_id("checked-brief")).to_have_css("font-size", "14px")
    page.screenshot(path=f"/tmp/p09-18-compare-{width}.png", full_page=True)
    # CSS zoom exercises the 200% reflow boundary without a product test switch.
    if width == 1280:
        page.evaluate("document.documentElement.style.zoom = '2'")
        expect(brief.get_by_test_id("checked-brief")).to_have_text(script.output["text"])
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert page.get_by_test_id("explanation").bounding_box()["y"] > grid(page).bounding_box()["y"]
        page.screenshot(path="/tmp/p09-18-compare-200-percent.png", full_page=True)


@pytest.mark.parametrize("mismatch", ["run_id", "case_id", "rec_version", "plan_key", "status"])
def test_brief_rejects_noncurrent_output_without_retrying_generation(assistant_current, mismatch):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    script.output[mismatch] = "HISTORICAL" if mismatch == "status" else "another-scope"
    script.output["text"] = "Must never appear as the current brief."
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief.get_by_test_id("template-summary")).to_have_text(script.summary)
    expect(brief.get_by_test_id("checked-brief")).to_have_count(0)
    expect(brief).not_to_contain_text(script.output["text"])
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    assert all(call["method"] == "GET" for call in script.calls)


def test_brief_cap_and_missing_measurements_keep_manual_work(assistant_current):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    script.output["provenance"] = "LIVE"
    script.output["usage"] = None
    script.output["sources"] = []
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief.get_by_text("Live", exact=True)).to_be_visible()
    expect(brief).to_contain_text("No workspace data used")
    expect(brief).to_contain_text("0 numbers checked")
    expect(brief).to_contain_text("Missing Gemini calls · Missing tool calls · Missing s")
    page.route("**" + script.path + "/brief", lambda route: route.fulfill(status=429,
        json={"error": "cap_reached", "message": DAILY, "kind": "ai", "scope": "daily"}))
    page.reload()
    expect(brief).to_contain_text(FALLBACK)
    expect(brief).to_contain_text(DAILY)
    expect(brief.get_by_test_id("template-summary")).to_have_text(script.summary)
    expect(brief.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()


def metric_source(script):
    """A resolved reference to actual recorded input, with no replacement engine arithmetic."""
    detail = case_json(script.page, script.binding["case_id"])
    lot = next(lot for lot in detail["engine_input"]["lots"] if lot["lot_id"] == detail["lot"]["lot_id"])
    ref = {"result_id": script.tool_result["result_id"], "path": "lot.recorded_qty", "kind": "plain",
           "value": lot["qty"], "text": f"{lot['qty']:,.0f}"}
    script.output.update(text="Recorded stock is {{0}} units. A manager must authorize business actions.",
                         refs=[ref], checks={"claims": 1, "removed": 0}, sources=[{
                             "result_id": ref["result_id"], "kind": "get_case",
                             "categories": ["Engine result · this version", "Stock records"]}])
    script.tool_result.update(result={"lot": {"lot_id": lot["lot_id"], "recorded_qty": lot["qty"],
        "last_verified_day": None}, "source_kind": "Recorded stock and calculated options", "units": "units"},
        source_kind="Recorded stock and calculated options", units="units")
    return ref


def test_source_scope_and_focus(assistant_current, real_assistant):
    """Exact authorized metric; no cached result after refusal; keyboard and pointer focus return."""
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    ref = metric_source(script)
    before = business_state(script.binding["run_id"])
    model_calls = len(real_assistant.calls())
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    metric = brief.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True)
    expect(metric).to_have_text(ref["text"])
    metric.focus()
    metric.press("Enter")
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer).to_be_visible()
    expect(drawer.get_by_test_id("source-metric-value")).to_have_text(ref["text"])
    expect(drawer).to_contain_text(script.tool_result["result_id"])
    expect(drawer).to_contain_text(script.binding["rec_version"])
    expect(drawer).to_contain_text("Recorded stock and calculated options")
    expect(drawer.get_by_role("button", name="Close", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(drawer).to_have_count(0)
    expect(metric).to_be_focused()
    # Every reopening reauthorizes; no last successful result remains during a refusal.
    script.state = "source-denied"
    category = brief.get_by_role("button", name="Engine result · this version", exact=True)
    category.click()
    expect(drawer).to_contain_text("Source didn't load. This source is not available.")
    expect(drawer.get_by_test_id("source-metric-value")).to_have_count(0)
    expect(drawer.get_by_text("Recorded stock and calculated options", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    script.state = "current"
    drawer.get_by_role("button", name="Try again", exact=True).click()
    expect(drawer).to_contain_text("Recorded stock and calculated options")
    drawer.get_by_role("button", name="Close", exact=True).click()
    expect(category).to_be_focused()
    source_reads = [call for call in script.calls if "/tool-results/" in call["path"]]
    assert len(source_reads) == 3 and all(call["path"] == script.path + "/tool-results/" + ref["result_id"] for call in source_reads)
    assert all(call["method"] == "GET" for call in script.calls)
    assert len(real_assistant.calls()) == model_calls and business_state(script.binding["run_id"]) == before


def test_source_loading_close_and_late_response_are_local(assistant_current, real_assistant):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    ref = metric_source(script)
    deferred = []
    page.route("**" + script.path + "/tool-results/*", lambda route: deferred.append(route))
    model_calls = len(real_assistant.calls())
    page.goto(decision_href(script.binding["case_id"], "compare"))
    trigger = page.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True)
    trigger.click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer.get_by_role("status")).to_have_text("Loading source…")
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    expect(page.get_by_test_id("compare-scroll")).to_be_visible()
    assert len(deferred) == 1
    drawer.get_by_role("button", name="Close", exact=True).click()
    expect(trigger).to_be_focused()
    with page.expect_response("**" + script.path + "/tool-results/" + ref["result_id"]):
        deferred.pop().fulfill(json=script.tool_result)
    expect(drawer).to_have_count(0)
    trigger.click()
    expect(drawer.get_by_role("status")).to_have_text("Loading source…")
    assert len(deferred) == 1
    deferred.pop().fulfill(json=script.tool_result)
    expect(drawer.get_by_test_id("source-metric-value")).to_have_text(ref["text"])
    assert len(real_assistant.calls()) == model_calls


@pytest.mark.parametrize("mismatch", ["run_id", "case_id", "rec_version", "plan_key", "scope", "result_id", "403", "500"])
def test_source_denies_wrong_scope_and_http_failures(assistant_current, mismatch):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    metric_source(script)
    response = copy.deepcopy(script.tool_result)
    response["result"]["note"] = "Private scope sentinel must never be rendered."
    if mismatch.isdigit():
        page.route("**" + script.path + "/tool-results/*", lambda route: route.fulfill(status=int(mismatch),
            json={"error": "forbidden" if mismatch == "403" else "db_busy", "message": "Read refused by the server."}))
    else:
        response[mismatch] = "ASK" if mismatch == "scope" else "another-scope"
        page.route("**" + script.path + "/tool-results/*", lambda route: route.fulfill(json=response))
    page.goto(decision_href(script.binding["case_id"], "compare"))
    page.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True).click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer.get_by_role("alert")).to_contain_text("Source didn't load.")
    expect(drawer).to_contain_text("Read refused by the server." if mismatch.isdigit() else "This source is not available.")
    expect(drawer.get_by_test_id("source-metric-value")).to_have_count(0)
    expect(drawer).not_to_contain_text(response["result"]["note"])
    expect(drawer.get_by_text("Technical details", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()


def test_source_partial_values_and_safe_technical_detail(assistant_current):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    metric_source(script)
    script.tool_result.update(created_at=None, duration_ms=None, source_kind=None, units=None)
    script.tool_result["result"] = {"lot": {"recorded_qty": None}, "note": '<img src=x onerror="window.sourceExecuted=true">',
        "source_kind": None, "units": None, "missing": ["Recorded quantity"],
        "hidden_truth": "Private hidden sentinel", "nested": {"credentials": "Private credential sentinel",
            "thought": "Private thought sentinel", "secret": "Private secret sentinel", "visible": "Authorized nested value"}}
    script.tool_result["inputs"] = {"branch": case_json(page, script.binding["case_id"])["source"],
                                   "authorization": "Private auth sentinel"}
    page.goto(decision_href(script.binding["case_id"], "compare"))
    page.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True).click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer.get_by_test_id("source-metric-value")).to_have_text("Missing")
    expect(drawer).to_contain_text("This metric is missing from this result.")
    expect(drawer).to_contain_text("Missing s")
    expect(drawer).not_to_contain_text("1970")
    expect(drawer.get_by_text("Technical details", exact=True)).to_be_visible()
    drawer.get_by_text("Technical details", exact=True).click()
    expect(drawer.get_by_label("Authorized technical detail", exact=True)).to_contain_text("Authorized nested value")
    expect(drawer.get_by_role("region", name="Source values", exact=True)).to_contain_text(script.tool_result["result"]["note"])
    technical = json.loads(drawer.get_by_label("Authorized technical detail", exact=True).text_content())
    assert technical["result"]["note"] == script.tool_result["result"]["note"]
    expect(drawer.locator("img, script")).to_have_count(0)
    assert page.evaluate("window.sourceExecuted") is None
    for sentinel in ("Private hidden sentinel", "Private credential sentinel", "Private thought sentinel",
                     "Private secret sentinel", "Private auth sentinel"):
        assert sentinel not in drawer.text_content()
    assert drawer.locator("[aria-label='Source values']").evaluate(
        "e => !!(e.compareDocumentPosition(document.querySelector('pre[aria-label=\"Authorized technical detail\"]')) & Node.DOCUMENT_POSITION_FOLLOWING)")


@pytest.mark.parametrize("mode", ["zero", "unlinked", "many"])
def test_source_zero_one_many_results_stay_individually_linked(assistant_current, mode):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    ref = metric_source(script)
    second = copy.deepcopy(script.tool_result)
    second["result_id"] = "result-" + "LongResultIdentifier" * 24
    second["result"]["note"] = "Second exact authorized result"
    if mode == "zero":
        script.output.update(sources=[], refs=[], text=CHECKED_TEXT, checks={"claims": 0, "removed": 0})
    elif mode == "unlinked":
        script.output["sources"] = []
        script.output["missing_sources"] = 1
    else:
        script.output["sources"].append({"kind": "get_case", "result_id": second["result_id"],
                                         "categories": ["Engine result · this version"]})
        page.route("**" + script.path + "/tool-results/" + second["result_id"], lambda route: route.fulfill(json=second))
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief.get_by_test_id("checked-brief")).to_be_visible()
    if mode == "zero":
        expect(brief).to_contain_text("No workspace data used")
        expect(brief).to_contain_text("0 numbers checked")
        expect(brief.get_by_role("button")).to_have_count(0)
    elif mode == "unlinked":
        expect(brief).to_contain_text("This output has no linked tool result.")
        brief.get_by_role("button", name="Source: Recorded quantity · Linked result", exact=True).click()
        expect(page.get_by_role("complementary", name="Tool result")).to_contain_text("This output has no linked tool result.")
        assert not any("/tool-results/" in call["path"] for call in script.calls)
    else:
        expect(brief).not_to_contain_text(second["result_id"])
        expect(brief).not_to_contain_text("Forecast")
        expect(brief).not_to_contain_text("Requests and confirmed facts")
        brief.get_by_text("Engine result · this version (2)", exact=True).click()
        first = brief.get_by_role("button", name="Stock decision details · result 1", exact=True)
        first.click()
        drawer = page.get_by_role("complementary", name="Tool result", exact=True)
        expect(drawer).to_contain_text(ref["result_id"])
        drawer.get_by_role("button", name="Close", exact=True).click()
        second_button = brief.get_by_role("button", name="Stock decision details · result 2", exact=True)
        second_button.click()
        expect(drawer).to_contain_text(second["result_id"])
        expect(drawer).to_contain_text(second["result"]["note"])
        drawer.get_by_role("button", name="Close", exact=True).click()
        expect(second_button).to_be_focused()


@pytest.mark.parametrize("width", [375, 390, 1280])
def test_source_drawer_wraps_ids_and_scrolls_structured_results_locally(assistant_current, width):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    metric_source(script)
    script.tool_result["result_id"] = "result-" + "LongResultIdentifier" * 24
    script.output["refs"][0]["result_id"] = script.tool_result["result_id"]
    script.output["sources"][0]["result_id"] = script.tool_result["result_id"]
    script.tool_result["result"].update(note="LongAuthorizedValue" * 40,
        lots=[{"lot": "LongLotIdentifier" * 25, "recorded_quantity": script.output["refs"][0]["value"]}])
    page.set_viewport_size({"width": width, "height": 844 if width < 768 else 800})
    page.goto(decision_href(script.binding["case_id"], "compare"))
    page.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True).click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer).to_contain_text(script.tool_result["result_id"])
    assert abs(drawer.bounding_box()["width"] - (width if width < 768 else 384)) <= 1
    drawer.get_by_text("Lots", exact=True).click()
    source_table = drawer.get_by_label("Source result table", exact=True)
    assert source_table.evaluate("e => e.scrollWidth > e.clientWidth")
    source_table.focus()
    expect(source_table).to_be_focused()
    expect(drawer.get_by_role("table")).to_contain_text(script.tool_result["result"]["lots"][0]["lot"])
    drawer.get_by_text("Technical details", exact=True).click()
    technical = drawer.get_by_label("Authorized technical detail", exact=True)
    expect(technical).to_contain_text(script.tool_result["result"]["note"])
    assert technical.evaluate("e => e.scrollWidth > e.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert drawer.bounding_box()["x"] >= 0
    page.screenshot(path=f"/tmp/p09-18-source-{width}.png")


def test_source_and_brief_clear_on_same_version_manager_selection(assistant_current):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    metric_source(script)
    detail = case_json(page, script.binding["case_id"])
    selected = next(option for option in detail["result"]["options"] if option["key"] != script.binding["plan_key"])
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    brief.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True).click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer).to_contain_text(script.tool_result["result_id"])
    detail["selected_plan"] = {"plan_key": selected["key"], "rec_version": detail["rec_version"], "label": selected["label"]}
    page.route("**" + script.path, lambda route: route.fulfill(json=detail))
    script.current_binding["plan_key"] = selected["key"]
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(brief).to_contain_text("Manager selection")
    expect(brief).to_contain_text(selected["label"])
    expect(brief).to_contain_text("Engine recommendation")
    expect(drawer).to_have_count(0)
    expect(brief.get_by_test_id("checked-brief")).to_have_count(0)
    expect(brief.get_by_test_id("template-summary")).to_have_text(detail["summary"])
    # The server's new current tuple, rather than an assistant-version change,
    # permits the replacement checked output on the next ordinary opening.
    script.output.update(plan_key=selected["key"], text="Current selected-plan brief.")
    page.reload()
    expect(brief.get_by_test_id("checked-brief")).to_have_text("Current selected-plan brief.")


def test_source_trace_read_and_retry_spend_no_model_call(assistant_current, real_assistant):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    metric_source(script)
    count_before = len(real_assistant.calls())
    trace_reads = []

    def read_trace(route):
        trace_reads.append(route.request.method)
        if len(trace_reads) == 1:
            route.fulfill(status=503, json={"error": "source_unavailable", "message": "Stored evidence is temporarily unavailable."})
        else:
            script._respond(route)

    page.route("**" + script.path + "/trace?*", read_trace)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    brief = page.get_by_role("region", name="AI brief", exact=True)
    brief.get_by_text("Show evidence", exact=True).click()
    expect(brief.get_by_role("alert")).to_have_text("Source didn't load. Stored evidence is temporarily unavailable.")
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    brief.get_by_role("button", name="Try again", exact=True).click()
    trace_call = brief.get_by_role("button", name="Stock decision details", exact=True)
    expect(trace_call).to_be_visible()
    trace_call.click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer).to_contain_text(script.tool_result["result_id"])
    drawer.get_by_role("button", name="Close", exact=True).click()
    expect(trace_call).to_be_focused()
    trace = next(call for call in script.calls if call["path"] == script.path + "/trace")
    assert trace["method"] == "GET"
    assert trace_reads == ["GET", "GET"]
    assert len(real_assistant.calls()) == count_before and all(call["method"] == "GET" for call in script.calls)


def test_source_real_checked_metric_uses_authorized_api_without_generation(visitor, real_assistant):
    from tests.web.test_compare import owned_at_compare

    assert os.environ.get("ASSISTANT_API_WORKTREE"), "Requires the bound assistant API"
    real_assistant.configure()
    page = visitor.page
    detail = owned_at_compare(page)
    brief = page.get_by_role("region", name="AI brief", exact=True)
    metric = brief.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True)
    expect(metric).to_be_visible()
    run = session_json(page)["run_id"]
    output, = assistant_rows(run, "ai_outputs")
    reference, = output["refs"]
    before, calls_before = business_state(run), len(real_assistant.calls())
    metric.focus()
    metric.press("Enter")
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer.get_by_test_id("source-metric-value")).to_have_text(reference["text"])
    expect(drawer).to_contain_text(reference["result_id"])
    expect(drawer).to_contain_text(detail["rec_version"])
    expect(drawer).not_to_contain_text("Hidden thought sentinel")
    drawer.get_by_text("Technical details", exact=True).click()
    technical = json.loads(drawer.get_by_label("Authorized technical detail", exact=True).text_content())
    assert technical["result"]["lot"]["recorded_qty"] == reference["value"]
    drawer.get_by_role("button", name="Close", exact=True).press("Escape")
    expect(drawer).to_have_count(0)
    expect(metric).to_be_focused()
    metric.click()
    expect(drawer.get_by_test_id("source-metric-value")).to_have_text(reference["text"])
    assert len(real_assistant.calls()) == calls_before
    assert business_state(run) == before


def ask_panel(page):
    return page.get_by_role("region", name="Ask about this stock decision", exact=True)


def assistant_requests(run):
    from api import db
    with db_tx() as conn:
        return db.rows(conn, "SELECT * FROM chat_requests WHERE run_id = :r", r=run)


def test_ask_checked_answer_and_interrupt(visitor, real_assistant):
    """One real private Ask: actual tools/checker/storage, then an actual dropped connection."""
    from tests.web.test_compare import owned_at_compare

    assert os.environ.get("ASSISTANT_API_WORKTREE"), "Requires the bound assistant API"
    real_assistant.configure()
    page = visitor.page
    detail = owned_at_compare(page)
    expect(page.get_by_role("region", name="AI brief", exact=True).get_by_test_id("checked-brief")).to_be_visible()
    ask = ask_panel(page)
    expect(ask).to_be_visible()
    run = session_json(page)["run_id"]
    before, calls_before = business_state(run), len(real_assistant.calls())
    writes = []
    page.on("request", lambda r: writes.append(urllib.parse.urlsplit(r.url).path)
            if r.method != "GET" and "/api/" in r.url else None)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    send = ask.get_by_role("button", name="Ask question", exact=True)
    field.fill(" \n ")
    expect(send).to_be_disabled()
    expect(ask.get_by_text("Enter a question.", exact=True)).to_be_visible()
    ask.get_by_role("button", name="Compare these options", exact=True).click()
    expect(field).to_have_value("Compare these options")
    assert len(real_assistant.calls()) == calls_before
    field.press("End")
    field.press("Enter")
    field.type("Keep the unresolved checks visible.")
    question = "Compare these options\nKeep the unresolved checks visible."
    expect(field).to_have_value(question)
    expect(ask.get_by_text("No questions yet", exact=True)).to_be_visible()
    # Test-only network control; the application sees an ordinary fetch stream.
    page.evaluate('''() => {
      const fetchNormally = window.fetch.bind(window);
      window.fetch = (url, init) => {
        if (String(url).endsWith('/chat') && init?.method === 'POST') {
          const controller = new AbortController();
          init.signal?.addEventListener('abort', () => controller.abort(), {once: true});
          window.interruptAsk = () => controller.abort();
          return fetchNormally(url, {...init, signal: controller.signal});
        }
        return fetchNormally(url, init);
      };
    }''')
    real_assistant.configure(block=True, block_check=True)
    try:
        field.press("Control+Enter")
        log = ask.get_by_role("list", name="Question tool progress", exact=True)
        expect(log).to_contain_text("Complete")
        expect(log).to_contain_text("Stock decision details")
        expect(log.get_by_role("listitem")).to_have_count(2)
        expect(send).to_be_disabled()
        expect(send).to_have_attribute("aria-busy", "true")
        expect(field).to_have_value(question)
        expect(field).to_have_attribute("readonly", "")
        expect(ask.get_by_test_id("answer-group")).to_have_count(0)
        assert len(real_assistant.calls()) == calls_before + 2
        real_assistant.release()
        expect(ask.get_by_text("Checking numbers…", exact=True)).to_be_visible()
        expect(ask.get_by_test_id("answer-group")).to_have_count(0)
        real_assistant.release_check()
        group = ask.get_by_test_id("answer-group")
        expect(group).to_have_count(1)
        expect(group).to_contain_text(question)
        expect(group).to_contain_text("A manager must send requests and authorize business actions.")
        expect(group).to_contain_text("⚠ number removed: not traceable to a tool result")
        expect(group).to_contain_text("1 number checked · 1 removed")
        expect(group).to_contain_text("2 Gemini calls · 1 tool call")
        expect(group).not_to_contain_text("Hidden thought sentinel")
        expect(group).not_to_contain_text("123456789.12")
        expect(field).to_be_focused()
        expect(ask.get_by_role("button", name="Go to answer", exact=True)).to_be_visible()
        completed, = [row for row in assistant_rows(run, "ai_outputs") if row["kind"] == "ANSWER"]
        audit, = [row for row in assistant_rows(run, "tool_results") if row["scope"] == "ASK"]
        assert completed["status"] == "CURRENT" and completed["checks"] == {"claims": 1, "removed": 1}
        assert completed["refs"][0]["result_id"] == audit["result_id"]
        group.get_by_role("button", name="Source: Recorded quantity · Engine result · this version", exact=True).click()
        drawer = page.get_by_role("complementary", name="Tool result", exact=True)
        expect(drawer).to_contain_text(audit["result_id"])
        drawer.get_by_role("button", name="Close", exact=True).click()
        assert business_state(run) == before

        real_assistant.configure(block=True)
        field.fill("Keep this interrupted question for me.")
        send.click()
        expect(log).to_contain_text("Complete")
        page.evaluate("window.interruptAsk()")
        expect(ask.get_by_role("alert")).to_have_text("Interrupted, ask again")
        expect(field).to_have_value("Keep this interrupted question for me.")
        expect(ask.get_by_role("button", name="Ask again", exact=True)).to_be_enabled()
        expect(group).to_have_count(1)
        # A bounded condition poll waits for server cleanup, without a timeout
        # sleep and without issuing another generation or write.
        for _ in range(100):
            records = assistant_requests(run)
            if any(row["status"] == "INTERRUPTED" for row in records):
                break
        assert sorted(row["status"] for row in records) == ["DONE", "INTERRUPTED"]
        interrupted, = [row for row in assistant_rows(run, "ai_outputs") if row["status"] == "INTERRUPTED"]
        assert interrupted["text"] == "" and interrupted["refs"] == [] and interrupted["cards"] == []
        assert any(row["status"] == "INTERRUPTED" for row in assistant_rows(run, "ai_usage"))
        assert len([row for row in assistant_rows(run, "tool_results") if row["scope"] == "ASK"]) == 2
        assert business_state(run) == before
        assert writes == [f"/api/cases/{detail['case_id']}/chat"] * 2
        history = page.request.get(f"/api/cases/{detail['case_id']}/chat").json()
        assert history["total"] == 1 and history["turns"][0]["question"] == question
    finally:
        real_assistant.release()
        real_assistant.release_check()


def personal_reads(script, turns=None, *, total=0, cursor=None):
    """Personal history fixture; only this browser's authorized read is replaced."""
    def respond(route):
        if route.request.method != "GET":
            script._respond(route)
            return
        script.calls.append({"method": "GET", "path": script.path + "/chat", "body": None, "key": None})
        route.fulfill(json={"turns": turns or [], "next_cursor": cursor, "total": total})
    script.page.route("**" + script.path + "/chat*", respond)


def test_ask_daily_cap_has_exact_copy_and_no_retry(assistant_cap):
    from tests.web.test_case import decision_href
    script, page = assistant_cap, assistant_cap.page
    personal_reads(script)
    before = business_state(script.binding["run_id"])
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Keep this question after the limit.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    expect(ask.get_by_role("alert")).to_have_text(DAILY)
    expect(field).to_have_value("Keep this question after the limit.")
    expect(ask.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    expect(ask.get_by_role("button", name="Ask again", exact=True)).to_have_count(0)
    expect(ask.get_by_role("button", name="Ask question", exact=True)).to_be_disabled()
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(field).to_have_value("Keep this question after the limit.")
    assert len([call for call in script.calls if call["method"] == "POST"]) == 1
    assert business_state(script.binding["run_id"]) == before


def test_ask_interrupted_retry_is_explicit_and_never_renders_partial_text(assistant_interrupted):
    from tests.web.test_case import decision_href
    script, page = assistant_interrupted, assistant_interrupted.page
    personal_reads(script)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Ask again only when I press it.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    expect(ask.get_by_role("alert")).to_have_text("Interrupted, ask again")
    assert len([call for call in script.calls if call["method"] == "POST"]) == 1
    expect(ask.get_by_test_id("answer-group")).to_have_count(0)
    script.state = "current"
    ask.get_by_role("button", name="Ask again", exact=True).click()
    expect(ask.get_by_test_id("answer-group")).to_contain_text(CHECKED_TEXT)
    posts = [call for call in script.calls if call["method"] == "POST"]
    assert len(posts) == 2 and posts[0]["key"] != posts[1]["key"]
    assert json.loads(posts[0]["body"])["question"] == json.loads(posts[1]["body"])["question"]


@pytest.mark.parametrize("known_total", [True, False])
def test_ask_personal_history_latest_three_older_on_demand_and_long_text(assistant_current, known_total):
    from tests.web.test_case import decision_href
    script, page = assistant_current, assistant_current.page
    turns = [script.output | {"kind": "ANSWER", "output_id": f"answer-{i}", "question": f"Question {i}",
        "text": ("LongDatasetIdentifier" * 45 if i == 4 else CHECKED_TEXT), "usage": script.output["usage"] | {
            "model_calls": 1, "tool_calls": 1, "total_ms": None}, "created_at": f"2026-10-18T05:00:0{i}+00:00"}
        for i in (4, 3, 2, 1)]
    reads = []
    def history(route):
        if route.request.method != "GET":
            script._respond(route)
            return
        older = "cursor=" in route.request.url
        reads.append(older)
        route.fulfill(json={"turns": turns[3:] if older else turns[:3],
            "next_cursor": None if older else "answer-2", "total": 4 if known_total else None})
    page.route("**" + script.path + "/chat*", history)
    page.set_viewport_size({"width": 375, "height": 844})
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    ask.get_by_role("button", name="Show your questions", exact=True).click()
    groups = ask.get_by_test_id("answer-group")
    expect(groups).to_have_count(3)
    assert groups.get_by_role("heading", level=4).all_text_contents() == ["Question 2", "Question 3", "Question 4"]
    expect(groups.last).to_contain_text("1 Gemini call · 1 tool call · Missing s")
    expect(groups.last).to_contain_text("LongDatasetIdentifier" * 45)
    earlier = ask.get_by_role("button", name="Show earlier questions (1)" if known_total else "Show earlier questions", exact=True)
    expect(earlier).to_be_visible()
    assert reads == [False]
    earlier.click()
    expect(groups).to_have_count(4)
    assert groups.get_by_role("heading", level=4).all_text_contents() == ["Question 1", "Question 2", "Question 3", "Question 4"]
    assert reads == [False, True]
    expect(ask.get_by_role("button", name="Show earlier questions", exact=False)).to_have_count(0)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    long_question = "UnbrokenQuestionIdentifier" * 70 + "\nA second line."
    field.fill(long_question)
    expect(field).to_have_value(long_question)
    assert field.evaluate("e => e.scrollWidth <= e.clientWidth + 1 && e.clientHeight > 80")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(field).to_have_value(long_question)
    expect(groups).to_have_count(4)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=f"/tmp/p09-19-ask-history-{known_total}.png")


def test_ask_history_read_error_is_local_and_forbidden_ask_does_not_retry(assistant_current):
    from tests.web.test_case import decision_href
    script, page = assistant_current, assistant_current.page
    reads, posts = [], []
    def chat(route):
        if route.request.method == "POST":
            posts.append(json.loads(route.request.post_data))
            route.fulfill(status=403, json={"error": "forbidden", "message": "This case is not available in this role."})
        else:
            reads.append(True)
            if len(reads) == 1:
                route.fulfill(status=503, json={"error": "db_busy", "message": "Your saved questions are temporarily unavailable."})
            else:
                route.fulfill(json={"turns": [], "next_cursor": None, "total": 0})
    page.route("**" + script.path + "/chat*", chat)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Keep this question through a read error.")
    expect(ask.get_by_role("alert")).to_have_text("Your questions didn't load. Your saved questions are temporarily unavailable.")
    expect(ask.get_by_role("button", name="Ask question", exact=True)).to_be_enabled()
    ask.get_by_role("button", name="Try again", exact=True).click()
    expect(ask.get_by_text("No questions yet", exact=True)).to_be_visible()
    expect(field).to_have_value("Keep this question through a read error.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    expect(ask.get_by_role("alert")).to_have_text("This case is not available in this role.")
    expect(ask.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    expect(ask.get_by_role("button", name="Ask again", exact=True)).to_have_count(0)
    assert len(posts) == 1 and len(reads) == 2


def test_whatif_human_apply(visitor, real_assistant):
    """Real engine cards; whole-list refusal, human review, atomic stale-plan refusal, then human Apply."""
    from tests.web.test_compare import owned_at_compare, money
    assert os.environ.get("ASSISTANT_API_WORKTREE"), "Requires the bound assistant API"
    real_assistant.configure()
    page = visitor.page
    detail = owned_at_compare(page)
    expect(page.get_by_role("region", name="AI brief", exact=True).get_by_test_id("checked-brief")).to_be_visible()
    session = session_json(page)
    cid, version, plan = detail["case_id"], detail["rec_version"], detail["result"]["recommended"]
    overrides = [{"kind": "demand_multiplier", "branch": detail["source"], "day_from": session["day"],
                  "day_to": session["day"], "value": 1.1}]
    mixed = overrides + [{"kind": "run_available", "run_code": detail["engine_input"]["runs"][0]["run_code"], "value": False}]
    real_assistant.configure(mode="whatif", simulations=[overrides, mixed])
    before, calls_before = business_state(session["run_id"]), len(real_assistant.calls())
    writes, pending = [], []
    page.on("request", lambda r: writes.append(urllib.parse.urlsplit(r.url).path)
            if r.method != "GET" and "/api/" in r.url else None)
    ask = ask_panel(page)
    ask.get_by_role("textbox", name="Your question", exact=True).fill("Compare these supplied assumptions before I decide.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    cards = ask.get_by_test_id("whatif-card")
    expect(cards).to_have_count(2)
    expect(cards.first).to_contain_text("What-if · not applied")
    expect(cards.first).to_contain_text(version)
    expect(cards.first).to_contain_text(plan)
    expect(cards.first).to_contain_text("Not provided")
    output, = [row for row in assistant_rows(session["run_id"], "ai_outputs") if row["kind"] == "ANSWER"]
    supplied = next(card for card in output["cards"] if card["adoptable"])
    first = cards.filter(has_text=supplied["result_id"])
    unsupported = cards.filter(has_text=next(card["result_id"] for card in output["cards"] if not card["adoptable"]))
    option = supplied["result"]["options"][supplied["result"]["recommended"]]
    expect(first).to_contain_text(money(page, option["net"]["base"], session["currency"]))
    expect(unsupported.get_by_role("button", name="Use these assumptions…", exact=True)).to_be_disabled()
    expect(unsupported).to_contain_text("These changes are for comparison only.")
    expect(unsupported.get_by_role("link", name="Review current checks", exact=True)).to_be_visible()
    assert case_json(page, cid)["rec_version"] == version and case_json(page, cid)["adopted"] == []
    after_ask = business_state(session["run_id"])
    assert {k: v for k, v in after_ask.items() if k != "recommendations"} == {k: v for k, v in before.items() if k != "recommendations"}
    assert len(real_assistant.calls()) == calls_before + 2
    assert writes == [f"/api/cases/{cid}/chat"]
    first.get_by_role("button", name="Source: What-if comparison", exact=True).click()
    drawer = page.get_by_role("complementary", name="Tool result", exact=True)
    expect(drawer).to_contain_text(supplied["result_id"])
    expect(drawer).to_contain_text("Engine what-if; not adopted")
    drawer.get_by_role("button", name="Close", exact=True).click()
    first.get_by_role("button", name="Use these assumptions…", exact=True).click()
    note = first.get_by_role("textbox", name="Source of this assumption (required)", exact=True)
    apply = first.get_by_role("button", name="Apply assumptions", exact=True)
    expect(apply).to_be_disabled()
    expect(first).to_contain_text("Add the source of this assumption.")
    expect(first).to_contain_text("This creates a new recommendation version and cancels pending approvals. Facts stay unverified and lot holds stay in place.")
    source_note = "Manager checked the revised forecast with the branch.\nUse the full reviewed list."
    note.fill(source_note)
    page.route(f"**/api/cases/{cid}/adopt", lambda route: pending.append(route))
    apply.click()
    expect(apply).to_have_attribute("aria-busy", "true")
    expect(apply).to_be_disabled()
    expect(note).to_have_value(source_note)
    expect(first.get_by_text("Assumptions applied · new version", exact=True)).to_have_count(0)
    assert len(pending) == 1
    reviewed = {"overrides": overrides, "note": source_note, "rec_version": version, "plan_key": plan}
    assert json.loads(pending[0].request.post_data) == reviewed
    first_key = pending[0].request.headers["idempotency-key"]
    # Another tab selects a different real option after review but before the
    # request reaches the server. Apply must retain its original tuple.
    other_plan = next(option["key"] for option in detail["result"]["options"] if option["key"] == "keep")
    assert other_plan != plan
    selected = page.request.post(f"/api/cases/{cid}/select", data={"rec_version": version, "plan_key": other_plan},
                                headers={"Idempotency-Key": uuid.uuid4().hex})
    assert selected.status == 200, selected.text()
    after_selection = business_state(session["run_id"])
    with page.expect_response(lambda response: response.url.endswith(f"/api/cases/{cid}/adopt")) as refused:
        pending.pop(0).continue_()
    assert refused.value.status == 409 and refused.value.json()["error"] == "stale_plan"
    expect(first.get_by_role("alert")).to_have_text(refused.value.json()["message"])
    expect(note).to_have_value(source_note)
    assert business_state(session["run_id"]) == after_selection
    expect(first.get_by_text("Assumptions applied · new version", exact=True)).to_have_count(0)
    restored = page.request.post(f"/api/cases/{cid}/select", data={"rec_version": version, "plan_key": plan},
                                headers={"Idempotency-Key": uuid.uuid4().hex})
    assert restored.status == 200, restored.text()
    first.get_by_role("button", name="Review current options", exact=True).click()
    expect(note).to_have_value(source_note)
    expect(apply).to_be_enabled()
    apply.click()
    expect(apply).to_have_attribute("aria-busy", "true")
    assert len(pending) == 1 and json.loads(pending[0].request.post_data) == reviewed
    assert pending[0].request.headers["idempotency-key"] != first_key
    assert len(real_assistant.calls()) == calls_before + 2
    real_assistant.configure()
    with page.expect_response(lambda response: response.url.endswith(f"/api/cases/{cid}/adopt")) as accepted:
        pending.pop(0).continue_()
    assert accepted.value.status == 200
    expect(first.get_by_text("Assumptions applied · new version", exact=True)).to_be_visible()
    adopted = case_json(page, cid)
    assert adopted["rec_version"] != version
    assert [item["override"] for item in adopted["adopted"]] == overrides
    assert adopted["adopted"][0]["note"] == source_note
    after_apply = business_state(session["run_id"])
    unsupported.get_by_role("button", name="Discard", exact=True).click()
    expect(cards).to_have_count(1)
    expect(ask.get_by_test_id("answer-group")).to_contain_text(output["text"])
    assert business_state(session["run_id"]) == after_apply
    assert writes == [f"/api/cases/{cid}/chat", f"/api/cases/{cid}/adopt", f"/api/cases/{cid}/adopt"]


@pytest.mark.parametrize("width", [375, 1280])
def test_whatif_independent_notes_missing_metrics_retry_and_local_discard(assistant_supplied_simulation, width):
    from tests.web.test_case import decision_href
    script, page = assistant_supplied_simulation, assistant_supplied_simulation.page
    first = copy.deepcopy(script.output["cards"][0])
    second = copy.deepcopy(first) | {"result_id": first["result_id"] + "-second"}
    first["result"] = {"recommended": first["result"]["recommended"], "options": {}}
    script.output["sources"].append({"kind": "engine_result", "result_id": second["result_id"]})
    turn = script.output | {"kind": "ANSWER", "question": "Keep each comparison separate.", "cards": [first, second]}
    personal_reads(script, [turn], total=1)
    page.set_viewport_size({"width": width, "height": 844 if width < 768 else 800})
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    ask.get_by_role("button", name="Show your questions", exact=True).click()
    cards = ask.get_by_test_id("whatif-card")
    expect(cards).to_have_count(2)
    expect(cards.first.get_by_text("Not provided", exact=True)).to_have_count(5)
    expect(cards.first.get_by_text("0", exact=True)).to_have_count(0)
    cards.first.get_by_role("button", name="Use these assumptions…", exact=True).click()
    note = cards.first.get_by_role("textbox", name="Source of this assumption (required)", exact=True)
    expect(note).to_be_focused()
    long_note = "ManagerSourceIdentifier" * 55 + "\nKeep the original source."
    note.fill(long_note)
    cards.last.get_by_role("button", name="Use these assumptions…", exact=True).click()
    other_note = cards.last.get_by_role("textbox", name="Source of this assumption (required)", exact=True)
    other_note.fill("A different source for the second comparison.")
    expect(note).to_have_value(long_note)
    before = business_state(script.binding["run_id"])
    attempts = []
    def refuse(route):
        attempts.append({"key": route.request.headers["idempotency-key"], "body": json.loads(route.request.post_data)})
        route.fulfill(status=503, json={"error": "db_busy", "message": "The saved assumptions are temporarily unavailable."})
    page.route("**" + script.path + "/adopt", refuse)
    cards.first.get_by_role("button", name="Apply assumptions", exact=True).click()
    expect(cards.first.get_by_role("alert")).to_have_text("The saved assumptions are temporarily unavailable.")
    expect(note).to_have_value(long_note)
    cards.first.get_by_role("button", name="Try again", exact=True).click()
    expect(cards.first.get_by_role("alert")).to_have_text("The saved assumptions are temporarily unavailable.")
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    note.fill("A newly edited source is a new human action.")
    cards.first.get_by_role("button", name="Apply assumptions", exact=True).click()
    expect(cards.first.get_by_role("alert")).to_have_text("The saved assumptions are temporarily unavailable.")
    assert len(attempts) == 3 and attempts[2]["key"] != attempts[0]["key"]
    assert attempts[2]["body"] == {"overrides": first["overrides"], "note": "A newly edited source is a new human action.",
        "rec_version": first["rec_version"], "plan_key": first["plan_key"]}
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(other_note).to_have_value("A different source for the second comparison.")
    expect(cards.first.get_by_role("textbox")).to_have_value("A newly edited source is a new human action.")
    cards.last.get_by_role("button", name="Discard", exact=True).click()
    expect(cards).to_have_count(1)
    expect(ask.get_by_test_id("answer-group")).to_contain_text(CHECKED_TEXT)
    assert business_state(script.binding["run_id"]) == before
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=f"/tmp/p09-19-whatif-{width}.png")


def test_assistant_edits_keep_original_binding(visitor, real_assistant):
    """One real what-if editor survives refresh and stays bound when another tab selects a plan."""
    from tests.web.test_compare import owned_at_compare
    assert os.environ.get("ASSISTANT_API_WORKTREE"), "Requires the bound assistant API"
    real_assistant.configure()
    page = visitor.page
    detail = owned_at_compare(page)
    expect(page.get_by_role("region", name="AI brief", exact=True).get_by_test_id("checked-brief")).to_be_visible()
    session = session_json(page)
    overrides = [{"kind": "demand_multiplier", "branch": detail["source"], "day_from": session["day"],
                  "day_to": session["day"], "value": 1.1}]
    real_assistant.configure(mode="whatif", simulations=[overrides])
    ask = ask_panel(page)
    question = ask.get_by_role("textbox", name="Your question", exact=True)
    question.fill("Compare one engine scenario for my review.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    card = ask.get_by_test_id("whatif-card")
    expect(card).to_have_count(1)
    card.get_by_role("button", name="Use these assumptions…", exact=True).click()
    note = card.get_by_role("textbox", name="Source of this assumption (required)", exact=True)
    note.fill("Keep this unsent source note on the reviewed baseline.\nOriginal manager wording.")
    question.fill("Keep my next unsent question on its original plan.")
    group = ask.get_by_test_id("answer-group")
    group.get_by_text("Show evidence", exact=True).click()
    trace = group.locator("details").filter(has=page.get_by_text("Show evidence", exact=True))
    expect(trace).to_have_attribute("open", "")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(question).to_have_value("Keep my next unsent question on its original plan.")
    expect(question).not_to_have_attribute("readonly", "")
    expect(note).to_have_value("Keep this unsent source note on the reviewed baseline.\nOriginal manager wording.")
    expect(trace).to_have_attribute("open", "")
    expect(card.get_by_role("button", name="Apply assumptions", exact=True)).to_be_enabled()
    real_assistant.configure()
    selected = page.request.post(f"/api/cases/{detail['case_id']}/select", data={"rec_version": detail["rec_version"], "plan_key": "keep"},
                                headers={"Idempotency-Key": uuid.uuid4().hex})
    assert selected.status == 200, selected.text()
    after_selection = business_state(session["run_id"])
    writes = []
    page.on("request", lambda r: writes.append(r.url) if r.method != "GET" and "/api/" in r.url else None)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_role("region", name="AI brief", exact=True)).to_contain_text("Manager selection")
    expect(question).to_have_attribute("readonly", "")
    expect(question).to_have_value("Keep my next unsent question on its original plan.")
    expect(ask.get_by_role("button", name="Ask question", exact=True)).to_be_disabled()
    expect(note).to_have_value("Keep this unsent source note on the reviewed baseline.\nOriginal manager wording.")
    expect(note).to_have_attribute("readonly", "")
    expect(card.get_by_role("button", name="Apply assumptions", exact=True)).to_be_disabled()
    expect(card).to_contain_text(detail["rec_version"])
    expect(card).to_contain_text(detail["result"]["recommended"])
    expect(ask).to_contain_text("Plan changed · review current checks. Your edits are kept here for reference.")
    expect(ask.get_by_role("link", name="Review current checks", exact=True)).to_be_visible()
    expect(trace).to_have_attribute("open", "")
    question.press("Control+Enter")
    assert writes == [] and business_state(session["run_id"]) == after_selection


def test_assistant_version_change_retains_inputs_and_ignores_late_apply(assistant_supplied_simulation):
    from tests.web.test_case import decision_href
    script, page = assistant_supplied_simulation, assistant_supplied_simulation.page
    turn = script.output | {"kind": "ANSWER", "question": "Review this stored engine comparison."}
    personal_reads(script, [turn], total=1)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("My unsent question belongs to the original version.")
    card = ask.get_by_test_id("whatif-card")
    expect(card).to_have_count(1)
    card.get_by_role("button", name="Use these assumptions…", exact=True).click()
    note = card.get_by_role("textbox", name="Source of this assumption (required)", exact=True)
    note.fill("My unsent source belongs to the original version.")
    pending = []
    page.route("**" + script.path + "/adopt", lambda route: pending.append(route))
    apply = card.get_by_role("button", name="Apply assumptions", exact=True)
    apply.click()
    expect(apply).to_have_attribute("aria-busy", "true")
    assert len(pending) == 1
    # A separate, real human action wins before this delayed browser response.
    changed = page.request.post(script.path + "/adopt", data={"overrides": script.output["cards"][0]["overrides"],
        "note": "Another tab explicitly adopted this scenario.", "rec_version": script.binding["rec_version"],
        "plan_key": script.binding["plan_key"]}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert changed.status == 200, changed.text()
    new_version = changed.json()["rec_version"]
    assert new_version != script.binding["rec_version"]
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(field).to_have_attribute("readonly", "")
    expect(field).to_have_value("My unsent question belongs to the original version.")
    expect(note).to_have_value("My unsent source belongs to the original version.")
    expect(card).to_contain_text(script.binding["rec_version"])
    expect(ask).to_contain_text("This result is for an earlier version. Your edits are kept here for reference.")
    before_ack = business_state(script.binding["run_id"])
    pending.pop().fulfill(json={"rec_version": new_version})
    expect(apply).not_to_have_attribute("aria-busy", "true")
    expect(apply).to_be_disabled()
    expect(card.get_by_text("Assumptions applied · new version", exact=True)).to_have_count(0)
    expect(note).to_have_attribute("readonly", "")
    assert business_state(script.binding["run_id"]) == before_ack


def test_assistant_plan_change_aborts_old_stream_without_publishing_answer(visitor, real_assistant):
    from tests.web.test_compare import owned_at_compare
    real_assistant.configure()
    page = visitor.page
    detail = owned_at_compare(page)
    expect(page.get_by_role("region", name="AI brief", exact=True).get_by_test_id("checked-brief")).to_be_visible()
    run = session_json(page)["run_id"]
    real_assistant.configure(block=True)
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Do not publish an answer after this plan changes.")
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/chat") else None)
    try:
        ask.get_by_role("button", name="Ask question", exact=True).click()
        expect(ask.get_by_role("list", name="Question tool progress", exact=True)).to_contain_text("Complete")
        selected = page.request.post(f"/api/cases/{detail['case_id']}/select", data={"rec_version": detail["rec_version"], "plan_key": "keep"},
                                    headers={"Idempotency-Key": uuid.uuid4().hex})
        assert selected.status == 200, selected.text()
        real_assistant.configure()
        after_selection = business_state(run)
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(field).to_have_attribute("readonly", "")
        expect(ask.get_by_role("button", name="Ask question", exact=True)).not_to_have_attribute("aria-busy", "true")
        expect(ask.get_by_test_id("answer-group")).to_have_count(0)
        expect(ask.get_by_role("button", name="Go to answer", exact=True)).to_have_count(0)
        expect(field).to_have_value("Do not publish an answer after this plan changes.")
        assert len(posts) == 1 and business_state(run) == after_selection
        for _ in range(100):
            rows = assistant_requests(run)
            if rows[0]["status"] == "INTERRUPTED":
                break
        assert rows[0]["status"] == "INTERRUPTED"
        answer, = [row for row in assistant_rows(run, "ai_outputs") if row["kind"] == "ANSWER"]
        assert answer["text"] == "" and answer["status"] == "INTERRUPTED"
        real_assistant.release()
    finally:
        real_assistant.release()
        real_assistant.release_check()


def test_assistant_unchanged_http_retry_reuses_body_and_edit_starts_new_action(assistant_current):
    from tests.web.test_case import decision_href
    script, page = assistant_current, assistant_current.page
    attempts = []
    def chat(route):
        if route.request.method == "GET":
            route.fulfill(json={"turns": [], "next_cursor": None, "total": 0})
            return
        body = json.loads(route.request.post_data)
        attempts.append({"key": route.request.headers["idempotency-key"], "body": body})
        if len(attempts) <= 2:
            route.fulfill(status=503, json={"error": "db_busy", "message": "This question could not start yet."})
        else:
            route.fulfill(content_type="text/event-stream", body=sse("answer", script.output | {
                "kind": "ANSWER", "request_id": body["request_id"]}))
    page.route("**" + script.path + "/chat*", chat)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("The unchanged question.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    expect(ask.get_by_role("alert")).to_have_text("This question could not start yet.")
    ask.get_by_role("button", name="Try again", exact=True).click()
    expect(ask.get_by_role("alert")).to_have_text("This question could not start yet.")
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    field.fill("An edited question starts a different human action.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    expect(ask.get_by_test_id("answer-group")).to_contain_text("An edited question starts a different human action.")
    assert len(attempts) == 3 and attempts[2]["key"] != attempts[0]["key"]
    assert attempts[2]["body"]["request_id"] == attempts[2]["key"]


def test_assistant_role_change_clears_personal_inputs_history_and_source(assistant_supplied_simulation):
    from tests.web.conftest import switch_seat
    from tests.web.test_case import decision_href
    script, page = assistant_supplied_simulation, assistant_supplied_simulation.page
    turn = script.output | {"kind": "ANSWER", "question": "Private manager conversation sentinel."}
    personal_reads(script, [turn], total=1)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    ask.get_by_role("textbox", name="Your question", exact=True).fill("Private unsent manager question.")
    card = ask.get_by_test_id("whatif-card")
    card.get_by_role("button", name="Use these assumptions…", exact=True).click()
    card.get_by_role("textbox", name="Source of this assumption (required)", exact=True).fill("Private manager source note.")
    # The legacy fixture is CASE-scoped. Its refusal remains local and is also
    # cleared when the real sign-out/sign-in changes the session and role.
    card.get_by_role("button", name="Source: What-if comparison", exact=True).click()
    expect(page.get_by_role("complementary", name="Tool result", exact=True)).to_be_visible()
    switch_seat(page, None)
    expect(page.get_by_role("complementary", name="Tool result", exact=True)).to_have_count(0)
    expect(page.get_by_text("Private manager conversation sentinel.", exact=True)).to_have_count(0)
    expect(page.get_by_text("Private manager source note.", exact=True)).to_have_count(0)
    expect(ask_panel(page).get_by_role("textbox", name="Your question", exact=True)).to_have_value("")
    expect(ask_panel(page).get_by_test_id("answer-group")).to_have_count(0)


def test_assistant_new_events_keep_focus_and_earlier_answer_in_view(visitor, real_assistant):
    from tests.web.test_compare import owned_at_compare
    real_assistant.configure()
    page = visitor.page
    owned_at_compare(page)
    expect(page.get_by_role("region", name="AI brief", exact=True).get_by_test_id("checked-brief")).to_be_visible()
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Show an answer I can inspect while asking again.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    old = ask.get_by_test_id("answer-group").first
    expect(old).to_be_visible()
    old_id = old.get_attribute("id")
    real_assistant.configure(block=True, block_check=True)
    try:
        field.fill("Continue without taking focus from the earlier answer.")
        ask.get_by_role("button", name="Ask question", exact=True).click()
        expect(ask.get_by_role("list", name="Question tool progress", exact=True)).to_contain_text("Complete")
        old.focus()
        page.evaluate("id => { const e = document.getElementById(id); scrollTo(0, scrollY + e.getBoundingClientRect().top - 100); }", old_id)
        top = old.bounding_box()["y"]
        real_assistant.release()
        expect(ask.get_by_text("Checking numbers…", exact=True)).to_be_visible()
        expect(old).to_be_focused()
        assert abs(old.bounding_box()["y"] - top) <= 2
        real_assistant.release_check()
        expect(ask.get_by_test_id("answer-group")).to_have_count(2)
        expect(page.locator("[id='" + old_id + "']")).to_be_focused()
        assert abs(page.locator("[id='" + old_id + "']").bounding_box()["y"] - top) <= 2
    finally:
        real_assistant.release()
        real_assistant.release_check()


@pytest.mark.parametrize("refusal", ["stale_version", "stale_plan", "historical"])
def test_assistant_server_stale_response_disables_original_form(assistant_current, refusal):
    from tests.web.test_case import decision_href
    script, page = assistant_current, assistant_current.page
    posts = []
    message = "This result is for an earlier version." if refusal == "historical" else "The reviewed context changed; reload the stock decision."
    def chat(route):
        if route.request.method == "GET":
            route.fulfill(json={"turns": [], "next_cursor": None, "total": 0})
        else:
            posts.append(json.loads(route.request.post_data))
            if refusal == "historical":
                route.fulfill(content_type="text/event-stream", body=sse("answer", script.output | {
                    "kind": "ANSWER", "status": "HISTORICAL", "request_id": posts[-1]["request_id"]}))
            else:
                route.fulfill(status=409, json={"error": refusal, "message": message})
    page.route("**" + script.path + "/chat*", chat)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    ask = ask_panel(page)
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Keep the question tied to the context I reviewed.")
    ask.get_by_role("button", name="Ask question", exact=True).click()
    expect(ask.get_by_role("alert")).to_have_text(message)
    expect(field).to_have_attribute("readonly", "")
    expect(field).to_have_value("Keep the question tied to the context I reviewed.")
    expect(ask.get_by_role("button", name="Ask question", exact=True)).to_be_disabled()
    expect(ask.get_by_role("link", name="Review current checks", exact=True)).to_be_visible()
    expect(ask.get_by_test_id("answer-group")).to_have_count(0)
    assert len(posts) == 1
# 09-21: append-only integration cases so the parallel Ask/draft work can merge independently.
def test_approve_backlink_focus_no_generation(assistant_current, real_assistant):
    """A local keyboard backlink reads the saved current brief; it has no business authority."""
    from tests.web.test_case import decision_href, panel, stepper

    script, page = assistant_current, assistant_current.page
    before, calls_before = business_state(script.binding["run_id"]), len(real_assistant.calls())
    writes = []
    page.on("request", lambda request: writes.append(request.url)
            if request.method != "GET" and "/api/" in request.url else None)
    page.goto(decision_href(script.binding["case_id"], "approve"))
    expect(panel(page)).to_have_attribute("data-step", "APPROVE")
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    backlink = panel(page).get_by_role("link", name="Read decision brief in Compare", exact=True)
    backlink.focus()
    backlink.press("Enter")
    expect(panel(page)).to_have_attribute("data-step", "COMPARE")
    expect(panel(page).get_by_role("heading", name="Decision brief", exact=True)).to_be_focused()
    expect(page.get_by_test_id("checked-brief")).to_have_text(CHECKED_TEXT)
    expect(stepper(page).get_by_role("button")).to_have_count(9)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    assert script.calls == [{"method": "GET", "path": script.path + "/brief", "body": None, "key": None}]
    assert writes == [] and len(real_assistant.calls()) == calls_before
    assert business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("state", ["generating", "fallback", "cap"])
def test_approve_backlink_keeps_template_and_human_controls(assistant_states, real_assistant, state):
    from tests.web.test_case import decision_href, panel

    script = assistant_states(state)
    page = script.page
    if state == "cap":
        page.route("**" + script.path + "/brief", lambda route: route.fulfill(status=429,
            json={"error": "cap_reached", "message": DAILY, "kind": "ai", "scope": "daily"}))
    before, calls_before = business_state(script.binding["run_id"]), len(real_assistant.calls())
    page.goto(decision_href(script.binding["case_id"], "approve"))
    expect(panel(page)).to_contain_text("Only people can approve.")
    panel(page).get_by_role("link", name="Read decision brief in Compare", exact=True).click()
    brief = page.get_by_role("region", name="AI brief", exact=True)
    expect(brief.get_by_role("heading", name="Decision brief", exact=True)).to_be_focused()
    expect(brief.get_by_test_id("template-summary")).to_have_text(script.summary)
    expect(brief).to_contain_text("Template summary · AI brief being written" if state == "generating" else FALLBACK)
    if state == "cap":
        expect(brief).to_contain_text(DAILY)
        expect(brief.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    assert all(call["method"] == "GET" and call["path"] == script.path + "/brief" for call in script.calls)
    assert len(real_assistant.calls()) == calls_before and business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("width,zoom", [(375, 1), (390, 1), (1280, 2)])
def test_approve_backlink_wraps_and_keeps_keyboard_navigation(assistant_current, width, zoom):
    from tests.web.test_case import decision_href, panel, stepper

    script, page = assistant_current, assistant_current.page
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(decision_href(script.binding["case_id"], "approve"))
    page.evaluate("zoom => document.documentElement.style.zoom = zoom", str(zoom))
    backlink = panel(page).get_by_role("link", name="Read decision brief in Compare", exact=True)
    expect(backlink).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    sidebar = page.get_by_role("navigation", name="Main", exact=True)
    was_visible = sidebar.is_visible()
    backlink.focus()
    page.keyboard.press("Enter")
    expect(panel(page).get_by_role("heading", name="Decision brief", exact=True)).to_be_focused()
    expect(stepper(page).get_by_role("button")).to_have_count(9)
    assert sidebar.is_visible() == was_visible
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.screenshot(path=f"/tmp/p09-21-backlink-{width}-{zoom}.png")


def seed_assistant_activity(script, count=5):
    """Persist authorized draft provenance; Activity and its privacy projection remain real API reads."""
    from api import db

    page = script.page
    detail = case_json(page, script.binding["case_id"])
    session = session_json(page)
    plans = detail["result"]["options"][:count]
    assert len(plans) == count
    ids = []
    with db_tx() as conn:
        for index, plan in enumerate(plans):
            draft_id = "D-history-" + uuid.uuid4().hex + "-" + str(index)
            ids.append(draft_id)
            db.run(conn, "INSERT INTO evidence_drafts (run_id, draft_id, case_id, rec_version, plan_key, fact_id, "
                "class, owner, to_branch, sku, lot_ids, original_text, refs, checks, model_id, assistant_version, "
                "request_context, state) VALUES (:r, :id, :c, :v, :p, :f, :cl, :o, :b, :sku, CAST(:lots AS jsonb), "
                ":text, '[]', CAST(:checks AS jsonb), :m, :av, CAST(:context AS jsonb), 'HISTORICAL')",
                r=session["run_id"], id=draft_id, c=detail["case_id"], v=detail["rec_version"], p=plan["key"],
                f=script.draft["fact_id"], cl=script.draft["class"], o=detail["source"], b=script.draft["to_branch"],
                sku=detail["sku"], lots=db.js(script.draft["lot_ids"]), text="Historical original draft is not shared Activity prose.",
                checks=db.js({"claims": 0, "removed": 0}), m=TEST_MODEL, av="stored-browser-revision",
                context=db.js(script.draft["request_context"]))
            db.log(conn, session["run_id"], session["day"], "evidence_drafted", detail["case_id"], draft_id=draft_id)
        db.log(conn, session["run_id"], session["day"], "chat_answered", detail["case_id"],
            scope="ASK", question="Another person's private question sentinel", answer_text="Private answer sentinel")
        db.log(conn, session["run_id"], session["day"], "legacy_record", detail["case_id"],
            audit={"scope": "ASK", "tool_result": "Private source sentinel"})
    response = page.request.get(script.path + "/activity")
    assert response.status == 200
    entries = response.json()
    assert len([entry for entry in entries if entry["group"] == "Assistant"]) == count
    assert not any("private" in json.dumps(entry).lower() for entry in entries)
    return ids, entries


def test_assistant_history_authorized_archive(assistant_current, real_assistant):
    """Latest three → complete read-only server history; read recovery stays local and authority-free."""
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    ids, _ = seed_assistant_activity(script)
    # A real human-only manual send must be grouped by the server as Data.
    response = page.request.post(script.path + "/requests", data={"rec_version": script.binding["rec_version"],
        "fact_id": script.draft["fact_id"]}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status == 200
    entries = page.request.get(script.path + "/activity").json()
    sent, = [entry for entry in entries if entry["type"] == "evidence_requested"]
    assert sent["group"] == "Data"
    before, calls_before = business_state(script.binding["run_id"]), len(real_assistant.calls())
    page.goto(decision_href(script.binding["case_id"], "compare"))
    latest = page.get_by_test_id("latest-history")
    latest.get_by_role("button", name="Assistant", exact=True).click()
    expect(latest.locator("ol > li")).to_have_count(3)
    for index, draft_id in enumerate(reversed(ids[-3:])):
        assert draft_id in latest.locator("ol > li").nth(index).text_content()
    assert not any(draft_id in latest.text_content() for draft_id in ids[:2])
    latest.get_by_role("button", name="Data", exact=True).click()
    expect(latest).to_contain_text("Stock check requested")
    expect(latest).not_to_contain_text("Stock check drafted")
    latest.get_by_role("button", name="Assistant", exact=True).click()
    latest.get_by_role("link", name="Show full history", exact=True).click()
    full = page.get_by_test_id("full-history")
    expect(full.get_by_role("button", name="Assistant", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(full.locator("ol > li")).to_have_count(5)
    for draft_id in ids:
        assert draft_id in full.text_content()
    first = full.locator("ol > li").first
    first.locator("summary").click()
    expect(first).to_contain_text("Read-only record")
    expect(full.get_by_role("button", name="Send request", exact=True)).to_have_count(0)
    for sentinel in ("Private answer sentinel", "private question sentinel", "Private source sentinel",
                     "Historical original draft is not shared Activity prose."):
        assert sentinel not in full.text_content()

    failure = [503]
    reads = []
    def activity_read(route):
        reads.append(route.request.method)
        if failure[0]:
            route.fulfill(status=failure[0], json={"error": "db_busy" if failure[0] == 503 else "forbidden",
                "message": "History is temporarily unavailable." if failure[0] == 503 else "History access was removed."})
        else:
            route.continue_()
    page.route("**" + script.path + "/activity", activity_read)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(full.get_by_role("alert")).to_contain_text("History is temporarily unavailable.")
    expect(full.locator("ol > li")).to_have_count(5)
    assert first.locator("details").evaluate("element => element.open")
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    failure[0] = None
    full.get_by_role("button", name="Try again", exact=True).click()
    expect(full.get_by_role("alert")).to_have_count(0)
    assert first.locator("details").evaluate("element => element.open")
    failure[0] = 403
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(full.get_by_role("alert")).to_contain_text("History access was removed.")
    expect(full.locator("ol > li")).to_have_count(0)
    assert not any(draft_id in full.text_content() for draft_id in ids)
    expect(page.get_by_role("button", name="Send for approval", exact=True)).to_be_enabled()
    assert reads == ["GET", "GET", "GET"]
    assert all(call["method"] == "GET" for call in script.calls)
    assert len(real_assistant.calls()) == calls_before and business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("count", [0, 1, 5])
def test_assistant_history_zero_one_many_and_missing_metadata(assistant_current, count):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    ids, entries = seed_assistant_activity(script, count)
    if count == 1:
        entry, = [entry for entry in entries if entry["group"] == "Assistant"]
        # A permitted legacy projection with unavailable provenance must never invent measurements.
        entry["payload"].pop("model_id")
        entry["payload"].pop("assistant_version")
        entry.update(at="", scenario_date=None)
        page.route("**" + script.path + "/activity", lambda route: route.fulfill(json=entries))
    page.goto(decision_href(script.binding["case_id"], "compare"))
    latest = page.get_by_test_id("latest-history")
    latest.get_by_role("button", name="Assistant", exact=True).click()
    expect(latest.locator("ol > li")).to_have_count(min(count, 3))
    if count == 0:
        expect(latest).to_contain_text("No assistant activity for this stock decision yet.")
        expect(latest.get_by_text("0 events", exact=True)).to_have_count(0)
    else:
        assert latest.locator("ol").evaluate("element => getComputedStyle(element).rowGap") == "8px"
        if count == 1:
            expect(latest.get_by_text("1 event", exact=True)).to_have_count(0)
            latest.locator("summary").click()
            expect(latest).to_contain_text("Model: Missing")
            expect(latest).to_contain_text("Assistant version: Missing")
            expect(latest).to_contain_text("Recorded time: Missing · Scenario date: Missing")
            expect(latest).not_to_contain_text("0 Gemini calls")
        else:
            expect(latest.get_by_text("3 events", exact=True)).to_be_visible()
    assert not any(call["method"] != "GET" for call in script.calls)
    assert all(draft_id not in latest.text_content() for draft_id in ids[:-3])


def test_assistant_history_loading_keeps_expansion_form_and_latest_response(assistant_current, real_assistant):
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    _, entries = seed_assistant_activity(script)
    before, calls_before = business_state(script.binding["run_id"]), len(real_assistant.calls())
    page.goto(decision_href(script.binding["case_id"], "compare"))
    latest = page.get_by_test_id("latest-history")
    latest.get_by_role("button", name="Assistant", exact=True).click()
    first = latest.locator("ol > li").first
    first.locator("summary").click()
    page.get_by_role("button", name="Reject stock decision…", exact=True).click()
    reason = page.get_by_label("Reason", exact=True)
    reason.fill("Unsent manual review note must survive a history refetch.")
    pending = []
    def activity_read(route):
        pending.append(route)
        if len(pending) == 2:
            route.fulfill(json=entries)
    page.route("**" + script.path + "/activity", activity_read)
    with page.expect_request("**" + script.path + "/activity"):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(latest.locator("ol > li")).to_have_count(3)
    assert first.locator("details").evaluate("element => element.open")
    expect(reason).to_have_value("Unsent manual review note must survive a history refetch.")
    expect(reason).to_be_focused()
    with page.expect_response("**" + script.path + "/activity"):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    assert len(pending) == 2
    # The older denial arrives last. The inherited load counter must ignore it.
    with page.expect_response("**" + script.path + "/activity"):
        pending[0].fulfill(status=403, json={"error": "forbidden", "message": "Obsolete history denial."})
    expect(latest.get_by_role("alert")).to_have_count(0)
    expect(latest.locator("ol > li")).to_have_count(3)
    assert first.locator("details").evaluate("element => element.open")
    expect(reason).to_have_value("Unsent manual review note must survive a history refetch.")
    assert len(real_assistant.calls()) == calls_before and all(call["method"] == "GET" for call in script.calls)
    assert business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("width", [375, 390, 1280])
def test_assistant_archive_long_details_wrap_without_losing_ids(assistant_current, width):
    from api import db
    from tests.web.test_case import decision_href

    script, page = assistant_current, assistant_current.page
    ids, _ = seed_assistant_activity(script, 1)
    model = "model-" + "LongAuthorizedModelIdentifier" * 25
    with db_tx() as conn:
        db.run(conn, "UPDATE evidence_drafts SET model_id = :model WHERE run_id = :r AND draft_id = :id",
            model=model, r=script.binding["run_id"], id=ids[0])
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(decision_href(script.binding["case_id"], "archive") + "&history=Assistant")
    full = page.get_by_test_id("full-history")
    expect(full.locator("ol > li")).to_have_count(1)
    disclosure = full.locator("summary")
    expect(disclosure).to_contain_text("Show full event")
    expect(full.get_by_text(model, exact=False)).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    disclosure.focus()
    disclosure.press("Enter")
    expect(full.locator("details > div")).to_contain_text(model)
    expect(full.locator("details > div")).to_contain_text(ids[0])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert full.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    for action in ("Send request", "Apply assumptions", "Approve plan", "Confirm count"):
        expect(full.get_by_role("button", name=action, exact=True)).to_have_count(0)
    page.screenshot(path=f"/tmp/p09-21-history-{width}.png")


# 09-20: append-only evidence review and receiving-Tasks acceptance.

def open_evidence_requests(page):
    region = page.get_by_role("region", name="Stock checks", exact=True)
    expect(region).to_be_visible()
    control = region.get_by_role("button", name="Review stock checks", exact=True)
    if control.count():
        control.click()
    return region

def real_evidence_drafts(page, assistant_api_binding, *, all_checks=False):
    """The actual bound CASE draft tool writes checked originals; no model/provider is called."""
    import subprocess

    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    detail, session = case_json(page, cid), session_json(page)
    listing = page.request.get(f"/api/cases/{cid}/drafts").json()
    facts = listing["attention"] if all_checks else listing["attention"][:1]
    assert facts, "the seeded workflow needs an eligible engine check"
    ctx = {"scope": "CASE", "run_id": session["run_id"], "case_id": cid,
           "rec_version": detail["rec_version"], "plan_key": listing["plan_key"],
           "actor": {"role": session["role"], "branch": session["branch"], "account": session["account"]},
           "model_id": TEST_MODEL, "assistant_version": "offline-test"}
    before = business_state(session["run_id"])
    script = '''
import asyncio, json, sys
from types import SimpleNamespace
from agent import tools
ctx, facts = json.loads(sys.argv[1])
for fact in facts:
    made = asyncio.run(tools.draft_evidence_request(fact["fact_id"],
        "Please check this flagged fact.\\nReport the information you observe.",
        tool_context=SimpleNamespace(state={"ctx": ctx})))
    assert made.get("draft_id"), made
'''
    result = subprocess.run([str(assistant_api_binding.python), "-c", script, json.dumps([ctx, facts])],
        cwd=assistant_api_binding.root, env={**os.environ, "GCP": "off", "DATABASE_URL": config.DATABASE_URL},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-4000:]
    assert business_state(session["run_id"]) == before, "drafting has no business authority"
    listing = page.request.get(f"/api/cases/{cid}/drafts").json()
    assert listing["requests_left"] > 0 and listing["drafts"]
    return detail, session, listing


def test_draft_requires_human_send(visitor, real_assistant, assistant_api_binding):
    from tests.web.test_case import decision_href

    page = visitor.page
    detail, session, listing = real_evidence_drafts(page, assistant_api_binding)
    draft = listing["drafts"][0]
    before = business_state(session["run_id"])
    page.goto(decision_href(detail["case_id"], "compare"))
    expect(page.get_by_role("region", name="AI brief", exact=True)).to_contain_text("Live")
    drafts = open_evidence_requests(page)
    expect(drafts).to_be_visible()
    row = drafts.locator(f'[data-draft-id="{draft["draft_id"]}"]')
    expect(row).to_contain_text(draft["class"])
    expect(row).to_contain_text(f'Sends: {detail["source"]} manager')
    expect(row).to_contain_text(f'Checks: {draft["to_branch"]} manager')
    row.get_by_role("button", name="Review draft", exact=True).click()
    message = row.get_by_role("textbox", name="Message", exact=True)
    expect(message).to_have_value(draft["original_text"])
    final = "  Manager wording: please inspect what is there.\nKeep this second line exactly.  "
    message.fill(final)
    expect(row).to_contain_text("Human-authored message")
    expect(row.get_by_test_id("draft-request-context")).to_contain_text(draft["sku"])
    for required in draft["request_context"]["required_parts"]:
        expect(row.get_by_test_id("draft-request-context")).to_contain_text(required)
    expect(row).to_contain_text("The response deadline starts when you send the request.")
    expect(row.get_by_test_id("sent-deadline")).to_have_count(0)
    assert business_state(session["run_id"]) == before
    pending = []
    page.route(f'**/api/cases/{detail["case_id"]}/requests', lambda route: pending.append(route))
    send = row.get_by_role("button", name="Send request", exact=True)
    send.click()
    expect(send).to_be_disabled()
    expect(send).to_have_text("Send request")
    expect(row.get_by_text("Request sent", exact=True)).to_have_count(0)
    assert len(pending) == 1
    assert json.loads(pending[0].request.post_data) == {"draft_id": draft["draft_id"],
        "rec_version": draft["rec_version"], "plan_key": draft["plan_key"], "message": final}
    assert pending[0].request.headers.get("idempotency-key")
    assert business_state(session["run_id"]) == before
    calls = real_assistant.calls()
    pending[0].continue_()
    expect(row.get_by_text("Request sent", exact=True)).to_be_visible()
    expect(row.get_by_role("link", name="View request", exact=True)).to_be_visible()
    sent, = case_json(page, detail["case_id"])["requests"]
    assert sent["message"] == final and sent["draft_id"] == draft["draft_id"]
    assert sent["request_context"] == draft["request_context"]
    assert dt.datetime.fromisoformat(sent["due_at"]) - dt.datetime.fromisoformat(sent["sent_at"]) == dt.timedelta(minutes=60)
    expect(row.get_by_test_id("sent-deadline")).to_contain_text("Respond by")
    after = page.request.get(f'/api/cases/{detail["case_id"]}/drafts').json()
    assert after["requests_left"] == listing["requests_left"] - 1
    current = business_state(session["run_id"])
    assert {k: v for k, v in current.items() if k != "evidence_requests"} == {
        k: v for k, v in before.items() if k != "evidence_requests"}
    assert real_assistant.calls() == calls, "human Send spends no model call"


def eligible_evidence_script(script):
    """Add the real draft-read eligibility fields to the older transport fixture."""
    script.draft.update(owner=script.draft["owner"]["branch"], can_send=True, read_only=False,
                        reason="This fact must be confirmed before the plan can proceed.", next_action="review_draft")
    script.draft["request_context"].update(fact_id=script.draft["fact_id"], **{"class": script.draft["class"]})
    return script


def show_evidence_script(script, *, drafts=None, reason=None, attention=None):
    payload = {"rec_version": script.current_binding["rec_version"], "plan_key": script.current_binding["plan_key"],
               "drafts": drafts if drafts is not None else [script.draft], "requests_left": script.requests_left,
               "reason": reason, "attention": attention or []}
    script.page.route("**" + script.path + "/drafts", lambda route: route.fulfill(json=payload))
    return payload


@pytest.mark.parametrize("state", ["no-checks", "no-draft"])
def test_evidence_empty_states_keep_manual_checks(assistant_edited_draft, state):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    reason = "The source manager can review the checks and request a check manually."
    show_evidence_script(script, drafts=[], reason=reason)
    if state == "no-checks":
        detail = case_json(page, script.binding["case_id"])
        detail["fact_checks"] = []
        page.route("**" + script.path, lambda route: route.fulfill(json=detail))
    before = business_state(script.binding["run_id"])
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    expect(region).to_contain_text("The engine flagged no suspect facts for this version." if state == "no-checks"
                                   else "No request drafted. " + reason)
    expect(region.get_by_role("link", name="Open fact checks", exact=True)).to_be_visible()
    expect(region.get_by_test_id("evidence-drafts-list")).to_have_count(0)
    expect(region.get_by_role("button", name="Send request", exact=True)).to_have_count(0)
    assert business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("width", [375, 390, 768, 1280])
def test_evidence_many_rows_wrap_and_context_opens_the_same_editor(assistant_edited_draft, width):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    rows = [script.draft | {"draft_id": script.draft["draft_id"] + f"-{i}",
            "class": "Required" if i < 9 else "Advisory"} for i in range(18)]
    for row in rows:
        row["request_context"] = row["request_context"] | {"class": row["class"]}
    rows[0]["original_text"] = "Long checked wording " + "LongUnbrokenManagerReference" * 90
    show_evidence_script(script, drafts=rows)
    before = business_state(script.binding["run_id"])
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    items = region.locator("[data-draft-id]")
    expect(items).to_have_count(18)
    assert [item.split(" · ")[0] for item in items.locator("p.font-semibold").all_text_contents()] == ["Required"] * 9 + ["Advisory"] * 9
    items.first.get_by_role("button", name="Review draft", exact=True).click()
    message = items.first.get_by_role("textbox", name="Message", exact=True)
    message.fill("Keep my unsent wording.\n" + "LongHumanReference" * 100)
    expect(region.get_by_role("textbox", name="Message", exact=True)).to_have_count(1)
    items.first.locator("summary").click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    editor = items.first.locator("[id^=draft-editor-]")
    assert editor.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    items.first.get_by_role("button", name="Close draft", exact=True).click()
    expect(region.get_by_role("group", name="Discard unsent edits")).to_be_visible()
    region.get_by_role("button", name="Keep editing", exact=True).click()
    expect(message).to_have_value("Keep my unsent wording.\n" + "LongHumanReference" * 100)
    region.get_by_role("link", name="Open fact checks", exact=True).click()
    expect(region.get_by_role("group", name="Discard unsent edits")).to_be_visible()
    region.get_by_role("button", name="Discard edits", exact=True).click()
    facts = page.get_by_test_id("fact-checks")
    facts.get_by_role("button", name="Review evidence drafts", exact=True).click()
    link = facts.get_by_role("link", name="Review draft", exact=True).first
    expect(link).to_have_attribute("href", decision_href(script.binding["case_id"], "compare").removeprefix("/") + "&draft=" + rows[0]["draft_id"])
    link.click()
    expect(open_evidence_requests(page).get_by_role("textbox", name="Message", exact=True)).to_have_value(rows[0]["original_text"])
    assert business_state(script.binding["run_id"]) == before
    page.screenshot(path=f"/tmp/p09-20-drafts-{width}.png")


@pytest.mark.parametrize("status,code", [(403, "forbidden"), (409, "stale_plan"), (409, "stale_version"),
                                        (429, "too_many_requests"), (503, "db_busy"), (0, "network")])
def test_evidence_send_refusal_retains_edits_binding_focus_and_retry(assistant_edited_draft, status, code):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    show_evidence_script(script)
    before = business_state(script.binding["run_id"])
    attempts = []
    def refuse(route):
        attempts.append({"body": json.loads(route.request.post_data), "key": route.request.headers["idempotency-key"]})
        if not status:
            route.abort("failed")
        else:
            route.fulfill(status=status, json={"error": code, "message": "Exact local refusal: " + code})
    page.route("**" + script.path + "/requests", refuse)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    region.get_by_role("button", name="Review draft", exact=True).click()
    field = region.get_by_role("textbox", name="Message", exact=True)
    value = "My review wording\nStays attached to the original fact."
    field.fill(value)
    region.get_by_role("button", name="Send request", exact=True).click()
    expect(region.get_by_role("alert")).to_contain_text("Exact local refusal: " + code if status else "Could not reach")
    expect(field).to_have_value(value)
    expect(field).to_be_focused()
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(field).to_have_value(value)
    assert attempts[0]["body"] == {"draft_id": script.draft["draft_id"], "rec_version": script.binding["rec_version"],
                                   "plan_key": script.binding["plan_key"], "message": value}
    if code in ("stale_plan", "stale_version"):
        expect(field).to_have_attribute("readonly", "")
        expect(region.get_by_role("button", name="Send request", exact=True)).to_be_disabled()
        expect(region.get_by_role("link", name="Review current checks", exact=True)).to_be_visible()
    elif code in ("db_busy", "network"):
        region.get_by_role("button", name="Try again", exact=True).click()
        expect(region.get_by_role("button", name="Try again", exact=True)).to_be_enabled()
        assert len(attempts) == 2 and attempts[1] == attempts[0]
        field.fill(value + "\nAn explicit edit.")
        region.get_by_role("button", name="Send request", exact=True).click()
        expect(region.get_by_role("button", name="Try again", exact=True)).to_be_enabled()
        assert len(attempts) == 3 and attempts[2]["key"] != attempts[0]["key"]
    else:
        expect(region.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    assert business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("condition", ["empty", "missing-context", "allowance", "existing", "wrong-role"])
def test_evidence_send_requires_message_context_allowance_and_owner(assistant_edited_draft, condition):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    if condition == "missing-context":
        script.draft["request_context"]["required_parts"] = []
    elif condition == "allowance":
        script.requests_left = 0
        script.draft.update(can_send=False, read_only=True, reason="No request slots remain for this version. Review current checks and the available stock decision actions.")
    elif condition == "existing":
        script.draft.update(request_id="REQ-existing", can_send=False, read_only=True, reason="A request already exists for this fact.")
    elif condition == "wrong-role":
        script.draft.update(can_send=False, read_only=True, reason="Only the source manager can send this request.")
    show_evidence_script(script)
    before = business_state(script.binding["run_id"])
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    if condition == "existing":
        expect(region.get_by_role("link", name="View request", exact=True)).to_be_visible()
        expect(region.get_by_role("button", name="Send request", exact=True)).to_have_count(0)
    else:
        region.get_by_role("button", name="Review draft", exact=True).click()
        if condition == "empty":
            region.get_by_role("textbox", name="Message", exact=True).fill(" \n ")
            expect(region).to_contain_text("Enter a request message.")
        elif condition == "missing-context":
            expect(region).to_contain_text("Required request context is missing.")
        else:
            expect(region).to_contain_text(script.draft["reason"])
        expect(region.get_by_role("button", name="Send request", exact=True)).to_be_disabled()
    expect(region.get_by_role("link", name="Open fact checks", exact=True)).to_be_visible()
    assert business_state(script.binding["run_id"]) == before


@pytest.mark.parametrize("axis", ["plan", "version"])
def test_evidence_original_binding_retained_and_late_send_ignored(assistant_edited_draft, axis):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    detail = case_json(page, script.binding["case_id"])
    payload = show_evidence_script(script)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    region.get_by_role("button", name="Review draft", exact=True).click()
    field = region.get_by_role("textbox", name="Message", exact=True)
    field.fill("Keep this original draft review.\nDo not rebind my edits.")
    pending = []
    page.route("**" + script.path + "/requests", lambda route: pending.append(route))
    region.get_by_role("button", name="Send request", exact=True).click()
    expect(region.get_by_role("button", name="Send request", exact=True)).to_be_disabled()
    assert len(pending) == 1
    changed = copy.deepcopy(detail)
    if axis == "plan":
        changed["selected_plan"] = {"plan_key": "keep", "rec_version": detail["rec_version"], "label": "Keep"}
        payload["plan_key"] = "keep"
    else:
        changed["rec_version"] = "new-reviewed-version"
        payload["rec_version"] = changed["rec_version"]
    page.route("**" + script.path, lambda route: route.fulfill(json=changed))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(region).to_contain_text("Plan changed · review current checks." if axis == "plan" else "Draft belongs to an older version.")
    expect(field).to_have_value("Keep this original draft review.\nDo not rebind my edits.")
    expect(field).to_have_attribute("readonly", "")
    pending[0].fulfill(json={"request_id": "REQ-late", "message": "late success", "due_at": "2026-10-18T08:00:00Z"})
    expect(region.get_by_role("button", name="Send request", exact=True)).to_be_disabled()
    expect(region.get_by_text("Request sent", exact=True)).to_have_count(0)
    assert script.binding["rec_version"] in region.text_content()


def test_evidence_and_question_edits_survive_activity_read_failure(assistant_edited_draft):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    show_evidence_script(script)
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    region.get_by_role("button", name="Review draft", exact=True).click()
    region.get_by_role("textbox", name="Message", exact=True).fill("Unsent evidence message")
    question = page.get_by_role("textbox", name="Your question", exact=True)
    question.fill("My unsent personal question")
    page.route("**" + script.path + "/activity", lambda route: route.fulfill(status=503, json={"error": "db_busy", "message": "Activity read unavailable"}))
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(page.get_by_text("Activity read unavailable", exact=True)).to_be_visible()
    expect(region.get_by_role("textbox", name="Message", exact=True)).to_have_value("Unsent evidence message")
    expect(question).to_have_value("My unsent personal question")
    expect(page.get_by_role("button", name="Ask question", exact=True)).to_be_enabled()


def test_evidence_refreshes_when_case_brief_finishes(assistant_edited_draft):
    from tests.web.test_case import decision_href

    script = eligible_evidence_script(assistant_edited_draft)
    page = script.page
    payload = show_evidence_script(script, drafts=[])
    pending = []
    page.route("**" + script.path + "/brief", lambda route: pending.append(route))
    before = business_state(script.binding["run_id"])
    page.goto(decision_href(script.binding["case_id"], "compare"))
    region = open_evidence_requests(page)
    expect(region).to_contain_text("No request drafted.")
    payload["drafts"] = [script.draft]
    assert len(pending) == 1
    pending[0].fulfill(content_type="text/event-stream", body=sse("brief", script.output))
    expect(region.locator("[data-draft-id]")).to_have_count(1)
    assert business_state(script.binding["run_id"]) == before


def human_sent_request(page, text, *, origin="manual", assistant_api_binding=None):
    """Set up the existing human request service, retaining its exact returned context."""
    if origin == "draft":
        detail, session, listing = real_evidence_drafts(page, assistant_api_binding)
        body = {"draft_id": listing["drafts"][0]["draft_id"], "rec_version": detail["rec_version"],
                "plan_key": listing["plan_key"], "message": text}
    else:
        cid = case_id_by_tag(page, STORY)
        take_ownership(page, cid)
        detail, session = case_json(page, cid), session_json(page)
        listing = page.request.get(f"/api/cases/{cid}/drafts").json()
        body = {"fact_id": listing["attention"][0]["fact_id"], "rec_version": detail["rec_version"],
                "plan_key": listing["plan_key"]}
        if text is not None:
            body["message"] = text
    before = business_state(session["run_id"])
    response = page.request.post(f'/api/cases/{detail["case_id"]}/requests', data=body,
                                 headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status == 200, response.text()
    request = response.json()
    after = business_state(session["run_id"])
    assert {k: v for k, v in before.items() if k != "evidence_requests"} == {
        k: v for k, v in after.items() if k != "evidence_requests"}
    assert len(after["evidence_requests"]) == len(before["evidence_requests"]) + 1
    assert request["message"] == text
    return detail, session, request


def receiving_tasks(page, request):
    from tests.web.conftest import switch_seat

    if session_json(page)["branch"] != request["to_branch"]:
        switch_seat(page, request["to_branch"])
    page.goto("/#/tasks")
    row = page.locator(f'article[data-request-id="{request["request_id"]}"]')
    expect(row).to_be_visible()
    toggle = row.get_by_role("heading", level=3).get_by_role("button")
    if toggle.get_attribute("aria-expanded") == "false":
        toggle.click()  # 07-19: a Tasks row opens to show the request with its sent message (09-20)
    return row


def test_tasks_preserves_sent_message(visitor, real_assistant):
    page = visitor.page
    final = "  Manager wording: inspect the shelf and backroom.\n\nKeep every line and both trailing spaces.  "
    detail, session, request = human_sent_request(page, final)
    before = business_state(session["run_id"])
    calls = real_assistant.calls()
    row = receiving_tasks(page, request)
    message = row.get_by_test_id("sent-message")
    expect(message).to_be_visible()
    assert message.text_content() == final
    assert message.evaluate("el => getComputedStyle(el).whiteSpace") == "pre-wrap"
    expect(row).to_contain_text(f'Message from {detail["source"]} manager')
    expect(row).to_contain_text("Human-authored message")
    context = row.get_by_role("region", name="Request context", exact=True)
    expect(context).to_contain_text(request["request_context"]["sku"])
    for lot in request["lot_ids"]:
        expect(context).to_contain_text(lot)
    for part in request["request_context"]["required_parts"]:
        expect(context).to_contain_text(part)
    assert message.evaluate("(el, context) => !!(el.compareDocumentPosition(context) & Node.DOCUMENT_POSITION_FOLLOWING)", context.element_handle())
    expect(row.get_by_test_id("due-line")).to_have_count(0)  # a real deadline is in the server's sentence, never a planned line (07 D-05)
    expect(row.get_by_role("button", name="Send answer", exact=True)).to_be_enabled()
    assert business_state(session["run_id"]) == before
    assert real_assistant.calls() == calls, "receiving Tasks has no model or business authority"


@pytest.mark.parametrize("origin,width", [("manual", 1280), ("draft", 375)])
def test_tasks_message_provenance_safe_text_and_existing_answer(visitor, real_assistant, assistant_api_binding, origin, width):
    page = visitor.page
    final = '  Please report only what you observe.\n<img src=x onerror="window.messageExecuted=true">\n' + "LongManagerReference" * 80 + "  "
    _, session, request = human_sent_request(page, final, origin=origin, assistant_api_binding=assistant_api_binding)
    row = receiving_tasks(page, request)
    # The shared seat-switch helper checks desktop navigation before this view.
    page.set_viewport_size({"width": width, "height": 844})
    assert row.get_by_test_id("sent-message").text_content() == final
    expect(row.locator("img")).to_have_count(0)
    assert not page.evaluate("!!window.messageExecuted")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    original = row.get_by_test_id("sent-original-draft")
    if origin == "draft":
        expect(original).not_to_be_visible()
        row.locator("summary").click()
        expect(original).to_be_visible()
        assert original.text_content() == request["original_text"] and request["original_text"] != final
        expect(row).to_contain_text("Read-only original")
    else:
        expect(original).to_have_count(0)
    for lot_id in request["lot_ids"]:
        lot = row.get_by_role("group", name=f"Lot {lot_id}", exact=True)
        lot.get_by_role("spinbutton", name="Shelf", exact=True).fill("0")
        lot.get_by_role("spinbutton", name="Backroom", exact=True).fill("0")
    reply = "Observed count entered by the receiving manager."
    row.get_by_role("textbox", name="Reply", exact=True).fill(reply)
    calls = real_assistant.calls()
    expect(row.get_by_role("button", name="Send answer", exact=True)).to_be_enabled()
    before_answer = business_state(session["run_id"])
    # The API requires observed; 09-17's form integration is deferred. Seed a
    # real answer here to verify this additive reader preserves it.
    answer = page.request.post(f'/api/requests/{request["request_id"]}/answer', multipart={
        "text": reply, "observed": "now", "counts": json.dumps({
            lot_id: {"shelf_units": 0, "backroom_units": 0} for lot_id in request["lot_ids"]})},
        headers={"Idempotency-Key": str(uuid.uuid4())})
    assert answer.status == 200, answer.text()
    page.reload()
    row = receiving_tasks(page, request)  # the reload closes the row again (07-19)
    expect(row.get_by_test_id("response-summary")).to_contain_text(reply)
    expect(row.get_by_role("button", name="Confirm count", exact=True)).to_be_disabled()
    assert row.get_by_test_id("sent-message").text_content() == final
    persisted, = page.request.get("/api/requests").json()
    assert persisted["message"] == final and persisted["request_context"] == request["request_context"]
    state = business_state(session["run_id"])
    assert {k: v for k, v in state.items() if k not in ("evidence_requests", "signals")} == {
        k: v for k, v in before_answer.items() if k not in ("evidence_requests", "signals")
    }, "an answer confirms and approves nothing; seeded decisions remain unchanged"
    assert real_assistant.calls() == calls
    page.screenshot(path=f"/tmp/p09-20-tasks-{origin}-{width}.png")


def test_tasks_manual_request_without_custom_message_keeps_instructions(visitor):
    page = visitor.page
    _, _, request = human_sent_request(page, None)
    row = receiving_tasks(page, request)
    expect(row.get_by_text(request["ask_text"], exact=True)).to_be_visible()
    expect(row.get_by_test_id("sent-message")).to_have_count(0)
    expect(row.get_by_test_id("sent-original-draft")).to_have_count(0)
    expect(row.get_by_role("button", name="Send answer", exact=True)).to_be_enabled()


@pytest.mark.parametrize("width", [390, 768])
def test_tasks_real_printed_expiry_context_is_separate_from_human_message(visitor, real_assistant, assistant_api_binding, width):
    import subprocess

    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    detail, session = case_json(page, cid), session_json(page)
    # The existing API expiry fixture's setup: own this visitor's series, mark
    # its recorded expiry estimated, then let the real engine flag the check.
    script = '''
import json, sys
from api import cases, db, overlay
run_id, cid, branch, sku, lot_id = json.loads(sys.argv[1])
with db.tx() as conn:
    run = db.lock_run(conn, run_id, mode="UPDATE")
    overlay.materialize(conn, run, "shelfshift", branch, sku)
    db.run(conn, "UPDATE lots SET expiry_source = 'ESTIMATED' WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l", r=run_id, l=lot_id)
    cases.evaluate_case(conn, run, cases.case_row(conn, run_id, cid, lock=True))
'''
    setup = subprocess.run([str(assistant_api_binding.python), "-c", script,
        json.dumps([session["run_id"], cid, detail["source"], detail["sku"], detail["lot"]["lot_id"]])],
        cwd=assistant_api_binding.root, env={**os.environ, "GCP": "off", "DATABASE_URL": config.DATABASE_URL},
        capture_output=True, text=True, timeout=40)
    assert setup.returncode == 0, setup.stderr[-4000:]
    listing = page.request.get(f"/api/cases/{cid}/drafts").json()
    fact = next(f for f in listing["attention"] if f["kind"] == "ESTIMATED_EXPIRY")
    final = "Please read the printed date on each specified lot.\nKeep each lot separate."
    response = page.request.post(f"/api/cases/{cid}/requests", data={"rec_version": listing["rec_version"],
        "plan_key": listing["plan_key"], "fact_id": fact["fact_id"], "message": final},
        headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status == 200, response.text()
    request = response.json()
    assert request["request_context"]["required_parts"] == ["printed date per lot"]
    before = business_state(session["run_id"])
    calls = real_assistant.calls()
    page.set_viewport_size({"width": width, "height": 844})
    row = receiving_tasks(page, request)
    assert row.get_by_test_id("sent-message").text_content() == final
    context = row.get_by_role("region", name="Request context", exact=True)
    expect(context).to_contain_text("printed date per lot")
    for lot in request["lot_ids"]:
        expect(context).to_contain_text(lot)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert business_state(session["run_id"]) == before
    assert real_assistant.calls() == calls


@pytest.mark.parametrize("role", ["checking-branch", "hq"])
def test_evidence_real_other_branch_and_hq_cannot_send(visitor, real_assistant, assistant_api_binding, role):
    from tests.web.conftest import switch_seat
    from tests.web.test_case import decision_href

    page = visitor.page
    detail, session, listing = real_evidence_drafts(page, assistant_api_binding)
    draft = listing["drafts"][0]
    switch_seat(page, draft["to_branch"] if role == "checking-branch" else None)
    before = business_state(session["run_id"])
    page.goto(decision_href(detail["case_id"], "compare"))
    region = open_evidence_requests(page)
    region.get_by_role("button", name="Review draft", exact=True).click()
    expect(region.get_by_role("textbox", name="Message", exact=True)).to_have_attribute("readonly", "")
    expect(region.get_by_role("button", name="Send request", exact=True)).to_be_disabled()
    expect(region).to_contain_text("Only the " + detail["source"] + " manager can send this request.")
    assert business_state(session["run_id"]) == before


def test_tasks_unedited_original_keeps_gemini_provenance(visitor, real_assistant, assistant_api_binding):
    page = visitor.page
    detail, _, listing = real_evidence_drafts(page, assistant_api_binding)
    draft = listing["drafts"][0]
    response = page.request.post(f'/api/cases/{detail["case_id"]}/requests',
        data={"draft_id": draft["draft_id"], "rec_version": draft["rec_version"], "plan_key": draft["plan_key"],
              "message": draft["original_text"]}, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert response.status == 200, response.text()
    row = receiving_tasks(page, response.json())
    assert row.get_by_test_id("sent-message").text_content() == draft["original_text"]
    expect(row).to_contain_text("Drafted by Gemini · Sent by manager")
    expect(row.get_by_text("Human-authored message", exact=True)).to_have_count(0)
