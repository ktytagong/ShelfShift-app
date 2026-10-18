"""D-09: two visitors never see each other's workspace (FLOW-02; T-05-26), and a change made in another tab of the same
visitor shows on the case page when that tab becomes visible again, with no interval polling on the case page.

06.1 (D-02, D-47): each visitor signs in on the card through conftest's `sign_in`, and a seat change is a sign-out and a
sign-in in the same browser (`switch_seat`), which keeps that visitor's workspace. Tasks replaced Requests (D-50), so
the other visitor's empty Tasks page is read there (06.1-22).

The helpers this file needs are its own (06.1-22): the Phase 5 board and case page tests they came from are being
rebuilt by the 06.1 page plans, and isolation must not break with them. The stock check and the send go through the
API (the subject here is the workspace and the refetch, not those controls); the refocus test reads the step the
stock decision page shows, which is the server's."""
from __future__ import annotations

import time
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import (
    case_json, cards_json, new_context, session_id, session_json, sign_in, switch_seat, take_ownership,
)
from tests.web.test_tasks import open_tasks_page, request_stock_check

pytestmark = pytest.mark.web

def own_card(page, key: str) -> dict:
    found = [c for c in cards_json(page) if c["key"] == key]
    assert len(found) == 1, found
    return found[0]


def set_visibility(page: Page, state: str) -> None:
    """Hide or show the tab the way the browser reports it: document.visibilityState plus a visibilitychange event
    (research A3; Playwright has no visibility API)."""
    page.evaluate("""(state) => {
        Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => state});
        document.dispatchEvent(new Event('visibilitychange'));
    }""", state)


def case_gets(page: Page, case_id: str) -> list[str]:
    """Start recording the page's own GET /api/cases/{id} requests (the case read, not /activity or the board)."""
    path = "/api/cases/" + urllib.parse.quote(case_id, safe="")
    seen: list[str] = []

    def on(request) -> None:
        if request.method == "GET" and urllib.parse.urlsplit(request.url).path == path:
            seen.append(request.url)

    page.on("request", on)
    return seen


def wait_for_state(page: Page, case_id: str, state: str) -> dict:
    deadline = time.monotonic() + 30
    while True:
        d = case_json(page, case_id)
        if d["state"] == state:
            return d
        assert time.monotonic() < deadline, f"{d['key']} stayed {d['state']}, never {state}"
        page.wait_for_timeout(100)


def send_through_api(page: Page, d: dict) -> None:
    """Take ownership and send the stock decision for approval as the page's account (Send needs an owner since 06.1-08,
    D-54)."""
    take_ownership(page, d["case_id"])
    now = case_json(page, d["case_id"])
    r = page.request.post("/api/cases/" + urllib.parse.quote(d["case_id"], safe="") + "/send",
                          headers={"Idempotency-Key": str(uuid.uuid4())}, data={"rec_version": now["rec_version"]})
    assert r.status == 200, r.text()
    wait_for_state(page, d["case_id"], "PENDING_APPROVAL")


def test_two_visitors_never_see_each_others_sandbox(browser, web):
    a, b = new_context(browser, web), new_context(browser, web)
    try:
        two_visitors_never_share(a, b, web)
    finally:
        a.close()
        b.close()


def two_visitors_never_share(a, b, web) -> None:
    pa, pb = a.new_page(), b.new_page()
    # Both sign in on the card as the same store manager: the same account still gets two workspaces.
    sa, sb = sign_in(pa, web), sign_in(pb, web)
    assert sa["account"] == sb["account"] == web.store
    assert session_id(a) != session_id(b)
    assert sa["run_id"] != sb["run_id"], "two visitors share one workspace"
    b_milk = own_card(pb, "SUS-19")
    assert b_milk["state"] == "OPEN", b_milk["state"]

    # A asks a branch for a stock check on SUS-19. As that branch, A has an open request; B has none.
    d, q = request_stock_check(pa)
    counter, source = q["to_branch"], d["source"]
    switch_seat(pa, counter)
    assert session_json(pa)["open_requests"] >= 1, "A's own request is not counted, so B's 0 below would prove nothing"
    switch_seat(pb, counter)
    open_tasks_page(pb)
    expect(pb.get_by_role("heading", level=2, name=f"No tasks for {counter}")).to_be_visible()
    assert session_json(pb)["open_requests"] == 0
    assert case_json(pb, b_milk["case_id"])["requests"] == []

    # A sends SUS-19 for approval; B's SUS-19 is still open
    switch_seat(pa, source)
    send_through_api(pa, d)
    switch_seat(pb, source)
    assert own_card(pb, "SUS-19")["state"] == "OPEN"

    # A's SUS-19 moved on; B's workspace is where it started (06.1 has no Next day: D-03, D-36)
    assert own_card(pa, "SUS-19")["state"] == "PENDING_APPROVAL"
    now = session_json(pb)
    assert (now["run_id"], now["day"], now["version"]) == (sb["run_id"], sb["day"], sb["version"])
    pb.reload()
    assert own_card(pb, "SUS-19")["state"] == "OPEN"
    assert case_json(pb, b_milk["case_id"])["state"] == "OPEN"


def open_decision(page: Page, case_id: str) -> None:
    """Open the stock decision page by its route and wait for its step panel (the page 06.1-18 builds)."""
    page.goto("/#/cases/" + urllib.parse.quote(case_id, safe=""))
    expect(page.get_by_test_id("step-panel")).to_be_visible()


def test_a_change_in_another_tab_shows_on_refocus(visitor):
    """The stock decision page never polls; a send made in another tab shows once this tab is visible again. The
    shown step is the server's (`step`), read from the step panel, so the test follows whatever control the page
    draws for it."""
    page_a = visitor.page
    page_b = visitor.context.new_page()  # the same visitor (same cookie, same sandbox) in a second tab
    page_b.clock.install()  # fake timers: any interval on the page would fire under run_for below
    d = case_json(page_a, own_card(page_a, "SUS-19")["case_id"])
    open_decision(page_b, d["case_id"])
    panel = page_b.get_by_test_id("step-panel")
    expect(panel).to_have_attribute("data-step", d["step"])
    gets = case_gets(page_b, d["case_id"])

    # B goes to the background; A sends SUS-19 for approval
    set_visibility(page_b, "hidden")
    send_through_api(page_a, d)
    sent = case_json(page_a, d["case_id"])
    assert sent["step"] != d["step"], (d["step"], sent["step"])

    # B is unchanged: ten seconds of its timers bring no case read (the page never polls)
    page_b.clock.run_for(10_000)
    assert gets == [], gets
    expect(panel).to_have_attribute("data-step", d["step"])

    # B becomes visible again: one read, and the step the send moved to shows
    with page_b.expect_response(lambda r: r.request.method == "GET" and r.url.endswith(
            "/api/cases/" + urllib.parse.quote(d["case_id"], safe=""))):
        set_visibility(page_b, "visible")
    expect(panel).to_have_attribute("data-step", sent["step"])
    assert len(gets) >= 1
