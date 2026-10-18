"""Illustrative saved payloads for presentation/request behavior, never evaluation evidence."""
from __future__ import annotations

import copy
import pathlib
import re
import urllib.parse

import pytest
from playwright.sync_api import expect

from tests.web.test_reports import CURRENT, NONE, answer, fixture, panel  # noqa: F401  (workspace budget: conftest, 77067e2)
from tests.web.conftest import HQ, new_context, session_json, sign_in

pytestmark = pytest.mark.web

LABELS = ["Modeled store rule", "Change from equivalent stock checks",
          "Change from ShelfShift decisions vs modeled rule", "ShelfShift total"]
INBOUND = "The modeled store rule ignores inbound deliveries when estimating surplus. Differences reflect this limitation as well as ShelfShift's planning."
CHECKS = "Simulated stock checks are answered correctly before the decision."


def variant(negative=False, near=False):
    body = fixture("reports_benchmark_negative" if negative else "reports_benchmark_current")
    values = [498120, -1210 if negative else 9870, 4350, 501260 if negative else 512340]
    if near:
        values = [12345678, 0, 1, 12345679]
    result = body["current"]
    result["summary"]["money"]["steps"] = [dict(key=k, label=label, kind=kind, value=value)
        for k, label, kind, value in zip(("store_rule", "checks_change", "decisions_change", "shelfshift"),
                                      LABELS, ("total", "change", "change", "total"), values)]
    result["summary"]["footnote_x"] = -222 if negative else 15555
    result["summary"]["categories_without_schedule"] = ["Illustrative category"]
    return body


def card(page):
    return page.get_by_role("region", name="Net revenue after operating costs", exact=True)


@pytest.mark.parametrize("negative", [False, True])
def test_money_steps_notes_and_table(visitor, negative):
    page = visitor.page
    body = variant(negative)
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    chart = card(page)
    expect(chart.locator("canvas")).to_be_visible()
    expect(chart.get_by_text(INBOUND, exact=True)).to_be_visible()
    expect(chart.get_by_text(CHECKS, exact=True)).to_be_visible()
    expect(chart.get_by_text("The modeled store rule has no markdown steps for Illustrative category, which affects its results there.", exact=True)).to_be_visible()
    expected = "−$1,210" if negative else "+$9,870"
    expect(chart.get_by_text(re.compile(re.escape(expected) + " from equivalent stock checks"))).to_be_visible()
    button = chart.get_by_role("button", name="Show as table")
    button.click()
    expect(chart.get_by_role("button", name="Show as chart")).to_be_focused()
    expect(chart.get_by_role("cell", name=expected, exact=True)).to_be_visible()
    for text in ("Modeled store rule with original records", LABELS[1], LABELS[2], "ShelfShift decisions with those checks"):
        expect(chart.get_by_role("cell", name=text, exact=True)).to_be_visible()
    expect(chart.get_by_text(INBOUND, exact=True)).to_be_visible()
    expect(chart.get_by_text(CHECKS, exact=True)).to_be_visible()
    expect(chart.get_by_text(re.compile("Holding decisions and stock outcomes fixed"))).to_contain_text("−$222" if negative else "+$15,555")
    expect(chart.get_by_text(re.compile("Axis starts at"))).to_be_visible()


def test_schedule_note_is_conditional(visitor):
    body = variant()
    body["current"]["summary"]["categories_without_schedule"] = []
    answer(visitor.page, benchmark=body)
    visitor.page.goto("/#/reports?tab=benchmark")
    expect(card(visitor.page).get_by_text(re.compile("has no markdown steps"))).to_have_count(0)


@pytest.mark.parametrize("width", [360, 390, 1280])
@pytest.mark.parametrize("near", [False, True])
def test_canvas_visual_backstop_capture(visitor, width, near):
    page = visitor.page
    page.set_viewport_size({"width": width, "height": 800})
    page.emulate_media(reduced_motion="reduce")
    answer(page, benchmark=variant(negative=not near, near=near))
    page.goto("/#/reports?tab=benchmark")
    chart = card(page)
    expect(chart.locator("canvas")).to_be_visible()
    expect(chart.locator("[data-orientation]")).to_have_attribute("data-orientation", "horizontal" if chart.bounding_box()["width"] < 560 else "vertical")
    page.wait_for_timeout(500)
    folder = pathlib.Path("test-results/reports-08-08")
    folder.mkdir(parents=True, exist_ok=True)
    # A tall capture keeps the card's notes inside the scrolling workspace on phones.
    page.set_viewport_size({"width": width, "height": 1600})
    chart.screenshot(path=str(folder / f"{width}-{'near-zero' if near else 'negative'}-chart.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    chart.get_by_role("button", name="Show as table").click()
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    expect(chart.get_by_role("button", name="Show as chart")).to_be_focused()
    chart.screenshot(path=str(folder / f"{width}-{'near-zero' if near else 'negative'}-table.png"))


def test_chart_import_failure_keeps_accessible_values(visitor):
    page = visitor.page
    page.route("**/assets/*", lambda route: route.abort() if re.search(r"chart[^/]*\.js", route.request.url, re.I) else route.continue_())
    answer(page, benchmark=variant(True))
    page.goto("/#/reports?tab=benchmark")
    chart = card(page)
    expect(chart.get_by_text(INBOUND, exact=True)).to_be_visible()
    expect(chart.get_by_text(re.compile("−\\$1,210 from equivalent"))).to_be_visible()
    chart.get_by_role("button", name="Show as table").click()
    expect(chart.get_by_role("cell", name="−$1,210", exact=True)).to_be_visible()


@pytest.mark.parametrize("settings", [False, True])
def test_header_conditions_versions_and_provenance(visitor, settings):
    page = visitor.page
    body = variant()
    result = body["current"]
    result["git_commit"] = "abcdef123456789"
    result["image_digest"] = "never-render-this-digest"
    if settings:
        body["settings_note"] = [dict(field="waste_price", workspace=0.1, benchmark=0)]
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    header = panel(page).get_by_role("heading", name="Benchmark evaluation").locator(".. ").locator("..")
    details = page.get_by_role("region", name="Evaluation conditions and versions")
    for text in (CHECKS, "Forecasts last refreshed on day 3.",
                 "The evaluation uses recorded units sold as a demand proxy. It does not estimate unmet demand during stockouts or remove historical promotion effects.",
                 "Deliveries are as in the dataset in every scenario, and no scenario places orders."):
        expect(details.get_by_text(text, exact=True)).to_be_visible()
    expect(header.get_by_text(re.compile("Network benchmark · 2 stores · 2 at-risk lots on day 3"))).to_be_visible()
    expect(details.get_by_text(re.compile("Benchmark illustrative-published-benchmark · published Recorded 18 Oct 2026, 07:00 Bangkok time · code abcdef1"))).to_be_visible()
    expect(details.get_by_text(re.compile("The window ends at the end of"))).to_be_visible()
    expect(details.get_by_text(re.compile("Modeled store rule = the Simple rule"))).to_be_visible()
    note = details.get_by_text(re.compile("This workspace's settings differ"))
    if settings:
        expect(note).to_contain_text("Waste price $0.10/kg (benchmark $0.00/kg)")
    else:
        expect(note).to_have_count(0)
    assert "never-render-this-digest" not in panel(page).inner_text()
    assert_versions(details, result["versions"])


VERSION_LABELS = {"engine": "Engine", "dataset": "Dataset", "policy": "Policy", "forecast_runs": "Forecast runs",
                  "evaluator": "Evaluator", "settings": "Settings"}


def version_value(details, key):
    return details.locator(f"xpath=.//dt[normalize-space()='{VERSION_LABELS[key]}']/following-sibling::dd[1]")


def shown_version(key, value):
    """Every version in full, except the settings digest (its first 12 characters, UI-SPEC B1 item 5)."""
    return ", ".join(value) if isinstance(value, list) else value[:12] if key == "settings" else value


def assert_versions(details, versions):
    assert len({str(v) for v in versions.values()}) == len(versions), "fixture versions must differ from each other"
    for key, value in versions.items():
        expect(version_value(details, key)).to_have_text(shown_version(key, value))


def breakdown_body(body, by="day", page=1, branch="ILLUSTRATIVE-PINNED", marker=0):
    body = copy.deepcopy(body)
    run = body["current"] or body["historical"]
    rows = [dict(day=day, store_rule=day, checks_change=0, decisions_change=-1, shelfshift=marker + day,
                 pinned=False) for day in range(7)] if by == "day" else [
        dict(branch=branch if page == 1 else "ILLUSTRATIVE-PAGE-TWO", pinned=page == 1,
             store_rule=0, checks_change=-1, decisions_change=2, shelfshift=marker)]
    run["breakdown"] = dict(benchmark_id=run["benchmark_id"], kind="SIMULATED", chart="money", by=by,
                            rows=rows, page=page, page_size=1 if by == "branch" else 7, total=2 if by == "branch" else 7)
    return body


def named_section(page, heading):
    return page.get_by_role("heading", name=heading, exact=True).locator("xpath=ancestor::section[1]")


def block(page, name, open_it=True):
    button = panel(page).locator("button[aria-expanded]").filter(has_text=re.compile("^" + re.escape(name)))
    if open_it:
        button.click()
        expect(button).to_be_focused()
    return button.locator("xpath=..")


def assert_reason(page, button, reason):
    button.focus()
    expect(button).to_be_focused()
    assert button.get_attribute("aria-describedby")
    box = button.bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44
    page.keyboard.press("Enter")
    expect(button.locator("xpath=..").get_by_text(reason, exact=True)).to_be_visible()
    page.keyboard.press("Space")
    expect(button).to_have_attribute("aria-expanded", "false")
    button.tap() if page.evaluate("navigator.maxTouchPoints > 0") else button.click()
    expect(button).to_have_attribute("aria-expanded", "true")


def test_headline_blocks_and_accessible_missing_reasons(visitor):
    page = visitor.page
    body = variant()
    # Nulls stay null; the recorded reasons must be available without a mouse hover.
    body["current"]["summary"]["trips"]["markdowns_only"]["short"] = None
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    headline = named_section(page, "Headline figures")
    for value in ("$512,340", "96.4%", "7 units", "88.2%"):
        expect(headline.get_by_text(value, exact=True)).to_be_visible()
    expect(headline.get_by_text(INBOUND, exact=True)).to_be_visible()
    toggle = headline.get_by_role("button", name="Show all scenario figures", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    missing = headline.get_by_role("button", name="Why unavailable: Sell-through of moved stock · markdowns_only", exact=True)
    assert_reason(page, missing, "No received transfers")
    expect(headline.get_by_role("columnheader")).to_have_count(5)
    for name in ("Stock checks & trips", "Decision quality", "Forecast quality", "By branch"):
        button = panel(page).get_by_role("button", name=re.compile("^" + re.escape(name)))
        expect(button).to_have_attribute("aria-expanded", "false")
        result = button.locator("span").last.inner_text()
        assert len(result) <= 90
    checks = block(page, "Stock checks & trips")
    for name in ("Requests", "Lots counted", "Records corrected up", "Records corrected down", "Nothing to correct",
                 "Short trips", "Failed trips", "Transport paid on unshipped stock"):
        expect(checks.get_by_role("cell", name=name, exact=True)).to_be_visible()
    assert_reason(page, checks.get_by_role("button", name="Why unavailable: Short trips · markdowns_only", exact=True), "No transfers in this scenario")
    expect(checks.get_by_text(CHECKS, exact=True)).to_be_visible()
    expect(checks.get_by_role("heading", name=re.compile("Closing balance · end of"))).to_be_visible()
    quality = block(page, "Decision quality")
    expect(quality.get_by_text("No limit broken · measured", exact=True)).to_be_visible()
    expect(quality.get_by_text("needs input", exact=True)).to_be_visible()
    expect(quality.get_by_text("Retry limit", exact=True)).to_be_visible()
    expect(quality.get_by_text("Not recorded in this run", exact=True)).to_be_visible()
    expect(quality.get_by_text("In the benchmark, ShelfShift's recommendations are committed automatically. In the app, people approve every action.", exact=True)).to_be_visible()
    forecast = block(page, "Forecast quality")
    assert_reason(page, forecast.get_by_role("button", name="Why unavailable: Forecast WAPE", exact=True),
                  "Undefined for a zero-total series or when no eligible in-stock observations were recorded.")
    expect(forecast.get_by_text("75%", exact=True)).to_be_visible()
    expect(forecast.get_by_text("Inputs · imputed days", exact=True)).to_be_visible()
    expect(forecast.get_by_text("Forecasts last refreshed on day 3.", exact=True)).to_be_visible()


def test_limit_breaks_render_saved_nonzero_measurement(visitor):
    page = visitor.page
    body = variant()
    body["current"]["summary"]["decision_quality"]["limit_breaks"]["shelfshift"] = 3
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    quality = block(page, "Decision quality")
    expect(quality.get_by_text("3 plans broke a limit · measured", exact=True)).to_be_visible()
    expect(quality.get_by_text("No limit broken · measured", exact=True)).to_have_count(0)


def branch_figures_body(body, page=1, own="ILLUSTRATIVE-PINNED"):
    response = breakdown_body(body, "branch", page, own)
    row = (response["current"] or response["historical"])["breakdown"]["rows"][0]
    row.update(waste_units=dict(store_rule=4, store_rule_checked=3, shelfshift=0, markdowns_only=None),
               moved_out=0, moved_in=8, demand_not_met=dict(store_rule=2, shelfshift=0))
    return response


def test_by_branch_additive_ledger_columns_paging_pin_and_origin(visitor):
    page = visitor.page
    body = variant()
    own = session_json(page)["branch"]
    asked = []

    def serve(route):
        q = query(route)
        asked.append(q)
        route.fulfill(json=branch_figures_body(body, int(q.get("page", 1)), own) if q.get("breakdown") else body)

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    branch = block(page, "By branch")
    expect(branch.get_by_role("columnheader", name="Demand not met, units · ShelfShift", exact=True)).to_be_visible()
    expect(branch.get_by_text("Your store", exact=True)).to_be_visible()
    row = branch.get_by_role("row").nth(1)
    expect(row.get_by_role("cell", name="0", exact=True).first).to_be_visible()
    assert row.get_by_role("rowheader").evaluate("e => getComputedStyle(e).position") == "sticky"
    assert branch.get_by_role("table").evaluate("e => e.getBoundingClientRect().width") >= 1200
    assert_reason(page, branch.get_by_role("button", name=re.compile("Why unavailable: .*waste_units markdowns_only")), "Not recorded in this published breakdown")
    branch.get_by_role("button", name="Next page", exact=True).click()
    expect(branch.get_by_role("row").nth(1)).to_contain_text("ILLUSTRATIVE-PAGE-TWO")
    for q in asked[1:]:
        assert q["benchmark_id"] == body["current"]["benchmark_id"]
        assert q["chart"] == "money" and q["breakdown"] == "branch"
    assert [q.get("page", "1") for q in asked[1:]] == ["1", "2"]


def test_branch_response_with_another_benchmark_is_rejected(visitor):
    page = visitor.page
    body = variant()

    def serve(route):
        q = query(route)
        response = branch_figures_body(body) if q.get("breakdown") else body
        if q.get("breakdown"):
            response["current"]["breakdown"]["benchmark_id"] = "another-published-benchmark"
        route.fulfill(json=response)

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    branch = block(page, "By branch")
    expect(branch.get_by_role("alert")).to_contain_text("The breakdown does not match this benchmark")
    expect(branch.get_by_role("table")).to_have_count(0)


def query(route):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))


def open_breakdown(page):
    button = card(page).get_by_role("button", name="View breakdown: Net revenue after operating costs", exact=True)
    button.click()
    expect(card(page).get_by_role("button", name="Hide breakdown: Net revenue after operating costs")).to_be_focused()
    return page.get_by_role("region", name="Money breakdown", exact=True)


def test_breakdown_saved_id_day_branch_paging_zero_and_error(visitor):
    page = visitor.page
    body = variant()
    own = session_json(page)["branch"]
    asked = []
    fail = [False]

    def serve(route):
        q = query(route)
        asked.append(q)
        if q.get("breakdown"):
            assert q["benchmark_id"] == body["current"]["benchmark_id"]
            assert q["chart"] == "money"
            if fail[0]:
                route.fulfill(status=500, json=dict(error="internal", message="Illustrative breakdown failure"))
            else:
                route.fulfill(json=breakdown_body(body, q["breakdown"], int(q.get("page", 1)), own))
        else:
            route.fulfill(json=body)

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    section = open_breakdown(page)
    expect(section.get_by_role("row")).to_have_count(8)
    expect(section.get_by_role("cell", name="$0", exact=True).first).to_be_visible()
    section.get_by_role("button", name="By branch", exact=True).click()
    expect(section.get_by_role("row").nth(1)).to_contain_text(own)
    expect(section.get_by_text("Your store", exact=True)).to_be_visible()
    section.get_by_role("button", name="Next page", exact=True).click()
    expect(section.get_by_role("row").nth(1)).to_contain_text("ILLUSTRATIVE-PAGE-TWO")
    fail[0] = True
    section.get_by_role("button", name="By day", exact=True).click()
    expect(section.get_by_role("alert")).to_have_text(re.compile("Illustrative breakdown failure"))
    expect(section.get_by_role("table")).to_have_count(0)
    expect(card(page).locator("canvas")).to_be_visible()
    fail[0] = False
    section.get_by_role("button", name="Try again", exact=True).click()
    expect(section.get_by_role("row")).to_have_count(8)
    card(page).get_by_role("button", name="Hide breakdown: Net revenue after operating costs").click()
    expect(section).to_have_count(0)
    assert [q.get("page", "1") for q in asked if q.get("breakdown") == "branch"] == ["1", "2"]


def historical(body):
    body = copy.deepcopy(body)
    body["historical"], body["current"], body["status"] = body["current"], None, "historical"
    body["historical"]["differs"] = ["engine", "dataset"]
    body["app"]["engine"] = "app-engine-version-123"
    body["app"]["dataset"] = "app-dataset-version-123"
    return body


def test_historical_is_collapsed_marked_and_pinned_to_origin(visitor):
    page = visitor.page
    body = historical(variant(True))
    body["settings_note"] = [dict(field="waste_price", workspace=1, benchmark=0)]
    asked = []

    def serve(route):
        q = query(route)
        asked.append(q)
        route.fulfill(json=breakdown_body(body, q["breakdown"], int(q.get("page", 1))) if q.get("breakdown") else body)

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    disclosure = panel(page).get_by_role("button", name=re.compile("Historical benchmark"))
    expect(disclosure).to_have_attribute("aria-expanded", "false")
    expect(panel(page).locator("canvas")).to_have_count(0)
    expect(card(page)).to_have_count(0)
    assert "$" not in panel(page).inner_text()
    disclosure.click()
    expect(disclosure).to_be_focused()
    expect(disclosure).to_have_attribute("aria-expanded", "true")
    expect(card(page).get_by_text("Different version · not representative of the current app", exact=True)).to_be_visible()
    details = page.get_by_role("region", name="Evaluation conditions and versions")
    for field in ("engine", "dataset"):
        assert body["app"][field] != body["historical"]["versions"][field]
        expect(version_value(details, field)).to_have_text(
            body["historical"]["versions"][field] + "Differs · app " + body["app"][field])
        expect(panel(page).get_by_text("Differs · app " + body["app"][field], exact=True)).to_be_visible()
    expect(panel(page).get_by_text(re.compile("This workspace's settings differ"))).to_have_count(0)
    section = open_breakdown(page)
    expect(section.get_by_role("row")).to_have_count(8)
    assert asked[-1]["benchmark_id"] == body["historical"]["benchmark_id"]
    disclosure.click()
    expect(card(page)).to_have_count(0)


def test_deferred_old_id_after_publication_cannot_mix_header_and_amounts(visitor):
    page = visitor.page
    a, b = variant(True), variant()
    a["current"]["benchmark_id"], b["current"]["benchmark_id"] = "illustrative-A", "illustrative-B"
    held, asked = [], []
    published = [False]

    def serve(route):
        q = query(route)
        asked.append(q)
        if q.get("breakdown"):
            if q["benchmark_id"] == "illustrative-A" and not published[0]:
                held.append(route)
            else:
                body = historical(a) if q["benchmark_id"] == "illustrative-A" else b
                route.fulfill(json=breakdown_body(body, q["breakdown"], int(q.get("page", 1)), marker=888 if q["benchmark_id"] == "illustrative-A" else 777))
        else:
            route.fulfill(json=historical(a) if q.get("benchmark_id") == "illustrative-A" else b if published[0] else a)

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    section = open_breakdown(page)
    expect(section.get_by_text("Loading the breakdown…", exact=True)).to_be_visible()
    assert len(held) == 1
    published[0] = True
    page.evaluate("location.hash='#/reports?tab=benchmark&benchmark_id=illustrative-B'")
    expect(card(page).get_by_text(re.compile("ShelfShift total \\$512,340 vs"))).to_be_visible()
    section = open_breakdown(page)
    expect(section.get_by_role("cell", name="$777", exact=True)).to_be_visible()
    held[0].fulfill(json=breakdown_body(historical(a), marker=999))
    page.wait_for_timeout(100)
    expect(section.get_by_role("cell", name="$777", exact=True)).to_be_visible()
    expect(section.get_by_role("cell", name="$999", exact=True)).to_have_count(0)
    page.evaluate("location.hash='#/reports?tab=benchmark&benchmark_id=illustrative-A'")
    disclosure = panel(page).get_by_role("button", name=re.compile("Historical benchmark"))
    disclosure.click()
    section = open_breakdown(page)
    expect(section.get_by_role("cell", name="$888", exact=True)).to_be_visible()
    assert asked[-1]["benchmark_id"] == "illustrative-A"


@pytest.mark.parametrize("bad_id", ["illustrative-unknown", "illustrative-unpublished"])
def test_explicit_unavailable_id_has_error_and_no_latest_fallback(visitor, bad_id):
    page = visitor.page
    asked = []

    def serve(route):
        q = query(route)
        asked.append(q)
        if q.get("benchmark_id"):
            route.fulfill(status=404, json=dict(error="not_found", message="No published benchmark with that ID"))
        else:
            route.fulfill(json=variant())

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    expect(card(page)).to_be_visible()
    page.evaluate("id => {location.hash='#/reports?tab=benchmark&benchmark_id='+id}", bad_id)
    expect(panel(page).get_by_text("No published benchmark with that ID", exact=True)).to_be_visible()
    expect(card(page)).to_have_count(0)
    assert asked[-1] == {"benchmark_id": bad_id}


def test_desktop_header_and_canvas_without_scrolling(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        page.set_viewport_size({"width": 1280, "height": 800})
        answer(page, benchmark=variant(True))
        page.goto("/#/reports?tab=benchmark")
        chart = card(page)
        expect(chart.locator("canvas")).to_be_visible()
        page.wait_for_timeout(500)
        folder = pathlib.Path("test-results/reports-08-08")
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / "1280x800-expanded-header-chart.png"))
        for el in (panel(page).get_by_role("heading", name="Benchmark evaluation"),
                   panel(page).get_by_text("Current configuration", exact=True),
                   chart.get_by_role("button", name="View breakdown: Net revenue after operating costs"), chart.locator("canvas")):
            box = el.bounding_box()
            assert box and box["y"] >= 0 and box["y"] + box["height"] <= 800
    finally:
        context.close()


SCENARIOS = ["store_rule", "store_rule_checked", "shelfshift", "markdowns_only"]
# UI-SPEC "Chart colours", waste chart: (point style, line colour, legend mark, legend mark colour) per scenario.
WASTE_STYLE = {"store_rule": ("triangle", "#8b93b8", "▲", "rgb(139, 147, 184)"),
               "store_rule_checked": ("rect", "#5b6487", "■", "rgb(91, 100, 135)"),
               "shelfshift": ("circle", "#13207a", "●", "rgb(19, 32, 122)"),
               "markdowns_only": ("rectRot", "#b54708", "◆", "rgb(181, 71, 8)")}


def waste_card(page):
    return page.get_by_role("region", name="Waste within the window", exact=True)


def waste_variant(zero=False):
    body = variant(True)
    waste = body["current"]["summary"]["waste"]
    for row in waste["by_day"]:
        for i, key in enumerate(SCENARIOS):
            row[key] = 0 if zero else i + 1
    for i, key in enumerate(SCENARIOS):
        waste["totals"][key] = dict(units=0 if zero else 7 * (i + 1), kg=0 if zero else 3.5 * (i + 1))
        waste["change_vs_markdowns_only"][key] = dict(units=0 if zero else 7 * (i - 3), kg=0 if zero else 3.5 * (i - 3))
    return body


def waste_breakdown_body(body, by="day", page=1, branch="ILLUSTRATIVE-PINNED", marker=0):
    body = copy.deepcopy(body)
    run = body["current"] or body["historical"]
    rows = [dict(day=day, **{k: i + 1 for i, k in enumerate(SCENARIOS)}) for day in range(7)] if by == "day" else [
        dict(branch=branch if page == 1 else "ILLUSTRATIVE-PAGE-TWO", pinned=page == 1,
             **{k: marker + i + 1 for i, k in enumerate(SCENARIOS)}, change_vs_markdowns_only=-3)]
    run["breakdown"] = dict(benchmark_id=run["benchmark_id"], kind="SIMULATED", chart="waste", by=by,
                            rows=rows, page=page, page_size=1 if by == "branch" else 7, total=2 if by == "branch" else 7)
    return body


@pytest.mark.parametrize("zero", [False, True])
def test_waste_lines_notes_cumulative_table_and_daily_breakdown(visitor, zero):
    page = visitor.page
    body = waste_variant(zero)
    asked = []
    def serve(route):
        q = query(route)
        asked.append(q)
        route.fulfill(json=waste_breakdown_body(body, q["breakdown"], int(q.get("page", 1))) if q.get("breakdown") else body)
    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    chart = waste_card(page)
    expect(chart.locator("canvas")).to_be_visible()
    expect(chart.get_by_role("list", name="Waste scenarios").get_by_role("listitem").locator("span:last-child")).to_have_text([
        s["label"] for s in body["current"]["summary"]["scenarios"]])
    page.wait_for_function(r"""async () => {
      const asset = performance.getEntriesByType('resource').find(r => /\/assets\/chart-.*\.js/.test(r.name));
      if (!asset) return false;
      const {Chart} = await import(asset.name);
      return [...document.querySelectorAll('canvas')].some(c => Chart.getChart(c)?.data.datasets.length === 4);
    }""")
    drawn = chart.locator("canvas").evaluate(r"""async canvas => {
      const asset = performance.getEntriesByType('resource').find(r => /\/assets\/chart-.*\.js/.test(r.name));
      const {Chart} = await import(asset.name);
      return Chart.getChart(canvas).data.datasets.map(d => ({label:d.label, data:d.data, pointStyle:d.pointStyle,
        borderColor:d.borderColor, borderDash:d.borderDash}));
    }""")
    labels = {s["key"]: s["label"] for s in body["current"]["summary"]["scenarios"]}
    by_label = {d["label"]: d for d in drawn}
    for key, (style, colour, _, _) in WASTE_STYLE.items():
        assert (by_label[labels[key]]["pointStyle"], by_label[labels[key]]["borderColor"].lower()) == (style, colour), key
    legend = chart.get_by_role("list", name="Waste scenarios").get_by_role("listitem")
    for i, key in enumerate(SCENARIOS):
        mark = legend.nth(i).locator("span[aria-hidden=true]")
        expect(mark).to_have_text(WASTE_STYLE[key][2])
        assert mark.evaluate("e => getComputedStyle(e).color") == WASTE_STYLE[key][3], key
    assert [d["label"] for d in drawn] == [labels[k] for k in SCENARIOS]
    assert all(d["borderDash"] == [] for d in drawn)
    assert [d["data"] for d in drawn] == [[0 if zero else (i + 1) * (day + 1) for day in range(7)] for i in range(4)]
    for sentence in chart.locator("p").all_text_contents():
        if "units vs" in sentence:
            assert not ("ShelfShift" in sentence and "Modeled store rule" in sentence)
    chart.get_by_role("button", name="Show as table").click()
    rows = chart.get_by_role("row")
    expect(rows.nth(2).get_by_role("cell").nth(3)).to_have_text("0" if zero else "6")
    calendar = session_json(page)["calendar"]
    start = next(d["date"] for d in calendar if d["day"] == 0)
    end = next(d["date"] for d in calendar if d["day"] == 6)
    import datetime
    range_text = f"{int(start[-2:])}–{int(end[-2:])} {datetime.date.fromisoformat(end):%b}"
    expect(rows.nth(8)).to_contain_text("Total · " + range_text)
    expect(rows.nth(9)).to_contain_text("Change vs Automatic markdowns only")
    expect(chart.get_by_text(INBOUND, exact=True)).to_be_visible()
    chart.get_by_role("button", name="View breakdown: Waste within the window").click()
    section = page.get_by_role("region", name="Waste breakdown", exact=True)
    expect(section.get_by_role("row").nth(2).get_by_role("cell").nth(3)).to_have_text("3")
    section.get_by_role("button", name="By branch", exact=True).click()
    expect(section.get_by_text("Your store", exact=True)).to_be_visible()
    expect(section.get_by_role("cell", name="−3", exact=True)).to_be_visible()
    section.get_by_role("button", name="Next page").click()
    expect(section.get_by_text("ILLUSTRATIVE-PAGE-TWO", exact=True)).to_be_visible()
    assert all(q["benchmark_id"] == body["current"]["benchmark_id"] and q["chart"] == "waste" for q in asked if q.get("breakdown"))


@pytest.mark.parametrize("width", [360, 390, 1024, 1280])
@pytest.mark.parametrize("sidebar", [False, True])
def test_two_chart_grid_fits_viewport(visitor, width, sidebar):
    page = visitor.page
    page.set_viewport_size(dict(width=width, height=800))
    answer(page, benchmark=waste_variant())
    page.goto("/#/reports?tab=benchmark")
    expect(waste_card(page).locator("canvas")).to_be_visible()
    toggle = page.get_by_role("button", name=re.compile("(Collapse|Expand|Open|Close) (navigation|sidebar|menu)", re.I))
    if sidebar and toggle.count():
        toggle.first.click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_sensitivity_states_and_recorded_assumptions(visitor):
    page = visitor.page
    answer(page, benchmark=NONE)
    page.goto("/#/reports?tab=benchmark")
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
    for name in ("Headline figures", "How sensitive are these results?"):
        expect(page.get_by_role("heading", name=name, exact=True)).to_have_count(0)
    body = waste_variant()
    body["current"]["summary"]["assumptions"]["markdown_response"] = {"recorded_multiplier": 1.25}
    answer(page, benchmark=body)
    page.reload()
    sensitivity = named_section(page, "How sensitive are these results?")
    expect(sensitivity.get_by_text("Sensitivity checks not run for this prototype.", exact=True)).to_be_visible()
    expect(sensitivity.get_by_text("Recorded multiplier: 1.25", exact=True)).to_be_visible()
    expect(sensitivity.get_by_text(re.compile("^Phantom stock and stale count checks only · at most 2 per recommendation version"))).to_be_visible()
    assert not re.search(r'[{}\[\]"_]|summary\.|PHANTOM_STOCK|STALE_COUNT', sensitivity.inner_text()), sensitivity.inner_text()
    expect(sensitivity.get_by_text("Assumption", exact=True)).to_have_count(4)
    expect(sensitivity.get_by_role("table")).to_have_count(0)
    expect(sensitivity.get_by_text("This release ran the benchmark once, under the assumptions below. It makes no claim about how the results change if they don't hold.", exact=True)).to_be_visible()
    sources = sensitivity.get_by_role("button", name="Source of this assumption")
    expect(sources).to_have_count(4)
    for i, text in enumerate(["Recorded with this benchmark's assumptions"] * 2 +
                             ["Recorded in this benchmark's evaluation settings"] * 2):
        sources.nth(i).focus()
        tip = page.get_by_role("tooltip")
        expect(tip).to_have_text(text)
        page.keyboard.press("Escape")
    order = [named_section(page, "Benchmark evaluation"), card(page), waste_card(page),
             page.get_by_role("region", name="Evaluation conditions and versions"), named_section(page, "Headline figures"),
             block(page, "Stock checks & trips", False), block(page, "Decision quality", False),
             block(page, "Forecast quality", False), block(page, "By branch", False), sensitivity]
    for first, second in zip(order, order[1:]):
        assert first.evaluate("(a,b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)", second.element_handle())
    answer(page, benchmark=historical(body))
    page.set_viewport_size(dict(width=390, height=800))
    page.reload()
    old = panel(page).get_by_role("button", name=re.compile("^Historical benchmark"))
    expect(old).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_role("heading", name="How sensitive are these results?", exact=True)).to_have_count(0)
    old.click()
    for section in (card(page), waste_card(page), named_section(page, "Headline figures")):
        expect(section.get_by_text("Different version · not representative of the current app", exact=True)).to_be_visible()
    expect(named_section(page, "How sensitive are these results?")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    old.click()
    expect(page.get_by_role("heading", name="Headline figures", exact=True)).to_have_count(0)


@pytest.mark.parametrize("width", [360, 390, 768, 1280])
def test_every_block_and_breakdown_open_fits_and_capture(visitor, width):
    page = visitor.page
    page.set_viewport_size(dict(width=width, height=800))
    page.emulate_media(reduced_motion="reduce")
    body = waste_variant()
    body["current"]["summary"]["forecast_quality"]["wape"] = None
    body["current"]["summary"]["forecast_quality"]["bias"] = None
    own = session_json(page)["branch"]

    def serve(route):
        q = query(route)
        if not q.get("breakdown"):
            response = body
        elif q["chart"] == "waste":
            response = waste_breakdown_body(body, q["breakdown"], int(q.get("page", 1)), own)
        elif q["breakdown"] == "branch":
            response = branch_figures_body(body, int(q.get("page", 1)), own)
        else:
            response = breakdown_body(body)
        route.fulfill(json=response)

    page.route("**/api/reports/benchmark*", serve)
    page.goto("/#/reports?tab=benchmark")
    expect(waste_card(page).locator("canvas")).to_be_visible()
    folder = pathlib.Path("test-results/reports-08-09")
    folder.mkdir(parents=True, exist_ok=True)
    if width in (390, 1280):
        # The canvas exists before the lazy chart module has drawn it. Capture the rendered plots.
        for chart in (card(page), waste_card(page)):
            page.wait_for_function("""canvas => canvas.width > 0 && canvas.height > 0 &&
                canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data
                  .some((value, index) => index % 4 === 3 && value > 0)""",
                arg=chart.locator("canvas").element_handle())
        page.screenshot(path=str(folder / f"{width}x800-overview.png"))
    open_breakdown(page)
    waste_card(page).get_by_role("button", name="View breakdown: Waste within the window", exact=True).click()
    named_section(page, "Headline figures").get_by_role("button", name="Show all scenario figures").click()
    opened = []
    for name in ("Stock checks & trips", "Decision quality", "Forecast quality", "By branch"):
        section = block(page, name)
        opened.append((name, section))
        button = section.get_by_role("button", name=re.compile("^" + re.escape(name)))
        result = button.locator("span").last
        assert len(result.inner_text()) <= 90
        if width == 390:
            title_box = button.locator("span").nth(1).bounding_box()
            assert result.bounding_box()["y"] >= title_box["y"] + title_box["height"]
    expect(opened[-1][1].get_by_role("table")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), page.evaluate("""() => JSON.stringify({widths:[innerWidth,document.documentElement.scrollWidth],outside:[...document.querySelectorAll('*')].filter(e => !e.closest('table') && e.getBoundingClientRect().right > innerWidth && getComputedStyle(e).overflowX !== 'hidden').slice(0,20).map(e => [e.tagName,e.className,e.clientWidth,e.scrollWidth,e.getBoundingClientRect().right])})""")
    overflowing = panel(page).evaluate("""root => [...root.querySelectorAll('*')].filter(e =>
      e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 1 &&
      ['auto','scroll'].includes(getComputedStyle(e).overflowX)).map(e => !!e.querySelector('table'))""")
    assert all(overflowing), "Only table containers may scroll sideways"
    for chart in (card(page), waste_card(page)):
        expect(chart.get_by_text(INBOUND, exact=True)).to_be_visible()
        chart.get_by_role("button", name="Show as table", exact=True).click()
        expect(chart.get_by_text(INBOUND, exact=True)).to_be_visible()
        expect(chart.get_by_text(re.compile("has no markdown steps"))).to_be_visible()
    expect(card(page).get_by_text(CHECKS, exact=True)).to_be_visible()
    expect(card(page).get_by_text(re.compile("Holding decisions and stock outcomes fixed"))).to_be_visible()
    expect(card(page).get_by_text(re.compile("Axis starts at"))).to_be_visible()
    if width in (390, 1280):
        # Actual 800px-high viewport captures, with every disclosure left expanded.
        # Separate scroll positions preserve readable type and capture all notes and block content.
        targets = [("money-notes", card(page).get_by_text(INBOUND, exact=True)),
                   ("waste-notes", waste_card(page).get_by_text(INBOUND, exact=True)),
                   ("headline", named_section(page, "Headline figures")), *opened,
                   ("sensitivity", named_section(page, "How sensitive are these results?"))]
        for name, target in targets:
            target.scroll_into_view_if_needed()
            page.screenshot(path=str(folder / f"{width}x800-{name.replace(' ', '-').replace('&', 'and')}-expanded.png"))
        # Taller element captures retain every row/note at the same tested width for human review.
        page.set_viewport_size(dict(width=width, height=1600))
        for name, section in [("money", card(page)), ("waste", waste_card(page)), *opened]:
            section.screenshot(path=str(folder / f"{width}-{name.replace(' ', '-').replace('&', 'and')}-full.png"))
        for name, chart in [("money", card(page)), ("waste", waste_card(page))]:
            chart.get_by_role("button", name="Show as chart", exact=True).click()
            chart.screenshot(path=str(folder / f"{width}-{name}-chart-all-notes-full.png"))
        page.set_viewport_size(dict(width=width, height=800))
        reason = opened[2][1].get_by_role("button", name="Why unavailable: Forecast WAPE", exact=True)
        reason.focus()
        page.keyboard.press("Enter")
        expect(reason).to_be_focused()
        page.screenshot(path=str(folder / f"{width}x800-forecast-reason-focused.png"))


def test_missing_values_support_touch_activation(browser, web):
    context = browser.new_context(base_url=web.url, has_touch=True, viewport=dict(width=1280, height=800))
    try:
        context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        page = context.new_page()
        sign_in(page, web)
        page.set_viewport_size(dict(width=390, height=800))
        body = variant()
        body["current"]["summary"]["trips"]["markdowns_only"]["short"] = None
        answer(page, benchmark=body)
        page.goto("/#/reports?tab=benchmark")
        headline = named_section(page, "Headline figures")
        headline.get_by_role("button", name="Show all scenario figures").tap()
        assert_reason(page, headline.get_by_role("button", name="Why unavailable: Sell-through of moved stock · markdowns_only", exact=True), "No received transfers")
        checks = block(page, "Stock checks & trips")
        assert_reason(page, checks.get_by_role("button", name="Why unavailable: Short trips · markdowns_only", exact=True), "No transfers in this scenario")
        forecast = block(page, "Forecast quality")
        assert_reason(page, forecast.get_by_role("button", name="Why unavailable: Forecast WAPE", exact=True), "Undefined for a zero-total series or when no eligible in-stock observations were recorded.")
    finally:
        context.close()


def test_zero_forecast_bias_has_no_direction(visitor):
    page = visitor.page
    page.set_viewport_size(dict(width=390, height=800))
    body = waste_variant()
    forecast = body["current"]["summary"]["forecast_quality"]
    for key in ("wape", "baseline_wape", "bias", "baseline_bias"):
        forecast[key] = 0
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    section = block(page, "Forecast quality")
    expect(section.get_by_text("Forecast bias · no net bias", exact=True)).to_be_visible()
    expect(section.get_by_text("Seasonal baseline bias · no net bias", exact=True)).to_be_visible()
    expect(section.get_by_text(re.compile("forecast (above|below) sales"))).to_have_count(0)
    expect(section.get_by_role("cell", name="0%", exact=True)).to_have_count(4)


@pytest.mark.parametrize("width", [360, 390, 1280])
def test_headline_notes_wrap_in_full(visitor, width):
    page = visitor.page
    page.set_viewport_size(dict(width=width, height=800))
    answer(page, benchmark=variant())
    page.goto("/#/reports?tab=benchmark")
    headline = named_section(page, "Headline figures")
    notes = headline.locator("div.grid > div > p:last-child")
    expect(notes).to_have_count(4)
    expect(notes.nth(2)).to_contain_text("units vs Automatic markdowns only")
    for i in range(4):
        note = notes.nth(i)
        note.scroll_into_view_if_needed()
        clipped = note.evaluate("""e => {
          const range = document.createRange(); range.selectNodeContents(e);
          const box = e.getBoundingClientRect();
          return getComputedStyle(e).textOverflow === 'ellipsis' || e.scrollWidth > e.clientWidth + 1 ||
            [...range.getClientRects()].some(r => r.right > box.right + 1 || r.bottom > box.bottom + 1);
        }""")
        assert not clipped, (width, note.inner_text())
        assert note.get_attribute("title") is None, "the note is read in full, not from a hover title"


def test_refused_live_settings_show_no_app_digest(visitor):
    """The server answers app.settings null when the live settings can't be evaluated (08 supervisor gap 5)."""
    page = visitor.page
    body = copy.deepcopy(variant())
    body["historical"], body["current"], body["status"] = body["current"], None, "historical"
    body["app"] = {**body["historical"]["versions"], "settings": None}
    body["historical"]["differs"] = ["settings"]
    body["settings_refused"] = "ordering"
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    expect(panel(page).get_by_text("No benchmark available for the current configuration.", exact=True)).to_be_visible()
    panel(page).get_by_role("button", name=re.compile("^Historical benchmark")).click()
    details = page.get_by_role("region", name="Evaluation conditions and versions")
    expect(details.get_by_text("Differs · app not available for these settings", exact=True)).to_be_visible()
    expect(details.get_by_text(re.compile("^Differs · app"))).to_have_count(1)
