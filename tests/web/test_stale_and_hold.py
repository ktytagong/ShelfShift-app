"""PROD-08 / D-57: the protections the approval workflow rests on, on the stock decision page (06.1-UI-SPEC "Booking
holds", "Interaction states and feedback"; 05-CONTEXT D-09; contract stale_version, hold_expired and
Idempotency-Key). Rewritten for the 06.1 route (06.1-26).

- A capacity hold shows as a fixed time, "Capacity held until HH:MM Bangkok time.", never minutes left.
- The decision re-reads on focus and once at the hold's end, timed from the server's clock: an expired hold reads
  "Capacity hold expired. Check availability and book again." with Check availability, which holds again in a new
  round, and leaves every other workspace alone. It never shows as booked and never renews by itself.
- A stale page's write gets its refusal with "Reload the stock decision" and is never resent.

The hold's end is never waited on: the visitor's own reservations move into the past in the database (by its run_id
and case_id), and the timer test drives the page's clock with Playwright. Every expectation is read from the API."""
from __future__ import annotations

import datetime
import json
import re
import time
import urllib.parse
import zoneinfo

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_id_by_tag, case_json, db_tx, expire_holds, run_id, session_json, switch_seat, take_ownership
from tests.web.test_case import STORY, decision_href, panel, step_button
from tests.web.test_tasks import count_fact, key, quote

pytestmark = pytest.mark.web

HOLD_EXPIRED = "Capacity hold expired. Check availability and book again."
STALE = "This recommendation was replaced by a newer version. Reload the stock decision."
CANCELLED = "Approval cancelled: the stock decision got a new version. Send for approval again."
MINUTES = re.compile(r"\d+ min\b")


def main(page: Page):
    return page.locator("main#content")


def slot(page: Page):
    return main(page).get_by_test_id("primary-slot")


def open_at(page: Page, case_id: str, step: str | None = None) -> dict:
    page.goto(decision_href(case_id, step))
    d = case_json(page, case_id)
    expect(main(page).get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    return d


def wait_for(page: Page, case_id: str, state: str, timeout_s: float = 20) -> dict:
    for _ in range(int(timeout_s * 10)):
        d = case_json(page, case_id)
        if d["state"] == state:
            return d
        page.wait_for_timeout(100)
    raise AssertionError(f"{case_id} never reached {state}: {case_json(page, case_id)['state']}")


def held_line(page: Page, d: dict) -> str:
    """"Capacity held until 14:35 Bangkok time." from the detail's hold in the session's zone, computed here."""
    zone = session_json(page)["time_zone"]
    until = datetime.datetime.fromisoformat(d["hold"]["until"]).astimezone(zoneinfo.ZoneInfo(zone))
    return f"Capacity held until {until:%H:%M} {zone.split('/')[-1].replace('_', ' ')} time."


def held_reservations(page: Page, case_id: str) -> int:
    import sqlalchemy as sa

    with db_tx() as conn:
        return conn.execute(sa.text("SELECT count(*) FROM reservations WHERE run_id = :r AND case_id = :c AND state = 'HELD'"),
                            {"r": run_id(page), "c": case_id}).scalar_one()


def owned_story(page: Page) -> str:
    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    return cid


def send_on_page(page: Page, case_id: str) -> dict:
    open_at(page, case_id)
    slot(page).get_by_role("button", name="Send for approval", exact=True).click()
    expect(step_button(page, 5, "Approve")).to_have_attribute("aria-current", "step")
    return wait_for(page, case_id, "PENDING_APPROVAL")


def case_requests(page: Page, case_id: str, method: str, route: str | None = None) -> list:
    """Start recording the page's own requests to /api/cases/{id} (route None) or /api/cases/{id}/{route}."""
    path = "/api/cases/" + urllib.parse.quote(case_id, safe="") + ("" if route is None else f"/{route}")
    seen: list = []

    def on(request) -> None:
        if request.method == method and urllib.parse.urlsplit(request.url).path == path:
            seen.append(request)

    page.on("request", on)
    return seen


def flush(page: Page) -> None:
    page.evaluate("0")  # a round trip, so every request event the page fired has reached the test


# --- the booking hold (D-57) -------------------------------------------------------------------------------------------

def test_an_expired_hold_reads_expired_on_focus_and_check_availability_holds_again(open_visitor):
    a, b = open_visitor(), open_visitor()

    # B sends the story in its own workspace: its hold must outlive A's expiry.
    b_cid = owned_story(b.page)
    b_sent = send_on_page(b.page, b_cid)

    page = a.page
    cid = owned_story(page)
    sent = send_on_page(page, cid)
    line = panel(page).get_by_test_id("booking-line")
    expect(line).to_have_text(held_line(page, sent))
    expect(main(page).get_by_text(MINUTES)).to_have_count(0)  # a fixed time, never minutes left
    assert run_id(page) != run_id(b.page), "two visitors share one workspace"

    # The hold ends in the database (A's run_id and this case only); a focus event re-reads the decision.
    assert expire_holds(run_id(page), cid) >= 1, "A's story had no HELD reservation"
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(main(page).get_by_test_id("booking-line")).to_have_text(HOLD_EXPIRED)
    expect(step_button(page, 4, "Compare")).to_have_attribute("aria-current", "step")
    expired = case_json(page, cid)
    assert expired["state"] == "OPEN" and "hold_expired" in expired["blocked_reasons"], (expired["state"], expired["blocked_reasons"])
    expect(main(page).get_by_text("Booking confirmed.")).to_have_count(0)  # an expired hold never reads as booked
    check = slot(page).get_by_role("button", name="Check availability", exact=True)
    expect(check).to_be_visible()
    expect(slot(page).get_by_role("button", name="Send for approval")).to_have_count(0)

    # Nothing renews by itself: another focus re-reads and the decision stays expired, with no send.
    sends = case_requests(page, cid, "POST", "send")
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    page.wait_for_timeout(500)
    flush(page)
    assert sends == [] and case_json(page, cid)["state"] == "OPEN"

    # Check availability holds again in a new round, sent once.
    check.click()
    expect(step_button(page, 5, "Approve")).to_have_attribute("aria-current", "step")
    again = wait_for(page, cid, "PENDING_APPROVAL")
    flush(page)
    assert len(sends) == 1, [r.url for r in sends]
    assert again["approvals"]["round"] == sent["approvals"]["round"] + 1, (sent["approvals"]["round"], again["approvals"]["round"])
    expect(panel(page).get_by_test_id("booking-line")).to_have_text(held_line(page, again))

    # B's workspace is untouched: still PENDING_APPROVAL with its hold, the same round, its reservations still HELD.
    b_now = case_json(b.page, b_cid)
    assert b_now["state"] == "PENDING_APPROVAL", b_now["state"]
    assert b_now["hold"]["until"] == b_sent["hold"]["until"] and b_now["approvals"]["round"] == b_sent["approvals"]["round"]
    assert held_reservations(b.page, b_cid) > 0


def test_the_page_rereads_once_at_the_holds_end_from_the_server_clock(visitor):
    page = visitor.page
    cid = owned_story(page)
    sent = send_on_page(page, cid)
    until, server_now = sent["hold"]["until"], sent["hold"]["server_now"]
    assert until and server_now, sent["hold"]

    # The page's clock is Playwright's from here, and set far from the server's: the timer must not read it.
    page.clock.install(time=datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
    page.reload()
    expect(panel(page).get_by_test_id("booking-line")).to_have_text(held_line(page, sent))
    assert expire_holds(run_id(page), cid) >= 1
    reads = case_requests(page, cid, "GET")
    after = page.evaluate("Date.now()")
    at_end = int((datetime.datetime.fromisoformat(until) - datetime.datetime.fromisoformat(server_now)).total_seconds() * 1000)
    assert at_end > 0, (until, server_now)

    # Just before the end (as the server counts it) nothing is fetched; just after it, one read shows the expiry.
    page.clock.run_for(max(at_end - 2_000, 0))
    flush(page)
    assert reads == [], [r.url for r in reads]
    page.clock.run_for(3_000)
    expect(main(page).get_by_test_id("booking-line")).to_have_text(HOLD_EXPIRED)
    page.clock.run_for(60_000)  # a minute more: no countdown ticks, no further read
    flush(page)
    assert len(reads) == 1, [r.url for r in reads]
    assert page.evaluate("Date.now()") >= after
    expect(main(page).get_by_text(MINUTES)).to_have_count(0)


# --- a stale page (stale_version; D-19 wrong_state) --------------------------------------------------------------------

def new_version(page: Page, d: dict) -> dict:
    """Give the stock decision a new recommendation version through the API: its source requests a stock check on its
    plan-changing count fact, the addressed store answers 0 + 0 for every lot and confirms (the confirm re-plans the
    decision). The page ends signed in as the source again; the new detail."""
    src = d["source"]
    fact = count_fact(d)
    r = page.request.post(f"/api/cases/{quote(d['case_id'])}/requests", headers=key(),
                          data={"rec_version": d["rec_version"], "fact_id": fact["fact_id"]})
    assert r.status == 200, r.text()
    q = r.json()
    switch_seat(page, q["to_branch"])
    counts = {lot: {"shelf_units": 0, "backroom_units": 0} for lot in q["lot_ids"]}
    r = page.request.post(f"/api/requests/{q['request_id']}/answer", headers=key(),
                          multipart={"text": "", "counts": json.dumps(counts)})
    assert r.status == 200, r.text()
    sig = r.json()
    r = page.request.post(f"/api/signals/{sig['signal_id']}/confirm", headers=key(),
                          data={"revision": sig["revision"], "checked_both": True})
    assert r.status == 200, r.text()
    switch_seat(page, src)
    newer = case_json(page, d["case_id"])
    assert newer["rec_version"] != d["rec_version"], "the confirmed count gave the decision a new version"
    return newer


def test_a_stale_send_gets_the_stale_message_and_is_never_resent(visitor):
    a = visitor.page
    b = visitor.context.new_page()  # the same context: the same cookie, the same account
    cid = owned_story(a)
    shown = open_at(b, cid)  # B shows the version it has now
    expect(slot(b).get_by_role("button", name="Send for approval", exact=True)).to_be_visible()

    newer = new_version(a, shown)
    assert newer["state"] == "OPEN", newer["state"]

    sends = case_requests(b, cid, "POST", "send")
    slot(b).get_by_role("button", name="Send for approval", exact=True).click()
    alert = slot(b).get_by_role("alert")
    expect(alert).to_contain_text(STALE)
    reload_btn = alert.get_by_role("button", name="Reload the stock decision", exact=True)
    expect(reload_btn).to_be_visible()
    flush(b)
    assert len(sends) == 1, [r.url for r in sends]
    answer = sends[0].response()
    assert answer is not None and answer.status == 409 and answer.json()["error"] == "stale_version", answer and answer.status
    assert sends[0].post_data_json == {"rec_version": shown["rec_version"]}, sends[0].post_data_json

    # Reload the stock decision: the new version's Send, with a new key; nothing was resent on the way.
    reload_btn.click()
    expect(slot(b).get_by_role("alert")).to_have_count(0)
    # The reloaded detail is the new version: Options' version line names it (the summary's Send stays in view).
    step_button(b, 3, "Options").click()
    expect(panel(b).get_by_test_id("version-line")).to_contain_text(newer["rec_version"][:12])
    flush(b)
    assert len(sends) == 1, [r.url for r in sends]
    slot(b).get_by_role("button", name="Send for approval", exact=True).click()
    wait_for(b, cid, "PENDING_APPROVAL")
    flush(b)
    assert len(sends) == 2, [r.url for r in sends]
    k1, k2 = (r.headers.get("idempotency-key") for r in sends)
    assert k1 and k2 and k1 != k2, "Send after Reload the stock decision reused the stale send's key"
    assert sends[1].post_data_json == {"rec_version": newer["rec_version"]}, sends[1].post_data_json


def test_a_stale_approve_after_a_new_version_reloads_and_is_sent_once(visitor):
    """A new version while the decision awaits approval cancels the round (D-19). A page still showing Approve plan gets
    the server's refusal (the API checks the state first: wrong_state "Case is OPEN") with Reload the stock decision,
    sent once; after the reload the page shows the cancelled round and Send for approval again."""
    a = visitor.page
    cid = owned_story(a)
    sent = send_on_page(a, cid)

    # The destination accepts through the API, so the source's next action is Approve plan.
    switch_seat(a, sent["dest"])
    r = a.request.post(f"/api/cases/{quote(cid)}/decision", headers=key(),
                       data={"rec_version": sent["rec_version"], "decision": "accept"})
    assert r.status == 200, r.text()
    switch_seat(a, sent["source"])
    b = visitor.context.new_page()
    open_at(b, cid)
    expect(slot(b).get_by_role("button", name="Approve plan", exact=True)).to_be_visible()

    newer = new_version(a, case_json(a, cid))
    assert newer["state"] == "OPEN" and newer["hold"] is None, (newer["state"], newer["hold"])

    decisions = case_requests(b, cid, "POST", "decision")
    slot(b).get_by_role("button", name="Approve plan", exact=True).click()
    alert = slot(b).get_by_role("alert")
    reload_btn = alert.get_by_role("button", name="Reload the stock decision", exact=True)
    expect(reload_btn).to_be_visible()
    flush(b)
    assert len(decisions) == 1, [r.url for r in decisions]
    answer = decisions[0].response()
    assert answer is not None and answer.status == 409, answer and answer.status
    body = answer.json()
    assert body["error"] in ("wrong_state", "stale_version"), body
    expect(alert.locator("span").first).to_have_text(STALE if body["error"] == "stale_version" else body["message"])

    reload_btn.click()
    expect(slot(b).get_by_role("button", name="Send for approval", exact=True)).to_be_visible()
    expect(main(b).get_by_test_id("approval-cancelled")).to_have_text(CANCELLED)
    flush(b)
    assert len(decisions) == 1, [r.url for r in decisions]


def test_try_again_resends_the_same_key_and_body(visitor):
    """The first Send is dropped once in the browser (an abort: the server never sees it). Try again sends exactly the
    same Idempotency-Key and body, and that request reaches the API and succeeds."""
    page = visitor.page
    cid = owned_story(page)
    d = open_at(page, cid)
    sends = case_requests(page, cid, "POST", "send")
    page.route(f"**/api/cases/{quote(cid)}/send", lambda route: route.abort(), times=1)
    slot(page).get_by_role("button", name="Send for approval", exact=True).click()
    alert = slot(page).get_by_role("alert")
    retry = alert.get_by_role("button", name="Try again", exact=True)
    expect(retry).to_be_visible()
    flush(page)
    assert len(sends) == 1 and case_json(page, cid)["state"] == "OPEN"
    retry.click()
    wait_for(page, cid, "PENDING_APPROVAL")
    flush(page)
    assert len(sends) == 2, [r.url for r in sends]
    first, second = sends
    assert first.headers.get("idempotency-key") and second.headers.get("idempotency-key") == first.headers.get("idempotency-key")
    assert second.post_data == first.post_data and first.post_data_json == {"rec_version": d["rec_version"]}
    deadline = time.monotonic() + 10
    while second.response() is None and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert second.response().status == 200
