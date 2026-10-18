"""The judge's path on the public dataset, in the browser, on the product screens (05.2 DATA-17, DATA-16; 05.2-CONTEXT
D-17, D-18, D-26, D-37, D-38; 06.1 D-01, D-47, D-54, D-61, D-63, D-67; UI-SPEC "Hand-off selectors").

Runs with SS_WEB_DATASET=public_tiny: the browser suite's template is then the tiny public set (a synthetic test
fixture in the FreshRetailNet-50K schema, tests/api/fixtures/public_tiny) built through `sandbox.build_template`. Every
case is found by its catalogue tag and every expectation is read from the API (the session, the board pages, the case
detail and its requests): the tests assert beats (the mark, the scope, a new version, a changed pick, each account's
buttons), never a quantity or an amount (04 D-14).

06.1-31 moved 05.2-22's path onto the product: the account signs in on the card (`sign_in`, and `switch_seat` signs
out and back in, 06.1-15); the mark is D-63's; ownership comes before Send for approval (D-54, D-67); the on-screen
names are D-61's and D-36's ("Count stock", Tasks, "Prepare transfer work order"); and the product has no day control
(D-01, D-03), so the step that checked the public set's missing day button is gone (the server's refusal of a public
day move stays covered by tests/api).
"""
from __future__ import annotations

import datetime
import re
import time
import urllib.parse

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.web.conftest import Web, case_json, new_context, public_only, session_json, sign_in, switch_seat
from tests.web.test_journey import acknowledge
from tests.web.test_tasks import (checked_just_now, count_fact, count_input, open_tasks_page, request_card,
                                  request_for, summary_value)

pytestmark = [pytest.mark.web, public_only]

PUBLIC_MARK = "Public sales dataset · Simulated operational scenario"  # D-63's mark, before its date

# The hand-off names (UI-SPEC "Hand-off selectors"; D-61): the stock decision's primary actions.
TAKE = "Take ownership"
SEND = "Send for approval"
ACCEPT = "Accept transfer"
APPROVE = "Approve plan"
PREPARE = "Prepare transfer work order"
DISPATCH = "Record dispatch"
ACTIONS = (TAKE, SEND, ACCEPT, APPROVE, PREPARE, DISPATCH)
REQUEST_STOCK_CHECK = "Count stock"  # 07-21 (07-CONTEXT D-36; was "Request stock check")
SEND_REQUEST = "Send request"  # the request panel's submit (07-21)
DAY_CONTROL = re.compile(r"\bday\b", re.IGNORECASE)  # any button naming the day (the Phase 5 demo panel had one)


def mark_date(iso: str) -> str:
    """The mark's scenario date as the UI writes it, "Sat 17 Oct 2026" (weekday, day, short month, year), composed here
    from the session's own `data_as_of`, never typed."""
    d = datetime.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b} {d.year}"


def expected_mark(s: dict) -> str:
    """The dataset mark for this session: its label with `{date}` filled from `data_as_of` (05.2 D-04; 06.1 D-63)."""
    return s["label"].replace("{date}", mark_date(s["data_as_of"]))


def seat(page: Page, branch: str | None) -> None:
    """Be signed in as `branch`'s store manager (or HQ for None): sign out and back in only when the session is another
    account (`switch_seat` signs in on the same browser; the workspace is kept, D-02)."""
    want = ("hq", None) if branch is None else ("manager", branch)
    s = session_json(page)
    if (s["role"], s["branch"]) != want:
        switch_seat(page, branch)


def board_pages(page: Page, query: dict | None = None) -> tuple[list[dict], dict]:
    """Every card of GET /api/cases in the account's scope (with `query`'s filters), walking the server's pages in
    order; returns the cards and the first page's response (page_size, total, scope, evaluated, at_risk)."""
    cards: list[dict] = []
    first: dict | None = None
    n = 1
    while True:
        params = {**(query or {}), "page": str(n)}
        r = page.request.get("/api/cases?" + urllib.parse.urlencode(params))
        assert r.status == 200, r.status
        body = r.json()
        first = first or body
        if not body["cases"]:
            break
        cards += body["cases"]
        if len(cards) >= body["total"]:
            break
        n += 1
    assert first is not None
    return cards, first


def card_by_tag(page: Page, tag: str, query: dict | None = None) -> dict:
    """The card whose catalogue tag is `tag` in the account's scope; fails naming the tags in scope when there is none."""
    cards, _ = board_pages(page, query)
    found = [c for c in cards if c["tag"] == tag]
    assert found, f"no card is tagged {tag!r}; the tags in scope are {sorted({c['tag'] for c in cards if c['tag']})}"
    return found[0]


def evaluated_text(board: dict) -> str:
    """The board's evaluated line from the server's two counts (05.2 D-38; boardview.evaluatedLine)."""
    return f"Evaluated {board['evaluated']} of {board['at_risk']} at-risk items"


def expect_mark(page: Page, want: str) -> None:
    """The signed-in shell shows the dataset mark `want` as its two lines (vocab.markLines: the dataset part, then
    "Scenario date: …" on its own line; the sidebar's head, or the strip above the page on a phone or a collapsed
    rail), each visible and exact."""
    at = want.index("Scenario date:")
    for line in (want[:at].removesuffix(" · "), want[at:]):
        expect(page.get_by_text(line, exact=True).filter(visible=True).first).to_be_visible()


def board_region(page: Page):
    return page.get_by_role("region", name="Decision board", exact=True)


def board_cards(page: Page):
    """The cards the Decision board shows (flag cards and case cards alike)."""
    return board_region(page).locator("[data-testid^='decision-card-']")


def open_board(page: Page, query: str = "") -> None:
    """The Decision board through the main navigation's route (the server scopes, filters and pages it)."""
    page.goto("/#/board" + query)
    expect(page.get_by_role("heading", level=1, name="Decision board", exact=True)).to_be_visible()


def decision_href(case_id: str, step: str | None = None) -> str:
    return "/#/cases/" + urllib.parse.quote(case_id, safe="") + ("" if step is None else f"?step={step}")


def open_by_route(page: Page, case_id: str, step: str | None = None) -> dict:
    """Open the stock decision at its route (UI-SPEC "Open a decision": `#/cases/{key}`), wait for its h1 and return
    its detail."""
    page.goto(decision_href(case_id, step))
    d = case_json(page, case_id)
    expect(page.locator("main#content").get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    return d


def step_panel(page: Page):
    return page.locator("main#content").get_by_test_id("step-panel")


def header(page: Page):
    return page.locator("main#content header")


def expect_actions(page: Page, shown: tuple[str, ...]) -> None:
    """The stock decision's header offers exactly the `shown` primary actions and none of the others."""
    for name in ACTIONS:
        expect(header(page).get_by_role("button", name=name, exact=True)).to_have_count(1 if name in shown else 0)


def wait_for(page: Page, case_id: str, done, what: str) -> dict:
    """Poll the API until `done(detail)` holds (a write's refetch is in flight); returns the detail."""
    deadline = time.monotonic() + 30
    while True:
        d = case_json(page, case_id)
        if done(d):
            return d
        assert time.monotonic() < deadline, f"{d['key']} never reached {what}: state {d['state']}, owner {d['owner']}"
        page.wait_for_timeout(100)


def press(page: Page, name: str, case_id: str, done, what: str) -> dict:
    """Press one primary action in the header and wait until the API reports its effect."""
    header(page).get_by_role("button", name=name, exact=True).click()
    d = wait_for(page, case_id, done, what)
    expect(header(page).get_by_role("button", name=name, exact=True)).to_have_count(0)
    return d


def state_is(state: str):
    return lambda d: d["state"] == state


def accounts_see(page: Page, case_id: str, shown: dict[str | None, tuple[str, ...]], back_to: str | None) -> dict:
    """Sign in as each account in `shown` (a store, or None for HQ), open the stock decision and check that account's
    actions are exactly the listed ones; then sign in as `back_to` with the decision open. Returns the detail."""
    for branch, actions in shown.items():
        seat(page, branch)
        open_by_route(page, case_id)
        expect_actions(page, actions)
    seat(page, back_to)
    return open_by_route(page, case_id)


def test_the_judges_path_on_the_public_dataset(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        s = sign_in(page, web)  # the card: Store manager, the start seat's store code and this run's passcode

        # The mark: the session's label with its scenario date (D-63), on every mark the page shows, and the context
        # line's scenario date
        assert s["kind"] == "public" and s["data_as_of"], (s["kind"], s["data_as_of"])
        mark = expected_mark(s)
        assert mark.startswith(PUBLIC_MARK) and f"Scenario date: {mark_date(s['data_as_of'])}" in mark, mark
        expect_mark(page, mark)
        expect(page.get_by_test_id("context-line").first).to_contain_text(f"Scenario date: {mark_date(s['data_as_of'])}")
        store = s["branch"]
        assert s["role"] == "manager" and store == web.store == s["account"], (s["role"], store, s["account"])

        # The board is scoped to the signed-in store (D-26): every card in scope has it as source or destination, the
        # board shows the server's first page, and the evaluated line is the server's counts
        open_board(page)
        cards, first = board_pages(page)
        assert cards, "the start seat's board is empty"
        assert all(store in (c["source"], c["dest"]) for c in cards), [(c["key"], c["source"], c["dest"]) for c in cards]
        expect(board_cards(page)).to_have_count(len(first["cases"]))
        expect(page.get_by_text(evaluated_text(first), exact=True)).to_be_visible()
        # No day control in the product (D-01, D-03): no button moves the scenario day
        expect(page.get_by_role("button", name=DAY_CONTROL)).to_have_count(0)
        day = s["day"]

        # Verify before you move: the start seat is the verify case's source (D-38). Its card opens the stock
        # decision; the Context step's plan-changing count fact offers Count stock
        verify = card_by_tag(page, "verify")
        assert verify["source"] == store, (verify["source"], store)
        board_region(page).get_by_test_id(f"decision-card-{verify['key']}").click()
        page.get_by_role("button", name="Open stock decision", exact=True).click()
        steps = page.get_by_role("navigation", name="Steps", exact=True)
        expect(steps).to_be_visible()
        d = case_json(page, verify["case_id"])
        fact = count_fact(d)
        assert fact["pick_changed"], fact
        steps.get_by_role("button", name="2. Context").click()
        expect(step_panel(page)).to_have_attribute("data-step", "CONTEXT")
        row = step_panel(page).locator(f"[data-fact-id='{fact['fact_id']}']")
        row.get_by_role("button", name=REQUEST_STOCK_CHECK, exact=True).click()  # 07-21: opens the request panel
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/requests")) as sent:
            row.get_by_test_id("request-panel").get_by_role("button", name=SEND_REQUEST, exact=True).click()
        assert sent.value.status == 200, sent.value.status
        q = request_for(page, d, fact["fact_id"])
        counter = q["to_branch"]
        expect(row.get_by_test_id("stock-check-status")).to_have_text(q["status"])
        expect(row.get_by_role("button", name=REQUEST_STOCK_CHECK)).to_have_count(0)
        # A request blocks nothing: the source takes ownership and Send for approval is offered while the stock check
        # is still open (it is not pressed here: the confirmed count re-plans first)
        expect_actions(page, (TAKE,))
        press(page, TAKE, verify["case_id"], lambda v: v["owner"] is not None and v["owner"]["account"] == store,
              "an owner")
        expect_actions(page, (SEND,))
        assert [r["state"] for r in case_json(page, verify["case_id"])["requests"]
                if r["request_id"] == q["request_id"]] == ["OPEN"], "the stock check is still open"

        # The addressed store answers every lot (shelf and backroom) and confirms on Tasks. The designed lot is
        # physically empty (the fixture's designed example), which is what the phantom fact's alternative assumes.
        seat(page, counter)
        open_tasks_page(page)
        card = request_card(page, q)
        for lot in q["lot_ids"]:
            for field in ("shelf", "backroom"):
                count_input(card, q, lot, field).fill("0")
        checked_just_now(card)  # Phase 7 (07 D-22): an answer says when the count was taken
        card.get_by_role("button", name="Send answer").click()
        expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
        card.get_by_label("I checked the shelf and the backroom for this lot").check()
        card.get_by_role("button", name="Confirm count").click()
        expect(page.get_by_test_id("task-status")).to_contain_text(f"Count confirmed by {counter} manager")  # 07-19

        # The confirm gave the stock decision a new version, and its pick changed to the fact's best plan if the count
        # holds: the API says so, and the Options step shows the new version and the new Recommended row
        seat(page, store)
        after = case_json(page, verify["case_id"])
        before_v, now_v = d["versions"]["rec_version"], after["versions"]["rec_version"]
        assert now_v != before_v, "the confirm gave the verify case a new version"
        assert after["suggested_key"] != d["suggested_key"], (d["suggested_key"], after["suggested_key"])
        assert after["suggested_key"] == fact["best_plan_if_alt"], (after["suggested_key"], fact["best_plan_if_alt"])
        open_by_route(page, verify["case_id"], "options")
        line = step_panel(page).get_by_test_id("version-line")
        expect(line).to_contain_text(now_v[:12])
        expect(line).not_to_contain_text(before_v[:12])
        expect(step_panel(page).get_by_test_id("recommended-row").locator("td").first).to_have_text(
            after["result"]["recommended_label"])

        # Take ownership, send, accept, approve and prepare across the accounts, each seeing only its own actions
        # (D-54, D-67: the source manager or HQ may take ownership; only the owner sends). When the count left the
        # verify case nothing to move, the transfer case (found by its tag at HQ) carries the approvals.
        tag = "verify" if after["dest"] is not None else "transfer"
        seat(page, None)
        x = card_by_tag(page, tag)
        src, dest, cid = x["source"], x["dest"], x["case_id"]
        assert dest is not None, (tag, x["suggested_key"])
        if x["owner"] is None:  # the transfer case: nobody owns it yet
            x = accounts_see(page, cid, {dest: (), None: (TAKE,), src: (TAKE,)}, back_to=src)
            assert x["owner"] is None, x["owner"]
            press(page, TAKE, cid, lambda v: v["owner"] is not None and v["owner"]["account"] == src, "an owner")
        x = case_json(page, cid)
        assert x["state"] == "OPEN" and x["owner"]["account"] == src and \
            x["approvals"]["required"] == ["source", "destination"], (x["state"], x["owner"], x["approvals"]["required"])
        accounts_see(page, cid, {dest: (), None: (), src: (SEND,)}, back_to=src)
        press(page, SEND, cid, state_is("PENDING_APPROVAL"), "PENDING_APPROVAL")
        accounts_see(page, cid, {src: (), None: (), dest: (ACCEPT,)}, back_to=dest)
        acknowledge(page, cid)  # Phase 7: Accept and Approve need the tick when the plan has Advisory facts
        press(page, ACCEPT, cid, lambda v: v["state"] == "PENDING_APPROVAL" and v["next_action"]["key"] == "approve",
              "the destination's acceptance")
        accounts_see(page, cid, {dest: (), None: (), src: (APPROVE,)}, back_to=src)
        acknowledge(page, cid)
        press(page, APPROVE, cid, state_is("APPROVED"), "APPROVED")
        accounts_see(page, cid, {dest: (), None: (), src: (PREPARE,)}, back_to=src)
        press(page, PREPARE, cid, state_is("PREPARED"), "PREPARED")
        accounts_see(page, cid, {dest: (), None: (), src: (DISPATCH,)}, back_to=src)
        assert case_json(page, cid)["state"] == "PREPARED"
        assert session_json(page)["day"] == day, "the scenario day never moved"
    finally:
        context.close()
