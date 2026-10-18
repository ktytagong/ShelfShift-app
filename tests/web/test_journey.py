"""PROD-21 and PROD-20 on the judge's path (06.1 D-47, D-59, D-61; UI-SPEC "Hand-off selectors"): two store managers
carry the story stock decision from sign-in to Close on the product screens, in one browser, and the same journey
runs again at 390x844 without the page ever scrolling sideways.

Every action is a button press found by role and name, every field is found by its label, and every switch of account
is a sign-out through the profile menu followed by a sign-in on the card (no API shortcut, no route fake). Only reads go
to the API: the session, the story's codes and its committed quantity, and the final state. The passcode comes from the
harness's `Web` field (repr=False) and goes into `fill` only: it is never printed, logged or put in a message.

The selectors are the UI-SPEC's hand-off selectors, so the Phase 6 session can copy them. Two controls the hand-off
table does not list are needed on the way, and are found by role and label the same way: the Condition select (the
server requires a condition on a dispatch and a receipt) and, when the plan has a markdown rest, Record price change
with its tick (06.1-11: a decision executes only when every work order is done)."""
from __future__ import annotations

import re
import time
import urllib.parse
from typing import Callable

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.web.conftest import Web, case_id_by_tag, case_json, new_context

pytestmark = pytest.mark.web

STORY_TAG = "milk-story"  # the story transfer (03-CONTEXT D-23)
PHONE = {"width": 390, "height": 844}
SIGNED_IN_S = 30
CLOSING_NOTE = "Received in full at the destination; the stock decision is complete."

# The hand-off names (UI-SPEC "Hand-off selectors"; D-61).
TAKE = "Take ownership"
SEND = "Send for approval"
ACCEPT = "Accept transfer"
APPROVE = "Approve plan"
PREPARE = "Prepare transfer work order"
DISPATCH = "Record dispatch"
RECEIPT = "Record receipt"
PRICE = "Record price change"
PRICE_TICK = "I changed the shelf price for this lot"
CLOSE = "Close stock decision"

AfterStep = Callable[[Page, str], None]


def nothing(page: Page, step: str) -> None:
    """The desktop journey checks nothing extra after a step."""


def no_sideways_scroll(page: Page, step: str) -> None:
    """D-59: the page never scrolls sideways (wide content scrolls inside its own box)."""
    wide, view = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert wide <= view, f"the page scrolls sideways after '{step}': scrollWidth {wide} > innerWidth {view}"


# --- reading (the only API use besides the final state) --------------------------------------------------------------

def signed_in(page: Page) -> dict | None:
    """GET /api/session through the page's context: the session, or None when signed out (401)."""
    r = page.request.get("/api/session")
    if r.status == 401:
        return None
    assert r.status == 200, r.status
    return r.json()


def execution(page: Page, case_id: str) -> dict:
    r = page.request.get("/api/cases/" + urllib.parse.quote(case_id, safe="") + "/execution")
    assert r.status == 200, r.status
    return r.json()


def final_state(page: Page, case_id: str) -> dict:
    """GET /api/cases/{id}, fetched in the page (its own cookie): the state and how it closed."""
    return page.evaluate("""async (id) => {
      const r = await fetch("/api/cases/" + encodeURIComponent(id), {credentials: "same-origin"});
      const d = await r.json();
      return {status: r.status, state: d.state, closed_as: d.closed_as};
    }""", case_id)


# --- the screens -----------------------------------------------------------------------------------------------------

def phone(page: Page) -> bool:
    return page.get_by_role("button", name="Open menu").is_visible()


def show_main_nav(page: Page):
    """The navigation named "Main": in the sidebar on a desktop, in the menu sheet on a phone."""
    if phone(page) and not page.get_by_role("dialog", name="Main menu").is_visible():
        page.get_by_role("button", name="Open menu").click()
    nav = page.get_by_role("navigation", name="Main")
    expect(nav).to_be_visible()
    return nav


def sign_in(page: Page, web: Web, account: str, after_step: AfterStep) -> None:
    """Sign in on the card as `account`'s store manager; the main navigation and the scenario date then show."""
    page.get_by_role("radio", name="Store manager").check()
    page.get_by_label("Store code").fill(account)
    page.get_by_label("Passcode", exact=True).fill(web.passcode)
    page.get_by_role("button", name="Sign in", exact=True).click()
    deadline = time.monotonic() + SIGNED_IN_S
    while (session := signed_in(page)) is None or session.get("account") != account:
        assert time.monotonic() < deadline, f"the session never named the account {account}"
        page.wait_for_timeout(100)
    # The data mark shows once signed in. The harness's template is the synthetic set, which has no data_as_of, so
    # its mark carries no "Scenario date:" (vocab.markLines); the session's own label is what must show.
    expect(page.get_by_text(session["label"]).filter(visible=True).first).to_be_visible()
    show_main_nav(page)
    after_step(page, f"sign in as {account}")


def sign_out(page: Page, after_step: AfterStep) -> None:
    """Sign out through the profile menu; the sign-in card shows again."""
    if phone(page) and page.get_by_role("dialog", name="Main menu").is_visible():
        page.keyboard.press("Escape")
    page.get_by_role("button", name=re.compile(r"^Account:")).click()
    page.get_by_role("menuitem", name="Sign out").click()
    expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
    assert signed_in(page) is None, "the session is still signed in after Sign out"
    after_step(page, "sign out")


def switch_to(page: Page, web: Web, account: str, after_step: AfterStep) -> None:
    sign_out(page, after_step)
    sign_in(page, web, account, after_step)


def open_decision(page: Page, key: str, after_step: AfterStep) -> None:
    """From the main navigation to the Decision board, the story's card, then Open stock decision."""
    nav = show_main_nav(page)
    nav.get_by_role("link", name=re.compile(r"^Decision board")).click()
    board = page.get_by_role("region", name="Decision board")
    card = board.get_by_test_id(f"decision-card-{key}")
    expect(card).to_be_visible()
    after_step(page, "the Decision board")
    card.click()
    page.get_by_role("button", name="Open stock decision").click()
    expect(page.get_by_role("navigation", name="Steps", exact=True)).to_be_visible()
    after_step(page, "Open stock decision")


def press(page: Page, name: str):
    """Press the one button named `name` and return it (gone or replaced once the server answered)."""
    button = page.get_by_role("button", name=name, exact=True)
    expect(button).to_have_count(1)
    button.click()
    return button


def waiting_for(page: Page, branch: str) -> None:
    expect(page.get_by_test_id("waiting-line")).to_contain_text(branch)


def condition(page: Page):
    """The Condition select ("Units received in good condition" holds the word too)."""
    return page.get_by_role("combobox", name=re.compile(r"^Condition"))


# --- the journey -----------------------------------------------------------------------------------------------------

def judges_journey(page: Page, web: Web, after_step: AfterStep) -> None:
    """Sign-in to Close by the story's two stores; `after_step(page, step)` runs after every sign-in, sign-out and
    step's button press, once the screen has answered."""
    page.goto("/")
    expect(page.get_by_role("radio", name="Store manager")).to_be_visible()
    after_step(page, "the sign-in card")
    sign_in(page, web, web.store, after_step)

    case_id = case_id_by_tag(page, STORY_TAG)
    d = case_json(page, case_id)
    key, src, dest = d["key"], d["source"], d["dest"]
    assert dest, "the story's recommendation is a transfer (03-CONTEXT D-23)"
    if src != web.store:
        switch_to(page, web, src, after_step)

    # The source store takes ownership and sends the plan for approval.
    open_decision(page, key, after_step)
    press(page, TAKE)
    expect(page.get_by_role("button", name=SEND, exact=True)).to_be_visible()
    after_step(page, TAKE)
    press(page, SEND)
    waiting_for(page, dest)
    after_step(page, SEND)

    # The destination accepts the transfer.
    switch_to(page, web, dest, after_step)
    open_decision(page, key, after_step)
    press(page, ACCEPT)
    waiting_for(page, src)
    after_step(page, ACCEPT)

    # The source approves, prepares the transfer work order and records the dispatch of the committed quantity.
    switch_to(page, web, src, after_step)
    open_decision(page, key, after_step)
    press(page, APPROVE)
    expect(page.get_by_role("button", name=PREPARE, exact=True)).to_be_visible()
    after_step(page, APPROVE)
    press(page, PREPARE)
    expect(page.get_by_role("button", name=DISPATCH, exact=True)).to_be_visible()
    after_step(page, PREPARE)

    view = execution(page, case_id)
    transfer = next(w for w in view["work_orders"] if w["kind"] == "TRANSFER")
    committed = str(int(transfer["qty"]))
    good = view["options"]["conditions"][0]["label"]

    press(page, DISPATCH)  # opens the form; its own submit carries the same name
    page.get_by_label("Units dispatched").fill(committed)
    condition(page).select_option(label=good)
    after_step(page, f"{DISPATCH} form")
    press(page, DISPATCH)
    waiting_for(page, dest)
    after_step(page, DISPATCH)

    # The destination records the receipt: everything arrived in good condition.
    switch_to(page, web, dest, after_step)
    open_decision(page, key, after_step)
    press(page, RECEIPT)
    page.get_by_label("Units received in good condition").fill(committed)
    page.get_by_label("Units damaged").fill("0")
    condition(page).select_option(label=good)
    after_step(page, f"{RECEIPT} form")
    press(page, RECEIPT)
    waiting_for(page, src)
    after_step(page, RECEIPT)

    # The source records the shelf price of a markdown rest when the plan has one, then closes the stock decision.
    switch_to(page, web, src, after_step)
    open_decision(page, key, after_step)
    if case_json(page, case_id)["next_action"]["key"] == "record_price":
        press(page, PRICE)
        page.get_by_label(PRICE_TICK).check()
        after_step(page, f"{PRICE} form")
        press(page, PRICE)
        expect(page.get_by_role("button", name=CLOSE, exact=True)).to_be_visible()
        after_step(page, PRICE)
    press(page, CLOSE)  # opens the form; its own submit carries the same name
    page.get_by_label("Closing note").fill(CLOSING_NOTE)
    after_step(page, f"{CLOSE} form")
    press(page, CLOSE)
    expect(page.get_by_text(f"Closed · {CLOSING_NOTE}")).to_be_visible()
    after_step(page, CLOSE)

    done = final_state(page, case_id)
    assert (done["status"], done["state"], done["closed_as"]) == (200, "CLOSED", "COMPLETED"), done


def test_judges_journey(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        judges_journey(context.new_page(), web, nothing)
    finally:
        context.close()


def test_judges_journey_on_a_phone(browser: Browser, web: Web):
    """The same journey at 390x844: no page scrolls sideways after any sign-in, sign-out or step (D-59, PROD-20)."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.set_viewport_size(PHONE)
        judges_journey(page, web, no_sideways_scroll)
    finally:
        context.close()
