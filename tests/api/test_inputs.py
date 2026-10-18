"""The engine input is the record, today's runs and the sandbox's own Policy (spec §6, §8 Recheck contract; IN-01).

Lots, branches and runs are found from the seed and its tags, never named: the story lot by `milk-story`, the phantom
lot through reference/scenarios.json, the story run as today's run from the story lot's branch to the phantom lot's
branch. Quantities a test inserts itself (a hold of 10 units, a delivery of 30) are test values, not seed values.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import sqlalchemy as sa

from api import advance, cases, config, db, evaluate, inputs, overlay, refdata, sandbox, screen
from engine import rec_version
from engine.adjust import validate
from engine.model import Bookings, Policy, Run
from engine.simulate import HorizonError
from tests.api.conftest import (by_tag, drop_runs, fresh, own_series, own_world, restore_the_designed_flag,
                                world_lot)


def story(run):
    """(the story case, its lot row, the phantom lot id, the phantom lot's branch, today's story run)."""
    case = by_tag(run, cases.STORY_TAG)
    lot = world_lot(run, case["lot_id"])
    phantom, branch = sandbox.phantom_lot()
    ref = refdata.get(run["dataset_version"])
    trip = next(r for r in ref.runs if r.origin == lot["branch"] and r.dest == branch and r.day == run["day"])
    return case, lot, phantom, branch, trip


def add_hold(run_id, case_id, kind, bucket, day, qty, state="HELD", minutes=60, round_=1):
    with db.tx() as conn:
        db.run(conn, "INSERT INTO reservations (run_id, res_id, case_id, round, kind, bucket, day, qty, state, "
                     "expires_at) VALUES (:r, :id, :c, :n, :k, :b, :d, :q, :s, :x)", r=run_id,
               id=f"{case_id}-{kind}-{day}-{round_}", c=case_id, n=round_, k=kind, b=bucket, d=day, q=qty, s=state,
               x=datetime.now(timezone.utc) + timedelta(minutes=minutes))


def story_input(run, exclude="story", run_id=None, overrides=()):
    case, lot, *_ = story(run)
    with db.tx() as conn:
        return inputs.build_input(conn, fresh(run_id or run["run_id"]), lot["lot_id"],
                                  exclude_case=case["case_id"] if exclude == "story" else exclude, overrides=overrides)


def test_the_input_holds_the_record_never_physical_stock(run):
    _, _, phantom, _, _ = story(run)
    record = world_lot(run, phantom)["qty"]  # as the workspace reads the record and physical stock (api/overlay.py)
    with db.tx() as conn:
        physical = overlay.physical(conn, fresh(run["run_id"]), "shelfshift", [phantom])[phantom]
    assert record > 0 and physical == 0  # the phantom lot: on the record, physically empty
    lot = next(l for l in story_input(run).lots if l.lot_id == phantom)
    assert lot.qty == record  # truth never enters an EngineInput


def test_only_todays_runs_leaving_the_source_are_offered(run):
    _, lot, _, _, trip = story(run)
    ref = refdata.get(run["dataset_version"])
    inp = story_input(run)
    today = [r for r in ref.runs if r.origin == lot["branch"] and r.day == run["day"]]
    assert list(inp.runs) == today and trip in inp.runs
    later = [r for r in ref.runs if r.origin == lot["branch"] and r.day != run["day"]]
    assert later and not set(later) & set(inp.runs)  # the source has runs on other days; none is offered (D-24)
    assert (inp.today, inp.decision_min) == (run["day"], ref.calendar["open_min"])


def test_other_cases_bookings_count_and_this_cases_own_do_not(run):
    case, lot, _, branch, _ = story(run)
    add_hold(run["run_id"], "OTHER", "DEMAND", f"{branch}|{lot['sku']}", run["day"], 10.0)
    add_hold(run["run_id"], case["case_id"], "DEMAND", f"{branch}|{lot['sku']}", run["day"] + 1, 99.0)
    assert story_input(run).bookings.demand == ((branch, run["day"], 10.0),)


def test_an_expired_hold_never_counts_even_before_cleanup(run):
    _, lot, _, branch, _ = story(run)
    add_hold(run["run_id"], "OTHER", "DEMAND", f"{branch}|{lot['sku']}", run["day"], 10.0, minutes=-1)
    assert story_input(run).bookings == Bookings()


def test_two_sandboxes_with_the_same_business_state_share_a_version(run):
    _, lot, _, branch, _ = story(run)
    with db.tx() as conn:
        other = sandbox.clone(conn, sandbox.active_template(conn))
    bucket = f"{branch}|{lot['sku']}"
    add_hold(run["run_id"], "OTHER-A", "DEMAND", bucket, run["day"], 10.0, minutes=30)
    add_hold(other, "OTHER-B", "DEMAND", bucket, run["day"], 10.0, minutes=50)  # another case and expiry, same state
    a, b = story_input(run), story_input(run, run_id=other)
    assert rec_version(a) == rec_version(b)
    add_hold(other, "OTHER-C", "DEMAND", bucket, run["day"] + 1, 1.0)  # one booking more, in one sandbox only
    assert rec_version(story_input(run, run_id=other)) != rec_version(a)
    assert rec_version(story_input(run)) == rec_version(a)


def test_a_committed_transfer_counts_once_as_inbound(run):
    _, lot, _, branch, trip = story(run)
    own_series(run, branch, lot["sku"])  # the test writes the delivery directly
    with db.tx() as conn:
        db.run(conn, "INSERT INTO inbound (run_id, world, inbound_id, branch, sku, qty, arrival_day, arrival_min, "
                     "expiry_day, received_seq, source_case_id, origin) VALUES (:r, 'shelfshift', :i, :b, :s, 30, :d, "
                     ":m, :e, 1000000, 'X', :o)", r=run["run_id"], i=f"X>{branch}", b=branch, s=lot["sku"],
               d=run["day"], m=trip.arrives_min, e=lot["expiry_day"], o=lot["branch"])
    add_hold(run["run_id"], "X", "DEMAND", f"{branch}|{lot['sku']}", run["day"], 30.0, state="CONVERTED")
    inp = story_input(run)
    assert [i.inbound_id for i in inp.inbound].count(f"X>{branch}") == 1
    assert inp.bookings.demand == ()  # the converted demand counts as the delivery, not again as a booking
    assert f"X>{branch}" not in [i.inbound_id for i in story_input(run, exclude="X").inbound]  # its own recheck


def test_the_policy_is_the_seed_overlaid_with_the_sandbox_settings(run):
    seeded = refdata.get(run["dataset_version"]).seed["policy"]
    assert story_input(run).policy == Policy(**seeded)
    changed = seeded["waste_price"] + 2.5
    with db.tx() as conn:
        db.run(conn, "INSERT INTO run_policy (run_id, field, value) VALUES (:r, 'waste_price', CAST(:v AS jsonb))",
               r=run["run_id"], v=db.js(changed))
    assert story_input(run).policy == Policy(**{**seeded, "waste_price": changed})


def test_the_products_schedule_is_passed(run):
    _, lot, _, _, _ = story(run)
    product = next(p for p in refdata.get(run["dataset_version"]).seed["products"] if p["sku"] == lot["sku"])
    schedule = tuple(tuple(step) for step in product["markdown_schedule"])
    assert schedule and story_input(run).markdown_schedule == schedule


def test_a_branch_without_an_outcome_row_is_left_out_of_yesterdays_sales(run):
    _, lot, _, branch, _ = story(run)
    inp = story_input(run)
    assert {b for b, _ in inp.sales_yesterday} == {lot["branch"], *(r.dest for r in inp.runs)}  # every branch has a row
    own_series(run, branch, lot["sku"])  # the test deletes the row directly
    with db.tx() as conn:
        db.run(conn, "DELETE FROM outcomes WHERE run_id = :r AND world = 'shelfshift' AND day = :d AND branch = :b "
                     "AND sku = :s", r=run["run_id"], d=run["day"] - 1, b=branch, s=lot["sku"])
    sales = dict(story_input(run).sales_yesterday)
    assert branch not in sales and lot["branch"] in sales  # unknown, never read as zero


def test_a_decimal_from_the_database_becomes_a_float_before_an_override(run):
    _, _, _, _, trip = story(run)
    with db.tx() as conn:
        quote = db.one(conn, "SELECT CAST(:v AS numeric) AS v", v=str(trip.fixed_cost))["v"]
    assert isinstance(quote, Decimal)
    inp = story_input(run, overrides=({"kind": "transport_quote", "run_code": trip.run_code, "fixed_cost": quote},))
    cost = inp.overrides[0]["fixed_cost"]
    assert type(cost) is float and cost == trip.fixed_cost
    validate(inp.overrides[0], inp)  # the engine's direct check needs a real float (contract-diff row 11)


def test_adopted_assumptions_end_with_their_trip_or_branch():
    def run_on(day):
        return Run("R", "S", "D", day, 720, 750, 800, 10, True, 10.0, 0.0)

    quote = {"override": {"kind": "transport_quote", "run_code": "R", "fixed_cost": 5.0},
             "trip": {"run_code": "R", "day": 3}, "branch": None, "note": "quote", "adopted_day": 3}
    demand = {"override": {"kind": "demand_multiplier", "branch": "D", "day_from": 3, "day_to": 5, "value": 1.2},
              "trip": None, "branch": "D", "note": "event", "adopted_day": 3}
    response = {"override": {"kind": "markdown_response", "m20": 1.3, "m30": 1.5}, "trip": None, "branch": None,
                "note": "test", "adopted_day": 3}
    adopted = [quote, demand, response]
    kept, ended = inputs.adopted_in_effect(adopted, [run_on(3)], {"S", "D"})
    assert kept == (quote["override"], demand["override"], response["override"]) and ended == []
    kept, ended = inputs.adopted_in_effect(adopted, [run_on(4)], {"S", "D"})  # day 4: R runs again, another trip
    assert kept == (demand["override"], response["override"]) and ended == [quote]
    kept, ended = inputs.adopted_in_effect(adopted, [], {"S"})  # no run and D not in today's input
    assert kept == (response["override"],) and ended == [quote, demand]


def test_the_template_names_a_broken_beat(template, monkeypatch):
    monkeypatch.setattr(sandbox, "outcome_problems", lambda conn, run: ["milk-story: a beat broke"])
    with db.tx() as conn:
        before = {r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE kind = 'TEMPLATE'")}
    with pytest.raises(sandbox.TemplateError, match="milk-story: a beat broke"):
        sandbox.build_template(fresh(template)["dataset_version"])
    with db.tx() as conn:
        new = db.one(conn, "SELECT * FROM demo_runs WHERE kind = 'TEMPLATE' AND NOT (run_id = ANY(:b))", b=list(before))
        assert sandbox.active_template(conn)["run_id"] == template  # the previous ACTIVE template stays
    assert (new["status"], new["failure"]) == ("FAILED", "milk-story: a beat broke")


def test_the_template_passes_the_d21_checks(run):
    with db.tx() as conn:
        assert sandbox.outcome_problems(conn, fresh(run["run_id"])) == []
    for tag in sandbox.REVIEWER_TAGS:
        assert by_tag(run, tag)["opened_day"] == run["day"]


def test_an_adopted_quote_ends_with_its_trip(run):
    """Carried round-3 CR-01: a quote adopted on the story run's day re-prices only that trip. After Next day the
    case re-evaluates without error, the new version carries no override for the old trip, and the detail lists the
    quote under adopted_ended."""
    from api import advance, workflow
    from tests.api.conftest import seat

    case, lot, _, _, trip = story(run)
    quote = {"kind": "transport_quote", "run_code": trip.run_code, "fixed_cost": trip.fixed_cost * 2}
    db.in_tx(lambda conn: workflow.adopt(conn, db.lock_run(conn, run["run_id"]), seat(lot["branch"]),
                                         case["case_id"], [quote], "carrier quote for this trip"))
    with db.tx() as conn:
        before = cases.active_rec(conn, cases.case_row(conn, run["run_id"], case["case_id"]))
    assert quote in before["engine_input"]["overrides"] and before["day"] == trip.day
    r = fresh(run["run_id"])
    db.in_tx(lambda conn: advance.advance_day(conn, r["run_id"], r["version"]))
    with db.tx() as conn:
        now = fresh(run["run_id"])
        row = cases.case_row(conn, run["run_id"], case["case_id"])
        after = cases.active_rec(conn, row)
        d = cases.detail(conn, now, case["case_id"])
    assert now["day"] == trip.day + 1 and row["reason"] is None  # re-evaluated, no missing data, no error
    assert after["day"] == now["day"] and after["rec_version"] != before["rec_version"]
    assert not any(o.get("kind") == "transport_quote" and o.get("run_code") == trip.run_code
                   for o in after["engine_input"]["overrides"])
    assert d["adopted"] == [] and [i["override"] for i in d["adopted_ended"]] == [quote]
    assert d["adopted_ended"][0]["trip"] == {"run_code": trip.run_code, "day": trip.day}


# ------------------------------------------------------------- the morning screen's shared reads (quick 261017-nhi)

SCREEN_WORLDS = ("shelfshift", "baseline_rule")  # the worlds a morning screen reads (open_cases, baseline_morning)

def _lots_read(conn, run, world):
    """Every lot id the run reads in `world` (overlay.everywhere), qty 0 lots included, in lot_id order."""
    read, p = overlay.everywhere(conn, run, "lots")
    return [r["lot_id"] for r in db.rows(conn, f"SELECT lot_id FROM lots WHERE {read} AND lots.world = :w "
                                               f"ORDER BY lot_id", w=world, **p)]


def _screened(inp):
    """The screen's result, or the HorizonError's message (both sides must agree on either)."""
    try:
        return screen.screen(inp, config.CASE_OPENING)
    except HorizonError as exc:
        return ("HorizonError", str(exc))


def _same_inputs(run_row, world):
    """For every lot the run reads in `world`: build_input with one ScreenReads equals build_input without (== and
    the same cache key), the screen agrees, and a gone lot is gone on both sides. Returns the uncached inputs."""
    out = []
    with db.tx() as conn:
        reads = inputs.ScreenReads(conn, run_row)
        for lot_id in _lots_read(conn, run_row, world):
            try:
                a = inputs.build_input(conn, run_row, lot_id, world=world)
            except LookupError:
                with pytest.raises(LookupError):
                    inputs.build_input(conn, run_row, lot_id, world=world, reads=reads)
                continue
            b = inputs.build_input(conn, run_row, lot_id, world=world, reads=reads)
            assert a == b, (world, lot_id)
            assert evaluate.cache_key(a) == evaluate.cache_key(b), (world, lot_id)
            assert _screened(a) == _screened(b), (world, lot_id)
            out.append(a)
    assert out, f"no lot compared in {world}"
    print(f"\nidentity {run_row['run_id'][:8]} {world}: {len(out)} lots compared")
    return out


def test_per_product_reads_match_the_per_series_reads(public_run):
    ref = refdata.get(public_run["dataset_version"])
    some_points = some_sales = False
    with db.tx():
        for day in sorted(ref.forecast_runs):
            for sku in sorted(ref.products):
                by_store = ref.forecast_of_product(day, sku)
                assert set(by_store) == {s for s in ref.branches if ref.forecast(day, s, sku)}, (day, sku)
                for store in ref.branches:
                    assert by_store.get(store, {}) == ref.forecast(day, store, sku), (day, store, sku)
                some_points = some_points or bool(by_store)
        assert ref.forecast_of_product(max(ref.forecast_runs) + 1000, sorted(ref.products)[0]) == {}  # no run that day
        for sku in sorted(ref.products):
            sold = ref.sales_of_product(sku, -1)
            for store in ref.branches:
                assert sold.get(store) == ref.sale(store, sku, -1), (store, sku)
            some_sales = some_sales or bool(sold)
            assert ref.sales_of_product(sku, 0) == {}  # a demo day is never answered, as `sale`
    assert some_points and some_sales


def test_screen_reads_match_on_the_public_day0_run(public_run):
    for world in SCREEN_WORLDS:
        compared = _same_inputs(public_run, world)
        assert any(i.forecasts for i in compared) and any(i.sales_yesterday for i in compared), world


def test_a_screen_reads_of_another_run_or_day_is_refused(public_run):
    with db.tx() as conn:
        reads = inputs.ScreenReads(conn, {**public_run, "day": public_run["day"] + 1})
        lot_id = _lots_read(conn, public_run, "shelfshift")[0]
        with pytest.raises(ValueError, match="one run on one day"):
            inputs.build_input(conn, public_run, lot_id, reads=reads)
        with pytest.raises(ValueError, match="record series table"):
            inputs.ScreenReads(conn, public_run).product("truth_lots", "shelfshift", "x")


def _statements(fn) -> int:
    """The number of statements `fn` sends (the test_publicref recorder)."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    sa.event.listen(db.engine(), "before_cursor_execute", record)
    try:
        fn()
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", record)
    return len(seen)


def _screen_all(conn, run, lots, world, reads=None):
    for lot in lots:
        _screened(inputs.build_input(conn, run, lot["lot_id"], world=world, reads=reads))


SCREENS = {"shelfshift": cases.screen_candidates, "baseline_rule": advance.baseline_candidates}


@pytest.mark.parametrize("world", sorted(SCREENS))
def test_the_cached_screen_reads_once_per_product(public_run, world):
    """The uncached screen first (it also warms PublicRef's run and capacity caches, and is counted), then the same
    lots with one fresh ScreenReads: at most 5 statements per product and a constant."""
    with db.tx() as conn:
        lots = SCREENS[world](conn, public_run)
        products = len({l["sku"] for l in lots})
        assert len(lots) > products > 0  # not vacuous: products hold several candidates
        uncached = _statements(lambda: _screen_all(conn, public_run, lots, world))
        reads = inputs.ScreenReads(conn, public_run)
        cached = _statements(lambda: _screen_all(conn, public_run, lots, world, reads))
    print(f"\nquery count {world}: {len(lots)} lots, {products} products, uncached {uncached}, cached {cached}")
    assert cached <= 5 * products + 8 and cached < uncached, (world, len(lots), products, uncached, cached)


class _Rollback(Exception):
    """Raised inside a db.tx() to roll it back: the module's run stays as it was."""


@pytest.mark.parametrize("module, morning", [("cases", "open_cases"), ("advance", "baseline_morning")])
def test_the_morning_screens_share_one_screen_reads(public_run, monkeypatch, module, morning):
    """open_cases and baseline_morning build every input of their screen with one ScreenReads of their own."""
    seen = []
    real = inputs.build_input

    def spy(*args, **kwargs):
        seen.append(kwargs.get("reads"))
        return real(*args, **kwargs)

    monkeypatch.setattr(inputs, "build_input", spy)
    with pytest.raises(_Rollback):
        with db.tx() as conn:
            getattr({"cases": cases, "advance": advance}[module], morning)(conn, public_run)
            raise _Rollback
    assert seen and all(isinstance(r, inputs.ScreenReads) for r in seen), seen[:3]
    assert len({id(r) for r in seen}) == 1


def test_screen_reads_match_on_the_synthetic_template(template):
    compared = {world: _same_inputs(fresh(template), world) for world in SCREEN_WORLDS}  # read only
    assert any(i.sales_yesterday for i in compared["shelfshift"])  # day 3: yesterday's outcome rows


def test_screen_reads_match_in_a_workspace_with_one_changed_series_then_every_series(run):
    case = by_tag(run, cases.STORY_TAG)
    lot = world_lot(run, case["lot_id"])
    branch, sku, day = lot["branch"], lot["sku"], run["day"]
    for world in SCREEN_WORLDS:  # the workspace owns the story series and its rows differ from the template's
        own_series(run, branch, sku, world)
        with db.tx() as conn:
            assert db.run(conn, "UPDATE lots SET qty = qty + 1 WHERE run_id = :r AND world = :w AND branch = :b "
                                "AND sku = :s AND qty > 0", r=run["run_id"], w=world, b=branch, s=sku) > 0
    counted = float(lot["qty"]) + 1
    add_hold(run["run_id"], "other-case", "DEMAND", f"{branch}|{sku}", day, 1)
    with db.tx() as conn:  # a confirmed count, as api/signals.py writes one
        db.run(conn, "INSERT INTO reported_facts (run_id, fact_id, signal_id, kind, adjustments, sku, valid_from, "
                     "valid_to, label, confirmed_by, confirmed_day) VALUES (:r, :f, :s, 'STOCK_COUNT', "
                     "CAST(:a AS jsonb), :sku, :d, :d, :lab, :by, :d)", r=run["run_id"], f="FACT-nhi", s="SIG-nhi",
               a=db.js([{"kind": "stock_count", "lot_id": lot["lot_id"], "units": counted, "counted_day": day}]),
               sku=sku, d=day, lab="test count", by=f"manager:{branch}")
    shelf = _same_inputs(fresh(run["run_id"]), "shelfshift")
    _same_inputs(fresh(run["run_id"]), "baseline_rule")
    assert any((branch, day, 1.0) in i.bookings.demand for i in shelf)
    assert any(i.reported_facts for i in shelf)
    story_input = next(i for i in shelf if i.case_lot_id == lot["lot_id"])
    assert next(l.qty for l in story_input.lots if l.lot_id == lot["lot_id"]) == counted  # the workspace's own row
    own_world(run)  # then every series, as Next day owns them
    for world in SCREEN_WORLDS:
        _same_inputs(fresh(run["run_id"]), world)


FLAG_COLUMNS = ("day, lot_id, key, branch, sku, a, b, keep_waste, stock_at_step, step_day, usable_runs, missing_data, "
                "tag, origin")


def _day0_public(conn, dataset_version):
    """A scratch day-0 TEMPLATE of the tiny public set, as the public_run fixture builds one: its demo_runs row."""
    run_id = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                 "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=dataset_version)
    sandbox.init_worlds(conn, run_id, refdata.get(dataset_version), refdata.truth(dataset_version))
    advance.day_start(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id))
    return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)


def test_open_cases_flags_the_same_lots_with_and_without_screen_reads(public_dv, monkeypatch):
    ids = []
    try:
        with db.tx() as conn:
            cached = _day0_public(conn, public_dv)
            ids.append(cached["run_id"])
            restore_the_designed_flag(conn, cached)  # test-local: the designed lot qualifies (quick 261017-ofn)
            assert cases.open_cases(conn, cached) == []
        monkeypatch.setattr(inputs, "ScreenReads", lambda conn, run: None)  # every input read as before the cache
        with db.tx() as conn:
            uncached = _day0_public(conn, public_dv)
            ids.append(uncached["run_id"])
            restore_the_designed_flag(conn, uncached)
            assert cases.open_cases(conn, uncached) == []
        with db.tx() as conn:
            flags = [db.rows(conn, f"SELECT {FLAG_COLUMNS} FROM case_flags WHERE run_id = :r ORDER BY lot_id", r=i)
                     for i in ids]
            numbers = [db.one(conn, "SELECT next_case_no FROM demo_runs WHERE run_id = :r", r=i)["next_case_no"]
                       for i in ids]
        assert flags[0] and flags[0] == flags[1]
        assert numbers[0] == numbers[1]
    finally:
        with db.tx() as conn:
            drop_runs(conn, ids)


def test_the_template_build_analyzes_before_the_day0_screen(template, monkeypatch):
    seen: list[str] = []
    copied: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    def stop(conn, run):
        copied.extend(seen)
        raise RuntimeError("stopped before the day-0 screen")

    monkeypatch.setattr(cases, "open_cases", stop)
    sa.event.listen(db.engine(), "before_cursor_execute", record)
    try:
        with pytest.raises(sandbox.TemplateError, match="stopped before the day-0 screen"):
            sandbox.build_template(fresh(template)["dataset_version"])
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", record)
    deletes = [i for i, s in enumerate(copied) if s.lstrip().startswith("DELETE FROM inbound")]
    assert deletes  # the day start ran
    for table in ("lots", "truth_lots", "inbound"):
        at = [i for i, s in enumerate(copied) if s.strip() == f"ANALYZE {table}"]
        assert at and min(at) > max(deletes), table
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template  # the FAILED row stays, the template is kept


# ------------------------------------------- scheduled deliveries count in an input built without a case (quick 261017-ofn)

def _scheduled(conn, run, world, sku):
    """The scheduled supplier deliveries (no source case) of `sku` the run reads in `world` (overlay.everywhere)."""
    read, p = overlay.everywhere(conn, run, "inbound")
    return db.rows(conn, f"SELECT inbound_id, branch, arrival_day FROM inbound WHERE {read} AND inbound.world = :w "
                         f"AND inbound.sku = :s AND source_case_id IS NULL ORDER BY inbound_id", w=world, s=sku, **p)


def test_the_day0_screen_input_counts_scheduled_deliveries(public_run):
    """A morning screen's input (exclude_case None), cached or not, holds every scheduled delivery of the lot's SKU at
    the input's branches from today to the lot's expiry, in both worlds, and the same inbound as a new case's input."""
    day = public_run["day"]
    with db.tx() as conn:
        for world, candidates in SCREENS.items():
            reads = inputs.ScreenReads(conn, public_run)
            some = False
            for lot in candidates(conn, public_run):
                uncached = inputs.build_input(conn, public_run, lot["lot_id"], world=world)
                cached = inputs.build_input(conn, public_run, lot["lot_id"], world=world, reads=reads)
                reach = {lot["branch"]} | {r.dest for r in uncached.runs}
                expected = {r["inbound_id"] for r in _scheduled(conn, public_run, world, lot["sku"])
                            if r["branch"] in reach and day <= r["arrival_day"] <= lot["expiry_day"]}
                for side, inp in (("uncached", uncached), ("cached", cached)):
                    held = {i.inbound_id for i in inp.inbound}
                    assert expected <= held, (world, side, lot["lot_id"], "missing", sorted(expected - held))
                new_case = inputs.build_input(conn, public_run, lot["lot_id"], world=world,
                                              exclude_case="ofn-no-such-case")
                assert uncached.inbound == new_case.inbound, (world, lot["lot_id"])
                some = some or bool(expected)
            assert some, f"no {world} candidate has a scheduled delivery in its window: the test would be vacuous"


def test_the_case_page_and_the_screen_agree_on_inbound(run):
    """For every case, the screen's input minus the case's own deliveries has the case page's inbound; a scheduled
    delivery written into the workspace's story series is in all three."""
    case = by_tag(run, cases.STORY_TAG)
    lot = world_lot(run, case["lot_id"])
    branch, sku = lot["branch"], lot["sku"]
    own_series(run, branch, sku)  # the test writes the delivery directly
    new_id = f"OFN>{branch}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO inbound (run_id, world, inbound_id, branch, sku, qty, arrival_day, arrival_min, "
                     "expiry_day, received_seq, source_case_id, origin) VALUES (:r, 'shelfshift', :i, :b, :s, 30, :d, "
                     "360, :e, 1000001, NULL, NULL)", r=run["run_id"], i=new_id, b=branch, s=sku,
               d=min(run["day"] + 1, lot["expiry_day"]), e=lot["expiry_day"])
    compared = 0
    with db.tx() as conn:
        t = fresh(run["run_id"])
        reads = inputs.ScreenReads(conn, t)
        read, p = overlay.everywhere(conn, t, "inbound")
        for row in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r ORDER BY case_id", r=t["run_id"]):
            builds = (lambda: cases.current_input(conn, t, row),
                      lambda: inputs.build_input(conn, t, row["lot_id"]),
                      lambda: inputs.build_input(conn, t, row["lot_id"], reads=reads))
            sides = []
            for build in builds:
                try:
                    sides.append(build())
                except LookupError:
                    sides.append(None)
            if None in sides:
                assert sides == [None, None, None], row["case_id"]
                continue
            page, screen_inp, cached = sides
            own = {r["inbound_id"] for r in db.rows(conn, f"SELECT inbound_id FROM inbound WHERE {read} "
                                                          f"AND inbound.world = 'shelfshift' AND source_case_id = :c",
                                                    c=row["case_id"], **p)}
            assert screen_inp.inbound == cached.inbound, row["case_id"]
            assert tuple(i for i in screen_inp.inbound if i.inbound_id not in own) == page.inbound, row["case_id"]
            compared += 1
            if row["case_id"] == case["case_id"]:
                for side, inp in zip(("page", "screen", "cached"), sides):
                    assert new_id in [i.inbound_id for i in inp.inbound], side
    assert compared, "no case compared"
