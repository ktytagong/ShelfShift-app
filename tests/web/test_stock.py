"""PROD-16 (06.1 D-15, D-17, D-31, D-56; UI-SPEC "Stock & lots"): the store's lots with simulated stock labelled, the
cover of the recorded stock by the forecast to expiry, server filters and pages, the row's decision action taken from
the server's `action` (never a second decision for a lot), and the lot drawer with eligibility, holds and review
flags, linked branch transfers and history.

The synthetic Day-3 template carries the story (SUS-19 at the start store). Public-dataset states (a flagged lot, a
page past the first, an unmapped store) are answered with `page.route` from tests/web/fixtures/lots_public.json, a
contract-shaped fixture written from the 2026-10-17 contract row (06.1-01's rule until 05.2-22's public browser
fixture reaches build/product; 06.1-31 reruns them on it). Expected figures are read from the API, never pasted.
"""
from __future__ import annotations

import json
import pathlib
import re
import time
import urllib.parse

import pytest
from playwright.sync_api import expect

from tests.web.conftest import case_id_by_tag, case_json, session_json, switch_seat

pytestmark = pytest.mark.web

STORY_KEY = "SUS-19"  # the template's story decision (03-CONTEXT D-23), a design id


def lots_json(page, query: str = "") -> dict:
    """GET /api/lots through the page's own context (same cookie, same store)."""
    r = page.request.get("/api/lots" + query)
    assert r.status == 200, r.status
    return r.json()


def lot_row(page, lot_id: str):
    return page.get_by_test_id("lots-table").locator(f'tr[data-lot="{lot_id}"]')


def test_the_story_manager_lists_the_lots_and_opens_the_story_lots_drawer(visitor):
    page = visitor.page
    page.goto("/#/stock")
    expect(page.get_by_role("heading", level=1, name="Stock & lots")).to_be_visible()
    nav = page.get_by_role("navigation", name="Main")
    expect(nav.get_by_role("link", name="Stock & lots")).to_have_attribute("aria-current", "page")

    story = next(x for x in lots_json(page)["items"] if x["case_key"] == STORY_KEY)
    table = page.get_by_test_id("lots-table")
    expect(table.locator("thead").get_by_text("Simulated", exact=True)).to_be_visible()  # over the stock columns
    row = lot_row(page, story["lot_id"])
    expect(row).to_be_visible()
    expect(row.get_by_role("link", name="View stock decision")).to_have_attribute("href", f"#/cases/{STORY_KEY}")
    expect(row.get_by_role("link", name="Open stock decision")).to_have_count(0)  # never a second decision
    expect(row).to_contain_text("sell by expiry")
    expect(row).to_contain_text("Not recorded")  # shelf / backroom: the record holds no split

    row.get_by_role("button", name="View lot").click()
    drawer = page.get_by_role("complementary", name=f"Lot {story['lot_id']}")
    expect(drawer).to_be_visible()
    expect(drawer.get_by_text("Transfer eligibility", exact=True)).to_be_visible()
    holds = drawer.get_by_test_id("lot-holds")
    expect(holds).to_contain_text("Holds and review flags")
    expect(holds).to_contain_text("None recorded")
    expect(drawer).to_contain_text(f"{story['branch']} · shelf and backroom not recorded")
    history = drawer.get_by_test_id("lot-history")
    expect(history.get_by_role("listitem").first).to_be_visible()
    first = history.get_by_role("listitem").first
    expect(first).to_contain_text("Recorded")
    expect(first).to_contain_text("Scenario date:")
    for gone in ("Edit", "Save", "Place hold", "Release hold"):
        expect(drawer.get_by_role("button", name=gone)).to_have_count(0)  # no quantity, expiry or hold control (D-56)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# --- filters, pages, the open action and the empty and blocked states (Task 2) -------------------------------------

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "lots_public.json").read_text(encoding="utf-8"))


def lots_request(page, action):
    """Run `action` and return the query of the GET /api/lots it sends (the list's request, not a lot's drawer)."""
    with page.expect_request(lambda r: urllib.parse.urlsplit(r.url).path == "/api/lots" and r.method == "GET") as req:
        action()
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.value.url).query)


def test_every_filter_goes_to_the_server_and_an_empty_answer_says_so(visitor):
    page = visitor.page
    page.goto("/#/stock")
    expect(page.get_by_role("heading", level=1, name="Stock & lots")).to_be_visible()
    expect(page.get_by_label("Branch")).to_have_count(0)  # a manager reads their own store only

    at_risk = page.get_by_role("button", name="At risk only")
    expect(at_risk).to_have_attribute("aria-pressed", "false")
    assert lots_request(page, at_risk.click) == {"at_risk": ["1"]}
    expect(at_risk).to_have_attribute("aria-pressed", "true")
    with_case = page.get_by_role("button", name="With an open stock decision")
    assert lots_request(page, with_case.click) == {"at_risk": ["1"], "with_case": ["1"]}
    expect(with_case).to_have_attribute("aria-pressed", "true")
    expect(lot_row(page, next(x for x in lots_json(page)["items"] if x["case_key"] == STORY_KEY)["lot_id"])).to_be_visible()

    # A product the store holds but no live decision covers: the server answers no rows, and the page says so.
    items = lots_json(page)["items"]
    decided = {x["sku"] for x in items if x["case_key"] is not None}
    quiet = sorted({x["sku"] for x in items} - decided)
    assert quiet, "the story store holds no product without a live decision"
    product = page.get_by_label("Product code")
    product.fill(quiet[0])
    assert lots_request(page, lambda: product.press("Enter")) == \
        {"product": [quiet[0]], "at_risk": ["1"], "with_case": ["1"]}
    expect(page.get_by_text("No lots match", exact=True)).to_be_visible()
    expect(page.get_by_text("Change the filters, or clear At risk only.", exact=True)).to_be_visible()
    expect(page.get_by_test_id("lots-table")).to_have_count(0)


def test_hq_names_a_branch_for_the_server(visitor):
    page = visitor.page
    store = session_json(page)["branch"]
    switch_seat(page, None)
    page.goto("/#/stock")
    branch = page.get_by_label("Branch")
    expect(branch).to_be_visible()
    branch.fill(store)
    assert lots_request(page, lambda: branch.press("Enter")) == {"branch": [store]}
    expect(page.get_by_test_id("lots-table").locator("tbody tr").first).to_be_visible()


def test_public_pages_open_a_flagged_lot_once_and_an_unmapped_store_says_why(visitor):
    page = visitor.page
    state = {"opened": False, "evaluations": 0, "lists": []}
    story = case_json(page, case_id_by_tag(page, "milk-story"))  # a real, contract-shaped evaluated detail
    flag = FIXTURE["page1"]["items"][0]
    flag_detail = {**story, "case_id": flag["flag_key"], "key": flag["flag_key"], "needs_evaluation": True,
                   "result": None, "fact_checks": [], "risk": FIXTURE["flag_risk"]}

    def lots(route):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(route.request.url).query)
        state["lists"].append(q)
        body = FIXTURE["page2"] if q.get("page") == ["2"] else \
            FIXTURE["page1_after_open" if state["opened"] else "page1"]
        route.fulfill(json=body)

    def evaluate(route):
        assert route.request.method == "POST" and route.request.headers.get("idempotency-key")
        state["evaluations"] += 1
        state["opened"] = True
        route.fulfill(json=story)

    page.route(lambda url: urllib.parse.urlsplit(url).path == "/api/lots", lots)
    page.route(f"**/api/lots/{FIXTURE['drawer_unmapped']['lot_id']}", lambda r: r.fulfill(json=FIXTURE["drawer_unmapped"]))
    page.route(f"**/api/cases/{flag['flag_key']}", lambda r: r.fulfill(json=flag_detail))
    page.route(f"**/api/cases/{flag['flag_key']}/evaluate", evaluate)

    page.goto("/#/stock")
    row = lot_row(page, flag["lot_id"])
    expect(row).to_contain_text("120 of 300 sell by expiry")
    expect(row).to_contain_text("Estimated")
    expect(page.get_by_test_id("pager")).to_have_text("Page 1 of 2 · 3 items")
    page.get_by_role("button", name="Next page").click()
    unmapped = FIXTURE["drawer_unmapped"]
    expect(lot_row(page, unmapped["lot_id"])).to_be_visible()
    assert state["lists"][-1] == {"page": ["2"]}
    lot_row(page, unmapped["lot_id"]).get_by_role("button", name="View lot").click()
    drawer = page.get_by_role("complementary", name=f"Lot {unmapped['lot_id']}")
    expect(drawer).to_contain_text("Transfers unavailable: location or route data missing")
    drawer.get_by_role("button", name="Close").click()
    page.get_by_role("button", name="Previous page").click()

    open_link = lot_row(page, flag["lot_id"]).get_by_role("link", name="Open stock decision")
    expect(open_link).to_have_attribute("href", f"#/cases/{flag['flag_key']}")
    expect(lot_row(page, flag["lot_id"]).get_by_role("link", name="View stock decision")).to_have_count(0)
    open_link.click()
    page.wait_for_url(f"**/#/cases/{flag['flag_key']}*")
    deadline = time.monotonic() + 30
    while state["evaluations"] < 1:
        assert time.monotonic() < deadline, "the decision page never evaluated the flagged lot"
        page.wait_for_timeout(100)

    page.goto("/#/stock")
    back = lot_row(page, flag["lot_id"])
    expect(back.get_by_role("link", name="View stock decision")).to_have_attribute("href", f"#/cases/{flag['flag_key']}")
    expect(back.get_by_role("link", name="Open stock decision")).to_have_count(0)
    assert state["evaluations"] == 1  # evaluated once; Stock & lots never posts anything


def test_the_page_offers_no_quantity_expiry_or_hold_edit():
    source = (pathlib.Path(__file__).resolve().parents[2] / "web" / "src" / "pages" / "stock.tsx").read_text("utf-8")
    assert not re.search(r'type="(number|date)"', source)
    assert "post(" not in source and "useAction" not in source


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_no_sideways_page_scroll_with_the_drawer_open(visitor, width):
    page = visitor.page
    page.set_viewport_size({"width": width, "height": 844})
    page.goto("/#/stock")
    story = next(x for x in lots_json(page)["items"] if x["case_key"] == STORY_KEY)
    expect(lot_row(page, story["lot_id"])).to_be_attached()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.goto(f"/#/stock?lot={urllib.parse.quote(story['lot_id'])}")
    expect(page.get_by_role("complementary", name=f"Lot {story['lot_id']}").get_by_test_id("lot-history")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# --- the lot drawer shows only the lot asked for and refreshes on refocus (quick 261017-wzs findings 3 and 6) --------

def units_text(n: float) -> str:
    """format.units: grouped, at most one decimal."""
    text = f"{n:,.1f}"
    return text[:-2] if text.endswith(".0") else text


def lot_detail(page, lot_id: str) -> dict:
    r = page.request.get(f"/api/lots/{urllib.parse.quote(lot_id, safe='')}")
    assert r.status == 200, r.status
    return r.json()


def two_lots(page) -> tuple[dict, dict]:
    """Two of the store's lots with different quantities; the second holds the most units."""
    items = sorted(lots_json(page, "?page_size=50")["items"], key=lambda x: x["qty"])
    a, b = items[0], items[-1]
    assert a["qty"] != b["qty"], "the store holds no two lots with different quantities"
    return a, b


def drawer_qty(drawer) -> str:
    return drawer.get_by_text(re.compile(r"^-?[\d,.]+ units on record$"))


def test_a_late_answer_for_an_earlier_lot_never_replaces_the_lot_shown(visitor):
    page = visitor.page
    page.goto("/#/stock")
    a, b = two_lots(page)
    late = {**lot_detail(page, a["lot_id"]), "qty": 777}  # A's real answer, its quantity marked so it is seen if shown
    held: list = []
    page.route(f"**/api/lots/{urllib.parse.quote(a['lot_id'], safe='')}", lambda route: held.append(route))
    page.goto(f"/#/stock?lot={urllib.parse.quote(a['lot_id'])}")
    deadline = time.monotonic() + 15
    while not held:
        assert time.monotonic() < deadline, "the drawer never asked for lot A"
        page.wait_for_timeout(50)
    page.evaluate(f"location.hash = {json.dumps('#/stock?lot=' + urllib.parse.quote(b['lot_id']))}")
    drawer = page.get_by_role("complementary", name=f"Lot {b['lot_id']}")
    expect(drawer_qty(drawer)).to_have_text(f"{units_text(b['qty'])} units on record")
    facts = drawer.locator("dl").first.inner_text()  # B's quantity, expiry, eligibility, holds and transfers
    held[0].fulfill(json=late)  # A's answer arrives after B's
    page.wait_for_timeout(500)
    expect(drawer).to_be_visible()
    expect(drawer_qty(drawer)).to_have_text(f"{units_text(b['qty'])} units on record")
    expect(drawer).not_to_contain_text("777 units")
    assert drawer.locator("dl").first.inner_text() == facts


def test_the_open_drawer_refreshes_on_refocus_and_keeps_its_lot_when_that_fails(visitor):
    page = visitor.page
    page.goto("/#/stock")
    _, b = two_lots(page)
    page.goto(f"/#/stock?lot={urllib.parse.quote(b['lot_id'])}")
    drawer = page.get_by_role("complementary", name=f"Lot {b['lot_id']}")
    expect(drawer_qty(drawer)).to_have_text(f"{units_text(b['qty'])} units on record")

    path = f"**/api/lots/{urllib.parse.quote(b['lot_id'], safe='')}"

    def lowered(route):
        body = route.fetch().json()
        route.fulfill(json={**body, "qty": body["qty"] - 40})  # a dispatch of 40 recorded in another tab

    page.route(path, lowered)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(drawer_qty(drawer)).to_have_text(f"{units_text(b['qty'] - 40)} units on record")
    page.unroute(path)

    busy = {"error": "db_busy", "message": "The server is busy. Try again in a moment."}
    page.route(path, lambda route: route.fulfill(status=503, json=busy))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(drawer.get_by_text(busy["message"], exact=True)).to_be_visible()
    expect(drawer.get_by_role("button", name="Try again")).to_be_visible()
    expect(drawer_qty(drawer)).to_have_text(f"{units_text(b['qty'] - 40)} units on record")  # the last details stay
    page.unroute(path)
    drawer.get_by_role("button", name="Try again").click()
    expect(drawer_qty(drawer)).to_have_text(f"{units_text(b['qty'])} units on record")
    expect(drawer.get_by_role("button", name="Try again")).to_have_count(0)


def test_a_refused_product_code_shows_the_error_alone_and_a_good_code_brings_the_rows_back(visitor):
    """QA F3 (quick 261018-hel): after a valid product, an unknown code shows the server's refusal and no lot rows of
    the previous product under it; the valid code again shows its rows and the error goes."""
    page = visitor.page
    page.goto("/#/stock")
    expect(page.get_by_role("heading", level=1, name="Stock & lots")).to_be_visible()
    good = lots_json(page)["items"][0]["sku"]
    product = page.get_by_label("Product code")
    product.fill(good)
    assert lots_request(page, lambda: product.press("Enter")) == {"product": [good]}
    rows = page.get_by_test_id("lots-table").locator("tbody tr")
    expect(rows.first).to_be_visible()

    bad = "QA-NO-SUCH-PRODUCT"
    r = page.request.get(f"/api/lots?product={bad}")
    assert r.status == 400, r.status
    product.fill(bad)
    product.press("Enter")
    expect(page).to_have_url(re.compile(rf"product={bad}"))
    expect(page.get_by_text(r.json()["message"], exact=True)).to_be_visible()
    expect(page.get_by_test_id("lots-table")).to_have_count(0)
    expect(rows).to_have_count(0)
    # A refetch that then fails another way (refocus, server busy) never brings the previous product's rows back.
    busy = {"error": "db_busy", "message": "The server is busy. Try again in a moment."}
    page.route("**/api/lots?*", lambda route: route.fulfill(status=503, json=busy))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.get_by_text(busy["message"], exact=True)).to_be_visible()
    expect(page.get_by_test_id("lots-table")).to_have_count(0)
    page.unroute("**/api/lots?*")

    product.fill(good)
    product.press("Enter")
    expect(rows.first).to_be_visible()
    expect(page.get_by_text(r.json()["message"], exact=True)).to_have_count(0)


# --- Phase 7: a held lot's chip and a confirmed fact's label (07-UI-SPEC surface 6; 07-22) ------------------------------

def test_a_held_lot_shows_its_chip_and_a_confirmed_fact_its_label(visitor):
    """The story lot under a hold shows "Lot hold" in its row; with a confirmed count it carries that fact's label as
    the row's title and as a focusable note."""
    from tests.api.conftest import fresh
    from tests.api.test_reassess import confirm_count
    from tests.web.conftest import case_id_by_tag, case_json, run_id, switch_seat
    from tests.web.test_banners import post
    from tests.web.test_transfers import STORY_TAG
    page = visitor.page
    d = case_json(page, case_id_by_tag(page, STORY_TAG))
    switch_seat(page, d["source"])
    lot = d["lot"]["lot_id"]
    post(page, f"/api/lots/{lot}/holds", {"reason": "Crushed crates in the chiller"})
    confirm_count(fresh(run_id(page)), lot)
    label = next(r for r in lots_json(page)["items"] if r["lot_id"] == lot)["fact_label"]
    assert label
    page.goto("/#/stock")
    row = lot_row(page, lot)
    expect(row.get_by_test_id("lot-hold-chip")).to_have_text("Lot hold")
    expect(row).to_have_attribute("title", label)
    expect(row.get_by_test_id("fact-label")).to_have_attribute("aria-label", label)
