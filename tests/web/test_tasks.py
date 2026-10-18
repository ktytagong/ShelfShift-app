"""Tasks (06.1-UI-SPEC "Tasks"; 06.1 D-39, D-50, D-52, D-58, D-61): the Phase 5 Requests page moved into the shell.
Verify before you move, in the browser: the source store asks a branch for a stock check on the story decision's
plan-changing count fact, the addressed store answers and confirms it on Tasks, and the stock decision re-plans;
confirming approves nothing. The Phase 5 partial-answer rules (05-UI-SPEC A-11, A-12, A-24, A-26) are kept with the
06.1 terms and the two clocks (D-58). Every expectation is read from the API (the case detail, its history, the
requests and the session), never pasted: the tests assert beats (a new recommendation id, the confirmed line), never a
quantity.

The stock check is requested through the API in setup (POST /api/cases/{id}/requests as the source manager), the way
the stock decision's Context step sends it, so these tests do not depend on that page. `ask_for_count` (the Phase 5
case page button) and the other helpers here are kept for the older tests that import them (test_isolation,
test_journey, test_stale_and_hold, test_case_actions, test_activity_export)."""
from __future__ import annotations

import datetime
import json
import re
import time
import urllib.parse
import uuid
import zoneinfo

import pytest
import sqlalchemy as sa
from playwright.sync_api import Page, expect

from api import config
from tests.web.conftest import CHECKED_NOW, case_json, cards_json, db_tx, decision_body, open_case, session_json, switch_seat

pytestmark = pytest.mark.web

COUNT_KINDS = ("PHANTOM_STOCK", "STALE_COUNT")  # api/signals.py COUNT_KINDS: the kinds a count settles
STORY = "SUS-19"  # the story decision's key on the release data
EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it."  # api/signals.py EARLIER_DAY
FINISHED = "Saved to history. This stock decision is closed, so this count cannot be confirmed."  # the user's words
DEMO_WORDS = re.compile(r"\b(demo|seat|sandbox|next day|start here)\b", re.IGNORECASE)  # D-03: none on Tasks
# "When did you check?" (07-CONTEXT D-22; 07-UI-SPEC CheckTime)
WHEN = "When did you check?"
JUST_NOW = "Checked just now"
AT_A_TIME = "At a date and time"
TIME_BOX = "Date and time checked (Bangkok time)"
TIME_MISSING = "Choose when you checked: Checked just now, or a date and time."
TIME_LATER = "The check time can't be later than now."
LATE = "Answered after deadline"


# --- helpers kept for the older tests ---------------------------------------------------------------------------------

def count_fact(d: dict) -> dict:
    """The first fact check on the active version a count can settle and that changes the plan."""
    found = [f for f in d["fact_checks"] if f["kind"] in COUNT_KINDS and f["changes_plan"]]
    assert found, f"{d['key']} has no plan-changing count fact: {[(f['kind'], f['changes_plan']) for f in d['fact_checks']]}"
    return found[0]


def fact_row(page: Page, fact_id: str):
    return page.locator(f"main#content li[data-fact-id='{fact_id}']")


def request_for(page: Page, d: dict, fact_id: str) -> dict:
    """The case's request for this fact on the version `d` showed, read back from the API."""
    found = [r for r in case_json(page, d["case_id"])["requests"]
             if r["fact_id"] == fact_id and r["rec_version"] == d["rec_version"]]
    assert len(found) == 1, found
    return found[0]


ASK = re.compile(r"^(Ask this branch for a count|Request stock check|Count stock)$")  # Phase 5, D-61 and 07 D-36 labels
ASKED = re.compile(r"(Evidence requested from|Stock check requested from|Awaiting response · respond by )")


def ask_for_count(page: Page) -> tuple[dict, dict]:
    """As the source manager on SUS-19, press the stock check button on its plan-changing count fact (the case page);
    returns the case detail before the ask and the request the API made."""
    d = open_case(page, STORY)
    fact = count_fact(d)
    row = fact_row(page, fact["fact_id"])
    row.get_by_role("button", name=ASK).click()
    # 07-21 (07-CONTEXT D-36): Count stock opens the request panel; its Send request makes the request
    row.get_by_test_id("request-panel").get_by_role("button", name="Send request", exact=True).click()
    expect(row).to_contain_text(ASKED)
    q = request_for(page, d, fact["fact_id"])
    expect(row).to_contain_text(q["to_branch"])
    expect(row.get_by_role("button", name=ASK)).to_have_count(0)
    return d, q


def request_card(page: Page, q: dict):
    """The Tasks row for this request (07-19: one row per request group, `data-request-id` its group), opened if it is
    collapsed (sanctioned helper change: the row's title button discloses the form, R-5)."""
    group = q.get("request_group") or q["request_id"]
    card = page.locator(f"main#content article[data-request-id='{group}']")
    expand(card)
    return card


def expand(card) -> None:
    """Open a collapsed Tasks row through its title button (aria-expanded)."""
    toggle = card.get_by_role("heading", level=3).get_by_role("button")
    expect(toggle).to_be_visible()
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")


def show(page: Page, view: str) -> None:
    """Pick Open or Closed on Tasks and wait for the list it loads."""
    with page.expect_response(lambda r: "/api/tasks?" in r.url and f"view={view.lower()}" in r.url):
        page.get_by_role("group", name="Show").get_by_role("button", name=view, exact=True).click()


def tile_count(page: Page, tile: str):
    return page.get_by_test_id(f"tile-{tile}").get_by_test_id("tile-count")


def open_tasks_page(page: Page) -> None:
    """Open Tasks through the Main navigation, the way a person does (D-50)."""
    page.get_by_role("navigation", name="Main").get_by_role("link", name=re.compile(r"^Tasks\b")).click()
    expect(page.get_by_role("heading", level=1)).to_have_text("Tasks")


open_requests_page = open_tasks_page  # the Phase 5 name, kept for the older tests (#/requests redirects here)


def count_input(card, q: dict, lot_id: str, field: str):
    return card.locator(f"input[id='{q['request_id']}-{lot_id}-{field}']")


def recorded_time(iso: str, zone: str) -> str:
    """A recorded server time as the page shows it, "16 Oct 2026, 20:30" (day, short month name, year, 24-hour
    time) in `zone`, computed here from the API's own time (05-21 imports it)."""
    at = datetime.datetime.fromisoformat(iso).astimezone(zoneinfo.ZoneInfo(zone))
    return f"{at.day} {at.strftime('%b')} {at.year}, {at:%H:%M}"


def city(zone: str) -> str:
    return zone.split("/")[-1].replace("_", " ")


def pin_browser_zone_elsewhere(page: Page, zone: str):
    """Pin the browser's own time zone to one that is not `zone` (America/New_York, or UTC when `zone` is New York)
    through a CDP session, and check the page now reports it; returns the CDP session, which the caller keeps for the
    rest of the test (the override lasts as long as it does) (05-21 imports it)."""
    other = "UTC" if zone == "America/New_York" else "America/New_York"
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setTimezoneOverride", {"timezoneId": other})
    assert page.evaluate("() => Intl.DateTimeFormat().resolvedOptions().timeZone") == other
    return cdp, other


def summary_value(card, label: str):
    """The value cell of one row of a card's read-only response summary (a <dd> after its <dt> label)."""
    return card.get_by_test_id("response-summary").locator(
        f"xpath=.//dt[normalize-space()='{label}']/following-sibling::dd[1]")


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def scenario_date(session: dict, day: int) -> str:
    """"Scenario date: Sat 17 Oct 2026" for a calendar day of the session."""
    iso = next(c["date"] for c in session["calendar"] if c["day"] == day)
    d = datetime.date.fromisoformat(iso)
    return f"Scenario date: {d:%a} {d.day} {d:%b} {d.year}"


def api_request(page: Page, request_id: str) -> dict:
    """The request as GET /api/requests gives it to the page's current account."""
    r = page.request.get("/api/requests")
    assert r.status == 200, r.status
    found = [x for x in r.json() if x["request_id"] == request_id]
    assert len(found) == 1, found
    return found[0]


def refocus(page: Page) -> None:
    """Fire visibilitychange (the tab became visible again) and wait for the Tasks refetch it causes."""
    with page.expect_response(lambda r: "/api/tasks?" in r.url and r.request.method == "GET"):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def next_day(page: Page) -> None:
    """Move the workspace one scenario day on through the API (the harness runs with demo mode on; the product has no
    control for it, D-03, D-36), then reload so the page reads the new day."""
    s = session_json(page)
    r = page.request.post("/api/demo/advance-day", headers={"Idempotency-Key": str(uuid.uuid4())},
                          data={"expected_version": s["version"]})
    assert r.status == 200 and r.json()["advanced"] is True, r.text()
    assert session_json(page)["day"] == s["day"] + 1
    page.reload()
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()


def checked_just_now(card) -> None:
    """Answer "When did you check?" with "Checked just now" (07-CONTEXT D-22; sanctioned in 07-17)."""
    card.get_by_role("group", name=WHEN).get_by_role("radio", name=JUST_NOW, exact=True).check()


def send_counts(page: Page, card, q: dict, value: str = "0") -> None:
    for lot in q["lot_ids"]:
        for field in ("shelf", "backroom"):
            count_input(card, q, lot, field).fill(value)
    checked_just_now(card)
    card.get_by_role("button", name="Send answer").click()


def bangkok_wall(iso: str, minutes: int = 0) -> datetime.datetime:
    """The server time `iso` moved by `minutes`, as Bangkok wall-clock time."""
    at = datetime.datetime.fromisoformat(iso) + datetime.timedelta(minutes=minutes)
    return at.astimezone(zoneinfo.ZoneInfo("Asia/Bangkok"))


def overdue(page: Page, q: dict) -> None:
    """Move the request's real deadline a minute into the past (test setup: the 60-minute wait, 07-CONTEXT D-05)."""
    from tests.web.conftest import run_id
    with db_tx() as conn:
        n = conn.execute(sa.text("UPDATE evidence_requests SET due_at = now() - interval '1 minute' WHERE run_id = :r "
                                 "AND request_id = :q"), {"r": run_id(page), "q": q["request_id"]}).rowcount
    assert n == 1, n


def key() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def quote(case_id: str) -> str:
    return urllib.parse.quote(case_id, safe="")


# --- setup through the API --------------------------------------------------------------------------------------------

def request_stock_check(page: Page) -> tuple[dict, dict]:
    """As the source manager of SUS-19, request a stock check on its plan-changing count fact through the API (the
    Context step's own call); returns the case detail before the ask and the request. The page stays signed in as the
    source manager."""
    card = next((c for c in cards_json(page) if c["key"] == STORY), None)
    assert card is not None, f"{STORY} is not on the signed-in store's Decision board"
    d = case_json(page, card["case_id"])
    if session_json(page)["branch"] != d["source"]:
        switch_seat(page, d["source"])
    fact = count_fact(d)
    r = page.request.post(f"/api/cases/{quote(d['case_id'])}/requests", headers=key(),
                          data={"rec_version": d["rec_version"], "fact_id": fact["fact_id"]})
    assert r.status == 200, r.text()
    return d, r.json()


def assigned_at(page: Page, q: dict) -> str:
    """When the request was made: its `evidence_requested` entry in the stock decision's history."""
    r = page.request.get(f"/api/cases/{quote(q['case_id'])}/activity")
    assert r.status == 200, r.status
    found = [e["at"] for e in r.json() if e["type"] == "evidence_requested" and e["payload"].get("request_id") == q["request_id"]]
    assert len(found) == 1, found
    return found[0]


def left_rule(locator) -> str:
    return locator.evaluate("e => getComputedStyle(e).borderLeftWidth")


# --- Task 1: the count loop on Tasks ------------------------------------------------------------------------------------

def test_answer_and_confirm_a_count_on_tasks(visitor):
    """Tracer (07-19 Task 1): the sidebar's Tasks item carries the session's count (and no Requests or Signals item);
    Tasks shows the tile "Checks to answer 1" and a collapsed row "Count stock · lot {lot}" with the server's
    "Awaiting response · respond by {t}" and the bar's "{N} min left"; "Answer request" opens the form with Shelf and
    Backroom "Not checked"; counts, "Checked just now" and Send answer give "Answered · confirm the count"; the tick and
    Confirm count give the server's green "Count confirmed by {store} manager · {t} · shelf a + backroom b = c", the row
    moves to Closed and the stock decision re-plans, approving and holding nothing. The D-58 lines are kept."""
    page = visitor.page
    d, q = request_stock_check(page)
    before, round_before = d["versions"]["rec_version"], d["approvals"]["round"]
    when = q["asked_at"]

    switch_seat(page, q["to_branch"])
    s = session_json(page)
    assert s["time_zone"] == config.SETTINGS["caps"]["time_zone"] == "Asia/Bangkok", s["time_zone"]
    nav = page.get_by_role("navigation", name="Main")
    expect(nav.get_by_role("link", name=re.compile(r"^(Requests|Signals)\b"))).to_have_count(0)
    expect(nav.get_by_role("link", name=re.compile(r"^Tasks\b"))).to_contain_text(str(s["nav_counts"]["tasks"]))
    cdp, pinned = pin_browser_zone_elsewhere(page, s["time_zone"])  # kept for the rest of the test
    open_tasks_page(page)
    tiles = page.get_by_role("group", name="Filter tasks")
    expect(tiles.get_by_role("button")).to_have_count(6)
    expect(tile_count(page, "to_answer")).to_have_text("1")  # a fresh workspace has no other stock check
    expect(tile_count(page, "to_confirm")).to_have_text("0")
    listed = page.request.get("/api/tasks").json()
    assert s["nav_counts"]["tasks"] == sum(listed["tiles"].values()), (s["nav_counts"], listed["tiles"])

    card = page.locator(f"main#content article[data-request-id='{q['request_id']}']")
    title = card.get_by_role("heading", level=3, name=f"Count stock · lot {', '.join(q['lot_ids'])}", exact=True)
    expect(title).to_be_visible()
    seen = api_request(page, q["request_id"])
    assert seen["status_kind"] == "AWAITING", seen["status_kind"]
    expect(card.get_by_text(seen["status"], exact=True)).to_be_visible()  # the server's sentence (07-CONTEXT D-06)
    left = card.get_by_test_id("deadline-text")
    expect(left).to_have_text(re.compile(r"^(\d+ min left|Less than 1 min left)$"))
    n = int(left.inner_text().split()[0]) if left.inner_text()[0].isdigit() else 0
    assert seen["minutes_left"] - 1 <= n <= seen["minutes_left"], (n, seen["minutes_left"])  # the server's clock
    expect(card).to_contain_text(f"for stock decision {q['case_key']}")
    expect(card.locator("input[type='number']")).to_have_count(0)  # collapsed
    assert left_rule(card) == "4px", "the store acts on this stock check: the 4px rule"
    card.get_by_role("button", name="Answer request", exact=True).click()
    expect(card.get_by_role("heading", level=3).get_by_role("button")).to_have_attribute("aria-expanded", "true")
    expect(card).to_contain_text(q["ask_text"])
    expect(card.get_by_role("list", name="Request progress")).to_be_visible()
    expect(card.get_by_test_id("assigned-line")).to_have_text(
        f"Stock check assigned to {q['to_branch']} · Recorded {recorded_time(when, s['time_zone'])} "
        f"{city(s['time_zone'])} time.")
    expect(card.get_by_test_id("due-line")).to_have_count(0)  # the real deadline is in the server's sentence (D-05)
    expect(card.get_by_role("link", name="View stock decision")).to_have_attribute("href", f"#/cases/{quote(q['case_id'])}")
    for lot in q["lot_ids"]:  # the lot label is the legend over its Shelf and Backroom inputs (05-UI-REVIEW finding 6)
        group = card.get_by_role("group", name=f"Lot {lot}", exact=True)
        expect(group).to_be_visible()
        for field, label in (("shelf", "Shelf"), ("backroom", "Backroom")):
            box = group.get_by_label(label, exact=True)
            expect(box).to_have_value("")
            expect(box).to_have_attribute("placeholder", "Not checked")
            box.fill("0")
    checked_just_now(card)
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
    expect(tile_count(page, "to_answer")).to_have_text("0")
    expect(tile_count(page, "to_confirm")).to_have_text("1")

    # The answer reads back as a read-only response (G-05-2 item 12) with the recorded row (D-58)
    expect(card.get_by_test_id("response-summary")).to_be_visible()
    expect(card.locator("input[type='number']")).to_have_count(0)
    stored = api_request(page, q["request_id"])
    ans = stored["answer"]
    for lot in q["lot_ids"]:
        expect(summary_value(card, f"Lot {lot}")).to_have_text("Shelf 0 · Backroom 0 · Total 0 units")
    expect(summary_value(card, "Submitted by")).to_have_text(f"{ans['reporter_branch']} manager")
    expect(summary_value(card, "Recorded")).to_have_text(
        f"{recorded_time(ans['created_at'], s['time_zone'])} {city(s['time_zone'])} time")
    elsewhere = recorded_time(ans["created_at"], pinned)
    if elsewhere != recorded_time(ans["created_at"], s["time_zone"]):
        expect(summary_value(card, "Recorded")).not_to_contain_text(elsewhere)
    expect(summary_value(card, "Status")).to_have_text("Awaiting confirmation")
    expect(card.get_by_test_id("response-note")).to_have_count(0)
    width = card.get_by_test_id("response-summary").evaluate("e => e.getBoundingClientRect().width")
    assert width <= 448, width  # the summary is capped at max-w-md (05-UI-REVIEW finding 7)
    assert cdp is not None

    confirm = card.get_by_role("button", name="Confirm count")
    expect(confirm).to_be_disabled()
    expect(card).to_contain_text("Confirming approves nothing. It re-plans every affected open stock decision, which "
                                 "cancels any approval in progress. Committed stock decisions in scope are flagged for review.")
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    confirm.click()
    said = page.get_by_test_id("task-status")
    expect(said).to_contain_text(f"Count confirmed by {q['to_branch']} manager · ")
    expect(tile_count(page, "to_confirm")).to_have_text("0")
    expect(page.get_by_role("heading", level=2, name=f"No open tasks for {q['to_branch']}", exact=True)).to_be_visible()

    show(page, "Closed")
    closed = request_card(page, q)
    expect(summary_value(closed, "Status")).to_have_text("Confirmed")
    expect(closed.get_by_text(re.compile(rf"^Count confirmed by {q['to_branch']} manager · .* · shelf 0 \+ backroom 0 = 0$")))\
        .to_be_visible()
    assert left_rule(closed) != "4px", "nothing left to do on a confirmed stock check"

    # One link label everywhere (D-39); no demo words (D-03); "case" never on screen (G3)
    main = page.locator("main#content")
    expect(main.get_by_role("link", name="View linked decision")).to_have_count(0)
    expect(main.get_by_role("link", name=re.compile(r"^Open "))).to_have_count(0)
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()
    assert not re.search(r"\bcases?\b", main.inner_text(), re.IGNORECASE), main.inner_text()

    # The stock decision re-planned; the confirm approved nothing and holds nothing
    after = case_json(page, q["case_id"])
    assert after["versions"]["rec_version"] != before, "the confirm gave the stock decision a new version"
    assert after["state"] == "OPEN" and after["hold"] is None, (after["state"], after["hold"])
    assert after["approvals"]["round"] == round_before
    with db_tx() as conn:
        held = conn.execute(sa.text(
            "SELECT count(*) FROM reservations WHERE run_id = :r AND case_id = :c AND state = 'HELD'"),
            {"r": session_json(page)["run_id"], "c": after["case_id"]}).scalar_one()
    assert held == 0, held


def test_a_text_only_reply_keeps_the_request_open(visitor):
    page = visitor.page
    _, q = request_stock_check(page)
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    card.get_by_label("Reply").fill("Counting after the delivery is put away.")
    checked_just_now(card)
    card.get_by_role("button", name="Send answer").click()
    expect(card).to_contain_text("Shelf: not checked")  # the stored answer is back on screen

    stored = api_request(page, q["request_id"])
    assert stored["state"] == "OPEN" and stored["partial_note"], stored
    assert stored["status_kind"] == "TEXT_PARTIAL", stored["status_kind"]
    # the server's sentence (its partial note) is the state pill, word for word and once (07-CONTEXT D-06)
    expect(card.get_by_text(stored["status"], exact=True)).to_have_count(1)
    expect(card).to_contain_text("Shelf or backroom not checked, so the total can't be confirmed.")
    expect(card).to_contain_text("Counting after the delivery is put away.")
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)


def test_unsaved_counts_survive_a_refetch(visitor):
    page = visitor.page
    _, q = request_stock_check(page)
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    shelf = count_input(card, q, q["lot_ids"][0], "shelf")
    shelf.fill("7")
    card.get_by_role("textbox", name="Reply", exact=True).fill("Half counted")
    checked_just_now(card)
    just_now = card.get_by_role("group", name=WHEN).get_by_role("radio", name=JUST_NOW, exact=True)

    refocus(page)  # a background refetch never wipes what was typed, the check time included
    expect(shelf).to_have_value("7")
    expect(card.get_by_role("textbox", name="Reply", exact=True)).to_have_value("Half counted")
    expect(just_now).to_be_checked()
    expect(card).not_to_contain_text("The answer changed since you looked at it")

    # The answer changes behind the page (the same account, through the API): the kept draft stays and says so.
    counts = {x: {"shelf_units": None, "backroom_units": None} for x in q["lot_ids"]}
    r = page.request.post(f"/api/requests/{q['request_id']}/answer", headers=key(),
                          multipart={"text": "Answered elsewhere", "counts": json.dumps(counts), **CHECKED_NOW})
    assert r.status == 200, r.text()
    refocus(page)
    expect(card).to_contain_text("Answered elsewhere")
    expect(card).to_contain_text("The answer changed since you looked at it")
    expect(shelf).to_have_value("7")
    expect(card.get_by_role("textbox", name="Reply", exact=True)).to_have_value("Half counted")
    expect(just_now).to_be_checked()


def test_an_expired_request_takes_a_late_answer(visitor):
    """A late answer is kept (spec §7 rev 6.7; 07-CONTEXT D-05, D-06, D-08): past its real deadline a request stays
    OPEN and reads the server's "Unanswered · Unverified" (no day-end EXPIRED any more); the addressed store still
    answers it, the complete late answer reads back with "Answered after deadline" as a warn chip on its own line, and
    is confirmed, the label staying. The deadline is moved into the past in the database (test setup)."""
    page = visitor.page
    _, q = request_stock_check(page)
    overdue(page, q)

    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    seen = api_request(page, q["request_id"])
    assert (seen["state"], seen["status_kind"], seen["overdue"]) == ("OPEN", "UNANSWERED", True), seen
    expect(card.get_by_text("Unanswered · Unverified", exact=True)).to_be_visible()
    expect(card.get_by_test_id("expired-line")).to_have_count(0)  # the legacy EXPIRED line only on legacy rows
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)
    send_counts(page, card, q)
    expect(summary_value(card, "Status")).to_have_text("Awaiting confirmation")

    late = api_request(page, q["request_id"])
    assert late["state"] == "ANSWERED" and late["late"] is True, late
    note = card.get_by_test_id("response-note")
    expect(note).to_have_text(LATE)
    status_box = summary_value(card, "Status").evaluate("e => e.getBoundingClientRect().bottom")
    assert note.evaluate("e => e.getBoundingClientRect().top") >= status_box, "the chip sits on its own line"
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    card.get_by_role("button", name="Confirm count").click()
    expect(page.get_by_test_id("task-status")).to_contain_text("Count confirmed by")
    show(page, "Closed")  # a confirmed request is a closed task (07-19)
    card = request_card(page, q)
    expect(summary_value(card, "Status")).to_have_text("Confirmed")
    expect(card.get_by_test_id("response-note")).to_have_text(LATE)  # it stays (07-CONTEXT D-08)
    assert api_request(page, q["request_id"])["state"] == "CONFIRMED"


def test_a_late_answer_on_a_finished_case_is_kept_and_confirmable(visitor):
    """07-CONTEXT D-08, D-17, D-22 (04-API-CONTRACT 07-06 row: `answerable` and `confirmable` no longer depend on the
    case's state; the fact is about the lot): after the stock decision is rejected, its overdue request still takes a
    late answer on Tasks, and the complete late answer is offered Confirm count with "Answered after deadline". This
    replaces 05-REVIEW WR-10's "a finished case offers no form", which the Phase 7 server no longer holds."""
    page = visitor.page
    asked, q = request_stock_check(page)
    overdue(page, q)
    r = page.request.post(f"/api/cases/{quote(asked['case_id'])}/reject", headers=key(),
                          data={"reason": "Checked with the branch; not moving this lot"})
    assert r.status == 200, r.text()

    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    seen = api_request(page, q["request_id"])
    assert (seen["state"], seen["answerable"], seen["confirmable"]) == ("OPEN", True, False), seen
    expect(card.get_by_role("button", name="Send answer")).to_be_visible()
    expect(card.get_by_test_id("not-confirmable")).to_have_count(0)
    send_counts(page, card, q)
    expect(card.get_by_test_id("response-note")).to_have_text(LATE)  # the answer is back on screen
    kept = api_request(page, q["request_id"])
    assert (kept["state"], kept["late"], kept["confirmable"]) == ("ANSWERED", True, True), kept
    expect(card.get_by_role("button", name="Confirm count")).to_be_visible()
    expect(card.get_by_test_id("not-confirmable")).to_have_count(0)


def test_an_answer_states_when_it_was_checked(visitor):
    """07-CONTEXT D-22 (07-UI-SPEC CheckTime): "When did you check?" has no default; Send answer without a choice
    shows the reason under the legend, marks the group and focuses its first radio, and sends nothing; a typed time
    later than now (Bangkok wall time) is refused the same way; "Checked just now" sends `observed` "now" and the
    server records its own time."""
    page = visitor.page
    _, q = request_stock_check(page)
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    when = card.get_by_role("group", name=WHEN)
    now_radio = when.get_by_role("radio", name=JUST_NOW, exact=True)
    at_radio = when.get_by_role("radio", name=AT_A_TIME, exact=True)
    expect(now_radio).not_to_be_checked()
    expect(at_radio).not_to_be_checked()
    expect(card.get_by_label(TIME_BOX, exact=True)).to_have_count(0)  # shown only after "At a date and time"
    for lot in q["lot_ids"]:
        for field in ("shelf", "backroom"):
            count_input(card, q, lot, field).fill("0")
    posts: list = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and r.url.endswith("/answer") else None)

    card.get_by_role("button", name="Send answer").click()
    expect(when.get_by_text(TIME_MISSING, exact=True)).to_be_visible()
    expect(when).to_have_attribute("aria-invalid", "true")
    expect(now_radio).to_be_focused()

    at_radio.check()
    box = card.get_by_label(TIME_BOX, exact=True)
    expect(box).to_be_visible()
    server_now = api_request(page, q["request_id"])["server_now"]
    top = box.get_attribute("max")
    assert top is not None and abs((datetime.datetime.fromisoformat(top)
                                    - bangkok_wall(server_now).replace(tzinfo=None, second=0, microsecond=0)).total_seconds()) <= 180, top
    box.fill(bangkok_wall(server_now, 120).strftime("%Y-%m-%dT%H:%M"))
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text(TIME_LATER, exact=True)).to_be_visible()
    page.wait_for_timeout(300)
    assert posts == [], [r.url for r in posts]

    now_radio.check()
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
    assert len(posts) == 1, [r.url for r in posts]
    assert 'name="observed"' in posts[0].post_data and "now" in posts[0].post_data
    stored = api_request(page, q["request_id"])
    assert stored["observed_at"] == stored["answered_at"], (stored["observed_at"], stored["answered_at"])


def test_a_typed_check_time_is_bangkok_wall_time(visitor):
    """The time-zone backstop (07-UI-SPEC): with the browser's own zone pinned elsewhere, a typed check time is read as
    Bangkok wall time and stored as UTC (20:40 Bangkok → 13:40Z)."""
    page = visitor.page
    _, q = request_stock_check(page)
    switch_seat(page, q["to_branch"])
    cdp, _pinned = pin_browser_zone_elsewhere(page, session_json(page)["time_zone"])
    open_tasks_page(page)
    card = request_card(page, q)
    typed = bangkok_wall(api_request(page, q["request_id"])["server_now"], -90).replace(second=0, microsecond=0)
    for lot in q["lot_ids"]:
        for field in ("shelf", "backroom"):
            count_input(card, q, lot, field).fill("0")
    card.get_by_role("group", name=WHEN).get_by_role("radio", name=AT_A_TIME, exact=True).check()
    card.get_by_label(TIME_BOX, exact=True).fill(typed.strftime("%Y-%m-%dT%H:%M"))
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
    stored = api_request(page, q["request_id"])
    assert datetime.datetime.fromisoformat(stored["observed_at"]) == typed, (stored["observed_at"], typed.isoformat())
    assert cdp is not None


def test_a_lot_off_the_record_on_a_live_case_says_so(visitor):
    """05-REVIEW WR-12, A-26: on a live stock decision, a request whose counted lot is no longer on the record at the
    addressed branch can't be confirmed; the card names the lot reason, never the finished line. The lot is moved to
    the source branch in the database (test setup, by run_id and lot_id)."""
    from tests.web.conftest import run_id

    page = visitor.page
    asked, q = request_stock_check(page)
    from api import overlay

    run, lot = run_id(page), q["lot_ids"][0]
    with db_tx() as conn:  # the workspace owns the series first (api/overlay.py), so the update reaches its own row
        row = overlay.lots(conn, run, "shelfshift", [lot], q["to_branch"])[0]
        overlay.materialize(conn, run, "shelfshift", q["to_branch"], row["sku"])
        moved = conn.execute(sa.text("UPDATE lots SET branch = :b WHERE run_id = :r AND world = 'shelfshift' "
                                     "AND lot_id = :l"), {"b": asked["source"], "r": run, "l": lot}).rowcount
    assert moved == 1, moved
    lot_line = (f"{'The counted lot is' if len(q['lot_ids']) == 1 else 'A counted lot is'} no longer on record at "
                f"{q['to_branch']}, so this count cannot be confirmed.")

    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    seen = api_request(page, q["request_id"])
    assert (seen["state"], seen["answerable"], seen["confirmable"]) == ("OPEN", False, False), seen
    expect(card.get_by_test_id("lot-left")).to_have_text(lot_line)
    expect(card.locator("input")).to_have_count(0)
    expect(card.get_by_text(FINISHED)).to_have_count(0)


def test_an_earlier_days_answer_asks_for_a_new_count(visitor):
    """05-REVIEW CR-02, WR-10 (A-24): an answer from an earlier scenario day is not confirmed; the card asks for a new
    count with the API's own words and the form, then the new count is confirmed. The due day is moved two days on in
    the database (test setup), so the request stays ANSWERED over the next day."""
    from tests.web.conftest import run_id

    page = visitor.page
    _, q = request_stock_check(page)
    with db_tx() as conn:
        moved = conn.execute(sa.text("UPDATE evidence_requests SET due_day = due_day + 2 WHERE run_id = :r "
                                     "AND request_id = :q AND state = 'OPEN'"),
                             {"r": run_id(page), "q": q["request_id"]}).rowcount
    assert moved == 1, moved
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    send_counts(page, card, q)
    expect(card.get_by_role("button", name="Confirm count")).to_be_visible()

    next_day(page)
    card = request_card(page, q)  # the reload closes the row; open it again
    stale = api_request(page, q["request_id"])
    assert (stale["state"], stale["confirmable"], stale["answerable"]) == ("ANSWERED", False, True), stale
    expect(card.get_by_test_id("earlier-day")).to_have_text(EARLIER_DAY)
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)

    send_counts(page, card, q)
    expect(card.get_by_test_id("earlier-day")).to_have_count(0)
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    card.get_by_role("button", name="Confirm count").click()
    expect(page.get_by_test_id("task-status")).to_contain_text("Count confirmed by")


def test_a_refused_confirm_does_not_outlive_its_answer(visitor):
    """05-REVIEW WR-13: the day moves on behind a loaded page, so Confirm count is refused with the API's earlier-day
    message and the card asks for a new count; once it is sent, Confirm count comes back without the old refusal."""
    from tests.web.conftest import run_id

    page = visitor.page
    _, q = request_stock_check(page)
    with db_tx() as conn:
        conn.execute(sa.text("UPDATE evidence_requests SET due_day = due_day + 2 WHERE run_id = :r "
                             "AND request_id = :q AND state = 'OPEN'"), {"r": run_id(page), "q": q["request_id"]})
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    send_counts(page, card, q)
    confirm = card.get_by_role("button", name="Confirm count")
    expect(confirm).to_be_visible()

    s = session_json(page)  # the day moves on from "another window": the page is not told
    r = page.request.post("/api/demo/advance-day", headers=key(), data={"expected_version": s["version"]})
    assert r.status == 200 and r.json()["advanced"] is True, r.text()
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    with page.expect_response(lambda x: "/confirm" in x.url and x.request.method == "POST") as got:
        confirm.click()
    assert got.value.status == 409 and got.value.json()["message"] == EARLIER_DAY, got.value.text()
    expect(card.get_by_test_id("earlier-day")).to_have_text(EARLIER_DAY)
    expect(confirm).to_have_count(0)

    send_counts(page, card, q)
    expect(confirm).to_be_visible()
    expect(card.get_by_text(EARLIER_DAY)).to_have_count(0)
    expect(card.get_by_role("alert")).to_have_count(0)


def test_a_recount_keeps_the_answer_and_asks_again(visitor):
    """A-12: the source manager doubts an answered count and asks again (one keyed POST with recount_of, as the
    stock decision sends it). On Tasks the new request asks again, and the original keeps its response, read-only,
    with the grey pill "Replaced by a recount" and no Confirm count."""
    page = visitor.page
    d, q = request_stock_check(page)
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    send_counts(page, request_card(page, q), q)
    expect(summary_value(request_card(page, q), "Status")).to_have_text("Awaiting confirmation")
    expect(request_card(page, q).get_by_role("button", name="Request recount")).to_have_count(0)  # not the source

    switch_seat(page, d["source"])
    r = page.request.post(f"/api/cases/{quote(d['case_id'])}/requests", headers=key(),
                          data={"rec_version": d["rec_version"], "fact_id": q["fact_id"], "recount_of": q["request_id"]})
    assert r.status == 200, r.text()
    fresh = r.json()
    assert fresh["request_id"] != q["request_id"] and fresh["state"] == "OPEN", fresh

    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    new_card = request_card(page, fresh)
    for lot in q["lot_ids"]:
        expect(count_input(new_card, fresh, lot, "shelf")).to_have_value("")
    expect(tile_count(page, "to_answer")).to_have_text("1")  # the superseded one is closed (07-19)
    show(page, "Closed")
    old = request_card(page, q)
    expect(old.get_by_text("Replaced by a recount", exact=True)).to_be_visible()
    expect(old.get_by_text(f"Replaced by request {fresh['request_id']}", exact=True)).to_be_visible()
    expect(summary_value(old, "Status")).to_have_text("Not confirmed · replaced by a recount")
    expect(old.get_by_role("link", name="View stock decision")).to_be_visible()
    expect(old.get_by_role("button", name="Confirm count")).to_have_count(0)
    expect(old.locator("input")).to_have_count(0)


def test_a_recount_from_tasks_keeps_the_answer_and_asks_again(visitor):
    """Nyquist G17 (05-18): Request recount on Tasks, shown only to the stock decision's source manager on an
    answered, recountable request addressed to that store; the request is addressed to the source in the database
    (test setup) and answered through the API (its lots are not there, so the page offers no form, WR-12)."""
    from tests.web.conftest import run_id

    page = visitor.page
    d, q = request_stock_check(page)
    assert q["to_branch"] != d["source"], "the request already went to the source: the setup below is not needed"
    with db_tx() as conn:
        moved = conn.execute(sa.text("UPDATE evidence_requests SET to_branch = :b WHERE run_id = :r "
                                     "AND request_id = :q AND state = 'OPEN'"),
                             {"b": d["source"], "r": run_id(page), "q": q["request_id"]}).rowcount
    assert moved == 1, moved
    open_tasks_page(page)
    card = request_card(page, q)
    expect(card.locator("input")).to_have_count(0)
    counts = {x: {"shelf_units": 0, "backroom_units": 0} for x in q["lot_ids"]}
    r = page.request.post(f"/api/requests/{q['request_id']}/answer", headers=key(),
                          multipart={"text": "", "counts": json.dumps(counts), **CHECKED_NOW})
    assert r.status == 200, r.text()
    refocus(page)
    button = card.get_by_role("button", name="Request recount")
    expect(button).to_be_visible()
    with page.expect_request(lambda x: x.method == "POST" and x.url.endswith("/requests")) as posted:
        button.click()
    assert posted.value.post_data_json == {"rec_version": d["rec_version"], "fact_id": q["fact_id"],
                                           "recount_of": q["request_id"]}, posted.value.post_data_json
    assert posted.value.header_value("Idempotency-Key")
    expect(card).to_have_count(0)  # superseded: it moved to Closed (07-19)
    show(page, "Closed")
    old = request_card(page, q).filter(has=page.get_by_text("Replaced by a recount", exact=True))
    expect(old).to_have_count(1)
    expect(summary_value(old, "Status")).to_have_text("Not confirmed · replaced by a recount")
    expect(old.get_by_role("button", name="Request recount")).to_have_count(0)


def test_needs_input_marks_the_fields(visitor):
    """G6: "Loading tasks…" while the first read is held; a confirm the server refuses with 409 needs_input shows its
    message word for word and marks only the listed fields aria-invalid; a confirm in flight shows "Recomputing the
    plan · Ns". The stored answer loses one count in the database after the page showed it complete (test setup)."""
    from tests.web.conftest import run_id

    page = visitor.page
    _, q = request_stock_check(page)
    switch_seat(page, q["to_branch"])

    held = []

    def hold_first(route) -> None:
        if not held and route.request.method == "GET":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/tasks?*", hold_first)
    open_tasks_page(page)
    expect(page.get_by_text("Loading tasks…", exact=True)).to_be_visible()
    deadline = time.monotonic() + 10
    while not held:
        assert time.monotonic() < deadline, "the Tasks read never reached the route"
        page.wait_for_timeout(50)
    held[0].continue_()
    page.unroute("**/api/tasks?*")
    card = request_card(page, q)
    expect(card).to_be_visible()
    expect(page.get_by_text("Loading tasks…")).to_have_count(0)

    send_counts(page, card, q)
    expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    signal = api_request(page, q["request_id"])["answer"]["signal_id"]
    lot = q["lot_ids"][0]
    with db_tx() as conn:
        n = conn.execute(sa.text(
            "UPDATE signals SET fields = jsonb_set(fields, ARRAY['counts', CAST(:l AS text), 'backroom_units'], "
            "'null'::jsonb) WHERE run_id = :r AND signal_id = :s"), {"l": lot, "r": run_id(page), "s": signal}).rowcount
    assert n == 1, n

    with page.expect_response(lambda x: x.url.endswith("/confirm") and x.request.method == "POST") as got:
        card.get_by_role("button", name="Confirm count").click()
    assert got.value.status == 409 and got.value.json()["error"] == "needs_input", got.value.text()
    expect(card.get_by_role("alert").filter(has_text="Fill in every count")).to_have_count(1)
    expect(count_input(card, q, lot, "backroom")).to_have_attribute("aria-invalid", "true")
    expect(count_input(card, q, lot, "shelf")).not_to_have_attribute("aria-invalid", "true")

    with db_tx() as conn:
        conn.execute(sa.text(
            "UPDATE signals SET fields = jsonb_set(fields, ARRAY['counts', CAST(:l AS text), 'backroom_units'], "
            "'0'::jsonb) WHERE run_id = :r AND signal_id = :s"), {"l": lot, "r": run_id(page), "s": signal})
    page.reload()
    card = request_card(page, q)
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    pending = []
    page.route("**/api/signals/*/confirm", lambda route: pending.append(route))
    card.get_by_role("button", name="Confirm count").click()
    deadline = time.monotonic() + 10
    while not pending:
        assert time.monotonic() < deadline, "the confirm never reached the route"
        page.wait_for_timeout(50)
    expect(card.get_by_text(re.compile(r"^Recomputing the plan · \d+s$"))).to_be_visible()
    expect(card.get_by_role("button", name="Confirm count")).to_be_disabled()
    pending[0].continue_()
    expect(page.get_by_test_id("task-status")).to_contain_text("Count confirmed by")


# --- Task 2: dispatches and receipts to record, and the empty states -------------------------------------------------

def post_ok(page: Page, path: str, body: dict) -> dict:
    r = page.request.post(path, headers=key(), data=body)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def prepare_story_transfer(page: Page) -> tuple[dict, dict]:
    """SUS-19 driven to PREPARED through the API, as the people involved do it in the stock decision: the source
    manager takes ownership and sends, the destination accepts, the source approves and prepares the transfer work
    order. Returns (the case detail before, the prepared transfer as GET /api/transfers lists it for the source);
    the page is left signed in as the source manager."""
    card = next((c for c in cards_json(page) if c["key"] == STORY), None)
    assert card is not None, f"{STORY} is not on the signed-in store's Decision board"
    d = case_json(page, card["case_id"])
    assert d["dest"] is not None, f"{STORY}'s recommendation is not a transfer"
    path = f"/api/cases/{quote(d['case_id'])}"
    if session_json(page)["branch"] != d["source"]:
        switch_seat(page, d["source"])
    post_ok(page, f"{path}/take-ownership", {})
    v = case_json(page, d["case_id"])["rec_version"]
    assert post_ok(page, f"{path}/send", {"rec_version": v})["state"] == "PENDING_APPROVAL"
    switch_seat(page, d["dest"])
    post_ok(page, f"{path}/decision", decision_body(page, d["case_id"], "accept", v))
    switch_seat(page, d["source"])
    assert post_ok(page, f"{path}/decision", decision_body(page, d["case_id"], "approve", v))["state"] == "APPROVED"
    assert post_ok(page, f"{path}/prepare", {})["state"] == "PREPARED"
    r = page.request.get("/api/transfers?direction=outgoing&state=planned")
    assert r.status == 200, r.text()
    mine = [t for t in r.json()["items"] if t["case_id"] == d["case_id"]]
    assert len(mine) == 1, r.json()
    return d, mine[0]


def no_posts_while(page: Page, fn) -> None:
    """Run `fn` and check the page sent no POST meanwhile (Tasks records nothing itself, D-52; T-06.1-66)."""
    posts: list[str] = []
    listener = lambda r: posts.append(r.url) if r.method == "POST" else None  # noqa: E731
    page.on("request", listener)
    try:
        fn()
    finally:
        page.remove_listener("request", listener)
    assert posts == [], posts


def test_dispatches_and_receipts_to_record_link_into_execute(visitor):
    """The source store's Tasks lists the prepared transfer as a dispatch to record (the "Dispatches" tile) with Record
    dispatch opening the stock decision's Execute step; once the dispatch is recorded there (here through the API),
    the destination's Tasks lists it as a receipt to record with when it was dispatched and Record receipt, and the
    source's dispatch is under Closed. Tasks posts nothing."""
    page = visitor.page
    d, t = prepare_story_transfer(page)
    s = session_json(page)
    execute = f"#/cases/{quote(d['case_id'])}?step=execute"

    no_posts_while(page, lambda: open_tasks_page(page))
    expect(tile_count(page, "dispatches")).to_have_text("1")
    expect(tile_count(page, "receipts")).to_have_text("0")
    with page.expect_response(lambda r: "tile=dispatches" in r.url):
        page.get_by_test_id("tile-dispatches").click()
    expect(page.get_by_test_id("tile-dispatches")).to_have_attribute("aria-pressed", "true")
    row = page.locator(f"main#content article[data-action-id='{t['action_id']}']")
    expect(page.locator("main#content article")).to_have_count(1)  # the server filtered the list
    expect(row).to_contain_text(t["action_id"])
    expect(row).to_contain_text(f"{t['committed_qty']:,.0f} units of {t['sku']} lot {t['lot_id']} → {t['dest']}")
    if t["run_code"] is not None and t["departs_min"] is not None:
        expect(row.get_by_test_id("task-when")).to_have_text(
            f"{t['run_code']} · departs {hhmm(t['departs_min'])} (planned) · {scenario_date(s, t['run_day'])} · "
            f"Owner: {d['source']} manager")
    else:
        expect(row.get_by_test_id("task-when")).to_have_text(f"Unscheduled · Owner: {d['source']} manager")
    expect(row.get_by_text("Planned", exact=True)).to_be_visible()
    link = row.get_by_role("link", name="Record dispatch", exact=True)
    expect(link).to_have_attribute("href", execute)
    assert left_rule(row) == "4px", "the source acts on its dispatch: the 4px rule"
    expect(row.get_by_test_id("dispatch-blocked")).to_have_count(0)
    expect(page.get_by_role("button", name=re.compile("^Record"))).to_have_count(0)  # links into Execute only

    # The dispatch is recorded in the stock decision (through the API here, as its Execute step does)
    post_ok(page, f"/api/cases/{quote(d['case_id'])}/dispatch",
            {"action_id": t["action_id"], "qty": int(t["committed_qty"]), "lot_id": t["lot_id"], "condition": "as_expected"})
    page.reload()
    expect(page.get_by_role("heading", level=2, name=f"No open tasks for {d['source']}")).to_be_visible()
    show(page, "Closed")
    expect(page.locator(f"main#content article[data-action-id='{t['action_id']}']")).to_be_visible()

    ex = page.request.get(f"/api/cases/{quote(d['case_id'])}/execution").json()
    sent_at = next(m["recorded_at"] for m in ex["movements"] if m["kind"] == "DISPATCH" and m["action_id"] == t["action_id"])
    switch_seat(page, t["dest"])
    no_posts_while(page, lambda: open_tasks_page(page))
    expect(tile_count(page, "receipts")).to_have_text("1")
    expect(tile_count(page, "dispatches")).to_have_text("0")
    row = page.locator(f"main#content article[data-action-id='{t['action_id']}']")
    expect(row).to_contain_text(f"{t['committed_qty']:,.0f} units of {t['sku']} from {d['source']}")
    expect(row.get_by_test_id("task-when")).to_have_text(
        f"Dispatched · Recorded {recorded_time(sent_at, s['time_zone'])} {city(s['time_zone'])} time · "
        f"Owner: {t['dest']} manager")
    expect(row.get_by_role("link", name="Record receipt", exact=True)).to_have_attribute("href", execute)
    assert left_rule(row) == "4px"
    main = page.locator("main#content")
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()


def test_empty_tasks_and_the_hq_line(visitor):
    """UI E4 empty: a store with nothing open reads "No open tasks for {store}" and its body, the six tiles a muted 0
    that still press; Closed reads "No closed tasks yet."; HQ reads the HQ line with Business events. No demo words."""
    page = visitor.page
    s = session_json(page)
    open_tasks_page(page)
    expect(page.get_by_role("heading", level=2, name=f"No open tasks for {s['branch']}", exact=True)).to_be_visible()
    main = page.locator("main#content")
    expect(main).to_contain_text("Count and expiry checks addressed to your store appear here, each with its response "
                                 "deadline. Lot holds you own and advisory tasks appear here too.")
    for tile in ("to_answer", "to_confirm", "lot_holds", "advisory", "dispatches", "receipts"):
        expect(tile_count(page, tile)).to_have_text("0")
        expect(tile_count(page, tile)).to_have_class(re.compile(r"\btext-tx3\b"))  # a 0 tile is muted
    page.get_by_test_id("tile-lot_holds").click()
    expect(page.get_by_test_id("tile-lot_holds")).to_have_attribute("aria-pressed", "true")
    expect(main).to_contain_text("Nothing here right now.")
    page.get_by_test_id("tile-lot_holds").click()  # pressed again, the filter clears
    expect(page.get_by_test_id("tile-lot_holds")).to_have_attribute("aria-pressed", "false")
    show(page, "Closed")
    expect(main.get_by_text("No closed tasks yet.", exact=True)).to_be_visible()
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()

    switch_seat(page, None)
    open_tasks_page(page)
    expect(main).to_contain_text("Tasks go to store managers. Transport reports for HQ are under Business events.")
    expect(main.get_by_role("link", name="Business events")).to_have_attribute("href", "#/events")
    expect(main.get_by_role("heading", level=2)).to_have_count(0)
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()


# --- 07-19 Task 2: printed dates and Mixed dates, combined parts, photos, Can't check, the Simulated chip -------------

PNG = bytes.fromhex(  # a 1x1 PNG: the smallest real image the server's content check accepts
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000154a24f5d"
    "0000000049454e44ae426082")


def workspace_run(page: Page) -> dict:
    from tests.api.conftest import fresh
    from tests.web.conftest import run_id
    return fresh(run_id(page))


def ask_as_source(page: Page, case_id: str, body: dict) -> dict:
    """The stock decision's source manager asks through the API (the Context step's own call); returns the request."""
    d = case_json(page, case_id)
    if session_json(page)["branch"] != d["source"]:
        switch_seat(page, d["source"])
    r = page.request.post(f"/api/cases/{quote(case_id)}/requests", headers=key(),
                          data={"rec_version": case_json(page, case_id)["rec_version"], **body})
    assert r.status == 200, r.text()
    return r.json()


def printed_iso(page: Page, lot_id: str) -> str:
    """The test's own printed date: a day before the lot's recorded expiry, as the API's expiry test chooses it."""
    from api import cases
    from tests.api.conftest import world_lot
    run = workspace_run(page)
    return cases.iso_of(run, world_lot(run, lot_id)["expiry_day"] - 1)


def date_group(card, lot: str):
    return card.get_by_role("group", name=f"Printed date or mixed dates for lot {lot}", exact=True)


def test_a_printed_date_is_answered_and_confirmed_on_tasks(visitor):
    """D-11: an expiry request reads "Check printed expiry date · lot {lot}"; a printed date per lot, the tick "I
    checked the specified lot" and "Checked just now" give "Answered · confirm the printed date"; Confirm printed date
    gives the server's "Printed date confirmed by {store} manager · {t} · {date}"."""
    from tests.api.test_requests import expiry_fixture
    page = visitor.page
    case, fact = expiry_fixture(workspace_run(page))
    q = ask_as_source(page, case["case_id"], {"fact_id": fact["fact_id"]})
    assert q["part"] == "expiry", q
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = page.locator(f"main#content article[data-request-id='{q['request_id']}']")
    expect(card.get_by_role("heading", level=3, name=f"Check printed expiry date · lot {', '.join(q['lot_ids'])}")).to_be_visible()
    card = request_card(page, q)
    lot = q["lot_ids"][0]
    group = date_group(card, lot)
    group.get_by_role("radio", name="Printed date", exact=True).check()
    group.get_by_label("Printed expiry date (business date)").fill(printed_iso(page, lot))
    expect(group).to_contain_text("As printed on the pack, in the dataset's calendar")
    checked_just_now(card)
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text(f"Tick the box to confirm you checked lot {lot}.", exact=True)).to_be_visible()
    card.get_by_label("I checked the specified lot").check()
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text("Answered · confirm the printed date", exact=True)).to_be_visible()
    expect(card.get_by_role("checkbox", name="I checked the shelf and the backroom for this lot")).to_have_count(0)
    card.get_by_role("button", name="Confirm printed date").click()
    expect(page.get_by_test_id("task-status")).to_contain_text(f"Printed date confirmed by {q['to_branch']} manager · ")
    assert api_request(page, q["request_id"])["state"] == "CONFIRMED"


def test_mixed_dates_send_and_hold_the_lot(visitor):
    """D-11, D-13: "Mixed dates in this lot" turns the button into "Send and hold lot", whose confirm step names the lot
    and "Separate stock and verify before moving" ("Go back" dismisses); sent, the request closes and the lot hold is
    a Tasks row the store owns."""
    from tests.api.test_requests import expiry_fixture
    page = visitor.page
    case, fact = expiry_fixture(workspace_run(page))
    q = ask_as_source(page, case["case_id"], {"fact_id": fact["fact_id"]})
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    lot = q["lot_ids"][0]
    date_group(card, lot).get_by_role("radio", name="Mixed dates in this lot", exact=True).check()
    card.get_by_label("I checked the specified lot").check()
    checked_just_now(card)
    expect(card.get_by_role("button", name="Send answer")).to_have_count(0)
    card.get_by_role("button", name="Send and hold lot").click()
    step = card.get_by_role("group", name="Send and hold lot")
    expect(step).to_contain_text(f"This places a lot hold on lot {lot}: Separate stock and verify before moving.")
    step.get_by_role("button", name="Go back").click()
    expect(step).to_have_count(0)
    expect(card.get_by_role("button", name="Send and hold lot")).to_be_focused()
    card.get_by_role("button", name="Send and hold lot").click()
    card.get_by_role("group", name="Send and hold lot").get_by_role("button", name="Send and hold lot").click()
    expect(tile_count(page, "lot_holds")).to_have_text("1")
    assert api_request(page, q["request_id"])["state"] == "HELD"
    expect(page.locator("main#content article").filter(
        has=page.get_by_role("heading", level=3, name=f"Lot hold · lot {lot}", exact=True))).to_be_visible()
    assert not re.search(r"\bsplit\b", page.locator("main#content").inner_text(), re.IGNORECASE)


def test_a_combined_request_is_one_row_with_two_lanes(visitor):
    """D-11, D-22 (Edge UI-05 adjacency): a count and an expiry asked together are one row with "Count" and "Expiry"
    lanes, each sent with its own button; confirming the count leaves the expiry lane open and the row in Open."""
    from tests.api.test_requests import pair_fixture
    page = visitor.page
    case, count, expiry = pair_fixture(workspace_run(page))
    q = ask_as_source(page, case["case_id"], {"fact_ids": [count["fact_id"], expiry["fact_id"]]})
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    expect(page.locator("main#content article[data-request-id]")).to_have_count(1)
    card = request_card(page, q)
    expect(card.get_by_role("heading", level=3)).to_contain_text("Count stock + Check printed expiry date · lot")
    lane_count = card.get_by_role("region", name="Count")
    lane_expiry = card.get_by_role("region", name="Expiry")
    expect(lane_expiry.get_by_role("button", name="Send expiry answer")).to_be_visible()
    for lot in q["lot_ids"]:
        for field in ("shelf", "backroom"):
            count_input(lane_count, q, lot, field).fill("0")
    lane_count.get_by_role("group", name=WHEN).get_by_role("radio", name=JUST_NOW, exact=True).check()
    lane_count.get_by_role("button", name="Send count answer").click()
    expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
    lane_count.get_by_label("I checked the shelf and the backroom for this lot").check()
    lane_count.get_by_role("button", name="Confirm count").click()
    expect(page.get_by_test_id("task-status")).to_contain_text("Count confirmed by")
    expect(card).to_be_visible()  # the expiry lane is still open: the row stays in Open
    expect(lane_expiry.get_by_role("button", name="Send expiry answer")).to_be_visible()
    expect(tile_count(page, "to_answer")).to_have_text("1")


def test_a_photo_alone_cant_check_and_the_simulated_chip(visitor, request):
    """VER-04, D-07, D-18: a photo with no counts reads "Photo received · count missing" and its photo is labelled
    "Photo · not a count"; Can't check goes through its confirm step ("Keep request open" dismisses) and moves the
    request to Closed with "Can't check", the fact's exposure line still there; a preloaded request carries
    "Simulated request". At 375 px the bar text never overlaps the bar (E10 backstop)."""
    from tests.web.conftest import run_id
    page = visitor.page
    _, q = request_stock_check(page)
    with db_tx() as conn:  # test setup: the request reads as preloaded (07-CONTEXT D-18)
        conn.execute(sa.text("UPDATE evidence_requests SET provenance = 'simulated' WHERE run_id = :r AND request_id = :q"),
                     {"r": run_id(page), "q": q["request_id"]})
    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    page.set_viewport_size({"width": 375, "height": 812})  # the phone top bar hides the nav: open Tasks first
    expect(page.get_by_role("button", name="Open menu", exact=True)).to_be_visible()
    card = page.locator(f"main#content article[data-request-id='{q['request_id']}']")
    expect(card.get_by_text("Simulated request", exact=True)).to_be_visible()
    bar = card.get_by_test_id("deadline-bar")
    geometry = bar.evaluate("""element => ({
        viewport: window.innerWidth,
        box: element.querySelector("span[aria-hidden='true']").getBoundingClientRect().toJSON(),
        text: element.querySelector('[data-testid="deadline-text"]').getBoundingClientRect().toJSON()
    })""")
    request.node.user_properties.append(("deadline_geometry", json.dumps(geometry)))
    assert geometry["viewport"] == 375
    box, text = geometry["box"], geometry["text"]
    assert box and text and (text["x"] >= box["x"] + box["width"] or text["y"] >= box["y"] + box["height"]), (box, text)
    page.set_viewport_size({"width": 1280, "height": 800})
    card = request_card(page, q)
    card.locator(f"input[id='{q['request_id']}-file']").set_input_files(
        {"name": "shelf-photo.png", "mimeType": "image/png", "buffer": PNG})
    expect(card).to_contain_text("shelf-photo.png")
    expect(card).to_contain_text("A photo alone doesn't confirm a count. Enter both numbers to complete it.")
    checked_just_now(card)
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text("Photo received · count missing", exact=True)).to_be_visible()
    expect(card.get_by_text("Photo · not a count", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)

    card.get_by_role("button", name="Can't check").click()
    step = card.get_by_role("group", name="Can't check")
    expect(step).to_contain_text("Close this request without an answer? The fact stays unverified")
    step.get_by_role("button", name="Keep request open").click()
    expect(step).to_have_count(0)
    card.get_by_role("button", name="Can't check").click()
    card.get_by_role("group", name="Can't check").get_by_role("button", name="Can't check").click()
    expect(page.get_by_test_id("task-status")).to_contain_text("Can't check")
    assert api_request(page, q["request_id"])["state"] == "CANT_CHECK"
    show(page, "Closed")
    closed = request_card(page, q)
    expect(closed.get_by_text("Can't check", exact=True).first).to_be_visible()
    expect(closed).to_contain_text("Unverified · estimated decision exposure")


# --- 07-19 Task 3: lot holds and advisory tasks; HQ; empty states ------------------------------------------------------

def test_a_lot_hold_is_inspected_and_released_on_tasks(visitor):
    """D-13: the lot's manager sees "Lot hold · lot {lot}" under Lot holds; "Inspect and release" goes through its
    confirm step ("Keep hold" dismisses; the server's inspection note is required); released, it moves to Closed with
    "Hold released by {store} manager · {t}". Another store sees no hold to release."""
    page = visitor.page
    d, q = request_stock_check(page)  # only to find the lot the counted branch owns
    lot, owner = q["lot_ids"][0], q["to_branch"]
    switch_seat(page, owner)
    r = page.request.post(f"/api/lots/{lot}/holds", headers=key(), data={"reason": "Crushed crates in the chiller"})
    assert r.status == 200, r.text()
    hold = r.json()
    open_tasks_page(page)
    expect(tile_count(page, "lot_holds")).to_have_text("1")
    row = page.locator(f"main#content article[data-hold-id='{hold['hold_id']}']")
    expect(row.get_by_role("heading", level=3, name=f"Lot hold · lot {lot}")).to_be_visible()
    expect(row).to_contain_text(f"Crushed crates in the chiller · Owner: {owner} manager")
    row.get_by_role("button", name="Inspect and release").click()  # the row's primary opens it
    expect(row).to_contain_text(hold["placed_label"])
    row.get_by_role("button", name="Inspect and release").click()
    step = row.get_by_role("group", name="Inspect and release")
    expect(step).to_contain_text(f"Release the hold on lot {lot}?")
    expect(step.get_by_role("button", name="Release hold")).to_be_disabled()
    step.get_by_role("button", name="Keep hold").click()
    row.get_by_role("button", name="Inspect and release").click()
    step = row.get_by_role("group", name="Inspect and release")
    step.get_by_label("What did you find?").fill("Inspected, all sound")
    step.get_by_role("button", name="Release hold").click()
    expect(page.get_by_test_id("task-status")).to_contain_text(f"Hold released by {owner} manager · ")
    expect(tile_count(page, "lot_holds")).to_have_text("0")
    show(page, "Closed")
    expect(page.locator(f"main#content article[data-hold-id='{hold['hold_id']}']")).to_contain_text(
        f"Hold released by {owner} manager")

    switch_seat(page, d["source"] if d["source"] != owner else d["dest"])
    open_tasks_page(page)
    expect(page.get_by_role("button", name="Inspect and release")).to_have_count(0)


def test_an_advisory_task_is_marked_done(visitor):
    """D-13: a confirmed display report is "Advisory task · {product}" with "Mark done" and "Closes this task for
    {store}. A lot hold, if any, stays."; done, it is under Closed with "Done by {store} manager · {t}" and the advisory
    tile is 0."""
    page = visitor.page
    _, q = request_stock_check(page)
    lot, owner = q["lot_ids"][0], q["to_branch"]
    switch_seat(page, owner)
    r = page.request.post("/api/signals", headers=key(), multipart={
        "kind": "CONDITION", "fields": json.dumps({"lot_id": lot, "condition": "DISPLAY"}), "text": "Shelf tag missing"})
    assert r.status == 200, r.text()
    rep = r.json()
    r = page.request.post(f"/api/signals/{rep['signal_id']}/confirm", headers=key(), data={"revision": rep["revision"]})
    assert r.status == 200, r.text()
    open_tasks_page(page)
    expect(tile_count(page, "advisory")).to_have_text("1")
    row = page.locator(f"main#content article[data-signal-id='{rep['signal_id']}']")
    expect(row.get_by_role("heading", level=3)).to_contain_text("Advisory task · ")
    expect(row).to_contain_text("Shelf tag missing")
    expect(row).to_contain_text("Advisory · no hold, no commitment block")
    expect(row).to_contain_text(f"Closes this task for {owner}. A lot hold, if any, stays.")
    row.get_by_role("button", name="Mark done").click()
    expect(page.get_by_test_id("task-status")).to_contain_text(f"Done by {owner} manager · ")
    expect(tile_count(page, "advisory")).to_have_text("0")
    show(page, "Closed")
    expect(page.locator(f"main#content article[data-signal-id='{rep['signal_id']}']")).to_contain_text(
        f"Done by {owner} manager")
