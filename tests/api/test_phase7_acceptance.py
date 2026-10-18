"""Phase 7's seven ROADMAP success criteria, against the API, on both templates (07-CONTEXT D-02, D-03; 07-23).

Each criterion runs on the synthetic template and on the tiny public template (tests/api/fixtures/public_tiny, built
through `sandbox.build_template` like the release build). The judge's case is found by its tag and story beat, never
by a key, a quantity or an amount: the synthetic set's story case (tag milk-story) and the public set's
verify-before-move case (the catalogue's tag "verify"). Its count fact is the plan-changing PHANTOM_STOCK or
STALE_COUNT check of the plan in force; every expected value is read from the stored engine output, the settings and
the criterion's own words. Nothing here rewords a criterion or tunes data (D-03; benchmarks, not targets): a beat the
data does not give is skipped with the reason (a reported gap), and a beat that fails names its criterion.

What a test writes is its own move, as the Phase 7 tests already make it: a past deadline (`due_at` moved behind
now()), an ESTIMATED expiry mark on the judge's lot, a Required check made from the record when the template gives
none (verify_helpers.required_fixture), the test's own twin lots and extra fact copies, a committed state set
directly. Nothing sleeps, and nothing new is added to api/.
"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api import (cases, closing, commitment, config, db, evaluate, execution, lotholds, preload, realtime, refdata,
                 sandbox, selection, signals, workflow)
from api.app import app
from api.errors import ApiError
from engine.adjust import effective
from engine.limits import CAP_REASON
from engine.model import TRANSFER
from tests.api.conftest import as_seat, by_tag, fresh, key, own_series, owned_send, seat, sign_in, world_lot
from tests.api.test_advance import advance_once
from tests.api.test_evidence import (add_facts, answer, confirm, counted, in_both, past_due, request_row, signal_row,
                                     table)
from tests.api.test_requests import PHOTO, activity, answer_dates
from tests.api.test_signals import active_rows, file_report, set_state, twin
from tests.api.test_workflow import add_booking, call
from tests.api.verify_helpers import ack, required_fixture, shown

DATASETS = ("synthetic", "public")
JUDGE_TAG = {"synthetic": cases.STORY_TAG, "public": "verify"}  # the story case; catalogue.py's verify-before-move
TITLES = {
    "1": "Count stock creates the shared evidence request",
    "2": "Tasks: the answer rules",
    "2-mixed-dates": "Tasks: a Mixed dates expiry answer holds the lot",
    "3": "Confirming a complete count",
    "4": "Unanswered and the commitment rule",
    "4-advisory": "Advisory checks are acknowledged in each approving decision",
    "5": "Business events: Report an observation",
    "6": "A blocked case always has a next action",
    "7": "Closing a plan never hides at-risk stock",
}
BANGKOK = ZoneInfo("Asia/Bangkok")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BLOCKED_LABEL = "Blocked · stock/expiry verification required"
MIXED = "Separate stock and verify before moving"
REVIEW = "New confirmed information · review required"
PREVIEW = "Potential impact · unconfirmed"
REVIEW_ONLY = "Requires human review · engine impact not calculated"
OLDER = "Checked earlier than the confirmed evidence · kept, not applied"
SAME_TIME = "Same check time as the confirmed evidence, different value · review required"
NEEDS, AWAITING = "Needs decision", "Awaiting verification"


# --- the two templates ------------------------------------------------------------------------------------------------

class Template:
    """The template a criterion runs on (ACTIVE while the test runs, so a signed-in session's workspace is cloned
    from it too), with the judge's tag."""

    def __init__(self, dataset: str, run_id: str):
        self.dataset, self.run_id, self.tag = dataset, run_id, JUDGE_TAG[dataset]

    def clone(self) -> dict:
        """A fresh workspace cloned from this template."""
        with db.tx() as conn:
            rid = sandbox.clone(conn, fresh(self.run_id))
        return fresh(rid)


@pytest.fixture
def tpl(dataset, template, public_template):
    """The dataset's template made the ACTIVE one for this test (the other RETIRED), and the public template ACTIVE
    again afterwards, as the module-scoped `public_template` keeps it until its own teardown."""
    chosen, other = (template, public_template) if dataset == "synthetic" else (public_template, template)
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :o", o=other)
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :c", c=chosen)
    try:
        yield Template(dataset, chosen)
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :t", t=template)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :p", p=public_template)


# --- reading the judge's case -----------------------------------------------------------------------------------------

def judge(run, t: Template) -> dict:
    case = by_tag(run, t.tag)
    assert case is not None, f"the {t.dataset} template has no case tagged {t.tag}"
    return case


def version(run, case_id) -> str:
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)["active_rec_version"]


def state(run, case_id) -> str:
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)["state"]


def case_now(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], case_id)


def active(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run["run_id"], case_id))


def in_force(run, case_id) -> list[dict]:
    """The fact checks of the plan in force (a selection's own, else the active version's)."""
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], case_id)
        return commitment.checks_in_force(case, cases.active_rec(conn, case))


def count_fact(run, case_id) -> dict:
    """The story beat's fact check: the plan-changing count fact (PHANTOM_STOCK or STALE_COUNT) of the plan in force."""
    fact = next((c for c in in_force(run, case_id) if c["kind"] in signals.COUNT_KINDS and c["changes_plan"]), None)
    assert fact is not None, "the judge's case has no plan-changing count fact"
    return fact


def required_check(run, case_id) -> dict:
    """A Required count check of the plan in force (pick_feasible_if_alt false): the template's own when it gives
    one, else made from the record (verify_helpers.required_fixture, the Phase 7 tests' sanctioned move)."""
    found = next((c for c in in_force(run, case_id) if c["kind"] in signals.COUNT_KINDS and c["changes_plan"]
                  and not c["pick_feasible_if_alt"]), None)
    return found if found is not None else required_fixture(run, case_id)


def detail(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.detail(conn, fresh(run["run_id"]), case_id)


def card(run, case_id) -> dict:
    """The case's card as the board lists it."""
    with db.tx() as conn:
        return next(c for c in cases.board(conn, fresh(run["run_id"])) if c["case_id"] == case_id)


def badge_kinds(run, case_id) -> list[str]:
    return [b["kind"] for b in card(run, case_id)["badges"]]


def plan_shown(run, case_id):
    return cases.parse_plan(shown(run, case_id))


def logged(run, case_id, type_) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM activity_log WHERE run_id = :r AND case_id = :c AND type = :t ORDER BY seq",
                       r=run["run_id"], c=case_id, t=type_)


def rows(run, table_, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, f"SELECT * FROM {table_} WHERE run_id = :r AND case_id = :c", r=run["run_id"], c=case_id)


def refused(fn, *args) -> ApiError:
    with pytest.raises(ApiError) as e:
        fn(*args)
    return e.value


def ask(run, case, fact_id, recount_of=None) -> dict:
    """The case's source manager asks (Count stock / Check printed expiry date): signals.request_evidence."""
    return call(run, signals.request_evidence, seat(case["source_branch"]), case["case_id"],
                version(run, case["case_id"]), fact_id, recount_of)


def to_seat(q) -> dict:
    return seat(q["to_branch"])


def bangkok_label(moment) -> str:
    """'16 Oct 2026, 20:30' in Asia/Bangkok time, computed here from the stored UTC time."""
    t = moment.astimezone(BANGKOK)
    return f"{t.day} {MONTHS[t.month - 1]} {t.year}, {t:%H:%M}"


def decide(run, case_id, actor, decision, plan_key=None, given="ack"):
    """A decision on the active version naming the plan shown (or `plan_key`) with the acknowledgement it needs."""
    given = ack(run, case_id) if given == "ack" else given
    return call(run, workflow.decide, actor, case_id, version(run, case_id), decision, None, given,
                plan_key or shown(run, case_id))


def approve(run, case_id) -> dict:
    """The plan shown sent and approved: a transfer's destination accepts, then the source approves."""
    case = case_now(run, case_id)
    src, plan = seat(case["source_branch"]), plan_shown(run, case_id)
    owned_send(run, src, case_id, version(run, case_id))
    if plan.kind == TRANSFER:
        assert decide(run, case_id, seat(plan.dest), "accept")["state"] == "PENDING_APPROVAL"
    return decide(run, case_id, src, "approve")


def approvable(run, case_id) -> str:
    """The plan this test approves: the plan shown when nothing Required blocks it, else the first engine option
    (in the result's order) other than Keep that is feasible today and depends on no Required fact, selected by the
    source manager (Select another plan)."""
    d = detail(run, case_id)
    if not d["commitment"]["blocks"]:
        return d["suggested_key"]
    key_ = next((o["key"] for o in d["options"] if o["feasible"] and not o["required_fact_ids"]
                 and o["key"] != "keep"), None)
    assert key_ is not None, "the judge's case has no feasible option without a Required check other than Keep"
    case = case_now(run, case_id)
    call(run, selection.select, seat(case["source_branch"]), case_id, version(run, case_id), key_)
    return key_


# --- the criteria -----------------------------------------------------------------------------------------------------

def criterion_1(t: Template, **_) -> None:
    """On the judge's case the source-branch manager presses Count stock: the request goes through the same object and
    server function every caller uses (the route below, the service the agent's tool calls), sharing the max-2 limit
    per version; it reads "Awaiting response · respond by {time}", 60 real minutes later, in Asia/Bangkok time; the
    board shows the "evidence requested" badge."""
    template_case = judge(t.run_id, t)
    c = TestClient(app)
    sign_in(c, template_case["source_branch"])
    run = fresh(c.get("/api/session").json()["run_id"])
    case = judge(run, t)
    cid = case["case_id"]
    fact = count_fact(run, cid)
    assert "evidence_requested" not in badge_kinds(run, cid)
    r = c.post(f"/api/cases/{cid}/requests", json={"rec_version": case["active_rec_version"],
                                                    "fact_id": fact["fact_id"]}, headers=key())
    assert r.status_code == 200, r.json()
    q = r.json()
    row = request_row(run, q)
    assert (q["state"], q["part"], q["fact_id"], q["to_branch"], q["lot_ids"]) == (
        "OPEN", "count", fact["fact_id"], fact["branch"], fact["lot_ids"])
    assert row["due_at"] - row["created_at"] == timedelta(minutes=60)  # 60 real minutes, from the database clock
    assert config.CAP_TZ.key == "Asia/Bangkok"
    awaiting = f"Awaiting response · respond by {bangkok_label(row['due_at'])}"
    assert (q["status_kind"], q["status"]) == ("AWAITING", awaiting)
    assert in_both(run, q)["status"] == awaiting
    shown_badge = next(b for b in card(run, cid)["badges"] if b["kind"] == "evidence_requested")
    assert shown_badge["text"] == "Evidence requested"
    detail_ = c.get(f"/api/cases/{cid}").json()
    assert "evidence_requested" in [b["kind"] for b in detail_["badges"]]
    # one object, one limit per version: the service (the agent tool's function) and the route share them
    add_facts(run, cid, [{**fact, "fact_id": "TEST:second"}, {**fact, "fact_id": "TEST:third"}])
    again = call(run, signals.request_evidence, seat(case["source_branch"]), cid, version(run, cid), fact["fact_id"])
    assert again["request_id"] == q["request_id"]  # asking twice is the same request
    second = call(run, signals.request_evidence, seat(case["source_branch"]), cid, version(run, cid), "TEST:second")
    assert second["state"] == "OPEN"
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM evidence_requests WHERE run_id = :r AND case_id = :c AND "
                            "rec_version = :v", r=run["run_id"], c=cid, v=version(run, cid))["n"] == 2
    r = c.post(f"/api/cases/{cid}/requests", json={"rec_version": version(run, cid), "fact_id": "TEST:third"},
               headers=key())
    assert (r.status_code, r.json()["error"]) == (409, "too_many_requests"), r.json()
    assert config.MAX_REQUESTS_PER_VERSION == 2


def criterion_2(t: Template, monkeypatch, tmp_path, **_) -> None:
    """As the addressed branch, under Tasks: Shelf and Backroom start "Not checked" (nothing is pre-filled and a blank
    part is never a zero); a photo alone leaves the request open with "Photo received · count missing" (" · overdue"
    after the deadline) and the case unverified; Can't check closes the attempt and the fact stays unverified; asking
    again marks the earlier request Superseded, and a late answer to it is kept but can't be confirmed; every count
    answer states "When did you check?" with no default."""
    monkeypatch.setattr(config, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(config, "UPLOADS_BUCKET", "")
    run = t.clone()
    case = judge(run, t)
    cid = case["case_id"]
    fact = count_fact(run, cid)
    v0 = version(run, cid)
    q = ask(run, case, fact["fact_id"])
    to = to_seat(q)
    with db.tx() as conn:
        listed = signals.tasks_for(conn, fresh(run["run_id"]), to)
    mine = next(r for r in listed["requests"] if r["request_id"] == q["request_id"])
    assert (mine["status_kind"], mine["answer"], mine["answerable"]) == ("AWAITING", None, True)  # nothing pre-filled
    assert listed["counts"]["to_answer"] >= 1
    before = {lot: world_lot(run, lot) for lot in q["lot_ids"]}
    photo = call(run, signals.answer, to, q["request_id"], "", None, "now", PHOTO)
    assert len(photo["fields"]["needs_input"]) == 2 * len(q["lot_ids"])  # Shelf and Backroom, both still to count
    seen = in_both(run, q)
    assert (request_row(run, q)["state"], seen["status_kind"], seen["status"], seen["confirmable"]) == (
        "OPEN", "PHOTO_PARTIAL", "Photo received · count missing", False)
    assert {lot: world_lot(run, lot) for lot in q["lot_ids"]} == before and table(run["run_id"], "reported_facts") == []
    assert version(run, cid) == v0  # the case stays unverified: nothing confirmed, nothing re-planned
    past_due(run, q)
    seen = in_both(run, q)
    assert (seen["status"], seen["overdue"]) == ("Photo received · count missing · overdue", True)
    unverified = next(b for b in card(run, cid)["badges"] if b["kind"] == "unverified")
    assert fact["fact_id"] in unverified["fact_ids"]
    closed = call(run, signals.cant_check, to, q["request_id"])
    assert (closed["state"], closed["status"]) == ("CANT_CHECK", "Can't check")
    assert fact["fact_id"] in next(b for b in card(run, cid)["badges"] if b["kind"] == "unverified")["fact_ids"]
    assert table(run["run_id"], "reported_facts") == [] and version(run, cid) == v0
    new = ask(run, case, fact["fact_id"], recount_of=q["request_id"])
    old = request_row(run, q)
    assert (new["state"], old["state"], old["replaced_by"]) == ("OPEN", "SUPERSEDED", new["request_id"])
    late = answer(run, q, counted(q, 1.0))  # an answer to the superseded request is kept
    assert request_row(run, q)["answer_signal_id"] == late["signal_id"] and signal_row(run, late["signal_id"])
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["late"], seen["confirmable"]) == ("SUPERSEDED", True, False)
    e = refused(call, run, signals.confirm, to, late["signal_id"], late["revision"], True)
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Request is SUPERSEDED")
    for no_time in (None, "", "  "):  # "When did you check?" has no default
        e = refused(call, run, signals.answer, to, new["request_id"], "", counted(new, 2.0), no_time)
        assert (e.status, e.code) == (400, "check_time"), no_time
    assert request_row(run, new)["answer_signal_id"] is None
    lot = new["lot_ids"][0]
    blank = answer(run, new, {**counted(new, 2.0), lot: {"shelf_units": 2.0, "backroom_units": None}})
    assert blank["fields"]["counts"][lot]["backroom_units"] is None  # Not checked stays blank, never 0
    assert {"lot_id": lot, "field": "backroom_units"} in blank["fields"]["needs_input"]
    row = signal_row(run, blank["signal_id"])
    assert row["observed_at"] == row["created_at"]  # "Checked just now": the server's now() at submission
    earlier = row["created_at"] - timedelta(minutes=20)
    typed = answer(run, new, counted(new, 2.0), observed=earlier.astimezone(BANGKOK).isoformat())
    assert signal_row(run, typed["signal_id"])["observed_at"] == earlier  # a date and time, as given


def criterion_2_mixed_dates(t: Template, **_) -> None:
    """A Mixed dates expiry answer (Check printed expiry date) creates an audited lot hold reading "Separate stock and
    verify before moving", and the answer states "When did you check?" with no default. The judge's lot is marked
    ESTIMATED (the test's own move: neither template gives an estimated expiry); the engine decides whether the
    printed date changes the plan."""
    run = t.clone()
    case = judge(run, t)
    cid = case["case_id"]
    lot = world_lot(run, case["lot_id"])
    own_series(run, lot["branch"], lot["sku"])
    with db.tx() as conn:
        db.run(conn, "UPDATE lots SET expiry_source = 'ESTIMATED' WHERE run_id = :r AND world = 'shelfshift' "
                     "AND lot_id = :l", r=run["run_id"], l=case["lot_id"])
        locked = db.lock_run(conn, run["run_id"], mode="UPDATE")
        cases.evaluate_case(conn, locked, cases.case_row(conn, run["run_id"], cid, lock=True))
    fact = next((c for c in in_force(run, cid) if c["kind"] == "ESTIMATED_EXPIRY" and c["changes_plan"]), None)
    if fact is None:
        found = [(c["fact_id"], c["changes_plan"]) for c in in_force(run, cid) if c["kind"] == "ESTIMATED_EXPIRY"]
        pytest.skip(f"data gap (reported, never forced): on the {t.dataset} template an estimated expiry on the "
                    f"{t.tag} lot does not change the plan ({found}), so Check printed expiry date can't be asked")
    q = ask(run, case, fact["fact_id"])
    assert (q["part"], q["status_kind"]) == ("expiry", "AWAITING")
    to = to_seat(q)
    mixed = {lot_id: signals.MIXED for lot_id in q["lot_ids"]}
    e = refused(call, run, signals.answer, to, q["request_id"], "", None, None, None, mixed, True)
    assert (e.status, e.code) == (400, "check_time")
    a = answer_dates(run, q, mixed)
    [hold] = table(run["run_id"], "lot_holds")
    assert (hold["kind"], hold["reason"], hold["state"], hold["placed_by"], hold["request_id"], hold["signal_id"]) == (
        "MIXED_DATES", MIXED, "ACTIVE", q["to_branch"], q["request_id"], a["signal_id"])
    assert [x["payload"]["hold_id"] for x in activity(run["run_id"], "hold_placed")] == [hold["hold_id"]]  # audited
    assert request_row(run, q)["state"] == "HELD" and table(run["run_id"], "reported_facts") == []
    assert "lot_hold" in badge_kinds(run, cid)


def criterion_3(t: Template, monkeypatch, **_) -> None:
    """Confirming a complete count (both parts, same lot, the tick ticked) after the applicability and newer-evidence
    checks creates the stock correction and re-plans every affected live case, and the judge's new version
    recommends the plan the fact check named for the counted value (confirming 0 + 0: its best_plan_if_alt); it
    approves nothing; every committed case in the fact's scope is flagged "New confirmed information · review
    required" with no engine run; evidence checked earlier than the confirmed fact in force is refused and kept, and
    an equal check time with a different value needs review."""
    run = t.clone()
    case = judge(run, t)
    cid = case["case_id"]
    fact = count_fact(run, cid)
    base = world_lot(run, case["lot_id"])
    live_twin = twin(run, base, 1)  # the test's own lots: the judge lot's twins at its store, each with its own case
    approved, prepared = twin(run, base, 2), twin(run, base, 3)
    for c in (live_twin, approved, prepared):
        assert set(fact["lot_ids"]) <= {lot["lot_id"] for lot in active(run, c)["engine_input"]["lots"]}, c
    set_state(run, approved, "APPROVED")  # committed states set directly (a flag needs no work order)
    set_state(run, prepared, "PREPARED")
    with db.tx() as conn:
        live_in_scope = [c["case_id"] for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = "
                                                             "ANY(:s) AND active_rec_version IS NOT NULL",
                                                       r=run["run_id"], s=list(cases.LIVE_STATES))
                         if set(fact["lot_ids"]) & {lot["lot_id"] for lot in
                                                    cases.active_rec(conn, c)["engine_input"]["lots"]}]
    assert {cid, live_twin} <= set(live_in_scope)
    before = {c: version(run, c) for c in [*live_in_scope, approved, prepared]}
    q = ask(run, case, fact["fact_id"])
    to = to_seat(q)
    e = refused(call, run, signals.answer, to, q["request_id"], "",
                {**counted(q, 0.0), f"{base['lot_id']}-T1": {"shelf_units": 0.0, "backroom_units": 0.0}}, "now")
    assert (e.status, e.code) == (409, "count_mismatch")  # applicability: only the asked lots
    a = answer(run, q, counted(q, 0.0))
    e = refused(call, run, signals.confirm, to, a["signal_id"], a["revision"], False)
    assert (e.status, e.code) == (409, "scope_tick")
    computed = []
    real = evaluate.compute
    monkeypatch.setattr(evaluate, "compute", lambda inp: computed.append(inp.case_lot_id) or real(inp))
    out = confirm(run, to, a["signal_id"], a["revision"])
    monkeypatch.undo()
    day = fresh(run["run_id"])["day"]
    for lot_id in q["lot_ids"]:  # the stock correction
        row = world_lot(run, lot_id)
        assert (row["qty"], row["last_verified_day"], row["verified_by"]) == (0.0, day, "COUNT")
    (fact_row,) = table(run["run_id"], "reported_facts")
    assert fact_row["fact_id"] == out["fact_id"] and fact_row["kind"] == "STOCK_COUNT"
    for c in live_in_scope:  # every affected live case re-planned
        assert version(run, c) != before[c], c
    assert set(live_in_scope) <= {x["case_id"] for x in out["cases"]}
    assert active(run, cid)["result"]["recommended"] == fact["best_plan_if_alt"]  # the story beat's new plan
    assert rows(run, "decisions", cid) == [] and state(run, cid) == case["state"]  # approves nothing
    assert not [r for r in rows(run, "reservations", cid) if r["state"] in ("HELD", "COMMITTED", "CONVERTED")]
    assert out["flagged"] == sorted([approved, prepared])
    flags = table(run["run_id"], "review_flags")
    assert sorted(f["case_id"] for f in flags) == sorted([approved, prepared])
    assert {(f["fact_id"], f["reason"]) for f in flags} == {(out["fact_id"], REVIEW)}
    for c in (approved, prepared):
        assert version(run, c) == before[c] and REVIEW in [b["text"] for b in card(run, c)["badges"]]
    assert not {f"{base['lot_id']}-T2", f"{base['lot_id']}-T3"} & set(computed)  # no engine run for them
    at = fact_row["observed_at"]
    add_facts(run, cid, [{**fact, "fact_id": "TEST:older"}, {**fact, "fact_id": "TEST:same"}])
    for fid, value, when, code, message in (("TEST:older", 5.0, at - timedelta(seconds=1), "older_evidence", OLDER),
                                             ("TEST:same", 3.0, at, "review_required", SAME_TIME)):
        q2 = ask(run, case_now(run, cid), fid)
        s = answer(run, q2, counted(q2, value), observed=when.isoformat())
        e = refused(call, run, signals.confirm, to_seat(q2), s["signal_id"], s["revision"], True)
        assert (e.status, e.code, e.message) == (409, code, message), fid
        assert (request_row(run, q2)["state"], signal_row(run, s["signal_id"])["state"]) == ("ANSWERED", "READ")
        assert table(run["run_id"], "reported_facts") == [fact_row]  # kept, not applied
        assert {world_lot(run, lot_id)["qty"] for lot_id in q2["lot_ids"]} == {0.0}


def criterion_4(t: Template, **_) -> None:
    """A request with no complete answer by its deadline reads "Unanswered · Unverified" on the next read, with no day
    advance and no background job. Then a Required fact check (the plan would break a hard limit if the fact were
    true) or an explicit lot hold makes the final approval and Prepare refuse, with the reason, the owner and the
    resolution path; Send stays allowed."""
    run = t.clone()
    case = judge(run, t)
    cid, src = case["case_id"], seat(case["source_branch"])
    rc = required_check(run, cid)
    q = ask(run, case_now(run, cid), rc["fact_id"])
    day = fresh(run["run_id"])["day"]
    past_due(run, q)
    seen = in_both(run, q)
    assert (seen["status_kind"], seen["status"]) == ("UNANSWERED", "Unanswered · Unverified")
    assert request_row(run, q)["state"] == "OPEN" and fresh(run["run_id"])["day"] == day  # derived on read
    plan = plan_shown(run, cid)
    assert owned_send(run, src, cid, version(run, cid))["state"] == "PENDING_APPROVAL"  # Send stays allowed
    if plan.kind == TRANSFER:
        assert decide(run, cid, seat(plan.dest), "accept")["state"] == "PENDING_APPROVAL"
    e = refused(decide, run, cid, src, "approve")
    assert (e.status, e.code) == (409, "commitment_blocked")
    (block,) = [b for b in e.extra["blocks"] if b["kind"] == "required_check"]
    assert (block["fact_id"], block["owner_branch"]) == (rc["fact_id"], rc["branch"])
    assert block["reason"] and block["resolution"] == f"Confirm a count from {rc['branch']}"
    assert state(run, cid) == "PENDING_APPROVAL"
    assert not [r for r in rows(run, "reservations", cid) if r["state"] in ("COMMITTED", "CONVERTED")]
    set_state(run, cid, "APPROVED")  # an approval made before the check (the test's own setup): Prepare refuses
    e = refused(call, run, workflow.prepare, src, cid)
    assert (e.code, [b["fact_id"] for b in e.extra["blocks"] if b["kind"] == "required_check"]) == (
        "commitment_blocked", [rc["fact_id"]])
    assert rows(run, "world_actions", cid) == []
    other = t.clone()  # an explicit lot hold, in its own workspace
    held = judge(other, t)
    hid, hsrc = held["case_id"], seat(held["source_branch"])
    lot = world_lot(other, held["lot_id"])
    hold = call(other, lotholds.place, seat(lot["branch"]), held["lot_id"], "CONDITION",
                "Crushed crates in the chiller")
    assert owned_send(other, hsrc, hid, version(other, hid))["state"] == "PENDING_APPROVAL"
    hplan = plan_shown(other, hid)
    if hplan.kind == TRANSFER:
        decide(other, hid, seat(hplan.dest), "accept")
    expected = {"kind": "lot_hold", "hold_id": hold["hold_id"], "lot_ids": [held["lot_id"]],
                "owner_branch": lot["branch"], "reason": hold["reason"],
                "resolution": "Release the hold or resolve its check"}
    e = refused(decide, other, hid, hsrc, "approve")
    assert e.code == "commitment_blocked" and expected in e.extra["blocks"]
    set_state(other, hid, "APPROVED")
    e = refused(call, other, workflow.prepare, hsrc, hid)
    assert e.code == "commitment_blocked" and expected in e.extra["blocks"]


def criterion_4_advisory(t: Template, **_) -> None:
    """Advisory checks (the plan stays allowed if the fact is true) show "Unverified · estimated decision exposure $X"
    (the badge's amount, the decision regret) and must be acknowledged in each approving decision."""
    run = t.clone()
    case = judge(run, t)
    cid, src = case["case_id"], seat(case["source_branch"])
    advisory = [c for c in in_force(run, cid) if c["changes_plan"] and c["pick_feasible_if_alt"]]
    if not advisory:
        pytest.skip(f"data gap (reported, never forced): the {t.dataset} template's {t.tag} case has no Advisory "
                    f"check (its checks: {[(c['fact_id'], c['pick_feasible_if_alt']) for c in in_force(run, cid)]})")
    unverified = next(b for b in card(run, cid)["badges"] if b["kind"] == "unverified")
    assert unverified["decision_regret"] == round(sum(c["regret"] for c in advisory), 2) and \
        unverified["decision_regret"] > 0  # the "$X" the badge shows: a decision exposure, never cash
    assert {c["fact_id"] for c in advisory} <= set(unverified["fact_ids"])
    expected = {"rec_version": version(run, cid), "plan_key": shown(run, cid),
                "fact_ids": sorted(c["fact_id"] for c in advisory)}
    assert detail(run, cid)["commitment"]["ack"] == expected
    plan = plan_shown(run, cid)
    owned_send(run, src, cid, version(run, cid))
    deciders = ([(seat(plan.dest), "accept")] if plan.kind == TRANSFER else []) + [(src, "approve")]
    for actor, decision in deciders:
        e = refused(decide, run, cid, actor, decision, None, None)
        assert (e.status, e.code) == (409, "ack_required"), decision
        decide(run, cid, actor, decision, None, expected)
    assert state(run, cid) == "APPROVED"
    assert [d["acknowledged"] for d in rows(run, "decisions", cid)] == [expected] * len(deciders)


def criterion_5(t: Template, **_) -> None:
    """Under Business events, Report an observation takes the typed fields by hand: its preview "Potential impact ·
    unconfirmed" blocks nothing; only the owning role confirms (the branch manager for branch facts, HQ for transport),
    creating a "Reported by <role> · <real time> · source" fact; the reporter can withdraw only before confirmation;
    condition observations read "Requires human review · engine impact not calculated"; the allowance (20 reports a
    day, 10 with a file) is shared by everyone in the workspace; the preloaded "Simulated report" and "Simulated
    response" items are there to work on, a minimum the data can't give named in the template's preload entry
    (07-CONTEXT D-39); there are no sample buttons."""
    c = TestClient(app)
    sign_in(c, "HQ")
    run = fresh(c.get("/api/session").json()["run_id"])
    run_id = run["run_id"]
    case = judge(run, t)
    cid = case["case_id"]
    the_card = next(x for x in c.get("/api/cases").json()["cases"] if x["case_id"] == cid)
    src, dest, zone = case["source_branch"], the_card["dest"], the_card["zone"]
    assert dest, "the judge's plan shown is a transfer"
    today = cases.iso_of(run, run["day"])
    third = next(b for b in refdata.get(run["dataset_version"]).branches if b not in (src, dest))
    # the preloaded items (D-18, D-39), and no sample buttons
    page = c.get("/api/signals").json()
    assert "signals" in page, page
    preloaded = [r for r in page["signals"] if r.get("provenance") == "simulated"]
    with db.tx() as conn:
        missing = preload.shortfall(conn, t.run_id)
        responses = db.rows(conn, "SELECT * FROM signals WHERE run_id = :r AND provenance = 'simulated' AND "
                                  "request_id IS NOT NULL", r=run_id)
    assert all((r["simulated_label"], r["state"]) == ("Simulated report", "READ") for r in preloaded)
    roles = {r["owner"]["role"] for r in preloaded}
    for role, minimum in (("manager", "no preloaded unconfirmed report for a store manager"),
                          ("hq", "no preloaded unconfirmed report for HQ")):
        assert role in roles or minimum in missing, (role, missing)  # given, or named as the data's shortfall
    if not responses:
        assert "no preloaded photo-only response" in missing, missing
    assert not [r.path for r in app.routes if "sample" in getattr(r, "path", "")]
    allowance = page["allowance"]
    assert (allowance["reports_limit"], allowance["files_limit"]) == (20, 10)
    assert config.WORKSPACE_CAPS["signal"] == (20, "day") and config.WORKSPACE_CAPS["upload"] == (10, "day")
    left = allowance["reports_left"]
    for r in preloaded:  # a preloaded report is work its owner can act on
        owner = r["owner"]
        as_seat(c, None if owner["role"] == "hq" else owner["branch"])
        done = c.post(f"/api/signals/{r['signal_id']}/confirm", json={"revision": r["revision"]}, headers=key())
        assert done.status_code == 200, done.json()
    # Report an observation, by hand, as the destination's manager
    as_seat(c, dest)
    rep = file_report(c, "CAPACITY", {"zone": zone, "free_totes": 0, "valid_from": today, "valid_to": today},
                      text="One chiller is down today").json()
    assert (rep["state"], rep["owner"], rep["fact_id"]) == ("READ", {"role": "manager", "branch": dest}, None), rep
    before, blocked = active_rows(run_id), detail(run, cid)["commitment"]["blocks"]
    r = c.post(f"/api/signals/{rep['signal_id']}/preview", json={"revision": 1}, headers=key())
    assert r.status_code == 200 and r.json()["label"] == PREVIEW, r.json()
    assert active_rows(run_id) == before and detail(run, cid)["commitment"]["blocks"] == blocked  # blocks nothing
    for other in (src, None):  # only the owning role confirms
        as_seat(c, other)
        r = c.post(f"/api/signals/{rep['signal_id']}/confirm", json={"revision": 1}, headers=key())
        assert (r.status_code, r.json()["error"]) == (403, "forbidden"), other
    as_seat(c, dest)
    out = c.post(f"/api/signals/{rep['signal_id']}/confirm", json={"revision": 1}, headers=key()).json()
    with db.tx() as conn:
        fact = db.one(conn, "SELECT * FROM reported_facts WHERE run_id = :r AND fact_id = :f", r=run_id,
                      f=out["fact_id"])
    assert out["labels"] == [f"Reported by {dest} manager · {realtime.label(fact['confirmed_at'])} · field report"]
    r = c.post(f"/api/signals/{rep['signal_id']}/withdraw", json={}, headers=key())
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")  # confirmed: too late to withdraw
    event = file_report(c, "DEMAND_EVENT", {"multiplier": 1.5, "valid_from": today, "valid_to": today}).json()
    as_seat(c, src)
    r = c.post(f"/api/signals/{event['signal_id']}/withdraw", json={}, headers=key())
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")  # only the reporter withdraws
    as_seat(c, dest)
    r = c.post(f"/api/signals/{event['signal_id']}/withdraw", json={}, headers=key())
    assert (r.status_code, r.json()["state"]) == (200, "WITHDRAWN"), r.json()
    # transport: HQ's to confirm
    as_seat(c, None)
    run_code = active(run, cid)["engine_input"]["runs"][0]["run_code"]
    quote = file_report(c, "TRANSPORT", {"run_code": run_code, "what": "QUOTE", "amount": 25,
                                         "pricing_basis": "PER_TRIP", "includes_handling": "YES",
                                         "applies_on": today, "valid_until": today}).json()
    assert quote["owner"] == {"role": "hq", "branch": None}, quote
    as_seat(c, src)
    r = c.post(f"/api/signals/{quote['signal_id']}/confirm", json={"revision": 1}, headers=key())
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(c, None)
    out = c.post(f"/api/signals/{quote['signal_id']}/confirm", json={"revision": 1}, headers=key()).json()
    with db.tx() as conn:
        fact = db.one(conn, "SELECT * FROM reported_facts WHERE run_id = :r AND fact_id = :f", r=run_id,
                      f=out["fact_id"])
    assert out["labels"] == [f"Reported by HQ · {realtime.label(fact['confirmed_at'])} · field report"]
    # a condition observation, from the lot's manager
    as_seat(c, src)
    cond = file_report(c, "CONDITION", {"lot_id": case["lot_id"], "condition": "DAMAGE", "hold": False},
                       text="Crushed crates in the chiller").json()
    assert cond["impact_label"] == REVIEW_ONLY, cond
    assert c.get(f"/api/signals/{cond['signal_id']}").json().get("preview") is None
    # the allowance is the workspace's: four reports from three seats, read from a fourth
    as_seat(c, third)
    assert c.get("/api/signals").json()["allowance"]["reports_left"] == left - 4
    with db.tx() as conn:
        assert db.one(conn, "SELECT coalesce(sum(count), 0) AS n FROM usage_counts WHERE kind = 'signal' AND "
                            "scope = :s", s=f"run:{run_id}")["n"] == 4


def criterion_6(t: Template, **_) -> None:
    """A blocked case always has a next action: after Can't check the row reads "Blocked · stock/expiry verification
    required" with the responsible branch; Select another plan offers only engine-generated options, rechecks
    feasibility, evaluates Required checks against the selected plan and starts a fresh approval round, an approval
    for the old plan can't commit it, and a lot hold stays; Cancel approved plan on APPROVED or PREPARED work records
    who, when and why, releases reservations, and leaves work that can't be prepared or executed, the original kept
    in history marked cancelled."""
    run = t.clone()
    case = judge(run, t)
    cid, src = case["case_id"], seat(case["source_branch"])
    rc = required_check(run, cid)
    q = ask(run, case_now(run, cid), rc["fact_id"])
    call(run, signals.cant_check, to_seat(q), q["request_id"])
    d = detail(run, cid)
    row = next(c for c in d["fact_checks"] if c["fact_id"] == rc["fact_id"])
    assert (row["blocked_label"], row["owner_branch"]) == (BLOCKED_LABEL, rc["branch"])
    assert {"ask_again", "select_plan", "close_proposal"} <= set(d["commitment"]["next_actions"])
    assert card(run, cid)["blocked"]
    result = active(run, cid)["result"]
    assert {o["key"] for o in d["options"]} == {o["key"] for o in result["options"]}  # engine-generated only
    plan = cases.parse_plan(result["recommended"])
    e = refused(call, run, selection.select, src, cid, version(run, cid), "tr:Nowhere:R-99:1:hold")
    assert (e.status, e.code) == (400, "not_an_option")
    owned_send(run, src, cid, version(run, cid))  # round n on the blocked plan, the destination accepting
    n = case_now(run, cid)["round"]
    if plan.kind == TRANSFER:
        decide(run, cid, seat(plan.dest), "accept")
    lot = world_lot(run, case["lot_id"])
    hold = call(run, lotholds.place, seat(lot["branch"]), case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    full = t.clone()  # Select another plan rechecks feasibility today: a transfer option whose truck is now full
    fc = judge(full, t)
    other_tr = next(o["key"] for o in active(full, fc["case_id"])["result"]["options"]
                    if o["key"] != result["recommended"] and o["key"].startswith("tr:"))
    oplan = cases.parse_plan(other_tr)
    with db.tx() as conn:
        eff = effective(cases.current_input(conn, fresh(full["run_id"]), case_now(full, fc["case_id"])))
    trip = next(r for r in eff.runs if r.run_code == oplan.run_code and r.dest == oplan.dest)
    taken = sum(n_ for code, n_ in eff.bookings.run_totes if code == trip.run_code)
    add_booking(full["run_id"], "OTHER", "TRUCK", f"{trip.run_code}|{trip.day}", trip.day, trip.spare_totes - taken)
    e = refused(call, full, selection.select, seat(fc["source_branch"]), fc["case_id"], version(full, fc["case_id"]),
                other_tr)
    assert (e.status, e.code) == (409, "conflict") and CAP_REASON["truck"] in e.extra["reasons"]
    assert case_now(full, fc["case_id"])["selected"] is None
    escape = next((o for o in d["options"] if o["feasible"] and not o["required_fact_ids"]), None)
    assert escape is not None, "no engine option escapes the Required check"
    out = call(run, selection.select, src, cid, version(run, cid), escape["key"])
    assert (out["state"], out["selected"]["plan_key"]) == ("OPEN", escape["key"])
    (cancelled,) = logged(run, cid, "approval_cancelled")
    assert (cancelled["payload"]["reason"], cancelled["payload"]["round"]) == ("plan selected", n)
    d = detail(run, cid)
    assert [b["kind"] for b in d["commitment"]["blocks"]] == ["lot_hold"]  # Required checks of the selected plan: none
    assert [h["hold_id"] for h in d["holds"] if h["state"] == "ACTIVE"] == [hold["hold_id"]]  # the hold stays
    assert owned_send(run, src, cid, version(run, cid)) == {"state": "PENDING_APPROVAL", "round": n + 1}
    e = refused(decide, run, cid, src, "approve", result["recommended"])  # an approval for the old plan
    assert (e.status, e.code) == (409, "stale_plan")
    e = refused(decide, run, cid, src, "approve")
    assert (e.code, [b["kind"] for b in e.extra["blocks"]]) == ("commitment_blocked", ["lot_hold"])
    call(run, lotholds.release, seat(lot["branch"]), hold["hold_id"], "Crates inspected, stock fine")
    if cases.parse_plan(escape["key"]).kind == TRANSFER:
        decide(run, cid, seat(cases.parse_plan(escape["key"]).dest), "accept")
    done = decide(run, cid, src, "approve")
    assert done["state"] in ("APPROVED", "EXECUTED") and logged(run, cid, "committed")[-1]["payload"]["plan"] == \
        escape["key"]
    why = "The destination's chiller failed"
    for from_state in ("APPROVED", "PREPARED"):  # Cancel approved plan
        w = t.clone()
        wc = judge(w, t)
        wid, wsrc = wc["case_id"], seat(wc["source_branch"])
        chosen = approvable(w, wid)
        assert approve(w, wid)["state"] == "APPROVED", chosen
        if from_state == "PREPARED":
            assert call(w, workflow.prepare, wsrc, wid) == {"state": "PREPARED"}
        orders = rows(w, "world_actions", wid)
        assert refused(call, w, closing.cancel, seat(None), wid, why).status == 403
        assert refused(call, w, closing.cancel, wsrc, wid, "  ").status == 400
        assert call(w, closing.cancel, wsrc, wid, why) == {"state": "CLOSED", "closed_as": "CANCELLED"}
        row = case_now(w, wid)
        assert (row["closed_by"], row["cancel_reason"]) == (wsrc["account"], why) and row["closed_at"] is not None
        assert {r["state"] for r in rows(w, "reservations", wid)} <= {"RELEASED"}  # reservations released
        assert all(d_["cancelled_at"] is not None for d_ in rows(w, "decisions", wid) if d_["round"] == wc["round"])
        (entry,) = logged(w, wid, "cancelled")
        assert (entry["payload"]["by"], entry["payload"]["reason"], entry["payload"]["from_state"]) == (
            wsrc["account"], why, from_state)
        assert refused(call, w, workflow.prepare, wsrc, wid).code == "wrong_state"  # can't be prepared
        after = rows(w, "world_actions", wid)
        assert sorted(a["action_id"] for a in after) == sorted(a["action_id"] for a in orders)  # kept in history
        assert {a["state"] for a in after} <= {"CANCELLED"}
        if from_state == "PREPARED":  # nothing executes the cancelled work: neither a person nor the world step
            assert orders
            condition = execution.settings_section()[execution.CONDITIONS][0]["key"]
            for a in orders:
                e = refused(call, w, execution.dispatch, wsrc, wid, a["action_id"], 1, a["lot_id"], condition) \
                    if a["kind"] == "TRANSFER" else \
                    refused(call, w, execution.price_applied, wsrc, wid, a["action_id"], True)
                assert e.status == 409, (a["kind"], e.code)
            if refdata.get(w["dataset_version"]).kind == "public":  # a public workspace has no Next day (05.2 D-37 B)
                assert refused(advance_once, w).code == "not_available"
            else:
                assert advance_once(w)["advanced"] is True
            assert {a["state"] for a in rows(w, "world_actions", wid)} == {"CANCELLED"}
            assert (case_now(w, wid)["state"], logged(w, wid, "executed")) == ("CLOSED", [])


def criterion_7(t: Template, **_) -> None:
    """Closing a plan never hides at-risk stock: after a reject or cancel, a lot still at risk stays on the board as
    "Needs decision" or "Awaiting verification" and is reassessed on changed inputs or Reassess; unchanged inputs never
    create a duplicate case; checks and holds carry over; a successor links to the previous case."""
    run = t.clone()
    case = judge(run, t)
    cid, src = case["case_id"], seat(case["source_branch"])
    required = [c["fact_id"] for c in in_force(run, cid) if not c["pick_feasible_if_alt"]]
    unconfirmed = {c["fact_id"] for c in in_force(run, cid) if c["changes_plan"]}
    assert call(run, workflow.reject, src, cid, "Not this week") == {"state": "REJECTED"}
    row = case_now(run, cid)
    assert row["lot_status"] == "AT_RISK"
    expected = AWAITING if required else NEEDS  # D-24: a Required check of the closed plan still in force
    assert card(run, cid)["lot_status"] == expected  # still on the board
    assert call(run, closing.reassess_lots, [case["lot_id"]], "test") == []  # unchanged inputs: no duplicate
    with db.tx() as conn:
        assert db.rows(conn, "SELECT 1 FROM cases WHERE run_id = :r AND predecessor = :c", r=run["run_id"], c=cid) == []
    lot = world_lot(run, case["lot_id"])
    hold = call(run, lotholds.place, seat(lot["branch"]), case["lot_id"], "CONDITION", "Crushed crates in the chiller")
    assert card(run, cid)["lot_status"] == AWAITING
    out = call(run, closing.reassess, src, cid)
    with db.tx() as conn:
        (succ,) = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND predecessor = :c", r=run["run_id"], c=cid)
    assert out == {"case_id": succ["case_id"], "key": succ["key"]} and succ["state"] in cases.LIVE_STATES
    assert (succ["lot_id"], succ["active_rec_version"] is not None) == (case["lot_id"], True)
    assert (card(run, cid)["successor"], card(run, succ["case_id"])["predecessor"]) == (succ["key"], cid)
    assert call(run, closing.reassess, src, cid) == out  # no second case
    blocks = detail(run, succ["case_id"])["commitment"]["blocks"]
    assert any(b["kind"] == "lot_hold" and b["hold_id"] == hold["hold_id"] for b in blocks)  # the hold carries over
    assert unconfirmed <= {c["fact_id"] for c in in_force(run, succ["case_id"])}  # the checks carry over
    assert set(required) <= {b.get("fact_id") for b in blocks if b["kind"] == "required_check"}
    other = t.clone()  # a cancelled approved plan follows the same rule
    oc = judge(other, t)
    approvable(other, oc["case_id"])
    assert approve(other, oc["case_id"])["state"] == "APPROVED"
    call(other, closing.cancel, seat(oc["source_branch"]), oc["case_id"], "The destination's chiller failed")
    closed = case_now(other, oc["case_id"])
    assert (closed["state"], closed["closed_as"], closed["lot_status"]) == ("CLOSED", "CANCELLED", "AT_RISK")
    assert card(other, oc["case_id"])["lot_status"] in (NEEDS, AWAITING)


CRITERIA = {"1": criterion_1, "2": criterion_2, "2-mixed-dates": criterion_2_mixed_dates, "3": criterion_3,
            "4": criterion_4, "4-advisory": criterion_4_advisory, "5": criterion_5, "6": criterion_6,
            "7": criterion_7}


@pytest.mark.parametrize("criterion", list(CRITERIA))
@pytest.mark.parametrize("dataset", DATASETS)
def test_success_criteria(dataset, criterion, tpl, monkeypatch, tmp_path):
    """The ROADMAP's Phase 7 success criterion `criterion` on the `dataset` template, by tag and story beat. A failing
    criterion names itself."""
    try:
        CRITERIA[criterion](tpl, monkeypatch=monkeypatch, tmp_path=tmp_path)
    except AssertionError as exc:
        raise AssertionError(f"Phase 7 success criterion {criterion} ({TITLES[criterion]}) fails on the {dataset} "
                             f"template ({tpl.tag}): {exc}") from exc
