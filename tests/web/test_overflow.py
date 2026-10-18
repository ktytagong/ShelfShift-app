"""No page scrolls sideways (06.1 D-37, D-59, PROD-20; UI-SPEC "Responsive and overflow contract"): at 360, 390, 768
and 1280 px, `document.documentElement.scrollWidth <= window.innerWidth` on
- every route of the page registry (web/src/pages/*.tsx), for a store manager and for HQ;
- the story decision at each of its nine steps (?step=);
- each page that has a drawer, with that drawer open (Today, the Decision board, the planned schedule, Branch
  transfers, Stock & lots).
Wide content scrolls only inside its own box. A failure names the route and the width, never data.

For the end-of-phase review (D-10, R-1) the route test saves a full-page screenshot per route at 1280×800 and 390×844
into test-results/overflow/ (git-ignored, never committed).

The nav never lists a Transport link (D-66): transport lives inside each stock decision, and no registry page claims
a /transport route.

Runs on the synthetic test set. Analytics and Forecasts show the server's "covers the public sales dataset" sentence
here; their charts and tables at four widths are checked on the public set by 06.1-31 (and by test_analytics.py and
test_forecasts.py with their contract-shaped fixtures).
"""
from __future__ import annotations

import pathlib
import re
import urllib.parse

import pytest
from playwright.sync_api import Browser, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests.web.conftest import HQ, case_id_by_tag, new_context, sign_in
from tests.web.test_transfers import prepare_story

pytestmark = pytest.mark.web

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHOTS = ROOT / "test-results" / "overflow"
WIDTHS = (360, 390, 768, 1280)
SHOT_SIZES = {1280: 800, 390: 844}  # the review sizes (UI-SPEC R-1); other widths use 844 high
STORY = "milk-story"
STEPS = ("trigger", "context", "options", "compare", "approve", "commit", "execute", "verify", "archive")
FITS = "() => document.documentElement.scrollWidth <= window.innerWidth"
WIDTHS_NOW = "() => [document.documentElement.scrollWidth, window.innerWidth]"


def registry_paths() -> list[str]:
    """Every page module's route: its exact `path`, or "/cases" for the stock decision's `match`."""
    out = []
    for f in sorted((ROOT / "web" / "src" / "pages").glob("*.tsx")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r'^\s*path: "([^"]+)"', text, re.M)
        if m:
            out.append(m.group(1))
        elif re.search(r"^\s*match: ", text, re.M):
            out.append("/cases")
    return out


def settled(page: Page) -> None:
    """The page's own content is on screen: its h1, and no "Loading …" line left."""
    main = page.locator("main#content")
    expect(main.get_by_role("heading", level=1).first).to_be_visible()
    expect(main.locator("[role=status]").filter(has_text=re.compile(r"^Loading"))).to_have_count(0)


def fits(page: Page, where: str, width: int, bad: list[str]) -> None:
    """No sideways scroll once the layout has settled (the sidebar's width transition takes 200 ms; 06.1-29)."""
    try:
        page.wait_for_function(FITS, timeout=5000)
    except PlaywrightTimeout:
        doc, view = page.evaluate(WIDTHS_NOW)
        bad.append(f"{where} at {width}px: document {doc}px wide in a {view}px window")


def shot(page: Page, name: str, width: int) -> None:
    if width in SHOT_SIZES:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{width}-{name}.png"), full_page=True)


def route_url(page: Page, path: str) -> str:
    return "/#/cases/" + case_id_by_tag(page, STORY) if path == "/cases" else "/#" + path


def name_of(path: str) -> str:
    return "today" if path == "/" else path.strip("/").replace("/", "-")


@pytest.mark.parametrize("width", WIDTHS)
def test_no_registry_route_scrolls_sideways_for_a_manager(visitor, width):
    page = visitor.page
    page.set_viewport_size({"width": width, "height": SHOT_SIZES.get(width, 844)})
    bad: list[str] = []
    for path in registry_paths():
        page.goto(route_url(page, path))
        settled(page)
        fits(page, path, width, bad)
        shot(page, name_of(path), width)
    assert bad == [], bad


@pytest.mark.parametrize("width", WIDTHS)
def test_no_registry_route_scrolls_sideways_for_hq(browser: Browser, web, width):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        page.set_viewport_size({"width": width, "height": SHOT_SIZES.get(width, 844)})
        bad: list[str] = []
        for path in registry_paths():
            if path == "/cases":
                continue  # the stock decision reads the same for HQ; the manager's pass covers it
            page.goto("/#" + path)
            settled(page)
            fits(page, f"HQ {path}", width, bad)
            shot(page, "hq-" + name_of(path), width)
        assert bad == [], bad
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_the_story_decision_fits_at_every_step(visitor, width):
    page = visitor.page
    page.set_viewport_size({"width": width, "height": 844})
    case_id = urllib.parse.quote(case_id_by_tag(page, STORY), safe="")
    bad: list[str] = []
    for step in STEPS:
        page.goto(f"/#/cases/{case_id}?step={step}")
        settled(page)
        expect(page.get_by_test_id("step-panel")).to_have_attribute("data-step", step.upper())
        fits(page, f"stock decision ?step={step}", width, bad)
    assert bad == [], bad


def open_drawer(page: Page, path: str):
    """Open the page's drawer on its first row, card or marker; returns the drawer."""
    main = page.locator("main#content")
    if path == "/":
        main.get_by_test_id("lane").locator("button").first.click()
    elif path == "/board":
        main.locator("[data-testid^='decision-card-']").first.click()
    elif path == "/schedule":
        main.get_by_test_id("schedule-row").first.click()
    elif path == "/transfers":
        main.locator("[data-testid^='transfer-row-']").first.get_by_role("button").first.click()
    drawer = page.locator("aside[role='complementary'][aria-labelledby]")
    expect(drawer).to_be_visible()
    return drawer


@pytest.mark.parametrize("width", WIDTHS)
def test_no_page_scrolls_sideways_with_its_drawer_open(visitor, width):
    page = visitor.page
    story = prepare_story(page)  # a prepared transfer, so Today, the schedule and Branch transfers have rows
    page.set_viewport_size({"width": width, "height": 844})
    bad: list[str] = []
    for path in ("/", "/board", "/schedule", "/transfers"):
        page.goto("/#" + path)
        settled(page)
        open_drawer(page, path)
        fits(page, f"{path} with its drawer open", width, bad)
    page.goto(f"/#/stock?lot={urllib.parse.quote(story['action']['lot_id'], safe='')}")
    settled(page)
    expect(page.get_by_role("complementary", name=f"Lot {story['action']['lot_id']}")).to_be_visible()
    fits(page, "/stock with its drawer open", width, bad)
    assert bad == [], bad


def test_no_transport_route_and_no_transport_link_for_a_manager_or_hq(browser: Browser, web):
    assert not any("transport" in p for p in registry_paths()), registry_paths()
    for account in (None, HQ):
        context = new_context(browser, web)
        try:
            page = context.new_page()
            sign_in(page, web, account)
            nav = page.get_by_role("navigation", name="Main")
            expect(nav.get_by_role("link").first).to_be_visible()
            expect(nav.get_by_role("link", name=re.compile("transport", re.I))).to_have_count(0)
        finally:
            context.close()
