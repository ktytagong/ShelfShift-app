"""PROD-06 (Archive) / D-39, D-50, D-58: the stock decision's full history at 9 · Archive, with the All · Decisions · Data
filter on the API's own groups, both clocks on every entry, details in words with 12-character hashes, and "Export stock
decision", which saves the API's export unchanged (06.1-UI-SPEC "Step panel" row 9, "History"; contract Export and
GET /api/cases/{id}/activity).

Rewritten by 06.1-28 for the stock decision page (the Phase 5 case page's More actions menu and Activity tab are gone).
Every expectation is read from the API (the case detail, its export and its activity log), never pasted (04 D-14): the
saved file is compared byte for byte with the API's own export, and the listed entries are counted against the API's
array, group by group, in its order. A decision opens at `#/cases/{id}?step=archive` directly.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_id_by_tag, case_json, take_ownership
from tests.web.test_transfers import case_path, get, post

pytestmark = pytest.mark.web

STORY = "milk-story"  # SUS-19, the story transfer, OPEN at Trigger in the template; its source is the default account
RECORDED = re.compile(r"^Recorded (\d{1,2} [A-Z][a-z]{2} \d{4}, \d{2}:\d{2}) .+ time · Scenario date: [A-Z][a-z]{2} ")


def activity_json(page: Page, case_id: str) -> list[dict]:
    return get(page, case_path(case_id, "activity"))


def export_bytes(page: Page, case_id: str) -> bytes:
    """GET /api/cases/{id}/export through the page's own context: the API's file as it sends it."""
    r = page.request.get(case_path(case_id, "export"))
    assert r.status == 200, r.status
    return r.body()


def open_archive(page: Page, case_id: str) -> None:
    page.goto("/#/cases/" + urllib.parse.quote(case_id, safe="") + "?step=archive")
    expect(panel(page)).to_have_attribute("data-step", "ARCHIVE")


def panel(page: Page):
    return page.locator("main#content").get_by_test_id("step-panel")


def history(page: Page):
    return panel(page).get_by_test_id("full-history")


def items(page: Page):
    return history(page).locator("ol > li")


def choose(page: Page, name: str) -> None:
    group = history(page).get_by_role("group", name="Show")
    group.get_by_role("button", name=name, exact=True).click()
    expect(group.get_by_role("button", name=name, exact=True)).to_have_attribute("aria-pressed", "true")


def recorded_at(meta: str) -> dt.datetime:
    m = RECORDED.match(meta)
    assert m, meta
    return dt.datetime.strptime(m.group(1), "%d %b %Y, %H:%M")


# --- the full history and its filter ----------------------------------------------------------------------------------

def test_archive_filters_the_full_history_by_the_apis_groups(visitor):
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    before = activity_json(page, cid)
    assert before and not [e for e in before if e["group"] == "Decisions"], "a new decision has no decisions yet"
    open_archive(page, cid)
    expect(items(page)).to_have_count(len(before))

    # An empty filter reads its empty line (05 copy with the D-61 term).
    choose(page, "Decisions")
    expect(items(page)).to_have_count(0)
    expect(history(page).get_by_text("No Decisions entries for this stock decision yet.", exact=True)).to_be_visible()

    # Taking ownership is a decision (06.1-13): the Decisions filter keeps it; Data keeps the data entries.
    d = case_json(page, cid)
    take_ownership(page, cid)
    after = activity_json(page, cid)
    page.reload()
    expect(panel(page)).to_have_attribute("data-step", "ARCHIVE")
    groups = {g: [e for e in after if e["group"] == g] for g in ("Decisions", "Data")}
    assert groups["Decisions"] and groups["Data"], [(e["type"], e["group"]) for e in after]
    choose(page, "Decisions")
    expect(items(page)).to_have_count(len(groups["Decisions"]))
    expect(items(page).last.locator("p").first).to_have_text(f"Ownership taken by {d['source']}")
    choose(page, "Data")
    expect(items(page)).to_have_count(len(groups["Data"]))
    expect(history(page).get_by_text(f"Ownership taken by {d['source']}", exact=True)).to_have_count(0)

    # All: every entry, oldest first by recorded time (the newest, the ownership, last), each with both clocks.
    choose(page, "All")
    expect(items(page)).to_have_count(len(after))
    expect(items(page).last.locator("p").first).to_have_text(f"Ownership taken by {d['source']}")
    assert all(e["scenario_date"] for e in after), "every entry carries its scenario date"
    metas = [items(page).nth(i).locator("p").nth(1).inner_text() for i in range(len(after))]
    times = [recorded_at(m) for m in metas]
    assert times == sorted(times), metas


# --- export ------------------------------------------------------------------------------------------------------------

def test_export_saves_the_stock_decision_file_unchanged(visitor):
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    d = case_json(page, cid)
    open_archive(page, cid)
    button = panel(page).get_by_role("button", name="Export stock decision", exact=True)
    with page.expect_download() as download:
        button.click()
    saved = download.value
    assert saved.suggested_filename == f"{d['key']}.json", saved.suggested_filename
    data = pathlib.Path(saved.path()).read_bytes()
    assert data == export_bytes(page, cid), "the saved file is the API's export, unchanged"
    expect(panel(page).get_by_role("alert")).to_have_count(0)


# --- long details and hashes (backstop) ---------------------------------------------------------------------------------

def test_long_details_wrap_in_full_and_hashes_show_twelve_characters(visitor):
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    d = case_json(page, cid)
    version = d["rec_version"]
    take_ownership(page, cid)
    post(page, case_path(cid, "send"), {"rec_version": version})
    reason = ("The destination's chilled room is being refitted this week, so it cannot take the transfer; reference "
              "REFIT-2026-10-CHILLED-ROOM-B-0000111122223333444455556666777788889999 in the maintenance log.")
    post(page, case_path(cid, "decision"), {"rec_version": version, "decision": "reject", "reason": reason})

    page.set_viewport_size({"width": 360, "height": 800})
    open_archive(page, cid)
    expect(items(page)).to_have_count(len(activity_json(page, cid)))
    text = history(page).inner_text()
    assert reason in text.replace("\n", " ") or reason in text, "the reason shows in full"
    assert version not in text, "a 64-character hash never shows"
    short = panel(page).locator(".font-mono", has_text=re.compile(rf"^{version[:12]}$"))
    expect(short.first).to_be_visible()
    width = page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 360, f"the page scrolls sideways at 360px ({width}px)"
