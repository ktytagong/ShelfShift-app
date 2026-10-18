"""The stock decision page's verification (07-21; 07-CONTEXT D-07, D-10, D-11, D-12, D-23, D-24, D-36; 07-UI-SPEC
surface 1): each fact check with its class and exposure, the request panel behind Count stock, the Commitment tile in
the summary's facts grid, the final-approval rule, and every way forward from a blocked Required check (acceptance
check 8 in the browser).

A real Required check comes from `tests.api.verify_helpers.required_fixture` (07-09): it changes the workspace's own
record (yesterday's zero sales row at the story's source, the lot's last count moved to yesterday) and re-evaluates
the story case, so the engine itself flags the source's phantom stock fact and judges the transfer not allowed under
it. Nothing is injected. Setup the people make outside the page (taking ownership, the destination's accept, Can't
check) goes through the API as that account; what the test checks is pressed on the page. Expected text is built from
the API's own answers, never typed from a fixture.
"""
from __future__ import annotations

import re
import urllib.parse
import uuid

import pytest
import sqlalchemy as sa
from playwright.sync_api import Page, expect

from api import cases
from tests.api.verify_helpers import required_fixture
from tests.web.conftest import (
    case_id_by_tag, case_json, db_tx, decision_body, run_id, session_json, switch_seat, take_ownership,
)

pytestmark = pytest.mark.web

STORY = cases.STORY_TAG
REQUIRED_CHIP = "Required for commitment"
ADVISORY_CHIP = "Advisory · acknowledge at approval"
NOT_ALLOWED_CHIP = "Unverified · plan may not be allowed"
COUNT_STOCK = "Count stock"
SEND_REQUEST = "Send request"
DONT_SEND = "Don't send request"
BLOCKS = "Blocks commitment"
SEND_FOR_APPROVAL = "Send for approval"
APPROVE = "Approve plan"
ACCEPT = "Accept transfer"


# --- setup -----------------------------------------------------------------------------------------------------------

def post(page: Page, path: str, body: dict) -> dict:
    """One keyed write through the page's own context (its account); asserts 200 and returns the answer."""
    r = page.request.post(path, data=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def case_path(case_id: str, route: str) -> str:
    return f"/api/cases/{urllib.parse.quote(case_id, safe='')}/{route}"


def required_story(page: Page) -> tuple[dict, dict]:
    """(detail, the Required check) of the story case after the Required fixture, read back as the page's account."""
    cid = case_id_by_tag(page, STORY)
    required_fixture({"run_id": run_id(page)}, cid)
    d = case_json(page, cid)
    check = next(f for f in d["fact_checks"] if f["class"] == "REQUIRED")
    assert d["source"] == session_json(page)["branch"], "the default account is the story's source manager"
    return d, check


def open_at(page: Page, case_id: str, step: str | None = None) -> dict:
    page.goto(f"/#/cases/{urllib.parse.quote(case_id, safe='')}" + ("" if step is None else f"?step={step}"))
    d = case_json(page, case_id)
    expect(page.get_by_role("heading", level=1)).to_contain_text(f"lot {d['lot']['lot_id']}")
    return d


def main(page: Page):
    return page.locator("main#content")


def panel(page: Page):
    return main(page).get_by_test_id("step-panel")


def slot(page: Page):
    return main(page).get_by_test_id("primary-slot")


def row(page: Page, fact_id: str):
    return panel(page).locator(f"[data-fact-id='{fact_id}']")


def tile(page: Page):
    return main(page).get_by_test_id("commit-state")


def fact_title(f: dict) -> str:
    """copy.factTitle for a count fact: "{branch} · lot {lots}: recorded {n} units"."""
    n = f["recorded_value"]
    shown = f"{int(n):,}" if float(n).is_integer() else f"{n:,.1f}"
    return f"{f['branch']} · lot {', '.join(f['lot_ids'])}: recorded {shown} units"


def blocker_line(f: dict) -> str:
    return f"Required: {fact_title(f)} · owner {f['owner_branch']} manager"


def latest_request(page: Page, case_id: str, fact_id: str) -> dict:
    d = case_json(page, case_id)
    mine = [r for r in d["requests"] if r["fact_id"] == fact_id and r["rec_version"] == d["rec_version"]]
    assert mine, f"no request for {fact_id}"
    return mine[-1]


def send_count_request(page: Page, case_id: str, f: dict, button: str = COUNT_STOCK) -> dict:
    """Press the row's button, then Send request in its panel; the request the API made."""
    r = row(page, f["fact_id"])
    r.get_by_role("button", name=button, exact=True).click()
    with page.expect_response(lambda x: x.request.method == "POST" and x.url.endswith("/requests")) as sent:
        r.get_by_test_id("request-panel").get_by_role("button", name=SEND_REQUEST, exact=True).click()
    assert sent.value.status == 200, (sent.value.status, sent.value.text())
    return latest_request(page, case_id, f["fact_id"])


# --- Task 1: a Required check shows and blocks -------------------------------------------------------------------------

def test_a_required_check_shows_and_blocks(visitor):
    page = visitor.page
    d, f = required_story(page)
    cid = d["case_id"]
    s = session_json(page)
    limit = s["requests"]["max_per_version"]
    take_ownership(page, cid)
    open_at(page, cid, "context")

    # The fact checks: Required first with its class chip, its exposure chip and the requests left on this version.
    facts = panel(page).get_by_test_id("fact-checks")
    expect(facts.get_by_test_id("requests-left")).to_have_text(f"{limit} of {limit} requests left on this version")
    classes = [x["class"] for x in d["fact_checks"]]
    assert classes.index("REQUIRED") < classes.index("ADVISORY"), classes  # the engine's order: not allowed first
    first = facts.locator("[data-fact-id]").first
    expect(first).to_have_attribute("data-fact-id", f["fact_id"])
    req = row(page, f["fact_id"])
    expect(req.get_by_text(REQUIRED_CHIP, exact=True)).to_be_visible()
    expect(req.get_by_text(NOT_ALLOWED_CHIP, exact=True)).to_be_visible()
    advisory = next(x for x in d["fact_checks"] if x["class"] == "ADVISORY")
    adv = row(page, advisory["fact_id"])
    expect(adv.get_by_text(ADVISORY_CHIP, exact=True)).to_be_visible()
    expect(adv.get_by_text(re.compile(r"^Unverified · estimated decision exposure \$"))).to_be_visible()
    expect(panel(page).get_by_role("button", name="Plan as if it's true")).to_have_count(0)  # never on stock (D-12)

    # Count stock opens the request panel with the deadline from settings; Don't send request writes nothing.
    req.get_by_role("button", name=COUNT_STOCK, exact=True).click()
    box = req.get_by_test_id("request-panel")
    expect(box.get_by_text(f"The branch has {s['requests']['response_min']} minutes to respond.", exact=True)).to_be_visible()
    expect(box.get_by_role("checkbox", name=COUNT_STOCK, exact=True)).to_be_checked()
    box.get_by_role("button", name=DONT_SEND, exact=True).click()
    expect(box).to_have_count(0)
    assert case_json(page, cid)["requests"] == [], "Don't send request writes nothing"

    q = send_count_request(page, cid, f)
    expect(req.get_by_test_id("stock-check-status")).to_have_text(q["status"])
    assert q["status"].startswith("Awaiting response · respond by "), q["status"]
    expect(req.get_by_test_id("deadline-bar")).to_contain_text(re.compile(r"(\d+ min left|Less than 1 min left)"))
    expect(facts.get_by_test_id("requests-left")).to_have_text(f"{limit - 1} of {limit} requests left on this version")

    # The Commitment tile reads the server's blocks; Send for approval is never disabled by it.
    expect(tile(page).get_by_text(BLOCKS, exact=True)).to_be_visible()
    expect(tile(page).get_by_role("link", name=blocker_line(f), exact=True)).to_be_visible()
    send = slot(page).get_by_role("button", name=SEND_FOR_APPROVAL, exact=True)
    expect(send).to_be_enabled()
    send.click()
    expect(main(page).get_by_test_id("waiting-line")).to_contain_text(d["dest"])

    # The destination's accept is not the final decision: enabled, with the note.
    switch_seat(page, d["dest"])
    open_at(page, cid)
    expect(slot(page).get_by_role("button", name=ACCEPT, exact=True)).to_be_visible()
    expect(slot(page).get_by_test_id("commit-waits")).to_have_text(
        f"Your approval is recorded. Commitment waits for: {blocker_line(f)}")
    post(page, case_path(cid, "decision"), decision_body(page, cid, "accept"))

    # The source's approve would be the final decision: disabled, with the first blocker.
    switch_seat(page, d["source"])
    open_at(page, cid)
    approve = slot(page).get_by_role("button", name=APPROVE, exact=True)
    expect(approve).to_be_disabled()
    expect(slot(page).get_by_test_id("approve-blocked")).to_have_text(f"Can't approve yet: {blocker_line(f)}")


# --- Task 2: every way forward from a blocked Required check ------------------------------------------------------------

BLOCKED = "Blocked · stock/expiry verification required"
ASK_AGAIN = "Ask again"
SUPERSEDE_NOTE = "The earlier request becomes Superseded. A late answer to it is kept but can't be confirmed."
SELECT_ANOTHER = "Select another plan"
ALSO_NEEDS = "Also needs this fact verified"
SELECT_THIS = "Select this plan"
KEEP_CURRENT = "Keep current plan"
REJECT_OPEN = "Reject stock decision…"
REJECT = "Reject stock decision"
KEEP_DECISION = "Keep the stock decision"
LOT_STAYS = "The lot stays on the Decision board while it's at risk."
ADD_REASON = "Add a reason"
CANCEL_OPEN = "Cancel approved plan…"
CANCEL = "Cancel approved plan"
KEEP_APPROVED = "Keep approved plan"
REASON_TEXT = "The source branch can't confirm the stock"


def cant_check(page: Page, request_id: str) -> None:
    """The addressed store's manager presses Can't check (the story's source holds the lot: the same account)."""
    post(page, f"/api/requests/{urllib.parse.quote(request_id, safe='')}/cant-check", {})


def city(tz: str) -> str:
    return tz.split("/")[-1].replace("_", " ")


def test_a_blocked_case_always_has_a_way_forward(visitor):
    page = visitor.page
    d, f = required_story(page)
    cid = d["case_id"]
    s = session_json(page)
    limit = s["requests"]["max_per_version"]
    take_ownership(page, cid)
    open_at(page, cid, "context")
    first = send_count_request(page, cid, f)

    # Can't check: the row reads Blocked with its responsible branch and offers Ask again within the limit.
    cant_check(page, first["request_id"])
    page.reload()  # the page reads the case again (the same hash would not)
    req = row(page, f["fact_id"])
    blocked = req.get_by_test_id("blocked-label")
    expect(blocked).to_contain_text(BLOCKED)
    expect(blocked).to_contain_text(f"Responsible: {f['owner_branch']} manager")
    expect(tile(page)).to_contain_text(BLOCKED)
    req.get_by_role("button", name=ASK_AGAIN, exact=True).click()
    expect(req.get_by_test_id("request-panel").get_by_text(SUPERSEDE_NOTE, exact=True)).to_be_visible()
    second = send_count_request_in_open_panel(page, cid, f)
    assert second["request_id"] != first["request_id"]
    assert latest_request(page, cid, f["fact_id"])["request_id"] == second["request_id"]

    # The limit used: the button is disabled with its reason; Select another plan and Reject stock decision… remain.
    cant_check(page, second["request_id"])
    page.reload()  # the page reads the case again (the same hash would not)
    req = row(page, f["fact_id"])
    expect(req.get_by_role("button", name=ASK_AGAIN, exact=True)).to_be_disabled()
    expect(req.get_by_text(f"{limit} of {limit} requests used on this version.", exact=True)).to_be_visible()
    expect(req.get_by_test_id("blocked-label")).to_contain_text(BLOCKED)
    expect(req.get_by_role("button", name=REJECT_OPEN, exact=True)).to_be_visible()
    expect(panel(page).get_by_role("button", name="Plan as if it's true")).to_have_count(0)  # never on stock (D-12)

    # Select another plan: the engine's other options, those that also need the fact disabled with the reason.
    req.get_by_role("button", name=SELECT_ANOTHER, exact=True).click()
    plans = req.get_by_test_id("plan-options")
    detail = case_json(page, cid)
    others = [o for o in detail["options"] if o["key"] != detail["suggested_key"]]
    expect(plans.locator("[data-option]")).to_have_count(len(others))
    for o in others:
        item = plans.locator(f"[data-option='{o['key']}']")
        pick = item.get_by_role("button", name=f"Select {o['label']}", exact=True)
        if f["fact_id"] in o["required_fact_ids"]:
            expect(pick).to_be_disabled()
            expect(item.get_by_text(ALSO_NEEDS, exact=True)).to_be_visible()
        elif o["feasible"]:
            expect(pick).to_be_enabled()
    keep = next(o for o in others if o["kind"] == "KEEP")
    plans.locator(f"[data-option='{keep['key']}']").get_by_role("button", name=f"Select {keep['label']}", exact=True).click()
    confirm = req.get_by_test_id("select-confirm")
    expect(confirm).to_contain_text(f"Switch this stock decision to {keep['label']}?")
    expect(confirm.get_by_role("button", name=KEEP_CURRENT, exact=True)).to_be_visible()
    with page.expect_response(lambda x: x.request.method == "POST" and x.url.endswith("/select")) as sel:
        confirm.get_by_role("button", name=SELECT_THIS, exact=True).click()
    assert sel.value.status == 200, (sel.value.status, sel.value.text())

    # Afterwards the summary names the selected plan, and the stepper is back at Send: approvals start over.
    after = case_json(page, cid)
    chosen = after["selected_plan"]
    assert chosen is not None and chosen["plan_key"] == keep["key"], chosen
    line = main(page).get_by_test_id("selected-plan")
    expect(line).to_contain_text(f"Selected plan · {keep['label']} · by {chosen['by']} · ")
    expect(line).to_contain_text(f"{city(s['time_zone'])} time")
    expect(slot(page).get_by_role("button", name=SEND_FOR_APPROVAL, exact=True)).to_be_visible()


def send_count_request_in_open_panel(page: Page, case_id: str, f: dict) -> dict:
    """Send request in the row's open panel; the request the API made."""
    box = row(page, f["fact_id"]).get_by_test_id("request-panel")
    with page.expect_response(lambda x: x.request.method == "POST" and x.url.endswith("/requests")) as sent:
        box.get_by_role("button", name=SEND_REQUEST, exact=True).click()
    assert sent.value.status == 200, (sent.value.status, sent.value.text())
    return latest_request(page, case_id, f["fact_id"])


def wait_for_state(page: Page, case_id: str, state: str) -> dict:
    for _ in range(200):
        d = case_json(page, case_id)
        if d["state"] == state:
            return d
        page.wait_for_timeout(100)
    raise AssertionError(f"{case_id} never reached {state}: {case_json(page, case_id)['state']}")


def test_close_and_cancel_from_the_case(open_visitor):
    # Before approval: Reject stock decision… from the blocked row needs a reason and leaves it Rejected (D-36).
    page = open_visitor().page
    d, f = required_story(page)
    cid = d["case_id"]
    take_ownership(page, cid)
    open_at(page, cid, "context")
    req = row(page, f["fact_id"])
    req.get_by_role("button", name=REJECT_OPEN, exact=True).click()
    form = req.get_by_test_id("reject-form")
    submit = form.get_by_role("button", name=REJECT, exact=True)
    expect(submit).to_be_disabled()
    expect(form.get_by_text(ADD_REASON, exact=True)).to_be_visible()
    expect(form.get_by_text(LOT_STAYS, exact=True)).to_be_visible()
    expect(form.get_by_role("button", name="Cancel", exact=True)).to_have_count(0)  # no bare "Cancel"
    expect(form.get_by_role("button", name=KEEP_DECISION, exact=True)).to_be_visible()
    form.get_by_label("Reason").fill(REASON_TEXT)
    expect(submit).to_be_enabled()
    submit.click()
    assert wait_for_state(page, cid, "REJECTED")["state"] == "REJECTED"

    # After approval: Cancel approved plan… needs a reason and leaves it Cancelled, with who, when and why.
    page = open_visitor().page
    cid = case_id_by_tag(page, STORY)
    d = case_json(page, cid)
    take_ownership(page, cid)
    post(page, case_path(cid, "send"), {"rec_version": d["rec_version"]})
    switch_seat(page, d["dest"])
    post(page, case_path(cid, "decision"), decision_body(page, cid, "accept"))
    switch_seat(page, d["source"])
    post(page, case_path(cid, "decision"), decision_body(page, cid, "approve"))
    assert case_json(page, cid)["state"] == "APPROVED"
    open_at(page, cid)
    slot(page).get_by_role("button", name=CANCEL_OPEN, exact=True).click()
    form = slot(page).get_by_test_id("cancel-form")
    submit = form.get_by_role("button", name=CANCEL, exact=True)
    expect(form).to_contain_text("Cancel this approved plan?")
    expect(form.get_by_text(LOT_STAYS, exact=True)).to_be_visible()  # D-36: the helper on both closes
    expect(submit).to_be_disabled()
    expect(form.get_by_text(ADD_REASON, exact=True)).to_be_visible()
    expect(form.get_by_role("button", name="Cancel", exact=True)).to_have_count(0)  # no bare "Cancel"
    form.get_by_role("button", name=KEEP_APPROVED, exact=True).click()
    expect(form).to_have_count(0)
    assert case_json(page, cid)["state"] == "APPROVED", "keeping the approved plan writes nothing"
    slot(page).get_by_role("button", name=CANCEL_OPEN, exact=True).click()
    form.get_by_label("Reason").fill(REASON_TEXT)
    form.get_by_role("button", name=CANCEL, exact=True).click()
    assert wait_for_state(page, cid, "CLOSED")["state"] == "CLOSED"
    s = session_json(page)
    line = main(page).get_by_test_id("cancelled-line")
    expect(line).to_contain_text(f"Cancelled by {s['account']} · ")
    expect(line).to_contain_text(f"{city(s['time_zone'])} time · {REASON_TEXT}")


# --- Task 3: the tile's states, the verification strip, the date line and Prepare's reason ------------------------------

NO_BLOCKING = "No blocking checks"


def hold_lot(page: Page, lot_id: str, reason: str = "Seal damaged on delivery") -> dict:
    """The lot's branch manager holds it for review through the API (a CONDITION hold, 07-CONTEXT D-13)."""
    return post(page, f"/api/lots/{urllib.parse.quote(lot_id, safe='')}/holds", {"reason": reason})


def mixed_dates_hold(page: Page, lot_id: str, branch: str) -> None:
    """A second ACTIVE hold on the lot, of the other kind (Mixed dates), written as the answer form would leave it:
    the API places it only from an expiry answer, which this story lot has no fact for (test setup, as 07-09's
    verify_helpers writes the record directly)."""
    with db_tx() as conn:
        conn.execute(sa.text(
            "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, state) "
            "VALUES (:r, :h, :l, :b, 'MIXED_DATES', 'Separate stock and verify before moving', :b, 'ACTIVE')"),
            {"r": run_id(page), "h": f"HOLD-{uuid.uuid4().hex[:8]}", "l": lot_id, "b": branch})


def test_the_tile_the_strip_and_the_date_line_without_blockers(visitor):
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    d = open_at(page, cid)
    s = session_json(page)
    n = len(d["commitment"]["advisory"])
    assert d["commitment"]["blocks"] == [] and n >= 1, d["commitment"]  # the milk story's destination phantom fact
    expect(tile(page).get_by_text(NO_BLOCKING, exact=True)).to_be_visible()
    expect(tile(page)).to_contain_text(f"{n} advisory to acknowledge at approval")
    expect(tile(page)).not_to_contain_text("safe")
    strip = main(page).get_by_test_id("verification-strip")
    advisory = d["commitment"]["advisory"][0]
    expect(strip.locator(f"[data-strip-fact='{advisory['fact_id']}']")).to_contain_text(
        "Unverified · estimated decision exposure $")
    # The date line: 06.1's wording wins (07-SCREENS-MAP 07-21 T4; "Data as of" is a removed string, 06.1 R-7).
    expect(main(page).get_by_text(re.compile(r"^This stock decision is for scenario date "))).to_be_visible()
    assert s["requests"]["max_per_version"] >= 1


def test_three_blockers_show_two_lines_and_more(visitor):
    page = visitor.page
    d, f = required_story(page)
    cid, lot = d["case_id"], d["lot"]["lot_id"]
    hold_lot(page, lot)
    mixed_dates_hold(page, lot, d["source"])
    d = open_at(page, cid)
    blocks = d["commitment"]["blocks"]
    assert [b["kind"] for b in blocks] == ["lot_hold", "lot_hold", "required_check"], blocks
    expect(tile(page).get_by_text(BLOCKS, exact=True)).to_be_visible()
    links = tile(page).get_by_role("link")
    expect(links).to_have_count(3)  # two blocker lines and "+1 more"
    expect(links.nth(0)).to_have_text(f"Lot hold: lot {lot} · owner {d['source']} manager")
    expect(links.nth(2)).to_have_text("+1 more")
    for i in range(3):
        expect(links.nth(i)).to_have_attribute("href", f"#/cases/{urllib.parse.quote(cid, safe='')}?step=context")
    links.nth(2).click()
    expect(panel(page)).to_have_attribute("data-step", "CONTEXT")


def test_an_approved_plan_with_a_hold_cannot_be_prepared(visitor):
    page = visitor.page
    cid = case_id_by_tag(page, STORY)
    d = case_json(page, cid)
    take_ownership(page, cid)
    post(page, case_path(cid, "send"), {"rec_version": d["rec_version"]})
    switch_seat(page, d["dest"])
    post(page, case_path(cid, "decision"), decision_body(page, cid, "accept"))
    switch_seat(page, d["source"])
    post(page, case_path(cid, "decision"), decision_body(page, cid, "approve"))
    hold_lot(page, d["lot"]["lot_id"])
    d = open_at(page, cid)
    assert d["state"] == "APPROVED" and d["next_action"]["key"] == "prepare", (d["state"], d["next_action"])
    prepare = slot(page).get_by_role("button", name=re.compile(r"^Prepare "))
    expect(prepare).to_be_disabled()
    expect(slot(page).get_by_test_id("prepare-blocked")).to_have_text(
        f"Can't prepare yet: Lot hold: lot {d['lot']['lot_id']} · owner {d['source']} manager")


def estimated_expiry(page: Page, case_id: str) -> None:
    """The case lot's expiry is an estimate on the record, and the case is re-evaluated, so the engine itself flags an
    ESTIMATED_EXPIRY fact on the same lot as the count fact (test setup on the workspace's own record, as the Required
    fixture writes it; nothing is injected)."""
    from api import db
    rid = run_id(page)
    with db_tx() as conn:
        case = cases.case_row(conn, rid, case_id)
        conn.execute(sa.text("UPDATE lots SET expiry_source = 'ESTIMATED' WHERE run_id = :r AND world = 'shelfshift' "
                             "AND lot_id = :l"), {"r": rid, "l": case["lot_id"]})
    with db.tx() as conn:
        r = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid)
        cases.evaluate_case(conn, r, cases.case_row(conn, rid, case_id))


def test_a_combined_request_counts_once(visitor):
    page = visitor.page
    d, f = required_story(page)
    cid = d["case_id"]
    estimated_expiry(page, cid)
    d = case_json(page, cid)
    f = next(x for x in d["fact_checks"] if x["fact_id"] == f["fact_id"])
    expiry = next(x for x in d["fact_checks"] if x["kind"] == "ESTIMATED_EXPIRY" and x["branch"] == f["branch"]
                  and set(x["lot_ids"]) <= set(f["lot_ids"]))
    limit = session_json(page)["requests"]["max_per_version"]
    take_ownership(page, cid)
    open_at(page, cid, "context")
    req = row(page, f["fact_id"])
    req.get_by_role("button", name=COUNT_STOCK, exact=True).click()
    box = req.get_by_test_id("request-panel")
    count_box = box.get_by_role("checkbox", name=COUNT_STOCK, exact=True)
    expiry_box = box.get_by_role("checkbox", name="Check printed expiry date", exact=True)
    expect(count_box).to_be_checked()
    expect(expiry_box).not_to_be_checked()
    once = f"One request · counts once toward the {limit} per version"
    expect(box.get_by_text(once, exact=True)).to_have_count(0)
    expiry_box.check()
    expect(box.get_by_text(once, exact=True)).to_be_visible()
    with page.expect_request(lambda x: x.method == "POST" and x.url.endswith("/requests")) as asked:
        box.get_by_role("button", name=SEND_REQUEST, exact=True).click()
    assert asked.value.post_data_json["fact_ids"] == [f["fact_id"], expiry["fact_id"]], asked.value.post_data_json
    expect(panel(page).get_by_test_id("requests-left")).to_have_text(f"{limit - 1} of {limit} requests left on this version")
    after = case_json(page, cid)
    groups = {r["request_group"] for r in after["requests"] if r["rec_version"] == after["rec_version"]}
    assert len(groups) == 1 and len(after["requests"]) == 2, after["requests"]
