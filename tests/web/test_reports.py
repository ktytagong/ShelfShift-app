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


@pytest.fixture(autouse=True)
def reports_workspace_budget(web):
    """Return only these tests' workspace usage after their browser contexts close.

    The full browser run owns its dedicated database under the shared test slot.
    Each test still spends against the real cap; its successful workspace creations
    must not consume the budget of later, unrelated browser tests.
    """
    from api import db

    with db.tx() as conn:
        before = {row["window_start"]: row["count"] for row in db.rows(
            conn, "SELECT window_start, count FROM usage_counts WHERE scope = 'global' AND kind = 'sandbox'")}
    yield
    with db.tx() as conn:
        for row in db.rows(conn, "SELECT window_start, count FROM usage_counts "
                                "WHERE scope = 'global' AND kind = 'sandbox' FOR UPDATE"):
            previous = before.get(row["window_start"])
            created = row["count"] - (previous or 0)
            assert created >= 0, "another test changed the Reports workspace counter"
            if not created:
                continue
            if previous is None:
                db.run(conn, "DELETE FROM usage_counts WHERE scope = 'global' AND kind = 'sandbox' "
                             "AND window_start = :w AND count = :n", w=row["window_start"], n=created)
            else:
                db.run(conn, "UPDATE usage_counts SET count = count - :n "
                             "WHERE scope = 'global' AND kind = 'sandbox' AND window_start = :w",
                       n=created, w=row["window_start"])


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
    else:
        expect(panel(page).get_by_role("heading", name="Benchmark evaluation")).to_be_visible()
        s = session_json(page)
        for value in VERSIONS.values():
            text = ", ".join(value) if isinstance(value, list) else value[:12] if value == VERSIONS["settings"] else value
            expect(panel(page).get_by_text(text, exact=True)).to_be_visible()
        end = next(d["date"] for d in s["calendar"] if d["day"] == RUN["window"]["last_day"])
        expect(panel(page).get_by_text(re.compile("Simulated evaluation ·.*" + end[:4]))).to_be_visible()


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


def test_unknown_hq_branch_shows_api_message_and_retains_previous_scope(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sign_in(page, web, HQ)
        answer(page)
        page.goto("/#/reports")
        expect(panel(page).get_by_role("heading", name="Workspace activity")).to_be_visible()
        message = "No branch ILLUSTRATIVE-UNKNOWN"
        page.route("**/api/reports/workspace*branch=ILLUSTRATIVE-UNKNOWN*", lambda r: r.fulfill(
            status=404, json={"error": "not_found", "message": message}))
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
    answer(page, benchmark=CURRENT)
    page.goto(f"/#/reports?tab={tab}")
    expect(panel(page).get_by_role("heading", name="Workspace activity" if tab == "workspace" else "Benchmark evaluation")).to_be_visible()
    page.wait_for_function("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert page.evaluate("() => document.documentElement.scrollWidth === document.documentElement.clientWidth")
    if hq and width < 768:
        for name in ("Show branch", "All branches"):
            assert page.get_by_role("button", name=name, exact=True).bounding_box()["height"] >= 44
    if width in (390, 1280):
        dest = pathlib.Path(__file__).resolve().parents[2] / "test-results" / "reports"
        dest.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(dest / f"{width}-{'hq' if hq else 'manager'}-{tab}.png"), full_page=True)
    if context:
        context.close()


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
