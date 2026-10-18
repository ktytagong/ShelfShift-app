"""Report an observation (07-20; 07-CONTEXT D-14, D-15, D-16, D-20; 07-UI-SPEC surfaces 4 and 5): a store manager files
a field report by hand in the sheet, sees its potential impact before submitting, and its owner confirms it on the
report's detail. Every action is a press found by role and name and every field is found by its label; the API is read
only to learn the story's codes and the scenario date, and (for the allowance states) a workspace's own usage row is
set the way a day of reports would leave it.

The store app's terms apply (07-SCREENS-MAP G3, G4): "stock decision" on screen for a case, "(scenario date)" on every
date field."""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_id_by_tag, case_json, session_json, switch_seat

pytestmark = pytest.mark.web

STORY = "milk-story"  # the template's story transfer (03-CONTEXT D-23)
SHEET = "Report an observation"
PREVIEW_LABEL = "Potential impact · unconfirmed"
ALLOWANCE = re.compile(r"^(\d+) of (\d+) reports left today · (\d+) with a file$")


def scenario_iso(page: Page) -> str:
    """The workspace's scenario date (the session calendar's entry for its day), as the date inputs take it."""
    s = session_json(page)
    return next(c["date"] for c in s["calendar"] if c["day"] == s["day"])


def story(page: Page) -> dict:
    return case_json(page, case_id_by_tag(page, STORY))


def sheet(page: Page):
    return page.get_by_role("dialog", name=SHEET)


def open_sheet(page: Page):
    page.goto("/#/events")
    expect(page.get_by_role("heading", level=1, name="Business events")).to_be_visible()
    page.get_by_role("button", name=SHEET, exact=True).first.click()
    dialog = sheet(page)
    expect(dialog).to_be_visible()
    return dialog


def allowance_left(dialog) -> int:
    line = dialog.get_by_test_id("allowance")
    expect(line).to_have_text(ALLOWANCE)
    return int(ALLOWANCE.match(line.inner_text().strip()).group(1))


def fill_capacity(page: Page, dialog, zone: str, totes: str) -> None:
    day = scenario_iso(page)
    dialog.get_by_role("radio", name="Storage capacity", exact=True).check()
    dialog.get_by_label(re.compile(r"^Zone")).select_option(zone)
    dialog.get_by_label(re.compile(r"^Available totes")).fill(totes)
    dialog.get_by_label(re.compile(r"^Valid from")).fill(day)
    dialog.get_by_label(re.compile(r"^Valid to")).fill(day)


def test_file_preview_and_confirm_a_report(visitor):
    """The tracer (07-20 Task 1): as the story's destination manager, Business events → Report an observation →
    Storage capacity: the store is the signed-in store, read-only; the dates are scenario dates. Preview impact shows
    "Potential impact · unconfirmed" with the story's row and "Nothing changes until the {dest} manager confirms this
    report."; Submit report closes the sheet and the new report heads the list, "Awaiting confirmation by {dest}
    manager"; the allowance reads one fewer. The owner (the same store: self-confirmation, D-15) opens Review report and
    confirms it as a reported fact: the fact label "Reported by {dest} manager · {t} · field report" and "Re-planned
    {N} stock decisions"."""
    page = visitor.page
    d = story(page)
    dest, zone = d["dest"], d["zone"]
    switch_seat(page, dest)

    dialog = open_sheet(page)
    left = allowance_left(dialog)
    fill_capacity(page, dialog, zone, "0")
    store = dialog.get_by_label("Store", exact=True)
    expect(store).to_have_value(dest)
    expect(store).to_have_attribute("readonly", "")
    expect(dialog.get_by_text("(scenario date)").first).to_be_visible()

    dialog.get_by_role("button", name="Preview impact", exact=True).click()
    panel = dialog.get_by_test_id("preview")
    expect(panel.get_by_text(PREVIEW_LABEL, exact=True)).to_be_visible()
    expect(panel.get_by_role("link", name=d["key"], exact=True)).to_be_visible()
    expect(panel).to_contain_text(f"Nothing changes until the {dest} manager confirms this report.")

    dialog.get_by_role("button", name="Submit report", exact=True).click()
    expect(dialog).to_have_count(0)
    first = page.get_by_test_id("report-row").first
    expect(first).to_contain_text("Storage capacity")
    expect(first).to_contain_text(f"Awaiting confirmation by {dest} manager")

    dialog = open_sheet(page)
    assert allowance_left(dialog) == left - 1
    dialog.get_by_role("button", name="Close without submitting", exact=True).click()
    expect(dialog).to_have_count(0)

    page.get_by_test_id("report-row").first.get_by_role("link", name="Review report", exact=True).click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Storage capacity")
    page.get_by_role("button", name="Confirm as reported fact", exact=True).click()
    done = page.get_by_test_id("confirmed")
    expect(done).to_contain_text(re.compile(rf"Reported by {re.escape(dest)} manager · .+ · field report"))
    expect(done).to_contain_text(re.compile(r"Re-planned \d+ stock decisions?"))
    assert story(page)["rec_version"] != d["rec_version"]  # the fact re-planned the story


# --- Task 2: every kind's fields, the check time, holds, the preview's texts, the dirty close, the allowance ---------

CHECK_MISSING = "Choose when you checked: Checked just now, or a date and time."
INCOMPLETE = "Complete report details to preview impact."
REVIEW_ONLY = "Requires human review · engine impact not calculated"


def label(dialog, text: str):
    """A field by the start of its label (the label adds "(scenario date)" and "(required)")."""
    return dialog.get_by_label(re.compile("^" + re.escape(text)))


def use_up(page: Page, kind: str, left: int = 0) -> None:
    """Set the workspace's own usage row for today the way a day of reports leaves it: `left` of `kind` remain
    (caps.workspace, scope run:<run_id>, D-20). Only this visitor's workspace row is written."""
    import sqlalchemy as sa
    from api import caps, config
    from tests.web.conftest import db_tx, run_id

    limit, window = config.WORKSPACE_CAPS[kind]
    with db_tx() as conn:
        conn.execute(sa.text(
            "INSERT INTO usage_counts (scope, kind, window_start, count) VALUES (:s, :k, :w, :n) "
            "ON CONFLICT (scope, kind, window_start) DO UPDATE SET count = :n"),
            {"s": f"run:{run_id(page)}", "k": kind, "w": caps.window_start(window), "n": limit - left})


def test_transport_as_hq_and_a_quotes_fields(visitor):
    """HQ sees only the Transport tile (spec §9); a price quote asks for the amount, the pricing basis, whether
    handling is included and the dates, each required one marked "(required)"; a delay asks for its hours."""
    page = visitor.page
    switch_seat(page, None)
    dialog = open_sheet(page)
    expect(dialog.get_by_role("radio")).to_have_count(1)
    expect(dialog.get_by_role("radio", name="Transport", exact=True)).to_be_checked()
    expect(dialog.get_by_label("Store", exact=True)).to_have_count(0)  # HQ has no store of its own
    dialog.get_by_label("What happened (required)", exact=True).select_option("QUOTE")
    for name in ("Scheduled truck run (required)", "Quote amount (required)", "Pricing basis (required)",
                 "Includes handling (required)", "Valid until (scenario date) (required)",
                 "Applies on (scenario date) (required)"):
        expect(dialog.get_by_label(name, exact=True)).to_be_visible()
    dialog.get_by_label("What happened (required)", exact=True).select_option("DELAY")
    expect(dialog.get_by_label("Delay (hours) (required)", exact=True)).to_be_visible()
    expect(dialog.get_by_label(re.compile("^Quote amount"))).to_have_count(0)


def test_counts_and_printed_dates_ask_when_they_were_checked(visitor):
    """D-22: a stock count and a printed expiry date take "When did you check?" with no default; Submit without it
    shows the reason under the legend and sends nothing."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["source"])
    dialog = open_sheet(page)
    left = allowance_left(dialog)
    dialog.get_by_role("radio", name="Stock count", exact=True).check()
    label(dialog, "Lot").fill(d["lot_id"])
    label(dialog, "Shelf").fill("3")
    label(dialog, "Backroom").fill("1")
    when = dialog.get_by_role("group", name="When did you check?")
    expect(when.get_by_role("radio", name="Checked just now")).not_to_be_checked()
    dialog.get_by_role("button", name="Submit report", exact=True).click()
    expect(when.get_by_text(CHECK_MISSING, exact=True)).to_be_visible()
    expect(dialog).to_be_visible()
    dialog.get_by_role("radio", name="Printed expiry date", exact=True).check()
    expect(dialog.get_by_role("group", name="When did you check?")).to_be_visible()
    expect(dialog.get_by_label("Mixed dates in this lot", exact=True)).to_be_visible()
    assert allowance_left(dialog) == left  # nothing was sent


def test_a_damage_report_holds_the_lot_through_its_confirm_step(visitor):
    """D-13: a damage condition on one's own lot offers "Hold this lot for review" with its helper; submitting with it
    ticked asks "Place hold" / "Don't hold lot" first, and Place hold files the report and holds the lot. A display
    issue says it creates an advisory task, no hold and no commitment block."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["source"])
    dialog = open_sheet(page)
    dialog.get_by_role("radio", name="Condition", exact=True).check()
    label(dialog, "Lot").fill(d["lot_id"])
    dialog.get_by_label("Condition (required)", exact=True).select_option("DISPLAY")
    expect(dialog.get_by_text("Creates an advisory task. No hold, no commitment block.", exact=True)).to_be_visible()
    expect(dialog.get_by_label("Hold this lot for review")).to_have_count(0)
    expect(dialog.get_by_test_id("preview")).to_contain_text(REVIEW_ONLY)
    expect(dialog.get_by_role("button", name="Preview impact")).to_have_count(0)
    dialog.get_by_label("Condition (required)", exact=True).select_option("DAMAGE")
    expect(dialog.get_by_text(re.compile(r"^Places a lot hold now\."))).to_be_visible()
    dialog.get_by_label("Hold this lot for review", exact=True).check()
    dialog.get_by_role("button", name="Submit report", exact=True).click()
    expect(dialog.get_by_text(re.compile(rf"^Hold lot {re.escape(d['lot_id'])}\?"))).to_be_visible()
    dialog.get_by_role("button", name="Don't hold lot", exact=True).click()
    expect(dialog.get_by_label("Hold this lot for review", exact=True)).not_to_be_checked()
    dialog.get_by_label("Hold this lot for review", exact=True).check()
    dialog.get_by_role("button", name="Submit report", exact=True).click()
    dialog.get_by_role("button", name="Place hold", exact=True).click()
    expect(dialog).to_have_count(0)
    lot = page.request.get(f"/api/lots/{d['lot_id']}").json()
    assert [h["state"] for h in lot["holds"] if h["kind"] == "CONDITION"] == ["ACTIVE"], lot["holds"]


def test_the_previews_texts_and_the_dirty_close(visitor):
    """D-16 and E8: with blank required fields the preview reads "Complete report details to preview impact." and
    its button is disabled; after a preview an edit dims it with "Details changed · preview again". Closing a sheet
    with something typed asks "Discard this report?" (Keep editing stays, Discard report closes)."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    dialog = open_sheet(page)
    dialog.get_by_role("radio", name="Storage capacity", exact=True).check()
    panel = dialog.get_by_test_id("preview")
    expect(panel.get_by_text(INCOMPLETE, exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Preview impact", exact=True)).to_be_disabled()
    fill_capacity(page, dialog, d["zone"], "4")
    dialog.get_by_role("button", name="Preview impact", exact=True).click()
    expect(panel.get_by_text(PREVIEW_LABEL, exact=True)).to_be_visible()
    label(dialog, "Available totes").fill("5")
    expect(panel.get_by_text("Details changed · preview again", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog.get_by_text("Discard this report? What you typed is lost.", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Keep editing", exact=True).click()
    expect(label(dialog, "Available totes")).to_have_value("5")
    dialog.get_by_role("button", name="Close without submitting", exact=True).click()
    dialog.get_by_role("button", name="Discard report", exact=True).click()
    expect(dialog).to_have_count(0)


def test_the_allowance_states(visitor):
    """D-20, FLOW-11: none left disables Submit with "0 of {limit} reports left today for this workspace · resets at
    00:00 Bangkok time"; no file left disables the file control with its text; a cap refusal arriving on Submit shows
    the server's words with no Try again. Only the workspace's own numbers appear."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    limit = int(ALLOWANCE.match(open_sheet(page).get_by_test_id("allowance").inner_text().strip()).group(2))
    page.keyboard.press("Escape")
    use_up(page, "upload")
    dialog = open_sheet(page)
    dialog.get_by_role("radio", name="Storage capacity", exact=True).check()
    expect(dialog.get_by_role("button", name="Add file")).to_be_disabled()
    expect(dialog.get_by_text("No file attachments left today · you can still send the report without a file",
                              exact=True)).to_be_visible()
    fill_capacity(page, dialog, d["zone"], "2")
    use_up(page, "signal")  # spent elsewhere in the workspace while this sheet was open
    dialog.get_by_role("button", name="Submit report", exact=True).click()
    refusal = dialog.get_by_role("alert")
    expect(refusal).to_contain_text("this workspace's")
    expect(refusal.get_by_role("button", name="Try again")).to_have_count(0)
    dialog.get_by_role("button", name="Close without submitting", exact=True).click()
    dialog.get_by_role("button", name="Discard report", exact=True).click()
    dialog = open_sheet(page)
    expect(dialog.get_by_test_id("allowance")).to_have_text(
        f"0 of {limit} reports left today for this workspace · resets at 00:00 Bangkok time")
    dialog.get_by_role("radio", name="Storage capacity", exact=True).check()
    expect(dialog.get_by_role("button", name="Submit report", exact=True)).to_be_disabled()


def test_long_text_and_a_long_file_name_fit_a_phone(visitor):
    """E8 backstop: at 375x812 the sheet is full screen under the top bar, and a 400-character report text and a
    120-character file name wrap or middle-truncate without making the sheet scroll sideways."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    page.set_viewport_size({"width": 375, "height": 812})
    dialog = open_sheet(page)
    dialog.get_by_role("radio", name="Storage capacity", exact=True).check()
    dialog.get_by_label("What happened (optional)", exact=True).fill("Chiller " * 50)
    name = "p" * 116 + ".png"
    dialog.locator("input[type=file]").set_input_files(
        {"name": name, "mimeType": "image/png", "buffer": b"\x89PNG\r\n\x1a\n" + b"0" * 64})
    expect(dialog.get_by_text(re.compile(r"^p+…p+\.png · "))).to_be_visible()
    wide, view = dialog.evaluate("el => [el.scrollWidth, el.clientWidth]")
    assert wide <= view, (wide, view)
    box = dialog.bounding_box()
    assert box is not None and box["width"] == 375, box
