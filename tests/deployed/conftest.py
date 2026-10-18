"""The deployed journey's harness (06-CONTEXT D-20): the judge's path through a browser against a running URL, the
deployed service or the local Compose app.

Deploy-safe: it never touches a database (DATABASE_URL points at a sentinel no server answers on, set before anything
imports api, and `api.db` must never have created an engine by the end of the run), never starts a server, never
resets the deployed service and uses no fixture shortcut: every step goes through the browser and the public API with
the page's own cookie. Each run opens two sandboxes through the gate (the dataset probe and the path).

The URL comes from SS_DEPLOYED_URL and must be https; an http URL is accepted only with SS_JOURNEY_LOCAL=1 and a
loopback host (127.0.0.1 or localhost), for the local Compose app. Both credentials (06.1 D-47) come from the
environment, SMOKE_PASSCODE (the store passcode) and SMOKE_HQ_PASSCODE, or from hidden prompts ("Store passcode: ",
"HQ passcode: ") when stdin is a terminal; they are held only in `Web.passcode` and `Web.hq_passcode` (hidden from
repr) and never printed, logged or passed as an argument. Before any browser step the target's readyz must answer
`{"ok": true, "db": "ok"}`, with only the known dependency keys besides (deploy.smoke.is_ready).

Run it with `make journey-deployed URL=<url>` (or `make compose-check` for the local Compose app); the `deployed`
marker keeps it out of every other run.
"""
from __future__ import annotations

import os

# Before anything imports api or tests.web: no database is reachable from this suite, and no Cloud SQL instance.
os.environ["DATABASE_URL"] = "postgresql+pg8000://deployed-suite@127.0.0.1:9/no_database"
os.environ.pop("CLOUD_SQL_INSTANCE", None)

import contextlib  # noqa: E402
import getpass  # noqa: E402
import sys  # noqa: E402
import urllib.parse  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from tests.web.conftest import Web  # noqa: E402  (the dataclass only: tests.web's fixtures are never registered here)
from deploy.smoke import is_ready  # noqa: E402  (the smoke's readyz rule, one definition)

LOOPBACK = ("127.0.0.1", "localhost")
READY_TIMEOUT_S = 60  # a cold Cloud Run instance may take a while to answer its first request


def _url() -> str:
    raw = os.environ.get("SS_DEPLOYED_URL", "").strip().rstrip("/")
    if not raw:
        pytest.fail("SS_DEPLOYED_URL is not set: run `make journey-deployed URL=<service URL>`", pytrace=False)
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme == "https" and parts.hostname:
        return raw
    if parts.scheme == "http" and os.environ.get("SS_JOURNEY_LOCAL") == "1" and parts.hostname in LOOPBACK:
        return raw
    pytest.fail("SS_DEPLOYED_URL must be an https URL; an http URL only with SS_JOURNEY_LOCAL=1 and the host "
                "127.0.0.1 or localhost (the local Compose app)", pytrace=False)


def _credential(pytestconfig, variable: str, prompt: str, name: str) -> str:
    """The credential from `variable`, else a hidden prompt on a terminal; pytest.fail naming the variable otherwise."""
    given = os.environ.get(variable)
    if given is not None and given.strip():
        return given
    capture = pytestconfig.pluginmanager.getplugin("capturemanager")
    # pytest points stdin at /dev/null while it captures output; the terminal check and the prompt run without it
    with capture.global_and_fixture_disabled() if capture else contextlib.nullcontext():
        if sys.stdin is not None and sys.stdin.isatty():
            typed = getpass.getpass(prompt)
            if typed.strip():
                return typed
    pytest.fail(f"set {variable}, or run on a terminal to be asked for the {name}", pytrace=False)


def _ready(url: str) -> None:
    try:
        r = httpx.get(f"{url}/api/readyz", timeout=READY_TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:
        pytest.fail(f"readyz could not be reached ({type(exc).__name__})", pytrace=False)
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code != 200 or not is_ready(body):
        pytest.fail(f'readyz answered {r.status_code} {body!r}, not 200 {{"ok": true, "db": "ok"}} with only known keys', pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def deployed_web(pytestconfig) -> Web:
    """The target, checked before the browser starts (autouse, so it runs ahead of the `browser` fixture): the URL
    rules, both credentials, then readyz."""
    url = _url()
    passcode = _credential(pytestconfig, "SMOKE_PASSCODE", "Store passcode: ", "store passcode")
    hq_passcode = _credential(pytestconfig, "SMOKE_HQ_PASSCODE", "HQ passcode: ", "HQ passcode")
    _ready(url)
    # ASSUMPTION A10 (06.1-HANDOFF): Web(url, api_url, store, passcode, hq_passcode); the test fills in store from the
    # story's source after the HQ sign-in
    return Web(url=url, api_url=url, store="", passcode=passcode, hq_passcode=hq_passcode)


@pytest.fixture(scope="session")
def web(deployed_web) -> Web:
    """The name the shared helpers (judges_journey, new_context) expect."""
    return deployed_web


@pytest.fixture(scope="session", autouse=True)
def no_database():
    """At the end of the run no database engine was ever created (D-20: deploy-safe)."""
    yield
    from api import db

    assert db._engine is None, "the deployed suite created a database engine"
