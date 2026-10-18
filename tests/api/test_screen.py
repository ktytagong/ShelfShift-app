"""The ESL-02 screen (03-CONTEXT D-22; 04-CONTEXT D-18, D-19): which lots get a case, and only those get a search.

The pure tests use the hand fixtures F1 and F6 (tests/engine/fixtures.py, never edited) with a markdown schedule set
by dataclasses.replace; every expected number below is derived by hand in its comment from the fixture's demand
(Bang Na 100 a day), the base response (m20 = 43/30, m30 = 1.6) and the schedule. The template tests find lots and
cases by tag; only SUS-19 has a stable key (D-23), and no seed number appears here.
"""
from dataclasses import replace

import pytest

from api import cases, config, db, evaluate, inputs, refdata, sandbox, screen
from data import config as refconfig
from engine import from_dict, rec_version
from engine.adjust import effective
from engine.model import KEEP, Lot, Plan
from engine.simulate import simulate
from tests.api.conftest import fresh, own_series, own_world, world_rows
from tests.engine.fixtures import BANG_NA, FRI, LAT_PHRAO, SAT, THU, f1, f6

A = ((2, 20), (1, 30))  # milk's schedule: 20% two days before expiry, 30% the day before
B = ((1, 20), (0, 30))  # the 3-day products' schedule: 20% the day before expiry, 30% on it
LOW = {"days_before_first_step": 1, "min_waste_units": 1, "min_discounted_totes": 1, "min_short_totes": 1}
NEVER = 10**9  # a threshold no fixture lot reaches, to look at one branch on its own


def th(**changes):
    return {**LOW, **changes}


# --- pure readings on hand inputs ---------------------------------------------------------------------------------

def test_reading_a1_uses_the_lots_own_waste_not_the_branchs():
    # Schedule A, F1 with the case lot cut to 50 units (expiry Sat) and a second Bang Na lot of 400 expiring Fri.
    # Thu: the Fri lot is at 30% (one day left), the Sat lot at 20%; 30% sells first: 100 x 1.6 = 160 from the Fri lot.
    # Fri: both at 30%; FEFO takes 160 more from the Fri lot (240 -> 80), which is wasted that night.
    # Sat: the case lot's 50 units sell at 30% (rate 160). The branch wastes 80; the case lot wastes nothing.
    inp = replace(f1(), markdown_schedule=A,
                  lots=(Lot("MLK-042", BANG_NA, 50.0, SAT, received_seq=1, last_verified_day=THU),
                        Lot("MLK-050", BANG_NA, 400.0, FRI, received_seq=0, last_verified_day=THU)))
    branch_waste = simulate(effective(inp), Plan(KEEP), "base", branches={BANG_NA}).wasted_by[BANG_NA]
    assert branch_waste == pytest.approx(80.0)
    found = screen.screen(inp, th(min_discounted_totes=NEVER))
    assert found["keep_waste"] == 0.0 and found["a"] is False  # the branch's 80 would have passed min_waste_units 1
    assert found["qualifies"] is False


def test_reading_a2_a_tote_is_units_per_tote_units_exactly():
    # Schedule B, F1: Thu is still full price (the first step is Fri), so 100 sell and Fri opens with 400 units,
    # exactly 40 totes of 10. Waste is ruled out (NEVER), so only branch (b) can qualify the lot.
    inp = replace(f1(), markdown_schedule=B)
    found = screen.screen(inp, th(min_waste_units=NEVER, min_discounted_totes=40))
    assert (found["step_day"], found["stock_at_step"]) == (FRI, 400.0)
    assert found["b"] is True and found["qualifies"] is True
    # a hair below 40 totes of units does not qualify: no rounding, no ceiling to whole totes
    short = replace(inp, lots=(replace(inp.lots[0], qty=500.0 - 1e-6),))
    found = screen.screen(short, th(min_waste_units=NEVER, min_discounted_totes=40))
    assert found["stock_at_step"] < 400.0 and found["b"] is False and found["qualifies"] is False
    # and the tote count is compared in units: 40 totes of 10 is 400 units, so 41 totes is not met by 400
    assert screen.screen(inp, th(min_waste_units=NEVER, min_discounted_totes=41))["b"] is False


def test_waste_compares_with_at_least():
    # No schedule, F1: 100 sell on each of Thu, Fri, Sat, so 500 - 300 = 200 are wasted after Sat.
    exactly = screen.screen(f1(), th(min_waste_units=200, min_discounted_totes=NEVER))
    assert exactly["keep_waste"] == 200.0 and exactly["a"] is True
    assert screen.screen(f1(), th(min_waste_units=200 + 1e-9, min_discounted_totes=NEVER))["a"] is False


def test_reading_a3_no_action_left_means_no_case():
    # Schedule A, F1 decided at 23:00: R-02's deadline (12:00 cutoff - 2 h packing = 10:00) has passed, and a markdown
    # decided now starts Fri, when the schedule is already at 30%, so both markdowns are covered. Only Keep is left.
    inp = replace(f1(), markdown_schedule=A, decision_min=23 * 60)
    found = screen.screen(inp, LOW)
    assert found["a"] is True  # Keep still wastes (36.67, as in the stepped test below)
    assert found["time_left"] is False and found["qualifies"] is False


def test_reading_a4_the_next_step_comes_from_the_schedule_alone():
    # The lot is recorded at 30% already on Thu, deeper than schedule A's 20% for a Sat expiry. From the schedule
    # alone the next deeper step is Fri (30% > 20%); from the lot's own 30% there would be none and the expiry day
    # (Sat) would be read. Thu sells 100 x 1.6 = 160 at 30%, so Fri opens with 500 - 160 = 340.
    inp = replace(f1(), markdown_schedule=A, lots=(replace(f1().lots[0], markdown_pct=30),))
    found = screen.screen(inp, LOW)
    assert found["step_day"] == FRI
    assert found["stock_at_step"] == pytest.approx(340.0)


def test_the_window_opens_the_day_before_the_first_step_and_ends_at_expiry():
    # Schedule A on a Sat (5) expiry: the first step is two days before, Thu (3); one day before that is Wed (2).
    first, last = screen.window(SAT, A, 1)
    assert (first, last) == (THU - 1, SAT)
    inside = lambda day: first <= day <= last  # noqa: E731
    assert inside(THU - 1) and inside(SAT)  # the opening day and the expiry day are screened
    assert not inside(THU - 2) and not inside(SAT + 1)  # the day before the window and the day after expiry are not
    # Schedule B on the same expiry: first step Fri (4), so the window is [Thu, Sat]
    assert screen.window(SAT, B, 1) == (THU, SAT)


def test_an_empty_schedule_is_refused():
    with pytest.raises(ValueError, match="no markdown schedule"):
        screen.window(SAT, (), 1)


@pytest.mark.parametrize("schedule", [A, B, ()])
@pytest.mark.parametrize("fixture", [f1, f6])
def test_the_stepped_keep_waste_matches_simulate(fixture, schedule):
    eff = effective(replace(fixture(), markdown_schedule=schedule))
    for branch in (BANG_NA, LAT_PHRAO):
        _, waste = screen.keep_projection(eff, branch)
        stepped = sum(w for lot_id, w in waste.items() if next(l for l in eff.lots if l.lot_id == lot_id).branch == branch)
        assert stepped == pytest.approx(simulate(eff, Plan(KEEP), "base", branches={branch}).wasted_by.get(branch, 0.0))
    # hand value for F1 under schedule A: Thu 20% sells 100 x 43/30, Fri and Sat 30% sell 160 each
    if fixture is f1 and schedule == A:
        _, waste = screen.keep_projection(eff, BANG_NA)
        assert waste["MLK-042"] == pytest.approx(500 - 100 * 43 / 30 - 160 - 160)


# --- on the Day-3 template ----------------------------------------------------------------------------------------

def lot_by_tag(run_id, tag):
    found = world_rows(run_id, "lots", "world = 'shelfshift' AND tag = :t", t=tag)  # as the workspace reads it
    return found[0] if found else None


def forget_cases(run_id):
    """Remove every case of a sandbox so the screen sees all its lots as never-cased; the counter restarts."""
    with db.tx() as conn:
        start = refdata.get(fresh(run_id)["dataset_version"]).calendar["case_no_start"]
        for table in ("cases", "recommendations"):
            db.run(conn, f"DELETE FROM {table} WHERE run_id = :r", r=run_id)
        db.run(conn, "UPDATE demo_runs SET next_case_no = :n WHERE run_id = :r", n=start, r=run_id)


def spy(monkeypatch, module, name):
    calls, real = [], getattr(module, name)

    def wrapper(*args, **kwargs):
        out = real(*args, **kwargs)
        calls.append((args, kwargs, out))
        return out

    monkeypatch.setattr(module, name, wrapper)
    return calls


def open_now(run_id):
    """The morning screen (cases.open_cases), a part of advance_day: it runs once the workspace owns every series."""
    own_world(run_id)
    with db.tx() as conn:
        return cases.open_cases(conn, fresh(run_id))


def test_sus19_qualifies_on_both_branches(run):
    story = lot_by_tag(run["run_id"], cases.STORY_TAG)
    with db.tx() as conn:
        found = screen.screen(inputs.build_input(conn, fresh(run["run_id"]), story["lot_id"]), config.CASE_OPENING)
        case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND lot_id = :l", r=run["run_id"], l=story["lot_id"])
        rec = cases.active_rec(conn, case)
        opened = db.one(conn, "SELECT * FROM activity_log WHERE run_id = :r AND case_id = :c AND type = 'opened'",
                        r=run["run_id"], c=case["case_id"])
    assert found["a"] is True and found["b"] is True and found["qualifies"] is True
    assert found["usable_runs"]  # a run today with no transfer_caps reasons reaches a short destination
    assert (case["key"], case["state"], case["opened_day"], case["tag"]) == (cases.STORY_KEY, "OPEN", run["day"],
                                                                             cases.STORY_TAG)
    assert opened["payload"]["a"] is True and opened["payload"]["b"] is True
    # the engine's recommendation is stored as a raw-input version: the stored input replays to the active version
    raw = from_dict(rec["engine_input"])
    assert rec_version(raw) == case["active_rec_version"] and not raw.applied_overrides
    assert rec["result"]["recommended"] and rec["day"] == run["day"]


def test_reading_a5_moved_lots_are_not_screened(run, monkeypatch):
    story = lot_by_tag(run["run_id"], cases.STORY_TAG)
    own_series(run, story["branch"], story["sku"])  # the test writes the series directly
    with db.tx() as conn:  # two copies of the story lot: one as if a delivered transfer created it, one plain
        for lot_id, moved_from in ((f"{story['lot_id']}>X", story["lot_id"]), (f"{story['lot_id']}-copy", None)):
            db.run(conn, "INSERT INTO lots (run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, "
                         "markdown_pct, expiry_source, last_verified_day, verified_by, origin, moved_from) VALUES "
                         "(:r, 'shelfshift', :l, :b, :s, :q, :e, :seq, :pct, :es, :lv, :vb, :o, :mf)",
                   r=run["run_id"], l=lot_id, b=story["branch"], s=story["sku"], q=story["qty"],
                   e=story["expiry_day"], seq=story["received_seq"] + 1, pct=story["markdown_pct"],
                   es=story["expiry_source"], lv=story["last_verified_day"], vb=story["verified_by"],
                   o="moved" if moved_from else "natural", mf=moved_from)
    screened = spy(monkeypatch, screen, "screen")
    open_now(run["run_id"])
    lots = {args[0].case_lot_id for args, _, _ in screened}
    assert f"{story['lot_id']}-copy" in lots
    assert f"{story['lot_id']}>X" not in lots


def test_a_non_qualifying_lot_never_reaches_the_search(run, monkeypatch):
    forget_cases(run["run_id"])
    screened = spy(monkeypatch, screen, "screen")
    stored = spy(monkeypatch, evaluate, "store")
    opened = open_now(run["run_id"])
    verdict = {args[0].case_lot_id: out["qualifies"] for args, _, out in screened}
    with db.tx() as conn:
        case_lot = {c["case_id"]: c["lot_id"] for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r",
                                                              r=run["run_id"])}
    searched = {case_lot[args[2]] for args, _, _ in stored}  # store(conn, run_id, case_id, day, inp)
    assert any(not q for q in verdict.values())  # the screen did turn some lot down
    assert searched == {lot for lot, q in verdict.items() if q}  # every search is for a qualifying lot
    assert set(case_lot) == set(opened) and set(case_lot.values()) == searched


def test_thresholds_come_from_reference_data(run, monkeypatch):
    assert config.CASE_OPENING == refconfig.load()["world"]["case_opening"]
    forget_cases(run["run_id"])
    monkeypatch.setattr(config, "CASE_OPENING", {**config.CASE_OPENING, "min_waste_units": NEVER,
                                                 "min_discounted_totes": NEVER})
    assert open_now(run["run_id"]) == []
    monkeypatch.undo()
    assert cases.STORY_KEY in open_now(run["run_id"])


def test_rejected_closed_and_executed_lots_are_never_screened_again(run, monkeypatch):
    """The sanctioned 07-12 rewrite (07-CONTEXT D-24, D-28; ESL-02's rule superseded for lots still at risk): a
    REJECTED lot (rejected through the route, so its fingerprint and considered record are stored) is looked at again
    but, re-screened the same morning with unchanged decision inputs and no D-28 reason, gets no new case; a CLOSED
    case whose lot is gone (closed_as null) is never screened; an EXECUTED case's lot is never screened here (it goes
    through its second look). Each lot keeps exactly one case."""
    from api import workflow
    from tests.api.conftest import seat
    from tests.api.test_workflow import call
    with db.tx() as conn:
        rejected = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = 'milk-story'", r=run["run_id"])
    call(run, workflow.reject, seat(rejected["source_branch"]), rejected["case_id"], "Not this week")
    ended = {"strawberry-cold-chain": "CLOSED", "bread-keep": "EXECUTED"}
    with db.tx() as conn:
        for tag, state in ended.items():
            db.run(conn, "UPDATE cases SET state = :s WHERE run_id = :r AND tag = :t", s=state, r=run["run_id"], t=tag)
    never = {lot_by_tag(run["run_id"], tag)["lot_id"] for tag in ended}
    screened = spy(monkeypatch, screen, "screen")
    open_now(run["run_id"])
    seen = {args[0].case_lot_id for args, _, _ in screened}
    assert not never & seen
    assert rejected["lot_id"] in seen  # looked at again, never hidden (D-24)
    with db.tx() as conn:
        for lot_id in never | {rejected["lot_id"]}:
            assert db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r AND lot_id = :l",
                          r=run["run_id"], l=lot_id)["n"] == 1


def expected_keys(start, n):
    out, k = [], start
    while len(out) < n:
        if f"SUS-{k}" != cases.STORY_KEY:
            out.append(f"SUS-{k}")
        k += 1
    return out


def test_qualifying_lots_open_in_a_fixed_order_and_sus19_keeps_its_key(run):
    with db.tx() as conn:
        other = sandbox.clone(conn, sandbox.active_template(conn))
    keyed = []
    for run_id in (run["run_id"], other):
        forget_cases(run_id)
        opened = open_now(run_id)
        with db.tx() as conn:
            rows = {c["case_id"]: c for c in db.rows(conn, "SELECT c.*, l.expiry_day FROM cases c JOIN lots l ON "
                                                           "l.run_id = c.run_id AND l.world = 'shelfshift' AND "
                                                           "l.lot_id = c.lot_id WHERE c.run_id = :r", r=run_id)}
        ref = refdata.get(fresh(run_id)["dataset_version"])
        order = {b: i for i, b in enumerate(ref.branches)}
        in_order = [rows[k] for k in opened]
        assert in_order == sorted(in_order, key=lambda c: (c["expiry_day"], order[c["source_branch"]], c["sku"],
                                                           c["lot_id"]))
        story = [c["case_id"] for c in in_order if c["tag"] == cases.STORY_TAG]
        assert story == [cases.STORY_KEY]
        rest = [c["case_id"] for c in in_order if c["tag"] != cases.STORY_TAG]
        assert rest == expected_keys(ref.calendar["case_no_start"], len(rest))
        keyed.append([(c["case_id"], c["lot_id"]) for c in in_order])
    assert keyed[0] == keyed[1]  # reproducible: the same lots get the same keys in another sandbox


def test_a_morning_with_no_lot_in_any_window_opens_nothing(run, monkeypatch):
    forget_cases(run["run_id"])
    own_world(run)  # the test writes every lot of the record directly
    with db.tx() as conn:  # every lot's window now starts weeks after today
        db.run(conn, "UPDATE lots SET expiry_day = expiry_day + 30 WHERE run_id = :r AND world = 'shelfshift'",
               r=run["run_id"])
    screened = spy(monkeypatch, screen, "screen")
    stored = spy(monkeypatch, evaluate, "store")
    assert open_now(run["run_id"]) == []
    assert screened == [] and stored == []
