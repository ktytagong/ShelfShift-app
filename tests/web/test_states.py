"""Every page's states (06.1 D-12; UI-SPEC "Interaction states and feedback", UI Considerations loading S3–S17, error
S3–S16, stale S3, S4, S5, S11–S14): across every route of the page registry,
- a first load whose GET is held shows the page's own "Loading …" line (no spinner), and the content after it;
- a first load whose GET fails shows the error section: the page's title, the message, "Nothing was changed.",
  Try again and Back to Today, and Try again loads the page;
- after a good load, a refetch that fails (the tab becoming visible again) keeps the content on screen under the
  stale banner "Couldn't refresh. Showing what was loaded before.".

The routes come from the registry (web/src/pages/*.tsx); a registry page with no row in PAGES fails the coverage test,
so a new page is never left out. A request is held or failed with page.route on the page's own GET, so nothing is
mocked beyond the network failure itself; About has no request (it is static text) and is checked for loading only by
being there. Analytics (S15) has no refetch trigger: a later load replaces the page, so it has no stale state to show
(UI-SPEC lists stale for S3, S4, S5, S11–S14 only).
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

import pytest
from playwright.sync_api import Page, Route, expect

from tests.web.conftest import case_id_by_tag

pytestmark = pytest.mark.web

ROOT = pathlib.Path(__file__).resolve().parents[2]
STORY = "milk-story"  # SUS-19, the template's story decision
NOTHING_CHANGED = "Nothing was changed."
STALE = "Couldn't refresh. Showing what was loaded before."
CONTENT = "main#content"


@dataclass(frozen=True)
class P:
    loading: str  # the page's own loading line (UI-SPEC "Interaction states")
    failed: str  # the error section's title
    api: str  # a regex for the page's own first GET (one or more requests)
    stale: bool = True  # the page refetches on visibility and keeps its content under the banner


# One row per registry path; "/cases" stands for the stock decision's pattern route (/cases/{key}).
PAGES: dict[str, P | None] = {
    "/": P("Loading today's work…", "Today's work didn't load", r"/api/today(\?|$)"),
    "/board": P("Loading the Decision board…", "The Decision board didn't load", r"/api/cases(\?|$)"),
    "/cases": P("Loading the stock decision…", "This stock decision didn't load", r"/api/cases/[^/?]+$"),
    "/schedule": P("Loading the planned schedule…", "The planned schedule didn't load", r"/api/schedule(\?|$)"),
    "/tasks": P("Loading tasks…", "Tasks didn't load", r"/api/(requests|transfers)(\?|$)"),
    "/transfers": P("Loading branch transfers…", "Branch transfers didn't load", r"/api/transfers(\?|$)"),
    "/stock": P("Loading stock…", "Stock & lots didn't load", r"/api/lots(\?|$)"),
    "/analytics": P("Loading analytics…", "Analytics didn't load", r"/api/analytics(\?|$)", stale=False),
    "/reports": P("Loading reports…", "Reports didn't load", r"/api/reports/workspace(\?|$)"),
    "/forecasts": P("Loading forecasts…", "Forecasts didn't load", r"/api/forecasts(\?|$)"),
    "/policies": P("Loading policies…", "Policies didn't load", r"/api/settings(\?|$)"),
    "/about": None,  # static text, no request
}


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


def test_every_registry_page_has_its_states_row():
    assert sorted(registry_paths()) == sorted(PAGES), (registry_paths(), sorted(PAGES))


def url_of(page: Page, path: str) -> str:
    if path == "/cases":
        return "/#/cases/" + case_id_by_tag(page, STORY)
    return "/#" + path


def api_matcher(p: P):
    pattern = re.compile(p.api)
    return lambda url: pattern.search(url.split("#")[0]) is not None


def leave(page: Page) -> None:
    """Show another page first, so the route under test mounts fresh (a hash change, no reload)."""
    page.goto("/#/about")
    expect(page.get_by_role("heading", level=1, name="About this prototype")).to_be_visible()


def set_visibility(page: Page, state: str) -> None:
    page.evaluate("""(state) => {
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => state});
        document.dispatchEvent(new Event('visibilitychange'));
    }""", state)


LOADED = [(path, p) for path, p in PAGES.items() if p is not None]


@pytest.mark.parametrize(("path", "p"), LOADED, ids=[path for path, _ in LOADED])
def test_a_held_first_load_shows_the_pages_loading_line(visitor, path, p):
    page = visitor.page
    url = url_of(page, path)
    leave(page)
    held: list[Route] = []
    match = api_matcher(p)
    page.route(match, lambda route: held.append(route) if route.request.method == "GET" else route.continue_())
    page.goto(url)
    expect(page.get_by_text(p.loading, exact=True)).to_be_visible()
    expect(page.locator(CONTENT).locator("[role=progressbar], .animate-spin")).to_have_count(0)  # no spinner
    for route in held:
        route.continue_()  # before unroute, which would settle a pending route itself
    page.unroute(match)
    expect(page.get_by_text(p.loading, exact=True)).to_have_count(0)
    expect(page.get_by_text(p.failed, exact=True)).to_have_count(0)


@pytest.mark.parametrize(("path", "p"), LOADED, ids=[path for path, _ in LOADED])
def test_a_failed_first_load_shows_the_error_section_and_try_again_loads(visitor, path, p):
    page = visitor.page
    url = url_of(page, path)
    leave(page)
    match = api_matcher(p)
    page.route(match, lambda route: route.abort() if route.request.method == "GET" else route.continue_())
    page.goto(url)
    section = page.locator(CONTENT).get_by_role("alert").filter(has_text=p.failed)
    expect(section).to_be_visible()
    expect(section.get_by_role("heading", name=p.failed)).to_be_visible()
    expect(section.get_by_text(NOTHING_CHANGED, exact=True)).to_be_visible()
    expect(section.get_by_role("link", name="Back to Today")).to_have_attribute("href", "#/")
    page.unroute(match)
    section.get_by_role("button", name="Try again").click()
    expect(page.get_by_text(p.failed, exact=True)).to_have_count(0)
    expect(page.get_by_text(p.loading, exact=True)).to_have_count(0)


STALE_PAGES = [(path, p) for path, p in LOADED if p.stale]


@pytest.mark.parametrize(("path", "p"), STALE_PAGES, ids=[path for path, _ in STALE_PAGES])
def test_a_failed_refetch_keeps_the_content_under_the_stale_banner(visitor, path, p):
    page = visitor.page
    page.goto(url_of(page, path))
    heading = page.locator(CONTENT).get_by_role("heading", level=1)
    expect(heading).to_be_visible()
    expect(page.get_by_text(p.loading, exact=True)).to_have_count(0)
    title = heading.inner_text()
    match = api_matcher(p)
    page.route(match, lambda route: route.abort() if route.request.method == "GET" else route.continue_())
    set_visibility(page, "hidden")
    set_visibility(page, "visible")
    expect(page.get_by_text(STALE, exact=True)).to_be_visible()
    expect(page.locator(CONTENT).get_by_role("heading", level=1)).to_have_text(title)  # the content stays
    expect(page.get_by_text(p.failed, exact=True)).to_have_count(0)  # never the error section over loaded content
    page.unroute(match)
    set_visibility(page, "hidden")
    set_visibility(page, "visible")
    expect(page.get_by_text(STALE, exact=True)).to_have_count(0)
