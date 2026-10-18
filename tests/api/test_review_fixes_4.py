"""The fourth outside-review fix pass on Phase 7's server: the impact preview (07-CONTEXT D-16, D-37; the Codex review
of 07-15, 0d545f5..5feb57a).

The preview must agree with confirmation: a confirmed report gives each case the version its preview showed.

- P1: a correction's preview leaves out the fact it replaces, as confirmation does (`replaced_by`).
- P2: the preview reads today's data (current bookings), as confirmation's re-plan does (`cases.current_input`).
- P3: a correction's preview lists the cases its earlier fact reached too, as confirmation re-plans both scopes.
- P4: a stored preview is served only while the cases it shows are as it read them; otherwise it is computed again.
  A repeat with nothing changed still runs nothing.
- The invariant: a report confirmed after bookings changed gives every previewed case the version its preview
  showed: a field report, a correction and a count (whose record correction the preview reads too), with a pending
  approval in scope whose cancellation releases holds another case in scope reads.

Lots are found by tag or created here by the test, never by a pasted id; nothing sleeps.
"""
from __future__ import annotations

import json

import pytest

from api import cases, db, evaluate, refdata
from tests.api import test_http
from tests.api.conftest import as_seat, take_and_send
from tests.api.test_review_fixes_2 import hold_booking, strawberry_case
from tests.api.test_signals import (active_rows, case_lot, confirm_count_report, confirm_report, confirmed, filed,
                                    in_case_order, input_of, live_in_scope, patch_report, preview, session_run,
                                    whatif_row)
from tests.api.test_workflow import call


def multipliers(engine_input: dict) -> list[float]:
    """The demand multipliers among an input's reported facts, in order."""
    return [a["value"] for a in engine_input["reported_facts"] if a["kind"] == "demand_multiplier"]


def row_of(body: dict, case_id: str) -> dict:
    return next(c for c in body["cases"] if c["case_id"] == case_id)


def demand_event(client, card: dict, multiplier: float, sku: str | None = None, **extra) -> dict:
    """A DEMAND_EVENT for today at the story case's destination, filed by its manager (the seat is left there)."""
    run = session_run(client)
    today = cases.iso_of(run, run["day"])
    as_seat(client, card["dest"])
    return filed(client, "DEMAND_EVENT", {"sku": sku or card["sku"], "multiplier": multiplier, "valid_from": today,
                                          "valid_to": today}, **extra)


# --- P1: a correction's preview leaves out the fact it replaces ------------------------------------------------------

def test_p1_a_corrections_preview_leaves_out_the_fact_it_replaces(client):
    """07-15 review P1 (D-15, D-16): the story case's destination confirms a demand event of 2.0 for milk today, then
    files a correction of 1.5. The correction's what-if holds the 1.5 alone, as the confirmed input will (the earlier
    fact is replaced, never stacked: 2.0 x 1.5 would preview three times the demand), and confirming it gives the
    story case the very version the preview ran."""
    card = test_http.story(client)
    run_id, cid = session_run(client)["run_id"], card["case_id"]
    first = demand_event(client, card, 2.0)
    confirmed(client, first["signal_id"])
    assert multipliers(input_of(run_id, cid)) == [2.0]
    fix = demand_event(client, card, 1.5, corrects=first["signal_id"])
    row = row_of(preview(client, fix["signal_id"]).json(), cid)
    assert multipliers(whatif_row(run_id, cid, row["whatif_version"])["engine_input"]) == [1.5]
    confirmed(client, fix["signal_id"])
    assert multipliers(input_of(run_id, cid)) == [1.5]
    assert test_http.story(client)["rec_version"] == row["whatif_version"]


# --- P2: the preview reads today's bookings --------------------------------------------------------------------------

def test_p2_the_preview_reads_todays_bookings(client):
    """07-15 review P2 (D-16; spec §8 Recheck contract): after the story case's version was stored, another product's
    case holds every chilled tote at the story's destination for the next days. A demand event for milk there then
    previews on today's bookings, as confirmation re-plans: the transfer in force is no longer allowed (on the stored
    input it still read allowed), and confirming gives the story case the very version the preview ran."""
    card = test_http.story(client)
    run = session_run(client)
    straw = strawberry_case(run)  # another chilled product: outside a milk event's scope
    for day in range(run["day"], run["day"] + 4):
        hold_booking(run["run_id"], straw["case_id"], "STORAGE", f"{card['dest']}|{card['zone']}", day, 1000.0)
    rep = demand_event(client, card, 1.5)
    row = row_of(preview(client, rep["signal_id"]).json(), card["case_id"])
    assert (row["recommended_before"], row["allowed"]) == (card["suggested_key"], False), row
    confirmed(client, rep["signal_id"])
    assert test_http.story(client)["rec_version"] == row["whatif_version"]


# --- P3: a correction's preview lists the cases its earlier fact reached ---------------------------------------------

def test_p3_a_corrections_preview_lists_the_cases_its_earlier_fact_reached(client):
    """07-15 review P3 (D-15, D-17): a confirmed demand event for milk at the story's destination is corrected to
    another product. Confirmation re-plans the cases in either fact's scope, so the preview lists both scopes' live
    cases in case_id order: the story case is among them, its what-if without the earlier multiplier, and confirming
    gives it the very version the preview ran."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, cid = run["run_id"], card["case_id"]
    other = next(s for s in refdata.get(run["dataset_version"]).products if s != card["sku"])
    first = demand_event(client, card, 2.0)
    confirmed(client, first["signal_id"])
    fix = demand_event(client, card, 1.5, sku=other, corrects=first["signal_id"])
    expected = in_case_order(set(live_in_scope(client, first["signal_id"])) | set(live_in_scope(client,
                                                                                               fix["signal_id"])))
    assert cid in expected
    out = preview(client, fix["signal_id"]).json()
    assert [c["case_id"] for c in out["cases"]] == expected[:5], out
    row = row_of(out, cid)
    assert multipliers(whatif_row(run_id, cid, row["whatif_version"])["engine_input"]) == []
    confirmed(client, fix["signal_id"])
    assert test_http.story(client)["rec_version"] == row["whatif_version"]


# --- P4: a stored preview is served only while its cases are as it read them -----------------------------------------

def fill_storage(run: dict, card: dict, case_id: str) -> None:
    """Another case (`case_id`) holds every chilled tote at the story's destination for the next days (test values)."""
    for day in range(run["day"], run["day"] + 4):
        hold_booking(run["run_id"], case_id, "STORAGE", f"{card['dest']}|{card['zone']}", day, 1000.0)


def test_p4_a_stored_preview_is_recomputed_when_its_cases_changed(client, monkeypatch):
    """07-15 review P4 (D-16; T-07-56): a demand event for milk at the story's destination is previewed and stored.
    Another product's case then takes every chilled tote there: the next preview of the same revision is computed
    again on today's bookings (the transfer no longer allowed), not the stored one. The story case is then re-planned
    on today's data (a new active version, as the morning or an adopted assumption gives it): the next preview shows
    that version as the plan now. A repeat with nothing changed returns the stored body and runs nothing
    (evaluate.compute and evaluate.store spied)."""
    card = test_http.story(client)
    run = session_run(client)
    cid = card["case_id"]
    rep = demand_event(client, card, 1.5)
    first = preview(client, rep["signal_id"]).json()
    fill_storage(run, card, strawberry_case(run)["case_id"])
    second = preview(client, rep["signal_id"]).json()
    before, after = row_of(first, cid), row_of(second, cid)
    assert (before["allowed"], after["allowed"]) == (True, False), (before, after)
    assert after["whatif_version"] != before["whatif_version"]
    call(run, lambda conn, r: cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], cid)))
    now = test_http.story(client)["rec_version"]
    assert now != card["rec_version"]
    third = preview(client, rep["signal_id"]).json()
    assert row_of(third, cid)["rec_version"] == now
    calls, stored = [], []
    real, real_store = evaluate.compute, evaluate.store
    monkeypatch.setattr(evaluate, "compute", lambda inp: calls.append(inp) or real(inp))
    monkeypatch.setattr(evaluate, "store", lambda *a, **k: stored.append(a) or real_store(*a, **k))
    assert preview(client, rep["signal_id"]).json() == third and (calls, stored) == ([], [])
    assert client.get(f"/api/signals/{rep['signal_id']}").json()["preview"] == third


# --- the invariant: the confirmed result is the version the preview showed -------------------------------------------

def untouched(run_id: str) -> dict:
    """Everything a preview must leave as it was: the active rows (cases, bookings, decisions, holds, facts, flags,
    requests, real versions), the record, deliveries, activity and the reports, their stored preview aside."""
    out = active_rows(run_id)
    with db.tx() as conn:
        for table in ("lots", "inbound", "activity_log"):
            rows = db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r", r=run_id)
            out[table] = sorted(rows, key=lambda x: json.dumps(x, sort_keys=True, default=str))
        out["signals"] = [{k: v for k, v in r.items() if k != "preview"} for r in db.rows(
            conn, "SELECT * FROM signals WHERE run_id = :r ORDER BY signal_id", r=run_id)]
    return out


def read_only_preview(client, run_id: str, signal_id: str) -> dict:
    before = untouched(run_id)
    r = preview(client, signal_id)
    assert r.status_code == 200, r.json()
    assert untouched(run_id) == before
    return r.json()


def active_version(run_id: str, case_id: str) -> str:
    with db.tx() as conn:
        return cases.case_row(conn, run_id, case_id)["active_rec_version"]


def send_story(client) -> dict:
    card = test_http.story(client)
    as_seat(client, card["source"])
    r = take_and_send(client, card["case_id"], card["rec_version"])
    assert (r.status_code, r.json()["state"]) == (200, "PENDING_APPROVAL"), r.json()
    return test_http.story(client)


@pytest.mark.parametrize("kind", ["report", "correction", "count"])
def test_the_confirmed_result_is_the_version_its_preview_showed(client, kind):
    """The invariant (07-15 review; D-16, D-17): the story case is pending approval (its holds booked) and a strawberry
    case at its source, whose runs reach the same destinations, is open. A report is filed and previewed:
    - report: "no free chilled totes today" at the story's destination;
    - correction: the same, correcting a confirmed report of 21 free totes (filed before the story was sent);
    - count: the story lot counted by its manager (the record correction is read, never written).
    Another case then books chilled storage at that destination, and the report is previewed again: the story's
    what-if moved with the bookings. Each preview writes nothing outside its stored body. Confirming then gives every
    previewed case exactly the version its preview showed, the strawberry case included: it is re-planned after the
    story case, whose cancelled approval released the holds its input read."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, cid, today = run["run_id"], card["case_id"], cases.iso_of(run, run["day"])
    straw = strawberry_case(run)
    capacity = {"zone": card["zone"], "valid_from": today, "valid_to": today}
    if kind == "correction":
        as_seat(client, card["dest"])
        earlier = filed(client, "CAPACITY", {**capacity, "free_totes": 21})
        confirmed(client, earlier["signal_id"])
    sent = send_story(client)
    if kind == "count":
        as_seat(client, card["source"])
        rep = filed(client, "STOCK_COUNT", {"lot_id": case_lot(run_id, cid), "shelf_units": 60, "backroom_units": 0},
                    observed="now")
    else:
        as_seat(client, card["dest"])
        extra = {"corrects": earlier["signal_id"]} if kind == "correction" else {}
        rep = filed(client, "CAPACITY", {**capacity, "free_totes": 0}, **extra)
    first = read_only_preview(client, run_id, rep["signal_id"])
    hold_booking(run_id, straw["case_id"], "STORAGE", f"{card['dest']}|{card['zone']}", run["day"] + 1, 2.0)
    second = read_only_preview(client, run_id, rep["signal_id"])
    assert row_of(second, cid)["whatif_version"] != row_of(first, cid)["whatif_version"]
    shown = [c["case_id"] for c in second["cases"]]
    assert cid in shown and (kind == "count" or straw["case_id"] in shown), second
    assert row_of(second, cid)["rec_version"] == sent["rec_version"]
    assert all(c["error"] is None for c in second["cases"]), second
    r = confirm_count_report(client, rep["signal_id"]) if kind == "count" else confirm_report(client, rep["signal_id"])
    assert r.status_code == 200, r.json()
    assert test_http.story(client)["state"] == "OPEN"  # the pending approval was cancelled, its holds released
    assert {c["case_id"]: active_version(run_id, c["case_id"]) for c in second["cases"]} == {
        c["case_id"]: c["whatif_version"] for c in second["cases"]}


def test_the_preview_fits_the_case_budget(client):
    """T-07-56 (the spec's 3 s case budget, never raised): with the story case pending approval and the strawberry case
    in scope, a preview computed afresh (each run a new revision with its own value, so new what-ifs and a cold engine
    cache, with the engine worker as the suite runs it) and its repeat (what it read checked again, no engine) each
    take a median under api.bench.BUDGET_S over 3 runs. The times are printed for the record."""
    import statistics
    import time
    from api import bench
    card = test_http.story(client)
    run = session_run(client)
    today = cases.iso_of(run, run["day"])
    strawberry_case(run)
    send_story(client)
    as_seat(client, card["dest"])
    totes = 13  # values no other test reports: the engine cache is shared across workspaces, so each run is cold
    rep = filed(client, "CAPACITY", {"zone": card["zone"], "free_totes": totes, "valid_from": today, "valid_to": today})
    computed, repeated = [], []
    for n in range(3):
        if n:
            r = patch_report(client, rep["signal_id"], {"revision": n, "fields": {"free_totes": totes + n}})
            assert r.status_code == 200, r.json()
        for times in (computed, repeated):
            start = time.perf_counter()
            r = preview(client, rep["signal_id"], revision=n + 1)
            times.append(time.perf_counter() - start)
            assert r.status_code == 200 and len(r.json()["cases"]) == 2, r.json()
    print(f"\npreview: computed median {statistics.median(computed):.3f} s of {[round(t, 3) for t in computed]}, "
          f"repeat median {statistics.median(repeated):.3f} s of {[round(t, 3) for t in repeated]} "
          f"(budget {bench.BUDGET_S:g} s)")
    assert statistics.median(computed) < bench.BUDGET_S and statistics.median(repeated) < bench.BUDGET_S
