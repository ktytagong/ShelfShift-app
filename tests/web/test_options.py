"""PROD-06: the Context and Options steps of the stock decision page (06.1-UI-SPEC "Step panel" rows 2 and 3; D-26,
D-37, D-39, D-61). Context: the lots the engine read (simulated), the fact checks with "Count stock" (07-21), and the
Forecast, Incoming and Not recorded boxes. Options: the plans checked, the Options table with its Recommended row in
the engine's order, the waste price (HQ changes it; a manager reads "(set by HQ)") and the version line with
"Engine". Every expected value is read from the API, never pasted."""
from __future__ import annotations

import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import HQ, case_id_by_tag, case_json, session_json, switch_seat
from tests.web.test_forecasts import SHORT_PROVIDER

pytestmark = pytest.mark.web

STORY = "milk-story"  # SUS-19: its source is the default account, and it carries a plan-changing count fact
COUNT_KINDS = ("PHANTOM_STOCK", "STALE_COUNT")


def open_step(page: Page, tag: str, step: str) -> dict:
    cid = case_id_by_tag(page, tag)
    page.goto("/#/cases/" + urllib.parse.quote(cid, safe="") + f"?step={step}")
    d = case_json(page, cid)
    expect(panel(page)).to_have_attribute("data-step", step.upper())
    return d


def panel(page: Page):
    return page.locator("main#content").get_by_test_id("step-panel")


def no_side_scroll(page: Page) -> bool:
    return page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


def test_context_shows_the_lots_the_fact_checks_and_the_boxes(visitor):
    page = visitor.page
    d = open_step(page, STORY, "context")
    s = session_json(page)
    assert (s["role"], s["branch"]) == ("manager", d["source"])
    p = panel(page)

    lots = p.get_by_test_id("lots")
    expect(lots.get_by_text("Simulated", exact=True)).to_be_visible()
    rows = lots.locator("tbody tr")
    expect(rows).to_have_count(len(d["engine_input"]["lots"]))
    expect(rows.first.locator("td").first).to_have_text(d["engine_input"]["lots"][0]["lot_id"])

    facts = p.locator("[data-fact-id]")
    expect(facts).to_have_count(len(d["fact_checks"]))
    asked = [f for f in d["fact_checks"] if f["kind"] in COUNT_KINDS and f["changes_plan"]]
    assert asked, "SUS-19 carries a plan-changing count fact in the template"
    row = p.locator(f"[data-fact-id='{asked[0]['fact_id']}']")
    expect(row.get_by_role("button", name="Count stock", exact=True)).to_be_visible()  # 07-21 (07-CONTEXT D-36)
    expect(p.get_by_role("button", name="Ask this branch for a count")).to_have_count(0)
    expect(p.get_by_role("button", name="Request stock check")).to_have_count(0)

    forecast = p.get_by_test_id("forecast")
    expect(forecast.get_by_text("Forecast · precomputed", exact=True)).to_be_visible()
    provenance = forecast.get_by_test_id("context-forecast-provenance")
    versions = d["versions"]
    codes = versions["forecast_series_providers"]
    if codes:
        providers = " + ".join(SHORT_PROVIDER.get(code, code) for code in codes)
        expect(provenance).to_have_text(
            f"Precomputed · {providers} · run {versions['forecast_run_id']} · history to {versions['forecast_cutoff']}"
        )
    else:
        expect(provenance).to_contain_text(versions["forecast_headline"])
        expect(provenance).to_contain_text(versions["forecast_cutoff"])
    expect(p.get_by_test_id("incoming")).to_contain_text("Incoming")
    nr = p.get_by_test_id("not-recorded")
    expect(nr.get_by_text("Shelf and backroom split: not recorded; a count gives one total.", exact=True)).to_be_visible()
    expect(nr.get_by_text("Condition: not recorded.", exact=True)).to_be_visible()


def test_requesting_a_stock_check_shows_its_status(visitor):
    page = visitor.page
    d = open_step(page, STORY, "context")
    fact = next(f for f in d["fact_checks"] if f["kind"] in COUNT_KINDS and f["changes_plan"])
    row = panel(page).locator(f"[data-fact-id='{fact['fact_id']}']")
    # 07-21 (07-CONTEXT D-36): Count stock opens the request panel; its Send request makes the request, and the row
    # shows the server's own state sentence.
    row.get_by_role("button", name="Count stock", exact=True).click()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/requests")) as sent:
        row.get_by_test_id("request-panel").get_by_role("button", name="Send request", exact=True).click()
    assert sent.value.status == 200, sent.value.status
    request = sent.value.json()
    assert request["fact_id"] == fact["fact_id"], request["fact_id"]
    expect(row.get_by_test_id("stock-check-status")).to_have_text(request["status"])
    expect(row.get_by_role("button", name="Count stock")).to_have_count(0)


def test_options_shows_the_checked_plans_the_recommended_row_and_the_version(visitor):
    page = visitor.page
    d = open_step(page, STORY, "options")
    r = d["result"]
    assert r is not None and r["options"]
    p = panel(page)
    expect(p.get_by_test_id("options-checked")).to_contain_text(f"{r['candidates_checked']:,} plans checked")

    table = p.get_by_test_id("options-scroll")
    body = table.locator("tbody tr")
    extra = (1 if r["wait"] is not None else 0) + len(r["rejected"])
    expect(body).to_have_count(len(r["options"]) + extra)
    labels = [body.nth(i).locator("td").first.inner_text().strip() for i in range(len(r["options"]))]
    assert labels == [o["label"] for o in r["options"]], "the rows follow the engine's order"
    best = table.get_by_test_id("recommended-row")
    expect(best).to_have_count(1)
    expect(best.locator("td").first).to_have_text(r["recommended_label"])
    expect(best.get_by_text("Recommended", exact=True)).to_be_visible()
    expect(best).to_have_class(re.compile(r"\bbg-gold-100\b"))
    for x in r["rejected"]:
        for reason in x["reasons"]:
            expect(table.get_by_text(reason, exact=True).first).to_be_visible()

    waste = p.get_by_test_id("waste-price")
    expect(waste).to_contain_text("(set by HQ)")
    expect(waste.get_by_role("link", name="Change waste price")).to_have_count(0)
    line = p.get_by_test_id("version-line")
    expect(line).to_contain_text(f"Engine {d['versions']['engine_version']}")
    expect(line).to_contain_text(d["versions"]["rec_version"][:12])
    expect(line).not_to_contain_text("engine engine")


def test_hq_changes_the_waste_price_from_options(visitor):
    page = visitor.page
    open_step(page, STORY, "options")
    switch_seat(page, None)
    assert session_json(page)["account"] == HQ
    open_step(page, STORY, "options")
    link = panel(page).get_by_test_id("waste-price").get_by_role("link", name="Change waste price")
    expect(link).to_have_attribute("href", "#/policies")


def test_options_at_768_scrolls_only_inside_its_table(visitor):
    page = visitor.page
    page.set_viewport_size({"width": 768, "height": 900})
    open_step(page, STORY, "options")
    expect(panel(page).get_by_test_id("options-scroll")).to_be_visible()
    # An absolutely placed element (a visually hidden table head) whose containing block lies outside the table's
    # scroll box escapes it and widens the document: the 1052px of 05-UI-REVIEW (D-37).
    assert no_side_scroll(page), page.evaluate("() => document.documentElement.scrollWidth")
    main_fits = page.evaluate("() => { const m = document.getElementById('content'); return m.scrollWidth <= m.clientWidth; }")
    assert main_fits, "the page content scrolls sideways at 768px"
