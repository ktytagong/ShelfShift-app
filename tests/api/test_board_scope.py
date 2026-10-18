"""The board on a public set: the signed-in store by default, HQ filters by store and risk, every list paginated in
one stable server order (spec §5 Public dataset; 05.2-CONTEXT D-24, D-26, D-28, D-38; cases.board_page).

The module's tiny public template is screened on day 0 with a test copy of the case-opening thresholds whose
`min_waste_units` is 0, so more of its 15 lots are flagged and a page of 3 has something to page through (a test
setting only: the reference thresholds are not changed). It is ACTIVE for the module; the synthetic template is ACTIVE
again at teardown. A workspace opens two flags, so each board holds evaluated cases and unevaluated flags.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import cases, config, db
from api.app import app
from tests.api.conftest import as_seat, fresh, key, seat, sign_in

RISK = ("a", "b", "keep_waste", "stock_at_step", "step_day")


@pytest.fixture(scope="module")
def board_set(template, public_run):
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CASE_OPENING", {**config.CASE_OPENING, "min_waste_units": 0})
    try:
        with db.tx() as conn:
            cases.open_cases(conn, fresh(public_run["run_id"]))
    finally:
        mp.undo()
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :t", t=template)
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :p", p=public_run["run_id"])
    try:
        with db.tx() as conn:
            flags = db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r ORDER BY key", r=public_run["run_id"])
        stores = sorted({f["branch"] for f in flags})
        assert len(flags) >= 7 and len(stores) >= 2, [(f["key"], f["branch"]) for f in flags]
        # two flags opened in each workspace: one at each of the first two stores that have one
        opened = [next(f["key"] for f in flags if f["branch"] == s) for s in stores[:2]]
        yield {"run": fresh(public_run["run_id"]), "flags": flags, "stores": stores, "opened": opened}
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'INITIALIZING' WHERE run_id = :p", p=public_run["run_id"])
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=template)


def workspace_client(board_set, seat: str | None = None) -> TestClient:
    """A new session whose workspace has opened the module's two flags, in the given seat (HQ when None)."""
    c = TestClient(app)
    sign_in(c)
    for k in board_set["opened"]:
        r = c.post(f"/api/cases/{k}/evaluate", json={}, headers=key())
        assert r.status_code == 200, r.json()
    as_seat(c, seat)
    return c


def walk(c: TestClient, query: str = "", size: int = 3) -> tuple[list[dict], list[dict]]:
    """Every page of the board in order: (the items, each page's response)."""
    items, pages, n = [], [], 1
    while True:
        r = c.get(f"/api/cases?page={n}&page_size={size}{query}")
        assert r.status_code == 200, r.json()
        body = r.json()
        pages.append(body)
        if not body["cases"]:
            return items, pages
        seen = {x["key"] for x in items}
        assert not seen & {x["key"] for x in body["cases"]}, f"page {n} repeats an item"
        items += body["cases"]
        n += 1


def expected(c: TestClient, board_set, store: str | None, risk: str | None = None) -> list[str]:
    """The keys the board should list, in order: the workspace's cases in scope in the board's order, then the flags
    in scope it has not opened by Keep waste, then stock at the step day, descending, then key."""
    ws = fresh(c.get("/api/session").json()["run_id"])
    with db.tx() as conn:
        cards = cases.board(conn, ws)
        opened_lots = {r["lot_id"] for r in db.rows(conn, "SELECT lot_id FROM cases WHERE run_id = :r", r=ws["run_id"])}
        stock = {r["case_id"] for r in db.rows(conn, "SELECT case_id FROM activity_log WHERE run_id = :r AND "
                                                     "type = 'opened' AND payload->>'b' = 'true'", r=ws["run_id"])}
    keep = [x for x in cards if store is None or store in (x["source"], x["dest"])]
    if risk == "waste":
        keep = [x for x in keep if (x["at_risk_units"] or 0) > 0]
    if risk == "stock":
        keep = [x for x in keep if x["case_id"] in stock]
    flags = [f for f in board_set["flags"] if f["lot_id"] not in opened_lots and store in (None, f["branch"])
             and (risk is None or f["a" if risk == "waste" else "b"])]
    flags.sort(key=lambda f: (f["keep_waste"] is None, -(f["keep_waste"] or 0), f["stock_at_step"] is None,
                              -(f["stock_at_step"] or 0), f["key"]))
    return [x["key"] for x in keep] + [f["key"] for f in flags]


def test_a_manager_sees_their_store_and_a_branch_parameter_changes_nothing(board_set):
    mine, other = board_set["stores"][0], board_set["stores"][1]
    c = workspace_client(board_set, mine)
    body = c.get("/api/cases").json()
    assert body["scope"] == {"branch": mine, "default": True}
    keys = [x["key"] for x in body["cases"]]
    assert keys == expected(c, board_set, mine)[:body["page_size"]]
    for x in body["cases"]:
        assert mine in (x["source"], x["dest"]), x["key"]
    assert c.get(f"/api/cases?branch={other}").json() == body  # a manager's branch parameter is ignored


def test_hq_sees_every_store_and_filters_by_store_and_risk(board_set):
    c = workspace_client(board_set, None)
    everything, pages = walk(c)
    assert pages[0]["scope"] == {"branch": None, "default": True}
    assert [x["key"] for x in everything] == expected(c, board_set, None)
    store = board_set["stores"][1]
    items, pages = walk(c, f"&branch={store}")
    assert pages[0]["scope"] == {"branch": store, "default": False}
    assert [x["key"] for x in items] == expected(c, board_set, store)
    assert all(store in (x["source"], x["dest"]) for x in items)
    for risk in ("waste", "stock"):
        items, _ = walk(c, f"&risk={risk}")
        assert [x["key"] for x in items] == expected(c, board_set, None, risk), risk
        for x in items:
            if x.get("needs_evaluation"):
                assert x["risk"]["a" if risk == "waste" else "b"] is True
    for bad in ("risk=everything", "branch=S999", "page=0", "page=x", "page_size=0", "page_size=1000",
                "page=1&page=2", "branch="):
        r = c.get(f"/api/cases?{bad}")
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), bad


def test_pages_cover_every_item_once_in_one_order(board_set):
    a, b = workspace_client(board_set, None), workspace_client(board_set, None)
    first, pages = walk(a)
    second, _ = walk(b)
    keys = [x["key"] for x in first]
    assert len(keys) == len(set(keys)) == pages[0]["total"]  # each case and flag exactly once
    assert keys == [x["key"] for x in second]  # two workspaces, one order
    every_flag = {f["key"] for f in board_set["flags"]}
    assert set(keys) == every_flag  # every flag is reachable, as a flag or as the case it opened (D-28)
    n_cases = len(board_set["opened"])
    assert all("needs_evaluation" not in x for x in first[:n_cases])  # cases first
    assert all(x["needs_evaluation"] is True for x in first[n_cases:])  # then the flags
    assert [x["key"] for x in first] == expected(a, board_set, None)


def test_the_page_keys_counts_and_a_page_past_the_last(board_set):
    c = workspace_client(board_set, None)
    body = c.get("/api/cases?page_size=3").json()
    total = len(board_set["flags"])
    assert (body["page"], body["page_size"], body["total"]) == (1, 3, total)
    assert (body["evaluated"], body["at_risk"]) == (len(board_set["opened"]), total)  # D-38: evaluated vs total
    assert len(body["cases"]) == 3
    past = c.get("/api/cases?page=99&page_size=3").json()
    assert (past["cases"], past["page"], past["total"]) == ([], 99, total)
    default = c.get("/api/cases").json()
    assert default["page_size"] == 25 and len(default["cases"]) == min(25, total)  # the reference page size
    store = board_set["stores"][0]
    scoped = c.get(f"/api/cases?branch={store}&risk=waste").json()
    assert scoped["at_risk"] == scoped["total"] == len(expected(c, board_set, store, "waste"))


def test_an_unevaluated_flag_answers_its_flag_detail(board_set):
    c = workspace_client(board_set, None)
    flag = next(f for f in board_set["flags"] if f["key"] not in board_set["opened"])
    d = c.get(f"/api/cases/{flag['key']}").json()
    assert d["needs_evaluation"] is True and d["risk"] == {k: flag[k] for k in RISK}
    assert d["lot"]["lot_id"] == flag["lot_id"] and d["result"] is None and d["fact_checks"] == []


def test_the_synthetic_board_is_every_case_on_one_page(board_set, template):
    """A workspace of the synthetic template (cloned from it directly: the public one is ACTIVE in this module)."""
    from api import sandbox
    with db.tx() as conn:
        rid = sandbox.clone(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :t", t=template))
    ws = fresh(rid)
    with db.tx() as conn:
        every = cases.board(conn, ws)
        plain = cases.board_page(conn, ws, seat(None))
        other = cases.board_page(conn, ws, seat(None), branch="S999", risk="waste", page=2, page_size=1)
        manager = cases.board_page(conn, ws, seat(every[0]["source"]))
    n = len(every)
    assert n > 0 and plain["cases"] == every and other == plain == manager
    assert (plain["page"], plain["page_size"], plain["total"]) == (1, n, n)
    assert plain["scope"] == {"branch": None, "default": True} and "evaluated" not in plain
    assert all("needs_evaluation" not in x for x in plain["cases"])
