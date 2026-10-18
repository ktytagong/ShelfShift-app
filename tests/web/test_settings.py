"""Policies and About (06.1-19; UI-SPEC "Policies" and "About"; D-03, D-05, D-53, D-63).

Policies: HQ edits the waste price and min sellable days inside the API's bounds (every min, max, unit and helper from
GET /api/settings, never pasted), saves with expected_version and an Idempotency-Key, and sees the saved line and the
new value after a reload. A store manager sees the same values as text with the HQ-only line and no input or Save
button; the server refuses a manager's write anyway (06.1-06). No demo wording is left on the page. The refusal paths:
out_of_bounds shows the server's message under its field; a stale page shows the stale line, reloads and keeps the
typed text. Nothing is resent on its own.

About: the 05.2-20 article, readable while signed out, plus "Two clocks" and "Operating model"; the footer shows the
mark and "Sign in" signed out, "Back to Today" signed in. Dingdong-Inc is named only as the dataset's creator.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.web.conftest import HQ, Visitor, new_context, session_json, sign_in

pytestmark = pytest.mark.web

FIELDS = {"waste_price": "Waste price", "min_sellable_days": "Min sellable days"}
STEP = {"waste_price": "0.01", "min_sellable_days": "1"}  # UI-SPEC: a cents step and a whole-day step
NOTE = "Changes every open stock decision in your workspace; an approval in progress on a changed plan is cancelled."
HQ_ONLY = "Only HQ can change policies. These values apply to every branch in your workspace."  # (proposed)
SERVER_HQ_ONLY = "Only HQ can change policies."  # the server's 403 (06.1-06, api/settings.py HQ_ONLY)
STALE = ("These values changed since you loaded this page. They were reloaded; your edits are kept in the fields.")
RECOMPUTING = re.compile(r"^Recomputing open stock decisions · \d+s$")
DEMO = re.compile(r"\bdemo\b", re.IGNORECASE)


@pytest.fixture
def hq(browser, web):
    """A new visitor signed in as the HQ planner (its own context, cookie and workspace)."""
    context = new_context(browser, web)
    page = context.new_page()
    sign_in(page, web, HQ)
    yield Visitor(context, page)
    context.close()


def saved_line(n: int) -> str:
    """The saved line for N recomputed open stock decisions (proposed wording, D-61 term)."""
    return "Saved. 1 open stock decision was recomputed." if n == 1 else f"Saved. {n} open stock decisions were recomputed."


def help_pattern(field: str, unit: str) -> str:
    """The helper under a field (UI-SPEC wording), its min and max captured."""
    tail = r"\. A retailer policy, not a cash cost\." if field == "waste_price" else r" on arrival\."
    return rf"Between ([\d,.]+) and ([\d,.]+) {re.escape(unit)}{tail}"


def number(text: str) -> float:
    return float(text.replace(",", ""))


def settings_json(page: Page) -> dict:
    """GET /api/settings through the page's own context (same cookie, same workspace)."""
    r = page.request.get("/api/settings")
    assert r.status == 200, r.status
    return r.json()


def field(page: Page, name: str):
    return page.get_by_label(FIELDS[name], exact=True)


def main(page: Page):
    return page.locator("main#content")


def save_button(page: Page):
    return page.get_by_role("button", name="Save policy", exact=True)


def open_policies(page: Page) -> None:
    """Open Policies from the sidebar and wait for its heading and its values."""
    nav = page.get_by_role("navigation", name="Main")
    nav.get_by_role("link", name="Policies", exact=True).click()
    expect(nav.get_by_role("link", name="Policies", exact=True)).to_have_attribute("aria-current", "page")
    expect(page.get_by_role("heading", level=1, name="Policies", exact=True)).to_be_visible()
    expect(page.get_by_test_id("policy-waste_price")).to_be_visible()


def wait_for(held: list, page: Page) -> None:
    """The route handler runs on Playwright's dispatcher; give it its turn until one request is held."""
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    assert len(held) == 1, held


def a_decimal_price(bounds: dict, share: int = 270) -> float:
    """A waste price inside the API's bounds with two decimals and a fraction (1/share of the range above its min)."""
    b = bounds["waste_price"]
    price = round(b["min"] + (b["max"] - b["min"]) / share, 2)
    assert b["min"] < price < b["max"] and price != int(price), (b, price)
    return price


def patches_of(page: Page) -> list:
    """Start recording the page's PATCH /api/settings requests; the list fills as they leave the browser."""
    seen: list = []
    page.on("request", lambda r: seen.append(r) if r.method == "PATCH" and r.url.endswith("/api/settings") else None)
    return seen


def flush(page: Page) -> None:
    """A round trip, so every request event the page fired has reached the test."""
    page.wait_for_timeout(300)
    page.evaluate("0")


def save_and_wait(page: Page) -> dict:
    """Press Save policy, wait for its 200 and the saved line; returns the PATCH response JSON."""
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/settings")) as got:
        save_button(page).click()
    assert got.value.status == 200, (got.value.status, got.value.text())
    out = got.value.json()
    expect(main(page).get_by_role("status")).to_have_text(saved_line(len(out["reevaluated"])))
    return out


# --- Policies -------------------------------------------------------------------------------------------------------

def test_hq_saves_a_policy_and_sees_it_after_a_reload(hq):
    page = hq.page
    loaded = settings_json(page)
    bounds, values = loaded["bounds"], loaded["values"]

    # The first load is held (the real request, released unmodified): the page reads "Loading policies…"
    held: list = []

    def hold_first_get(route) -> None:
        if route.request.method == "GET" and not held:
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/settings", hold_first_get)
    page.get_by_role("navigation", name="Main").get_by_role("link", name="Policies", exact=True).click()
    expect(page.get_by_text("Loading policies…", exact=True)).to_be_visible()
    wait_for(held, page)
    held[0].continue_()
    page.unroute("**/api/settings", hold_first_get)
    open_policies(page)
    expect(main(page)).to_contain_text(NOTE)
    expect(main(page)).not_to_contain_text(HQ_ONLY)

    # Each field's min, max, step, unit and helper come from the GET response's bounds; shortlist_step is not shown
    for name in FIELDS:
        b = bounds[name]
        box = field(page, name)
        expect(box).to_be_editable()
        expect(box).to_have_attribute("type", "number")
        expect(box).to_have_attribute("required", "")
        expect(box).to_have_attribute("step", STEP[name])
        assert number(box.get_attribute("min")) == b["min"] and number(box.get_attribute("max")) == b["max"], \
            (name, box.get_attribute("min"), box.get_attribute("max"), b)
        assert number(box.input_value()) == values[name], (name, box.input_value(), values[name])
        text = page.get_by_test_id(f"help-{name}").inner_text()
        m = re.fullmatch(help_pattern(name, b["unit"]), text)
        assert m, (name, text, b)
        assert (number(m.group(1)), number(m.group(2))) == (b["min"], b["max"]), (name, text, b)
    expect(main(page)).not_to_contain_text(re.compile("shortlist", re.IGNORECASE))
    expect(save_button(page)).to_be_enabled()

    # A decimal waste price inside the bounds; min sellable days stays at its loaded value
    price = a_decimal_price(bounds)
    assert price != values["waste_price"]
    field(page, "waste_price").fill(f"{price:.2f}")

    # Save policy: the PATCH is held (released unmodified) while the form shows Elapsed and is disabled
    patches: list = []

    def hold_patch(route) -> None:
        if route.request.method == "PATCH":
            patches.append(route)
        else:
            route.continue_()

    page.route("**/api/settings", hold_patch)
    save_button(page).click()
    wait_for(patches, page)
    expect(page.get_by_text(RECOMPUTING)).to_be_visible()
    for name in FIELDS:
        expect(field(page, name)).to_be_disabled()
    expect(save_button(page)).to_be_disabled()
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/settings")) as got:
        patches[0].continue_()
    page.unroute("**/api/settings", hold_patch)
    response = got.value
    assert response.status == 200, (response.status, response.text())
    request = response.request
    assert request.post_data_json == {
        "expected_version": loaded["version"],
        "changes": {"waste_price": price, "min_sellable_days": values["min_sellable_days"]},
    }, request.post_data_json
    assert (request.headers.get("idempotency-key") or "").strip(), request.headers
    out = response.json()
    assert out["values"]["waste_price"] == price and out["version"] > loaded["version"], out
    expect(main(page).get_by_role("status")).to_have_text(saved_line(len(out["reevaluated"])))
    expect(field(page, "waste_price")).to_be_enabled()
    assert session_json(page)["version"] == out["version"]

    # After a reload the page shows the saved value
    page.reload()
    expect(page.get_by_role("heading", level=1, name="Policies", exact=True)).to_be_visible()
    expect(field(page, "waste_price")).to_be_visible()
    assert number(field(page, "waste_price").input_value()) == price
    assert settings_json(page)["values"]["waste_price"] == price
    assert not DEMO.search(main(page).inner_text()), main(page).inner_text()


def test_a_store_manager_sees_the_policies_read_only(visitor):
    page = visitor.page
    s = session_json(page)
    assert s["role"] == "manager", s["role"]
    loaded = settings_json(page)
    values = loaded["values"]
    open_policies(page)

    # The same values as text, with the HQ-only line; no input, no Save button, no effect note
    expect(main(page)).to_contain_text(HQ_ONLY)
    expect(main(page)).not_to_contain_text(NOTE)
    expect(main(page).locator("input, select, textarea")).to_have_count(0)
    expect(save_button(page)).to_have_count(0)
    expect(main(page).get_by_role("button")).to_have_count(0)

    waste = page.get_by_test_id("policy-waste_price").inner_text()
    m = re.search(r"(\d[\d,]*\.\d{2})/kg", waste)
    assert m and number(m.group(1)) == values["waste_price"], (waste, values)
    assert "Waste price" in waste, waste
    days = page.get_by_test_id("policy-min_sellable_days").inner_text()
    n = values["min_sellable_days"]
    assert "Min sellable days" in days and f"{n:g} {'day' if n == 1 else 'days'}" in days, (days, values)
    # The bounds the values sit in, from the API
    for name in FIELDS:
        text = page.get_by_test_id(f"help-{name}").inner_text()
        m = re.fullmatch(help_pattern(name, loaded["bounds"][name]["unit"]), text)
        assert m, (name, text)
    assert not DEMO.search(main(page).inner_text()), main(page).inner_text()

    # The server is the control: a manager's write is refused with its reason, and nothing changes
    r = page.request.patch("/api/settings", headers={"Idempotency-Key": "06.1-19-manager-write"},
                           data={"expected_version": loaded["version"], "changes": {"waste_price": values["waste_price"]}})
    assert r.status == 403 and r.json()["message"] == SERVER_HQ_ONLY, (r.status, r.text())
    assert settings_json(page)["version"] == loaded["version"]


def test_a_refused_value_shows_the_server_message_under_its_field(hq):
    page = hq.page
    open_policies(page)
    loaded = settings_json(page)
    b = loaded["bounds"]["waste_price"]
    sent = patches_of(page)
    box = field(page, "waste_price")

    # The browser blocks Save on an empty or an out-of-range value: nothing is sent
    box.fill("")
    save_button(page).click()
    assert box.evaluate("el => el.validity.valueMissing") is True
    box.fill(f"{b['max'] + 1:g}")
    save_button(page).click()
    assert box.evaluate("el => el.validity.rangeOverflow") is True
    flush(page)
    assert sent == [], [r.url for r in sent]

    # Without the native max, the server refuses: its out_of_bounds message shows under Waste price; nothing is resent
    box.evaluate("el => el.removeAttribute('max')")
    with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/settings")) as got:
        save_button(page).click()
    assert got.value.status == 400, got.value.status
    refusal = got.value.json()
    assert refusal["error"] == "out_of_bounds" and refusal["field"] == "waste_price", refusal
    expect(page.locator("#setting-waste_price-error")).to_have_text(refusal["message"])
    expect(box).to_have_attribute("aria-invalid", "true")
    expect(field(page, "min_sellable_days")).not_to_have_attribute("aria-invalid", "true")
    expect(main(page).get_by_role("status")).to_have_count(0)
    flush(page)
    assert len(sent) == 1, [r.url for r in sent]
    assert settings_json(page)["version"] == loaded["version"]


def test_a_stale_policies_page_keeps_your_edits(hq):
    page_a = hq.page
    page_b = hq.context.new_page()  # the same HQ visitor (same cookie, same workspace) in a second tab
    page_b.goto("/#/policies")
    expect(page_b.get_by_role("heading", level=1, name="Policies", exact=True)).to_be_visible()
    expect(field(page_b, "waste_price")).to_be_visible()
    loaded = settings_json(page_b)
    bounds = loaded["bounds"]

    # Tab A saves a new waste price and a new min sellable days (each computed from the bounds)
    days = bounds["min_sellable_days"]
    new_days = loaded["values"]["min_sellable_days"] + (1 if loaded["values"]["min_sellable_days"] < days["max"] else -1)
    open_policies(page_a)
    field(page_a, "waste_price").fill(f"{a_decimal_price(bounds, 270):.2f}")
    field(page_a, "min_sellable_days").fill(str(new_days))
    a_out = save_and_wait(page_a)
    assert a_out["version"] > loaded["version"]

    # Tab B, still on the version it loaded, types another price and saves: the stale line (never the server's demo
    # wording), sent once; the values reload (A's min sellable days shows) and B's typed price stays in its input
    typed = f"{a_decimal_price(bounds, 130):.2f}"
    sent = patches_of(page_b)
    field(page_b, "waste_price").fill(typed)
    with page_b.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/api/settings")) as got:
        save_button(page_b).click()
    assert got.value.status == 409 and got.value.json()["error"] == "stale_version", got.value.text()
    expect(main(page_b).get_by_role("alert")).to_have_text(STALE)
    expect(field(page_b, "min_sellable_days")).to_have_value(str(new_days))
    expect(field(page_b, "waste_price")).to_have_value(typed)
    assert not DEMO.search(main(page_b).inner_text()), main(page_b).inner_text()
    flush(page_b)
    assert len(sent) == 1, [r.url for r in sent]

    # A second save from B succeeds on the new version, with its own key and B's typed price
    b_out = save_and_wait(page_b)
    assert len(sent) == 2, [r.url for r in sent]
    second = sent[1]
    assert second.post_data_json == {
        "expected_version": a_out["version"],
        "changes": {"waste_price": float(typed), "min_sellable_days": new_days},
    }, second.post_data_json
    assert second.headers["idempotency-key"] != sent[0].headers["idempotency-key"]
    assert b_out["values"]["waste_price"] == float(typed) and b_out["version"] > a_out["version"], b_out
    expect(main(page_b).get_by_role("alert")).to_have_count(0)


def test_min_sellable_days_reads_day_or_days(hq):
    """The suffix beside Min sellable days follows the field's number: "day" for exactly 1, "days" otherwise, from the
    loaded value and then from what HQ types (G-05-12 (e)). Nothing is saved."""
    page = hq.page
    open_policies(page)
    loaded = settings_json(page)
    sent = patches_of(page)
    suffix = page.get_by_test_id("unit-min_sellable_days")
    n = loaded["values"]["min_sellable_days"]
    expect(suffix).to_have_text("day" if n == 1 else "days")
    expect(page.get_by_test_id("unit-waste_price")).to_have_text(loaded["bounds"]["waste_price"]["unit"])

    box = field(page, "min_sellable_days")
    box.fill("2")
    expect(suffix).to_have_text("days")
    box.fill("1")
    expect(suffix).to_have_text("day")
    flush(page)
    assert sent == [], [r.url for r in sent]
    assert settings_json(page)["version"] == loaded["version"]


# --- About ----------------------------------------------------------------------------------------------------------

TWO_CLOCKS = ("Recorded is when a person acted, in Bangkok time. Scenario date is the operational date of the public "
              "sales scenario. It does not move with the calendar.")  # (proposed) UI-SPEC "About"
OPERATING_MODEL = ("The roles, routines and nine steps are a proposed operating model for this prototype, not a "
                   "retailer's practice.")  # (proposed) UI-SPEC "About"; D-05
ARTICLE_11 = ("Versions", "Checks and evaluations", "AI evaluation", "Synthetic 5-seed engineering check",
              "Real now", "Simulated", "Supported options", "Not yet proven", "Labels", "The dataset")


def about_section(page: Page, title: str):
    """The About section whose heading is `title`."""
    return page.locator("article section").filter(has=page.get_by_role("heading", name=title, exact=True))


def expect_the_article(page: Page) -> None:
    """The Phase 11 release cards retain the dataset credits, two clocks and proposed operating model."""
    expect(page.get_by_role("heading", level=1, name="About this prototype", exact=True)).to_be_visible()
    expect(page.locator("article section h2")).to_have_text(list(ARTICLE_11))
    dataset = about_section(page, "The dataset")
    expect(dataset.get_by_role("heading", level=3)).to_have_text(
        ["The data", "Modifications", "Limits", "Two clocks", "Operating model"]
    )
    expect(dataset).to_contain_text(TWO_CLOCKS)
    expect(dataset).to_contain_text(OPERATING_MODEL)
    # Dingdong-Inc is named only where the dataset is credited to its creator (CC BY 4.0 attribution)
    for title in ARTICLE_11:
        text = about_section(page, title).inner_text()
        if title == "The dataset":
            assert "FreshRetailNet-50K by Dingdong-Inc" in text, text
        else:
            assert "Dingdong" not in text, (title, text)
    expect(page.locator("article footer").get_by_test_id("dataset-mark")).to_be_visible()


def test_about_signed_out_shows_the_two_clocks_the_operating_model_and_sign_in(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/#/about")
        expect_the_article(page)
        footer = page.locator("article footer")
        expect(footer.get_by_role("link", name="Sign in", exact=True)).to_have_attribute("href", "#/")
        expect(footer.get_by_role("link", name="Back to Today", exact=True)).to_have_count(0)
    finally:
        context.close()


def test_about_signed_in_shows_back_to_today(visitor):
    page = visitor.page
    page.goto("/#/about")
    expect_the_article(page)
    footer = page.locator("article footer")
    expect(footer.get_by_role("link", name="Back to Today", exact=True)).to_have_attribute("href", "#/")
    expect(footer.get_by_role("link", name="Sign in", exact=True)).to_have_count(0)
