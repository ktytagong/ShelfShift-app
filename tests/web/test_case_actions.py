"""PROD-08 / D-54: the approvals on the stock decision page (06.1-UI-SPEC "Step panel" rows 5 and 6, "Primary action by
state and role", "Destructive and consequential actions"). The destination accepts, the source approves, Commit
confirms the booking and prepares the transfer work order; the inline Reject stock decision… and Decline transfer…
forms say no with a reason. Rewritten for the 06.1 route (06.1-26): the Phase 5 action bar, the review-day test and
the Next day test are gone with the case page and the demo controls (D-03, D-36).

Every expectation is read from the API (the case detail, its activity log and the session), never pasted (04 D-14);
the step is always the server's (D-24)."""
from __future__ import annotations

import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_id_by_tag, case_json, session_json, switch_seat, take_ownership
from tests.web.test_case import STORY, decision_href, panel, step_button

pytestmark = pytest.mark.web

REASON = ("No chilled space at the destination this week: the walk-in cooler is being serviced and the backroom "
          "shelving is full with the weekend promotion delivery.")
REJECT_HELPER = "A rejected stock decision is not reopened. The lot follows the markdown schedule."


def main(page: Page):
    return page.locator("main#content")


def slot(page: Page):
    """The summary's action slot: the current step's primary and its secondary, or the waiting line."""
    return main(page).get_by_test_id("primary-slot")


def open_at(page: Page, case_id: str, step: str | None = None) -> dict:
    page.goto(decision_href(case_id, step))
    d = case_json(page, case_id)
    expect(main(page).get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    return d


def wait_for(page: Page, case_id: str, state: str, timeout_s: float = 20) -> dict:
    """Poll the detail until its state is `state` (the write is the page's; this only waits for it)."""
    for _ in range(int(timeout_s * 10)):
        d = case_json(page, case_id)
        if d["state"] == state:
            return d
        page.wait_for_timeout(100)
    raise AssertionError(f"{case_id} never reached {state}: {case_json(page, case_id)['state']}")


def sent_story(page: Page) -> dict:
    """The story transfer, owned and sent by its source manager through the page; its detail (PENDING_APPROVAL)."""
    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    open_at(page, cid)
    slot(page).get_by_role("button", name="Send for approval", exact=True).click()
    expect(step_button(page, 5, "Approve")).to_have_attribute("aria-current", "step")
    return wait_for(page, cid, "PENDING_APPROVAL")


def latest_history(page: Page):
    return main(page).get_by_test_id("latest-history")


# --- accept, approve, commit, prepare ---------------------------------------------------------------------------------

def test_destination_accepts_source_approves_and_prepares(visitor):
    page = visitor.page
    d = sent_story(page)
    cid, src, dest = d["case_id"], d["source"], d["dest"]
    assert dest is not None and d["next_action"] == {"key": "accept", "who": [{"role": "manager", "branch": dest}]}, d["next_action"]

    # The source waits for the destination; the boxes show who decides and their chips.
    expect(slot(page).get_by_test_id("waiting-line")).to_have_text(f"Waiting for the {dest} manager to accept the transfer.")
    expect(panel(page).get_by_test_id("source-approval")).to_contain_text(f"{src} manager")
    expect(panel(page).get_by_test_id("source-approval")).to_contain_text("Waiting")
    expect(panel(page).get_by_test_id("destination-acceptance")).to_contain_text(f"{dest} manager")
    expect(panel(page).get_by_test_id("destination-acceptance")).to_contain_text("May decline, with a reason.")
    expect(panel(page).get_by_text("Only people can approve.", exact=True)).to_be_visible()
    expect(panel(page).get_by_test_id("held-resources")).to_contain_text("Available excludes this decision's own bookings")

    # The destination store sees Accept transfer and Decline transfer….
    switch_seat(page, dest)
    open_at(page, cid)
    expect(slot(page).get_by_role("button", name="Accept transfer", exact=True)).to_be_visible()
    expect(slot(page).get_by_role("button", name="Decline transfer…", exact=True)).to_be_visible()
    expect(slot(page).get_by_role("button", name="Approve plan")).to_have_count(0)
    slot(page).get_by_role("button", name="Accept transfer", exact=True).click()
    expect(slot(page).get_by_test_id("waiting-line")).to_have_text(f"Waiting for the {src} manager to approve the plan.")
    after = case_json(page, cid)
    assert after["approvals"]["decisions"].get("destination") == "accept", after["approvals"]["decisions"]
    # Only one side decided: the decided box shows its chip, the other Waiting; the booking line is still a fixed time.
    expect(panel(page).get_by_test_id("destination-acceptance")).to_contain_text("Accepted")
    expect(panel(page).get_by_test_id("source-approval")).to_contain_text("Waiting")
    expect(panel(page).get_by_test_id("booking-line")).to_have_text(re.compile(r"^Capacity held until \d{2}:\d{2} .+ time\.$"))

    # The source approves: the round commits, Commit shows the confirmed booking and Prepare.
    switch_seat(page, src)
    open_at(page, cid)
    slot(page).get_by_role("button", name="Approve plan", exact=True).click()
    expect(step_button(page, 6, "Commit")).to_have_attribute("aria-current", "step")
    assert wait_for(page, cid, "APPROVED")["step"] == "COMMIT"
    expect(panel(page)).to_have_attribute("data-step", "COMMIT")
    expect(panel(page).get_by_test_id("commit-text")).to_contain_text("When both sides approve")
    expect(panel(page).get_by_test_id("booking-line")).to_have_text("Booking confirmed.")
    prepare = slot(page).get_by_role("button", name="Prepare transfer work order", exact=True)
    expect(prepare).to_be_visible()
    prepare.click()
    expect(step_button(page, 7, "Execute")).to_have_attribute("aria-current", "step")
    done = wait_for(page, cid, "PREPARED")
    assert done["step"] == "EXECUTE", done["step"]

    # The work order line on Commit: "Transfer work order {id} · {qty} units · {run}" from the server's work order.
    r = page.request.get("/api/cases/" + urllib.parse.quote(cid, safe="") + "/execution")
    assert r.status == 200, r.status
    wo = next(w for w in r.json()["work_orders"] if w["kind"] == "TRANSFER")
    open_at(page, cid, "commit")
    line = panel(page).get_by_test_id("work-order-line").filter(has_text="Transfer work order")
    expect(line).to_have_text(f"Transfer work order {wo['action_id']} · {wo['qty']:,g} units · {wo['run_code']}")
    expect(main(page).get_by_text(re.compile(r"\d+ min\b"))).to_have_count(0)


# --- Reject stock decision… and Decline transfer… ----------------------------------------------------------------------

def test_the_owner_rejects_with_a_reason_and_the_decision_is_archived(visitor):
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    take_ownership(page, cid)
    open_at(page, cid)
    opener = slot(page).get_by_role("button", name="Reject stock decision…", exact=True)

    # The form opens with its Reason focused; Keep the stock decision closes it and returns focus to its opener.
    opener.click()
    form = slot(page).get_by_test_id("reject-form")
    expect(form.get_by_label("Reason")).to_be_focused()
    expect(form.get_by_text(REJECT_HELPER, exact=True)).to_be_visible()
    expect(form.get_by_role("button", name="Cancel")).to_have_count(0)
    form.get_by_role("button", name="Keep the stock decision", exact=True).click()
    expect(form).to_have_count(0)
    expect(opener).to_be_focused()
    assert case_json(page, cid)["state"] == "OPEN", "keeping the stock decision writes nothing"

    opener.click()
    form.get_by_label("Reason").fill(REASON)
    form.get_by_role("button", name="Reject stock decision", exact=True).click()
    expect(step_button(page, 9, "Archive")).to_have_attribute("aria-current", "step")
    after = wait_for(page, cid, "REJECTED")
    assert after["step"] == "ARCHIVE" and after["next_action"]["key"] is None, (after["step"], after["next_action"])
    expect(slot(page)).to_have_count(0)  # nobody acts on a rejected decision: no button and no waiting line
    # The rejection names the store that rejected it (quick 261017-l5q: every person-made entry names its actor).
    expect(latest_history(page)).to_contain_text(f"Rejected by {session_json(page)['account']}")
    expect(latest_history(page)).to_contain_text(REASON)


def test_the_destination_declines_the_transfer_with_a_reason(visitor):
    page = visitor.page
    d = sent_story(page)
    cid, dest = d["case_id"], d["dest"]
    switch_seat(page, dest)
    open_at(page, cid)
    opener = slot(page).get_by_role("button", name="Decline transfer…", exact=True)
    opener.click()
    form = slot(page).get_by_test_id("decline-form")
    expect(form.get_by_label("Reason")).to_be_focused()
    form.get_by_role("button", name="Keep the transfer", exact=True).click()
    expect(form).to_have_count(0)
    expect(opener).to_be_focused()
    assert case_json(page, cid)["state"] == "PENDING_APPROVAL", "keeping the transfer writes nothing"

    opener.click()
    form.get_by_label("Reason").fill(REASON)
    form.get_by_role("button", name="Decline transfer", exact=True).click()
    after = wait_for(page, cid, "REJECTED")
    expect(step_button(page, 9, "Archive")).to_have_attribute("aria-current", "step")
    assert after["reason"] == f"Declined: {REASON}", after["reason"]
    expect(latest_history(page)).to_contain_text("Declined by ")
    expect(latest_history(page)).to_contain_text(REASON)


def test_approve_fits_at_390_without_sideways_scroll(visitor):
    page = visitor.page
    d = sent_story(page)
    switch_seat(page, d["dest"])  # at full width: the Main navigation shows in the sidebar there
    page.set_viewport_size({"width": 390, "height": 800})
    open_at(page, d["case_id"])
    expect(slot(page).get_by_role("button", name="Accept transfer", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the page scrolls sideways at 390px"
