"""PROD-12 (06.1 D-11, D-18, D-22, D-33; UI-SPEC "Today", sketch 002 B): Today is the page a signed-in person lands
on. A store manager sees the four summary tiles, the planned day strip, the action cards in the server's order, the
movements, the expected-versus-actual outcomes, the coming markdown steps and the exceptions; HQ sees the network
tiles, 05.2 D-38's evaluated line and the branches needing attention. Every time on the page is a planned time from
GET /api/today: there is no current-time line, countdown or overdue state.

Expected text is built from the API's own answer (GET /api/today through the page's signed-in context), never typed
here; states the template cannot give are answered from a contract-shaped fixture through `page.route`.
"""
from __future__ import annotations

import copy
import datetime
import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_id_by_tag, case_json, switch_seat

pytestmark = pytest.mark.web

STORY_TAG = "milk-story"


def get(page: Page, path: str) -> dict:
    r = page.request.get(path)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def hhmm(minute: int) -> str:
    """format.hhmm: minutes after midnight as 24-hour "HH:MM"."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def tile(page: Page, label: str):
    return page.get_by_role("group", name="Needs attention").get_by_role("button", name=re.compile(f"^{label}"))


def card(page: Page, key: str):
    return page.get_by_test_id(f"today-card-{key}")


# --- Task 1: the manager lands on Today ------------------------------------------------------------------------------

def test_the_story_source_lands_on_today_with_its_due_decision(visitor, web):
    page = visitor.page
    story = case_json(page, case_id_by_tag(page, STORY_TAG))
    assert story["source"] == web.store, "the default store manager is the story's source (tests.api first_store)"
    today = get(page, "/api/today")
    assert today["summary"]["decisions_due"] >= 1
    first = today["summary"]["first_deadline"]

    page.goto("/#/")
    expect(page).to_have_url(re.compile(r"#/$"))
    expect(page.get_by_role("heading", level=1, name="Today")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile("^Today"))).to_have_attribute(
        "aria-current", "page")
    due = tile(page, "Decisions due")
    expect(due).to_contain_text(str(today["summary"]["decisions_due"]))
    expect(due).to_contain_text(f"First: decide by {hhmm(first['min'])}")
    expect(due).to_have_attribute("aria-pressed", "false")

    cards = page.get_by_test_id(re.compile(r"^today-card-"))
    expect(cards.first).to_contain_text(story["key"])
    expect(cards.first.get_by_role("link", name="Take ownership")).to_have_count(1)
    # Planned times only (D-18): nothing marks the current time.
    expect(page.get_by_text(re.compile(r"^Now$"))).to_have_count(0)
    expect(page.locator("[data-now]")).to_have_count(0)


# --- Task 2: the planned day strip -----------------------------------------------------------------------------------

PLANNED_NOTE = "Planned times. Actual times appear when someone records the work."

NO_SIDE_SCROLL = ("() => document.documentElement.scrollWidth <= window.innerWidth "
                  "&& document.querySelector('main#content').scrollWidth "
                  "<= document.querySelector('main#content').clientWidth")


def lane_names(today: dict) -> list[str]:
    """The strip's lane labels from the API's lanes, in its order."""
    names = []
    for lane in today["lanes"]:
        if lane["kind"] == "decisions":
            names.append("Decisions")
        elif lane["kind"] == "stock_checks":
            names.append("Stock checks")
        elif lane["kind"] == "floor":
            names.append(f"{lane['branch']} floor")
        else:
            names.append(f"{lane['run_code']} → {lane['dest']}")
    return names


def test_the_day_strip_draws_planned_lanes_and_markers_only(visitor):
    page = visitor.page
    story = case_json(page, case_id_by_tag(page, STORY_TAG))
    today = get(page, "/api/today")
    runs = [lane for lane in today["lanes"] if lane["kind"] == "run"]
    assert runs, "the story source has outgoing scheduled truck runs on the scenario date"
    deadline = next(m for m in today["lanes"][0]["items"] if m["case_key"] == story["key"])

    page.goto("/#/")
    strip = page.get_by_test_id("day-strip")
    expect(strip.get_by_test_id("lane-label")).to_have_text(lane_names(today))
    expect(strip).to_contain_text("06:00–18:00 · planned times")
    marker = strip.get_by_role("button", name=f"{story['key']} decide by {hhmm(deadline['min'])}, planned")
    expect(marker).to_have_count(1)
    for lane in runs:
        cutoff = lane["items"][0]
        expect(strip.get_by_role("button", name=f"{lane['run_code']} loading cutoff {hhmm(cutoff['min'])}, planned")) \
            .to_have_count(1)
    expect(strip.get_by_test_id("planned-note")).to_have_count(1)
    expect(strip.get_by_test_id("planned-note")).to_have_text(PLANNED_NOTE)
    for word in ("Decision deadline", "Loading cutoff", "Scheduled work", "Conditional work (if sent)", "Delivery"):
        expect(strip.get_by_test_id("strip-legend")).to_contain_text(word)
    expect(page.locator("[data-now]")).to_have_count(0)

    marker.click()
    drawer = page.get_by_role("complementary", name=re.compile(r"^Decide: "))
    expect(drawer).to_contain_text(story["key"])
    expect(drawer.get_by_role("link", name="Open stock decision")).to_have_attribute("href", f"#/cases/{story['key']}")
    expect(drawer.get_by_test_id("planned-note")).to_have_count(1)


def test_the_strip_scrolls_in_its_own_box_on_a_phone(visitor):
    page = visitor.page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("/#/")
    box = page.get_by_test_id("strip-scroll")
    expect(box).to_be_visible()
    assert box.evaluate("el => el.scrollWidth > el.clientWidth")
    page.wait_for_function(NO_SIDE_SCROLL, timeout=5_000)
    for width in (360, 768, 1280):  # no page scrolls sideways (D-37, D-59)
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_function(NO_SIDE_SCROLL, timeout=5_000)


# --- Task 3: movements, outcomes, exceptions, HQ's network view, the empty and stale states ---------------------------

def short_date(iso: str) -> str:
    """todayview.shortDate: "Thu 15 Oct" from an ISO date."""
    d = datetime.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def test_movements_keep_decision_and_movement_apart_and_outcomes_keep_expected_apart(visitor):
    page = visitor.page
    today = get(page, "/api/today")
    page.goto("/#/")

    movements = page.get_by_test_id("movements")
    out = today["movements"]["outgoing"]
    expect(movements).to_contain_text(f"Outgoing · {len(out)} scheduled truck run")
    carrying = next(r for r in out if r["transfers"])
    row = movements.get_by_test_id(f"run-row-{carrying['run_code']}")
    expect(row).to_contain_text(f"{carrying['source']} → {carrying['dest']}")
    expect(row).to_contain_text(f"Planned: cutoff {hhmm(carrying['loading_cutoff']['min'])} · departs "
                                f"{hhmm(carrying['departure']['min'])} · arrives {hhmm(carrying['arrival']['min'])}")
    t = carrying["transfers"][0]
    line = row.get_by_test_id(f"transfer-{t['case_key']}")
    expect(line.get_by_test_id("today-decision-chip")).to_contain_text("Decision: ")
    expect(line.get_by_test_id("today-movement-chip")).to_have_text(
        "Movement: Not yet recorded" if t["movement"]["state"] is None else re.compile(r"^Movement: "))
    empty_run = next((r for r in out if not r["transfers"]), None)
    if empty_run is not None:
        expect(movements.get_by_test_id(f"run-row-{empty_run['run_code']}")).to_contain_text("No transfers assigned")
    expect(movements.get_by_test_id("planned-note")).to_have_count(1)

    assert today["outcomes"], "a stock decision of the template is in Verify (SUS-21)"
    for o in today["outcomes"]:
        box = page.get_by_test_id(f"outcome-{o['case_key']}")
        expected = box.get_by_test_id("expected")
        actual = box.get_by_test_id("actual")
        expect(expected).to_contain_text("Expected")
        expect(expected).to_contain_text("Engine estimate vs keeping it · simulated")
        expect(actual).to_contain_text("Actual")
        expect(actual).to_contain_text("Not yet recorded")
        expect(actual).not_to_contain_text("$")  # the expected figure is never shown as actual (PROD-12)

    for step in today["coming_up"]:
        expect(page.get_by_test_id("coming-up")).to_contain_text(f"{step['lot_id']} goes to {step['pct']}% off at opening")

    exceptions = page.get_by_test_id("exceptions")
    if today["exceptions"]:
        first = exceptions.get_by_test_id("exception-row").first
        expect(first).to_contain_text(today["exceptions"][0]["case_key"])
        expect(first).to_contain_text("Owner: ")
        expect(first).to_contain_text("Next: ")
    else:
        expect(exceptions).to_contain_text("No exceptions need you.")


def test_hq_sees_the_network_tiles_the_evaluated_line_and_the_branches(visitor):
    page = visitor.page
    switch_seat(page, None)
    today = get(page, "/api/today")
    net = today["network"]
    page.goto("/#/")
    expect(page.get_by_role("heading", level=1, name="Today")).to_be_visible()
    for label, key in (("Decisions due", "decisions_due"), ("Approvals waiting", "approvals_waiting"),
                       ("Stock checks", "stock_checks"), ("Transfers at risk", "transfers_at_risk")):
        expect(tile(page, label).locator(".text-display")).to_have_text(str(net[key]))
    expect(page.get_by_test_id("evaluated-line")).to_have_text(
        f"Evaluated {net['evaluated']} of {net['at_risk']} at-risk items")
    expect(page.get_by_test_id("day-strip")).to_have_count(0)
    expect(page.get_by_test_id("movements")).to_have_count(0)

    table = page.get_by_role("region", name="Branches needing attention")
    rows = table.get_by_test_id(re.compile(r"^branch-row-"))
    expect(rows).to_have_count(len(today["branches"]["items"]))
    first = today["branches"]["items"][0]
    expect(rows.first).to_contain_text(first["branch"])
    if not today["cards"]:
        expect(page.get_by_text("Nothing waits on HQ. Store managers own their stock decisions; HQ can take ownership "
                                "from the Decision board.", exact=True)).to_be_visible()
    rows.first.click()
    page.wait_for_url(f"**/#/board?branch={urllib.parse.quote(first['branch'], safe='')}")


def quiet(today: dict) -> dict:
    """The store's real day with nothing due: no cards, zero counts, no decision or stock-check markers."""
    q = copy.deepcopy(today)
    q["summary"] = {"decisions_due": 0, "approvals_waiting": 0, "stock_checks": 0, "transfers_at_risk": 0,
                    "first_deadline": None}
    q["cards"] = []
    for lane in q["lanes"]:
        if lane["kind"] in ("decisions", "stock_checks"):
            lane["items"] = []
    return q


def test_a_quiet_store_says_nothing_needs_a_decision_and_a_failed_refetch_keeps_the_page(visitor):
    page = visitor.page
    q = quiet(get(page, "/api/today"))
    page.route("**/api/today*", lambda route: route.fulfill(json=q))
    page.goto("/#/")
    page.reload()  # sign-in already mounted Today at "#/": load it again, now answered by the fixture
    expect(page.get_by_test_id("today-empty")).to_contain_text(f"Nothing needs a decision on {short_date(q['scenario_date'])}")
    expect(page.get_by_test_id("today-empty")).to_contain_text("The strip shows the planned runs and deliveries only.")
    for label, note in (("Decisions due", f"None on {short_date(q['scenario_date'])}"),
                        ("Approvals waiting", "None waiting"), ("Stock checks", "None open"),
                        ("Transfers at risk", "None")):
        expect(tile(page, label).locator(".text-display")).to_have_text("0")
        expect(tile(page, label)).to_contain_text(note)
    tile(page, "Decisions due").click()
    expect(page.get_by_test_id("today-filter")).to_have_text("Showing: Decisions due")
    expect(page.get_by_test_id("today-filter-empty")).to_contain_text(f"No decisions due for {q['branch']}.")
    page.get_by_role("button", name="Clear filter").click()
    expect(tile(page, "Decisions due")).to_have_attribute("aria-pressed", "false")

    page.unroute("**/api/today*")
    page.route("**/api/today*", lambda route: route.fulfill(
        status=503, json={"error": "db_busy", "message": "The database is busy. Try again in a moment."}))
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.get_by_text("Couldn't refresh. Showing what was loaded before.", exact=True)).to_be_visible()
    expect(page.get_by_test_id("day-strip")).to_be_visible()
