"""PROD-06 / PROD-04: the stock decision page (sketch 006 A; 06.1-UI-SPEC "Stock decision page"). The persistent
summary, the nine-step stepper with one step's panel at a time, Take ownership at Trigger (D-25, D-67), the latest
history under every step (D-58) and the page's partial and overflow states. Every expected value is read from the API
(the case detail, the activity log and the session), never pasted; the step is always the server's (D-24)."""
from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import (
    HQ, case_id_by_tag, case_json, cards_json, session_json, switch_seat, take_ownership,
)
from tests.web.test_tasks import count_fact

pytestmark = pytest.mark.web

STORY = "milk-story"  # SUS-19, the story transfer: OPEN at Trigger in the template, its source the default account
WIDTHS = (360, 390, 768, 1280)


def mark_date(iso: str) -> str:
    """format.markDate: "Sat 17 Oct 2026" from an ISO date (the UTC calendar)."""
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b} {d.year}"


def city(s: dict) -> str:
    return s["time_zone"].split("/")[-1].replace("_", " ")


def decision_href(case_id: str, step: str | None = None) -> str:
    return "/#/cases/" + urllib.parse.quote(case_id, safe="") + ("" if step is None else f"?step={step}")


def open_decision(page: Page, tag: str, step: str | None = None) -> dict:
    """Open the stock decision of the case tagged `tag` (optionally at ?step=) and wait for its h1; its detail JSON."""
    cid = case_id_by_tag(page, tag)
    page.goto(decision_href(cid, step))
    d = case_json(page, cid)
    expect(page.locator("main#content").get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    return d


def stepper(page: Page):
    return page.locator("main#content").get_by_role("navigation", name="Steps")


def step_button(page: Page, n: int, name: str):
    return stepper(page).get_by_role("button", name=f"{n}. {name}")


def panel(page: Page):
    return page.locator("main#content").get_by_test_id("step-panel")


# --- Task 1: the summary, the stepper and Take ownership ------------------------------------------------------------

def test_the_story_decision_opens_at_trigger_with_its_summary(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    s = session_json(page)
    assert (s["role"], s["branch"]) == ("manager", d["source"]), "the default account is the story's source"
    assert d["step"] == "TRIGGER" and d["owner"] is None, (d["step"], d["owner"])
    main = page.locator("main#content")

    expect(main.get_by_role("link", name="Decision board", exact=True)).to_have_attribute("href", "#/board")
    h1 = main.get_by_role("heading", level=1)
    expect(h1).to_contain_text(d["sku"])
    expect(h1).to_contain_text(f"lot {d['lot']['lot_id']}")
    expect(main.get_by_text("Product code", exact=True)).to_be_visible()
    expect(main.get_by_text("Step 1 of 9 · Trigger", exact=True)).to_be_visible()
    snapshot = next(c["date"] for c in s["calendar"] if c["day"] == d["versions"]["day"])
    expect(main.get_by_text(f"This stock decision is for scenario date {mark_date(snapshot)}.", exact=True)).to_be_visible()
    expect(step_button(page, 1, "Trigger")).to_have_attribute("aria-current", "step")
    expect(main.get_by_role("button", name="Take ownership", exact=True)).to_be_visible()
    expect(main.get_by_text("Records you as the owner. It approves nothing.", exact=True)).to_be_visible()
    expect(main.get_by_role("button", name="Send for approval")).to_have_count(0)  # Send is not offered before ownership


def test_take_ownership_moves_the_stepper_to_compare(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    s = session_json(page)
    page.locator("main#content").get_by_role("button", name="Take ownership", exact=True).click()

    taken = re.compile(rf"Taken by {re.escape(d['source'])} · Recorded \d{{1,2}} [A-Z][a-z]{{2}} \d{{4}}, \d{{2}}:\d{{2}} "
                       rf"{re.escape(city(s))} time")
    expect(panel(page).get_by_test_id("owner-box")).to_contain_text(taken)
    expect(step_button(page, 4, "Compare")).to_have_attribute("aria-current", "step")
    expect(step_button(page, 3, "Options")).to_have_attribute("data-state", "done")
    expect(step_button(page, 1, "Trigger")).to_have_attribute("data-state", "done")
    after = case_json(page, d["case_id"])
    assert (after["step"], after["owner"]["account"]) == ("COMPARE", d["source"]), (after["step"], after["owner"])
    expect(page.locator("main#content").get_by_role("button", name="Take ownership")).to_have_count(0)


def test_the_destination_waits_for_the_source_to_take_ownership(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    assert d["dest"] is not None, "the story decision is a transfer with a destination"
    switch_seat(page, d["dest"])
    open_decision(page, STORY)
    main = page.locator("main#content")
    expect(main.get_by_text(f"Waiting for the {d['source']} manager to take ownership.", exact=True)).to_be_visible()
    expect(main.get_by_role("button", name="Take ownership")).to_have_count(0)


# Takes ownership as HQ in the same workspace and returns to the store account, without reloading the page: four keyed
# POSTs inside the page; only their statuses come back, never a body. The credentials arrive as evaluate arguments.
_HQ_TAKES_JS = """async ([store, storePass, hqPass, caseId]) => {
  const post = async (path, body) => (await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID()},
    body: JSON.stringify(body),
  })).status;
  return [
    await post("/api/session/sign-out", {}), await post("/api/session", {account: "HQ", passcode: hqPass}),
    await post(`/api/cases/${encodeURIComponent(caseId)}/take-ownership`, {}),
    await post("/api/session/sign-out", {}), await post("/api/session", {account: store, passcode: storePass}),
  ];
}"""


def test_an_owned_decision_shows_its_owner_and_no_takeover(visitor, web):
    page = visitor.page
    d = open_decision(page, STORY)
    statuses = page.evaluate(_HQ_TAKES_JS, [d["source"], web.passcode, web.hq_passcode, d["case_id"]])
    assert statuses == [200, 200, 200, 200, 200], statuses
    page.goto(decision_href(d["case_id"], "trigger"))
    page.reload()  # another account's write shows on the next load (or refocus) of the page
    main = page.locator("main#content")
    expect(panel(page).get_by_text("Owned by the HQ planner.", exact=True)).to_be_visible()
    expect(main.get_by_role("button", name="Take ownership")).to_have_count(0)
    expect(main.get_by_text("Owner: HQ planner", exact=True)).to_be_visible()
    assert case_json(page, d["case_id"])["owner"]["account"] == HQ


# --- Task 3: every step shows content, the latest history, the partial and overflow states --------------------------

STEP_NAMES = ("Trigger", "Context", "Options", "Compare", "Approve", "Commit", "Execute", "Verify", "Archive")


def activity_json(page: Page, case_id: str) -> list[dict]:
    r = page.request.get("/api/cases/" + urllib.parse.quote(case_id, safe="") + "/activity")
    assert r.status == 200, r.status
    return r.json()


def test_every_step_shows_a_panel_never_an_empty_one(visitor):
    """A step whose own module has not landed shows the Phase 5 component that covered it (Compare: the engine's
    recommendation), so no panel is ever only its header; the stepper, previous and next move between them."""
    page = visitor.page
    d = open_decision(page, STORY)
    for n, name in enumerate(STEP_NAMES, 1):
        page.goto(decision_href(d["case_id"], name.lower()))
        p = panel(page)
        expect(p).to_have_attribute("data-step", name.upper())
        expect(p.get_by_role("heading", level=2).first).to_contain_text(name)
        assert p.locator(":scope > *").count() >= 2, f"the {name} panel shows only its header"
        if name != "Trigger":
            expect(page.locator("main#content").get_by_role("link", name="Back to the current step")).to_be_visible()
    page.goto(decision_href(d["case_id"], "compare"))
    expect(panel(page)).to_contain_text(d["result"]["recommended_label"])
    page.locator("main#content").get_by_role("navigation", name="Step navigation").get_by_role("link", name="‹ Options").click()
    expect(panel(page)).to_have_attribute("data-step", "OPTIONS")
    step_button(page, 2, "Context").click()
    expect(panel(page)).to_have_attribute("data-step", "CONTEXT")
    expect(step_button(page, 2, "Context")).to_have_attribute("aria-pressed", "true")
    page.locator("main#content").get_by_role("link", name="Back to the current step").click()
    expect(panel(page)).to_have_attribute("data-step", "TRIGGER")


def test_the_latest_history_shows_three_entries_with_both_clocks(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    page.locator("main#content").get_by_role("button", name="Take ownership", exact=True).click()
    expect(step_button(page, 4, "Compare")).to_have_attribute("aria-current", "step")
    log = activity_json(page, d["case_id"])
    assert log and log[-1]["type"] == "ownership_taken", [e["type"] for e in log[-3:]]
    s = session_json(page)
    card = page.locator("main#content").get_by_test_id("latest-history")
    items = card.locator("ol > li")
    expect(items).to_have_count(min(3, len(log)))
    first = items.first
    expect(first.locator("p").first).to_have_text(f"Ownership taken by {d['source']}")
    meta = re.compile(rf"^Recorded \d{{1,2}} [A-Z][a-z]{{2}} \d{{4}}, \d{{2}}:\d{{2}} {re.escape(city(s))} time · "
                      rf"Scenario date: {re.escape(mark_date(log[-1]['scenario_date']))}$")
    expect(first.locator("p").nth(1)).to_have_text(meta)
    for i in range(items.count()):
        expect(items.nth(i).locator("p").first).to_have_text(re.compile(r"^.+ by .+$"))
        expect(items.nth(i).locator("p").nth(1)).to_contain_text("Scenario date: ")
    card.get_by_role("link", name="Show full history").click()
    expect(panel(page)).to_have_attribute("data-step", "ARCHIVE")


def test_a_rejected_decision_has_no_price_drop_line(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    assert d["markdown_step"] is not None, "SUS-19 has a next markdown step in the template"
    facts = page.locator("main#content").get_by_test_id("facts")
    expect(facts.get_by_test_id("esl-line")).to_have_text(re.compile(rf"^Price drops {d['markdown_step']['pct']}% on "))
    r = page.request.post("/api/cases/" + urllib.parse.quote(d["case_id"], safe="") + "/reject",
                          headers={"Idempotency-Key": str(uuid.uuid4())}, data={"reason": "Checked with the branch"})
    assert r.status == 200, r.text()
    page.reload()
    expect(step_button(page, 9, "Archive")).to_have_attribute("aria-current", "step")
    expect(page.locator("main#content")).not_to_contain_text("Price drops")
    expect(facts.get_by_text("No action needed", exact=True)).to_be_visible()


def test_no_destination_and_no_deadline_drop_their_parts(visitor):
    """A decision with no destination reads "{source} · in store"; one with no decide-by has no Decide by fact."""
    page = visitor.page
    cards = [c for c in cards_json(page) if c["dest"] is None and c.get("needs_evaluation") is not True]
    assert cards, "no decision without a destination on the board"
    card = cards[0]
    page.goto(decision_href(card["case_id"]))
    d = case_json(page, card["case_id"])
    facts = page.locator("main#content").get_by_test_id("facts")
    expect(facts).to_contain_text(f"{d['source']} · in store")
    if d["decide_by"] is None:
        expect(facts.get_by_text("Decide by", exact=True)).to_have_count(0)
    else:
        expect(facts.get_by_text("Decide by", exact=True)).to_have_count(1)
    none = next((c for c in cards_json(page) if c["decide_by"] is None and c.get("needs_evaluation") is not True), None)
    assert none is not None, "no decision without a decide-by on the board: the partial rule goes unchecked"
    page.goto(decision_href(none["case_id"]))
    expect(page.locator("main#content").get_by_role("heading", level=1)).to_be_visible()
    expect(facts.get_by_text("Decide by", exact=True)).to_have_count(0)


def test_the_page_never_scrolls_sideways_and_the_stepper_scrolls_in_its_box(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 844})
        for step in ("trigger", "context", "options", "compare", "archive"):
            page.goto(decision_href(d["case_id"], step))
            expect(panel(page)).to_have_attribute("data-step", step.upper())
            wide = page.evaluate("() => document.documentElement.scrollWidth")
            assert wide <= width, (width, step, wide)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(decision_href(d["case_id"]))
    nav = stepper(page)
    expect(nav).to_be_visible()
    assert nav.evaluate("n => n.scrollWidth > n.clientWidth"), "the stepper fits at 390px: nothing to scroll"
    box = page.locator("main#content").get_by_role("button", name="Take ownership", exact=True).bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390, box
    page.locator("main#content").get_by_role("button", name="Take ownership", exact=True).click()
    current = step_button(page, 4, "Compare")
    expect(current).to_have_attribute("aria-current", "step")
    inside = nav.evaluate("""n => { const b = n.querySelector('[aria-current="step"]'); const r = b.getBoundingClientRect();
        const o = n.getBoundingClientRect(); return r.left >= o.left - 1 && r.right <= o.right + 1; }""")
    assert inside, "the current step is not in view inside the stepper"


# --- a successful Try again finishes Take ownership like the first press (quick 261017-wzs finding 4) ----------------

def test_take_ownership_that_succeeds_on_try_again_shows_the_owner_and_the_next_step(visitor):
    page = visitor.page
    d = open_decision(page, STORY)
    s = session_json(page)
    busy = {"error": "db_busy", "message": "The server is busy. Try again in a moment."}
    seen = {"n": 0}

    def handle(route):
        seen["n"] += 1
        if seen["n"] == 1:
            route.fulfill(status=503, json=busy)
        else:
            route.fallback()

    page.route(f"**/api/cases/{urllib.parse.quote(d['case_id'], safe='')}/take-ownership", handle)
    main = page.locator("main#content")
    main.get_by_role("button", name="Take ownership", exact=True).click()
    expect(main.get_by_text(busy["message"], exact=True)).to_be_visible()
    main.get_by_role("button", name="Try again").click()

    expect(main.get_by_role("button", name="Take ownership")).to_have_count(0)
    expect(panel(page).get_by_test_id("owner-box")).to_contain_text(f"Taken by {d['source']}")
    expect(panel(page).get_by_test_id("owner-box")).to_contain_text(f"{city(s)} time")
    after = case_json(page, d["case_id"])
    assert (after["step"], after["owner"]["account"]) == ("COMPARE", d["source"]), (after["step"], after["owner"])
    expect(step_button(page, 4, "Compare")).to_have_attribute("aria-current", "step")
    assert seen["n"] == 2


# --- Send for approval at Context while a stock check is open (quick 261018-01e leftover) -----------------------------

def test_at_context_with_a_stock_check_open_the_page_offers_send_for_approval(visitor):
    """A stock check does not stop Send (C-7): the server's step stays Context with Send as its next action, and the
    page offers Compare's Send for approval there, as the board's move drawer does."""
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    d = case_json(page, cid)
    r = page.request.post(f"/api/cases/{urllib.parse.quote(cid, safe='')}/requests",
                          headers={"Idempotency-Key": str(uuid.uuid4())},
                          data={"rec_version": d["rec_version"], "fact_id": count_fact(d)["fact_id"]})
    assert r.status == 200, r.text()
    take_ownership(page, cid)
    d = case_json(page, cid)
    assert (d["step"], d["next_action"]["key"]) == ("CONTEXT", "send"), (d["step"], d["next_action"])
    page.goto(decision_href(cid))
    send = page.locator("main#content").get_by_test_id("primary-slot").get_by_role(
        "button", name="Send for approval", exact=True)
    expect(send).to_be_visible()
    send.click()
    expect(send).to_have_count(0)
    assert case_json(page, cid)["state"] == "PENDING_APPROVAL"


def test_hq_as_owner_is_offered_send_but_never_reject(visitor):
    """Only the source store's manager may reject (api/workflow.reject): HQ owning the decision sees Send for approval
    and no Reject stock decision… at Compare (Codex check 3 of quick 261017-wzs)."""
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    switch_seat(page, None)
    take_ownership(page, cid)
    d = case_json(page, cid)
    assert d["next_action"]["key"] == "send", d["next_action"]
    page.goto(decision_href(cid))
    slot = page.locator("main#content").get_by_test_id("primary-slot")
    expect(slot.get_by_role("button", name="Send for approval", exact=True)).to_be_visible()
    expect(slot.get_by_role("button", name="Reject stock decision…", exact=True)).to_have_count(0)
