"""Business events and the report detail (07-20 Task 3; 07-CONTEXT D-04, D-13, D-15, D-16, D-17; 07-UI-SPEC surface 4,
the Report detail, E7 and E9): the owner corrects, confirms or dismisses a report; the reporter withdraws it before
confirmation; a confirmed report offers "Report a correction"; a quote with handling Unknown can't be confirmed yet.
The timeline opens on "Needs my confirmation (N)" when N > 0, its filters are the server's query, and its empty
states are the UI-SPEC's. Report an observation is reachable from a stock decision and from a Stock & lots row, with
the store, product and lot pre-filled (07-SCREENS-MAP 07-20 T2: the store app has no ⋯ menu, so the stock decision's
entry is a button in its Context step's header).

Reports a test needs before it acts in the browser are filed through the API in the page's own context (the same
routes the sheet uses: test_report_sheet.py drives the sheet itself)."""
from __future__ import annotations

import json
import re
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_json, switch_seat
from tests.web.test_report_sheet import scenario_iso, sheet, story

pytestmark = pytest.mark.web

SHEET = "Report an observation"


def file_report(page: Page, kind: str, fields: dict, **extra) -> dict:
    """POST /api/signals as the page's own account (multipart, a fresh Idempotency-Key); the Report."""
    parts = {"kind": kind, "fields": json.dumps(fields), "text": extra.pop("text", ""), **extra}
    r = page.request.post("/api/signals", multipart=parts, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status == 200, r.text()
    return r.json()


def report_json(page: Page, signal_id: str) -> dict:
    r = page.request.get("/api/signals/" + urllib.parse.quote(signal_id, safe=""))
    assert r.status == 200, r.status
    return r.json()


def capacity(page: Page, d: dict, totes: int) -> dict:
    day = scenario_iso(page)
    return file_report(page, "CAPACITY", {"zone": d["zone"], "free_totes": totes, "valid_from": day, "valid_to": day})


def open_report(page: Page, rep: dict) -> None:
    page.goto("/#/events/" + urllib.parse.quote(rep["report_id"], safe=""))
    expect(page.get_by_role("heading", level=1)).to_be_visible()


def test_the_owner_corrects_then_confirms_the_new_revision(visitor):
    """D-15: Correct fields makes the fields editable; Save corrections makes a new revision and Confirm as reported
    fact confirms that revision (its value is the corrected one)."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    rep = capacity(page, d, 3)
    open_report(page, rep)
    page.get_by_role("button", name="Correct fields", exact=True).click()
    page.get_by_label(re.compile(r"^Available totes")).fill("7")
    page.get_by_role("button", name="Save corrections", exact=True).click()
    expect(page.get_by_role("button", name="Correct fields", exact=True)).to_be_visible()
    seen = report_json(page, rep["signal_id"])
    assert [x["revision"] for x in seen["revisions"]] == [1, 2]
    page.get_by_role("button", name="Confirm as reported fact", exact=True).click()
    expect(page.get_by_test_id("confirmed")).to_contain_text("field report")
    last = report_json(page, rep["signal_id"])["revisions"][-1]
    assert (last["revision"], last["state"], last["fields"]["free_totes"]) == (2, "CONFIRMED", 7)


def test_dismiss_and_withdraw_go_through_their_confirm_steps(visitor):
    """D-15: the owner's Dismiss asks first (Dismiss report / Keep report) and the row reads "Dismissed by {store}
    manager · {t}"; the reporter's Withdraw asks first and reads "Withdrawn by reporter · {t}"."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    one, two = capacity(page, d, 2), capacity(page, d, 1)
    open_report(page, one)
    page.get_by_role("button", name="Dismiss", exact=True).click()
    expect(page.get_by_text("Dismiss this report? It stays in the history and is never applied.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Keep report", exact=True).click()
    page.get_by_role("button", name="Dismiss", exact=True).click()
    page.get_by_role("button", name="Dismiss report", exact=True).click()
    expect(page.get_by_text(re.compile(rf"^Dismissed by {re.escape(d['dest'])} manager · \d"))).to_be_visible()
    expect(page.get_by_role("button", name="Withdraw", exact=True)).to_have_count(0)
    open_report(page, two)
    page.get_by_role("button", name="Withdraw", exact=True).click()
    page.get_by_role("button", name="Withdraw report", exact=True).click()
    expect(page.get_by_text(re.compile(r"^Withdrawn by reporter · \d"))).to_be_visible()


def test_a_confirmed_report_offers_a_correction_and_no_withdraw(visitor):
    """D-15: after confirmation there is no Withdraw; "Report a correction" opens the sheet with the same kind and
    fields pre-filled."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    rep = capacity(page, d, 4)
    open_report(page, rep)
    page.get_by_role("button", name="Confirm as reported fact", exact=True).click()
    expect(page.get_by_test_id("confirmed")).to_contain_text(re.compile(r"Re-planned \d+ stock decisions?"))
    expect(page.get_by_role("button", name="Withdraw", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Report a correction", exact=True).click()
    dialog = sheet(page)
    expect(dialog.get_by_role("radio", name="Storage capacity", exact=True)).to_be_checked()
    expect(dialog.get_by_label(re.compile(r"^Available totes"))).to_have_value("4")


def test_a_quote_with_handling_unknown_cant_be_confirmed_yet(visitor):
    """07-UI-SPEC Actions: HQ's Confirm stays disabled on a transport quote until the pricing basis and handling are
    chosen, with "Choose the pricing basis and handling to confirm." beside it."""
    page = visitor.page
    d = story(page)
    run = (d.get("wait") or {}).get("run_code")
    assert run, "the story decision names no run"
    switch_seat(page, None)
    day = scenario_iso(page)
    rep = file_report(page, "TRANSPORT", {"run_code": run, "what": "QUOTE", "amount": 12.5, "pricing_basis": "PER_TRIP",
                                          "includes_handling": "UNKNOWN", "applies_on": day, "valid_until": day})
    open_report(page, rep)
    expect(page.get_by_role("button", name="Confirm as reported fact", exact=True)).to_be_disabled()
    expect(page.get_by_text("Choose the pricing basis and handling to confirm.", exact=True)).to_be_visible()


def test_the_timeline_view_filters_and_empty_states(visitor):
    """E7 and D-04: with a report waiting for the signed-in store, the page opens on "Needs my confirmation (N)" with
    the row's "Review report"; Kind with nothing to show reads "No reports match these filters." and Clear filters
    brings the list back; Store narrows on the server. A store with nothing to confirm, on "Needs my confirmation",
    reads "Nothing waiting for your confirmation." with "Show all reports". A preloaded report reads "Simulated
    report"; a workspace with no report reads "No field reports yet"."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["dest"])
    preloaded = page.request.get("/api/signals?page_size=50").json()["signals"]
    if not preloaded:
        page.goto("/#/events?view=all")
        expect(page.get_by_text("No field reports yet", exact=True)).to_be_visible()
    rep = capacity(page, d, 5)
    page.goto("/#/events")
    n = page.request.get("/api/signals").json()["needs_my_confirmation"]
    expect(page.get_by_role("button", name=f"Needs my confirmation ({n})", exact=True)).to_have_attribute("aria-pressed", "true")
    row = page.locator(f"[data-report='{rep['report_id']}']")
    expect(row.get_by_role("link", name="Review report", exact=True)).to_be_visible()
    expect(row).to_contain_text(re.compile(r"Affects \d+ stock decisions?"))
    page.get_by_label("Kind", exact=True).select_option("DEMAND_EVENT")
    expect(page.get_by_text("No reports match these filters.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Clear filters", exact=True).first.click()
    expect(row).to_be_visible()
    page.get_by_role("button", name="All", exact=True).click()
    page.get_by_label("Store", exact=True).fill(d["dest"])
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page).to_have_url(re.compile("store=" + re.escape(urllib.parse.quote_plus(d["dest"]))))
    expect(row).to_be_visible()
    simulated = [r for r in preloaded if r["provenance"] == "simulated"]
    if simulated:
        page.goto("/#/events?view=all")
        expect(page.locator(f"[data-report='{simulated[0]['report_id']}']")).to_contain_text("Simulated report")
    switch_seat(page, d["source"])
    page.goto("/#/events?view=mine")
    if page.request.get("/api/signals?mine=1").json()["total"] == 0:
        expect(page.get_by_text("Nothing waiting for your confirmation.", exact=True)).to_be_visible()
        page.get_by_role("link", name="Show all reports", exact=True).click()
        expect(page).to_have_url(re.compile(r"view=all"))


def test_report_an_observation_from_a_stock_decision_and_a_stock_record(visitor):
    """D-04, D-14: the stock decision's Context step and a Stock & lots row open the sheet with the store, the product
    and the lot pre-filled; HQ's Stock & lots offers no store report."""
    page = visitor.page
    d = story(page)
    switch_seat(page, d["source"])
    page.goto("/#/cases/" + urllib.parse.quote(d["case_id"], safe="") + "?step=context")
    page.locator("main#content").get_by_role("button", name=SHEET, exact=True).click()
    dialog = sheet(page)
    dialog.get_by_role("radio", name="Stock count", exact=True).check()
    expect(dialog.get_by_label("Store", exact=True)).to_have_value(d["source"])
    expect(dialog.get_by_label("Product", exact=True)).to_have_value(d["sku"])
    expect(dialog.get_by_label(re.compile(r"^Lot"))).to_have_value(d["lot_id"])
    dialog.get_by_role("button", name="Close without submitting", exact=True).click()
    dialog.get_by_role("button", name="Discard report", exact=True).click()

    page.goto("/#/stock")
    row = page.locator(f"tr[data-lot='{d['lot_id']}']")
    row.get_by_role("button", name=SHEET, exact=True).click()
    dialog = sheet(page)
    dialog.get_by_role("radio", name="Condition", exact=True).check()
    expect(dialog.get_by_label(re.compile(r"^Lot"))).to_have_value(d["lot_id"])
    expect(dialog.get_by_label(re.compile(r"^Lot"))).to_have_attribute("readonly", "")
    expect(dialog.get_by_label("Product", exact=True)).to_have_value(d["sku"])
    dialog.get_by_role("button", name="Close without submitting", exact=True).click()
    dialog.get_by_role("button", name="Discard report", exact=True).click()

    switch_seat(page, None)
    assert case_json(page, d["case_id"])["source"] == d["source"]
    page.goto("/#/stock")
    expect(page.locator("tr[data-lot]").first).to_be_visible()
    expect(page.locator("tr[data-lot]").get_by_role("button", name=SHEET)).to_have_count(0)
