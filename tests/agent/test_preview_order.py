"""Deterministic field-report previews share an urgency order and a complete version key.

The extra cases and stored selection deadlines are this test's own data. Real
engine inputs/results and Phase 7's what-if/confirmation path are left intact.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.exc import DBAPIError

# Initialize the reused API fixture's session credentials before loading API configuration.
from tests.api.conftest import as_seat, seat, take_and_send
from api import cases, db, evaluate, impact, inputs, reports, scope
from engine import from_dict
from tests.api import test_http, test_signals


def capacity_report(client, deadlines, *, free_totes=0):
    """Seven live cases in one capacity scope, with deliberately unsorted deadlines."""
    card = test_http.story(client)
    run = test_signals.session_run(client)
    base = test_signals.lot_row(run, test_signals.case_lot(run["run_id"], card["case_id"]))
    ids = [card["case_id"], *(test_signals.twin(run, base, n) for n in range(1, 7))]
    with db.tx() as conn:
        # Only these test cases stay live; unrelated template cases do not enter the scope.
        db.run(conn, "UPDATE cases SET state = 'CLOSED' WHERE run_id = :r AND case_id <> ALL(:ids)",
               r=run["run_id"], ids=ids)
        for i, (cid, deadline) in enumerate(zip(ids, deadlines, strict=True)):
            case = cases.case_row(conn, run["run_id"], cid)
            rec = cases.active_rec(conn, case)
            selected = {"rec_version": rec["rec_version"], "plan_key": rec["result"]["recommended"],
                        "fact_checks": rec["fact_checks"], "decide_by": None if deadline is None else
                        {"day": run["day"] + deadline[0], "latest_decision_min": deadline[1]}}
            db.run(conn, "UPDATE cases SET key = :k, selected = CAST(:s AS jsonb) "
                         "WHERE run_id = :r AND case_id = :c", k=f"P12-{7-i}", s=db.js(selected),
                   r=run["run_id"], c=cid)
    as_seat(client, card["dest"])
    today = test_signals.today_iso(client)
    report = test_signals.filed(client, "CAPACITY", {"zone": card["zone"], "free_totes": free_totes,
                                                   "valid_from": today, "valid_to": today})
    assert len(test_signals.live_in_scope(client, report["signal_id"])) == 7
    return run, ids, report


def preview(client, report):
    response = test_signals.preview(client, report["signal_id"], report["revision"])
    assert response.status_code == 200, response.json()
    return response.json()


def test_most_urgent_five(client, monkeypatch):
    run, ids, report = capacity_report(client, [(1, 30), (0, 600), (0, 60), (2, 0),
                                              (0, 180), (1, 10), (0, 300)])
    out = preview(client, report)
    assert [row["case_id"] for row in out["cases"]] == [ids[i] for i in (2, 4, 6, 1, 5)]
    assert (out["affected"], out["more"], out["more_text"]) == (7, 2, "Previewing 5 of 7 affected cases.")
    assert len(out["preview_key"]) == 64 and int(out["preview_key"], 16) >= 0
    with db.tx() as conn:
        stored = db.one(conn, "SELECT preview FROM signals WHERE run_id = :r AND signal_id = :s",
                        r=run["run_id"], s=report["signal_id"])["preview"]
    assert stored["preview_key"] == out["preview_key"]
    assert stored["affected"] == 7
    calls = []
    original = evaluate.compute
    original_store = evaluate.store

    def compute(*args, **kwargs):
        calls.append("compute")
        return original(*args, **kwargs)

    def store(*args, **kwargs):
        calls.append("store")
        return original_store(*args, **kwargs)

    monkeypatch.setattr(evaluate, "compute", compute)
    monkeypatch.setattr(evaluate, "store", store)
    assert preview(client, report) == out
    assert calls == []


def test_ties_and_missing_deadlines(client):
    _, ids, report = capacity_report(client, [None, (0, 60), (0, 60), None,
                                            (1, 30), None, (0, 180)])
    out = preview(client, report)
    # Equal deadlines use the displayed case key, even when its order differs from case_id.
    assert [row["case_id"] for row in out["cases"]] == [ids[i] for i in (2, 1, 6, 4, 5)]


def test_key_is_canonical_and_covers_each_input():
    pairs = [("case-b", None), ("case-a", "version-a")]
    first = impact.preview_key("signal-a", 1, pairs, "engine-a")
    assert len(first) == 64 and int(first, 16) >= 0
    assert impact.preview_key("signal-a", 1, list(reversed(pairs)), "engine-a") == first
    assert impact.preview_key("signal-b", 1, pairs, "engine-a") != first
    assert impact.preview_key("signal-a", 2, pairs, "engine-a") != first
    assert impact.preview_key("signal-a", 1, pairs + [("case-c", "version-c")], "engine-a") != first
    assert impact.preview_key("signal-a", 1, [("case-b", "version-b"), pairs[1]], "engine-a") != first
    assert impact.preview_key("signal-a", 1, pairs, "engine-b") != first


def test_key_covers_all_affected(client, monkeypatch):
    run, ids, report = capacity_report(client, [(0, 100 + 10 * i) for i in range(7)], free_totes=3)
    first = preview(client, report)
    assert [row["case_id"] for row in first["cases"]] == ids[:5]
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], ids[5])
        active = cases.active_rec(conn, case)
        inp = from_dict(active["engine_input"])
        version, _, _ = evaluate.store(conn, run["run_id"], ids[5], run["day"],
                                      replace(inp, decision_min=inp.decision_min + 1))
        assert version != active["rec_version"]
        # Keep this test's unchanged plan/deadline bound to the new version so
        # the sixth case remains unpreviewed; only its version changes the key.
        selected = {**case["selected"], "rec_version": version}
        db.run(conn, "UPDATE cases SET active_rec_version = :v, selected = CAST(:selected AS jsonb) "
                     "WHERE run_id = :r AND case_id = :c", v=version, selected=db.js(selected),
               r=run["run_id"], c=ids[5])
    calls = []
    original_compute, original_store = evaluate.compute, evaluate.store

    def compute(*args, **kwargs):
        calls.append("compute")
        return original_compute(*args, **kwargs)

    def store(*args, **kwargs):
        calls.append("store")
        return original_store(*args, **kwargs)

    monkeypatch.setattr(evaluate, "compute", compute)
    monkeypatch.setattr(evaluate, "store", store)
    second = preview(client, report)
    assert second["preview_key"] != first["preview_key"]
    assert second["cases"] == first["cases"]
    # Release C keeps the 07-20 read-back (api/impact.py `_kept`): the key change refreshes the stored preview, and
    # what-ifs already stored under their labels are read back with no engine run.
    assert calls == [] and stored_key(run, report) == second["preview_key"]
    calls.clear()
    monkeypatch.setattr(impact, "ENGINE_VERSION", impact.ENGINE_VERSION + "-p12-test")
    third = preview(client, report)
    assert third["preview_key"] != second["preview_key"]
    assert third["cases"] == second["cases"]
    assert calls == [] and stored_key(run, report) == third["preview_key"]


def stored_key(run, report):
    with db.tx() as conn:
        return db.one(conn, "SELECT preview->>'preview_key' AS k FROM signals WHERE run_id = :r AND signal_id = :s",
                      r=run["run_id"], s=report["signal_id"])["k"]


def test_key_read_takes_no_locks(client):
    run, ids, report = capacity_report(client, [(0, 100 + 10 * i) for i in range(7)])
    with db.tx() as conn:
        current = reports._current(conn, run["run_id"], report["signal_id"])
        impact._read(conn, run, current)
        blocked = None
        try:
            with db.tx() as other:
                db.one(other, "SELECT case_id FROM cases WHERE run_id = :r AND case_id = :c FOR UPDATE NOWAIT",
                       r=run["run_id"], c=ids[0])
        except DBAPIError as exc:
            blocked = str(exc)
        assert blocked is None, "A preview key read locked an affected case: " + str(blocked)
    with db.tx() as conn:
        scope.affected(conn, run, reports.scope_of(run, current))
        with pytest.raises(DBAPIError, match="55P03"):
            with db.tx() as other:
                db.one(other, "SELECT case_id FROM cases WHERE run_id = :r AND case_id = :c FOR UPDATE NOWAIT",
                       r=run["run_id"], c=ids[0])


def test_not_relevant_preview(monkeypatch):
    current = {"signal_id": "p12-not-relevant", "report_id": "p12-not-relevant", "revision": 1,
               "kind": "NOT_RELEVANT", "state": reports.OPEN, "branch": "P12-S", "reporter_branch": "P12-S",
               "case_id": None, "fields": {}, "preview": None}
    monkeypatch.setattr(reports, "_current", lambda *args, **kwargs: current)
    stored = []
    monkeypatch.setattr(db, "run", lambda *args, **kwargs: stored.append(kwargs))

    def unexpected(*args, **kwargs):
        pytest.fail("Not relevant must not construct an engine input or run the engine")

    monkeypatch.setattr(inputs, "sandbox_policy", unexpected)
    monkeypatch.setattr(evaluate, "compute", unexpected)
    monkeypatch.setattr(evaluate, "store", unexpected)
    out = impact.preview(None, {"run_id": "p12"}, seat("P12-S"), current["signal_id"], 1)
    assert out["cases"] == []
    assert out["message"] == "Not relevant · no impact preview"
    assert (out["affected"], out["more"], out["more_text"]) == (0, 0, None)
    assert stored


def test_preview_blocks_nothing_still(client):
    card = test_http.story(client)
    sent = take_and_send(client, card["case_id"], card["rec_version"])
    assert sent.status_code == 200, sent.json()
    run = test_signals.session_run(client)
    as_seat(client, card["dest"])
    today = test_signals.today_iso(client)
    report = test_signals.filed(client, "CAPACITY", {"zone": card["zone"], "free_totes": 0,
                                                   "valid_from": today, "valid_to": today})
    before = test_signals.active_rows(run["run_id"])
    assert before["reservations"]
    first = preview(client, report)
    assert first["cases"]
    assert preview(client, report) == first
    assert test_signals.active_rows(run["run_id"]) == before
