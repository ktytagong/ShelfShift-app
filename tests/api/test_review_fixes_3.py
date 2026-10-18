"""The third outside-review fix pass on Phase 7's server (07-CONTEXT D-37, D-38; the review of the second fix pass
and of 07-14).

- U1, U2: bugs in the second pass's reopen rules (D-38.1, D-38.3).
- V1, V2: bugs in 07-14's field reports (a count's day across an edit; a report file's linked case).

Lots are found by tag or created here by the test, never by a pasted id; nothing sleeps.
"""
from __future__ import annotations

import pytest

from api import db, workflow
from tests.api.conftest import fresh, seat
from tests.api.test_reassess import opened_entry, reject_story, successor_of
from tests.api.test_review_fixes_2 import closing, pending_with_hold, recheck_spy, story_places, strawberry_case
from tests.api.test_workflow import call


# --- U1 (D-38.1): a close that ends an expired hold rechecks the lots that hold can affect -------------------------

def booking_states(run_id: str, case_id: str) -> set[str]:
    with db.tx() as conn:
        return {r["state"] for r in db.rows(conn, "SELECT state FROM reservations WHERE run_id = :r AND case_id = :c",
                                            r=run_id, c=case_id)}


@pytest.mark.parametrize("marked_first", [False, True], ids=["expired-by-the-close", "expired-by-an-earlier-read"])
def test_u1_a_close_that_ends_an_expired_hold_rechecks_the_lots_it_can_affect(run, monkeypatch, marked_first):
    """D-38.1 (fix-pass review U1): the strawberry case is pending approval, holding chilled storage at a destination
    of the story lot, when the story case is rejected. The strawberry round's hour then runs out and its source
    manager rejects it. The hold is marked EXPIRED, by the close's own hold expiry or by an earlier read (the case
    page calls bookings.expire_holds), so the close releases nothing. An expired hold is a change all the same: the
    milk lot is rechecked and reopens once, trigger "release", reason "inputs"."""
    from api import bookings
    dest, _ = story_places(run)
    straw = strawberry_case(run)
    pending_with_hold(run, straw, "STORAGE", f"{dest}|chilled", fresh(run["run_id"])["day"] + 1)
    row = reject_story(run)
    assert successor_of(run, row["case_id"]) is None
    with db.tx() as conn:  # the round's hour is up (a test value), not yet marked
        db.run(conn, "UPDATE reservations SET expires_at = now() - interval '1 minute' WHERE run_id = :r "
                     "AND case_id = :c AND state = 'HELD'", r=run["run_id"], c=straw["case_id"])
    if marked_first:
        with db.tx() as conn:
            assert bookings.expire_holds(conn, fresh(run["run_id"]), straw["case_id"]) == [straw["case_id"]]
    calls = recheck_spy(monkeypatch)
    assert call(run, workflow.reject, seat(straw["source_branch"]), straw["case_id"], "Not needed") == \
        {"state": "REJECTED"}
    assert booking_states(run["run_id"], straw["case_id"]) == {"EXPIRED"}  # nothing was left to release
    assert row["case_id"] in calls, "the expired hold could affect the milk lot"
    succ = successor_of(run, row["case_id"])
    assert succ is not None and (opened_entry(run, succ["case_id"])["trigger"],
                                 opened_entry(run, succ["case_id"])["reasons"]) == ("release", ["inputs"])


# --- U2 (D-38.3): a case closed for missing data stores the empty considered record ---------------------------------

def without_source_day(monkeypatch, run, case: dict):
    """Reference data with the case lot's source missing its forecast for tomorrow (its destinations' data intact), as
    a gap in the source's series leaves it: the search and the ESL-02 screen raise the engine's HorizonError, while
    the morning state (`closing.state`: Keep simulated at the destinations only) still reads. Monkeypatched on the
    cached Ref for the context's life; reference data is never edited."""
    from api import publicref, refdata
    ref = refdata.get(fresh(run["run_id"])["dataset_version"])
    real, gap = ref.forecast, fresh(run["run_id"])["day"] + 1

    def gapped(day, store, sku):
        points = real(day, store, sku)
        if (store, sku) == (case["source_branch"], case["sku"]):
            points.pop(gap, None)
        return points
    monkeypatch.setattr(ref, "forecast", publicref.Called(dict(real), gapped))


def test_u2_a_lot_closed_for_missing_data_stores_the_empty_record_even_when_its_state_reads(run, monkeypatch):
    """D-38.3 (fix-pass review U2): the story lot's source loses tomorrow's forecast, so re-evaluating the story case
    leaves it without a version and with missing_data, while the morning state still reads its actions from the
    destinations' data. Rejecting it stores the empty considered record (the day and days left, no step, no
    clearance, the window closed, no actions), never the actions the state read. The next morning, with the forecast
    back, the lot still qualifies with real options and reopens once through `new_option`."""
    from api import cases
    from tests.api.conftest import world_lot
    from tests.api.test_advance import advance_once
    from tests.api.test_reassess import lot_cases, lot_state, qualifies
    from tests.api.test_selection import case_now
    from tests.api.verify_helpers import story
    case = story(run)
    with monkeypatch.context() as m:
        without_source_day(m, run, case)
        db.in_tx(lambda conn: cases.evaluate_case(conn, fresh(run["run_id"]),
                                                  cases.case_row(conn, run["run_id"], case["case_id"])))
        missing = case_now(run, case["case_id"])
        assert missing["active_rec_version"] is None and missing["missing_data"].startswith("missing data: ")
        assert lot_state(run, case["lot_id"])["actions"], "the state reads the destinations' data"
        row = reject_story(run)
    day = fresh(run["run_id"])["day"]
    days_left = world_lot(run, case["lot_id"])["expiry_day"] - day
    assert row["lot_status"] == "AT_RISK"
    assert row["considered"] == {"day": day, "days_left": days_left, "band": closing()._band(days_left),
                                 "step_pct": 0, "clearance": False, "transfer_open": False, "actions": []}
    assert advance_once(run)["advanced"] is True
    assert lot_state(run, case["lot_id"])["actions"] and qualifies(run, case["lot_id"])
    succ = successor_of(run, row["case_id"])
    assert succ is not None and opened_entry(run, succ["case_id"])["trigger"] == "next_day"
    assert "new_option" in opened_entry(run, succ["case_id"])["reasons"]
    assert len(lot_cases(run, case["lot_id"])) == 2


# --- V1 (CR-02, D-22): a count edited by its text alone keeps the day it was taken ----------------------------------

def test_v1_a_count_edited_by_text_alone_on_a_later_day_is_still_from_its_day(client):
    """07-14 review V1 (05-REVIEW CR-02, D-22): the lot's manager files a count of 20 units, checked now. After Next
    day the reporter edits the text only: revision 2 keeps the count's day, so confirming it is 409 stale_version
    naming the day it was filed, and nothing is written (the record and the facts as they were). An edit that gives
    a new check time is today's count again, confirmed today."""
    from tests.api import test_http
    from tests.api.conftest import as_seat, world_lot
    from tests.api.test_evidence import table
    from tests.api.test_signals import confirm_count_report, filed, patch_report, phantom_lot, session_run
    card = test_http.story(client)
    run = session_run(client)
    run_id, day = run["run_id"], run["day"]
    lot_id, branch = phantom_lot(client, card)
    as_seat(client, branch)
    rep = filed(client, "STOCK_COUNT", {"lot_id": lot_id, "shelf_units": 20, "backroom_units": 0}, observed="now")
    assert rep["day"] == day
    test_http.advance(client)
    assert session_run(client)["day"] == day + 1
    r = patch_report(client, rep["signal_id"], {"revision": 1, "text": "Counted before the delivery"})
    assert r.status_code == 200, r.json()
    two = r.json()
    assert (two["revision"], two["text"], two["observed_at"]) == (2, "Counted before the delivery", rep["observed_at"])
    record, facts = world_lot(run_id, lot_id), table(run_id, "reported_facts")
    r = confirm_count_report(client, rep["report_id"], revision=2)
    assert (r.status_code, r.json().get("error"), r.json().get("answer_day")) == (409, "stale_version", day), r.json()
    assert world_lot(run_id, lot_id) == record and table(run_id, "reported_facts") == facts
    assert two["day"] == day  # the report's day is still the day the count was taken
    assert client.get(f"/api/signals/{two['signal_id']}").json()["state"] == "READ"  # kept, unconfirmed
    r = patch_report(client, rep["report_id"], {"revision": 2, "observed": "now"})
    assert r.status_code == 200 and r.json()["day"] == day + 1, r.json()
    assert confirm_count_report(client, rep["report_id"], revision=3).status_code == 200


# --- V2 (D-20): a report's file is read by the seat parties of the case it is linked to ------------------------------

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00 a test shelf photo \xff\xd9"


def test_v2_a_report_file_is_read_by_its_linked_cases_destination(client, tmp_path, monkeypatch):
    """07-14 review V2 (D-20; research A6): the story case's source manager files a CAPACITY report with a photo,
    linked to the story case (a transfer from that source to its destination). The destination's manager, a party to
    the report through the case, reads the report and downloads its file; a manager of an unrelated store gets 403
    on both. Files go to the test's own folder (the bucket key unset)."""
    from api import config
    from tests.api import test_http
    from tests.api.conftest import as_seat, key
    from tests.api.test_signals import report_parts, today_iso
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    card = test_http.story(client)
    source, dest, day = card["source"], card["dest"], today_iso(client)
    assert dest is not None and dest != source
    as_seat(client, source)
    parts = report_parts("CAPACITY", {"zone": card["zone"], "free_totes": 5, "valid_from": day, "valid_to": day},
                         case_id=card["case_id"])
    r = client.post("/api/signals", files={**parts, "file": ("shelf.jpg", JPEG, "image/jpeg")}, headers=key())
    assert r.status_code == 200, r.json()
    rep = r.json()
    assert (rep["branch"], rep["case_id"], rep["file"]["type"]) == (source, card["case_id"], "image/jpeg")
    path = f"/api/signals/{rep['signal_id']}/file"
    as_seat(client, dest)
    assert client.get(f"/api/signals/{rep['signal_id']}").status_code == 200  # a party to the report
    got = client.get(path)
    assert (got.status_code, got.content) == (200, JPEG), got.text
    unrelated = next(b for b in store_codes(client) if b not in (source, dest))
    as_seat(client, unrelated)
    assert test_http.error(client.get(f"/api/signals/{rep['signal_id']}")) == (403, "forbidden")
    assert test_http.error(client.get(path)) == (403, "forbidden")


def store_codes(client) -> list[str]:
    from api import refdata
    from tests.api.test_signals import session_run
    return refdata.get(session_run(client)["dataset_version"]).branches
