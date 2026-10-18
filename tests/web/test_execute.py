"""PROD-09, PROD-10 and PROD-14 on screen (06.1 D-30, D-33..D-35, D-48, D-49, D-55, D-65; UI-SPEC "Execute"): the
stock decision's Execute step records a dispatch (the source manager), a receipt (the destination manager), a
reconciliation (the owner or HQ) and a price change (the source manager), shows the server's dispatch blocks word for
word, and carries 06.1-29's transport details panel for a transfer.

The story stock decision (the milk-story tag) reaches PREPARED through the API in setup, as the people involved make
it: the source takes ownership and sends, the destination accepts, the source approves and prepares (each POST with its
own Idempotency-Key, through the page's own signed-in context). The step under test is then driven on the screen.
A decision is opened with `page.goto("#/cases/{key}?step=execute")`, the deep link Tasks and Branch transfers use.
Expected text is built from the API's own answers (quantities, run times, lots, server reasons, the session's zone),
never typed here, except the screen's own words.
"""
from __future__ import annotations

import re
import urllib.parse
import uuid

import pytest
import sqlalchemy as sa
from playwright.sync_api import Page, expect

from tests.web.conftest import (
    case_id_by_tag, case_json, cards_json, db_tx, decision_body, run_id, session_json, switch_seat, take_ownership,
)

pytestmark = pytest.mark.web

STORY_TAG = "milk-story"


# --- setup through the API -------------------------------------------------------------------------------------------

def post(page: Page, path: str, body: dict) -> dict:
    """One keyed write through the page's own context; asserts 200 and returns the answer."""
    r = page.request.post(path, data=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def get(page: Page, path: str) -> dict:
    r = page.request.get(path)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def case_path(case_id: str, route: str) -> str:
    return f"/api/cases/{urllib.parse.quote(case_id, safe='')}/{route}"


def prepare_story(page: Page) -> dict:
    """The story stock decision taken, sent, accepted, approved and prepared; ends signed in as its source. Returns
    {case_id, key, source, dest, action, markdown, condition}: `action` is its TRANSFER work order and `markdown` its
    MARKDOWN rest work order (or None), both from the execution view."""
    case_id = case_id_by_tag(page, STORY_TAG)
    d = case_json(page, case_id)
    source, dest, version = d["source"], d["dest"], d["rec_version"]
    assert dest, "the story's recommendation is a transfer (03-CONTEXT D-23)"
    take_ownership(page, case_id)
    post(page, case_path(case_id, "send"), {"rec_version": version})
    switch_seat(page, dest)
    post(page, case_path(case_id, "decision"), decision_body(page, case_id, "accept", version))
    switch_seat(page, source)
    post(page, case_path(case_id, "decision"), decision_body(page, case_id, "approve", version))
    post(page, case_path(case_id, "prepare"), {})
    view = get(page, case_path(case_id, "execution"))
    action = next(w for w in view["work_orders"] if w["kind"] == "TRANSFER" and w["state"] == "PLANNED")
    markdown = next((w for w in view["work_orders"] if w["kind"] == "MARKDOWN" and w["state"] == "PLANNED"), None)
    return {"case_id": case_id, "key": d["key"], "source": source, "dest": dest, "action": action,
            "markdown": markdown, "condition": view["options"]["conditions"][0]["key"],
            "condition_label": view["options"]["conditions"][0]["label"], "options": view["options"]}


def dispatch_story(page: Page, story: dict, qty: int | None = None) -> int:
    """The source records the dispatch (the whole committed quantity by default); returns the units dispatched."""
    qty = int(story["action"]["qty"]) if qty is None else qty
    post(page, case_path(story["case_id"], "dispatch"), {
        "action_id": story["action"]["action_id"], "qty": qty, "lot_id": story["action"]["lot_id"],
        "condition": story["condition"]})
    return qty


def execute_url(key: str) -> str:
    return f"/#/cases/{urllib.parse.quote(key, safe='')}?step=execute"


def units(n: float) -> str:
    """format.units for whole counts: grouped, en-US."""
    return f"{int(n):,}" if float(n).is_integer() else f"{n:,.1f}"


def hhmm(minute: int) -> str:
    """format.hhmm: minutes after midnight as 24-hour "HH:MM"."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def city(tz: str) -> str:
    """vocab.cityOf: "Asia/Bangkok" -> "Bangkok"."""
    return tz.split("/")[-1].replace("_", " ")


def condition_of(form):
    """The Condition select ("Units received in good condition" also holds the word)."""
    return form.get_by_role("combobox", name=re.compile(r"^Condition"))


def panel_of(page: Page):
    return page.get_by_test_id("step-panel")


NOT_PROMISED = re.compile(r"\b(ETA|delay(ed)?|late|overdue|countdown|ago|min(utes)? left)\b", re.IGNORECASE)


# --- Task 1: the source records the dispatch -------------------------------------------------------------------------

def test_the_source_records_the_dispatch_and_the_movement_shows_dispatched(visitor):
    page = visitor.page
    story = prepare_story(page)
    committed = int(story["action"]["qty"])
    sent = committed - 6
    run = get(page, case_path(story["case_id"], "transport"))["run"]
    assert run is not None, "the prepared story transfer has its Scheduled truck run (06.1-12)"
    tz = session_json(page)["time_zone"]

    page.goto(execute_url(story["key"]))
    panel = panel_of(page)
    expect(panel).to_have_attribute("data-step", "EXECUTE")
    opener = page.get_by_test_id("primary-slot").get_by_role("button", name="Record dispatch")
    expect(opener).to_be_visible()
    opener.click()

    form = page.get_by_role("form", name="Record dispatch")
    qty = form.get_by_label("Units dispatched")
    expect(qty).to_have_value("")  # empty by default (D-48)
    expect(form).to_contain_text(f"Up to {units(committed)} committed units.")
    expect(form.get_by_test_id("form-lot")).to_have_text(story["action"]["lot_id"])
    qty.fill(str(sent))
    condition_of(form).select_option(label=story["condition_label"])
    submit = form.get_by_role("button", name="Record dispatch")
    expect(submit).to_have_accessible_description(
        f"Records {units(sent)} units leaving {story['source']}. This updates the stock record.")
    submit.click()

    transport = panel.get_by_test_id("transport-panel")
    movement = transport.get_by_test_id("movement-box")
    expect(movement.get_by_test_id("movement-chip")).to_have_text("Dispatched")
    expect(movement).to_contain_text(f"Short shipment · {units(sent)} of {units(committed)}")
    record = transport.get_by_test_id("movement-record").locator("li")
    expect(record.nth(1)).to_contain_text("Recorded ")
    expect(record.nth(1)).to_contain_text(f"{city(tz)} time")
    expect(record.nth(1)).to_contain_text(f"{units(sent)} units dispatched")
    expect(page.get_by_role("form", name="Record dispatch")).to_have_count(0)
    # The work list shows the transfer work order as dispatched, kept apart from any receipt.
    expect(panel.get_by_test_id(f"work-{story['action']['action_id']}")).to_contain_text("Dispatched")

    # The transport details: the run, six milestones, planned times, Ready unscheduled.
    expect(transport.get_by_role("heading", name=f"Scheduled truck run · {story['source']} → {story['dest']}")).to_be_visible()
    expect(transport).to_contain_text(run["code"])
    expect(transport.get_by_test_id("milestones").locator("li")).to_have_count(6)
    expect(transport.get_by_test_id("milestone-loading_cutoff")).to_contain_text(hhmm(run["loading_cutoff_min"]))
    expect(transport.get_by_test_id("milestone-departure")).to_contain_text(hhmm(run["departs_min"]))
    expect(transport.get_by_test_id("milestone-ready")).to_contain_text("Unscheduled")
    assert not NOT_PROMISED.search(transport.inner_text()), NOT_PROMISED.search(transport.inner_text())

    # The source has nothing more to press: the destination records the receipt.
    expect(page.get_by_role("button", name="Record dispatch")).to_have_count(0)
    expect(page.get_by_test_id("waiting-line")).to_contain_text(story["dest"])


def test_the_tasks_link_by_case_id_lands_on_execute_with_the_dispatch_button(visitor):
    """Tasks links with the case id (Branch transfers with the key): either lands on Execute (?step=execute)."""
    page = visitor.page
    story = prepare_story(page)
    page.goto(f"/#/cases/{urllib.parse.quote(story['case_id'], safe='')}?step=execute")
    expect(panel_of(page)).to_have_attribute("data-step", "EXECUTE")
    expect(page.get_by_test_id("primary-slot").get_by_role("button", name="Record dispatch")).to_be_visible()
    expect(panel_of(page).get_by_test_id(f"work-{story['action']['action_id']}")).to_contain_text("Planned")


# --- Task 2: the receipt, the discrepancy banner and the reconcile form ----------------------------------------------

def record_price(page: Page, story: dict) -> None:
    """The source records the shelf price change of the story's markdown rest, when the plan has one."""
    if story["markdown"] is not None:
        post(page, case_path(story["case_id"], "price-applied"),
             {"action_id": story["markdown"]["action_id"], "confirmed": True})


def test_the_destination_records_a_receipt_from_empty_fields(visitor):
    page = visitor.page
    story = prepare_story(page)
    sent = dispatch_story(page, story)
    missing = sent - 100 - 2
    assert missing > 0, "the story dispatches more than 102 units"
    switch_seat(page, story["dest"])

    page.goto(execute_url(story["key"]))
    page.get_by_test_id("primary-slot").get_by_role("button", name="Record receipt").click()
    form = page.get_by_role("form", name="Record receipt")
    received = form.get_by_label("Units received in good condition")
    damaged = form.get_by_label("Units damaged")
    reason = form.get_by_label("Reason (required for a short or damaged receipt)")
    # Nothing is pre-filled with the dispatched or committed quantity (D-48; BUSINESS-FLOW-BRIEF §5).
    expect(received).to_have_value("")
    expect(damaged).to_have_value("")
    expect(reason).to_have_value("")
    expect(form.get_by_test_id("form-lot")).to_have_text(story["action"]["new_lot_id"])
    expect(reason).to_have_attribute("aria-required", "false")

    received.fill("100")
    damaged.fill("2")
    summary = form.get_by_test_id("receipt-summary")
    expect(summary).to_have_attribute("aria-live", "polite")
    expect(summary).to_contain_text(f"Dispatched {units(sent)} · Received 100 · Damaged 2 · Missing {units(missing)}")
    expect(summary).to_contain_text(
        "This receipt opens a discrepancy. The stock decision can't close until it is reconciled.")
    expect(reason).to_have_attribute("aria-required", "true")
    condition_of(form).select_option(label=story["condition_label"])

    # Without a reason the server refuses (400); its message sits next to the field, which is marked invalid.
    form.get_by_role("button", name="Record receipt").click()
    expect(reason).to_have_attribute("aria-invalid", "true")
    error_id = reason.get_attribute("aria-describedby").split()[-1]
    error = page.locator(f"[id='{error_id}']")
    expect(error).to_have_attribute("role", "alert")
    expect(error).to_contain_text("reason")

    reason.fill("Two cartons crushed; the rest did not arrive")
    form.get_by_role("button", name="Record receipt").click()
    banner = page.get_by_test_id("discrepancy-banner")
    expect(banner).to_contain_text("Receipt discrepancy—reconciliation required.")
    expect(banner).to_contain_text(f"Dispatched {units(sent)} · Received 100 · Damaged 2 · Missing {units(missing)}")
    expect(page.get_by_role("form", name="Record receipt")).to_have_count(0)
    # The destination records receipts only: no Reconcile form for it (UI-SPEC open question 2).
    expect(page.get_by_role("button", name="Reconcile discrepancy")).to_have_count(0)
    expect(page.get_by_role("form", name="Reconcile discrepancy")).to_have_count(0)
    # Dispatched and received stay separate in the movement record (D-65).
    record = page.get_by_test_id("movement-record").locator("li")
    expect(record.nth(1)).to_contain_text(f"{units(sent)} units dispatched")
    expect(record.nth(2)).to_contain_text("Partially received")
    expect(record.nth(2)).to_contain_text("100 units received")


def test_the_owner_reconciles_a_short_receipt_and_the_decision_moves_to_verify(visitor):
    page = visitor.page
    story = prepare_story(page)
    sent = dispatch_story(page, story)
    record_price(page, story)
    switch_seat(page, story["dest"])
    post(page, case_path(story["case_id"], "receipt"), {
        "action_id": story["action"]["action_id"], "received_qty": sent - 4, "damaged_qty": 0,
        "condition": story["condition"], "reason": "Four cartons not on the truck"})
    resolutions = {r["key"]: r["label"] for r in story["options"]["resolutions"]}
    switch_seat(page, story["source"])  # the owner (it took ownership in setup)

    page.goto(execute_url(story["key"]))
    banner = page.get_by_test_id("discrepancy-banner")
    expect(banner).to_contain_text("Receipt discrepancy—reconciliation required.")
    expect(banner).to_contain_text(f"Dispatched {units(sent)} · Received {units(sent - 4)} · Damaged 0 · Missing 4")
    expect(page.get_by_role("navigation", name="Steps", exact=True).locator("[aria-current=step]")).to_contain_text(
        "7. Execute")

    for resolution, n in (("found_at_destination", 3), ("lost_or_written_off", 1)):
        page.get_by_test_id("primary-slot").get_by_role("button", name="Reconcile discrepancy").click()
        form = page.get_by_role("form", name="Reconcile discrepancy")
        units_field = form.get_by_label("Units", exact=False).first
        expect(units_field).to_have_value("")
        form.get_by_label("Resolution").select_option(label=resolutions[resolution])
        units_field.fill(str(n))
        form.get_by_label("Reason").fill(f"{n} of the missing cartons settled")
        form.get_by_role("button", name="Record reconciliation").click()
        expect(page.get_by_role("form", name="Reconcile discrepancy")).to_have_count(0)

    expect(page.get_by_test_id("discrepancy-banner")).to_have_count(0)
    expect(page.get_by_role("navigation", name="Steps", exact=True).locator("[aria-current=step]")).to_contain_text(
        "8. Verify")
    gaps = get(page, case_path(story["case_id"], "execution"))["discrepancies"]
    assert [g["state"] for g in gaps] == ["RECONCILED"], gaps


DISCREPANCY_GONE = "This discrepancy was already reconciled. Choose another one."


def field(form, name: str):
    """A record form's field by its label: the name, then "(required)"."""
    return form.get_by_label(re.compile(rf"^{name}\b"))


def two_gaps(page: Page) -> tuple[dict, str, str]:
    """A receipt 2 short and 2 damaged opens two discrepancies of 2 units each; ends signed in as the owner. Returns
    (story, the id the form chooses first, the other id), in the server's order."""
    story = prepare_story(page)
    sent = dispatch_story(page, story)
    record_price(page, story)
    switch_seat(page, story["dest"])
    post(page, case_path(story["case_id"], "receipt"), {
        "action_id": story["action"]["action_id"], "received_qty": sent - 4, "damaged_qty": 2,
        "condition": story["condition"], "reason": "Two cartons crushed, two not on the truck"})
    switch_seat(page, story["source"])  # the owner
    gaps = get(page, case_path(story["case_id"], "execution"))["discrepancies"]
    assert sorted(g["kind"] for g in gaps) == ["DAMAGED", "SHORT"], gaps
    assert [g["open_units"] for g in gaps] == [2, 2], gaps
    return story, gaps[0]["discrepancy_id"], gaps[1]["discrepancy_id"]


def open_units(page: Page, case_id: str) -> dict[str, float]:
    """Each discrepancy's open units by id, from the execution view."""
    return {g["discrepancy_id"]: g["open_units"] for g in get(page, case_path(case_id, "execution"))["discrepancies"]}


def test_a_discrepancy_reconciled_elsewhere_is_never_swapped_for_another(visitor):
    """The reconcile draft stays bound to the discrepancy the person chose (quick 261018-4fi): when another window
    reconciles it, the form clears the draft, says so and sends nothing until the person picks again; it never
    falls back to another open discrepancy."""
    page = visitor.page
    story, first, other = two_gaps(page)
    resolution = story["options"]["resolutions"][0]
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/reconcile") else None)

    page.goto(execute_url(story["key"]))
    page.get_by_test_id("primary-slot").get_by_role("button", name="Reconcile discrepancy").click()
    form = page.get_by_role("form", name="Reconcile discrepancy")
    picker = field(form, "Discrepancy")
    expect(picker).to_have_value(first)
    field(form, "Units").fill("2")  # typed for the first discrepancy ...
    picker.select_option(value=other)  # ... then the person picks the other one
    expect(field(form, "Units")).to_have_value("")  # a new pick starts that discrepancy's own draft
    form.get_by_label("Resolution").select_option(value=resolution["key"])
    field(form, "Units").fill("1")
    field(form, "Reason").fill("One crushed carton written off")

    # Another window reconciles the chosen discrepancy in full; this tab becomes visible again and refetches.
    post(page, case_path(story["case_id"], "reconcile"), {
        "discrepancy_id": other, "resolution": resolution["key"], "units": 2,
        "reason": "Settled in another window"})
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.get_by_test_id("discrepancy-banner").locator("li")).to_have_count(1)

    # The unchanged draft is never sent for the discrepancy that is still open.
    form.get_by_role("button", name="Record reconciliation").click()
    page.wait_for_timeout(1000)
    still = open_units(page, story["case_id"])
    assert still == {first: 2, other: 0}, still
    assert posts == [], posts

    # The form says so, the draft is cleared and nothing is chosen for the person.
    expect(form.get_by_text(DISCREPANCY_GONE, exact=True)).to_be_visible()
    expect(picker).to_have_value("")
    expect(form.get_by_label("Resolution")).to_have_value("")
    expect(field(form, "Units")).to_have_value("")
    expect(field(form, "Reason")).to_have_value("")

    # Picking again binds the form to that discrepancy, and only then is anything recorded.
    picker.select_option(value=first)
    expect(form.get_by_text(DISCREPANCY_GONE, exact=True)).to_have_count(0)
    form.get_by_label("Resolution").select_option(value=resolution["key"])
    field(form, "Units").fill("1")
    field(form, "Reason").fill("One carton found")
    form.get_by_role("button", name="Record reconciliation").click()
    expect(page.get_by_role("form", name="Reconcile discrepancy")).to_have_count(0)
    after = open_units(page, story["case_id"])
    assert after == {first: 1, other: 0} and len(posts) == 1, (after, posts)


# --- Task 3: dispatch blocks, the price change, Keep, after-dispatch evidence, the transport panel, overflow ---------

def insert_hold(run: str, lot_id: str, branch: str, reason: str) -> None:
    """An ACTIVE lot hold (Phase 7's table) on one lot of this visitor's sandbox only."""
    with db_tx() as conn:
        conn.execute(sa.text(
            "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, state) "
            "VALUES (:r, :h, :l, :b, 'QUALITY', :why, 'HQ', 'ACTIVE')"),
            {"r": run, "h": f"H-{uuid.uuid4().hex[:8]}", "l": lot_id, "b": branch, "why": reason})


def insert_flag(run: str, case_id: str, reason: str) -> None:
    """An uncleared review flag (Phase 7's table) on one stock decision of this visitor's sandbox only."""
    with db_tx() as conn:
        conn.execute(sa.text(
            "INSERT INTO review_flags (run_id, flag_id, case_id, fact_id, reason) VALUES (:r, :f, :c, :fact, :why)"),
            {"r": run, "f": f"RF-{uuid.uuid4().hex[:8]}", "c": case_id, "fact": f"F-{uuid.uuid4().hex[:8]}",
             "why": reason})


def no_side_scroll(page: Page) -> None:
    """The document fits the viewport once the layout settled (the sidebar's width transition)."""
    page.wait_for_function(
        "() => document.documentElement.scrollWidth <= window.innerWidth "
        "&& document.querySelector('main#content').scrollWidth <= document.querySelector('main#content').clientWidth",
        timeout=5_000)


def test_dispatch_blocks_replace_the_form_with_every_reason(visitor):
    page = visitor.page
    story = prepare_story(page)
    run = run_id(page)
    insert_hold(run, story["action"]["lot_id"], story["source"], "Temperature log missing for this lot")
    insert_flag(run, story["case_id"], "Count at the source disagrees with the record")
    blocks = get(page, case_path(story["case_id"], "execution"))["dispatch"]["blocks"]
    kinds = [b["kind"] for b in blocks]
    assert "lot_hold" in kinds and "review_flag" in kinds, kinds

    page.set_viewport_size({"width": 360, "height": 800})
    page.goto(execute_url(story["key"]))
    banner = page.get_by_test_id("dispatch-blocked")
    expect(banner).to_contain_text("Dispatch blocked")
    rows = banner.get_by_test_id("dispatch-block")
    expect(rows).to_have_count(len(blocks))  # one row per block (zero-one-many)
    for i, b in enumerate(blocks):
        expect(rows.nth(i)).to_contain_text(b["reason"])  # word for word
        expect(rows.nth(i)).to_contain_text(f"Resolve: {b['resolution']}")
    hold = rows.nth(kinds.index("lot_hold"))
    expect(hold).to_contain_text("Lot hold")
    flag = rows.nth(kinds.index("review_flag"))
    expect(flag).to_contain_text("New confirmed information · review required")
    expect(flag).to_contain_text("Review before dispatch")
    # No Record dispatch button while blocked, and never a bare disabled one (D-49).
    expect(page.get_by_role("button", name="Record dispatch")).to_have_count(0)
    expect(page.get_by_role("form", name="Record dispatch")).to_have_count(0)
    no_side_scroll(page)


def test_missing_checks_never_read_as_clear(visitor):
    page = visitor.page
    story = prepare_story(page)
    unavailable = {"kind": "checks_unavailable", "id": None, "lot_ids": [story["action"]["lot_id"]],
                   "owner_branch": None, "reason": "Dispatch checks unavailable",
                   "resolution": "Dispatch stays blocked until the checks can run."}

    def answer(route):
        # The holds table reported missing: the view answers only the checks_unavailable block (06.1-07).
        response = route.fetch()
        body = response.json()
        body["dispatch"] = {"allowed": False, "blocks": [unavailable]}
        route.fulfill(response=response, json=body)

    page.route("**/api/cases/*/execution", answer)
    page.goto(execute_url(story["key"]))
    banner = page.get_by_test_id("dispatch-blocked")
    expect(banner).to_contain_text("Dispatch checks unavailable")
    expect(banner).to_contain_text("Resolve: Dispatch stays blocked until the checks can run.")
    expect(page.get_by_role("button", name="Record dispatch")).to_have_count(0)


def test_the_price_change_is_recorded_once_the_box_is_ticked(visitor):
    page = visitor.page
    story = prepare_story(page)
    md = story["markdown"]
    assert md is not None, "the story's plan marks its rest down (06.1-11)"
    sent = dispatch_story(page, story)
    switch_seat(page, story["dest"])
    post(page, case_path(story["case_id"], "receipt"), {
        "action_id": story["action"]["action_id"], "received_qty": sent, "damaged_qty": 0,
        "condition": story["condition"], "reason": ""})
    switch_seat(page, story["source"])

    page.goto(execute_url(story["key"]))
    page.get_by_test_id("primary-slot").get_by_role("button", name="Record price change").click()
    form = page.get_by_role("form", name="Record price change")
    expect(form).to_contain_text(f"Mark lot {md['lot_id']} down {md['pct']}% at {md['origin'] or story['source']}.")
    submit = form.get_by_role("button", name="Record price change")
    tick = form.get_by_label("I changed the shelf price for this lot")
    expect(tick).not_to_be_checked()
    expect(submit).to_be_disabled()
    expect(form).to_contain_text("Tick the box to record.")
    tick.check()
    expect(submit).to_be_enabled()
    expect(form).not_to_contain_text("Tick the box to record.")
    submit.click()
    expect(page.get_by_role("navigation", name="Steps", exact=True).locator("[aria-current=step]")).to_contain_text(
        "8. Verify")
    expect(panel_of(page).get_by_test_id(f"work-{md['action_id']}")).to_contain_text("Done")


def test_a_keep_decision_has_nothing_to_record(visitor):
    page = visitor.page
    switch_seat(page, None)  # HQ reads every store's stock decisions
    keep = next((c for c in cards_json(page) if c["suggested_key"] == "keep"), None)
    assert keep is not None, "the template has a stock decision whose recommendation is Keep (06.1-12 D3)"
    page.goto(execute_url(keep["key"]))
    panel = panel_of(page)
    expect(panel).to_contain_text("Nothing to record. The store's markdown schedule prices this lot.")
    expect(panel).to_contain_text("No transport assigned.")
    expect(panel.get_by_test_id("transport-panel")).to_have_count(0)
    expect(page.get_by_role("form")).to_have_count(0)


def test_evidence_after_dispatch_is_an_exception_that_recalls_nothing(visitor):
    page = visitor.page
    story = prepare_story(page)
    dispatch_story(page, story)
    insert_flag(run_id(page), story["case_id"], "Count at the source disagrees with the record")
    page.goto(execute_url(story["key"]))
    banner = page.get_by_test_id("after-dispatch")
    expect(banner).to_contain_text("New confirmed information · operational exception · review")
    expect(banner).to_contain_text("The commitment stands. Stock already moved is not recalled.")
    expect(banner).to_contain_text("Count at the source disagrees with the record")
    expect(page.get_by_test_id("dispatch-blocked")).to_have_count(0)


@pytest.mark.parametrize("width", (360, 390, 768, 1280))
def test_execute_shows_the_transport_details_and_never_scrolls_sideways(visitor, width):
    page = visitor.page
    story = prepare_story(page)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(execute_url(story["key"]))
    transport = panel_of(page).get_by_test_id("transport-panel")
    chips = transport.get_by_test_id("run-chips")
    expect(chips).to_contain_text("Own fleet (assumed)")
    expect(chips).to_contain_text("Logistics coordinator (represented)")
    expect(transport).to_contain_text("Tote use per unit: not stored")
    expect(transport.get_by_role("button", name=re.compile("Origin · load"))).to_contain_text(story["source"])
    expect(transport.get_by_role("button", name=re.compile("Destination · receive"))).to_contain_text(story["dest"])
    expect(page.get_by_test_id("planned-note")).to_have_count(1)
    expect(transport.get_by_test_id("milestone-received")).to_contain_text("Unscheduled")
    assert not NOT_PROMISED.search(transport.inner_text()), NOT_PROMISED.search(transport.inner_text())
    no_side_scroll(page)


# --- a successful Try again finishes the write like the first press (quick 261017-wzs finding 4) ---------------------

BUSY = {"error": "db_busy", "message": "The server is busy. Try again in a moment."}


def busy_once(page: Page, path: str) -> dict:
    """Answer the first POST to `path` with 503 db_busy (it never reaches the server) and let every later one through;
    returns the count of the POSTs seen."""
    seen = {"n": 0}

    def handle(route):
        if route.request.method != "POST":
            route.fallback()
            return
        seen["n"] += 1
        if seen["n"] == 1:
            route.fulfill(status=503, json=BUSY)
        else:
            route.fallback()

    page.route(path, handle)
    return seen


def test_a_dispatch_that_succeeds_on_try_again_closes_the_form_and_shows_it_dispatched(visitor):
    page = visitor.page
    story = prepare_story(page)
    sent = int(story["action"]["qty"]) - 6
    seen = busy_once(page, "**" + case_path(story["case_id"], "dispatch"))
    page.goto(execute_url(story["key"]))
    page.get_by_test_id("primary-slot").get_by_role("button", name="Record dispatch").click()
    form = page.get_by_role("form", name="Record dispatch")
    form.get_by_label("Units dispatched").fill(str(sent))
    condition_of(form).select_option(label=story["condition_label"])
    form.get_by_role("button", name="Record dispatch").click()
    expect(form.get_by_text(BUSY["message"], exact=True)).to_be_visible()
    form.get_by_role("button", name="Try again").click()

    expect(page.get_by_role("form", name="Record dispatch")).to_have_count(0)
    panel = panel_of(page)
    movement = panel.get_by_test_id("transport-panel").get_by_test_id("movement-box")
    expect(movement.get_by_test_id("movement-chip")).to_have_text("Dispatched")
    expect(panel.get_by_test_id(f"work-{story['action']['action_id']}")).to_contain_text("Dispatched")
    expect(panel).to_have_attribute("data-step", case_json(page, story["case_id"])["step"])  # the server's step
    expect(page.get_by_test_id("waiting-line")).to_contain_text(story["dest"])
    assert seen["n"] == 2


def test_a_receipt_refused_as_already_recorded_offers_reload_the_stock_decision(visitor):
    """A 409 under an execution form (here wrong_state: another window recorded the receipt first) offers "Reload the
    stock decision", which closes the form and shows the server's state (Codex check; quick 261018-01e leftover)."""
    page = visitor.page
    story = prepare_story(page)
    sent = dispatch_story(page, story)
    switch_seat(page, story["dest"])
    page.goto(execute_url(story["key"]))
    page.get_by_test_id("primary-slot").get_by_role("button", name="Record receipt").click()
    form = page.get_by_role("form", name="Record receipt")
    form.get_by_label("Units received in good condition").fill(str(sent))
    form.get_by_label("Units damaged").fill("0")
    condition_of(form).select_option(label=story["condition_label"])
    post(page, case_path(story["case_id"], "receipt"), {  # the other window's receipt
        "action_id": story["action"]["action_id"], "received_qty": sent, "damaged_qty": 0,
        "condition": story["condition"]})
    form.get_by_role("button", name="Record receipt").click()
    reload_button = form.get_by_role("button", name="Reload the stock decision", exact=True)
    expect(reload_button).to_be_visible()
    reload_button.click()
    expect(page.get_by_role("form", name="Record receipt")).to_have_count(0)
    expect(page.get_by_test_id("primary-slot").get_by_role("button", name="Record receipt")).to_have_count(0)


def test_try_again_never_resends_a_press_for_another_discrepancy(visitor):
    """A press for one discrepancy that failed (503) is dropped when the person picks another one: Try again goes,
    and nothing is recorded for the first (Codex check 4fi)."""
    page = visitor.page
    story, first, other = two_gaps(page)
    resolution = story["options"]["resolutions"][0]
    seen = busy_once(page, "**" + case_path(story["case_id"], "reconcile"))
    page.goto(execute_url(story["key"]))
    page.get_by_test_id("primary-slot").get_by_role("button", name="Reconcile discrepancy").click()
    form = page.get_by_role("form", name="Reconcile discrepancy")
    expect(field(form, "Discrepancy")).to_have_value(first)
    form.get_by_label("Resolution").select_option(value=resolution["key"])
    field(form, "Units").fill("1")
    field(form, "Reason").fill("One carton found")
    form.get_by_role("button", name="Record reconciliation").click()
    expect(form.get_by_role("button", name="Try again")).to_be_visible()

    field(form, "Discrepancy").select_option(value=other)
    expect(form.get_by_role("button", name="Try again")).to_have_count(0)
    expect(form.get_by_text(BUSY["message"], exact=True)).to_have_count(0)
    gaps = {g["kind"]: g for g in get(page, case_path(story["case_id"], "execution"))["discrepancies"]}
    assert [gaps[k]["open_units"] for k in ("SHORT", "DAMAGED")] == [2, 2] and seen["n"] == 1, (gaps, seen)
