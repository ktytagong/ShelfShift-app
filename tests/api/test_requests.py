"""A request's real deadline and its read-time state (07-CONTEXT D-05, D-06, D-09; 04-API-CONTRACT "Request" and
"Verify path" 13).

Cases are found by tag and facts by kind, never by a key or a lot id from the seed. Nothing sleeps: a deadline moves
by `UPDATE evidence_requests SET due_at = now() - interval '1 second'`, as hold expiry moves `expires_at`. The pure
label rules are checked with an explicit `now`; the server's now() decides every state, never Python's clock.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from api import cases, config, db, realtime, refdata, signals, workflow
from api import facts as facts_mod
from api.errors import ApiError
from tests.api.conftest import as_seat, fresh, key, own_series, own_world, seat
from tests.api.test_advance import advance_once
from tests.api.test_evidence import (active, add_facts, answer, ask, asked_entries, badges, confirm, confirmed_at,
                                     counted, from_inbox, in_both, legacy_expired, lot_row, not_preloaded,
                                     other_sandbox, past_due, phantom, recount, refused, request_row, signal_row, source,
                                     story, table)
from tests.api.test_workflow import approve_transfer, call, state, version


def test_deadline_is_read_time(run):
    """Acceptance check 1: asked at T, due at T + requests.response_min; 'Awaiting response · respond by {t}' until
    then, 'Unanswered · Unverified' on the first read after it, with no Next day, and the stored state stays OPEN."""
    case = story(run)
    q = ask(run, case["case_id"])
    row = request_row(run, q)
    assert row["due_at"] - row["created_at"] == timedelta(minutes=config.REQUEST_RESPONSE_MIN)
    respond_by = realtime.label(row["due_at"])
    assert q["ask_text"].endswith(f"Respond by {respond_by}.") and case["sku"] in q["ask_text"]
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["status"], seen["overdue"]) == (
        "AWAITING", f"Awaiting response · respond by {respond_by}", False)
    assert seen["minutes_left"] in (config.REQUEST_RESPONSE_MIN, config.REQUEST_RESPONSE_MIN - 1)
    assert (seen["asked_at"], seen["due_at"]) == (realtime.iso(row["created_at"]), realtime.iso(row["due_at"]))
    assert row["created_at"] <= datetime.fromisoformat(seen["server_now"]) < row["due_at"]
    assert seen["late"] is False and q["status_kind"] == "AWAITING"  # the ask's own response reads it too
    past_due(run, q)
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["status"], seen["overdue"], seen["minutes_left"]) == (
        "UNANSWERED", "Unanswered · Unverified", True, 0)
    assert datetime.fromisoformat(seen["server_now"]) >= datetime.fromisoformat(seen["due_at"])
    assert request_row(run, q)["state"] == "OPEN"  # derived on read: nothing stores Unanswered


def test_the_deadline_is_a_setting(run, monkeypatch):
    monkeypatch.setattr(config, "REQUEST_RESPONSE_MIN", 5)  # the test's own value for the settings leaf
    q = ask(run, story(run)["case_id"])
    row = request_row(run, q)
    assert row["due_at"] - row["created_at"] == timedelta(minutes=5)
    assert from_inbox(run, q)["minutes_left"] in (5, 4)


NOW = datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)
DUE = NOW + timedelta(minutes=60)
BEFORE, AFTER = DUE - timedelta(seconds=1), DUE + timedelta(minutes=5)


def status(state, now, answer=None, part="count", due=DUE) -> dict:
    return realtime.request_status(state, due, now, answer, part)


def partial(photo: bool) -> dict:
    return {"complete": False, "photo": photo, "note": signals.PARTIAL, "answered_at": NOW}


def test_request_status_labels():
    awaiting = f"Awaiting response · respond by {realtime.label(DUE)}"
    assert status("OPEN", BEFORE) == {"kind": "AWAITING", "sentence": awaiting, "overdue": False, "late": False}
    assert status("OPEN", DUE) == {"kind": "UNANSWERED", "sentence": "Unanswered · Unverified", "overdue": True,
                                   "late": False}  # due_at itself is past due
    assert status("OPEN", BEFORE, partial(False)) == {"kind": "TEXT_PARTIAL", "sentence": signals.PARTIAL,
                                                      "overdue": False, "late": False}
    assert status("OPEN", AFTER, partial(False))["sentence"] == f"{signals.PARTIAL} · overdue"
    assert status("OPEN", AFTER, partial(False))["kind"] == "TEXT_PARTIAL"
    assert status("OPEN", BEFORE, partial(True)) == {"kind": "PHOTO_PARTIAL",
                                                     "sentence": "Photo received · count missing",
                                                     "overdue": False, "late": False}
    assert status("OPEN", AFTER, partial(True))["sentence"] == "Photo received · count missing · overdue"
    assert status("OPEN", BEFORE, partial(True), part="expiry")["sentence"] == "Photo received · date missing"
    assert status("OPEN", AFTER, partial(True), part="expiry")["sentence"] == "Photo received · date missing · overdue"
    on_time = {"complete": True, "photo": False, "note": None, "answered_at": BEFORE}
    late = {**on_time, "answered_at": DUE + timedelta(seconds=1)}
    assert status("ANSWERED", AFTER, on_time) == {"kind": "ANSWERED", "sentence": "Answered", "overdue": True,
                                                  "late": False}
    assert status("ANSWERED", AFTER, late)["late"] is True
    assert status("CONFIRMED", AFTER, late) == {"kind": "CONFIRMED", "sentence": "Confirmed", "overdue": True,
                                                "late": True}  # the late label outlives confirmation
    assert status("SUPERSEDED", BEFORE, late)["kind"] == "SUPERSEDED"
    for state in ("OPEN", "ANSWERED", "EXPIRED"):  # a legacy row (no due_at): its stored state, no sentence
        assert status(state, AFTER, None, due=None) == {"kind": state, "sentence": None, "overdue": False,
                                                       "late": False}
    assert status("EXPIRED", AFTER) == {"kind": "EXPIRED", "sentence": None, "overdue": False, "late": False}


def test_minutes_left_are_floored_and_never_negative():
    assert realtime.minutes_left(DUE, DUE - timedelta(seconds=90)) == 1
    assert realtime.minutes_left(DUE, DUE - timedelta(minutes=60)) == 60
    assert realtime.minutes_left(DUE, DUE) == 0 and realtime.minutes_left(DUE, AFTER) == 0
    assert realtime.minutes_left(None, NOW) is None


def test_label_format():
    assert config.CAP_TZ.key == "Asia/Bangkok"  # the settings leaf caps.time_zone the labels are shown in (D-09)
    assert realtime.label(datetime(2026, 10, 16, 13, 30, tzinfo=timezone.utc)) == "16 Oct 2026, 20:30"
    assert realtime.label(datetime(2026, 10, 4, 20, 5, tzinfo=timezone.utc)) == "5 Oct 2026, 03:05"  # no leading 0
    assert realtime.iso(datetime(2026, 10, 16, 20, 30, tzinfo=config.CAP_TZ)) == "2026-10-16T13:30:00+00:00"
    assert realtime.iso(None) is None
    assert realtime.MONTHS == cases.MONTHS


# --- Next day and the badge (Task 3; 07-CONTEXT D-05, D-06) -----------------------------------------------------------

def unverified(run, case_id) -> tuple[list[str], list[str]]:
    """(badge kinds, the unverified badge's fact_ids) on the case detail and, checked equal, on the board card."""
    found = badges(run, case_id)
    with db.tx() as conn:
        card = next(c for c in cases.board(conn, fresh(run["run_id"])) if c["case_id"] == case_id)
    assert card["badges"] == found  # the board's rows and the detail's views give the same badges
    return [b["kind"] for b in found], next((b["fact_ids"] for b in found if b["kind"] == "unverified"), [])


def test_next_day_expires_nothing(run):
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:late", "kind": "STALE_COUNT"}])  # a second flagged fact (own)
    ahead, behind = ask(run, cid), ask(run, cid, "TEST:late")
    past_due(run, behind)
    advance_once(run)
    with db.tx() as conn:
        stored = [r["state"] for r in db.rows(conn, "SELECT state FROM evidence_requests WHERE run_id = :r",
                                              r=run["run_id"])]
    assert stored == ["OPEN", "OPEN"]  # nothing is EXPIRED after the advance
    assert (from_inbox(run, ahead)["status_kind"], from_inbox(run, behind)["status_kind"]) == ("AWAITING", "UNANSWERED")


def test_an_unanswered_fact_counts_on_the_badge_again(run):
    """D-06 and research Pitfall 7: an asked fact leaves the unverified badge only while its request is AWAITING (or
    answered); once Unanswered, or a partial reply is overdue, it counts again and "Evidence requested" goes."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    kinds, ids = unverified(run, cid)
    assert fact["fact_id"] in ids  # counted before anyone asks
    q = ask(run, cid)
    kinds, ids = unverified(run, cid)
    assert "evidence_requested" in kinds and fact["fact_id"] not in ids  # AWAITING
    past_due(run, q)
    kinds, ids = unverified(run, cid)
    assert "evidence_requested" not in kinds and fact["fact_id"] in ids  # UNANSWERED
    answer(run, q, {}, text="The backroom is locked")
    assert from_inbox(run, q)["status"] == f"{signals.PARTIAL} · overdue"
    kinds, ids = unverified(run, cid)
    assert "evidence_requested" not in kinds and fact["fact_id"] in ids  # TEXT_PARTIAL past due
    answer(run, q, counted(q))
    kinds, ids = unverified(run, cid)
    assert "evidence_requested" in kinds and fact["fact_id"] not in ids  # ANSWERED (late)


def test_a_legacy_expired_row_still_reads_and_answers(run):
    case = story(run)
    q = ask(run, case["case_id"])
    legacy_expired(run, q)
    seen = in_both(run, q)
    assert (seen["state"], seen["status_kind"], seen["status"], seen["overdue"], seen["late"]) == (
        "EXPIRED", "EXPIRED", None, False, False)
    assert (seen["due_at"], seen["minutes_left"]) == (None, None)
    answer(run, q, {}, text="shelf done, backroom later")
    assert (request_row(run, q)["state"], from_inbox(run, q)["status_kind"]) == ("EXPIRED", "EXPIRED")
    late = answer(run, q, counted(q, 0.0))
    seen = in_both(run, q)
    assert (seen["state"], seen["status_kind"], seen["status"], seen["late"]) == ("ANSWERED", "ANSWERED", None, False)
    confirm(run, seat(q["to_branch"]), late["signal_id"], late["revision"])
    assert request_row(run, q)["state"] == "CONFIRMED"


# --- "When did you check?" (07-05 Task 1; 07-CONTEXT D-22, acceptance check 17 first half) ---------------------------

CHOOSE = "Choose when you checked: Checked just now, or a date and time."
LATER = "The check time can't be later than now."


def db_clock() -> datetime:
    with db.tx() as conn:
        return realtime.db_now(conn)


def test_parse_observed_boundaries():
    """Edge VER-03 boundary: a check time equal to the submission's now() is accepted, one a second later is refused;
    "now" is the server's now(); a time without its offset, a blank and anything else is refused (no default)."""
    assert realtime.parse_observed("now", NOW) == NOW
    assert realtime.parse_observed(NOW.isoformat(), NOW) == NOW
    assert realtime.parse_observed(NOW.astimezone(config.CAP_TZ).isoformat(), NOW) == NOW  # the same instant, +07:00
    assert realtime.parse_observed("2026-10-16T13:30:00Z", NOW) == NOW
    assert realtime.parse_observed((NOW - timedelta(days=2)).isoformat(), NOW) == NOW - timedelta(days=2)
    for value, message in (((NOW + timedelta(seconds=1)).isoformat(), LATER), ("2026-10-16T20:30:00", CHOOSE),
                           ("2026-10-16", CHOOSE), (None, CHOOSE), ("", CHOOSE), ("  ", CHOOSE),
                           ("yesterday", CHOOSE), (5, CHOOSE), ({"at": "now"}, CHOOSE)):
        with pytest.raises(ApiError) as e:
            realtime.parse_observed(value, NOW)
        assert (e.value.status, e.value.code, e.value.message) == (400, "check_time", message), value


def test_observed_time_rules(run):
    """An answer without `observed` is 400 check_time and writes no signal; a later or naive time likewise; "now"
    stores the transaction's now(); an explicit time is stored as given; the view's answer carries observed_at (ISO
    UTC). 07-06 adds the confirm half."""
    q = ask(run, story(run)["case_id"])
    to = seat(q["to_branch"])
    later = (db_clock() + timedelta(minutes=1)).isoformat()
    for observed, message in ((None, CHOOSE), ("", CHOOSE), ("2026-10-16T20:30:00", CHOOSE), (later, LATER)):
        e = refused(run, signals.answer, to, q["request_id"], "counted", counted(q), observed)
        assert (e.status, e.code, e.message) == (400, "check_time", message), observed
        assert not_preloaded(table(run["run_id"], "signals")) == []  # nothing is stored
        assert (request_row(run, q)["state"], request_row(run, q)["answer_signal_id"]) == ("OPEN", None)
    just_now = answer(run, q, counted(q, 1.0))
    row = signal_row(run, just_now["signal_id"])
    assert row["observed_at"] == row["created_at"]  # "Checked just now": the server's now() at submission
    assert just_now["observed_at"] == realtime.iso(row["observed_at"])
    earlier = db_clock() - timedelta(minutes=20)
    typed = answer(run, q, counted(q, 2.0), observed=earlier.astimezone(config.CAP_TZ).isoformat())
    row = signal_row(run, typed["signal_id"])
    assert row["observed_at"] == earlier and row["created_at"] > earlier  # observation and submission kept apart
    seen = from_inbox(run, q)["answer"]
    assert seen["observed_at"] == realtime.iso(earlier) and seen["observed_at"].endswith("+00:00")


OLDER = "Checked earlier than the confirmed evidence · kept, not applied"
SAME_TIME = "Same check time as the confirmed evidence, different value · review required"


def test_observed_time_rules_at_confirm(run, monkeypatch):
    """The confirm half (07-06; acceptance check 17, D-22, Verify path 15): check times for the same lot and fact are
    compared with the confirmed fact in force, never submission or confirmation times, at timestamptz precision.
    A count checked one second earlier than the fact in force and submitted after it was confirmed is 409
    older_evidence and kept; an equal time with another value is 409 review_required and kept; an equal time with the
    same value confirms and changes nothing; one second later applies."""
    monkeypatch.setattr(config, "MAX_REQUESTS_PER_VERSION", 10)  # the test's own value: five answers for one lot
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    at = (db_clock() - timedelta(minutes=30)).replace(microsecond=123456)  # T, with microseconds of its own

    def asked(name: str) -> dict:
        """A further request for the same lots on the active version (a test fact with the phantom's scope)."""
        add_facts(run, cid, [{**fact, "fact_id": f"TEST:{name}"}])
        return ask(run, cid, f"TEST:{name}")

    def given(q, value, when) -> dict:
        return answer(run, q, counted(q, value), observed=when.isoformat())

    a = ask(run, cid)
    to = seat(a["to_branch"])
    first = given(a, 2.0, at)
    out = confirm(run, to, first["signal_id"], first["revision"])
    (fact_a,) = table(run["run_id"], "reported_facts")
    assert (out["fact_id"], fact_a["observed_at"]) == (fact_a["fact_id"], at)
    lots = {l: lot_row(run, l)["qty"] for l in a["lot_ids"]}
    assert set(lots.values()) == {4.0}

    def unchanged(v) -> None:
        assert table(run["run_id"], "reported_facts") == [fact_a]
        assert {l: lot_row(run, l)["qty"] for l in a["lot_ids"]} == lots and version(run, cid) == v

    for name, value, when, code, message in (("B", 5.0, at - timedelta(seconds=1), "older_evidence", OLDER),
                                             ("C", 3.0, at, "review_required", SAME_TIME)):
        v = version(run, cid)
        q = asked(name)
        s = given(q, value, when)
        assert signal_row(run, s["signal_id"])["created_at"] > fact_a["confirmed_at"]  # submitted after A confirmed
        e = refused(run, signals.confirm, to, s["signal_id"], s["revision"], True)
        assert (e.status, e.code, e.message, e.extra["lot_ids"]) == (409, code, message, a["lot_ids"]), name
        assert in_both(run, q)["confirmable"] is False  # the view says what confirm does (WR-10)
        unchanged(v)
        assert (request_row(run, q)["state"], signal_row(run, s["signal_id"])["state"]) == ("ANSWERED", "READ")
    v = version(run, cid)
    d = asked("D")
    same = given(d, 2.0, at)
    entries = activity(run["run_id"], "count_confirmed")
    out = confirm(run, to, same["signal_id"], same["revision"])
    assert (out["fact_id"], out["cases"], out["unchanged"]) == (None, [], True)
    unchanged(v)
    assert (request_row(run, d)["state"], signal_row(run, same["signal_id"])["state"]) == ("CONFIRMED", "CONFIRMED")
    (logged,) = [x for x in activity(run["run_id"], "count_confirmed") if x not in entries]
    assert (logged["payload"]["unchanged"], logged["payload"]["fact_id"]) == (True, None)
    newer = asked("E")
    later = given(newer, 4.0, at + timedelta(seconds=1))
    out = confirm(run, to, later["signal_id"], later["revision"])
    applied = next(f for f in table(run["run_id"], "reported_facts") if f["fact_id"] == out["fact_id"])
    assert applied["observed_at"] == at + timedelta(seconds=1)
    assert {lot_row(run, l)["qty"] for l in a["lot_ids"]} == {8.0}


# --- Can't check, Ask again, late answers (07-05 Task 2; 07-CONTEXT D-07, D-08) ------------------------------------

def cant_check(run, q, actor=None) -> dict:
    return call(run, signals.cant_check, actor or seat(q["to_branch"]), q["request_id"])


def activity(run_id, kind) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM activity_log WHERE run_id = :r AND type = :t ORDER BY seq", r=run_id,
                       t=kind)


def test_cant_check_and_supersede(run):
    """Acceptance check 3: Can't check closes the attempt and the fact stays unverified; asking again marks the old
    request Superseded; a late answer to the superseded request is stored and labelled but refused at confirm."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    q = ask(run, cid)
    for other in (source(run, cid), seat(None)):  # only the addressed branch's manager (FLOW-10)
        e = refused(run, signals.cant_check, other, q["request_id"])
        assert (e.status, e.code) == (403, "forbidden")
    assert request_row(run, q)["state"] == "OPEN" and activity(run["run_id"], "evidence_cant_check") == []
    closed = cant_check(run, q)
    row = request_row(run, q)
    assert (closed["state"], closed["status_kind"], closed["status"], row["state"]) == (
        "CANT_CHECK", "CANT_CHECK", "Can't check", "CANT_CHECK")
    assert row["closed_by"] == f"manager:{q['to_branch']}" and row["closed_at"] >= row["created_at"]
    assert (closed["closed_at"], closed["closed_by"]) == (realtime.iso(row["closed_at"]), row["closed_by"])
    [entry] = activity(run["run_id"], "evidence_cant_check")
    assert (entry["case_id"], entry["payload"]["request_id"]) == (cid, q["request_id"])
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", counted(q), "now")
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is CANT_CHECK")
    e = refused(run, signals.cant_check, seat(q["to_branch"]), q["request_id"])  # already closed
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is CANT_CHECK")
    kinds, ids = unverified(run, cid)
    assert fact["fact_id"] in ids and "evidence_requested" not in kinds  # the fact stays unverified
    seen = in_both(run, q)
    assert (seen["askable_again"], seen["recountable"], seen["answerable"]) == (True, True, False)
    new = recount(run, cid, q["request_id"])
    assert (new["state"], new["status_kind"], new["fact_id"]) == ("OPEN", "AWAITING", fact["fact_id"])
    old = request_row(run, q)
    assert (old["state"], old["replaced_by"]) == ("SUPERSEDED", new["request_id"])
    assert [a["payload"]["recount_of"] for a in asked_entries(run["run_id"])
            if a["payload"]["request_id"] == new["request_id"]] == [q["request_id"]]
    e = refused(run, signals.cant_check, seat(q["to_branch"]), q["request_id"])  # superseded is read-only
    assert (e.code, e.message) == ("wrong_state", "Request is SUPERSEDED")
    past_due(run, q)
    kept = answer(run, q, counted(q, 1.0))  # an answer that arrives for the superseded request is kept
    row = request_row(run, q)
    assert (row["state"], row["answer_signal_id"]) == ("SUPERSEDED", kept["signal_id"])
    assert signal_row(run, kept["signal_id"])["observed_at"] is not None
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["late"], seen["confirmable"], seen["answerable"], seen["askable_again"]) == (
        "SUPERSEDED", True, False, False, False)
    assert seen["answer"]["signal_id"] == kept["signal_id"]
    e = refused(run, signals.confirm, seat(q["to_branch"]), kept["signal_id"], kept["revision"], True)
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is SUPERSEDED")
    assert request_row(run, new)["state"] == "OPEN"  # the current request is untouched


def test_ask_again_needs_unanswered_or_cant_check(run):
    """Ask again is refused on a request still awaiting its answer, allowed once it is Unanswered (no answer, or a
    partial reply, past its deadline); the old and new requests count once each toward the limit."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:third", "kind": "STALE_COUNT"}])  # a second flagged fact (own)
    v = version(run, cid)
    q = ask(run, cid)

    def again(of) -> ApiError:
        before = table(run["run_id"], "evidence_requests")
        e = refused(run, signals.request_evidence, source(run, cid), cid, v, fact["fact_id"], of["request_id"])
        assert table(run["run_id"], "evidence_requests") == before  # nothing written
        return e
    assert in_both(run, q)["askable_again"] is False
    e = again(q)
    assert (e.status, e.code, e.message, e.extra["state"]) == (409, "wrong_state", "Request is OPEN", "OPEN")
    past_due(run, q)
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["askable_again"], seen["recountable"]) == ("UNANSWERED", True, True)
    new = recount(run, cid, q["request_id"])
    assert request_row(run, q)["state"] == "SUPERSEDED" and in_both(run, new)["askable_again"] is False
    e = again(q)
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is SUPERSEDED")
    limit = config.MAX_REQUESTS_PER_VERSION
    e = refused(run, signals.request_evidence, source(run, cid), cid, v, "TEST:third")  # a third distinct group
    assert (e.status, e.code, e.message) == (409, "too_many_requests", f"At most {limit} count requests per case version")
    past_due(run, new)
    assert in_both(run, new)["askable_again"] is False  # Unanswered, but the version holds its limit
    e = again(new)
    assert (e.status, e.code) == (409, "too_many_requests")
    other = other_sandbox()  # a partial reply: not askable before its deadline, askable after it
    c2 = story(other)
    q2 = ask(other, c2["case_id"])
    answer(other, q2, {}, text="The backroom is locked")
    assert in_both(other, q2)["askable_again"] is False
    past_due(other, q2)
    seen = in_both(other, q2)
    assert (seen["status_kind"], seen["askable_again"]) == ("TEXT_PARTIAL", True)
    assert recount(other, c2["case_id"], q2["request_id"])["state"] == "OPEN"
    assert request_row(other, q2)["state"] == "SUPERSEDED"


def test_late_answer_confirms(run):
    """Acceptance check 4: a complete answer after the deadline is ANSWERED and late, can be confirmed, and keeps
    "Answered after deadline" after confirmation."""
    case = story(run)
    q = ask(run, case["case_id"])
    past_due(run, q)
    late = answer(run, q, counted(q, 0.0))
    seen = in_both(run, q)
    assert (seen["state"], seen["status_kind"], seen["late"], seen["confirmable"]) == ("ANSWERED", "ANSWERED", True,
                                                                                        True)
    out = confirm(run, seat(q["to_branch"]), late["signal_id"], late["revision"])
    assert out["fact_id"] and request_row(run, q)["state"] == "CONFIRMED"
    seen = in_both(run, q)
    assert (seen["state"], seen["status_kind"], seen["late"]) == ("CONFIRMED", "CONFIRMED", True)


def race(n: int, fn) -> list[tuple[str, object]]:
    """n threads start fn at once; each result is ("ok", value) or ("error", ApiError)."""
    start, out, lock = threading.Barrier(n), [], threading.Lock()

    def go():
        start.wait()
        try:
            got = ("ok", fn())
        except ApiError as e:
            got = ("error", e)
        with lock:
            out.append(got)
    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_two_asks_race(run):
    """Edge VER-02 concurrency: two simultaneous asks for one fact create one request; two simultaneous ask-agains of
    one request create one successor and one SUPERSEDED row (the case row lock serialises them)."""
    case = story(run)
    cid = case["case_id"]
    src, v, fid = source(run, cid), version(run, cid), phantom(run, cid)["fact_id"]
    asks = race(2, lambda: call(run, signals.request_evidence, src, cid, v, fid))
    assert [k for k, _ in asks] == ["ok", "ok"] and asks[0][1]["request_id"] == asks[1][1]["request_id"]
    assert len(table(run["run_id"], "evidence_requests")) == 1
    q = asks[0][1]
    past_due(run, q)
    agains = race(2, lambda: call(run, signals.request_evidence, src, cid, v, fid, q["request_id"]))
    won = [x for k, x in agains if k == "ok"]
    lost = [x for k, x in agains if k == "error"]
    assert len(won) == 1 and len(lost) == 1
    assert (lost[0].status, lost[0].code, lost[0].message) == (409, "wrong_state", "Request is SUPERSEDED")
    rows = {r["request_id"]: r for r in table(run["run_id"], "evidence_requests")}
    assert sorted(r["state"] for r in rows.values()) == ["OPEN", "SUPERSEDED"]
    assert rows[q["request_id"]]["replaced_by"] == won[0]["request_id"]


# --- Tasks and the answer edges (07-05 Task 3; 07-CONTEXT D-04, 04-API-CONTRACT GET /api/tasks) ----------------------

def tasks(run, actor) -> dict:
    with db.tx() as conn:
        return signals.tasks_for(conn, fresh(run["run_id"]), actor)


def waiting(run, actor) -> int:
    """The session's tasks_waiting for the seat, as views.session_info computes it."""
    with db.tx() as conn:
        return signals.waiting(conn, fresh(run["run_id"]), actor)


def test_tasks_lists_the_seats_requests_with_counts(run):
    """D-04: the addressed seat gets its requests (newest first, every state); to_answer counts AWAITING, UNANSWERED
    and the partials, to_confirm the ANSWERED ones; lot holds and advisory tasks are 0 until 07-08 and 07-15; HQ and
    a seat nobody asked get empty lists; the session's tasks_waiting is the counts' sum."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:late", "kind": "STALE_COUNT"}])  # a second flagged fact (own)
    to = seat(fact["branch"])

    def check(want: dict, order: list[str]) -> dict:
        got = tasks(run, to)
        assert set(got) == {"requests", "holds", "advisory", "counts", "server_now"}
        assert got["counts"] == {**want, "lot_holds": 0, "advisory": 0}
        assert (got["holds"], got["advisory"]) == ([], [])
        assert [r["request_id"] for r in got["requests"]] == order
        assert {r["server_now"] for r in got["requests"]} <= {got["server_now"]}  # one now() for every view
        assert datetime.fromisoformat(got["server_now"]).tzinfo is not None
        assert waiting(run, to) == sum(got["counts"].values())
        return got
    check({"to_answer": 0, "to_confirm": 0}, [])
    first = ask(run, cid)
    second = ask(run, cid, "TEST:late")
    check({"to_answer": 2, "to_confirm": 0}, [second["request_id"], first["request_id"]])  # newest first
    answer(run, first, counted(first, 0.0))
    past_due(run, second)  # UNANSWERED still wants an answer
    got = check({"to_answer": 1, "to_confirm": 1}, [second["request_id"], first["request_id"]])
    assert [r["status_kind"] for r in got["requests"]] == ["UNANSWERED", "ANSWERED"]
    answer(run, second, {}, text="The backroom is locked")  # an overdue partial reply: still to answer
    check({"to_answer": 1, "to_confirm": 1}, [second["request_id"], first["request_id"]])
    cant_check(run, second)  # closed: listed, no longer waiting
    got = check({"to_answer": 0, "to_confirm": 1}, [second["request_id"], first["request_id"]])
    assert got["requests"][0]["status_kind"] == "CANT_CHECK"
    for other in (seat(None), source(run, cid)):  # HQ, and a seat no request is addressed to
        got = tasks(run, other)
        assert (got["requests"], got["holds"], got["advisory"]) == ([], [], [])
        assert got["counts"] == {"to_answer": 0, "to_confirm": 0, "lot_holds": 0, "advisory": 0}
        assert waiting(run, other) == 0


def test_answer_edges(run):
    """Edge VER-03 boundary, adjacency, empty and ordering: a part of 0 is a count; a lot total above the engine's
    bound is 400; no count and no reply is 400; a blank stays null; two lots are stored per lot and never summed; a
    second answer is the next revision and confirm with the first is 409 stale_version."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    q = ask(run, cid)
    to = seat(q["to_branch"])
    lot = q["lot_ids"][0]
    top = signals.UNITS[1]  # the engine's bound on a lot's units (engine.adjust.UNITS)
    for counts, text in (({lot: {"shelf_units": top / 2, "backroom_units": top / 2 + 0.01}}, ""), ({}, ""),
                         ({lot: {"shelf_units": None, "backroom_units": None}}, "  ")):
        e = refused(run, signals.answer, to, q["request_id"], text, counts, "now")
        assert (e.status, e.code) == (400, "bad_request")
        assert not_preloaded(table(run["run_id"], "signals")) == []
    at_top = answer(run, q, {**counted(q), lot: {"shelf_units": top / 2, "backroom_units": top / 2}})
    assert at_top["fields"]["counts"][lot] == {"shelf_units": top / 2, "backroom_units": top / 2}  # the bound itself
    blank = answer(run, q, {**counted(q), lot: {"shelf_units": 0, "backroom_units": None}})
    assert blank["fields"]["counts"][lot] == {"shelf_units": 0.0, "backroom_units": None}  # 0 a count, blank null
    assert blank["fields"]["needs_input"] == [{"lot_id": lot, "field": "backroom_units"}]
    zero = answer(run, q, counted(q, 0.0))
    assert all(v == 0.0 and v is not None for c in zero["fields"]["counts"].values() for v in c.values())
    assert zero["fields"]["needs_input"] == [] and request_row(run, q)["state"] == "ANSWERED"  # zeros complete it
    assert (blank["revision"], zero["revision"]) == (at_top["revision"] + 1, at_top["revision"] + 2)
    assert {signal_row(run, x["signal_id"])["state"] for x in (at_top, blank)} == {"DISMISSED"}
    e = refused(run, signals.confirm, to, zero["signal_id"], blank["revision"], True)
    assert (e.status, e.code, e.extra["revision"]) == (409, "stale_version", zero["revision"])
    two_lots = [fact["lot_ids"][0], "TEST-LOT-ADJ"]  # adjacency: one request, two lots (a test fact)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:two", "kind": "STALE_COUNT", "lot_ids": two_lots}])
    pair = ask(run, cid, "TEST:two")
    both = answer(run, pair, {two_lots[0]: {"shelf_units": 4, "backroom_units": 1},
                              two_lots[-1]: {"shelf_units": 7, "backroom_units": 2}})
    assert both["fields"]["lot_ids"] == two_lots
    assert both["fields"]["counts"][two_lots[0]] == {"shelf_units": 4.0, "backroom_units": 1.0}
    assert both["fields"]["counts"][two_lots[-1]] == {"shelf_units": 7.0, "backroom_units": 2.0}
    assert set(both["fields"]) == {"lot_ids", "counts", "needs_input"}  # no total across lots is stored


# --- applicability and the concurrency rule (07-06 Task 3; D-17, D-22, acceptance check 6) -----------------------------

def moved(run, lot_id, **values) -> None:
    """The lot's record changes under the request (the test's own move, after the workspace owns every series)."""
    sets = ", ".join(f"{k} = :{k}" for k in values)
    with db.tx() as conn:
        db.run(conn, f"UPDATE lots SET {sets} WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               r=run["run_id"], l=lot_id, **values)


def test_applicability(run):
    """Acceptance check 6 (Verify path 7): a confirm is refused (409 count_mismatch naming the lot, nothing written)
    when a lot is no longer on the record at the addressed branch, when its SKU is no longer the request's, or when the
    answer's lots are not the request's; a new case version between the answer and the confirm never refuses it, and
    neither does the request's case having become APPROVED: the fact applies and the case is flagged (D-17)."""
    case = story(run)
    cid = case["case_id"]
    q = ask(run, cid)
    s = answer(run, q, counted(q, 0.0))
    to = seat(q["to_branch"])
    lot = q["lot_ids"][0]
    row = lot_row(run, lot)
    own_world(run)
    other_sku = next(k for k in sorted(refdata.get(run["dataset_version"]).products) if k != case["sku"])
    for change, back in (({"branch": case["source_branch"]}, {"branch": row["branch"]}),
                         ({"sku": other_sku}, {"sku": row["sku"]})):
        moved(run, lot, **change)
        e = refused(run, signals.confirm, to, s["signal_id"], s["revision"], True)
        assert (e.status, e.code, e.extra["lot_id"]) == (409, "count_mismatch", lot), change
        assert in_both(run, q)["confirmable"] is False
        assert table(run["run_id"], "reported_facts") == [] and request_row(run, q)["state"] == "ANSWERED"
        moved(run, lot, **back)
    with db.tx() as conn:  # the request now names a lot the answer does not (the test's own change)
        db.run(conn, "UPDATE evidence_requests SET lot_ids = lot_ids || CAST(:x AS jsonb) WHERE run_id = :r "
                     "AND request_id = :q", x=db.js(["TEST-LOT-X"]), r=run["run_id"], q=q["request_id"])
    e = refused(run, signals.confirm, to, s["signal_id"], s["revision"], True)
    assert (e.status, e.code, e.extra["lot_id"]) == (409, "count_mismatch", "TEST-LOT-X")
    with db.tx() as conn:
        db.run(conn, "UPDATE evidence_requests SET lot_ids = CAST(:x AS jsonb) WHERE run_id = :r AND request_id = :q",
               x=db.js(q["lot_ids"]), r=run["run_id"], q=q["request_id"])
    assert in_both(run, q)["confirmable"] is True
    plan = cases.parse_plan(active(run, cid)["result"]["recommended"])
    trip = next(t for t in active(run, cid)["engine_input"]["runs"]
                if t["run_code"] == plan.run_code and t["dest"] == plan.dest)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    call(run, workflow.adopt, source(run, cid), cid, quote, "carrier quote by phone")
    assert version(run, cid) != q["rec_version"]  # a new version between the answer and the confirm
    out = confirm(run, to, s["signal_id"], s["revision"])
    assert out["fact_id"] and request_row(run, q)["state"] == "CONFIRMED"

    went = other_sandbox()  # the request's case went APPROVED after the answer
    c2 = story(went)
    q2 = ask(went, c2["case_id"])
    a2 = answer(went, q2, counted(q2, 0.0))
    approve_transfer(went, c2["case_id"])
    v2 = version(went, c2["case_id"])
    assert state(went, c2["case_id"]) == "APPROVED" and in_both(went, q2)["confirmable"] is True
    out = confirm(went, seat(q2["to_branch"]), a2["signal_id"], a2["revision"])
    assert out["fact_id"] and out["flagged"] == [c2["case_id"]]
    assert all(lot_row(went, l)["qty"] == 0.0 for l in q2["lot_ids"])  # the fact applies
    (flag,) = table(went["run_id"], "review_flags")
    assert (flag["case_id"], flag["fact_id"]) == (c2["case_id"], out["fact_id"])
    assert (state(went, c2["case_id"]), version(went, c2["case_id"])) == ("APPROVED", v2)  # never re-planned


def test_two_confirms_serialise(run):
    """Edge VER-05 concurrency: two answers for one lot with different check times, confirmed at once from two
    threads. Confirm locks the run FOR UPDATE, so the second is judged against the first one's fact: either the
    later-checked one applies first and the earlier one is refused (older_evidence), or the earlier one applies first
    and the later one replaces it. Never both applied out of order; the later-checked count ends in force."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:second"}])  # a second request for the same lots (a test fact)
    first, second = ask(run, cid), ask(run, cid, "TEST:second")
    at = db_clock() - timedelta(minutes=10)
    newer = answer(run, first, counted(first, 3.0), observed=at.isoformat())
    older = answer(run, second, counted(second, 1.0), observed=(at - timedelta(seconds=1)).isoformat())
    to = seat(first["to_branch"])
    start, out = threading.Barrier(2), {}

    def go(name, s):
        start.wait()
        try:  # as the route locks it: the run FOR UPDATE first
            out[name] = ("ok", db.in_tx(lambda conn: signals.confirm(
                conn, db.lock_run(conn, run["run_id"], mode="UPDATE"), to, s["signal_id"], s["revision"], True)))
        except ApiError as e:
            out[name] = ("error", e)
    threads = [threading.Thread(target=go, args=x) for x in (("newer", newer), ("older", older))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert out["newer"][0] == "ok", out["newer"]
    facts = {f["fact_id"]: f for f in table(run["run_id"], "reported_facts")}
    applied = facts[out["newer"][1]["fact_id"]]
    if out["older"][0] == "ok":  # the earlier check applied first, then the later one replaced it in order
        assert facts[out["older"][1]["fact_id"]]["confirmed_at"] < applied["confirmed_at"]
    else:
        assert (out["older"][1].code, out["older"][1].extra["lot_ids"]) == ("older_evidence", first["lot_ids"])
        assert list(facts) == [applied["fact_id"]]
    assert all(lot_row(run, l)["qty"] == 6.0 for l in first["lot_ids"])  # the later-checked count is the record
    with db.tx() as conn:
        assert {facts_mod.in_force(conn, run["run_id"], l, "stock_count")["fact_id"] for l in first["lot_ids"]} == {
            applied["fact_id"]}


PHOTO = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00 a test shelf photo \xff\xd9"  # the test's own bytes behind a JPEG signature


def test_photo_only_answer(run, monkeypatch, tmp_path):
    """Acceptance check 2 (VER-04): a photo without every count keeps the request OPEN as 'Photo received · count
    missing', ' · overdue' once due; the record is unchanged and the fact stays unverified (the badge counts it again
    after the deadline). Edge VER-04 boundary: every count but one is still a photo partial, and at exactly due_at the
    label gains ' · overdue'. The view carries the file's type and size, never where it is stored (D-20)."""
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    case = story(run)
    cid = case["case_id"]
    q = ask(run, cid)
    to = seat(q["to_branch"])
    before = {l: lot_row(run, l) for l in q["lot_ids"]}
    s = call(run, signals.answer, to, q["request_id"], "", None, "now", PHOTO)
    assert s["file"] == {"type": "image/jpeg", "size": len(PHOTO)} and len(s["fields"]["needs_input"]) == \
        2 * len(q["lot_ids"])
    seen = in_both(run, q)
    assert (request_row(run, q)["state"], seen["status_kind"], seen["status"]) == (
        "OPEN", "PHOTO_PARTIAL", "Photo received · count missing")
    assert seen["answer"]["file"] == {"type": "image/jpeg", "size": len(PHOTO)} and seen["confirmable"] is False
    assert "file_uri" not in json.dumps(seen) and "file://" not in json.dumps(seen)
    one_short = {**counted(q, 2.0), q["lot_ids"][0]: {"shelf_units": 2.0, "backroom_units": None}}
    s2 = call(run, signals.answer, to, q["request_id"], "", one_short, "now", PHOTO)
    assert s2["fields"]["needs_input"] == [{"lot_id": q["lot_ids"][0], "field": "backroom_units"}]
    assert (in_both(run, q)["status_kind"], in_both(run, q)["status"]) == (
        "PHOTO_PARTIAL", "Photo received · count missing")
    e = refused(run, signals.confirm, to, s2["signal_id"], s2["revision"], True)
    assert (e.status, e.code) == (409, "needs_input")
    assert {l: lot_row(run, l) for l in q["lot_ids"]} == before  # a photo changes no stock
    assert table(run["run_id"], "reported_facts") == []
    past_due(run, q)
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["status"], seen["overdue"]) == (
        "PHOTO_PARTIAL", "Photo received · count missing · overdue", True)
    assert request_row(run, q)["state"] == "OPEN"
    (unverified,) = [b for b in badges(run, cid) if b["kind"] == "unverified"]
    assert q["fact_id"] in unverified["fact_ids"]  # still unverified, counted again once overdue
    photo = {"complete": False, "photo": True, "note": None, "answered_at": NOW}
    assert status("OPEN", DUE, photo)["sentence"] == "Photo received · count missing · overdue"  # at due_at itself
    assert status("OPEN", BEFORE, photo)["sentence"] == "Photo received · count missing"


# --- Check printed expiry date (07-08 Task 1; 07-CONTEXT D-11, D-21, D-22; research F-23) -----------------------------

def expiry_fixture(run) -> tuple[dict, dict]:
    """(the story case, its plan-changing ESTIMATED_EXPIRY fact): the test marks the story lot's expiry ESTIMATED by
    UPDATE (the workspace owns the series first, as every write in api/ does) and re-evaluates the case. The template
    gives no estimated expiry, so the fact is the test's own move; the engine decides whether it changes the plan."""
    case = story(run)
    lot = lot_row(run, case["lot_id"])
    own_series(run, lot["branch"], lot["sku"])
    with db.tx() as conn:
        db.run(conn, "UPDATE lots SET expiry_source = 'ESTIMATED' WHERE run_id = :r AND world = 'shelfshift' "
                     "AND lot_id = :l", r=run["run_id"], l=case["lot_id"])
        locked = db.lock_run(conn, run["run_id"], mode="UPDATE")
        cases.evaluate_case(conn, locked, cases.case_row(conn, run["run_id"], case["case_id"], lock=True))
    fact = next((f for f in active(run, case["case_id"])["fact_checks"]
                 if f["kind"] == "ESTIMATED_EXPIRY" and f["changes_plan"] and f["lot_id"] == case["lot_id"]), None)
    assert fact is not None, "hand-back: an ESTIMATED story lot gives no plan-changing ESTIMATED_EXPIRY fact"
    return story(run), fact


def answer_dates(run, q, dates, checked_lot=True, observed="now", text="", file=None, actor=None) -> dict:
    """The addressed branch answers an expiry part: a printed date (or "MIXED") per lot, the tick 'I checked the
    specified lot' and when it checked (07-CONTEXT D-11, D-22)."""
    return call(run, signals.answer, actor or seat(q["to_branch"]), q["request_id"], text, None, observed, file,
                dates, checked_lot)


def iso_day(run, day: int) -> str:
    return cases.iso_of(fresh(run["run_id"]), day)


def test_business_dates_go_through_one_helper(run):
    """Edge VER-03 precision: a printed date is a whole business day in the dataset calendar, converted by one helper
    pair; anything but YYYY-MM-DD is 400 bad_request."""
    r = fresh(run["run_id"])
    ref = refdata.get(r["dataset_version"])
    assert cases.iso_of(r, r["day"]) == ref.date(r["day"]).isoformat()
    assert cases.day_of(r, cases.iso_of(r, r["day"] + 3)) == r["day"] + 3
    for bad in ("15/10/2026", "2026-10-15T00:00:00", "20261015", "", None, 20261015, "2026-02-30"):
        with pytest.raises(ApiError) as e:
            cases.day_of(r, bad)
        assert (e.value.status, e.value.code, e.value.message) == (400, "bad_request", "Give the date as YYYY-MM-DD")


def test_expiry_check_end_to_end(run):
    """D-11 tracer: 'Check printed expiry date' on an ESTIMATED_EXPIRY fact through the same request_evidence (the
    old 'Only a stock count…' refusal is gone); the answer gives the printed date per lot with the tick and the check
    time; the lot's manager confirms; one `expiry` reported fact valid from the business day to the later of the
    printed and recorded dates; lots.expiry_day is never rewritten (F-23); the case re-plans without the suspicion."""
    case, fact = expiry_fixture(run)
    cid = case["case_id"]
    lot_id = fact["lot_id"]
    before = lot_row(run, lot_id)
    v0 = version(run, cid)
    q = ask(run, cid, fact["fact_id"])
    assert (q["part"], q["fact_kind"], q["fact_id"], q["lot_ids"], q["to_branch"]) == (
        "expiry", "ESTIMATED_EXPIRY", fact["fact_id"], fact["lot_ids"], fact["branch"])
    assert q["ask_text"].startswith("Please check the printed expiry date of ") and case["sku"] in q["ask_text"]
    respond_by = realtime.label(request_row(run, q)["due_at"])
    assert lot_id in q["ask_text"] and q["ask_text"].endswith(f"Respond by {respond_by}.")
    assert (q["request_group"], [p["part"] for p in q["group_parts"]]) == (q["request_id"], ["expiry"])
    printed = before["expiry_day"] - 1  # the test's own printed date: a day earlier than the record
    a = answer_dates(run, q, {lot_id: iso_day(run, printed)})
    assert (a["kind"], a["fields"]["dates"], a["fields"]["needs_input"], a["fields"]["checked_lot"]) == (
        "EXPIRY", {lot_id: iso_day(run, printed)}, [], True)
    seen = in_both(run, q)
    assert (seen["state"], seen["status_kind"], seen["confirmable"], seen["part"]) == (
        "ANSWERED", "ANSWERED", True, "expiry")
    out = confirm(run, seat(q["to_branch"]), seen["answer_signal_id"], seen["answer"]["revision"], None)
    day = fresh(run["run_id"])["day"]
    [f] = table(run["run_id"], "reported_facts")
    assert (f["fact_id"], f["kind"], f["source"], f["branch"]) == (out["fact_id"], "EXPIRY", "request", q["to_branch"])
    assert f["adjustments"] == [{"kind": "expiry", "lot_id": lot_id, "expiry_day": printed}]
    assert (f["valid_from"], f["valid_to"]) == (day, max(printed, before["expiry_day"], day))
    assert f["observed_at"] == signal_row(run, a["signal_id"])["observed_at"]
    assert out["labels"] == [f"Printed date confirmed by {q['to_branch']} manager · {confirmed_at(run, f['fact_id'])} "
                             f"· {cases.date_label(printed, refdata.get(run['dataset_version']))}"]
    after = lot_row(run, lot_id)
    assert (after["expiry_day"], after["expiry_source"]) == (before["expiry_day"], "ESTIMATED")  # never rewritten
    assert request_row(run, q)["state"] == "CONFIRMED" and signal_row(run, a["signal_id"])["state"] == "CONFIRMED"
    v1 = version(run, cid)
    assert v1 != v0 and cid in [c["case_id"] for c in out["cases"]]
    rec = active(run, cid)
    assert {"kind": "expiry", "lot_id": lot_id, "expiry_day": printed} in rec["engine_input"]["reported_facts"]
    assert not [c for c in rec["fact_checks"] if c["kind"] == "ESTIMATED_EXPIRY" and c["lot_id"] == lot_id]
    assert [e["payload"]["fact_id"] for e in activity(run["run_id"], "expiry_confirmed")] == [out["fact_id"]]


def test_expiry_answer_edges(run, monkeypatch, tmp_path):
    """An expiry answer with a date and no tick is 409 scope_tick naming the lot; a photo and no date is
    'Photo received · date missing'; a date outside the engine's expiry bound is 400 out_of_bounds with field
    'dates.<lot>'; a malformed date is 400; a date for another lot is 409 count_mismatch. None of them writes a fact."""
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    case, fact = expiry_fixture(run)
    q = ask(run, case["case_id"], fact["fact_id"])
    lot_id = fact["lot_id"]
    day = fresh(run["run_id"])["day"]
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", None, "now", None,
                {lot_id: iso_day(run, day)}, False)
    assert (e.status, e.code, e.message) == (409, "scope_tick", f"Tick the box to confirm you checked lot {lot_id}.")
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", None, "now", None,
                {lot_id: iso_day(run, day - 2)}, True)  # before yesterday: outside the engine's bound
    assert (e.status, e.code, e.extra["field"]) == (400, "out_of_bounds", f"dates.{lot_id}")
    assert lot_id in e.message
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", None, "now", None,
                {lot_id: "15/10/2026"}, True)
    assert (e.status, e.code) == (400, "bad_request")
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", None, "now", None,
                {"NOT-A-LOT": iso_day(run, day)}, True)
    assert (e.status, e.code) == (409, "count_mismatch")
    assert request_row(run, q)["answer_signal_id"] is None
    s = answer_dates(run, q, None, checked_lot=False, file=PHOTO)
    assert s["fields"]["needs_input"] == [{"lot_id": lot_id, "field": "date"}]
    seen = in_both(run, q)
    assert (request_row(run, q)["state"], seen["status_kind"], seen["status"], seen["confirmable"]) == (
        "OPEN", "PHOTO_PARTIAL", "Photo received · date missing", False)
    e = refused(run, signals.confirm, seat(q["to_branch"]), s["signal_id"], s["revision"], None)
    assert (e.status, e.code) == (409, "needs_input")
    text = answer_dates(run, q, None, checked_lot=False, text="The label is under the shrink wrap")
    assert text["fields"]["needs_input"] == [{"lot_id": lot_id, "field": "date"}]
    assert (in_both(run, q)["status_kind"], in_both(run, q)["status"]) == ("TEXT_PARTIAL", signals.PARTIAL_DATE)
    assert table(run["run_id"], "reported_facts") == []


def test_count_path_keeps_its_part(run):
    """'Count stock' stays on PHANTOM_STOCK and STALE_COUNT: its request is the part 'count', and dates are refused on
    it (only an expiry part takes dates)."""
    case = story(run)
    q = ask(run, case["case_id"])
    assert (q["part"], q["request_group"], [p["part"] for p in q["group_parts"]]) == ("count", q["request_id"],
                                                                                     ["count"])
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", counted(q), "now", None,
                {q["lot_ids"][0]: iso_day(run, fresh(run["run_id"])["day"])}, True)
    assert (e.status, e.code) == (400, "bad_request")


def test_expiry_check_over_http(client):
    """The multipart fields `dates` (JSON) and `checked_lot` reach the service; `fact_ids` with one fact is the same
    as `fact_id`; the expiry confirm takes `{"revision"}`."""
    run = fresh(client.get("/api/session").json()["run_id"])
    case, fact = expiry_fixture(run)
    cid = case["case_id"]
    src = case["source_branch"]
    as_seat(client, src)
    body = {"rec_version": version(run, cid), "fact_ids": [fact["fact_id"]]}
    r = client.post(f"/api/cases/{cid}/requests", json=body, headers=key())
    assert r.status_code == 200, r.json()
    q = r.json()
    assert q["part"] == "expiry"
    as_seat(client, q["to_branch"])
    printed = iso_day(run, lot_row(run, fact["lot_id"])["expiry_day"])
    path = f"/api/requests/{q['request_id']}/answer"
    form = {"text": (None, ""), "dates": (None, json.dumps({fact["lot_id"]: printed})), "observed": (None, "now")}
    r = client.post(path, files=form, headers=key())
    assert (r.status_code, r.json()["error"]) == (409, "scope_tick")
    r = client.post(path, files={**form, "dates": (None, "{not json")}, headers=key())
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")
    r = client.post(path, files={**form, "checked_lot": (None, "true")}, headers=key())
    assert r.status_code == 200, r.json()
    assert (r.json()["kind"], r.json()["fields"]["dates"]) == ("EXPIRY", {fact["lot_id"]: printed})
    done = client.post(f"/api/signals/{r.json()['signal_id']}/confirm", json={"revision": r.json()["revision"]},
                       headers=key())
    assert done.status_code == 200, done.json()
    assert done.json()["fact_id"] and version(run, cid) != q["rec_version"]


# --- Combined requests (07-08 Task 2; 07-CONTEXT D-11, D-22; acceptance check 5) ---------------------------------------

def pair_fixture(run) -> tuple[dict, dict, dict]:
    """(the story case, a count fact, the expiry fact) at one branch with the expiry lots within the count lots: the
    Task 1 fixture plus the test's own STALE_COUNT fact on the same lots (add_facts)."""
    case, expiry = expiry_fixture(run)
    count = {**expiry, "fact_id": "TEST:stale", "kind": "STALE_COUNT"}
    add_facts(run, case["case_id"], [count])
    return case, count, expiry


def ask_pair(run, case_id, fact_ids, actor=None) -> dict:
    return call(run, signals.request_evidence, actor or source(run, case_id), case_id, version(run, case_id), None,
                None, fact_ids)


def parts_of(run, q) -> dict:
    """{part: request view} for every part of q's group, as the case detail lists them."""
    with db.tx() as conn:
        r = fresh(run["run_id"])
        views = signals.requests_for_case(conn, r, cases.case_row(conn, r["run_id"], q["case_id"]))
    return {v["part"]: v for v in views if v["request_group"] == q["request_group"]}


def test_combined_parts(run):
    """Acceptance check 5: a count fact and an expiry fact asked together are one combined request: two rows with one
    request_group, parts count and expiry, the same due_at; the count part answered and confirmed alone applies its
    fact; the expiry part stays OPEN and later reads Unanswered on its own deadline."""
    case, count, expiry = pair_fixture(run)
    cid = case["case_id"]
    q = ask_pair(run, cid, [expiry["fact_id"], count["fact_id"]])  # either order
    assert q["part"] == "count" and q["fact_id"] == count["fact_id"]
    assert [(p["part"], p["fact_id"], p["status_kind"]) for p in q["group_parts"]] == [
        ("count", count["fact_id"], "AWAITING"), ("expiry", expiry["fact_id"], "AWAITING")]
    rows = table(run["run_id"], "evidence_requests")
    assert len(rows) == 2 and {r["request_group"] for r in rows} == {q["request_id"]}
    assert {r["part"] for r in rows} == {"count", "expiry"} and len({r["due_at"] for r in rows}) == 1
    parts = parts_of(run, q)
    assert parts["expiry"]["request_group"] == q["request_id"] and parts["expiry"]["group_parts"] == q["group_parts"]
    assert ask_pair(run, cid, [count["fact_id"], expiry["fact_id"]])["request_id"] == q["request_id"]  # asked twice
    assert len(table(run["run_id"], "evidence_requests")) == 2
    answer(run, q, counted(q, 0.0))
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["confirmable"]) == ("ANSWERED", True)
    out = confirm(run, seat(q["to_branch"]), seen["answer_signal_id"], seen["answer"]["revision"])
    [fact] = table(run["run_id"], "reported_facts")
    assert (fact["fact_id"], fact["kind"]) == (out["fact_id"], "STOCK_COUNT")
    parts = parts_of(run, q)
    assert (parts["count"]["state"], parts["expiry"]["state"], parts["expiry"]["status_kind"]) == (
        "CONFIRMED", "OPEN", "AWAITING")
    assert [p["status_kind"] for p in parts["expiry"]["group_parts"]] == ["CONFIRMED", "AWAITING"]
    past_due(run, parts["expiry"])
    assert parts_of(run, q)["expiry"]["status_kind"] == "UNANSWERED"


def test_the_group_is_the_unit_of_the_limit_and_of_cant_check(run):
    """The assumption-delta invariant: a combined request uses one slot of requests.max_per_version (a second single
    request still fits, a third group is 409 too_many_requests); Can't check on either part closes both open parts;
    a confirmed part stays confirmed."""
    case, count, expiry = pair_fixture(run)
    cid = case["case_id"]
    add_facts(run, cid, [{**count, "fact_id": "TEST:second"}, {**count, "fact_id": "TEST:third"}])
    limit = config.MAX_REQUESTS_PER_VERSION
    q = ask_pair(run, cid, [count["fact_id"], expiry["fact_id"]])
    single = ask(run, cid, "TEST:second")  # the second group fits
    assert single["request_group"] == single["request_id"] and single["part"] == "count"
    e = refused(run, signals.request_evidence, source(run, cid), cid, version(run, cid), "TEST:third")
    assert (e.status, e.code, e.message) == (409, "too_many_requests",
                                             f"At most {limit} count requests per case version")
    assert in_both(run, single)["askable_again"] is False  # the version holds its limit of groups
    expiry_part = parts_of(run, q)["expiry"]
    closed = cant_check(run, expiry_part)  # Can't check on the expiry part closes the count part too
    assert closed["state"] == "CANT_CHECK"
    assert {p["part"]: p["state"] for p in parts_of(run, q).values()} == {"count": "CANT_CHECK",
                                                                           "expiry": "CANT_CHECK"}
    [entry] = activity(run["run_id"], "evidence_cant_check")
    assert sorted(entry["payload"]["parts"]) == sorted([q["request_id"], expiry_part["request_id"]])
    assert request_row(run, single)["state"] == "OPEN"  # another group is untouched
    other = other_sandbox()  # a confirmed part stays confirmed when the other part is closed
    c2, count2, expiry2 = pair_fixture(other)
    q2 = ask_pair(other, c2["case_id"], [count2["fact_id"], expiry2["fact_id"]])
    answer(other, q2, counted(q2, 0.0))
    seen = in_both(other, q2)
    confirm(other, seat(q2["to_branch"]), seen["answer_signal_id"], seen["answer"]["revision"])
    cant_check(other, parts_of(other, q2)["expiry"])
    assert {p["part"]: p["state"] for p in parts_of(other, q2).values()} == {"count": "CONFIRMED",
                                                                            "expiry": "CANT_CHECK"}
    e = refused(other, signals.cant_check, seat(q2["to_branch"]), q2["request_id"])
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is CONFIRMED")


def test_a_bad_pair_is_refused(run):
    """Only one count fact and one expiry fact at one branch, the expiry lots within the count lots, form a combined
    request: two count facts, an expiry fact at another branch or on lots outside the count fact's lots are 400
    bad_request, and nothing is written."""
    case, count, expiry = pair_fixture(run)
    cid = case["case_id"]
    other_branch = next(b for b in refdata.get(run["dataset_version"]).branches if b != expiry["branch"])
    add_facts(run, cid, [{**count, "fact_id": "TEST:count2"},
                         {**expiry, "fact_id": "TEST:far", "branch": other_branch},
                         {**expiry, "fact_id": "TEST:outside", "lot_ids": ["TEST-LOT-ELSEWHERE"],
                          "lot_id": "TEST-LOT-ELSEWHERE"}])
    pair = "Ask together one count fact and one expiry fact at one branch, the expiry lots within the count lots"
    for ids in ([count["fact_id"], "TEST:count2"], [expiry["fact_id"], "TEST:far"], [count["fact_id"], "TEST:far"],
                [count["fact_id"], "TEST:outside"], [count["fact_id"], count["fact_id"]]):
        e = refused(run, signals.request_evidence, source(run, cid), cid, version(run, cid), None, None, ids)
        assert (e.status, e.code, e.message) == (400, "bad_request", pair), ids
    e = refused(run, signals.request_evidence, source(run, cid), cid, version(run, cid), None, None,
                [count["fact_id"], "TEST:nowhere"])
    assert (e.status, e.code) == (400, "unknown_fact")
    assert table(run["run_id"], "evidence_requests") == [] and asked_entries(run["run_id"]) == []
