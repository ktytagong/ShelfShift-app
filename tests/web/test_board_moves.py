"""The Decision board's moves against the real API (06.1-CONTEXT D-69, which keeps C-21): a drag or the Move control
requests the stock decision's next business action. Direct writes (Send for approval, Prepare) go through the existing
keyed, versioned endpoints; approvals, stock records and closing open the stock decision's own form in a drawer; a
refusal shows the server's message; a card changes column only when the server's board says so. Nothing here writes
a stage."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import case_json, decision_body, switch_seat, take_ownership
from tests.web.test_case_actions import ACK_TICK
from tests.web.test_board import STORY, as_story_source, drawer, is_board, open_board, stage
from tests.web.test_execute import case_path, condition_of, get, post, prepare_story
from tests.web.test_overflow import WIDTHS
from tests.web.test_tasks import count_fact
from tests.web.test_verify_close import executed_story

pytestmark = pytest.mark.web

# 06.1 D-03, D-14, D-58: no demo words, no relative day and no countdown on screen.
BANNED = re.compile(r"(?i)\b(today|ago|demo|seats?|sandbox(es)?|next day|start here|minutes left|countdown)\b")
FITS = """() => {
  const main = document.querySelector("main");
  return document.documentElement.scrollWidth <= window.innerWidth && (main === null || main.scrollWidth <= main.clientWidth);
}"""


def owned_story(page: Page) -> dict:
    story = as_story_source(page)
    take_ownership(page, story["case_id"])
    open_board(page)
    return story


def card_in(page: Page, name: str, key: str = STORY):
    return stage(page, name).get_by_test_id(f"decision-card-{key}")


def move_select(page: Page, key: str = STORY, where: str | None = None):
    scope = page if where is None else stage(page, where)
    return scope.get_by_label(f"Move {key} to", exact=True)


def move_button(page: Page, key: str = STORY):
    return page.get_by_role("button", name=f"Move {key}", exact=True)


def move(page: Page, target: str, key: str = STORY) -> None:
    """Choose the column in the card's Move select (which requests nothing), then press its Move button."""
    move_select(page, key).select_option(target)
    move_button(page, key).click()


def posts(page: Page) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if r.method == "POST" else None)
    return seen


def screen_words(page: Page) -> str:
    return page.locator("main").inner_text()


def test_drag_saves_the_transition_survives_reload_and_cannot_reverse_approval(visitor):
    page = visitor.page
    story = owned_story(page)
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/send")) as sent:
        card_in(page, "Decide").drag_to(stage(page, "Authorise"), target_position={"x": 30, "y": 20})
    assert sent.value.status == 200
    assert sent.value.request.post_data_json == {"rec_version": story["rec_version"]}
    assert sent.value.request.headers.get("idempotency-key")
    expect(card_in(page, "Authorise")).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "PENDING_APPROVAL"
    page.reload()
    expect(card_in(page, "Authorise")).to_be_visible()

    writes = posts(page)
    card_in(page, "Authorise").drag_to(stage(page, "Decide"), target_position={"x": 30, "y": 20})
    expect(page.get_by_text(re.compile("Recorded approvals and stock movements cannot be undone"))).to_be_visible()
    expect(card_in(page, "Authorise")).to_be_visible()
    assert writes == []


def test_choosing_a_column_writes_nothing_and_move_writes_once_then_focus_follows_the_card(visitor):
    page = visitor.page
    story = owned_story(page)
    writes = posts(page)
    expect(move_button(page)).to_be_disabled()
    move_select(page).select_option("authorise")
    expect(move_button(page)).to_be_enabled()
    move_select(page).select_option("do")
    move_select(page).select_option("authorise")
    page.wait_for_timeout(300)  # a select that wrote on change would have sent by now
    assert writes == []
    assert case_json(page, story["case_id"])["state"] == "OPEN"

    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/send")) as sent:
        move_button(page).click()
    assert sent.value.status == 200
    expect(card_in(page, "Authorise")).to_be_visible()
    expect(move_select(page, where="Authorise")).to_be_focused()
    assert len(writes) == 1 and writes[0].endswith("/send")
    expect(page.get_by_test_id("move-status")).to_have_text(f"Move saved for {STORY}.")


def test_a_keyboard_alone_moves_a_card_and_focus_lands_on_its_move_select(visitor):
    page = visitor.page
    story = owned_story(page)
    writes = posts(page)
    card_in(page, "Decide").focus()
    page.keyboard.press("Tab")
    select = move_select(page)
    expect(select).to_be_focused()
    page.keyboard.type("Au")  # the closed select's type-ahead chooses Authorise and requests nothing
    expect(select).to_have_value("authorise")
    page.keyboard.press("Tab")
    expect(move_button(page)).to_be_focused()
    assert writes == []
    page.keyboard.press("Enter")
    expect(card_in(page, "Authorise")).to_be_visible()
    expect(move_select(page, where="Authorise")).to_be_focused()
    assert case_json(page, story["case_id"])["state"] == "PENDING_APPROVAL"
    assert len(writes) == 1


@pytest.mark.parametrize("width", WIDTHS)
def test_the_board_and_a_guided_move_never_scroll_sideways_or_show_a_banned_word(visitor, width):
    page = visitor.page
    page.set_viewport_size({"width": width, "height": 844})
    as_story_source(page)  # the story is not taken yet: moving it to Authorise opens Take ownership in a drawer
    open_board(page)
    expect(move_select(page)).to_be_visible()
    assert page.evaluate(FITS), (width, "board")
    assert BANNED.search(screen_words(page)) is None, BANNED.search(screen_words(page))

    move(page, "authorise")
    guided = drawer(page).get_by_test_id("guided-move")
    expect(guided).to_be_visible()
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True).first).to_be_visible()
    expect(card_in(page, "Decide")).to_be_visible()
    assert page.evaluate(FITS), (width, "guided drawer")
    assert BANNED.search(screen_words(page)) is None, BANNED.search(screen_words(page))


def test_a_real_409_and_a_403_show_the_servers_words_and_never_move_the_card(visitor):
    page = visitor.page
    story = owned_story(page)

    def wrong_version(route):
        route.continue_(post_data=json.dumps({"rec_version": "not-the-active-version"}))

    page.route("**/send", wrong_version, times=1)
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/send")) as refused:
        move(page, "authorise")
    assert refused.value.status == 409
    stale = refused.value.json()
    assert stale["error"] == "stale_version"
    alert = page.get_by_role("alert").filter(has_text=stale["message"])
    expect(alert).to_be_visible()
    expect(alert.get_by_text(stale["message"], exact=True)).to_be_visible()
    expect(alert.get_by_role("button", name="Reload the stock decision", exact=True)).to_be_visible()
    expect(card_in(page, "Decide")).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "OPEN"
    alert.get_by_role("button", name="Reload the stock decision", exact=True).click()
    expect(alert).to_have_count(0)

    forbidden = {"error": "forbidden", "message": "Only the owner of this stock decision can send it for approval."}
    page.route("**/send", lambda route: route.fulfill(status=403, json=forbidden), times=1)
    move(page, "authorise")
    alert = page.get_by_role("alert").filter(has_text=forbidden["message"])
    expect(alert.get_by_text(forbidden["message"], exact=True)).to_be_visible()
    expect(alert.get_by_role("button")).to_have_count(0)  # a refusal is never resent
    expect(card_in(page, "Decide")).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "OPEN"


def test_a_conflict_lists_its_reasons_word_for_word(visitor):
    page = visitor.page
    story = owned_story(page)
    refusal = {"error": "conflict", "message": "This plan can't go ahead now; see the reasons.",
               "reasons": ["The scheduled truck run has no chilled space left.", "The destination has no shelf space."]}
    page.route("**/send", lambda route: route.fulfill(status=409, json=refusal), times=1)
    move(page, "authorise")
    expect(page.get_by_role("alert").get_by_text(refusal["message"], exact=True)).to_be_visible()
    expect(page.get_by_test_id("move-conflict-reasons").get_by_role("listitem")).to_have_text(refusal["reasons"])
    expect(card_in(page, "Decide")).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "OPEN"


def test_the_guided_drawer_reads_again_when_the_board_shows_its_card_changed(visitor):
    page = visitor.page
    story = as_story_source(page)
    open_board(page)
    move(page, "authorise")
    expect(drawer(page).get_by_test_id("guided-move")).to_be_visible()
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True).first).to_be_visible()
    take_ownership(page, story["case_id"])  # another tab acts; only the board's poll sees it
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True)).to_have_count(0)
    expect(drawer(page).get_by_role("button", name="Send for approval", exact=True)).to_be_visible()


def test_the_guided_drawer_reads_again_when_its_card_leaves_the_boards_results(visitor):
    page = visitor.page
    story = as_story_source(page)
    open_board(page)
    move(page, "authorise")
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True).first).to_be_visible()

    def without_the_story(route):  # as a filter would once the card no longer matches it
        board = route.fetch().json()
        board["cases"] = [c for c in board["cases"] if c["key"] != STORY]
        route.fulfill(json=board)

    page.route(is_board, without_the_story)
    take_ownership(page, story["case_id"])
    expect(board_card(page)).to_have_count(0)
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True)).to_have_count(0)
    expect(drawer(page).get_by_role("button", name="Send for approval", exact=True)).to_be_visible()

    # Still absent from the results, it keeps following the polls: a send recorded afterwards reaches the drawer.
    post(page, case_path(story["case_id"], "send"), {"rec_version": story["rec_version"]})
    expect(drawer(page)).to_contain_text(f"{STORY} is now in Authorise.")
    expect(board_card(page)).to_have_count(0)


def test_the_guided_drawer_says_done_when_the_decision_is_already_past_its_column(visitor):
    """Another account moves the decision past the drawer's column (shown here with the API's own answers, edited to
    the later step): the drawer says where it is and offers Done, never Complete the next action again."""
    page = visitor.page
    story = as_story_source(page)
    open_board(page)
    move(page, "authorise")
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True).first).to_be_visible()
    past = {"stage": "do", "step": "EXECUTE", "state": "PREPARED", "step_number": 7}

    def board_past(route):
        board = route.fetch().json()
        for c in board["cases"]:
            if c["key"] == STORY:
                c.update(past)
        route.fulfill(json=board)

    def detail_past(route):
        detail = route.fetch().json()
        detail.update(past)
        route.fulfill(json=detail)

    page.route(f"**/api/cases/{urllib.parse.quote(story['case_id'], safe='')}", detail_past)
    page.route(is_board, board_past)
    expect(drawer(page)).to_contain_text(f"{STORY} is now in Do.")
    expect(drawer(page).get_by_role("button", name="Done", exact=True)).to_be_visible()
    expect(drawer(page)).not_to_contain_text("Complete the next action")


def test_a_decision_read_slower_than_the_poll_still_reaches_the_drawer_while_its_card_is_off_the_board(visitor):
    """Each decision read takes 3 s, longer than the board's 2 s poll. While the card is off the board, a poll that
    comes while a read is in flight starts no new read, so the slow read lands instead of being superseded."""
    page = visitor.page
    story = as_story_source(page)
    open_board(page)
    move(page, "authorise")
    expect(drawer(page).get_by_role("button", name="Take ownership", exact=True).first).to_be_visible()

    def without_the_story(route):
        board = route.fetch().json()
        board["cases"] = [c for c in board["cases"] if c["key"] != STORY]
        route.fulfill(json=board)

    held = []  # (due, route, answer): each decision read answers 3 s after it was sent

    def slow_read(route):
        held.append((time.monotonic() + 3.0, route, route.fetch()))

    page.route(f"**/api/cases/{urllib.parse.quote(story['case_id'], safe='')}", slow_read)
    page.route(is_board, without_the_story)
    take_ownership(page, story["case_id"])
    take = drawer(page).get_by_role("button", name="Take ownership", exact=True)
    end = time.monotonic() + 20
    while take.count() > 0 and time.monotonic() < end:
        page.wait_for_timeout(100)  # lets the routes run
        while held and held[0][0] <= time.monotonic():
            _, route, answer = held.pop(0)
            route.fulfill(response=answer)
    assert take.count() == 0, "the drawer never showed a decision read: each poll superseded the read before it"
    expect(drawer(page).get_by_role("button", name="Send for approval", exact=True)).to_be_visible()


def board_card(page: Page, key: str = STORY):
    return page.get_by_role("region", name="Decision board", exact=True).get_by_test_id(f"decision-card-{key}")


def test_a_guided_move_offers_send_for_approval_at_context_while_a_stock_check_is_open(visitor):
    page = visitor.page
    story = as_story_source(page)
    d = case_json(page, story["case_id"])
    r = page.request.post(case_path(story["case_id"], "requests"), headers={"Idempotency-Key": str(uuid.uuid4())},
                          data={"rec_version": d["rec_version"], "fact_id": count_fact(d)["fact_id"]})
    assert r.status == 200, r.text()
    open_board(page)
    move(page, "authorise")
    drawer(page).get_by_role("button", name="Take ownership", exact=True).first.click()
    send = drawer(page).get_by_role("button", name="Send for approval", exact=True)
    expect(send).to_be_visible()
    after = case_json(page, story["case_id"])
    assert (after["step"], after["next_action"]["key"]) == ("CONTEXT", "send")  # a stock check does not stop Send (C-7)
    send.click()
    expect(drawer(page)).to_contain_text(f"{STORY} is now in Authorise.")
    expect(card_in(page, "Authorise")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/#/board$"))


def test_a_drawer_that_fails_to_load_offers_reload_and_stays_on_the_decision_board(visitor):
    page = visitor.page
    story = as_story_source(page)
    open_board(page)
    reads = []

    def the_drawers_read_fails(route):
        reads.append(route.request.url)
        if len(reads) == 2:  # the first read is the move's own check; the second, the drawer's
            route.abort()
        else:
            route.continue_()

    page.route(f"**/api/cases/{urllib.parse.quote(story['case_id'], safe='')}", the_drawers_read_fails)
    move(page, "authorise")
    expect(drawer(page).get_by_text("The stock decision didn't load", exact=True)).to_be_visible()
    expect(drawer(page).get_by_role("link", name=re.compile("Today"))).to_have_count(0)
    assert BANNED.search(drawer(page).inner_text()) is None
    drawer(page).get_by_role("button", name="Reload the stock decision", exact=True).click()
    expect(drawer(page).get_by_test_id("guided-move")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/#/board$"))


def test_move_retries_a_lost_success_with_the_same_key_and_sends_once(visitor):
    page = visitor.page
    story = owned_story(page)
    keys = []
    page.on("request", lambda r: keys.append(r.headers.get("idempotency-key"))
            if r.method == "POST" and r.url.endswith("/send") else None)

    def lose_response(route):
        response = route.fetch()  # the real API commits; only its response is lost
        assert response.status == 200
        route.abort()

    page.route("**/send", lose_response, times=1)
    move(page, "authorise")
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "PENDING_APPROVAL"
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(page.get_by_role("button", name="Try again", exact=True)).to_have_count(0)
    expect(card_in(page, "Authorise")).to_be_visible()
    assert len(keys) == 2 and keys[0] and keys[0] == keys[1]
    history = get(page, case_path(story["case_id"], "activity"))
    assert len([e for e in history if e["type"] == "sent"]) == 1


def test_failed_preflight_can_retry_without_submitting_before_the_read_succeeds(visitor):
    page = visitor.page
    story = owned_story(page)
    encoded = urllib.parse.quote(story["case_id"], safe="")
    page.route(f"**/api/cases/{encoded}", lambda route: route.abort(), times=1)
    move(page, "authorise")
    expect(page.get_by_role("button", name="Try again", exact=True)).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "OPEN"
    page.get_by_role("button", name="Try again", exact=True).click()
    expect(card_in(page, "Authorise")).to_be_visible()


def test_stale_card_refreshes_the_board_without_another_write(visitor):
    page = visitor.page
    story = as_story_source(page)
    take_ownership(page, story["case_id"])
    # The clock is installed and paused before the board loads, so its 2 s poll never runs and the card on screen
    # stays the one picked up while a different request changes the real workflow.
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    page.clock.install(time=start)
    page.clock.pause_at(start + dt.timedelta(seconds=1))
    open_board(page)
    page.reload()
    expect(card_in(page, "Decide")).to_be_visible()
    post(page, case_path(story["case_id"], "send"), {"rec_version": story["rec_version"]})
    expect(card_in(page, "Decide")).to_be_visible()
    writes = posts(page)
    move(page, "authorise")
    expect(page.get_by_text("This stock decision changed. Review its current status and move it again.")).to_be_visible()
    expect(card_in(page, "Authorise")).to_be_visible()
    expect(page.get_by_role("button", name="Reload the stock decision", exact=True)).to_be_visible()
    assert writes == []


def test_approval_move_waits_for_the_correct_person_then_approves_and_prepares_on_the_board(visitor):
    page = visitor.page
    story = owned_story(page)
    post(page, case_path(story["case_id"], "send"), {"rec_version": story["rec_version"]})
    open_board(page)
    card_in(page, "Authorise").drag_to(stage(page, "Do"), target_position={"x": 30, "y": 20})
    expect(drawer(page).get_by_test_id("guided-move")).to_be_visible()
    expect(drawer(page)).to_contain_text(f"Accept transfer · {story['dest']} manager")
    expect(drawer(page).get_by_role("button", name="Approve plan", exact=True)).to_have_count(0)
    expect(card_in(page, "Authorise")).to_be_visible()
    drawer(page).get_by_role("button", name="Close", exact=True).click()

    switch_seat(page, story["dest"])
    post(page, case_path(story["case_id"], "decision"), decision_body(page, story["case_id"], "accept", story["rec_version"]))
    switch_seat(page, story["source"])
    open_board(page)
    move(page, "do")
    drawer(page).get_by_role("checkbox", name=ACK_TICK).check()  # the story's Advisory fact (07 D-12, D-26; sanctioned)
    drawer(page).get_by_role("button", name="Approve plan", exact=True).click()
    drawer(page).get_by_role("button", name=re.compile("^Prepare ")).click()
    expect(card_in(page, "Do")).to_be_visible()
    expect(drawer(page)).to_contain_text(f"{STORY} is now in Do.")
    assert case_json(page, story["case_id"])["state"] == "PREPARED"
    expect(page).to_have_url(re.compile(r"/#/board$"))
    drawer(page).get_by_role("button", name="Done", exact=True).click()
    expect(drawer(page)).to_have_count(0)
    expect(move_select(page, where="Do")).to_be_focused()  # the Move button that opened it left with the card


def test_execution_move_uses_the_record_form_and_waits_for_receipt(visitor):
    page = visitor.page
    story = prepare_story(page)
    open_board(page)
    card_in(page, "Do", story["key"]).drag_to(stage(page, "Check and close"), target_position={"x": 30, "y": 20})
    drawer(page).get_by_role("button", name="Record dispatch", exact=True).click()
    form = drawer(page).get_by_role("form", name="Record dispatch")
    expect(form.get_by_label("Units dispatched")).to_have_value("")
    form.get_by_label("Units dispatched").fill(str(int(story["action"]["qty"])))
    condition_of(form).select_option(story["condition"])
    form.get_by_role("button", name="Record dispatch", exact=True).click()
    expect(drawer(page).get_by_role("form", name="Record dispatch")).to_have_count(0)
    expect(drawer(page)).to_contain_text(f"Record receipt · {story['dest']} manager")
    expect(card_in(page, "Do", story["key"])).to_be_visible()
    movements = get(page, case_path(story["case_id"], "execution"))["movements"]
    assert len([m for m in movements if m["kind"] == "DISPATCH"]) == 1
    expect(page).to_have_url(re.compile(r"/#/board$"))


def test_close_from_move_requires_a_note_and_persists_in_closed(visitor):
    page = visitor.page
    story, _, _ = executed_story(page)
    open_board(page)
    move(page, "closed", story["key"])
    drawer(page).get_by_role("button", name="Close stock decision", exact=True).click()
    form = drawer(page).get_by_test_id("close-form")
    form.get_by_role("button", name="Close stock decision", exact=True).click()
    expect(form).to_contain_text("Write a closing note to close this stock decision.")
    assert case_json(page, story["case_id"])["state"] == "EXECUTED"
    form.get_by_label("Closing note").fill("Receipt and recorded quantities checked.")
    form.get_by_role("button", name="Close stock decision", exact=True).click()
    expect(drawer(page)).to_contain_text(f"{story['key']} is now in Closed.")
    drawer(page).get_by_role("button", name="Done", exact=True).click()
    page.reload()
    closed = stage(page, "Check and close").get_by_role("group", name=re.compile(r"^Closed \d+$"))
    expect(closed.get_by_test_id(f"decision-card-{story['key']}")).to_be_visible()
    assert case_json(page, story["case_id"])["state"] == "CLOSED"
