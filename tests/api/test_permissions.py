"""Who may do what, decided on the server from the signed-in account (06.1 D-04, D-53, D-62; 04-API-CONTRACT
"Product mode (06.1)", Identity and permissions; 06.1-HQ-SECRET-HANDOFF item 7).

- Only HQ changes policies (`PATCH /api/settings`); a store manager reads them and is refused 403 forbidden. The
  check reads the session row's role, never a body field.
- HQ signs in with its own credential only: the store passcode on the HQ account is 401 bad_passcode.
- The demo controls (reset, advance day) answer 403 forbidden unless DEMO_MODE is on, checked first; DEMO_MODE is
  off unless the environment says "on". The suite's conftest turns it on (the tests drive Next day, D-04); each test
  here sets it itself.
- The seat switch and Wait are retired (404 not_found); no route sets a role or a branch after sign-in.

Credentials are generated per test run by conftest and never printed; every check on them is a boolean. Each test
that sends a wrong credential uses its own client address, so the per-address limit of another test is not spent.
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import app as app_module
from api import config, db, settings
from api.app import COOKIE, app
from tests.api.conftest import PASSCODE, as_seat, key, sign_in

HQ_ONLY = "Only HQ can change policies."  # (proposed) 06.1 contract, PATCH /api/settings row
NOT_HERE = "This is not available in this workspace."  # (proposed) 06.1 contract, demo reset and advance-day row
RETIRED = (("POST", "/api/demo/role"), ("POST", "/api/cases/{case_id}/wait"))  # 06.1 D-04


def error(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]


def patch(client, changes):
    body = {"expected_version": client.get("/api/session").json()["version"], "changes": changes}
    return client.patch("/api/settings", json=body, headers=key())


def a_new_value(client) -> dict:
    """A shortlist_step inside its bound that differs from the workspace's current one (from the bounds it reads)."""
    got = client.get("/api/settings").json()
    allowed = got["bounds"]["shortlist_step"]["allowed"]
    return {"shortlist_step": next(v for v in allowed if v != got["values"]["shortlist_step"])}


def test_the_refusal_is_one_named_constant():
    assert settings.HQ_ONLY == HQ_ONLY


def test_a_store_manager_reads_policies_and_cannot_change_them(client):
    s = client.get("/api/session").json()
    assert (s["role"], s["account"]) == ("manager", s["branch"])
    before = client.get("/api/settings")
    assert before.status_code == 200 and set(before.json()) >= {"values", "bounds", "version"}
    r = patch(client, a_new_value(client))
    assert error(r) == (403, "forbidden") and r.json()["message"] == HQ_ONLY
    assert client.get("/api/settings").json() == before.json()  # nothing changed, not even the version
    assert client.get("/api/session").json()["version"] == s["version"]


def test_the_role_comes_from_the_session_never_the_body(client):
    before = client.get("/api/settings").json()
    body = {"expected_version": before["version"], "changes": a_new_value(client), "role": "hq", "account": "HQ"}
    r = client.patch("/api/settings", json=body, headers=key())
    assert error(r) == (403, "forbidden") and r.json()["message"] == HQ_ONLY
    assert client.get("/api/settings").json() == before


def test_hq_changes_policies_within_the_bounds(client):
    store_view = client.get("/api/settings").json()
    as_seat(client, None)  # signs out, then in as HQ with HQ's own credential; the workspace is kept
    s = client.get("/api/session").json()
    assert (s["account"], s["role"], s["branch"]) == ("HQ", "hq", None)
    assert client.get("/api/settings").json() == store_view  # GET is the same for every account
    change = a_new_value(client)
    r = patch(client, change)
    assert r.status_code == 200, r.json()
    assert r.json()["values"]["shortlist_step"] == change["shortlist_step"]
    assert r.json()["version"] == s["version"] + 1
    out = patch(client, {"waste_price": store_view["bounds"]["waste_price"]["max"] + 1})
    assert error(out) == (400, "out_of_bounds") and out.json()["field"] == "waste_price"
    assert client.get("/api/settings").json()["values"]["shortlist_step"] == change["shortlist_step"]


def test_the_store_passcode_on_the_hq_account_is_refused(template):
    c = TestClient(app, client=("perm-hq-store-passcode", 50000))
    r = c.post("/api/session", json={"account": "HQ", "passcode": PASSCODE})
    assert error(r) == (401, "bad_passcode")
    assert not c.cookies.get(COOKIE) and error(c.get("/api/session")) == (401, "locked")
    sign_in(c, "HQ")  # HQ's own credential opens it
    assert c.get("/api/session").json()["role"] == "hq"


# --- the demo controls, demo mode and the retired routes (Task 2) ---------------------------------------------------

def workspace(client) -> dict:
    s = client.get("/api/session").json()
    return {k: s[k] for k in ("run_id", "day", "version", "account", "role", "branch")}


def key_rows(session_id: str) -> int:
    with db.tx() as conn:
        return db.one(conn, "SELECT count(*) AS n FROM idempotency_keys WHERE session_id = :s", s=session_id)["n"]


def test_the_refusal_of_a_demo_control_is_one_named_constant():
    assert app_module.NOT_HERE == NOT_HERE


def test_demo_controls_are_refused_first_outside_demo_mode(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", False)
    before = workspace(client)
    sid = client.cookies.get(COOKIE)
    keys = key_rows(sid)
    for path, body in (("/api/demo/reset", {}), ("/api/demo/advance-day", {"expected_version": before["version"]})):
        for headers in (key(), {}):  # with a key, and without one: the demo check comes before the key check
            r = client.post(path, json=body, headers=headers)
            assert error(r) == (403, "forbidden") and r.json()["message"] == NOT_HERE, (path, headers)
    assert workspace(client) == before  # the same run, day and version: nothing reset or advanced
    assert key_rows(sid) == keys  # no key row was kept for a refused control


def test_demo_controls_work_as_before_in_demo_mode(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    before = workspace(client)
    r = client.post("/api/demo/advance-day", json={"expected_version": before["version"]}, headers=key())
    assert r.status_code == 200, r.json()
    assert r.json() == {"advanced": True, "day": before["day"] + 1, "version": before["version"] + 1}
    r = client.post("/api/demo/reset", json={}, headers=key())
    assert r.status_code == 200, r.json()
    assert r.json()["run_id"] != before["run_id"] and workspace(client)["run_id"] == r.json()["run_id"]


@pytest.fixture
def public_client(template, public_run):
    """A client signed in on a workspace of the tiny public set (its template ACTIVE for this test only)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :t", t=template)
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :p", p=public_run["run_id"])
    try:
        c = TestClient(app)
        sign_in(c)
        yield c
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'INITIALIZING' WHERE run_id = :p", p=public_run["run_id"])
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=template)


def test_next_day_on_a_public_workspace_is_refused_either_way(public_client, monkeypatch):
    before = workspace(public_client)
    body = {"expected_version": before["version"]}
    monkeypatch.setattr(config, "DEMO_MODE", True)
    r = public_client.post("/api/demo/advance-day", json=body, headers=key())
    assert error(r) == (409, "not_available")  # demo mode on: the public workspace has no Next day (05.2 D-37 B)
    monkeypatch.setattr(config, "DEMO_MODE", False)
    r = public_client.post("/api/demo/advance-day", json=body, headers=key())
    assert error(r) == (403, "forbidden") and r.json()["message"] == NOT_HERE  # demo mode off: refused first
    assert workspace(public_client) == before


def test_the_seat_switch_and_wait_are_gone(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)  # gone in demo mode too, not just refused
    before = workspace(client)
    cid = client.get("/api/cases").json()["cases"][0]["case_id"]
    for body in ({"role": "hq"}, {"role": "manager", "branch": before["branch"]}):
        r = client.post("/api/demo/role", json=body, headers=key())
        assert error(r) == (404, "not_found") and r.json()["message"] == "No such route"
    r = client.post(f"/api/cases/{cid}/wait", json={"until_day": before["day"] + 1, "reason": "x"}, headers=key())
    assert error(r) == (404, "not_found") and r.json()["message"] == "No such route"
    assert workspace(client) == before  # still the store manager, in the same workspace


def test_no_route_sets_a_role_or_a_branch_after_sign_in():
    """The route table: the retired paths are absent, and no route other than sign-in (POST /api/session) writes
    sessions.role or sessions.branch (06.1 T-06.1-18, T-06.1-21). Sign-in is the one place a seat is set: from the
    account and its credential (api/accounts.py)."""
    routes = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert not set(RETIRED) & routes
    assert not [p for _, p in routes if "role" in p or "seat" in p or p.endswith("/wait")]
    api = pathlib.Path(__file__).resolve().parents[2] / "api"
    updates = [(path.name, sets) for path in sorted(api.glob("*.py"))
               for sets in re.findall(r"UPDATE sessions SET (.*?) WHERE", path.read_text())]
    assert updates  # the scan finds the session updates there are (sign-out, reset)
    assert [(name, sets) for name, sets in updates if re.search(r"\b(role|branch)\b", sets)] == []


def test_demo_mode_is_off_unless_the_environment_says_on():
    """D-62: with DEMO_MODE unset, api.config reads demo mode as off; only "on" turns it on. The child process prints
    booleans only."""
    root = pathlib.Path(__file__).resolve().parents[2]
    probe = "from api import config; print(config.DEMO_MODE)"
    env = {k: v for k, v in os.environ.items() if k != "DEMO_MODE"}
    for extra, want in (({}, "False"), ({"DEMO_MODE": "off"}, "False"), ({"DEMO_MODE": "yes"}, "False"),
                        ({"DEMO_MODE": "on"}, "True")):
        out = subprocess.run([sys.executable, "-c", probe], env={**env, **extra}, cwd=root, capture_output=True,
                             text=True)
        assert out.returncode == 0 and out.stdout.strip() == want, extra


# --- the four deployed checks, proved locally (Task 3; 06.1-HQ-SECRET-HANDOFF item 7) --------------------------------

def test_deployed_checks_locally(template, store, monkeypatch):
    """The four checks the release runs against the deployed service, in its order, with demo mode off as deployed:
    (1) a store sign-in cannot reach an HQ function: a policy edit is 403; (2) an HQ sign-in can edit policies;
    (3) the store passcode on the HQ account is refused; (4) the seat switch, advance day, reset and Wait are refused.
    The Phase 6 smoke and tests/deployed mirror it. Credentials stay in memory; every check on them is a boolean."""
    monkeypatch.setattr(config, "DEMO_MODE", False)
    c = TestClient(app, client=("perm-deployed-checks", 50000))

    sign_in(c, store)  # (1) the store manager
    assert c.get("/api/session").json()["role"] == "manager"
    before = c.get("/api/settings").json()
    r = patch(c, a_new_value(c))
    assert error(r) == (403, "forbidden") and r.json()["message"] == HQ_ONLY
    assert c.get("/api/settings").json() == before

    as_seat(c, None)  # (2) HQ, with its own credential
    change = a_new_value(c)
    r = patch(c, change)
    assert r.status_code == 200 and r.json()["values"]["shortlist_step"] == change["shortlist_step"]

    other = TestClient(app, client=("perm-deployed-hq", 50000))  # (3) the store passcode on HQ
    r = other.post("/api/session", json={"account": "HQ", "passcode": PASSCODE})
    assert error(r) == (401, "bad_passcode") and not other.cookies.get(COOKIE)

    s = c.get("/api/session").json()  # (4) every demo endpoint, as HQ, with a key and a valid body
    cid = c.get("/api/cases").json()["cases"][0]["case_id"]
    refused = {
        "/api/demo/role": (c.post("/api/demo/role", json={"role": "hq"}, headers=key()), (404, "not_found")),
        "/api/demo/advance-day": (c.post("/api/demo/advance-day", json={"expected_version": s["version"]},
                                         headers=key()), (403, "forbidden")),
        "/api/demo/reset": (c.post("/api/demo/reset", json={}, headers=key()), (403, "forbidden")),
        "/api/cases/{id}/wait": (c.post(f"/api/cases/{cid}/wait", json={"until_day": s["day"] + 1, "reason": "x"},
                                        headers=key()), (404, "not_found")),
    }
    assert {path: error(r) for path, (r, _) in refused.items()} == {path: want for path, (_, want) in refused.items()}
    assert all(r.json()["message"] == NOT_HERE for r, want in refused.values() if want[0] == 403)
    after = c.get("/api/session").json()
    assert {k: after[k] for k in ("run_id", "day", "version")} == {k: s[k] for k in ("run_id", "day", "version")}
