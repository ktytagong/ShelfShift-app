"""The nine steps, the four stages, the owner, the next action and the blocked flag as server keys, the board's
filters and stage counts, static hold times and closed summaries (06.1-CONTEXT D-17, D-23, D-24, D-36, D-54, D-57,
D-65; 04-API-CONTRACT "Product mode (06.1)", Card and detail keys; api/stepkeys.py).

A documented test shortcut (06.1-10-PLAN.md): the states, the owner column, the work orders, the stock checks and the
discrepancies a step needs are set directly in the test database, so this file does not wait on the take-ownership,
dispatch, receipt and close routes (06.1-08, -09, -11 prove those flows; 06.1-31 proves them end to end). Every read
goes through the server's own card and detail.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api import cases, config, db, refdata
from api.app import app
from tests.api.conftest import fresh, key, seat, sign_in

STEP_KEYS = {"step", "step_number", "stage", "first_stage", "owner", "next_action", "blocked", "blocked_reasons",
             "closed_as"}
CASE_COLUMNS = {"state", "owner_taken_by", "missing_data", "hold_expired", "active_rec_version", "closed_as"}


# --- helpers ----------------------------------------------------------------------------------------------------------

def story_client() -> tuple[TestClient, str, dict]:
    """A new workspace signed in as the story case's source manager: (client, run_id, the story case row)."""
    c = TestClient(app)
    sign_in(c)
    run_id = c.get("/api/session").json()["run_id"]
    case = row(run_id, cases.STORY_KEY)
    sign_in(c, case["source_branch"])
    return c, run_id, case


def row(run_id: str, case_id: str) -> dict:
    with db.tx() as conn:
        return db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", r=run_id, c=case_id)


def set_case(run_id: str, case_id: str, **cols) -> None:
    """The documented shortcut: case columns set directly (names from a fixed list, never from a request)."""
    assert set(cols) <= CASE_COLUMNS, cols
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with db.tx() as conn:
        db.run(conn, f"UPDATE cases SET {sets} WHERE run_id = :r AND case_id = :c", r=run_id, c=case_id, **cols)


def card_of(c: TestClient, case_key: str, query: str = "") -> dict:
    body = c.get(f"/api/cases{query}").json()
    return next(x for x in body["cases"] if x["key"] == case_key)


def add_work_order(run_id: str, case: dict, kind: str, state: str, dest: str | None) -> str:
    action_id = f"{case['case_id']}#t:{kind.lower()}:{uuid.uuid4().hex[:6]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO world_actions (run_id, world, action_id, case_id, kind, lot_id, sku, qty, dest, pct, "
                     "origin, state) VALUES (:r, 'shelfshift', :a, :c, :k, :l, :s, 5, :d, :p, :o, :st)",
               r=run_id, a=action_id, c=case["case_id"], k=kind, l=case["lot_id"], s=case["sku"], d=dest,
               p=30 if kind == "MARKDOWN" else None, o=case["source_branch"] if kind == "TRANSFER" else None, st=state)
    return action_id


def add_discrepancy(run_id: str, case: dict, action_id: str) -> str:
    did = f"D-{uuid.uuid4().hex[:8]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO discrepancies (run_id, discrepancy_id, case_id, action_id, kind, units, open_units, "
                     "state) VALUES (:r, :d, :c, :a, 'SHORT', 2, 2, 'OPEN')", r=run_id, d=did, c=case["case_id"],
               a=action_id)
    return did


def add_stock_check(run_id: str, case: dict, state: str = "OPEN") -> None:
    with db.tx() as conn:
        db.run(conn, "INSERT INTO evidence_requests (run_id, request_id, case_id, rec_version, fact_id, fact_kind, "
                     "sku, lot_id, to_branch, ask_text, due_day, due_min, state, created_by, created_day) VALUES (:r, "
                     ":q, :c, :v, :f, 'RECORD_QTY', :s, :l, :b, 'Count it', :d, 600, :st, 'manager', :d)",
               r=run_id, q=f"Q-{uuid.uuid4().hex[:8]}", c=case["case_id"], v=case["active_rec_version"],
               f=f"F-{uuid.uuid4().hex[:6]}", s=case["sku"], l=case["lot_id"], b=case["source_branch"],
               d=fresh(run_id)["day"], st=state)


# --- Task 1: the steps from the server -------------------------------------------------------------------------------

def test_the_story_card_says_trigger_then_compare_after_take_ownership(template):
    c, run_id, case = story_client()
    set_case(run_id, case["case_id"], owner_taken_by=None)
    src = case["source_branch"]
    card = card_of(c, cases.STORY_KEY)
    assert STEP_KEYS <= set(card)
    assert (card["step"], card["step_number"], card["stage"], card["first_stage"]) == (
        "TRIGGER", 1, "decide", "needs_action")
    assert card["owner"] is None and card["closed_as"] is None
    assert card["next_action"] == {"key": "take_ownership", "who": [{"role": "manager", "branch": src}]}
    set_case(run_id, case["case_id"], owner_taken_by=src)
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"], card["first_stage"]) == (
        "COMPARE", 4, "decide", "needs_action")
    assert card["owner"] == {"account": src, "role": "manager", "label": f"{src} manager"}
    assert card["next_action"] == {"key": "send", "who": [{"role": "manager", "branch": src}]}
    detail = c.get(f"/api/cases/{cases.STORY_KEY}").json()
    assert {k: detail[k] for k in STEP_KEYS} == {k: card[k] for k in STEP_KEYS}  # one function, both views
    add_stock_check(run_id, case)
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"]) == ("CONTEXT", 2, "decide")


def test_approval_commit_and_execute_steps(template):
    c, run_id, case = story_client()
    src = case["source_branch"]
    set_case(run_id, case["case_id"], owner_taken_by=src, state="PENDING_APPROVAL")
    card = card_of(c, cases.STORY_KEY)
    dest = card["dest"]
    assert dest is not None  # the story case's plan is a transfer
    assert (card["step"], card["step_number"], card["stage"], card["first_stage"]) == (
        "APPROVE", 5, "authorise", None)
    assert card["next_action"] == {"key": "accept", "who": [{"role": "manager", "branch": dest}]}
    set_case(run_id, case["case_id"], state="APPROVED")
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"]) == ("COMMIT", 6, "authorise")
    assert card["next_action"] == {"key": "prepare", "who": [{"role": "manager", "branch": src}]}
    set_case(run_id, case["case_id"], state="PREPARED")
    add_work_order(run_id, case, "TRANSFER", "PLANNED", dest)
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"], card["first_stage"]) == ("EXECUTE", 7, "do", None)
    assert card["next_action"] == {"key": "record_dispatch", "who": [{"role": "manager", "branch": src}]}
    with db.tx() as conn:
        db.run(conn, "UPDATE world_actions SET state = 'DISPATCHED' WHERE run_id = :r AND case_id = :c",
               r=run_id, c=case["case_id"])
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["next_action"]) == (
        "EXECUTE", {"key": "record_receipt", "who": [{"role": "manager", "branch": dest}]})


def test_an_open_receipt_discrepancy_keeps_the_step_at_execute(template):
    c, run_id, case = story_client()
    src = case["source_branch"]
    set_case(run_id, case["case_id"], owner_taken_by=src, state="EXECUTED")
    action = add_work_order(run_id, case, "TRANSFER", "RECEIVED", card_of(c, cases.STORY_KEY)["dest"])
    did = add_discrepancy(run_id, case, action)
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"]) == ("EXECUTE", 7, "do")
    assert card["next_action"] == {"key": "reconcile", "who": [{"role": "manager", "branch": src}]}
    assert card["blocked"] is False  # the plan ran: nothing left to block
    with db.tx() as conn:
        db.run(conn, "UPDATE discrepancies SET state = 'RECONCILED', open_units = 0 WHERE run_id = :r "
                     "AND discrepancy_id = :d", r=run_id, d=did)
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"]) == ("VERIFY", 8, "check")
    assert card["next_action"] == {"key": "close", "who": [{"role": "manager", "branch": src}]}
    set_case(run_id, case["case_id"], state="CLOSED", closed_as="COMPLETED")
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["step_number"], card["stage"], card["closed_as"]) == (
        "ARCHIVE", 9, "check", "COMPLETED")
    assert card["next_action"] == {"key": None, "who": []}


@pytest.fixture(scope="module")
def flagged(public_run):
    """The tiny public template after its day-0 screen with a test copy of the thresholds (min_waste_units 0), so
    several lots are flagged (a test setting only; the reference thresholds are unchanged). Read directly, never
    made ACTIVE."""
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CASE_OPENING", {**config.CASE_OPENING, "min_waste_units": 0})
    try:
        with db.tx() as conn:
            cases.open_cases(conn, fresh(public_run["run_id"]))
    finally:
        mp.undo()
    with db.tx() as conn:
        flags = db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r ORDER BY key", r=public_run["run_id"])
    assert len(flags) >= 3, flags
    return {"run": fresh(public_run["run_id"]), "flags": flags}


def test_a_flag_is_trigger_and_needs_evaluation(flagged):
    run = flagged["run"]
    with db.tx() as conn:
        body = cases.board_page(conn, run, seat(None), page_size=len(flagged["flags"]))
        flag = flagged["flags"][0]
        detail = cases.detail(conn, run, flag["key"])
    cards = [x for x in body["cases"] if x.get("needs_evaluation")]
    assert len(cards) == len(flagged["flags"])
    for x in cards:
        assert (x["step"], x["step_number"], x["stage"], x["first_stage"]) == (
            "TRIGGER", 1, "decide", "needs_evaluation")
        assert x["owner"] is None and x["blocked"] is False and x["blocked_reasons"] == []
        assert x["next_action"] == {"key": "evaluate", "who": [{"role": "manager", "branch": x["source"]}]}
    assert detail["step"] == "TRIGGER" and detail["first_stage"] == "needs_evaluation"
    assert detail["comparison"] == {"status": "not_computed", "reason": None, "columns": []}  # 06.1-04's open item


# --- Task 2: the board's server filters, stage counts and the blocked flag -----------------------------------------

def every_card(c: TestClient, query: str = "") -> dict:
    r = c.get(f"/api/cases?{query}")
    assert r.status_code == 200, r.json()
    return r.json()


def counted(cards: list[dict], n_flags: int = 0) -> dict:
    out = {"decide": n_flags, "authorise": 0, "do": 0, "check": 0, "closed": 0}
    for x in cards:
        out["closed" if x["step"] == "ARCHIVE" else x["stage"]] += 1
    return out


def test_owner_product_deadline_and_blocked_filter_on_the_server(template):
    c, run_id, case = story_client()
    src = case["source_branch"]
    everything = every_card(c)["cases"]
    other = next(x for x in everything if x["key"] != cases.STORY_KEY and x["state"] in ("OPEN", "WAITING"))
    set_case(run_id, case["case_id"], owner_taken_by=src)
    set_case(run_id, other["case_id"], owner_taken_by="HQ")
    mine = every_card(c, "owner=me")["cases"]
    assert cases.STORY_KEY in [x["key"] for x in mine] and other["key"] not in [x["key"] for x in mine]
    assert all(x["owner"]["account"] == src for x in mine)
    by_product = every_card(c, f"product={case['sku']}")["cases"]
    assert cases.STORY_KEY in [x["key"] for x in by_product] and all(x["sku"] == case["sku"] for x in by_product)
    day = fresh(run_id)["day"]
    today = every_card(c, "deadline=scenario_date")["cases"]
    undated = every_card(c, "deadline=none")["cases"]
    assert all(x["due"] is not None and x["due"]["day"] == day for x in today)
    assert all(x["due"] is None for x in undated)
    full = every_card(c)["cases"]
    assert [x["key"] for x in today] == [x["key"] for x in full if x["due"] and x["due"]["day"] == day]  # order kept
    assert [x["key"] for x in undated] == [x["key"] for x in full if x["due"] is None]
    assert every_card(c, "blocked=1")["cases"] == [x for x in full if x["blocked"]]
    set_case(run_id, case["case_id"], missing_data="missing data: no forecast for Thu")
    stuck = every_card(c, "blocked=1")
    story = next(x for x in stuck["cases"] if x["key"] == cases.STORY_KEY)
    assert (story["blocked"], story["blocked_reasons"]) == (True, ["missing_data"])
    assert all(x["blocked"] for x in stuck["cases"]) and stuck["blocked_total"] == len(stuck["cases"])
    for bad in ("owner=nobody", "owner=", "deadline=tomorrow", "blocked=0", "blocked=yes", "product=NOPE",
                "owner=me&owner=me"):
        r = c.get(f"/api/cases?{bad}")
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), bad


def test_stage_counts_and_blocked_total_count_the_filtered_set(template):
    c, run_id, case = story_client()
    src = case["source_branch"]
    set_case(run_id, case["case_id"], owner_taken_by=src, missing_data="missing data: no forecast for Thu")
    body = every_card(c)
    assert body["stage_counts"] == counted(body["cases"]) and sum(body["stage_counts"].values()) == body["total"]
    assert body["blocked_total"] == sum(1 for x in body["cases"] if x["blocked"]) >= 1
    mine = every_card(c, "owner=me")
    assert mine["stage_counts"] == counted(mine["cases"]) and mine["total"] == len(mine["cases"])
    assert mine["blocked_total"] == sum(1 for x in mine["cases"] if x["blocked"])


def test_a_public_board_counts_every_page_and_keeps_evaluated_and_at_risk(flagged):
    run = flagged["run"]
    flags = flagged["flags"]
    sku = flags[0]["sku"]
    with db.tx() as conn:
        first = cases.board_page(conn, run, seat(None), page_size=1)
        whole = cases.board_page(conn, run, seat(None), page_size=len(flags))
        product = cases.board_page(conn, run, seat(None), page_size=1, product=sku)
        owned = cases.board_page(conn, run, seat(None), page_size=1, owner="me")
        undated = cases.board_page(conn, run, seat(None), page_size=len(flags), deadline="none")
        today = cases.board_page(conn, run, seat(None), page_size=1, deadline="scenario_date")
        stuck = cases.board_page(conn, run, seat(None), page_size=1, blocked="1")
    n = len(flags)
    assert first["total"] == n and first["stage_counts"] == whole["stage_counts"] == counted([], n)  # every page
    assert (first["evaluated"], first["at_risk"], first["blocked_total"]) == (0, n, 0)
    assert [x["key"] for x in undated["cases"]] == [x["key"] for x in whole["cases"]]  # a flag has no deadline
    with_sku = sum(1 for f in flags if f["sku"] == sku)
    assert (product["total"], product["at_risk"], product["evaluated"]) == (with_sku, with_sku, 0)
    assert product["stage_counts"]["decide"] == with_sku and all(x["sku"] == sku for x in product["cases"])
    for empty in (owned, today, stuck):  # nobody owns a flag, a flag has no deadline, none is blocked
        assert (empty["cases"], empty["total"], empty["evaluated"], empty["at_risk"]) == ([], 0, 0, 0)
        assert empty["stage_counts"] == counted([]) and empty["blocked_total"] == 0


def test_a_prepared_case_with_an_active_lot_hold_is_blocked_by_the_dispatch_check(template):
    from tests.api.test_advance import prepare_story
    c, run_id, case = story_client()
    set_case(run_id, case["case_id"], owner_taken_by=case["source_branch"])
    action = prepare_story(fresh(run_id))
    assert action["state"] == "PLANNED"
    card = card_of(c, cases.STORY_KEY)
    assert (card["step"], card["blocked"], card["blocked_reasons"]) == ("EXECUTE", False, [])
    with db.tx() as conn:
        db.run(conn, "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, state) VALUES "
                     "(:r, 'h-1', :l, :b, 'MIXED_DATES', 'Mixed dates found on the shelf', :b, 'ACTIVE')",
               r=run_id, l=case["lot_id"], b=case["source_branch"])
    card = card_of(c, cases.STORY_KEY)
    assert (card["blocked"], card["blocked_reasons"]) == (True, ["dispatch_blocked"])
    assert cases.STORY_KEY in [x["key"] for x in every_card(c, "blocked=1")["cases"]]


def test_an_expired_capacity_hold_is_blocked_until_check_availability(template):
    c, run_id, case = story_client()
    set_case(run_id, case["case_id"], owner_taken_by=case["source_branch"], hold_expired=True)
    card = card_of(c, cases.STORY_KEY)
    assert (card["blocked"], card["blocked_reasons"]) == (True, ["hold_expired"])
    assert card["next_action"]["key"] == "check_availability"


# --- Task 3: static hold times with the server clock, and no "Unverified" on closed summaries ----------------------

def test_a_sent_transfer_holds_until_a_static_time_with_the_server_clock(template):
    from api import workflow
    c, run_id, case = story_client()
    src = case["source_branch"]
    set_case(run_id, case["case_id"], owner_taken_by=src)

    def send(conn):
        run = db.lock_run(conn, run_id)
        return workflow.send(conn, run, seat(src), case["case_id"], case["active_rec_version"])
    db.in_tx(send)
    with db.tx() as conn:  # five minutes left, so the hold_expiring badge shows
        db.run(conn, "UPDATE reservations SET expires_at = now() + interval '5 minutes' WHERE run_id = :r "
                     "AND case_id = :c AND state = 'HELD'", r=run_id, c=case["case_id"])
    d = c.get(f"/api/cases/{cases.STORY_KEY}").json()
    hold = d["hold"]
    until, now = datetime.fromisoformat(hold["until"]), datetime.fromisoformat(hold["server_now"])
    assert until.utcoffset().total_seconds() == 0 and now.utcoffset().total_seconds() == 0  # ISO UTC
    assert now < until and d["approvals"]["hold"] == hold
    badge = next(b for b in d["badges"] if b["kind"] == "hold_expiring")
    assert badge["text"] == f"Held until {until.astimezone(config.CAP_TZ).strftime('%H:%M')}"
    assert re.fullmatch(r"Held until \d\d:\d\d", badge["text"])
    for b in d["badges"]:
        assert not re.search(r"\d+\s*min", b["text"]), b  # no minutes left in any badge text (D-57)


def test_a_closed_or_rejected_decision_has_no_unverified_sentence(template):
    c, run_id, case = story_client()
    assert "Unverified:" in c.get(f"/api/cases/{cases.STORY_KEY}").json()["summary"]  # an OPEN case still may
    for state in ("REJECTED", "CLOSED"):
        set_case(run_id, case["case_id"], state=state)
        text = c.get(f"/api/cases/{cases.STORY_KEY}").json()["summary"]
        assert text.startswith("The engine recommends") and "Unverified:" not in text, state


# --- orchestrator addition (06.1-09 deferred item 1): a dispatched transfer is never closed as "lot gone" ----------

def test_next_day_never_closes_a_case_whose_whole_lot_is_on_the_truck(client, monkeypatch):
    """The whole committed quantity dispatched and the rest of the source lot gone, so the lot has no stock on the
    record or physically while the moved lot does not exist yet: Next day leaves the case PREPARED with its work order
    DISPATCHED, waiting for the destination's receipt (06.1 D-34)."""
    from tests.api.test_execution import committed, dispatched, story_case, work_order
    monkeypatch.setattr(config, "DEMO_MODE", True)  # Next day is a demo control (06.1 D-62)
    run, action, _, _, sent = dispatched(client, short=0)
    assert sent == committed(action)
    with db.tx() as conn:  # the documented shortcut: what the dispatch left at the source sold out
        for table in ("lots", "truth_lots"):
            db.run(conn, f"UPDATE {table} SET qty = 0 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                   r=run["run_id"], l=action["lot_id"])
    version = client.get("/api/session").json()["version"]
    r = client.post("/api/demo/advance-day", json={"expected_version": version}, headers=key())
    assert r.status_code == 200 and r.json()["advanced"], r.json()
    case = story_case(run["run_id"])
    assert (case["state"], case["closed_as"]) == ("PREPARED", None)
    assert work_order(run["run_id"], case["case_id"])["state"] == "DISPATCHED"
