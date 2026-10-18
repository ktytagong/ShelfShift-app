"""Saved answer readings and exact-source confirmation, using local SDK fakes."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import as_seat, key
from agent.reading import Evidence, Reading
from api import config, db, files, refdata, sandbox
from api.app import COOKIE, app
from agent import readings_job
from tests.api.test_evidence import ask, from_inbox, request_row, signal_row, table
from tests.api.test_requests import answer_dates, expiry_fixture
from tests.api.test_files import JPEG, http_answer, http_ask
from tests.api.test_http import multipart
from tests.agent.test_readings_http import FakeModels, counts, identity, install


def answer_reading(client, answer):
    return client.post(f"/api/signals/{answer['signal_id']}/reading",
                       json={"revision": answer["revision"]})


def saved_answer(client, monkeypatch, tmp_path, *, text="", typed=None):
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    request = http_ask(client)
    result = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text, counts=typed)
    assert result.status_code == 200, result.text
    run, _ = identity(client)
    return run, request, result.json()


def test_photo_only_answer_is_read_as_an_observation(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    lot = request["lot_ids"][0]
    fake = install(monkeypatch, FakeModels(Reading(kind="STOCK_COUNT", lot_id=lot, shelf_units=0,
        photo_observation="visible slot empty", evidence=[
            Evidence(field="lot_id", source="photo", excerpt=lot, confidence=.9),
            Evidence(field="shelf_units", source="photo", excerpt="empty", confidence=.9)])))
    before = signal_row(run, answer["signal_id"])
    request_before = request_row(run, request)
    spent = counts()

    result = answer_reading(client, answer)
    assert result.status_code == 200, result.text
    out = result.json()
    assert (out["status"], out["served"]) == ("DONE", "live")
    assert out["observations"] == ["visible slot empty"]
    assert out["pending_counts"] is None and out["printed_dates"] is None
    assert out["not_checked"] == "Backroom stock not checked, so the total can't be confirmed"
    assert out["conflicts"] == [] and out["after_confirmation"] is False
    assert signal_row(run, answer["signal_id"]) == before
    assert request_row(run, request) == request_before
    assert from_inbox(run, request)["status"] == "Photo received · count missing"
    assert counts()["ai"] == spent.get("ai", 0) + 2  # one request, session and global counters
    again = answer_reading(client, answer)
    assert again.status_code == 200 and again.json()["reading_id"] == out["reading_id"]
    assert again.json()["served"] == "reuse" and len(fake.calls) == 1
    assert signal_row(run, answer["signal_id"]) == before
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM readings WHERE run_id=:r AND reading_id=:id",
                     r=run["run_id"], id=out["reading_id"])
    assert (row["purpose"], row["signal_id"], row["signal_revision"], row["request_id"]) == (
        "ANSWER", answer["signal_id"], answer["revision"], request["request_id"])
    context = json.loads(fake.calls[0]["contents"][0].parts[0].text.split("\nREPORT_DATA", 1)[0])
    assert context["store"] == request["to_branch"]
    assert context["case_prefill"]["sku"] and context["lot_ids"] == request["lot_ids"]


def test_typed_only_answer_uses_no_reading(client, monkeypatch):
    request = http_ask(client)
    typed = {lot: {"shelf_units": 4, "backroom_units": 2} for lot in request["lot_ids"]}
    saved = client.post(f"/api/requests/{request['request_id']}/answer",
                        files=multipart("", typed), headers=key())
    assert saved.status_code == 200, saved.text
    fake = install(monkeypatch)
    before = counts()
    result = answer_reading(client, saved.json())
    assert result.status_code == 409, result.text
    assert result.json() == {"error": "wrong_state", "message": "Nothing to read"}
    assert counts() == before and not fake.calls and not fake.counts


def test_file_permissions_precede_bytes_or_ai(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    with db.tx() as conn:
        case = db.one(conn, "SELECT * FROM cases WHERE run_id=:r AND case_id=:c",
                      r=run["run_id"], c=request["case_id"])
    outsiders = set(refdata.get(run["dataset_version"]).branches) - {
        request["to_branch"], case["source_branch"], case.get("plan_dest")}
    assert outsiders
    as_seat(client, sorted(outsiders)[0])
    fake = install(monkeypatch)
    reads = []
    monkeypatch.setattr(files, "read", lambda uri: reads.append(uri))
    before = counts()
    result = answer_reading(client, answer)
    assert result.status_code == 403, result.text
    assert not reads and not fake.calls and counts() == before


def test_old_answer_revision_is_stale(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    newer = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg")
    assert newer.status_code == 200 and newer.json()["revision"] == answer["revision"] + 1
    fake = install(monkeypatch)
    before = counts()
    old = answer_reading(client, answer)
    wrong_revision = answer_reading(client, {**newer.json(), "revision": answer["revision"]})
    assert old.status_code == wrong_revision.status_code == 409
    assert old.json()["error"] == wrong_revision.json()["error"] == "stale_version"
    assert not fake.calls and not fake.counts and counts() == before


def test_answer_scope_does_not_reassign_another_lots_count(client, monkeypatch, tmp_path):
    text = "None on the shelf, none in the backroom either."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    install(monkeypatch, FakeModels(Reading(kind="STOCK_COUNT", lot_id="not-a-requested-lot", shelf_units=0,
        backroom_units=0, evidence=[Evidence(field=field, source="message", excerpt=text, confidence=.9)
            for field in ("kind", "lot_id", "shelf_units", "backroom_units")])))
    before = signal_row(run, answer["signal_id"])
    result = answer_reading(client, answer)
    assert result.status_code == 200 and result.json()["pending_counts"] is None, result.text
    assert result.json()["not_checked"] == "Backroom stock not checked, so the total can't be confirmed"
    assert signal_row(run, answer["signal_id"]) == before


@pytest.mark.parametrize("field", ["case_id", "request_id"])
def test_reading_optional_ids_reject_multipart_files(client, monkeypatch, field):
    from fastapi.testclient import TestClient
    from api.app import app

    fake = install(monkeypatch)
    spent = counts()
    # Observe the public HTTP status even on the vulnerable baseline's TypeError.
    with TestClient(app, base_url=str(client.base_url), cookies=client.cookies,
                    raise_server_exceptions=False) as guarded:
        result = guarded.post("/api/readings", files={
            "text": (None, "A shelf note for the reading"),
            field: ("identifier.txt", b"file in a string field", "text/plain")})
    assert result.status_code == 400, result.text
    assert result.json()["error"] == "bad_request"
    assert not fake.calls and not fake.counts and counts() == spent


def reply_counts(request, text, shelf=0, backroom=0):
    return Reading(kind="STOCK_COUNT", lot_id=request["lot_ids"][0], shelf_units=shelf,
        backroom_units=backroom, evidence=[Evidence(field=field, source="message", excerpt=text, confidence=.9)
            for field in ("kind", "lot_id", "shelf_units", "backroom_units")])


def test_complementary_partial_sources_remain_separate(client, monkeypatch, tmp_path):
    text = "There are five in the backroom; the shelf was not checked."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    lot = request["lot_ids"][0]
    typed = {lot: {"shelf_units": 10, "backroom_units": None}}
    saved = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text, counts=typed)
    assert saved.status_code == 200, saved.text
    answer = saved.json()
    install(monkeypatch, FakeModels(reply_counts(request, text, shelf=None, backroom=5)))
    before = signal_row(run, answer["signal_id"])
    result = answer_reading(client, answer)
    assert result.status_code == 200, result.text
    out = result.json()
    assert out["typed_values"]["counts"] == typed
    assert out["pending_counts"] == {lot: {"shelf_units": None, "backroom_units": 5}}
    assert out["conflicts"] == []
    assert out["not_checked"] == "Backroom stock not checked, so the total can't be confirmed"
    assert signal_row(run, answer["signal_id"]) == before


def test_reply_counts_are_pending_and_conflicts_are_flagged(client, monkeypatch, tmp_path):
    text = "None on the shelf, none in the backroom either."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    typed = {lot: {"shelf_units": 4, "backroom_units": 2} for lot in request["lot_ids"]}
    saved = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text, counts=typed)
    assert saved.status_code == 200, saved.text
    answer = saved.json()
    fake = install(monkeypatch, FakeModels(reply_counts(request, text)))
    before = signal_row(run, answer["signal_id"])
    result = answer_reading(client, answer)
    assert result.status_code == 200, result.text
    out = result.json()
    lot = request["lot_ids"][0]
    assert out["pending_counts"] == {lot: {"shelf_units": 0, "backroom_units": 0}}
    assert out["conflicts"] == [
        {"field": f"counts.{lot}.shelf_units", "typed": 4, "read": 0},
        {"field": f"counts.{lot}.backroom_units", "typed": 2, "read": 0}]
    assert out["not_checked"] is None
    assert signal_row(run, answer["signal_id"]) == before and len(fake.calls) == 1


@pytest.mark.parametrize("terminal", ["FAILED", "REFUSED"])
def test_retry_only_when_missing_or_interrupted(client, monkeypatch, tmp_path, terminal):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text="None in either location.")
    fake = install(monkeypatch, FakeModels(None))
    with monkeypatch.context() as limit:
        if terminal == "REFUSED":
            limit.setitem(config.SESSION_CAPS, "ai", (0, "day"))
        first = answer_reading(client, answer)
    assert first.status_code == (429 if terminal == "REFUSED" else 200), first.text
    before, n = counts(), len(fake.calls)
    reused = answer_reading(client, answer)
    assert reused.status_code == 200 and reused.json()["status"] == terminal
    assert reused.json()["served"] == "reuse" and len(fake.calls) == n and counts() == before
    saved = http_answer(client, request, "again.jpg", JPEG, "image/jpeg", text="None in either location.")
    assert saved.status_code == 200
    fake.output = reply_counts(request, "None in either location.")
    new = answer_reading(client, saved.json())
    assert new.status_code == 200 and new.json()["status"] == "DONE"
    assert new.json()["reading_id"] != reused.json()["reading_id"] and len(fake.calls) == n + 1


def test_interrupted_revision_retries_only_once(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text="None in either location.")
    fake = install(monkeypatch, FakeModels(reply_counts(request, "None in either location.")))
    first = answer_reading(client, answer)
    assert first.status_code == 200
    with db.tx() as conn:
        db.run(conn, "UPDATE readings SET status='INTERRUPTED' WHERE run_id=:r AND reading_id=:id",
               r=run["run_id"], id=first.json()["reading_id"])
    before = counts()
    second = answer_reading(client, answer)
    assert (second.status_code, second.json()["status"], second.json()["served"]) == (200, "DONE", "live")
    assert second.json()["reading_id"] == first.json()["reading_id"]
    assert len(fake.calls) == 2 and counts()["ai"] == before["ai"] + 2
    with db.tx() as conn:
        row = db.one(conn, "UPDATE readings SET status='INTERRUPTED' WHERE run_id=:r AND reading_id=:id RETURNING *",
                     r=run["run_id"], id=first.json()["reading_id"])
    assert row["attempt"] == 2
    before = counts()
    third = answer_reading(client, answer)
    assert (third.json()["status"], third.json()["served"]) == ("INTERRUPTED", "reuse")
    assert len(fake.calls) == 2 and counts() == before


def test_two_screens_one_reading(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text="None in either location.")
    entered, release = threading.Event(), threading.Event()

    class Waiting(FakeModels):
        async def generate_content(self, **kwargs):
            with db.tx() as conn:
                db.run(conn, "SET LOCAL lock_timeout='1s'")
                db.lock_run(conn, run["run_id"], mode="UPDATE")
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return await super().generate_content(**kwargs)

    fake = install(monkeypatch, Waiting(reply_counts(request, "None in either location.")))
    other = TestClient(app)
    other.cookies.set(COOKIE, client.cookies.get(COOKIE))
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(answer_reading, other, answer)
        try:
            assert entered.wait(15), "the reading never reached the model"
            second = answer_reading(client, answer)
            assert second.status_code == 200, second.text
            assert (second.json()["status"], second.json()["served"]) == ("GENERATING", "generating")
        finally:
            release.set()
        completed = first.result(timeout=15)
    assert completed.status_code == 200 and completed.json()["status"] == "DONE"
    assert answer_reading(client, answer).json()["reading_id"] == completed.json()["reading_id"]
    assert len(fake.calls) == 1


def test_final_answer_revision_is_reused_after_context_change(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text="None in either location.")
    fake = install(monkeypatch, FakeModels(None))
    first = answer_reading(client, answer)
    assert first.status_code == 200 and first.json()["status"] == "FAILED"
    before = counts()
    monkeypatch.setattr(readings_job.assistant_config, "ASSISTANT_VERSION", "test-next-assistant-version")
    reused = answer_reading(client, answer)
    assert reused.status_code == 200 and reused.json()["reading_id"] == first.json()["reading_id"]
    assert reused.json()["served"] == "reuse" and len(fake.calls) == 1 and counts() == before


def test_unquoted_reply_counts_stay_missing(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text="Please inspect the shelf photo.")
    install(monkeypatch, FakeModels(reply_counts(request, "None in either location.")))
    result = answer_reading(client, answer)
    assert result.status_code == 200 and result.json()["pending_counts"] is None
    assert result.json()["not_checked"] == "Backroom stock not checked, so the total can't be confirmed"


def confirm_answer(client, answer, **choices):
    return client.post(f"/api/signals/{answer['signal_id']}/confirm", headers=key(),
                       json={"revision": answer["revision"], "checked_both": True, **choices})


def test_confirm_from_the_reading(client, monkeypatch, tmp_path):
    text = "None on the shelf, none in the backroom either."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    install(monkeypatch, FakeModels(reply_counts(request, text)))
    before = signal_row(run, answer["signal_id"])
    read = answer_reading(client, answer)
    assert read.status_code == 200
    result = confirm_answer(client, answer, value_source="reading", reading_id=read.json()["reading_id"])
    assert result.status_code == 200, result.text
    with db.tx() as conn:
        fact = db.one(conn, "SELECT * FROM reported_facts WHERE run_id=:r AND fact_id=:f",
                      r=run["run_id"], f=result.json()["fact_id"])
    assert fact["reading_id"] == read.json()["reading_id"] and fact["source"] == "reading"
    assert all(adj["units"] == 0 for adj in fact["adjustments"])
    assert "reply" in fact["label"] and request["to_branch"] in fact["label"]
    logged = next(row["payload"] for row in table(run["run_id"], "activity_log")
                  if row["type"] == "count_confirmed" and row["payload"]["signal_id"] == answer["signal_id"])
    assert logged["value_source"] == "reading" and logged["reading_id"] == read.json()["reading_id"]
    stored = signal_row(run, answer["signal_id"])
    assert stored["fields"] == before["fields"] and stored["created_at"] == before["created_at"]
    assert request_row(run, request)["state"] == "CONFIRMED"


@pytest.mark.parametrize("bad_reading", ["unknown", "old_revision", "superseded", "after_confirmation", "FAILED"])
def test_stale_reading_refused(client, monkeypatch, tmp_path, bad_reading):
    text = "None on the shelf, none in the backroom either."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    install(monkeypatch, FakeModels(reply_counts(request, text)))
    read = answer_reading(client, answer)
    assert read.status_code == 200
    reading_id = read.json()["reading_id"]
    if bad_reading == "unknown":
        reading_id = "no-such-reading"
    elif bad_reading == "old_revision":
        newer = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text)
        assert newer.status_code == 200
        answer = newer.json()
    else:
        with db.tx() as conn:
            if bad_reading == "superseded":
                columns = [c for c in sandbox.columns(conn, "readings") if c not in ("reading_id", "input_key", "created_at")]
                rest = ",".join(f'"{c}"' for c in columns)
                db.run(conn, f"INSERT INTO readings (reading_id,input_key,{rest}) SELECT :new,input_key||'-new',{rest} "
                             "FROM readings WHERE run_id=:r AND reading_id=:id",
                       new=uuid.uuid4().hex, r=run["run_id"], id=reading_id)
            elif bad_reading == "after_confirmation":
                db.run(conn, "UPDATE readings SET after_confirmation=true WHERE run_id=:r AND reading_id=:id",
                       r=run["run_id"], id=reading_id)
            else:
                db.run(conn, "UPDATE readings SET status='FAILED' WHERE run_id=:r AND reading_id=:id",
                       r=run["run_id"], id=reading_id)
    before = (signal_row(run, answer["signal_id"]), request_row(run, request), table(run["run_id"], "reported_facts"))
    result = confirm_answer(client, answer, value_source="reading", reading_id=reading_id)
    assert result.status_code == 409 and result.json()["error"] == "stale_version", result.text
    assert (signal_row(run, answer["signal_id"]), request_row(run, request), table(run["run_id"], "reported_facts")) == before


def test_choose_which_count(client, monkeypatch, tmp_path):
    text = "None on the shelf, none in the backroom either."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    typed = {lot: {"shelf_units": 4, "backroom_units": 2} for lot in request["lot_ids"]}
    saved = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text, counts=typed)
    assert saved.status_code == 200
    answer = saved.json()
    install(monkeypatch, FakeModels(reply_counts(request, text)))
    assert answer_reading(client, answer).status_code == 200
    before = signal_row(run, answer["signal_id"])
    ambiguous = confirm_answer(client, answer)
    assert ambiguous.status_code == 400, ambiguous.text
    assert ambiguous.json() == {"error": "bad_request", "message": "Choose which count to confirm."}
    assert signal_row(run, answer["signal_id"]) == before
    selected = confirm_answer(client, answer, value_source="typed")
    assert selected.status_code == 200, selected.text
    with db.tx() as conn:
        fact = db.one(conn, "SELECT * FROM reported_facts WHERE run_id=:r AND fact_id=:f",
                      r=run["run_id"], f=selected.json()["fact_id"])
    assert fact["reading_id"] is None and all(adj["units"] == 6 for adj in fact["adjustments"])
    logged = next(row["payload"] for row in table(run["run_id"], "activity_log")
                  if row["type"] == "count_confirmed" and row["payload"]["signal_id"] == answer["signal_id"])
    assert logged["value_source"] == "typed" and logged["reading_id"] is None


def test_complementary_partial_counts_cannot_be_confirmed(client, monkeypatch, tmp_path):
    text = "There are five in the backroom; the shelf was not checked."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    lot = request["lot_ids"][0]
    typed = {lot: {"shelf_units": 10, "backroom_units": None}}
    saved = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text, counts=typed)
    assert saved.status_code == 200, saved.text
    answer = saved.json()
    install(monkeypatch, FakeModels(reply_counts(request, text, shelf=None, backroom=5)))
    read = answer_reading(client, answer)
    assert read.status_code == 200, read.text
    before = (signal_row(run, answer["signal_id"]), request_row(run, request), table(run["run_id"], "reported_facts"))
    for choices in ({}, {"value_source": "typed"},
                    {"value_source": "reading", "reading_id": read.json()["reading_id"]}):
        result = confirm_answer(client, answer, **choices)
        assert result.status_code == 409 and result.json()["error"] == "needs_input", result.text
        assert (signal_row(run, answer["signal_id"]), request_row(run, request),
                table(run["run_id"], "reported_facts")) == before
    complete = {lot: {"shelf_units": 10, "backroom_units": 5}}
    saved = http_answer(client, request, "checked.jpg", JPEG, "image/jpeg", text=text, counts=complete)
    assert saved.status_code == 200, saved.text
    selected = confirm_answer(client, saved.json(), value_source="typed")
    assert selected.status_code == 200, selected.text
    with db.tx() as conn:
        fact = db.one(conn, "SELECT * FROM reported_facts WHERE run_id=:r AND fact_id=:f",
                      r=run["run_id"], f=selected.json()["fact_id"])
    assert fact["reading_id"] is None and all(adj["units"] == 15 for adj in fact["adjustments"])


def test_printed_date_from_photo(client, monkeypatch, tmp_path):
    run, _ = identity(client)
    case, fact = expiry_fixture(run)
    request = ask(run, case["case_id"], fact["fact_id"])
    as_seat(client, request["to_branch"])
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    answer = answer_dates(run, request, {}, checked_lot=False, file=JPEG)
    lot = request["lot_ids"][0]
    printed = refdata.get(run["dataset_version"]).date(run["day"]).isoformat()
    install(monkeypatch, FakeModels(Reading(kind="EXPIRY", lot_id=lot, printed_date=printed,
        evidence=[Evidence(field=field, source="photo", excerpt=printed, confidence=.9)
                  for field in ("kind", "lot_id", "printed_date")])))
    read = answer_reading(client, answer)
    assert read.status_code == 200 and read.json()["printed_dates"] == {lot: printed}
    missing_tick = confirm_answer(client, answer, value_source="reading", reading_id=read.json()["reading_id"])
    assert missing_tick.status_code == 409 and missing_tick.json()["error"] == "scope_tick", missing_tick.text
    confirmed = confirm_answer(client, answer, value_source="reading", reading_id=read.json()["reading_id"], checked_date=True)
    assert confirmed.status_code == 200, confirmed.text
    with db.tx() as conn:
        stored = db.one(conn, "SELECT * FROM reported_facts WHERE run_id=:r AND fact_id=:f",
                        r=run["run_id"], f=confirmed.json()["fact_id"])
    assert stored["reading_id"] == read.json()["reading_id"]
    assert signal_row(run, answer["signal_id"])["fields"]["dates"] == {lot: None}


@pytest.mark.parametrize("finish_after", ["confirmation", "new_revision"])
def test_late_reading_is_history(client, monkeypatch, tmp_path, finish_after):
    text = "None on the shelf, none in the backroom either."
    run, request, answer = saved_answer(client, monkeypatch, tmp_path, text=text)
    typed = {lot: {"shelf_units": 4, "backroom_units": 2} for lot in request["lot_ids"]}
    answer = http_answer(client, request, "shelf.jpg", JPEG, "image/jpeg", text=text, counts=typed).json()
    entered, release = threading.Event(), threading.Event()

    class Waiting(FakeModels):
        async def generate_content(self, **kwargs):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(.01)
            return await super().generate_content(**kwargs)

    install(monkeypatch, Waiting(reply_counts(request, text)))
    other = TestClient(app)
    other.cookies.set(COOKIE, client.cookies.get(COOKIE))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(answer_reading, other, answer)
        try:
            assert entered.wait(15)
            if finish_after == "confirmation":
                changed = confirm_answer(client, answer, value_source="typed")
            else:
                changed = http_answer(client, request, "again.jpg", JPEG, "image/jpeg", text=text, counts=typed)
            assert changed.status_code == 200, changed.text
            before = (signal_row(run, answer["signal_id"]), request_row(run, request), table(run["run_id"], "reported_facts"))
        finally:
            release.set()
        result = pending.result(timeout=15)
    assert result.status_code == 200 and result.json()["after_confirmation"] is True, result.text
    assert result.json()["differs_from_confirmed"] is (finish_after == "confirmation")
    assert (signal_row(run, answer["signal_id"]), request_row(run, request), table(run["run_id"], "reported_facts")) == before
