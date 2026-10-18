"""The public dataset's screen states, in the browser, on the product screens (05.2 DATA-14, DATA-16, DATA-17;
05.2-CONTEXT D-02, D-04, D-18, D-24..D-26, D-31, D-38; 06.1 D-61, D-63; UI-SPEC A-33..A-40).

Runs with SS_WEB_DATASET=public_tiny (see test_public_journey). Every case is found by its catalogue tag or read from the
board pages; every expectation comes from the API. The tests assert beats, labels and tags, never a quantity or an
amount (04 D-14).

The tiny set's open-day catalogue evaluates every flag it has (its budget is larger than the flag count and not every
kind occurs), so no "Needs evaluation" item is left in the browser suite's template. The flagged-item test therefore
builds a second template of the same set with the catalogue's budget at 0 (as tests/api/test_public_template.py does),
makes it ACTIVE for that test only, and puts the suite's template back at teardown.

06.1-31 moved 05.2-22's checks onto the product: the Decision board's Branch filter (HQ only) and pager in place of the
Phase 5 board's Store box and scope line, the stock decision's Options step in place of the case tabs, Tasks in place
of Requests (D-61), and the sign-in card in place of the passcode gate (D-47).
"""
from __future__ import annotations

import re
import time
import urllib.parse

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.web.conftest import Web, case_json, db_tx, new_context, public_only, session_json, sign_in
from tests.web.test_public_journey import (board_cards, board_pages, board_region, card_by_tag, evaluated_text,
                                           open_board, open_by_route, seat, step_panel)

pytestmark = [pytest.mark.web, public_only]

OLD_LABEL = "Synthetic data"  # the synthetic set's label, which no public screen shows (D-03, D-04)
UNMAPPED = "Transfers unavailable: location or route data missing"  # D-31's sentence (UI-SPEC A-40)
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


@pytest.fixture
def flags_template(web_template):
    """A template of the same public set whose open-day catalogue evaluates nothing (budget 0), ACTIVE for one test:
    every flag of the open day stays "Needs evaluation". At teardown the suite's template is ACTIVE again and this one
    RETIRED, in one transaction (the next schema rebuild drops its rows)."""
    from api import catalogue, db, sandbox
    with db_tx() as conn:
        dv = db.one(conn, "SELECT dataset_version FROM demo_runs WHERE run_id = :r", r=web_template)["dataset_version"]
    real = catalogue.rule
    catalogue.rule = lambda ref: {**real(ref), "budget": 0}
    try:
        run_id = sandbox.build_template(dv)
    finally:
        catalogue.rule = real
    try:
        yield run_id
    finally:
        with db_tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :p", p=run_id)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=web_template)


def board_keys(page: Page) -> list[str]:
    """The keys of the cards the board shows (flag cards and case cards alike)."""
    return [x.removeprefix("decision-card-") for x in
            board_cards(page).evaluate_all("els => els.map(e => e.getAttribute('data-testid'))")]


SMALL_PAGE = 2  # a page size the server accepts, so the tiny set's few HQ items (4 on the fixture) span pages


def is_board_read(url: str) -> bool:
    return urllib.parse.urlsplit(url).path == "/api/cases"


def with_page_size(url: str, size: int) -> str:
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query)) | {"page_size": str(size)}
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))


def branch_box(page: Page):
    """HQ's Branch filter on the Decision board (a store code, sent to the server when committed)."""
    return page.locator("main#content").get_by_label("Branch", exact=True)


def options_rows(page: Page):
    return step_panel(page).get_by_test_id("options-scroll").locator("tbody tr")


def test_a_flagged_item_shows_progress_then_its_case(flags_template, open_visitor):
    page = open_visitor().page
    seat(page, None)
    flags = [c for c in board_pages(page)[0] if c.get("needs_evaluation") is True]
    assert flags, "the budget-0 template left no flagged item"
    flag = flags[0]
    seat(page, flag["source"])
    open_board(page)

    # The card reads Needs evaluation with the screen's figures, and no plan, deadline or amount (D-24)
    card = board_region(page).get_by_test_id(f"decision-card-{flag['key']}")
    expect(card).to_have_count(1)
    expect(card).to_contain_text("Needs evaluation")
    expect(card).not_to_contain_text(re.compile(r"\b(Transfer|Markdown|Keep)\b|\$|Decide by|No deadline"))

    # Opening it shows the progress state while the evaluation runs (the real POST is held, never answered by the
    # test, then released unmodified), then the evaluated stock decision
    held = []

    def hold_the_post(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/cases/*/evaluate", hold_the_post)
    card.click()
    page.get_by_role("button", name="Open stock decision", exact=True).click()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held, "POST /evaluate never left the browser"
    progress = page.get_by_test_id("evaluating")
    expect(page.get_by_test_id("needs-evaluation")).to_be_visible()
    expect(progress).to_contain_text("Evaluating this item…")
    expect(progress.get_by_role("progressbar")).to_be_visible()
    assert len(held) == 1
    held[0].continue_()

    expect(page.get_by_test_id("needs-evaluation")).to_have_count(0)
    d = case_json(page, flag["key"])
    assert d.get("needs_evaluation") is not True and d["result"] is not None, (d.get("needs_evaluation"), d["result"])
    expect(page.locator("main#content").get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    page.unroute("**/api/cases/*/evaluate", hold_the_post)
    assert len(held) == 1, "the page posted /evaluate more than once"

    # The stock decision shows its recommendation: the Recommended row on Options
    open_by_route(page, d["case_id"], "options")
    best = step_panel(page).get_by_test_id("recommended-row")
    expect(best).to_have_count(1)
    expect(best.locator("td").first).to_have_text(d["result"]["recommended_label"])


def test_hq_filters_and_pages_cover_every_item_once(visitor):
    page = visitor.page
    start = session_json(page)["branch"]
    _, mine = board_pages(page)
    open_board(page)
    expect(page.get_by_text(evaluated_text(mine), exact=True)).to_be_visible()  # the start seat's scope

    # The tiny set fits on one page of the reference page size, so the board's requests ask the real server for pages
    # of SMALL_PAGE (the request is changed, never the answer) and the pager has pages to walk
    seat(page, None)
    page.route(is_board_read, lambda route: route.continue_(url=with_page_size(route.request.url, SMALL_PAGE)))
    open_board(page)
    cards, first = board_pages(page, {"page_size": str(SMALL_PAGE)})
    want = [c["key"] for c in cards]
    assert len(want) == len(set(want)) == first["total"], (len(want), first["total"])
    assert first["scope"] != mine["scope"], (first["scope"], mine["scope"])  # HQ's scope is every store
    expect(page.get_by_text(evaluated_text(first), exact=True)).to_be_visible()

    # Walk the pager until the last page: every key once
    size = first["page_size"]
    assert size == SMALL_PAGE, size
    pages = -(-first["total"] // size)
    assert pages > 1, f"{first['total']} items fit on one page of {size}: the pager is not exercised"
    seen: list[str] = []
    for n in range(1, pages + 1):
        expect(page.get_by_test_id("pager")).to_contain_text(f"Page {n} of {pages}")  # the board now shows page n
        expect(board_cards(page)).to_have_count(min(size, first["total"] - (n - 1) * size))
        seen += board_keys(page)
        if n < pages:
            page.get_by_role("button", name="Next page").click()
    expect(page.get_by_role("button", name="Next page")).to_be_disabled()
    assert sorted(seen) == sorted(want) and len(seen) == len(set(seen)), (seen, want)

    # The Branch filter limits the cards to that store's scope, as the server answers it
    page.unroute(is_board_read)
    open_board(page)
    other = next(c["source"] for c in cards if c["source"] != start)
    box = branch_box(page)
    box.fill(other)
    box.press("Enter")
    expect(page).to_have_url(re.compile(r"[?&]branch=" + re.escape(other) + r"(&|$)"))
    scoped, sfirst = board_pages(page, {"branch": other})
    assert scoped and all(other in (c["source"], c["dest"]) for c in scoped), scoped
    expect(board_cards(page)).to_have_count(len(sfirst["cases"]))
    expect(page.get_by_text(evaluated_text(sfirst), exact=True)).to_be_visible()
    assert sorted(board_keys(page)) == sorted(c["key"] for c in sfirst["cases"])


def test_an_unmapped_store_says_transfers_are_unavailable(visitor):
    page = visitor.page
    seat(page, None)
    unmapped = card_by_tag(page, "transfers_unavailable")
    open_board(page)
    box = branch_box(page)
    box.fill(unmapped["source"])
    box.press("Enter")
    expect(page).to_have_url(re.compile(r"[?&]branch=" + re.escape(unmapped["source"]) + r"(&|$)"))
    card = board_region(page).get_by_test_id(f"decision-card-{unmapped['key']}")
    card.click()
    page.get_by_role("button", name="Open stock decision", exact=True).click()
    d = case_json(page, unmapped["case_id"])
    assert d["eligibility"]["status"] == "unavailable_unmapped", d["eligibility"]
    expect(page.locator("main#content").get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")

    open_by_route(page, d["case_id"], "options")
    expect(step_panel(page).get_by_test_id("transfers-unavailable")).to_contain_text(UNMAPPED)
    keys = [o["key"] for o in d["result"]["options"]]
    assert "keep" in keys and any(k.startswith("md") for k in keys), keys  # keep and markdown are still offered
    assert not any(k.startswith("tr:") for k in keys), keys
    for o in d["result"]["options"]:
        expect(options_rows(page).filter(has_text=o["label"]).first).to_be_visible()


def test_the_designed_lot_is_labelled(visitor):
    page = visitor.page
    d = open_by_route(page, card_by_tag(page, "verify")["case_id"])
    assert d["lot"]["origin"] == "designed", d["lot"]
    label = page.locator("main#content").get_by_test_id("designed-example")
    expect(label).to_contain_text("Designed example")
    expect(label.get_by_role("button", name="More about the designed example")).to_have_count(1)


def test_products_show_their_code(visitor):
    page = visitor.page
    d = open_by_route(page, card_by_tag(page, "verify")["case_id"])
    head = page.locator("main#content header")
    expect(head.get_by_text("Product code", exact=True)).to_be_visible()
    expect(head.get_by_role("heading", level=1)).to_contain_text(d["sku"])


def test_about_gives_the_attribution_without_the_passcode(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/#/about")
        expect(page.get_by_role("heading", level=1, name="About this prototype")).to_be_visible()
        assert not [c for c in context.cookies() if c["name"] == "ss_session"], "About opened a session"
        expect(page.get_by_label("Passcode")).to_have_count(0)
        expect(page.get_by_label("HQ passcode")).to_have_count(0)
        expect(page.locator(f"a[href='{LICENSE_URL}']")).to_have_count(1)
        expect(page.locator("main, article").first).to_contain_text("not affiliated with or endorsed by Dingdong-Inc")
        kinds = page.locator('[data-about-section="simulated"]')
        for kind in ("Public:", "Assumption:", "Simulated:"):
            expect(kinds).to_contain_text(kind)
    finally:
        context.close()


def test_no_public_screen_shows_the_old_label(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        body = page.locator("body")
        page.goto("/")
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
        expect(body).not_to_contain_text(OLD_LABEL)  # the sign-in card
        sign_in(page, web)
        open_board(page)
        expect(body).not_to_contain_text(OLD_LABEL)  # the Decision board
        open_by_route(page, card_by_tag(page, "verify")["case_id"])
        expect(body).not_to_contain_text(OLD_LABEL)  # a stock decision
        page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile(r"^Tasks\b")).click()
        expect(page.get_by_role("heading", level=1)).to_have_text("Tasks")
        expect(body).not_to_contain_text(OLD_LABEL)  # Tasks
        page.goto("/#/about")
        expect(page.get_by_role("heading", level=1, name="About this prototype")).to_be_visible()
        expect(body).not_to_contain_text(OLD_LABEL)  # About
    finally:
        context.close()
