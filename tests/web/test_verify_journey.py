"""Verify before you move, in the browser, on the tiny public set (07-23 truth 2; 07-CONTEXT D-02, D-03, D-22, D-26).

Runs with SS_WEB_DATASET=public_tiny (the conftest's public switch, 05.2-22). The verify-before-move case is found by
its catalogue tag; every expectation is read from the API (the case detail and its requests), never a key, a quantity
or an amount typed here. The path: the source manager presses Count stock and Send request on the case's count fact;
the lot's manager answers on Tasks with the record's own count and "Checked just now", then confirms it; the case
re-plans on the confirmed fact (a new version without that fact); the approvals each name the plan shown and carry
the acknowledgement the case asks for, and the plan is committed. Then, under Business events, the destination's
manager reports an observation by hand, previews it ("Potential impact · unconfirmed") and confirms it as its owner.
"""
from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Browser, expect

from tests.web.conftest import Web, case_json, new_context, public_only, sign_in
from tests.web.test_public_journey import (ACCEPT, APPROVE, SEND, SEND_REQUEST, TAKE, card_by_tag, open_by_route,
                                           press, seat, state_is, step_panel)
from tests.web.test_report_sheet import PREVIEW_LABEL, fill_capacity, open_sheet
from tests.web.test_tasks import (count_fact, count_input, checked_just_now, open_tasks_page, request_card,
                                  request_for)

pytestmark = [pytest.mark.web, public_only]

COUNT_STOCK = "Count stock"  # 07-CONTEXT D-36


def test_verify_before_move_journey(browser: Browser, web: Web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        s = sign_in(page, web)  # the start seat: the verify case's source store (05.2 D-38)
        store = s["branch"]
        verify = card_by_tag(page, "verify")
        cid = verify["case_id"]
        assert verify["source"] == store, (verify["source"], store)
        d = case_json(page, cid)
        fact = count_fact(d)

        # Count stock → Send request, on the case's count fact
        open_by_route(page, cid, "context")
        row = step_panel(page).locator(f"[data-fact-id='{fact['fact_id']}']")
        row.get_by_role("button", name=COUNT_STOCK, exact=True).click()
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/requests")) as sent:
            row.get_by_test_id("request-panel").get_by_role("button", name=SEND_REQUEST, exact=True).click()
        assert sent.value.status == 200, sent.value.status
        q = request_for(page, d, fact["fact_id"])
        assert q["status"].startswith("Awaiting response · respond by "), q["status"]
        expect(row.get_by_test_id("stock-check-status")).to_have_text(q["status"])
        assert "evidence_requested" in [b["kind"] for b in case_json(page, cid)["badges"]]

        # Tasks: the lot's manager counts the record's own units (the shelf), none in the backroom, "Checked just
        # now", and confirms
        assert len(q["lot_ids"]) == 1, "the tiny set's verify fact counts one lot"
        (lot,) = q["lot_ids"]
        units = fact["recorded_value"]
        assert units is not None and float(units).is_integer(), units
        seat(page, q["to_branch"])
        open_tasks_page(page)
        card = request_card(page, q)
        count_input(card, q, lot, "shelf").fill(str(int(units)))
        count_input(card, q, lot, "backroom").fill("0")
        checked_just_now(card)
        card.get_by_role("button", name="Send answer").click()
        expect(card.get_by_text("Answered · confirm the count", exact=True)).to_be_visible()
        card.get_by_label("I checked the shelf and the backroom for this lot").check()
        card.get_by_role("button", name="Confirm count").click()
        expect(page.get_by_test_id("task-status")).to_contain_text(f"Count confirmed by {q['to_branch']} manager")
        answer = request_for(page, d, fact["fact_id"])["answer"]
        assert answer["observed_at"] is not None  # the check time was stated

        # The case re-planned on the confirmed fact: a new version, the counted fact no longer in question
        seat(page, store)
        after = case_json(page, cid)
        assert after["versions"]["rec_version"] != d["versions"]["rec_version"], "no new version after the confirm"
        assert fact["fact_id"] not in [f["fact_id"] for f in after["fact_checks"]], after["fact_checks"]
        assert after["dest"] is not None and after["commitment"]["blocks"] == [], (
            after["suggested_key"], after["commitment"]["blocks"])

        # The approvals: each decision names the plan shown and carries the case's acknowledgement (D-26), then the
        # plan is committed
        decisions: list[dict] = []
        page.on("request", lambda r: decisions.append(json.loads(r.post_data))
                if r.method == "POST" and r.url.endswith(f"/cases/{cid}/decision") else None)
        open_by_route(page, cid)
        press(page, TAKE, cid, lambda v: v["owner"] is not None and v["owner"]["account"] == store, "an owner")
        press(page, SEND, cid, state_is("PENDING_APPROVAL"), "PENDING_APPROVAL")
        dest = after["dest"]
        seat(page, dest)
        open_by_route(page, cid)
        press(page, ACCEPT, cid, lambda v: v["state"] == "PENDING_APPROVAL" and v["next_action"]["key"] == "approve",
              "the destination's acceptance")
        seat(page, store)
        open_by_route(page, cid)
        press(page, APPROVE, cid, state_is("APPROVED"), "APPROVED")
        shown = case_json(page, cid)
        # (writes.decisionBody sends `acknowledged` only while the plan in force has Advisory facts; none is null)
        assert [(b["decision"], b["plan_key"], b.get("acknowledged")) for b in decisions] == [
            ("accept", shown["suggested_key"], after["commitment"]["ack"]),
            ("approve", shown["suggested_key"], after["commitment"]["ack"])], decisions

        # Business events → Report an observation → preview → the owner confirms
        seat(page, dest)
        dialog = open_sheet(page)
        fill_capacity(page, dialog, after["zone"], "0")
        dialog.get_by_role("button", name="Preview impact", exact=True).click()
        expect(dialog.get_by_test_id("preview").get_by_text(PREVIEW_LABEL, exact=True)).to_be_visible()
        assert case_json(page, cid)["state"] == "APPROVED"  # a preview changes and blocks nothing
        dialog.get_by_role("button", name="Submit report", exact=True).click()
        expect(dialog).to_have_count(0)
        first = page.get_by_test_id("report-row").first
        expect(first).to_contain_text(f"Awaiting confirmation by {dest} manager")
        first.get_by_role("link", name="Review report", exact=True).click()
        page.get_by_role("button", name="Confirm as reported fact", exact=True).click()
        expect(page.get_by_test_id("confirmed")).to_contain_text(
            re.compile(rf"Reported by {re.escape(dest)} manager · .+ · field report"))
    finally:
        context.close()
