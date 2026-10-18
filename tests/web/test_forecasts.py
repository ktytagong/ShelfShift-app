"""PROD-17's Forecasts half (06.1 D-52; UI-SPEC "Forecasts", Copywriting "Empty: Forecasts"): a read-only page with the
store's precomputed demand per product. The run line names the run, its provider (from the server, so the stand-in is
never called BigQuery output), the history cutoff and the period as scenario dates, with the "Forecast · precomputed"
chip; a table in the server's pages shows seven daily bars, the base total and the 80% range, and links each product
to Analytics.

The browser suite's template is the synthetic set, whose API answers Forecasts `available: false`. The public-dataset
states are therefore answered with `page.route` from tests/web/fixtures/forecasts.json, the tiny public set's own
answer (06.1-01's rule until 05.2-22's public-dataset fixture reaches build/product; 06.1-31 reruns this file on the
real fixture). The handler pages and scopes the way api/stock.forecasts does: a manager reads the signed-in store, HQ
must name a known store. Expected text is built here from the fixture, never typed twice.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import pathlib
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.web

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "forecasts.json"
CHIP = "Forecast · precomputed"  # UI-SPEC "Data kinds"
# data/forecast.py PROVIDERS: the names the forecast pipeline gives each provider code.
PROVIDER = {
    "ai_forecast": "TimesFM 2.5 via BigQuery AI.FORECAST",
    "arima_plus": "ARIMA_PLUS via BigQuery ML.FORECAST",
    "local-seasonal-naive": "local seasonal-naive stand-in (development and tests only)",
}


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def whole(x: float) -> str:
    """format.expected: whole units, half away from zero (Intl's default), grouped."""
    n = int(decimal.Decimal(str(x)).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))
    return f"{n:,}"


def day(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso)


def mark_date(iso: str) -> str:
    """format.markDate: "Fri 16 Oct 2026"."""
    d = day(iso)
    return f"{d:%a} {d.day} {d:%b} {d.year}"


def short_day(d: dt.date) -> str:
    """A day of the period: "Sat 17 Oct"."""
    return f"{d:%a} {d.day} {d:%b}"


def period_from(run: dict) -> str:
    """The period's first day without the month and year it shares with the last ("Sat 17")."""
    a, b = day(run["period_from"]), day(run["period_to"])
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%a} {a.day}"
    return short_day(a) if a.year == b.year else mark_date(run["period_from"])


def run_line(run: dict) -> str:
    return (f"Forecast run {run['id']} · {PROVIDER[run['provider']]} · history to {mark_date(run['history_to'])} · "
            f"period {period_from(run)} – {mark_date(run['period_to'])}")


def demand_name(run: dict, item: dict) -> str:
    """The bars' accessible name: every day of the period with its base value."""
    first = day(run["period_from"])
    days = ", ".join(f"{short_day(first + dt.timedelta(days=i))} {whole(v)}" for i, v in enumerate(item["daily_base"]))
    return f"Demand per day for {item['sku']}: {days}"


def serve(page: Page, fx: dict, *, hq: bool = False, empty: tuple[str, ...] = ()) -> list[dict]:
    """Answer GET /api/forecasts from the fixture as api/stock.forecasts does: a manager's store is the fixture's
    manager store (a branch the request sends is ignored); HQ without a branch, or with an unknown one, is 400 with the
    server's sentence; a store in `empty` has no forecast rows. Pages of the fixture's page_size, in the fixture's
    (the server's) product order. Returns the parsed query of every request, in order."""
    asked: list[dict] = []

    def handle(route):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query).items()}
        asked.append(q)
        store = q.get("branch") if hq else fx["manager_store"]
        if store is None or store not in fx["items"]:
            message = "Choose a branch to see its forecasts." if store is None else f"No store {store}"
            route.fulfill(status=400, content_type="application/json",
                          body=json.dumps({"error": "bad_request", "message": message}))
            return
        items = [] if store in empty else [{**item, "provider_codes": item.get("provider_codes", [fx["run"]["provider"]])} for item in fx["items"][store]]
        n, size = int(q.get("page", "1")), fx["page_size"]
        body = {"available": True, "branch": store, "run": fx["run"], "items": items[(n - 1) * size:n * size],
                "page": n, "page_size": size, "total": len(items), "kind": "forecast"}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/forecasts*", handle)
    return asked


def test_a_manager_reads_the_run_line_and_the_first_product_row(visitor):
    fx = fixture()
    page = visitor.page
    serve(page, fx)
    page.goto("/#/forecasts")
    expect(page.get_by_role("heading", level=1, name="Forecasts")).to_be_visible()
    expect(page.get_by_test_id("forecast-run")).to_have_text(run_line(fx["run"]))
    expect(page.get_by_test_id("forecast-run-kind")).to_have_text(CHIP)
    # The stand-in is named as the stand-in, never as BigQuery output.
    expect(page.get_by_test_id("forecast-run")).not_to_contain_text("BigQuery")

    item = fx["items"][fx["manager_store"]][0]
    row = page.get_by_test_id("forecast-row").first
    expect(row.get_by_role("cell").first).to_have_text(item["sku"])
    bars = row.get_by_role("img")
    expect(bars).to_have_accessible_name(demand_name(fx["run"], item))
    expect(bars.locator("[data-bar]")).to_have_count(len(item["daily_base"]))
    expect(row.get_by_test_id("base-total")).to_have_text(whole(item["base_total"]))
    expect(row.get_by_test_id("range-80")).to_have_text(f"{whole(item['low_total'])} – {whole(item['high_total'])}")
    expect(page.get_by_role("columnheader", name="80% range")).to_be_visible()


def test_a_bigquery_run_is_named_by_its_provider(visitor):
    fx = fixture()
    fx["run"] = {**fx["run"], "provider": "ai_forecast", "providers": {"ai_forecast": 10}}
    page = visitor.page
    serve(page, fx)
    page.goto("/#/forecasts")
    expect(page.get_by_test_id("forecast-run")).to_have_text(run_line(fx["run"]))
    expect(page.get_by_test_id("forecast-run")).to_contain_text("TimesFM 2.5")


# --- Task 2: paging, the Analytics link, HQ's store choice, the empty and unavailable states -----------------------

ALLOWED_BUTTONS = {"Previous page", "Next page", "Show as table", "Show as chart"}  # the Pager and the view toggle


def main_buttons(page: Page) -> set[str]:
    """The accessible names of the buttons in the page's content (the shell's sidebar and menus are outside main)."""
    main = page.get_by_role("main")
    return {b.get_attribute("aria-label") or b.inner_text().strip() for b in main.get_by_role("button").all()}


def test_the_pager_walks_the_servers_pages_and_each_row_links_to_analytics(visitor):
    fx = fixture()
    items = fx["items"][fx["manager_store"]]
    page = visitor.page
    asked = serve(page, fx)
    page.goto("/#/forecasts")
    rows = page.get_by_test_id("forecast-row")
    expect(rows).to_have_count(1)
    expect(rows.first.get_by_role("cell").first).to_have_text(items[0]["sku"])
    expect(page.get_by_test_id("pager")).to_have_text(f"Page 1 of {len(items)} · {len(items)} items")
    link = rows.first.get_by_role("link", name="View in Analytics")
    expect(link).to_have_attribute("href", f"#/analytics?product={items[0]['sku']}")

    page.get_by_role("button", name="Next page").click()
    expect(rows.first.get_by_role("cell").first).to_have_text(items[1]["sku"])
    expect(page.get_by_test_id("pager")).to_have_text(f"Page 2 of {len(items)} · {len(items)} items")
    assert asked[-1] == {"page": "2"}  # a manager's query never names a branch; the server pages
    page.get_by_role("button", name="Previous page").click()
    expect(rows.first.get_by_role("cell").first).to_have_text(items[0]["sku"])

    # Read-only (D-52): the content holds the Pager, the view toggle and links, and no other control.
    assert main_buttons(page) <= ALLOWED_BUTTONS, main_buttons(page)
    # Simulated inputs are none on every row of the public set: one explanation line, not a column of "None".
    expect(page.get_by_test_id("simulated-inputs")).to_have_text(
        f"Simulated inputs: none. Each forecast reads the public sales history only ({fx['run']['input_label']}).")
    expect(page.get_by_role("columnheader", name="Simulated inputs")).to_have_count(0)


def test_the_table_view_lists_each_days_value(visitor):
    fx = fixture()
    item = fx["items"][fx["manager_store"]][0]
    page = visitor.page
    serve(page, fx)
    page.goto("/#/forecasts")
    toggle = page.get_by_role("button", name="Show as table")
    toggle.click()
    expect(page.get_by_role("button", name="Show as chart")).to_have_attribute("aria-pressed", "true")
    first = day(fx["run"]["period_from"])
    row = page.get_by_test_id("forecast-row").first
    for i, v in enumerate(item["daily_base"]):
        expect(page.get_by_role("columnheader", name=short_day(first + dt.timedelta(days=i)), exact=True)).to_be_visible()
        expect(row.get_by_role("cell").nth(2 + i)).to_have_text(whole(v))
    expect(row.get_by_role("img")).to_have_count(0)


def test_a_row_with_simulated_inputs_shows_the_column(visitor):
    fx = fixture()
    store = fx["manager_store"]
    fx["items"][store][0]["simulated_inputs"] = True
    fx["page_size"] = 2
    page = visitor.page
    serve(page, fx)
    page.goto("/#/forecasts")
    expect(page.get_by_role("columnheader", name="Simulated inputs")).to_be_visible()
    rows = page.get_by_test_id("forecast-row")
    expect(rows.nth(0)).to_contain_text("Stock simulated")
    expect(rows.nth(1).get_by_test_id("simulated-cell")).to_have_text("None")
    expect(page.get_by_test_id("simulated-inputs")).to_have_count(0)


def test_hq_chooses_a_branch_and_an_empty_branch_says_so(visitor):
    from tests.web.conftest import switch_seat

    fx = fixture()
    page = visitor.page
    switch_seat(page, None)
    asked = serve(page, fx, hq=True, empty=("S13",))
    page.goto("/#/forecasts")
    expect(page.get_by_text("Choose a branch to see its forecasts.")).to_be_visible()
    branch = page.get_by_label("Branch")
    expect(branch).to_be_focused()
    assert asked == []  # nothing is asked without a store

    branch.fill("S12")
    branch.press("Enter")
    item = fx["items"]["S12"][0]
    row = page.get_by_test_id("forecast-row").first
    expect(row.get_by_role("cell").first).to_have_text(item["sku"])
    expect(row.get_by_role("img")).to_have_accessible_name(demand_name(fx["run"], item))
    assert asked[-1] == {"branch": "S12"}
    expect(row.get_by_role("link", name="View in Analytics")).to_have_attribute(
        "href", f"#/analytics?branch=S12&product={item['sku']}")

    branch.fill("S13")
    branch.press("Enter")
    expect(page.get_by_role("heading", name="No forecasts for S13")).to_be_visible()
    expect(page.get_by_text("Forecasts appear when the precomputed run covers this branch.")).to_be_visible()
    expect(page.get_by_test_id("forecast-row")).to_have_count(0)


def test_a_failed_refetch_keeps_the_rows_under_the_stale_banner(visitor):
    fx = fixture()
    page = visitor.page
    serve(page, fx)
    page.goto("/#/forecasts")
    expect(page.get_by_test_id("forecast-row")).to_have_count(1)
    page.unroute("**/api/forecasts*")
    page.route("**/api/forecasts*", lambda route: route.abort())
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.get_by_text("Couldn't refresh. Showing what was loaded before.")).to_be_visible()
    expect(page.get_by_test_id("forecast-row")).to_have_count(1)


def test_the_synthetic_set_says_forecasts_cover_the_public_dataset(visitor):
    page = visitor.page
    # Keep this empty-state behavior available on both browser datasets.
    page.route("**/api/forecasts*", lambda route: route.fulfill(json={"available":False, "reason":"Analytics covers the public sales dataset."}))
    page.goto("/#/forecasts")
    expect(page.get_by_role("heading", level=1, name="Forecasts")).to_be_visible()
    expect(page.get_by_text("Analytics covers the public sales dataset.")).to_be_visible()
    expect(page.get_by_test_id("forecast-row")).to_have_count(0)
    assert main_buttons(page) <= ALLOWED_BUTTONS, main_buttons(page)


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_the_page_never_scrolls_sideways(visitor, width):
    fx = fixture()
    page = visitor.page
    serve(page, fx)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto("/#/forecasts")
    expect(page.get_by_test_id("forecast-row")).to_have_count(1)
    for view in ("chart", "table"):
        fits = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        wide = page.evaluate("""() => [...document.querySelectorAll('body *')]
            .filter(e => e.getBoundingClientRect().right > window.innerWidth + 0.5)
            .slice(0, 8).map(e => e.tagName + '.' + (e.className || '').toString().slice(0, 60) + ' ' +
                Math.round(e.getBoundingClientRect().right))""")
        assert fits, f"{view} view at {width}px scrolls sideways: {wide}"
        if view == "chart":
            page.get_by_role("button", name="Show as table").click()


def test_a_page_past_the_last_asks_for_the_last_page_even_when_it_came_back_empty(visitor):
    """The shared Pager's correction (quick 261017-wzs finding 5, Codex check) also runs when the page past the last
    came back with no rows: the page asks the server for the last page with records and shows its row."""
    fx = fixture()
    items = fx["items"][fx["manager_store"]]
    page = visitor.page
    asked = serve(page, fx)
    page.goto("/#/forecasts?page=99")
    rows = page.get_by_test_id("forecast-row")
    expect(rows.first.get_by_role("cell").first).to_have_text(items[-1]["sku"])
    expect(page.get_by_test_id("pager")).to_have_text(f"Page {len(items)} of {len(items)} · {len(items)} items")
    assert asked[0] == {"page": "99"} and asked[-1] == {"page": str(len(items))}, asked


from tests.web.conftest import public_only

SHORT_PROVIDER = {"ai_forecast": "TimesFM 2.5", "arima_plus": "ARIMA_PLUS", "local-seasonal-naive": "local seasonal-naive stand-in"}


@public_only
def test_forecasts_rows_name_their_provider(visitor):
    page = visitor.page
    with page.expect_response(lambda r: "/api/forecasts" in r.url and r.request.method == "GET") as read:
        page.goto("/#/forecasts")
    data = read.value.json()
    assert data["available"] and data["items"]
    expect(page.get_by_role("columnheader").nth(0)).to_have_text("Product code")
    expect(page.get_by_role("columnheader").nth(1)).to_have_text("Provider")
    for item in data["items"]:
        expected = " + ".join(SHORT_PROVIDER.get(code, code) for code in item["provider_codes"])
        expect(page.get_by_test_id(f"forecast-provider-{item['sku']}")).to_have_text(expected)
    page.get_by_role("button", name="Show as table", exact=True).click()
    expect(page.get_by_role("columnheader", name="Provider", exact=True)).to_be_visible()
    for item in data["items"]:
        expect(page.get_by_test_id(f"forecast-provider-{item['sku']}")).to_have_text(" + ".join(SHORT_PROVIDER.get(code, code) for code in item["provider_codes"]))
