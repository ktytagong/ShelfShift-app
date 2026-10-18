"""Sign-in by account (06.1 D-02, D-47, D-64, D-68; ASVS V3 session rotation; 04-API-CONTRACT "Product mode (06.1)",
Identity and permissions).

An account is a store code of the workspace's reference data (a store manager, compared only against DEMO_PASSCODE)
or "HQ" (compared only against HQ_PASSCODE). Every test uses its own TestClient with its own client address, so its
wrong-passcode count starts at zero. Credentials are generated per test session (conftest) or per test, kept in
memory and checked as booleans computed before the assert, so no failure message can print one.
"""
from __future__ import annotations

import dataclasses
import json
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient

from api import accounts, caps, cases, config, db, refdata, sandbox
from api.app import COOKIE, _actor, app, body_hash
from tests.api.conftest import PASSCODE, by_tag, key

UNKNOWN = "No store with that code. Check the code and try again."
WRONG = "That passcode is not right"


def first_store() -> str:
    """The first store code of the ACTIVE template's reference data (a store manager account)."""
    with db.tx() as conn:
        template = sandbox.active_template(conn)
    return refdata.get(template["dataset_version"]).branches[0]


def address() -> str:
    return f"acct-{uuid.uuid4().hex[:10]}"


def visitor(host: str | None = None) -> TestClient:
    """A browser with no cookie, from its own client address."""
    return TestClient(app, client=(host or address(), 50000))


def wrong_count(host: str) -> int:
    """How many wrong passcodes this client address has spent in its current windows."""
    with db.tx() as conn:
        return db.one(conn, "SELECT coalesce(sum(count), 0) AS n FROM usage_counts WHERE scope = :s "
                            "AND kind = 'passcode'", s=caps.client_scope(host))["n"]


def session_row(sid: str) -> dict | None:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM sessions WHERE session_id = :s", s=sid)


def sign_in(c: TestClient, account: str, passcode: str = PASSCODE, headers: dict | None = None):
    return c.post("/api/session", json={"account": account, "passcode": passcode}, headers=headers or {})


def error(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]


# --- Task 1: a store manager signs in; the session rotates ------------------------------------------------------------

def test_a_store_manager_signs_in_with_a_store_code(template):
    store, c = first_store(), visitor()
    r = sign_in(c, store)
    assert r.status_code == 200, r.json()
    body = r.json()
    assert isinstance(body["reset_ms"], int) and body["reset_ms"] > 0
    assert body == {"ok": True, "reset_ms": body["reset_ms"], "account": store, "role": "manager", "branch": store}
    sid = c.cookies.get(COOKIE)
    assert sid
    row = session_row(sid)
    assert (row["account"], row["role"], row["branch"]) == (store, "manager", store) and row["run_id"]


def test_signing_in_again_rotates_the_session_and_keeps_the_workspace(template):
    store, c = first_store(), visitor()
    assert sign_in(c, store).status_code == 200
    old = c.cookies.get(COOKIE)
    before = session_row(old)
    r = sign_in(c, store, headers=key())
    assert r.status_code == 200, r.json()
    new = c.cookies.get(COOKIE)
    assert new and new != old
    after = session_row(new)
    assert (after["run_id"], after["last_reset_ms"]) == (before["run_id"], before["last_reset_ms"])
    assert session_row(old) is None  # the old row is gone in the same transaction
    assert r.json() == {"ok": True, "reset_ms": before["last_reset_ms"], "account": store, "role": "manager",
                        "branch": store}
    assert c.get("/api/session").json()["run_id"] == before["run_id"]


def test_a_repeated_sign_in_returns_the_first_answer_and_the_same_rotated_cookie(template):
    store, c = first_store(), visitor()
    assert sign_in(c, store).status_code == 200
    old = c.cookies.get(COOKIE)
    run_id = session_row(old)["run_id"]
    k = key()
    first = sign_in(c, store, headers=k)
    assert first.status_code == 200, first.json()
    rotated = c.cookies.get(COOKIE)
    again = visitor()  # the same browser retrying with its old cookie and the same key
    again.cookies.set(COOKIE, old)
    repeat = sign_in(again, store, headers=k)
    assert repeat.status_code == 200, repeat.json()
    assert repeat.json() == first.json()
    assert repeat.cookies.get(COOKIE) == rotated
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM sessions WHERE run_id = :r", r=run_id)["n"] == 1
    # with a cookie the key is required
    assert error(sign_in(visitor_with(rotated), store)) == (400, "idempotency_key")


def visitor_with(sid: str) -> TestClient:
    c = visitor()
    c.cookies.set(COOKIE, sid)
    return c


def test_an_unknown_store_code_is_refused_and_spends_nothing(template):
    host = address()
    c = visitor(host)
    r = sign_in(c, "NOPE-999")
    assert error(r) == (400, "unknown_account") and r.json()["message"] == UNKNOWN
    assert wrong_count(host) == 0 and not c.cookies.get(COOKIE)
    wrong = sign_in(c, first_store(), PASSCODE + "x")
    assert error(wrong) == (401, "bad_passcode") and wrong.json()["message"] == WRONG
    left = wrong.json().get("attempts_left")  # build/deploy's D-24 hunks add it; build/api has none yet
    assert left is None or isinstance(left, int)
    assert wrong_count(host) == 1
    missing = sign_in(c, "NOPE-999", "")  # a wrong credential is checked before the account, so it spends
    assert error(missing) == (401, "bad_passcode") and wrong_count(host) == 2


def test_the_sign_in_key_row_hashes_the_account_only(template):
    store, c = first_store(), visitor()
    assert sign_in(c, store).status_code == 200
    k = key()
    assert sign_in(c, store, headers=k).status_code == 200
    with db.tx() as conn:
        row = db.one(conn, "SELECT body_hash, response FROM idempotency_keys WHERE key = :k AND endpoint = 'sign-in'",
                     k=k["Idempotency-Key"])
    assert row is not None
    hashes_account_only = row["body_hash"] == body_hash({"account": store})
    leaked = PASSCODE in json.dumps(row["response"])
    assert hashes_account_only and not leaked


# --- Task 2: HQ's own credential, the startup refusal, sign-out, the session's account, history's actor ----------------

@dataclasses.dataclass
class Secret:
    """A credential held in memory only; its repr never shows the value."""
    value: str = dataclasses.field(repr=False)


@pytest.fixture
def hq(monkeypatch) -> Secret:
    """A sound HQ credential generated for this test (never printed)."""
    held = Secret(secrets.token_urlsafe(16))
    monkeypatch.setattr(config, "HQ_PASSCODE", held.value, raising=False)
    return held


def test_hq_signs_in_only_with_its_own_credential(template, hq):
    host = address()
    c = visitor(host)
    r = sign_in(c, "HQ", hq.value)
    assert r.status_code == 200, r.json()
    assert {k: r.json()[k] for k in ("account", "role", "branch")} == {"account": "HQ", "role": "hq", "branch": None}
    assert session_row(c.cookies.get(COOKIE))["account"] == "HQ"
    store_on_hq = sign_in(visitor(host), "HQ", PASSCODE)  # the store passcode never opens HQ
    hq_on_store = sign_in(visitor(host), first_store(), hq.value)  # nor the HQ value a store
    assert error(store_on_hq) == (401, "bad_passcode") and error(hq_on_store) == (401, "bad_passcode")
    assert wrong_count(host) == 2


@pytest.mark.parametrize("problem", ["unset", "short", "duplicate"])
def test_the_app_refuses_to_start_without_a_sound_hq_credential(template, monkeypatch, problem):
    value = {"unset": "", "short": secrets.token_urlsafe(16)[:config_min() - 1],
             "duplicate": config.DEMO_PASSCODE}[problem]
    rule = {"unset": "is not set", "short": f"shorter than {config_min()} characters",
            "duplicate": "equals DEMO_PASSCODE"}[problem]
    monkeypatch.setattr(config, "HQ_PASSCODE", value, raising=False)
    with pytest.raises(RuntimeError) as refused:
        with TestClient(app):
            pass
    text = str(refused.value)
    names_the_key_and_rule = "HQ_PASSCODE" in text and rule in text
    shows_a_value = bool(value) and value in text or config.DEMO_PASSCODE in text
    assert names_the_key_and_rule and not shows_a_value
    # and HQ never opens on it, not even with the store passcode
    for given in (value, PASSCODE):
        assert error(sign_in(visitor(), "HQ", given)) == (401, "bad_passcode")


def config_min() -> int:
    return getattr(config, "HQ_MIN_LENGTH", 12)


def test_the_app_starts_with_a_sound_hq_credential(template, hq):
    with TestClient(app) as c:
        assert c.get("/api/healthz").json() == {"ok": True}


def test_sign_out_keeps_the_workspace_and_locks_every_private_route(template):
    store, c = first_store(), visitor()
    assert sign_in(c, store).status_code == 200
    run_id = c.get("/api/session").json()["run_id"]
    k = key()
    out = c.post("/api/session/sign-out", json={}, headers=k)
    assert out.status_code == 200 and out.json() == {"signed_out": True}
    again = c.post("/api/session/sign-out", json={}, headers=k)  # the same key: the first answer
    assert again.status_code == 200 and again.json() == {"signed_out": True}
    locked = c.get("/api/session")
    assert error(locked) == (401, "locked") and locked.json()["message"] == "Sign in to continue."
    assert error(c.get("/api/cases")) == (401, "locked")
    twice = c.post("/api/session/sign-out", json={}, headers=key())  # signing out a signed-out browser changes nothing
    assert twice.status_code == 200 and twice.json() == {"signed_out": True}
    back = sign_in(c, store, headers=key())
    assert back.status_code == 200, back.json()
    assert c.get("/api/session").json()["run_id"] == run_id


def test_the_session_names_its_account_and_lists_no_seats(template):
    store, c = first_store(), visitor()
    assert sign_in(c, store).status_code == 200
    s = c.get("/api/session").json()
    assert (s["account"], s["role"], s["branch"]) == (store, "manager", store) and "seats" not in s


def test_history_names_its_actor_and_scenario_date_in_recorded_order(template):
    store, c = first_store(), visitor()
    assert sign_in(c, store).status_code == 200
    run_id = c.get("/api/session").json()["run_id"]
    case = by_tag(run_id, cases.STORY_TAG)
    person = {"account": store, "role": "manager", "branch": store}
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        db.log(conn, run_id, run["day"], "probe_actor", case["case_id"], actor=person)
        db.log(conn, run_id, run["day"], "probe_seat", case["case_id"], role="hq", branch=None)
    ref = refdata.get(run["dataset_version"])
    entries = c.get(f"/api/cases/{case['case_id']}/activity").json()
    assert entries and all({"actor", "scenario_date"} <= set(e) for e in entries)
    assert all(e["scenario_date"] == ref.date(e["day"]).isoformat() for e in entries)
    by_type = {e["type"]: e for e in entries}
    assert by_type["probe_actor"]["actor"] == person
    assert by_type["probe_seat"]["actor"] == {"account": "HQ", "role": "hq", "branch": None}
    engine = [e for e in entries if not {"actor", "role", "by", "branch"} & set(e["payload"])]
    assert engine and all(e["actor"] is None for e in engine)
    assert [e["at"] for e in entries] == sorted(e["at"] for e in entries)


def test_permission_checks_read_the_account_not_the_body(template):
    """The assumption-delta invariant (06.1-05): forged role, branch, seat or account fields in a write's body change
    nothing; `_actor` reads the session row only."""
    c = visitor()
    with db.tx() as conn:
        ref = refdata.get(sandbox.active_template(conn)["dataset_version"])
    assert sign_in(c, first_store()).status_code == 200
    run_id = c.get("/api/session").json()["run_id"]
    case = by_tag(run_id, cases.STORY_TAG)
    src = case["source_branch"]
    other = next(b for b in ref.branches if b != src)
    c2 = visitor()
    assert sign_in(c2, other).status_code == 200  # a manager who may not take or send this case
    run2 = c2.get("/api/session").json()["run_id"]
    case2 = by_tag(run2, cases.STORY_TAG)
    v = c2.get(f"/api/cases/{case2['case_id']}").json()["rec_version"]
    forged = {"role": "manager", "branch": src, "account": src, "seat": {"role": "hq", "branch": None}}
    # 06.1 D-54, D-67: an unowned case is taken before it is sent, and taking it is the seat check (source or HQ)
    path = f"/api/cases/{case2['case_id']}/take-ownership"
    plain = c2.post(path, json={"rec_version": v}, headers=key())
    lying = c2.post(path, json={"rec_version": v, **forged}, headers=key())
    assert error(plain) == error(lying) == (403, "forbidden")
    assert plain.json()["message"] == lying.json()["message"]
    assert by_tag(run2, cases.STORY_TAG)["state"] == case2["state"]
    row = session_row(c2.cookies.get(COOKIE))
    assert _actor({**row, "body": forged}) == {"account": other, "role": "manager", "branch": other,
                                               "session_id": row["session_id"]}
    assert accounts.is_hq(" hq ") and not accounts.is_hq(src)
