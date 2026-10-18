"""FLOW-11: one cap counter for every kind (04-CONTEXT D-08; 04-API-CONTRACT "Caps").

Limits are shrunk with monkeypatch on config.SESSION_CAPS / GLOBAL_CAPS so a test reaches them in a few actions; the
real values stay in api/settings.json. The daily cap across sessions is counted by every session in the test run, so
a test that reaches it sets the limit relative to today's count. Service-level spends use an injected instant in a
window no other test touches, and a fresh session id, so their counts start at zero. The messages are the contract's
plain messages, naming the limit and when it resets (the cap time zone is Asia/Bangkok in api/settings.json).
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import caps, cases, config, db, refdata
from api.app import COOKIE, app
from api.errors import ApiError
from data import config as refconfig
from tests.api.conftest import PASSCODE, as_seat, first_store, fresh, key, sign_in
from tests.api.test_http import multipart, phantom, story

BANGKOK = timedelta(hours=7)  # Asia/Bangkok is UTC+7 all year (no daylight saving)
DAILY = "The daily limit for new workspaces has been reached; it resets at midnight Bangkok time."


def counts(scope: str, kind: str) -> int:
    """Today's count for (scope, kind) in the day window, 0 when nothing was spent."""
    with db.tx() as conn:
        row = db.one(conn, "SELECT count FROM usage_counts WHERE scope = :s AND kind = :k AND window_start = :w",
                     s=scope, k=kind, w=caps.window_start("day"))
    return row["count"] if row else 0


def total() -> int:
    with db.tx() as conn:
        return db.one(conn, "SELECT coalesce(sum(count), 0) AS n FROM usage_counts")["n"]


def session_of(client) -> dict:
    s = client.get("/api/session").json()
    with db.tx() as conn:
        s["session_id"] = db.one(conn, "SELECT session_id FROM sessions WHERE run_id = :r", r=s["run_id"])["session_id"]
    return s


def spend(session_id, kind, now, n=1):
    """One spend in its own transaction, as a request makes it."""
    return db.in_tx(lambda conn: caps.spend(conn, session_id, kind, n, now=now))


def far_window(day: int) -> datetime:
    """10:30 in Bangkok on this day of January 2031: a day window no request and no other test uses."""
    return datetime(2031, 1, day, 3, 30, tzinfo=timezone.utc)


def test_the_nth_action_passes_and_the_next_is_a_plain_429(client, monkeypatch):
    monkeypatch.setitem(config.SESSION_CAPS, "reset", (2, "day"))
    first = session_of(client)
    for _ in range(2):
        assert client.post("/api/demo/reset", json={}, headers=key()).status_code == 200
    before = client.get("/api/session").json()["run_id"]
    k = key()
    r = client.post("/api/demo/reset", json={}, headers=k)
    assert (r.status_code, r.json()["error"], r.json()["kind"]) == (429, "cap_reached", "reset")
    assert r.json()["message"] == ("You have reached this session's limit (2 resets per day). "
                                   "It resets at midnight Bangkok time.")
    assert client.get("/api/session").json()["run_id"] == before  # the sandbox was not replaced
    assert counts(first["session_id"], "reset") == 2  # the refused reset left no count
    assert client.post("/api/demo/reset", json={}, headers=k).status_code == 429  # and stored no key


def test_the_daily_cap_counts_across_sessions(client, monkeypatch):
    today = counts(caps.GLOBAL, "sandbox")
    assert today >= 1  # the client fixture's own sandbox was counted
    monkeypatch.setitem(config.GLOBAL_CAPS, "sandbox", today + 2)
    other = TestClient(app)
    sign_in(other)  # today + 1
    assert client.post("/api/demo/reset", json={}, headers=key()).status_code == 200  # today + 2: a reset counts
    third = TestClient(app)
    r = third.post("/api/session", json={"account": first_store(), "passcode": PASSCODE})
    assert (r.status_code, r.json()["error"], r.json()["kind"]) == (429, "cap_reached", "sandbox")
    assert r.json()["message"] == DAILY and not any(ch.isdigit() for ch in r.json()["message"])  # no cap number
    assert not third.cookies.get("ss_session")
    r = other.post("/api/demo/reset", json={}, headers=key())
    assert (r.status_code, r.json()["kind"]) == (429, "sandbox")
    assert counts(caps.GLOBAL, "sandbox") == today + 2


def test_global_kinds_count_without_a_session(monkeypatch):
    now = far_window(1)
    for kind in ("ai", "sandbox", "upload"):
        assert kind in config.GLOBAL_CAPS
        spend(None, kind, now)
        spend(None, kind, now)
    with db.tx() as conn:
        got = {(r["scope"], r["kind"]): r["count"] for r in db.rows(
            conn, "SELECT * FROM usage_counts WHERE window_start = :w", w=caps.window_start("day", now))}
    assert got == {(caps.GLOBAL, "ai"): 2, (caps.GLOBAL, "sandbox"): 2, (caps.GLOBAL, "upload"): 2}


def test_a_kind_without_a_cap_is_never_counted(monkeypatch):
    now, sid = far_window(2), f"t-{uuid.uuid4().hex}"
    before = total()
    spend(sid, "no-such-kind", now, n=10**6)  # never refused
    spend(None, "no-such-kind", now)
    monkeypatch.delitem(config.WORKSPACE_CAPS, "signal")  # a kind whose cap is removed from the settings (07 D-20)
    spend(sid, "signal", now, n=10**6)
    assert total() == before
    spend(sid, "reset", now)  # a per-session kind with no daily cap counts for the session only
    spend(sid, "sandbox", now)  # a daily kind with no per-session cap counts globally only
    with db.tx() as conn:
        got = {(r["scope"], r["kind"]) for r in db.rows(
            conn, "SELECT * FROM usage_counts WHERE window_start = :w", w=caps.window_start("day", now))}
    assert got == {(sid, "reset"), (caps.GLOBAL, "sandbox")}


def test_windows_start_at_local_midnight_in_the_cap_time_zone():
    assert config.CAP_TZ.key == "Asia/Bangkok"
    before = datetime(2030, 3, 10, 16, 59, 59, tzinfo=timezone.utc)  # 23:59:59 on 10 March in Bangkok
    after = datetime(2030, 3, 10, 17, 0, 0, tzinfo=timezone.utc)  # 00:00:00 on 11 March in Bangkok
    assert caps.window_start("day", before) == datetime(2030, 3, 10, tzinfo=timezone.utc) - BANGKOK
    assert caps.window_start("day", after) == datetime(2030, 3, 11, tzinfo=timezone.utc) - BANGKOK
    assert caps.window_start("hour", datetime(2030, 3, 10, 10, 42, 7, tzinfo=timezone.utc)) == \
        datetime(2030, 3, 10, 10, tzinfo=timezone.utc)
    assert caps.window_start("day", before).utcoffset() == BANGKOK  # local, stored as timestamptz
    sid = f"t-{uuid.uuid4().hex}"
    spend(sid, "reset", before)
    spend(sid, "reset", after)
    with db.tx() as conn:
        stored = [r["window_start"] for r in db.rows(conn, "SELECT window_start FROM usage_counts WHERE scope = :s "
                                                           "ORDER BY window_start", s=sid)]
    assert stored == [datetime(2030, 3, 9, 17, tzinfo=timezone.utc), datetime(2030, 3, 10, 17, tzinfo=timezone.utc)]
    with pytest.raises(ValueError):
        caps.window_start("week", before)


def test_a_refused_spend_rolls_back_with_the_request(client, monkeypatch):
    s = session_of(client)
    resets = counts(s["session_id"], "reset")
    monkeypatch.setitem(config.GLOBAL_CAPS, "sandbox", counts(caps.GLOBAL, "sandbox"))  # the next sandbox is over
    r = client.post("/api/demo/reset", json={}, headers=key())
    assert (r.status_code, r.json()["kind"]) == (429, "sandbox")  # the session cap passed, then the daily one refused
    assert counts(s["session_id"], "reset") == resets  # the session's count rolled back with the request
    assert client.get("/api/session").json()["run_id"] == s["run_id"]
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM idempotency_keys WHERE session_id = :s AND endpoint = 'reset'",
                      s=s["session_id"])["n"] == 0


def test_ai_and_signal_kinds_are_capped_at_service_level(monkeypatch):
    """The sanctioned 07-13 change (07-CONTEXT D-20): `signal` and `upload` are capped per workspace (scope
    run:<run_id>, "this workspace's"), no longer per session; `ai` stays per session."""
    now = far_window(3)  # 10:30 in Bangkok: the hour window resets at 11:00
    monkeypatch.setitem(config.SESSION_CAPS, "ai", (2, "hour"))
    monkeypatch.setitem(config.WORKSPACE_CAPS, "signal", (2, "day"))
    monkeypatch.setitem(config.WORKSPACE_CAPS, "upload", (1, "day"))
    monkeypatch.setitem(config.GLOBAL_CAPS, "ai", 3)
    assert "signal" not in config.SESSION_CAPS and "upload" not in config.SESSION_CAPS
    one, two = f"t-{uuid.uuid4().hex}", f"t-{uuid.uuid4().hex}"
    workspace = f"run:t-{uuid.uuid4().hex}"
    spend(one, "ai", now)
    spend(one, "ai", now)
    with pytest.raises(ApiError) as e:
        spend(one, "ai", now)
    assert (e.value.status, e.value.code, e.value.extra["kind"]) == (429, "cap_reached", "ai")
    assert e.value.message == "You have reached this session's limit (2 AI requests per hour). It resets at 11:00 " \
                              "Bangkok time."
    spend(two, "ai", now)  # the third across sessions: the daily cap is 3
    with pytest.raises(ApiError) as e:
        spend(two, "ai", now)
    assert e.value.message == "The daily limit for AI requests has been reached; it resets at midnight Bangkok time."
    spend(workspace, "signal", now)
    spend(workspace, "signal", now)
    with pytest.raises(ApiError) as e:
        spend(workspace, "signal", now)
    assert e.value.extra["kind"] == "signal"
    assert e.value.message == "You have reached this workspace's limit (2 field reports per day). It resets at " \
                              "midnight Bangkok time."
    spend(workspace, "upload", now)
    with pytest.raises(ApiError) as e:
        spend(workspace, "upload", now)
    assert e.value.extra["kind"] == "upload" and "this workspace's" in e.value.message


def test_a_count_request_spends_nothing(client):
    card = story(client)
    fact = phantom(client.get(f"/api/cases/{card['case_id']}").json())
    before = total()
    r = client.post(f"/api/cases/{card['case_id']}/requests",
                    json={"rec_version": card["rec_version"], "fact_id": fact["fact_id"]}, headers=key())
    assert r.status_code == 200, r.json()
    assert total() == before  # no cap of any kind was spent (D-20)


def test_concurrent_spends_never_undercount(monkeypatch):
    now, each = far_window(4), 25
    monkeypatch.setitem(config.GLOBAL_CAPS, "upload", 10**6)
    errors = []

    def spender():
        try:
            for _ in range(each):
                spend(None, "upload", now)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=spender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with db.tx() as conn:
        got = db.one(conn, "SELECT count FROM usage_counts WHERE scope = :s AND kind = 'upload' AND window_start = :w",
                     s=caps.GLOBAL, w=caps.window_start("day", now))["count"]
    assert got == 2 * each


def test_wrong_passcodes_are_limited_per_client_address(template, monkeypatch):
    """WR-03: wrong passcodes spend the one cap counter, per client address (hashed, never stored as is), in a
    committed transaction of their own. After the limit every attempt from that address is a plain 429, the right
    passcode included, so a guess after the limit learns nothing; another address is not locked out."""
    assert config.CLIENT_CAPS["passcode"] == tuple(config.SETTINGS["caps"]["client"]["passcode"].values())
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (3, "hour"))  # a test value; the real one is in settings.json
    guesser = TestClient(app, client=("wr03-guesser", 50000))
    for _ in range(3):
        r = guesser.post("/api/session", json={"account": first_store(), "passcode": "nope"})
        assert (r.status_code, r.json()["error"]) == (401, "bad_passcode")
    nxt = (caps.window_start("hour") + timedelta(hours=1)).astimezone(config.CAP_TZ)
    for passcode in ("nope", PASSCODE):
        r = guesser.post("/api/session", json={"account": first_store(), "passcode": passcode})
        assert r.status_code == 429 and r.json() == {
            "error": "cap_reached", "kind": "passcode",
            "message": f"You have reached this address's limit (3 wrong passcodes per hour). It resets at "
                       f"{nxt:%H:%M} Bangkok time."}
    assert not guesser.cookies.get("ss_session")
    judge = TestClient(app, client=("wr03-judge", 50000))
    sign_in(judge)
    with db.tx() as conn:
        scopes = [r["scope"] for r in db.rows(conn, "SELECT scope FROM usage_counts WHERE kind = 'passcode'")]
    assert scopes and not any("wr03" in s for s in scopes)


# --- D-24: the counted address, the Secure cookie and attempts_left ---------------------------------------------------
# Every test below makes its own TestClient peer (the WR-03 pattern) and uses documentation addresses (RFC 5737) that
# no other test counts against, so each counter starts at zero. The passcode cap is a test value (monkeypatch); the
# real one is in api/settings.json.

def guess(c, xff=None):
    """One wrong guess, with X-Forwarded-For set to `xff` (a string, or a list of header lines) when given."""
    lines = [] if xff is None else ([xff] if isinstance(xff, str) else xff)
    return c.post("/api/session", json={"passcode": "nope"}, headers=[("X-Forwarded-For", x) for x in lines])


def left(r) -> int:
    assert (r.status_code, r.json()["error"]) == (401, "bad_passcode"), r.json()
    value = r.json()["attempts_left"]
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def test_off_cloud_run_a_forged_forwarded_for_is_counted_with_the_peer(template, monkeypatch):
    """D-24 locally: the counted address is the TCP peer, whatever X-Forwarded-For says; rotating it never opens a
    new counter."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", False)
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (10, "hour"))
    c = TestClient(app, client=("d24-local", 50000))
    a = left(guess(c))
    assert left(guess(c, "203.0.113.9")) == a - 1
    assert left(guess(c, "203.0.113.10")) == a - 2
    assert left(guess(c, "203.0.113.11, 198.51.100.21")) == a - 3


def test_on_cloud_run_the_rightmost_forwarded_for_entry_is_the_counted_address(template, monkeypatch):
    """D-24 on Cloud Run: the rightmost entry is the one Google's front end appends; the client-written entries to its
    left are ignored, so rotating them stays on one counter, and another rightmost entry is another counter."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (10, "hour"))
    c = TestClient(app, client=("d24-cloud", 50000))
    a = left(guess(c, "203.0.113.1, 198.51.100.7"))
    assert left(guess(c, "203.0.113.2, 198.51.100.7")) == a - 1
    assert left(guess(c, "198.51.100.7")) == a - 2
    assert left(guess(c, "203.0.113.1, 198.51.100.8")) == 9  # a separate counter: its first wrong guess


def test_on_cloud_run_an_unusable_last_entry_counts_the_peer(template, monkeypatch):
    """No header, an empty last element, or a last element that is not an IP address: the TCP peer is counted."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (10, "hour"))
    c = TestClient(app, client=("d24-peer", 50000))
    a = left(guess(c))
    assert left(guess(c, "203.0.113.3, ")) == a - 1
    assert left(guess(c, "203.0.113.3, not-an-address")) == a - 2
    assert left(guess(c, "198.51.100.30:443")) == a - 3  # an address with a port is not an IP address
    assert left(guess(c, "")) == a - 4


def test_on_cloud_run_several_header_lines_are_read_in_order_as_one_list(template, monkeypatch):
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (10, "hour"))
    c = TestClient(app, client=("d24-lines", 50000))
    a = left(guess(c, ["203.0.113.5", "198.51.100.9"]))  # the rightmost entry is the second line's
    assert left(guess(c, "203.0.113.6, 198.51.100.9")) == a - 1
    assert left(guess(c, ["198.51.100.9", "203.0.113.7"])) == 9  # in order: now 203.0.113.7 is the rightmost


def test_client_address_reads_only_the_rightmost_entry_on_cloud_run(monkeypatch):
    from starlette.requests import Request

    from api.app import client_address

    def request(*lines, peer=("10.0.0.1", 1234)):
        headers = [(b"x-forwarded-for", line.encode()) for line in lines]
        return Request({"type": "http", "method": "POST", "path": "/api/session", "headers": headers, "client": peer})
    monkeypatch.setattr(config, "ON_CLOUD_RUN", False)
    assert client_address(request("203.0.113.1, 198.51.100.7")) == "10.0.0.1"
    assert client_address(request(peer=None)) is None
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    assert client_address(request("203.0.113.1, 198.51.100.7")) == "198.51.100.7"
    assert client_address(request(" 198.51.100.7 ")) == "198.51.100.7"
    assert client_address(request("203.0.113.1", "2001:DB8:0:0::1")) == "2001:db8::1"  # normalised
    for unusable in ((), ("",), ("203.0.113.1,",), ("203.0.113.1, unknown",), ("198.51.100.7:80",)):
        assert client_address(request(*unusable)) == "10.0.0.1", unusable
    assert client_address(request("bogus", peer=None)) is None


def test_no_counted_scope_holds_an_address(template, monkeypatch):
    """WR-03 kept under D-24: usage_counts holds hashed scopes only."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    guess(TestClient(app, client=("d24-hash", 50000)), "203.0.113.40, 198.51.100.40")
    with db.tx() as conn:
        scopes = [r["scope"] for r in db.rows(conn, "SELECT scope FROM usage_counts WHERE kind = 'passcode'")]
    assert scopes and all(s.startswith("client:") for s in scopes)
    assert not any(part in s for s in scopes for part in ("198.51.100", "203.0.113", "d24-", "10.0.0."))


def secure(r) -> bool:
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("ss_session="))
    return "secure" in [part.strip().lower() for part in cookie.split(";")[1:]]


def test_the_session_cookie_is_secure_on_cloud_run_whatever_the_scheme(template, monkeypatch):
    """D-24: Secure comes from K_SERVICE (config.ON_CLOUD_RUN), whatever the proxy headers say; off Cloud Run it
    follows the request's scheme."""
    monkeypatch.setattr(config, "ON_CLOUD_RUN", True)
    on_cloud_run = TestClient(app, client=("d24-cookie-run", 50000))
    r = on_cloud_run.post("/api/session", json={"account": first_store(), "passcode": PASSCODE},
                         headers={"X-Forwarded-Proto": "http"})
    assert r.status_code == 200 and secure(r)
    monkeypatch.setattr(config, "ON_CLOUD_RUN", False)
    https = TestClient(app, base_url="https://testserver", client=("d24-cookie-https", 50000))
    r = https.post("/api/session", json={"account": first_store(), "passcode": PASSCODE})
    assert r.status_code == 200 and secure(r)
    http = TestClient(app, client=("d24-cookie-http", 50000))
    r = http.post("/api/session", json={"account": first_store(), "passcode": PASSCODE},
                  headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and not secure(r)


def test_on_cloud_run_is_set_exactly_when_k_service_is_set_and_not_blank():
    """A child process imports api.config with and without K_SERVICE; it prints only a boolean."""
    import os
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    probe = "from api import config; print(config.ON_CLOUD_RUN)"
    name = "K_" + "SERVICE"
    env = {k: v for k, v in os.environ.items() if k != name}
    for extra, want in (({}, "False"), ({name: ""}, "False"), ({name: "  "}, "False"), ({name: "shelfshift"}, "True")):
        out = subprocess.run([sys.executable, "-c", probe], env={**env, **extra}, cwd=root, capture_output=True,
                             text=True)
        assert (out.returncode, out.stdout.strip()) == (0, want), (extra, out.stderr)


def test_a_wrong_passcode_carries_the_attempts_left_then_a_plain_429(template, monkeypatch):
    """D-24: the 401 carries attempts_left, the limit minus this window's count; the cap is still checked before the
    compare, so once none are left every attempt is the same 429 as before, the right passcode included."""
    monkeypatch.setitem(config.CLIENT_CAPS, "passcode", (3, "hour"))
    c = TestClient(app, client=("d24-attempts", 50000))
    for want in (2, 1, 0):
        r = guess(c)
        assert r.json()["message"] == "That passcode is not right" and left(r) == want
    nxt = (caps.window_start("hour") + timedelta(hours=1)).astimezone(config.CAP_TZ)
    for passcode in ("nope", PASSCODE):
        r = c.post("/api/session", json={"passcode": passcode})
        assert r.status_code == 429 and r.json() == {
            "error": "cap_reached", "kind": "passcode",
            "message": f"You have reached this address's limit (3 wrong passcodes per hour). It resets at "
                       f"{nxt:%H:%M} Bangkok time."}


def test_spend_returns_what_is_left_under_the_kinds_own_cap(monkeypatch):
    now, sid = far_window(5), f"t-{uuid.uuid4().hex}"
    monkeypatch.setitem(config.SESSION_CAPS, "reset", (3, "day"))
    assert [spend(sid, "reset", now) for _ in range(3)] == [2, 1, 0]
    assert spend(None, "sandbox", now) is None  # no own cap: nothing to report
    assert spend(sid, "no-such-kind", now) is None


# --- D-24 (06-05): the judging deployment's passcode block, 10 wrong guesses per address per clock minute ----------
# These use the real setting from api/settings.json (no monkeypatch), an injected instant in February 2031 that no
# request uses, and a fresh scope per test, so no test waits on the clock and every counter starts at zero.

def minute_of(day: int) -> datetime:
    """10:30:00 in Bangkok on this day of February 2031: the start of a local clock minute."""
    return datetime(2031, 2, day, 3, 30, tzinfo=timezone.utc)


def wrong_guess(scope: str, now: datetime) -> int | None:
    """One wrong passcode counted for `scope`, in its own transaction as open_session spends it."""
    return db.in_tx(lambda conn: caps.spend(conn, scope, "passcode", now=now))


def blocked(scope: str, now: datetime) -> ApiError | None:
    """The 429 `reached` raises for `scope` at `now`, or None when the address may guess again."""
    try:
        db.in_tx(lambda conn: caps.reached(conn, scope, "passcode", now=now))
    except ApiError as exc:
        return exc
    return None


def test_a_minute_window_starts_on_the_local_clock_minute():
    t = datetime(2030, 3, 10, 10, 42, 7, 123456, tzinfo=timezone.utc)  # 17:42:07.123456 in Bangkok
    start = caps.window_start("minute", t)
    assert start == datetime(2030, 3, 10, 10, 42, tzinfo=timezone.utc) and start.utcoffset() == BANGKOK
    assert caps.window_start("minute", start) == start
    assert caps.window_start("minute", start + timedelta(seconds=59, microseconds=999999)) == start
    assert caps.window_start("minute", start + timedelta(seconds=60)) == start + timedelta(minutes=1)
    # the hour and day windows are unchanged
    assert caps.window_start("hour", t) == datetime(2030, 3, 10, 10, tzinfo=timezone.utc)
    assert caps.window_start("day", t) == datetime(2030, 3, 10, tzinfo=timezone.utc) - BANGKOK


def test_ten_wrong_passcodes_block_an_address_until_the_clock_minute_ends():
    """D-24 as the user reworded it (2026-10-16; 06-REVIEWS R2-4, D-R1): after 10 wrong guesses within one clock
    minute the address is refused until that minute ends, at most 60 s, and never permanently."""
    assert config.CLIENT_CAPS["passcode"] == (10, "minute")  # the real setting (api/settings.json, 06-CONTEXT D-24)
    start, scope = minute_of(1), caps.client_scope(f"d24-minute-{uuid.uuid4().hex}")
    at = start + timedelta(seconds=5)
    assert [wrong_guess(scope, at) for _ in range(10)] == list(range(9, -1, -1))
    nxt = (start + timedelta(minutes=1)).astimezone(config.CAP_TZ)
    assert f"{nxt:%H:%M}" == "10:31"
    for when in (at, start + timedelta(seconds=59)):
        refused = blocked(scope, when)
        assert refused is not None, when
        assert (refused.status, refused.code, refused.extra) == (429, "cap_reached", {"kind": "passcode"})
        assert refused.message == ("You have reached this address's limit (10 wrong passcodes per minute). "
                                   "It resets at 10:31 Bangkok time.")
    with pytest.raises(ApiError) as eleventh:  # an 11th guess in the same minute is refused by spend too
        wrong_guess(scope, start + timedelta(seconds=30))
    assert eleventh.value.status == 429 and "per minute" in eleventh.value.message
    assert blocked(scope, start + timedelta(seconds=60)) is None  # the next clock minute: guessing again is allowed
    assert wrong_guess(scope, start + timedelta(seconds=60)) == 9  # and it starts a fresh count


def test_a_burst_late_in_a_minute_is_refused_only_until_that_minute_ends():
    """The block is the clock-minute bucket (D-R1), not 60 s after the 10th guess."""
    start, scope = minute_of(2), caps.client_scope(f"d24-burst-{uuid.uuid4().hex}")
    for _ in range(10):
        wrong_guess(scope, start + timedelta(seconds=55))
    assert blocked(scope, start + timedelta(seconds=59, microseconds=999999)) is not None
    assert blocked(scope, start + timedelta(seconds=60)) is None  # 5 s after the 10th guess, not 60 s


def settings_with(tmp_path, window: str):
    """A copy of api/settings.json whose passcode cap has this window."""
    tree = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    tree["caps"]["client"]["passcode"]["value"]["window"] = window
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(tree), encoding="utf-8")
    return path


@pytest.mark.parametrize("window", ["minute", "hour", "day"])
def test_load_settings_accepts_a_minute_hour_or_day_window(tmp_path, window):
    assert config.load_settings(settings_with(tmp_path, window))["caps"]["client"]["passcode"]["window"] == window


@pytest.mark.parametrize("window", ["week", "second", "Minute", ""])
def test_load_settings_refuses_any_other_window_naming_the_path(tmp_path, window):
    with pytest.raises(refconfig.ConfigError) as refused:
        config.load_settings(settings_with(tmp_path, window))
    assert str(refused.value) == ("settings.caps.client.passcode: must be {limit, window} with window one of "
                                  "minute, hour, day")


# --- the workspace allowance (07-13; 07-CONTEXT D-20; FLOW-11; acceptance check 14 without files) ---------------------

def capacity_fields(client, zone: str) -> dict:
    """A CAPACITY report's fields for today's business date (the seat's own store, from the session)."""
    run = fresh(client.get("/api/session").json()["run_id"])
    day = cases.iso_of(run, run["day"])
    return {"zone": zone, "free_totes": 5, "valid_from": day, "valid_to": day}


def file_capacity(client, fields: dict, headers: dict | None = None):
    return client.post("/api/signals", files={"kind": (None, "CAPACITY"), "fields": (None, json.dumps(fields))},
                       headers=key() if headers is None else headers)


def test_workspace_allowance(client, monkeypatch):
    """Acceptance check 14 (files left to 07-14), D-20: with the limit at 3, three reports from two seats pass and the
    fourth from a third seat is 429 with "this workspace's" message; switching seats never refills it; a refused (400)
    report, a retried Idempotency-Key and an answer to a request spend nothing; the counter's scope is run:<run_id>;
    GET /api/signals shows 0 reports left and the workspace's own limits only, never the global upload cap."""
    monkeypatch.setitem(config.WORKSPACE_CAPS, "signal", (3, "day"))
    card = story(client)
    run_id = client.get("/api/session").json()["run_id"]
    scope = f"run:{run_id}"
    fact = phantom(client.get(f"/api/cases/{card['case_id']}").json())
    as_seat(client, card["source"])
    q = client.post(f"/api/cases/{card['case_id']}/requests", json={"rec_version": card["rec_version"],
                                                                     "fact_id": fact["fact_id"]}, headers=key())
    assert q.status_code == 200, q.json()
    as_seat(client, fact["branch"])
    counts_given = {lot: {"shelf_units": 0, "backroom_units": 0} for lot in q.json()["lot_ids"]}
    ans = client.post(f"/api/requests/{q.json()['request_id']}/answer", files=multipart("counted", counts_given),
                      headers=key())
    assert ans.status_code == 200, ans.json()
    assert (counts(scope, "signal"), counts(scope, "upload")) == (0, 0)  # an answer spends no allowance
    fields = capacity_fields(client, card["zone"])
    as_seat(client, card["source"])
    k = key()
    first = file_capacity(client, fields, k)
    assert first.status_code == 200, first.json()
    again = file_capacity(client, fields, k)  # a retried key answers the stored report and spends nothing
    assert (again.status_code, again.json()["signal_id"]) == (200, first.json()["signal_id"])
    assert counts(scope, "signal") == 1
    bad = file_capacity(client, {**fields, "zone": "no-such-zone"})
    assert (bad.status_code, bad.json()["error"]) == (400, "out_of_bounds")
    assert counts(scope, "signal") == 1  # a refused report spends nothing
    assert file_capacity(client, fields).status_code == 200
    as_seat(client, card["dest"])
    assert file_capacity(client, fields).status_code == 200  # the third, from another seat
    third = next(b for b in refdata.get(fresh(run_id)["dataset_version"]).branches
                 if b not in (card["source"], card["dest"]))
    for seat in (third, card["source"]):  # any seat of the workspace; switching seats never refills it
        as_seat(client, seat)
        r = file_capacity(client, fields)
        assert r.status_code == 429 and r.json() == {
            "error": "cap_reached", "kind": "signal",
            "message": "You have reached this workspace's limit (3 field reports per day). It resets at midnight "
                       "Bangkok time."}
    assert counts(scope, "signal") == 3
    with db.tx() as conn:  # counted for the workspace only, never for a session
        sessions = [r["session_id"] for r in db.rows(conn, "SELECT session_id FROM sessions WHERE run_id = :r",
                                                     r=run_id)]
        assert not db.rows(conn, "SELECT * FROM usage_counts WHERE kind = 'signal' AND scope = ANY(:s)", s=sessions)
    allowance = client.get("/api/signals").json()["allowance"]
    assert allowance == {"reports_left": 0, "reports_limit": 3, "files_left": config.WORKSPACE_CAPS["upload"][0],
                         "files_limit": config.WORKSPACE_CAPS["upload"][0],
                         "resets": (caps.window_start("day") + timedelta(days=1)).astimezone(timezone.utc)
                         .isoformat()}  # the next local midnight in the cap time zone, as UTC
    assert config.GLOBAL_CAPS["upload"] not in allowance.values()  # never the cross-workspace cap's number


def test_concurrent_reports_never_exceed_the_allowance(client, monkeypatch):
    """Edge FLOW-11 concurrency (D-20): six concurrent reports in one workspace with 3 left give exactly three
    reports and three 429s; the counter holds 3."""
    monkeypatch.setitem(config.WORKSPACE_CAPS, "signal", (3, "day"))
    card = story(client)
    as_seat(client, card["dest"])
    run_id = client.get("/api/session").json()["run_id"]
    fields, cookie, codes = capacity_fields(client, card["zone"]), client.cookies.get(COOKIE), []

    def one():
        c = TestClient(app)
        c.cookies.set(COOKIE, cookie)
        codes.append(file_capacity(c, fields).status_code)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200, 200, 200, 429, 429, 429]
    assert counts(f"run:{run_id}", "signal") == 3
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r AND report_id IS NOT NULL "
                            "AND provenance IS DISTINCT FROM 'simulated'", r=run_id)["n"] == 3  # 07-16: not preloaded


# --- report files under the workspace's file allowance (07-14; D-20; acceptance check 14 with files) -----------------

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00 a test shelf photo \xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR a test png"


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """Stored files go to this test's own folder; the bucket key is unset (the local branch, never a cloud call)."""
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    return tmp_path / "uploads"


def file_with(client, fields: dict, data: bytes | None, headers: dict | None = None):
    """A CAPACITY report with one file part (or none)."""
    parts = {"kind": (None, "CAPACITY"), "fields": (None, json.dumps(fields))}
    if data is not None:
        parts["file"] = ("shelf.bin", data, "application/octet-stream")
    return client.post("/api/signals", files=parts, headers=key() if headers is None else headers)


def patch_with(client, signal_id: str, revision: int, data: bytes | None = None, fields: dict | None = None):
    """PATCH /api/signals/{id} as multipart: the revision, the fields to correct and one file part (or none)."""
    parts = {"revision": (None, str(revision))}
    if fields is not None:
        parts["fields"] = (None, json.dumps(fields))
    if data is not None:
        parts["file"] = ("shelf.bin", data, "application/octet-stream")
    return client.patch(f"/api/signals/{signal_id}", files=parts, headers=key())


def counted_flag(run_id: str, signal_id: str):
    with db.tx() as conn:
        return db.one(conn, "SELECT file_counted FROM signals WHERE run_id = :r AND signal_id = :s", r=run_id,
                      s=signal_id)["file_counted"]


def test_workspace_file_allowance(client, monkeypatch, uploads):
    """Acceptance check 14, files (D-20; T-07-55): with the file allowance at 2, two reports with a file pass and the
    third with a file is 429 cap_reached kind upload ("this workspace's"), while a text-only report still passes. A
    report's first file spends one workspace file and the cross-workspace count (file_counted true); adding the first
    file to a text-only report by PATCH spends one too; replacing that file spends only the cross-workspace count
    (file_counted false); an edit without a file, a retried key and a refused file spend nothing."""
    monkeypatch.setitem(config.WORKSPACE_CAPS, "upload", (2, "day"))
    card = story(client)
    run_id = client.get("/api/session").json()["run_id"]
    scope = f"run:{run_id}"
    as_seat(client, card["dest"])
    fields = capacity_fields(client, card["zone"])
    world = counts(caps.GLOBAL, "upload")
    k = key()
    one = file_with(client, fields, JPEG, k)
    assert one.status_code == 200, one.json()
    assert one.json()["file"] == {"type": "image/jpeg", "size": len(JPEG)}
    assert counted_flag(run_id, one.json()["signal_id"]) is True
    assert (counts(scope, "upload"), counts(caps.GLOBAL, "upload")) == (1, world + 1)
    again = file_with(client, fields, JPEG, k)  # a retried key answers the stored report and spends nothing
    assert (again.status_code, again.json()["signal_id"]) == (200, one.json()["signal_id"])
    assert (counts(scope, "upload"), counts(scope, "signal")) == (1, 1)
    bad = file_with(client, fields, b"<svg>not a photo</svg>")
    assert (bad.status_code, bad.json()["error"]) == (415, "bad_file")
    assert file_with(client, fields, PNG).status_code == 200
    r = file_with(client, fields, JPEG)
    assert r.status_code == 429 and r.json() == {
        "error": "cap_reached", "kind": "upload",
        "message": "You have reached this workspace's limit (2 uploads per day). It resets at midnight Bangkok time."}
    assert (counts(scope, "upload"), counts(scope, "signal"), counts(caps.GLOBAL, "upload")) == (2, 2, world + 2)
    text = file_with(client, fields, None)  # a text-only report still goes through
    assert text.status_code == 200 and text.json()["file"] is None
    monkeypatch.setitem(config.WORKSPACE_CAPS, "upload", (3, "day"))
    first = patch_with(client, text.json()["signal_id"], 1, JPEG)
    assert first.status_code == 200, first.json()
    assert (first.json()["revision"], first.json()["file"]) == (2, {"type": "image/jpeg", "size": len(JPEG)})
    assert counted_flag(run_id, first.json()["signal_id"]) is True
    assert (counts(scope, "upload"), counts(caps.GLOBAL, "upload")) == (3, world + 3)
    swap = patch_with(client, text.json()["report_id"], 2, PNG)  # replacing the file: the global count only
    assert swap.status_code == 200, swap.json()
    assert counted_flag(run_id, swap.json()["signal_id"]) is False
    assert (counts(scope, "upload"), counts(caps.GLOBAL, "upload")) == (3, world + 4)
    plain = patch_with(client, text.json()["report_id"], 3, fields={**fields, "free_totes": 2})
    assert plain.status_code == 200, plain.json()
    assert plain.json()["file"] == {"type": "image/png", "size": len(PNG)}  # the file stays with the report
    assert (counts(scope, "upload"), counts(caps.GLOBAL, "upload"), counts(scope, "signal")) == (3, world + 4, 3)
    allowance = client.get("/api/signals").json()["allowance"]
    assert (allowance["files_left"], allowance["files_limit"]) == (0, 3)


def test_the_twentieth_report_and_the_tenth_file(client, monkeypatch, uploads):
    """Edge FLOW-11 boundary (D-20), at the settings' own limits: the 10th first file of the day succeeds and the 11th
    is 429 (kind upload) while a text-only report still goes through; the 20th report succeeds and the 21st is 429
    (kind signal) for another seat of the workspace."""
    assert (config.WORKSPACE_CAPS["signal"][0], config.WORKSPACE_CAPS["upload"][0]) == (20, 10)
    card = story(client)
    run_id = client.get("/api/session").json()["run_id"]
    as_seat(client, card["dest"])
    fields = capacity_fields(client, card["zone"])
    for n in range(10):
        r = file_with(client, fields, JPEG)
        assert r.status_code == 200, (n, r.json())
    r = file_with(client, fields, PNG)
    assert (r.status_code, r.json()["kind"]) == (429, "upload")
    for n in range(10):
        r = file_with(client, fields, None)
        assert r.status_code == 200, (n, r.json())
    assert counts(f"run:{run_id}", "signal") == 20
    as_seat(client, card["source"])
    r = file_with(client, fields, None)
    assert (r.status_code, r.json()["kind"]) == (429, "signal")
    assert client.get("/api/signals").json()["allowance"]["reports_left"] == 0
