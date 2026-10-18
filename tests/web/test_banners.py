"""The stock decision page's banners (07-22; 07-UI-SPEC "Banners on the case", "Holds, flags and reports"; 07-CONTEXT
D-13, D-16, D-17, D-24, and D-36, which replaces the information-only rule for prepared work with "Dispatch blocked").

Holds, reports and closes are made through the API as the people involved make them (the lot's manager files a
Mixed dates EXPIRY report or places a hold; the source rejects or cancels). Expected text is built from the API's own
answers, never typed ids. A stock decision opens at `#/cases/{id}` directly.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import (
    case_id_by_tag, case_json, db_tx, decision_body, run_id, switch_seat, take_ownership,
)
from tests.web.test_transfers import STORY_TAG, case_path, prepare_story

pytestmark = pytest.mark.web

MIXED = "Separate stock and verify before moving"


# --- setup through the API -------------------------------------------------------------------------------------------

def keyed() -> dict:
    return {"Idempotency-Key": str(uuid.uuid4())}


def post(page: Page, path: str, body: dict) -> dict:
    r = page.request.post(path, data=body, headers=keyed())
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def file_report(page: Page, kind: str, fields: dict, **extra: str) -> dict:
    """POST /api/signals (multipart) as the signed-in manager; asserts 200."""
    parts = {"kind": kind, "fields": json.dumps(fields), "text": "", **extra}
    r = page.request.post("/api/signals", multipart=parts, headers=keyed())
    assert r.status == 200, (r.status, r.text())
    return r.json()


def story(page: Page) -> dict:
    """The story stock decision's detail, signed in as its source (the lot's own manager)."""
    d = case_json(page, case_id_by_tag(page, STORY_TAG))
    switch_seat(page, d["source"])
    return d


def mixed_hold(page: Page, d: dict) -> dict:
    """The lot's manager reports Mixed dates on the story lot: an ACTIVE MIXED_DATES hold placed at submission."""
    rep = file_report(page, "EXPIRY", {"lot_id": d["lot"]["lot_id"], "printed_date": "MIXED"}, observed="now")
    assert rep["hold_id"] is not None, rep
    return rep


def open_case(page: Page, case_id: str) -> None:
    page.goto(f"/#/cases/{urllib.parse.quote(case_id, safe='')}")
    expect(page.get_by_test_id("step-panel")).to_be_visible()


# --- Task 1: lot holds -----------------------------------------------------------------------------------------------

def test_a_hold_banner_and_its_owner(visitor):
    page = visitor.page
    d = story(page)
    lot, src, dest = d["lot"]["lot_id"], d["source"], d["dest"]
    mixed_hold(page, d)
    open_case(page, d["case_id"])
    banner = page.get_by_test_id("lot-hold-banner")
    expect(banner).to_contain_text(f"Lot hold · lot {lot} · {MIXED}")
    expect(banner).to_contain_text(re.compile(rf"Placed by {re.escape(src)} manager · .+ Bangkok time · Owner: "
                                              rf"{re.escape(src)} manager"))
    expect(banner.get_by_role("link", name="Confirm printed date")).to_have_attribute("href", "#/tasks")
    expect(banner.get_by_role("button", name="Inspect and release")).to_be_visible()

    switch_seat(page, dest)  # another store's manager reads who can resolve it, with no action
    open_case(page, d["case_id"])
    other = page.get_by_test_id("lot-hold-banner")
    expect(other).to_contain_text(f"Only the {src} manager can resolve this hold.")
    expect(other.get_by_role("button")).to_have_count(0)

    switch_seat(page, src)
    open_case(page, d["case_id"])
    page.get_by_role("button", name="Inspect and release").click()
    step = page.get_by_test_id("release-step")
    expect(step).to_contain_text(f"Release the hold on lot {lot}? Release it only after inspecting the stock.")
    release = step.get_by_role("button", name="Release hold")
    expect(release).to_be_disabled()
    expect(step).to_contain_text("Add a note")
    step.get_by_label("Inspection note").fill("Separated the lots; one printed date on the shelf")
    release.click()
    expect(page.get_by_test_id("lot-hold-banner")).to_have_count(0)
    assert case_json(page, d["case_id"])["holds"] == []


def test_a_prepared_case_reads_dispatch_blocked_with_its_reason(visitor):
    """D-36 (replaces 07-22's "a PREPARED case never reads blocked"): a hold on the lot of prepared work reads
    "Dispatch blocked · Resolve the lot hold." with the hold's reason and owner, and the owner's "Review hold" opens
    the release step; released, the banner is gone and the Execute step's dispatch is no longer blocked by it."""
    page = visitor.page
    s = prepare_story(page)
    lot = s["action"]["lot_id"]
    post(page, f"/api/lots/{urllib.parse.quote(lot, safe='')}/holds", {"reason": "Crushed crates in the chiller"})
    open_case(page, s["case_id"])
    banner = page.get_by_test_id("dispatch-blocked-hold")
    expect(banner).to_contain_text("Dispatch blocked · Resolve the lot hold.")
    expect(banner).to_contain_text(f"Lot hold · lot {lot} · Held for review: Crushed crates in the chiller")
    expect(banner).to_contain_text(f"Owner: {s['source']} manager")
    expect(page.locator("body")).not_to_contain_text("Informational until dispatch is recorded")
    page.get_by_role("button", name="Review hold").click()
    step = page.get_by_test_id("release-step")
    step.get_by_label("Inspection note").fill("Inspected: crates are fine")
    step.get_by_role("button", name="Release hold").click()
    expect(page.get_by_test_id("dispatch-blocked-hold")).to_have_count(0)


# --- Task 2: review flags, new reports, the successor link and Reassess ----------------------------------------------

NEW_INFO = "New confirmed information · review required"


def approve_story(page: Page) -> dict:
    """The story stock decision taken, sent, accepted and approved (not prepared); ends signed in as its source."""
    d = story(page)
    cid, version = d["case_id"], d["rec_version"]
    take_ownership(page, cid)
    post(page, case_path(cid, "send"), {"rec_version": version})
    switch_seat(page, d["dest"])
    post(page, case_path(cid, "decision"), decision_body(page, cid, "accept", version))
    switch_seat(page, d["source"])
    post(page, case_path(cid, "decision"), decision_body(page, cid, "approve", version))
    assert case_json(page, cid)["state"] == "APPROVED"
    return d


def raise_flag(page: Page, case_id: str) -> None:
    """A review flag on a committed case, raised by the service a confirmed fact calls (api/reviewflags.raise_for), as
    the API test M does: the browser suite has no faster honest way to a confirmed fact in the plan's scope."""
    from api import cases, reviewflags
    from tests.api.conftest import fresh
    with db_tx() as conn:
        r = fresh(run_id(page))
        assert reviewflags.raise_for(conn, r, [cases.case_row(conn, r["run_id"], case_id)], "TEST:new-count") == [case_id]


def test_a_flag_on_an_approved_plan_is_kept_with_a_review_note(visitor):
    page = visitor.page
    d = approve_story(page)
    raise_flag(page, d["case_id"])
    open_case(page, d["case_id"])
    banner = page.get_by_test_id("review-flag-banner")
    expect(banner).to_contain_text(NEW_INFO)
    expect(banner).to_contain_text(re.compile(r" · confirmed .+ Bangkok time"))
    expect(page.locator("body")).not_to_contain_text("Dispatch blocked")
    banner.get_by_role("button", name="Reviewed · keep plan").click()
    step = page.get_by_test_id("keep-step")
    keep = step.get_by_role("button", name="Reviewed · keep plan")
    expect(keep).to_be_disabled()
    expect(step).to_contain_text("Add a review note")
    step.get_by_label("Review note").fill("Checked the count: keep the plan")
    keep.click()
    expect(page.get_by_test_id("review-flag-banner")).to_have_count(0)
    assert [f for f in case_json(page, d["case_id"])["review_flags"] if f["cleared_at"] is None] == []


def test_with_a_hold_the_keep_action_is_absent_and_cancel_shows(visitor):
    """D-17: while the plan can't continue (a lot hold refuses Prepare) the keep action is absent and "Cancel approved
    plan…" shows; the cancel needs a reason, and the cancelled lot stays in sight ("Awaiting verification" while the
    hold remains, D-24)."""
    page = visitor.page
    d = approve_story(page)
    raise_flag(page, d["case_id"])
    post(page, f"/api/lots/{urllib.parse.quote(d['lot']['lot_id'], safe='')}/holds", {"reason": "Crushed crates"})
    open_case(page, d["case_id"])
    banner = page.get_by_test_id("review-flag-banner")
    expect(banner).to_contain_text(NEW_INFO)
    expect(banner.get_by_role("button", name="Reviewed · keep plan")).to_have_count(0)
    banner.get_by_role("button", name="Cancel approved plan…").click()
    step = page.get_by_test_id("cancel-step")
    expect(step.get_by_role("button", name="Cancel approved plan")).to_be_disabled()
    expect(page.get_by_text("The lot stays on the Decision board while it's at risk.").first).to_be_visible()
    step.get_by_label("Reason").fill("The chiller failed")
    step.get_by_role("button", name="Cancel approved plan").click()
    expect(page.get_by_test_id("lot-status-banner")).to_contain_text("Awaiting verification")
    after = case_json(page, d["case_id"])
    assert (after["state"], after["closed_as"]) == ("CLOSED", "CANCELLED")


def test_a_flag_on_prepared_work_reads_dispatch_blocked(visitor):
    """D-36: an open review flag on prepared work reads "Dispatch blocked · Review new confirmed information." and its
    "Review changes" opens the keep step; kept, the banner goes."""
    page = visitor.page
    s = prepare_story(page)
    raise_flag(page, s["case_id"])
    open_case(page, s["case_id"])
    banner = page.get_by_test_id("dispatch-blocked-flag")
    expect(banner).to_contain_text("Dispatch blocked · Review new confirmed information.")
    expect(banner).to_contain_text("Review before dispatch")
    banner.get_by_role("button", name="Review changes").click()
    step = page.get_by_test_id("keep-step")
    step.get_by_label("Review note").fill("Reviewed the new count; dispatch as planned")
    step.get_by_role("button", name="Reviewed · keep plan").click()
    expect(page.get_by_test_id("dispatch-blocked-flag")).to_have_count(0)


def test_an_unconfirmed_report_shows_its_banner_with_review_impact(visitor):
    page = visitor.page
    d = story(page)
    lot = d["lot"]["lot_id"]
    rep = file_report(page, "STOCK_COUNT", {"lot_id": lot, "shelf_units": 1, "backroom_units": 0}, observed="now")
    assert any(r["signal_id"] == rep["signal_id"] for r in case_json(page, d["case_id"])["reports"])
    open_case(page, d["case_id"])
    banner = page.get_by_test_id("new-report-banner")
    expect(banner).to_contain_text(f"New report from {d['source']} may change this plan")
    expect(banner).to_contain_text("Stock count · ")
    expect(banner.get_by_role("link", name="Review impact")).to_have_attribute(
        "href", f"#/events/{urllib.parse.quote(rep['signal_id'], safe='')}")


def test_reject_then_reassess_opens_the_linked_successor(visitor):
    """D-24, FLOW-14: after Reject the story decision reads "Needs decision" with its owner's "Reassess"; pressing it
    opens the successor, which reads "Follows {key} · Rejected {t}", while the old one reads "Followed by {key}"."""
    page = visitor.page
    d = story(page)
    take_ownership(page, d["case_id"])
    post(page, case_path(d["case_id"], "reject"), {"reason": "Not this week"})
    assert case_json(page, d["case_id"])["lot_status"] == "Needs decision"
    open_case(page, d["case_id"])
    status = page.get_by_test_id("lot-status-banner")
    expect(status).to_contain_text("Needs decision")
    status.get_by_role("button", name="Reassess").click()
    page.wait_for_url(re.compile(r"#/cases/(?!" + re.escape(urllib.parse.quote(d["case_id"], safe="")) + r"$)"))
    follows = page.get_by_test_id("follows-line")
    expect(follows).to_contain_text(re.compile(rf"Follows {re.escape(d['key'])} · Rejected .+ Bangkok time"))
    succ = case_json(page, d["case_id"])["successor"]
    assert succ is not None
    open_case(page, d["case_id"])
    expect(page.get_by_test_id("followed-by-line")).to_contain_text(f"Followed by {succ}")
    expect(page.get_by_test_id("lot-status-banner")).to_have_count(0)


def test_banners_stack_in_order_and_wrap_at_375(visitor):
    """E11 backstop: a lot hold and a new report on one stock decision stack hold first, then report (a review flag
    sits between them on committed work, where reports no longer show), and nothing scrolls sideways at 375 px."""
    page = visitor.page
    d = story(page)
    mixed_hold(page, d)  # its EXPIRY report stays unconfirmed, so the decision carries a New report banner as well
    page.set_viewport_size({"width": 375, "height": 800})
    open_case(page, d["case_id"])
    hold = page.get_by_test_id("lot-hold-banner").bounding_box()
    report = page.get_by_test_id("new-report-banner").bounding_box()
    assert hold is not None and report is not None and hold["y"] < report["y"]
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
