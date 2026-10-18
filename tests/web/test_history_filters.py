"""The history filters keep their entries (06.1 D-50, D-58; UI-SPEC "History"): All · Decisions · Data on the stock
decision's Latest history card and in the lot drawer each show the entries of their group, never an empty list
when the API holds entries of that group.

bars.historyFilter compares the API's own group names ("Decisions", "Data") exactly (06.1-28). A page that renames
the group before filtering (the Latest history card lower-cased it; the lot drawer wrote lower-case names itself)
makes the filter show nothing, and no other browser test pressed those filters.

The story transfer is taken, sent, accepted, approved, prepared and dispatched through the API (test_transfers'
helpers), so its decision holds Decisions entries (ownership, sending) and Data entries (the dispatch), and its
source lot holds a stock decision and a movement. Expected counts come from the API's answers, never typed here.
"""
from __future__ import annotations

import urllib.parse

import pytest
from playwright.sync_api import expect

from tests.web.test_transfers import case_path, dispatch_story, get, prepare_story

pytestmark = pytest.mark.web

LATEST = 3  # the Latest history card shows the three newest entries under the chosen filter (06.1-18)


def press(region, name: str) -> None:
    button = region.get_by_role("group", name="Show", exact=True).get_by_role("button", name=name, exact=True)
    button.click()
    expect(button).to_have_attribute("aria-pressed", "true")


def test_the_latest_history_card_and_the_lot_drawer_keep_their_decisions_and_data_entries(visitor):
    page = visitor.page
    story = prepare_story(page)
    dispatch_story(page, story)

    entries = get(page, case_path(story["case_id"], "activity"))
    groups = {g: sum(1 for e in entries if e["group"] == g) for g in ("Decisions", "Data")}
    assert groups["Decisions"] > 0 and groups["Data"] > 0, groups  # the setup gives both groups entries

    page.goto(f"/#/cases/{urllib.parse.quote(story['key'], safe='')}")
    card = page.get_by_test_id("latest-history")
    expect(card.get_by_role("listitem").first).to_be_visible()
    for name, n in groups.items():
        press(card, name)
        expect(card.get_by_role("listitem")).to_have_count(min(LATEST, n))

    lot_id = story["action"]["lot_id"]
    page.goto(f"/#/stock?lot={urllib.parse.quote(lot_id, safe='')}")
    drawer = page.get_by_role("complementary", name=f"Lot {lot_id}")
    history = drawer.get_by_test_id("lot-history")
    expect(history.get_by_role("listitem").first).to_be_visible()
    lot = get(page, f"/api/lots/{urllib.parse.quote(lot_id, safe='')}")["history"]
    lot_groups = {"Decisions": sum(1 for h in lot if h["type"] == "stock_decision"),
                  "Data": sum(1 for h in lot if h["type"] == "movement")}
    assert lot_groups["Decisions"] > 0 and lot_groups["Data"] > 0, lot_groups
    for name, n in lot_groups.items():
        press(history, name)
        expect(history.get_by_role("listitem")).to_have_count(n)
