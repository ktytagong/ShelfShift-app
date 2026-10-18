"""PROD-11 on screen (06.1 D-34, D-65; UI-SPEC "Verify and close"): Verify shows what the engine expected next to what
was recorded, the receipt with its quantities, and Close by the owner with a closing note; an open receipt discrepancy
hides Close and shows the discrepancy banner instead. After the close, Archive keeps the closing line.

The story transfer (the milk-story tag) is carried through the API in setup, as the people involved make it: the source
takes ownership, sends, the destination accepts, the source approves, prepares and records the dispatch, the
destination records the receipt, and the source records the price change of the plan's markdown rest (06.1-11: the
decision executes only when every work order is done). Expected text is built from the API's own answers (the
recommendation, the execution view, the session), never typed here. A decision opens at `#/cases/{id}` directly.
"""
from __future__ import annotations

import datetime as dt
import decimal
import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_json, session_json, switch_seat
from tests.web.test_transfers import case_path, dispatch_story, get, post, prepare_story, units

pytestmark = pytest.mark.web

CLOSE = "Close stock decision"
NOTE = "Closing note"
KEEP_OPEN = "Keep it open"
DISCREPANCY = "Receipt discrepancy—reconciliation required."


# --- setup through the API -------------------------------------------------------------------------------------------

def receive_story(page: Page, story: dict, sent: int, missing: int = 0) -> dict:
    """The destination records the receipt (`missing` units short, with a reason when short), then the source records
    the price change of every planned markdown work order; ends signed in as the source. The case detail after it."""
    switch_seat(page, story["dest"])
    post(page, case_path(story["case_id"], "receipt"), {
        "action_id": story["action"]["action_id"], "received_qty": sent - missing, "damaged_qty": 0,
        "condition": story["condition"], "reason": "Units missing on arrival." if missing else ""})
    switch_seat(page, story["source"])
    view = get(page, case_path(story["case_id"], "execution"))
    for w in view["work_orders"]:
        if w["kind"] == "MARKDOWN" and w["state"] == "PLANNED":
            post(page, case_path(story["case_id"], "price-applied"), {"action_id": w["action_id"], "confirmed": True})
    return case_json(page, story["case_id"])


def executed_story(page: Page, missing: int = 0) -> tuple[dict, dict, int]:
    """(story, detail, units dispatched): the story transfer dispatched in full and received (short by `missing`)."""
    story = prepare_story(page)
    sent = dispatch_story(page, story)
    d = receive_story(page, story, sent, missing)
    assert d["state"] == "EXECUTED", d["state"]
    return story, d, sent


# --- reading the screen ----------------------------------------------------------------------------------------------

def mark_date(iso: str) -> str:
    """format.markDate: "Sat 17 Oct 2026" from an ISO date (the UTC calendar)."""
    day = dt.date.fromisoformat(iso)
    return f"{day:%a} {day.day} {day:%b} {day.year}"


def money(v: float) -> str:
    """format.money for USD (the session's currency in the test set): "$800.27", "-$3.10"."""
    cents = decimal.Decimal(str(v)).quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_EVEN)
    return f"{'-' if cents < 0 else ''}${abs(cents):,.2f}"


def whole(n: float) -> str:
    """format.expected: whole units, half away from zero, grouped."""
    return f"{int(decimal.Decimal(str(n)).quantize(decimal.Decimal('1'), rounding=decimal.ROUND_HALF_UP)):,}"


def decision_date(page: Page, d: dict) -> str:
    """The decision's scenario date (D-20): its recommendation's snapshot day in the calendar."""
    s = session_json(page)
    return next(c["date"] for c in s["calendar"] if c["day"] == d["versions"]["day"])


def open_at(page: Page, case_id: str, step: str | None = None) -> None:
    page.goto("/#/cases/" + urllib.parse.quote(case_id, safe="") + ("" if step is None else f"?step={step}"))
    expect(panel(page)).to_be_visible()


def main(page: Page):
    return page.locator("main#content")


def panel(page: Page):
    return main(page).get_by_test_id("step-panel")


def step_button(page: Page, n: int, name: str):
    return main(page).get_by_role("navigation", name="Steps").get_by_role("button", name=f"{n}. {name}")


def slot(page: Page):
    return main(page).get_by_test_id("primary-slot")


def close_form(page: Page):
    return panel(page).get_by_test_id("close-form")


# --- Task 1 ----------------------------------------------------------------------------------------------------------

def test_verify_shows_expected_beside_recorded_and_the_owner_closes(visitor):
    page = visitor.page
    story, d, sent = executed_story(page)
    assert d["step"] == "VERIFY" and d["next_action"]["key"] == "close", (d["step"], d["next_action"])
    rec = next(o for o in d["result"]["options"] if o["key"] == d["result"]["recommended"])
    open_at(page, story["case_id"], "verify")
    expect(panel(page)).to_have_attribute("data-step", "VERIFY")
    expect(step_button(page, 8, "Verify")).to_have_attribute("aria-current", "step")

    expected = panel(page).get_by_test_id("verify-expected")
    expect(expected.get_by_text("Simulated", exact=True)).to_be_visible()
    expect(expected).to_contain_text(f"{money(rec['net']['base'])} net · {whole(rec['wasted_units'])} units wasted")
    expect(expected.get_by_text("Engine estimate at decision time", exact=True)).to_be_visible()

    actual = panel(page).get_by_test_id("verify-actual")
    expect(actual.get_by_text("Not yet recorded", exact=True)).to_be_visible()
    expect(actual).to_contain_text(
        f"Appears when sales after {mark_date(decision_date(page, d))} are recorded or a new dataset is loaded.")
    expect(actual).not_to_contain_text("$")  # nothing observed is claimed before it is recorded

    receipt = panel(page).get_by_test_id("verify-receipt")
    expect(receipt).to_contain_text(f"Dispatched {units(sent)} · Received {units(sent)}")
    expect(receipt.get_by_text("No discrepancy", exact=True)).to_be_visible()
    expect(panel(page).get_by_role("alert")).to_have_count(0)

    # Keep it open closes the form and gives the focus back to the summary's button.
    button = slot(page).get_by_role("button", name=CLOSE, exact=True)
    button.click()
    expect(close_form(page)).to_be_visible()
    expect(close_form(page).get_by_label(NOTE, exact=True)).to_be_focused()
    close_form(page).get_by_role("button", name=KEEP_OPEN, exact=True).click()
    expect(close_form(page)).to_have_count(0)
    expect(slot(page).get_by_role("button", name=CLOSE, exact=True)).to_be_focused()
    assert case_json(page, story["case_id"])["state"] == "EXECUTED", "Keep it open changes nothing"

    # A closing note is required; then the owner closes the decision and Archive shows the closing line.
    slot(page).get_by_role("button", name=CLOSE, exact=True).click()
    close_form(page).get_by_role("button", name=CLOSE, exact=True).click()
    expect(close_form(page).get_by_label(NOTE, exact=True)).to_have_attribute("aria-invalid", "true")
    assert case_json(page, story["case_id"])["state"] == "EXECUTED", "an empty note sends nothing"
    note = "All 110 units arrived in good condition; sold through at the destination."
    close_form(page).get_by_label(NOTE, exact=True).fill(note)
    close_form(page).get_by_role("button", name=CLOSE, exact=True).click()
    expect(step_button(page, 9, "Archive")).to_have_attribute("aria-current", "step")
    after = case_json(page, story["case_id"])
    assert (after["state"], after["step"], after["closed_as"]) == ("CLOSED", "ARCHIVE", "COMPLETED"), after["state"]
    expect(panel(page)).to_have_attribute("data-step", "ARCHIVE")
    expect(panel(page).get_by_test_id("closing-line")).to_have_text(f"Closed · {note}")
    expect(main(page).get_by_role("button", name=CLOSE, exact=True)).to_have_count(0)


def test_a_short_receipt_shows_the_discrepancy_and_no_close(visitor):
    page = visitor.page
    story, d, sent = executed_story(page, missing=6)
    # D-65: the decision stays in Execute until the discrepancy is reconciled; the owner's next action is Reconcile.
    assert d["step"] == "EXECUTE" and d["next_action"]["key"] == "reconcile", (d["step"], d["next_action"])
    open_at(page, story["case_id"], "verify")

    receipt = panel(page).get_by_test_id("verify-receipt")
    expect(receipt).to_contain_text(f"Dispatched {units(sent)} · Received {units(sent - 6)}")
    expect(receipt.get_by_text("Receipt discrepancy", exact=True)).to_be_visible()
    expect(receipt.get_by_text("No discrepancy", exact=True)).to_have_count(0)
    banner = panel(page).get_by_role("alert").filter(has_text=DISCREPANCY)
    expect(banner).to_have_count(1)
    expect(banner).to_contain_text(f"Missing {units(6)}")
    expect(main(page).get_by_role("button", name=CLOSE, exact=True)).to_have_count(0)
    # The owner (the server's next action names the source) is led to Execute, where the reconciliation is recorded.
    link = banner.get_by_role("link", name="Reconcile discrepancy", exact=True)
    expect(link).to_have_attribute("href", re.compile(r"\?step=execute$"))

    # The destination store neither closes nor reconciles: it sees the banner without the link.
    switch_seat(page, story["dest"])
    open_at(page, story["case_id"], "verify")
    banner = panel(page).get_by_role("alert").filter(has_text=DISCREPANCY)
    expect(banner).to_have_count(1)
    expect(banner.get_by_role("link", name="Reconcile discrepancy")).to_have_count(0)
    expect(main(page).get_by_role("button", name=CLOSE, exact=True)).to_have_count(0)


def test_the_servers_refusals_show_in_its_words(visitor):
    """A refusal the page could not foresee shows the server's sentence: 403 under the form; 409 discrepancy_open closes
    the form and shows the discrepancy banner (UI-SPEC "Close with an open discrepancy (409)"). The refusals are the
    server's own envelopes, answered for the one POST through the page's route."""
    page = visitor.page
    story, d, _ = executed_story(page)
    open_at(page, story["case_id"], "verify")
    path = f"**/api/cases/{urllib.parse.quote(story['case_id'], safe='')}/close"
    forbidden = "Only the owner of this stock decision can close it."

    page.route(path, lambda route: route.fulfill(status=403, json={"error": "forbidden", "message": forbidden}))
    slot(page).get_by_role("button", name=CLOSE, exact=True).click()
    close_form(page).get_by_label(NOTE, exact=True).fill("Closing after the receipt.")
    close_form(page).get_by_role("button", name=CLOSE, exact=True).click()
    expect(panel(page).get_by_text(forbidden, exact=True)).to_be_visible()
    page.unroute(path)

    page.route(path, lambda route: route.fulfill(status=409, json={"error": "discrepancy_open", "message": DISCREPANCY}))
    if close_form(page).count() == 0:
        slot(page).get_by_role("button", name=CLOSE, exact=True).click()
        close_form(page).get_by_label(NOTE, exact=True).fill("Closing after the receipt.")
    close_form(page).get_by_role("button", name=CLOSE, exact=True).click()
    expect(panel(page).get_by_role("alert").filter(has_text=DISCREPANCY)).to_have_count(1)
    expect(close_form(page)).to_have_count(0)
    page.unroute(path)
    assert case_json(page, story["case_id"])["state"] == "EXECUTED", "nothing was closed"


def test_verify_and_archive_never_scroll_sideways(visitor):
    """06.1 page rule: no sideways page scroll at 360, 390, 768 and 1280px, with the banner and the three boxes shown."""
    page = visitor.page
    story, _, _ = executed_story(page, missing=6)
    for step in ("verify", "archive"):
        for width in (360, 390, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            open_at(page, story["case_id"], step)
            if step == "verify":
                expect(panel(page).get_by_test_id("verify-receipt")).to_contain_text("Missing")
            else:
                expect(panel(page).get_by_test_id("full-history").locator("ol > li").first).to_be_visible()
            scroll = page.evaluate("() => document.documentElement.scrollWidth")
            assert scroll <= width, f"{step} at {width}px is {scroll}px wide"


# --- a successful Try again finishes the close like the first press (quick 261017-wzs finding 4) ---------------------

def test_a_close_that_succeeds_on_try_again_moves_to_archive_with_the_closing_line(visitor):
    page = visitor.page
    story, d, _ = executed_story(page)
    open_at(page, story["case_id"], "verify")
    busy = {"error": "db_busy", "message": "The server is busy. Try again in a moment."}
    seen = {"n": 0}

    def handle(route):
        seen["n"] += 1
        if seen["n"] == 1:
            route.fulfill(status=503, json=busy)
        else:
            route.fallback()

    page.route(f"**/api/cases/{urllib.parse.quote(story['case_id'], safe='')}/close", handle)
    slot(page).get_by_role("button", name=CLOSE, exact=True).click()
    note = "Closed after the receipt; nothing left to do."
    close_form(page).get_by_label(NOTE, exact=True).fill(note)
    close_form(page).get_by_role("button", name=CLOSE, exact=True).click()
    expect(close_form(page).get_by_text(busy["message"], exact=True)).to_be_visible()
    close_form(page).get_by_role("button", name="Try again").click()

    expect(close_form(page)).to_have_count(0)
    expect(step_button(page, 9, "Archive")).to_have_attribute("aria-current", "step")
    expect(panel(page)).to_have_attribute("data-step", "ARCHIVE")
    expect(panel(page).get_by_test_id("closing-line")).to_have_text(f"Closed · {note}")
    after = case_json(page, story["case_id"])
    assert (after["state"], after["step"]) == ("CLOSED", "ARCHIVE"), after["state"]
    assert seen["n"] == 2
