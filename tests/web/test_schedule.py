"""PROD-13 (06.1 D-17, D-18, D-29; UI-SPEC "Planned schedule"; sketch 003 A): the Planned schedule is a read-only
work-order table beside a horizontal time grid. It draws only the planned times GET /api/schedule sends; a task with no
planned time reads "Unscheduled" and has no bar; the server filters, groups and pages, and every choice on the page is
a request to it. Nothing on the page can be dragged, edited or posted.

The story stock decision (the milk-story tag) is prepared through the API in setup, as the people involved make it:
the source takes ownership and sends, the destination accepts, the source approves and prepares (each POST with its own
Idempotency-Key, through the page's own signed-in context). Expected text comes from the API's own answers, never typed
here. States the real data cannot give (an empty window) are answered from a fixture through `page.route` (06.1-01
execution rules).
"""
from __future__ import annotations

import re
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from tests.web.conftest import cards_json, case_id_by_tag, case_json, switch_seat, take_ownership

pytestmark = pytest.mark.web

STORY_TAG = "milk-story"
PLANNED_NOTE = "Planned times. Actual times appear when someone records the work."


# --- setup through the API -------------------------------------------------------------------------------------------

def post(page: Page, path: str, body: dict) -> dict:
    """One keyed write through the page's own context; asserts 200 and returns the answer."""
    r = page.request.post(path, data=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def get(page: Page, path: str) -> dict:
    r = page.request.get(path)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def case_path(case_id: str, route: str) -> str:
    return f"/api/cases/{urllib.parse.quote(case_id, safe='')}/{route}"


def prepare_story(page: Page) -> dict:
    """The story stock decision taken, sent, accepted, approved and prepared; ends signed in as its source. Returns
    {case_id, key, sku, source, dest}."""
    case_id = case_id_by_tag(page, STORY_TAG)
    sku = next(c["sku"] for c in cards_json(page) if c["case_id"] == case_id)
    d = case_json(page, case_id)
    source, dest, version = d["source"], d["dest"], d["rec_version"]
    assert dest, "the story's recommendation is a transfer (03-CONTEXT D-23)"
    take_ownership(page, case_id)
    post(page, case_path(case_id, "send"), {"rec_version": version})
    switch_seat(page, dest)
    post(page, case_path(case_id, "decision"), {"rec_version": version, "decision": "accept"})
    switch_seat(page, source)
    post(page, case_path(case_id, "decision"), {"rec_version": version, "decision": "approve"})
    post(page, case_path(case_id, "prepare"), {})
    return {"case_id": case_id, "key": d["key"], "sku": sku, "source": source, "dest": dest}


def row(page: Page, row_id: str):
    return page.locator(f'[data-testid="schedule-row"][data-row-id="{row_id}"]')


def schedule_request(**params: str):
    """A predicate for the next GET /api/schedule whose query holds `params` (a value of None: the key is absent)."""
    def match(request) -> bool:
        url = urllib.parse.urlparse(request.url)
        if url.path != "/api/schedule":
            return False
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        return all(q.get(k) == v for k, v in params.items())
    return match


# --- Task 1: the story work order and its activities on the Day grid --------------------------------------------------

def test_the_story_work_order_shows_its_activities_on_the_day_grid(visitor):
    page = visitor.page
    story = prepare_story(page)
    wo = f"wo:{story['case_id']}"

    page.goto("/#/schedule")
    expect(page.get_by_role("heading", level=1, name="Planned schedule")).to_be_visible()
    expect(page.get_by_role("navigation", name="Main").get_by_role("link", name="Schedule")).to_have_attribute(
        "aria-current", "page")
    expect(page.get_by_test_id("planned-note")).to_have_text(PLANNED_NOTE)

    parent = row(page, wo)
    expect(parent).to_be_visible()
    expect(parent).to_contain_text(story["sku"])
    expect(parent).to_contain_text(f"{story['source']} → {story['dest']}")
    expect(parent).to_have_attribute("aria-level", "1")
    for kind, name in (("pick_and_pack", "Pick and pack"), ("loading_cutoff", "Loading cutoff"),
                       ("transport", "Transport"), ("receive", "Receive")):
        child = row(page, f"{wo}:{kind}")
        expect(child).to_be_visible()
        expect(child).to_have_attribute("aria-level", "2")
        # The kind is named first: the grid's colour and icon mark only the kind (D-29), so the name says it too.
        assert (child.get_by_role("button").first.get_attribute("aria-label") or "").startswith(name), kind

    # A planned time is drawn; the receive has none in the data, so it reads Unscheduled and has no bar.
    sched = get(page, "/api/schedule")
    by_id = {r["id"]: r for r in sched["rows"]}
    pack = by_id[f"{wo}:pick_and_pack"]
    assert pack["start"] is not None and by_id[f"{wo}:receive"]["start"] is None
    expect(row(page, f"{wo}:receive").get_by_test_id("schedule-when")).to_have_text("Unscheduled")
    expect(row(page, f"{wo}:receive").get_by_test_id("schedule-bar")).to_have_count(0)
    if pack["start"]["day"] == sched["window"]["from_day"]:
        expect(row(page, f"{wo}:pick_and_pack").get_by_test_id("schedule-bar")).to_have_count(1)
        hhmm = f"{pack['start']['min'] // 60:02d}:{pack['start']['min'] % 60:02d}"
        expect(row(page, f"{wo}:pick_and_pack").get_by_test_id("schedule-when")).to_contain_text(hhmm)


# --- Task 2: tabs, the date stepper, grouping, filters and windows ----------------------------------------------------

def test_grouped_by_run_a_run_with_no_transfer_says_so(visitor):
    page = visitor.page
    page.goto("/#/schedule")
    expect(page.get_by_role("heading", level=1, name="Planned schedule")).to_be_visible()
    group = page.get_by_role("group", name="Group by")
    expect(group.get_by_role("button")).to_have_text(["Work order", "Branch", "Scheduled truck run"])
    expect(group.get_by_role("button", name="Work order")).to_have_attribute("aria-pressed", "true")
    with page.expect_request(schedule_request(group="run")):
        group.get_by_role("button", name="Scheduled truck run").click()
    sched = get(page, "/api/schedule?group=run")
    runs = [r for r in sched["rows"] if r["kind"] == "run"]
    assert runs, "the store has scheduled truck runs leaving on the scenario date (reference timetable)"
    empty = [r for r in runs if not any(c["parent_id"] == r["id"] for c in sched["rows"])]
    assert empty, "at least one run carries no transfer before anything is prepared"
    first = row(page, empty[0]["id"])
    expect(first).to_contain_text(empty[0]["title"])
    expect(first).to_contain_text(empty[0]["sub"])  # the run's origin and destination, as the server says
    expect(page.locator(f'[data-testid="schedule-none"][data-parent-id="{empty[0]["id"]}"]')).to_have_text(
        "No transfers assigned")


def test_the_stepper_windows_and_tabs_go_to_the_server(visitor):
    page = visitor.page
    page.goto("/#/schedule")
    expect(page.get_by_role("heading", level=1, name="Planned schedule")).to_be_visible()
    first = get(page, "/api/schedule")
    scenario = first["scenario_date"]
    stepper = page.get_by_role("group", name="Date")
    expect(stepper).to_contain_text(first["window"]["from"][:4])
    with page.expect_request(schedule_request(view="day")) as req:
        stepper.get_by_role("button", name="Next day").click()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.value.url).query)
    assert q["date"][0] > scenario, q
    with page.expect_request(schedule_request(date=scenario)):
        page.get_by_role("button", name="Scenario date").click()
    with page.expect_request(schedule_request(view="week", date=scenario)):
        page.get_by_role("group", name="Window").get_by_role("button", name="Week").click()
    head = page.locator('[role="columnheader"][aria-current="date"]')
    expect(head).to_have_count(1)
    expect(head).to_have_class(re.compile(r"bg-navy-50"))
    with page.expect_request(schedule_request(view="week")) as req:
        stepper.get_by_role("button", name="Previous week").click()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.value.url).query)
    assert q["date"][0] < scenario, q
    with page.expect_request(schedule_request(view="month")):
        page.get_by_role("group", name="Window").get_by_role("button", name="Month").click()

    # The Activities tab lists the same rows, flat: no parent row, the Group by control gone.
    page.get_by_role("button", name="Scenario date").click()
    page.get_by_role("group", name="Window").get_by_role("button", name="Day").click()
    tabs = page.get_by_role("navigation", name="Schedule views")
    tabs.get_by_role("button", name="Activities").click()
    expect(tabs.get_by_role("button", name="Activities")).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("group", name="Group by")).to_have_count(0)
    expect(page.locator('[data-testid="schedule-row"][data-kind="work_order"]')).to_have_count(0)
    leaves = [r for r in get(page, "/api/schedule")["rows"] if r["kind"] not in ("work_order", "run", "branch")]
    expect(page.get_by_test_id("schedule-row")).to_have_count(len(leaves))


def test_the_filters_are_labelled_and_go_to_the_server(visitor, web):
    page = visitor.page
    page.goto("/#/schedule")
    branch = page.get_by_label("Branch", exact=True)
    expect(branch).to_be_disabled()  # a manager reads their own store (D-17)
    expect(branch).to_have_value(web.store)
    activity = page.get_by_label("Activity", exact=True)
    with page.expect_request(schedule_request(activity="loading_cutoff")):
        activity.select_option("loading_cutoff")
    status = page.get_by_label("Status", exact=True)
    with page.expect_request(schedule_request(activity="loading_cutoff", status="scheduled")):
        status.select_option("scheduled")
    switch_seat(page, None)
    page.goto("/#/schedule")
    hq_branch = page.get_by_label("Branch", exact=True)
    hq_branch.fill(web.store)
    with page.expect_request(schedule_request(branch=web.store)):
        hq_branch.press("Enter")


def test_day_view_draws_dependency_connectors_and_the_legend(visitor):
    page = visitor.page
    story = prepare_story(page)
    page.goto("/#/schedule")
    expect(row(page, f"wo:{story['case_id']}:transport")).to_be_visible()
    sched = get(page, "/api/schedule")
    drawn = [r for r in sched["rows"] if r["depends_on"] and r["start"] is not None
             and r["start"]["day"] == sched["window"]["from_day"]]
    if drawn:
        expect(page.get_by_test_id("schedule-connector").first).to_be_attached()
    legend = page.get_by_test_id("schedule-legend")
    for word in ("Planned schedule", "Decision deadline", "Loading cutoff", "Markdown", "Supplier delivery",
                 "Pick and pack", "Transport", "Depends on", "Unscheduled = no planned time in the data"):
        expect(legend).to_contain_text(word)
    page.get_by_role("group", name="Window").get_by_role("button", name="Week").click()
    expect(page.get_by_test_id("schedule-connector")).to_have_count(0)


# --- Task 3: selection and the read-only drawer, the status column, empty states and overflow ------------------------

def drawer_of(page: Page, title: str):
    """The row's drawer, named by the row's title (the sidebar is a complementary region too)."""
    return page.get_by_role("complementary", name=title, exact=True)


# The recorded states these tests meet, in the page's words (pages/schedule.tsx STATUS).
STATUS_TEXT = {"open": "Open", "not_started": "Not started", "scheduled": "Scheduled", "not_requested": "Not requested"}
NOT_PROMISED = re.compile(r"\b(ETA|delay(ed)?|late|overdue|countdown|ago|now|min(utes)? left)\b", re.IGNORECASE)


def test_a_row_click_selects_it_and_opens_the_read_only_drawer(visitor):
    page = visitor.page
    story = prepare_story(page)
    wo = f"wo:{story['case_id']}"
    page.goto("/#/schedule")
    sched = get(page, "/api/schedule")
    by_id = {r["id"]: r for r in sched["rows"]}

    pack = row(page, f"{wo}:pick_and_pack")
    expect(pack).to_have_attribute("aria-selected", "false")
    pack.get_by_role("button").first.click()
    expect(pack).to_have_attribute("aria-selected", "true")
    drawer = drawer_of(page, by_id[f"{wo}:pick_and_pack"]["title"])
    expect(drawer).to_be_visible()
    for label in ("Owner", "Branch", "Quantity", "Planned", "Status", "Depends on", "Scheduled truck run"):
        expect(drawer.get_by_test_id(f"kv-{label}")).to_be_visible()
    expect(drawer.get_by_test_id("kv-Owner")).to_contain_text(by_id[f"{wo}:pick_and_pack"]["owner"]["label"])
    expect(drawer.get_by_test_id("kv-Scheduled truck run")).to_contain_text(by_id[f"{wo}:pick_and_pack"]["run_code"])
    start = by_id[f"{wo}:pick_and_pack"]["start"]["min"]
    expect(drawer.get_by_test_id("kv-Planned")).to_contain_text(f"{start // 60:02d}:{start % 60:02d}")
    expect(drawer.get_by_role("link", name="View stock decision")).to_have_attribute(
        "href", f"#/cases/{urllib.parse.quote(story['key'], safe='')}")
    # Read-only: no input, select, textarea or editable control in the drawer, no write from the page.
    expect(drawer.locator("input, select, textarea, [contenteditable], [draggable=true]")).to_have_count(0)
    assert not NOT_PROMISED.search(drawer.inner_text()), NOT_PROMISED.search(drawer.inner_text())

    # The receive has no planned time in the data.
    row(page, f"{wo}:receive").get_by_role("button").first.click()
    expect(row(page, f"{wo}:receive")).to_have_attribute("aria-selected", "true")
    expect(row(page, f"{wo}:pick_and_pack")).to_have_attribute("aria-selected", "false")
    receive = drawer_of(page, by_id[f"{wo}:receive"]["title"])
    expect(receive.get_by_test_id("kv-Planned")).to_have_text("Unscheduled · no planned time in the data")
    expect(receive.get_by_test_id("kv-Depends on")).to_have_text(by_id[f"{wo}:transport"]["title"])

    # A work order lists its activities.
    row(page, wo).get_by_role("button").nth(1).click()
    expect(row(page, wo)).to_have_attribute("aria-selected", "true")
    expect(drawer_of(page, by_id[wo]["title"]).get_by_test_id("schedule-activities").get_by_role("listitem")).to_have_count(
        len([r for r in sched["rows"] if r["parent_id"] == wo]))
    # The status column shows the server's recorded state, with an icon and text.
    for kind in ("pick_and_pack", "receive"):
        expect(row(page, f"{wo}:{kind}").get_by_test_id("schedule-status")).to_have_text(
            STATUS_TEXT[by_id[f"{wo}:{kind}"]["status"]])


def empty_answer(route) -> None:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(route.request.url).query)
    date = q.get("date", ["2026-10-17"])[0]
    route.fulfill(json={"date": date, "view": "day", "group": "work_order", "branch": "S000",
                        "scenario_date": "2026-10-17",
                        "window": {"from": date, "to": date, "from_day": 3, "to_day": 3},
                        "rows": [], "page": 1, "page_size": 50, "total": 0})


def test_nothing_planned_and_no_match_say_so(visitor):
    page = visitor.page
    page.route("**/api/schedule*", empty_answer)
    page.goto("/#/schedule?date=2026-10-18")
    empty = page.get_by_test_id("schedule-empty")
    expect(empty.get_by_role("heading", name="Nothing planned for Sun 18 Oct 2026")).to_be_visible()
    expect(empty).to_contain_text(
        "Planned work appears here when a stock decision is sent or a scheduled truck run is booked.")
    page.get_by_label("Activity", exact=True).select_option("markdown")
    expect(page.get_by_test_id("schedule-empty").get_by_role("heading")).to_have_text(
        "No activity matches these filters.")


WIDTHS = (360, 390, 768, 1280)
NO_SIDE_SCROLL = ("() => document.documentElement.scrollWidth <= window.innerWidth "
                  "&& document.querySelector('main#content').scrollWidth "
                  "<= document.querySelector('main#content').clientWidth")


def no_side_scroll(page: Page) -> bool:
    try:
        page.wait_for_function(NO_SIDE_SCROLL, timeout=5_000)
    except PlaywrightTimeout:
        return False
    return True


def test_the_grid_scrolls_in_its_own_box_with_the_left_column_sticky(visitor):
    page = visitor.page
    prepare_story(page)
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        page.goto("/#/schedule")
        expect(page.get_by_test_id("schedule-row").first).to_be_visible()
        assert no_side_scroll(page), width
        box = page.get_by_test_id("schedule-scroll")
        if width < 1280:
            assert box.evaluate("el => el.scrollWidth > el.clientWidth"), width
        left = page.get_by_test_id("schedule-row").first.get_by_test_id("schedule-left")
        assert left.evaluate("el => getComputedStyle(el).position") == "sticky", width
        if width < 768:
            assert round(left.evaluate("el => el.getBoundingClientRect().width")) == 160, width
            box.evaluate("el => { el.scrollLeft = 400; }")
            assert left.evaluate("el => el.getBoundingClientRect().left") == box.evaluate(
                "el => el.getBoundingClientRect().left"), width
    page.get_by_test_id("schedule-row").first.get_by_role("button").last.click()
    assert no_side_scroll(page)


def test_a_failed_load_says_so_and_try_again_loads(visitor):
    page = visitor.page
    message = "The database is busy. Try again in a moment."
    page.route("**/api/schedule*", lambda route: route.fulfill(
        status=503, json={"error": "db_busy", "message": message}))
    page.goto("/#/schedule")
    expect(page.get_by_role("heading", name="The planned schedule didn't load")).to_be_visible()
    expect(page.get_by_text(message, exact=True)).to_be_visible()
    page.unroute("**/api/schedule*")
    page.get_by_role("button", name="Try again").click()
    expect(page.get_by_role("treegrid", name="Planned schedule")).to_be_visible()


def test_the_page_never_writes(visitor):
    page = visitor.page
    writes: list[str] = []
    page.on("request", lambda r: writes.append(f"{r.method} {r.url}") if r.method not in ("GET", "HEAD") else None)
    page.goto("/#/schedule")
    expect(page.get_by_role("treegrid", name="Planned schedule")).to_be_visible()
    for b in ("Week", "Month", "Day"):
        page.get_by_role("group", name="Window").get_by_role("button", name=b).click()
    assert writes == []
    expect(page.locator("[draggable=true], [contenteditable]")).to_have_count(0)


def test_the_date_window_and_activity_live_in_the_address_so_back_and_reload_keep_them(visitor):
    """QA F2 (quick 261018-hel): Next day, Week and Activity: Markdown, then Tasks and Back, return to the same
    schedule; a reload keeps it too; an unknown value in the address falls back to the default without an error."""
    page = visitor.page
    page.goto("/#/schedule")
    expect(page.get_by_role("heading", level=1, name="Planned schedule")).to_be_visible()
    stepper = page.get_by_role("group", name="Date")
    with page.expect_request(schedule_request(view="day")) as req:
        stepper.get_by_role("button", name="Next day").click()
    nxt = urllib.parse.parse_qs(urllib.parse.urlparse(req.value.url).query)["date"][0]
    window = page.get_by_role("group", name="Window")
    with page.expect_request(schedule_request(view="week", date=nxt)):
        window.get_by_role("button", name="Week").click()
    activity = page.get_by_label("Activity", exact=True)
    with page.expect_response(schedule_request(view="week", date=nxt, activity="markdown")):
        activity.select_option("markdown")
    expect(page).to_have_url(re.compile(r"#/schedule\?.*view=week"))
    rng = page.get_by_test_id("schedule-range")
    expect(rng).to_contain_text(" – ")  # the week's range
    label = rng.inner_text()

    def kept() -> None:
        expect(window.get_by_role("button", name="Week")).to_have_attribute("aria-pressed", "true")
        expect(activity).to_have_value("markdown")
        expect(rng).to_have_text(label)

    # The Schedule link opens the default schedule, though the page mounted at that same address; Back returns to the
    # chosen one (Codex check hel, findings 2).
    with page.expect_request(schedule_request(view="day", activity=None)):
        page.get_by_role("navigation", name="Main").get_by_role("link", name="Schedule").click()
    expect(window.get_by_role("button", name="Day")).to_have_attribute("aria-pressed", "true")
    expect(page).to_have_url(re.compile(r"#/schedule$"))
    with page.expect_request(schedule_request(view="week", date=nxt, activity="markdown")):
        page.go_back()
    kept()

    page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile("^Tasks")).click()
    expect(page).to_have_url(re.compile(r"#/tasks"))
    with page.expect_request(schedule_request(view="week", date=nxt, activity="markdown")):
        page.go_back()
    kept()
    with page.expect_request(schedule_request(view="week", date=nxt, activity="markdown")):
        page.reload()
    kept()

    # An unknown value is ignored: the default window and every activity, and the page loads (opened from another
    # page, so the schedule mounts with that address).
    page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile("^Tasks")).click()
    expect(page).to_have_url(re.compile(r"#/tasks"))
    with page.expect_request(schedule_request(view="day", activity=None)):
        page.goto("/#/schedule?view=year&activity=nope")
    expect(window.get_by_role("button", name="Day")).to_have_attribute("aria-pressed", "true")
    expect(activity).to_have_value("")
    expect(page.get_by_test_id("schedule-range")).not_to_have_text("")
    expect(page.get_by_role("heading", name="The planned schedule didn't load")).to_have_count(0)
