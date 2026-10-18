"""The judge's path on a running URL (06-CONTEXT D-20; 06.1 D-47, D-53), on whichever dataset the target serves (05.2
D-37 B; quick 261017-v6z). The HQ planner signs in (the probe: its session's `kind` names the dataset, and the mark is
checked), the reviewer case's source store is read from the board, then that dataset's path runs:
- public: tests/web's public judge's path (tests/web/test_public_journey.py) in a context of its own, signed in as the
  source store of the first verify case, else of the first transfer case (the start seat, 05.2 D-38);
- synthetic: HQ signs out, and the WEB_REF's store journey (tests/web/test_journey.py judges_journey: sign-in to Close
  by the story's two stores) runs with the story's (milk-story) source store.
Both go through the browser and the public API only, with no database check. Each run opens two sandboxes on the public
set (the probe and the path) and one on the synthetic set (the store sign-in keeps the probe's workspace).

The HQ sign-in uses the UI-SPEC "Hand-off selectors" verbatim: the radio "HQ planner", the label "HQ passcode", the
button "Sign in", the navigation "Main", and the profile button /^Account:/ with the menuitem "Sign out". The two gaps
06.1-34 found (the Condition select and Record price change) are carried by judges_journey itself, not here.

This file imports WEB_REF test code (judges_journey, nothing and sign_out from tests.web.test_journey; new_context, Web
and HQ from tests.web.conftest; the public path from tests.web.test_public_journey), so it runs after the WEB_REF
merge, under `make compose-check` and the release's journey step. Both credentials come from
tests/deployed/conftest.py (SMOKE_PASSCODE and SMOKE_HQ_PASSCODE, or hidden prompts), never from argv, files or logs.
"""
import dataclasses
import os
import re
import time

import pytest
from playwright.sync_api import Browser, Page, expect

from deploy import smoke
from tests.web import conftest as web_conftest
from tests.web import test_public_journey
from tests.web.conftest import HQ, Web, new_context  # ASSUMPTION A10 (06.1-HANDOFF)
from tests.web.test_journey import judges_journey, nothing  # ASSUMPTION A10 (06.1-HANDOFF)
from tests.web.test_journey import sign_out as ui_sign_out  # ASSUMPTION A10 (06.1-HANDOFF)

pytestmark = pytest.mark.deployed

SCENARIO_MARK = "Scenario date:"  # ASSUMPTION A11 (06.1-HANDOFF): the deployed release data's mark
SIGNED_IN_S = 30


def _session(page: Page) -> dict | None:
    r = page.request.get("/api/session")
    return r.json() if r.status == 200 else None


def _hq_sign_in(page: Page, web: Web) -> dict:
    """Sign in on the card as the HQ planner through the hand-off selectors; the session JSON once it names HQ."""
    page.goto("/")
    page.get_by_role("radio", name="HQ planner").check()
    page.get_by_label("HQ passcode").fill(web.hq_passcode)
    page.get_by_role("button", name="Sign in", exact=True).click()
    deadline = time.monotonic() + SIGNED_IN_S
    while (session := _session(page)) is None or session.get("account") != HQ:
        assert time.monotonic() < deadline, "the session never named the HQ account"
        page.wait_for_timeout(100)
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()
    if os.environ.get("SS_JOURNEY_LOCAL") == "1":  # the local Compose app's template may carry no scenario date
        # a public label ends with the scenario date the UI formats from data_as_of: its words before that are shown
        shown = session["label"].split(" · Scenario date")[0]
        expect(page.get_by_text(shown).filter(visible=True).first).to_be_visible()
    else:
        expect(page.get_by_text(SCENARIO_MARK).filter(visible=True).first).to_be_visible()
    return session


def _story_source(page: Page, tag: str) -> str | None:
    """The source store of the first card tagged `tag`, paging GET /api/cases through the page's own cookie (smoke's
    bound); None when the board has none."""
    for number in range(1, smoke.MAX_PAGES + 1):
        r = page.request.get(f"/api/cases?page={number}")
        assert r.status == 200, r.status
        board = r.json()
        cards = board.get("cases") or []
        for card in cards:
            if card.get("tag") == tag:
                assert card.get("source"), f"the {tag} card has no source"
                return card["source"]
        if not cards or number * int(board.get("page_size") or 0) >= int(board.get("total") or 0):
            break
    return None


def _reviewer_source(page: Page, tags: tuple[str, ...]) -> str:
    """The source store of the first card tagged tags[0], else tags[1], and so on; fails naming the tags."""
    for tag in tags:
        if (source := _story_source(page, tag)) is not None:
            return source
    pytest.fail(f"no card tagged {' or '.join(tags)} on the board", pytrace=False)


def test_hq_signs_in_then_the_judges_journey_runs_on_the_deployed_url(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        kind = _hq_sign_in(page, web).get("kind")
        assert kind in ("public", "synthetic"), kind
        if kind == "public":  # the public path signs in on its own context (05.2 D-38: the start seat)
            source = _reviewer_source(page, tuple(t for t in smoke.STORY_TAGS if t != smoke.STORY_TAG))
            context.close()
            public_web = dataclasses.replace(web, store=source)
            web_conftest._RUNNING[:] = [public_web]  # its switch_seat signs in with this target's credentials
            try:
                test_public_journey.test_the_judges_path_on_the_public_dataset(browser, public_web)
            finally:
                web_conftest._RUNNING.clear()
            return
        source = _reviewer_source(page, (smoke.STORY_TAG,))
        ui_sign_out(page, nothing)
        expect(page.get_by_role("button", name=re.compile(r"^Sign in$"))).to_be_visible()
        judges_journey(page, dataclasses.replace(web, store=source), nothing)
    finally:
        context.close()
