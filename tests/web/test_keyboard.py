"""UI-09 for the 06.1 shell (UI-SPEC "Accessibility contract"): the judge's path by keyboard alone, with a visible
focus ring at every stop, and status that never relies on colour.

The path test walks with Tab and Enter only (the store code and passcode are typed): no mouse call, no click and no
programmatic focus. It signs in on the card, goes to the Decision board from the nav, opens the story decision's
drawer and its "Open stock decision" link, takes ownership and sends the decision for approval; the state is read
from the API. At every focus stop the focused element matches :focus-visible and its computed outline is not "none"
and at least 2px wide.

The labels test checks on the working pages that every button with no visible text has an aria-label and every pill
has text (R-6). Expectations come from the API, never pasted numbers.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.web.conftest import Web, case_id_by_tag, case_json, new_context, session_json

pytestmark = pytest.mark.web

STORY = "milk-story"  # SUS-19, OPEN at Trigger in the template; its source is the default account

# What has focus now and its focus ring, read straight from the DOM: nothing is moved or focused by this script.
FOCUS = """() => {
  const el = document.activeElement;
  if (el === null || el === document.body || el === document.documentElement) return null;
  const cs = getComputedStyle(el);
  const labelled = el.labels && el.labels.length > 0 ? el.labels[0].innerText : '';
  const name = (el.getAttribute('aria-label') || labelled || (el.tagName === 'SELECT' ? '' : el.innerText) || '')
    .replace(/\\s+/g, ' ').trim();
  return {
    tag: el.tagName.toLowerCase(), name, testid: el.getAttribute('data-testid'), href: el.getAttribute('href'),
    nav: el.closest('nav[aria-label="Main"]') !== null, drawer: el.closest('aside[role="complementary"][aria-labelledby]') !== null,
    slot: el.closest('[data-testid="primary-slot"]') !== null,
    focusVisible: el.matches(':focus-visible'), outlineStyle: cs.outlineStyle,
    outlineWidth: parseFloat(cs.outlineWidth) || 0,
  };
}"""


def tab(page: Page, stops: list[dict]) -> dict | None:
    """Press Tab once and record the new focus stop (None when focus left the page's elements)."""
    page.keyboard.press("Tab")
    stop = page.evaluate(FOCUS)
    if stop is not None:
        stops.append(stop)
    return stop


def tab_to(page: Page, stops: list[dict], found, limit: int, what: str) -> dict:
    for _ in range(limit):
        stop = tab(page, stops)
        if stop is not None and found(stop):
            return stop
    pytest.fail(f"Tab never reached {what} in {limit} presses: {stops[-5:]}")


def assert_rings(stops: list[dict]) -> None:
    """Every stop shows a visible focus ring at least 2px wide (UI-SPEC: 2px navy, gold inside the navy sidebar)."""
    bad = [s for s in stops if not s["focusVisible"] or s["outlineStyle"] == "none" or s["outlineWidth"] < 2]
    assert not bad, f"focus stops without a visible 2px focus ring: {bad}"


def test_the_judges_path_by_keyboard_alone(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        code = page.get_by_label("Store code")
        expect(code).to_be_focused()  # the card opens on the store code (autofocus, not this script)
        stops: list[dict] = [page.evaluate(FOCUS)]

        # The sign-in card: type the store code, Tab to the passcode, type it, Enter signs in.
        page.keyboard.type(web.store)
        stop = tab(page, stops)
        assert stop is not None and stop["name"] == "Passcode", stop
        page.keyboard.type(web.passcode)
        page.keyboard.press("Enter")
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        expect(page.locator("main#content").get_by_role("heading", level=1, name="Today")).to_be_visible()
        assert session_json(page)["account"] == web.store

        # The nav: Tab to Decision board and press Enter.
        tab_to(page, stops, lambda s: s["nav"] and s["href"] == "#/board", 60, "the nav's Decision board link")
        page.keyboard.press("Enter")
        expect(page.locator("main#content").get_by_role("heading", level=1, name="Decision board")).to_be_visible()

        # The story decision's card opens its drawer; its "Open stock decision" link opens the page.
        case_id = case_id_by_tag(page, STORY)
        key = case_json(page, case_id)["key"]
        tab_to(page, stops, lambda s: s["testid"] == f"decision-card-{key}", 200, f"the {key} card")
        page.keyboard.press("Enter")
        drawer = page.locator("aside[role='complementary'][aria-labelledby]")
        expect(drawer).to_be_visible()
        tab_to(page, stops, lambda s: s["drawer"] and s["name"] == "Open stock decision", 60,
               "the drawer's Open stock decision link")
        page.keyboard.press("Enter")
        expect(page.get_by_test_id("step-panel")).to_be_visible()

        # Take ownership, then Send for approval, both from the summary's action slot.
        tab_to(page, stops, lambda s: s["slot"] and s["name"] == "Take ownership", 100, "Take ownership")
        page.keyboard.press("Enter")
        expect(page.get_by_test_id("primary-slot").get_by_role("button", name="Send for approval")).to_be_visible()
        assert case_json(page, case_id)["owner"] is not None, "Take ownership recorded no owner"
        tab_to(page, stops, lambda s: s["slot"] and s["name"] == "Send for approval", 200, "Send for approval")
        page.keyboard.press("Enter")
        expect(page.get_by_test_id("primary-slot").get_by_role("button", name="Send for approval")).to_have_count(0)
        assert case_json(page, case_id)["state"] == "PENDING_APPROVAL"

        assert len(stops) > 10, stops
        assert_rings(stops)
    finally:
        context.close()


# Every visible button with no visible text and no aria-label; every pill (ui.Pill: span.inline-flex.h-5) with no text.
UNLABELLED = """() => [...document.querySelectorAll('button')]
  .filter((b) => b.offsetParent !== null && b.innerText.trim() === '' && !(b.getAttribute('aria-label') || '').trim())
  .map((b) => b.outerHTML.slice(0, 120))"""
EMPTY_PILLS = """() => [...document.querySelectorAll('span.inline-flex.h-5')]
  .filter((p) => p.textContent.trim() === '').map((p) => p.outerHTML.slice(0, 120))"""


def test_icon_controls_are_labelled_and_status_has_text(visitor):
    page = visitor.page
    case_id = case_id_by_tag(page, STORY)
    for route in ("/#/", "/#/board", "/#/schedule", "/#/tasks", "/#/transfers", "/#/stock", f"/#/cases/{case_id}",
                  f"/#/cases/{case_id}?step=options", "/#/policies", "/#/analytics", "/#/forecasts", "/#/reports", "/#/about"):
        page.goto(route)
        main = page.locator("main#content")
        expect(main.get_by_role("heading", level=1).first).to_be_visible()
        expect(main.locator("[role=status]").filter(has_text="Loading")).to_have_count(0)
        assert page.evaluate(UNLABELLED) == [], f"{route}: icon-only buttons without an aria-label"
        assert page.evaluate(EMPTY_PILLS) == [], f"{route}: pills without text"


def test_secondary_pages_by_keyboard(visitor):
    from tests.web.test_analytics import answer
    page = visitor.page
    answer(page)  # chart-bearing public-shaped response on the synthetic fixture too
    page.goto('/#/')
    expect(page.locator('main#content').get_by_role('heading', name='Today', exact=True)).to_be_visible()
    stops = []
    for path, title in [('/board','Decision board'),('/tasks','Tasks'),('/schedule','Planned schedule'),
                        ('/transfers','Branch transfers'),('/stock','Stock & lots'),
                        ('/analytics','Analytics'),('/forecasts','Forecasts'),('/reports','Reports'),('/policies','Policies')]:
        tab_to(page, stops, lambda s: s['nav'] and s['href']=='#'+path, 250, title+' navigation')
        page.keyboard.press('Enter')
        expect(page.locator('main#content').get_by_role('heading', name=title, exact=True)).to_be_visible()
        if path == '/board':
            tab_to(page, stops, lambda s: s['name']=='Table', 150, 'board table view')
            page.keyboard.press('Space')
            expect(page.locator('main#content table')).to_be_visible()
        if path == '/stock':
            tab_to(page, stops, lambda s: s['name']=='View lot', 200, 'View lot')
            page.keyboard.press('Enter')
            drawer = page.locator("aside[role='complementary'][aria-labelledby]")
            expect(drawer).to_be_visible()
            stops.append(page.evaluate(FOCUS))
            page.keyboard.press('Escape')
            expect(drawer).to_have_count(0)
            assert page.evaluate(FOCUS)['name'] == 'View lot'
            stops.append(page.evaluate(FOCUS))
        if path == '/analytics':
            for _ in range(4):
                tab_to(page, stops, lambda s: s['name']=='Show as table', 250, 'chart table toggle')
                page.keyboard.press('Space')
                assert page.locator('main#content button[aria-pressed=true]').count() >= 1
            expect(page.locator('main#content canvas')).to_have_count(0)
        if path == '/reports':
            tab_to(page, stops, lambda s:s['name']=='This workspace', 150, 'Reports workspace tab')
            page.keyboard.press('ArrowRight'); stops.append(page.evaluate(FOCUS))
            assert stops[-1]['name']=='Benchmark'
            expect(page.locator('main#content')).to_contain_text('Evaluation not yet available')
            page.keyboard.press('ArrowLeft'); stops.append(page.evaluate(FOCUS))
            assert stops[-1]['name']=='This workspace'
            expect(page.locator('main#content')).to_contain_text('Workspace activity')
        if path == '/forecasts' and page.get_by_role('button',name='Show as table',exact=True).count():
            tab_to(page, stops, lambda s:s['name']=='Show as table', 150, 'forecast table toggle')
            page.keyboard.press('Space')
            expect(page.get_by_role('button',name='Show as chart',exact=True)).to_have_attribute('aria-pressed','true')
    assert_rings(stops)


def test_about_jump_links_by_keyboard(visitor):
    page = visitor.page
    page.goto('/#/about')
    expect(page.get_by_role('heading', name='About this prototype', exact=True)).to_be_visible()
    links = [('versions','Versions'),('checks','Checks and evaluations'),('ai-evaluation','AI evaluation'),
             ('weekcheck','Synthetic 5-seed engineering check'),('real-now','Real now'),('simulated','Simulated'),
             ('not-yet-proven','Not yet proven'),('labels','Labels'),('dataset','The dataset')]
    nav = page.get_by_role('navigation', name='On this page')
    assert nav.get_by_role('link').all_inner_texts() == [label for _,label in links]
    stops=[]
    for id, label in links:
        tab_to(page, stops, lambda s:s['href']=='#/about?s='+id, 300, label+' jump link')
        page.keyboard.press('Enter')
        expect(page.locator('h2#'+id)).to_be_focused()
        stops.append(page.evaluate(FOCUS))
    assert_rings(stops)


def test_unavailable_screen_by_keyboard(browser, web):
    from tests.web.test_paused import PAUSED, READY, HEADING
    context = new_context(browser, web)
    try:
        page=context.new_page()
        page.route('**/api/session', lambda route:route.fulfill(status=503,json=PAUSED))
        checks=[]
        def ready(route):
            checks.append(True)
            route.fulfill(status=200,json=READY)
        page.route('**/api/readyz', ready)
        page.goto('/#/')
        expect(page.get_by_role('heading',name=HEADING,exact=True)).to_be_focused()
        assert page.evaluate(UNLABELLED) == []
        assert page.evaluate(EMPTY_PILLS) == []
        stops=[]
        assert tab(page,stops)['name'] == PAUSED['contact']
        assert tab(page,stops)['name'] == 'Try again'
        assert tab(page,stops)['name'] == 'About ShelfShift and the data'
        page.keyboard.press('Shift+Tab'); stops.append(page.evaluate(FOCUS))
        page.unroute('**/api/session')
        page.keyboard.press('Enter')
        expect(page.get_by_label('Store code')).to_be_visible()
        assert checks == [True]
        assert_rings(stops)
    finally:
        context.close()
