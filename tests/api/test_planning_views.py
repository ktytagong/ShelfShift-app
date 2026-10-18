"""Today and the planned schedule, read-only (06.1-CONTEXT D-11, D-17, D-18, D-22, D-29, D-33; 04-API-CONTRACT
"Product mode (06.1)", Read views).

Every test runs in the HTTP client's own workspace on the synthetic Day-3 template. States beyond Take ownership are
written straight into the test database (owner set, work orders and discrepancies in the contract's Execution shape),
as 06.1-10 and 06.1-12 did: a documented test shortcut, so these reads are tested beside the plans that record the
work. `worker.run` is patched to fail in every test: no read calls the engine worker (OPS-06).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import cases, config, db, inputs, refdata, workflow, worker
from api.app import app
from engine.model import TRANSFER
from tests.api.conftest import fresh, seat, sign_in
from tests.api.verify_helpers import ack, shown  # approve and accept acknowledge (D-26) and name the plan (07 review C)
from tests.api.test_stock_views import captured, unbound

STORY = cases.STORY_KEY
TODAY_KEYS = {"scenario_date", "day", "branch", "scope", "summary", "lanes", "cards", "movements", "outcomes",
              "coming_up", "exceptions"}
CARD_KEYS = {"key", "kind", "what", "where", "owner", "deadline", "date", "status", "next_action", "case_key",
             "case_id", "tiles"}
COUNTS = ("decisions_due", "approvals_waiting", "stock_checks", "transfers_at_risk")


# --- helpers ---------------------------------------------------------------------------------------------------------

def session(account: str | None = None) -> tuple[TestClient, str]:
    """A new signed-in client with its own workspace (as `account`, default the start store): (client, run_id)."""
    c = TestClient(app)
    sign_in(c)
    if account is not None:
        sign_in(c, account)  # rotates the session, keeps the workspace
    return c, c.get("/api/session").json()["run_id"]


def scratch(run_id: str, sql: str, **params) -> None:
    """A test-only write in the test's own workspace."""
    with db.tx() as conn:
        db.run(conn, sql, r=run_id, **params)


def rows(run_id: str, sql: str, **params) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, sql, r=run_id, **params)


def today(c: TestClient, **params) -> dict:
    r = c.get("/api/today", params=params)
    assert r.status_code == 200, r.json()
    return r.json()


def story_card(c: TestClient) -> dict:
    r = c.get(f"/api/cases/{STORY}")
    assert r.status_code == 200, r.json()
    return r.json()


def absolute(t: dict) -> int:
    return t["day"] * 1440 + t["min"]


def quiet_store(run_id: str, but: str) -> str:
    """A store with nothing to do in this workspace (test shortcut): the last store other than `but`, with every stock
    decision from or to it rejected and every stock check addressed to it confirmed, straight in the test database."""
    store = next(b for b in reversed(refdata.get(fresh(run_id)["dataset_version"]).branches) if b != but)
    scratch(run_id, "UPDATE cases SET state = 'REJECTED' WHERE run_id = :r AND (source_branch = :b OR plan_dest = :b)",
            b=store)
    scratch(run_id, "UPDATE evidence_requests SET state = 'CONFIRMED' WHERE run_id = :r AND to_branch = :b", b=store)
    return store


def call(run_id: str, fn, *args):
    """One service call in its own transaction after the run lock, as a route makes it."""
    return db.in_tx(lambda conn: fn(conn, db.lock_run(conn, run_id), *args))


def prepare_story(run_id: str) -> dict:
    """SUS-19 owned by its source (set in the database), sent, accepted, approved and prepared as the routes make
    them: its TRANSFER work order row."""
    case = rows(run_id, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", c=STORY)[0]
    with db.tx() as conn:
        plan = cases.parse_plan(cases.active_rec(conn, case)["result"]["recommended"])
    assert plan.kind == TRANSFER, "SUS-19's pick is a transfer"
    src, dest = seat(case["source_branch"]), seat(plan.dest)
    scratch(run_id, "UPDATE cases SET owner_taken_by = :o, owner_taken_at = now() WHERE run_id = :r AND case_id = :c",
            o=case["source_branch"], c=STORY)
    v = case["active_rec_version"]
    call(run_id, workflow.send, src, STORY, v)
    call(run_id, workflow.decide, dest, STORY, v, "accept", None, ack(run_id, STORY), shown(run_id, STORY))
    assert call(run_id, workflow.decide, src, STORY, v, "approve", None, ack(run_id, STORY),
                shown(run_id, STORY))["state"] == "APPROVED"
    assert call(run_id, workflow.prepare, src, STORY) == {"state": "PREPARED"}
    return rows(run_id, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' AND case_id = :c "
                        "AND kind = 'TRANSFER'", c=STORY)[0]


def dispatched_and_short(run_id: str, action: dict, short: float) -> None:
    """The source's recorded dispatch and the destination's receipt `short` units short, in the contract's Execution
    shape (test shortcut): DISPATCH and RECEIPT movements, an OPEN SHORT discrepancy, the work order RECEIVED and the
    case EXECUTED."""
    sent = float(action["qty"])
    for kind, qty, lot, by in (("DISPATCH", sent, action["lot_id"], action["origin"]),
                               ("RECEIPT", sent - short, action["new_lot_id"], action["dest"])):
        scratch(run_id, "INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, lot_id, qty, "
                        "damaged_qty, condition, recorded_by, role, branch, scenario_day) VALUES (:r, :m, :c, :a, :k, "
                        ":l, :q, 0, 'as_expected', :b, 'manager', :b, (SELECT day FROM demo_runs WHERE run_id = :r))",
                m=f"{action['action_id']}:{kind}", c=STORY, a=action["action_id"], k=kind, l=lot, q=qty, b=by)
    scratch(run_id, "INSERT INTO discrepancies (run_id, discrepancy_id, case_id, action_id, kind, units, open_units, "
                    "state) VALUES (:r, :i, :c, :a, 'SHORT', :u, :u, 'OPEN')",
            i=f"{action['action_id']}:SHORT", c=STORY, a=action["action_id"], u=float(short))
    scratch(run_id, "UPDATE world_actions SET state = 'RECEIVED' WHERE run_id = :r AND world = 'shelfshift' "
                    "AND action_id = :a", a=action["action_id"])
    scratch(run_id, "UPDATE cases SET state = 'EXECUTED' WHERE run_id = :r AND case_id = :c", c=STORY)


def lane(view: dict, key: str) -> dict:
    found = [l for l in view["lanes"] if l["key"] == key]
    assert len(found) == 1, (key, [l["key"] for l in view["lanes"]])
    return found[0]


CLOCK_WORDS = ("now", "server_now", "current", "overdue", "missed", "countdown", "minutes_left")


def clockless(value, path: str = "") -> list[str]:
    """Every key under `value` that would carry a current time or a countdown (D-18): none may exist."""
    if isinstance(value, dict):
        return [f"{path}.{k}" for k in value if k in CLOCK_WORDS] + \
            [p for k, v in value.items() for p in clockless(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in clockless(v, f"{path}[{i}]")]
    return []


@pytest.fixture
def no_worker(monkeypatch):
    """Today and the schedule never call the engine worker (OPS-06)."""
    monkeypatch.setattr(worker, "run", lambda *a, **k: pytest.fail("a read called the engine worker"))


# --- Task 1: Today's summary and action cards ------------------------------------------------------------------------

def test_the_story_source_manager_sees_the_count_and_the_card_for_its_decision(template, no_worker):
    c, run_id = session()
    story = story_card(c)
    me = c.get("/api/session").json()
    assert me["branch"] == story["source"] and story["owner"] is None  # the start seat is SUS-19's source (05.2)
    view = today(c)
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    assert TODAY_KEYS <= set(view)
    assert view["scenario_date"] == ref.date(run["day"]).isoformat() and view["day"] == run["day"]
    assert view["branch"] == me["branch"]
    summary = view["summary"]
    assert set(COUNTS) <= set(summary) and summary["decisions_due"] >= 1
    assert story["due"]["day"] == run["day"]  # SUS-19 is due on the scenario date
    first = summary["first_deadline"]
    assert (first["day"], first["min"]) == (story["due"]["day"], story["due"]["min"])
    card = view["cards"][0]
    assert CARD_KEYS <= set(card)
    assert card["case_key"] == STORY and card["kind"] == "stock_decision"
    assert card["what"]["sku"] == story["sku"] and card["what"]["lot_id"] == story["lot_id"]
    assert card["where"] == {"source": story["source"], "dest": story["dest"]}
    assert card["owner"] is None  # nobody owns it yet: the card says so, never a guessed owner
    assert (card["deadline"]["day"], card["deadline"]["min"]) == (story["due"]["day"], story["due"]["min"])
    assert card["deadline"]["cause"]["source"] == story["decide_by"]["source"]
    assert card["status"]["state"] == "OPEN" and card["status"]["step"] == "TRIGGER"
    assert card["next_action"]["key"] == "take_ownership"
    assert card["next_action"]["who"] == [{"role": "manager", "branch": me["branch"]}]
    assert "decisions_due" in card["tiles"]


def test_cards_are_ordered_by_planned_deadline_then_unscheduled_by_date(template, no_worker):
    c, _ = session()
    cards = today(c)["cards"]
    assert cards, "the start store has work on the scenario date"
    timed = [card for card in cards if card["deadline"] is not None]
    untimed = [card for card in cards if card["deadline"] is None]
    assert cards == timed + untimed  # every planned deadline first
    assert [absolute(card["deadline"]) for card in timed] == sorted(absolute(card["deadline"]) for card in timed)
    assert [card["date"] for card in untimed] == sorted(card["date"] for card in untimed)
    assert len({card["key"] for card in cards}) == len(cards)


def test_a_store_with_nothing_to_do_has_four_zero_counts_and_no_cards(template, no_worker):
    c, run_id = session()
    quiet = quiet_store(run_id, c.get("/api/session").json()["branch"])
    assert today(c, branch=quiet)["branch"] != quiet  # a manager's branch parameter is ignored
    sign_in(c, quiet)  # the same workspace, as the quiet store's manager
    view = today(c)
    assert view["branch"] == quiet
    assert {k: view["summary"][k] for k in COUNTS} == dict.fromkeys(COUNTS, 0)
    assert view["summary"]["first_deadline"] is None and view["cards"] == []


def test_a_manager_reads_only_their_own_store(template, no_worker):
    c, run_id = session()
    me = c.get("/api/session").json()["branch"]
    other = next(b for b in refdata.get(fresh(run_id)["dataset_version"]).branches if b != me)
    assert today(c, branch=other)["branch"] == me  # another store's day is never shown to a manager (T-06.1-44)


# --- Task 2: lanes, movements, outcomes, coming steps and exceptions; HQ's network view; nav counts -------------------

def test_the_source_stores_lanes_hold_planned_markers_only(template, no_worker):
    c, run_id = session()
    story = story_card(c)
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    me = story["source"]
    with db.tx() as conn:
        packing = inputs.sandbox_policy(conn, run, ref).packing_min
    trip = next(r for r in ref.runs_from(me, run["day"])
                if r.run_code == cases.parse_plan(story["suggested_key"]).run_code and r.dest == story["dest"])
    view = today(c)
    assert [l["key"] for l in view["lanes"]][:3] == ["decisions", "stock_checks", "floor"]
    deadline = next(m for m in lane(view, "decisions")["items"] if m["case_key"] == STORY)
    assert deadline == {"kind": "decision_deadline", "case_key": STORY, "case_id": STORY,
                        "day": story["due"]["day"], "min": story["due"]["min"]}
    cutoff = trip.day * 1440 + trip.loading_cutoff_min
    bar = next(b for b in lane(view, "floor")["items"] if b["kind"] == "pick_and_pack" and b["case_key"] == STORY)
    assert absolute(bar["start"]) == cutoff - packing and absolute(bar["end"]) == cutoff
    assert bar["conditional"] is True and bar["action_id"] is None  # not yet sent: the work happens only if it is
    runs = ref.runs_from(me, run["day"])
    assert [l["key"] for l in view["lanes"] if l["key"].startswith("run:")] == [f"run:{r.run_code}" for r in runs]
    mine = lane(view, f"run:{trip.run_code}")
    assert mine["dest"] == trip.dest and mine["cold_chain"] == trip.cold_chain
    marks = {m["kind"]: m for m in mine["items"]}
    assert (marks["loading_cutoff"]["day"], marks["loading_cutoff"]["min"]) == (trip.day, trip.loading_cutoff_min)
    assert absolute(marks["travel"]["start"]) == trip.day * 1440 + trip.departs_min
    assert absolute(marks["travel"]["end"]) == trip.day * 1440 + trip.arrives_min
    assert clockless(view) == []  # no element carries the current time (D-18)
    action = prepare_story(run_id)
    bar = next(b for b in lane(today(c), "floor")["items"] if b["kind"] == "pick_and_pack" and b["case_key"] == STORY)
    assert bar["conditional"] is False and bar["action_id"] == action["action_id"]  # prepared: a solid bar
    assert absolute(bar["end"]) == cutoff
    out = today(c)["movements"]["outgoing"]
    row = next(r for r in out if r["run_code"] == trip.run_code)
    moving = next(t for t in row["transfers"] if t["case_key"] == STORY)
    assert moving["decision"] == {"state": "PREPARED", "step": "EXECUTE"} and moving["movement"] == {"state": "planned"}
    assert {r["run_code"] for r in out} == {r.run_code for r in runs}
    empty = [r for r in out if r["run_code"] != trip.run_code]
    assert all(r["transfers"] == [] for r in empty)  # a run with no transfer: nothing invented


def test_expected_stays_apart_from_actual_and_a_discrepancy_is_an_exception(template, no_worker):
    c, run_id = session()
    action = prepare_story(run_id)
    story = story_card(c)
    dispatched_and_short(run_id, action, short=3)
    view = today(c)
    gap = next(x for x in view["exceptions"] if x["kind"] == "discrepancy" and x["case_key"] == STORY)
    assert gap["id"] == f"{action['action_id']}:SHORT" and gap["open_units"] == 3.0
    assert gap["owner"]["account"] == story["source"]
    assert gap["next_action"]["key"] == "reconcile"
    assert [o for o in view["outcomes"] if o["case_key"] == STORY] == []  # still in Execute until reconciled (D-65)
    incoming = today(c, branch=action["dest"])  # a manager's branch is ignored: still the source's own day
    assert incoming["branch"] == story["source"]
    scratch(run_id, "UPDATE discrepancies SET state = 'RECONCILED', open_units = 0 WHERE run_id = :r AND case_id = :c",
            c=STORY)
    view = today(c)
    assert not [x for x in view["exceptions"] if x["kind"] == "discrepancy"]
    outcome = next(o for o in view["outcomes"] if o["case_key"] == STORY)
    assert outcome["expected"] == {"net_vs_keep": story["est_benefit"], "kind": "simulated"}
    assert outcome["actual"] is None  # never the expected figure in its place (PROD-12)
    assert story_card(c)["step"] == "VERIFY"


def test_hq_gets_network_totals_and_a_paged_table_of_stores_needing_attention(template, no_worker):
    c, run_id = session()
    sign_in(c, "HQ")
    view = today(c)
    assert {"scenario_date", "network", "branches", "cards"} <= set(view) and view["branch"] is None
    table = view["branches"]
    assert set(table) == {"items", "page", "page_size", "total"} and table["total"] == len(table["items"])
    order = [(-i["decisions_due"], i["branch"]) for i in table["items"]]
    assert order == sorted(order) and table["items"], "the network has stores needing attention"
    for k in COUNTS:  # the network's counts are the stores' counts summed
        assert view["network"][k] == sum(i[k] for i in table["items"]), k
    for item in table["items"]:  # each row is the store's own Today summary
        summary = today(c, branch=item["branch"])["summary"]
        assert {k: summary[k] for k in COUNTS} == {k: item[k] for k in COUNTS}, item["branch"]
    board = c.get("/api/cases").json()
    assert view["network"]["evaluated"] == board.get("evaluated", board["total"])
    assert view["network"]["at_risk"] == board.get("at_risk", board["total"])
    assert all(any(w["role"] == "hq" for w in card["next_action"]["who"]) for card in view["cards"])
    assert clockless(view) == []


def test_nav_counts_equal_the_counts_the_pages_show(template, no_worker):
    c, run_id = session()
    prepare_story(run_id)
    for account in (None, "HQ"):
        if account:
            sign_in(c, account)
        nav = c.get("/api/session").json()["nav_counts"]
        board = c.get("/api/cases").json()
        page = today(c)
        assert nav["today"] == len(page["cards"])
        assert nav["board"] == sum(board["stage_counts"][k] for k in ("decide", "authorise", "do", "check"))
        if account == "HQ":
            assert nav["tasks"] == 0  # tasks go to store managers
        else:
            planned = c.get("/api/transfers", params={"direction": "outgoing", "state": "planned"}).json()["total"]
            arriving = c.get("/api/transfers", params={"direction": "incoming", "state": "dispatched"}).json()["total"]
            assert planned >= 1
            assert nav["tasks"] == page["summary"]["stock_checks"] + planned + arriving


def test_taking_ownership_is_a_decision_in_the_activity(template, no_worker):
    c, _ = session()
    r = c.post(f"/api/cases/{STORY}/take-ownership", json={}, headers={"Idempotency-Key": "planning-own"})
    assert r.status_code == 200, r.json()
    entries = [e for e in c.get(f"/api/cases/{STORY}/activity").json() if e["type"] == "ownership_taken"]
    assert entries and all(e["group"] == "Decisions" for e in entries)  # a decision, never data (06.1-08 note)


# --- Task 3: the planned schedule -------------------------------------------------------------------------------------

ROW_KEYS = {"id", "parent_id", "kind", "title", "sub", "branch", "start", "end", "status", "owner", "qty", "run_code",
            "case_key", "depends_on"}


def schedule(c: TestClient, **params) -> dict:
    r = c.get("/api/schedule", params=params)
    assert r.status_code == 200, r.json()
    body = r.json()
    for row in body["rows"]:
        assert ROW_KEYS <= set(row), row
    return body


def open_check(run_id: str, to_branch: str) -> str:
    """An OPEN stock check on SUS-19's active version addressed to `to_branch`, in the shape signals.py writes one
    (test shortcut): its request id."""
    case = rows(run_id, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", c=STORY)[0]
    rid = f"REQ-PLAN-{to_branch}"
    scratch(run_id, "INSERT INTO evidence_requests (run_id, request_id, case_id, rec_version, fact_id, fact_kind, sku, "
                    "lot_id, to_branch, ask_text, due_day, due_min, state, created_by, created_day) VALUES (:r, :q, "
                    ":c, :v, 'F-PLAN', 'STALE_COUNT', :s, :l, :b, 'Count the lot', :d, 540, 'OPEN', 'manager', :d)",
            q=rid, c=STORY, v=case["active_rec_version"], s=case["sku"], l=case["lot_id"], b=to_branch,
            d=fresh(run_id)["day"])
    return rid


def walk_parents(c: TestClient, **params) -> list[str]:
    """Every page of the schedule with one parent row per page: the parent ids in the server's order."""
    seen, n = [], 1
    while True:
        body = schedule(c, page=n, page_size=1, **params)
        parents = [r["id"] for r in body["rows"] if r["parent_id"] is None]
        assert len(parents) <= 1 and body["page"] == n
        if not parents:
            assert body["total"] == len(seen)
            return seen
        seen += parents
        n += 1


def test_the_story_work_order_holds_its_planned_activities_only(template, no_worker):
    c, run_id = session()
    story = story_card(c)
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    open_check(run_id, story["source"])
    with db.tx() as conn:
        packing = inputs.sandbox_policy(conn, run, ref).packing_min
    trip = next(r for r in ref.runs_from(story["source"], run["day"])
                if r.run_code == cases.parse_plan(story["suggested_key"]).run_code and r.dest == story["dest"])
    body = schedule(c, date=ref.date(run["day"]).isoformat())
    assert (body["view"], body["group"], body["date"]) == ("day", "work_order", ref.date(run["day"]).isoformat())
    parent = next(r for r in body["rows"] if r["id"] == f"wo:{STORY}")
    assert parent["kind"] == "work_order" and parent["parent_id"] is None and parent["case_key"] == STORY
    kids = {r["kind"]: r for r in body["rows"] if r["parent_id"] == parent["id"]}
    assert set(kids) >= {"decision_deadline", "stock_check", "pick_and_pack", "loading_cutoff", "transport", "receive"}
    by = story["decide_by"]
    assert kids["decision_deadline"]["start"] == kids["decision_deadline"]["end"] == \
        {"day": by["day"], "min": by["latest_decision_min"]}
    cutoff = trip.day * 1440 + trip.loading_cutoff_min
    assert absolute(kids["pick_and_pack"]["start"]) == cutoff - packing
    assert absolute(kids["pick_and_pack"]["end"]) == cutoff
    assert absolute(kids["loading_cutoff"]["start"]) == absolute(kids["loading_cutoff"]["end"]) == cutoff
    assert absolute(kids["transport"]["start"]) == trip.day * 1440 + trip.departs_min
    assert absolute(kids["transport"]["end"]) == trip.day * 1440 + trip.arrives_min
    for k in ("receive", "stock_check"):  # nothing plans a time for them: Unscheduled, never invented
        assert kids[k]["start"] is None and kids[k]["end"] is None, k
    assert kids["stock_check"]["status"] == "open" and kids["decision_deadline"]["status"] == "open"
    assert kids["pick_and_pack"]["status"] == "not_started"  # no work order yet: recorded states only
    assert kids["loading_cutoff"]["depends_on"] == [kids["pick_and_pack"]["id"]]
    assert kids["transport"]["depends_on"] == [kids["loading_cutoff"]["id"]]
    assert kids["receive"]["depends_on"] == [kids["transport"]["id"]]
    dated = [k for k in kids.values() if k["start"] is not None]  # a day-only markdown counts as its whole day

    def at(t, end=False):
        return t["day"] * 1440 + (t["min"] if t["min"] is not None else (1439 if end else 0))
    assert parent["start"] == min((k["start"] for k in dated), key=at)
    assert parent["end"] == max((k["end"] for k in dated), key=lambda t: at(t, True))
    assert clockless(body) == []
    action = prepare_story(run_id)
    kids = {r["kind"]: r for r in schedule(c)["rows"] if r["parent_id"] == f"wo:{STORY}"}
    assert kids["pick_and_pack"]["status"] == "scheduled" and kids["decision_deadline"]["status"] == "done"
    assert kids["transport"]["qty"] == float(action["qty"])


def test_grouped_by_run_every_run_shows_and_by_branch_rows_nest_by_store(template, no_worker):
    c, run_id = session()
    story = story_card(c)
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    body = schedule(c, group="run")
    parents = [r for r in body["rows"] if r["parent_id"] is None]
    runs = ref.runs_from(story["source"], run["day"])
    outgoing = [f"run:{r.run_code}:{r.day}" for r in runs]
    ids = [p["id"] for p in parents]
    assert set(outgoing) <= set(ids)  # every run of the store on that date
    keys = [(p["start"]["day"], absolute(p["start"]), p["run_code"]) for p in parents]
    assert keys == sorted(keys)  # day, loading cutoff, run code
    mine = cases.parse_plan(story["suggested_key"]).run_code
    for p in parents:
        kids = [r for r in body["rows"] if r["parent_id"] == p["id"]]
        assert p["kind"] == "run"
        if p["id"] not in outgoing:  # another store's run brings a transfer here
            assert kids and p["sub"].endswith(f"→ {story['source']}"), p
        elif p["run_code"] == mine:
            assert STORY in {k["case_key"] for k in kids} and "transport" in {k["kind"] for k in kids}
        elif not kids:
            assert p["status"] is None  # a run with no transfer: a row with no children, no work invented
    assert any(not [r for r in body["rows"] if r["parent_id"] == p["id"]] for p in parents), \
        "the store has a run with no transfer on the scenario date"
    body = schedule(c, group="branch")
    tops = [r for r in body["rows"] if r["parent_id"] is None]
    assert all(t["kind"] == "branch" for t in tops) and story["source"] in {t["branch"] for t in tops}
    orders = [r for r in body["rows"] if r["kind"] == "work_order"]
    assert orders and all(o["parent_id"] == f"branch:{o['branch']}" for o in orders)
    leaf = next(r for r in body["rows"] if r["parent_id"] == f"wo:{STORY}")
    assert leaf["kind"] in ("decision_deadline", "stock_check", "pick_and_pack", "loading_cutoff", "transport",
                            "receive", "markdown")


def test_windows_filters_pages_and_refusals(template, no_worker):
    c, run_id = session()
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    day = ref.date(run["day"])
    one, week, month = (schedule(c, view=v, date=day.isoformat()) for v in ("day", "week", "month"))
    monday = day.toordinal() - day.weekday()
    assert week["window"]["from"] == type(day).fromordinal(monday).isoformat()
    assert week["window"]["to"] == type(day).fromordinal(monday + 6).isoformat()
    assert month["window"]["from"] == day.replace(day=1).isoformat()
    tops = {v: {r["id"] for r in b["rows"] if r["parent_id"] is None} for v, b in
            (("day", one), ("week", week), ("month", month))}
    assert tops["day"] <= tops["week"] <= tops["month"] and len(tops["month"]) > len(tops["day"])
    marks = [r for r in month["rows"] if r["kind"] == "markdown"]
    assert marks and all(m["start"]["day"] is not None and m["start"]["min"] is None for m in marks)
    picks = schedule(c, view="week", activity="pick_and_pack")
    assert {r["kind"] for r in picks["rows"] if r["parent_id"] is not None} == {"pick_and_pack"}
    opened = schedule(c, view="week", status="open")
    assert all(r["status"] == "open" for r in opened["rows"] if r["parent_id"] is not None)
    for params in ({"view": "month"}, {"view": "week", "group": "run"}, {"view": "week", "group": "branch"}):
        walked = walk_parents(c, **params)
        assert walked == [r["id"] for r in schedule(c, **params)["rows"] if r["parent_id"] is None]
        assert len(walked) == len(set(walked))  # each parent once
    me = c.get("/api/session").json()["branch"]
    other = next(b for b in ref.branches if b != me)
    assert schedule(c, branch=other)["branch"] == me  # a manager reads only their own store (T-06.1-44)
    for bad in ({"group": "transfer"}, {"view": "year"}, {"status": "late"}, {"activity": "drive"},
                {"date": "17 Oct"}, {"date": "2026-02-30"}, {"page": "0"}):
        r = c.get("/api/schedule", params=bad)
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), bad


# --- the public set (last: `public_template` stays ACTIVE for the rest of this module) -------------------------------

@pytest.fixture(scope="module")
def public_flags(public_template):
    """The module's public template with more of its lots flagged this morning, so `at_risk` exceeds `evaluated`: its
    screen run again with a test copy of the case-opening thresholds whose `min_waste_units` is 0 (a test setting only,
    as test_stock_views.py does; the reference thresholds are not changed). Its run_id."""
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CASE_OPENING", {**config.CASE_OPENING, "min_waste_units": 0})
    try:
        with db.tx() as conn:
            cases.open_cases(conn, fresh(public_template))
    finally:
        mp.undo()
    return public_template


def test_hq_network_evaluated_and_at_risk_follow_the_boards_rule_on_the_public_set(public_flags, no_worker):
    c, run_id = session("HQ")
    run = fresh(run_id)
    ref = refdata.get(run["dataset_version"])
    assert ref.kind == "public"
    view = today(c)
    board = c.get("/api/cases").json()
    assert (view["network"]["evaluated"], view["network"]["at_risk"]) == (board["evaluated"], board["at_risk"])
    assert board["at_risk"] > board["evaluated"] > 0  # flags this workspace has not opened count as at risk (D-38)
    assert view["scenario_date"] == c.get("/api/session").json()["data_as_of"]
    table = view["branches"]
    assert table["page_size"] == int(ref.settings["board"]["page_size"])
    pages, n = [], 1
    while True:  # one page at a time: every store once, in the server's order
        body = today(c, page=n, page_size=1)["branches"]
        if not body["items"]:
            break
        pages += body["items"]
        n += 1
    assert [i["branch"] for i in pages] == [i["branch"] for i in table["items"]]
    assert c.get("/api/session").json()["nav_counts"]["tasks"] == 0  # tasks go to store managers
    manager, _ = session()
    day = today(manager)
    assert day["branch"] == manager.get("/api/session").json()["branch"] and day["scope"]["default"] is True


def test_today_and_the_schedule_never_read_a_large_public_table_without_a_store(public_flags, no_worker):
    hq, _ = session("HQ")
    manager, _ = session()
    store = manager.get("/api/session").json()["branch"]

    def calls():
        for c, params in ((manager, {}), (hq, {}), (hq, {"branch": store})):
            today(c, **params)
        for c, params in ((manager, {"view": "month"}), (hq, {"view": "week"}),
                          (hq, {"view": "week", "group": "run", "branch": store}),
                          (hq, {"view": "month", "group": "branch", "branch": store})):
            schedule(c, **params)
        for c in (manager, hq):
            assert c.get("/api/session").status_code == 200
    tables, whole = unbound(captured(calls))
    assert "ref_inbound0" in tables, tables  # not vacuous: the store's planned deliveries are read, bound to it
    assert whole == [], whole
