"""Reports rendering/request tests on illustrative contract bodies, never evaluation evidence or an auth oracle.
08-10 verifies the real server's scope and tile-to-Decision-board agreement.
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
import urllib.parse

import pytest
from playwright.sync_api import Route, expect

from tests.web.conftest import HQ, new_context, session_json, sign_in

pytestmark = pytest.mark.web


def fixture(name):
    body = json.loads((pathlib.Path(__file__).parent / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    body.pop("_about")
    return body


WORKSPACE = fixture("reports_workspace")
NONE = fixture("reports_benchmark_none")
CURRENT = fixture("reports_benchmark_current")
HISTORICAL = fixture("reports_benchmark_historical")
VERSIONS = CURRENT["app"]
RUN = CURRENT["current"]

LABELS = {"open": "Open decisions", "pending_approval": "Pending approvals", "unanswered_check": "Unanswered checks",
          "committed": "Committed plans", "cancelled": "Cancellations"}


def projection(overlap=False):
    return fixture("reports_workspace_overlap" if overlap else "reports_workspace_once")


def answer(page, benchmark=None, workspace=None):
    s = session_json(page)
    asked = {"workspace": [], "benchmark": []}

    def serve(route: Route, kind):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))
        asked[kind].append(q)
        body = copy.deepcopy((workspace or WORKSPACE) if kind == "workspace" else (benchmark or NONE))
        if kind == "workspace":
            body["scope"] = {"role": s["role"], "branch": q.get("branch") if s["role"] == "hq" else s["branch"]}
            if s["role"] == "hq":
                body["activity"]["open"]["need_action"] = 0
                body["activity"]["pending_approval"]["waiting_for_you"] = 0
                body["activity"]["unanswered_check"]["addressed_to_you"] = 0
        route.fulfill(json=body)

    page.route("**/api/reports/workspace*", lambda r: serve(r, "workspace"))
    page.route("**/api/reports/benchmark*", lambda r: serve(r, "benchmark"))
    return asked


def panel(page):
    return page.get_by_role("tabpanel")


VERSION_LABELS = {"engine": "Engine", "dataset": "Dataset", "policy": "Policy", "forecast_runs": "Forecast runs",
                  "evaluator": "Evaluator", "settings": "Settings"}


def version_value(details, key):
    return details.locator(f"xpath=.//dt[normalize-space()='{VERSION_LABELS[key]}']/following-sibling::dd[1]")


def test_workspace_tiles_notes_links_and_nav(visitor):
    page = visitor.page
    asked = answer(page)
    page.goto("/#/reports")
    expect(page.get_by_role("heading", level=1, name="Reports")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name="Reports", exact=True)).to_be_visible()
    notes = ("1 needs action", "1 waiting for you", "1 past the response time", "2 dispatched · 1 received", "Approved plans cancelled")
    for (key, label), note in zip(LABELS.items(), notes):
        n = WORKSPACE["activity"][key]["count"]
        tile = panel(page).get_by_role("group", name=f"{label}: {n}, {note}", exact=True)
        expect(tile).to_be_visible()
        if n:
            expect(tile.get_by_role("link", name=f"View breakdown: {label}")).to_have_attribute(
                "href", f"#/board?view=table&report={key}")
        else:
            expect(tile.get_by_role("link")).to_have_count(0)
            expect(tile.locator("p.num")).to_have_class(re.compile("text-tx3"))
    for label in ("Pending approvals", "Unanswered checks"):
        expect(panel(page).get_by_role("group", name=re.compile(f"^{label}:")).first).to_have_class(re.compile("border-l-4"))
    assert len(asked["workspace"]) == 1
    assert asked["benchmark"] == []


@pytest.mark.parametrize("status", ("none", "historical", "current"))
def test_server_decides_benchmark_status(visitor, status):
    page = visitor.page
    body = copy.deepcopy({"none": NONE, "historical": HISTORICAL, "current": CURRENT}[status])
    if status == "current":
        # Deliberately unequal versions: only the response status decides the presentation.
        body["app"] = {**body["app"], "engine": "illustrative-app-version-differs"}
    answer(page, benchmark=body)
    page.goto("/#/reports?tab=benchmark")
    if status == "none":
        expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
        expect(panel(page).get_by_text("The benchmark runs once on the shipped public week before release. Until it has run, Reports shows no comparison.")).to_be_visible()
        expect(panel(page).locator("canvas, [role=group]")).to_have_count(0)
        assert "$" not in panel(page).inner_text()
        assert not re.search(r"\b0\b", panel(page).inner_text())
    elif status == "historical":
        expect(panel(page).get_by_text("No benchmark available for the current configuration.", exact=True)).to_be_visible()
        expect(panel(page).get_by_role("heading", name="Benchmark evaluation")).to_have_count(0)
        old = body["historical"]
        assert old["differs"] == [k for k in VERSION_LABELS if old["versions"][k] != body["app"][k]] == ["engine"]
        panel(page).get_by_role("button", name=re.compile("^Historical benchmark")).click()
        details = page.get_by_role("region", name="Evaluation conditions and versions")
        expect(version_value(details, "engine")).to_have_text(
            old["versions"]["engine"] + "Differs · app " + body["app"]["engine"])
        expect(details.get_by_text(re.compile("^Differs · app"))).to_have_count(1)
    else:
        expect(panel(page).get_by_role("heading", name="Benchmark evaluation")).to_be_visible()
        s = session_json(page)
        details = page.get_by_role("region", name="Evaluation conditions and versions")
        versions = RUN["versions"]
        assert len({str(v) for v in versions.values()}) == len(versions)
        for key, value in versions.items():
            # Every version in full, except the settings digest (its first 12 characters).
            text = ", ".join(value) if isinstance(value, list) else value[:12] if key == "settings" else value
            expect(version_value(details, key)).to_have_text(text)
        end = next(d["date"] for d in s["calendar"] if d["day"] == RUN["window"]["last_day"])
        expect(panel(page).get_by_text(re.compile("Simulated evaluation ·.*" + end[:4])).first).to_be_visible()


@pytest.mark.parametrize("failed", ("workspace", "benchmark"))
def test_tab_failure_is_isolated_and_retry_recovers(visitor, failed):
    page = visitor.page
    answer(page)
    held = {"fail": True}
    message = "Reports service is temporarily unavailable."
    def serve(route):
        route.fulfill(status=503, json={"error": "db_busy", "message": message}) if held["fail"] else route.fulfill(
            json=WORKSPACE if failed == "workspace" else NONE)
    page.route(f"**/api/reports/{failed}*", serve)
    page.goto(f"/#/reports?tab={failed}")
    title = "Reports didn't load" if failed == "workspace" else "The benchmark didn't load"
    expect(panel(page).get_by_role("heading", name=title)).to_be_visible()
    expect(panel(page).get_by_text(message, exact=True)).to_be_visible()
    expect(panel(page).get_by_text("Nothing was changed.", exact=True)).to_be_visible()
    expect(panel(page).get_by_role("link", name="Back to Today")).to_have_attribute("href", "#/")
    held["fail"] = False
    panel(page).get_by_role("button", name="Try again").click()
    expect(panel(page).get_by_role("heading", name=title)).to_have_count(0)
    other = "Benchmark" if failed == "workspace" else "This workspace"
    page.get_by_role("tab", name=other, exact=True).click()
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available" if failed == "workspace" else "Workspace activity")).to_be_visible()


def wait_held(page, held, n):
    for _ in range(200):
        if len(held) >= n:
            return
        page.wait_for_timeout(20)
    assert len(held) >= n, "the new scoped read was not sent"


@pytest.mark.parametrize("transition", ("branch", "tab", "page", "unmount", "workspace_reset", "account"))
def test_deferred_response_never_replaces_new_scope(visitor, browser, web, transition):
    context = new_context(browser, web) if transition == "branch" else None
    page = context.new_page() if context else visitor.page
    if context:
        sign_in(page, web, HQ)
    s = session_json(page)
    answer(page)
    held = []
    page.route("**/api/reports/workspace*", lambda r: held.append(r))
    page.goto("/#/reports?tab=workspace&branch=OLD")
    expect(panel(page).get_by_text("Loading reports…", exact=True)).to_be_visible()
    page.wait_for_function("() => true")
    wait_held(page, held, 1)
    first = held[0]
    if transition in ("workspace_reset", "account"):
        changed = {**s, "run_id": "illustrative-reset-workspace"} if transition == "workspace_reset" else {**s, "account": "illustrative-other-account"}
        page.route("**/api/session", lambda r: r.fulfill(json=changed))
        # The app's existing reset boundary refreshes session and leaves the page. Re-enter without reloading.
        page.route("**/api/reports/benchmark*", lambda r: r.fulfill(status=409, json={"error": "sandbox_reset", "message": "Workspace changed."}))
        page.get_by_role("tab", name="Benchmark", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Today", exact=True)).to_be_visible()
        page.evaluate("location.hash = '#/reports?tab=workspace'")
    elif transition == "tab":
        page.get_by_role("tab", name="Benchmark", exact=True).click()
        expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
        page.get_by_role("tab", name="This workspace", exact=True).click()
    elif transition == "unmount":
        page.evaluate("location.hash = '#/about'")
        expect(page.get_by_role("heading", level=1, name=re.compile("About"))).to_be_visible()
        page.evaluate("location.hash = '#/reports'")
    else:
        page.evaluate("hash => { location.hash = hash; }", "#/reports?tab=workspace&" + ("branch=NEW" if transition == "branch" else "page=2"))
    expect(panel(page).get_by_text("Loading reports…", exact=True)).to_be_visible()
    wait_held(page, held, 2)
    fresh = copy.deepcopy(WORKSPACE)
    fresh["activity"]["open"]["count"] = 19
    held[-1].fulfill(json=fresh)
    expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 19,"))).to_be_visible()
    old = copy.deepcopy(WORKSPACE)
    old["activity"]["open"]["count"] = 999
    first.fulfill(json=old)
    page.wait_for_timeout(100)
    expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 19,"))).to_be_visible()
    expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 999,"))).to_have_count(0)
    if context:
        context.close()


def test_header_and_tabs_render_during_loading(visitor):
    page = visitor.page
    page.route("**/api/reports/workspace*", lambda r: None)
    page.goto("/#/reports")
    expect(page.get_by_role("heading", level=1, name="Reports")).to_be_visible()
    expect(page.get_by_role("tablist", name="Report views")).to_be_visible()
    expect(panel(page).get_by_text("Loading reports…", exact=True)).to_be_visible()


@pytest.mark.parametrize("transition", ("tab", "page"))
def test_deferred_benchmark_keeps_new_selection(visitor, transition):
    page = visitor.page
    answer(page)
    held = []
    page.route("**/api/reports/benchmark*", lambda r: held.append(r))
    page.goto("/#/reports?tab=benchmark")
    expect(panel(page).get_by_text("Loading the benchmark…", exact=True)).to_be_visible()
    wait_held(page, held, 1)
    if transition == "tab":
        page.get_by_role("tab", name="This workspace", exact=True).click()
        expect(panel(page).get_by_role("heading", name="Workspace activity")).to_be_visible()
        page.get_by_role("tab", name="Benchmark", exact=True).click()
    else:
        page.evaluate("location.hash = '#/reports?tab=benchmark&page=2'")
    wait_held(page, held, 2)
    held[-1].fulfill(json=NONE)
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
    held[0].fulfill(json=CURRENT)
    page.wait_for_timeout(100)
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
    expect(panel(page).get_by_role("heading", name="Benchmark evaluation")).to_have_count(0)


def test_hq_applies_branch_only_on_submit_then_clears_both_reads(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        asked = answer(page)
        page.goto("/#/reports")
        expect(panel(page).get_by_role("heading", name="Workspace activity")).to_be_visible()
        code = web.store
        page.get_by_label("Branch", exact=True).fill(" " + code + " ")
        expect(page.get_by_label("Branch", exact=True)).to_have_value(" " + code + " ")
        assert len(asked["workspace"]) == 1
        assert asked["workspace"][0] == {}
        page.get_by_role("button", name="Show branch", exact=True).click()
        expect(page).to_have_url(re.compile("branch=" + re.escape(urllib.parse.quote_plus(code))))
        expect(panel(page).get_by_text(f"Figures from your workspace for {code}.", exact=False)).to_be_visible()
        expect(panel(page).get_by_role("link", name="View breakdown: Open decisions")).to_have_attribute(
            "href", f"#/board?view=table&report=open&branch={urllib.parse.quote(code, safe='')}")
        page.get_by_role("tab", name="Benchmark", exact=True).click()
        expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
        assert asked["workspace"][-1] == {"branch": code}
        assert asked["benchmark"][-1] == {"branch": code}
        with page.expect_response(lambda r: "/api/reports/benchmark" in r.url and "branch=" not in r.url):
            page.get_by_role("button", name="All branches", exact=True).click()
        expect(page).not_to_have_url(re.compile("branch="))
        expect(page.get_by_label("Branch", exact=True)).to_have_value("")
        expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
        page.get_by_role("tab", name="This workspace", exact=True).click()
        expect(panel(page).get_by_text("Figures from your workspace for all branches.", exact=False)).to_be_visible()
        assert asked["workspace"][-1] == asked["benchmark"][-1] == {}
    finally:
        context.close()


@pytest.mark.parametrize("status", (400, 404))
def test_unknown_hq_branch_shows_api_message_and_retains_previous_scope(browser, web, status):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        answer(page)
        page.goto("/#/reports")
        expect(panel(page).get_by_role("heading", name="Workspace activity")).to_be_visible()
        message = "No branch ILLUSTRATIVE-UNKNOWN"
        page.route("**/api/reports/workspace*branch=ILLUSTRATIVE-UNKNOWN*", lambda r: r.fulfill(
            status=status, json={"error": "bad_request" if status == 400 else "not_found", "message": message}))
        page.get_by_label("Branch", exact=True).fill("ILLUSTRATIVE-UNKNOWN")
        page.get_by_role("button", name="Show branch", exact=True).click()
        expect(page.locator("#reports-branch-error")).to_have_text(message)
        expect(page.get_by_label("Branch", exact=True)).to_have_attribute("aria-invalid", "true")
        expect(panel(page).get_by_text("Figures from your workspace for all branches.", exact=False)).to_be_visible()
        expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 5,"))).to_be_visible()
        expect(panel(page).get_by_role("link", name="View breakdown: Open decisions")).to_have_attribute(
            "href", "#/board?view=table&report=open")
    finally:
        context.close()


def test_manager_has_no_branch_form_and_uses_server_scope(visitor):
    page = visitor.page
    asked = answer(page)
    page.goto("/#/reports?branch=ILLUSTRATIVE-OTHER")
    expect(panel(page).get_by_role("heading", name="Workspace activity")).to_be_visible()
    expect(page.get_by_label("Branch", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Show branch")).to_have_count(0)
    assert asked["workspace"] == [{}]
    own = session_json(page)["branch"]
    expect(panel(page).get_by_text(f"Figures from your workspace for {own}:", exact=False)).to_be_visible()


@pytest.mark.parametrize("tab,selected", (("benchmark", "Benchmark"), ("zzz", "This workspace"), ("workspace", "This workspace")))
def test_hash_selects_only_known_tabs(visitor, tab, selected):
    page = visitor.page
    answer(page)
    page.goto(f"/#/reports?tab={tab}")
    expect(page.get_by_role("tab", name=selected, exact=True)).to_have_attribute("aria-selected", "true")


def test_tabs_by_keyboard_and_panel_relationships(visitor):
    page = visitor.page
    answer(page)
    page.goto("/#/reports")
    expect(panel(page).get_by_role("heading", name="Workspace activity")).to_be_visible()
    # Enter the tabs through the real keyboard order, with no programmatic focus or click.
    for _ in range(70):
        page.keyboard.press("Tab")
        if page.get_by_role("tab", name="This workspace", exact=True).evaluate("el => el === document.activeElement"):
            break
    expect(page.get_by_role("tab", name="This workspace", exact=True)).to_be_focused()
    for key, name in (("ArrowRight", "Benchmark"), ("ArrowRight", "This workspace"),
                      ("ArrowLeft", "Benchmark"), ("Home", "This workspace"), ("End", "Benchmark")):
        page.keyboard.press(key)
        tab = page.get_by_role("tab", name=name, exact=True)
        expect(tab).to_be_focused()
        expect(tab).to_have_attribute("aria-selected", "true")
        expect(tab).to_have_attribute("tabindex", "0")
        other = page.get_by_role("tab", name="This workspace" if name == "Benchmark" else "Benchmark", exact=True)
        expect(other).to_have_attribute("tabindex", "-1")
        expect(panel(page)).to_have_attribute("id", tab.get_attribute("aria-controls"))
        expect(panel(page)).to_have_attribute("aria-labelledby", tab.get_attribute("id"))
        assert tab.evaluate("el => el.matches(':focus-visible') && parseFloat(getComputedStyle(el).outlineWidth) >= 2")
    page.keyboard.press("Tab")
    expect(panel(page)).to_be_focused()
    page.go_back()
    expect(page.get_by_role("tab", name="This workspace", exact=True)).to_have_attribute("aria-selected", "true")


@pytest.mark.parametrize("width", (360, 390, 768, 1280))
@pytest.mark.parametrize("tab", ("workspace", "benchmark"))
@pytest.mark.parametrize("hq", (False, True))
def test_reports_fit_four_widths(visitor, browser, web, width, tab, hq):
    context = new_context(browser, web) if hq else None
    page = context.new_page() if context else visitor.page
    if context:
        sign_in(page, web, HQ)
    page.set_viewport_size({"width": width, "height": 844})
    body = projection()
    body["expected"]["plans"][0]["plan_label"] = "IllustrativeStoredPlan" * 20
    body["expected"]["plans"][0]["key"] = "IllustrativeStockDecision" * 12
    answer(page, benchmark=CURRENT, workspace=body)
    if tab == "benchmark":
        serve_saved(page, CURRENT)
    page.goto(f"/#/reports?tab={tab}")
    expect(panel(page).get_by_role("heading", name="Workspace activity" if tab == "workspace" else "Benchmark evaluation")).to_be_visible()
    if tab == "benchmark":
        open_every_block(page)
    page.wait_for_function("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate("() => document.documentElement.scrollWidth === document.documentElement.clientWidth")
    if hq and width < 768:
        for name in ("Show branch", "All branches"):
            assert page.get_by_role("button", name=name, exact=True).bounding_box()["height"] >= 44
    if width in (390, 1280):
        dest = pathlib.Path(__file__).resolve().parents[2] / "test-results" / "reports"
        dest.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(dest / f"{width}-{'hq' if hq else 'manager'}-{tab}.png"), full_page=True)
        if tab == "workspace":
            for name in ("Expected outcomes", "Actual outcomes"):
                panel(page).get_by_role("heading", name=name, exact=True).scroll_into_view_if_needed()
                page.screenshot(path=str(dest / f"{width}-{'hq' if hq else 'manager'}-{name.replace(' ', '-')}.png"), full_page=True)
    if tab == "benchmark":
        # The historical block, opened, with everything inside it open as well.
        serve_saved(page, HISTORICAL)
        page.reload()
        panel(page).get_by_role("button", name=re.compile("^Historical benchmark")).click()
        open_every_block(page)
        page.wait_for_function("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        assert page.evaluate("() => document.documentElement.scrollWidth === document.documentElement.clientWidth")
    if context:
        context.close()


def saved_breakdown(body, chart, by, page_no):
    """Illustrative breakdown rows of the saved run, wide enough to test the layout, never evaluation results."""
    body = copy.deepcopy(body)
    run = body["current"] or body["historical"]
    keys = ("store_rule", "store_rule_checked", "shelfshift", "markdowns_only")
    if by == "day":
        rows = [dict(day=d, pinned=False, checks_change=-1234.5, decisions_change=2345.6, total_change=1111.1,
                     change_vs_markdowns_only=-3, **{k: 123456.7 + i for i, k in enumerate(keys)}) for d in range(7)]
    else:
        rows = [dict(branch="ILLUSTRATIVE-LONG-BRANCH-CODE-" + str(page_no), pinned=page_no == 1, checks_change=-1234.5, decisions_change=2345.6, total_change=1111.1,
                     change_vs_markdowns_only=-3, **{k: 123456.7 + i for i, k in enumerate(keys)},
                     **({"waste_units": {k: 12345 for k in keys}, "moved_out": 1234, "moved_in": None,
                         "demand_not_met": {"store_rule": 12, "shelfshift": None}} if chart == "money" else {}))]
    run["breakdown"] = dict(benchmark_id=run["benchmark_id"], kind="SIMULATED", chart=chart, by=by, rows=rows,
                            page=page_no, page_size=7 if by == "day" else 1, total=7 if by == "day" else 2)
    return body


def serve_saved(page, body):
    def serve(route):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))
        route.fulfill(json=saved_breakdown(body, q["chart"], q["breakdown"], int(q.get("page", 1)))
                      if q.get("breakdown") else body)
    page.route("**/api/reports/benchmark*", serve)


def open_every_block(page):
    """Both chart breakdowns by branch, every scenario figure and all four blocks open (UI-SPEC overflow rule)."""
    for title, region in (("Net revenue after operating costs", "Money breakdown"), ("Waste within the window", "Waste breakdown")):
        panel(page).get_by_role("button", name=f"View breakdown: {title}", exact=True).click()
        section = panel(page).get_by_role("region", name=region, exact=True)
        section.get_by_role("button", name="By branch", exact=True).click()
        expect(section.get_by_role("table")).to_be_visible()
    panel(page).get_by_role("button", name="Show all scenario figures", exact=True).click()
    for name in ("Stock checks & trips", "Decision quality", "Forecast quality", "By branch"):
        button = panel(page).locator("button[aria-expanded]").filter(has_text=re.compile("^" + re.escape(name)))
        button.click()
        expect(button).to_have_attribute("aria-expanded", "true")
    expect(panel(page).get_by_label("Branch figures table").get_by_role("table")).to_be_visible()


def test_reports_preserves_public_dataset_provenance(visitor):
    page = visitor.page
    s = session_json(page)
    public = {**s, "kind": "public", "data_as_of": s["calendar"][3]["date"],
              "label": "Public sales dataset · Simulated operational scenario · Scenario date: {date}"}
    page.route("**/api/session", lambda r: r.fulfill(json=public))
    answer(page)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/#/reports?tab=benchmark")
    page.reload()  # a hash-only navigation keeps the shell session; load the illustrative public session
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
    strip = page.get_by_test_id("mark-strip")
    expect(strip.get_by_text("Public sales dataset · Simulated operational scenario", exact=True)).to_be_visible()
    expect(strip.get_by_text(re.compile("^Scenario date:"))).to_be_visible()


@pytest.mark.parametrize("variant", ("total", "overlap", "not_computed", "empty"))
def test_expected_and_actual_outcomes_are_server_owned(visitor, variant):
    page = visitor.page
    body = projection(variant == "overlap") if variant != "empty" else copy.deepcopy(WORKSPACE)
    if variant == "not_computed":
        p = body["expected"]["plans"][0]
        p.update(computed=False, net_change=None, waste_change_units=None, waste_change_kg=None, transport_and_handling=None)
        body["expected"]["combined"] = {"additive": False, "plans": 3, "reason": "not_computed"}
    answer(page, workspace=body)
    page.goto("/#/reports")
    expect(panel(page).get_by_role("heading", name="Expected outcomes", exact=True)).to_be_visible()
    expect(panel(page).get_by_text("Expected network impact of plans involving this store", exact=True)).to_be_visible()
    expect(panel(page).get_by_text("Not yet recorded.", exact=True)).to_have_count(2)
    expect(panel(page).get_by_text("12 units in 2 transfers", exact=True)).to_be_visible()
    expect(panel(page).get_by_text("10 units", exact=True)).to_be_visible()
    region = panel(page).get_by_role("region", name="Expected outcomes table")
    if variant == "empty":
        expect(panel(page).get_by_role("heading", name="No committed plans yet")).to_be_visible()
        expect(panel(page).get_by_role("link", name="View breakdown", exact=True)).to_have_count(0)
        expect(region).to_have_count(0)
        return
    expect(panel(page).get_by_role("link", name="View breakdown", exact=True)).to_have_attribute("href", "#/board?view=table&report=committed")
    expect(region.get_by_role("columnheader", name="Transport and handling cost", exact=True)).to_be_visible()
    s = session_json(page)
    for p, row in zip(body["expected"]["plans"], region.locator("tbody tr").all()):
        expect(row.get_by_role("link", name=p["key"], exact=True)).to_have_attribute("href", f"#/cases/{p['key']}")
        expect(row.get_by_text(p["plan_label"], exact=True)).to_be_visible()
        dates = []
        for day in (p["horizon"]["first_day"], p["horizon"]["last_day"]):
            d = next(d for d in s["calendar"] if d["day"] == day)
            dates.append(page.evaluate("d => `${d.weekday} ${Number(d.date.slice(8))} ${new Intl.DateTimeFormat('en-US', {month:'short', timeZone:'UTC'}).format(new Date(d.date+'T00:00:00Z'))}`", d))
        expect(row.get_by_text(" – ".join(dates), exact=True)).to_be_visible()
        if not p["computed"]:
            expect(row.get_by_text("Not computed", exact=True)).to_have_count(3)
        else:
            def amount(value, plus=False):
                return page.evaluate("([v,c,p]) => new Intl.NumberFormat('en-US', {style:'currency',currency:c,signDisplay:p?'exceptZero':'negative'}).format(v).replace('-', '−')", [value, s["currency"], plus])
            expect(row.locator("td").nth(3)).to_have_text(amount(p["net_change"], True))
            expect(row.locator("td").nth(4)).to_have_text("−42 units · −43.1 kg")
            expect(row.locator("td").nth(5)).to_have_text(amount(p["transport_and_handling"]))
    if variant == "total":
        footer = region.locator("tfoot")
        expect(footer.get_by_role("rowheader", name="Combined · 3 plans")).to_be_visible()
        expect(footer.locator("td").nth(0)).to_have_text(amount(12.34, True))
        expect(footer.locator("td").nth(1)).to_have_text("−126 units · −129.3 kg")
        expect(footer.locator("td").nth(2)).to_have_text(amount(12.62))
    else:
        expect(region.get_by_role("rowheader", name=re.compile("Combined"))).to_have_count(0)
        if variant == "overlap":
            expect(region.get_by_text("Not additive", exact=True)).to_be_visible()
            expect(region.get_by_text("These plans project some of the same products and branches, so their figures overlap and can't be added.", exact=True)).to_be_visible()
        else:
            expect(region.get_by_text("Some plans have no stored estimate, so no total is shown.", exact=True)).to_be_visible()


def test_hq_expected_copy_and_actual_zero_one(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        body = projection()
        body["actual"].update(dispatched_units=1, dispatched_transfers=1, received_units=0, discrepancies_open=0)
        answer(page, workspace=body)
        page.goto("/#/reports")
        expect(panel(page).get_by_text("Expected network impact of active committed plans", exact=True)).to_be_visible()
        expect(panel(page).get_by_text("1 units in 1 transfer", exact=True)).to_be_visible()
        expect(panel(page).get_by_text("None yet", exact=True)).to_be_visible()
        expect(panel(page).get_by_text("None", exact=True)).to_be_visible()
        body["actual"].update(dispatched_units=0, dispatched_transfers=0)
        page.reload()
        expect(panel(page).get_by_text("None yet", exact=True)).to_have_count(2)
    finally:
        context.close()


def test_expected_paging_asks_server_and_keeps_all_page_total(visitor):
    page = visitor.page
    answer(page)
    asked = []
    def serve(route):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))
        asked.append(q)
        body = projection()
        body["expected"].update(page=int(q.get("page", 1)), total=21)
        # The server's all-pages total deliberately differs from this page's figures.
        body["expected"]["combined"]["net_change"] = 432.10
        if q.get("page") == "2":
            body["expected"]["plans"] = [body["expected"]["plans"][2]]
        route.fulfill(json=body)
    page.route("**/api/reports/workspace*", serve)
    page.goto("/#/reports")
    region = panel(page).get_by_role("region", name="Expected outcomes table")
    expect(region.locator("tbody tr")).to_have_count(3)
    total = region.locator("tfoot td").first.inner_text()
    assert "432.10" in total
    panel(page).get_by_role("button", name="Next page", exact=True).click()
    expect(region.locator("tbody tr")).to_have_count(1)
    expect(region.locator("tfoot td").first).to_have_text(total)
    assert asked == [{}, {"page": "2"}]
    expect(panel(page).get_by_test_id("pager")).to_contain_text("Page 2 of 2")


def test_pending_expected_page_keeps_tiles_and_table(visitor):
    page = visitor.page
    answer(page)
    held = []

    def serve(route):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query))
        if q.get("page") == "2":
            held.append(route)
            return
        body = projection()
        body["expected"].update(page=1, total=21)
        route.fulfill(json=body)

    page.route("**/api/reports/workspace*", serve)
    page.goto("/#/reports")
    region = panel(page).get_by_role("region", name="Expected outcomes table")
    expect(region.locator("tbody tr")).to_have_count(3)
    panel(page).get_by_role("button", name="Next page", exact=True).click()
    wait_held(page, held, 1)
    # Later reads keep the old content (UI-SPEC): only the paged table says the next page is loading.
    expect(panel(page).get_by_text("Loading the breakdown…", exact=True)).to_be_visible()
    expect(panel(page).get_by_text("Loading reports…", exact=True)).to_have_count(0)
    expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 5,"))).to_be_visible()
    expect(panel(page).get_by_role("heading", name="Actual outcomes", exact=True)).to_be_visible()
    expect(region.locator("tbody tr")).to_have_count(3)
    expect(panel(page).get_by_test_id("pager")).to_contain_text("Page 1 of 2")
    second = projection()
    second["expected"].update(page=2, total=21)
    second["expected"]["plans"] = [second["expected"]["plans"][2]]
    held[0].fulfill(json=second)
    expect(region.locator("tbody tr")).to_have_count(1)
    expect(panel(page).get_by_test_id("pager")).to_contain_text("Page 2 of 2")
    expect(panel(page).get_by_text("Loading the breakdown…", exact=True)).to_have_count(0)


def visible(page):
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def test_workspace_freshness_stale_retry_and_benchmark_independence(visitor):
    page = visitor.page
    asked = answer(page, workspace=projection())
    page.goto("/#/reports")
    expect(panel(page).get_by_role("heading", name="Expected outcomes", exact=True)).to_be_visible()
    page.get_by_role("tab", name="Benchmark", exact=True).click()
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
    page.get_by_role("tab", name="This workspace", exact=True).click()
    wait_held(page, asked["workspace"], 2)
    held = []
    page.route("**/api/reports/workspace*", lambda r: held.append(r))
    visible(page)
    wait_held(page, held, 1)
    held[0].fulfill(status=500, json={"error": "db_busy", "message": "Workspace read failed."})
    stale = "Couldn't refresh. Showing what was loaded before."
    expect(panel(page).get_by_text(stale, exact=True)).to_be_visible()
    expect(panel(page).get_by_role("region", name="Expected outcomes table").locator("tbody tr")).to_have_count(3)
    expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 5,"))).to_be_visible()
    panel(page).get_by_role("button", name="Try again", exact=True).click()
    wait_held(page, held, 2)
    expect(panel(page).get_by_text(stale, exact=True)).to_be_visible()
    held[1].fulfill(json=projection())
    expect(panel(page).get_by_text(stale, exact=True)).to_have_count(0)
    page.get_by_role("tab", name="Benchmark", exact=True).click()
    expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
    visible(page)
    page.wait_for_timeout(100)
    assert len(asked["benchmark"]) == 1
    # Re-entry is a new read, while a workspace focus never reloads the saved benchmark.
    page.evaluate("location.hash='#/about'")
    expect(page.get_by_role("heading", level=1, name=re.compile("About"))).to_be_visible()
    page.evaluate("location.hash='#/reports?tab=benchmark'")
    wait_held(page, asked["benchmark"], 2)


@pytest.mark.parametrize("transition", ("visibility", "tab", "page", "branch"))
@pytest.mark.parametrize("old_failure", (False, True))
def test_latest_generation_owns_figures_errors_and_pager(visitor, browser, web, transition, old_failure):
    context = new_context(browser, web) if transition == "branch" else None
    page = context.new_page() if context else visitor.page
    if context:
        sign_in(page, web, HQ)
    try:
        initial = projection()
        initial["expected"]["total"] = 40
        answer(page, workspace=initial)
        page.goto("/#/reports")
        expect(panel(page).get_by_role("heading", name="Expected outcomes", exact=True)).to_be_visible()
        held = []
        page.route("**/api/reports/workspace*", lambda r: held.append(r))
        visible(page)
        wait_held(page, held, 1)
        if transition == "visibility":
            visible(page)
        elif transition == "tab":
            page.get_by_role("tab", name="Benchmark", exact=True).click()
            expect(panel(page).get_by_role("heading", name="Evaluation not yet available")).to_be_visible()
            page.get_by_role("tab", name="This workspace", exact=True).click()
        elif transition == "page":
            panel(page).get_by_role("button", name="Next page", exact=True).click()
        else:
            page.evaluate("location.hash='#/reports?branch=NEW'")
        wait_held(page, held, 2)
        fresh = projection()
        fresh["activity"]["open"]["count"] = 19
        fresh["expected"].update(page=2 if transition == "page" else 1, total=40)
        fresh["expected"]["combined"]["net_change"] = 456.78
        fresh["scope"]["branch"] = "NEW" if transition == "branch" else session_json(page)["branch"]
        held[-1].fulfill(json=fresh)
        page_text = f"Page {2 if transition == 'page' else 1} of 2"
        expect(panel(page).get_by_test_id("pager")).to_contain_text(page_text)
        expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 19,"))).to_be_visible()
        if old_failure:
            held[0].fulfill(status=500, json={"error": "db_busy", "message": "Old request failed."})
        else:
            old = projection()
            old["activity"]["open"]["count"] = 999
            held[0].fulfill(json=old)
        page.wait_for_timeout(100)
        expect(panel(page).get_by_test_id("pager")).to_contain_text(page_text)
        expect(panel(page).get_by_role("group", name=re.compile("^Open decisions: 19,"))).to_be_visible()
        expect(panel(page).get_by_text("Couldn't refresh. Showing what was loaded before.", exact=True)).to_have_count(0)
        expect(panel(page).get_by_text("Old request failed.", exact=True)).to_have_count(0)
        assert "456.78" in panel(page).locator("tfoot td").first.inner_text()
    finally:
        if context:
            context.close()


@pytest.mark.parametrize("identity", ("account", "workspace_reset", "role"))
def test_identity_change_clears_cached_totals_and_pending_errors(visitor, identity):
    page = visitor.page
    s = session_json(page)
    answer(page, workspace=projection())
    page.goto("/#/reports")
    expect(panel(page).locator("tfoot")).to_contain_text("Combined")
    held = []
    page.route("**/api/reports/workspace*", lambda r: held.append(r))
    visible(page)
    wait_held(page, held, 1)
    changed = {**s, "account": "illustrative-other-account"} if identity == "account" else (
        {**s, "run_id": "illustrative-reset-workspace"} if identity == "workspace_reset" else {**s, "role": "hq", "branch": None})
    page.route("**/api/session", lambda r: r.fulfill(json=changed))
    page.route("**/api/reports/benchmark*", lambda r: r.fulfill(status=409, json={"error": "sandbox_reset", "message": "Workspace changed."}))
    page.get_by_role("tab", name="Benchmark", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Today", exact=True)).to_be_visible()
    page.evaluate("location.hash='#/reports'")
    expect(panel(page).get_by_text("Loading reports…", exact=True)).to_be_visible()
    expect(panel(page).locator("tfoot")).to_have_count(0)
    wait_held(page, held, 2)
    held[0].fulfill(status=500, json={"error": "db_busy", "message": "Old identity failed."})
    expect(panel(page).get_by_text("Old identity failed.", exact=True)).to_have_count(0)
    held[1].fulfill(json=WORKSPACE)
    expect(panel(page).get_by_role("heading", name="No committed plans yet")).to_be_visible()
    expect(panel(page).locator("tfoot")).to_have_count(0)
    expect(panel(page).get_by_text("Couldn't refresh. Showing what was loaded before.", exact=True)).to_have_count(0)


def report_board(page_no=1, empty=False):
    body = fixture("board_public")
    body["cases"] = [] if empty else [body["cases"][0]]
    if body["cases"]:
        body["cases"][0].update(key=f"illustrative-match-{page_no}", case_id=f"illustrative-match-{page_no}")
    body.update(page=page_no, page_size=1, total=0 if empty else 2, evaluated=0 if empty else 2)
    body["stage_counts"]["decide"] = body["total"]
    return body


@pytest.mark.parametrize("figure", tuple(LABELS))
def test_report_tile_opens_filtered_board_with_paging_and_clear(visitor, figure):
    page = visitor.page
    body = projection()
    body["activity"]["cancelled"]["count"] = 1
    answer(page, workspace=body)
    held = []
    page.route(re.compile(r"/api/cases(?:\?.*)?$"), lambda r: held.append(r))
    page.goto("/#/reports")
    panel(page).get_by_role("link", name=f"View breakdown: {LABELS[figure]}", exact=True).click()
    line = f"Showing: {LABELS[figure]} · from Reports"
    expect(page.get_by_text(line, exact=True)).to_be_visible()
    expect(page.get_by_text("Loading the Decision board…", exact=True)).to_be_visible()
    expect(page.locator("[data-testid^='decision-row-']")).to_have_count(0)
    wait_held(page, held, 1)
    query = lambda r: dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r.request.url).query))
    assert query(held[0])["report"] == figure
    held[0].fulfill(json=report_board())
    expect(page.get_by_test_id("decision-row-illustrative-match-1")).to_be_visible()
    page.get_by_role("button", name="Next page", exact=True).click()
    wait_held(page, held, 2)
    assert query(held[1])["report"] == figure and query(held[1])["page"] == "2"
    held[1].fulfill(json=report_board(2))
    expect(page.get_by_test_id("decision-row-illustrative-match-2")).to_be_visible()
    page.get_by_role("button", name="Clear filters", exact=True).click()
    wait_held(page, held, 3)
    assert "report" not in query(held[-1])
    expect(page).not_to_have_url(re.compile("report="))
    expect(page.get_by_text(line, exact=True)).to_have_count(0)
    held[-1].fulfill(json=report_board())


@pytest.mark.parametrize("value", ("unknown", "__proto__", "committed,open"))
def test_invalid_report_hash_is_dropped_before_board_request(visitor, value):
    page = visitor.page
    asked = []
    def serve(route):
        asked.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(route.request.url).query)))
        route.fulfill(json=report_board())
    page.route(re.compile(r"/api/cases(?:\?.*)?$"), serve)
    page.goto("/#/board?view=table&report=" + urllib.parse.quote(value, safe=""))
    expect(page.get_by_test_id("decision-row-illustrative-match-1")).to_be_visible()
    assert asked and all("report" not in q for q in asked)
    expect(page.get_by_text(re.compile("· from Reports$"))).to_have_count(0)


@pytest.mark.parametrize("outcome", ("empty", "error", "loaded"))
def test_report_filter_never_labels_unfiltered_rows(visitor, outcome):
    page = visitor.page
    held = []
    def serve(route):
        if "report=" in route.request.url:
            held.append(route)
        else:
            route.fulfill(json=report_board())
    page.route(re.compile(r"/api/cases(?:\?.*)?$"), serve)
    page.goto("/#/board?view=table")
    expect(page.get_by_test_id("decision-row-illustrative-match-1")).to_be_visible()
    page.evaluate("location.hash='#/board?report=committed'")
    expect(page.get_by_text("Showing: Committed plans · from Reports", exact=True)).to_be_visible()
    expect(page.get_by_text("Loading the Decision board…", exact=True)).to_be_visible()
    expect(page.locator("[data-testid^='decision-row-']")).to_have_count(0)
    wait_held(page, held, 1)
    if outcome == "error":
        message = "report is one of open, pending_approval, unanswered_check, committed, cancelled"
        held[0].fulfill(status=400, json={"error": "bad_request", "message": message})
        expect(page.get_by_text(message, exact=True)).to_be_visible()
        expect(page.locator("[data-testid^='decision-row-']")).to_have_count(0)
        expect(page.get_by_role("button", name="Clear filters", exact=True)).to_be_visible()
    elif outcome == "empty":
        held[0].fulfill(json=report_board(empty=True))
        expect(page.get_by_text("No stock decisions match these filters.", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Clear filters", exact=True).last).to_be_visible()
    else:
        held[0].fulfill(json=report_board(2))
        expect(page.get_by_test_id("decision-row-illustrative-match-2")).to_be_visible()
        expect(page.get_by_test_id("decision-row-illustrative-match-1")).to_have_count(0)
    page.set_viewport_size({"width": 360, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
