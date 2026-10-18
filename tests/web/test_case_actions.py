"""PROD-08 / D-54: the approvals on the stock decision page (06.1-UI-SPEC "Step panel" rows 5 and 6, "Primary action by
state and role", "Destructive and consequential actions"). The destination accepts, the source approves, Commit
confirms the booking and prepares the transfer work order; the inline Reject stock decision… and Decline transfer…
forms say no with a reason. Rewritten for the 06.1 route (06.1-26): the Phase 5 action bar, the review-day test and
the Next day test are gone with the case page and the demo controls (D-03, D-36).

Every expectation is read from the API (the case detail, its activity log and the session), never pasted (04 D-14);
the step is always the server's (D-24)."""
from __future__ import annotations

import json
import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import cards_json, case_id_by_tag, case_json, session_json, switch_seat, take_ownership
from tests.web.test_case import STORY, decision_href, panel, step_button

pytestmark = pytest.mark.web

REASON = ("No chilled space at the destination this week: the walk-in cooler is being serviced and the backroom "
          "shelving is full with the weekend promotion delivery.")
REJECT_HELPER = "The lot stays on the Decision board while it's at risk."  # 07-CONTEXT D-24, D-36 (07-21)
# The acknowledgement (07-CONTEXT D-12, D-26; 07-UI-SPEC "Approve step: acknowledging advisory checks", E13)
ACK_HINT = "Tick the box to approve with unverified facts"
ACK_AGAIN = "These facts changed · acknowledge again"
PLAN_AGAIN = "This plan changed · review it again"
ACK_TICK = re.compile(r"^I've seen (this unverified fact|these \d+ unverified facts) and approve with (it|them) unresolved$")
EXPOSURE = "Unverified · estimated decision exposure "


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


def ack_box(page: Page):
    return slot(page).get_by_role("checkbox", name=ACK_TICK)


def ack_list(page: Page):
    """The Approve panel's "Acknowledge unverified facts ({N})" list."""
    return panel(page).get_by_test_id("ack-facts")


def decision_posts(page: Page) -> list:
    """Every POST to a decision route the page sends from now on (the list fills as they go)."""
    sent: list = []
    page.on("request", lambda r: sent.append(r) if r.method == "POST" and r.url.endswith("/decision") else None)
    return sent


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
    ack_box(page).check()  # the story's plan has an Advisory fact (07-CONTEXT D-12, D-26; sanctioned)
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
    ack_box(page).check()  # sanctioned, as above
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


# --- the acknowledgement of unverified facts (07-17 Task 1; 07-CONTEXT D-12, D-26; UI E13) ------------------------------

def test_accept_and_approve_wait_for_the_acknowledgement_of_the_unverified_facts(visitor):
    """The story's plan has Advisory facts: on Accept transfer (the destination) and then on Approve plan (the source)
    the Approve panel lists them under "Acknowledge unverified facts ({N})", the button stays disabled with the hint
    beside it until the one box is ticked, and the decision then carries the detail's acknowledgement and the plan the
    page shows (07 review C, D-32); the stock decision reaches APPROVED."""
    page = visitor.page
    d = sent_story(page)
    cid, src, dest = d["case_id"], d["source"], d["dest"]
    ack, advisory = d["commitment"]["ack"], d["commitment"]["advisory"]
    assert ack is not None and advisory, "hand-back: the story's plan has no Advisory fact (test_commitment.py)"
    facts = {f["fact_id"]: f for f in d["fact_checks"]}
    sent = decision_posts(page)

    for who, name, state in ((dest, "Accept transfer", "PENDING_APPROVAL"), (src, "Approve plan", "APPROVED")):
        switch_seat(page, who)
        open_at(page, cid)
        listed = ack_list(page)
        expect(listed.get_by_role("heading", name=f"Acknowledge unverified facts ({len(advisory)})", exact=True)).to_be_visible()
        for a in advisory:
            row = listed.locator(f"li[data-fact-id='{a['fact_id']}']")
            expect(row).to_contain_text(f"{a['branch']} · lot {', '.join(a['lot_ids'])}")
            expect(row).to_contain_text(EXPOSURE)
            assert facts[a["fact_id"]]["pick_feasible_if_alt"], "an Advisory fact is one the plan survives"
        button = slot(page).get_by_role("button", name=name, exact=True)
        expect(button).to_be_disabled()
        expect(slot(page).get_by_text(ACK_HINT, exact=True)).to_be_visible()
        expect(ack_box(page)).not_to_be_checked()
        ack_box(page).check()
        expect(button).to_be_enabled()
        expect(slot(page).get_by_text(ACK_HINT, exact=True)).to_have_count(0)
        button.click()
        if state == "APPROVED":
            expect(step_button(page, 6, "Commit")).to_have_attribute("aria-current", "step")
        else:
            expect(slot(page).get_by_test_id("waiting-line")).to_be_visible()
        assert wait_for(page, cid, state)["state"] == state

    bodies = [r.post_data_json for r in sent]
    assert [b["decision"] for b in bodies] == ["accept", "approve"], bodies
    for b in bodies:
        assert (b["acknowledged"], b["plan_key"], b["rec_version"]) == (ack, d["suggested_key"], d["rec_version"]), b


def test_a_stale_acknowledgement_unticks_the_box(visitor):
    """E13 loading · error: the server refuses an acknowledgement that no longer matches (here the request is changed
    on its way, standing in for facts that changed after the page loaded): the box unticks with "These facts changed
    · acknowledge again", the button is disabled again, and a fresh tick then records the decision."""
    page = visitor.page
    d = sent_story(page)
    cid, dest = d["case_id"], d["dest"]
    switch_seat(page, dest)
    open_at(page, cid)

    def stale(route):
        body = route.request.post_data_json
        body["acknowledged"] = {**body["acknowledged"], "fact_ids": []}
        route.continue_(post_data=json.dumps(body))

    page.route("**/decision", stale)
    ack_box(page).check()
    with page.expect_response(lambda r: r.url.endswith("/decision")) as got:
        slot(page).get_by_role("button", name="Accept transfer", exact=True).click()
    assert (got.value.status, got.value.json()["error"]) == (409, "ack_required"), got.value.text()
    expect(slot(page).get_by_text(ACK_AGAIN, exact=True)).to_be_visible()
    expect(ack_box(page)).not_to_be_checked()
    expect(slot(page).get_by_role("button", name="Accept transfer", exact=True)).to_be_disabled()
    assert case_json(page, cid)["approvals"]["decisions"] == {}, "the refused accept recorded nothing"

    page.unroute("**/decision")
    ack_box(page).check()
    slot(page).get_by_role("button", name="Accept transfer", exact=True).click()
    expect(slot(page).get_by_test_id("waiting-line")).to_be_visible()
    assert case_json(page, cid)["approvals"]["decisions"].get("destination") == "accept"


def test_a_changed_plan_reloads_and_unticks_the_box(visitor):
    """07 review C on the page: a decision that names another plan than the plan in force is refused (409 stale_plan;
    here the request is changed on its way, standing in for a plan selected elsewhere); the page shows "This plan
    changed · review it again", reloads the stock decision and unticks the box."""
    page = visitor.page
    d = sent_story(page)
    cid, dest = d["case_id"], d["dest"]
    switch_seat(page, dest)
    open_at(page, cid)

    def other_plan(route):
        body = route.request.post_data_json
        body["plan_key"] = "KEEP" if body["plan_key"] != "KEEP" else "OTHER"
        route.continue_(post_data=json.dumps(body))

    page.route("**/decision", other_plan)
    ack_box(page).check()
    with page.expect_response(lambda r: r.url.endswith("/decision")) as got:
        with page.expect_response(lambda r: r.url.endswith(urllib.parse.quote(cid, safe="")) and r.request.method == "GET"):
            slot(page).get_by_role("button", name="Accept transfer", exact=True).click()
    assert (got.value.status, got.value.json()["error"]) == (409, "stale_plan"), got.value.text()
    expect(slot(page).get_by_text(PLAN_AGAIN, exact=True)).to_be_visible()
    expect(ack_box(page)).not_to_be_checked()
    expect(slot(page).get_by_role("button", name="Accept transfer", exact=True)).to_be_disabled()
    page.unroute("**/decision")


def test_with_no_advisory_facts_there_is_no_acknowledgement_panel(visitor):
    """E13 empty: a stock decision whose plan has no Advisory fact shows no panel, no box and no hint: the first
    approver's button is enabled as soon as the plan was sent. The decision is found from HQ's board (every store's)."""
    page = visitor.page
    switch_seat(page, None)
    found = None
    for c in cards_json(page):
        if c["state"] != "OPEN" or c["rec_version"] is None or c["suggested_key"] is None:
            continue
        d = case_json(page, c["case_id"])
        if d["commitment"]["ack"] is None and not d["commitment"]["advisory"]:
            found = d
            break
    assert found is not None, "no OPEN stock decision whose plan has no Advisory fact"
    cid = found["case_id"]
    switch_seat(page, found["source"])
    take_ownership(page, cid)
    open_at(page, cid)
    slot(page).get_by_role("button", name="Send for approval", exact=True).click()
    nxt = wait_for(page, cid, "PENDING_APPROVAL")["next_action"]
    assert nxt["key"] in ("accept", "approve"), nxt
    switch_seat(page, nxt["who"][0]["branch"])
    open_at(page, cid)
    name = "Accept transfer" if nxt["key"] == "accept" else "Approve plan"
    expect(slot(page).get_by_role("button", name=name, exact=True)).to_be_enabled()
    expect(ack_box(page)).to_have_count(0)
    expect(main(page).get_by_text(ACK_HINT)).to_have_count(0)
    open_at(page, cid, "approve")
    expect(ack_list(page)).to_have_count(0)


def test_ten_long_advisory_facts_keep_approve_in_view(visitor):
    """E13 long-text backstop at 1280x800: ten Advisory facts with long titles list in the panel while the tick and
    Approve plan stay in view in the summary's action slot (the detail is padded on its way to the page)."""
    page = visitor.page
    d = sent_story(page)
    cid, dest = d["case_id"], d["dest"]
    switch_seat(page, dest)
    page.set_viewport_size({"width": 1280, "height": 800})
    base = d["commitment"]["advisory"][0]
    fact = next(f for f in d["fact_checks"] if f["fact_id"] == base["fact_id"])

    def padded(route):
        r = route.fetch()
        body = r.json()
        if body.get("commitment", {}).get("advisory"):
            extra = [{**base, "fact_id": f"{base['fact_id']}-{i}", "lot_ids": [f"LOT-WITH-A-VERY-LONG-NAME-{i:02d}-" + "X" * 40]}
                     for i in range(9)]
            body["commitment"]["advisory"] = [base] + extra
            body["fact_checks"] = body["fact_checks"] + [{**fact, "fact_id": e["fact_id"], "lot_ids": e["lot_ids"]} for e in extra]
        route.fulfill(response=r, json=body)

    path = "/api/cases/" + urllib.parse.quote(cid, safe="")
    is_detail = lambda url: urllib.parse.urlsplit(url).path == path  # noqa: E731
    page.route(is_detail, padded)
    page.reload()
    open_at(page, cid)
    expect(ack_list(page).locator("li")).to_have_count(10)
    button = slot(page).get_by_role("button", name="Accept transfer", exact=True)
    expect(button).to_be_in_viewport()
    expect(ack_box(page)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "the list scrolls sideways"
    page.unroute(is_detail)
