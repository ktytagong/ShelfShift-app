"""Next day moves three private worlds on one hidden demand (spec §8 Demo clock and advance day; FLOW-07, FLOW-08).

Assertions are beat-level (counts, equalities, states), never seed numbers (D-14). Days are read from the seed
calendar through refdata, never written as constants. Every shipment a test executes is created by the API itself
(send, accept, approve, prepare), never a hand-built Shipment or world action (Carried shipment contract).
"""
import json

import pytest

import math
import uuid

from api import advance, bookings, cases, db, inputs, refdata, sandbox, screen, workflow
from api.errors import ApiError
from engine.model import MARKDOWN, TRANSFER, trip_cost
from tests.api.conftest import fresh, own_series, own_world, owned_send, physical, world_lot, world_rows
from tests.api.test_workflow import STORY, approve_transfer, call, case_with_pick, pick, source_of, version
from tests.api.verify_helpers import shown  # approve names the plan the page shows (07 review C)


def advance_once(run):
    r = fresh(run["run_id"])
    return db.in_tx(lambda conn: advance.advance_day(conn, r["run_id"], r["version"]))


def ref_of(run):
    return refdata.get(run["dataset_version"])


def check_units(run_id):
    r = fresh(run_id)
    with db.tx() as conn:
        sandbox.network_check(conn, r, refdata.get(r["dataset_version"]), refdata.truth(r["dataset_version"]))


def rows_without_run_id(run_id, table, **where):
    """Every row of a per-run table for the run, minus run_id, as sorted JSON text (comparable across runs)."""
    cond = "".join(f" AND {k} = :{k}" for k in where)
    with db.tx() as conn:
        found = db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r{cond}", r=run_id, **where)
    return sorted(json.dumps({k: v for k, v in row.items() if k != "run_id"}, sort_keys=True, default=str)
                  for row in found)


def test_all_three_worlds_move_and_units_add_up(run):
    out = advance_once(run)
    assert out == {"advanced": True, "day": run["day"] + 1, "version": run["version"] + 1}
    check_units(run["run_id"])
    with db.tx() as conn:
        worlds = {w["world"] for w in db.rows(conn, "SELECT DISTINCT world FROM outcomes WHERE run_id = :r AND day = :d",
                                              r=run["run_id"], d=run["day"])}
        logged = db.rows(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND type = 'day_advanced' "
                               "AND day = :d", r=run["run_id"], d=run["day"])
    assert worlds == set(advance.WORLDS)
    assert [l["payload"] for l in logged] == [{"to_day": run["day"] + 1}]


def test_every_world_sells_the_same_hidden_demand(run):
    advance_once(run)
    truth = refdata.truth(run["dataset_version"])
    with db.tx() as conn:
        got = db.rows(conn, "SELECT world, branch, sku, demand FROM outcomes WHERE run_id = :r AND day = :d",
                      r=run["run_id"], d=run["day"])
    by_world = {w: {(o["branch"], o["sku"]): o["demand"] for o in got if o["world"] == w} for w in advance.WORLDS}
    hidden = {(b, s): truth["demand"][(b, s, run["day"])] for b, s in by_world["shelfshift"]}
    assert by_world["shelfshift"] == by_world["baseline_rule"] == by_world["do_nothing"] == hidden


def test_a_day_with_nothing_prepared_still_writes_every_outcome(run):
    ref = ref_of(run)
    with db.tx() as conn:
        planned = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND state = 'PLANNED'",
                         r=run["run_id"])["n"]
        # cases open during the warm-up (04-06), but none is approved or prepared before 04-07/04-08
        prepared = db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r AND state IN ('APPROVED', 'PREPARED')",
                          r=run["run_id"])["n"]
    assert (planned, prepared) == (0, 0)
    advance_once(run)
    with db.tx() as conn:
        counts = {r["world"]: r["n"] for r in db.rows(conn, "SELECT world, count(*) AS n FROM outcomes WHERE run_id = :r "
                                                            "AND day = :d GROUP BY world", r=run["run_id"], d=run["day"])}
    assert counts == {w: len(ref.branches) * len(ref.products) for w in advance.WORLDS}
    check_units(run["run_id"])


def test_the_decision_days_deliveries_are_lots_before_the_screen_and_count_once(run):
    ref = ref_of(run)
    tomorrow = run["day"] + 1
    arriving = world_rows(run, "inbound", "world = 'shelfshift' AND arrival_day = :d AND source_case_id IS NULL",
                          "inbound_id", d=tomorrow)  # as the workspace reads them (api/overlay.py)
    assert arriving, "the seed has a delivery on the day after the template day"
    row = arriving[0]
    assert row["arrival_min"] < ref.calendar["open_min"]  # a 06:00 delivery, before the 09:00 screen
    physical = refdata.truth(run["dataset_version"])["physical"].get(row["inbound_id"], row["qty"])
    advance_once(run)
    with db.tx() as conn:
        for world in advance.WORLDS:
            lots = db.rows(conn, "SELECT * FROM lots WHERE run_id = :r AND world = :w AND lot_id = :l",
                           r=run["run_id"], w=world, l=row["inbound_id"])
            still = db.rows(conn, "SELECT * FROM inbound WHERE run_id = :r AND world = :w AND inbound_id = :l",
                            r=run["run_id"], w=world, l=row["inbound_id"])
            phys = db.rows(conn, "SELECT qty FROM truth_lots WHERE run_id = :r AND world = :w AND lot_id = :l",
                           r=run["run_id"], w=world, l=row["inbound_id"])
            assert (len(lots), still) == (1, []), world
            [lot] = lots
            assert (lot["qty"], lot["branch"], lot["expiry_day"], lot["markdown_pct"], lot["verified_by"],
                    lot["origin"]) == (row["qty"], row["branch"], row["expiry_day"], 0, "COUNT", row["origin"])
            assert [p["qty"] for p in phys] == [physical], world
    check_units(run["run_id"])  # counted once: as inbound up to its arrival day and then only as a lot
    advance_once(run)  # it sells on its arrival day through the world step, and is still counted once
    check_units(run["run_id"])


def test_advance_is_idempotent_on_the_expected_version(run):
    v = fresh(run["run_id"])["version"]
    db.in_tx(lambda conn: advance.advance_day(conn, run["run_id"], v))
    again = db.in_tx(lambda conn: advance.advance_day(conn, run["run_id"], v))
    assert again == {"advanced": False, "day": run["day"] + 1, "version": v + 1}
    assert (fresh(run["run_id"])["day"], fresh(run["run_id"])["version"]) == (run["day"] + 1, v + 1)


def test_after_the_last_demo_day_the_demo_is_complete(run):
    last = ref_of(run).calendar["last_demo_day"]
    for _ in range(last - run["day"] + 1):  # the last demo day is stepped too
        assert advance_once(run)["advanced"]
    before = fresh(run["run_id"])
    assert before["day"] == last + 1
    with pytest.raises(ApiError) as e:
        advance_once(run)
    assert (e.value.status, e.value.code) == (409, "demo_complete")
    after = fresh(run["run_id"])
    assert (after["day"], after["version"]) == (before["day"], before["version"])
    check_units(run["run_id"])


def add_markdown(run, lot, pct, action_id):
    with db.tx() as conn:
        db.run(conn, "INSERT INTO world_actions (run_id, world, action_id, kind, lot_id, sku, pct, effective_day) "
                     "VALUES (:r, 'shelfshift', :a, 'MARKDOWN', :l, :s, :p, :d)", r=run["run_id"], a=action_id,
               l=lot["lot_id"], s=lot["sku"], p=pct, d=run["day"])


def action_state(run, action_id):
    with db.tx() as conn:
        return db.one(conn, "SELECT state FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                            "AND action_id = :a", r=run["run_id"], a=action_id)["state"]


def test_a_repeated_price_step_is_voided_and_the_day_completes(run):
    on_sale = world_rows(run, "lots", "world = 'shelfshift' AND qty > 0 AND expiry_day >= :d",
                         "markdown_pct DESC, lot_id", d=run["day"])
    repeat = on_sale[0]
    own_series(run, repeat["branch"], repeat["sku"])  # the test writes the record directly
    with db.tx() as conn:
        if repeat["markdown_pct"] == 0:  # a price already lowered on an earlier day, as the record shows it
            db.run(conn, "UPDATE lots SET markdown_pct = 20 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   r=run["run_id"], l=repeat["lot_id"])
            repeat = {**repeat, "markdown_pct": 20}
    add_markdown(run, repeat, repeat["markdown_pct"], "test:repeat")
    deeper = next((l for l in on_sale if l["markdown_pct"] == 0 and l["lot_id"] != repeat["lot_id"]), None)
    if deeper is not None:
        add_markdown(run, deeper, 30, "test:deeper")
    assert advance_once(run)["advanced"]
    assert action_state(run, "test:repeat") == "VOIDED"
    if deeper is not None:
        assert action_state(run, "test:deeper") == "DONE"  # a change that lowers the price is still applied
    with db.tx() as conn:
        voided = db.rows(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND type = 'price_change_voided'",
                         r=run["run_id"])
    assert [v["payload"]["action_id"] for v in voided] == ["test:repeat"]
    assert voided[0]["payload"]["pct"] == voided[0]["payload"]["lot_pct"] == repeat["markdown_pct"]
    check_units(run["run_id"])


def test_two_sandboxes_advanced_from_the_same_state_write_the_same_outcomes(run, template):
    with db.tx() as conn:
        other = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r",
                       r=sandbox.clone(conn, sandbox.active_template(conn)))
    advance_once(run)
    advance_once(other)
    for table in ("outcomes", "world_events", "lots", "truth_lots", "inbound", "world_actions"):
        assert rows_without_run_id(run["run_id"], table) == rows_without_run_id(other["run_id"], table), table
    assert rows_without_run_id(run["run_id"], "outcomes", day=run["day"])  # the day was written, not left empty


# --- prepared work orders execute on Next day (FLOW-07) ---------------------------------------------------------------

def prepare_story(run):
    """SUS-19 through send, accept, approve and prepare: its TRANSFER (and any markdown rest) work order exists."""
    assert pick(run, STORY).kind == TRANSFER, "D-15 hand-back: SUS-19's pick is not a transfer"
    assert approve_transfer(run)["state"] == "APPROVED"
    assert call(run, workflow.prepare, source_of(run, STORY), STORY) == {"state": "PREPARED"}
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                            "AND kind = 'TRANSFER'", r=run["run_id"], c=STORY)


def events(run, kind, world="shelfshift"):
    with db.tx() as conn:
        return [e["payload"] for e in db.rows(conn, "SELECT payload FROM world_events WHERE run_id = :r AND world = :w "
                                                    "AND kind = :k ORDER BY day, seq", r=run["run_id"], w=world, k=kind)]


def case_bookings(run, case_id):
    """{kind: {states}} of the case's bookings."""
    with db.tx() as conn:
        out = {}
        for r in db.rows(conn, "SELECT kind, state FROM reservations WHERE run_id = :r AND case_id = :c",
                         r=run["run_id"], c=case_id):
            out.setdefault(r["kind"], set()).add(r["state"])
        return out


def test_a_prepared_transfer_ships_on_next_day_and_arrives_as_a_moved_lot(run):
    action = prepare_story(run)
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], STORY)
    plan = pick(run, STORY)
    assert (action["origin"], action["dest"], action["run_day"], action["qty"]) == \
        (case["source_branch"], plan.dest, run["day"], plan.qty)  # the booked transfer, never guessed
    out = advance_once(run)
    assert out["advanced"] and out["version"] == run["version"] + 1
    with db.tx() as conn:
        states = {a["kind"]: a["state"] for a in db.rows(conn, "SELECT kind, state FROM world_actions WHERE run_id = :r "
                                                              "AND world = 'shelfshift' AND case_id = :c",
                                                        r=run["run_id"], c=STORY)}
        moved = db.rows(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND moved_from = :l",
                        r=run["run_id"], l=case["lot_id"])
        after = cases.case_row(conn, run["run_id"], STORY)
        logged = [a["type"] for a in db.rows(conn, "SELECT type FROM activity_log WHERE run_id = :r AND case_id = :c "
                                                   "ORDER BY seq", r=run["run_id"], c=STORY)]
    assert set(states.values()) == {"DONE"} and "TRANSFER" in states
    assert [(m["lot_id"], m["branch"], m["origin"]) for m in moved] == [(action["new_lot_id"], plan.dest, advance.MOVED)]
    booked = case_bookings(run, STORY)
    assert booked["SOURCE_LOT"] == booked["TRUCK"] == {"DONE"}  # they ended when the run departed
    assert booked["STORAGE"] == {"COMMITTED"}  # the moved stock still takes the space
    assert booked["DEMAND"] == {"CONVERTED"}
    if plan.pct:
        assert booked["LOT_PRICE"] == {"DONE"}  # the price change took effect
    # it executed, and the next morning SUS-19 opens again for its second look (the user's story,
    # 04-SECOND-LOOK-FIX): stock is still at risk and the seed's run leaves again today
    again = pick(run, STORY)
    assert (after["state"], after["look_round"]) == ("OPEN", after["round"])
    assert (again.kind, again.dest, again.run_code) == (TRANSFER, plan.dest, plan.run_code)
    assert logged.index("prepared") < logged.index("executed") < logged.index("second_look")
    check_units(run["run_id"])


def test_prepare_writes_the_adopted_trip_quote_and_next_day_charges_it(run):
    """CR-02: an adopted transport_quote and handling_cost replace the seeded trip cost on the work order the manager
    approved (the effective run), and Next day charges the quote, not the seed's costs."""
    assert pick(run, STORY).kind == TRANSFER, "D-15 hand-back: SUS-19's pick is not a transfer"
    seeded = story_run(run)
    fixed, per_unit = round(seeded.fixed_cost / 2 + 1.25, 2), round(seeded.per_unit_cost / 2 + 0.05, 2)
    assert (fixed, per_unit) != (seeded.fixed_cost, seeded.per_unit_cost)
    quote = [{"kind": "transport_quote", "run_code": seeded.run_code, "fixed_cost": fixed},
             {"kind": "handling_cost", "run_code": seeded.run_code, "per_unit_cost": per_unit}]
    call(run, workflow.adopt, source_of(run, STORY), STORY, quote, "carrier quote by phone")
    plan = pick(run, STORY)
    assert (plan.kind, plan.run_code, plan.dest) == (TRANSFER, seeded.run_code, seeded.dest), "a cheaper trip stays"
    action = prepare_story(run)
    assert (action["fixed_cost"], action["per_unit_cost"]) == (fixed, per_unit)  # the quote as approved
    advance_once(run)
    with db.tx() as conn:
        shipped = {e["payload"]["lot_id"]: float(e["payload"]["shipped"]) for e in db.rows(
            conn, "SELECT payload FROM world_events WHERE run_id = :r AND world = 'shelfshift' AND day = :d "
                  "AND kind = 'SHORT_SHIPMENT'", r=run["run_id"], d=run["day"])}
        done = db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                             "AND kind = 'TRANSFER' AND state = 'DONE' AND run_day = :d AND origin = :o AND sku = :s",
                       r=run["run_id"], d=run["day"], o=action["origin"], s=action["sku"])
    assert action["action_id"] in {a["action_id"] for a in done}
    due = [trip_cost(a["fixed_cost"], a["cancel_cost"], a["per_unit_cost"], shipped.get(a["lot_id"], float(a["qty"])))
           for a in done]
    o = outcome(run, "shelfshift", action["origin"], action["sku"], run["day"])
    assert o["transport"] == pytest.approx(sum(t for t, _ in due), abs=0.01)
    assert o["handling"] == pytest.approx(sum(h for _, h in due), abs=0.01)


def test_a_transfer_arriving_on_a_later_day_is_counted_once_at_the_destination(run):
    """WR-09: when a shipment becomes the moved lot on its run day, its pending delivery leaves `inbound` whatever its
    arrival day, so the next morning's input holds the stock once (as a lot), never also as inbound. The seed's runs
    all arrive the same day; the test moves this delivery's arrival to the next day (a test value)."""
    action = prepare_story(run)
    with db.tx() as conn:
        assert db.run(conn, "UPDATE inbound SET arrival_day = arrival_day + 1 WHERE run_id = :r AND world = 'shelfshift' "
                            "AND inbound_id = :i", r=run["run_id"], i=action["new_lot_id"]) == 1
    advance_once(run)
    with db.tx() as conn:
        left = db.rows(conn, "SELECT inbound_id FROM inbound WHERE run_id = :r AND inbound_id = :i",
                       r=run["run_id"], i=action["new_lot_id"])
        moved = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :i",
                       r=run["run_id"], i=action["new_lot_id"])
        inp = inputs.build_input(conn, fresh(run["run_id"]), action["new_lot_id"])
    assert moved is not None and left == []
    ids = [l.lot_id for l in inp.lots] + [i.inbound_id for i in inp.inbound]
    assert ids.count(action["new_lot_id"]) == 1


def test_a_transfer_is_received_in_full_even_if_some_sells_the_same_day(run):
    action = prepare_story(run)
    held = physical(run, [action["lot_id"]])[action["lot_id"]]  # as the workspace reads it (api/overlay.py)
    assert held >= action["qty"], "the story lot physically holds what the transfer takes"
    advance_once(run)
    [got] = [e for e in events(run, "TRANSFER_RECEIVED") if e["lot_id"] == action["new_lot_id"]]
    assert (got["branch"], got["qty"]) == (action["dest"], action["qty"])  # what arrived, not what was left at night
    assert not [e for e in events(run, "SHORT_SHIPMENT") if e["lot_id"] == action["lot_id"]]


# --- failed deliveries through API-created shipments (Carried from Phase 1: the shipment contract) --------------------

def story_run(run):
    """The seed run the story transfer takes (its code, today, from the source to the destination)."""
    plan = pick(run, STORY)
    return next(r for r in refdata.get(run["dataset_version"]).runs
                if r.run_code == plan.run_code and r.dest == plan.dest and r.day == run["day"])


def outcome(run, world, branch, sku, day):
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM outcomes WHERE run_id = :r AND world = :w AND branch = :b AND sku = :s "
                            "AND day = :d", r=run["run_id"], w=world, b=branch, s=sku, d=day)


def assert_failed_delivery(run, action, units_kept=True):
    """The day completed; the shipment is reported as shipping nothing; the source branch paid the run's cancel cost
    (the trip rule: the cancel cost, or the full quote when the run has none) and no handling; nothing arrived."""
    trip = story_run(run)
    out = advance_once(run)
    assert out["advanced"] and out["version"] == run["version"] + 1
    failed = [e for e in events(run, "SHORT_SHIPMENT") if e["lot_id"] == action["lot_id"]]
    assert [(e["planned"], e["shipped"]) for e in failed] == [(action["qty"], 0.0)]
    paid = trip.fixed_cost if trip.cancel_cost is None else trip.cancel_cost
    o = outcome(run, "shelfshift", action["origin"], action["sku"], run["day"])
    assert (o["transport"], o["handling"]) == (pytest.approx(paid), 0.0)
    assert failed[0]["transport_paid"] == pytest.approx(paid)
    with db.tx() as conn:
        moved = db.rows(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND moved_from = :l",
                        r=run["run_id"], l=action["lot_id"])
        state_ = db.one(conn, "SELECT state FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                              "AND action_id = :a", r=run["run_id"], a=action["action_id"])["state"]
    assert moved == [] and state_ == "DONE"
    assert not [e for e in events(run, "TRANSFER_RECEIVED") if e["lot_id"] == action["new_lot_id"]]
    if units_kept:  # units still add up unless the test itself took stock off the shelf
        check_units(run["run_id"])


def test_a_transfer_whose_source_lot_expired_is_a_failed_delivery_and_the_day_completes(run):
    action = prepare_story(run)
    own_series(run, action["origin"], action["sku"])  # the test writes the record directly
    with db.tx() as conn:  # a confirmed correction moved its expiry before the run day (record; truth has no date)
        db.run(conn, "UPDATE lots SET expiry_day = :e WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               e=run["day"] - 1, r=run["run_id"], l=action["lot_id"])
    assert_failed_delivery(run, action)


def test_a_transfer_whose_source_lot_is_missing_is_a_failed_delivery_and_the_day_completes(run):
    action = prepare_story(run)
    own_series(run, action["origin"], action["sku"])  # the test writes the record directly
    with db.tx() as conn:  # the lot is gone from the record and from the shelf
        for table in ("lots", "truth_lots"):
            db.run(conn, f"DELETE FROM {table} WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   r=run["run_id"], l=action["lot_id"])
    assert_failed_delivery(run, action, units_kept=False)


# --- only PREPARED plans execute --------------------------------------------------------------------------------------

def live_bookings(run, case_id):
    with db.tx() as conn:
        return db.one(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = :r AND case_id = :c "
                            "AND state = ANY(:s)", r=run["run_id"], c=case_id, s=list(bookings.OPEN_STATES))["n"]


def activity(run, case_id):
    with db.tx() as conn:
        return db.rows(conn, "SELECT day, type, payload FROM activity_log WHERE run_id = :r AND case_id = :c "
                             "ORDER BY seq", r=run["run_id"], c=case_id)


def test_an_approved_plan_not_prepared_in_time_is_invalidated_and_released(run):
    assert approve_transfer(run)["state"] == "APPROVED"
    assert live_bookings(run, STORY) > 0
    advance_once(run)
    with db.tx() as conn:
        c = cases.case_row(conn, run["run_id"], STORY)
        inbound = db.one(conn, "SELECT count(*) AS n FROM inbound WHERE run_id = :r AND source_case_id = :c",
                         r=run["run_id"], c=STORY)["n"]
        work = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND case_id = :c",
                      r=run["run_id"], c=STORY)["n"]
        moved = db.rows(conn, "SELECT lot_id FROM lots WHERE run_id = :r AND world = 'shelfshift' AND moved_from = :l",
                        r=run["run_id"], l=c["lot_id"])
    assert (live_bookings(run, STORY), inbound, work, moved) == (0, 0, 0, [])  # nothing shipped, everything released
    [inv] = [a for a in activity(run, STORY) if a["type"] == "invalidated"]
    assert inv["day"] == run["day"] and inv["payload"]["reason"] == advance.NOT_PREPARED
    assert (c["state"], c["re_review"]) == ("OPEN", True)


def test_an_unprepared_markdown_is_invalidated_at_end_of_day(run):
    case = case_with_pick(run, MARKDOWN)
    cid, src, v = case["case_id"], source_of(run, case["case_id"]), version(run, case["case_id"])
    owned_send(run, src, cid, v)
    assert call(run, workflow.decide, src, cid, v, "approve", None, None, shown(run, cid))["state"] == "APPROVED"
    with db.tx() as conn:
        price = db.rows(conn, "SELECT state FROM reservations WHERE run_id = :r AND case_id = :c AND kind = 'LOT_PRICE'",
                        r=run["run_id"], c=cid)
    assert [p["state"] for p in price] == ["COMMITTED"]
    advance_once(run)
    with db.tx() as conn:
        price = db.rows(conn, "SELECT state FROM reservations WHERE run_id = :r AND case_id = :c AND kind = 'LOT_PRICE'",
                        r=run["run_id"], c=cid)
        work = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND case_id = :c",
                      r=run["run_id"], c=cid)["n"]
    assert [p["state"] for p in price] == ["RELEASED"] and work == 0  # the price right is released, no instruction ran
    [inv] = [a for a in activity(run, cid) if a["type"] == "invalidated"]
    assert inv["day"] == run["day"] and inv["payload"]["reason"] == advance.NOT_PREPARED


def lot_lot(run, case):
    return world_lot(run, case["lot_id"])  # as the workspace reads it (api/overlay.py)


def test_an_invalidated_case_reopens_with_re_review_and_a_new_version(run):
    before = version(run, STORY)
    approve_transfer(run)
    advance_once(run)
    r = fresh(run["run_id"])
    with db.tx() as conn:
        c = cases.case_row(conn, r["run_id"], STORY)
        card = cases.card(conn, r, c, [])
    assert c["state"] == "OPEN" and c["active_rec_version"] not in (None, before)
    assert "re_review" in [b["kind"] for b in card["badges"]] and card["column"] == "New"
    types = [a["type"] for a in activity(run, STORY)]
    assert types.index("invalidated") < len(types) - 1 - types[::-1].index("new_version")


def test_prepare_refuses_a_markdown_the_schedule_now_covers(run, monkeypatch):
    case = case_with_pick(run, MARKDOWN)
    cid, src, v = case["case_id"], source_of(run, case["case_id"]), version(run, case["case_id"])
    plan = pick(run, cid)
    owned_send(run, src, cid, v)
    assert call(run, workflow.decide, src, cid, v, "approve", None, None, shown(run, cid))["state"] == "APPROVED"
    ref = refdata.get(run["dataset_version"])
    expiry = lot_lot(run, case)["expiry_day"]
    # the product's schedule now has a step at the plan's percent that applies from today (a valid one-step schedule)
    monkeypatch.setitem(ref.schedules, case["sku"], ((expiry - run["day"], plan.pct),))
    with pytest.raises(ApiError) as e:
        call(run, workflow.prepare, src, cid)
    assert (e.value.status, e.value.code) == (409, "conflict")
    assert any(r.startswith(f"the schedule already has it at {plan.pct}% from day ") for r in e.value.extra["reasons"])
    with db.tx() as conn:
        work = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id = :r AND case_id = :c",
                      r=run["run_id"], c=cid)["n"]
    assert (work, pick(run, cid).pct) == (0, plan.pct)
    with db.tx() as conn:
        assert cases.case_row(conn, run["run_id"], cid)["state"] == "APPROVED"


def test_a_dispatch_ships_what_is_physically_there(run):
    action = prepare_story(run)
    short = math.floor(action["qty"] / 2)  # packing will find only half of what the transfer takes
    own_series(run, action["origin"], action["sku"])  # the test writes physical stock directly
    with db.tx() as conn:
        db.run(conn, "UPDATE truth_lots SET qty = :q WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               q=float(short), r=run["run_id"], l=action["lot_id"])
    advance_once(run)
    [got] = [e for e in events(run, "SHORT_SHIPMENT") if e["lot_id"] == action["lot_id"]]
    assert (got["planned"], got["shipped"]) == (action["qty"], short)
    [received] = [e for e in events(run, "TRANSFER_RECEIVED") if e["lot_id"] == action["new_lot_id"]]
    assert received["qty"] == short
    with db.tx() as conn:
        rec = db.one(conn, "SELECT l.qty AS recorded, t.qty AS physical FROM lots l JOIN truth_lots t "
                           "USING (run_id, world, lot_id) WHERE l.run_id = :r AND l.world = 'shelfshift' "
                           "AND l.lot_id = :l", r=run["run_id"], l=action["lot_id"])
    assert rec["recorded"] == rec["physical"] == 0  # reconciled to the physical count, never below 0
    assert any(c["lot_id"] == action["lot_id"] for c in events(run, "RECORD_CORRECTION"))


# --- the store rule's world (research A6, A17) ------------------------------------------------------------------------

def test_the_store_rule_acts_on_its_own_records_through_the_same_screen(run, monkeypatch):
    screened, planned = [], []
    real_screen, real_plan = screen.screen, advance.baseline_plan

    def spy_screen(inp, thresholds):
        out = real_screen(inp, thresholds)
        screened.append((inp.case_lot_id, inp.case_lot().qty, inp.policy, out["qualifies"]))
        return out

    def spy_plan(inp):
        planned.append(inp.case_lot_id)
        return real_plan(inp)

    monkeypatch.setattr(screen, "screen", spy_screen)
    monkeypatch.setattr(advance, "baseline_plan", spy_plan)
    own_world(run)  # baseline_morning is part of advance_day, which owns every series first
    r = fresh(run["run_id"])
    with db.tx() as conn:
        candidates = advance.baseline_candidates(conn, r)
        record = {l["lot_id"]: l for l in db.rows(conn, "SELECT * FROM lots WHERE run_id = :r "
                                                        "AND world = 'baseline_rule'", r=r["run_id"])}
        sandbox_policy = inputs.policy(conn, r)
        acted = advance.baseline_morning(conn, r)
        written = db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND state = 'PLANNED' "
                                "ORDER BY action_id", r=r["run_id"])
    assert [s[0] for s in screened] == [c["lot_id"] for c in candidates]  # every candidate through the same screen
    assert all(record[s[0]]["qty"] == s[1] for s in screened)  # on the baseline world's own record
    assert all(s[2] == sandbox_policy for s in screened)  # with the sandbox Policy (A17)
    qualified = {s[0] for s in screened if s[3]}
    assert set(planned) == qualified  # baseline_plan runs only for lots the screen qualifies
    assert acted and set(acted) <= qualified  # the rule acts on Day 3, only on qualifying lots
    assert {a["world"] for a in written} == {"baseline_rule"} and {a["lot_id"] for a in written} == set(acted)
    for a in written:
        assert a["case_id"] is None
        if a["kind"] == "TRANSFER":
            assert a["origin"] == record[a["lot_id"]]["branch"]
    assert all(c["moved_from"] is None for c in candidates)


def test_the_phantom_is_written_off_not_wasted(run):
    lot_id, branch = sandbox.phantom_lot()
    phantom = world_lot(run, lot_id, "do_nothing")  # as the workspace reads it (api/overlay.py)
    assert phantom is not None and phantom["qty"] > 0  # on the record, not on the shelf
    while fresh(run["run_id"])["day"] < phantom["expiry_day"]:
        advance_once(run)
    day = fresh(run["run_id"])["day"]
    with db.tx() as conn:  # what can physically expire at the phantom's branch on its expiry day, before the step
        shelf = db.one(conn, "SELECT coalesce(sum(t.qty), 0) AS q FROM lots l JOIN truth_lots t "
                             "USING (run_id, world, lot_id) WHERE l.run_id = :r AND l.world = 'do_nothing' "
                             "AND l.branch = :b AND l.sku = :s AND l.expiry_day <= :d",
                       r=run["run_id"], b=branch, s=phantom["sku"], d=day)["q"]
        late = db.one(conn, "SELECT coalesce(sum(qty), 0) AS q FROM inbound WHERE run_id = :r AND world = 'do_nothing' "
                            "AND branch = :b AND sku = :s AND arrival_day = :d AND expiry_day <= :d",
                      r=run["run_id"], b=branch, s=phantom["sku"], d=day)["q"]
    advance_once(run)
    for world in advance.WORLDS:
        corrections = [c for c in events(run, "RECORD_CORRECTION", world) if c["lot_id"] == lot_id]
        assert [c["physical"] for c in corrections] == [0.0], world
        assert corrections[0]["recorded"] > 0, world
    wasted = outcome(run, "do_nothing", branch, phantom["sku"], day)["wasted"]
    assert wasted <= shelf + late + 1e-6  # the phantom's recorded units are never counted as waste


def test_cases_close_once_their_lots_are_gone(run):
    prepare_story(run)
    last = refdata.get(run["dataset_version"]).calendar["last_demo_day"]
    while fresh(run["run_id"])["day"] <= last:
        advance_once(run)
    with db.tx() as conn:
        story = cases.case_row(conn, run["run_id"], STORY)
        lingering = db.rows(conn, f"SELECT case_id, state FROM cases c WHERE c.run_id = :r "
                                  f"AND c.state <> ALL(:s) AND {cases.LOT_GONE}",
                            r=run["run_id"], s=["CLOSED", "REJECTED"])
        closed = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'CLOSED' AND reason = :why",
                         r=run["run_id"], why=cases.GONE_REASON)
    types = [a["type"] for a in activity(run, STORY)]
    assert story["state"] == "CLOSED"
    # the plain path on the user's second-look seed (04-SECOND-LOOK-FIX): round 1 ran, SUS-19 reopened for its
    # second look on Day 4, nobody acted on it again, and it closed as gone; round 1's bookings end with it
    assert types.index("executed") < types.index("second_look") < types.index("closed")
    assert live_bookings(run, STORY) == 0
    assert lingering == []  # every case whose lot and moved lots are gone is closed
    assert closed, "a case that never executed closes when its lot is gone, released"
    assert all(live_bookings(run, c["case_id"]) == 0 for c in closed)  # every gone-closed case holds 0 live bookings


def reservation_states(run, case_id) -> dict:
    """{res_id: (round, state)} of every booking row the case has."""
    with db.tx() as conn:
        return {r["res_id"]: (r["round"], r["state"]) for r in db.rows(
            conn, "SELECT res_id, round, state FROM reservations WHERE run_id = :r AND case_id = :c",
            r=run["run_id"], c=case_id)}


def test_a_case_closed_as_gone_after_a_second_look_ends_every_rounds_bookings(run):
    """close_gone after a second look: the earlier look's live bookings (the plan that already ran) end DONE, as on
    the observed path (advance._lifecycle_after), and the current look's holds are RELEASED. No look round keeps a
    HELD, COMMITTED or CONVERTED row, and every other row keeps its state."""
    prepare_story(run)
    advance_once(run)  # round 1 ships; SUS-19 opens again for its second look
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], STORY)
    look = case["look_round"]
    assert (case["state"], look) == ("OPEN", case["round"]), "04-SECOND-LOOK-FIX: SUS-19's Day-4 second look"
    owned_send(run, source_of(run, STORY), STORY, version(run, STORY))  # the new look holds its own round
    before = reservation_states(run, STORY)
    assert any(n <= look and st in bookings.OPEN_STATES for n, st in before.values()), "round 1 still occupies"
    assert any(n > look and st == "HELD" for n, st in before.values()), "the second look's send holds its round"
    with db.tx() as conn:  # the lot and its moved lots are gone, on the record and physically
        gone = [r["lot_id"] for r in db.rows(conn, "SELECT lot_id FROM lots WHERE run_id = :r AND world = 'shelfshift' "
                                                   "AND (lot_id = :l OR moved_from = :l)",
                                             r=run["run_id"], l=case["lot_id"])]
        for table in ("lots", "truth_lots"):
            db.run(conn, f"UPDATE {table} SET qty = 0 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = ANY(:ls)",
                   r=run["run_id"], ls=gone)
        assert STORY in cases.close_gone(conn, fresh(run["run_id"]))
        after = cases.case_row(conn, run["run_id"], STORY)
    assert (after["state"], after["reason"]) == ("CLOSED", cases.GONE_REASON)
    expected = {res: (n, st if st not in bookings.OPEN_STATES else "DONE" if n <= look else "RELEASED")
                for res, (n, st) in before.items()}
    assert reservation_states(run, STORY) == expected  # no live row for any look round; nothing else touched
    assert live_bookings(run, STORY) == 0


def test_a_case_opens_on_a_later_day(run):
    last = refdata.get(run["dataset_version"]).calendar["last_demo_day"]
    while fresh(run["run_id"])["day"] < last:
        advance_once(run)
    with db.tx() as conn:
        later = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND opened_day > :d", r=run["run_id"], d=run["day"])
        logged = db.rows(conn, "SELECT case_id FROM activity_log WHERE run_id = :r AND type = 'opened' AND day > :d",
                         r=run["run_id"], d=run["day"])
    assert later, "the screen opens cases on the days after the template day"
    assert {c["case_id"] for c in later} == {l["case_id"] for l in logged}


def test_the_synthetic_template_writes_one_outcome_row_per_store_and_product(template):
    """On the synthetic set every store carries every product, so every (world, day) of the warm-up has exactly one
    outcome row per store and product (quick 261017-p52: rows for the carrying stores only change nothing here)."""
    ref = refdata.get(fresh(template)["dataset_version"])
    with db.tx() as conn:
        got = db.rows(conn, "SELECT world, day, branch, sku FROM outcomes WHERE run_id = :r", r=template)
    by: dict = {}
    for o in got:
        by.setdefault((o["world"], o["day"]), []).append((o["branch"], o["sku"]))
    assert {w for w, _ in by} == set(advance.WORLDS)
    want = sorted((b, s) for b in ref.branches for s in ref.products)
    assert all(sorted(pairs) == want for pairs in by.values())


def test_a_public_warm_up_steps_stores_without_the_product(public_dv, monkeypatch):
    """A public set's store that does not carry a product has no hidden demand series for it (05.2 D-11). The warm-up
    still advances: the engine gets a demand for every store (0.0 for that one), and outcome rows are written only for
    the stores that carry the product that day (hidden demand, its lots, arrivals, shipments' destinations and
    origins; quick 261017-p52). A scratch TEMPLATE run built as the public_run fixture builds one, so that fixture
    stays on day 0."""
    from api import publicref
    from tests.api.conftest import drop_runs
    ref, truth = refdata.get(public_dv), refdata.truth(public_dv)
    store, sku = "S13", "P102"
    assert store in ref.branches and sku in ref.products and isinstance(truth, publicref.PublicTruth)
    run_id = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=public_dv)
        sandbox.init_worlds(conn, run_id, ref, truth)
        advance.day_start(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id))
        gone = [l["lot_id"] for l in db.rows(conn, "SELECT lot_id FROM lots WHERE run_id = :r AND branch = :b "
                                                   "AND sku = :s", r=run_id, b=store, s=sku)]
        assert gone
        db.run(conn, "DELETE FROM truth_lots WHERE run_id = :r AND lot_id = ANY(:ids)", r=run_id, ids=gone)
        db.run(conn, "DELETE FROM lots WHERE run_id = :r AND branch = :b AND sku = :s", r=run_id, b=store, s=sku)
        db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND branch = :b AND sku = :s", r=run_id, b=store, s=sku)
    hidden_of: dict = {}
    original = publicref.PublicTruth.demand_for

    def demand_for(self, k, day):  # the store has no series of the product
        got = dict(original(self, k, day))
        if k == sku:
            got.pop(store, None)
        hidden_of[(k, day)] = set(got)
        return got

    current: dict = {}
    real_world, real_step = advance.step_world, advance.step_day

    def step_world(conn, run, world, *args, **kwargs):
        current["world"] = world
        return real_world(conn, run, world, *args, **kwargs)

    calls = []

    def step_day(lots, day, demand, m, product, shipments=(), price_changes=(), arrivals=(), schedule=()):
        # captured before the call: step_day mutates lots
        branches = ({l.branch for l in lots} | {a.branch for a in arrivals} | {sh.dest for sh in shipments}
                    | {sh.origin for sh in shipments})
        calls.append({"world": current["world"], "day": day, "sku": product.sku, "demand": dict(demand),
                      "branches": branches})
        return real_step(lots, day, demand, m, product, shipments, price_changes, arrivals, schedule=schedule)

    monkeypatch.setattr(publicref.PublicTruth, "demand_for", demand_for)
    monkeypatch.setattr(advance, "step_world", step_world)
    monkeypatch.setattr(advance, "step_day", step_day)
    try:
        advanced = 0
        while True:  # build_template's warm-up loop
            with db.tx() as conn:
                if fresh(run_id)["day"] >= ref.calendar["demo_open_day"]:
                    break
                sandbox.autopilot(conn, db.lock_run(conn, run_id, statuses=("INITIALIZING",)))
            with db.tx() as conn:
                assert advance.advance_day(conn, run_id, fresh(run_id)["version"])["advanced"]
            advanced += 1
        assert advanced == ref.calendar["demo_open_day"] == 3
        assert {c["world"] for c in calls} == set(advance.WORLDS)
        for c in calls:
            assert set(c["demand"]) == set(ref.branches)
            if c["sku"] == sku:
                assert c["demand"][store] == 0.0
        with db.tx() as conn:
            got = db.rows(conn, "SELECT world, day, sku, branch FROM outcomes WHERE run_id = :r", r=run_id)
        written: dict = {}
        for o in got:
            written.setdefault((o["world"], o["day"], o["sku"]), set()).add(o["branch"])
        expected = {(c["world"], c["day"], c["sku"]): hidden_of[(c["sku"], c["day"])] | c["branches"] for c in calls}
        assert written == expected
        assert any(store not in written[(w, d, sku)] for w, d, k in expected if k == sku)
    finally:
        with db.tx() as conn:
            drop_runs(conn, [run_id])
