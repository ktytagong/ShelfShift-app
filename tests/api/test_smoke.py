"""The smoke script against the product app (06.1 D-47, D-53, D-62; HQ handoff item 7): `deploy.smoke.check` through
a TestClient on the test database, with DEMO_MODE off as the deployed service runs it (the product API suite's
conftest turns it on; each test here turns it off with monkeypatch).

This file drives build/product's app: it imports the product suite's two credentials (tests.api.conftest's PASSCODE
and HQ). It passes in the trial against build/product's real app and after the WEB_REF merge (deploy/README.md step
12, release-day step 4), not on build/deploy-hq alone, whose Phase 5 app has no accounts.

build/product's app serves no SPA: config.WEB_DIST, index.html's no-cache and the SPA guard are build/deploy's Phase 6
code, and so are the wrong-passcode counter's attempts_left and the rightmost X-Forwarded-For address (D-24). The
tests that need those end in `_merged_app` and run only after the merge; the others assert every step but the SPA
pair and the counter. The DB-free step-by-step cases (a broken deploy's answers) live in tests/deploy/test_smoke.py.

Every TestClient gets its own `client=(<name>, 50000)` peer (the WR-03 pattern), so no two tests share a wrong-passcode
counter. No test prints, stores or asserts on a credential's value except to prove it is absent.
"""
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from api import config
from api.app import app
from deploy import smoke
from tests.api.conftest import HQ, PASSCODE

CHAT = ("chat ping", "not run (no chat route until Phase 9)")
NOT_PRODUCT = ("spa page", "spa guard", "passcode counter", "chat ping")  # build/deploy's Phase 6 and D-24 code
COUNTED = "ok (the forged X-Forwarded-For was counted with this client's own guesses)"
FRONT_END = "198.51.100.7"  # stands in for the address Google's front end appends (RFC 5737)


@pytest.fixture(autouse=True)
def demo_mode_off(monkeypatch):
    """The deployed service runs with demo mode off (06.1 D-62)."""
    monkeypatch.setattr(config, "DEMO_MODE", False)


@pytest.fixture
def web_dist(tmp_path, monkeypatch):
    """A tiny web build (build/deploy's config.WEB_DIST): only the `_merged_app` tests use it, because build/product's
    config has no WEB_DIST to set."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<!doctype html><html><body><div id="root"></div></body></html>', "utf-8")
    monkeypatch.setattr(config, "WEB_DIST", str(dist))
    return dist


def visitor(name: str, *hooks) -> TestClient:
    c = TestClient(app, client=(f"smoke-{name}", 50000))
    if hooks:
        c.event_hooks = {"request": list(hooks), "response": []}
    return c


def no_sleep(seconds):
    raise AssertionError(f"the smoke slept {seconds} s with attempts left")


def record(sent: list):
    def hook(request: httpx.Request) -> None:
        sent.append(request)
    return hook


def key() -> dict:
    return {"Idempotency-Key": uuid.uuid4().hex}


def sign_in(c, account: str, passcode: str) -> dict:
    r = c.post("/api/session", json={"account": account, "passcode": passcode}, headers=key())
    assert r.status_code == 200, r.json()
    return r.json()


def story(c) -> dict:
    return smoke.story_card(c)


# --- the product app (the trial and after the merge) ---------------------------------------------------------------

def test_every_product_step_is_ok_against_the_app(template):
    sent = []
    out = smoke.check(visitor("product-ok", record(sent)), PASSCODE, HQ, sleep=no_sleep)
    assert [step for step, _ in out] == list(smoke.STEPS) and out[-1] == CHAT
    bad = [(step, status) for step, status in out if step not in NOT_PRODUCT and status != "ok"]
    assert not bad, bad
    sign_ins = [json.loads(r.content) for r in sent if r.method == "POST" and r.url.path == "/api/session"]
    assert sign_ins[0]["account"] == "HQ" and sign_ins[1]["account"] != "HQ"
    for status in dict(out).values():
        assert PASSCODE not in status and HQ not in status


def test_a_store_policy_edit_is_403_and_changes_nothing(template):
    c = visitor("store-patch")
    sign_in(c, "HQ", HQ)
    source = story(c)["source"]
    sign_in(c, source, PASSCODE)
    before = c.get("/api/settings").json()
    r = c.patch("/api/settings", json={"expected_version": before["version"],
                                       "changes": {"waste_price": before["bounds"]["waste_price"]["max"] + 1}},
                headers=key())
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    after = c.get("/api/settings").json()
    assert (after["version"], after["values"]) == (before["version"], before["values"])


def test_hq_reaches_the_write_path_and_out_of_bounds_writes_nothing(template):
    c = visitor("hq-patch")
    assert sign_in(c, "HQ", HQ)["role"] == "hq"
    before = c.get("/api/settings").json()
    r = c.patch("/api/settings", json={"expected_version": before["version"],
                                       "changes": {"waste_price": before["bounds"]["waste_price"]["max"] + 1}},
                headers=key())
    assert (r.status_code, r.json()["error"]) == (400, "out_of_bounds")
    stale = c.patch("/api/settings", json={"expected_version": before["version"] + 1,
                                           "changes": {"waste_price": before["bounds"]["waste_price"]["max"]}},
                    headers=key())
    assert (stale.status_code, stale.json()["error"]) == (409, "stale_version")  # HQ is past the role check
    after = c.get("/api/settings").json()
    assert (after["version"], after["values"]) == (before["version"], before["values"])


def test_the_demo_routes_are_refused_with_demo_mode_off(template):
    c = visitor("demo-off")
    sign_in(c, "HQ", HQ)
    version = c.get("/api/session").json()["version"]
    case_id = story(c)["case_id"]
    for path, body, want in (("/api/demo/reset", {}, (403, "forbidden")),
                             ("/api/demo/advance-day", {"expected_version": version}, (403, "forbidden")),
                             ("/api/demo/role", {}, (404, "not_found")),
                             (f"/api/cases/{case_id}/wait", {}, (404, "not_found"))):
        r = c.post(path, json=body, headers=key())
        assert (r.status_code, r.json()["error"]) == want, path
    assert c.get("/api/session").json()["version"] == version


def test_the_store_passcode_never_opens_hq(template):
    c = visitor("hq-store")
    r = c.post("/api/session", json={"account": "HQ", "passcode": PASSCODE})
    assert (r.status_code, r.json()["error"]) == (401, "bad_passcode")
    out = dict(smoke.check(visitor("hq-store-step"), PASSCODE, HQ, sleep=no_sleep))
    assert out["HQ refuses the store passcode"] == "ok"


def test_a_forged_x_forwarded_for_is_counted_with_the_peer(template, monkeypatch):
    """The product app counts wrong guesses per TCP peer, so a forged X-Forwarded-For lands on the same counter:
    with a limit of 2, a plain and a forged wrong guess leave the next one blocked."""
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (2, "hour"))
    c = visitor("forged-peer")
    guess = {"account": "HQ", "passcode": uuid.uuid4().hex}
    assert c.post("/api/session", json=guess).status_code == 401
    assert c.post("/api/session", json=guess, headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401
    blocked = c.post("/api/session", json=guess)
    assert (blocked.status_code, blocked.json()["error"]) == (429, "cap_reached")


def test_a_wrong_hq_passcode_fails_the_hq_sign_in_and_skips_what_needs_it(template):
    out = dict(smoke.check(visitor("hq-wrong"), PASSCODE, "not-the-hq-passcode-x"))
    assert out["HQ sign-in"] == "FAIL 401 bad_passcode"
    for step in ("story card", "HQ policy edit", "store sign-in", "store policy edit refused", "simulate", "export",
                 "demo endpoints refused", "passcode counter"):
        assert out[step] == "skipped (needs HQ sign-in)", step
    for step in ("healthz", "readyz", "locked gate", "HQ refuses the store passcode"):
        assert out[step] == "ok", step


# --- after the merge only: build/deploy's SPA and D-24 code ----------------------------------------------------------

def front_end(request: httpx.Request) -> None:
    """What Google's front end does on Cloud Run: append the address it saw to X-Forwarded-For."""
    seen = request.headers.get("x-forwarded-for")
    request.headers["X-Forwarded-For"] = f"{seen}, {FRONT_END}" if seen else FRONT_END


def test_every_checked_step_is_ok_merged_app(template, web_dist, monkeypatch):
    """Needs build/deploy's WEB_DIST and SPA guard and D-24's attempts_left."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", False)
    out = smoke.check(visitor("merged-ok"), PASSCODE, HQ, sleep=no_sleep)
    assert dict(out)["passcode counter"] == COUNTED and out[-1] == CHAT
    bad = [(step, status) for step, status in out[:-2] if status != "ok"]
    assert not bad, bad


def test_the_counter_step_is_ok_on_cloud_run_behind_the_front_end_merged_app(template, web_dist, monkeypatch):
    """Needs D-24's rightmost X-Forwarded-For address on Cloud Run."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    out = dict(smoke.check(visitor("merged-run", front_end), PASSCODE, HQ, sleep=no_sleep))
    assert out["passcode counter"] == COUNTED


def test_the_counter_step_fails_when_the_forged_entry_is_counted_apart_merged_app(template, web_dist, monkeypatch):
    """Needs D-24: on Cloud Run without a front end the forged entry is the rightmost one, so it gets its own counter
    and the step reports FAIL after one retry."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    out = dict(smoke.check(visitor("merged-apart"), PASSCODE, HQ, sleep=no_sleep))
    assert out["passcode counter"] == "FAIL the forged X-Forwarded-For was counted as another address"
