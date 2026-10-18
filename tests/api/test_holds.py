"""Explicit lot holds (07-CONTEXT D-11, D-13; 04-API-CONTRACT "Hold"; acceptance check 10, the hold half).

A hold is placed by a person (the lot's branch manager: "Hold this lot for review", or "Mixed dates in this lot" on an
expiry answer) and lifted only by the lot's branch manager (release, with a note) or by a printed date confirmed for
that lot after the hold was placed. No adoption, plan choice or automatic step clears one. Cases are found by tag and
facts by kind; nothing sleeps.
"""
import json
from datetime import timedelta

from api import cases, db, lotholds, realtime, refdata, signals, workflow
from tests.api.conftest import as_seat, fresh, key, seat
from tests.api.test_evidence import (active, ask, lot_row, other_sandbox, recount, refused, request_row,
                                     source, story, table)
from tests.api.test_requests import activity, answer_dates, expiry_fixture, in_both
from tests.api.test_workflow import call, version


def place(run, actor, lot_id, reason="Crushed crates in the chiller", kind="CONDITION") -> dict:
    return call(run, lotholds.place, actor, lot_id, kind, reason)


def release(run, actor, hold_id, note) -> dict:
    return call(run, lotholds.release, actor, hold_id, note)


def holds(run) -> list[dict]:
    return table(run["run_id"], "lot_holds")


def detail(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.detail(conn, fresh(run["run_id"]), case_id)


def tasks(run, actor) -> dict:
    with db.tx() as conn:
        return signals.tasks_for(conn, fresh(run["run_id"]), actor)


def mixed(run) -> tuple[dict, dict, dict, dict]:
    """(case, the expiry request, the MIXED answer, the hold it placed): the story lot's expiry check answered
    'Mixed dates in this lot' by the addressed manager."""
    case, fact = expiry_fixture(run)
    q = ask(run, case["case_id"], fact["fact_id"])
    a = answer_dates(run, q, {l: signals.MIXED for l in q["lot_ids"]})
    [hold] = holds(run)
    return case, q, a, hold


def other_branch(run, *not_these) -> str:
    return next(b for b in refdata.get(run["dataset_version"]).branches if b not in not_these)


def test_mixed_dates_places_a_hold(run):
    """'Mixed dates in this lot' places one ACTIVE MIXED_DATES hold 'Separate stock and verify before moving', placed
    by the answering seat at now(); the part is HELD (askable again) and the fact stays unverified (no fact, the
    record untouched); the answer and the case detail show the hold; no text says the app splits the lot."""
    case, fact = expiry_fixture(run)
    cid = case["case_id"]
    q = ask(run, cid, fact["fact_id"])
    lot_id = fact["lot_id"]
    before = lot_row(run, lot_id)
    a = answer_dates(run, q, {lot_id: signals.MIXED})
    [row] = holds(run)
    assert (row["lot_id"], row["branch"], row["kind"], row["reason"], row["state"]) == (
        lot_id, q["to_branch"], "MIXED_DATES", lotholds.MIXED_TEXT, "ACTIVE")
    assert lotholds.MIXED_TEXT == "Separate stock and verify before moving"
    assert (row["placed_by"], row["request_id"], row["signal_id"]) == (q["to_branch"], q["request_id"],
                                                                       a["signal_id"])
    req = request_row(run, q)
    assert row["placed_at"] >= req["created_at"] and req["state"] == "HELD"
    assert (req["closed_at"], req["closed_by"]) == (row["placed_at"], f"manager:{q['to_branch']}")
    assert [(h["hold_id"], h["state"], h["kind"]) for h in a["holds"]] == [(row["hold_id"], "ACTIVE", "MIXED_DATES")]
    seen = in_both(run, q)
    assert (seen["state"], seen["status_kind"], seen["askable_again"], seen["confirmable"]) == (
        "HELD", "HELD", True, False)
    assert table(run["run_id"], "reported_facts") == [] and lot_row(run, lot_id) == before
    d = detail(run, cid)
    [shown] = d["holds"]
    assert (shown["hold_id"], shown["owner_branch"], shown["state"], shown["reason"]) == (
        row["hold_id"], q["to_branch"], "ACTIVE", lotholds.MIXED_TEXT)
    assert shown["placed_at"] == realtime.iso(row["placed_at"]) and cid in [c["case_id"] for c in shown["cases"]]
    assert shown["placed_label"] == f"Placed by {q['to_branch']} manager · {realtime.label(row['placed_at'])}"
    [entry] = activity(run["run_id"], "hold_placed")
    assert (entry["payload"]["hold_id"], entry["payload"]["kind"]) == (row["hold_id"], "MIXED_DATES")
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", None, "now", None,
                {lot_id: signals.MIXED}, True)
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is HELD")  # a held part takes no answer
    text = json.dumps([a, d["holds"], [x["payload"] for x in activity(run["run_id"], "hold_placed")]]).lower()
    assert "split" not in text


def test_only_the_lots_manager_releases(run):
    """Another branch's manager or HQ releasing is 403; the lot's manager without a note is 400; with a note the hold
    is RELEASED with resolved_by, resolved_at, resolution 'released' and the note, logged hold_released."""
    case, q, a, hold = mixed(run)
    owner = seat(hold["branch"])
    for other in (seat(other_branch(run, hold["branch"])), seat(None)):
        e = refused(run, lotholds.release, other, hold["hold_id"], "Inspected")
        assert (e.status, e.code) == (403, "forbidden")
    for blank in ("", "   ", None):
        e = refused(run, lotholds.release, owner, hold["hold_id"], blank)
        assert (e.status, e.code) == (400, "bad_request")
    assert holds(run)[0]["state"] == "ACTIVE"
    out = release(run, owner, hold["hold_id"], "Separated the older crates and checked the rest")
    [row] = holds(run)
    assert (row["state"], row["resolved_by"], row["resolution"], row["note"]) == (
        "RELEASED", hold["branch"], "released", "Separated the older crates and checked the rest")
    assert row["resolved_at"] >= row["placed_at"] and out["state"] == "RELEASED"
    assert out["resolved_label"] == f"Hold released by {hold['branch']} manager · {realtime.label(row['resolved_at'])}"
    [entry] = activity(run["run_id"], "hold_released")
    assert (entry["payload"]["hold_id"], entry["payload"]["note"]) == (hold["hold_id"], row["note"])
    e = refused(run, lotholds.release, owner, hold["hold_id"], "again")
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Hold is RELEASED")
    e = refused(run, lotholds.release, owner, "HOLD-nowhere", "note")
    assert (e.status, e.code) == (404, "not_found")
    assert detail(run, case["case_id"])["holds"] == []  # only active holds show on the case


def test_a_confirmed_printed_date_resolves_the_mixed_hold(run):
    """A printed date checked after the hold was placed, confirmed for that lot, resolves the MIXED_DATES hold
    (RESOLVED, logged hold_resolved); one checked before placed_at confirms its fact and leaves the hold ACTIVE."""
    case, q, a, hold = mixed(run)
    lot_id = hold["lot_id"]
    again = recount(run, case["case_id"], q["request_id"], q["fact_id"])  # the HELD part is askable again
    assert (again["part"], request_row(run, q)["state"]) == ("expiry", "SUPERSEDED")
    printed = cases.iso_of(fresh(run["run_id"]), lot_row(run, lot_id)["expiry_day"])
    earlier = (hold["placed_at"] - timedelta(minutes=5)).isoformat()
    s = answer_dates(run, again, {lot_id: printed}, observed=earlier)
    out = call(run, signals.confirm, seat(again["to_branch"]), s["signal_id"], s["revision"], None)
    assert out["fact_id"] and holds(run)[0]["state"] == "ACTIVE"  # checked before the hold: not after separation
    assert activity(run["run_id"], "hold_resolved") == []
    other = other_sandbox()
    c2, q2, _, hold2 = mixed(other)
    again2 = recount(other, c2["case_id"], q2["request_id"], q2["fact_id"])
    printed2 = cases.iso_of(fresh(other["run_id"]), lot_row(other, hold2["lot_id"])["expiry_day"])
    s2 = answer_dates(other, again2, {hold2["lot_id"]: printed2})  # "Checked just now": after the hold
    out2 = call(other, signals.confirm, seat(again2["to_branch"]), s2["signal_id"], s2["revision"], None)
    [row] = holds(other)
    assert (row["state"], row["resolved_by"], row["resolution"]) == ("RESOLVED", hold2["branch"],
                                                                    "printed date confirmed")
    [entry] = activity(other["run_id"], "hold_resolved")
    assert (entry["payload"]["hold_id"], entry["payload"]["fact_id"]) == (row["hold_id"], out2["fact_id"])


def test_adoption_never_clears_a_hold(run):
    """Adopting an assumption on a case involving the held lot re-plans the case and leaves the hold ACTIVE and
    unchanged (D-12, D-13; T-07-26)."""
    case = story(run)
    cid = case["case_id"]
    lot = lot_row(run, case["lot_id"])
    place(run, seat(lot["branch"]), case["lot_id"])
    before = holds(run)
    plan = cases.parse_plan(active(run, cid)["result"]["recommended"])
    trip = next(t for t in active(run, cid)["engine_input"]["runs"]
                if t["run_code"] == plan.run_code and t["dest"] == plan.dest)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    v0 = version(run, cid)
    call(run, workflow.adopt, source(run, cid), cid, quote, "carrier quote by phone")
    assert version(run, cid) != v0
    assert holds(run) == before and before[0]["state"] == "ACTIVE"
    assert [h["hold_id"] for h in detail(run, cid)["holds"]] == [before[0]["hold_id"]]


def test_the_lots_manager_places_a_condition_hold(run):
    """POST /api/lots/{lot}/holds by the lot's manager: an ACTIVE CONDITION hold; a second call returns the same
    hold; another seat is 403; a blank reason 400; a lot not on the record 404. Tasks lists the hold for the lot's
    branch with counts.lot_holds 1, and the session's tasks_waiting counts it."""
    case = story(run)
    lot = lot_row(run, case["lot_id"])
    owner = seat(lot["branch"])
    for other in (seat(other_branch(run, lot["branch"])), seat(None)):
        e = refused(run, lotholds.place, other, lot["lot_id"], "CONDITION", "Crushed crates")
        assert (e.status, e.code) == (403, "forbidden")
    e = refused(run, lotholds.place, owner, lot["lot_id"], "CONDITION", "  ")
    assert (e.status, e.code) == (400, "bad_request")
    e = refused(run, lotholds.place, owner, "TEST-LOT-NOWHERE", "CONDITION", "Crushed crates")
    assert (e.status, e.code) == (404, "not_found")
    assert holds(run) == []
    first = place(run, owner, lot["lot_id"])
    again = place(run, owner, lot["lot_id"], reason="Another look")
    assert (first["kind"], first["state"], first["reason"]) == ("CONDITION", "ACTIVE", "Crushed crates in the chiller")
    assert again["hold_id"] == first["hold_id"] and len(holds(run)) == 1
    got = tasks(run, owner)
    assert [h["hold_id"] for h in got["holds"]] == [first["hold_id"]] and got["counts"]["lot_holds"] == 1
    with db.tx() as conn:
        assert signals.waiting(conn, fresh(run["run_id"]), owner) == sum(got["counts"].values())
    elsewhere = tasks(run, seat(other_branch(run, lot["branch"])))
    assert (elsewhere["holds"], elsewhere["counts"]["lot_holds"]) == ([], 0)
    assert tasks(run, seat(None))["counts"]["lot_holds"] == 0
    assert [h["hold_id"] for h in detail(run, case["case_id"])["holds"]] == [first["hold_id"]]


def test_holds_over_http(client):
    """The two hold routes: the lot's manager places a CONDITION hold and releases it with a note; another seat is
    403; both need an Idempotency-Key."""
    run = fresh(client.get("/api/session").json()["run_id"])
    case = story(run)
    lot = lot_row(run, case["lot_id"])
    as_seat(client, other_branch(run, lot["branch"]))
    r = client.post(f"/api/lots/{lot['lot_id']}/holds", json={"reason": "Crushed crates"}, headers=key())
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(client, lot["branch"])
    r = client.post(f"/api/lots/{lot['lot_id']}/holds", json={"reason": "Crushed crates"})
    assert (r.status_code, r.json()["error"]) == (400, "idempotency_key")
    r = client.post(f"/api/lots/{lot['lot_id']}/holds", json={"reason": "Crushed crates"}, headers=key())
    assert r.status_code == 200, r.json()
    hold = r.json()
    assert (hold["kind"], hold["state"], hold["owner_branch"]) == ("CONDITION", "ACTIVE", lot["branch"])
    tiles = client.get("/api/tasks").json()
    assert tiles["counts"]["lot_holds"] == 1 and tiles["holds"][0]["hold_id"] == hold["hold_id"]
    r = client.post(f"/api/holds/{hold['hold_id']}/release", json={"note": ""}, headers=key())
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")
    r = client.post(f"/api/holds/{hold['hold_id']}/release", json={"note": "Inspected, all sound"}, headers=key())
    assert r.status_code == 200, r.json()
    assert (r.json()["state"], r.json()["note"]) == ("RELEASED", "Inspected, all sound")
    assert client.get("/api/tasks").json()["counts"]["lot_holds"] == 0
