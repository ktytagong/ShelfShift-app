"""The demo path over HTTP: sessions, routes, roles, idempotency, reset and the worker's place in the app
(04-API-CONTRACT; FLOW-01, FLOW-02, FLOW-06, FLOW-10, OPS-06).

Every call goes through the real app with a TestClient, as Phase 5 will. The story case is found by its tag on the
board, never by a key, a lot id or an amount from the seed; branches come from the case itself (its source and its
pick's destination). Expected values are the contract's rules and beats (the label, the error codes, the day the
demo opens on), or derived in the test from the calendar (`data.world.demo_date`), never pasted from a response.
"""
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import cases, config, db, evaluate, sandbox, worker
from api.app import COOKIE, app
from data.world import demo_date
from engine import evaluate as engine_evaluate
from engine import from_dict, rec_version
from engine.model import ENGINE_VERSION
from tests.api.conftest import PASSCODE, as_seat, first_store, key, own, sign_in, take_and_send
from tests.api.verify_helpers import ack

LABEL = "Synthetic data · hypothetical scenario"  # the contract's label (D-16)


PUBLIC = {("GET", "/api/about"), ("GET", "/api/healthz"), ("GET", "/api/readyz"), ("POST", "/api/session")}
LATER = ("samples", "inventory", "metrics", "fact-checks", "upload")  # Phases 8, 10 (06.1 serves GET /api/transfers
# and /api/forecasts; 07-07 the file read; 07-13 dismiss and withdraw a field report; 09-07 serves /brief and /chat;
# 11 serves the public About)


def story(client) -> dict:
    """SUS-19's card, found by its milk-story tag."""
    return next(c for c in client.get("/api/cases").json()["cases"] if c["tag"] == cases.STORY_TAG)


def post(client, path, body=None, **kw):
    return client.post(path, json={} if body is None else body, headers=key(), **kw)


def detail(client, case_id) -> dict:
    return client.get(f"/api/cases/{case_id}").json()


def activity_types(client, case_id) -> list[str]:
    return [a["type"] for a in client.get(f"/api/cases/{case_id}/activity").json()]


def error(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]


def advance(client) -> dict:
    s = client.get("/api/session").json()
    r = post(client, "/api/demo/advance-day", {"expected_version": s["version"]})
    assert r.status_code == 200, r.json()
    return r.json()


def phantom(d: dict) -> dict:
    """The active version's plan-changing phantom fact on the case detail (a D-15 beat of SUS-19)."""
    fact = next((f for f in d["fact_checks"] if f["kind"] == "PHANTOM_STOCK" and f["changes_plan"]), None)
    assert fact is not None, "D-15 hand-back: SUS-19 has no plan-changing phantom fact"
    return fact


def multipart(text: str, counts, observed: str | None = "now") -> dict:
    """The answer's form fields as multipart parts (no filename, so none is a file). `observed` is "When did you
    check?" ("Checked just now" unless a test is about the check time; None leaves the field out; 07-CONTEXT D-22)."""
    parts = {"text": (None, text), "counts": (None, counts if isinstance(counts, str) else json.dumps(counts))}
    return parts if observed is None else {**parts, "observed": (None, observed)}


def test_a_visitor_reaches_sus19_and_sends_it_over_http(template):
    c = TestClient(app)
    opened = sign_in(c)
    assert opened.status_code == 200 and opened.json()["ok"] is True and opened.json()["reset_ms"] > 0
    assert c.cookies.get(COOKIE)
    session = c.get("/api/session").json()
    assert session["label"] == LABEL and session["role"] == "manager"
    board = c.get("/api/cases").json()
    assert board["day"] == session["day"] == 3  # the demo opens on Day 3 (spec §5)
    card = story(c)
    assert card["state"] == "OPEN" and card["source"] == session["branch"]  # the default seat is SUS-19's source
    assert card["decide_by"] is not None and card["due"]["label"].startswith("Decide by ")
    assert card["wait"] is not None and "text" not in card["wait"] and isinstance(card["wait"]["departs_min"], int)
    detail = c.get(f"/api/cases/{card['case_id']}").json()
    assert detail["rec_version"] == card["rec_version"] and detail["versions"]["label"] == LABEL
    sent = take_and_send(c, card["case_id"], detail["rec_version"])  # the owner sends (06.1 D-54)
    assert sent.status_code == 200, sent.json()
    assert sent.json()["state"] == "PENDING_APPROVAL" and sent.json()["round"] >= 1
    assert story(c)["state"] == "PENDING_APPROVAL"


def test_every_private_route_needs_the_passcode_session(template):
    c = TestClient(app)
    routes = [(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods]
    private = [(m, p) for m, p in routes if (m, p) not in PUBLIC]
    core = [(m, r.path) for r in app.routes if isinstance(r, APIRoute) and r.endpoint.__module__ == "api.app"
            for m in r.methods if (m, r.path) not in PUBLIC]
    assert len(core) == 40  # app.py's demo-path routes, less session, healthz and readyz (06.1: sign-out in; seat
    # switch and Wait retired, 06.1-06; Phase 7: Can't check and Tasks, 07-05; the file read, 07-07; place and
    # release a lot hold, 07-08; Reviewed · keep plan, 07-09; Select another plan, 07-10; Cancel approved plan, 07-11;
    # Reassess, 07-12; file, list, read, withdraw and dismiss a field report, 07-13; Mark done and edit a report,
    # 07-14; the impact preview, 07-15; the preview before filing, 07-20)
    assert ("POST", "/api/cases/{case_id}/take-ownership") in private  # a route module's (06.1 D-25), checked too
    for method, path in private:
        r = c.request(method, re.sub(r"\{[^}]+\}", "x", path), json={}, headers=key())
        assert error(r) == (401, "locked"), (method, path)
        assert r.json()["message"] == "Sign in to continue."  # (proposed) 06.1 contract
    paths = {p for _, p in routes}
    assert not [p for p in paths for word in LATER if word in p]
    assert "/api/signals" in paths and "/api/signals/{signal_id}" in paths  # field reports (07-13)
    assert c.get("/api/healthz").json() == {"ok": True}
    readiness = c.get("/api/readyz")
    assert readiness.status_code == 200
    health = readiness.json()
    assert list(health) == ["ok", "db", "vertex_ai", "cloud_storage", "data_bucket", "uploads_bucket", "model_id", "checked_at"]
    assert health["ok"] is True and health["db"] == "ok"
    assert [health[k] for k in ("vertex_ai", "cloud_storage", "uploads_bucket")] == ["disabled"] * 3
    assert health["data_bucket"] == "not_checked" and health["model_id"] == config.GEMINI_MODEL
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", health["checked_at"])
    assert error(c.get("/api/metrics")) == (404, "not_found")
    # a session row with no sandbox is locked too, and a write through it changes nothing
    with db.tx() as conn:
        db.run(conn, "INSERT INTO sessions (session_id, role, branch) VALUES ('no-sandbox', 'manager', NULL)")
    c.cookies.set(COOKIE, "no-sandbox")
    assert error(c.get("/api/cases")) == (401, "locked")
    assert error(post(c, "/api/demo/reset")) == (401, "locked")
    with db.tx() as conn:
        assert db.one(conn, "SELECT run_id FROM sessions WHERE session_id = 'no-sandbox'")["run_id"] is None
        assert db.one(conn, "SELECT count(*) AS n FROM idempotency_keys WHERE session_id = 'no-sandbox'")["n"] == 0


def test_a_wrong_passcode_is_refused(template):
    c = TestClient(app)
    for wrong in ("nope", PASSCODE + "x", "pässcode", 12345, None):  # with a valid store account (06.1 D-47)
        r = c.post("/api/session", json={"account": first_store(), "passcode": wrong})
        assert error(r) == (401, "bad_passcode") and r.json()["message"] == "That passcode is not right"
    assert COOKIE not in r.cookies and not c.cookies.get(COOKIE)
    assert error(c.post("/api/session", content=b"{not json", headers={"content-type": "application/json"})) == \
        (400, "bad_request")


TOO_LARGE = (400, "bad_request")


def test_an_oversized_or_too_deep_body_is_a_plain_400(template, client):
    """WR-02: the body is read up to the settings limit (http.max_body_bytes), before the passcode check, whether or
    not the client sends a Content-Length; too large or too deeply nested JSON is 400 bad_request in the envelope."""
    limit = config.MAX_BODY_BYTES
    assert limit == config.SETTINGS["http"]["max_body_bytes"]  # a setting, never a code constant
    c = TestClient(app, raise_server_exceptions=False, client=("wr02", 50000))
    big = json.dumps({"passcode": "x" * limit}).encode()
    for r in (c.post("/api/session", content=big, headers={"content-type": "application/json"}),
              c.post("/api/session", content=iter([big[:limit // 2], big[limit // 2:]]),  # chunked, no length
                     headers={"content-type": "application/json"})):
        assert error(r) == TOO_LARGE and r.json()["message"] == "The request body is too large"
    deep = b"[" * 20000 + b"]" * 20000  # a RecursionError in the parser
    assert len(deep) < limit
    r = c.post("/api/session", content=deep, headers={"content-type": "application/json"})
    assert error(r) == TOO_LARGE and r.json()["message"] == "The request body must be JSON"
    # the count answer's multipart form: the same bound, and its counts field parsed the same way
    lost = client.post("/api/requests/none/answer", files=multipart("", deep.decode()), headers=key())
    assert error(lost) == TOO_LARGE
    huge = client.post("/api/requests/none/answer", files=multipart("x" * limit, {}), headers=key())
    assert error(huge) == TOO_LARGE and huge.json()["message"] == "The request body is too large"


def test_a_blank_passcode_refuses_to_start_and_opens_no_session(template, monkeypatch):
    """CR-01: an unset or blank DEMO_PASSCODE fails closed: the app does not start (a plain error naming the variable,
    never a value) and no body, empty or not, opens a session."""
    for blank in ("", " \n"):
        monkeypatch.setattr(config, "DEMO_PASSCODE", blank)
        with pytest.raises(RuntimeError) as refused:
            with TestClient(app):
                pass
        assert str(refused.value) == "DEMO_PASSCODE is not set; refusing to start"
        c = TestClient(app, client=("cr01-blank", 50000))
        store = first_store()
        for body in ({}, {"account": store}, {"account": store, "passcode": ""}, {"account": store, "passcode": blank},
                     {"account": store, "passcode": None}):
            assert error(c.post("/api/session", json=body)) == (401, "bad_passcode")
        assert not c.cookies.get(COOKIE)


def test_the_passcode_is_compared_trimmed_on_both_sides(template, monkeypatch):
    """CR-01: a secret stored with a trailing newline still matches the passcode a visitor types."""
    monkeypatch.setattr(config, "DEMO_PASSCODE", PASSCODE + "\n")
    c = TestClient(app, client=("cr01-trim", 50000))
    assert c.post("/api/session", json={"account": first_store(), "passcode": f" {PASSCODE} "}).status_code == 200


def test_the_code_holds_no_passcode_default():
    """CR-01: with DEMO_PASSCODE unset, config has no passcode at all (no default in code); a set one is trimmed.
    The child process prints only booleans, never a value."""
    root = pathlib.Path(__file__).resolve().parents[2]
    probe = "import os; from api import config; print(config.DEMO_PASSCODE == os.environ.get('WANT', ''))"
    env = {k: v for k, v in os.environ.items() if k != "DEMO_PASSCODE"}
    token = secrets.token_urlsafe(12)
    for extra in ({}, {"DEMO_PASSCODE": f"{token}\n", "WANT": token}):
        out = subprocess.run([sys.executable, "-c", probe], env={**env, **extra}, cwd=root, capture_output=True,
                             text=True)
        assert out.returncode == 0 and out.stdout.strip() == "True"


def test_each_session_gets_its_own_sandbox(client):
    other = TestClient(app)
    sign_in(other)
    mine, theirs = client.get("/api/session").json(), other.get("/api/session").json()
    assert mine["run_id"] != theirs["run_id"]
    again = sign_in(client)  # a session with a sandbox gets it back, under a rotated session id (06.1)
    assert again.json() == {"ok": True, "reset_ms": mine["last_reset_ms"], "account": mine["account"],
                            "role": mine["role"], "branch": mine["branch"]}
    assert client.get("/api/session").json()["run_id"] == mine["run_id"]


def test_signing_in_as_another_account_changes_the_seat(client):
    dest = story(client)["dest"]
    as_seat(client, dest)  # sign out, then in as the destination's store manager (06.1-06: no seat switch)
    s = client.get("/api/session").json()
    d = demo_date(3)  # Day 3, the demo's opening day; every demo day opens at 09:00 (spec §5)
    assert (s["role"], s["branch"], s["day"]) == ("manager", dest, 3)
    assert s["clock"] == f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, 09:00"
    assert {"day": 3, "date": d.isoformat(), "weekday": d.strftime("%a")} in s["calendar"]
    assert s["account"] == dest and "seats" not in s  # 06.1: the seat is the account's; no seat list
    as_seat(client, None)
    s = client.get("/api/session").json()
    assert (s["account"], s["role"], s["branch"], s["open_requests"]) == ("HQ", "hq", None, 0)
    for body in ({"role": "manager", "branch": dest}, {"role": "admin"}, {"role": "manager"}):
        r = post(client, "/api/demo/role", body)  # the seat switch is retired (06.1 D-04)
        assert error(r) == (404, "not_found") and r.json()["message"] == "No such route"
    assert client.get("/api/session").json()["role"] == "hq"


def test_the_seat_switch_is_gone_in_and_outside_demo_mode(client, monkeypatch):
    before = client.get("/api/session").json()
    for demo_mode in (True, False):
        monkeypatch.setattr(config, "DEMO_MODE", demo_mode)
        r = post(client, "/api/demo/role", {"role": "hq"})
        assert error(r) == (404, "not_found") and r.json()["message"] == "No such route"
        after = client.get("/api/session").json()
        assert (after["role"], after["branch"], after["demo_mode"]) == (before["role"], before["branch"], demo_mode)


def test_the_judges_path_over_http(client):
    card = story(client)
    cid, src, dest, v = card["case_id"], card["source"], card["dest"], card["rec_version"]
    assert take_and_send(client, cid, v).json()["state"] == "PENDING_APPROVAL"
    given = ack(client.get("/api/session").json()["run_id"], cid)  # the Advisory facts (07-CONTEXT D-26)
    as_seat(client, dest)
    page = card["suggested_key"]  # approve and accept name the plan the page shows (07 review C)
    r = post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "accept", "acknowledged": given,
                                                    "plan_key": page})
    assert r.json() == {"state": "PENDING_APPROVAL"}
    assert story(client)["next_owner"] == [{"role": "manager", "branch": src}]
    as_seat(client, src)
    assert post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "approve", "plan_key": page,
                                                       "acknowledged": given}).json() == {"state": "APPROVED"}
    assert post(client, f"/api/cases/{cid}/prepare").json() == {"state": "PREPARED"}
    s = client.get("/api/session").json()
    assert advance(client) == {"advanced": True, "day": s["day"] + 1, "version": s["version"] + 1}
    assert client.get("/api/cases").json()["day"] == s["day"] + 1 == 4
    # Day 4: the plan ran, and SUS-19 opens again for its second look (the user's story, 04-SECOND-LOOK-FIX):
    # the seed's run leaves again today and moving more still beats keeping on the base scenario
    day4, ran = story(client), cases.parse_plan(card["suggested_key"])
    again = cases.parse_plan(day4["suggested_key"])
    assert (day4["state"], day4["decisions"]) == ("OPEN", {}) and day4["rec_version"] != v
    assert "Second look" in [b["text"] for b in day4["badges"]]
    assert (again.kind, again.dest, again.run_code) == ("TRANSFER", dest, ran.run_code)  # the same run, a day later
    assert day4["est_benefit"] > 0  # the Day-4 plan beats keep on the base scenario
    types = activity_types(client, cid)
    steps = ["sent", "decision", "decision", "committed", "prepared", "executed", "second_look"]
    assert [t for t in types if t in steps][-len(steps):] == steps


def test_a_wrong_seat_gets_403_over_http(client):
    card = story(client)
    cid, src, dest, v = card["case_id"], card["source"], card["dest"], card["rec_version"]
    as_seat(client, None)
    r = post(client, f"/api/cases/{cid}/send", {"rec_version": v})
    assert error(r) == (409, "not_owned") and story(client)["state"] == "OPEN"  # 06.1 D-54: nobody owns it yet
    as_seat(client, dest)
    assert error(post(client, f"/api/cases/{cid}/take-ownership")) == (403, "forbidden")  # 06.1 D-67: the source's
    as_seat(client, src)
    assert take_and_send(client, cid, v).status_code == 200
    as_seat(client, dest)
    assert error(post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "approve"})) == \
        (403, "forbidden")
    as_seat(client, src)
    assert error(post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": "accept"})) == \
        (403, "forbidden")
    assert story(client)["decisions"] == {}


def test_the_count_path_over_http(client):
    card = story(client)
    cid, v = card["case_id"], card["rec_version"]
    fact = phantom(detail(client, cid))
    r = post(client, f"/api/cases/{cid}/requests", {"rec_version": v, "fact_id": fact["fact_id"]})
    assert r.status_code == 200, r.json()
    q = r.json()
    assert (q["to_branch"], q["lot_ids"], q["state"]) == (fact["branch"], fact["lot_ids"], "OPEN")
    as_seat(client, fact["branch"])
    assert q["request_id"] in [x["request_id"] for x in client.get("/api/requests").json()]
    assert client.get("/api/session").json()["open_requests"] >= 1
    counts = {lot: {"shelf_units": 0, "backroom_units": 0} for lot in q["lot_ids"]}  # the test's own count
    path = f"/api/requests/{q['request_id']}/answer"
    photo = client.post(path, files={**multipart("", counts), "file": ("shelf.png", b"\x89PNG\r\n", "image/png")},
                        headers=key())
    assert error(photo) == (415, "bad_file")
    assert photo.json()["message"] == "Only PNG, JPEG or PDF files can be attached"  # a cut PNG signature (07-07)
    assert error(client.post(path, files=multipart("", "{not json"), headers=key())) == (400, "bad_request")
    ans = client.post(path, files=multipart("counted both", counts), headers=key())
    assert ans.status_code == 200, ans.json()
    assert (ans.json()["kind"], ans.json()["state"], ans.json()["fields"]["needs_input"]) == ("STOCK_COUNT", "READ", [])
    done = post(client, f"/api/signals/{ans.json()['signal_id']}/confirm",
                {"revision": ans.json()["revision"], "checked_both": True})
    assert done.status_code == 200, done.json()
    replanned = {c["case_id"]: c["rec_version"] for c in done.json()["cases"]}
    assert replanned[cid] != v and detail(client, cid)["rec_version"] == replanned[cid]
    types = activity_types(client, cid)
    order = [t for t in types if t in ("evidence_requested", "evidence_answered", "count_confirmed", "new_version")]
    assert order[-4:] == ["evidence_requested", "evidence_answered", "count_confirmed", "new_version"]


def test_a_recount_replaces_an_answered_request_and_keeps_its_answer(client):
    """G-05-2 item 12: Request recount is a new request for the same fact; the original stays readable, SUPERSEDED,
    with its answer untouched, and the new count follows the addressed manager's normal confirm (VER-05)."""
    card = story(client)
    cid, v, src = card["case_id"], card["rec_version"], card["source"]
    fact = phantom(detail(client, cid))
    q = post(client, f"/api/cases/{cid}/requests", {"rec_version": v, "fact_id": fact["fact_id"]}).json()
    as_seat(client, q["to_branch"])
    counts = {lot: {"shelf_units": 2, "backroom_units": 1} for lot in q["lot_ids"]}  # the test's own count
    ans = client.post(f"/api/requests/{q['request_id']}/answer", files=multipart("counted both", counts),
                      headers=key())
    assert ans.status_code == 200, ans.json()
    before = next(r for r in detail(client, cid)["requests"] if r["request_id"] == q["request_id"])
    assert before["state"] == "ANSWERED"
    as_seat(client, src)
    r = post(client, f"/api/cases/{cid}/requests",
             {"rec_version": v, "fact_id": fact["fact_id"], "recount_of": q["request_id"]})
    assert r.status_code == 200, r.json()
    new = r.json()
    assert new["request_id"] != q["request_id"]
    assert (new["state"], new["fact_id"], new["lot_ids"]) == ("OPEN", fact["fact_id"], q["lot_ids"])
    requests = {x["request_id"]: x for x in detail(client, cid)["requests"]}
    old = requests[q["request_id"]]
    assert (old["state"], old["fact_id"], old["replaced_by"]) == ("SUPERSEDED", fact["fact_id"], new["request_id"])
    assert old["answer"] == before["answer"]  # the original response is kept as it was
    assert requests[new["request_id"]]["state"] == "OPEN"
    as_seat(client, q["to_branch"])
    stale = post(client, f"/api/signals/{ans.json()['signal_id']}/confirm",
                 {"revision": ans.json()["revision"], "checked_both": True})
    assert error(stale) == (409, "wrong_state") and stale.json()["message"] == "Request is SUPERSEDED"
    late = client.post(f"/api/requests/{q['request_id']}/answer", files=multipart("", counts), headers=key())
    assert late.status_code == 200, late.json()  # stored for the record (07-CONTEXT D-07), never confirmable:
    late_confirm = post(client, f"/api/signals/{late.json()['signal_id']}/confirm",
                        {"revision": late.json()["revision"], "checked_both": True})
    assert error(late_confirm) == (409, "wrong_state") and late_confirm.json()["message"] == "Request is SUPERSEDED"
    again = client.post(f"/api/requests/{new['request_id']}/answer", files=multipart("recounted", counts),
                        headers=key())
    assert again.status_code == 200, again.json()
    done = post(client, f"/api/signals/{again.json()['signal_id']}/confirm",
                {"revision": again.json()["revision"], "checked_both": True})
    assert done.status_code == 200, done.json()
    assert detail(client, cid)["rec_version"] != v
    kept = next(x for x in detail(client, cid)["requests"] if x["request_id"] == q["request_id"])
    assert (kept["state"], kept["answer"]["signal_id"]) == ("SUPERSEDED", late.json()["signal_id"])
    with db.tx() as conn:  # the original response is kept too, as the earlier revision (D-22)
        first = db.one(conn, "SELECT * FROM signals WHERE signal_id = :s", s=before["answer"]["signal_id"])
    assert (first["revision"], first["state"], first["fields"]) == (before["answer"]["revision"], "DISMISSED",
                                                                    before["answer"]["fields"])


def test_an_answer_states_when_the_branch_checked_over_http(client):
    """07-CONTEXT D-22 (acceptance check 17, first half): the multipart field `observed` reaches the service; without
    it the answer is the 400 envelope with code check_time and nothing is stored; the idempotency body hash covers it."""
    card = story(client)
    cid, v = card["case_id"], card["rec_version"]
    fact = phantom(detail(client, cid))
    q = post(client, f"/api/cases/{cid}/requests", {"rec_version": v, "fact_id": fact["fact_id"]}).json()
    as_seat(client, q["to_branch"])
    counts = {lot: {"shelf_units": 3, "backroom_units": 0} for lot in q["lot_ids"]}  # the test's own count
    path = f"/api/requests/{q['request_id']}/answer"
    for observed in (None, "", "2026-10-16T20:30:00"):  # missing, blank, naive (no offset)
        r = client.post(path, files=multipart("counted", counts, observed), headers=key())
        assert error(r) == (400, "check_time"), r.json()
        assert r.json()["message"] == "Choose when you checked: Checked just now, or a date and time."
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM signals WHERE request_id = :q", q=q["request_id"]) == []
    once = key()
    first = client.post(path, files=multipart("counted", counts), headers=once)
    assert first.status_code == 200, first.json()
    with db.tx() as conn:
        row = db.one(conn, "SELECT observed_at, created_at FROM signals WHERE signal_id = :s",
                     s=first.json()["signal_id"])
    assert row["observed_at"] == row["created_at"]  # "Checked just now" is the server's now() at submission
    assert first.json()["observed_at"] == row["observed_at"].astimezone(ZoneInfo("UTC")).isoformat()
    assert client.post(path, files=multipart("counted", counts), headers=once).json() == first.json()  # a repeat
    other = client.post(path, files=multipart("counted", counts, row["observed_at"].isoformat()), headers=once)
    assert error(other) == (409, "idempotency_conflict")  # the same key with another check time is another request


def test_cant_check_and_tasks_over_http(client):
    """07-CONTEXT D-04, D-07 over HTTP: Can't check needs an Idempotency-Key and answers the request with state
    CANT_CHECK; GET /api/tasks answers the contract's keys; the session's tasks_waiting is the counts' sum."""
    card = story(client)
    cid, v = card["case_id"], card["rec_version"]
    fact = phantom(detail(client, cid))
    q = post(client, f"/api/cases/{cid}/requests", {"rec_version": v, "fact_id": fact["fact_id"]}).json()
    as_seat(client, q["to_branch"])
    t = client.get("/api/tasks")
    assert t.status_code == 200, t.json()
    assert set(t.json()) == {"requests", "holds", "advisory", "counts", "server_now", "tiles", "list"}  # 07-19 adds two
    assert set(t.json()["counts"]) == {"to_answer", "to_confirm", "lot_holds", "advisory"}
    assert q["request_id"] in [r["request_id"] for r in t.json()["requests"]]
    assert t.json()["counts"]["to_answer"] >= 1
    assert client.get("/api/session").json()["tasks_waiting"] == sum(t.json()["counts"].values())
    path = f"/api/requests/{q['request_id']}/cant-check"
    assert error(client.post(path, json={})) == (400, "idempotency_key")
    once = key()
    closed = client.post(path, json={}, headers=once)
    assert closed.status_code == 200, closed.json()
    assert (closed.json()["request_id"], closed.json()["state"]) == (q["request_id"], "CANT_CHECK")
    assert client.post(path, json={}, headers=once).json() == closed.json()  # a repeat with the key
    again = client.post(path, json={}, headers=key())
    assert error(again) == (409, "wrong_state") and again.json()["message"] == "Request is CANT_CHECK"
    after = client.get("/api/tasks").json()
    assert after["counts"]["to_answer"] == t.json()["counts"]["to_answer"] - 1
    assert client.get("/api/session").json()["tasks_waiting"] == sum(after["counts"].values())
    as_seat(client, None)
    hq = client.get("/api/tasks").json()
    assert (hq["requests"], hq["counts"]) == ([], {"to_answer": 0, "to_confirm": 0, "lot_holds": 0, "advisory": 0})
    assert client.get("/api/session").json()["tasks_waiting"] == 0


def test_whatif_adopt_reject_over_http(client):
    card = story(client)
    cid, v = card["case_id"], card["rec_version"]
    d = detail(client, cid)
    best = next(o for o in d["result"]["options"] if o["key"] == d["result"]["recommended"])
    trip = next(r for r in d["engine_input"]["runs"] if r["run_code"] == best["run_code"] and r["dest"] == best["dest"])
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    w = post(client, f"/api/cases/{cid}/simulate", {"overrides": quote})
    assert w.status_code == 200, w.json()
    assert w.json()["rec_version"] != v and w.json()["overrides"] == quote
    assert "text" not in (w.json()["result"]["wait"] or {}) and w.json()["summary"].startswith("The engine recommends")
    assert detail(client, cid)["rec_version"] == v  # a what-if never changes the active plan
    count = [{"kind": "stock_count", "lot_id": card["lot_id"], "units": 1, "counted_day": 3}]
    assert error(post(client, f"/api/cases/{cid}/simulate", {"overrides": count})) == (400, "bad_override")
    r = post(client, f"/api/cases/{cid}/adopt", {"overrides": quote, "note": " "})
    assert error(r) == (400, "bad_request") and r.json()["message"] == "Say where these assumptions come from"
    adopted = post(client, f"/api/cases/{cid}/adopt", {"overrides": quote, "note": "carrier quote by phone"}).json()
    assert adopted["rec_version"] != v and detail(client, cid)["rec_version"] == adopted["rec_version"]
    day = client.get("/api/session").json()["day"]
    r = post(client, f"/api/cases/{cid}/wait", {"until_day": day + 1, "reason": "recount tomorrow"})
    assert error(r) == (404, "not_found")  # Wait is retired (06.1 D-04)
    assert story(client)["state"] == "OPEN"
    assert error(post(client, f"/api/cases/{cid}/reject", {"reason": ""})) == (400, "bad_request")
    assert post(client, f"/api/cases/{cid}/reject", {"reason": "store will run a promotion"}).json() == \
        {"state": "REJECTED"}
    assert story(client)["column"] == "Resolved"


@pytest.mark.parametrize("binding", ["stale_version", "stale_plan", "current", "absent"])
def test_adopt_optional_reviewed_binding_is_atomic_and_backward_compatible(client, binding):
    """09-19 prerequisite: a delayed human Apply cannot adopt against a different review binding.
    Refusal preserves the pending round, capacity and lot holds; callers omitting the new fields still adopt.
    """
    from api import workflow

    cid = story(client)["case_id"]
    d = detail(client, cid)
    plan = d["result"]["recommended"]
    trip = next(r for r in d["engine_input"]["runs"]
                if r["run_code"] == cases.parse_plan(plan).run_code)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    hold = post(client, f"/api/lots/{d['lot']['lot_id']}/holds", {"reason": "Review this lot"})
    assert hold.status_code == 200, hold.json()
    sent = take_and_send(client, cid, d["rec_version"])
    assert sent.status_code == 200, sent.json()
    run = client.get("/api/session").json()["run_id"]

    def snapshot():
        with db.tx() as conn:
            return {table: sorted(db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r", r=run), key=repr)
                    for table in ("cases", "reservations", "lot_holds", "recommendations", "decisions", "activity_log")}

    before = snapshot()
    pending = next(row for row in before["cases"] if row["case_id"] == cid)
    assert pending["state"] == "PENDING_APPROVAL" and pending["active_rec_version"] == d["rec_version"]
    assert before["reservations"] and before["lot_holds"]
    body = {"overrides": quote, "note": "Carrier quote reviewed by the manager"}
    if binding != "absent":
        body.update(rec_version=d["rec_version"], plan_key=plan)
    if binding == "stale_version":
        body["rec_version"] = "earlier-reviewed-version"
    elif binding == "stale_plan":
        body["plan_key"] = next(o["key"] for o in d["result"]["options"] if o["key"] != plan)
    response = post(client, f"/api/cases/{cid}/adopt", body)
    if binding.startswith("stale_"):
        assert response.status_code == 409, response.json()
        assert error(response) == (409, binding)
        assert response.json() == ({"error": "stale_version", "message": workflow.STALE,
                                    "active_rec_version": d["rec_version"]} if binding == "stale_version" else
                                   {"error": "stale_plan", "message": workflow.PLAN_CHANGED, "plan_key": plan})
        assert snapshot() == before
    else:
        assert response.status_code == 200, response.json()
        after = detail(client, cid)
        assert after["rec_version"] == response.json()["rec_version"] != d["rec_version"]
        assert after["state"] == "OPEN" and after["adopted"][-1]["override"] == quote[0]
        assert after["adopted"][-1]["note"] == body["note"]
        assert snapshot()["lot_holds"] == before["lot_holds"]


def test_wait_is_retired_for_every_review_day(client):
    """06.1 D-04: POST /api/cases/{id}/wait is gone (it was bounded by the last review day, G-05-13); any review day,
    in or out of the old bound, is 404 not_found and the case stays as it was."""
    from api import refdata
    from tests.api.conftest import fresh
    s = client.get("/api/session").json()
    last = refdata.get(fresh(s["run_id"])["dataset_version"]).last_review_day
    card = story(client)
    for until_day in (s["day"] + 1, last, last + 1, 2147483647):
        r = post(client, f"/api/cases/{card['case_id']}/wait", {"until_day": until_day, "reason": "review then"})
        assert error(r) == (404, "not_found") and r.json()["message"] == "No such route", until_day
    assert story(client)["state"] == card["state"] == "OPEN"
    assert "waiting" not in activity_types(client, card["case_id"])


def test_advance_day_from_a_stale_page_does_not_advance_twice(client):
    s = client.get("/api/session").json()
    assert post(client, "/api/demo/advance-day", {"expected_version": s["version"]}).json()["advanced"]
    r = post(client, "/api/demo/advance-day", {"expected_version": s["version"]})  # a second tab, a new key
    assert r.json() == {"advanced": False, "day": s["day"] + 1, "version": s["version"] + 1}
    assert error(post(client, "/api/demo/advance-day", {"expected_version": "1"})) == (400, "bad_request")


def test_the_session_names_the_display_time_zone(client):
    """Spec rev 6.7 §5: recorded times are shown in the settings' zone (caps.time_zone), which the session names so
    the client holds no zone of its own (07-CONTEXT D-09)."""
    zone = client.get("/api/session").json()["time_zone"]
    assert zone == config.SETTINGS["caps"]["time_zone"]
    assert ZoneInfo(zone).key == zone  # an IANA name


def test_the_session_names_the_request_deadline_and_limit(client):
    """07-21 (07-CONTEXT D-05, D-07; 07-UI-SPEC "Request actions"): the request panel says "The branch has {N}
    minutes to respond." and the fact checks "{N} of {M} requests left on this version" before any request exists,
    so the session names both settings leaves; the client holds neither number (CLAUDE.md "Data, not hardcoding")."""
    s = client.get("/api/session").json()
    assert s["requests"] == {"response_min": config.REQUEST_RESPONSE_MIN,
                             "max_per_version": config.MAX_REQUESTS_PER_VERSION}
    assert (s["requests"]["response_min"], s["requests"]["max_per_version"]) == (60, 2)  # settings.json today


def test_after_the_last_demo_day_the_session_shows_demo_complete(client):
    s = client.get("/api/session").json()
    for _ in range(len(s["calendar"])):
        if s["demo_complete"]:
            break
        assert s["clock"] != "Demo complete"
        advance(client)
        s = client.get("/api/session").json()
    assert s["demo_complete"] and s["clock"] == "Demo complete"
    r = post(client, "/api/demo/advance-day", {"expected_version": s["version"]})
    assert error(r) == (409, "demo_complete") and r.json()["message"] == "Demo complete · Reset to start again"
    assert client.get("/api/session").json()["day"] == s["day"]


def test_two_sessions_never_see_each_others_changes(client):
    b = TestClient(app)
    sign_in(b)
    board, session, inbox = b.get("/api/cases").json(), b.get("/api/session").json(), b.get("/api/requests").json()
    card = story(client)
    cid, v = card["case_id"], card["rec_version"]
    assert take_and_send(client, cid, v).status_code == 200
    advance(client)
    as_seat(client, card["dest"])
    assert b.get("/api/cases").json() == board and b.get("/api/requests").json() == inbox
    assert b.get("/api/session").json() == session  # the seat, the clock and the run are b's own
    assert take_and_send(b, cid, v).status_code == 200  # the same demand, own sandbox


def test_responses_carry_the_synthetic_label_and_no_wait_text(client):
    s = client.get("/api/session").json()
    assert s["label"] == LABEL and s["currency"] == "USD"
    board = client.get("/api/cases").json()["cases"]
    assert board and all("text" not in (c["wait"] or {}) for c in board)
    cid = story(client)["case_id"]
    d = detail(client, cid)
    assert d["versions"]["label"] == LABEL and d["versions"]["synthetic"] is True
    assert "text" not in (d["wait"] or {}) and "text" not in (d["result"]["wait"] or {})


# --- idempotency, reset and the worker (Task 3) ---------------------------------------------------------------------

def sent_entries(client, case_id) -> int:
    return activity_types(client, case_id).count("sent")


def test_a_write_without_an_idempotency_key_is_refused(client):
    card = story(client)
    path = f"/api/cases/{card['case_id']}/send"
    for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "   "}):
        r = client.post(path, json={"rec_version": card["rec_version"]}, headers=headers)
        assert error(r) == (400, "idempotency_key") and r.json()["message"] == "Idempotency-Key header required"
    assert story(client)["state"] == "OPEN" and sent_entries(client, card["case_id"]) == 0
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM idempotency_keys WHERE run_id = :r",
                      r=client.get("/api/session").json()["run_id"])["n"] == 0


def test_a_repeated_key_returns_the_first_response_once(client):
    card = story(client)
    path, h = f"/api/cases/{card['case_id']}/send", key()
    own(client, card["case_id"])  # the owner sends (06.1 D-54)
    first = client.post(path, json={"rec_version": card["rec_version"]}, headers=h)
    again = client.post(path, json={"rec_version": card["rec_version"]}, headers=h)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert sent_entries(client, card["case_id"]) == 1  # acted once: a second send would be 409 wrong_state
    other = client.post(path, json={"rec_version": "another"}, headers=h)
    assert error(other) == (409, "idempotency_conflict")
    assert other.json()["message"] == "This Idempotency-Key was used with a different request"


def test_the_same_body_in_another_key_order_is_the_same_request(client):
    cid = story(client)["case_id"]  # an adopt (06.1-06: Wait is retired): two body fields, one activity entry per act
    d = detail(client, cid)
    best = next(o for o in d["result"]["options"] if o["key"] == d["result"]["recommended"])
    trip = next(r for r in d["engine_input"]["runs"] if r["run_code"] == best["run_code"] and r["dest"] == best["dest"])
    quote = json.dumps([{"kind": "transport_quote", "run_code": trip["run_code"],
                         "fixed_cost": trip["fixed_cost"] * 2}])
    path, h = f"/api/cases/{cid}/adopt", {**key(), "content-type": "application/json"}
    one = client.post(path, content=f'{{"overrides": {quote}, "note": "carrier quote"}}', headers=h)
    two = client.post(path, content=f'{{"note": "carrier quote", "overrides": {quote}}}', headers=h)
    assert one.status_code == two.status_code == 200 and one.json() == two.json()
    assert set(one.json()) == {"rec_version"}
    changed = client.post(path, content=f'{{"note": "another quote", "overrides": {quote}}}', headers=h)
    assert error(changed) == (409, "idempotency_conflict")
    assert activity_types(client, cid).count("assumptions_adopted") == 1


def test_simultaneous_requests_with_one_key_act_once(client):
    card = story(client)
    path, h, out = f"/api/cases/{card['case_id']}/send", key(), []
    own(client, card["case_id"])  # the owner sends (06.1 D-54)

    def go():
        out.append(client.post(path, json={"rec_version": card["rec_version"]}, headers=h))
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r.status_code for r in out] == [200, 200] and out[0].json() == out[1].json()
    assert sent_entries(client, card["case_id"]) == 1


def test_a_retried_reset_with_the_same_key_resets_once(client):
    before = client.get("/api/session").json()["run_id"]
    h = key()
    first = client.post("/api/demo/reset", json={}, headers=h)
    again = client.post("/api/demo/reset", json={}, headers=h)  # the session already moved to the new run (A13)
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert first.json()["run_id"] != before and client.get("/api/session").json()["run_id"] == first.json()["run_id"]
    with db.tx() as conn:
        assert db.one(conn, "SELECT status FROM demo_runs WHERE run_id = :r", r=before)["status"] == "RETIRED"
        assert db.one(conn, "SELECT status FROM demo_runs WHERE run_id = :r", r=first.json()["run_id"])["status"] == \
            "ACTIVE"
    assert error(client.post("/api/demo/reset", json={"again": True}, headers=h)) == (409, "idempotency_conflict")


def test_a_reset_meets_the_2_s_target(client):
    r = post(client, "/api/demo/reset")
    assert r.status_code == 200 and 1 <= r.json()["reset_ms"] < 2000  # the 2 s target, never raised
    assert client.get("/api/session").json()["last_reset_ms"] == r.json()["reset_ms"]


def test_a_reset_that_fails_partway_keeps_the_old_sandbox(client, monkeypatch):
    before = client.get("/api/session").json()["run_id"]

    def broken(conn, template):
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "('sbx-half', 'SANDBOX', 'x', 'ACTIVE', 3, 1)")
        raise RuntimeError("disk full")
    monkeypatch.setattr(sandbox, "clone", broken)
    h = key()
    with pytest.raises(RuntimeError):
        client.post("/api/demo/reset", json={}, headers=h)
    monkeypatch.undo()
    assert client.get("/api/session").json()["run_id"] == before
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM demo_runs WHERE run_id = 'sbx-half'")["n"] == 0
        assert db.one(conn, "SELECT status FROM demo_runs WHERE run_id = :r", r=before)["status"] == "ACTIVE"
    retried = client.post("/api/demo/reset", json={}, headers=h)  # the failed reset kept no key: a retry runs
    assert retried.status_code == 200 and retried.json()["run_id"] != before


def test_reset_racing_an_approval_leaves_one_consistent_sandbox(client):
    card = story(client)
    out = {}
    own(client, card["case_id"])  # the owner sends (06.1 D-54)

    def send():
        out["send"] = post(client, f"/api/cases/{card['case_id']}/send", {"rec_version": card["rec_version"]})

    def reset():
        out["reset"] = post(client, "/api/demo/reset")
    threads = [threading.Thread(target=send), threading.Thread(target=reset)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out["reset"].status_code == 200 and out["send"].status_code in (200, 409)
    if out["send"].status_code == 409:
        assert out["send"].json()["error"] == "sandbox_reset"
    now = story(client)
    holding = now["hold"] is not None and now["hold"]["until"] is not None
    assert (now["state"] == "PENDING_APPROVAL") == holding
    with db.tx() as conn:
        live = db.one(conn, "SELECT count(*) AS n FROM demo_runs d JOIN sessions s USING (run_id) "
                            "WHERE s.session_id = :s AND d.status = 'ACTIVE'", s=client.cookies.get(COOKIE))["n"]
    assert live == 1


def test_no_session_ever_points_at_a_template(client, template):
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM sessions s JOIN demo_runs d USING (run_id) "
                            "WHERE d.kind = 'TEMPLATE'")["n"] == 0
        assert db.one(conn, "SELECT kind FROM demo_runs WHERE run_id = :r",
                      r=client.get("/api/session").json()["run_id"])["kind"] == "SANDBOX"


def test_the_worker_starts_with_the_app(template, monkeypatch):
    worker.stop()
    assert worker.pid() is None
    monkeypatch.setattr(config, "ENGINE_WORKER", True)
    with TestClient(app) as c:
        assert worker.pid() is not None  # warm before the first request
        assert c.get("/api/healthz").json() == {"ok": True}
    assert worker.pid() is None and worker._pool is None


def test_page_loads_never_call_the_engine(client, monkeypatch):
    calls = []

    def spy(name, fn):
        def wrapped(*args, **kwargs):
            calls.append(name)
            return fn(*args, **kwargs)
        return wrapped
    monkeypatch.setattr(worker, "run", spy("worker.run", worker.run))
    monkeypatch.setattr(evaluate, "run_engine", spy("run_engine", evaluate.run_engine))
    monkeypatch.setattr(evaluate, "compute", spy("compute", evaluate.compute))
    cid = story(client)["case_id"]
    for path in ("/api/session", "/api/cases", f"/api/cases/{cid}", f"/api/cases/{cid}/activity", "/api/requests"):
        assert client.get(path).status_code == 200
    assert calls == []
    d = detail(client, cid)  # the spy is wired: a what-if does call the engine
    best = next(o for o in d["result"]["options"] if o["key"] == d["result"]["recommended"])
    trip = next(r for r in d["engine_input"]["runs"] if r["run_code"] == best["run_code"] and r["dest"] == best["dest"])
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 3}]
    assert post(client, f"/api/cases/{cid}/simulate", {"overrides": quote}).status_code == 200
    assert "run_engine" in calls


def test_a_pool_timeout_is_a_plain_503(client, monkeypatch):
    def exhausted():
        raise sa.exc.TimeoutError("QueuePool limit of size 5 overflow 2 reached, connection timed out")
    monkeypatch.setattr(db, "tx", exhausted)
    for r in (client.get("/api/cases"), post(client, "/api/demo/reset")):
        assert r.json() == {"error": "db_busy", "message": "The server is busy. Try again in a moment."}
        assert r.status_code == 503 and "QueuePool" not in r.text and "Traceback" not in r.text


# --- the export (04-API-CONTRACT "Export"; IN-04, D-24) -------------------------------------------------------------

EXPORT_KEYS = {"engine_input", "rec_version", "engine_version", "result", "fact_checks", "dataset_version",
               "forecast_run_id", "case", "label"}


def test_the_export_carries_the_engine_version_and_replays_offline(client):
    card = story(client)
    d = detail(client, card["case_id"])
    r = client.get(f"/api/cases/{card['case_id']}/export")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == f'attachment; filename="{card["key"]}.json"'
    bundle = r.json()
    assert set(bundle) == EXPORT_KEYS
    assert bundle["engine_version"] == ENGINE_VERSION == d["versions"]["engine_version"]
    assert bundle["rec_version"] == card["rec_version"] == d["rec_version"]
    assert bundle["case"] == card["key"] and bundle["label"] == LABEL
    assert bundle["dataset_version"] == d["versions"]["dataset_version"]
    assert bundle["forecast_run_id"] == d["versions"]["forecast_run_id"]
    assert bundle["engine_input"] == d["engine_input"]  # the stored raw input, exactly
    # the stored checks, as the engine wrote them (the detail adds the commitment rule's keys, 07-09)
    assert bundle["fact_checks"] == [{k: v for k, v in c.items() if k in cases.FACT_KEYS} for c in d["fact_checks"]]
    # offline: the bundle alone replays to the same version and the same answer
    inp = from_dict(bundle["engine_input"])
    assert rec_version(inp) == bundle["rec_version"]
    replayed = json.loads(json.dumps(engine_evaluate(inp)))
    assert replayed["recommended"] == bundle["result"]["recommended"]
    assert replayed["options"] == bundle["result"]["options"]
    assert replayed == bundle["result"]


def test_an_export_of_another_engine_version_is_refused(client):
    card = story(client)
    run_id = client.get("/api/session").json()["run_id"]
    with db.tx() as conn:
        db.run(conn, "UPDATE recommendations SET engine_input = jsonb_set(engine_input, '{engine_version}', "
                     "'\"engine-0\"') WHERE run_id = :r AND case_id = :c AND rec_version = :v",
               r=run_id, c=card["case_id"], v=card["rec_version"])
    r = client.get(f"/api/cases/{card['case_id']}/export")
    assert error(r) == (409, "engine_version")
    assert r.json()["message"] == "This case was computed by another engine version"
    assert r.json()["engine_version"] == "engine-0" and r.json()["current"] == ENGINE_VERSION
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET active_rec_version = NULL WHERE run_id = :r AND case_id = :c",
               r=run_id, c=card["case_id"])
    assert error(client.get(f"/api/cases/{card['case_id']}/export")) == (409, "missing_data")
    assert error(client.get("/api/cases/no-such-case/export")) == (404, "not_found")


def test_the_export_keeps_the_engines_wait_text(client):
    card = story(client)
    assert card["wait"] is not None  # SUS-19 has a last chance to transfer (a D-15 beat)
    shown = detail(client, card["case_id"])["result"]["wait"]
    exported = client.get(f"/api/cases/{card['case_id']}/export").json()["result"]["wait"]
    assert "text" not in shown  # never on a card or a case page (03-CONTEXT D-24)
    assert isinstance(exported["text"], str) and exported["text"]  # the export keeps the engine's own sentence
    assert {k: v for k, v in exported.items() if k != "text"} == shown


MODULE_BODY = {  # a route module's write (06.1): every body field the contract names for them, well typed
    "action_id": "no-action", "qty": 1, "lot_id": "no-lot", "condition": "good", "received_qty": 1, "damaged_qty": 0,
    "reason": "test", "confirmed": True, "discrepancy_id": "no-discrepancy", "resolution": "found_at_destination",
    "units": 1}


class Ids(dict):
    def __missing__(self, name):
        return f"no-{name}"


def test_every_write_route_needs_an_idempotency_key(client):
    """FLOW-06 over every write route (from app.routes, never a hand list): a keyless request from a logged-in client
    is 400 idempotency_key, writes no key row and changes neither the session's seat nor its run, day or version."""
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path != "/api/session"]
    writes = sorted((m, r.path) for r in routes for m in r.methods if m in ("POST", "PATCH"))
    core = [r for r in routes if r.endpoint.__module__ == "api.app" and r.methods & {"POST", "PATCH"}]
    assert len(core) == 28, writes  # app.py's 29 POST/PATCH routes, less /api/session (06.1: sign-out in; seat
    # switch and Wait retired, 06.1-06; Phase 7: Can't check, 07-05; place and release a lot hold, 07-08;
    # Reviewed · keep plan, 07-09; Select another plan, 07-10; Cancel approved plan, 07-11; Reassess, 07-12;
    # file, withdraw and dismiss a field report, 07-13; Mark done and edit a report, 07-14; the impact preview, 07-15)
    assert ("POST", "/api/requests/{request_id}/cant-check") in writes
    assert ("POST", "/api/lots/{lot_id}/holds") in writes and ("POST", "/api/holds/{hold_id}/release") in writes
    assert ("POST", "/api/cases/{case_id}/review") in writes  # 07-09: Reviewed · keep plan
    assert ("POST", "/api/cases/{case_id}/select") in writes  # 07-10: Select another plan
    assert ("POST", "/api/cases/{case_id}/cancel") in writes  # 07-11: Cancel approved plan
    assert ("POST", "/api/cases/{case_id}/reassess") in writes  # 07-12: Reassess
    assert ("POST", "/api/signals") in writes  # 07-13: file a field report
    assert ("POST", "/api/signals/{signal_id}/withdraw") in writes  # 07-13: the reporter withdraws it
    assert ("POST", "/api/signals/{signal_id}/dismiss") in writes  # 07-13: the owner dismisses it
    assert ("POST", "/api/signals/{signal_id}/done") in writes  # 07-14: Mark done on an advisory task
    assert ("PATCH", "/api/signals/{signal_id}") in writes  # 07-14: edit a report (a new revision)
    assert ("POST", "/api/signals/{signal_id}/preview") in writes  # 07-15: the impact preview
    assert ("POST", "/api/signals/preview") in writes  # 07-20: the preview before filing
    assert ("POST", "/api/cases/{case_id}/take-ownership") in writes  # a route module's (06.1 D-25), checked too
    card = story(client)
    cid, v = card["case_id"], card["rec_version"]
    fact = phantom(detail(client, cid))
    q = post(client, f"/api/cases/{cid}/requests", {"rec_version": v, "fact_id": fact["fact_id"]}).json()
    as_seat(client, fact["branch"])
    counts = {lot: {"shelf_units": 0, "backroom_units": 0} for lot in q["lot_ids"]}
    ans = client.post(f"/api/requests/{q['request_id']}/answer", files=multipart("counted", counts), headers=key())
    assert ans.status_code == 200, ans.json()
    ids = {"case_id": cid, "request_id": q["request_id"], "signal_id": ans.json()["signal_id"]}
    before = client.get("/api/session").json()
    bodies = {  # valid bodies, so only the missing key can refuse them
        "/api/demo/advance-day": {"expected_version": before["version"]},
        "/api/cases/{case_id}/send": {"rec_version": v},
        "/api/cases/{case_id}/decision": {"rec_version": v, "decision": "approve", "plan_key": card["suggested_key"]},
        "/api/cases/{case_id}/reject": {"reason": "test"},
        "/api/cases/{case_id}/adopt": {"overrides": [], "note": "test"},
        "/api/cases/{case_id}/requests": {"rec_version": v, "fact_id": fact["fact_id"]},
        "/api/signals/{signal_id}/confirm": {"revision": ans.json()["revision"], "checked_both": True},
        "/api/settings": {"expected_version": before["version"], "changes": {"hold_ttl_min": 30}},
        "/api/lots/{lot_id}/holds": {"reason": "test"},  # 07-08: the lot's manager places a hold
        "/api/holds/{hold_id}/release": {"note": "test"},  # 07-08: and releases it with a note
        "/api/cases/{case_id}/review": {"flag_ids": ["test"], "note": "test"},  # 07-09: Reviewed · keep plan
        "/api/cases/{case_id}/select": {"rec_version": v, "plan_key": card["suggested_key"]},  # 07-10
        "/api/cases/{case_id}/cancel": {"reason": "test"},  # 07-11: Cancel approved plan
        "/api/cases/{case_id}/reassess": {},  # 07-12: Reassess
        "/api/signals/{signal_id}/withdraw": {},  # 07-13: withdraw a field report
        "/api/signals/{signal_id}/dismiss": {"reason": "test"},  # 07-13: dismiss a field report
        "/api/signals/{signal_id}/done": {},  # 07-14: Mark done
        "/api/signals/{signal_id}": {"revision": 1},  # 07-14: edit a report
        "/api/signals/{signal_id}/preview": {"revision": 1},  # 07-15: the impact preview
        "/api/signals/preview": {"kind": "CAPACITY", "fields": {}},  # 07-20: the preview before filing
    }
    with db.tx() as conn:
        keys_before = db.one(conn, "SELECT count(*) AS n FROM idempotency_keys WHERE session_id = "
                                   "(SELECT session_id FROM sessions WHERE run_id = :r)", r=before["run_id"])["n"]
    for method, path in writes:
        url = path.format_map(Ids(ids))  # a route module's own id (06.1) is a placeholder: the key is checked first
        if path.endswith("/answer"):
            r = client.request(method, url, files=multipart("again", counts))
        elif path == "/api/signals":  # a field report's multipart form (07-13)
            r = client.request(method, url, files={"kind": (None, "CAPACITY"), "fields": (None, "{}")})
        else:
            r = client.request(method, url, json=bodies.get(path, MODULE_BODY))
        assert error(r) == (400, "idempotency_key"), (method, path, r.json())
    after = client.get("/api/session").json()
    assert {k: after[k] for k in ("run_id", "day", "version", "role", "branch")} == \
        {k: before[k] for k in ("run_id", "day", "version", "role", "branch")}
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM idempotency_keys WHERE session_id = "
                            "(SELECT session_id FROM sessions WHERE run_id = :r)", r=before["run_id"])["n"] == keys_before
