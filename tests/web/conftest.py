"""The browser harness (05-CONTEXT D-09; research Pattern 9): a production build of web/ under `vite preview`, proxied
to a real API child on the browser suite's own _test database, opened by headless Chromium. Assistant-only scripted
transport is opt-in from tests/web/test_assistant.py; no product setting enables it. All other API paths stay real.

Order: `web_database` refuses any database whose name does not end in _web_test first (review WR-09: a bare
`pytest` collects tests/api first, whose conftest points DATABASE_URL at Phase 4's shelfshift_test), then Phase 4's
`schema` fixture runs
(it refuses a database whose name does not end in _test and refuses Cloud SQL before anything is dropped), then `template` builds the Day-3 template, then the API child and the preview child
start on controlled ports (an occupied port fails setup naming it), each polled until it is ready. Both children stop
before the session's fixtures are torn down. Both credentials (the store passcode and HQ's own, 06.1 D-47) are random
per run and held only in memory (`repr=False` fields, booleans in every check): never written to a file, a log, a test
name, an assertion message or a commit, and only our API child accepts them, so a passed sign-in proves the browser
reached our server.

Signing in (06.1 D-02, D-47): `sign_in` drives the sign-in card the way a person does; `pass_gate` and `switch_seat`
keep their names for the older tests and now sign in (RESEARCH Pitfall 2).

`make test-web` runs this suite in its own pytest invocation, never together with tests/api, so the schema is dropped
once per run.
"""
from __future__ import annotations

import os

# Before anything imports api.config: a bare `pytest tests/web` must never fall back to Phase 4's shelfshift_test.
os.environ.setdefault("DATABASE_URL", "postgresql+pg8000://postgres@localhost:5432/shelfshift_web_test")

import dataclasses  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
import uuid  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from playwright.sync_api import Browser, BrowserContext, Page, expect  # noqa: E402

from tests.api.conftest import first_store, require_test_database, template  # noqa: E402, F401  (never edited)
from tests.api.conftest import TINY  # noqa: E402  (the tiny public set, 05.2-22)
from tests.api.conftest import schema as api_schema  # noqa: E402, F401  (wrapped below by `schema`, never edited)
from api import config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]  # the ShelfShift repo
WEB = ROOT / "web"
WEB_DATABASE = "shelfshift_web_test"  # the browser suite's own database (05-03, D-09): the default
WEB_SUFFIX = "_web_test"  # the only names accepted: a branch may test on its own *_web_test database (07 D-29)
HOST = "127.0.0.1"
API_PORT = int(os.environ.get("SS_WEB_API_PORT", "18080"))
PREVIEW_PORT = int(os.environ.get("SS_WEB_PREVIEW_PORT", "14173"))
READY_S = 90  # how long each child may take to answer its readiness check
STOP_S = 10  # how long a child may take to stop before it is killed
CARRIED_ENV = ("PATH", "HOME", "LANG", "TMPDIR")  # the only variables a child inherits: no GOOGLE_*, no CLOUD_SQL_*
THIRD_PARTY_FONTS = ("https://fonts.googleapis.com/**", "https://fonts.gstatic.com/**")
HQ = "HQ"  # the HQ planner's account (04-API-CONTRACT "Product mode (06.1)", POST /api/session)
SIGNED_IN_S = 30  # how long a sign-in or a seat switch may take before the session reports the account
TEST_MODEL = "offline-browser-model"  # test-only configured identity, never a paid model or a product default

# Engine-backed steps (Next day, confirm, settings) take seconds; Playwright's 5 s default is too short (Pitfall 5).
expect.set_options(timeout=30_000)


@dataclasses.dataclass(frozen=True)
class Web:
    url: str  # the preview origin the browser opens
    api_url: str  # the API child's own origin
    store: str  # the default store manager account: the ACTIVE template's start store (tests.api first_store)
    passcode: str = dataclasses.field(repr=False)  # the store passcode; never shown in a repr or a failure message
    hq_passcode: str = dataclasses.field(repr=False)  # HQ's own credential (D-47, D-64); never shown either


# The running harness's Web, for helpers whose signature names no Web (switch_seat, kept for 05.2's tests). Set by the
# `web` fixture for the session and cleared at its end; it holds the same object, so nothing new is printed or stored.
_RUNNING: list[Web] = []


@dataclasses.dataclass
class Visitor:
    context: BrowserContext
    page: Page


@dataclasses.dataclass(frozen=True)
class ApiBinding:
    root: pathlib.Path
    python: pathlib.Path


def _api_binding(value: str | None = None) -> ApiBinding:
    """09-INTEGRATION: preview stays here; an explicit API root may only be the supervisor's build/assistant.

    Explicit roots are validated before schema setup. The unset default preserves the original harness: current
    interpreter, this root, no git subprocess or local-venv prerequisite (including source archives).
    """
    if value is None:
        return ApiBinding(ROOT, pathlib.Path(sys.executable))
    root = pathlib.Path(value).resolve()
    if root not in (ROOT, (ROOT.parent / "ShelfShift-assistant").resolve()):
        raise RuntimeError("ASSISTANT_API_WORKTREE must be the integrated web root or bound ShelfShift-assistant")
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE).strip()
    try:
        if pathlib.Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            raise RuntimeError("ASSISTANT_API_WORKTREE must name a tracked repository root")
        git("ls-files", "--error-unmatch", "--", "api/app.py", "api/schema.sql", "api/config.py",
            "tests/api/conftest.py", "requirements.lock")
        if root != ROOT:
            if git("branch", "--show-current") != "build/assistant":
                raise RuntimeError("ASSISTANT_API_WORKTREE must be on bound branch build/assistant")
            common = pathlib.Path(git("rev-parse", "--git-common-dir"))
            own = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"], text=True).strip()
            if (root / common).resolve() != (ROOT / own).resolve():
                raise RuntimeError("ASSISTANT_API_WORKTREE must share the bound repository")
    except (subprocess.CalledProcessError, OSError) as error:
        raise RuntimeError("ASSISTANT_API_WORKTREE is not a tracked, bound API worktree") from error
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("the bound API needs its own existing .venv/bin/python; no install runs in this harness")
    return ApiBinding(root, python)


@pytest.fixture(scope="session")
def assistant_api_binding() -> ApiBinding:
    return _api_binding(os.environ.get("ASSISTANT_API_WORKTREE"))


def _child_env(**extra: str) -> dict[str, str]:
    return {k: os.environ[k] for k in CARRIED_ENV if k in os.environ} | extra


def _occupied(port: int) -> bool:
    """True when something already answers on, or holds, 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((HOST, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound:
        bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            bound.bind((HOST, port))
        except OSError:
            return True
    return False


def _tail(path: pathlib.Path, lines: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def _wait_ready(what: str, url: str, ready, child: subprocess.Popen, log: pathlib.Path) -> None:
    """Poll `url` until `ready(response)` holds; fail with the child's log tail if it exits or the time runs out."""
    deadline = time.monotonic() + READY_S
    with httpx.Client(trust_env=False, timeout=2.0) as http:
        while time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(f"{what} exited with code {child.returncode} before it was ready:\n{_tail(log)}")
            try:
                if ready(http.get(url)):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
    pytest.fail(f"{what} was not ready at {url} within {READY_S} s:\n{_tail(log)}")


def _stop(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(STOP_S)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(STOP_S)


def _json_is(expected: dict):
    def ready(r: httpx.Response) -> bool:
        try:
            return r.status_code == 200 and r.json() == expected
        except ValueError:
            return False
    return ready


def _api_ready(model_id: str):
    """Require configured identity for an assistant-capable API without a model ping; accept additive metadata."""
    def ready(response: httpx.Response) -> bool:
        try:
            body = response.json()
            return (response.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
                    and body.get("db") == "ok" and body.get("model_id") == model_id)
        except ValueError:
            return False
    return ready


def _api_readiness(binding: ApiBinding):
    if binding.root == ROOT and not (ROOT / "agent" / "model.py").is_file():
        return _json_is({"ok": True, "db": "ok"})
    return _api_ready(TEST_MODEL)


def _bound_fixture(binding: ApiBinding, name: str, *args):
    """Run the bound tracked schema/template fixture with its own interpreter, never copy SQL into this root."""
    script = '''import json, sys
from api import config
from sqlalchemy.engine import make_url
assert (make_url(config.DATABASE_URL).database or "").endswith("_web_test")
assert make_url(config.DATABASE_URL).host in ("localhost", "127.0.0.1", "::1")
assert not config.CLOUD_SQL_INSTANCE
from tests.api import conftest as fixtures
name, args = sys.argv[1], json.loads(sys.argv[2])
if name == "public_template":
    from api import refdata, sandbox
    sandbox.clear_cache()
    result = sandbox.build_template(refdata.install(fixtures.TINY, allow_local=True))
else:
    assert name in ("schema", "template")
    result = getattr(fixtures, name).__wrapped__(*args)
print("SS_WEB_FIXTURE:" + json.dumps(result))
'''
    result = subprocess.run([str(binding.python), "-c", script, name, json.dumps(args)], cwd=binding.root,
        env=_child_env(GCP="off", DATABASE_URL=config.DATABASE_URL, ENGINE_WORKER="off", DEMO_MODE="on"),
        capture_output=True, text=True, timeout=120)
    if result.returncode:
        pytest.fail(f"bound API {name} fixture failed:\n{result.stderr[-4000:]}", pytrace=False)
    rows = [row.removeprefix("SS_WEB_FIXTURE:") for row in result.stdout.splitlines()
            if row.startswith("SS_WEB_FIXTURE:")]
    if len(rows) != 1:
        pytest.fail(f"bound API {name} fixture returned no unique setup result", pytrace=False)
    return json.loads(rows[0])


@pytest.fixture(scope="session")
def web_database() -> None:
    """Refuse, before Phase 4's `schema` drops anything, a DATABASE_URL whose database name does not end in _web_test
    (shelfshift_web_test by default; a branch may use its own, e.g. shelfshift_verify_web_test, 07 D-29 amended): the
    _test suffix alone would let the browser suite drop the schema an API run is using on shelfshift_test."""
    url = sa.engine.make_url(config.DATABASE_URL)
    name = url.database or ""
    if not name.endswith(WEB_SUFFIX) or not re.fullmatch(r"[a-z0-9_]+_web_test", name):  # 07 D-31
        pytest.fail(f"the browser suite runs only on a database whose name ends in {WEB_SUFFIX} (by default "
                    f"{WEB_DATABASE}), not {name!r}: run it with `make test-web`", pytrace=False)
    if url.host not in ("localhost", "127.0.0.1", "::1") or config.CLOUD_SQL_INSTANCE:
        pytest.fail("browser tests require local PostgreSQL, never Cloud SQL", pytrace=False)
    if os.environ.get("GCP", "off") != "off":
        pytest.fail("browser tests require GCP=off", pytrace=False)


@pytest.fixture(scope="session")
def schema(request, web_database, assistant_api_binding):
    """Phase 4's `schema` for the browser suite, set up only after `web_database` passed (`template` asks for it)."""
    if assistant_api_binding.root == ROOT:
        return request.getfixturevalue("api_schema")
    return _bound_fixture(assistant_api_binding, "schema")


# The dataset the browser suite runs on (05.2-22): "synthetic" (the default: data/reference's Day-3 template) or
# "public_tiny" (the tiny public set, tests/api/fixtures/public_tiny: a synthetic test fixture in the FreshRetailNet-50K
# schema). The public files skip on the synthetic set, naming the switch.
DATASET = os.environ.get("SS_WEB_DATASET", "synthetic")
if DATASET not in ("synthetic", "public_tiny"):
    raise RuntimeError(f"SS_WEB_DATASET is 'synthetic' or 'public_tiny', not {DATASET!r}")
public_only = pytest.mark.skipif(DATASET != "public_tiny", reason="runs with SS_WEB_DATASET=public_tiny")


@pytest.fixture(scope="session")
def web_template(request, schema, assistant_api_binding) -> str:
    """The ACTIVE template every visitor's sandbox is cloned from, for the whole browser run: the synthetic Day-3
    template (Phase 4's `template`), or with SS_WEB_DATASET=public_tiny the tiny public set installed beside
    data/reference and built through `sandbox.build_template` (its warm-up, the open day's catalogue and the checks)."""
    if assistant_api_binding.root != ROOT:
        return _bound_fixture(assistant_api_binding, "template", schema) if DATASET == "synthetic" else \
            _bound_fixture(assistant_api_binding, "public_template")
    if DATASET == "synthetic":
        return request.getfixturevalue("template")
    from api import refdata, sandbox
    sandbox.clear_cache()  # the schema was rebuilt: read its tables again
    return sandbox.build_template(refdata.install(TINY, allow_local=True))


@pytest.fixture(scope="session")
def assistant_api_command(assistant_api_binding):
    """Tests can inject a local scripted model launcher without a product switch."""
    return [str(assistant_api_binding.python), "-m", "uvicorn", "api.app:app", "--no-proxy-headers"]


@pytest.fixture(scope="session")
def web(web_template, tmp_path_factory, assistant_api_binding, assistant_api_command) -> Web:
    """The API child and the vite preview child for the whole browser run (after the schema and template exist)."""
    require_test_database(config.DATABASE_URL)  # `schema` already refused anything else; checked again before start
    taken = [port for port in (API_PORT, PREVIEW_PORT) if _occupied(port)]
    if taken:
        pytest.fail(f"port {', '.join(map(str, taken))} on {HOST} is already in use: stop what holds it, or set "
                    "SS_WEB_API_PORT / SS_WEB_PREVIEW_PORT", pytrace=False)
    if not (WEB / "dist" / "index.html").is_file():
        pytest.fail("web/dist is missing: run the suite through `make test-web`, which builds it", pytrace=False)

    logs = tmp_path_factory.mktemp("web-children")
    passcode = secrets.token_urlsafe(16)
    hq_passcode = secrets.token_urlsafe(16)  # a different value of the length the startup check accepts (D-64)
    api_url, url = f"http://{HOST}:{API_PORT}", f"http://{HOST}:{PREVIEW_PORT}"
    api = preview = None
    try:
        with open(logs / "api.out", "wb") as out, open(logs / "api.err", "wb") as err:
            api = subprocess.Popen(
                [*assistant_api_command, "--host", HOST, "--port", str(API_PORT)],
                cwd=assistant_api_binding.root, stdout=out, stderr=err,
                env=_child_env(DATABASE_URL=config.DATABASE_URL, DEMO_PASSCODE=passcode, HQ_PASSCODE=hq_passcode,
                               GCP="off", GEMINI_MODEL=TEST_MODEL, VERTEX_LOCATION="offline-test-location",
                               DEMO_MODE="on", ENGINE_WORKER="on"))
        _wait_ready("the API child", f"{api_url}/api/readyz", _api_readiness(assistant_api_binding),
                    api, logs / "api.err")

        with open(logs / "preview.out", "wb") as out, open(logs / "preview.err", "wb") as err:
            preview = subprocess.Popen(
                [str(WEB / "node_modules" / ".bin" / "vite"), "preview", "--host", HOST, "--port", str(PREVIEW_PORT),
                 "--strictPort"],
                cwd=WEB, stdout=out, stderr=err, env=_child_env(SS_API_ORIGIN=api_url))
        _wait_ready("vite preview", f"{url}/", lambda r: r.status_code == 200 and 'id="root"' in r.text, preview,
                    logs / "preview.err")
        _wait_ready("the /api proxy of vite preview", f"{url}/api/healthz", _json_is({"ok": True}), preview,
                    logs / "preview.err")
        running = Web(url=url, api_url=api_url, store=first_store(), passcode=passcode, hq_passcode=hq_passcode)
        _RUNNING[:] = [running]
        yield running
    finally:
        _RUNNING.clear()
        _stop(preview)
        _stop(api)


def new_context(browser: Browser, web: Web) -> BrowserContext:
    """One visitor's own browser context on the preview origin. The third-party font stylesheet is aborted so an
    offline machine never stalls on it; that is a font, not the API, so nothing is mocked."""
    context = browser.new_context(base_url=web.url)
    for pattern in THIRD_PARTY_FONTS:
        context.route(pattern, lambda route: route.abort())
    return context


def _session_or_none(page: Page) -> dict | None:
    """GET /api/session through the page's context: the JSON when signed in, None when locked (401)."""
    r = page.request.get("/api/session")
    if r.status == 401:
        return None
    assert r.status == 200, r.status
    return r.json()


def _wait_for_account(page: Page, account: str) -> dict:
    """Poll the session until it names `account` (a store code or HQ); returns the session JSON."""
    deadline = time.monotonic() + SIGNED_IN_S
    while True:
        s = _session_or_none(page)
        if s is not None and s.get("account") == account:
            return s
        assert time.monotonic() < deadline, f"the session never named the account {account!r}"
        page.wait_for_timeout(100)


def fill_sign_in(page: Page, web: Web, account: str | None = None) -> None:
    """Fill the sign-in card for `account` (default: web.store; HQ for the HQ planner) with this run's credential,
    found by role and label (UI-SPEC "Hand-off selectors"). The credential is typed, never printed."""
    account = account or web.store
    if account == HQ:
        page.get_by_role("radio", name="HQ planner").check()
        page.get_by_label("HQ passcode").fill(web.hq_passcode)
    else:
        page.get_by_role("radio", name="Store manager").check()
        page.get_by_label("Store code").fill(account)
        page.get_by_label("Passcode", exact=True).fill(web.passcode)


def sign_in(page: Page, web: Web, account: str | None = None) -> dict:
    """Open the app signed out, sign in on the card as `account` (default: web.store; HQ for the HQ planner) and
    return the session JSON once it names that account and the main navigation shows."""
    account = account or web.store
    page.goto("/")
    fill_sign_in(page, web, account)
    page.get_by_role("button", name="Sign in", exact=True).click()
    s = _wait_for_account(page, account)
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()
    return s


def sign_out(page: Page) -> None:
    """POST /api/session/sign-out through the page's own context with a fresh Idempotency-Key (the workspace is
    kept, D-02); the page itself is not reloaded."""
    r = page.request.post("/api/session/sign-out", headers={"Idempotency-Key": str(uuid.uuid4())}, data={})
    assert r.status == 200, r.status


def pass_gate(page: Page, web: Web) -> None:
    """The Phase 5 name, kept for the older tests: signs in as the default store manager (web.store)."""
    sign_in(page, web)


@pytest.fixture
def open_visitor(browser: Browser, web: Web):
    """A factory: each call is a new independent visitor (its own context, cookie and sandbox) past the gate. Every
    context it made is closed at teardown, and the workspaces the test opened are given back to the day's global
    count: the full suite opens more than caps.global.sandbox (300 a day), which stays as configured; test_reports.py
    does the same for its module."""
    from api import db

    count = "SELECT window_start, count FROM usage_counts WHERE scope = 'global' AND kind = 'sandbox'"
    with db_tx() as conn:
        before = {row["window_start"]: row["count"] for row in db.rows(conn, count)}
    made: list[BrowserContext] = []

    def open_one() -> Visitor:
        context = new_context(browser, web)
        made.append(context)
        page = context.new_page()
        pass_gate(page, web)
        return Visitor(context, page)

    yield open_one
    for context in made:
        context.close()
    with db_tx() as conn:
        for row in db.rows(conn, count + " FOR UPDATE"):
            w = row["window_start"]
            if w not in before:
                db.run(conn, "DELETE FROM usage_counts WHERE scope = 'global' AND kind = 'sandbox' AND window_start = :w", w=w)
            elif row["count"] > before[w]:
                db.run(conn, "UPDATE usage_counts SET count = :n WHERE scope = 'global' AND kind = 'sandbox' "
                             "AND window_start = :w", n=before[w], w=w)


@pytest.fixture
def visitor(open_visitor) -> Visitor:
    return open_visitor()


def session_json(page: Page) -> dict:
    """GET /api/session through the page's own context (same cookie, same seat)."""
    r = page.request.get("/api/session")
    assert r.status == 200, r.status
    return r.json()


def session_id(context: BrowserContext) -> str:
    """The visitor's session id: the value of its ss_session cookie (the API keys per-session caps on it)."""
    found = [c["value"] for c in context.cookies() if c["name"] == "ss_session"]
    assert found, "the context has no ss_session cookie"
    return found[0]


def db_tx():
    """api.db.tx for SQL in a test: this process already points at the browser suite's _test database."""
    from api import db  # imported here so the module loads after DATABASE_URL is set above

    require_test_database(config.DATABASE_URL)
    return db.tx()


def run_id(page: Page) -> str:
    """The visitor's own sandbox run_id, from GET /api/session through the page's context."""
    r = session_json(page)["run_id"]
    assert r, "the session has no run_id"
    return r


def expire_holds(run: str, case_id: str) -> int:
    """Move one visitor's HELD reservations for one case into the past (spec §8 hold expiry, D-09): the test never
    sleeps through a hold. Scoped to that sandbox's run_id and that case_id, so no other sandbox is touched; the next
    read of the case applies the expiry. Returns the number of reservations moved."""
    with db_tx() as conn:
        return conn.execute(sa.text(
            "UPDATE reservations SET expires_at = now() - interval '1 minute' "
            "WHERE run_id = :r AND case_id = :c AND state = 'HELD'"), {"r": run, "c": case_id}).rowcount


# Signs out and back in inside the page (its own cookie), each POST with a fresh Idempotency-Key; returns the two
# HTTP statuses only, never a body. The credential arrives as an evaluate argument, so it is in no script text.
_SWITCH_JS = """async ([account, passcode]) => {
  const post = async (path, body) => (await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID()},
    body: JSON.stringify(body),
  })).status;
  return [await post("/api/session/sign-out", {}), await post("/api/session", {account, passcode})];
}"""


def switch_seat(page: Page, branch: str | None) -> None:
    """The Phase 5 name, kept for the older tests (06.1 has no seat switch): sign out and sign back in as `branch`'s
    store manager, or as HQ for None, in the same browser (the workspace is kept, D-02), reload, and wait until the
    session reports that seat."""
    assert _RUNNING, "switch_seat needs the `web` fixture"
    web = _RUNNING[0]
    account = HQ if branch is None else branch
    statuses = page.evaluate(_SWITCH_JS, [account, web.hq_passcode if account == HQ else web.passcode])
    assert statuses == [200, 200], statuses
    page.reload()
    want = ("hq", None) if branch is None else ("manager", branch)
    deadline = time.monotonic() + SIGNED_IN_S
    while True:
        s = session_json(page)
        if (s["role"], s["branch"]) == want:
            break
        assert time.monotonic() < deadline, f"the session never reported {want}: {(s['role'], s['branch'])}"
        page.wait_for_timeout(100)
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()


def cards_json(page: Page) -> list[dict]:
    """The `cases` list of GET /api/cases through the page's own context, in the server's order."""
    r = page.request.get("/api/cases")
    assert r.status == 200, r.status
    return r.json()["cases"]


def case_id_by_tag(page: Page, tag: str) -> str:
    """The case_id of the card whose seed tag is `tag`; fails naming the tags on the board when there is none."""
    cards = cards_json(page)
    found = [c["case_id"] for c in cards if c["tag"] == tag]
    assert found, f"no card is tagged {tag!r}; the board's tags are {sorted({c['tag'] for c in cards if c['tag']})}"
    return found[0]


def case_json(page: Page, case_id: str) -> dict:
    """GET /api/cases/{id} through the page's own context (same cookie, same seat)."""
    r = page.request.get("/api/cases/" + urllib.parse.quote(case_id, safe=""))
    assert r.status == 200, r.status
    return r.json()


def take_ownership(page: Page, case_id: str) -> dict:
    """POST /api/cases/{id}/take-ownership through the page's own context (same cookie, same seat) with a fresh
    Idempotency-Key; asserts 200 and returns the answer. Send needs an owner since 06.1-08 (D-54): older browser tests
    that press Send take ownership through the API first, until the stock decision page has its own control."""
    r = page.request.post("/api/cases/" + urllib.parse.quote(case_id, safe="") + "/take-ownership", data={},
                          headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status == 200, r.status
    return r.json()


def open_case(page: Page, key_or_tag: str) -> dict:
    """Open the stock decision whose key or seed tag is `key_or_tag` at "#/cases/{id}" directly (06.1: board cards
    open a drawer), wait for the page's stepper and h1, and return that case's detail JSON."""
    page.goto("/#/")
    cards = cards_json(page)
    found = [c for c in cards if key_or_tag in (c["key"], c["tag"])]
    assert found, f"no card has the key or tag {key_or_tag!r}"
    card = found[0]
    page.goto("/#/cases/" + urllib.parse.quote(card["case_id"], safe=""))
    detail = case_json(page, card["case_id"])
    expect(page.get_by_role("navigation", name="Steps", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"lot {detail['lot']['lot_id']}")
    return detail


def decision_body(page: Page, case_id: str, decision: str, rec_version: str | None = None) -> dict:
    """The body the stock decision page sends for one decision, read from the detail as the page's own account: the
    version (the detail's, or `rec_version` when a test sends an older one on purpose), the decision, and for an
    approve or accept the plan the page shows (`plan_key`, the detail's `suggested_key`; 07 review C, D-32) and, when
    that plan has Advisory facts, the exact acknowledgement the detail carries (`acknowledged`, `commitment.ack`;
    07-CONTEXT D-12, D-26). The one way the browser tests' helpers post a decision, so none copies the rule."""
    d = case_json(page, case_id)
    body = {"rec_version": d["rec_version"] if rec_version is None else rec_version, "decision": decision}
    if decision in ("approve", "accept"):
        body["plan_key"] = d["suggested_key"]
        ack = (d.get("commitment") or {}).get("ack")
        if ack is not None:
            body["acknowledged"] = ack
    return body


CHECKED_NOW = {"observed": "now"}  # "When did you check?" → "Checked just now" on a test's answer post (07 D-22)


@pytest.fixture(autouse=True)
def workspace_budget(request):
    """Return each browser test's workspace usage after its browser contexts close (was test_reports.py's own, 08).

    Each test still spends against the real cap; its successful workspace creations must not consume the budget of
    later, unrelated browser tests. The merged suite (freeze + Phase 7 + Phase 9) opens more workspaces in one run than
    the daily global cap (settings caps.global.sandbox), so every test that drives the app gives its own back.
    """
    if "web" not in request.fixturenames:  # the static checks (make web-check) open no database
        yield
        return
    request.getfixturevalue("web")
    from api import db

    with db.tx() as conn:
        before = {row["window_start"]: row["count"] for row in db.rows(
            conn, "SELECT window_start, count FROM usage_counts WHERE scope = 'global' AND kind = 'sandbox'")}
    yield
    with db.tx() as conn:
        for row in db.rows(conn, "SELECT window_start, count FROM usage_counts "
                                "WHERE scope = 'global' AND kind = 'sandbox' FOR UPDATE"):
            previous = before.get(row["window_start"])
            created = row["count"] - (previous or 0)
            assert created >= 0, "another test changed the workspace counter"
            if not created:
                continue
            if previous is None:
                db.run(conn, "DELETE FROM usage_counts WHERE scope = 'global' AND kind = 'sandbox' "
                             "AND window_start = :w AND count = :n", w=row["window_start"], n=created)
            else:
                db.run(conn, "UPDATE usage_counts SET count = count - :n "
                             "WHERE scope = 'global' AND kind = 'sandbox' AND window_start = :w",
                       n=created, w=row["window_start"])
