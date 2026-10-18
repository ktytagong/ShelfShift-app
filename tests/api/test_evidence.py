"""The count path of "verify before you move" (spec §7; 04-API-CONTRACT "Count path"; 04-CONTEXT D-05, D-12, D-20).

The story case is found by its tag and the phantom fact by its kind on the active version, never by a lot id or an
amount from the seed. Expected values come from the stored engine output the test reads (the fact's branch, lot ids
and best plan if the alternative is true, decide_by) and from the contract's rules (the lead minutes from settings);
the counts a test enters are its own values. Hold expiry and caps are never tested by sleeping.
"""
import threading
from datetime import datetime

import pytest

from api import cases, config, db, facts, realtime, refdata, sandbox, signals, workflow
from api.errors import ApiError
from tests.api.conftest import by_tag, fresh, own_series, owned_send, seat, world_lot
from tests.api.test_advance import advance_once
from tests.api.test_workflow import call, state, version
from tests.api.verify_helpers import ack, shown  # approve and accept acknowledge (D-26) and name the plan (07 review C)


def story(run) -> dict:
    return by_tag(run, cases.STORY_TAG)


def active(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run["run_id"], case_id))


def phantom(run, case_id) -> dict:
    """The active version's plan-changing phantom fact (D-15 hand-back if the template lost it)."""
    fact = next((f for f in active(run, case_id)["fact_checks"]
                 if f["kind"] == "PHANTOM_STOCK" and f["changes_plan"]), None)
    assert fact is not None, "D-15 hand-back: SUS-19 has no plan-changing phantom fact"
    return fact


def source(run, case_id) -> dict:
    with db.tx() as conn:
        return seat(cases.case_row(conn, run["run_id"], case_id)["source_branch"])


def ask(run, case_id, fact_id=None, actor=None, v=None) -> dict:
    return call(run, signals.request_evidence, actor or source(run, case_id), case_id, v or version(run, case_id),
                fact_id or phantom(run, case_id)["fact_id"])


def answer(run, q, counts=None, text="", actor=None, observed="now") -> dict:
    """The addressed branch answers; it states when it checked ("Checked just now" unless a test is about the check
    time; 07-CONTEXT D-22)."""
    return call(run, signals.answer, actor or seat(q["to_branch"]), q["request_id"], text, counts, observed)


def confirm(run, actor, signal_id, revision, checked_both=True) -> dict:
    return call(run, signals.confirm, actor, signal_id, revision, checked_both)


def inbox(run, actor) -> list[dict]:
    with db.tx() as conn:
        return signals.inbox(conn, fresh(run["run_id"]), actor)


def from_inbox(run, q) -> dict:
    return next(r for r in inbox(run, seat(q["to_branch"])) if r["request_id"] == q["request_id"])


def counted(q, value=0.0) -> dict:
    """Both counts for every lot of the request."""
    return {lot: {"shelf_units": value, "backroom_units": value} for lot in q["lot_ids"]}


def lot_row(run, lot_id) -> dict:
    return world_lot(run, lot_id)  # as the workspace reads it (api/overlay.py)


def request_row(run, q) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND request_id = :q",
                      r=run["run_id"], q=q["request_id"])


def past_due(run, q) -> None:
    """The request's deadline is one second behind the database's clock (the test's own move; nothing sleeps)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE evidence_requests SET due_at = now() - interval '1 second' WHERE run_id = :r "
                     "AND request_id = :q", r=run["run_id"], q=q["request_id"])


def legacy_expired(run, q) -> None:
    """A legacy EXPIRED row, as stored before 07-CONTEXT D-05 (nothing sets EXPIRED any more): set by UPDATE, with
    no due_at."""
    with db.tx() as conn:
        db.run(conn, "UPDATE evidence_requests SET state = 'EXPIRED', due_at = NULL WHERE run_id = :r "
                     "AND request_id = :q", r=run["run_id"], q=q["request_id"])


def badges(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return cases.detail(conn, fresh(run["run_id"]), case_id)["badges"]


def confirmed_at(run, fact_id) -> str:
    """The fact's confirmation time as its label shows it ("16 Oct 2026, 20:45", 07-CONTEXT D-09)."""
    with db.tx() as conn:
        return realtime.label(db.one(conn, "SELECT confirmed_at FROM reported_facts WHERE run_id = :r AND fact_id = :f",
                                     r=run["run_id"], f=fact_id)["confirmed_at"])


def full_loop(run, case_id, value=0.0):
    """Ask, answer both counts for every lot, read the request back from the inbox and confirm with its answer."""
    q = ask(run, case_id)
    answer(run, q, counted(q, value))
    r = from_inbox(run, q)
    out = confirm(run, seat(q["to_branch"]), r["answer_signal_id"], r["answer"]["revision"])
    return q, out


def test_a_count_request_goes_to_the_branch_holding_every_lot_before_the_deadline(run):
    case = story(run)
    fact = phantom(run, case["case_id"])
    q = ask(run, case["case_id"])
    assert (q["to_branch"], q["lot_ids"], q["fact_id"], q["state"]) == (fact["branch"], fact["lot_ids"],
                                                                        fact["fact_id"], "OPEN")
    assert q["to_branch"] != case["source_branch"]  # the lots are at the destination, not with the asker
    assert all(lot_row(run, l)["branch"] == q["to_branch"] for l in q["lot_ids"])
    assert all(l in q["ask_text"] for l in q["lot_ids"])
    assert (q["answer_signal_id"], q["answer"], q["partial_note"]) == (None, None, None)
    by = active(run, case["case_id"])["result"]["decide_by"]
    deadline = by["day"] * 1440 + by["latest_decision_min"]
    assert q["due_day"] * 1440 + q["due_min"] == deadline - config.REQUEST_LEAD_MIN < deadline


def test_the_full_loop_moves_the_plan_and_approves_nothing(run):
    case = story(run)
    v0 = version(run, case["case_id"])
    fact = phantom(run, case["case_id"])
    q, out = full_loop(run, case["case_id"], value=0.0)  # the phantom is real: the engine's own alternative (0)
    v1 = version(run, case["case_id"])
    assert v1 != v0 and {"case_id": case["case_id"], "key": "SUS-19", "rec_version": v1} in out["cases"]
    assert active(run, case["case_id"])["result"]["recommended"] == fact["best_plan_if_alt"]
    day = fresh(run["run_id"])["day"]
    for lot_id in q["lot_ids"]:
        row = lot_row(run, lot_id)
        assert (row["qty"], row["last_verified_day"], row["verified_by"]) == (0.0, day, "COUNT")
    stamp = confirmed_at(run, out["fact_id"])  # the real confirmation time (07-CONTEXT D-09, D-21)
    assert out["labels"] == [f"Count confirmed by {q['to_branch']} manager · {stamp} · shelf 0 + backroom 0 = 0"] * len(
        q["lot_ids"])
    assert request_row(run, q)["state"] == "CONFIRMED"
    assert state(run, case["case_id"]) == "OPEN"
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM reservations WHERE run_id = :r AND state IN ('HELD', 'COMMITTED') "
                             "AND case_id = :c", r=run["run_id"], c=case["case_id"]) == []
        assert db.rows(conn, "SELECT * FROM decisions WHERE run_id = :r AND case_id = :c",
                       r=run["run_id"], c=case["case_id"]) == []
        facts = db.rows(conn, "SELECT * FROM reported_facts WHERE run_id = :r", r=run["run_id"])
    assert [a["counted_day"] for f in facts for a in f["adjustments"]] == [day] * len(q["lot_ids"])
    assert not any(b["kind"] == "unverified" for b in badges(run, case["case_id"]))


def test_the_activity_shows_the_flag_the_count_and_the_new_version(run):
    case = story(run)
    v0 = version(run, case["case_id"])
    fact = phantom(run, case["case_id"])
    full_loop(run, case["case_id"])
    v1 = version(run, case["case_id"])
    with db.tx() as conn:
        log = db.rows(conn, "SELECT type, payload FROM activity_log WHERE run_id = :r AND case_id = :c ORDER BY seq",
                      r=run["run_id"], c=case["case_id"])
        old = db.one(conn, "SELECT fact_checks FROM recommendations WHERE run_id = :r AND case_id = :c "
                           "AND rec_version = :v", r=run["run_id"], c=case["case_id"], v=v0)
    start = next(i for i, e in enumerate(log) if e["type"] == "evidence_requested")
    story_types = [e["type"] for e in log[start:]]
    assert story_types == ["evidence_requested", "evidence_answered", "count_confirmed", "new_version"]
    flag, _, count, new = log[start:]
    assert (flag["payload"]["fact_id"], flag["payload"]["lot_ids"]) == (fact["fact_id"], fact["lot_ids"])
    assert flag["payload"]["decision_regret"] == fact["decision_regret"]
    assert {"case_id": case["case_id"], "rec_version": v1} == {k: c[k] for c in count["payload"]["cases"]
                                                              if c["case_id"] == case["case_id"]
                                                              for k in ("case_id", "rec_version")}
    assert new["payload"]["rec_version"] == v1
    assert any(f["fact_id"] == fact["fact_id"] and f["changes_plan"] for f in old["fact_checks"])


# --- the count rules (Task 2) -----------------------------------------------------------------------------------------

def refused(run, fn, *args) -> ApiError:
    with pytest.raises(ApiError) as e:
        call(run, fn, *args)
    return e.value


def add_facts(run, case_id, facts: list[dict]) -> None:
    """Extra flagged facts on the active version's stored fact checks (a test's own values)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE recommendations SET fact_checks = fact_checks || CAST(:x AS jsonb) WHERE run_id = :r "
                     "AND case_id = :c AND rec_version = :v", x=db.js(facts), r=run["run_id"], c=case_id,
               v=version(run, case_id))


def table(run_id, name) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, f"SELECT * FROM {name} WHERE run_id = :r", r=run_id)


def not_preloaded(rows: list[dict]) -> list[dict]:
    """The rows a test's own actions could have written: the workspace's preloaded material (provenance 'simulated',
    07-16, D-18) sits beside them in the same tables."""
    return [r for r in rows if r.get("provenance") != "simulated"]


def test_only_the_source_manager_may_ask(run):
    case = story(run)
    fact = phantom(run, case["case_id"])
    plan = cases.parse_plan(active(run, case["case_id"])["result"]["recommended"])
    branches = refdata.get(run["dataset_version"]).branches
    other = next(b for b in branches if b not in (case["source_branch"], plan.dest))
    for actor in (seat(plan.dest), seat(None), seat(other)):
        e = refused(run, signals.request_evidence, actor, case["case_id"], version(run, case["case_id"]),
                    fact["fact_id"])
        assert (e.status, e.code) == (403, "forbidden")
    assert table(run["run_id"], "evidence_requests") == []


def asked_entries(run_id) -> list[dict]:
    return [a for a in table(run_id, "activity_log") if a["type"] == "evidence_requested"]


def test_a_finished_case_takes_no_count_request(run):
    """G-05-2 item 11: once a case is past the states a count re-plans (cases.LIVE_STATES), a request is refused
    (409 wrong_state) and nothing is written; the seat check still comes first."""
    case = story(run)
    fact = phantom(run, case["case_id"])
    v = version(run, case["case_id"])
    call(run, workflow.reject, source(run, case["case_id"]), case["case_id"], "store will run a promotion")
    assert state(run, case["case_id"]) == "REJECTED"
    requests, entries = table(run["run_id"], "evidence_requests"), asked_entries(run["run_id"])
    e = refused(run, signals.request_evidence, source(run, case["case_id"]), case["case_id"], v, fact["fact_id"])
    assert (e.status, e.code, e.message, e.extra["state"]) == (409, "wrong_state", "Case is REJECTED", "REJECTED")
    assert table(run["run_id"], "evidence_requests") == requests and asked_entries(run["run_id"]) == entries
    e = refused(run, signals.request_evidence, seat(None), case["case_id"], v, fact["fact_id"])
    assert (e.status, e.code) == (403, "forbidden")  # the seat is checked before the state


def test_a_waiting_or_pending_case_still_takes_a_count_request(run):
    """A WAITING or PENDING_APPROVAL story case is still live (cases.LIVE_STATES): its count can re-plan it."""
    case = story(run)
    day = fresh(run["run_id"])["day"]
    assert call(run, workflow.wait, source(run, case["case_id"]), case["case_id"], day + 1, "recount tomorrow") == \
        {"state": "WAITING"}
    q = ask(run, case["case_id"])
    assert q["state"] == "OPEN" and state(run, case["case_id"]) == "WAITING"
    with db.tx() as conn:  # a second sandbox from the same template, for the pending story case
        other_id = sandbox.clone(conn, sandbox.active_template(conn))
    other = fresh(other_id)
    pending = story(other)
    sent = owned_send(other, source(other, pending["case_id"]), pending["case_id"],
                      version(other, pending["case_id"]))
    assert sent["state"] == "PENDING_APPROVAL"
    q = ask(other, pending["case_id"])
    assert q["state"] == "OPEN" and state(other, pending["case_id"]) == "PENDING_APPROVAL"


def test_a_count_request_spends_no_ai_cap(run):
    with db.tx() as conn:
        before = db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope, kind, window_start")
    full_loop(run, story(run)["case_id"])
    with db.tx() as conn:
        assert db.rows(conn, "SELECT * FROM usage_counts ORDER BY scope, kind, window_start") == before


def test_only_a_flagged_fact_on_the_active_version_can_be_asked_about(run):
    case = story(run)
    fact = phantom(run, case["case_id"])
    add_facts(run, case["case_id"], [{**fact, "fact_id": "TEST:unflagged", "changes_plan": False}])
    for fact_id in ("TEST:unflagged", "TEST:nowhere"):
        e = refused(run, signals.request_evidence, source(run, case["case_id"]), case["case_id"],
                    version(run, case["case_id"]), fact_id)
        assert (e.status, e.code) == (400, "unknown_fact")
    v0 = version(run, case["case_id"])
    full_loop(run, case["case_id"])  # a new active version; the flag stays on v0
    e = refused(run, signals.request_evidence, source(run, case["case_id"]), case["case_id"], v0, fact["fact_id"])
    assert (e.status, e.code, e.extra["active_rec_version"]) == (409, "stale_version", version(run, case["case_id"]))


def test_asking_twice_returns_the_same_request(run):
    case = story(run)
    first, again = ask(run, case["case_id"]), ask(run, case["case_id"])
    assert same_request(first, again)
    assert len(table(run["run_id"], "evidence_requests")) == 1
    asked = [a for a in table(run["run_id"], "activity_log") if a["type"] == "evidence_requested"]
    assert len(asked) == 1


def test_at_most_two_requests_per_version(run):
    case = story(run)
    fact = phantom(run, case["case_id"])
    limit = config.MAX_REQUESTS_PER_VERSION
    assert limit == 2  # the settings value (api/settings.json requests.max_per_version)
    extra = [{**fact, "fact_id": f"TEST:{i}", "kind": "STALE_COUNT"} for i in range(limit)]
    add_facts(run, case["case_id"], extra)
    asked = [ask(run, case["case_id"], f["fact_id"]) for f in [fact, *extra][:limit]]
    e = refused(run, signals.request_evidence, source(run, case["case_id"]), case["case_id"],
                version(run, case["case_id"]), extra[-1]["fact_id"])
    assert (e.status, e.code, e.message) == (409, "too_many_requests", f"At most {limit} count requests per case version")
    assert ask(run, case["case_id"])["request_id"] == asked[0]["request_id"]  # asking again is not a new request


def test_a_reply_without_counts_keeps_the_request_open_and_corrects_nothing(run):
    case = story(run)
    v0 = version(run, case["case_id"])
    q = ask(run, case["case_id"])
    before = {l: lot_row(run, l) for l in q["lot_ids"]}
    s = answer(run, q, {}, text="The backroom is locked until noon")
    assert s["fields"]["needs_input"] == [{"lot_id": l, "field": f} for l in q["lot_ids"]
                                          for f in ("shelf_units", "backroom_units")]
    r = request_row(run, q)
    assert (r["state"], r["partial_note"], r["answer_signal_id"]) == ("OPEN", "reply received · count missing",
                                                                     s["signal_id"])
    lot = q["lot_ids"][0]
    answer(run, q, {lot: {"shelf_units": 4, "backroom_units": None}})  # a count with a blank is partial too
    assert request_row(run, q)["state"] == "OPEN"
    assert {l: lot_row(run, l) for l in q["lot_ids"]} == before
    assert table(run["run_id"], "reported_facts") == [] and version(run, case["case_id"]) == v0


def test_a_blank_backroom_is_not_zero(run):
    case = story(run)
    q = ask(run, case["case_id"])
    lot = q["lot_ids"][0]
    s = answer(run, q, {**counted(q), lot: {"shelf_units": 0, "backroom_units": None}})
    assert s["fields"]["counts"][lot] == {"shelf_units": 0.0, "backroom_units": None}
    assert s["fields"]["needs_input"] == [{"lot_id": lot, "field": "backroom_units"}]
    e = refused(run, signals.confirm, seat(q["to_branch"]), s["signal_id"], s["revision"], True)
    assert (e.status, e.code, e.extra["fields"]) == (409, "needs_input", [{"lot_id": lot, "field": "backroom_units"}])
    assert table(run["run_id"], "reported_facts") == []


def test_only_the_asked_branch_answers(run):
    case = story(run)
    q = ask(run, case["case_id"])
    for actor in (seat(case["source_branch"]), seat(None)):
        e = refused(run, signals.answer, actor, q["request_id"], "", counted(q), "now")
        assert (e.status, e.code) == (403, "forbidden")
    s = answer(run, q, counted(q))
    for actor in (seat(case["source_branch"]), seat(None)):
        e = refused(run, signals.confirm, actor, s["signal_id"], s["revision"], True)
        assert (e.status, e.code) == (403, "forbidden")
    assert request_row(run, q)["state"] == "ANSWERED"


def test_a_count_for_another_lot_is_refused(run):
    case = story(run)
    q = ask(run, case["case_id"])
    assert case["lot_id"] not in q["lot_ids"]
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "",
                {**counted(q), case["lot_id"]: {"shelf_units": 1, "backroom_units": 1}}, "now")
    assert (e.status, e.code, e.extra["lot_id"]) == (409, "count_mismatch", case["lot_id"])
    assert not_preloaded(table(run["run_id"], "signals")) == [] and request_row(run, q)["state"] == "OPEN"


def test_the_count_needs_the_scope_tick(run):
    case = story(run)
    q = ask(run, case["case_id"])
    s = answer(run, q, counted(q))
    for tick in (False, None, "yes"):
        e = refused(run, signals.confirm, seat(q["to_branch"]), s["signal_id"], s["revision"], tick)
        assert (e.status, e.code) == (409, "scope_tick")
    assert request_row(run, q)["state"] == "ANSWERED" and table(run["run_id"], "reported_facts") == []


def test_an_empty_answer_is_refused(run):
    case = story(run)
    q = ask(run, case["case_id"])
    for text, counts in (("  ", {}), ("", None), ("", {l: {"shelf_units": None, "backroom_units": None}
                                                       for l in q["lot_ids"]})):
        e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], text, counts, "now")
        assert (e.status, e.code) == (400, "bad_request")
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", {q["lot_ids"][0]: {"shelf_units": -1}},
                "now")
    assert (e.status, e.code) == (400, "bad_request")
    assert not_preloaded(table(run["run_id"], "signals")) == [] and request_row(run, q)["answer_signal_id"] is None


def test_every_lot_in_a_phantom_fact_must_be_counted(run):
    case = story(run)
    first = phantom(run, case["case_id"])
    base = lot_row(run, first["lot_ids"][0])
    own_series(run, base["branch"], base["sku"])  # the test writes the series directly
    with db.tx() as conn:  # a second uncounted lot at the phantom branch (a test lot, not a seed lot)
        db.run(conn, "INSERT INTO lots (run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, "
                     "markdown_pct, expiry_source, last_verified_day, verified_by) VALUES (:r, 'shelfshift', "
                     "'TEST-LOT-2', :b, :s, 12.0, :e, :seq, 0, :es, :lv, 'COUNT')", r=run["run_id"],
               b=base["branch"], s=base["sku"], e=base["expiry_day"], seq=base["received_seq"] + 1,
               es=base["expiry_source"], lv=base["last_verified_day"])
        db.run(conn, "INSERT INTO truth_lots (run_id, world, lot_id, qty) VALUES (:r, 'shelfshift', 'TEST-LOT-2', "
                     "12.0)", r=run["run_id"])
        r = fresh(run["run_id"])
        cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], case["case_id"]))
    fact = phantom(run, case["case_id"])
    assert fact["lot_ids"] == sorted([*first["lot_ids"], "TEST-LOT-2"])
    q = ask(run, case["case_id"])
    assert q["lot_ids"] == fact["lot_ids"]
    one, two = q["lot_ids"][0], q["lot_ids"][1]
    s1 = answer(run, q, {one: {"shelf_units": 0, "backroom_units": 0}})
    assert s1["fields"]["needs_input"] == [{"lot_id": two, "field": "shelf_units"},
                                           {"lot_id": two, "field": "backroom_units"}]
    e = refused(run, signals.confirm, seat(q["to_branch"]), s1["signal_id"], s1["revision"], True)
    assert (e.code, e.extra["fields"]) == ("needs_input", s1["fields"]["needs_input"])
    s2 = answer(run, q, {one: {"shelf_units": 0, "backroom_units": 0}, two: {"shelf_units": 3, "backroom_units": 5}})
    assert s2["revision"] == s1["revision"] + 1 and request_row(run, q)["state"] == "ANSWERED"
    e = refused(run, signals.confirm, seat(q["to_branch"]), s1["signal_id"], s1["revision"], True)
    assert (e.code, e.extra["state"]) == ("wrong_state", "DISMISSED")  # the replaced answer is gone
    e = refused(run, signals.confirm, seat(q["to_branch"]), s2["signal_id"], s1["revision"], True)
    assert (e.code, e.extra["revision"]) == ("stale_version", s2["revision"])
    out = confirm(run, seat(q["to_branch"]), s2["signal_id"], s2["revision"])
    day = fresh(run["run_id"])["day"]
    (fact_row,) = table(run["run_id"], "reported_facts")
    assert fact_row["adjustments"] == [{"kind": "stock_count", "lot_id": one, "units": 0.0, "counted_day": day},
                                       {"kind": "stock_count", "lot_id": two, "units": 8.0, "counted_day": day}]
    assert (lot_row(run, one)["qty"], lot_row(run, two)["qty"]) == (0.0, 8.0)
    stamp = confirmed_at(run, out["fact_id"])
    assert out["labels"][1] == f"Count confirmed by {q['to_branch']} manager · {stamp} · shelf 3 + backroom 5 = 8"
    assert not any(f["kind"] == "PHANTOM_STOCK" and f["branch"] == q["to_branch"]
                   for f in active(run, case["case_id"])["fact_checks"])  # one round clears the whole fact


def test_an_unanswered_request_reads_unanswered_after_its_deadline_and_next_day_sets_nothing(run):
    """07-CONTEXT D-05 (the sanctioned rewrite of the Next-day expiry test, 07-04): an unanswered request reads
    Unanswered after its deadline, Next day sets nothing, and Send is never blocked."""
    case = story(run)
    q = ask(run, case["case_id"])
    assert q["due_day"] == fresh(run["run_id"])["day"]  # the legacy due day: decide_by is on today's run
    assert "evidence_requested" in [b["kind"] for b in badges(run, case["case_id"])]
    sent = owned_send(run, source(run, case["case_id"]), case["case_id"], version(run, case["case_id"]))
    assert sent["state"] == "PENDING_APPROVAL"  # a pending check never blocks send
    past_due(run, q)
    assert (from_inbox(run, q)["status_kind"], request_row(run, q)["state"]) == ("UNANSWERED", "OPEN")
    assert "evidence_requested" not in [b["kind"] for b in badges(run, case["case_id"])]
    advance_once(run)
    assert request_row(run, q)["state"] == "OPEN"  # Next day expires no request
    late = answer(run, q, counted(q))  # a late answer is kept (spec §7 rev 6.7; 07-CONTEXT D-08)
    assert late["state"] == "READ" and request_row(run, q)["state"] == "ANSWERED"
    assert from_inbox(run, q)["late"] is True


# --- late answers (05-16 Task 3; spec §7 rev 6.7, 07-CONTEXT D-08) -----------------------------------------------------

def signal_row(run, signal_id) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s", r=run["run_id"], s=signal_id)


def test_a_late_answer_is_kept_and_confirmed_on_its_own_day(run):
    case = story(run)
    q = ask(run, case["case_id"])
    first = answer(run, q, counted(q, 1.0))  # given on the due day and left unconfirmed
    assert request_row(run, q)["state"] == "ANSWERED"
    advance_once(run)
    legacy_expired(run, q)  # Next day sets none since D-05: a legacy EXPIRED row by UPDATE
    day = fresh(run["run_id"])["day"]
    assert request_row(run, q)["state"] == "EXPIRED" and day > q["due_day"]
    facts = table(run["run_id"], "reported_facts")
    e = refused(run, signals.confirm, seat(q["to_branch"]), first["signal_id"], first["revision"], True)
    assert (e.status, e.code, e.message, e.extra["state"]) == (409, "wrong_state", "Request is EXPIRED", "EXPIRED")
    assert table(run["run_id"], "reported_facts") == facts  # an earlier day's count is not applied to a moved record
    for other in (seat(case["source_branch"]), seat(None)):  # only the addressed manager answers, late or not
        e = refused(run, signals.answer, other, q["request_id"], "", counted(q), "now")
        assert (e.status, e.code) == (403, "forbidden")
    partial = {lot: {"shelf_units": 0.0, "backroom_units": None} for lot in q["lot_ids"]}
    answer(run, q, partial, text="shelf done, backroom later")
    row = request_row(run, q)
    assert (row["state"], row["partial_note"]) == ("EXPIRED", signals.PARTIAL)  # kept, still expired
    late = answer(run, q, counted(q, 0.0))
    row = request_row(run, q)
    assert (row["state"], row["partial_note"], row["answer_signal_id"]) == ("ANSWERED", None, late["signal_id"])
    assert late["revision"] == first["revision"] + 2 and signal_row(run, first["signal_id"])["state"] == "DISMISSED"
    assert signal_row(run, late["signal_id"])["created_day"] == day > q["due_day"]
    out = confirm(run, seat(q["to_branch"]), late["signal_id"], late["revision"])
    assert out["labels"] and request_row(run, q)["state"] == "CONFIRMED"
    added = [f for f in table(run["run_id"], "reported_facts") if f not in facts]
    assert [a["counted_day"] for f in added for a in f["adjustments"]] == [day] * len(q["lot_ids"])
    assert all(lot_row(run, l)["last_verified_day"] == day for l in q["lot_ids"])
    e = refused(run, signals.answer, seat(q["to_branch"]), q["request_id"], "", counted(q), "now")
    assert (e.code, e.message, e.extra["state"]) == ("wrong_state", "Request is CONFIRMED", "CONFIRMED")


def test_a_late_answer_on_a_finished_case_still_applies(run):
    """07-CONTEXT D-17, D-22 (the sanctioned rewrite of the late-answer case-state refusal, 07-06): the fact is about
    the lot, not the case, so a late answer confirmed after its case was rejected applies: the fact is written and the
    record corrected. The REJECTED case is history only: no new version and no review flag."""
    case = story(run)
    cid = case["case_id"]
    q = ask(run, cid)
    advance_once(run)
    legacy_expired(run, q)  # Next day sets none since D-05: a legacy EXPIRED row by UPDATE
    assert request_row(run, q)["state"] == "EXPIRED"
    late = answer(run, q, counted(q, 0.0))
    assert request_row(run, q)["state"] == "ANSWERED"
    assert state(run, cid) == "OPEN"  # the morning re-evaluated it; its lot is still on the record
    call(run, workflow.reject, source(run, cid), cid, "store will run a promotion")
    v = version(run, cid)
    out = confirm(run, seat(q["to_branch"]), late["signal_id"], late["revision"])
    assert out["fact_id"] and out["flagged"] == [] and cid not in [c["case_id"] for c in out["cases"]]
    assert [f["fact_id"] for f in table(run["run_id"], "reported_facts")] == [out["fact_id"]]
    assert all(lot_row(run, l)["qty"] == 0.0 for l in q["lot_ids"])  # the record is corrected
    assert (state(run, cid), version(run, cid)) == ("REJECTED", v) and table(run["run_id"], "review_flags") == []
    row = request_row(run, q)
    assert (row["state"], row["answer_signal_id"]) == ("CONFIRMED", late["signal_id"])
    assert signal_row(run, late["signal_id"])["state"] == "CONFIRMED"


# --- a count is confirmed only on the day it was given (05-REVIEW CR-02, WR-10) ----------------------------------------

EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it."


def due_later(run, q, days: int) -> None:
    """Move the request's due day `days` days after today (the test's own value): the shape the engine gives when the
    recommended plan's markdown step falls after today, so Next day does not expire it."""
    with db.tx() as conn:
        db.run(conn, "UPDATE evidence_requests SET due_day = :d WHERE run_id = :r AND request_id = :q",
               d=fresh(run["run_id"])["day"] + days, r=run["run_id"], q=q["request_id"])


def test_an_earlier_days_answer_is_not_confirmed_on_a_later_day(run):
    """CR-02: a request due on a later day survives Next day as ANSWERED, but the count it holds was taken the day
    before and the record has moved a day since. Confirming it is 409 stale_version naming the answer's day, and
    nothing is written; today's count for the same request is confirmed today."""
    case = story(run)
    q = ask(run, case["case_id"])
    due_later(run, q, 2)
    day = fresh(run["run_id"])["day"]
    first = answer(run, q, counted(q, 0.0))
    advance_once(run)
    today = fresh(run["run_id"])["day"]
    row = request_row(run, q)
    assert (today, row["state"]) == (day + 1, "ANSWERED") and row["due_day"] > day  # due later, so not expired
    assert state(run, case["case_id"]) in cases.LIVE_STATES
    facts, lots = table(run["run_id"], "reported_facts"), [lot_row(run, l) for l in q["lot_ids"]]
    entries = [a for a in table(run["run_id"], "activity_log") if a["type"] == "count_confirmed"]
    e = refused(run, signals.confirm, seat(q["to_branch"]), first["signal_id"], first["revision"], True)
    assert (e.status, e.code, e.message, e.extra) == (409, "stale_version", EARLIER_DAY, {"answer_day": day})
    assert table(run["run_id"], "reported_facts") == facts
    assert [lot_row(run, l) for l in q["lot_ids"]] == lots  # lots.qty is unchanged
    assert [a for a in table(run["run_id"], "activity_log") if a["type"] == "count_confirmed"] == entries
    assert request_row(run, q)["state"] == "ANSWERED" and signal_row(run, first["signal_id"])["state"] == "READ"
    seen = in_both(run, q)
    assert (seen["confirmable"], seen["answerable"]) == (False, True)  # send today's count, then confirm it
    again = answer(run, q, counted(q, 0.0))
    assert in_both(run, q)["confirmable"] is True
    confirm(run, seat(q["to_branch"]), again["signal_id"], again["revision"])
    added = [f for f in table(run["run_id"], "reported_facts") if f not in facts]
    assert [a["counted_day"] for f in added for a in f["adjustments"]] == [today] * len(q["lot_ids"])
    assert request_row(run, q)["state"] == "CONFIRMED"


def test_the_request_view_says_when_a_count_can_be_confirmed(run):
    """WR-10: `confirmable` mirrors every check confirm() makes (the request OPEN or ANSWERED, its current answer READ
    and complete, given today, every lot still on the record at the addressed branch; a late answer on a finished case
    too since 07-06, D-17/D-22), and `answerable` says whether an answer sent today could be confirmed today. Each view
    is checked against what confirm() then does."""
    case = story(run)
    cid = case["case_id"]
    q = ask(run, cid)
    assert (q["confirmable"], q["answerable"]) == (False, True)  # OPEN, no answer yet
    lot = q["lot_ids"][0]
    partial = answer(run, q, {lot: {"shelf_units": 1, "backroom_units": None}})
    assert (in_both(run, q)["confirmable"], in_both(run, q)["answerable"]) == (False, True)
    e = refused(run, signals.confirm, seat(q["to_branch"]), partial["signal_id"], partial["revision"], True)
    assert e.code == "needs_input"
    full = answer(run, q, counted(q, 0.0))
    assert (in_both(run, q)["confirmable"], in_both(run, q)["answerable"]) == (True, True)
    for b in (q["to_branch"], case["source_branch"]):  # the test moves the lot between two series directly
        own_series(run, b, case["sku"])
    with db.tx() as conn:  # the lot leaves the addressed branch's record (the test's own move): confirm would refuse
        db.run(conn, "UPDATE lots SET branch = :b WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               b=case["source_branch"], r=run["run_id"], l=lot)
    assert (in_both(run, q)["confirmable"], in_both(run, q)["answerable"]) == (False, False)
    e = refused(run, signals.confirm, seat(q["to_branch"]), full["signal_id"], full["revision"], True)
    assert (e.code, e.extra["lot_id"]) == ("count_mismatch", lot)

    late = other_sandbox()  # a late answer: live case, then finished case
    c2 = story(late)
    q2 = ask(late, c2["case_id"])
    advance_once(late)
    legacy_expired(late, q2)  # Next day sets none since D-05: a legacy EXPIRED row by UPDATE
    seen = in_both(late, q2)
    assert (seen["state"], seen["confirmable"], seen["answerable"]) == ("EXPIRED", False, True)  # the case is live
    call(late, workflow.reject, source(late, c2["case_id"]), c2["case_id"], "store will run a promotion")
    seen = in_both(late, q2)
    assert (seen["state"], seen["confirmable"], seen["answerable"]) == ("EXPIRED", False, True)  # finished: the fact
    kept = answer(late, q2, counted(q2, 0.0))  # is about the lot, so today's answer can be confirmed (D-17, D-22)
    seen = in_both(late, q2)
    assert (seen["state"], seen["confirmable"], seen["answerable"]) == ("ANSWERED", True, True)
    assert confirm(late, seat(q2["to_branch"]), kept["signal_id"], kept["revision"])["fact_id"]

    on_time = other_sandbox()  # a confirmable view is confirmed
    c3 = story(on_time)
    q3 = ask(on_time, c3["case_id"])
    a3 = answer(on_time, q3, counted(q3, 0.0))
    assert in_both(on_time, q3)["confirmable"] is True
    confirm(on_time, seat(q3["to_branch"]), a3["signal_id"], a3["revision"])
    seen = in_both(on_time, q3)
    assert (seen["state"], seen["confirmable"], seen["answerable"]) == ("CONFIRMED", False, False)


# --- Request recount (05-17; G-05-2 item 12) ------------------------------------------------------------------------

def recount(run, case_id, of, fact_id=None, actor=None, v=None) -> dict:
    """Ask again for a fact whose request `of` was answered: a new request, the original kept."""
    return call(run, signals.request_evidence, actor or source(run, case_id), case_id, v or version(run, case_id),
                fact_id or phantom(run, case_id)["fact_id"], of)


def test_a_recount_is_refused_when_it_cannot_replace_an_answered_request(run):
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    v0 = version(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:other", "kind": "STALE_COUNT"}])  # a second flagged fact

    def refused_recount(of, fact_id=fact["fact_id"], v=v0, actor=None) -> ApiError:
        requests = table(run["run_id"], "evidence_requests")
        e = refused(run, signals.request_evidence, actor or source(run, cid), cid, v, fact_id, of)
        assert table(run["run_id"], "evidence_requests") == requests  # nothing written
        return e
    q = ask(run, cid)
    e = refused_recount(q["request_id"])
    assert (e.status, e.code, e.message, e.extra["state"]) == (409, "wrong_state", "Request is OPEN", "OPEN")
    e = refused_recount("REQ-nowhere")
    assert (e.status, e.code) == (404, "not_found")
    other = ask(run, cid, "TEST:other")
    answer(run, other, counted(other))
    e = refused_recount(other["request_id"])  # another fact's request
    assert (e.status, e.code, e.message) == (400, "bad_request", "A recount must name this fact's request on this "
                                                                 "version")
    answer(run, q, counted(q))
    e = refused_recount(q["request_id"], actor=seat(q["to_branch"]))
    assert (e.status, e.code) == (403, "forbidden")
    limit = config.MAX_REQUESTS_PER_VERSION
    e = refused_recount(q["request_id"])  # the version already holds `limit` requests: a recount counts toward it
    assert (e.status, e.code, e.message) == (409, "too_many_requests", f"At most {limit} count requests per case version")
    r = from_inbox(run, q)
    confirm(run, seat(q["to_branch"]), r["answer_signal_id"], r["answer"]["revision"])
    v1 = version(run, cid)
    assert v1 != v0
    e = refused_recount(other["request_id"], fact_id="TEST:other")  # sent from the replaced version
    assert (e.status, e.code, e.extra["active_rec_version"]) == (409, "stale_version", v1)
    call(run, workflow.reject, source(run, cid), cid, "store will run a promotion")
    e = refused_recount(other["request_id"], fact_id="TEST:other", v=v1)
    assert (e.status, e.code, e.message, e.extra["state"]) == (409, "wrong_state", "Case is REJECTED", "REJECTED")


def test_a_recount_takes_the_last_request_slot_of_the_version(run):
    """Edge UI-04 boundary: one fact asked and answered, the recount is request 2 of 2; any further ask on the
    version is 409 too_many_requests."""
    case = story(run)
    cid = case["case_id"]
    fact = phantom(run, cid)
    add_facts(run, cid, [{**fact, "fact_id": "TEST:other", "kind": "STALE_COUNT"}])
    q = ask(run, cid)
    answer(run, q, counted(q))
    new = recount(run, cid, q["request_id"])
    assert len(table(run["run_id"], "evidence_requests")) == config.MAX_REQUESTS_PER_VERSION == 2
    e = refused(run, signals.request_evidence, source(run, cid), cid, version(run, cid), "TEST:other")
    assert (e.status, e.code) == (409, "too_many_requests")
    assert ask(run, cid)["request_id"] == new["request_id"]  # a plain ask returns the newest request, never a new row


def test_one_live_request_per_fact_and_every_view_names_the_engine_fact(run):
    case = story(run)
    cid = case["case_id"]
    q = ask(run, cid)
    first = answer(run, q, counted(q, 1.0))
    new = recount(run, cid, q["request_id"])
    assert (new["state"], new["fact_id"], new["lot_ids"]) == ("OPEN", q["fact_id"], q["lot_ids"])
    row = request_row(run, q)
    assert (row["state"], row["replaced_by"], row["replaced_fact_id"]) == ("SUPERSEDED", new["request_id"],
                                                                           q["fact_id"])
    assert signal_row(run, first["signal_id"])["state"] == "READ" and row["answer_signal_id"] == first["signal_id"]
    asked = [a for a in asked_entries(run["run_id"]) if a["payload"]["request_id"] == new["request_id"]]
    assert [a["payload"]["recount_of"] for a in asked] == [q["request_id"]]
    with db.tx() as conn:
        r = fresh(run["run_id"])
        views = signals.requests_for_case(conn, r, cases.case_row(conn, r["run_id"], cid))
        checks = {x["rec_version"]: {f["fact_id"] for f in x["fact_checks"]} for x in db.rows(
            conn, "SELECT rec_version, fact_checks FROM recommendations WHERE run_id = :r AND case_id = :c",
            r=r["run_id"], c=cid)}
    views += inbox(run, seat(q["to_branch"]))
    assert {x["request_id"] for x in views} == {q["request_id"], new["request_id"]}
    live = [(x["case_id"], x["rec_version"], x["fact_id"]) for x in views if x["state"] in ("OPEN", "ANSWERED")]
    assert len(set(live)) == 1  # one live request per (case, version, fact), seen once in each view
    assert all(x["fact_id"] in checks[x["rec_version"]] for x in views)
    assert new["due_day"] <= fresh(run["run_id"])["day"]
    advance_once(run)
    assert request_row(run, q)["state"] == "SUPERSEDED"
    assert request_row(run, new)["state"] == "OPEN"  # Next day expires no request (07-CONTEXT D-05)


def case_requests(run, case_id) -> list[dict]:
    with db.tx() as conn:
        r = fresh(run["run_id"])
        return signals.requests_for_case(conn, r, cases.case_row(conn, r["run_id"], case_id))


READ_TIME = ("server_now", "minutes_left")  # the database's now() of each read (07-CONTEXT D-06), not the request


def same_request(a: dict, b: dict) -> bool:
    """Two reads of one request agree on everything but the read's own clock."""
    return {k: v for k, v in a.items() if k not in READ_TIME} == {k: v for k, v in b.items() if k not in READ_TIME}


def in_both(run, q) -> dict:
    """The request as the addressed seat's inbox shows it, checked equal to the case detail's view of it."""
    seen = from_inbox(run, q)
    assert same_request(seen, next(x for x in case_requests(run, q["case_id"]) if x["request_id"] == q["request_id"]))
    return seen


def other_sandbox() -> dict:
    with db.tx() as conn:
        run_id = sandbox.clone(conn, sandbox.active_template(conn))
    return fresh(run_id)  # read after the clone commits


def test_the_request_view_says_when_a_recount_is_allowed(run):
    case = story(run)
    cid = case["case_id"]
    q = ask(run, cid)
    assert (q["recountable"], q["case_source"], q["replaced_by"]) == (False, case["source_branch"], None)
    first = answer(run, q, counted(q, 1.0))
    day = fresh(run["run_id"])["day"]
    seen = in_both(run, q)
    assert (seen["state"], seen["recountable"], seen["case_source"]) == ("ANSWERED", True, case["source_branch"])
    stored = signal_row(run, first["signal_id"])  # the seat that answered, as recorded
    assert (stored["reporter_role"], stored["reporter_branch"]) == ("manager", q["to_branch"])
    for view in (first, seen["answer"]):
        assert (view["day"], view["reporter_role"], view["reporter_branch"]) == (day, "manager", q["to_branch"])
        assert view["created_at"] == stored["created_at"].isoformat()
    second = answer(run, q, counted(q, 0.0))
    assert second["revision"] == first["revision"] + 1
    assert datetime.fromisoformat(second["created_at"]) >= datetime.fromisoformat(first["created_at"])
    new = recount(run, cid, q["request_id"])
    assert (in_both(run, q)["recountable"], in_both(run, new)["recountable"]) == (False, False)  # SUPERSEDED, OPEN
    answer(run, new, counted(new))
    full = in_both(run, new)
    assert (full["state"], full["recountable"]) == ("ANSWERED", False)  # the version holds its limit of requests
    moved = other_sandbox()  # an ANSWERED request on a version that is no longer active
    c2 = story(moved)
    q2 = ask(moved, c2["case_id"])
    answer(moved, q2, counted(q2))
    assert in_both(moved, q2)["recountable"] is True
    plan = cases.parse_plan(active(moved, c2["case_id"])["result"]["recommended"])
    trip = next(r for r in active(moved, c2["case_id"])["engine_input"]["runs"]
                if r["run_code"] == plan.run_code and r["dest"] == plan.dest)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    call(moved, workflow.adopt, source(moved, c2["case_id"]), c2["case_id"], quote, "carrier quote by phone")
    assert version(moved, c2["case_id"]) != q2["rec_version"] and state(moved, c2["case_id"]) == "OPEN"
    assert in_both(moved, q2)["recountable"] is False
    rejected = other_sandbox()  # an ANSWERED request on a REJECTED case
    c3 = story(rejected)
    q3 = ask(rejected, c3["case_id"])
    answer(rejected, q3, counted(q3))
    assert in_both(rejected, q3)["recountable"] is True
    call(rejected, workflow.reject, source(rejected, c3["case_id"]), c3["case_id"], "store will run a promotion")
    assert in_both(rejected, q3)["recountable"] is False


def test_the_unverified_badge_shows_decision_regret_until_someone_asks(run):
    case = story(run)
    rec = active(run, case["case_id"])
    floor = rec["engine_input"]["policy"]["min_regret"]
    flagged = [f for f in rec["fact_checks"]
               if f["changes_plan"] and (not f["pick_feasible_if_alt"] or f["regret"] >= floor)]
    priced = [f for f in flagged if f["pick_feasible_if_alt"]]
    (badge,) = [b for b in badges(run, case["case_id"]) if b["kind"] == "unverified"]
    assert badge == {"kind": "unverified", "text": "Unverified", "fact_ids": [f["fact_id"] for f in flagged],
                     "not_allowed_fact_ids": [f["fact_id"] for f in flagged if not f["pick_feasible_if_alt"]],
                     **{k: round(sum(f[k] for f in priced), 2) if priced else None
                        for k in ("decision_regret", "net_revenue_difference", "waste_policy_difference")}}
    q = ask(run, case["case_id"])
    after = badges(run, case["case_id"])
    assert {"kind": "evidence_requested", "text": "Evidence requested"} in after
    assert not any(q["fact_id"] in b.get("fact_ids", ()) for b in after)


def test_two_sandboxes_never_see_each_others_requests_or_facts(run):
    with db.tx() as conn:
        other = {"run_id": sandbox.clone(conn, sandbox.active_template(conn))}
    case = story(run)
    fact = phantom(run, case["case_id"])
    theirs = {l: lot_row(other, l) for l in fact["lot_ids"]}
    v_other = version(other, case["case_id"])
    q, _ = full_loop(run, case["case_id"])
    assert inbox(other, seat(q["to_branch"])) == [] and inbox(run, seat(q["to_branch"])) != []
    with db.tx() as conn:
        assert cases.detail(conn, fresh(other["run_id"]), case["case_id"])["requests"] == []
    for name in ("evidence_requests", "signals", "reported_facts"):
        assert not_preloaded(table(other["run_id"], name)) == [] and not_preloaded(table(run["run_id"], name)) != []
    assert {l: lot_row(other, l) for l in fact["lot_ids"]} == theirs
    assert version(other, case["case_id"]) == v_other
    e = refused(other, signals.answer, seat(q["to_branch"]), q["request_id"], "", counted(q), "now")
    assert (e.status, e.code) == (404, "not_found")


def test_a_commit_never_runs_while_a_count_confirm_is_in_flight(run, monkeypatch):
    """WR-01: confirm serialises with every other write in the sandbox (the run lock FOR UPDATE). A decide that comes
    while a confirm has written the record but not committed waits for it, then sees the counted record: the new
    version cancelled the approval, so nothing commits on the record the count replaced."""
    case = story(run)
    cid = case["case_id"]
    src, v = source(run, cid), version(run, cid)
    plan = cases.parse_plan(active(run, cid)["result"]["recommended"])
    q = ask(run, cid)
    answer(run, q, counted(q, 0.0))
    r = from_inbox(run, q)
    owned_send(run, src, cid, v)
    if plan.dest is not None:
        call(run, workflow.decide, seat(plan.dest), cid, v, "accept", None, ack(run, cid), shown(run, cid))
    reached, go, out = threading.Event(), threading.Event(), {}
    real = facts.apply

    def paused(*args, **kwargs):  # the count is on the record (uncommitted); the re-evaluation has not started
        reached.set()
        assert go.wait(60)
        return real(*args, **kwargs)
    monkeypatch.setattr(facts, "apply", paused)

    def attempt(name, fn):
        try:
            out[name] = fn()
        except Exception as exc:  # recorded and asserted below
            out[name] = exc
    counting = threading.Thread(target=attempt, args=("confirm", lambda: confirm(
        run, seat(q["to_branch"]), r["answer_signal_id"], r["answer"]["revision"])))
    deciding = threading.Thread(target=attempt, args=("decide", lambda: call(
        run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid))))
    counting.start()
    assert reached.wait(60)
    deciding.start()
    deciding.join(timeout=3)
    waited = deciding.is_alive()
    go.set()
    counting.join(60)
    deciding.join(60)
    assert waited, "the commit ran while the count was in flight"
    assert not isinstance(out["confirm"], Exception), out["confirm"]
    assert isinstance(out["decide"], ApiError) and out["decide"].code in ("wrong_state", "stale_version")
    assert state(run, cid) == "OPEN" and version(run, cid) != v


# 09-10: human Send extends the same request path and receiving Tasks.
def test_draft_send_reaches_tasks_with_exact_message_context_and_db_clock(client):
    from datetime import timedelta
    from tests.agent.test_drafts import setup_draft, send
    from tests.api.conftest import as_seat

    session, run, case, _, saved = setup_draft(client)
    message = "  Please check 987654321 units?\nรายงานตามที่เห็น\r\nKeep these spaces.  "
    untouched = {name: table(run["run_id"], name) for name in ("decisions", "reservations")}
    with db.tx() as conn:
        before = realtime.db_now(conn)
    response = send(client, case, saved, message=message)
    assert response.status_code == 200, response.text
    q = response.json()
    stored = request_row(run, q)
    with db.tx() as conn:
        after = realtime.db_now(conn)
    assert stored["message"] == q["message"] == message
    assert stored["draft_id"] == q["draft_id"] == saved["draft_id"]
    assert q["original_text"] == saved["original_text"]
    assert stored["request_context"] == q["request_context"] == saved["request_context"]
    assert stored["sent_by"] == q["sent_by"] == session["account"]
    assert stored["sent_at"] == stored["asked_at"] == stored["created_at"]
    assert before <= stored["sent_at"] <= after
    assert stored["due_at"] - stored["sent_at"] == timedelta(minutes=60)
    assert q["sent_at"] == q["asked_at"] and q["plan_key"] == saved["plan_key"]
    assert message not in q["ask_text"] and all(lot in q["ask_text"] for lot in saved["lot_ids"])
    assert "Respond by " + realtime.label(stored["due_at"]) in q["ask_text"]
    as_seat(client, q["to_branch"])
    tasks = client.get("/api/tasks")
    assert tasks.status_code == 200, tasks.text
    received, = [r for r in tasks.json()["requests"] if r["request_id"] == q["request_id"]]
    for field in ("message", "request_context", "original_text", "draft_id", "sent_by", "sent_at", "due_at"):
        assert received[field] == q[field], field
    assert received["answer"] is None and received["confirmable"] is False
    assert table(run["run_id"], "reported_facts") == []
    assert {name: table(run["run_id"], name) for name in untouched} == untouched


def test_draft_send_idempotency_conflicts_without_rewriting_the_sent_message(client):
    from tests.agent.test_drafts import setup_draft, send
    from tests.api.conftest import key

    _, run, case, _, saved = setup_draft(client)
    headers, message = key(), " First wording.\nThe exact message. "
    first = send(client, case, saved, message=message, headers=headers)
    assert first.status_code == 200, first.text
    before = table(run["run_id"], "evidence_requests")
    assert send(client, case, saved, message=message, headers=headers).json() == first.json()
    conflict = send(client, case, saved, message="Changed wording", headers=headers)
    assert (conflict.status_code, conflict.json()["error"]) == (409, "idempotency_conflict")
    repeat = send(client, case, saved, message="Another tab's wording")
    assert repeat.status_code == 200 and repeat.json()["request_id"] == first.json()["request_id"]
    assert repeat.json()["message"] == message
    assert table(run["run_id"], "evidence_requests") == before
    assert len(asked_entries(run["run_id"])) == 1
