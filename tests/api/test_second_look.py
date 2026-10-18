"""Second look (spec §8 Second look; FLOW-09): a plan that ran but left stock at risk opens again, from the data.

The rule tests do not depend on what the seed's SUS-19 does on Day 4 (research Pitfall 5). `executed_keep_case`
inserts a synthetic lot (record and physical alike) at a branch the test picks from the seed's runs, opens a case on
it with one unit (so the engine's pick is Keep), approves that Keep through the workflow (EXECUTED), and then sets the
lot to the test's own unit count on the record and physically: a kept lot that sells slower than forecast. Units and
expiry days are the test's own values; branches, runs and schedules come from the seed through the sandbox. No test
asserts a seed quantity or an amount.
"""
import ast
from pathlib import Path

import pytest

import api
from api import cases, config, db, evaluate, inputs, refdata, screen, weekcheck, workflow
from api.errors import ApiError
from engine.adjust import effective
from engine.canonical import from_dict
from engine.model import TRANSFER, scheduled_pct, trip_cost
from tests.api.conftest import fresh, own_series, owned_send, seat, world_lot
from tests.api.test_advance import advance_once, prepare_story
from tests.api.test_workflow import STORY, call, holds, pick, source_of, state, version
from tests.api.verify_helpers import ack, shown  # approve and accept acknowledge (D-26) and name the plan (07 review C)

UNITS_AT_RISK = 400  # a test value: far more than a branch sells of one SKU in the few days left


def story_lot(run) -> dict:
    """SUS-19's lot row (the milk-story tag), the template for the synthetic lots' SKU and fields."""
    with db.tx() as conn:
        story = db.one(conn, "SELECT lot_id FROM cases WHERE run_id = :r AND tag = :t", r=run["run_id"],
                       t=cases.STORY_TAG)
    return world_lot(run, story["lot_id"])  # as the workspace reads it (api/overlay.py)


def branch_with_a_run_tomorrow(run) -> str:
    """The first seed branch, other than SUS-19's source, with a run leaving on the next demo day (a second look can
    weigh a transfer). SUS-19's source is left out: its own at-risk milk lot competes with the synthetic one-unit lot
    for the same run and demand, so the engine would not keep that one unit (since R-02 also leaves it on Day 4)."""
    ref = refdata.get(run["dataset_version"])
    story = story_lot(run)["branch"]
    return next(b for b in ref.branches
                if b != story and any(r.origin == b and r.day == run["day"] + 1 for r in ref.runs))


def branch_without_runs(run) -> str:
    """The first seed branch with no run leaving today or tomorrow (Keep and markdowns are all it has)."""
    ref = refdata.get(run["dataset_version"])
    return next(b for b in ref.branches
                if not any(r.origin == b and r.day in (run["day"], run["day"] + 1) for r in ref.runs))


def lot_qty(run, lot_id) -> float:
    return world_lot(run, lot_id)["qty"]  # as the workspace reads it (api/overlay.py)


def executed_keep_case(run, units: float, expiry_day: int, branch: str | None = None,
                       case_id: str = "TEST-LOOK") -> dict:
    """A synthetic lot and an EXECUTED Keep case for it: the engine picks Keep for one unit, the source manager
    approves it through the workflow, then the lot holds `units` on the record and physically. Returns the case."""
    branch = branch or branch_with_a_run_tomorrow(run)
    base = story_lot(run)
    lot_id = f"{case_id}-LOT"
    own_series(run, branch, base["sku"])  # the test writes the series directly
    with db.tx() as conn:
        r = fresh(run["run_id"])
        db.run(conn, "INSERT INTO lots (run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, markdown_pct, "
                     "expiry_source, last_verified_day, verified_by) VALUES (:r, 'shelfshift', :l, :b, :s, 1, :e, "
                     ":seq, 0, :es, :d, 'COUNT')", r=r["run_id"], l=lot_id, b=branch, s=base["sku"], e=expiry_day,
               seq=base["received_seq"], es=base["expiry_source"], d=r["day"])
        db.run(conn, "INSERT INTO truth_lots (run_id, world, lot_id, qty) VALUES (:r, 'shelfshift', :l, 1)",
               r=r["run_id"], l=lot_id)
        db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, state, opened_day) VALUES "
                     "(:r, :c, :c, :l, :b, :s, 'OPEN', :d)", r=r["run_id"], c=case_id, l=lot_id, b=branch,
               s=base["sku"], d=r["day"])
        cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], case_id))
        rec = cases.active_rec(conn, cases.case_row(conn, r["run_id"], case_id))
    assert rec["result"]["recommended"] == "keep", "one unit with days to sell: the engine keeps it"
    v = version(run, case_id)
    owned_send(run, seat(branch), case_id, v)
    assert call(run, workflow.decide, seat(branch), case_id, v, "approve", None, None, shown(run, case_id)) == \
        {"state": "EXECUTED"}
    with db.tx() as conn:  # it sells slower than forecast: more is left than the Keep plan expected
        for table in ("lots", "truth_lots"):
            db.run(conn, f"UPDATE {table} SET qty = :q WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   q=float(units), r=run["run_id"], l=lot_id)
        return cases.case_row(conn, run["run_id"], case_id)


def at_risk_expiry(run) -> int:
    """An expiry whose ESL-02 window contains tomorrow and leaves a few selling days after it."""
    return run["day"] + 3


def test_an_executed_case_left_at_risk_gets_a_second_look(run):
    case = executed_keep_case(run, UNITS_AT_RISK, at_risk_expiry(run))
    ran = case["active_rec_version"]
    advance_once(run)
    r = fresh(run["run_id"])
    with db.tx() as conn:
        after = cases.case_row(conn, r["run_id"], case["case_id"])
        card = cases.detail(conn, r, case["case_id"])
        before = db.rows(conn, "SELECT role, decision FROM decisions WHERE run_id = :r AND case_id = :c "
                               "AND rec_version = :v", r=r["run_id"], c=case["case_id"], v=ran)
        log = db.rows(conn, "SELECT day, payload FROM activity_log WHERE run_id = :r AND case_id = :c "
                            "AND type = 'second_look'", r=r["run_id"], c=case["case_id"])
    assert (after["state"], after["look_round"]) == ("OPEN", after["round"]) and after["round"] >= 1
    assert after["active_rec_version"] not in (None, ran)  # a new version, from today's data
    assert card["suggested_key"] != "keep"
    assert "Second look" in [b["text"] for b in card["badges"]]
    assert card["decisions"] == {}  # the new version needs its own approval
    assert [(d["role"], d["decision"]) for d in before] == [("source", "approve")]  # the history stays
    assert [(l["day"], l["payload"]["units_left"]) for l in log] == [(r["day"], round(lot_qty(run, case["lot_id"]), 2))]


# --- SUS-19 after a careful Day 3: shipped units, a fresh approval, each trip paid once, rejection -------------------

def careful_day_then_second_look(run, monkeypatch) -> dict:
    """SUS-19 through `weekcheck.careful_day` on Day 3 (the count is confirmed from physical stock, then the warm-up
    autopilot sends, accepts, approves and prepares it), then one advance into a morning where it has stock at risk
    and an action to weigh. The seed's own run leaves again one day later (R-02 also leaves on Day 4, the user's
    second-look seed); it is asserted, never copied. Test setup only, derived from the case itself: the unshipped
    remainder raised on the record and physically by the quantity that shipped, and the destination's free storage on
    the later days raised by the totes the moved stock holds, since that STORAGE stays committed. Returns
    {plan, trip, case} of the round that ran."""
    ref = refdata.get(run["dataset_version"])
    db.in_tx(lambda conn: weekcheck.careful_day(conn, db.lock_run(conn, run["run_id"])))
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], STORY)
        plan = cases.parse_plan(cases.active_rec(conn, case)["result"]["recommended"])
        storage = db.rows(conn, "SELECT bucket, day, qty FROM reservations WHERE run_id = :r AND case_id = :c "
                                "AND kind = 'STORAGE' AND state = 'COMMITTED'", r=run["run_id"], c=STORY)
    assert case["state"] == "PREPARED" and plan.kind == TRANSFER, "D-15 hand-back: careful Day 3 moves SUS-19"
    trip = next(r for r in ref.runs if r.run_code == plan.run_code and r.dest == plan.dest and r.day == run["day"])
    assert any((r.run_code, r.origin, r.dest, r.day) == (trip.run_code, trip.origin, trip.dest, run["day"] + 1)
               for r in ref.runs), "the seed's run that SUS-19 took leaves again the next day"
    for s in storage:
        dest, zone = s["bucket"].split("|")
        if s["day"] > run["day"]:
            monkeypatch.setitem(ref.capacity, (dest, zone, s["day"]),
                                ref.capacity[(dest, zone, s["day"])] + int(round(float(s["qty"]))))
    with db.tx() as conn:
        for table in ("lots", "truth_lots"):
            db.run(conn, f"UPDATE {table} SET qty = qty + :q WHERE run_id = :r AND world = 'shelfshift' "
                         f"AND lot_id = :l", q=float(plan.qty), r=run["run_id"], l=case["lot_id"])
    advance_once(run)
    assert state(run, STORY) == "OPEN", "the setup gives SUS-19 stock at risk and a transfer to weigh"
    return {"plan": plan, "trip": trip, "case": case}


def test_a_second_look_never_counts_shipped_units_again(run, monkeypatch):
    ran = careful_day_then_second_look(run, monkeypatch)
    dest = ran["plan"].dest
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], STORY)
        inp = from_dict(cases.active_rec(conn, case)["engine_input"])
        record = db.one(conn, "SELECT qty FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                        r=run["run_id"], l=case["lot_id"])["qty"]
        arrived = db.rows(conn, "SELECT lot_id, branch, qty FROM lots WHERE run_id = :r AND world = 'shelfshift' "
                                "AND moved_from = :l AND qty > 0", r=run["run_id"], l=case["lot_id"])
        booked = {(r["kind"], r["state"]) for r in db.rows(
            conn, "SELECT kind, state FROM reservations WHERE run_id = :r AND case_id = :c AND round <= :n",
            r=run["run_id"], c=STORY, n=case["look_round"])}
        storage = db.one(conn, "SELECT sum(qty) AS q FROM reservations WHERE run_id = :r AND case_id = :c "
                               "AND kind = 'STORAGE' AND state = 'COMMITTED' AND day = :d",
                         r=run["run_id"], c=STORY, d=inp.today)["q"]
    assert case["look_round"] == case["round"] >= 1
    assert inp.case_lot().qty == record  # the stock left on the record, not Day 3's plan replayed
    assert not [u for lot_id, u in inp.bookings.lot_units if lot_id == case["lot_id"]]  # the set-aside left
    moved = [(l.lot_id, l.branch, l.qty) for l in inp.lots if l.lot_id.startswith(f"{STORY}>")]
    assert moved == [(a["lot_id"], a["branch"], a["qty"]) for a in arrived] and [m[1] for m in moved] == [dest]
    assert not [i for i in inp.inbound if i.inbound_id.startswith(f"{STORY}>")]  # never again as inbound
    assert {("SOURCE_LOT", "DONE"), ("TRUCK", "DONE"), ("STORAGE", "COMMITTED")} <= booked
    assert sum(t for b, d, t in inp.bookings.totes if b == dest and d == inp.today) >= storage > 0  # still takes space


def transfers(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                             "AND kind = 'TRANSFER' ORDER BY run_day", r=run["run_id"], c=case_id)


def transport_on(run, day) -> float:
    with db.tx() as conn:
        return float(db.one(conn, "SELECT coalesce(sum(transport), 0) AS t FROM outcomes WHERE run_id = :r "
                                  "AND world = 'shelfshift' AND day = :d", r=run["run_id"], d=day)["t"])


def test_a_second_look_needs_a_fresh_approval_and_pays_each_trip_once(run, monkeypatch):
    careful_day_then_second_look(run, monkeypatch)
    src = source_of(run, STORY)
    with pytest.raises(ApiError) as e:
        call(run, workflow.prepare, src, STORY)
    assert e.value.code == "wrong_state"  # nothing carries over from the round that ran
    v, plan = version(run, STORY), pick(run, STORY)
    owned_send(run, src, STORY, v)
    if plan.kind == TRANSFER:
        call(run, workflow.decide, seat(plan.dest), STORY, v, "accept", None, ack(run, STORY), shown(run, STORY))
    assert call(run, workflow.decide, src, STORY, v, "approve", None, ack(run, STORY), shown(run, STORY))["state"] == \
        "APPROVED"
    call(run, workflow.prepare, src, STORY)
    advance_once(run)
    with db.tx() as conn:
        assert weekcheck.trips_paid_once(conn, run["run_id"]) == []
    trips = transfers(run, STORY)
    assert [t["state"] for t in trips] == ["DONE"] * len(trips) and len({t["action_id"] for t in trips}) == len(trips)
    assert len({t["run_day"] for t in trips}) == len(trips)  # one trip a day: Day 3's and the second look's
    for day in sorted({t["run_day"] for t in trips}):  # every trip is charged on its own day, once
        with db.tx() as conn:
            shipped = {e["payload"]["lot_id"]: float(e["payload"]["shipped"]) for e in db.rows(
                conn, "SELECT payload FROM world_events WHERE run_id = :r AND world = 'shelfshift' AND day = :d "
                      "AND kind = 'SHORT_SHIPMENT'", r=run["run_id"], d=day)}
            due = sum(trip_cost(a["fixed_cost"], a["cancel_cost"], a["per_unit_cost"],
                                shipped.get(a["lot_id"], float(a["qty"])))[0]
                      for a in db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                                             "AND kind = 'TRANSFER' AND state = 'DONE' AND run_day = :d",
                                       r=run["run_id"], d=day))
        assert transport_on(run, day) == pytest.approx(due, abs=0.01)


def test_rejecting_a_second_look_keeps_what_the_first_plan_still_occupies(run, monkeypatch):
    ran = careful_day_then_second_look(run, monkeypatch)
    with db.tx() as conn:
        first = cases.case_row(conn, run["run_id"], STORY)["look_round"]
    src, v, plan = source_of(run, STORY), version(run, STORY), pick(run, STORY)
    owned_send(run, src, STORY, v)
    assert holds(run, STORY)  # the second look's own round holds what its plan needs
    call(run, workflow.decide, src, STORY, v, "reject", "Keep it where it is")
    assert state(run, STORY) == "REJECTED"
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT round, kind, bucket, state FROM reservations WHERE run_id = :r AND case_id = :c",
                       r=run["run_id"], c=STORY)
    storage = {(r["bucket"].split("|")[0], r["state"]) for r in rows if r["round"] <= first and r["kind"] == "STORAGE"}
    assert storage == {(ran["plan"].dest, "COMMITTED")}  # the moved stock still takes the space
    assert {r["state"] for r in rows if r["round"] > first} == {"RELEASED"}  # only the second look's round ends
    # WR-07: once the lot and the stock it moved are gone (record and physical), the earlier look's bookings end
    with db.tx() as conn:
        gone = [r["lot_id"] for r in db.rows(conn, "SELECT lot_id FROM lots WHERE run_id = :r AND world = 'shelfshift' "
                                                   "AND (lot_id = :l OR moved_from = :l)",
                                             r=run["run_id"], l=ran["case"]["lot_id"])]
        for table in ("lots", "truth_lots"):
            db.run(conn, f"UPDATE {table} SET qty = 0 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = ANY(:ls)",
                   r=run["run_id"], ls=gone)
    advance_once(run)
    with db.tx() as conn:
        ended = db.rows(conn, "SELECT round, state FROM reservations WHERE run_id = :r AND case_id = :c",
                        r=run["run_id"], c=STORY)
    assert state(run, STORY) == "REJECTED"
    assert {r["state"] for r in ended if r["round"] <= first} <= {"DONE", "RELEASED", "EXPIRED"}
    assert not [r for r in ended if r["state"] in ("HELD", "COMMITTED", "CONVERTED")]


def test_a_second_look_carries_no_adopted_assumption(run):
    case = executed_keep_case(run, UNITS_AT_RISK, at_risk_expiry(run))
    adopted = {"override": {"kind": "demand_multiplier", "branch": case["source_branch"], "day_from": run["day"],
                            "day_to": at_risk_expiry(run), "value": 1.0},
               "trip": None, "branch": case["source_branch"], "note": "test", "adopted_day": run["day"]}
    with db.tx() as conn:  # an assumption adopted for the look that ran, still in effect by its scope
        db.run(conn, "UPDATE cases SET adopted = CAST(:a AS jsonb) WHERE run_id = :r AND case_id = :c",
               a=db.js([adopted]), r=run["run_id"], c=case["case_id"])
    advance_once(run)
    with db.tx() as conn:
        after = cases.case_row(conn, run["run_id"], case["case_id"])
        rec = cases.active_rec(conn, after)
    assert after["state"] == "OPEN" and after["adopted"] == []  # the second look starts from the data
    assert rec["engine_input"]["overrides"] == [] and rec["overrides"] == []


# --- the opening rule: Keep waste, the engine's pick, the window and the states ------------------------------------

def second_look_logged(run, case_id) -> bool:
    with db.tx() as conn:
        return bool(db.rows(conn, "SELECT 1 FROM activity_log WHERE run_id = :r AND case_id = :c "
                                  "AND type = 'second_look'", r=run["run_id"], c=case_id))


def test_no_second_look_while_keeping_is_still_the_best_plan(run):
    branch = branch_without_runs(run)
    ref = refdata.get(run["dataset_version"])
    expiry = run["day"] + 1  # tomorrow is its expiry day: the schedule's deepest step is in force, no markdown left
    assert all(scheduled_pct(ref.schedules[story_lot(run)["sku"]], expiry, run["day"] + 1) >= pct
               for _, pct in ref.schedules[story_lot(run)["sku"]])
    case = executed_keep_case(run, UNITS_AT_RISK, expiry, branch=branch)
    advance_once(run)
    with db.tx() as conn:
        inp = inputs.build_input(conn, fresh(run["run_id"]), case["lot_id"], exclude_case=case["case_id"])
    _, waste = screen.keep_projection(effective(inp), branch)
    assert waste.get(case["lot_id"], 0.0) >= config.CASE_OPENING["min_waste_units"]  # still at risk ...
    assert state(run, case["case_id"]) == "EXECUTED"  # ... but keeping is still best: nobody is asked again
    assert not second_look_logged(run, case["case_id"])


@pytest.mark.parametrize("below, reopens", [(0.0, True), (0.001, False)])
def test_keep_waste_at_exactly_the_threshold_reopens_and_below_does_not(run, monkeypatch, below, reopens):
    case = executed_keep_case(run, UNITS_AT_RISK, at_risk_expiry(run))
    threshold = config.CASE_OPENING["min_waste_units"]
    real = screen.keep_projection

    def projection(eff, branch):  # the case lot's own Keep waste set to the boundary; everything else as computed
        start, waste = real(eff, branch)
        if eff.case_lot_id == case["lot_id"]:
            waste = {**waste, case["lot_id"]: threshold - below}
        return start, waste

    monkeypatch.setattr(screen, "keep_projection", projection)
    advance_once(run)
    with db.tx() as conn:
        rec = cases.active_rec(conn, cases.case_row(conn, run["run_id"], case["case_id"]))
        raw = inputs.build_input(conn, fresh(run["run_id"]), case["lot_id"], exclude_case=case["case_id"])
        _, result, _ = evaluate.run_engine(conn, raw)
    assert result["recommended"] != "keep"  # a non-keep pick is available either way
    assert state(run, case["case_id"]) == ("OPEN" if reopens else "EXECUTED")
    assert second_look_logged(run, case["case_id"]) is reopens
    assert (rec["rec_version"] != case["active_rec_version"]) is reopens


def test_no_second_look_outside_the_window(run, monkeypatch):
    ref = refdata.get(run["dataset_version"])
    schedule = ref.schedules[story_lot(run)["sku"]]
    days_before = config.CASE_OPENING["days_before_first_step"]
    expiry = run["day"] + 1 + max(k for k, _ in schedule) + days_before + 1  # the window starts the day after tomorrow
    assert screen.window(expiry, schedule, days_before)[0] == run["day"] + 2
    case = executed_keep_case(run, UNITS_AT_RISK, expiry)
    looked = []
    real = screen.keep_projection
    monkeypatch.setattr(screen, "keep_projection",
                        lambda eff, branch: (looked.append(eff.case_lot_id), real(eff, branch))[1])
    advance_once(run)
    assert case["lot_id"] not in looked  # never even weighed
    assert state(run, case["case_id"]) == "EXECUTED" and not second_look_logged(run, case["case_id"])


@pytest.mark.parametrize("final", ["REJECTED", "CLOSED"])
def test_rejected_and_closed_cases_get_no_second_look(run, final):
    case = executed_keep_case(run, UNITS_AT_RISK, at_risk_expiry(run))  # the tracer's case: it would reopen
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = :s WHERE run_id = :r AND case_id = :c", s=final, r=run["run_id"],
               c=case["case_id"])
    advance_once(run)
    assert state(run, case["case_id"]) == final
    assert not second_look_logged(run, case["case_id"])


def test_trips_paid_once_charges_a_failed_trip_its_cancel_cost_and_compares_to_the_cent(run):
    action = prepare_story(run)
    with db.tx() as conn:  # the lot is gone from the record and the shelf: the trip fails and pays its cancel cost
        for table in ("lots", "truth_lots"):
            db.run(conn, f"DELETE FROM {table} WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   r=run["run_id"], l=action["lot_id"])
    advance_once(run)
    with db.tx() as conn:
        assert weekcheck.trips_paid_once(conn, run["run_id"]) == []
        for nudge, problems in ((0.009, 0), (0.02, 1)):  # within a cent passes; more than a cent is reported
            db.run(conn, "UPDATE outcomes SET transport = transport + :x WHERE run_id = :r AND world = 'shelfshift' "
                         "AND day = :d AND branch = :b AND sku = :s", x=nudge, r=run["run_id"], d=action["run_day"],
                   b=action["origin"], s=action["sku"])
            assert len(weekcheck.trips_paid_once(conn, run["run_id"])) == problems
            db.run(conn, "UPDATE outcomes SET transport = transport - :x WHERE run_id = :r AND world = 'shelfshift' "
                         "AND day = :d AND branch = :b AND sku = :s", x=nudge, r=run["run_id"], d=action["run_day"],
                   b=action["origin"], s=action["sku"])


def test_no_route_imports_the_weekcheck_driver():
    """weekcheck reads physical stock; no other module in the api package may import it (FLOW-08; D-06)."""
    package = Path(api.__file__).resolve().parent
    importers = []
    for path in sorted(package.glob("*.py")):
        if path.name == "weekcheck.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
            if any(n.split(".")[-1] == "weekcheck" or ".weekcheck" in n for n in names):
                importers.append(path.name)
    assert importers == []
