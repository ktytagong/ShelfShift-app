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

from api import caps, config, db
from api.app import app
from api.errors import ApiError
from data import config as refconfig
from tests.api.conftest import PASSCODE, first_store, key, sign_in
from tests.api.test_http import phantom, story

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
    monkeypatch.delitem(config.SESSION_CAPS, "signal")  # a kind whose cap is removed from the settings
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
    now = far_window(3)  # 10:30 in Bangkok: the hour window resets at 11:00
    monkeypatch.setitem(config.SESSION_CAPS, "ai", (2, "hour"))
    monkeypatch.setitem(config.SESSION_CAPS, "signal", (2, "day"))
    monkeypatch.setitem(config.SESSION_CAPS, "upload", (1, "day"))
    monkeypatch.setitem(config.GLOBAL_CAPS, "ai", 3)
    one, two = f"t-{uuid.uuid4().hex}", f"t-{uuid.uuid4().hex}"
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
    spend(one, "signal", now)
    spend(one, "signal", now)
    with pytest.raises(ApiError) as e:
        spend(one, "signal", now)
    assert e.value.extra["kind"] == "signal" and "(2 field reports per day)" in e.value.message
    spend(one, "upload", now)
    with pytest.raises(ApiError) as e:
        spend(one, "upload", now)
    assert e.value.extra["kind"] == "upload"


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
    assert not any(part in s for s in scopes for part in ("198.51.100", "203.0.113", "d24", "10.0.0."))


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
