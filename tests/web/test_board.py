"""The Decision board (06.1-UI-SPEC "Decision board"; sketch 005 B; D-17, D-23, D-24, D-25, D-54): four stage columns
whose cards sit where the server's `stage` and `step` keys put them, a Board | Table switch, server-side filters and
pages, the card drawer with Take ownership, and the 2 s poll. Every expected value is read from the API through the
page's own context; nothing here derives a step or a stage."""
from __future__ import annotations

import json
import pathlib
import re
import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_json, cards_json, session_json, switch_seat, take_ownership

pytestmark = pytest.mark.web

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "board_public.json"  # contract-shaped public answer (06.1-01 rule)
STORY = "SUS-19"  # the milk story case of the synthetic template (found by its key, as the API names it)


def board_region(page: Page):
    return page.get_by_role("region", name="Decision board", exact=True)


def stage(page: Page, name: str):
    return board_region(page).get_by_role("region", name=name, exact=True)


def decide_group(page: Page, label: str):
    """One Decide sub-group by its header ("1 · Trigger · new", "2 · Context", "4 · Compare"); the count follows it."""
    return stage(page, "Decide").get_by_role("group", name=re.compile("^" + re.escape(label) + r" \d+$"))


def drawer(page: Page):
    """The card drawer (a named complementary panel; the sidebar is an unnamed aside)."""
    return page.locator("aside[role='complementary'][aria-labelledby]")


def open_board(page: Page) -> None:
    page.goto("/#/board")
    expect(board_region(page)).to_be_visible()


def as_story_source(page: Page) -> dict:
    """Sign the visitor in as the story case's source store (the account that may take it, D-67); its card."""
    story = next(c for c in cards_json(page) if c["key"] == STORY)
    if session_json(page).get("account") != story["source"]:
        switch_seat(page, story["source"])
    return story


def test_the_story_case_waits_at_trigger_and_take_ownership_keeps_it_in_decide_at_compare(visitor):
    page = visitor.page
    story = as_story_source(page)
    open_board(page)

    card = decide_group(page, "1 · Trigger · new").get_by_test_id(f"decision-card-{STORY}")
    expect(card).to_be_visible()
    expect(card).to_contain_text("Step 1 of 9 · Trigger")
    expect(card).to_contain_text("Needs action")
    # 06.1 D-69: cards are draggable; a drag requests the next business action
    expect(card).to_have_attribute("draggable", "true")

    card.click()
    expect(drawer(page)).to_contain_text(STORY)
    drawer(page).get_by_role("button", name="Take ownership").click()

    moved = decide_group(page, "4 · Compare").get_by_test_id(f"decision-card-{STORY}")
    expect(moved).to_be_visible()
    expect(moved).to_contain_text("Step 4 of 9 · Compare")
    expect(moved).to_contain_text(f"{story['source']} manager")
    expect(decide_group(page, "1 · Trigger · new").get_by_test_id(f"decision-card-{STORY}")).to_have_count(0)
    after = next(c for c in cards_json(page) if c["key"] == STORY)
    assert (after["stage"], after["step"], after["owner"]["account"]) == ("decide", "COMPARE", story["source"])


def is_board(url: str) -> bool:
    """GET /api/cases itself (any query), never a case detail under it."""
    return urllib.parse.urlparse(url).path == "/api/cases"


def params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


def stage_count(page: Page, name: str):
    return stage(page, name).get_by_test_id("stage-count")


def expect_counts(page: Page, board: dict) -> None:
    """Each column's count is the server's stage_counts under the current filters (Check and close adds Closed)."""
    sc = board["stage_counts"]
    for name, n in (("Decide", sc["decide"]), ("Authorise", sc["authorise"]), ("Do", sc["do"]),
                    ("Check and close", sc["check"] + sc["closed"])):
        expect(stage_count(page, name)).to_have_text(str(n))


def test_the_table_lists_the_same_records_in_the_servers_order(visitor):
    page = visitor.page
    open_board(page)
    keys = [c["key"] for c in cards_json(page)]
    page.get_by_role("group", name="Layout").get_by_role("button", name="Table").click()
    expect(page).to_have_url(re.compile(r"view=table"))
    rows = page.locator("[data-testid^='decision-row-']")
    expect(rows).to_have_count(len(keys))
    assert [r.get_attribute("data-testid").removeprefix("decision-row-") for r in rows.all()] == keys
    rows.first.click()
    expect(drawer(page)).to_contain_text(keys[0])


def test_owner_product_deadline_and_blocked_go_to_the_server_and_the_counts_follow(visitor):
    page = visitor.page
    story = as_story_source(page)
    take_ownership(page, story["case_id"])
    open_board(page)
    every = len(cards_json(page))

    with page.expect_response(lambda r: is_board(r.url) and params(r.url).get("owner") == "me") as got:
        page.get_by_label("Owner", exact=True).select_option("me")
    mine = got.value.json()
    assert STORY in [c["key"] for c in mine["cases"]]
    assert all(c["owner"]["account"] == story["source"] for c in mine["cases"])  # the server filtered, not the page
    expect(board_region(page).get_by_test_id(f"decision-card-{STORY}")).to_be_visible()
    expect_counts(page, mine)
    expect(page.get_by_text(f"Showing {len(mine['cases'])} of {mine['total']}")).to_be_visible()

    with page.expect_request(lambda r: is_board(r.url) and params(r.url).get("product") == story["sku"]):
        page.get_by_label("Product", exact=True).fill(story["sku"])
        page.get_by_label("Product", exact=True).press("Enter")
    with page.expect_request(lambda r: is_board(r.url) and params(r.url).get("deadline") == "scenario_date"):
        page.get_by_label("Deadline", exact=True).select_option("scenario_date")
    page.get_by_label("Deadline", exact=True).select_option("")

    blocked = page.get_by_role("button", name=re.compile(r"^Blocked only \(\d+\)$"))
    with page.expect_response(lambda r: is_board(r.url) and params(r.url).get("blocked") == "1") as got:
        blocked.click()
    expect(blocked).to_have_attribute("aria-pressed", "true")
    if not got.value.json()["cases"]:  # owner=me and blocked=1: the story case is not blocked, so nothing matches
        expect(page.get_by_text("No stock decisions match these filters.")).to_be_visible()

    with page.expect_response(lambda r: is_board(r.url) and not ({"owner", "product", "blocked"} & params(r.url).keys())):
        page.get_by_role("button", name="Clear filters").first.click()
    expect(page.locator("[data-testid^='decision-card-']")).to_have_count(every)
    expect(page.get_by_role("button", name="Clear filters")).to_have_count(0)


def test_filters_that_match_nothing_say_so_with_clear_filters(visitor):
    page = visitor.page
    switch_seat(page, None)  # HQ owns nothing in a fresh workspace, so Owner: Me matches nothing
    open_board(page)
    with page.expect_response(lambda r: is_board(r.url) and params(r.url).get("owner") == "me") as got:
        page.get_by_label("Owner", exact=True).select_option("me")
    assert got.value.json()["cases"] == []
    expect(page.get_by_text("No stock decisions match these filters.")).to_be_visible()
    page.get_by_role("group", name="Layout").get_by_role("button", name="Table").click()
    expect(page.get_by_text("No stock decisions match these filters.")).to_be_visible()
    page.get_by_role("button", name="Clear filters").first.click()
    expect(page.locator("[data-testid^='decision-row-']")).to_have_count(len(cards_json(page)))


def test_hq_alone_gets_branch_and_risk_and_both_go_to_the_server(visitor):
    page = visitor.page
    story = next(c for c in cards_json(page) if c["key"] == STORY)
    open_board(page)
    expect(page.get_by_label("Branch", exact=True)).to_have_count(0)
    expect(page.get_by_label("Risk", exact=True)).to_have_count(0)

    switch_seat(page, None)
    open_board(page)
    with page.expect_request(lambda r: is_board(r.url) and params(r.url).get("branch") == story["source"]):
        page.get_by_label("Branch", exact=True).fill(story["source"])
        page.get_by_label("Branch", exact=True).press("Enter")
    with page.expect_request(lambda r: is_board(r.url) and params(r.url).get("risk") == "waste"):
        page.get_by_label("Risk", exact=True).select_option("waste")


def test_the_evaluated_line_shows_the_servers_counts_and_follows_the_filters(visitor):
    page = visitor.page
    whole = json.loads(FIXTURE.read_text(encoding="utf-8"))
    flags_only = dict(whole, cases=[whole["cases"][1]], total=1, evaluated=0, at_risk=1,
                      stage_counts={"decide": 1, "authorise": 0, "do": 0, "check": 0, "closed": 0})

    def answer(route):
        route.fulfill(json=flags_only if params(route.request.url).get("deadline") == "none" else whole)

    page.route(is_board, answer)
    open_board(page)
    expect(page.get_by_text(f"Evaluated {whole['evaluated']} of {whole['at_risk']} at-risk items")).to_be_visible()
    page.get_by_label("Deadline", exact=True).select_option("none")
    expect(page.get_by_text(f"Evaluated {flags_only['evaluated']} of {flags_only['at_risk']} at-risk items")).to_be_visible()
    # Unevaluated items stay visible as Needs evaluation cards (05.2 D-38).
    expect(board_region(page).get_by_test_id("decision-card-SUS-2")).to_contain_text("Needs evaluation")


LONG_WHY = ("12 units would expire unsold if nothing changes, because the store's markdown schedule starts too late for "
            "this lot and no truck run before its expiry has space for the chilled totes it would need")


def public_board(page: Page, edit=None) -> dict:
    """Answer GET /api/cases from the contract-shaped public fixture (06.1-01 rule), optionally edited."""
    board = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if edit is not None:
        edit(board)
    page.route(is_board, lambda route: route.fulfill(json=board))
    return board


def test_a_card_shows_two_badges_and_names_the_rest_behind_plus_n(visitor):
    page = visitor.page

    def three_badges(b):
        b["cases"][0]["badges"] = [{"kind": "missing_data", "text": "Missing data"},
                                   {"kind": "evidence_requested", "text": "Evidence requested"},
                                   {"kind": "hold_expiring", "text": "Held until 14:35", "minutes_left": 30}]
    public_board(page, three_badges)
    open_board(page)
    card = board_region(page).get_by_test_id("decision-card-SUS-1")
    expect(card).to_contain_text("Missing data")
    expect(card).to_contain_text("Stock check requested")
    expect(card).not_to_contain_text("Held until 14:35")
    expect(card.get_by_role("img", name=re.compile(r"Held until 14:35"))).to_have_text("+1")


def test_a_needs_evaluation_card_shows_only_the_screens_figures(visitor):
    page = visitor.page
    board = public_board(page)
    open_board(page)
    flag = board["cases"][1]
    card = board_region(page).get_by_test_id(f"decision-card-{flag['key']}")
    expect(card).to_contain_text("Needs evaluation")
    expect(card).to_contain_text(f"{flag['risk']['keep_waste']} units")  # the screen's figure, nothing else
    for absent in ("Owner", "Expected", "Decide by", "No deadline", "Next", "vs keep", "Not taken yet"):
        expect(card).not_to_contain_text(absent)


def test_the_story_drawer_lists_the_nine_steps_and_what_the_morning_pass_checked(visitor):
    page = visitor.page
    open_board(page)
    detail = case_json(page, next(c for c in cards_json(page) if c["key"] == STORY)["case_id"])
    board_region(page).get_by_test_id(f"decision-card-{STORY}").click()
    progress = drawer(page).get_by_role("list", name="Progress")
    expect(progress.get_by_role("listitem")).to_have_count(9)
    current = progress.locator("[aria-current='step']")
    expect(current).to_have_count(1)
    expect(current).to_contain_text("current step")
    expect(current.locator(".sr-only")).to_have_text(re.compile("current step"))
    result = detail["result"]
    expect(drawer(page).get_by_test_id("drawer-checked")).to_contain_text(f"{result['candidates_checked']} plans checked")


def test_a_long_why_is_one_clamped_line_on_the_card_and_whole_in_the_drawer(visitor):
    page = visitor.page

    def long_reason(b):
        b["cases"][0]["reason"] = LONG_WHY
    public_board(page, long_reason)
    page.set_viewport_size({"width": 1280, "height": 800})
    open_board(page)
    card = board_region(page).get_by_test_id("decision-card-SUS-1")
    why = card.get_by_test_id("card-why")
    expect(why).to_have_text(LONG_WHY)
    assert why.evaluate("e => e.scrollWidth > e.clientWidth && getComputedStyle(e).textOverflow === 'ellipsis'")
    card.click()
    expect(drawer(page)).to_contain_text(LONG_WHY)


@pytest.mark.parametrize("width", [768, 1280])
def test_no_sideways_page_scroll_on_the_board_or_the_table(visitor, width):
    page = visitor.page
    page.set_viewport_size({"width": width, "height": 900})
    for view in ("/#/board", "/#/board?view=table"):
        page.goto(view)
        expect(board_region(page).or_(page.locator("[data-testid^='decision-row-']").first)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (width, view)


def test_a_poll_sent_before_a_write_never_replaces_the_board_read_after_it(visitor):
    """261017-wzs Codex finding 8: a poll that left before Take ownership and answers after the board read that followed
    it must not put the card back at Trigger (pages/board.tsx load: only the latest load's answer is applied)."""
    page = visitor.page
    as_story_source(page)
    open_board(page)
    trigger = decide_group(page, "1 · Trigger · new").get_by_test_id(f"decision-card-{STORY}")
    compare = decide_group(page, "4 · Compare").get_by_test_id(f"decision-card-{STORY}")
    expect(trigger).to_be_visible()

    held = []

    def hold_the_next_poll(route):
        if held:
            route.continue_()  # every later board read goes through
        else:
            held.append((route, route.fetch()))  # the unowned answer, kept back; useBoardPoll skips ticks meanwhile

    page.route(is_board, hold_the_next_poll)
    for _ in range(50):
        if held:
            break
        page.wait_for_timeout(100)
    assert held, "no board poll was held"

    trigger.click()
    drawer(page).get_by_role("button", name="Take ownership").click()
    expect(compare).to_be_visible()

    route, stale = held[0]
    with page.expect_response(lambda r: is_board(r.url)) as released:
        route.fulfill(response=stale)
    released.value.finished()
    page.evaluate("() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")
    # Counted at once, never retried: a later poll would hide a stale answer within 2 s.
    assert compare.count() == 1
    assert trigger.count() == 0
    expect(drawer(page).get_by_role("button", name="Take ownership")).to_have_count(0)


def test_a_failed_poll_keeps_the_cards_and_shows_the_stale_banner(visitor):
    page = visitor.page
    open_board(page)
    card = board_region(page).get_by_test_id(f"decision-card-{STORY}")
    expect(card).to_be_visible()
    page.route(is_board, lambda route: route.abort())
    expect(page.get_by_text("Couldn't refresh. Showing what was loaded before.")).to_be_visible()
    expect(page.get_by_role("button", name="Try again")).to_be_visible()
    expect(card).to_be_visible()


# --- Phase 7 badges, the owner line, the Blocked filter and a closed lot still at risk (07-22) -------------------------

def test_a_held_card_shows_its_badges_in_priority_and_who_resolves_it(visitor):
    """07-UI-SPEC "Board badges" and surface 2: with a Mixed dates hold (its report stays unconfirmed) and a Required
    check on the story case, the card shows "Lot hold" and "Required check" first, then "+N" whose accessible name
    lists the rest (the new report among them); its Next line reads "Resolve hold" and the hold's owner; the
    Blocked filter (the server's) counts and lists it."""
    from tests.api.conftest import fresh
    from tests.api.verify_helpers import required_fixture
    from tests.web.conftest import run_id
    from tests.web.test_banners import mixed_hold
    page = visitor.page
    story = as_story_source(page)
    required_fixture(fresh(run_id(page)))
    mixed_hold(page, case_json(page, story["case_id"]))
    open_board(page)
    card = board_region(page).get_by_test_id(f"decision-card-{STORY}")
    pills = card.locator("span", has_text=re.compile(r"^(Lot hold|Required check)$"))
    expect(pills.first).to_have_text("Lot hold")
    expect(card).to_contain_text("Required check")
    more = card.get_by_role("img", name=re.compile(r"^\+\d+: "))
    expect(more).to_have_attribute("aria-label", re.compile(r"New report"))
    expect(card).to_contain_text("Resolve hold")
    expect(card).to_contain_text(f"{story['source']} manager")
    blocked = page.get_by_role("button", name=re.compile(r"^Blocked only \(\d+\)$"))
    with page.expect_response(lambda r: is_board(r.url) and params(r.url).get("blocked") == "1") as got:
        blocked.click()
    assert STORY in [c["key"] for c in got.value.json()["cases"]]
    assert got.value.json()["blocked_total"] >= 1
    expect(board_region(page).get_by_test_id(f"decision-card-{STORY}")).to_be_visible()


def test_a_rejected_lot_still_at_risk_reads_needs_decision_on_its_card(visitor):
    """D-24: rejecting never hides at-risk stock: the closed card reads "Needs decision"."""
    from tests.web.test_banners import post
    from tests.web.test_transfers import case_path
    page = visitor.page
    story = as_story_source(page)
    take_ownership(page, story["case_id"])
    post(page, case_path(story["case_id"], "reject"), {"reason": "Not this week"})
    open_board(page)
    expect(board_region(page).get_by_test_id(f"decision-card-{STORY}")).to_contain_text("Needs decision")
