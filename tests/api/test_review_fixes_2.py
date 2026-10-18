"""The second outside-review fix pass on Phase 7's server (07-CONTEXT D-37, D-38; the 07-12 and 07-13 reviews).

- R1-R3: bugs in 07-12's reassessment and closing code (D-24, D-28 as built).
- W1-W3: the user's D-38 rules for when a closed lot reopens by itself.
- T1-T3: bugs in 07-13's field reports.

Lots are found by tag or created here by the test, never by a pasted id; nothing sleeps. The synthetic tests come first:
the public tests at the end use the module-scoped `public_template`, which stays ACTIVE for the rest of the module.
"""
from __future__ import annotations

from api import cases, db, workflow
from tests.api.conftest import fresh, own_series, seat
from tests.api.test_reassess import (NEEDS, confirm_count, inputs_of, lot_status, morning_screen, opened_entry,
                                     qualifies, reject_story, successor_of)
from tests.api.test_selection import case_now, detail_of
from tests.api.test_workflow import call


def closing():
    from api import closing as module
    return module


def set_qty(run, row: dict, qty: float) -> None:
    """The case lot's record and physical stock set to `qty` (the test writes its own copy of the series)."""
    own_series(run, row["source_branch"], row["sku"])
    with db.tx() as conn:
        for table in ("lots", "truth_lots"):
            db.run(conn, f"UPDATE {table} SET qty = :q WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   q=qty, r=run["run_id"], l=row["lot_id"])


# --- R1: the morning screen refreshes a closed lot's status (D-24) ---------------------------------------------------

def test_r1_the_morning_screen_clears_a_closed_lot_that_no_longer_qualifies(run):
    """07-12 review R1: the story case rejected while at risk reads "Needs decision" and offers Reassess; its lot then
    keeps one unit on the record (still stock, still inside its screen window, no longer qualifying). The morning
    screen judges it, opens nothing and stores lot_status null, as `reassess_lots` does: the card reads no lot status
    and Reassess is no longer offered."""
    row = reject_story(run)
    cid = row["case_id"]
    assert lot_status(run, cid) == NEEDS and detail_of(run, cid)["commitment"]["next_actions"] == ["reassess"]
    set_qty(run, row, 1.0)
    assert not qualifies(run, row["lot_id"])
    assert morning_screen(run) == [] and successor_of(run, cid) is None
    assert case_now(run, cid)["lot_status"] is None
    assert lot_status(run, cid) is None
    assert detail_of(run, cid)["commitment"]["next_actions"] == []


# --- R2: a successor's `opened` entry carries the screen's figures (05.2 D-26's risk filter) --------------------------

SCREEN_KEYS = ("a", "b", "usable_runs", "step_day")  # what an ordinary opening logs from the screen


def screen_now(run, lot_id) -> dict:
    """The ESL-02 screen of the lot on the workspace's day, as the opening path runs it."""
    from api import config, screen
    with db.tx() as conn:
        return screen.screen(inputs_of(conn, run, lot_id), config.CASE_OPENING)


def figures(entry: dict) -> dict:
    return {k: entry.get(k) for k in SCREEN_KEYS}


def test_r2_a_successor_logs_the_screen_figures(run):
    """07-12 review R2: a successor opened by Reassess and one opened by a confirmed count (trigger "fact") each log
    `opened` with the screen's a, b, usable_runs and step_day, as an ordinary opening does (cases.open_cases), so the
    board's `risk` filter reads them."""
    row = reject_story(run)
    found = screen_now(run, row["lot_id"])
    out = call(run, closing().reassess, seat(row["source_branch"]), row["case_id"])
    assert figures(opened_entry(run, out["case_id"])) == {k: found[k] for k in SCREEN_KEYS}
    assert call(run, workflow.reject, seat(row["source_branch"]), out["case_id"], "Not this week") == \
        {"state": "REJECTED"}
    confirm_count(run, row["lot_id"])
    succ = successor_of(run, out["case_id"])
    assert succ is not None and opened_entry(run, succ["case_id"])["trigger"] == "fact"
    found = screen_now(run, row["lot_id"])
    assert figures(opened_entry(run, succ["case_id"])) == {k: found[k] for k in SCREEN_KEYS}


# --- R3: bookings count by their full state (D-38.1: an approval or an executed transfer is no release) -------------

def hold_booking(run_id: str, case_id: str, kind: str, bucket: str, day: int | None, qty: float,
                 state: str = "HELD") -> None:
    """Another case's booking in round 1, written directly (test values): HELD for an hour of real time, as
    bookings.hold places it, or COMMITTED."""
    import uuid
    with db.tx() as conn:
        db.run(conn, "INSERT INTO reservations (run_id, res_id, case_id, round, kind, bucket, day, qty, state, "
                     "expires_at) VALUES (:r, :id, :c, 1, :k, :b, :d, :q, :s, CASE WHEN :s = 'HELD' THEN now() + "
                     "interval '60 minutes' END)", r=run_id, id=uuid.uuid4().hex, c=case_id, k=kind, b=bucket, d=day,
               q=float(qty), s=state)


def story_places(run) -> tuple[str, str]:
    """(a destination of the story case's version, the bucket of a run leaving its source today): places a
    transfer of another case would book that touch the story lot."""
    from api import refdata
    from tests.api.verify_helpers import story
    case = story(run)
    with db.tx() as conn:
        dest = next(b for b in closing().branches(conn, case) if b != case["source_branch"])
    r = fresh(run["run_id"])
    run_code = refdata.get(r["dataset_version"]).runs_from(case["source_branch"], r["day"])[0].run_code
    return dest, f"{run_code}|{r['day']}"


def stored_and_now(run, case_id) -> tuple[str, str]:
    from tests.api.test_reassess import recomputed
    return case_now(run, case_id)["fingerprint"], recomputed(run, case_id)


def test_r3_an_approval_or_an_executed_transfer_is_no_change_a_release_or_expiry_is(run):
    """07-12 review R3 (D-24, D-38.1): another case's transfer holds demand and storage at a destination of the story
    lot and truck space on a run leaving its source when the story case is rejected. Its approval (bookings.commit:
    DEMAND CONVERTED, the rest COMMITTED), its execution (TRUCK and SOURCE_LOT DONE, as a dispatch records them) and
    its end (bookings.done) leave the story lot's fingerprint as stored, and reassessing opens nothing. A hold that
    expires (before and after expire_holds marks it) and a booking released (bookings.release) each change it, and the
    release reopens the lot once with the reason "inputs"."""
    from api import bookings
    from tests.api.test_reassess import reassess_lots
    dest, truck = story_places(run)
    day = fresh(run["run_id"])["day"]
    hold_booking(run["run_id"], "TEST-A", "DEMAND", f"{dest}|MILK-1L", day + 1, 12.0)
    hold_booking(run["run_id"], "TEST-A", "STORAGE", f"{dest}|chilled", day + 1, 2.0)
    hold_booking(run["run_id"], "TEST-A", "TRUCK", truck, day, 2.0)
    hold_booking(run["run_id"], "TEST-B", "TRUCK", truck, day, 1.0)
    hold_booking(run["run_id"], "TEST-C", "STORAGE", f"{dest}|chilled", day + 1, 1.0, state="COMMITTED")
    row = reject_story(run)
    cid = row["case_id"]
    with db.tx() as conn:  # the approval, as bookings.commit turns a round's holds into bookings
        bookings.commit(conn, run["run_id"], "TEST-A", 1, cases.parse_plan("keep"), None)
        assert {r["kind"]: r["state"] for r in db.rows(conn, "SELECT kind, state FROM reservations WHERE run_id = :r "
                                                             "AND case_id = 'TEST-A'", r=run["run_id"])} == \
            {"DEMAND": "CONVERTED", "STORAGE": "COMMITTED", "TRUCK": "COMMITTED"}
    stored, now = stored_and_now(run, cid)
    assert now == stored, "an approval is no change"
    with db.tx() as conn:  # the dispatch, as api/execution.py records it
        db.run(conn, "UPDATE reservations SET state = 'DONE' WHERE run_id = :r AND case_id = 'TEST-A' "
                     "AND kind IN ('SOURCE_LOT', 'TRUCK')", r=run["run_id"])
    assert stored_and_now(run, cid)[1] == stored, "an executed transfer is no change"
    with db.tx() as conn:  # the plan ran and its lot is gone
        bookings.done(conn, run["run_id"], "TEST-A")
    assert stored_and_now(run, cid)[1] == stored
    assert reassess_lots(run, row["lot_id"]) == [] and successor_of(run, cid) is None
    with db.tx() as conn:  # TEST-B's hold is past its hour, not yet marked
        db.run(conn, "UPDATE reservations SET expires_at = now() - interval '1 minute' WHERE run_id = :r "
                     "AND case_id = 'TEST-B'", r=run["run_id"])
    expired = stored_and_now(run, cid)[1]
    assert expired != stored, "an expired hold is a release"
    with db.tx() as conn:
        bookings.expire_holds(conn, fresh(run["run_id"]), "TEST-B")
    assert stored_and_now(run, cid)[1] == expired  # marked EXPIRED: the same booking set
    with db.tx() as conn:
        bookings.release(conn, run["run_id"], "TEST-C")
    released = stored_and_now(run, cid)[1]
    assert released not in (stored, expired), "a released booking is a change"
    opened = reassess_lots(run, row["lot_id"])
    succ = successor_of(run, cid)
    assert succ is not None and opened == [succ["key"]] and opened_entry(run, succ["case_id"])["reasons"] == ["inputs"]


# --- W1 (D-38.1): only a change that can affect the lot counts ------------------------------------------------------

def add_fact(run_id: str, fact_id: str, kind: str, adjustments: list[dict], day: int, sku: str | None = None,
             branch: str | None = None, zone: str | None = None) -> None:
    """A confirmed reported fact valid on `day`, written directly (test values), as facts.write stores one."""
    with db.tx() as conn:
        db.run(conn, "INSERT INTO reported_facts (run_id, fact_id, signal_id, kind, adjustments, sku, valid_from, "
                     "valid_to, label, confirmed_by, confirmed_day, branch, zone) VALUES (:r, :f, 'TEST-SIG', :k, "
                     "CAST(:a AS jsonb), :s, :d, :d, 'test', 'test', :d, :b, :z)", r=run_id, f=fact_id, k=kind,
               a=db.js(adjustments), s=sku, d=day, b=branch, z=zone)


def drop_test_rows(run_id: str) -> None:
    with db.tx() as conn:
        db.run(conn, "DELETE FROM reservations WHERE run_id = :r AND case_id LIKE 'TEST-W%'", r=run_id)
        db.run(conn, "DELETE FROM reported_facts WHERE run_id = :r AND fact_id LIKE 'TEST-W%'", r=run_id)


def other_truck(run, source: str, branches: list[str]) -> str:
    """The bucket of a run leaving a destination of the story case's version (never its source) on some day from
    today: a run the story lot could not use."""
    from api import refdata
    r = fresh(run["run_id"])
    ref = refdata.get(r["dataset_version"])
    return next(f"{x.run_code}|{d}" for d in range(r["day"], r["day"] + 7) for b in branches if b != source
                for x in ref.runs_from(b, d) if x.origin != source)


def recheck_spy(monkeypatch) -> list[str]:
    """The closed cases a reopen check looks at from here on (closing.reopen_allowed, wrapped)."""
    calls: list[str] = []
    real = closing().reopen_allowed

    def spy(conn, r, prev, lot_row, next_day=False, seen=None):
        calls.append(prev["case_id"])
        return real(conn, r, prev, lot_row, next_day=next_day, seen=seen)
    monkeypatch.setattr(closing(), "reopen_allowed", spy)
    return calls


def test_w1_the_fingerprint_counts_only_what_can_affect_the_lot(run):
    """D-38.1, each probe added alone to the rejected story lot (chilled milk at its source, with its version's
    destinations) and taken out again: its own product's demand at those branches counts, another product's does not;
    storage in its own zone there counts from any product, another zone's does not; truck space on a run leaving its
    source counts from any product, a run leaving only a destination does not; a capacity fact for its zone counts,
    one for another zone does not; a demand event for every product or its own counts, one for another product does
    not; a transport fact on a run leaving its source counts, one on a run leaving only a destination does not."""
    from api import refdata
    from tests.api.test_reassess import recomputed
    dest, truck = story_places(run)
    row = reject_story(run)
    cid, stored, sku, source = row["case_id"], row["fingerprint"], row["sku"], row["source_branch"]
    r = fresh(run["run_id"])
    ref, day = refdata.get(r["dataset_version"]), r["day"]
    zone = ref.products[sku].zone
    other_sku = next(s for s, p in ref.products.items() if s != sku and p.zone != zone)
    other_zone = ref.products[other_sku].zone
    with db.tx() as conn:
        branches = closing().branches(conn, case_now(run, cid))
    far = other_truck(run, source, branches)
    code, run_day = truck.split("|")[0], int(truck.split("|")[1])
    far_code, far_day = far.split("|")[0], int(far.split("|")[1])
    far_origin = next(b for b in branches if any(x.run_code == far_code for x in ref.runs_from(b, far_day)))
    probes = {
        "demand, its product": ("booking", ("DEMAND", f"{dest}|{sku}")),
        "demand, another product": ("booking", ("DEMAND", f"{dest}|{other_sku}")),
        "storage, its zone": ("booking", ("STORAGE", f"{dest}|{zone}")),
        "storage, another zone": ("booking", ("STORAGE", f"{dest}|{other_zone}")),
        "truck, a run leaving its source": ("booking", ("TRUCK", truck)),
        "truck, a run leaving only a destination": ("booking", ("TRUCK", far)),
        "capacity fact, its zone": ("fact", ("CAPACITY", [{"kind": "capacity", "branch": dest, "day_from": day,
                                                           "day_to": day, "free_totes": 0}], None, dest, zone, day)),
        "capacity fact, another zone": ("fact", ("CAPACITY", [{"kind": "capacity", "branch": dest, "day_from": day,
                                                               "day_to": day, "free_totes": 0}], None, dest,
                                                 other_zone, day)),
        "demand event, every product": ("fact", ("DEMAND_EVENT", [{"kind": "demand_multiplier", "branch": dest,
                                                                   "day_from": day, "day_to": day, "value": 1.5}],
                                                 None, dest, None, day)),
        "demand event, another product": ("fact", ("DEMAND_EVENT", [{"kind": "demand_multiplier", "branch": dest,
                                                                     "day_from": day, "day_to": day, "value": 1.5}],
                                                   other_sku, dest, None, day)),
        "transport fact, a run leaving its source": ("fact", ("TRANSPORT", [{"kind": "run_available",
                                                                             "run_code": code, "value": False}],
                                                              None, source, None, run_day)),
        "transport fact, a run leaving only a destination": ("fact", ("TRANSPORT", [{"kind": "run_available",
                                                                                     "run_code": far_code,
                                                                                     "value": False}],
                                                                      None, far_origin, None, far_day)),
    }
    seen = {}
    for n, (name, (what, probe)) in enumerate(probes.items()):
        if what == "booking":
            kind, bucket = probe
            hold_booking(run["run_id"], f"TEST-W{n}", kind, bucket, day + 1 if kind != "TRUCK" else None, 2.0,
                         state="COMMITTED")
        else:
            kind, adjustments, fsku, branch, fzone, fday = probe
            add_fact(run["run_id"], f"TEST-W{n}", kind, adjustments, fday, sku=fsku, branch=branch, zone=fzone)
        seen[name] = recomputed(run, cid) != stored
        drop_test_rows(run["run_id"])
        assert recomputed(run, cid) == stored
    assert seen == {name: not name.endswith(("another product", "another zone", "only a destination"))
                    for name in probes}


def bread_transfer_places(run, case: dict) -> list[tuple[str, str, int | None]]:
    """(kind, bucket, day) a transfer of the bread-keep case on its version's run books, as bookings.holds_for
    places them: demand and ambient storage at its destination, the truck, its own lot."""
    with db.tx() as conn:
        rec = cases.active_rec(conn, case)
    run0 = rec["engine_input"]["runs"][0]
    day = fresh(run["run_id"])["day"]
    return [("DEMAND", f"{run0['dest']}|{case['sku']}", day + 1), ("STORAGE", f"{run0['dest']}|ambient", day + 1),
            ("TRUCK", f"{run0['run_code']}|{run0['day']}", run0["day"]), ("SOURCE_LOT", case["lot_id"], None)]


def test_w1_bread_taking_ambient_space_never_reopens_chilled_milk(run, monkeypatch):
    """D-38.1 (the user's example): the bread-keep case (ambient bread, its transfer to a destination the story lot's
    version also covers, on a run leaving another store) holds its demand, ambient storage, truck and lot; the story
    case (chilled milk) is rejected. Rejecting the bread case releases all of it: none of it can affect the milk lot,
    so the milk lot is not rechecked, its fingerprint is as stored and no successor opens."""
    from tests.api.conftest import by_tag
    from tests.api.test_reassess import recomputed
    bread = by_tag(run, "bread-keep")
    for kind, bucket, day in bread_transfer_places(run, bread):
        hold_booking(run["run_id"], bread["case_id"], kind, bucket, day, 2.0)
    row = reject_story(run)
    with db.tx() as conn:
        assert set(closing().branches(conn, bread)) & set(closing().branches(conn, case_now(run, row["case_id"])))
    calls = recheck_spy(monkeypatch)
    assert call(run, workflow.reject, seat(bread["source_branch"]), bread["case_id"], "Not needed") == \
        {"state": "REJECTED"}
    assert row["case_id"] not in calls, "nothing the bread case released can affect the milk lot"
    assert recomputed(run, row["case_id"]) == row["fingerprint"] and successor_of(run, row["case_id"]) is None


def strawberry_case(run) -> dict:
    """A chilled product's case at the story lot's source (a strawberry lot written by the test, evaluated by the
    engine): its version covers the same destinations."""
    from tests.api.test_workflow import insert_case
    from tests.api.verify_helpers import story
    source = story(run)["source_branch"]
    case = insert_case(run, {"lot_id": "STR-T1", "branch": source, "sku": "STRAW-500", "qty": 300.0,
                             "expiry_day": 6, "received_seq": 5, "markdown_pct": 0, "expiry_source": "PRINTED"},
                       "TEST-STR")
    assert case["active_rec_version"] is not None
    return case


def test_w1_another_chilled_product_in_shared_cold_storage_triggers_a_recheck(run, monkeypatch):
    """D-38.1: a strawberry case (another chilled product) holds chilled storage at a destination of the story lot;
    the story case is rejected. Rejecting the strawberry case releases it: the milk lot is rechecked, its inputs
    changed, and it reopens once (trigger "release", reason "inputs")."""
    dest, _ = story_places(run)
    straw = strawberry_case(run)
    hold_booking(run["run_id"], straw["case_id"], "STORAGE", f"{dest}|chilled", fresh(run["run_id"])["day"] + 1, 2.0)
    row = reject_story(run)
    calls = recheck_spy(monkeypatch)
    assert call(run, workflow.reject, seat(straw["source_branch"]), straw["case_id"], "Not needed") == \
        {"state": "REJECTED"}
    assert row["case_id"] in calls
    succ = successor_of(run, row["case_id"])
    assert succ is not None and (opened_entry(run, succ["case_id"])["trigger"],
                                 opened_entry(run, succ["case_id"])["reasons"]) == ("release", ["inputs"])


def test_w1_another_products_booking_on_the_same_run_triggers_a_recheck(run, monkeypatch):
    """D-38.1: the strawberry case holds truck space on a run leaving the story lot's source (another product's
    booking on a run the milk lot could use); the story case is rejected. Rejecting the strawberry case releases it:
    the milk lot is rechecked and reopens once (trigger "release", reason "inputs")."""
    _, truck = story_places(run)
    straw = strawberry_case(run)
    hold_booking(run["run_id"], straw["case_id"], "TRUCK", truck, int(truck.split("|")[1]), 2.0)
    row = reject_story(run)
    calls = recheck_spy(monkeypatch)
    assert call(run, workflow.reject, seat(straw["source_branch"]), straw["case_id"], "Not needed") == \
        {"state": "REJECTED"}
    assert row["case_id"] in calls
    succ = successor_of(run, row["case_id"])
    assert succ is not None and (opened_entry(run, succ["case_id"])["trigger"],
                                 opened_entry(run, succ["case_id"])["reasons"]) == ("release", ["inputs"])



# --- W2 (D-38.2): a confirmed fact or a selection that cancels a pending approval rechecks the closed lots ----------

def pending_with_hold(run, case: dict, kind: str, bucket: str, day: int | None) -> None:
    """The case sent by its source manager (PENDING_APPROVAL, its own holds placed), plus one more hold of its round
    that touches the story lot (as its transfer would book it; a test value)."""
    from tests.api.conftest import owned_send
    owned_send(run, seat(case["source_branch"]), case["case_id"], case_now(run, case["case_id"])["active_rec_version"])
    assert case_now(run, case["case_id"])["state"] == "PENDING_APPROVAL"
    hold_booking(run["run_id"], case["case_id"], kind, bucket, day, 2.0)


def test_w2_a_confirmed_fact_that_cancels_a_pending_approval_rechecks_closed_lots(run):
    """D-38.2: the strawberry case is pending approval, holding chilled storage at a destination of the story lot,
    when the story case is rejected. A confirmed count on the strawberry lot gives its case a new version, which
    cancels the pending approval and releases its holds: the milk lot is rechecked at once (the same helper a close
    uses) and reopens once, trigger "release", reason "inputs"; the count's answer lists it among `reassessed`."""
    dest, _ = story_places(run)
    straw = strawberry_case(run)
    pending_with_hold(run, straw, "STORAGE", f"{dest}|chilled", fresh(run["run_id"])["day"] + 1)
    row = reject_story(run)
    assert successor_of(run, row["case_id"]) is None
    done = confirm_count(run, straw["lot_id"])
    assert case_now(run, straw["case_id"])["state"] == "OPEN"  # the approval round was cancelled
    succ = successor_of(run, row["case_id"])
    assert succ is not None and succ["key"] in done["reassessed"]
    assert (opened_entry(run, succ["case_id"])["trigger"], opened_entry(run, succ["case_id"])["reasons"]) == \
        ("release", ["inputs"])


def test_w2_a_selection_that_cancels_a_pending_approval_rechecks_closed_lots(run):
    """D-38.2: the same, with the strawberry case's source manager selecting Keep instead (selection._cancel_round
    releases the pending round): the milk lot is rechecked at once and reopens once, trigger "release"."""
    from api import selection
    _, truck = story_places(run)
    straw = strawberry_case(run)
    pending_with_hold(run, straw, "TRUCK", truck, int(truck.split("|")[1]))
    row = reject_story(run)
    v = case_now(run, straw["case_id"])["active_rec_version"]
    out = call(run, selection.select, seat(straw["source_branch"]), straw["case_id"], v, "keep")
    assert out["state"] == "OPEN"
    succ = successor_of(run, row["case_id"])
    assert succ is not None and (opened_entry(run, succ["case_id"])["trigger"],
                                 opened_entry(run, succ["case_id"])["reasons"]) == ("release", ["inputs"])


# --- W3 (D-38.3): a close during missing data stores an empty considered record ----------------------------------

def test_w3_a_lot_closed_while_data_was_missing_reopens_once_the_data_arrives(run, monkeypatch):
    """D-38.3: the story case is rejected while the data to judge its lot is missing (the screen and the morning state
    raise the engine's HorizonError, as a missing forecast day makes them): the close stores an empty considered
    record (the day and days left, no step, no clearance, the window closed, no actions), not null, and the lot
    stays at risk. The next morning, with the data back, the lot still qualifies with real options: the morning
    screen reopens it once through `new_option`; re-running opens nothing more."""
    from api import screen
    from engine.simulate import HorizonError
    from tests.api.conftest import world_lot
    from tests.api.test_advance import advance_once
    from tests.api.test_reassess import lot_cases, lot_state
    from tests.api.verify_helpers import story

    def missing(*args, **kwargs):
        raise HorizonError("no forecast for a day the lot needs")
    case = story(run)
    with monkeypatch.context() as m:
        m.setattr(screen, "screen", missing)
        m.setattr(closing(), "state", missing)
        row = reject_story(run)
    day = fresh(run["run_id"])["day"]
    days_left = world_lot(run, case["lot_id"])["expiry_day"] - day  # the record's, as the close reads it
    assert row["lot_status"] == "AT_RISK"
    assert row["considered"] == {"day": day, "days_left": days_left, "band": closing()._band(days_left),
                                 "step_pct": 0, "clearance": False, "transfer_open": False, "actions": []}
    assert advance_once(run)["advanced"] is True
    now = lot_state(run, case["lot_id"])
    assert now["actions"] and qualifies(run, case["lot_id"])
    succ = successor_of(run, row["case_id"])
    assert succ is not None and opened_entry(run, succ["case_id"])["trigger"] == "next_day"
    assert "new_option" in opened_entry(run, succ["case_id"])["reasons"]
    assert morning_screen(run) == [] and len(lot_cases(run, case["lot_id"])) == 2


# --- the optional index (07-12 review, Codex 5 / agy 8): a lot's chain is read through `predecessor` ------------

def test_cases_are_indexed_by_predecessor(run):
    """The successor lookups (closing.successor, latest, the morning screen and the release recheck) filter cases by
    (run_id, predecessor): an additive index serves them."""
    with db.tx() as conn:
        row = db.one(conn, "SELECT indexdef FROM pg_indexes WHERE tablename = 'cases' "
                           "AND indexdef LIKE '%(run_id, predecessor)%'")
    assert row is not None


# --- T1-T3: field reports (the 07-13 review) ---------------------------------------------------------------------

def test_t1_a_demand_event_for_later_days_changes_todays_input_on_those_days_only(client):
    """07-13 review T1: the story case's destination manager files and confirms a DEMAND_EVENT for tomorrow and the
    day after (x1.5). The case's new input today holds its demand multiplier, and the effective forecast at that store
    is 1.5 times the recorded one on those two days only: today and the day after them are unchanged, and so is every
    other store."""
    from dataclasses import replace
    from engine import from_dict
    from engine.adjust import effective
    from tests.api import test_http
    from tests.api.conftest import as_seat
    from tests.api.test_signals import confirmed, filed, input_of, session_run
    card = test_http.story(client)
    run = session_run(client)
    d, dest, cid = run["day"], card["dest"], card["case_id"]
    as_seat(client, dest)
    event = filed(client, "DEMAND_EVENT", {"multiplier": 1.5, "valid_from": cases.iso_of(run, d + 1),
                                           "valid_to": cases.iso_of(run, d + 2)})
    confirmed(client, event["signal_id"])
    raw = input_of(run["run_id"], cid)
    assert [a for a in raw["reported_facts"] if a["kind"] == "demand_multiplier"] == [
        {"kind": "demand_multiplier", "branch": dest, "day_from": d + 1, "day_to": d + 2, "value": 1.5}]
    inp = from_dict(raw)
    with_event = {(f.branch, f.day): f.base for f in effective(inp).forecasts}
    without = {(f.branch, f.day): f.base for f in effective(replace(inp, reported_facts=())).forecasts}
    changed = {k for k in without if abs(with_event[k] - without[k]) > 1e-9}
    assert changed == {(dest, d + 1), (dest, d + 2)}
    assert all(abs(with_event[k] - 1.5 * without[k]) < 1e-9 for k in changed)


def test_t2_a_quote_valid_until_before_the_day_it_applies_on_is_refused_at_filing(client):
    """07-13 review T2: HQ files a transport quote for tomorrow's run whose `valid_until` is today, before its
    `applies_on`: 400 bad_request naming valid_until, and nothing is filed. The same quote valid until tomorrow
    files."""
    from tests.api import test_http
    from tests.api.conftest import as_seat
    from tests.api.test_signals import file_report, input_of, session_run
    card = test_http.story(client)
    run = session_run(client)
    d = run["day"]
    code = input_of(run["run_id"], card["case_id"])["runs"][0]["run_code"]
    quote = {"run_code": code, "what": "QUOTE", "amount": 25, "pricing_basis": "PER_TRIP", "includes_handling": "YES",
             "applies_on": cases.iso_of(run, d + 1), "valid_until": cases.iso_of(run, d)}
    as_seat(client, None)
    with db.tx() as conn:
        before = db.one(conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r", r=run["run_id"])["n"]
    r = file_report(client, "TRANSPORT", quote)
    assert (r.status_code, r.json().get("error"), r.json().get("field")) == \
        (400, "bad_request", "valid_until"), r.json()
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r", r=run["run_id"])["n"] == before
    r = file_report(client, "TRANSPORT", {**quote, "valid_until": cases.iso_of(run, d + 1)})
    assert r.status_code == 200, r.json()



def test_t2_a_quote_whose_day_has_passed_is_still_quote_expired(client):
    """07-13 review T2, its consequence: with valid_until never before applies_on, a quote can only expire once its
    run's day has passed too. HQ files a quote for today's run valid until today; after one Next day its confirmation
    is still 409 quote_expired "This quote expired on <that day>" (the quote's own refusal comes before the range
    check), and nothing is applied."""
    from api import refdata
    from tests.api import test_http
    from tests.api.conftest import as_seat
    from tests.api.test_advance import advance_once
    from tests.api.test_signals import confirm_report, filed, input_of, session_run
    card = test_http.story(client)
    run = session_run(client)
    d = run["day"]
    code = input_of(run["run_id"], card["case_id"])["runs"][0]["run_code"]
    as_seat(client, None)
    quote = filed(client, "TRANSPORT", {"run_code": code, "what": "QUOTE", "amount": 25, "pricing_basis": "PER_TRIP",
                                        "includes_handling": "YES", "applies_on": cases.iso_of(run, d),
                                        "valid_until": cases.iso_of(run, d)})
    assert advance_once(run)["advanced"] is True
    r = confirm_report(client, quote["signal_id"])
    assert (r.status_code, r.json().get("error")) == (409, "quote_expired"), r.json()
    assert r.json()["message"] == f"This quote expired on {cases.date_label(d, refdata.get(run['dataset_version']))}"
    with db.tx() as conn:
        assert db.one(conn, "SELECT state FROM signals WHERE run_id = :r AND signal_id = :s", r=run["run_id"],
                      s=quote["signal_id"])["state"] == "READ"

def test_t3_a_huge_number_is_out_of_bounds_never_a_server_error(client):
    """07-13 review T3: a report whose whole-number or decimal field is a JSON integer far past the float range (1
    followed by 400 zeros) is 400 out_of_bounds naming the field, never a 500."""
    from tests.api import test_http
    from tests.api.conftest import as_seat
    from tests.api.test_signals import file_report, session_run
    card = test_http.story(client)
    run = session_run(client)
    today = cases.iso_of(run, run["day"])
    as_seat(client, card["dest"])
    huge = 10 ** 400
    for kind, fields, name in (
            ("CAPACITY", {"zone": "chilled", "free_totes": huge, "valid_from": today, "valid_to": today}, "free_totes"),
            ("DEMAND_EVENT", {"multiplier": huge, "valid_from": today, "valid_to": today}, "multiplier")):
        r = file_report(client, kind, fields)
        assert (r.status_code, r.json()["error"], r.json().get("field")) == (400, "out_of_bounds", name), r.json()

# --- the public set (module-scoped public_template: ACTIVE for the rest of this module, so these stay last) --------

def public_workspace() -> dict:
    """A workspace cloned from the ACTIVE template (the tiny public set while `public_template` is in use)."""
    from api import sandbox
    with db.tx() as conn:
        rid = sandbox.clone(conn, sandbox.active_template(conn))
    return fresh(rid)


def test_r2_the_stock_risk_filter_lists_a_public_successor(public_template):
    """07-12 review R2 on the tiny public set: a case opened on rule (b) is rejected; a confirmed count on its lot
    opens its successor, whose `opened` entry carries b true, so `GET /api/cases?risk=stock` (board_page) lists it."""
    run = public_workspace()
    with db.tx() as conn:
        first = db.one(conn, "SELECT c.* FROM cases c JOIN activity_log g ON g.run_id = c.run_id AND g.case_id = "
                             "c.case_id AND g.type = 'opened' WHERE c.run_id = :r AND c.state = 'OPEN' AND "
                             "g.payload->>'b' = 'true' ORDER BY c.case_id LIMIT 1", r=run["run_id"])
    assert first is not None, "the tiny public template opens a case on rule (b)"
    assert call(run, workflow.reject, seat(first["source_branch"]), first["case_id"], "Not this week") == \
        {"state": "REJECTED"}
    confirm_count(run, first["lot_id"])
    succ = successor_of(run, first["case_id"])
    assert succ is not None and opened_entry(run, succ["case_id"]).get("b") is True
    with db.tx() as conn:
        page = cases.board_page(conn, fresh(run["run_id"]), seat(None), branch=first["source_branch"], risk="stock")
    assert succ["key"] in [c["key"] for c in page["cases"]]



def test_w2_a_confirmed_fact_that_cancels_a_public_pending_approval_rechecks_closed_lots(public_template):
    """D-38.2 on the tiny public set: two cases at one store, for two products. One is rejected; the other is pending
    approval with a hold in the first one's storage zone at a branch its version covered. A confirmed count on the
    pending case's lot cancels its approval: the rejected lot is rechecked at once and reopens once (trigger
    "release")."""
    from api import refdata
    run = public_workspace()
    ref = refdata.get(run["dataset_version"])
    with db.tx() as conn:
        pair = db.rows(conn, "SELECT a.case_id AS closed, b.case_id AS pending FROM cases a JOIN cases b ON "
                             "b.run_id = a.run_id AND b.source_branch = a.source_branch AND b.sku <> a.sku WHERE "
                             "a.run_id = :r AND a.state = 'OPEN' AND b.state = 'OPEN' ORDER BY a.case_id, b.case_id "
                             "LIMIT 1", r=run["run_id"])
    assert pair, "the tiny public template opens two cases at one store"
    closed, pending = case_now(run, pair[0]["closed"]), case_now(run, pair[0]["pending"])
    with db.tx() as conn:
        dest = next(iter(sorted(set(closing().branches(conn, closed)) - {closed["source_branch"]})),
                    closed["source_branch"])
    zone = ref.products[closed["sku"]].zone
    pending_with_hold(run, pending, "STORAGE", f"{dest}|{zone}", run["day"] + 1)
    assert call(run, workflow.reject, seat(closed["source_branch"]), closed["case_id"], "Not this week") == \
        {"state": "REJECTED"}
    assert successor_of(run, closed["case_id"]) is None
    done = confirm_count(run, pending["lot_id"])
    assert case_now(run, pending["case_id"])["state"] == "OPEN"
    succ = successor_of(run, closed["case_id"])
    assert succ is not None and succ["key"] in done["reassessed"]
    assert opened_entry(run, succ["case_id"])["trigger"] == "release"
