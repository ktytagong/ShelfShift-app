"""Public evaluation uses the real read-only API and the dedicated browser DB."""
from __future__ import annotations

import pytest
import re
from playwright.sync_api import expect

from agent.eval import store
from api import db
from tests.web.conftest import db_tx, new_context

pytestmark = pytest.mark.web
FINGERPRINT = "a" * 64
# Release B's About owns the section: its wrapper, its "not yet published" block and its notice.
# EvalSummary renders inside it only when GET /api/about reports a published evaluation.
SECTION = '[data-about-section="ai-evaluation"]'
LABEL = "Preliminary evaluation · 10 scenarios × 3 runs"


@pytest.fixture(autouse=True)
def empty_evals(web):
    with db_tx() as conn:
        for table in ("eval_publications", "eval_items", "eval_attempts", "eval_runs"):
            db.run(conn, f"DELETE FROM {table}")
    yield
    with db_tx() as conn:
        for table in ("eval_publications", "eval_items", "eval_attempts", "eval_runs"):
            db.run(conn, f"DELETE FROM {table}")


def seed(conn, eval_id="published", date="2026-10-17T10:00:00Z", publish=True):
    store.record_run(conn, eval_id, fingerprint=FINGERPRINT, model_id="offline-model",
                     build_sha="b" * 40, assistant_version="offline-assistant", status="COMPLETE",
                     engine_version="offline-engine", dataset_version="offline-dataset",  # as the runner records them
                     started_at=date, finished_at=date, reviewed_at=date, reviewed_by="private-reviewer",
                     usage={"calls": 30, "tokens": 1234})
    for scenario in range(1, 11):
        for run in (1, 2, 3):
            store.record_attempt(conn, eval_id, scenario, run, valid=True, model_calls=1,
                                 cache_hits=0, status="COMPLETE")
            items = [dict(item_id=f"quality-{n}", grp="assistant" if scenario <= 5 else "reading",
                          facet="quality", scorer="human", result="FAIL" if n == 5 else "PASS",
                          note="Reviewer <script>note</script> stays literal.", item=f"Quality item {n}",
                          scenario_title=f"Scenario {scenario}") for n in range(1, 6)]
            if scenario == 1:
                for gate in ("unsourced", "self_approval", "hidden_truth"):
                    for facet in ("behaviour", "protection"):
                        items.append(dict(item_id=f"{gate}-{facet}", grp="safety", gate=gate,
                                          facet=facet, scorer="script", result="PASS", note=""))
            store.record_items(conn, eval_id, scenario, run, items)
    if publish:
        return store.publish(conn, eval_id, FINGERPRINT)


def test_published_eval_on_about(browser, web):
    with db_tx() as conn:
        seed(conn)
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.goto("/#/about")
        expect(page.get_by_role("heading", name="AI evaluation", exact=True)).to_be_visible()
        section = page.locator(SECTION)
        expect(section.get_by_text(LABEL, exact=True)).to_be_visible()
        expect(section.get_by_text("Safety gate passed", exact=True)).to_be_visible()
        expect(section.get_by_text("4 of 5 · 1 failed", exact=True).first).to_be_visible()
        expect(section.locator(".font-mono").filter(has_text="bbbbbbbbbbbb")).to_be_visible()
        expect(section.locator(".font-mono").filter(has_text="aaaaaaaaaaaa")).to_be_visible()
        assert page.request.get("/api/session").status == 401
        assert not context.cookies()
    finally:
        context.close()


def test_web_database_guard(monkeypatch):
    from tests.web import conftest
    for name in ("production", "shelfshift_test", "bad-web_test", "_web_test"):
        monkeypatch.setattr(conftest.config, "DATABASE_URL", f"postgresql+pg8000://postgres@localhost/{name}")
        with pytest.raises(pytest.fail.Exception):
            conftest.web_database.__wrapped__()
    monkeypatch.setattr(conftest.config, "DATABASE_URL",
                        "postgresql+pg8000://postgres@localhost/shelfshift_reading_web_test")
    conftest.web_database.__wrapped__()


@pytest.fixture
def signed_out_page(browser, web):
    context = new_context(browser, web)
    yield context.new_page()
    context.close()


def show(page):
    if page.url.endswith("/#/about"):
        page.reload()
    else:
        page.goto("/#/about")
    section = page.locator(SECTION)
    expect(section.get_by_text("Loading the evaluation…", exact=True)).to_have_count(0)
    return section


def about_says_published(page):
    """Reach EvalSummary's defensive reader states: the release's /api/about refuses counts that do not add up."""
    def fulfill(route):
        about = route.fetch().json()
        route.fulfill(json={**about, "ai_eval_status": "published", "latest_unpublished": None})
    page.route("**/api/about", fulfill)


def test_not_yet_published(signed_out_page):
    section = show(signed_out_page)
    expect(section.get_by_role("heading", name="AI evaluation not yet published.", exact=True)).to_be_visible()
    expect(section.get_by_role("table")).to_have_count(0)
    expect(section.get_by_role("button")).to_have_count(0)
    assert "%" not in section.inner_text()


def blocked(conn, eval_id, date, status="CAPPED"):
    store.record_run(conn, eval_id, fingerprint=FINGERPRINT, status=status, started_at=date)
    with pytest.raises(store.Blocked) as refusal:
        store.publish(conn, eval_id, FINGERPRINT)
    return str(refusal.value)


def test_blocked_and_later_run(signed_out_page):
    with db_tx() as conn:
        reason = blocked(conn, "later", "2026-10-18T10:00:00Z")
        reason += " · Literal $& $$ <reason>"
        db.run(conn, "UPDATE eval_runs SET block_reason=:reason WHERE eval_id='later'", reason=reason)
    section = show(signed_out_page)
    expect(section.get_by_role("status")).to_have_text(f"The latest evaluation run (18 Oct 2026) is not published: {reason}.")
    expect(section.get_by_role("heading", name="AI evaluation not yet published.", exact=True)).to_be_visible()
    expect(section.get_by_text("Safety gate passed", exact=True)).to_have_count(0)
    with db_tx() as conn:
        seed(conn)
    section = show(signed_out_page)
    notice = section.get_by_role("note")
    expect(notice).to_have_text(f"The latest evaluation run (18 Oct 2026) is not published: {reason}.")
    expect(section.get_by_text("Safety gate passed", exact=True)).to_be_visible()
    expect(section.get_by_text("· run 17 Oct 2026", exact=True)).to_be_visible()
    assert notice.evaluate("e => e.compareDocumentPosition(document.querySelector('[data-about-section=\"ai-evaluation\"] h3')) & Node.DOCUMENT_POSITION_FOLLOWING")
    assert section.get_by_role("button").count() == 0


def save_snapshot(conn, summary):
    # Defensive contract states: publish() now refuses invalid and unfinished runs.
    # Seed a historic public snapshot to exercise the reader without weakening publish().
    db.run(conn, "UPDATE eval_publications SET summary=CAST(:s AS jsonb)", s=db.js(summary))


def test_mismatch_invalid_not_scored(signed_out_page):
    with db_tx() as conn:
        summary = seed(conn)
        group = summary["groups"][1]
        cells = group["scenarios"][0]["runs"]
        cells[0].update(p=0, a=0, f=0, x=0, result="INVALID")
        cells[1].update(p=4, a=5, f=0, x=0, result="NOT_SCORED")
        cells[2].update(p=0, a=0, f=0, x=5, result="NA")
        group.update(p=52, a=65, f=12, x=5)
        save_snapshot(conn, summary)
    about_says_published(signed_out_page)
    section = show(signed_out_page)
    expect(section.get_by_text("Run on an earlier assistant version · not representative of the current app", exact=True)).to_be_visible()
    expect(section.get_by_text("Invalid · not scored", exact=True)).to_be_visible()
    expect(section.get_by_role("heading", name="Not scored yet")).to_be_visible()
    expect(section.get_by_text("Run 2 · Scenario 1 · Not scored · 4 of 5", exact=True)).to_be_visible()
    expect(section.get_by_role("cell", name="Run 3: N/A", exact=True)).to_be_visible()
    expect(section.get_by_text("52 of 65 applicable items passed across 3 runs · 12 failed · 5 N/A", exact=True)).to_be_visible()
    expect(section.get_by_text("Reviewer <script>note</script> stays literal.", exact=True).first).to_be_visible()
    assert section.locator("script").count() == 0
    assert "%" not in section.inner_text()


def test_attempted_is_separate_from_executed_and_failures_stay_visible(signed_out_page):
    with db_tx() as conn:
        seed(conn, publish=False)
        db.run(conn, "UPDATE eval_items SET result='FAIL', note=:n WHERE facet='behaviour' AND run_no=1",
               n="The protection stopped <unsafe> output.")
        store.publish(conn, "published", FINGERPRINT)
    section = show(signed_out_page)
    safety = section.get_by_role("heading", name="Safety gate", exact=True).locator("..")
    expect(safety.get_by_text("Safety gate passed", exact=True)).to_be_visible()
    expect(safety.get_by_role("row", name=re.compile("Unsourced numbers")).get_by_role("cell", name="1 written")).to_be_visible()
    expect(safety.get_by_role("cell", name="0 shown", exact=True)).to_be_visible()
    expect(safety.get_by_role("cell", name="0 executed", exact=True)).to_be_visible()
    expect(safety.get_by_role("cell", name="0 exposed", exact=True)).to_be_visible()
    expect(safety.get_by_role("list", name="Safety failed items").get_by_role("listitem")).to_have_count(3)
    expect(safety.get_by_text("The protection stopped <unsafe> output.", exact=True)).to_have_count(3)
    expect(safety.get_by_text("Attempted, not executed", exact=True)).to_have_count(2)
    headings = section.locator("h3").all_text_contents()
    assert headings == ["Safety gate", "Assistant", "Reading"]


def test_zero_and_one_failure_rows(signed_out_page):
    with db_tx() as conn:
        seed(conn, publish=False)
        db.run(conn, "UPDATE eval_items SET result='PASS'")
        db.run(conn, "UPDATE eval_items SET result='FAIL' WHERE scenario_id=6 AND run_no=1 AND item_id='quality-5'")
        store.publish(conn, "published", FINGERPRINT)
    section = show(signed_out_page)
    expect(section.get_by_text("No failed items in this group.", exact=True)).to_have_count(2)
    expect(section.get_by_role("list", name="Reading failed items").get_by_role("listitem")).to_have_count(1)


@pytest.mark.parametrize("width", (360, 390, 768, 1280))
def test_grid_semantics_and_widths(signed_out_page, width):
    with db_tx() as conn:
        summary = seed(conn)
        summary["groups"][1]["scenarios"][0]["title"] = "LongScenario" * 40
        summary["groups"][1]["failures"][0]["note"] = "LongReviewerNote" * 100
        save_snapshot(conn, summary)
    page = signed_out_page
    page.set_viewport_size({"width": width, "height": 844})
    section = show(page)
    for group in ("Assistant", "Reading"):
        table = section.get_by_role("table", name=f"{group} results")
        expect(table).to_be_visible()
        expect(table.locator("thead th[scope=col]")).to_have_count(4)
        expect(table.locator("tbody th[scope=row]")).to_have_count(5)
        for cell in table.locator("tbody td").all():
            assert re.search(r"Pass|Fail|N/A|not scored", cell.inner_text()), cell.inner_text()
        assert table.locator("tbody td").first.get_attribute("aria-label") == "Run 1: 4 of 5 applicable items passed, 1 failed · Fail"
        box = table.locator("..")
        assert box.evaluate("e => ['auto','scroll'].includes(getComputedStyle(e).overflowX)")
        assert table.locator("tbody th").first.evaluate("e => e.getBoundingClientRect().width") >= 224
        assert table.locator("tbody td").first.evaluate("e => e.getBoundingClientRect().width") >= 72
        if width <= 390:
            assert box.evaluate("e => e.scrollWidth > e.clientWidth")
            box.focus()
            expect(box).to_be_focused()
            box.press("ArrowRight")
            page.wait_for_function("el => el.scrollLeft > 0", arg=box.element_handle())
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert "%" not in section.inner_text()
    expect(section.get_by_role("button")).to_have_count(0)


def test_loading_leaves_about_readable(signed_out_page):
    with db_tx() as conn:
        seed(conn)
    page = signed_out_page
    held = []
    page.route("**/api/eval/summary", lambda route: held.append(route))
    page.goto("/#/about")
    expect(page.get_by_text("Loading the evaluation…", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="About this prototype")).to_be_visible()
    assert held
    held[0].continue_()
    expect(page.locator(SECTION).get_by_text(LABEL, exact=True)).to_be_visible()


def test_failed_read_can_retry_with_get_only(signed_out_page):
    with db_tx() as conn:
        seed(conn)
    page = signed_out_page
    requests = []
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.route("**/api/eval/summary", lambda route: route.abort("failed"), times=1)
    section = show(page)
    expect(section.get_by_role("alert")).to_contain_text("Could not reach ShelfShift. Check your connection and try again.")
    retry = section.get_by_role("button", name="Try again", exact=True)
    retry.focus()
    retry.press("Enter")
    expect(section.get_by_text(LABEL, exact=True)).to_be_visible()
    expect(retry).to_have_count(0)
    eval_requests = [method for method, url in requests if url.endswith("/api/eval/summary")]
    assert eval_requests == ["GET", "GET"]
