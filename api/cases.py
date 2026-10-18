"""Cases: opening through the ESL-02 screen, re-evaluation, and closing a case whose lot is gone (spec §8 Case states).

A case opens only for a lot the screen qualifies (03-CONTEXT D-22): a shelfshift lot with stock on the record that
never had a case of any state, was not created by a delivered transfer, and whose screen window contains today.
Only a qualifying lot gets a full engine search; every other lot follows the markdown schedule with no case. A lot a
person closed (REJECTED, or CLOSED as CANCELLED) comes back only as a linked successor under the D-24/D-28 rule
(api/closing.py `reopen_allowed`: its decision inputs changed or, on the synthetic Next day, a materially different
decision appeared; 07-12, superseding ESL-02's "never screened again" for lots still at risk). A CLOSED case whose
lot is gone never comes back, and an EXECUTED case's lot goes through the second look instead. A lot has at most one
open case (the unique index), and SUS-19's key comes from the milk-story tag's first case, every other key (a
successor's included) from the run's counter starting at the calendar's `case_no_start` and skipping SUS-19, so
natural cases can never shift the story key (D-23).

Qualifying lots open in the order expiry day, seed branch order, sku, lot_id, so case keys are reproducible. Every
opened case logs the screen's branches, usable runs and step day in the same transaction (T-04-19).

The board and case-page views (04-API-CONTRACT "Shapes"; 04-CONTEXT D-16) read every engine figure as stored and
compute none: `decide_by`, `markdown_step` and `act_before` are returned as the engine gives them, and the deadline
`due` is `decide_by` itself, never recomputed (contract-diff row 43); with a selected plan in force, `decide_by` and
`due` are that plan's own, as the engine computes them and the selection stores them (api/selection.py; 07 review G). The engine's `wait.text` never leaves the
server in a card or a case detail (03-CONTEXT D-24): the `wait` object carries the run code, day and minute plus the
run's `departs_min` for Phase 5's sentence. Every label the server writes names the weekday from the run's own dataset
calendar (`Ref.date`: the synthetic seed's, or a public set's shifted calendar, 05.2 D-13), never "day N" (Carried
IN-02). A fact check's decision regret is a policy-scored amount, never
cash at stake: the `unverified` badge carries it with its two parts as numbers (null when every fact it counts is
not allowed: such a fact has no regret figure, 02-CONTEXT D-29) and the text "Unverified" (Carried 2026-10-13).
Money is a number in the reference currency (USD by default).
"""
from __future__ import annotations

import copy
import re
from dataclasses import replace
from datetime import date, datetime, timezone

from data.forecast import PROVIDER_ORDER

from engine.adjust import AdjustmentError, InputError, effective
from engine.model import KEEP, TRANSFER, Plan
from engine.simulate import HorizonError
from engine.timing import fmt_min

from . import (bookings, config, db, eligibility, evaluate, inputs, overlay, realtime, refdata, reviewflags, screen,
               signals, simplerule, stepkeys)
from .simplerule import parse_plan  # noqa: F401  (cases.parse_plan: the worker-safe home is api/simplerule.py)
from .errors import ApiError

TERMINAL = ("CLOSED", "REJECTED")
LIVE_STATES = ("OPEN", "WAITING", "PENDING_APPROVAL")  # re-evaluated when the inputs change
REEVALUATED = (*LIVE_STATES, "INVALIDATED")  # each morning; INVALIDATED returns to OPEN with re_review first
COLUMNS = {"OPEN": "New", "WAITING": "Waiting", "PENDING_APPROVAL": "Awaiting approval", "APPROVED": "Ready to dispatch",
           "PREPARED": "Ready to dispatch", "EXECUTED": "Tracking results", "OBSERVED": "Tracking results",
           "CLOSED": "Resolved", "REJECTED": "Resolved", "INVALIDATED": "New"}
STORY_TAG, STORY_KEY = "milk-story", "SUS-19"  # demo design ids (03-CONTEXT D-23), not retailer values
GONE_REASON = "Lot sold out or expired before any action"
NOT_CLOSED_HERE = (*TERMINAL, "EXECUTED", "OBSERVED")  # executed lots close through the lifecycle (04-08)
DUE_STATES = ("OPEN", "WAITING", "PENDING_APPROVAL", "APPROVED")  # a card shows its deadline only while it binds
DONE_STATES = ("EXECUTED", "OBSERVED", "CLOSED", "REJECTED")  # no unverified badge once the decision is behind us
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # calendar names, as the session's calendar writes them
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# a fact check's keys in the engine's order (engine/factchecks.py): jsonb keeps no key order, so the detail restores it
FACT_KEYS = ("fact_id", "kind", "lot_id", "lot_ids", "branch", "recorded_value", "alt_value", "regret", "decision_regret",
             "unserved_units", "net_revenue_difference", "waste_policy_difference", "waste_kg_difference",
             "best_plan_if_alt", "best_plan_if_alt_label", "pick_changed", "pick_feasible_if_alt", "changes_plan")
# a case's lot and every lot moved from it (shelfshift) have no stock left, on the record or physically (alias c)
LOT_GONE = ("NOT EXISTS (SELECT 1 FROM lots l LEFT JOIN truth_lots t USING (run_id, world, lot_id) "
            "WHERE l.run_id = c.run_id AND l.world = 'shelfshift' AND (l.lot_id = c.lot_id OR l.moved_from = c.lot_id) "
            "AND (l.qty > 0 OR coalesce(t.qty, 0) > 0))")
DAY_N = re.compile(r"\bday (-?\d+)\b")  # engine wording that names a day index


def case_row(conn, run_id: str, case_id: str, lock: bool = False) -> dict:
    c = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c" + (" FOR UPDATE" if lock else ""),
               r=run_id, c=case_id)
    if c is None:
        raise ApiError(404, "not_found", f"No case {case_id}")
    return c


def active_rec(conn, case: dict) -> dict | None:
    if not case["active_rec_version"]:
        return None
    return db.one(conn, "SELECT * FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                  r=case["run_id"], c=case["case_id"], v=case["active_rec_version"])


def current_input(conn, run: dict, case: dict, unconfirmed: inputs.Unconfirmed | None = None):
    """The case's raw input today: this case's own bookings left out, the adopted assumptions still in effect.

    Which adopted items are in effect is judged on the input's own runs (`inputs.case_runs`: today's runs, for a public
    set those that pass stage 1 for this lot), so the input is built first and the kept overrides set on it, exactly
    as `build_input` sets the overrides it is given. `unconfirmed` (the impact preview only, api/impact.py): the input
    this case's re-plan would read once that report is confirmed (07-15 review P1, P2)."""
    inp = inputs.build_input(conn, run, case["lot_id"], exclude_case=case["case_id"], unconfirmed=unconfirmed)
    kept, _ = inputs.adopted_in_effect(case["adopted"], inp.runs, {case["source_branch"], *(r.dest for r in inp.runs)})
    return replace(inp, overrides=tuple(inputs.plain(o) for o in kept))


def evaluate_case(conn, run: dict, case: dict, note: str | None = None) -> str | None:
    """A fresh raw input from current data plus the adopted assumptions in effect; a new version becomes active.

    A missing forecast or capacity day leaves the case without a version and with missing_data "missing data: ...",
    a column of its own: the manager's wait or reject reason in `reason` is never written here (WR-05). A version
    change on a PENDING_APPROVAL case cancels its approval: its holds and bookings are released, the change is
    logged as approval_cancelled, and the case returns to OPEN (spec §8; 04-CONTEXT D-09). A new version clears the
    case's selection (api/selection.py; 07-CONTEXT D-23, research Pitfall 11); on the same version a selection stays
    the plan in force, and the board's `plan_dest` follows it."""
    from . import selection  # it reads this module at import
    try:
        inp = current_input(conn, run, case)
        version, result, _ = evaluate.store(conn, run["run_id"], case["case_id"], run["day"], inp, note=note)
        missing = None
    except HorizonError as exc:
        version, result, missing = None, None, f"missing data: {exc}"
    plan = parse_plan(result["recommended"]) if result else None
    kept = selection.bound(case) if version == case["active_rec_version"] else None
    if kept is not None:  # the same version: the selection stays in force (D-23)
        plan = parse_plan(kept["plan_key"])
    dest = plan.dest if plan is not None and plan.kind == TRANSFER else None  # the board's scope (05.2 D-26)
    if version == case["active_rec_version"] and missing == case["missing_data"]:
        if dest != case.get("plan_dest"):
            db.run(conn, "UPDATE cases SET plan_dest = :p WHERE run_id = :r AND case_id = :c", p=dest,
                   r=run["run_id"], c=case["case_id"])
        return version
    cancelled = version != case["active_rec_version"] and case["state"] == "PENDING_APPROVAL"
    if cancelled:
        bookings.release(conn, run["run_id"], case["case_id"])
        db.log(conn, run["run_id"], run["day"], "approval_cancelled", case["case_id"], round=case["round"],
               rec_version=case["active_rec_version"], reason="new version")
    db.run(conn, "UPDATE cases SET active_rec_version = :v, missing_data = :miss, hold_expired = false, plan_dest = :p, "
                 "state = CASE WHEN :cancel THEN 'OPEN' ELSE state END WHERE run_id = :r AND case_id = :c",
           v=version, miss=missing, p=dest, cancel=cancelled, r=run["run_id"], c=case["case_id"])
    if version != case["active_rec_version"]:
        selection.clear(conn, run["run_id"], case["case_id"])  # a selection belongs to its version (Pitfall 11)
        db.log(conn, run["run_id"], run["day"], "new_version", case["case_id"], rec_version=version, reason=missing)
    return version


def next_key(conn, run: dict, lot: dict, successor: bool = False) -> str:
    """SUS-19 for the milk-story lot's first case; every other case the run's counter, skipping SUS-19 (D-23). A
    successor always takes the counter (07-12, research F-19: the story lot's successor would collide on SUS-19)."""
    if lot["tag"] == STORY_TAG and not successor:
        return STORY_KEY
    while True:
        n = db.one(conn, "UPDATE demo_runs SET next_case_no = next_case_no + 1 WHERE run_id = :r "
                         "RETURNING next_case_no - 1 AS n", r=run["run_id"])["n"]
        if f"SUS-{n}" != STORY_KEY:
            return f"SUS-{n}"


def next_keys(conn, run: dict, lots: list[dict]) -> list[str]:
    """next_key for each lot, in order, with the same rules: the milk-story lot gets SUS-19 and uses no number; every
    other lot takes the run's next number, and a number whose key is SUS-19 is used up and skipped (D-23). One read
    of the counter FOR UPDATE and one write of it, instead of one UPDATE per key (quick 261017-p52)."""
    if all(lot["tag"] == STORY_TAG for lot in lots):
        return [STORY_KEY for _ in lots]
    start = db.one(conn, "SELECT next_case_no FROM demo_runs WHERE run_id = :r FOR UPDATE",
                   r=run["run_id"])["next_case_no"]
    n, keys = start, []
    for lot in lots:
        if lot["tag"] == STORY_TAG:
            keys.append(STORY_KEY)
            continue
        while f"SUS-{n}" == STORY_KEY:
            n += 1
        keys.append(f"SUS-{n}")
        n += 1
    db.run(conn, "UPDATE demo_runs SET next_case_no = :n WHERE run_id = :r", n=n, r=run["run_id"])
    return keys


def screen_candidates(conn, run: dict) -> list[dict]:
    """Shelfshift lots with stock on the record, never moved, whose window contains today, that never had a case or
    whose latest case a person closed (REJECTED, or CLOSED as CANCELLED) and may come back; in the order expiry day,
    seed branch order, sku, lot_id.

    A closed lot is a candidate only when `closing.reopen_allowed` gives reasons (07-CONTEXT D-24, D-28): on the
    synthetic set with `next_day` (the Next-day morning is the only caller that counts the D-28 thresholds), and the
    call merges the morning's state into the closed case's considered record when the lot qualifies; on a public set
    through `flags.reopens` (no thresholds: product mode reassesses on new data, confirmed evidence or Reassess). Such
    a lot carries `_prev` (the closed case), `_why` (the reasons), `_state` and `_screen` (the screen it already ran).
    A closed lot the check judged and let stay closed has its stored lot_status refreshed (`closing.keep_status`, as
    `closing.reassess_lots` does), so "Needs decision" and Reassess clear once it no longer qualifies (07-12 review
    R1). A lot with any other case (open, committed, executed, or closed because its lot was gone) is never a candidate:
    an executed lot goes through the second look."""
    from . import closing, flags  # they read this module at import
    ref = refdata.get(run["dataset_version"])
    before = config.CASE_OPENING["days_before_first_step"]
    lots = db.rows(conn, "SELECT l.* FROM lots l WHERE l.run_id = :r AND l.world = 'shelfshift' AND l.qty > 0 "
                         "AND l.moved_from IS NULL AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.run_id = l.run_id "
                         "AND c.lot_id = l.lot_id)", r=run["run_id"])
    closed = db.rows(conn, f"SELECT l.*, c.case_id AS prev_case FROM lots l JOIN cases c ON c.run_id = l.run_id AND "
                           f"c.lot_id = l.lot_id WHERE l.run_id = :r AND l.world = 'shelfshift' AND l.qty > 0 AND "
                           f"l.moved_from IS NULL AND {closing.CLOSED_BY_PERSON} AND NOT EXISTS (SELECT 1 FROM cases s "
                           f"WHERE s.run_id = c.run_id AND s.predecessor = c.case_id) AND NOT EXISTS (SELECT 1 FROM "
                           f"cases o WHERE o.run_id = c.run_id AND o.lot_id = c.lot_id AND o.case_id <> c.case_id "
                           f"AND o.state NOT IN ('CLOSED', 'REJECTED'))", r=run["run_id"])
    order = {b: i for i, b in enumerate(ref.branches)}
    inside = []
    for lot in [*lots, *closed]:
        first, last = screen.window(lot["expiry_day"], ref.schedules[lot["sku"]], before)
        if not first <= run["day"] <= last:
            continue
        prev_case = lot.pop("prev_case", None)
        if prev_case is not None:
            prev, seen = case_row(conn, run["run_id"], prev_case), {}
            why = (flags.reopens(conn, run, prev, lot, seen) if ref.kind == "public" else
                   closing.reopen_allowed(conn, run, prev, lot, next_day=True, seen=seen))
            if not why:  # its stored status follows what the screen judged (07-12 review R1)
                closing.keep_status(conn, run, prev, seen)
                continue
            lot.update(_prev=prev, _why=why, _state=seen["state"], _screen=seen["screen"])
        inside.append(lot)
    return sorted(inside, key=lambda l: (l["expiry_day"], order[l["branch"]], l["sku"], l["lot_id"]))


def open_cases(conn, run: dict, *, eager=None) -> list[str]:
    """Screen every candidate lot and open a case, with a full search, for each one that qualifies.

    On a public set whose reference settings say `evaluation.on_demand`, each qualifying lot is flagged instead
    (api/flags.py: the screen's figures, no search; 05.2 D-23..D-25); the engine runs when someone opens it. A lot the
    screen cannot judge for lack of data is flagged with its reason, never skipped (D-28). A lot a person closed that
    `screen_candidates` lets back (07-CONTEXT D-24, D-28) opens through `closing.open_successor` on either set: a
    counter key, its predecessor, the reasons, the morning's state and the trigger ("next_day" on the synthetic set)
    logged. Returns the opened case ids (no new lot's when the set flags).

    `eager=True` (the benchmark only, 08-CONTEXT D-07: every at-risk lot) opens and evaluates every qualifying lot
    even on an on-demand set; the default None keeps the set's own rule. No route passes it."""
    from . import flags  # flags reads this module's views
    ref = refdata.get(run["dataset_version"])
    deferred = not eager and flags.on_demand(ref)
    from . import closing  # it reads this module at import
    opened, flagged = [], []
    reads = inputs.ScreenReads(conn, run)  # safe: this loop writes nothing build_input reads (inputs.ScreenReads)
    for lot in screen_candidates(conn, run):
        if lot.get("_prev") is not None:  # a lot a person closed comes back as a linked successor (D-24, D-28)
            key = closing.open_successor(conn, run, lot["_prev"], lot, why=lot["_why"], state=lot["_state"],
                                         trigger="screen" if ref.kind == "public" else "next_day",
                                         found=lot["_screen"])  # its opened entry logs the screen (07-12 review R2)
            if key is not None:
                opened.append(key)
            continue
        try:
            found = screen.screen(inputs.build_input(conn, run, lot["lot_id"], reads=reads), config.CASE_OPENING)
        except HorizonError as exc:  # never a silent skip: the case opens with a "missing data" badge
            found = {"qualifies": True, "a": None, "b": None, "usable_runs": [], "step_day": None,
                     "keep_waste": None, "stock_at_step": None, "missing": f"missing data: {exc}"}
        if not found["qualifies"]:
            continue
        if deferred:
            flagged.append((lot, found))
            continue
        key = next_key(conn, run, lot)
        db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, tag, state, opened_day, "
                     "missing_data) VALUES (:r, :c, :c, :l, :b, :s, :t, 'OPEN', :d, :why)", r=run["run_id"], c=key,
               l=lot["lot_id"], b=lot["branch"], s=lot["sku"], t=lot["tag"], d=run["day"], why=found.get("missing"))
        db.log(conn, run["run_id"], run["day"], "opened", key, lot_id=lot["lot_id"], a=found["a"], b=found["b"],
               usable_runs=found["usable_runs"], step_day=found["step_day"], reason=found.get("missing"))
        evaluate_case(conn, run, case_row(conn, run["run_id"], key))
        opened.append(key)
    if flagged:
        flags.write_flags(conn, run, flagged)
    return opened


def second_looks(conn, run: dict) -> list[str]:
    """A plan that ran can still leave stock at risk: a kept lot that sells slower than forecast, or the rest held
    after a transfer (spec §8 Second look; FLOW-09). Each morning, after the re-evaluation and before the screen opens
    new cases, every EXECUTED case whose lot still has stock on the record and whose ESL-02 window contains today (the
    screen's own `screen.window`, 03-CONTEXT D-22) is looked at again, in case_id order. REJECTED and CLOSED cases
    never are.

    The look starts from the data: every round so far becomes an earlier look (look_round = round), so what those
    rounds still occupy (the moved stock's storage) stays booked and counts against the new round, while shipped units
    are off the record and appear once, as the moved lot at the destination. Adopted assumptions are not carried: the
    input is built fresh from the record. The case reopens only when the lot's own Keep waste (the screen's
    `keep_projection`, reading A1) is at least `min_waste_units` and the engine's pick is not keep; otherwise nobody
    is asked and the case stays EXECUTED with its adopted list as it ran. A reopened case gets a new version, no
    decisions in its new round, and a second_look entry with the units left on the record. A day with no forecast
    skips the case. Earlier versions, decisions, bookings and activity stay as they were. Returns the reopened ids."""
    ref = refdata.get(run["dataset_version"])
    before = config.CASE_OPENING["days_before_first_step"]
    reopened = []
    for case in db.rows(conn, "SELECT c.*, l.qty, l.expiry_day FROM cases c JOIN lots l ON l.run_id = c.run_id "
                              "AND l.world = 'shelfshift' AND l.lot_id = c.lot_id WHERE c.run_id = :r "
                              "AND c.state = 'EXECUTED' AND l.qty > 0 ORDER BY c.case_id", r=run["run_id"]):
        first, last = screen.window(case["expiry_day"], ref.schedules[case["sku"]], before)
        if not first <= run["day"] <= last:
            continue
        # every round so far belongs to looks that ran: what they still occupy counts as booked (inputs.bookings)
        db.run(conn, "UPDATE cases SET look_round = round WHERE run_id = :r AND case_id = :c",
               r=run["run_id"], c=case["case_id"])
        try:
            inp = inputs.build_input(conn, run, case["lot_id"], exclude_case=case["case_id"])  # no adopted overrides
            _, waste = screen.keep_projection(effective(inp), case["source_branch"])
            if waste.get(case["lot_id"], 0.0) < config.CASE_OPENING["min_waste_units"]:
                continue
            _, result, _ = evaluate.run_engine(conn, inp)
        except HorizonError:
            continue  # nothing to weigh without the data
        if result["recommended"] == "keep":
            continue  # at risk, but keeping is still the best plan: nobody needs to look again
        db.run(conn, "UPDATE cases SET state = 'OPEN', adopted = '[]', selected = NULL WHERE run_id = :r "
                     "AND case_id = :c", r=run["run_id"], c=case["case_id"])  # the plan that ran is no choice now
        db.log(conn, run["run_id"], run["day"], "second_look", case["case_id"], lot_id=case["lot_id"],
               units_left=round(float(case["qty"]), 2), look_round=case["round"])
        evaluate_case(conn, run, case_row(conn, run["run_id"], case["case_id"]))
        reopened.append(case["case_id"])
    return reopened


def close_gone(conn, run: dict) -> list[str]:
    """A case that never executed (not resolved, executed or observed) whose lot and moved lots have no stock left,
    on the record or physically, is CLOSED with a log entry, and everything its current look held or booked is
    released. After a second look, the earlier looks' bookings (what the plan that already ran still occupies) end
    DONE, as on the observed path, since the lot and moved lots they served are gone. An executed case closes through
    the lifecycle instead (advance._lifecycle_after: observed, then closed). A case with a DISPATCHED work order is
    never closed here: a dispatch can leave the source lot at zero while the stock is on the truck and the moved lot
    does not exist until someone records the receipt (06.1 D-34; 06.1-09 deferred item 1)."""
    closed = []
    for case in db.rows(conn, f"SELECT * FROM cases c WHERE c.run_id = :r AND c.state <> ALL(:s) AND {LOT_GONE} "
                              f"AND NOT EXISTS (SELECT 1 FROM world_actions a WHERE a.run_id = c.run_id "
                              f"AND a.case_id = c.case_id AND a.state = 'DISPATCHED') "
                              f"ORDER BY c.case_id", r=run["run_id"], s=list(NOT_CLOSED_HERE)):
        bookings.release(conn, run["run_id"], case["case_id"])  # rounds after look_round: RELEASED
        bookings.done(conn, run["run_id"], case["case_id"])  # rounds up to look_round (earlier looks): DONE
        db.run(conn, "UPDATE cases SET state = 'CLOSED', reason = :why WHERE run_id = :r AND case_id = :c",
               why=GONE_REASON, r=run["run_id"], c=case["case_id"])
        db.log(conn, run["run_id"], run["day"], "closed", case["case_id"], lot_id=case["lot_id"], reason=GONE_REASON,
               from_state=case["state"])
        closed.append(case["case_id"])
    return closed



# --- views: the board, the card and the case detail (04-API-CONTRACT "Shapes") ------------------------------------

def weekday(day: int, ref: refdata.Ref) -> str:
    """The day's weekday ("Thu") in the run's dataset calendar (`ref.date`, 05.2 D-13)."""
    return WEEKDAYS[ref.date(day).weekday()]


def date_label(day: int, ref: refdata.Ref) -> str:
    """The day as "Thu 15 Oct", from the run's dataset calendar (`ref.date`, 05.2 D-13)."""
    d = ref.date(day)
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
BAD_DATE = "Give the date as YYYY-MM-DD"


def day_of(run: dict, iso_date) -> int:
    """A business date "YYYY-MM-DD" in the run's dataset calendar as its day index (07-CONTEXT D-21: printed expiry
    dates and report validity dates are business dates): the one conversion in, with `iso_of` the one out. Anything
    but a real calendar date in that exact form is 400 bad_request "Give the date as YYYY-MM-DD"."""
    if not isinstance(iso_date, str) or not ISO_DATE.fullmatch(iso_date):
        raise ApiError(400, "bad_request", BAD_DATE)
    try:
        d = date.fromisoformat(iso_date)
    except ValueError:
        raise ApiError(400, "bad_request", BAD_DATE) from None
    return (d - refdata.get(run["dataset_version"]).date(0)).days


def iso_of(run: dict, day: int) -> str:
    """A day index as its business date "YYYY-MM-DD" in the run's dataset calendar (`Ref.date`, 05.2 D-13)."""
    return refdata.get(run["dataset_version"]).date(day).isoformat()


def due(result: dict | None, ref: refdata.Ref) -> dict | None:
    """{day, min, date, weekday, label} from the engine's decide_by as returned; None when the engine gives none.

    The label always names the weekday, today included ("Decide by Thu 10:00"), never "day N" (04-API-CONTRACT
    Conventions; D-16). Nothing here recomputes the deadline (contract-diff row 43)."""
    by = (result or {}).get("decide_by")
    if not by:
        return None
    day, minute = by["day"], by["latest_decision_min"]
    return {"day": day, "min": minute, "date": ref.date(day).isoformat(), "weekday": weekday(day, ref),
            "label": f"Decide by {weekday(day, ref)} {fmt_min(minute)}"}


def wait_view(result: dict | None, stored_input: dict | None) -> dict | None:
    """The engine's wait without its text, plus the named run's departs_min from the stored input's runs."""
    wait = (result or {}).get("wait")
    if not wait:
        return None
    run = next((r for r in (stored_input or {}).get("runs", ()) if r["run_code"] == wait["run_code"]
                and r["day"] == wait["day"]), None)
    run = run or next((r for r in (stored_input or {}).get("runs", ()) if r["run_code"] == wait["run_code"]), None)
    return {"run_code": wait["run_code"], "day": wait["day"], "latest_decision_min": wait["latest_decision_min"],
            "departs_min": run["departs_min"] if run else None}


def without_wait_text(result: dict | None) -> dict | None:
    """A copy of the stored result with wait.text removed; the export alone keeps it (03-CONTEXT D-24)."""
    if result is None:
        return None
    out = copy.deepcopy(result)
    if out.get("wait"):
        out["wait"].pop("text", None)
    return out


def in_engine_order(check: dict) -> dict:
    """A stored fact check with its keys in the engine's order; every key kept, nothing truncated."""
    return {**{k: check[k] for k in FACT_KEYS if k in check}, **{k: v for k, v in check.items() if k not in FACT_KEYS}}


def money(ref: refdata.Ref, amount: float) -> str:
    return f"{ref.currency} {amount:,.2f}"


def summary(result: dict | None, checks: list[dict], ref: refdata.Ref, *, waste_price: float,
            state: str | None = None, selected: dict | None = None) -> str:
    """The deterministic template summary (spec §7), the fallback whenever Gemini is not available.

    It starts "The engine recommends", gives amounts in the reference currency, never quotes the engine's wait
    wording and names a day by its weekday. A plan-changing fact under which the recommended plan is not allowed
    (pick_feasible_if_alt false) reads "If this fact is true, the recommended plan is not allowed." with no regret
    figure (spec §6; 05-CONTEXT D-16); every other plan-changing fact reads as a decision regret with its net-revenue
    and waste-policy parts kept separate, never as cash at stake. At a waste price of 0 (the price of the input the
    result ran on) the waste-policy part is left out (PROJECT.md: at $0 the second line is absent); above 0 the two
    parts stay separate. A REJECTED or CLOSED case (`state`) gets no "Unverified: ..." sentence: the decision is
    behind us, so no fact can change it any more (06.1 D-36). With a selection in force (`selected`, 07-CONTEXT D-23)
    a sentence "Selected plan · {option}: ..." follows the engine's own, `checks` are the selected plan's, and a
    not-allowed fact reads "the selected plan is not allowed"."""
    if not result:
        return "No recommendation: data is missing for this case."
    rows = {r["key"]: r for r in result["options"]}
    best = rows[result["recommended"]]
    wins = result["scenario_wins"]["wins"]
    gain = "" if best["kind"] == KEEP else f", {money(ref, best['delta_vs_keep'])} more than keeping"
    parts = [f"The engine recommends {best['label']}: expected net {money(ref, best['net']['base'])} in the base "
             f"scenario{gain}, with {best['wasted_units']:,.0f} units wasted. It wins "
             f"{'all 3' if wins == 3 else f'{wins} of 3'} tested scenarios."]
    if result["rejected"]:
        reasons = sorted({r for x in result["rejected"] for r in x["reasons"]})
        parts.append(f"{len(result['rejected'])} option(s) rejected by a limit: {', '.join(reasons)}.")
    which = "recommended"
    if selected is not None:  # the source manager's choice among the engine's options (D-23)
        row = rows[selected["plan_key"]]
        more = "" if row["kind"] == KEEP else f", {money(ref, row['delta_vs_keep'])} more than keeping"
        parts.append(f"Selected plan · {row['label']}: expected net {money(ref, row['net']['base'])} in the base "
                     f"scenario{more}, with {row['wasted_units']:,.0f} units wasted.")
        which = "selected"
    for c in ([] if state in TERMINAL else checks):
        if c["changes_plan"]:
            fact = f"Unverified: {c['branch']} {', '.join(c['lot_ids'])} ({c['kind'].replace('_', ' ').lower()})"
            if not c["pick_feasible_if_alt"]:
                parts.append(f"{fact}. If this fact is true, the {which} plan is not allowed.")
            else:
                diff = f"net revenue difference {money(ref, c['net_revenue_difference'])}"
                if waste_price > 0:
                    diff += f", waste policy difference {money(ref, c['waste_policy_difference'])}"
                parts.append(f"{fact}; decision regret {money(ref, c['decision_regret'])} ({diff}).")
    return DAY_N.sub(lambda m: weekday(int(m.group(1)), ref), " ".join(parts))


def next_owner(case: dict, plan: Plan | None, decided) -> list[dict]:
    """Who acts next (04-API-CONTRACT Card): the source manager while OPEN, WAITING, APPROVED or INVALIDATED; during
    PENDING_APPROVAL each side that has not decided yet; nobody otherwise."""
    src = {"role": "manager", "branch": case["source_branch"]}
    state = case["state"]
    if state in ("OPEN", "WAITING", "APPROVED", "INVALIDATED"):
        return [src]
    if state == "PENDING_APPROVAL":
        out = [] if "source" in decided else [src]
        if plan is not None and plan.kind == TRANSFER and "destination" not in decided:
            out.append({"role": "manager", "branch": plan.dest})
        return out
    return []


def _asked(r: dict, now) -> bool:
    """True while a request hides its fact from the unverified badge (07-CONTEXT D-06; research Pitfall 7): ANSWERED,
    or OPEN before its deadline (AWAITING, or a partial reply not yet overdue). Unanswered, an overdue partial reply,
    Can't check, Superseded, Held and a legacy EXPIRED row never hide it. A request view carries `overdue`; a stored
    row is read against `now` (the database's now() of the same read); a legacy row (no due_at) is not overdue."""
    if r["state"] == "ANSWERED":
        return True
    if r["state"] != "OPEN":
        return False
    if "overdue" in r:
        return not r["overdue"]
    due_at = r.get("due_at")
    return due_at is None or now is None or due_at > now


def _once(values) -> list[str]:
    """The values in their first-seen order, each once, Nones left out (a badge's `owner_branches`, 07-22)."""
    out: list[str] = []
    for v in values:
        if v is not None and v not in out:
            out.append(v)
    return out


def badges(case: dict, rec: dict | None, holds: dict | None, requests: list[dict], now=None,
           flags: list[dict] | None = None, lot_holds: list[dict] | None = None,
           required: list[str] | None = None, new_reports: list[dict] | None = None) -> list[dict]:
    """The card's badges (04-API-CONTRACT Badges). The unverified badge counts the plan in force's fact checks on the
    active version (a selected plan's own, else the stored ones: api/commitment.py `checks_in_force`; 07 review J) that
    change the plan, have no request that still hides them (`_asked`: ANSWERED, or OPEN before its deadline; an
    Unanswered one counts again, D-06), and either are not allowed (pick_feasible_if_alt false, whatever their
    regret; 02-CONTEXT D-29) or reach the stored input's policy.min_regret. It sums, to the cent, the decision regret
    and its net-revenue and waste-policy parts over the counted facts whose plan stays allowed only, and gives None
    for all three when there is none (a not-allowed fact has no regret figure, spec §6); not_allowed_fact_ids lists
    the others. Its text names no amount. "Hold expired" shows only on an OPEN case and "Re-review" only while the
    case is OPEN, WAITING, PENDING_APPROVAL or APPROVED: neither outlives the state it describes (WR-08).
    "New confirmed information · review required" (`review_flag`, with `flag_ids`) shows while `flags`, the case's
    uncleared review flags, has one and the case is APPROVED, PREPARED, EXECUTED or OBSERVED (07-CONTEXT D-17).
    "Lot hold" (`lot_hold`, with `hold_ids` and the held lots' stores `owner_branches`) shows while `lot_holds`, the
    ACTIVE holds on the lots the case involves, has one, on any state but CLOSED and REJECTED; "Required check"
    (`required_check`, with `fact_ids` and the facts' stores `owner_branches`) while `required`, the Required checks
    of the plan in force, has one (only while a commit or Prepare is ahead; D-12, D-13). "New report" (`new_report`, with `signal_ids`) while `new_reports`, the unconfirmed field reports whose
    scope holds the live case, has one (07-CONTEXT D-16; api/reports.py)."""
    out = []
    if case["missing_data"]:  # the engine's state, never the manager's words (WR-05)
        out.append({"kind": "missing_data", "text": "Missing data"})
    if case["hold_expired"] and case["state"] == "OPEN":  # only while Recheck and resend can act on it (WR-08)
        out.append({"kind": "hold_expired", "text": "Hold expired"})
    elif holds and holds.get("minutes_left") is not None and holds["minutes_left"] <= 10:
        out.append({"kind": "hold_expiring", "text": f"Held until {held_until(holds['until'])}",
                    "minutes_left": holds["minutes_left"]})  # a static time, never minutes (06.1 D-57)
    if case["re_review"] and case["state"] in DUE_STATES:  # while the re-reviewed decision is still ahead (WR-08)
        out.append({"kind": "re_review", "text": "Re-review"})
    if case["look_round"] and case["state"] in ("OPEN", "WAITING", "PENDING_APPROVAL", "APPROVED", "PREPARED"):
        out.append({"kind": "second_look", "text": "Second look"})
    if flags and case["state"] in reviewflags.STATES:  # new confirmed information on a committed plan (D-17)
        out.append({"kind": "review_flag", "text": reviewflags.REASON, "flag_ids": [f["flag_id"] for f in flags]})
    if lot_holds and case["state"] not in TERMINAL:  # a hold on a lot the case involves (07-CONTEXT D-13)
        out.append({"kind": "lot_hold", "text": "Lot hold", "hold_ids": [h["hold_id"] for h in lot_holds],
                    "owner_branches": _once(h["branch"] for h in lot_holds)})  # who resolves it (07-22)
    if required:  # a Required check of the plan in force (07-CONTEXT D-12)
        from . import commitment  # it reads this module at import
        branch_of = {c["fact_id"]: c["branch"] for c in commitment.checks_in_force(case, rec)}
        out.append({"kind": "required_check", "text": "Required check", "fact_ids": list(required),
                    "owner_branches": _once(branch_of.get(f) for f in required)})  # who resolves it (07-22)
    if new_reports and case["state"] in LIVE_STATES:  # a filed report that may change this case (07-CONTEXT D-16)
        out.append({"kind": "new_report", "text": "New report", "signal_ids": [r["signal_id"] for r in new_reports]})
    if rec:
        asked = {r["fact_id"] for r in requests if _asked(r, now) and r["rec_version"] == rec["rec_version"]}
        if asked:
            out.append({"kind": "evidence_requested", "text": "Evidence requested"})
        floor = rec["engine_input"]["policy"]["min_regret"]
        from . import commitment  # it reads this module at import
        facts = [c for c in commitment.checks_in_force(case, rec) if c["changes_plan"] and c["fact_id"] not in asked
                 and (not c["pick_feasible_if_alt"] or c["regret"] >= floor)]
        if facts and case["state"] not in DONE_STATES:
            priced = [c for c in facts if c["pick_feasible_if_alt"]]
            out.append({"kind": "unverified", "text": "Unverified", "fact_ids": [c["fact_id"] for c in facts],
                        "not_allowed_fact_ids": [c["fact_id"] for c in facts if not c["pick_feasible_if_alt"]],
                        **{k: round(sum(c[k] for c in priced), 2) if priced else None
                           for k in ("decision_regret", "net_revenue_difference", "waste_policy_difference")}})
    return out


def utc(moment: datetime) -> str:
    """A recorded time as ISO-8601 UTC."""
    return moment.astimezone(timezone.utc).isoformat()


def held_until(until: str) -> str:
    """The hold's expiry as HH:MM in the session's time zone (config.CAP_TZ), a static time (06.1 D-57)."""
    return datetime.fromisoformat(until).astimezone(config.CAP_TZ).strftime("%H:%M")


def hold_info(conn, case: dict) -> dict | None:
    """While PENDING_APPROVAL: when the round's earliest hold expires (real time), with `server_now`, the database's
    clock read in the same statement, so the client can tell an expired hold without trusting its own clock (06.1
    D-57); `minutes_left` stays a number the client never shows. None in other states."""
    if case["state"] != "PENDING_APPROVAL":
        return None
    r = db.one(conn, "SELECT min(expires_at) AS until, now() AS server_now, "
                     "ceil(extract(epoch FROM min(expires_at) - now()) / 60) AS left_min "
                     "FROM reservations WHERE run_id = :r AND case_id = :c AND round = :n AND state = 'HELD'",
               r=case["run_id"], c=case["case_id"], n=case["round"])
    if r["until"] is None:
        return {"until": None, "server_now": utc(r["server_now"]), "minutes_left": None}
    return {"until": utc(r["until"]), "server_now": utc(r["server_now"]),
            "minutes_left": max(0, int(float(r["left_min"])))}


def decided_roles(conn, case: dict) -> dict[str, str]:
    """The decisions recorded in the current round on the active version."""
    return {d["role"]: d["decision"] for d in db.rows(
        conn, "SELECT role, decision FROM decisions WHERE run_id = :r AND case_id = :c AND rec_version = :v "
              "AND round = :n ORDER BY role", r=case["run_id"], c=case["case_id"], v=case["active_rec_version"] or "",
        n=case["round"])}


def lot_meta(conn, run: dict, case: dict) -> dict:
    """The case lot's record row as the workspace reads it (api/overlay.py); once the lot is gone, its seed row
    (origin and tag never change)."""
    return _lot_view(run, case, overlay.lot(conn, run, "shelfshift", case["lot_id"]))


def _lot_view(run: dict, case: dict, row: dict | None) -> dict:
    """`lot_meta`'s answer from the lot's record row as the workspace reads it (None once the lot is gone: its seed
    row, bound to the case's one series)."""
    if row is not None:
        return {k: row[k] for k in ("lot_id", "branch", "qty", "expiry_day", "expiry_source", "markdown_pct",
                                    "last_verified_day", "verified_by", "origin", "tag")}
    ref, series = refdata.get(run["dataset_version"]), (case["source_branch"], case["sku"])  # one series (05.2 D-11)
    found = next((l for l in ref.lots0(*series) if l["lot_id"] == case["lot_id"]), None) or \
        next((i for i in ref.inbound0(*series) if i["inbound_id"] == case["lot_id"]), None) or {}
    return {"lot_id": case["lot_id"], "branch": case["source_branch"], "qty": None, "expiry_day": found.get("expiry_day"),
            "expiry_source": found.get("expiry_source"), "markdown_pct": None, "last_verified_day": None,
            "verified_by": None, "origin": found.get("origin"), "tag": case["tag"]}


def execution_state(conn, case: dict) -> dict:
    """The case's work orders (shelfshift world) and whether a receipt discrepancy of the case is OPEN, read only
    once the plan is prepared or ran (api/stepkeys.py reads them; 06.1 D-65)."""
    if case["state"] not in ("PREPARED", *stepkeys.DONE):
        return {"actions": [], "open_discrepancy": False}
    p = {"r": case["run_id"], "c": case["case_id"]}
    actions = db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                            "ORDER BY action_id", **p)
    open_one = db.one(conn, "SELECT 1 AS x FROM discrepancies WHERE run_id = :r AND case_id = :c AND state = 'OPEN' "
                            "LIMIT 1", **p)
    return {"actions": actions, "open_discrepancy": open_one is not None}


def card(conn, run: dict, case: dict, requests: list[dict], now=None) -> dict:
    """One board card, every contract key present (04-API-CONTRACT Card), with the step, stage, owner, next action
    and blocked keys computed once by api/stepkeys.py (06.1 D-23, D-24, D-54), and `lot_status`, `predecessor` and
    `successor` (07-11: a rejected or cancelled plan's lot stays visible while at risk, api/closing.py; D-24).
    `requests` may hold other cases' (stored rows or request views). `now` is the database's now() of the read (the
    board reads it once); a card given stored rows with a deadline and no `now` reads it itself, so the badge's request
    rule (D-06) uses one clock. `cards` for this one case."""
    return cards(conn, run, [case], requests, now)[0]


def _active_recs(conn, run_id: str, rows: list[dict]) -> dict[str, dict | None]:
    """`active_rec` for many cases in one read: {case_id: the active version's recommendations row, or None when the
    case has no active version or the row does not exist}."""
    out: dict[str, dict | None] = {c["case_id"]: None for c in rows}
    want = [c for c in rows if c["active_rec_version"]]
    if not want:
        return out
    for r in db.rows(conn, "SELECT r.* FROM recommendations r JOIN unnest(CAST(:ids AS text[]), CAST(:vs AS text[])) "
                           "AS k(case_id, v) ON r.case_id = k.case_id AND r.rec_version = k.v WHERE r.run_id = :r",
                     r=run_id, ids=[c["case_id"] for c in want], vs=[c["active_rec_version"] for c in want]):
        out[r["case_id"]] = r
    return out


def _holds_many(conn, run_id: str, rows: list[dict]) -> dict[str, dict | None]:
    """`hold_info` for many cases in one read: the PENDING_APPROVAL cases' earliest HELD expiry in their round, with
    `server_now` and `minutes_left` from the same expressions in one statement (a case without a HELD row still
    gets server_now); None for every other case."""
    out: dict[str, dict | None] = {c["case_id"]: None for c in rows}
    want = [c for c in rows if c["state"] == "PENDING_APPROVAL"]
    if not want:
        return out
    for r in db.rows(conn, "SELECT k.case_id, min(s.expires_at) AS until, now() AS server_now, "
                           "ceil(extract(epoch FROM min(s.expires_at) - now()) / 60) AS left_min "
                           "FROM unnest(CAST(:ids AS text[]), CAST(:ns AS int[])) AS k(case_id, n) "
                           "LEFT JOIN reservations s ON s.run_id = :r AND s.case_id = k.case_id AND s.round = k.n "
                           "AND s.state = 'HELD' GROUP BY k.case_id",
                     r=run_id, ids=[c["case_id"] for c in want], ns=[c["round"] for c in want]):
        if r["until"] is None:
            out[r["case_id"]] = {"until": None, "server_now": utc(r["server_now"]), "minutes_left": None}
        else:
            out[r["case_id"]] = {"until": utc(r["until"]), "server_now": utc(r["server_now"]),
                                 "minutes_left": max(0, int(float(r["left_min"])))}
    return out


def cards(conn, run: dict, rows: list[dict], requests: list[dict], now=None) -> list[dict]:
    """The board cards of these cases, in `rows`' order: `card`'s body, on records read with one statement per table
    for every case (recommendations, decisions, holds, lots, work orders and discrepancies; and Phase 7's review flags,
    lot holds, filed reports and successors) instead of reads per case. The step keys still come from stepkeys.keys
    with the connection (its dispatch check reads what it needs). `requests` may hold other cases' (stored rows or
    request views). `now`: as `card`; without it, the database's now() is read once, and only when one of these
    cases' stored request rows has a deadline (now() is fixed inside the transaction, so every card reads the same
    clock it would read alone).

    The Phase 7 keys follow the per-case rules word for word, on the same records read for every case at once:
    the plan in force (api/selection.py `plan_in_force`, `decide_by`, `card_view`), the open review flags while the
    state is a committed one (reviewflags.open_for_many), the ACTIVE holds on the lots each case involves while it is
    not CLOSED or REJECTED (commitment.holds_on_many), the Required checks while a commit or Prepare is ahead
    (commitment.required_ids), the unconfirmed reports whose scope holds a live case (reports.unconfirmed_many, read
    for the badge's signal ids), the closed lot's label (closing.label_of, `closing.label`'s rule on these records)
    and the successor's key (closing.successors)."""
    from . import closing, commitment, reports, selection  # they read this module: imported here
    ref = refdata.get(run["dataset_version"])
    ids = [c["case_id"] for c in rows]
    recs = _active_recs(conn, run["run_id"], rows)
    decisions = decided_many(conn, run["run_id"], rows)
    held = _holds_many(conn, run["run_id"], rows)
    lots = overlay.lot_rows(conn, run, "shelfshift", [c["lot_id"] for c in rows])
    executions = execution_many(conn, run["run_id"], rows)
    wanted = set(ids)
    if now is None and any(r["case_id"] in wanted and r.get("due_at") is not None and "overdue" not in r
                           for r in requests):
        now = realtime.db_now(conn)
    flagged = reviewflags.open_for_many(conn, run["run_id"],
                                        [c["case_id"] for c in rows if c["state"] in reviewflags.STATES])
    # the closed cases `closing.label` may read the holds of, beside every case that is not CLOSED or REJECTED
    labelled = [c for c in rows if closing.is_closed_by_person(c) and c.get("lot_status") == closing.AT_RISK]
    on_lots = commitment.holds_on_many(conn, run, [c for c in rows if c["state"] not in TERMINAL] + labelled, lots)
    reported = reports.unconfirmed_many(conn, run, [c["case_id"] for c in rows if c["state"] in LIVE_STATES])
    following = closing.successors(conn, run["run_id"], ids)
    out = []
    for case in rows:
        cid = case["case_id"]
        rec = recs[cid]
        result = rec["result"] if rec else None
        key, _, chosen = selection.plan_in_force(case, rec)  # the selection bound to the version, else the recommended
        plan = parse_plan(key) if key else None
        by = selection.decide_by(case, rec)  # the plan in force's own deadline (07 review G, D-32)
        best = next(r for r in result["options"] if r["key"] == key) if result else None
        risk = screen.lot_keep_waste(rec["engine_input"]) if rec else None  # the lot's own Keep waste (261018-h9t)
        decided = decisions[cid]
        holds = held[cid]
        meta = _lot_view(run, case, lots[case["lot_id"]])
        product = ref.products[case["sku"]]
        mine = [r for r in requests if r["case_id"] == cid]
        steps = stepkeys.keys(conn, run, case, plan, decided, mine, executions[cid])
        flags = flagged[cid] if case["state"] in reviewflags.STATES else []
        lot_holds = on_lots[cid] if case["state"] not in TERMINAL else []
        required = commitment.required_ids(case, rec)
        filed = reported[cid] if case["state"] in LIVE_STATES else []  # D-16
        if case["state"] in commitment.COMMIT_AHEAD:  # the commitment rule's blocks (07-CONTEXT D-12, D-13)
            extra = [k for k, on in (("required_check", required), ("lot_hold", lot_holds)) if on]
            steps = {**steps, "blocked": steps["blocked"] or bool(extra),
                     "blocked_reasons": steps["blocked_reasons"] + extra}
        out.append({
            "case_id": cid, "key": case["key"], "tag": case["tag"], "origin": meta["origin"],
            "state": case["state"], "column": COLUMNS[case["state"]],
            "sku": case["sku"], "product": ref.names[case["sku"]], "zone": product.zone,
            "source": case["source_branch"], "dest": plan.dest if plan else None, "lot_id": case["lot_id"],
            "suggested": (best["label"] if chosen else result["recommended_label"]) if result else None,  # in force
            "suggested_key": key,
            "selected_plan": selection.card_view(chosen),  # "Selected plan · {option}" (07-CONTEXT D-23)
            "at_risk_units": None if risk is None else round(risk, 2),  # kg and value from the unrounded figure
            "at_risk_kg": None if risk is None else round(risk * product.kg_per_unit, 2),
            "at_risk_value": None if risk is None or product.price is None else round(risk * product.price, 2),
            "est_benefit": best["delta_vs_keep"] if best else None,
            "due": due({"decide_by": by}, ref) if case["state"] in DUE_STATES else None,
            "decide_by": by, "markdown_step": (result or {}).get("markdown_step"),
            "act_before": (result or {}).get("act_before"),
            "wait": wait_view(result, rec["engine_input"] if rec else None),
            "rec_version": case["active_rec_version"],
            "next_owner": next_owner(case, plan, decided),
            "decisions": decided, "hold": holds,
            "badges": badges(case, rec, holds, mine, now, flags, lot_holds, required, filed),
            "reason": case["reason"] or case["missing_data"], "waiting_until_day": case["waiting_until_day"],
            # closing never hides at-risk stock (07-CONTEXT D-24; api/closing.py label): "Needs decision", "Awaiting
            # verification" or None on a plan a person closed; the case it follows and the case that follows it (07-12)
            "lot_status": closing.label_of(case, following[cid], lots[case["lot_id"]], on_lots.get(cid, []), rec)
            if closing.is_closed_by_person(case) else None,
            "predecessor": case.get("predecessor"), "successor": following[cid],  # the links (07-12)
            **steps,
        })
    return out


def board(conn, run: dict) -> list[dict]:
    """Every case's card, sorted by the absolute minute of `due`, then at-risk value descending, then key; a card
    with no due date (resolved, or no deadline) comes last. Equal deadlines order the same way in every sandbox.
    Expired holds are applied first (spec §8 Hold expiry: any read that touches a case)."""
    bookings.expire_holds(conn, run)
    rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r ORDER BY case_id", r=run["run_id"])
    requests = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r ORDER BY created_at, request_id",
                       r=run["run_id"])
    listed = cards(conn, run, rows, requests)  # one clock for every card's request rule (07-CONTEXT D-06)
    listed.sort(key=board_order)
    return listed


def board_order(c: dict) -> tuple:
    """The board's order: the absolute minute of `due` (none last), at-risk value descending, then key."""
    return (c["due"] is None, c["due"]["day"] * 1440 + c["due"]["min"] if c["due"] else 0, -(c["at_risk_value"] or 0.0),
            c["key"])


RISKS = ("waste", "stock")  # the board's risk filter (05.2 D-26): Keep wastes the lot's stock; stock left at the step
OWNERS = ("me",)  # the board's owner filter (06.1 D-23): the stock decisions the signed-in account owns
DEADLINES = ("scenario_date", "none")  # due on the session's scenario day; no deadline
BLOCKED_ONLY = ("1",)


def check_filters(owner: str | None = None, deadline: str | None = None, blocked: str | None = None,
                  report: str | None = None) -> None:
    """The enumerated board filters (06.1 D-17, D-23); any other value is 400 bad_request."""
    from . import reporting
    for name, value, allowed in (("owner", owner, OWNERS), ("deadline", deadline, DEADLINES),
                                 ("blocked", blocked, BLOCKED_ONLY), ("report", report, reporting.REPORTS)):
        if value is not None and value not in allowed:
            raise ApiError(400, "bad_request", f"{name} is one of {', '.join(allowed)}")


def _filtered(cards: list[dict], run: dict, actor: dict | None, owner: str | None, product: str | None,
              deadline: str | None, blocked: str | None) -> list[dict]:
    """The case cards that pass the 06.1 filters, read from each card's own server keys, in their given order."""
    me = (actor or {}).get("account")
    out = []
    for c in cards:
        if owner == "me" and (c["owner"] is None or me is None or c["owner"]["account"] != me):
            continue
        if product is not None and c["sku"] != product:
            continue
        if deadline == "scenario_date" and (c["due"] is None or c["due"]["day"] != run["day"]):
            continue
        if deadline == "none" and c["due"] is not None:
            continue
        if blocked == "1" and not c["blocked"]:
            continue
        out.append(c)
    return out


def stage_counts(cards: list[dict], n_flags: int = 0) -> dict:
    """{decide, authorise, do, check, closed} over the filtered list (every page): an archived card counts as closed
    and nowhere else, so the five add up to the total; an unevaluated flag is in Decide (06.1 D-23)."""
    counts = dict.fromkeys(stepkeys.STAGE_COUNTS, 0)
    for c in cards:
        counts["closed" if c["step"] == "ARCHIVE" else c["stage"]] += 1
    counts["decide"] += n_flags
    return counts


def _scope(ref, actor: dict | None, branch: str | None) -> tuple[str | None, bool]:
    """(the store the board is limited to, None for every store; whether that is the seat's default). A manager's
    scope is the reference `board.default_scope` ("branch": the seat's store; "all": every store) and any `branch`
    parameter is ignored; HQ sees every store by default and may name one known store (400 for another)."""
    if actor is None or actor["role"] != "hq":
        mine = actor["branch"] if actor is not None else None
        return (mine if ref.settings["board"].get("default_scope", "all") == "branch" else None), True
    if branch is None:
        return None, True
    if branch not in ref.branches:
        raise ApiError(400, "bad_request", f"No store {branch}")
    return branch, False


FLAG_COUNT = "SELECT count(*) AS n FROM case_flags f WHERE {}"  # the board's flag count over `_flag_scope`'s clause


def _case_scope(run: dict, store: str | None) -> tuple[str, dict]:
    """(the board's case clause over alias c, its parameters): every case of the run, or, for a store, the cases
    whose source or recommended destination is the store (05.2 D-26)."""
    where, p = "c.run_id = :r", {"r": run["run_id"]}
    if store is not None:
        where += " AND (c.source_branch = :b OR c.plan_dest = :b)"
        p["b"] = store
    return where, p


def _flag_scope(conn, run: dict, store: str | None, risk: str | None = None, product: str | None = None,
                owner: str | None = None, blocked: str | None = None, deadline: str | None = None) -> tuple[str, dict]:
    """(the board's clause over this morning's flags the workspace has not opened, alias f; its parameters): the
    run whose flags the workspace reads (overlay.flags_run), the store, `risk` rule (a) or (b), the product, and
    nothing at all under owner, blocked or deadline=scenario_date (nobody owns a flag, none is blocked, and a flag
    has no deadline). Every value is a bound parameter."""
    fwhere = ("f.run_id = :f AND f.day = :d AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.run_id = :r "
              "AND c.lot_id = f.lot_id)")
    fp = {"f": overlay.flags_run(conn, run), "d": run["day"], "r": run["run_id"]}
    if store is not None:
        fwhere += " AND f.branch = :b"
        fp["b"] = store
    if risk is not None:
        fwhere += " AND f.a" if risk == "waste" else " AND f.b"
    if product is not None:
        fwhere += " AND f.sku = :p"
        fp["p"] = product
    if owner is not None or blocked is not None or deadline == "scenario_date":
        fwhere += " AND false"  # nobody owns a flag, none is blocked, and a flag has no deadline
    return fwhere, fp


def board_counts(conn, run: dict, actor: dict | None) -> dict:
    """The board's default call (`board_page` with no branch, risk, filter or page) counted without building a card:
    {"cases": the cases in the account's default scope, "open": those whose state is not in stepkeys.ARCHIVED,
    "flags": this morning's flags in that scope the workspace has not opened}.

    The rule it relies on: `stage_counts` puts a card in "closed" exactly when its step is ARCHIVE, and
    stepkeys.step_of gives ARCHIVE exactly for a state in stepkeys.ARCHIVED; every other card is in one of the four
    stages, and the flags are added to "decide". So the board's stage_counts summed over stepkeys.STAGES is open +
    flags, its `evaluated` is cases and its `at_risk` (and total) is cases + flags. A set whose settings carry no
    `board` (the synthetic set) shows every case of the run and no flags. The scope and flag clauses and the flag
    count statement are board_page's own. Hold expiry never archives a case or changes its scope, so these counts do
    not depend on it; every caller applies it first in the same transaction, as every read does."""
    ref = refdata.get(run["dataset_version"])
    board = "board" in ref.settings
    store = _scope(ref, actor, None)[0] if board else None
    where, p = _case_scope(run, store)
    n = db.one(conn, f"SELECT count(*) AS cases, count(*) FILTER (WHERE c.state <> ALL(:archived)) AS open "
                     f"FROM cases c WHERE {where}", archived=list(stepkeys.ARCHIVED), **p)
    flags = 0
    if board:
        fwhere, fp = _flag_scope(conn, run, store)
        flags = int(db.one(conn, FLAG_COUNT.format(fwhere), **fp)["n"])
    return {"cases": int(n["cases"]), "open": int(n["open"]), "flags": flags}


def decided_many(conn, run_id: str, rows: list[dict]) -> dict[str, dict[str, str]]:
    """`decided_roles` for many cases in one read: {case_id: the decisions recorded in its current round on its
    active version, by role in role order}. Empty for every case when `rows` is."""
    out: dict[str, dict[str, str]] = {c["case_id"]: {} for c in rows}
    if not rows:
        return out
    for d in db.rows(conn, "SELECT d.case_id, d.role, d.decision FROM decisions d JOIN unnest(CAST(:ids AS text[]), "
                           "CAST(:vs AS text[]), CAST(:ns AS int[])) AS k(case_id, v, n) ON d.case_id = k.case_id "
                           "AND d.rec_version = k.v AND d.round = k.n WHERE d.run_id = :r ORDER BY d.case_id, d.role",
                     r=run_id, ids=[c["case_id"] for c in rows], vs=[c["active_rec_version"] or "" for c in rows],
                     ns=[c["round"] for c in rows]):
        out[d["case_id"]][d["role"]] = d["decision"]
    return out


def execution_many(conn, run_id: str, rows: list[dict]) -> dict[str, dict]:
    """`execution_state` for many cases in two reads at most: {case_id: {"actions": its shelfshift work orders in
    action_id order, "open_discrepancy": bool}}, read only for the cases that are prepared or ran (the others get
    execution_state's empty answer)."""
    out = {c["case_id"]: {"actions": [], "open_discrepancy": False} for c in rows}
    ids = [c["case_id"] for c in rows if c["state"] in ("PREPARED", *stepkeys.DONE)]
    if not ids:
        return out
    for a in db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                           "AND case_id = ANY(:c) ORDER BY action_id", r=run_id, c=ids):
        out[a["case_id"]]["actions"].append(a)
    for d in db.rows(conn, "SELECT DISTINCT case_id FROM discrepancies WHERE run_id = :r AND case_id = ANY(:c) "
                           "AND state = 'OPEN'", r=run_id, c=ids):
        out[d["case_id"]]["open_discrepancy"] = True
    return out


def board_page(conn, run: dict, actor: dict | None = None, branch: str | None = None, risk: str | None = None,
               page: int = 1, page_size: int | None = None, owner: str | None = None, product: str | None = None,
               deadline: str | None = None, blocked: str | None = None, report: str | None = None) -> dict:
    """GET /api/cases as {cases, page, page_size, total, scope, stage_counts, blocked_total[, evaluated, at_risk]}
    (05.2 D-24, D-26, D-38; 06.1 D-17, D-23). Reports filters use strict store involvement (08-CONTEXT D-18).

    A set whose reference settings carry no `board` (the synthetic set) answers every case's card in the board's
    order on one page, as before: page 1, page_size = total, scope every store; `branch`, `risk` and `page` change
    nothing there. A public set's board is scoped (`_scope`), filtered by `risk` ("waste": flags whose rule (a) holds
    and cases whose Keep wastes stock; "stock": flags whose rule (b) holds and cases opened on it) and paginated in
    one stable server order: the cases in scope (source or recommended destination is the store) in the board's order
    (deadline, value at risk, key), then this morning's flags at the store that this workspace has not opened, by Keep
    waste, then stock at the step day, descending (a flag the screen could not judge last), then key. `page_size`
    defaults to the reference `board.page_size`, which is also the largest page (400 above it); a page past the last
    is an empty list with the same total. `evaluated` counts the cases in scope (each went through the engine) and
    `at_risk` adds the unevaluated flags, so the board can show evaluated against total (D-38). Expired holds are
    applied first.

    The 06.1 filters apply on both sets, on the server, before paging, and never change the order: `owner=me` (cases
    the signed-in account owns), `product=<code>` (a product of the reference data, else 400), `deadline`
    (`scenario_date`: due on the session's scenario day; `none`: no deadline) and `blocked=1` (the server's blocked
    flag). Owner and blocked leave the flags out (nobody owns one and none is blocked); product and deadline apply to
    them (a flag has no deadline). `stage_counts` and `blocked_total` count the filtered list, every page of it, and
    `evaluated` and `at_risk` follow the same scope and filters."""
    from . import flags, reporting  # flags reads this module's views
    ref = refdata.get(run["dataset_version"])
    check_filters(owner, deadline, blocked, report)
    if product is not None and product not in ref.products:
        raise ApiError(400, "bad_request", f"No product {product}")
    if "board" not in ref.settings:
        listed = _filtered(board(conn, run), run, actor, owner, product, deadline, blocked)
        ids, store = reporting.report_ids(conn, run, actor, branch, report) if report else (None, None)
        listed = [c for c in listed if ids is None or c["case_id"] in ids]
        return {"cases": listed, "page": 1, "page_size": len(listed), "total": len(listed),
                "scope": {"branch": store, "default": True}, "stage_counts": stage_counts(listed),
                "blocked_total": sum(1 for c in listed if c["blocked"])}
    largest = int(ref.settings["board"]["page_size"])
    size = largest if page_size is None else page_size
    if not 1 <= size <= largest:
        raise ApiError(400, "bad_request", f"page_size runs from 1 to {largest}")
    if risk is not None and risk not in RISKS:
        raise ApiError(400, "bad_request", f"risk is one of {', '.join(RISKS)}")
    store, default = _scope(ref, actor, branch)
    if report:
        store = reporting.scope_store(ref, actor, branch)
    bookings.expire_holds(conn, run)
    where, p = _case_scope(run, None if report else store)
    if report:
        clause, params = reporting.report_clause(report, store)
        where, p = f"{where} AND ({clause})", {**p, **params}
    if risk == "stock":  # rule (b) held when the case opened (its `opened` entry carries the screen's figures)
        where += (" AND EXISTS (SELECT 1 FROM activity_log g WHERE g.run_id = c.run_id AND g.case_id = c.case_id "
                  "AND g.type = 'opened' AND g.payload->>'b' = 'true')")
    if product is not None:  # a bound parameter, never text in the statement
        where += " AND c.sku = :p"
        p["p"] = product
    rows = db.rows(conn, f"SELECT c.* FROM cases c WHERE {where} ORDER BY c.case_id", **p)
    requests = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r ORDER BY created_at, request_id",
                       r=run["run_id"])
    listed = cards(conn, run, rows, requests)  # one clock for every card's request rule (07-CONTEXT D-06)
    if risk == "waste":
        listed = [c for c in listed if (c["at_risk_units"] or 0.0) > 0]
    listed = _filtered(listed, run, actor, owner, product, deadline, blocked)
    listed.sort(key=board_order)
    fwhere, fp = _flag_scope(conn, run, store, risk, product, owner, blocked, deadline)
    fwhere += " AND false" if report else ""
    n_flags = db.one(conn, FLAG_COUNT.format(fwhere), **fp)["n"]
    first = (page - 1) * size
    shown = listed[first:first + size]
    room = size - len(shown)
    frows = db.rows(conn, f"SELECT * FROM case_flags f WHERE {fwhere} ORDER BY f.keep_waste DESC NULLS LAST, "
                          f"f.stock_at_step DESC NULLS LAST, f.key COLLATE \"C\" LIMIT :lim OFFSET :off",
                    lim=room, off=max(0, first - len(listed)), **fp) if room > 0 else []
    total = len(listed) + int(n_flags)
    return {"cases": shown + [flags.flag_card(ref, f) for f in frows], "page": page, "page_size": size,
            "total": total, "scope": {"branch": store, "default": default},
            "evaluated": len(listed), "at_risk": total,
            "stage_counts": stage_counts(listed, int(n_flags)),
            "blocked_total": sum(1 for c in listed if c["blocked"])}


def approval_resources(conn, run: dict, case: dict, plan: Plan | None) -> list[dict]:
    """What the plan holds, needed against available on today's effective input without this case's own bookings,
    with the transfer caps of the plan's run (04-API-CONTRACT Approvals); [] for Keep, for a run not in today's
    input, or when the input cannot be built (a gone lot, a missing forecast or capacity day, an engine refusal)."""
    if plan is None or plan.kind == KEEP:
        return []
    try:
        return bookings.plan_resources(effective(current_input(conn, run, case)), plan)
    except (HorizonError, LookupError, AdjustmentError, InputError):
        return []


def mark_label(ref: refdata.Ref) -> str:
    """The set's label with `{date}` filled from its scenario date (`data_as_of`, the dataset calendar's), as "Sat 17
    Oct 2026" (05.2 D-04), so no raw `{date}` reaches a screen or an export file. The synthetic label has none."""
    label = ref.label
    if "{date}" not in label or not ref.data_as_of:
        return label
    d = date.fromisoformat(ref.data_as_of)
    return label.replace("{date}", f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}")


def base_versions(run: dict, ref: refdata.Ref) -> dict:
    """The case detail's `versions` before any recommendation: the dataset, the forecast headline, whether the data is
    generated (`synthetic`: true only for the generated synthetic set, never for a public set, 05.2 D-03) and the
    filled label."""
    return {"rec_version": None, "effective_version": None, "engine_version": None,
            "dataset_version": run["dataset_version"], "forecast_run_id": None, "forecast_providers": None,
            "forecast_series_providers": None,
            "forecast_headline": ref.forecast["provider_summary"]["headline"], "forecast_cutoff": None,
            "day": None, "synthetic": ref.kind != "public", "label": mark_label(ref)}


def comparison(case: dict, rec: dict | None) -> dict:
    """The detail's `comparison` (06.1 D-27; 04-API-CONTRACT Comparison), read as stored, never computed here: the
    version row's comparison; `missing_data` with the engine's reason when the case has no version for lack of data;
    else `not_computed` (a version stored before 06.1, until its next write)."""
    if rec is not None and rec.get("simple_rule") is not None:
        return rec["simple_rule"]
    if rec is None and case.get("missing_data"):
        return simplerule.empty(simplerule.MISSING_DATA, case["missing_data"].removeprefix("missing data: "))
    return simplerule.empty(simplerule.NOT_COMPUTED)


def detail(conn, run: dict, case_id: str) -> dict:
    """The card plus the lot, the adopted assumptions in effect and ended, the requests, the stored result without
    the wait text, every fact check in the engine's order, the summary, the stored raw input, versions, approvals,
    review flags and the ACTIVE lot holds on the lots the case involves (04-API-CONTRACT Case detail, "Hold"; on a
    PREPARED case each with the informational `info` "Review before dispatch"), and the commitment rule's view
    (`commitment`; each fact check with its class, owner, requests left, adoptable, blocked label and Required reasons;
    07-CONTEXT D-12, D-13). Everything reads the plan in force (api/selection.py), and `options` lists the version's
    engine options for Select another plan (D-23). Expired holds are applied first. A key no case has yet but a flag of this morning
    has answers the flag's detail (api/flags.py: needs_evaluation, no result; 05.2 D-24)."""
    bookings.expire_holds(conn, run, case_id)
    case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=case_id)
    if case is None:
        from . import flags  # flags reads this module's views
        flag = flags.find(conn, run, case_id)
        if flag is None:
            raise ApiError(404, "not_found", f"No case {case_id}")
        return flags.flag_detail(conn, run, flag)
    from . import closing, commitment, lotholds, reports, selection  # they read cases at import: imported here
    ref = refdata.get(run["dataset_version"])
    requests = signals.requests_for_case(conn, run, case)  # oldest first, each with its answer (04-09)
    out = card(conn, run, case, requests)
    rec = active_rec(conn, case)
    lot = lot_meta(conn, run, case)
    runs = inputs.case_runs(ref, case["source_branch"], run["day"], lot=lot, product=ref.products[case["sku"]],
                            policy=inputs.sandbox_policy(conn, run, ref))
    _, ended = inputs.adopted_in_effect(case["adopted"], runs, {case["source_branch"], *(r.dest for r in runs)})
    result = rec["result"] if rec else None
    key, _, chosen = selection.plan_in_force(case, rec)  # the plan in force (07-CONTEXT D-23)
    plan = parse_plan(key) if key else None
    versions = base_versions(run, ref)
    checks = commitment.checks_in_force(case, rec)
    required, _ = commitment.classes(checks)
    reasons = commitment.reasons_of(rec, plan, required) if required and plan is not None else {}
    held = commitment.holds_on(conn, run, case)
    left = commitment.requests_left(rec, requests)
    opts = selection.options(conn, run, case, rec)  # every engine option, annotated for Select another plan (D-23)
    if rec:
        fr = ref.forecast_runs[rec["day"]]
        versions.update(rec_version=rec["rec_version"], effective_version=result["effective_version"],
                        engine_version=result["engine_version"], forecast_run_id=fr["forecast_run_id"],
                        forecast_series_providers=sorted({code for point in rec["engine_input"]["forecasts"]
                            if point["branch"] == case["source_branch"]
                            if (code := ref.provider_of(rec["day"], case["source_branch"], case["sku"], point["day"])) is not None},
                            key=lambda code: (PROVIDER_ORDER.index(code) if code in PROVIDER_ORDER else len(PROVIDER_ORDER), code)),
                        forecast_providers=fr["providers"], forecast_cutoff=date_label(fr["cutoff_day"], ref),
                        day=rec["day"])
    out.update(
        lot=lot,
        eligibility=eligibility.summary(conn, run, case, lot),  # stage 1 at case time (05.2 D-27, D-29..D-31)
        adopted=[i for i in case["adopted"] if i not in ended], adopted_ended=ended,
        requests=requests,
        result=without_wait_text(result),
        fact_checks=[{**in_engine_order(c), **commitment.check_keys(c, rec, requests, left, reasons)}
                     for c in checks],  # the list in the engine's order, with the Phase 7 keys
        commitment=commitment.summary(conn, run, case, rec, plan, checks, requests, reasons, held, opts),
        options=opts,
        summary=summary(result, checks, ref,  # the plan in force's checks (D-23)
                        waste_price=rec["engine_input"]["policy"]["waste_price"] if rec else 0.0,
                        state=case["state"], selected=chosen),  # no "Unverified" once rejected or closed (06.1 D-36)
        engine_input=rec["engine_input"] if rec else None,
        comparison=comparison(case, rec),  # Recommended, Simple rule, Keep as stored (06.1 D-27)
        versions=versions,
        predecessor_closed=closing.predecessor_closed(conn, run, case),  # "Follows {key} · Rejected {t}" (07-22)
        review_flags=[reviewflags.view(f, case["state"]) for f in reviewflags.for_case(conn, run["run_id"], case_id)],
        reports=reports.unconfirmed_for(conn, run, case_id) if case["state"] in LIVE_STATES else [],  # D-16
        holds=[{**lotholds.view(conn, run, h),  # every ACTIVE hold on a lot the case involves (07-CONTEXT D-13)
                "info": reviewflags.BEFORE_DISPATCH if case["state"] == "PREPARED" else None} for h in held],
        approvals={"round": case["round"],
                   "required": ["source", "destination"] if plan is not None and plan.kind == TRANSFER else ["source"],
                   "decisions": out["decisions"], "hold": out["hold"],
                   "resources": approval_resources(conn, run, case, plan),
                   "next_owner": out["next_owner"]},
    )
    return out
