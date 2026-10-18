"""PROD-15 and PROD-14's panel (06.1 D-17, D-30, D-33, D-52, D-55; UI-SPEC "Branch transfers", "Execute" item 1):
the Branch transfers page lists a store's outgoing or incoming transfers with their quantities, the decision status
and the movement status kept apart, and links into the stock decision's Execute step; it records nothing itself.

The story transfer (the milk-story tag) reaches PREPARED and then DISPATCHED through the API in setup, as the people
involved make it: the source takes ownership and sends, the destination accepts, the source approves, prepares and
records the dispatch (each POST with its own Idempotency-Key, through the page's own signed-in context). Expected
text is built from the API's own answers (quantities, run times, the session's zone), never typed here.
"""
from __future__ import annotations

import re
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests.web.conftest import (
    case_id_by_tag, case_json, cards_json, session_json, switch_seat, take_ownership,
)

pytestmark = pytest.mark.web

STORY_TAG = "milk-story"


# --- setup through the API -------------------------------------------------------------------------------------------

def post(page: Page, path: str, body: dict) -> dict:
    """One keyed write through the page's own context; asserts 200 and returns the answer."""
    r = page.request.post(path, data=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def get(page: Page, path: str) -> dict:
    r = page.request.get(path)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def case_path(case_id: str, route: str) -> str:
    return f"/api/cases/{urllib.parse.quote(case_id, safe='')}/{route}"


def prepare_story(page: Page) -> dict:
    """The story stock decision taken, sent, accepted, approved and prepared; ends signed in as its source. Returns
    {case_id, key, source, dest, action}: `action` is its TRANSFER work order from the execution view."""
    case_id = case_id_by_tag(page, STORY_TAG)
    d = case_json(page, case_id)
    source, dest, version = d["source"], d["dest"], d["rec_version"]
    assert dest, "the story's recommendation is a transfer (03-CONTEXT D-23)"
    take_ownership(page, case_id)
    post(page, case_path(case_id, "send"), {"rec_version": version})
    switch_seat(page, dest)
    post(page, case_path(case_id, "decision"), {"rec_version": version, "decision": "accept"})
    switch_seat(page, source)
    post(page, case_path(case_id, "decision"), {"rec_version": version, "decision": "approve"})
    post(page, case_path(case_id, "prepare"), {})
    view = get(page, case_path(case_id, "execution"))
    action = next(w for w in view["work_orders"] if w["kind"] == "TRANSFER" and w["state"] == "PLANNED")
    return {"case_id": case_id, "key": d["key"], "source": source, "dest": dest, "action": action,
            "condition": view["options"]["conditions"][0]["key"]}


def dispatch_story(page: Page, story: dict) -> int:
    """The source records the dispatch of the whole committed quantity; returns the units dispatched."""
    qty = int(story["action"]["qty"])
    post(page, case_path(story["case_id"], "dispatch"), {
        "action_id": story["action"]["action_id"], "qty": qty, "lot_id": story["action"]["lot_id"],
        "condition": story["condition"]})
    return qty


def units(n: float) -> str:
    """format.units for whole counts: grouped, en-US."""
    return f"{int(n):,}" if float(n).is_integer() else f"{n:,.1f}"


def row(page: Page, action_id: str):
    return page.get_by_test_id(f"transfer-row-{action_id}")


def drawer_of(page: Page):
    """The row's drawer (the sidebar is a complementary region too)."""
    return page.get_by_role("complementary", name=re.compile(r"^Branch transfer · "))


# --- Task 1: the story transfer from both ends -----------------------------------------------------------------------

def test_the_story_transfer_shows_at_both_ends_with_its_quantities(visitor):
    page = visitor.page
    story = prepare_story(page)
    sent = dispatch_story(page, story)
    committed = story["action"]["qty"]

    page.goto("/#/transfers")
    expect(page.get_by_role("heading", level=1, name="Branch transfers")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name="Branch transfers")).to_have_attribute(
        "aria-current", "page")
    mine = row(page, story["action"]["action_id"])
    expect(mine).to_be_visible()
    expect(mine).to_contain_text(f"Committed {units(committed)} · Dispatched {units(sent)}")
    # Nothing was received yet, so no received figure is claimed (QtyBar shows only recorded quantities).
    expect(mine.get_by_test_id("transfer-units")).not_to_contain_text("Received")
    expect(mine.get_by_test_id("decision-chip")).to_have_text("Committed")
    expect(mine.get_by_test_id("movement-chip")).to_have_text("Dispatched")
    expect(mine.get_by_role("link", name="View stock decision")).to_have_count(1)

    switch_seat(page, story["dest"])
    page.goto("/#/transfers")
    page.get_by_role("button", name="Incoming").click()
    theirs = row(page, story["action"]["action_id"])
    expect(theirs).to_contain_text(f"Committed {units(committed)} · Dispatched {units(sent)}")
    expect(theirs.get_by_test_id("movement-chip")).to_have_text("Dispatched")
    link = theirs.get_by_role("link", name="Record receipt")
    expect(link).to_have_attribute("href", f"#/cases/{urllib.parse.quote(story['key'], safe='')}?step=execute")
    assert session_json(page)["branch"] == story["dest"]


# --- Task 2: the transport details panel in the row's drawer ---------------------------------------------------------

NOT_PROMISED = re.compile(r"\b(ETA|delay(ed)?|late|overdue|countdown|ago|min(utes)? left)\b", re.IGNORECASE)


def hhmm(minute: int) -> str:
    """format.hhmm: minutes after midnight as 24-hour "HH:MM"."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def city(tz: str) -> str:
    """vocab.cityOf: "Asia/Bangkok" -> "Bangkok"."""
    return tz.split("/")[-1].replace("_", " ")


def test_the_row_opens_the_transport_details_panel(visitor):
    page = visitor.page
    story = prepare_story(page)
    dispatch_story(page, story)
    view = get(page, case_path(story["case_id"], "transport"))
    run = view["run"]
    assert run is not None, "the prepared story transfer has its Scheduled truck run (06.1-12)"
    tz = session_json(page)["time_zone"]

    page.goto("/#/transfers")
    row(page, story["action"]["action_id"]).get_by_role("button").first.click()
    drawer = drawer_of(page)
    expect(drawer.get_by_role("heading", name=f"Scheduled truck run · {story['source']} → {story['dest']}")).to_be_visible()
    chips = drawer.get_by_test_id("run-chips")
    expect(chips).to_contain_text("Own fleet (assumed)")
    expect(chips).to_contain_text("Logistics coordinator (represented)")
    expect(chips).to_contain_text("Cold chain" if run["cold_chain"] else "Ambient")
    expect(chips).to_contain_text("Simulated")

    expect(drawer.get_by_test_id("milestones").locator("li")).to_have_count(6)
    expect(drawer.get_by_test_id("milestone-loading_cutoff")).to_contain_text(f"{hhmm(run['loading_cutoff_min'])}")
    expect(drawer.get_by_test_id("milestone-loading_cutoff")).to_contain_text("planned")
    expect(drawer.get_by_test_id("milestone-departure")).to_contain_text(hhmm(run["departs_min"]))
    expect(drawer.get_by_test_id("milestone-ready")).to_contain_text("Unscheduled")
    expect(drawer.get_by_test_id("milestone-received")).to_contain_text("Unscheduled")

    record = drawer.get_by_test_id("movement-record").locator("li")
    expect(record).to_have_count(3)
    expect(record.nth(1)).to_contain_text("Dispatched")
    expect(record.nth(1)).to_contain_text("Recorded ")
    expect(record.nth(1)).to_contain_text(f"{city(tz)} time")
    expect(record.nth(2)).to_contain_text("Not yet recorded")
    expect(drawer.get_by_test_id("decision-box")).to_contain_text(story["key"])
    expect(drawer.get_by_test_id("movement-box").get_by_test_id("movement-chip")).to_have_text("Dispatched")
    expect(drawer).to_contain_text("A committed decision is not a departure.")
    expect(drawer).to_contain_text("Tote use per unit: not stored")

    origin = drawer.get_by_role("button", name=re.compile("Origin · load"))
    dest = drawer.get_by_role("button", name=re.compile("Destination · receive"))
    expect(origin).to_have_attribute("aria-pressed", "true")
    dest.click()
    expect(dest).to_have_attribute("aria-pressed", "true")
    expect(origin).to_have_attribute("aria-pressed", "false")
    expect(drawer.get_by_test_id("stop-panel")).to_contain_text("Receive ")
    expect(drawer.get_by_test_id("stop-panel")).to_contain_text(story["key"])

    expect(drawer.get_by_test_id("planned-note")).to_have_count(1)
    expect(drawer.get_by_test_id("planned-note")).to_have_text(
        "Planned times. Actual times appear when someone records the work.")
    expect(drawer.get_by_role("link", name="View stock decision")).to_have_attribute(
        "href", f"#/cases/{urllib.parse.quote(story['key'], safe='')}?step=execute")
    assert not NOT_PROMISED.search(drawer.inner_text()), NOT_PROMISED.search(drawer.inner_text())
    # The drawer records nothing (D-52): no record button on the page.
    expect(page.get_by_role("button", name=re.compile(r"^Record"))).to_have_count(0)


def fake_row(case_id: str, key: str, source: str, dest: str, n: int = 1) -> dict:
    """A contract-shaped GET /api/transfers row (04-API-CONTRACT "Read views"), for a state the real data cannot give:
    a page answered from a fixture through `page.route` (06.1-01 execution rules)."""
    return {"action_id": f"{key}#1:T{n}", "case_id": case_id, "key": key, "sku": "P0001", "lot_id": f"L-{n:04d}",
            "new_lot_id": None, "source": source, "dest": dest, "committed_qty": 10, "dispatched_qty": None,
            "received_qty": None, "damaged_qty": None, "run_code": None, "run_day": None, "departs_min": None,
            "arrives_min": None, "decision_state": "PREPARED", "movement_state": "planned",
            "next_action": {"key": "record_dispatch", "who": [{"role": "manager", "branch": source}]}}


def answer(route, items: list[dict], page_size: int, total: int) -> None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
    n = int(q.get("page", ["1"])[0])
    route.fulfill(json={"items": items[(n - 1) * page_size:n * page_size], "page": n, "page_size": page_size,
                        "total": total, "scope": {"branch": q.get("branch", [None])[0], "direction": "all"}})


def test_a_keep_decision_panel_reads_no_transport_assigned(visitor):
    page = visitor.page
    switch_seat(page, None)  # HQ reads every store's stock decisions
    keep = next((c for c in cards_json(page) if c["suggested_key"] == "keep"), None)
    assert keep is not None, "the template has a stock decision whose recommendation is Keep (06.1-12 D3)"
    # A Keep decision has no transfer row, so the list is answered from a fixture whose row names that decision;
    # the panel itself reads the real GET /api/cases/{id}/transport.
    items = [fake_row(keep["case_id"], keep["key"], keep["source"], keep["source"])]
    page.route("**/api/transfers*", lambda route: answer(route, items, 50, 1))
    page.goto("/#/transfers")
    row(page, items[0]["action_id"]).get_by_role("button").first.click()
    panel = drawer_of(page).get_by_test_id("transport-panel")
    expect(panel).to_have_text("No transport assigned.")
    expect(panel.get_by_test_id("milestones")).to_have_count(0)
    assert not NOT_PROMISED.search(panel.inner_text())


# --- Task 3: the filters, the empty state and overflow ---------------------------------------------------------------

WIDTHS = (360, 390, 768, 1280)


def transfers_request(direction: str | None = None, **params: str):
    """A predicate for the next GET /api/transfers whose query holds `direction` and `params`."""
    def match(request) -> bool:
        url = urllib.parse.urlparse(request.url)
        if url.path != "/api/transfers":
            return False
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        want = dict(params, **({} if direction is None else {"direction": direction}))
        return all(q.get(k) == v for k, v in want.items())
    return match


def test_incoming_and_the_status_filter_go_to_the_server(visitor):
    page = visitor.page
    page.goto("/#/transfers")
    expect(page.get_by_role("heading", level=1, name="Branch transfers")).to_be_visible()
    group = page.get_by_role("group", name="Direction")
    expect(group.get_by_role("button", name="Outgoing")).to_have_attribute("aria-pressed", "true")
    expect(group.get_by_role("button", name="All")).to_have_count(0)  # HQ only
    expect(page.get_by_label("Branch")).to_have_count(0)  # HQ only
    with page.expect_request(transfers_request("incoming")):
        group.get_by_role("button", name="Incoming").click()
    expect(group.get_by_role("button", name="Incoming")).to_have_attribute("aria-pressed", "true")
    status = page.get_by_label("Movement status")
    expect(status.locator("option")).to_have_text(
        ["All", "Planned", "Dispatched", "Partially received", "Received", "Discrepancy"])
    with page.expect_request(transfers_request("incoming", state="dispatched")):
        status.select_option("dispatched")


def test_hq_sees_all_and_a_branch_input_and_the_pager_walks_pages(visitor, web):
    page = visitor.page
    switch_seat(page, None)
    items = [fake_row(f"SUS-9{n}", f"SUS-9{n}", web.store, web.store, n) for n in range(1, 4)]
    page.route("**/api/transfers*", lambda route: answer(route, items, 1, len(items)))
    page.goto("/#/transfers")
    group = page.get_by_role("group", name="Direction")
    expect(group.get_by_role("button")).to_have_text(["Outgoing", "Incoming", "All"])
    expect(group.get_by_role("button", name="All")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("pager")).to_have_text("Page 1 of 3 · 3 items")
    expect(row(page, items[0]["action_id"])).to_be_visible()
    with page.expect_request(transfers_request(page="2")):
        page.get_by_role("button", name="Next page").click()
    expect(page.get_by_test_id("pager")).to_have_text("Page 2 of 3 · 3 items")
    expect(row(page, items[1]["action_id"])).to_be_visible()
    branch = page.get_by_label("Branch")
    branch.fill(web.store)
    with page.expect_request(transfers_request("all", branch=web.store)):
        branch.press("Enter")
    expect(page.get_by_test_id("pager")).to_have_text("Page 1 of 3 · 3 items")


NO_SIDE_SCROLL = ("() => document.documentElement.scrollWidth <= window.innerWidth "
                  "&& document.querySelector('main#content').scrollWidth "
                  "<= document.querySelector('main#content').clientWidth")


def no_side_scroll(page) -> bool:
    """Neither the page nor the main column scrolls sideways, once the layout has settled after a resize (the
    sidebar's width transition runs for 200ms)."""
    try:
        page.wait_for_function(NO_SIDE_SCROLL, timeout=5_000)
    except PlaywrightTimeout:
        return False
    return True


def test_an_empty_store_says_so_and_the_page_records_nothing(visitor, web):
    page = visitor.page
    page.goto("/#/transfers")
    empty = page.get_by_test_id("transfers-empty")
    expect(empty.get_by_role("heading", name=f"No branch transfers for {web.store}")).to_be_visible()
    expect(empty).to_contain_text("Transfers appear here when a stock decision that moves stock is approved.")
    expect(page.get_by_role("button", name=re.compile(r"^Record"))).to_have_count(0)
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        assert no_side_scroll(page), width


def test_the_table_scrolls_in_its_own_box(visitor, web):
    page = visitor.page
    items = [fake_row("SUS-91", "SUS-91", web.store, web.store)]
    page.route("**/api/transfers*", lambda route: answer(route, items, 50, 1))
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        page.goto("/#/transfers")
        expect(row(page, items[0]["action_id"])).to_be_visible()
        assert no_side_scroll(page), width
        if width < 960:
            box = page.get_by_test_id("transfers-scroll")
            assert box.evaluate("el => el.scrollWidth > el.clientWidth"), width
    expect(page.get_by_role("button", name=re.compile(r"^Record"))).to_have_count(0)


def test_a_failed_load_says_so_and_try_again_loads(visitor, web):
    page = visitor.page
    message = "The database is busy. Try again in a moment."  # the server's words are shown as they come
    page.route("**/api/transfers*", lambda route: route.fulfill(
        status=503, json={"error": "db_busy", "message": message}))
    page.goto("/#/transfers")
    expect(page.get_by_role("heading", name="Branch transfers didn't load")).to_be_visible()
    expect(page.get_by_text(message, exact=True)).to_be_visible()
    expect(page.get_by_text("Nothing was changed.", exact=True)).to_be_visible()
    page.unroute("**/api/transfers*")
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_role("heading", name=f"No branch transfers for {web.store}")).to_be_visible()


# --- a list that shrank past the current page asks the server for its last page (quick 261017-wzs finding 5) ---------

def test_a_page_past_the_last_asks_the_server_for_the_last_page_with_records(visitor, web):
    page = visitor.page
    switch_seat(page, None)
    items = [fake_row(f"SUS-9{n}", f"SUS-9{n}", web.store, web.store, n) for n in range(1, 4)]
    shown: dict = {"items": items, "asked": []}

    def serve(route):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
        shown["asked"].append(q.get("page", ["1"])[0])
        answer(route, shown["items"], 2, len(shown["items"]))

    page.route("**/api/transfers*", serve)
    page.goto("/#/transfers")
    expect(page.get_by_test_id("pager")).to_have_text("Page 1 of 2 · 3 items")
    page.get_by_role("button", name="Next page").click()
    expect(row(page, items[2]["action_id"])).to_be_visible()

    shown["items"] = items[:2]  # one transfer left the list elsewhere: page 2 is now past the last
    before = len(shown["asked"])
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(row(page, items[0]["action_id"])).to_be_visible()  # the page asked for page 1 by itself
    expect(row(page, items[1]["action_id"])).to_be_visible()
    assert shown["asked"][before] == "2" and shown["asked"][-1] == "1", shown["asked"]
