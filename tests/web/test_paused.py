"""OPS-04 outage/recovery by browser interception only (11-CONTEXT D-03, D-04, D-14).
The shared database stays running. Values below are artificial response fixtures.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.web.conftest import new_context

pytestmark = pytest.mark.web
HEADING = "ShelfShift is temporarily unavailable."
PAUSED = {"ok": False, "db": "unreachable", "error": "paused", "message": HEADING,
          "contact": "help@example.org"}
READY = {"ok": True, "db": "ok", "vertex_ai": "disabled", "cloud_storage": "disabled",
         "data_bucket": "not_checked", "uploads_bucket": "disabled", "model_id": "", "checked_at": "2026-10-18T04:00:00Z"}


@pytest.fixture
def outage_page(browser, web):
    context = new_context(browser, web)
    try:
        yield context.new_page()
    finally:
        context.close()


def test_signed_out_first_load_shows_the_unavailable_screen_and_recovers(outage_page):
    page = outage_page
    page.route("**/api/session", lambda route: route.fulfill(status=503, json=PAUSED))
    page.goto("/#/")
    expect(page.get_by_role("heading", level=1, name=HEADING, exact=True)).to_be_visible()
    expect(page.get_by_text("Please try again in a moment.", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name=PAUSED["contact"])).to_have_attribute("href", "mailto:" + PAUSED["contact"])
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="About ShelfShift and the data")).to_have_attribute("href", "#/about")
    expect(page.get_by_test_id("dataset-mark")).to_be_visible()
    page.route("**/api/readyz", lambda route: route.fulfill(status=200, json=READY))
    page.unroute("**/api/session")
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_label("Store code")).to_be_visible()
    expect(page.get_by_role("heading", name=HEADING, exact=True)).to_have_count(0)
    assert page.url.endswith("/#/")

import re


def test_about_keeps_its_text_under_the_banner(outage_page):
    page = outage_page
    page.route("**/api/session", lambda route: route.fulfill(status=503, json=PAUSED))
    page.route("**/api/about", lambda route: route.fulfill(status=503, json=PAUSED))
    page.goto("/#/about")
    banner = page.get_by_role("status").filter(has_text=HEADING)
    expect(banner).to_be_visible()
    expect(banner.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", level=1, name="About this prototype")).to_be_visible()
    expect(page.get_by_role("heading", name="Not yet proven", exact=True)).to_be_visible()
    page.unroute("**/api/session")
    page.unroute("**/api/about")
    page.route("**/api/readyz", lambda route: route.fulfill(status=200, json=READY))
    banner.get_by_role("button", name="Try again", exact=True).click()
    expect(banner).to_have_count(0)
    expect(page.get_by_role("heading", name="About this prototype", exact=True)).to_be_visible()


def test_a_signed_in_route_switches_to_the_unavailable_screen(visitor):
    page = visitor.page
    matcher = re.compile(r".*/api/cases(?:\?.*)?$")
    page.route(matcher, lambda route: route.fulfill(status=503, json=PAUSED))
    page.goto("/#/board")
    expect(page.get_by_role("heading", level=1, name=HEADING, exact=True)).to_be_visible()
    expect(page.get_by_role("navigation", name="Main")).to_have_count(0)
    page.unroute(matcher)
    page.route("**/api/readyz", lambda route: route.fulfill(status=200, json=READY))
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.locator("main#content").get_by_role("heading", name="Decision board", exact=True)).to_be_visible()
    expect(page.locator("#content")).to_be_focused()


def test_empty_contact_shows_no_contact_sentence(outage_page):
    page = outage_page
    page.route("**/api/session", lambda route: route.fulfill(status=503, json={**PAUSED, "contact": ""}))
    page.goto("/#/")
    expect(page.get_by_role("heading", name=HEADING, exact=True)).to_be_visible()
    expect(page.get_by_text("Please try again in a moment.", exact=True)).to_be_visible()
    expect(page.locator('a[href^="mailto:"]')).to_have_count(0)
    assert "If the problem continues" not in page.locator("body").inner_text()


def test_still_down_and_network_failure(outage_page):
    page = outage_page
    page.route("**/api/session", lambda route: route.fulfill(status=503, json=PAUSED))
    page.route("**/api/readyz", lambda route: route.fulfill(status=503, json=PAUSED))
    page.goto("/#/")
    button = page.get_by_role("button", name="Try again", exact=True)
    button.click()
    live = page.locator('[aria-live="polite"]').filter(has_text="Still unavailable. Try again in a few minutes.")
    expect(live).to_be_visible()
    expect(button).to_be_focused()
    page.unroute("**/api/readyz")
    page.route("**/api/readyz", lambda route: route.abort())
    button.click()
    expect(page.locator('[aria-live="polite"]').filter(has_text="Could not reach ShelfShift. Check your connection and try again.")).to_be_visible()


@pytest.mark.parametrize(("status", "code", "message"), [
    (503, "db_busy", "The server is busy. Try again in a moment."),
    (500, "internal", "Something went wrong on our side."),
])
def test_busy_and_internal_are_not_unavailable(visitor, status, code, message):
    page = visitor.page
    matcher = re.compile(r".*/api/cases(?:\?.*)?$")
    page.route(matcher, lambda route: route.fulfill(status=status, json={"error": code, "message": message}))
    page.goto("/#/board")
    expect(page.get_by_text(message, exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name=HEADING, exact=True)).to_have_count(0)


def test_focus_order_and_widths(outage_page):
    page = outage_page
    contact = "a" * 48 + "@example.org"
    assert len(contact) == 60
    page.route("**/api/session", lambda route: route.fulfill(status=503, json={**PAUSED, "contact": contact}))
    page.goto("/#/")
    heading = page.get_by_role("heading", level=1, name=HEADING, exact=True)
    expect(heading).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name=contact)).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="About ShelfShift and the data")).to_be_focused()
    for width in (360, 390, 768, 1280):
        page.set_viewport_size({"width": width, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        assert page.get_by_role("link", name=contact).evaluate("el => el.getBoundingClientRect().right <= innerWidth"), width
    text = page.locator("body").inner_text().lower()
    assert "paused" not in text and "demo" not in text
    page.goto("/#/about")
    banner = page.get_by_role("status").filter(has_text=HEADING)
    expect(banner).to_be_visible()
    page.set_viewport_size({"width": 360, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert banner.get_by_role("link", name=contact).evaluate("el => el.getBoundingClientRect().right <= innerWidth")


def test_checking_keeps_the_about_link_usable(outage_page):
    page = outage_page
    pending = []
    page.route("**/api/session", lambda route: route.fulfill(status=503, json=PAUSED))
    page.route("**/api/readyz", lambda route: pending.append(route))
    page.goto("/#/")
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("button", name=re.compile("Checking"))).to_be_disabled()
    link = page.get_by_role("link", name="About ShelfShift and the data")
    expect(link).to_be_enabled()
    link.click()
    expect(page.get_by_role("heading", name="About this prototype", exact=True)).to_be_visible()
    for route in pending:
        route.fulfill(status=503, json=PAUSED)
