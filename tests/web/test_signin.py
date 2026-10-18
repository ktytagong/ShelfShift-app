"""PROD-01 (06.1 D-02, D-47; UI-SPEC "Sign-in"): the sign-in card replaces the passcode gate. A store manager signs in
with a store code and the store passcode, the HQ planner with HQ's own passcode; every refusal shows its line under the
right field; the credential never reaches storage or the URL. Credentials are this run's generated values: they are
typed into the card and compared only as booleans, never printed in a test name, a log line or an assertion message.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from tests.web.conftest import new_context, session_json, sign_in, sign_out

pytestmark = pytest.mark.web


def test_store_manager_signs_in(browser, web):
    """A new visitor sees "Store manager" chosen, types the store code in lower case and the store passcode, presses
    Sign in and lands in the app as that store's manager. The server resolves the typed code to the reference spelling
    (06.1-05: case-insensitive match), so the account is the store code itself."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        expect(page.get_by_role("radio", name="Store manager")).to_be_checked()
        expect(page.get_by_role("radio", name="HQ planner")).not_to_be_checked()
        page.get_by_label("Store code").fill(web.store.lower())
        page.get_by_label("Passcode", exact=True).fill(web.passcode)
        page.get_by_role("button", name="Sign in", exact=True).click()

        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        s = session_json(page)
        assert s["account"] == web.store, s["account"]
        assert (s["role"], s["branch"]) == ("manager", web.store), (s["role"], s["branch"])
    finally:
        context.close()


# --- HQ, every refusal, the busy states and the credential kept out of storage (Task 2) -------------------------------

NETWORK = "Could not reach ShelfShift. Check your connection and try again."  # UI-SPEC "Interaction states" (proposed)
BAD_PASSCODE = "That passcode is not right"  # the server's sentence, the same for both account kinds (contract)
STORE_ERROR, PASS_ERROR = "#signin-store-error", "#signin-error"


def is_sign_in_post(r) -> bool:
    return r.request.method == "POST" and r.url.endswith("/api/session")


def stored_text(page) -> str:
    """Everything the page keeps in storage plus its URL, as one string (never printed)."""
    return page.evaluate("() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage), "
                         "location.href])")


def held_posts(page) -> list:
    """Hold every POST /api/session in the browser until the test releases it with route.continue_() (the request and
    its answer then pass through unmodified); any other call to the route continues at once."""
    held = []

    def hold(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()

    page.route("**/api/session", hold)
    return held


def wait_held(page, held: list) -> None:
    for _ in range(200):
        if held:
            return
        page.wait_for_timeout(50)  # lets Playwright deliver the intercepted request to the handler
    raise AssertionError("POST /api/session never left the browser")


def test_hq_signs_in_with_its_own_passcode(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        page.get_by_role("radio", name="HQ planner").check()
        expect(page.get_by_label("Store code")).to_have_count(0)
        expect(page.get_by_label("Passcode", exact=True)).to_have_count(0)
        hq = page.get_by_label("HQ passcode")
        expect(hq).to_have_value("")
        hq.fill(web.hq_passcode)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        s = session_json(page)
        assert (s["account"], s["role"], s["branch"]) == ("HQ", "hq", None), (s["account"], s["role"], s["branch"])
    finally:
        context.close()


def test_the_store_passcode_does_not_open_hq(browser, web):
    """A wrong credential shows the server's sentence under the passcode field, marks it invalid and keeps focus."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        page.get_by_role("radio", name="HQ planner").check()
        hq = page.get_by_label("HQ passcode")
        hq.fill(web.passcode)  # the store passcode: never HQ's (D-47)
        with page.expect_response(is_sign_in_post) as answer:
            page.get_by_role("button", name="Sign in", exact=True).click()
        assert answer.value.status == 401 and answer.value.json()["error"] == "bad_passcode", answer.value.status
        expect(page.locator(PASS_ERROR)).to_have_text(BAD_PASSCODE)
        expect(hq).to_have_attribute("aria-invalid", "true")
        expect(hq).to_be_focused()
        expect(page.locator(PASS_ERROR).get_by_role("button")).to_have_count(0)  # no Try again on a refusal
        expect(page.get_by_role("navigation", name="Main")).to_have_count(0)
    finally:
        context.close()


def test_an_unknown_store_code_is_named_under_store_code(browser, web):
    """With the right store passcode, a code that is no store of the reference data gets the server's sentence under
    Store code, word for word, with focus there; nothing opens."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        code = page.get_by_label("Store code")
        code.fill("NO SUCH STORE 0")
        page.get_by_label("Passcode", exact=True).fill(web.passcode)
        with page.expect_response(is_sign_in_post) as answer:
            page.get_by_role("button", name="Sign in", exact=True).click()
        body = answer.value.json()
        assert answer.value.status == 400 and body["error"] == "unknown_account", answer.value.status
        expect(page.locator(STORE_ERROR)).to_have_text(body["message"])
        expect(code).to_have_attribute("aria-invalid", "true")
        expect(code).to_be_focused()
        expect(page.locator(PASS_ERROR)).to_have_count(0)
        expect(page.get_by_role("navigation", name="Main")).to_have_count(0)
    finally:
        context.close()


def test_a_first_sign_in_shows_opening_your_workspace(browser, web):
    """A new visitor's sign-in creates the workspace: the held POST shows "Opening your workspace…" with the elapsed
    counter, the button disabled and the inputs read-only; released, the app opens."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        held = held_posts(page)
        code, passcode = page.get_by_label("Store code"), page.get_by_label("Passcode", exact=True)
        code.fill(web.store)
        passcode.fill(web.passcode)
        page.get_by_role("button", name="Sign in", exact=True).click()
        wait_held(page, held)
        busy = page.get_by_role("button", name="Opening your workspace…")
        expect(busy).to_be_disabled()
        expect(busy).to_have_text(re.compile(r"^Opening your workspace…\s*·\s*\d+s$"))
        expect(code).not_to_be_editable()
        expect(passcode).not_to_be_editable()
        held[0].continue_()
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        assert session_json(page)["account"] == web.store
    finally:
        context.close()


def test_after_a_lock_signing_in_again_shows_signing_in(browser, web):
    """A signed-in visitor whose session is signed out (here through the API, as another tab would) is sent back to
    the card by the next locked answer; the workspace is kept, so the held POST reads "Signing in…", and signing in
    returns to the same workspace."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        run_before = sign_in(page, web)["run_id"]
        sign_out(page)
        page.goto("/#/board")  # sign-in lands on Today, which does not poll (06.1-20): the board's read comes next
        code = page.get_by_label("Store code")
        expect(code).to_be_visible()  # the board's next read answered 401 locked
        expect(page.get_by_role("radio", name="Store manager")).to_be_checked()
        expect(code).to_have_value("")
        held = held_posts(page)
        code.fill(web.store)
        passcode = page.get_by_label("Passcode", exact=True)
        passcode.fill(web.passcode)
        page.get_by_role("button", name="Sign in", exact=True).click()
        wait_held(page, held)
        expect(page.get_by_role("button", name="Signing in…")).to_be_disabled()
        expect(code).not_to_be_editable()
        expect(passcode).not_to_be_editable()
        held[0].continue_()
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        assert session_json(page)["run_id"] == run_before, "signing in again opened another workspace"
    finally:
        context.close()


def test_the_credential_stays_out_of_storage_and_the_url(browser, web):
    """The card lists no store and pre-fills nothing; after a store sign-in and an HQ sign-in neither credential is in
    localStorage, sessionStorage or the URL (booleans only, so a failure never prints one)."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        expect(page.get_by_label("Store code")).to_have_value("")
        expect(page.get_by_label("Passcode", exact=True)).to_have_value("")
        expect(page.locator("datalist, select, option")).to_have_count(0)
        card_names_store = web.store.casefold() in page.locator("main").inner_text().casefold()
        assert not card_names_store, "the sign-in card names a store"

        sign_in(page, web)
        assert web.passcode not in stored_text(page), "the store passcode was kept in storage or the URL"
        sign_out(page)
        page.goto("/#/board")  # Today does not poll (06.1-20): the board's next read answers 401 and shows the card
        expect(page.get_by_role("radio", name="HQ planner")).to_be_visible()
        sign_in(page, web, "HQ")
        text = stored_text(page)
        leaked = web.hq_passcode in text or web.passcode in text
        assert not leaked, "a credential was kept in storage or the URL"
    finally:
        context.close()


def test_an_empty_sign_in_sends_nothing(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        sent = []
        page.on("request", lambda r: sent.append(r) if r.method == "POST" and r.url.endswith("/api/session") else None)
        page.goto("/")
        page.get_by_role("button", name="Sign in", exact=True).click()
        assert page.get_by_label("Store code").evaluate("e => e.validity.valueMissing") is True  # the browser blocked it
        page.get_by_label("Store code").fill(web.store)
        page.get_by_role("button", name="Sign in", exact=True).click()
        assert page.get_by_label("Passcode", exact=True).evaluate("e => e.validity.valueMissing") is True
        page.wait_for_timeout(500)  # long enough for a sent request to reach page.on("request")
        assert sent == [], "an empty sign-in was sent"
    finally:
        context.close()


def test_a_dropped_sign_in_offers_try_again_with_the_same_key(browser, web):
    """A POST dropped in the browser (an abort; the server never sees it) shows the network line with Try again under
    the passcode; Try again resends with the same Idempotency-Key and opens the app."""
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        keys = []

        def drop_first_post(route) -> None:
            if route.request.method == "POST":
                keys.append(route.request.headers.get("idempotency-key"))
                if len(keys) == 1:
                    route.abort()
                    return
            route.continue_()

        page.route("**/api/session", drop_first_post)
        page.get_by_label("Store code").fill(web.store)
        passcode = page.get_by_label("Passcode", exact=True)
        passcode.fill(web.passcode)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator(PASS_ERROR)).to_contain_text(NETWORK)
        expect(passcode).not_to_have_attribute("aria-invalid", "true")
        expect(passcode).to_be_focused()
        page.locator(PASS_ERROR).get_by_role("button", name="Try again", exact=True).click()
        expect(page.get_by_role("navigation", name="Main")).to_be_visible()
        assert len(keys) == 2 and keys[0] and keys[0] == keys[1], "Try again did not resend the same key"
    finally:
        context.close()


def test_a_reached_limit_shows_its_message_without_try_again(browser, web):
    """cap_reached shows the server's message word for word with no Try again. Reaching the real per-address limit
    would refuse every later sign-in of this run for an hour, so the answer is the API's own 429 envelope, built by
    api.caps, fulfilled in the browser for this one request."""
    from api import caps, config as api_config

    limit, window = api_config.CLIENT_CAPS["passcode"]
    refusal = caps._refusal("passcode", limit, window, "this address's", caps.window_start(window))
    envelope = {"error": refusal.code, "message": refusal.message, **refusal.extra}
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        page.route("**/api/session", lambda route: route.fulfill(status=429, json=envelope)
                   if route.request.method == "POST" else route.continue_())
        page.get_by_label("Store code").fill(web.store)
        passcode = page.get_by_label("Passcode", exact=True)
        passcode.fill(web.passcode)
        page.get_by_role("button", name="Sign in", exact=True).click()
        expect(page.locator(PASS_ERROR)).to_have_text(refusal.message)
        expect(page.locator(PASS_ERROR).get_by_role("button")).to_have_count(0)
        expect(passcode).to_be_focused()
    finally:
        context.close()


# The 05.2-20 About page, readable while signed out (moved from test_gate.py): the DATA-04 sentence, the "Not yet
# proven" list, the dataset's sections with its CC BY 4.0 attribution and links; reached from the card's About link.
DATA_04 = "Public sales from FreshRetailNet-50K, with simulated stock and labelled assumptions"
NOT_YET_PROVEN = (
    "There is no real retailer data.",
    "The markdown response and the cost of a trip that ships nothing are assumptions.",
    "Execution is simulated.",
    "The forecast does not learn from what happens in the app.",
    "Stock, lots and deliveries are simulated from the public sales.",
    # Phase 7's stated limitations, word for word (07-UI-SPEC surface 7; 07-22)
    "Fact checks test each unresolved fact on its own: a plan that survives each fact alone but fails when two are "
    "true together is not detected, so ShelfShift claims no complete feasibility protection.",
    "A lot with mixed expiry dates is held for people to separate; the app doesn't split it.",
    "Staff must state when they checked a count or a date; that stops silent misdating but can't prove a typed time "
    "accurate.",
)
DATA_SECTIONS = ("Simulated", "The dataset", "The data", "Modifications", "Limits")
ATTRIBUTION = ("© Dingdong-Inc, licensed under CC BY 4.0", "ShelfShift is not affiliated with or endorsed by Dingdong-Inc.")
LINKS = ("https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K", "https://creativecommons.org/licenses/by/4.0/")


def test_about_is_readable_from_the_card_without_signing_in(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/")
        expect(page.get_by_test_id("dataset-mark")).to_have_text("Public sales dataset")  # no session (05.2 D-04)
        page.get_by_role("link", name="About", exact=True).click()
        expect(page.get_by_role("heading", name="About this prototype", level=1)).to_be_visible()
        assert page.url.endswith("#/about")
        article = page.locator("article")
        expect(article).to_contain_text(DATA_04)
        for item in NOT_YET_PROVEN:
            expect(article.get_by_text(item, exact=True)).to_be_visible()
        for title in DATA_SECTIONS:
            expect(article.get_by_role("heading", name=title, exact=True)).to_be_visible()
        for line in ATTRIBUTION:
            expect(article).to_contain_text(line)
        for href in LINKS:
            expect(article.get_by_role("link", name=href, exact=True)).to_have_attribute("href", href)
        # Phase 11 names the real architecture and configured model. Unshipped AI surfaces are explicit exclusions.
        from tests.web.test_freeze import REQUIRED_SHIPPED
        for surface, name in (("assistant", "Gemini decision briefs and Ask"),
                              ("reading", "Gemini field-report reading")):
            if surface not in REQUIRED_SHIPPED:
                expect(article.get_by_text(f"{name} is not part of this release.", exact=True)).to_be_visible()
        page.go_back()
        expect(page.get_by_label("Store code")).to_be_visible()
        assert [c for c in context.cookies() if c["name"] == "ss_session"] == []  # no session was opened
    finally:
        context.close()
