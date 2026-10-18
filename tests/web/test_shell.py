"""PROD-03 (06.1 D-01, D-03, D-06..D-09, D-59, D-63, D-66; UI-SPEC "Shell", "Routes"): the product shell of sketch
001 E. A navy sidebar with the logo, the mark and the nav from the page registry; the page header's context line; no
demo machinery. Expected text is built from the session JSON (the label and scenario date are reference data), never
typed here, so the same test reads right on the public dataset and on the synthetic test set.
"""
from __future__ import annotations

import datetime as dt
import urllib.parse

import pytest
from playwright.sync_api import expect

from tests.web.conftest import fill_sign_in, new_context, session_json, sign_in

pytestmark = pytest.mark.web

ROLE = {"manager": "Store manager", "hq": "HQ planner"}
DATE_PART = " · Scenario date:"


def mark_date(iso: str) -> str:
    """format.markDate: "Sat 17 Oct 2026" from an ISO date (the UTC calendar)."""
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b} {d.year}"


def mark(s: dict) -> str:
    """copy.datasetMark: the session's label with "{date}" filled from data_as_of; with none, that part left out."""
    label = s["label"]
    if "{date}" not in label:
        return label
    if s["data_as_of"] is None:
        return " · ".join(p for p in label.split(" · ") if "{date}" not in p)
    return label.replace("{date}", mark_date(s["data_as_of"]))


def mark_lines(s: dict) -> list[str]:
    """vocab.markLines: the mark split before " · Scenario date:" into the dataset line and the date line."""
    m = mark(s)
    at = m.find(DATE_PART)
    return [m] if at < 0 else [m[:at], m[at + len(" · "):]]


def context_line(s: dict) -> str:
    """vocab.contextLine: "{account} · Scenario date: {date} · {role}"; without a scenario date, that part is left out."""
    date = None if s["data_as_of"] is None else f"Scenario date: {mark_date(s['data_as_of'])}"
    return " · ".join(x for x in (s["account"], date, ROLE[s["role"]]) if x)


def test_the_navy_sidebar_lists_the_board_from_the_registry(visitor):
    page = visitor.page
    page.goto("/#/board")
    nav = page.get_by_role("navigation", name="Main")
    board = nav.get_by_role("link", name="Decision board")
    expect(board).to_be_visible()
    expect(board).to_have_attribute("aria-current", "page")
    for gone in ("Transport", "Next day", "Reset", "Start here"):
        expect(nav.get_by_role("link", name=gone)).to_have_count(0)


def test_the_mark_and_the_context_line_come_from_the_session(visitor):
    page = visitor.page
    page.goto("/#/board")
    s = session_json(page)
    side = page.locator("aside")
    for line in mark_lines(s):
        expect(side.get_by_text(line, exact=True)).to_be_visible()
    if s["data_as_of"] is not None:
        expect(side.get_by_text(f"Scenario date: {mark_date(s['data_as_of'])}", exact=True)).to_be_visible()
    expect(page.get_by_test_id("context-line")).to_have_text(context_line(s))
    expect(page.locator("body")).not_to_contain_text("Data as of")


def test_the_root_route_is_today(visitor):
    page = visitor.page
    page.goto("/#/board")
    page.goto("/#/")
    expect(page.get_by_role("heading", level=1, name="Today")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name="Today")) \
        .to_have_attribute("aria-current", "page")
    assert page.url.endswith("#/")  # Today claims "/" itself (06.1-20): no redirect


# --- every page in the registry, the redirects and Page not found (Task 2) -----------------------------------------

# The registry's nav in its final order (sections "", Movement, Stock, Admin, each by `order`; 06.1-17..-29).
NAV_ORDER = ["Today", "Decision board", "Schedule", "Tasks", "Branch transfers", "Stock & lots", "Analytics",
             "Forecasts", "Reports", "Policies", "About"]

def nav_labels(page) -> list[str]:
    """The Main nav's links in order, each by its visible label (a count's words are visually hidden)."""
    return page.get_by_role("navigation", name="Main").get_by_role("link").evaluate_all(
        "links => links.map(a => a.querySelector('span.min-w-0').textContent)")


def test_the_nav_lists_the_working_pages_in_order(visitor):
    page = visitor.page
    page.goto("/#/board")
    assert nav_labels(page) == NAV_ORDER
    nav = page.get_by_role("navigation", name="Main")
    for section in ("Movement", "Stock", "Admin"):
        expect(nav.get_by_text(section, exact=True)).to_be_visible()
    expect(nav.get_by_role("link", name="Transport")).to_have_count(0)  # transport lives in each decision (D-66)
    for label in nav_labels(page):
        nav.get_by_role("link", name=label).click()
        expect(nav.get_by_role("link", name=label)).to_have_attribute("aria-current", "page")
        expect(page.get_by_text("Page not found.")).to_have_count(0)


def test_the_old_routes_redirect_and_an_unknown_route_is_not_found(visitor):
    page = visitor.page
    for old, new in (("#/requests", "#/tasks"), ("#/settings", "#/policies"), ("#/list", "#/board?view=table")):
        page.goto("/" + old)
        page.wait_for_url("**/" + new)
    page.goto("/#/nope")
    expect(page.get_by_text("Page not found.", exact=True)).to_be_visible()
    page.get_by_role("link", name="Back to Today").click()
    page.wait_for_url("**/#/")
    expect(page.get_by_role("heading", level=1, name="Today")).to_be_visible()


def test_a_case_route_shows_its_stock_decision(visitor):
    page = visitor.page
    r = page.request.get("/api/cases")
    assert r.status == 200, r.status
    card = r.json()["cases"][0]
    page.goto("/#/cases/" + urllib.parse.quote(card["case_id"], safe=""))
    expect(page.get_by_role("heading", level=1)).to_contain_text(card["product"])
    expect(page.get_by_text("Page not found.")).to_have_count(0)


def test_about_opens_while_signed_out(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/#/about")
        expect(page.get_by_role("heading", level=1, name="About this prototype")).to_be_visible()
        expect(page.get_by_role("navigation", name="Main")).to_have_count(0)
        expect(page.get_by_role("link", name="Sign in", exact=True)).to_be_visible()
        expect(page.locator("body")).not_to_contain_text("Back to the demo")
    finally:
        context.close()


# --- the rail, the phone top bar and nav sheet, the profile menu and Sign out, no demo machinery (Task 3) -----------

NAVY_800 = "rgb(19, 32, 122)"  # the focus ring on light surfaces (UI-SPEC "Accessibility contract")
GOLD_500 = "rgb(212, 190, 133)"  # the focus ring inside .on-navy
SIGNED_OUT = "You signed out. Your workspace is kept in this browser."
DEMO_STRINGS = ("Next day", "Reset my sandbox", "Acting as", "Start here", "Wait until", "Your sandbox")
WIDTHS = (360, 390, 768, 1280)


def aside_width(page) -> float:
    return page.locator("aside").bounding_box()["width"]


def no_side_scroll(page) -> bool:
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


def test_the_sidebar_collapses_to_the_rail_and_stays_collapsed(visitor):
    page = visitor.page
    page.goto("/#/board")
    s = session_json(page)
    expect(page.get_by_role("button", name="Collapse sidebar")).to_have_attribute("aria-expanded", "true")
    page.get_by_role("button", name="Collapse sidebar").click()
    expect(page.get_by_role("button", name="Expand sidebar")).to_have_attribute("aria-expanded", "false")
    page.wait_for_function("() => document.querySelector('aside').getBoundingClientRect().width === 72")
    assert page.evaluate("() => localStorage.getItem('ss-shell')") == "1"
    # The mark leaves the rail for the strip above the page, outside the scrolling area (quick 261017-wzs finding 10).
    expect(page.locator("aside").get_by_text(mark_lines(s)[0], exact=True)).to_have_count(0)
    expect(page.get_by_test_id("mark-strip").get_by_text(mark_lines(s)[0], exact=True)).to_be_visible()

    page.reload()
    expect(page.get_by_role("button", name="Expand sidebar")).to_be_visible()
    assert aside_width(page) == 72

    tasks = page.get_by_role("navigation", name="Main").get_by_role("link", name="Tasks")
    tasks.focus()
    expect(page.get_by_role("tooltip").filter(has_text="Tasks")).to_be_visible()

    page.get_by_role("button", name="Expand sidebar").click()
    page.wait_for_function("() => document.querySelector('aside').getBoundingClientRect().width === 236")
    expect(page.get_by_role("button", name="Collapse sidebar")).to_be_visible()


def test_the_rail_is_the_default_between_768_and_1023(visitor):
    page = visitor.page
    page.set_viewport_size({"width": 800, "height": 900})
    page.goto("/#/board")
    expect(page.get_by_role("button", name="Expand sidebar")).to_be_visible()
    page.wait_for_function("() => document.querySelector('aside').getBoundingClientRect().width === 72")
    page.reload()  # a fresh load at this width starts on the rail, with no choice stored
    expect(page.get_by_role("button", name="Expand sidebar")).to_be_visible()
    assert aside_width(page) == 72
    assert page.evaluate("() => localStorage.getItem('ss-shell')") is None


def test_the_phone_top_bar_and_nav_sheet(visitor):
    page = visitor.page
    s = session_json(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/#/board")
    expect(page.locator("aside")).to_have_count(0)
    expect(page.get_by_test_id("mark-strip").get_by_text(mark_lines(s)[0], exact=True)).to_be_visible()
    menu = page.get_by_role("button", name="Open menu")
    expect(menu).to_have_attribute("aria-expanded", "false")
    assert menu.bounding_box()["height"] >= 44
    menu.click()
    sheet = page.get_by_role("dialog", name="Main menu")
    expect(sheet).to_be_visible()
    expect(sheet.get_by_role("navigation", name="Main").get_by_role("link", name="Decision board")).to_be_visible()
    page.keyboard.press("Escape")
    expect(sheet).to_have_count(0)
    expect(menu).to_be_focused()

    menu.click()
    page.get_by_role("dialog", name="Main menu").get_by_role("link", name="Tasks").click()
    page.wait_for_url("**/#/tasks")
    expect(page.get_by_role("dialog", name="Main menu")).to_have_count(0)


def test_no_page_scrolls_sideways(visitor):
    page = visitor.page
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        for route in ("#/board", "#/tasks", "#/policies", "#/about"):
            page.goto("/" + route)
            expect(page.locator("main#content")).to_be_visible()
            assert no_side_scroll(page), (width, route)


def test_the_profile_menu_and_sign_out(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        s = sign_in(page, web)
        account = s["account"]
        button = page.get_by_role("button", name=f"Account: {account}, Store manager")
        expect(button).to_have_attribute("aria-haspopup", "menu")
        button.click()
        menu = page.get_by_role("menu")
        expect(menu.get_by_role("menuitem")).to_have_text(["About ShelfShift and the data", "Sign out"])
        expect(page.get_by_text(f"{account} manager", exact=True)).to_be_visible()
        expect(page.get_by_text(f"Branch: {account} · can approve outgoing stock, accept incoming stock, "
                                "request stock checks", exact=True)).to_be_visible()
        page.keyboard.press("Escape")
        expect(menu).to_have_count(0)
        expect(button).to_be_focused()

        button.click()
        page.get_by_role("menuitem", name="About ShelfShift and the data").click()
        page.wait_for_url("**/#/about")

        page.get_by_role("button", name=f"Account: {account}, Store manager").click()
        page.get_by_role("menuitem", name="Sign out").click()
        expect(page.get_by_text(SIGNED_OUT, exact=True)).to_be_visible()
        expect(page.get_by_role("navigation", name="Main")).to_have_count(0)
        expect(page.get_by_label("Store code")).to_have_value("")
        expect(page.locator("body")).not_to_contain_text(account)

        fill_sign_in(page, web)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        assert session_json(page)["run_id"] == s["run_id"]
    finally:
        context.close()


def test_no_demo_machinery_and_the_navy_focus_ring(visitor):
    page = visitor.page
    page.goto("/#/board")
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()
    html = page.content()
    assert [x for x in DEMO_STRINGS if x in html] == []
    expect(page.get_by_role("region", name="Demo controls")).to_have_count(0)

    page.goto("/#/nope")
    page.get_by_role("link", name="Back to Today").focus()
    assert page.evaluate("() => getComputedStyle(document.activeElement).outlineColor") == NAVY_800
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Decision board").focus()
    page.keyboard.press("Tab")  # keyboard focus inside the navy sidebar: the next link in the registry's order
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name="Schedule")).to_be_focused()
    assert page.evaluate("() => getComputedStyle(document.activeElement).outlineColor") == GOLD_500


# --- the mark stays on screen; drawers start below the phone bar and the strip (quick 261017-wzs findings 7, 10) ------

STRIP_UNCOVERED = """() => {
  const el = document.querySelector('[data-testid="mark-strip"]');
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return hit !== null && el.contains(hit);
}"""


def story_lot(page) -> str:
    r = page.request.get("/api/lots")
    assert r.status == 200, r.status
    return next(x for x in r.json()["items"] if x["case_key"] is not None)["lot_id"]


def assert_strip_on_screen(page, s: dict) -> dict:
    """The strip shows both mark lines, lies inside the viewport and is not covered; its box."""
    strip = page.get_by_test_id("mark-strip")
    expect(strip).to_be_visible()
    for line in mark_lines(s):
        expect(strip.get_by_text(line, exact=True)).to_be_visible()
    box = strip.bounding_box()
    view = page.viewport_size
    assert box["y"] >= 0 and box["y"] + box["height"] <= view["height"], box
    assert page.evaluate(STRIP_UNCOVERED)
    return box


@pytest.mark.parametrize("size", [{"width": 390, "height": 844}, {"width": 800, "height": 900}],
                         ids=["phone", "rail"])
def test_the_mark_stays_on_screen_and_drawers_start_below_it(visitor, size):
    page = visitor.page
    s = session_json(page)
    page.set_viewport_size(size)
    page.goto("/#/stock")
    expect(page.get_by_role("heading", level=1, name="Stock & lots")).to_be_visible()
    page.evaluate("() => { const m = document.querySelector('main#content'); m.scrollTop = m.scrollHeight; }")
    page.wait_for_timeout(100)
    assert_strip_on_screen(page, s)

    lot = story_lot(page)
    page.goto(f"/#/stock?lot={urllib.parse.quote(lot)}")
    drawer = page.get_by_role("complementary", name=f"Lot {lot}")
    expect(drawer.get_by_test_id("lot-history")).to_be_visible()
    strip = assert_strip_on_screen(page, s)
    assert drawer.bounding_box()["y"] >= strip["y"] + strip["height"] - 0.5, (drawer.bounding_box(), strip)
    close = drawer.get_by_role("button", name="Close")
    close.click(trial=True, timeout=5000)  # nothing covers it: the tap reaches Close
    close.click()
    expect(drawer).to_have_count(0)


def test_the_expanded_sidebar_keeps_the_mark_and_a_full_height_drawer(visitor):
    page = visitor.page
    s = session_json(page)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto("/#/stock")
    expect(page.get_by_role("button", name="Collapse sidebar")).to_be_visible()
    expect(page.get_by_test_id("mark-strip")).to_have_count(0)
    for line in mark_lines(s):
        expect(page.locator("aside").get_by_text(line, exact=True)).to_be_visible()
    lot = story_lot(page)
    page.goto(f"/#/stock?lot={urllib.parse.quote(lot)}")
    box = page.get_by_role("complementary", name=f"Lot {lot}").bounding_box()
    assert box["y"] == 0 and box["height"] == 900, box


def test_crossing_the_phone_breakpoint_keeps_the_page_and_its_draft(visitor):
    """The page under the shell is the same element on the phone and beside the rail, so turning a phone keeps what
    the person typed (quick 261017-wzs, Codex check of the mark strip)."""
    page = visitor.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/#/stock")
    product = page.get_by_label("Product code")
    product.fill("DRAFT-1")
    page.set_viewport_size({"width": 844, "height": 390})
    expect(page.get_by_role("button", name="Expand sidebar")).to_be_visible()
    expect(page.get_by_label("Product code")).to_have_value("DRAFT-1")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name="Open menu")).to_be_visible()
    expect(page.get_by_label("Product code")).to_have_value("DRAFT-1")
