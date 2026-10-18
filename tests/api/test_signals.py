"""Confirmation by case state and the business date (07-CONTEXT D-09, D-17, D-21, D-22; 04-API-CONTRACT "Verify path"
7 and 15; acceptance checks 12 and 16).

A confirmed fact carries its check time, branch and source and a label with the real confirmation time; the business
day and the run version never move. Cases are found by tag and facts by kind, never by a key or a lot id from the
seed; the extra cases a test needs are its own (twins of the story lot). Nothing sleeps.
"""
import json
from datetime import timedelta

from api import cases, db, evaluate, inputs, lotholds, realtime, refdata, reports, reviewflags, scope, workflow
from tests.api import test_http
from tests.api.conftest import as_seat, fresh, key, seat, take_and_send, world_lot
from tests.api.test_evidence import (active, answer, ask, confirm, counted, from_inbox, full_loop, lot_row, refused,
                                     signal_row, source, story, table)
from tests.api.test_workflow import approve_transfer, call, insert_case, state, version
from tests.api.verify_helpers import ack


def test_business_date_unchanged(run):
    """Acceptance check 16 (D-21): confirming a count never moves the workspace's business day or its run version;
    the fact's adjustments carry counted_day = the business day, and its label shows the real confirmation time."""
    case = story(run)
    before = fresh(run["run_id"])
    q, out = full_loop(run, case["case_id"])
    after = fresh(run["run_id"])
    assert (after["day"], after["version"]) == (before["day"], before["version"])
    (fact,) = table(run["run_id"], "reported_facts")
    assert [a["counted_day"] for a in fact["adjustments"]] == [before["day"]] * len(q["lot_ids"])
    stamp = realtime.label(fact["confirmed_at"])
    assert stamp in fact["label"]
    assert out["labels"] == [f"Count confirmed by {q['to_branch']} manager · {stamp} · shelf 0 + backroom 0 = 0"] * len(
        q["lot_ids"])
    with db.tx() as conn:
        answered = db.one(conn, "SELECT answer_signal_id FROM evidence_requests WHERE run_id = :r AND request_id = :q",
                          r=run["run_id"], q=q["request_id"])["answer_signal_id"]
    s = signal_row(run, answered)
    assert (fact["observed_at"], fact["branch"], fact["source"], fact["signal_id"]) == (
        s["observed_at"], q["to_branch"], "request", answered)
    assert fact["observed_at"] <= fact["confirmed_at"] and fact["replaced_by"] is None


# --- one scope matcher (07-06 Task 2; research Pattern 3) ------------------------------------------------------------

CASE = {"case_id": "TEST-C", "sku": "TEST-SKU", "source_branch": "TEST-A", "lot_id": "TEST-L1"}
STORED = {"today": 5, "forecasts": [{"day": 5}, {"day": 8}], "lots": [{"lot_id": "TEST-L1"}, {"lot_id": "TEST-L2"}],
          "runs": [{"run_code": "TEST-R1", "dest": "TEST-B"}]}  # a stored input's keys the matcher reads (test values)


def fact(adjustments, sku=None, branch=None, zone=None, valid_from=None, valid_to=None) -> scope.FactScope:
    return scope.of_adjustments(adjustments, sku=sku, branch=branch, zone=zone, valid_from=valid_from,
                                valid_to=valid_to)


def hit(s, involved=("TEST-L1",), zone="chilled", stored=STORED) -> bool:
    return scope.matches(s, CASE, stored, involved, zone)


def counted_lot(lot_id) -> list[dict]:
    return [{"kind": "stock_count", "lot_id": lot_id, "units": 1.0, "counted_day": 5}]


def test_scope_matching():
    """Pattern 3 on the stored input only: a lot fact matches by the input's lots and the lots the case involves (moved
    from it, or the lot it was moved from); a branch fact by the source or an input destination, with SKU and zone; a
    run fact by the input's run codes; a validity range outside the decision day..last forecast day matches nothing."""
    assert hit(fact(counted_lot("TEST-L2"), sku="TEST-SKU", branch="TEST-B", valid_from=5, valid_to=5))
    assert not hit(fact(counted_lot("TEST-L9"), sku="TEST-SKU", branch="TEST-B"))  # a count is about its lots only
    assert hit(fact(counted_lot("TEST-L9"), sku="TEST-SKU"), involved=("TEST-L1", "TEST-L9"))  # moved from the lot
    assert hit(fact(counted_lot("TEST-L0"), sku="TEST-SKU"), involved=("TEST-L0", "TEST-L1"))  # the lot it came from
    assert not hit(fact(counted_lot("TEST-L2"), sku="TEST-OTHER"))
    assert hit(fact(counted_lot("TEST-L1"), sku="TEST-SKU"), stored=None)  # no version: the case's own lot
    capacity = [{"kind": "capacity", "branch": "TEST-B", "day_from": 5, "day_to": 6, "free_totes": 3}]
    assert hit(fact(capacity, zone="chilled", valid_from=5, valid_to=6))  # an input destination
    assert not hit(fact(capacity, zone="ambient", valid_from=5, valid_to=6))  # another zone
    assert not hit(fact(capacity, zone="chilled"), stored=None)  # no input: no destinations known
    demand = [{"kind": "demand_multiplier", "branch": "TEST-A", "day_from": 5, "day_to": 5, "value": 1.5}]
    assert hit(fact(demand, sku="TEST-SKU", valid_from=5, valid_to=5))  # the source branch
    assert hit(fact(demand, sku="TEST-SKU"), stored=None)
    assert not hit(fact(demand, sku="TEST-OTHER", valid_from=5, valid_to=5))
    assert not hit(fact([{**demand[0], "branch": "TEST-C"}], sku="TEST-SKU", valid_from=5, valid_to=5))
    quote = [{"kind": "transport_quote", "run_code": "TEST-R1", "fixed_cost": 40.0}]
    assert hit(fact(quote, valid_from=5, valid_to=5)) and not hit(fact([{**quote[0], "run_code": "TEST-R2"}]))
    assert not hit(fact(quote, valid_from=0, valid_to=4))  # ends before the decision day
    assert not hit(fact(quote, valid_from=9, valid_to=9))  # starts after the last forecast day
    assert hit(fact(quote, valid_from=8, valid_to=8)) and hit(fact(quote, valid_from=0, valid_to=5))  # the edges
    product = [{"kind": "markdown_response", "m20": 1.1, "m30": 1.2}]
    assert hit(fact(product, sku="TEST-SKU")) and not hit(fact(product, sku="TEST-OTHER"))
    s = fact(counted_lot("TEST-L2"), sku="TEST-SKU", branch="TEST-B", zone="chilled", valid_from=5, valid_to=5)
    assert (s.lot_ids, s.branches, s.run_codes, s.sku, s.zone, s.day_from, s.day_to) == (
        frozenset({"TEST-L2"}), frozenset(), frozenset(), "TEST-SKU", "chilled", 5, 5)


# --- confirmation by case state (07-06 Task 2; D-17, acceptance check 12) ---------------------------------------------

def twin(run, base: dict, n: int) -> str:
    """A twin of the story lot at its branch (the test's own lot: same SKU, record quantity and expiry) with an OPEN
    case: its stored input holds the destination's lots, the counted ones included."""
    lot = {**base, "lot_id": f"{base['lot_id']}-T{n}", "received_seq": base["received_seq"] + n}
    return insert_case(run, lot, f"TEST-T{n}")["case_id"]


def set_state(run, case_id, state_) -> None:
    """A committed state set directly (the test's own setup: no work order is needed for a flag)."""
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = :s WHERE run_id = :r AND case_id = :c", s=state_, r=run["run_id"],
               c=case_id)


def in_case_order(case_ids) -> list[str]:
    """The ids as the database orders case_id (its collation, not Python's)."""
    with db.tx() as conn:
        return [r["case_id"] for r in db.rows(conn, "SELECT DISTINCT case_id FROM cases WHERE case_id = ANY(:c) "
                                                    "ORDER BY case_id", c=list(case_ids))]


def case_detail(run, case_id) -> dict:
    with db.tx() as conn:
        return cases.detail(conn, fresh(run["run_id"]), case_id)


def bookings_of(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM reservations WHERE run_id = :r AND case_id = :c ORDER BY res_id",
                       r=run["run_id"], c=case_id)


def test_confirm_by_case_state(run, monkeypatch):
    """Acceptance check 12 (D-17): more than five live cases holding the counted lot each get a new version; every
    APPROVED, PREPARED or EXECUTED case in scope is flagged once with the fact id and keeps its version, state and
    bookings, with no engine run for it; a REJECTED case gets nothing. The card carries the review_flag badge while the
    flag is open; the detail lists the flags with the fact's label, and a PREPARED one adds "Review before dispatch"."""
    case = story(run)
    cid = case["case_id"]
    base = lot_row(run, case["lot_id"])
    q = ask(run, cid)
    answer(run, q, counted(q, 0.0))
    live = [twin(run, base, n) for n in range(1, 7)]
    approved, prepared, executed, rejected = (twin(run, base, n) for n in range(7, 11))
    for t in [*live, approved, prepared, executed, rejected]:  # every stored input holds the counted lots
        assert set(q["lot_ids"]) <= {l["lot_id"] for l in active(run, t)["engine_input"]["lots"]}, t
    approve_transfer(run, approved)
    set_state(run, prepared, "PREPARED")
    set_state(run, executed, "EXECUTED")
    call(run, workflow.reject, source(run, rejected), rejected, "store will run a promotion")
    committed = [approved, prepared, executed]
    before = {c: (version(run, c), state(run, c)) for c in [cid, *live, *committed, rejected]}
    held = bookings_of(run, approved)
    assert held  # the approval's bookings
    evaluated, computed = [], []
    real_case, real_compute = cases.evaluate_case, evaluate.compute

    def spy_case(conn, r, c, note=None):
        evaluated.append(c["case_id"])
        return real_case(conn, r, c, note=note)

    def spy_compute(inp):
        computed.append(inp.case_lot_id)
        return real_compute(inp)
    monkeypatch.setattr(cases, "evaluate_case", spy_case)
    monkeypatch.setattr(evaluate, "compute", spy_compute)
    r = from_inbox(run, q)
    out = confirm(run, seat(q["to_branch"]), r["answer_signal_id"], r["answer"]["revision"])
    monkeypatch.undo()
    # 07-12 (07-CONTEXT D-24): the rejected case stays history, but its lot, still at risk, is reassessed on the
    # confirmed fact in scope: exactly one successor opens and is evaluated (its own new case, after the live ones)
    with db.tx() as conn:
        succ = [c["case_id"] for c in db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r AND predecessor = :c",
                                               r=run["run_id"], c=rejected)]
    assert len(succ) == 1 and evaluated[-1:] == succ
    evaluated = evaluated[:-1]
    assert {cid, *live} <= set(evaluated) and len(evaluated) > 5 and evaluated == in_case_order(evaluated)
    assert not set(evaluated) & {*committed, rejected}  # committed and rejected cases never re-run
    assert not set(computed) & {f"{base['lot_id']}-T{n}" for n in range(7, 10)}  # no engine run for them
    for c in [cid, *live]:
        assert version(run, c) != before[c][0], c  # every live case in scope gets a new version
    assert {x["case_id"] for x in out["cases"]} >= {cid, *live}
    assert out["flagged"] == in_case_order(committed)
    flags = table(run["run_id"], "review_flags")
    assert sorted(f["case_id"] for f in flags) == sorted(committed)  # once each, the rejected case none
    assert {(f["fact_id"], f["reason"], f["cleared_at"]) for f in flags} == {(out["fact_id"], reviewflags.REASON, None)}
    for c in [*committed, rejected]:
        assert (version(run, c), state(run, c)) == before[c], c  # no committed plan is rewritten
    assert bookings_of(run, approved) == held
    (fact,) = table(run["run_id"], "reported_facts")
    flag_of = {f["case_id"]: f["flag_id"] for f in flags}
    for c in committed:
        seen = case_detail(run, c)
        assert {"kind": "review_flag", "text": reviewflags.REASON, "flag_ids": [flag_of[c]]} in seen["badges"]
        (listed,) = seen["review_flags"]
        assert (listed["flag_id"], listed["fact_id"], listed["reason"], listed["fact_label"], listed["note"]) == (
            flag_of[c], fact["fact_id"], reviewflags.REASON, fact["label"], None)
        assert listed["info"] == ("Review before dispatch" if c == prepared else None)
    with db.tx() as conn:
        card = next(x for x in cases.board(conn, fresh(run["run_id"])) if x["case_id"] == approved)
    assert "review_flag" in [b["kind"] for b in card["badges"]]
    assert case_detail(run, rejected)["review_flags"] == []
    assert "review_flag" not in [b["kind"] for b in case_detail(run, rejected)["badges"]]
    log = table(run["run_id"], "activity_log")
    (counted_at,) = [e["seq"] for e in log if e["type"] == "count_confirmed"]
    raised = [e for e in log if e["type"] == "review_flag_raised"]
    assert sorted(e["case_id"] for e in raised) == sorted(committed)
    assert counted_at < min(e["seq"] for e in raised)  # the count is logged before what it causes


# --- Reviewed · keep plan (07-09 Task 3; D-17, acceptance check 18, the keep half; 07-14 adds Mark done) ------------

def keep(run, actor, case_id, flag_ids, note):
    return call(run, reviewflags.keep, actor, case_id, flag_ids, note)


def flags_of(run, case_id) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM review_flags WHERE run_id = :r AND case_id = :c ORDER BY created_at, flag_id",
                       r=run["run_id"], c=case_id)


def test_review_keep_and_mark_done(run):
    """Acceptance check 18, keep half (D-17): on a flagged APPROVED case "Reviewed · keep plan" needs a note (400) and
    a seat that may approve the case (another branch 403); with both it clears exactly the named flag (cleared_by,
    cleared_at, note) and logs review_kept with the fact ids. A later confirmed fact raises a new flag. While a lot hold
    is on its lot, keep is 409 cannot_continue and the flag stays open. A successful keep never clears a hold or
    changes the version's checks."""
    case = story(run)
    cid = case["case_id"]
    base = lot_row(run, case["lot_id"])
    approved = twin(run, base, 1)
    q = ask(run, cid)
    answer(run, q, counted(q, 0.0))
    approve_transfer(run, approved)
    r = from_inbox(run, q)
    out = confirm(run, seat(q["to_branch"]), r["answer_signal_id"], r["answer"]["revision"])
    assert out["flagged"] == [approved]
    (flag,) = flags_of(run, approved)
    src = source(run, approved)
    dest = seat(cases.parse_plan(active(run, approved)["result"]["recommended"]).dest)
    other = next(b for b in refdata.get(run["dataset_version"]).branches if b not in (src["branch"], dest["branch"]))
    e = refused(run, reviewflags.keep, src, approved, [flag["flag_id"]], "  ")
    assert (e.status, e.code) == (400, "bad_request")
    e = refused(run, reviewflags.keep, seat(other), approved, [flag["flag_id"]], "Checked the plan")
    assert (e.status, e.code) == (403, "forbidden")
    e = refused(run, reviewflags.keep, src, approved, ["FLAG-none"], "Checked the plan")
    assert (e.status, e.code) == (400, "bad_request")
    checks_before = active(run, approved)["fact_checks"]
    assert keep(run, dest, approved, [flag["flag_id"]], "Destination still short; keep the plan") == {
        "cleared": [flag["flag_id"]]}  # the transfer's destination manager may approve it too
    (row,) = flags_of(run, approved)
    assert (row["cleared_by"], row["note"]) == (dest["account"], "Destination still short; keep the plan")
    assert row["cleared_at"] is not None
    (kept,) = [e for e in table(run["run_id"], "activity_log") if e["type"] == "review_kept"]
    assert (kept["case_id"], kept["payload"]["flag_ids"], kept["payload"]["fact_ids"], kept["payload"]["note"]) == (
        approved, [flag["flag_id"]], [flag["fact_id"]], "Destination still short; keep the plan")
    e = refused(run, reviewflags.keep, src, approved, [flag["flag_id"]], "again")  # already cleared
    assert (e.status, e.code) == (400, "bad_request")
    with db.tx() as conn:  # a later confirmed fact in scope raises a new flag; the cleared one stays cleared
        c = cases.case_row(conn, run["run_id"], approved)
        assert reviewflags.raise_for(conn, fresh(run["run_id"]), [c], flag["fact_id"]) == []
        assert reviewflags.raise_for(conn, fresh(run["run_id"]), [c], "TEST-LATER:fact") == [approved]
    (new,) = [f for f in flags_of(run, approved) if f["cleared_at"] is None]
    hold = call(run, lotholds.place, src, base["lot_id"] + "-T1", "CONDITION", "Crushed crates in the chiller")
    e = refused(run, reviewflags.keep, src, approved, [new["flag_id"]], "Keep it")
    assert (e.status, e.code, e.message) == (409, "cannot_continue",
                                             "This plan can't continue; cancel the approved plan instead")
    assert [b["kind"] for b in e.extra["blocks"]] == ["lot_hold"]
    assert [f["flag_id"] for f in flags_of(run, approved) if f["cleared_at"] is None] == [new["flag_id"]]
    set_state(run, approved, "PREPARED")  # on a PREPARED case the hold is informational: keep goes through
    assert keep(run, src, approved, [new["flag_id"]], "Reviewed before dispatch")["cleared"] == [new["flag_id"]]
    (still,) = [h for h in table(run["run_id"], "lot_holds") if h["hold_id"] == hold["hold_id"]]
    assert still["state"] == "ACTIVE"  # keep never clears a hold
    assert active(run, approved)["fact_checks"] == checks_before  # nor settles a check


# --- field reports filed by hand (07-13; D-14, D-15, D-17, D-20; SIG-03, FLOW-11) -------------------------------------

LABEL_SOURCE = "field report"  # the label's source for a report a person filed (D-15)


def report_parts(kind: str, fields: dict, text: str = "", **extra) -> dict:
    """A report's form fields as multipart parts (no filename, so none is a file)."""
    parts = {"kind": (None, kind), "fields": (None, json.dumps(fields)), "text": (None, text)}
    return {**parts, **{k: (None, v) for k, v in extra.items()}}


def file_report(client, kind: str, fields: dict, headers: dict | None = None, **extra):
    return client.post("/api/signals", files=report_parts(kind, fields, **extra),
                       headers=key() if headers is None else headers)


def reports_page(client) -> dict:
    r = client.get("/api/signals")
    assert r.status_code == 200, r.json()
    return r.json()


def session_run(client) -> dict:
    return fresh(client.get("/api/session").json()["run_id"])


def today_iso(client) -> str:
    run = session_run(client)
    return cases.iso_of(run, run["day"])


def tagged_card(client, tag: str) -> dict:
    return next(c for c in client.get("/api/cases").json()["cases"] if c["tag"] == tag)


def fact_row(run_id: str, fact_id: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM reported_facts WHERE run_id = :r AND fact_id = :f", r=run_id, f=fact_id)


def input_of(run_id: str, case_id: str) -> dict:
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run_id, case_id))["engine_input"]


def test_report_confirm_applies(client):
    """The tracer (D-14, D-15, D-17, D-20): the story case's destination manager files CAPACITY for their own store's
    chilled zone today; it is READ, owned by that store's manager, spends one of the workspace's reports and changes
    no case. The same seat confirms it (self-confirmation): a reported fact with its zone and the label "Reported by
    {store} manager · {t} · field report"; the story case gets a new version whose input carries the capacity
    adjustment; the ambient case sending to the same store gets nothing (zone)."""
    card = test_http.story(client)
    bread = tagged_card(client, "bread-keep")
    dest, zone, run_id = card["dest"], card["zone"], session_run(client)["run_id"]
    assert zone != bread["zone"]
    with db.tx() as conn:
        bread_lot = cases.case_row(conn, run_id, bread["case_id"])["lot_id"]
    assert dest in {r["dest"] for r in input_of(run_id, bread["case_id"])["runs"]}  # the same store, another zone
    day = today_iso(client)
    as_seat(client, dest)
    left = reports_page(client)["allowance"]["reports_left"]
    fields = {"zone": zone, "free_totes": 5, "valid_from": day, "valid_to": day}
    r = file_report(client, "CAPACITY", fields, text="One chiller is down today")
    assert r.status_code == 200, r.json()
    rep = r.json()
    assert (rep["kind"], rep["state"], rep["revision"], rep["branch"], rep["reporter_branch"]) == (
        "CAPACITY", "READ", 1, dest, dest)
    assert rep["owner"] == {"role": "manager", "branch": dest} and rep["needs_input"] == []
    assert rep["report_id"] == rep["signal_id"] and rep["fields"] == fields and rep["fact_id"] is None
    page = reports_page(client)
    assert page["allowance"]["reports_left"] == left - 1
    assert rep["signal_id"] in [x["signal_id"] for x in page["signals"]]
    assert test_http.story(client)["rec_version"] == card["rec_version"]  # nothing applies before confirmation
    out = client.post(f"/api/signals/{rep['signal_id']}/confirm", json={"revision": 1}, headers=key())
    assert out.status_code == 200, out.json()
    done = out.json()
    fact = fact_row(run_id, done["fact_id"])
    stamp = realtime.label(fact["confirmed_at"])
    assert done["labels"] == [f"Reported by {dest} manager · {stamp} · {LABEL_SOURCE}"] == [fact["label"]]
    d = session_run(client)["day"]
    assert fact["adjustments"] == [{"kind": "capacity", "branch": dest, "day_from": d, "day_to": d, "free_totes": 5}]
    assert (fact["kind"], fact["zone"], fact["branch"], fact["source"], fact["signal_id"], fact["replaced_by"]) == (
        "CAPACITY", zone, dest, LABEL_SOURCE, rep["signal_id"], None)
    after = test_http.story(client)
    assert after["rec_version"] != card["rec_version"]
    assert after["case_id"] in [c["case_id"] for c in done["cases"]]
    assert fact["adjustments"][0] in input_of(run_id, card["case_id"])["reported_facts"]
    assert tagged_card(client, "bread-keep")["rec_version"] == bread["rec_version"]  # another zone: untouched
    with db.tx() as conn:  # and its input never reads the chilled fact, wherever it is built from
        assert inputs.build_input(conn, fresh(run_id), bread_lot).reported_facts == ()
    seen = client.get(f"/api/signals/{rep['signal_id']}").json()
    assert (seen["state"], seen["fact_id"], seen["fact_label"], seen["confirmed_by"]) == (
        "CONFIRMED", done["fact_id"], fact["label"], dest)
    assert seen["confirmed_at"] is not None
    logged = [e["type"] for e in table(run_id, "activity_log")]
    assert "report_filed" in logged and "report_confirmed" in logged


def test_report_seats(client):
    """D-15: a manager reports for their own store only (the store comes from the seat; a body naming one is 400),
    HQ files TRANSPORT only (CAPACITY is 403), and only the owner confirms (another store's manager is 403). A
    refused report stores nothing and spends nothing."""
    card = test_http.story(client)
    dest, day = card["dest"], today_iso(client)
    run_id = session_run(client)["run_id"]
    fields = {"zone": card["zone"], "free_totes": 5, "valid_from": day, "valid_to": day}
    as_seat(client, dest)
    left = reports_page(client)["allowance"]["reports_left"]
    other = next(b for b in refdata.get(session_run(client)["dataset_version"]).branches if b != dest)
    r = file_report(client, "CAPACITY", {**fields, "branch": other})
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.json()
    as_seat(client, None)
    r = file_report(client, "CAPACITY", fields)
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    assert reports_page(client)["allowance"]["reports_left"] == left  # refusals spend nothing
    assert [s for s in table(run_id, "signals") if s["report_id"] and s["provenance"] != "simulated"] == []
    as_seat(client, dest)
    rep = file_report(client, "CAPACITY", fields).json()
    as_seat(client, other)
    r = client.post(f"/api/signals/{rep['signal_id']}/confirm", json={"revision": 1}, headers=key())
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(client, None)
    r = client.post(f"/api/signals/{rep['signal_id']}/confirm", json={"revision": 1}, headers=key())
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")  # HQ owns transport only


def confirm_report(client, signal_id: str, revision: int = 1):
    return client.post(f"/api/signals/{signal_id}/confirm", json={"revision": revision}, headers=key())


def report_action(client, signal_id: str, action: str, body: dict | None = None):
    return client.post(f"/api/signals/{signal_id}/{action}", json=body or {}, headers=key())


def filed(client, kind: str, fields: dict, **extra) -> dict:
    r = file_report(client, kind, fields, **extra)
    assert r.status_code == 200, r.json()
    return r.json()


def confirmed(client, signal_id: str) -> dict:
    r = confirm_report(client, signal_id)
    assert r.status_code == 200, r.json()
    return r.json()


def adjustments_in(run_id: str, case_id: str, kind: str) -> list[dict]:
    return [a for a in input_of(run_id, case_id)["reported_facts"] if a["kind"] == kind]


def test_report_kinds(client):
    """Spec §7 mapping (research F-9; D-14, D-15): a DEMAND_EVENT confirmed by its store's manager is a demand
    multiplier at that store; a TRANSPORT quote filed and confirmed by HQ maps PER_TRIP to the run's transport quote,
    with "includes handling: yes" a zero handling cost; handling left UNKNOWN is 409 needs_input naming it; an expired
    quote is 409 quote_expired; a store's manager can't confirm transport (403) nor report a run leaving another
    store; RUN_NOT_AVAILABLE and DELAY map to run availability and a departure delay; an amount above the Policy's
    adjustment_cost_max is 400 out_of_bounds naming the field."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, d, cid = run["run_id"], run["day"], card["case_id"]
    today, yesterday = cases.iso_of(run, d), cases.iso_of(run, d - 1)
    runs = input_of(run_id, cid)["runs"]
    first, second = runs[0]["run_code"], runs[1]["run_code"]
    with db.tx() as conn:
        cost_max = inputs.sandbox_policy(conn, run).adjustment_cost_max
    as_seat(client, card["dest"])
    demand = filed(client, "DEMAND_EVENT", {"multiplier": 1.5, "valid_from": today, "valid_to": today})
    assert demand["owner"] == {"role": "manager", "branch": card["dest"]} and demand["needs_input"] == []
    confirmed(client, demand["signal_id"])
    assert adjustments_in(run_id, cid, "demand_multiplier") == [
        {"kind": "demand_multiplier", "branch": card["dest"], "day_from": d, "day_to": d, "value": 1.5}]
    as_seat(client, None)
    quote = {"run_code": first, "what": "QUOTE", "amount": 25, "pricing_basis": "PER_TRIP",
             "includes_handling": "YES", "applies_on": today, "valid_until": today}
    q = filed(client, "TRANSPORT", quote, text="Carrier quote for the morning run")
    assert (q["owner"], q["branch"], q["reporter_role"]) == ({"role": "hq", "branch": None}, card["source"], "hq")
    as_seat(client, card["source"])
    r = confirm_report(client, q["signal_id"])
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")  # transport is HQ's to confirm
    other = next(b for b in refdata.get(run["dataset_version"]).branches if b not in (card["source"], card["dest"]))
    as_seat(client, other)
    r = file_report(client, "TRANSPORT", quote)  # a run leaving another store
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(client, None)
    out = confirmed(client, q["signal_id"])
    fact = fact_row(run_id, out["fact_id"])
    assert out["labels"] == [f"Reported by HQ · {realtime.label(fact['confirmed_at'])} · {LABEL_SOURCE}"]
    assert adjustments_in(run_id, cid, "transport_quote") == [
        {"kind": "transport_quote", "run_code": first, "fixed_cost": 25.0}]
    assert adjustments_in(run_id, cid, "handling_cost") == [
        {"kind": "handling_cost", "run_code": first, "per_unit_cost": 0.0}]
    unknown = filed(client, "TRANSPORT", {**quote, "includes_handling": "UNKNOWN"})
    assert unknown["needs_input"] == [{"field": "includes_handling"}]
    r = confirm_report(client, unknown["signal_id"])
    assert (r.status_code, r.json()["error"], r.json()["fields"]) == (409, "needs_input",
                                                                      [{"field": "includes_handling"}])
    assert r.json()["message"] == "Choose the pricing basis and whether handling is included"
    r = file_report(client, "TRANSPORT", {**quote, "valid_until": yesterday})  # before its applies_on (07-13 review T2)
    assert (r.status_code, r.json()["error"], r.json()["field"]) == (400, "bad_request", "valid_until")
    # 409 quote_expired once the quote's day has passed: tests/api/test_review_fixes_2.py
    gone = filed(client, "TRANSPORT", {"run_code": second, "what": "RUN_NOT_AVAILABLE", "applies_on": today})
    confirmed(client, gone["signal_id"])
    late = filed(client, "TRANSPORT", {"run_code": second, "what": "DELAY", "delay_hours": 2, "applies_on": today})
    confirmed(client, late["signal_id"])
    assert adjustments_in(run_id, cid, "run_available") == [{"kind": "run_available", "run_code": second,
                                                             "value": False}]
    assert adjustments_in(run_id, cid, "departure_delay_h") == [{"kind": "departure_delay_h", "run_code": second,
                                                                 "hours": 2.0}]
    r = file_report(client, "TRANSPORT", {**quote, "amount": cost_max + 1})
    assert (r.status_code, r.json()["error"], r.json()["field"]) == (400, "out_of_bounds", "amount")
    assert r.json()["bound"] == {"min": 0.0, "max": cost_max}


def test_withdraw_and_correct(client):
    """Acceptance check 13 (D-15): the reporter withdraws before confirmation (WITHDRAWN, no fact, no new version;
    another seat 403; again 409); a confirmed report can't be withdrawn (409); a correction (`corrects`) confirmed by
    the owner replaces the earlier fact from then on (replaced_by), both reports and facts kept, the multipliers never
    stacking; a correction must name a confirmed report not yet corrected; the owner dismisses with a reason
    (DISMISSED, dismissed_by and dismissed_at, nothing applied; another seat 403)."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, d, cid, dest = run["run_id"], run["day"], card["case_id"], card["dest"]
    today = cases.iso_of(run, d)
    event = {"multiplier": 1.5, "valid_from": today, "valid_to": today}
    as_seat(client, dest)
    first = filed(client, "DEMAND_EVENT", event)
    as_seat(client, card["source"])
    r = report_action(client, first["signal_id"], "withdraw")
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(client, dest)
    r = report_action(client, first["signal_id"], "withdraw")
    assert r.status_code == 200, r.json()
    assert (r.json()["state"], r.json()["fact_id"]) == ("WITHDRAWN", None) and r.json()["withdrawn_at"] is not None
    r = report_action(client, first["signal_id"], "withdraw")
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")
    assert test_http.story(client)["rec_version"] == card["rec_version"]  # no operational effect
    assert table(run_id, "reported_facts") == []
    r = file_report(client, "DEMAND_EVENT", {**event, "multiplier": 2.0}, corrects=first["signal_id"])
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")  # only a confirmed report is corrected
    second = filed(client, "DEMAND_EVENT", event)
    old = confirmed(client, second["signal_id"])
    r = report_action(client, second["signal_id"], "withdraw")
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")
    fix = filed(client, "DEMAND_EVENT", {**event, "multiplier": 2.0}, corrects=second["signal_id"])
    assert fix["corrects"] == second["signal_id"]
    new = confirmed(client, fix["signal_id"])
    assert fact_row(run_id, old["fact_id"])["replaced_by"] == new["fact_id"]
    assert fact_row(run_id, new["fact_id"])["replaced_by"] is None
    assert adjustments_in(run_id, cid, "demand_multiplier") == [
        {"kind": "demand_multiplier", "branch": dest, "day_from": d, "day_to": d, "value": 2.0}]  # never stacked
    kept = client.get(f"/api/signals/{second['signal_id']}").json()
    assert (kept["state"], kept["fact_id"], kept["replaced_by"]) == ("CONFIRMED", old["fact_id"], fix["report_id"])
    r = file_report(client, "DEMAND_EVENT", {**event, "multiplier": 2.5}, corrects=second["signal_id"])
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")  # correct the latest report instead
    third = filed(client, "DEMAND_EVENT", event)
    as_seat(client, card["source"])
    r = report_action(client, third["signal_id"], "dismiss", {"reason": "not ours"})
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(client, dest)
    before = test_http.story(client)["rec_version"]
    r = report_action(client, third["signal_id"], "dismiss", {"reason": "The fair was cancelled"})
    assert r.status_code == 200, r.json()
    gone = r.json()
    assert (gone["state"], gone["dismissed_by"], gone["dismiss_reason"], gone["fact_id"]) == (
        "DISMISSED", dest, "The fair was cancelled", None)
    assert gone["dismissed_at"] is not None
    assert test_http.story(client)["rec_version"] == before  # nothing applied
    assert len(table(run_id, "reported_facts")) == 2  # both confirmed facts kept
    logged = [e["type"] for e in table(run_id, "activity_log")]
    assert "report_withdrawn" in logged and "report_dismissed" in logged


def test_blank_required_fields(client):
    """Edge SIG-03 empty (D-14): a report with a blank required field is filed and lists it in needs_input; its
    confirmation is 409 needs_input naming it, and nothing is applied."""
    card = test_http.story(client)
    today = today_iso(client)
    as_seat(client, card["dest"])
    rep = filed(client, "DEMAND_EVENT", {"valid_from": today, "valid_to": today})
    assert rep["needs_input"] == [{"field": "multiplier"}] and rep["state"] == "READ"
    r = confirm_report(client, rep["signal_id"])
    assert (r.status_code, r.json()["error"], r.json()["fields"]) == (409, "needs_input", [{"field": "multiplier"}])
    assert r.json()["message"] == "Complete the report before confirming"
    assert test_http.story(client)["rec_version"] == card["rec_version"]


def badge(card: dict, kind: str) -> dict | None:
    return next((b for b in card["badges"] if b["kind"] == kind), None)


def test_new_report_badge(client):
    """D-16, 04-API-CONTRACT Badges: an unconfirmed report whose scope holds the story case gives its card the New
    report badge (with the report's id) and lists the report on the case detail under `reports`; a case outside its
    scope gets neither; the owner's list counts it in needs_my_confirmation. After confirmation, or withdrawal, the
    badge and the listing are gone."""
    card = test_http.story(client)
    day = today_iso(client)
    as_seat(client, None)
    hq = reports_page(client)["needs_my_confirmation"]  # the workspace's preloaded transport quote (07-16, D-18)
    as_seat(client, card["dest"])
    fields = {"zone": card["zone"], "free_totes": 5, "valid_from": day, "valid_to": day}
    rep = filed(client, "CAPACITY", fields)
    seen = test_http.story(client)
    assert badge(seen, "new_report") == {"kind": "new_report", "text": "New report", "signal_ids": [rep["signal_id"]]}
    detail = client.get(f"/api/cases/{card['case_id']}").json()
    assert [r["signal_id"] for r in detail["reports"]] == [rep["signal_id"]]
    assert detail["reports"][0]["state"] == "READ"
    assert badge(tagged_card(client, "bread-keep"), "new_report") is None  # another zone: not in scope
    assert reports_page(client)["needs_my_confirmation"] == 1
    as_seat(client, None)
    assert reports_page(client)["needs_my_confirmation"] == hq  # HQ owns transport only: unchanged
    as_seat(client, card["dest"])
    confirmed(client, rep["signal_id"])
    assert badge(test_http.story(client), "new_report") is None
    assert client.get(f"/api/cases/{card['case_id']}").json()["reports"] == []
    assert reports_page(client)["needs_my_confirmation"] == 0
    second = filed(client, "CAPACITY", {**fields, "free_totes": 4})
    assert badge(test_http.story(client), "new_report")["signal_ids"] == [second["signal_id"]]
    assert report_action(client, second["signal_id"], "withdraw").status_code == 200
    assert badge(test_http.story(client), "new_report") is None


# --- count and printed-date reports (07-14; D-11, D-14, D-22; the request answers' rules) -----------------------------

def phantom_lot(client, card: dict) -> tuple[str, str]:
    """(lot_id, branch): the first lot of the story case's plan-changing phantom fact, at the branch holding it."""
    fact = test_http.phantom(client.get(f"/api/cases/{card['case_id']}").json())
    return fact["lot_ids"][0], fact["branch"]


def confirm_count_report(client, signal_id: str, revision: int = 1, checked_both=True):
    return client.post(f"/api/signals/{signal_id}/confirm", json={"revision": revision, "checked_both": checked_both},
                       headers=key())


def test_count_report_confirms_like_an_answer(client):
    """D-14, D-22 (acceptance check 17): the lot's manager files STOCK_COUNT {lot, shelf, backroom}; without "When did
    you check?" it is 400 check_time; with "now" it is READ. Confirm without the tick is 409 scope_tick; with it, a
    stock_count fact (source "field report", the report's check time, the business day), the record corrected and
    the cases holding the lot re-planned. A second report checked earlier than that fact is 409 older_evidence at
    confirmation and is kept, unconfirmed."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, d = run["run_id"], run["day"]
    lot_id, branch = phantom_lot(client, card)
    as_seat(client, branch)
    fields = {"lot_id": lot_id, "shelf_units": 0, "backroom_units": 0}
    r = file_report(client, "STOCK_COUNT", fields)
    assert (r.status_code, r.json()["error"]) == (400, "check_time"), r.json()
    rep = filed(client, "STOCK_COUNT", fields, observed="now")
    assert (rep["state"], rep["kind"], rep["branch"], rep["needs_input"]) == ("READ", "STOCK_COUNT", branch, [])
    assert rep["owner"] == {"role": "manager", "branch": branch} and rep["observed_at"] is not None
    assert test_http.story(client)["rec_version"] == card["rec_version"]  # nothing applies before confirmation
    r = confirm_report(client, rep["signal_id"])
    assert (r.status_code, r.json()["error"]) == (409, "scope_tick"), r.json()
    r = confirm_count_report(client, rep["signal_id"])
    assert r.status_code == 200, r.json()
    done = r.json()
    fact = fact_row(run_id, done["fact_id"])
    assert (fact["kind"], fact["source"], fact["branch"], fact["signal_id"]) == (
        "STOCK_COUNT", LABEL_SOURCE, branch, rep["signal_id"])
    assert fact["adjustments"] == [{"kind": "stock_count", "lot_id": lot_id, "units": 0.0, "counted_day": d}]
    assert realtime.iso(fact["observed_at"]) == rep["observed_at"]
    assert done["labels"] == [f"Reported by {branch} manager · {realtime.label(fact['confirmed_at'])} · "
                              f"{LABEL_SOURCE}"] == [fact["label"]]
    row = world_lot(run_id, lot_id)
    assert (row["qty"], row["last_verified_day"], row["verified_by"]) == (0.0, d, "COUNT")  # the record corrected
    after = test_http.story(client)
    assert after["rec_version"] != card["rec_version"] and card["case_id"] in [c["case_id"] for c in done["cases"]]
    earlier = (fact["observed_at"] - timedelta(minutes=5)).isoformat()
    second = filed(client, "STOCK_COUNT", {**fields, "shelf_units": 4}, observed=earlier)
    r = confirm_count_report(client, second["signal_id"])
    assert (r.status_code, r.json()["error"]) == (409, "older_evidence"), r.json()
    kept = client.get(f"/api/signals/{second['signal_id']}").json()
    assert (kept["state"], kept["fact_id"]) == ("READ", None)  # kept, never applied
    assert len(table(run_id, "reported_facts")) == 1 and world_lot(run_id, lot_id)["qty"] == 0.0


def test_expiry_report(client):
    """D-11, D-14: an EXPIRY report with a printed date, confirmed by the lot's manager, writes an expiry fact and never
    rewrites lots.expiry_day. An EXPIRY report with "MIXED" places an ACTIVE MIXED_DATES hold at submission, placed by
    the reporter (the lot's manager), and has nothing to confirm: 409 needs_input naming printed_date."""
    card = test_http.story(client)
    run = session_run(client)
    run_id = run["run_id"]
    lot_id, branch = phantom_lot(client, card)
    lot = world_lot(run_id, lot_id)
    printed_day = max(run["day"], lot["expiry_day"] - 1)
    fields = {"lot_id": lot_id, "printed_date": cases.iso_of(run, printed_day)}
    as_seat(client, branch)
    r = file_report(client, "EXPIRY", fields)
    assert (r.status_code, r.json()["error"]) == (400, "check_time"), r.json()
    rep = filed(client, "EXPIRY", fields, observed="now")
    assert (rep["state"], rep["needs_input"], rep["hold_id"]) == ("READ", [], None)
    done = confirmed(client, rep["signal_id"])
    fact = fact_row(run_id, done["fact_id"])
    assert (fact["kind"], fact["source"], fact["branch"]) == ("EXPIRY", LABEL_SOURCE, branch)
    assert fact["adjustments"] == [{"kind": "expiry", "lot_id": lot_id, "expiry_day": printed_day}]
    after = world_lot(run_id, lot_id)
    assert (after["expiry_day"], after["expiry_source"]) == (lot["expiry_day"], lot["expiry_source"])  # never rewritten
    mixed = filed(client, "EXPIRY", {"lot_id": lot_id, "printed_date": "MIXED"}, observed="now")
    assert mixed["needs_input"] == [{"field": "printed_date"}]
    [hold] = [h for h in table(run_id, "lot_holds") if h["signal_id"] == mixed["signal_id"]]
    assert (hold["kind"], hold["state"], hold["reason"], hold["lot_id"], hold["branch"]) == (
        "MIXED_DATES", "ACTIVE", lotholds.MIXED_TEXT, lot_id, branch)
    assert hold["placed_by"] == branch and mixed["hold_id"] == hold["hold_id"]
    r = confirm_report(client, mixed["signal_id"])
    assert (r.status_code, r.json()["error"], r.json()["fields"]) == (409, "needs_input", [{"field": "printed_date"}])
    assert len(table(run_id, "reported_facts")) == 1


# --- condition observations: human review only, explicit holds (07-14; D-13) ----------------------------------------

REVIEW_ONLY = "Requires human review · engine impact not calculated"


def test_condition_is_human_review_only(client):
    """D-13 (T-07-52): a CONDITION DAMAGE report about the reporter's own lot with hold true places an ACTIVE
    CONDITION hold before any confirmation, logged on its own (hold_placed); the report reads "Requires human review ·
    engine impact not calculated" and shows no preview. Its confirmation writes no reported fact (row count) and
    re-plans nothing. A display issue can't hold the lot (400). A withdrawn condition report leaves its hold ACTIVE
    until the lot's manager releases it."""
    card = test_http.story(client)
    run_id = session_run(client)["run_id"]
    lot_id, branch = phantom_lot(client, card)
    as_seat(client, branch)
    r = file_report(client, "CONDITION", {"lot_id": lot_id, "condition": "DISPLAY", "hold": True})
    assert (r.status_code, r.json()["error"], r.json().get("field")) == (400, "bad_request", "hold"), r.json()
    rep = filed(client, "CONDITION", {"lot_id": lot_id, "condition": "DAMAGE", "hold": True},
                text="Crushed crates in the chiller")
    assert (rep["state"], rep["needs_input"], rep["impact_label"], rep["owner"]) == (
        "READ", [], REVIEW_ONLY, {"role": "manager", "branch": branch})
    [hold] = [h for h in table(run_id, "lot_holds") if h["signal_id"] == rep["signal_id"]]
    assert (hold["kind"], hold["state"], hold["lot_id"], hold["placed_by"], hold["reason"]) == (
        "CONDITION", "ACTIVE", lot_id, branch, "Held for review: Damage")
    assert rep["hold_id"] == hold["hold_id"]
    placed = [e for e in table(run_id, "activity_log") if e["type"] == "hold_placed"]
    assert [e["payload"]["signal_id"] for e in placed] == [rep["signal_id"]]  # its own, logged entry
    seen = client.get(f"/api/signals/{rep['signal_id']}").json()
    assert seen["impact_label"] == REVIEW_ONLY and seen.get("preview") is None
    facts_before = len(table(run_id, "reported_facts"))
    out = confirmed(client, rep["signal_id"])
    assert (out["fact_id"], out["labels"], out["cases"], out["flagged"]) == (None, [], [], [])
    assert len(table(run_id, "reported_facts")) == facts_before  # no engine input from a condition (T-07-52)
    assert test_http.story(client)["rec_version"] == card["rec_version"]  # nothing re-planned
    done = client.get(f"/api/signals/{rep['signal_id']}").json()
    assert (done["state"], done["fact_id"], done["impact_label"], done["task"]) == ("CONFIRMED", None, REVIEW_ONLY,
                                                                                   None)
    r = client.post(f"/api/holds/{hold['hold_id']}/release", json={"note": "Crates replaced"}, headers=key())
    assert r.status_code == 200, r.json()
    safety = filed(client, "CONDITION", {"lot_id": lot_id, "condition": "SAFETY", "hold": True})
    [held] = [h for h in table(run_id, "lot_holds") if h["signal_id"] == safety["signal_id"]]
    assert (held["state"], held["reason"]) == ("ACTIVE", "Held for review: Possible safety issue")
    assert report_action(client, safety["signal_id"], "withdraw").status_code == 200
    [still] = [h for h in table(run_id, "lot_holds") if h["hold_id"] == held["hold_id"]]
    assert still["state"] == "ACTIVE"  # withdrawal has no operational effect; the lot's manager releases it


def test_mark_done(client):
    """Acceptance check 18, the Mark done half (D-13, T-07-53): a confirmed CONDITION DISPLAY report is an advisory
    task in its branch's Tasks (counts.advisory 1), with no hold. Mark done by another branch is 403; by the branch's
    manager it records who and when (task_done_at, task_done_by), leaves the open count (counts.advisory 0), keeps the
    task listed as done with "Done by {store} manager · {t}", logs task_done, and leaves a hold on the same lot
    ACTIVE. Done again is 409 wrong_state."""
    card = test_http.story(client)
    run_id = session_run(client)["run_id"]
    lot_id, branch = phantom_lot(client, card)
    as_seat(client, branch)
    r = client.post(f"/api/lots/{lot_id}/holds", json={"reason": "Leaking crate"}, headers=key())
    assert r.status_code == 200, r.json()
    hold_id = r.json()["hold_id"]
    rep = filed(client, "CONDITION", {"lot_id": lot_id, "condition": "DISPLAY"}, text="Shelf tag missing")
    assert rep["hold_id"] is None
    before = client.get("/api/tasks").json()
    assert before["counts"]["advisory"] == 0 and before["advisory"] == []
    confirmed(client, rep["signal_id"])
    tasks = client.get("/api/tasks").json()
    [task] = tasks["advisory"]
    assert (task["signal_id"], task["task"]["done_at"], task["task"]["done_by"]) == (rep["signal_id"], None, None)
    assert tasks["counts"]["advisory"] == 1
    other = next(b for b in refdata.get(session_run(client)["dataset_version"]).branches if b != branch)
    as_seat(client, other)
    r = report_action(client, rep["signal_id"], "done")
    assert (r.status_code, r.json()["error"]) == (403, "forbidden"), r.json()
    as_seat(client, branch)
    r = report_action(client, rep["signal_id"], "done")
    assert r.status_code == 200, r.json()
    with db.tx() as conn:
        row = db.one(conn, "SELECT task_done_at, task_done_by FROM signals WHERE run_id = :r AND signal_id = :s",
                     r=run_id, s=rep["signal_id"])
    assert row["task_done_at"] is not None and row["task_done_by"] == branch
    assert r.json()["task"]["done_at"] == realtime.iso(row["task_done_at"])
    after = client.get("/api/tasks").json()
    assert after["counts"]["advisory"] == 0
    [closed] = after["advisory"]
    assert closed["task"]["done_label"] == f"Done by {branch} manager · {realtime.label(row['task_done_at'])}"
    [h] = [x for x in table(run_id, "lot_holds") if x["hold_id"] == hold_id]
    assert h["state"] == "ACTIVE"  # Mark done never clears or releases a hold
    [entry] = [e for e in table(run_id, "activity_log") if e["type"] == "task_done"]
    assert entry["payload"]["signal_id"] == rep["signal_id"]
    r = report_action(client, rep["signal_id"], "done")
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")


# --- report edits as revisions; the list's pages (07-14; D-15, research F-26) -----------------------------------------

def patch_report(client, signal_id: str, body: dict, headers: dict | None = None):
    return client.patch(f"/api/signals/{signal_id}", json=body, headers=key() if headers is None else headers)


def test_report_revisions(client):
    """D-15, F-26 (T-07-54): before confirmation the reporter edits a READ report: revision 2 of the same report, the
    earlier REPLACED and kept; the owner (HQ for transport) corrects a field: revision 3; another store's manager is
    403; the fields are checked again (400 out_of_bounds); a stale revision is 409 stale_version. Confirm naming
    revision 2 is 409 stale_version; naming 3 applies revision 3's fields. A confirmed report can't be edited (409
    wrong_state). The report keeps every revision, oldest first."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, today = run["run_id"], cases.iso_of(run, run["day"])
    first = input_of(run_id, card["case_id"])["runs"][0]["run_code"]
    as_seat(client, card["source"])
    rep = filed(client, "TRANSPORT", {"run_code": first, "what": "DELAY", "delay_hours": 2, "applies_on": today},
                text="The driver called in late")
    r = patch_report(client, rep["signal_id"], {"revision": 1, "fields": {"delay_hours": 3}})
    assert r.status_code == 200, r.json()
    two = r.json()
    assert (two["report_id"], two["revision"], two["state"], two["fields"]["delay_hours"], two["text"]) == (
        rep["report_id"], 2, "READ", 3.0, "The driver called in late")
    assert two["signal_id"] != rep["signal_id"] and two["reporter_branch"] == card["source"]
    assert client.get(f"/api/signals/{rep['signal_id']}").json()["state"] == "REPLACED"
    other = next(b for b in refdata.get(run["dataset_version"]).branches if b not in (card["source"], card["dest"]))
    as_seat(client, other)
    r = patch_report(client, rep["report_id"], {"revision": 2, "fields": {"delay_hours": 5}})
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    as_seat(client, None)  # HQ owns transport: the owner corrects a field
    r = patch_report(client, rep["report_id"], {"revision": 2, "fields": {"delay_hours": 99}})
    assert (r.status_code, r.json()["error"], r.json()["field"]) == (400, "out_of_bounds", "delay_hours")
    r = patch_report(client, rep["report_id"], {"revision": 1, "fields": {"delay_hours": 4}})
    assert (r.status_code, r.json()["error"]) == (409, "stale_version")
    three = patch_report(client, rep["report_id"], {"revision": 2, "fields": {"delay_hours": 4}}).json()
    assert (three["revision"], three["fields"]["delay_hours"], three["reporter_role"]) == (3, 4.0, "manager")
    r = confirm_report(client, rep["report_id"], revision=2)
    assert (r.status_code, r.json()["error"]) == (409, "stale_version"), r.json()
    out = confirmed_rev(client, rep["report_id"], 3)
    assert adjustments_in(run_id, card["case_id"], "departure_delay_h") == [
        {"kind": "departure_delay_h", "run_code": first, "hours": 4.0}]
    assert fact_row(run_id, out["fact_id"])["signal_id"] == three["signal_id"]
    r = patch_report(client, rep["report_id"], {"revision": 3, "fields": {"delay_hours": 1}})
    assert (r.status_code, r.json()["error"]) == (409, "wrong_state")
    seen = client.get(f"/api/signals/{three['signal_id']}").json()
    assert [(x["revision"], x["state"]) for x in seen["revisions"]] == [(1, "REPLACED"), (2, "REPLACED"),
                                                                         (3, "CONFIRMED")]
    edited = [e for e in table(run_id, "activity_log") if e["type"] == "report_edited"]
    assert [e["payload"]["revision"] for e in edited] == [2, 3]


def confirmed_rev(client, signal_id: str, revision: int) -> dict:
    r = confirm_report(client, signal_id, revision)
    assert r.status_code == 200, r.json()
    return r.json()


def test_report_list_pages(client):
    """GET /api/signals (07-13's gap): one row per report (its latest revision, never a REPLACED one), newest first,
    in pages (`page`, `page_size`, `total`)."""
    card = test_http.story(client)
    today = today_iso(client)
    as_seat(client, card["dest"])
    fields = {"zone": card["zone"], "free_totes": 5, "valid_from": today, "valid_to": today}
    made = [filed(client, "CAPACITY", {**fields, "free_totes": n}) for n in (1, 2, 3)]
    edited = patch_report(client, made[0]["signal_id"], {"revision": 1, "fields": {"free_totes": 9}}).json()
    page = client.get("/api/signals?page=1&page_size=2").json()
    assert (page.get("page"), page.get("page_size"), page.get("total")) == (1, 2, 3), page.keys()
    assert [x["signal_id"] for x in page["signals"]] == [edited["signal_id"], made[2]["signal_id"]]
    rest = client.get("/api/signals?page=2&page_size=2").json()
    assert [x["signal_id"] for x in rest["signals"]] == [made[1]["signal_id"]]
    assert client.get("/api/signals?state=REPLACED").json()["total"] == 1


# --- the impact preview: what-ifs labelled potential, changing nothing (07-15; D-12, D-16) ---------------------------

PREVIEW_LABEL = "Potential impact · unconfirmed"
ACTIVE = ("cases", "reservations", "decisions", "lot_holds", "reported_facts", "review_flags", "evidence_requests")


def preview(client, signal_id: str, revision: int = 1, headers: dict | None = None):
    return client.post(f"/api/signals/{signal_id}/preview", json={"revision": revision},
                       headers=key() if headers is None else headers)


def active_rows(run_id: str) -> dict:
    """Every row a preview must leave as it was: the cases, bookings, decisions, holds, facts, flags and requests of
    the workspace, and its real (not what-if) versions."""
    with db.tx() as conn:
        out = {t: db.rows(conn, f"SELECT * FROM {t} WHERE run_id = :r", r=run_id) for t in ACTIVE}
        out["versions"] = db.rows(conn, "SELECT case_id, rec_version, result, fact_checks FROM recommendations "
                                        "WHERE run_id = :r AND NOT is_whatif", r=run_id)
    return {t: sorted(rows, key=lambda x: json.dumps(x, sort_keys=True, default=str)) for t, rows in out.items()}


def live_in_scope(client, signal_id: str) -> list[str]:
    """The live cases the report's scope holds now, in case_id order: the matcher confirmation uses (api/scope.py)."""
    run = session_run(client)
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s", r=run["run_id"], s=signal_id)
        live, _, _ = scope.affected(conn, run, reports.scope_of(run, row))
    return [c["case_id"] for c in live]


def whatif_row(run_id: str, case_id: str, version: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                      r=run_id, c=case_id, v=version)


def test_preview_lists_potential_impact(client):
    """D-16 (the tracer): with the story case sent for approval (its holds booked), its destination's manager files
    CAPACITY "no free totes today" for their zone. The preview is 200, labelled "Potential impact · unconfirmed": a
    row per affected live case in case_id order (the scope matcher confirmation uses), each with the plan now and if
    confirmed, the estimated benefit now and if confirmed and whether the plan changes, and more = 0. The what-if
    versions are stored with is_whatif true; the story case's version, its bookings, its decisions, every hold, fact,
    flag and request are exactly as they were. The report's detail carries the stored preview."""
    card = test_http.story(client)
    run_id, today = session_run(client)["run_id"], today_iso(client)
    as_seat(client, card["source"])
    r = take_and_send(client, card["case_id"], card["rec_version"])
    assert r.status_code == 200, r.json()
    sent = test_http.story(client)
    assert sent["state"] == "PENDING_APPROVAL"
    as_seat(client, card["dest"])
    rep = filed(client, "CAPACITY", {"zone": card["zone"], "free_totes": 0, "valid_from": today, "valid_to": today})
    expected = live_in_scope(client, rep["signal_id"])
    assert card["case_id"] in expected
    before = active_rows(run_id)
    r = preview(client, rep["signal_id"])
    assert r.status_code == 200, r.json()
    out = r.json()
    assert (out.get("label"), out.get("revision"), out.get("complete"), out.get("message")) == (
        PREVIEW_LABEL, 1, True, None), out
    assert [c["case_id"] for c in out["cases"]] == expected[:5]
    assert (out["more"], out["more_text"]) == (0, None) and len(expected) <= 5
    for row in out["cases"]:
        assert {"case_id", "key", "rec_version", "whatif_version", "plan_now", "plan_if", "benefit_now", "benefit_if",
                "changed", "allowed"} <= set(row), row
        assert isinstance(row["changed"], bool) and isinstance(row["allowed"], bool)
        if row["whatif_version"] != row["rec_version"]:
            assert whatif_row(run_id, row["case_id"], row["whatif_version"])["is_whatif"] is True
    story_row = next(c for c in out["cases"] if c["case_id"] == card["case_id"])
    assert (story_row["rec_version"], story_row["plan_now"], story_row["benefit_now"]) == (
        sent["rec_version"], sent["suggested"], sent["est_benefit"])
    assert active_rows(run_id) == before  # nothing active changed (T-07-57)
    after = test_http.story(client)
    assert (after["state"], after["rec_version"]) == ("PENDING_APPROVAL", sent["rec_version"])
    assert client.get(f"/api/signals/{rep['signal_id']}").json()["preview"] == out


def case_lot(run_id: str, case_id: str) -> str:
    with db.tx() as conn:
        return cases.case_row(conn, run_id, case_id)["lot_id"]


def decide(client, card: dict, decision: str, given) -> dict:
    """An accept or approve over HTTP, naming the plan the page shows and acknowledging its Advisory facts."""
    r = client.post(f"/api/cases/{card['case_id']}/decision", headers=key(), json={
        "rec_version": card["rec_version"], "decision": decision, "plan_key": card["suggested_key"],
        "acknowledged": given})
    assert r.status_code == 200, r.json()
    return r.json()


def test_preview_blocks_nothing(client):
    """Acceptance check 9 (D-12, D-16): the source's manager counts the story case's own lot at 0 + 0 and files it,
    unconfirmed. Its preview says the plan changes: the transfer would no longer be allowed, and the what-if carries
    the count as a reported fact (a count can't be an override, research F-2). Nothing active changed, and Send, the
    destination's accept and the source's approve (with the acknowledgement) still commit the transfer: no lot hold,
    no review flag, no block and no new version appear, and the report stays unconfirmed."""
    card = test_http.story(client)
    run_id, cid = session_run(client)["run_id"], card["case_id"]
    lot_id = case_lot(run_id, cid)
    as_seat(client, card["source"])
    rep = filed(client, "STOCK_COUNT", {"lot_id": lot_id, "shelf_units": 0, "backroom_units": 0}, observed="now")
    before = active_rows(run_id)
    r = preview(client, rep["signal_id"])
    assert r.status_code == 200, r.json()
    out = r.json()
    row = next(c for c in out["cases"] if c["case_id"] == cid)
    assert (row["recommended_before"], row["changed"], row["allowed"]) == (card["suggested_key"], True, False), row
    assert row["plan_if"] != row["plan_now"] == card["suggested"]
    whatif = whatif_row(run_id, cid, row["whatif_version"])
    assert whatif["is_whatif"] is True
    count = {"kind": "stock_count", "lot_id": lot_id, "units": 0.0, "counted_day": session_run(client)["day"]}
    assert count in whatif["engine_input"]["reported_facts"]
    assert all(o["kind"] != "stock_count" for o in whatif["engine_input"]["overrides"])
    assert active_rows(run_id) == before
    r = take_and_send(client, cid, card["rec_version"])
    assert (r.status_code, r.json()["state"]) == (200, "PENDING_APPROVAL"), r.json()
    given = ack(run_id, cid)
    as_seat(client, card["dest"])
    assert decide(client, card, "accept", given) == {"state": "PENDING_APPROVAL"}
    as_seat(client, card["source"])
    assert decide(client, card, "approve", given) == {"state": "APPROVED"}
    after = test_http.story(client)
    assert (after["state"], after["rec_version"], after["blocked"]) == ("APPROVED", card["rec_version"], False)
    assert table(run_id, "lot_holds") == [] and table(run_id, "review_flags") == []
    assert client.get(f"/api/signals/{rep['signal_id']}").json()["state"] == "READ"


def test_preview_texts(client, monkeypatch):
    """D-13, D-16 (research F-2): an EXPIRY report previews through the fact what-if (its printed date a reported fact
    of the what-if input); a CONDITION report's preview is 200 with "Requires human review · engine impact not
    calculated" and no rows; a report with blank required fields reads "Complete report details to preview impact."
    and runs nothing: no engine call and no what-if version."""
    card = test_http.story(client)
    run = session_run(client)
    run_id, cid = run["run_id"], card["case_id"]
    lot_id = case_lot(run_id, cid)
    printed = max(run["day"], world_lot(run_id, lot_id)["expiry_day"] - 1)
    as_seat(client, card["source"])
    rep = filed(client, "EXPIRY", {"lot_id": lot_id, "printed_date": cases.iso_of(run, printed)}, observed="now")
    out = preview(client, rep["signal_id"]).json()
    assert (out["label"], out["complete"], out["message"]) == (PREVIEW_LABEL, True, None), out
    row = next(c for c in out["cases"] if c["case_id"] == cid)
    whatif = whatif_row(run_id, cid, row["whatif_version"])
    assert {"kind": "expiry", "lot_id": lot_id, "expiry_day": printed} in whatif["engine_input"]["reported_facts"]
    assert whatif["is_whatif"] is True and all(o["kind"] != "expiry" for o in whatif["engine_input"]["overrides"])
    calls = []
    real = evaluate.compute
    monkeypatch.setattr(evaluate, "compute", lambda inp: calls.append(inp) or real(inp))
    with db.tx() as conn:
        versions = db.one(conn, "SELECT count(*) AS n FROM recommendations WHERE run_id = :r", r=run_id)["n"]
    cond = filed(client, "CONDITION", {"lot_id": lot_id, "condition": "DAMAGE"})
    out = preview(client, cond["signal_id"]).json()
    assert (out["label"], out["complete"], out["message"], out["cases"], out["more"]) == (
        PREVIEW_LABEL, True, REVIEW_ONLY, [], 0), out
    as_seat(client, card["dest"])
    blank = filed(client, "CAPACITY", {"zone": card["zone"], "free_totes": None, "valid_from": today_iso(client),
                                       "valid_to": today_iso(client)})
    assert blank["needs_input"] == [{"field": "free_totes"}]
    out = preview(client, blank["signal_id"]).json()
    assert (out["label"], out["complete"], out["message"], out["cases"], out["more"]) == (
        PREVIEW_LABEL, False, "Complete report details to preview impact.", [], 0), out
    assert calls == []
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM recommendations WHERE run_id = :r", r=run_id)["n"] == versions


def test_preview_counts_the_rest(client):
    """Edge SIG-03 (D-16): with seven live cases in the report's scope (six twins of the story lot added, each input
    holding the counted lot), the preview re-runs the first five by case_id and counts the rest: more = 2, "and 2
    more affected cases". The limit is reports.preview_cases."""
    card = test_http.story(client)
    run = session_run(client)
    lot_id = case_lot(run["run_id"], card["case_id"])
    base = lot_row(run, lot_id)
    for n in range(1, 7):
        twin(run, base, n)
    as_seat(client, card["source"])
    rep = filed(client, "STOCK_COUNT", {"lot_id": lot_id, "shelf_units": 4, "backroom_units": 0}, observed="now")
    expected = live_in_scope(client, rep["signal_id"])
    assert len(expected) == 7 and card["case_id"] in expected
    out = preview(client, rep["signal_id"]).json()
    assert [c["case_id"] for c in out["cases"]] == expected[:5] and len(out["cases"]) == 5
    assert (out["more"], out["more_text"]) == (2, "and 2 more affected cases"), out


def test_preview_is_single_flight(client, monkeypatch):
    """T-07-56 (D-16): the preview needs an Idempotency-Key (400 idempotency_key) and the report's current revision;
    a second preview of the same revision (a new key) returns the stored body and runs no engine (evaluate.compute
    and evaluate.store spied: none is called, not even for a cache hit); after an edit the old revision is 409 stale_version and the new one previews again. The previewed
    what-if agrees with the confirmed result: confirming gives the story case the very version the preview ran."""
    card = test_http.story(client)
    run_id, today = session_run(client)["run_id"], today_iso(client)
    as_seat(client, card["dest"])
    rep = filed(client, "CAPACITY", {"zone": card["zone"], "free_totes": 0, "valid_from": today, "valid_to": today})
    r = client.post(f"/api/signals/{rep['signal_id']}/preview", json={"revision": 1})
    assert (r.status_code, r.json()["error"]) == (400, "idempotency_key")
    r = preview(client, rep["signal_id"], revision=2)
    assert (r.status_code, r.json()["error"]) == (409, "stale_version")
    first = preview(client, rep["signal_id"]).json()
    calls, stored = [], []
    real, real_store = evaluate.compute, evaluate.store
    monkeypatch.setattr(evaluate, "compute", lambda inp: calls.append(inp) or real(inp))
    monkeypatch.setattr(evaluate, "store", lambda *a, **k: stored.append(a) or real_store(*a, **k))
    again = preview(client, rep["signal_id"])  # the engine cache would answer a re-run too: no what-if is stored
    assert again.status_code == 200 and again.json() == first and (calls, stored) == ([], [])
    two = patch_report(client, rep["signal_id"], {"revision": 1, "fields": {"free_totes": 1}}).json()
    assert two["revision"] == 2
    r = preview(client, rep["signal_id"], revision=1)
    assert (r.status_code, r.json()["error"]) == (409, "stale_version")
    second = preview(client, rep["signal_id"], revision=2).json()
    assert second["revision"] == 2 and len(calls) >= 1 and len(stored) >= 1
    row = next(c for c in second["cases"] if c["case_id"] == card["case_id"])
    assert client.get(f"/api/signals/{rep['signal_id']}").json()["preview"] == second
    assert confirm_report(client, rep["signal_id"], revision=2).status_code == 200
    assert test_http.story(client)["rec_version"] == row["whatif_version"]  # the preview agrees with the result


# --- 07-20: the sheet's preview before a report is filed, and the Business events filters (D-04, D-16; G5) -----------

def draft_preview(client, body: dict, headers: dict | None = None):
    return client.post("/api/signals/preview", json=body, headers=key() if headers is None else headers)


def test_previews_spend_their_own_cap_before_the_engine_reuse_kept_results_and_filing_stays_open(client, monkeypatch):
    """A preview runs the engine (up to reports.preview_cases what-ifs), so a preview with a what-if not stored yet
    spends the `preview` cap before its first engine run (07-20 review, 2026-10-18): the workspace's limit (scope
    run:<run_id>, every seat), then the limit across workspaces (caps.global.preview). A refusal is 429 cap_reached
    naming when it resets, and the engine never runs. A repeated preview (the same draft again, or the report filed
    from it) reads the stored what-ifs back: no engine run, no cap. Filing a report keeps its own allowance. The test
    uses a day window so no clock edge splits it; the configured windows are in api/settings.json."""
    from api import config, evaluate
    card = test_http.story(client)
    run = session_run(client)
    today = today_iso(client)
    fields = {"zone": card["zone"], "free_totes": 0, "valid_from": today, "valid_to": today}
    other = {**fields, "free_totes": 1}
    as_seat(client, card["dest"])
    assert "preview" in config.WORKSPACE_CAPS and "preview" in config.GLOBAL_CAPS  # configured, as Policy
    mine = f"run:{run['run_id']}"

    def counts():
        with db.tx() as conn:
            return {r["scope"]: r["count"] for r in db.rows(conn, "SELECT scope, sum(count) AS count FROM usage_counts "
                                                            "WHERE kind = 'preview' AND scope LIKE 'run:%' GROUP BY scope")}
    before = counts()
    monkeypatch.setitem(config.WORKSPACE_CAPS, "preview", (1, "day"))
    first = draft_preview(client, {"kind": "CAPACITY", "fields": fields})
    assert first.status_code == 200 and first.json()["cases"], first.json()
    ran = []
    real = evaluate.store
    monkeypatch.setattr(evaluate, "store", lambda *a, **k: ran.append(a) or real(*a, **k))
    again = draft_preview(client, {"kind": "CAPACITY", "fields": fields})
    assert (again.status_code, again.json(), ran) == (200, first.json(), [])  # read back: no engine, no cap
    r = draft_preview(client, {"kind": "CAPACITY", "fields": other})
    assert (r.status_code, r.json()["error"], r.json()["kind"]) == (429, "cap_reached", "preview"), r.json()
    assert "this workspace's limit" in r.json()["message"] and "It resets at" in r.json()["message"]
    assert ran == []  # refused before the engine
    filed = file_report(client, "CAPACITY", fields)
    assert filed.status_code == 200, filed.json()  # filing stays open: its own allowance
    r = preview(client, filed.json()["signal_id"], revision=1)
    assert (r.status_code, r.json()["cases"], ran) == (200, first.json()["cases"], [])  # the draft's what-ifs, read back
    filed = file_report(client, "CAPACITY", other)
    assert filed.status_code == 200, filed.json()
    r = preview(client, filed.json()["signal_id"], revision=1)
    assert (r.status_code, r.json()["error"]) == (429, "cap_reached") and ran == []  # a filed report's new what-if too
    assert counts() == {**before, mine: before.get(mine, 0) + 1}  # one spend, in this workspace's scope only
    monkeypatch.setitem(config.WORKSPACE_CAPS, "preview", (100, "day"))
    monkeypatch.setitem(config.GLOBAL_WINDOWS, "preview", "day")  # a day window here, so no clock edge splits the test
    monkeypatch.setitem(config.GLOBAL_CAPS, "preview", 0)  # the limit across workspaces: any spend is over it
    r = draft_preview(client, {"kind": "CAPACITY", "fields": other})
    assert (r.status_code, r.json()["error"]) == (429, "cap_reached") and "daily limit" in r.json()["message"]
    assert ran == []
    assert draft_preview(client, {"kind": "CAPACITY", "fields": fields}).status_code == 200  # stored: still served


def test_draft_preview_before_filing(client):
    """D-16 in the Report an observation sheet (07-20): `POST /api/signals/preview` {kind, fields} previews a report
    that is not filed yet, with the seat's own rules (a manager for their store, HQ transport only: 403 otherwise) and
    the values checked as filing checks them (400 out_of_bounds). The body is the stored preview's shape with
    signal_id, report_id and revision null: the same rows, labelled "Potential impact · unconfirmed", and the same
    texts for blank fields and a condition. It stores no report, spends no allowance and changes nothing active; a
    report filed with the same fields previews the same rows."""
    card = test_http.story(client)
    run_id, today = session_run(client)["run_id"], today_iso(client)
    fields = {"zone": card["zone"], "free_totes": 0, "valid_from": today, "valid_to": today}
    as_seat(client, card["dest"])
    left = reports_page(client)["allowance"]["reports_left"]
    before = active_rows(run_id)
    r = client.post("/api/signals/preview", json={"kind": "CAPACITY", "fields": fields})
    assert (r.status_code, r.json()["error"]) == (400, "idempotency_key")
    r = draft_preview(client, {"kind": "CAPACITY", "fields": fields})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert (out["label"], out["complete"], out["message"]) == (PREVIEW_LABEL, True, None), out
    assert (out["signal_id"], out["report_id"], out["revision"], out["kind"]) == (None, None, None, "CAPACITY")
    assert card["case_id"] in [c["case_id"] for c in out["cases"]]
    assert active_rows(run_id) == before  # nothing active changed
    assert [s for s in table(run_id, "signals") if s["report_id"] and s["provenance"] != "simulated"] == []
    assert reports_page(client)["allowance"]["reports_left"] == left  # a preview spends nothing
    blank = draft_preview(client, {"kind": "CAPACITY", "fields": {**fields, "free_totes": None}}).json()
    assert (blank["complete"], blank["message"], blank["cases"]) == (
        False, "Complete report details to preview impact.", []), blank
    lot_id = case_lot(run_id, card["case_id"])
    as_seat(client, card["source"])
    cond = draft_preview(client, {"kind": "CONDITION", "fields": {"lot_id": lot_id, "condition": "DAMAGE"}}).json()
    assert (cond["complete"], cond["message"], cond["cases"]) == (True, REVIEW_ONLY, []), cond
    r = draft_preview(client, {"kind": "CAPACITY", "fields": {**fields, "free_totes": -1}})
    assert (r.status_code, r.json()["error"]) == (400, "out_of_bounds")
    r = draft_preview(client, {"kind": "CAPACITY", "fields": "nope"})
    assert (r.status_code, r.json()["error"]) == (400, "bad_request")
    as_seat(client, None)
    r = draft_preview(client, {"kind": "CAPACITY", "fields": fields})
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")  # HQ reports transport only
    as_seat(client, card["dest"])
    rep = filed(client, "CAPACITY", fields)
    stored = preview(client, rep["signal_id"]).json()
    assert [(c["case_id"], c["plan_if"], c["changed"]) for c in stored["cases"]] == [
        (c["case_id"], c["plan_if"], c["changed"]) for c in out["cases"]]


def test_report_list_filters(client):
    """Business events' filters are the server's (07-20; 06.1 D-17, G5): `mine=1` lists the READ reports this seat
    confirms (needs_my_confirmation of them), `store` one store's reports, `case` (a stock decision's key or case_id)
    the reports linked to it or holding it in their scope; each row carries `affects_count`, how many live stock
    decisions its scope held when filed. A malformed `mine` is 400."""
    card = test_http.story(client)
    today = today_iso(client)
    fields = {"zone": card["zone"], "free_totes": 3, "valid_from": today, "valid_to": today}
    as_seat(client, card["dest"])
    mine = filed(client, "CAPACITY", fields)
    assert mine["affects_count"] >= 1
    lot_id = case_lot(session_run(client)["run_id"], card["case_id"])
    as_seat(client, card["source"])
    theirs = filed(client, "CONDITION", {"lot_id": lot_id, "condition": "DISPLAY"}, case_id=card["case_id"])
    assert theirs["sku"] == card["sku"] and mine["sku"] is None  # the lot's product; a zone's report names none
    as_seat(client, card["dest"])
    page = client.get("/api/signals?mine=1").json()
    assert mine["signal_id"] in [x["signal_id"] for x in page["signals"]]
    assert all(x["state"] == "READ" and x["owner"] == {"role": "manager", "branch": card["dest"]}
               for x in page["signals"])
    assert page["total"] == page["needs_my_confirmation"]
    as_seat(client, None)
    hq = client.get(f"/api/signals?store={card['source']}").json()
    assert theirs["signal_id"] in [x["signal_id"] for x in hq["signals"]]
    assert all(x["branch"] == card["source"] for x in hq["signals"])
    by_key = client.get(f"/api/signals?case={card['key']}").json()
    by_id = client.get(f"/api/signals?case={card['case_id']}").json()
    assert by_key["signals"] == by_id["signals"]
    assert {mine["signal_id"], theirs["signal_id"]} <= {x["signal_id"] for x in by_key["signals"]}
    assert client.get("/api/signals?case=NO-SUCH-CASE").json()["total"] == 0
    assert client.get("/api/signals?mine=yes").status_code == 400
