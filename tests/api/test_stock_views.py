"""Stock & lots, Analytics and Forecasts: read-only views bound to one store (06.1-CONTEXT D-15, D-17, D-31, D-32,
D-51, D-52, D-56; 04-API-CONTRACT "Product mode (06.1)", Read views; api/stock.py).

The first tests run on the synthetic Day-3 template (the milk story, SUS-19); the public tests after them make the
tiny public template ACTIVE for the rest of this module (`public_template`), so they come last in the file. Every
expected figure below is read again from the workspace's own rows or the reference tables, never pasted from a run.
Holds, review flags, work orders and movements a test needs are written straight into the test database (06.1-04's
tables; a documented test shortcut: the plans that write them run beside this one).
"""
from __future__ import annotations

import re
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from api import cases, config, db, overlay, refdata
from api.app import app
from api.screen import lot_keep_waste
from tests.api.conftest import by_tag, first_store, fresh, key, sign_in, world_rows
from tests.api.test_publicref import BOUND, LARGE

ROW_KEYS = {"lot_id", "branch", "sku", "product", "zone", "origin", "expiry_day", "expiry_date", "expiry_source", "qty",
            "markdown_pct", "last_verified_day", "last_verified_date", "reserved", "available", "incoming",
            "forecast_to_expiry", "next_markdown", "discrepancies", "case_key", "case_state", "flag_key", "action",
            "eligibility", "kinds",
            "lot_hold", "fact_label"}  # 07-22: the row's hold and confirmed-fact label (contract row 2026-10-18)
DRAWER_KEYS = ROW_KEYS | {"holds", "review_flags", "transfers", "history"}
LOT_KINDS = {"stock": "simulated", "forecast": "forecast"}
PRODUCTS: set[str] = set()  # the public set's product codes, filled by the capture test
BUCKETS = ["0–1", "2–3", "4–7", "8+"]  # the contract's aging buckets, days to expiry
NOT_PUBLIC = {"available": False, "reason": "Analytics covers the public sales dataset."}


def signed_in(account: str) -> TestClient:
    """A new browser signed in as `account` (a store code, or "HQ"), with its own workspace."""
    c = TestClient(app)
    sign_in(c, account)
    return c


def workspace(c: TestClient) -> dict:
    return fresh(c.get("/api/session").json()["run_id"])


def scratch(run_id: str, sql: str, **params) -> None:
    with db.tx() as conn:
        db.run(conn, sql, r=run_id, **params)


def test_the_story_manager_lists_the_story_lot_with_its_kinds_stated(template):
    story = by_tag(template, cases.STORY_TAG)
    source = story["source_branch"]
    c = signed_in(source)
    ws = workspace(c)
    r = c.get("/api/lots")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["total"] == len(body["items"]) and body["items"]  # the synthetic set answers one page, as its board
    assert {x["branch"] for x in body["items"]} == {source}  # the signed-in store only
    row = next(x for x in body["items"] if x["lot_id"] == story["lot_id"])
    assert set(row) == ROW_KEYS
    lot = next(l for l in world_rows(ws, "lots", "world = 'shelfshift' AND lot_id = :l", l=story["lot_id"]))
    ref = refdata.get(ws["dataset_version"])
    assert (row["qty"], row["expiry_day"], row["expiry_source"], row["last_verified_day"]) == \
        (lot["qty"], lot["expiry_day"], lot["expiry_source"], lot["last_verified_day"])
    assert row["expiry_date"] == ref.date(lot["expiry_day"]).isoformat()
    with db.tx() as conn:
        held = db.one(conn, "SELECT coalesce(sum(qty), 0) AS q FROM reservations WHERE run_id = :r AND kind = "
                            "'SOURCE_LOT' AND bucket = :l AND (state = 'COMMITTED' OR (state = 'HELD' AND "
                            "expires_at > now()))", r=ws["run_id"], l=story["lot_id"])["q"]
    assert row["reserved"] == round(float(held), 2)
    assert row["available"] == round(max(0.0, lot["qty"] - float(held)), 2)
    points = ref.forecast(ws["day"], source, story["sku"])  # the base forecast from today through expiry, by hand
    assert row["forecast_to_expiry"] == round(sum(points[d][1] for d in range(ws["day"], lot["expiry_day"] + 1)), 2)
    assert row["case_key"] == cases.STORY_KEY and row["discrepancies"] == 0
    assert row["eligibility"]["applies"] is False  # the synthetic set has no stage 1
    assert row["kinds"] == LOT_KINDS  # simulated stock, precomputed forecast; never "observed" (D-15)
    order = [(x["expiry_day"], x["sku"], x["lot_id"]) for x in body["items"]]
    assert order == sorted(order)


def test_the_story_lot_drawer_shows_holds_flags_transfers_and_history(template):
    story = by_tag(template, cases.STORY_TAG)
    c = signed_in(story["source_branch"])
    ws = workspace(c)
    action = f"wo-{uuid.uuid4().hex[:6]}"
    scratch(ws["run_id"], "INSERT INTO world_actions (run_id, world, action_id, case_id, kind, lot_id, sku, qty, dest, "
                          "run_code, run_day, state, new_lot_id, origin) VALUES (:r, 'shelfshift', :a, :c, 'TRANSFER', "
                          ":l, :k, 12, :d, 'R-T', :day, 'PLANNED', :n, :o)", a=action, c=story["case_id"],
            l=story["lot_id"], k=story["sku"], d="ELSEWHERE", day=ws["day"], n=f"{story['lot_id']}-moved",
            o=story["source_branch"])
    scratch(ws["run_id"], "INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, lot_id, qty, "
                          "recorded_by, role, branch, scenario_day) VALUES (:r, 'm-1', :c, :a, 'DISPATCH', :l, 12, :b, "
                          "'manager', :b, :day)", c=story["case_id"], a=action, l=story["lot_id"],
            b=story["source_branch"], day=ws["day"])
    r = c.get(f"/api/lots/{story['lot_id']}")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert set(body) == DRAWER_KEYS
    assert body["holds"] == [] and body["review_flags"] == []  # none recorded
    assert [t["action_id"] for t in body["transfers"]] == [action]
    t = body["transfers"][0]
    assert (t["key"], t["source"], t["qty"], t["state"]) == (cases.STORY_KEY, story["source_branch"], 12.0, "PLANNED")
    ref = refdata.get(ws["dataset_version"])
    moves = [h for h in body["history"] if h["type"] == "movement"]
    assert [(m["movement_id"], m["kind"], m["qty"]) for m in moves] == [("m-1", "DISPATCH", 12.0)]
    assert moves[0]["recorded_at"] and moves[0]["scenario_date"] == ref.date(ws["day"]).isoformat()
    assert moves[0]["actor"] == {"account": story["source_branch"], "role": "manager", "branch": story["source_branch"]}
    decisions = [h for h in body["history"] if h["type"] == "stock_decision"]
    assert [d["key"] for d in decisions] == [cases.STORY_KEY]
    assert decisions[0]["scenario_date"] == ref.date(story["opened_day"]).isoformat()
    stamps = [h["recorded_at"] for h in body["history"] if h["recorded_at"]]
    assert stamps == sorted(stamps)  # by recorded time


def test_another_stores_manager_cannot_open_the_story_lot(template):
    story = by_tag(template, cases.STORY_TAG)
    ref = refdata.get(fresh(template)["dataset_version"])
    other = next(b for b in ref.branches if b != story["source_branch"])
    c = signed_in(other)
    r = c.get(f"/api/lots/{story['lot_id']}")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    missing = c.get("/api/lots/no-such-lot")
    assert missing.status_code == 404 and missing.json()["message"].startswith("No lot ")  # the same answer
    assert r.json()["message"] == f"No lot {story['lot_id']}"


def test_analytics_and_forecasts_cover_the_public_dataset_only(template):
    """The synthetic set has no public sales: both pages say so instead of inventing figures (D-17), for any seat."""
    for account in (first_store(), "HQ"):
        c = signed_in(account)
        for path in ("/api/analytics", "/api/forecasts"):
            r = c.get(path)
            assert r.status_code == 200, r.json()
            assert r.json() == {"available": False, "reason": "Analytics covers the public sales dataset."}


# --- the tiny public template: ACTIVE from here to the end of the module (keep these tests last) ---------------------

@pytest.fixture(scope="module")
def public_flags(public_template):
    """The module's public template with more of its lots flagged this morning: its screen run again with a test copy
    of the case-opening thresholds whose `min_waste_units` is 0 (a test setting only, as test_board_scope.py does; the
    reference thresholds are not changed). The catalogue opened every flag of the open day, so this gives the lot list
    flags no case was opened for. Its run_id."""
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "CASE_OPENING", {**config.CASE_OPENING, "min_waste_units": 0})
    try:
        with db.tx() as conn:
            cases.open_cases(conn, fresh(public_template))
    finally:
        mp.undo()
    return public_template


def walk(c: TestClient, query: str = "", size: int = 2) -> list[dict]:
    """Every page of GET /api/lots in order; each page holds at most `size` items and no item twice."""
    items, n = [], 1
    while True:
        r = c.get(f"/api/lots?page={n}&page_size={size}{query}")
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["page"] == n and body["page_size"] == size and len(body["items"]) <= size
        if not body["items"]:
            assert body["total"] == len(items)
            return items
        assert not {x["lot_id"] for x in items} & {x["lot_id"] for x in body["items"]}, f"page {n} repeats a lot"
        items += body["items"]
        n += 1


def case_and_flag_lots(ws: dict) -> tuple[dict, dict]:
    """({lot: live case key}, {lot: flag key} for this morning's flags of lots no case was opened for), read here."""
    with db.tx() as conn:
        live = {r["lot_id"]: r["key"] for r in db.rows(
            conn, "SELECT lot_id, key FROM cases WHERE run_id = :r AND state NOT IN ('CLOSED', 'REJECTED')",
            r=ws["run_id"])}
        any_case = {r["lot_id"] for r in db.rows(conn, "SELECT lot_id FROM cases WHERE run_id = :r", r=ws["run_id"])}
        flagged = {r["lot_id"]: r["key"] for r in db.rows(
            conn, "SELECT lot_id, key FROM case_flags WHERE run_id = :f AND day = :d",
            f=overlay.flags_run(conn, ws), d=ws["day"]) if r["lot_id"] not in any_case}
    return live, flagged


def test_lot_filters_and_pages_walk_one_stable_order(public_flags):
    hq = signed_in("HQ")
    ws = workspace(hq)
    lots = world_rows(ws, "lots", "world = 'shelfshift' AND qty > 0")
    every = sorted(lots, key=lambda l: (l["expiry_day"], l["sku"], l["lot_id"]))
    assert [x["lot_id"] for x in walk(hq)] == [l["lot_id"] for l in every]  # HQ without a store: every store
    live, flagged = case_and_flag_lots(ws)
    assert set(live) and set(flagged), (live, flagged)  # the open day has decisions and unopened flags
    risky = [x["lot_id"] for x in walk(hq, "&at_risk=1")]
    assert risky == [l["lot_id"] for l in every if l["lot_id"] in live or l["lot_id"] in flagged]
    assert [x["lot_id"] for x in walk(hq, "&with_case=1", size=1)] == [l["lot_id"] for l in every
                                                                       if l["lot_id"] in live]
    sku = every[0]["sku"]
    assert [x["lot_id"] for x in walk(hq, f"&product={sku}")] == [l["lot_id"] for l in every if l["sku"] == sku]
    store = every[0]["branch"]
    assert [x["lot_id"] for x in walk(hq, f"&branch={store}", size=3)] == [l["lot_id"] for l in every
                                                                           if l["branch"] == store]
    manager = signed_in(store)  # a manager's own store; another store named in `branch` is ignored
    other = next(l["branch"] for l in every if l["branch"] != store)
    mine = [x["lot_id"] for x in walk(manager, f"&branch={other}")]
    assert mine == [l["lot_id"] for l in world_rows(workspace(manager), "lots", "world = 'shelfshift' AND qty > 0 "
                                                    "AND branch = :b", "expiry_day, sku, lot_id", b=store)]
    for bad in ("page_size=0", "page_size=26", "at_risk=2", "with_case=yes", "product=NOPE", "branch=NOPE", "page=0"):
        r = hq.get(f"/api/lots?{bad}")
        assert r.status_code == 400 and r.json()["error"] == "bad_request", bad


def test_a_lot_points_at_its_live_decision_and_a_flag_at_its_key(public_flags):
    hq = signed_in("HQ")
    ws = workspace(hq)
    live, flagged = case_and_flag_lots(ws)
    rows = walk(hq, size=25)
    for x in rows:
        if x["lot_id"] in live:
            assert (x["action"], x["case_key"], x["flag_key"]) == ("view", live[x["lot_id"]], None), x
        elif x["lot_id"] in flagged:
            assert (x["action"], x["case_key"], x["flag_key"]) == ("open", None, flagged[x["lot_id"]]), x
        else:
            assert (x["action"], x["case_key"], x["flag_key"]) == (None, None, None), x
    assert {x["action"] for x in rows} == {"view", "open", None}  # every kind of row occurs
    opened = next(x for x in rows if x["action"] == "open")  # opening the flag turns the row into "view"
    r = hq.post(f"/api/cases/{opened['flag_key']}/evaluate", json={}, headers=key())
    assert r.status_code == 200, r.json()
    again = next(x for x in walk(hq, size=25) if x["lot_id"] == opened["lot_id"])
    assert (again["action"], again["case_key"], again["flag_key"]) == ("view", opened["flag_key"], None)


def captured(fn) -> list[str]:
    """Every statement the API sends while `fn` runs, with the reference caches cold (05.2-12's capture helper)."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    refdata.get.cache_clear()
    sa.event.listen(db.engine(), "before_cursor_execute", record)
    try:
        fn()
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", record)
    return seen


def unbound(seen: list[str]) -> tuple[set[str], list[str]]:
    """(the large public tables read, the reads of them that bind neither a store nor a product)."""
    reads = [s for s in seen if re.search(r"\bSELECT\b", s, re.I) and any(re.search(rf"\b{t}\b", s) for t in LARGE)]
    return ({t for s in reads for t in LARGE if re.search(rf"\b{t}\b", s)},
            [s for s in reads if not re.match(r"\s*INSERT\b", s, re.I) and not BOUND.search(s)])


def test_lots_analytics_and_forecasts_never_read_a_large_public_table_without_a_store(public_flags):
    PRODUCTS.update(refdata.get(fresh(public_flags)["dataset_version"]).products)
    manager, hq = signed_in(first_store()), signed_in("HQ")

    def calls():
        store = first_store()
        for c, path in ((manager, "/api/lots"), (manager, "/api/lots?at_risk=1"), (hq, "/api/lots"),
                        (hq, f"/api/lots?branch={store}"), (manager, "/api/analytics"), (hq, f"/api/analytics?branch="
                        f"{store}&product={sorted(PRODUCTS)[0]}"), (manager, "/api/forecasts?page_size=1"),
                        (hq, f"/api/forecasts?branch={store}")):
            r = c.get(path)
            assert r.status_code == 200, r.json()
    tables, whole = unbound(captured(calls))
    assert {"ref_forecast_point", "ref_sales_day"} <= tables, tables  # not vacuous: each view reads them, bound
    assert whole == [], whole


def store_sales(ref, store: str) -> dict:
    """{sku: (units, hours out of stock)} of one store over the history days (first day to day -1), read here."""
    with db.tx() as conn:
        return {r["sku"]: (round(r["u"], 2), r["h"]) for r in db.rows(
            conn, "SELECT sku, sum(units) AS u, sum(oos_hours) AS h FROM ref_sales_day WHERE dataset_version = :v "
                  "AND store = :s AND day BETWEEN :a AND -1 GROUP BY sku", v=ref.dataset_version, s=store,
            a=ref.calendar["first_day"])}


def test_a_store_sees_public_sales_simulated_aging_estimated_waste_and_the_forecast(public_flags):
    store = first_store()
    c = signed_in(store)
    ws = workspace(c)
    ref = refdata.get(ws["dataset_version"])
    sold = store_sales(ref, store)
    assert len(sold) >= 2, sold  # the store sells more than one product
    sku = sorted(sold)[-1]  # a product the store sells, not necessarily its top seller
    r = c.get(f"/api/analytics?product={sku}")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["available"] is True and body["branch"] == store
    first = ref.calendar["first_day"]
    assert body["period"] == {"from": ref.date(first).isoformat(), "to": ref.date(-1).isoformat()}
    # sales by product: public, at most 10, by units descending then code, each total read again by hand
    top = sorted(sold, key=lambda k: (-sold[k][0], k))[:10]
    assert [x["sku"] for x in body["sales_by_product"]] == top
    for x in body["sales_by_product"]:
        assert (x["units"], x["hours_out_of_stock"], x["kind"]) == (sold[x["sku"]][0], sold[x["sku"]][1], "public")
    # one product's history (public) and the forecast made on the workspace's day (forecast, the 80% range)
    with db.tx() as conn:
        days = db.rows(conn, "SELECT day, units, oos_hours FROM ref_sales_day WHERE dataset_version = :v AND store = :s "
                             "AND sku = :k AND day BETWEEN :a AND -1 ORDER BY day", v=ref.dataset_version, s=store,
                       k=sku, a=first)
    series = body["series"]
    assert days and series["sku"] == sku and series["kinds"] == {"days": "public", "forecast": "forecast"}
    assert [(d["day"], d["units"], d["hours_out_of_stock"], d["kind"]) for d in series["days"]] == \
        [(d["day"], d["units"], d["oos_hours"], "public") for d in days]
    points = ref.forecast(ws["day"], store, sku)
    assert points and [(f["day"], f["low"], f["base"], f["high"], f["kind"]) for f in series["forecast"]] == \
        [(d, lo, base, hi, "forecast") for d, (lo, base, hi) in sorted(points.items())]
    # aging stock (simulated): the store's recorded lots by days to expiry, bucketed by hand
    lots = world_rows(ws, "lots", "world = 'shelfshift' AND qty > 0 AND branch = :b", b=store)
    bounds = [(None, 1), (2, 3), (4, 7), (8, None)]
    assert [a["bucket"] for a in body["aging"]] == BUCKETS and {a["kind"] for a in body["aging"]} == {"simulated"}
    for a, (lo, hi) in zip(body["aging"], bounds):
        inside = [l for l in lots if (lo is None or l["expiry_day"] - ws["day"] >= lo)
                  and (hi is None or l["expiry_day"] - ws["day"] <= hi)]
        assert (a["units"], a["lots"]) == (round(sum(l["qty"] for l in inside), 2), len(inside)), a
    # potential waste (estimated, against Keep only): live decisions' Keep and unopened flags' screen figure
    live, flagged = case_and_flag_lots(ws)
    with db.tx() as conn:
        # each live decision's lot: the lot's own Keep waste (quick 261018-h9t), never the Keep option's network total
        engine = {r["lot_id"]: lot_keep_waste(r["engine_input"]) or 0.0 for r in db.rows(
            conn, "SELECT c.lot_id, r.engine_input FROM cases c JOIN recommendations r ON r.run_id = "
                  "c.run_id AND r.case_id = c.case_id AND r.rec_version = c.active_rec_version WHERE c.run_id = :r "
                  "AND c.source_branch = :b AND c.state NOT IN ('CLOSED', 'REJECTED')", r=ws["run_id"], b=store)}
        screen = {r["lot_id"]: r["keep_waste"] for r in db.rows(
            conn, "SELECT lot_id, keep_waste FROM case_flags WHERE run_id = :f AND day = :d AND branch = :b",
            f=overlay.flags_run(conn, ws), d=ws["day"], b=store) if r["lot_id"] in flagged}
    wasted = {**{k: v for k, v in screen.items() if v and v > 0}, **{k: v for k, v in engine.items() if v > 0}}
    expect = sorted(wasted, key=lambda l: (-wasted[l], l))[:10]
    assert expect and [w["lot_id"] for w in body["waste"]] == expect  # not vacuous
    assert len({w["lot_id"] for w in body["waste"]}) == len(body["waste"])  # each lot once
    assert any(w["source"] == "engine" for w in body["waste"])  # a live decision's lot figure is checked
    for w in body["waste"]:
        product = ref.products[w["sku"]]
        assert (w["kind"], w["basis"]) == ("estimated", "keep")
        units = wasted[w["lot_id"]]  # kg and value come from the unrounded figure, as on the board's card
        assert w["units"] == round(units, 2) and w["kg"] == round(units * product.kg_per_unit, 2)
        assert w["value"] == (None if product.price is None else round(units * product.price, 2))
        assert w["source"] == ("engine" if w["lot_id"] in live else "screen")
    assert body["kinds"] == {"sales_by_product": "public", "series": {"days": "public", "forecast": "forecast"},
                             "aging": "simulated", "waste": "estimated"}
    assert "observed" not in r.text  # no value is called observed (D-15)
    default = c.get("/api/analytics").json()  # no product: the store's top seller
    assert default["series"]["sku"] == top[0]


def test_analytics_needs_a_store_for_hq_and_a_product_the_store_sells(public_flags):
    store = first_store()
    hq = signed_in("HQ")
    r = hq.get("/api/analytics")
    assert r.status_code == 400 and r.json() == {"error": "bad_request",
                                                 "message": "Choose a branch to see its analytics."}
    assert hq.get(f"/api/analytics?branch={store}").json()["branch"] == store
    for bad in ("NOPE", "P999"):
        r = hq.get(f"/api/analytics?branch={store}&product={bad}")
        assert r.status_code == 404 and r.json()["error"] == "not_found", bad
    r = hq.get("/api/forecasts")
    assert r.status_code == 400 and r.json() == {"error": "bad_request",
                                                 "message": "Choose a branch to see its forecasts."}
    assert hq.get("/api/analytics?branch=NOPE").status_code == 400


def test_forecasts_page_a_stores_products_with_seven_days_and_the_80_percent_range(public_flags):
    store = first_store()
    c = signed_in(store)
    ws = workspace(c)
    ref = refdata.get(ws["dataset_version"])
    fr = ref.forecast_runs[ws["day"]]
    last = min(ws["day"] + 6, fr["last_day"])
    with db.tx() as conn:
        points = db.rows(conn, "SELECT sku, day, low, base, high FROM ref_forecast_point WHERE dataset_version = :v "
                               "AND forecast_run_id = :f AND store = :s AND day BETWEEN :a AND :b ORDER BY sku, day",
                         v=ref.dataset_version, f=fr["forecast_run_id"], s=store, a=ws["day"], b=last)
    skus = sorted({p["sku"] for p in points})
    items, n = [], 1
    while True:
        r = c.get(f"/api/forecasts?page={n}&page_size=1")
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["total"] == len(skus) and body["page_size"] == 1
        if not body["items"]:
            break
        items += body["items"]
        n += 1
    run = body["run"]
    assert (run["id"], run["history_to"], run["period_from"], run["period_to"]) == \
        (fr["forecast_run_id"], ref.date(fr["cutoff_day"]).isoformat(), ref.date(ws["day"]).isoformat(),
         ref.date(last).isoformat())
    assert run["provider"] in fr["providers"]
    assert [x["sku"] for x in items] == skus
    for x in items:
        mine = [p for p in points if p["sku"] == x["sku"]]
        assert len(x["daily_base"]) == 7 and x["daily_base"] == [p["base"] for p in mine]
        assert (x["base_total"], x["low_total"], x["high_total"]) == tuple(
            round(sum(p[k] for p in mine), 2) for k in ("base", "low", "high"))
        assert x["simulated_inputs"] is False and x["kind"] == "forecast"
    assert c.get("/api/forecasts?page_size=26").status_code == 400
