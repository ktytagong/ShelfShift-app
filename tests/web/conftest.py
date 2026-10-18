"""The browser harness (05-CONTEXT D-09; research Pattern 9): a production build of web/ under `vite preview`, proxied
to a real API child on the browser suite's own _test database, opened by headless Chromium. Never mocks the API.

Order: `web_database` refuses any database but shelfshift_web_test first (review WR-09: a bare `pytest` collects
tests/api first, whose conftest points DATABASE_URL at Phase 4's shelfshift_test), then Phase 4's `schema` fixture runs
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
WEB_DATABASE = "shelfshift_web_test"  # default only; every accepted name ends in _web_test (07 D-31)
HOST = "127.0.0.1"
API_PORT = int(os.environ.get("SS_WEB_API_PORT", "18080"))
PREVIEW_PORT = int(os.environ.get("SS_WEB_PREVIEW_PORT", "14173"))
READY_S = 90  # how long each child may take to answer its readiness check
STOP_S = 10  # how long a child may take to stop before it is killed
CARRIED_ENV = ("PATH", "HOME", "LANG", "TMPDIR")  # the only variables a child inherits: no GOOGLE_*, no CLOUD_SQL_*
THIRD_PARTY_FONTS = ("https://fonts.googleapis.com/**", "https://fonts.gstatic.com/**")
HQ = "HQ"  # the HQ planner's account (04-API-CONTRACT "Product mode (06.1)", POST /api/session)
SIGNED_IN_S = 30  # how long a sign-in or a seat switch may take before the session reports the account

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


def _database_ready(r: httpx.Response) -> bool:
    try:
        body = r.json()
        return r.status_code == 200 and body.get("ok") is True and body.get("db") == "ok"
    except (ValueError, AttributeError):
        return False


@pytest.fixture(scope="session")
def web_database() -> None:
    """Refuse before schema drop any name without the _web_test suffix (07 D-31).
    A plain _test suffix could collide with an API run; each session gets its own web database."""
    name = sa.engine.make_url(config.DATABASE_URL).database or ""
    if not re.fullmatch(r"[a-z0-9_]+_web_test", name):
        pytest.fail(f"the browser suite requires a name ending in _web_test, not {name!r}: run it with `make test-web`",
                    pytrace=False)


@pytest.fixture(scope="session")
def schema(web_database, api_schema):
    """Phase 4's `schema` for the browser suite, set up only after `web_database` passed (`template` asks for it)."""
    return api_schema


# The dataset the browser suite runs on (05.2-22): "synthetic" (the default: data/reference's Day-3 template) or
# "public_tiny" (the tiny public set, tests/api/fixtures/public_tiny: a synthetic test fixture in the FreshRetailNet-50K
# schema). The public files skip on the synthetic set, naming the switch.
DATASET = os.environ.get("SS_WEB_DATASET", "synthetic")
if DATASET not in ("synthetic", "public_tiny"):
    raise RuntimeError(f"SS_WEB_DATASET is 'synthetic' or 'public_tiny', not {DATASET!r}")
public_only = pytest.mark.skipif(DATASET != "public_tiny", reason="runs with SS_WEB_DATASET=public_tiny")


@pytest.fixture(scope="session")
def web_template(request, schema) -> str:
    """The ACTIVE template every visitor's sandbox is cloned from, for the whole browser run: the synthetic Day-3
    template (Phase 4's `template`), or with SS_WEB_DATASET=public_tiny the tiny public set installed beside
    data/reference and built through `sandbox.build_template` (its warm-up, the open day's catalogue and the checks)."""
    if DATASET == "synthetic":
        return request.getfixturevalue("template")
    from api import refdata, sandbox
    sandbox.clear_cache()  # the schema was rebuilt: read its tables again
    return sandbox.build_template(refdata.install(TINY, allow_local=True))


@pytest.fixture(scope="session")
def web(web_template, tmp_path_factory) -> Web:
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
                [sys.executable, "-m", "uvicorn", "api.app:app", "--host", HOST, "--port", str(API_PORT), "--no-proxy-headers"],
                cwd=ROOT, stdout=out, stderr=err,
                env=_child_env(DATABASE_URL=config.DATABASE_URL, DEMO_PASSCODE=passcode, HQ_PASSCODE=hq_passcode,
                               DEMO_MODE="on", ENGINE_WORKER="on"))
        _wait_ready("the API child", f"{api_url}/api/readyz", _database_ready, api, logs / "api.err")

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
    context it made is closed at teardown."""
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
