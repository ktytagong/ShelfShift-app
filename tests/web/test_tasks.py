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
from tests.web.conftest import case_json, cards_json, db_tx, open_case, session_json, switch_seat

pytestmark = pytest.mark.web

COUNT_KINDS = ("PHANTOM_STOCK", "STALE_COUNT")  # api/signals.py COUNT_KINDS: the kinds a count settles
STORY = "SUS-19"  # the story decision's key on the release data
EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it."  # api/signals.py EARLIER_DAY
FINISHED = "Saved to history. This stock decision is closed, so this count cannot be confirmed."  # the user's words
DEMO_WORDS = re.compile(r"\b(demo|seat|sandbox|next day|start here)\b", re.IGNORECASE)  # D-03: none on Tasks


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


ASK = re.compile(r"^(Ask this branch for a count|Request stock check)$")  # the Phase 5 label and the D-61 one
ASKED = re.compile(r"(Evidence requested from|Stock check requested from)")


def ask_for_count(page: Page) -> tuple[dict, dict]:
    """As the source manager on SUS-19, press the stock check button on its plan-changing count fact (the case page);
    returns the case detail before the ask and the request the API made."""
    d = open_case(page, STORY)
    fact = count_fact(d)
    row = fact_row(page, fact["fact_id"])
    row.get_by_role("button", name=ASK).click()
    expect(row).to_contain_text(ASKED)
    q = request_for(page, d, fact["fact_id"])
    expect(row).to_contain_text(q["to_branch"])
    expect(row.get_by_role("button", name=ASK)).to_have_count(0)
    return d, q


def request_card(page: Page, q: dict):
    """The Tasks card for this request, found by its title "{key} · lot {lots}" (an h3 inside the Stock checks
    section)."""
    title = f"{q['case_key']} · lot {', '.join(q['lot_ids'])}"
    return page.locator("main#content article").filter(
        has=page.get_by_role("heading", level=3, name=title, exact=True))


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
    with page.expect_response(lambda r: r.url.endswith("/api/requests") and r.request.method == "GET"):
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


def send_counts(page: Page, card, q: dict, value: str = "0") -> None:
    for lot in q["lot_ids"]:
        for field in ("shelf", "backroom"):
            count_input(card, q, lot, field).fill(value)
    card.get_by_role("button", name="Send answer").click()


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

def test_the_count_loop_runs_on_tasks(visitor):
    """Tracer: the addressed store answers the stock check on Tasks and confirms it; the stock decision re-plans (a new
    rec_version), approving and holding nothing. The row reads the D-58 lines; one link label everywhere."""
    page = visitor.page
    d, q = request_stock_check(page)
    before, round_before = d["versions"]["rec_version"], d["approvals"]["round"]
    when = assigned_at(page, q)

    switch_seat(page, q["to_branch"])
    s = session_json(page)
    assert s["time_zone"] == config.SETTINGS["caps"]["time_zone"] == "Asia/Bangkok", s["time_zone"]
    cdp, pinned = pin_browser_zone_elsewhere(page, s["time_zone"])  # kept for the rest of the test
    open_tasks_page(page)
    checks = page.get_by_test_id("tasks-stock-checks")
    expect(checks.get_by_role("heading", level=2, name="Stock checks", exact=True)).to_be_visible()
    expect(checks.get_by_test_id("task-count")).to_have_text("1")  # a fresh workspace has no other stock check
    card = request_card(page, q)
    expect(card).to_be_visible()
    expect(card.get_by_text("Open", exact=True)).to_be_visible()
    expect(card).to_contain_text(q["ask_text"])
    # D-58: who and when (recorded, Bangkok, with the browser pinned elsewhere), then the planned due with its date
    expect(card.get_by_test_id("assigned-line")).to_have_text(
        f"Stock check assigned to {q['to_branch']} · Recorded {recorded_time(when, s['time_zone'])} "
        f"{city(s['time_zone'])} time.")
    expect(card.get_by_test_id("due-line")).to_have_text(
        f"Respond by {hhmm(q['due_min'])} (planned) · {scenario_date(s, q['due_day'])}")
    assert left_rule(card) == "4px", "the store acts on this stock check: the 4px rule"
    expect(card.get_by_role("link", name="View stock decision")).to_have_attribute("href", f"#/cases/{quote(q['case_id'])}")
    for lot in q["lot_ids"]:  # the lot label is the legend over its Shelf and Backroom inputs (05-UI-REVIEW finding 6)
        group = card.get_by_role("group", name=f"Lot {lot}", exact=True)
        expect(group).to_be_visible()
        for field, label in (("shelf", "Shelf"), ("backroom", "Backroom")):
            box = group.get_by_label(label, exact=True)
            expect(box).to_have_value("")
            expect(box).to_have_attribute("placeholder", "Not checked")
            box.fill("0")
    card.get_by_role("button", name="Send answer").click()
    expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()

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
    expect(card).to_contain_text("Confirming the count approves nothing. It recomputes the stock decision")
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    confirm.click()
    expect(card).to_contain_text(f"Count confirmed by {q['to_branch']} manager")
    expect(summary_value(card, "Status")).to_have_text("Confirmed")
    assert left_rule(card) != "4px", "nothing left to do on a confirmed stock check"

    # One link label everywhere (D-39); no demo words (D-03)
    main = page.locator("main#content")
    expect(main.get_by_role("link", name="View linked decision")).to_have_count(0)
    expect(main.get_by_role("link", name=re.compile(r"^Open "))).to_have_count(0)
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()

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
    card.get_by_role("button", name="Send answer").click()
    expect(card).to_contain_text("Shelf: not checked")  # the stored answer is back on screen

    stored = api_request(page, q["request_id"])
    assert stored["state"] == "OPEN" and stored["partial_note"], stored
    expect(card.get_by_text(stored["partial_note"], exact=True)).to_be_visible()  # the server's note, verbatim
    expect(card.get_by_text("Open", exact=True)).to_be_visible()
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
    card.get_by_label("Reply").fill("Half counted")

    refocus(page)  # a background refetch never wipes what was typed
    expect(shelf).to_have_value("7")
    expect(card.get_by_label("Reply")).to_have_value("Half counted")
    expect(card).not_to_contain_text("The answer changed since you looked at it")

    # The answer changes behind the page (the same account, through the API): the kept draft stays and says so.
    counts = {x: {"shelf_units": None, "backroom_units": None} for x in q["lot_ids"]}
    r = page.request.post(f"/api/requests/{q['request_id']}/answer", headers=key(),
                          multipart={"text": "Answered elsewhere", "counts": json.dumps(counts)})
    assert r.status == 200, r.text()
    refocus(page)
    expect(card).to_contain_text("Answered elsewhere")
    expect(card).to_contain_text("The answer changed since you looked at it")
    expect(shelf).to_have_value("7")
    expect(card.get_by_label("Reply")).to_have_value("Half counted")


def test_an_expired_request_takes_a_late_answer(visitor):
    """A late answer is kept (spec §7 rev 6.7; A-11): the next scenario day expires the request; the addressed store
    still answers it, and the complete late answer reads back with "Answered after deadline" as a warn chip on its own
    line (UI-SPEC open question 8), then confirms."""
    page = visitor.page
    _, q = request_stock_check(page)
    assert q["due_day"] <= session_json(page)["day"], "the request is due by the end of the day, so the next day expires it"
    next_day(page)

    switch_seat(page, q["to_branch"])
    s = session_json(page)
    open_tasks_page(page)
    card = request_card(page, q)
    assert api_request(page, q["request_id"])["state"] == "EXPIRED"
    expect(card.get_by_text("Expired", exact=True)).to_be_visible()
    expect(card.get_by_test_id("expired-line")).to_have_text(
        f"This stock check expired at {hhmm(q['due_min'])} (planned) · {scenario_date(s, q['due_day'])}. "
        "The stock decision shows the fact as unverified.")
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)
    send_counts(page, card, q)
    expect(summary_value(card, "Status")).to_have_text("Awaiting confirmation")

    late = api_request(page, q["request_id"])
    assert late["state"] == "ANSWERED" and late["answer"]["day"] > late["due_day"], late
    note = card.get_by_test_id("response-note")
    expect(note).to_have_text("Answered after deadline")
    status_box = summary_value(card, "Status").evaluate("e => e.getBoundingClientRect().bottom")
    assert note.evaluate("e => e.getBoundingClientRect().top") >= status_box, "the chip sits on its own line"
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    card.get_by_role("button", name="Confirm count").click()
    expect(summary_value(card, "Status")).to_have_text("Confirmed")
    expect(note).to_have_text("Answered after deadline")  # it stays (07-CONTEXT D-08)
    assert api_request(page, q["request_id"])["state"] == "CONFIRMED"


def test_a_late_answer_on_a_finished_case_offers_no_confirm_and_no_form(visitor):
    """05-REVIEW WR-10, A-26: once the stock decision is finished, an expired request offers no late-answer form, and a
    late answer still sent through the API shows no Confirm count and says why, in the user's words."""
    page = visitor.page
    asked, q = request_stock_check(page)
    next_day(page)
    r = page.request.post(f"/api/cases/{quote(asked['case_id'])}/reject", headers=key(),
                          data={"reason": "Checked with the branch; not moving this lot"})
    assert r.status == 200, r.text()

    switch_seat(page, q["to_branch"])
    open_tasks_page(page)
    card = request_card(page, q)
    seen = api_request(page, q["request_id"])
    assert (seen["state"], seen["answerable"], seen["confirmable"]) == ("EXPIRED", False, False), seen
    expect(card.get_by_test_id("expired-line")).to_be_visible()
    expect(card.locator("input")).to_have_count(0)
    expect(card.get_by_role("button", name="Send answer")).to_have_count(0)
    expect(card.get_by_test_id("not-confirmable")).to_have_count(0)
    expect(card.get_by_test_id("lot-left")).to_have_count(0)
    assert left_rule(card) != "4px", "nothing to do on a stock check the server will not take"

    counts = {x: {"shelf_units": 0, "backroom_units": 0} for x in q["lot_ids"]}
    r = page.request.post(f"/api/requests/{q['request_id']}/answer", headers=key(),
                          multipart={"text": "", "counts": json.dumps(counts)})
    assert r.status == 200, r.text()
    refocus(page)
    kept = api_request(page, q["request_id"])
    assert (kept["state"], kept["answerable"], kept["confirmable"]) == ("ANSWERED", False, False), kept
    expect(card.get_by_test_id("not-confirmable")).to_have_text(FINISHED)
    expect(card.get_by_test_id("response-note")).to_have_text("Answered after deadline")
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)
    expect(card.locator("input")).to_have_count(0)
    expect(card.get_by_test_id("earlier-day")).to_have_count(0)


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
    stale = api_request(page, q["request_id"])
    assert (stale["state"], stale["confirmable"], stale["answerable"]) == ("ANSWERED", False, True), stale
    expect(card.get_by_test_id("earlier-day")).to_have_text(EARLIER_DAY)
    expect(card.get_by_role("button", name="Confirm count")).to_have_count(0)

    send_counts(page, card, q)
    expect(card.get_by_test_id("earlier-day")).to_have_count(0)
    card.get_by_label("I checked the shelf and the backroom for this lot").check()
    card.get_by_role("button", name="Confirm count").click()
    expect(summary_value(card, "Status")).to_have_text("Confirmed")


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
    new_card = page.locator(f"main#content article[data-request-id='{fresh['request_id']}']")
    for lot in q["lot_ids"]:
        expect(count_input(new_card, fresh, lot, "shelf")).to_have_value("")
    old = page.locator(f"main#content article[data-request-id='{q['request_id']}']")
    expect(old.get_by_text("Replaced by a recount", exact=True)).to_be_visible()
    expect(summary_value(old, "Status")).to_have_text("Not confirmed · replaced by a recount")
    expect(old.get_by_role("link", name="View stock decision")).to_be_visible()
    expect(old.get_by_role("button", name="Confirm count")).to_have_count(0)
    expect(old.locator("input")).to_have_count(0)
    expect(page.get_by_test_id("tasks-stock-checks").get_by_test_id("task-count")).to_have_text("2")


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
                          multipart={"text": "", "counts": json.dumps(counts)})
    assert r.status == 200, r.text()
    refocus(page)
    button = card.get_by_role("button", name="Request recount")
    expect(button).to_be_visible()
    with page.expect_request(lambda x: x.method == "POST" and x.url.endswith("/requests")) as posted:
        button.click()
    assert posted.value.post_data_json == {"rec_version": d["rec_version"], "fact_id": q["fact_id"],
                                           "recount_of": q["request_id"]}, posted.value.post_data_json
    assert posted.value.header_value("Idempotency-Key")
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

    page.route("**/api/requests", hold_first)
    open_tasks_page(page)
    expect(page.get_by_text("Loading tasks…", exact=True)).to_be_visible()
    deadline = time.monotonic() + 10
    while not held:
        assert time.monotonic() < deadline, "the Tasks read never reached the route"
        page.wait_for_timeout(50)
    held[0].continue_()
    page.unroute("**/api/requests")
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
    expect(summary_value(card, "Status")).to_have_text("Confirmed")


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
    post_ok(page, f"{path}/decision", {"rec_version": v, "decision": "accept"})
    switch_seat(page, d["source"])
    assert post_ok(page, f"{path}/decision", {"rec_version": v, "decision": "approve"})["state"] == "APPROVED"
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
    """The source store's Tasks lists the prepared transfer under "Dispatches to record" with Record dispatch opening
    the stock decision's Execute step; once the dispatch is recorded there (here through the API), the destination's
    Tasks lists it under "Receipts to record" with when it was dispatched and Record receipt. Tasks posts nothing."""
    page = visitor.page
    d, t = prepare_story_transfer(page)
    s = session_json(page)
    execute = f"#/cases/{quote(d['case_id'])}?step=execute"

    no_posts_while(page, lambda: open_tasks_page(page))
    dispatches = page.get_by_test_id("tasks-dispatches")
    expect(dispatches.get_by_role("heading", level=2, name="Dispatches to record", exact=True)).to_be_visible()
    expect(dispatches.get_by_test_id("task-count")).to_have_text("1")
    row = dispatches.locator(f"li[data-action-id='{t['action_id']}']")
    expect(row).to_contain_text(t["action_id"])
    expect(row).to_contain_text(f"{t['committed_qty']:,.0f} units of {t['sku']} lot {t['lot_id']} → {t['dest']}")
    if t["run_code"] is not None and t["departs_min"] is not None:
        expect(row.get_by_test_id("task-when")).to_have_text(
            f"{t['run_code']} · departs {hhmm(t['departs_min'])} (planned) · {scenario_date(s, t['run_day'])}")
    else:
        expect(row.get_by_test_id("task-when")).to_have_text("Unscheduled")
    expect(row.get_by_text("Planned", exact=True)).to_be_visible()
    link = row.get_by_role("link", name="Record dispatch", exact=True)
    expect(link).to_have_attribute("href", execute)
    assert left_rule(row) == "4px", "the source acts on its dispatch: the 4px rule"
    receipts = page.get_by_test_id("tasks-receipts")
    expect(receipts.get_by_test_id("task-count")).to_have_text("0")
    expect(receipts).to_contain_text("None waiting.")
    expect(page.get_by_role("button", name=re.compile("^Record"))).to_have_count(0)  # links into Execute only

    # The dispatch is recorded in the stock decision (through the API here, as its Execute step does)
    post_ok(page, f"/api/cases/{quote(d['case_id'])}/dispatch",
            {"action_id": t["action_id"], "qty": int(t["committed_qty"]), "lot_id": t["lot_id"], "condition": "as_expected"})
    page.reload()
    expect(page.get_by_role("heading", level=2, name=f"No tasks for {d['source']}")).to_be_visible()
    expect(page.locator("main#content")).to_contain_text(
        f"Stock checks, dispatches and receipts for {d['source']} appear here.")

    ex = page.request.get(f"/api/cases/{quote(d['case_id'])}/execution").json()
    sent_at = next(m["recorded_at"] for m in ex["movements"] if m["kind"] == "DISPATCH" and m["action_id"] == t["action_id"])
    switch_seat(page, t["dest"])
    no_posts_while(page, lambda: open_tasks_page(page))
    receipts = page.get_by_test_id("tasks-receipts")
    expect(receipts.get_by_role("heading", level=2, name="Receipts to record", exact=True)).to_be_visible()
    expect(receipts.get_by_test_id("task-count")).to_have_text("1")
    row = receipts.locator(f"li[data-action-id='{t['action_id']}']")
    expect(row).to_contain_text(f"{t['committed_qty']:,.0f} units of {t['sku']} from {d['source']}")
    expect(row.get_by_test_id("task-when")).to_have_text(
        f"Dispatched · Recorded {recorded_time(sent_at, s['time_zone'])} {city(s['time_zone'])} time")
    expect(row.get_by_role("link", name="Record receipt", exact=True)).to_have_attribute("href", execute)
    assert left_rule(row) == "4px"
    expect(page.get_by_test_id("tasks-dispatches").get_by_test_id("task-count")).to_have_text("0")
    main = page.locator("main#content")
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()


def test_empty_tasks_and_the_hq_line(visitor):
    """A store with nothing waiting reads "No tasks for {store}" and its body; HQ reads the HQ line. No demo words."""
    page = visitor.page
    s = session_json(page)
    open_tasks_page(page)
    expect(page.get_by_role("heading", level=2, name=f"No tasks for {s['branch']}", exact=True)).to_be_visible()
    main = page.locator("main#content")
    expect(main).to_contain_text(f"Stock checks, dispatches and receipts for {s['branch']} appear here.")
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()

    switch_seat(page, None)
    open_tasks_page(page)
    expect(main).to_contain_text("Tasks go to store managers. HQ sees each branch's work on the Decision board.")
    expect(main.get_by_role("heading", level=2)).to_have_count(0)
    assert not DEMO_WORDS.search(main.inner_text()), main.inner_text()
