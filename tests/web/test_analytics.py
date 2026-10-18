"""PROD-17, Analytics half (06.1 D-15, D-17, D-32, D-51; UI-SPEC "Analytics"): one store's public sales, the
precomputed forecast with hours out of stock, simulated aging stock and estimated potential waste, each chart a
ChartCard with its kind, its period in scenario dates, a one-line summary and "Show as table".

The browser suite runs the synthetic set, whose real GET /api/analytics answers available false (the last test reads
that real answer). Until 05.2-22's public-dataset fixture reaches build/product, the public answer comes from
fixtures/analytics.json, a contract-shaped body (04-API-CONTRACT row 2026-10-17, 06.1-14 as built) served by
page.route for /api/analytics only; 06.1-31 reruns these on the real public fixture. Expected text is built from that
body here, never typed twice.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import pathlib
import urllib.parse

import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.web.conftest import HQ, new_context, session_json, sign_in

pytestmark = pytest.mark.web

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "analytics.json").read_text(encoding="utf-8"))
FIXTURE.pop("_about")
ANALYTICS = "**/api/analytics*"


def units(n: float | None) -> str:
    """format.units: grouped, at most one decimal (the fixture's figures need no rounding)."""
    if n is None:
        return "—"
    text = f"{n:,.1f}"
    return text[:-2] if text.endswith(".0") else text


def expected(n: float | None) -> str:
    """format.expected: whole units, half away from zero, grouped."""
    if n is None:
        return "—"
    return f"{int(math.copysign(math.floor(abs(n) + 0.5), n)):,}"


def short_date(iso: str) -> str:
    """"Sat 3 Oct" (format.markDate without its year)."""
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def answer(page: Page, view: dict | None = None) -> list[dict]:
    """Serve `view` (default: the fixture with the signed-in store as its branch) for GET /api/analytics on this page,
    and return the list the asked queries are appended to ({key: value} per request). As the server does, the series
    is the asked product's, and HQ's answer names the asked branch."""
    base = copy.deepcopy(view if view is not None else FIXTURE)
    s = session_json(page)
    asked: list[dict] = []

    def serve(route: Route) -> None:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))
        asked.append(q)
        body = copy.deepcopy(base)
        if body.get("available"):
            body["branch"] = s["branch"] if s["branch"] is not None else q.get("branch")
            if "product" in q and body["series"] is not None:
                body["series"]["sku"] = body["series"]["product"] = q["product"]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(ANALYTICS, serve)
    return asked


def card(page: Page, title: str):
    return page.get_by_role("region", name=title, exact=True)


def table_rows(region) -> list[list[str]]:
    return [[c.strip() for c in row.locator("td").all_inner_texts()] for row in region.locator("tbody tr").all()]


# --- Task 1 (tracer): Sales by product, as a chart and as a table ---------------------------------------------------

def test_a_store_manager_reads_sales_by_product_as_a_chart_and_as_a_table(visitor):
    page = visitor.page
    asked = answer(page)
    page.goto("/#/analytics")
    expect(page.get_by_role("heading", level=1, name="Analytics")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name="Analytics")) \
        .to_have_attribute("aria-current", "page")

    sales = FIXTURE["sales_by_product"]
    region = card(page, "Sales by product")
    expect(region.locator("canvas")).to_have_attribute("aria-hidden", "true")
    top = sales[0]
    expect(region).to_have_accessible_description(f"{top['product']} sold the most: {units(top['units'])} units "
                                                  "in this period.")
    assert asked and all("branch" not in q for q in asked), asked  # a manager's query never names a branch

    region.get_by_role("button", name="Show as table").click()
    expect(region.locator("canvas")).to_have_count(0)
    expect(region.locator("thead th")).to_have_text(["Product code", "Units", "Hours out of stock"])
    assert table_rows(region) == [[x["product"], units(x["units"]), units(x["hours_out_of_stock"])] for x in sales]

    region.get_by_role("button", name="Show as chart").click()
    expect(region.locator("table")).to_have_count(0)
    expect(region.locator("canvas")).to_have_attribute("aria-hidden", "true")
    expect(region.get_by_role("button", name="Show as table")).to_have_attribute("aria-pressed", "false")


# --- Task 2: Sales and forecast, Aging stock, Potential waste, the pickers and the empty states ---------------------

def test_choosing_a_product_shows_its_sales_and_forecast_with_the_forecast_chip(visitor):
    page = visitor.page
    asked = answer(page)
    page.goto("/#/analytics")
    expect(card(page, "Sales by product")).to_be_visible()
    # The Product code drives only this card, so it sits in it, never in the page header (the user's review, 261018-hel).
    first = card(page, f"Sales and forecast · {FIXTURE['series']['product']}")
    expect(first.get_by_label("Product code")).to_be_visible()
    expect(page.locator("header").get_by_label("Product code")).to_have_count(0)
    first.get_by_label("Product code").fill("P38")
    first.get_by_role("button", name="Show", exact=True).click()
    page.wait_for_url("**/#/analytics?product=P38")
    expect(card(page, "Sales and forecast · P38").get_by_label("Product code")).to_have_value("P38")
    page.get_by_label("Product code").fill("P101")
    page.get_by_role("button", name="Show", exact=True).click()
    page.wait_for_url("**/#/analytics?product=P101")
    region = card(page, "Sales and forecast · P101")
    expect(region.get_by_text("Forecast · precomputed", exact=True)).to_be_visible()
    assert asked[-1] == {"product": "P101"}, asked

    series = FIXTURE["series"]
    days, fc = series["days"], series["forecast"]
    expect(region).to_have_accessible_description(
        f"{units(sum(d['units'] for d in days))} units sold, {units(sum(d['hours_out_of_stock'] for d in days))} hours "
        f"out of stock; forecast {expected(sum(f['base'] for f in fc))} units for the next {len(fc)} days.")
    region.get_by_role("button", name="Show as table").click()
    expect(region.locator("thead th")).to_have_text(
        ["Date", "Units", "Hours out of stock", "Forecast low", "Forecast base", "Forecast high"])
    rows = table_rows(region)
    assert rows == ([[short_date(d["date"]), units(d["units"]), units(d["hours_out_of_stock"]), "—", "—", "—"]
                     for d in days]
                    + [[short_date(f["date"]), "—", "—", expected(f["low"]), expected(f["base"]), expected(f["high"])]
                       for f in fc])


def test_aging_is_simulated_and_potential_waste_estimated_with_kg_and_value(visitor):
    page = visitor.page
    answer(page)
    page.goto("/#/analytics")
    sales = card(page, "Sales by product")
    expect(sales).to_be_visible()
    for chip in ("Simulated", "Estimated", "Forecast · precomputed"):
        expect(sales.get_by_text(chip, exact=True)).to_have_count(0)  # public sales carry no chip

    aging = card(page, "Aging stock")
    expect(aging.get_by_text("Simulated", exact=True)).to_be_visible()
    aging.get_by_role("button", name="Show as table").click()
    expect(aging.locator("thead th")).to_have_text(["Days to expiry", "Units", "Lots"])
    assert table_rows(aging) == [[f"{r['bucket']} days", units(r["units"]), units(r["lots"])] for r in FIXTURE["aging"]]

    waste = card(page, "Potential waste")
    expect(waste.get_by_text("Estimated", exact=True)).to_be_visible()
    rows = FIXTURE["waste"]
    expect(waste).to_have_accessible_description(
        f"{expected(sum(r['units'] for r in rows))} units in {len(rows)} lots are projected to expire unsold "
        "if nothing changes.")
    waste.get_by_role("button", name="Show as table").click()
    expect(waste.locator("thead th")).to_have_text(["Product · lot", "Units at risk", "kg", "Value at full price"])
    assert session_json(page)["currency"] == "USD"  # the session's currency, from reference data
    money = lambda v: "—" if v is None else f"${v:,.2f}"  # noqa: E731  (format.money in USD)
    assert table_rows(waste) == [[f"{r['product']} · {r['lot_id']}", expected(r["units"]), units(r["kg"]),
                                  money(r["value"])] for r in rows]


def test_hq_chooses_a_branch_before_anything_loads(browser: Browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        asked = answer(page)
        page.goto("/#/analytics")
        expect(page.get_by_text("Choose a branch to see its analytics.", exact=True)).to_be_visible()
        expect(page.get_by_label("Branch", exact=True)).to_be_focused()
        assert asked == []  # nothing loads all series (D-17)
        page.get_by_label("Branch", exact=True).fill(web.store)
        page.get_by_role("button", name="Show", exact=True).click()
        expect(card(page, "Sales by product")).to_be_visible()
        assert asked[-1] == {"branch": web.store}, asked
    finally:
        context.close()


def test_hq_follows_forecasts_view_in_analytics_link_with_branch_and_product(browser: Browser, web):
    """Forecasts' "View in Analytics" sends HQ to #/analytics?branch=…&product=… (06.1-25): Analytics reads both."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        asked = answer(page)
        page.goto(f"/#/analytics?branch={web.store}&product=P101")
        expect(card(page, "Sales and forecast · P101")).to_be_visible()
        assert asked[-1] == {"branch": web.store, "product": "P101"}, asked
        expect(page.get_by_text("Choose a branch to see its analytics.", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_a_product_with_no_history_says_so_in_the_charts_place(visitor):
    page = visitor.page
    view = copy.deepcopy(FIXTURE)
    for d in view["series"]["days"]:
        d["units"], d["hours_out_of_stock"] = 0.0, 0
    answer(page, view)
    page.goto("/#/analytics?product=P109")
    region = card(page, "Sales and forecast · P109")
    expect(region.get_by_text("No sales recorded for P109 in this period.", exact=True)).to_be_visible()
    expect(region.get_by_text("Forecast · precomputed", exact=True)).to_be_visible()
    expect(region.locator("canvas")).to_have_count(0)
    expect(region.get_by_role("button", name="Show as table")).to_have_count(0)


def test_the_synthetic_set_says_analytics_covers_the_public_dataset(visitor):
    page = visitor.page  # no route: the real API's answer on the synthetic set
    page.goto("/#/analytics")
    expect(page.get_by_role("heading", level=1, name="Analytics")).to_be_visible()
    expect(page.get_by_text("Analytics covers the public sales dataset.", exact=True)).to_be_visible()
    expect(page.locator("main canvas")).to_have_count(0)


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_no_page_scrolls_sideways(visitor, width):
    page = visitor.page
    answer(page)
    page.set_viewport_size({"width": width, "height": 844 if width < 768 else 900})
    page.goto("/#/analytics")
    expect(card(page, "Potential waste")).to_be_visible()
    for title in ("Sales by product", "Sales and forecast · P104", "Aging stock", "Potential waste"):
        card(page, title).get_by_role("button", name="Show as table").click()
    over = page.evaluate("""() => {
      const main = document.getElementById("content");
      return [document.documentElement.scrollWidth - window.innerWidth, main.scrollWidth - main.clientWidth];
    }""")
    assert over[0] <= 0 and over[1] <= 0, (width, over)  # wide tables scroll inside their own box


# --- refocus refreshes the charts; a failed refresh keeps them under the stale banner (quick 261017-wzs finding 6) ----

def test_refocus_refreshes_the_charts_and_a_failed_refresh_keeps_them_under_the_stale_banner(visitor):
    page = visitor.page
    s = session_json(page)
    state = {"mode": "fixture"}
    changed = copy.deepcopy(FIXTURE)
    changed["aging"][0]["units"] = FIXTURE["aging"][0]["units"] + 11  # a dispatch recorded in another tab

    def serve(route: Route) -> None:
        if state["mode"] == "busy":
            route.fulfill(status=503, json={"error": "db_busy", "message": "The server is busy. Try again in a moment."})
            return
        body = copy.deepcopy(changed if state["mode"] == "changed" else FIXTURE)
        body["branch"] = s["branch"]
        route.fulfill(json=body)

    page.route(ANALYTICS, serve)
    page.goto("/#/analytics")
    aging = card(page, "Aging stock")
    first = FIXTURE["aging"][0]
    summary = "{} units in {} lots expire within 1 day."
    expect(aging).to_have_accessible_description(summary.format(units(first["units"]), first["lots"]))

    state["mode"] = "changed"
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(aging).to_have_accessible_description(summary.format(units(first["units"] + 11), first["lots"]))

    state["mode"] = "busy"
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    banner = page.get_by_text("Couldn't refresh. Showing what was loaded before.", exact=True)
    expect(banner).to_be_visible()
    expect(aging).to_have_accessible_description(summary.format(units(first["units"] + 11), first["lots"]))
    expect(page.get_by_text("Analytics didn't load", exact=True)).to_have_count(0)

    state["mode"] = "fixture"
    page.get_by_role("button", name="Try again").click()
    expect(banner).to_have_count(0)
    expect(aging).to_have_accessible_description(summary.format(units(first["units"]), first["lots"]))


def test_another_query_shows_its_own_loading_line_never_the_previous_charts(visitor):
    """A new store or product shows its own loading line while its answer is on the way; only a refetch of the same
    query keeps the charts on screen (Codex check of quick 261017-wzs)."""
    page = visitor.page
    s = session_json(page)
    held: list = []

    def serve(route: Route) -> None:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))
        if "product" in q:
            held.append(route)  # the new query's answer is slow
            return
        body = copy.deepcopy(FIXTURE)
        body["branch"] = s["branch"]
        route.fulfill(json=body)

    page.route(ANALYTICS, serve)
    page.goto("/#/analytics")
    expect(card(page, "Aging stock")).to_be_visible()
    page.goto("/#/analytics?product=P101")
    expect(page.get_by_text("Loading analytics…", exact=True)).to_be_visible()
    expect(card(page, "Aging stock")).to_have_count(0)
    deadline = 0
    while not held and deadline < 100:
        page.wait_for_timeout(50)
        deadline += 1
    body = copy.deepcopy(FIXTURE)
    body["branch"] = s["branch"]
    body["series"]["sku"] = body["series"]["product"] = "P101"
    held[0].fulfill(json=body)
    expect(card(page, "Sales and forecast · P101")).to_be_visible()
