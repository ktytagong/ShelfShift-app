"""PROD-07: the Compare step of the stock decision page (06.1-UI-SPEC "Comparison (Compare step)"; 06.1-CONTEXT D-27,
D-28, D-54). The owner reads the three-way comparison (Recommended, Simple rule, Keep) the server stored, traces each
figure to its source, and sends the decision for approval. Every expected value is read from the API (the case detail
and the session), never pasted; the columns render detail.comparison only (T-06.1-74)."""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_id_by_tag, case_json, session_json, take_ownership
from tests.web.test_case import STORY, decision_href, panel, step_button

pytestmark = pytest.mark.web

HEADERS = ("Recommended", "Simple rule (store markdowns + earliest transfer)", "Keep (store markdowns only)")
SIMULATED_LINE = "Simulated with the engine's forecast. Not observed results."
NOT_FOR_VERSION = "Comparison not computed for this version."


def money(page: Page, v: float, currency: str) -> str:
    """format.money in the page's own Intl: the session currency, cents, a true minus."""
    return page.evaluate(
        """([v, c]) => new Intl.NumberFormat("en-US", {style: "currency", currency: c, signDisplay: "negative"})
             .formatToParts(v).map((p) => (p.type === "minusSign" ? "−" : p.value)).join("")""", [v, currency])


def owned_at_compare(page: Page) -> dict:
    """Take ownership of the story decision through the API as its source manager, open it at its current step and
    wait for its h1; its detail JSON (step COMPARE)."""
    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    page.goto(decision_href(cid))
    d = case_json(page, cid)
    expect(page.locator("main#content").get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    assert d["step"] == "COMPARE", d["step"]
    return d


def grid(page: Page):
    return panel(page).get_by_role("table", name="Comparison")


def row_cells(page: Page, row: str):
    """The three value cells of the comparison row `row` (data-row), in column order."""
    return grid(page).locator(f"[role=row][data-row='{row}'] [role=cell]")


# --- Task 1 (tracer): the owner reads the comparison and sends it -------------------------------------------------

def test_the_owner_reads_the_comparison_and_sends_it(visitor):
    page = visitor.page
    d = owned_at_compare(page)
    s = session_json(page)
    cmp = d["comparison"]
    assert cmp["status"] == "ok", cmp["status"]
    assert [c["column"] for c in cmp["columns"]] == ["recommended", "simple_rule", "keep"], cmp["columns"]

    expect(panel(page).get_by_text(SIMULATED_LINE, exact=True)).to_be_visible()
    for name in HEADERS:
        expect(grid(page).get_by_role("columnheader", name=name, exact=True)).to_be_visible()
    net = row_cells(page, "net")
    expect(net).to_have_count(3)
    for i, col in enumerate(cmp["columns"]):
        expect(net.nth(i)).to_contain_text(money(page, col["net"]["base"], s["currency"]))
        expect(row_cells(page, "plan").nth(i)).to_have_text(col["label"])

    send = page.locator("main#content").get_by_role("button", name="Send for approval", exact=True)
    expect(send).to_be_visible()
    send.click()
    expect(step_button(page, 5, "Approve")).to_have_attribute("aria-current", "step")
    after = case_json(page, d["case_id"])
    assert (after["state"], after["step"]) == ("PENDING_APPROVAL", "APPROVE"), (after["state"], after["step"])
    city = s["time_zone"].split("/")[-1].replace("_", " ")
    expect(panel(page).get_by_test_id("booking-line")).to_have_text(
        re.compile(rf"^Capacity held until \d{{2}}:\d{{2}} {re.escape(city)} time\.$"))
    expect(page.locator("main#content").get_by_text(re.compile(r"\d+ min\b"))).to_have_count(0)


# --- Task 2: figure links, missing and older comparisons, overflow --------------------------------------------------

def test_figure_refs_open_their_sources(visitor):
    page = visitor.page
    d = owned_at_compare(page)
    expl = panel(page).get_by_test_id("explanation")
    expect(expl.get_by_text("Template summary · from the engine's figures", exact=True)).to_be_visible()
    expect(expl.get_by_test_id("template-summary")).to_have_text(d["summary"])
    for ref, name in (("F1", "Fact check"), ("L1", "Limits"), ("N1", "Net revenue"), ("X1", "What would change it")):
        expect(expl.get_by_role("button", name=f"Source: {name}", exact=True)).to_have_text(ref)

    # N1 brings the Net revenue row into view with the navy ring (motion allowed here).
    page.set_viewport_size({"width": 390, "height": 500})
    expl.get_by_role("button", name="Source: Net revenue", exact=True).click()
    row = grid(page).locator("[role=row][data-row='net']")
    expect(row).to_have_attribute("data-flash", "true")
    expect(row).to_be_in_viewport()
    assert row.evaluate("el => getComputedStyle(el).boxShadow") != "none", "the flash shows a ring"

    # Under reduced motion the same press scrolls but draws no ring.
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_timeout(1300)  # the earlier flash has ended
    expl.get_by_role("button", name="Source: Net revenue", exact=True).click()
    expect(row).to_have_attribute("data-flash", "true")
    assert row.evaluate("el => getComputedStyle(el).boxShadow") == "none", "no ring under reduced motion"
    page.emulate_media(reduced_motion="no-preference")

    # F1 opens Context, L1 opens Options.
    expl.get_by_role("button", name="Source: Fact check", exact=True).click()
    expect(page).to_have_url(re.compile(r"\?step=context$"))
    expect(panel(page)).to_have_attribute("data-step", "CONTEXT")
    page.goto(decision_href(d["case_id"], "compare"))
    panel(page).get_by_test_id("explanation").get_by_role("button", name="Source: Limits", exact=True).click()
    expect(page).to_have_url(re.compile(r"\?step=options$"))
    expect(panel(page)).to_have_attribute("data-step", "OPTIONS")


def answer_with_comparison(page: Page, case_id: str, comparison: dict) -> None:
    """Answer the page's GET of this case detail with the real response, its `comparison` replaced."""
    import urllib.parse

    path = "/api/cases/" + urllib.parse.quote(case_id, safe="")

    def handle(route) -> None:
        if urllib.parse.urlsplit(route.request.url).path != path or route.request.method != "GET":
            route.continue_()
            return
        real = route.fetch()
        body = real.json()
        body["comparison"] = comparison
        route.fulfill(response=real, body=json.dumps(body), headers={**real.headers, "content-type": "application/json"})

    page.route("**/api/cases/*", handle)


def test_a_comparison_not_computed_or_missing_says_so_in_every_cell(visitor):
    page = visitor.page
    d = owned_at_compare(page)
    answer_with_comparison(page, d["case_id"], {"status": "not_computed", "reason": None, "columns": []})
    page.reload()
    cells = grid(page).locator("[role=cell]")
    expect(cells.first).to_have_text(NOT_FOR_VERSION)
    n = cells.count()
    assert n == 3 * 8, n
    for i in range(n):
        expect(cells.nth(i)).to_have_text(NOT_FOR_VERSION)

    page.unroute("**/api/cases/*")
    reason = "No forecast for 18 Oct at S017"
    answer_with_comparison(page, d["case_id"], {"status": "missing_data", "reason": reason, "columns": []})
    page.reload()
    cells = grid(page).locator("[role=cell]")
    expect(cells.first).to_have_text(f"Not computed: {reason}")
    for i in range(cells.count()):
        expect(cells.nth(i)).to_have_text(f"Not computed: {reason}")
    expect(page.locator("main#content").get_by_text("wastes less than")).to_have_count(0)


def test_the_grid_scrolls_in_its_box_at_390_with_the_label_column_sticky(visitor):
    page = visitor.page
    owned_at_compare(page)
    page.set_viewport_size({"width": 390, "height": 800})
    box = panel(page).get_by_test_id("compare-scroll")
    expect(box).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the page scrolls sideways"
    assert box.evaluate("el => el.scrollWidth > el.clientWidth"), "the grid scrolls inside its own box"
    label = grid(page).locator("[role=row][data-row='net'] [role=rowheader]")
    before = label.evaluate("el => el.getBoundingClientRect().left")
    box.evaluate("el => { el.scrollLeft = el.scrollWidth; }")
    page.wait_for_timeout(50)
    after = label.evaluate("el => el.getBoundingClientRect().left")
    assert abs(after - before) < 1, (before, after)
    assert label.evaluate("el => getComputedStyle(el).position") == "sticky"
    expect(page.locator("main#content").get_by_text("wastes less than")).to_have_count(0)
