"""Public About release cards (11-CONTEXT D-05, D-06, D-08): API values, static prose first, no cloud calls."""
from __future__ import annotations

import pytest
from datetime import date
from playwright.sync_api import expect

from engine.model import ENGINE_VERSION
from tests.web.conftest import new_context
from tests.web.test_freeze import REQUIRED_SHIPPED

pytestmark = pytest.mark.web


def check_live_versions(page):
    response = page.request.get("/api/about")
    assert response.status == 200
    about = response.json()
    expect(page.get_by_test_id("about-version-engine")).to_have_text(ENGINE_VERSION)
    expect(page.get_by_test_id("about-version-dataset")).to_have_text(about["dataset_version"])
    expect(page.get_by_role("heading", name="About this prototype", exact=True)).to_be_visible()
    return about


def test_about_versions_signed_out(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        held = []
        page.route("**/api/about", lambda route: held.append(route))
        page.goto("/#/about")
        expect(page.get_by_role("heading", name="About this prototype", exact=True)).to_be_visible()
        expect(page.get_by_text("Loading release details…", exact=True).first).to_be_visible()
        page.wait_for_timeout(1000)
        for route in held:
            route.fulfill(response=route.fetch())
        page.unroute("**/api/about")
        about = check_live_versions(page)
        if about["label"]:
            label = about["label"]
            if about["data_as_of"]:
                label = label.replace("{date}", date.fromisoformat(about["data_as_of"]).strftime("%a %-d %b %Y"))
            expect(page.locator("article footer").get_by_test_id("dataset-mark")).to_have_text(label)
    finally:
        context.close()


def test_about_versions_signed_in(visitor):
    page = visitor.page
    page.goto("/#/about")
    expect(page.get_by_role("navigation", name="Main")).to_be_visible()
    check_live_versions(page)

WEEKCHECK_TEXT = [
    "Synthetic retail scenarios with a weekday profile derived from FreshRetailNet-50K and explicitly stated demand, ordering and markdown assumptions.",
    "In the synthetic 5-seed engineering check, delivery sizing from in-stock days only is biased low: when the order is 90–100% of the estimate, the in-stock days are the lower-demand days, which produces the slow rise in stockouts shown in the synthetic check.",
    "In public FreshRetailNet-50K histories, sales during stockout hours are censored and cannot by themselves establish uncensored demand; stockout-day imputation for forecast training is an explicit modeling assumption.",
]
BENCHMARK_SCENARIOS = ["Modeled store rule with original records", "Modeled store rule with equivalent stock checks",
                       "ShelfShift decisions with those checks", "Automatic markdowns only · no transfers or additional intervention"]


def test_checks_and_evaluations_not_published(visitor):
    page = visitor.page
    page.goto("/#/about")
    expect(page.locator('[data-about-section="checks"]').get_by_role("heading", name="Checks and evaluations", exact=True)).to_be_visible()
    expect(page.get_by_test_id("check-status-benchmark")).to_have_text("Evaluation not yet available")
    expect(page.get_by_test_id("check-status-ai-eval")).to_have_text("AI evaluation not yet published.")
    data = page.request.get("/api/about").json()
    expected = "In code · not run yet" if data["weekcheck_status"] == "in_code" else "Not part of this release"
    expect(page.get_by_test_id("check-status-weekcheck")).to_have_text(expected)
    expect(page.locator('[data-about-section="checks"] tbody tr')).to_have_count(3)
    benchmark = page.locator("#benchmark")
    expect(benchmark.get_by_text("Evaluation not yet available", exact=True)).to_be_visible()
    expect(benchmark.get_by_text("When published, it compares:", exact=True)).to_be_visible()
    for scenario in BENCHMARK_SCENARIOS:
        expect(benchmark.get_by_text(scenario, exact=True)).to_be_visible()
    assert not benchmark.locator("time").count()


def test_ai_evaluation_empty_block(visitor):
    page = visitor.page
    page.goto("/#/about")
    block = page.locator('[data-about-section="ai-evaluation"]')
    expect(block.get_by_role("heading", name="AI evaluation not yet published.", exact=True)).to_be_visible()
    expect(block.locator("table")).to_have_count(0)
    assert "accuracy" not in block.inner_text().lower()


def test_weekcheck_section(visitor):
    page = visitor.page
    page.goto("/#/about")
    section = page.locator('[data-about-section="weekcheck"]')
    expect(section.get_by_role("heading", name="Synthetic 5-seed engineering check", exact=True)).to_be_visible()
    expect(section.locator("p")).to_have_count(3)
    assert section.locator("p").all_text_contents() == WEEKCHECK_TEXT
    # D-06 locks '90–100%' in the explanatory paragraph; no result percentage is added.
    assert section.inner_text().count("%") == sum(p.count("%") for p in WEEKCHECK_TEXT)


def test_failed_about_read_keeps_the_static_prose_and_retries(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        page.route("**/api/about", lambda route: route.fulfill(status=500, json={"error": "internal", "message": "Something went wrong on our side."}))
        page.goto("/#/about")
        expect(page.get_by_role("heading", name="About this prototype", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Not yet proven", exact=True)).to_be_visible()
        card = page.locator('[data-about-section="versions"]')
        expect(card.get_by_role("alert")).to_contain_text("Something went wrong on our side.")
        page.unroute("**/api/about")
        card.get_by_role("button", name="Try again", exact=True).click()
        expect(page.get_by_test_id("about-version-engine")).to_have_text(ENGINE_VERSION)
    finally:
        context.close()


def test_real_now_and_not_in_release_signed_out(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        def with_forecast(route):
            data = route.fetch().json()
            data["forecast"] = {"headline": "Fixture provider mix", "providers": {"ai_forecast": 2}, "run_id": "fixture", "cutoff_date": "2026-10-13"}
            route.fulfill(json=data)
        page.route("**/api/about", with_forecast)
        page.goto("/#/about")
        block = page.locator('[data-about-section="real-now"]')
        expect(block).to_contain_text(ENGINE_VERSION)
        expect(block).to_contain_text("Demand forecasts precomputed through Google Cloud BigQuery ML: Fixture provider mix.")
        if "assistant" in REQUIRED_SHIPPED:  # Gemini briefs and Ask ship; field-report reading does not (release.ts)
            expect(block).to_contain_text("writes decision briefs · answers questions.")
            expect(block).not_to_contain_text("reads field reports")
        else:
            expect(block).not_to_contain_text("Gemini on Google Cloud")
        expect(page.get_by_text("Branch orders is not part of this release.", exact=True)).to_be_visible()
    finally:
        context.close()


def about_card(page, id):
    return page.locator(f'[data-about-section="{id}"]')


def test_simulated_cards(visitor):
    page = visitor.page
    page.goto("/#/about")
    card = about_card(page, "simulated")
    expect(card.get_by_role("heading", name="Forecast training inputs", exact=True)).to_be_visible()
    expect(card).to_contain_text("Full-price in-stock days are kept as recorded.")
    expect(card.get_by_role("heading", name="Benchmark sales replay", exact=True)).to_have_count(int("reports" in REQUIRED_SHIPPED))


def test_not_yet_proven_groups(visitor):
    page = visitor.page
    page.goto("/#/about")
    card = about_card(page, "not-yet-proven")
    expect(card.locator("h3")).to_have_text(["Data and assumptions", "Decisions and checks", "Running it for a real customer", "Not part of this release"])
    expect(card).to_contain_text("This dataset comes from online grocery. Its stockout patterns may differ from those in other retail settings.")
    expect(card).to_contain_text("point-in-time recovery")
    expect(card).to_contain_text("personal sign-in for every user")


def test_labels_in_real_tones(visitor):
    page = visitor.page
    page.goto("/#/about")
    card = about_card(page, "labels")
    for name in ["Forecast · precomputed", "Simulated", "Estimated"]:
        expect(card.get_by_text(name, exact=True)).to_have_class(__import__("re").compile("border-dashed"))
    expect(card.get_by_text("Assumption", exact=True)).to_have_class(__import__("re").compile("bg-gray-soft"))
    if "assistant" in REQUIRED_SHIPPED:
        expect(card.get_by_text("Precomputed · Gemini model ID", exact=True)).to_be_visible()
    else:
        expect(card).not_to_contain_text("Gemini")


def test_on_this_page_jumps(visitor):
    page = visitor.page
    page.set_viewport_size({"width": 360, "height": 800})
    page.emulate_media(reduced_motion="reduce")
    page.goto("/#/about")
    page.evaluate("window.__scrolls = []; Element.prototype.scrollIntoView = function(options) {window.__scrolls.push(options)}")
    nav = page.get_by_role("navigation", name="On this page", exact=True)
    links = nav.get_by_role("link")
    assert links.count() == 9
    assert all(link.get_attribute("href").startswith("#/about?s=") for link in links.all())
    nav.get_by_role("link", name="Labels", exact=True).click()
    expect(page.locator("h2#labels")).to_be_focused()
    assert page.evaluate("window.__scrolls.at(-1).behavior") == "auto"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    boxes = [link.bounding_box() for link in links.all()]
    assert len({round(box["y"]) for box in boxes if box}) > 1


@pytest.mark.parametrize('matches',[True,False])
def test_about_shows_the_published_benchmark(visitor, matches):
    from api import about, db
    from tests.api.test_about import benchmark_tables, seed_benchmark
    page=visitor.page
    with benchmark_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn)
            seed_benchmark(conn,tpl['dataset_version'],ENGINE_VERSION if matches else 'old-engine')
        data=page.request.get('/api/about').json()
        assert data['benchmark_status']=='published'
        page.goto('/#/about')
        b=data['benchmark']
        chip='Published' if b['version_match'] else 'Different version · not representative of the current app'
        expect(page.get_by_test_id('check-status-benchmark')).to_have_text(chip)
        expect(page.locator('[data-about-section="checks"]')).to_contain_text('run 18 Oct 2026 · commit 0123456789ab · dataset '+tpl['dataset_version'])
        block=page.locator('#benchmark')
        expect(block).to_contain_text('Simulated evaluation')
        expect(block).to_contain_text('5 stores')
        expect(block).not_to_contain_text('null')
        for name in BENCHMARK_SCENARIOS: expect(block).to_contain_text(name)
        expect(block).not_to_contain_text('Evaluation not yet available')


@pytest.mark.parametrize('with_publication',[False,True])
def test_about_ai_evaluation_states(visitor, with_publication):
    import pathlib
    from api import about, db
    from tests.api.test_about import eval_tables, seed_eval
    page=visitor.page
    reason='Safety gate failed · not published'
    with eval_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn)
            if with_publication: seed_eval(conn,tpl['dataset_version'])
            seed_eval(conn,tpl['dataset_version'],ident='p11-later',published=False,reason=reason,started='2026-10-18T05:00:00Z')
        page.goto('/#/about')
        block=about_card(page,'ai-evaluation')
        notice=block.get_by_role('status')
        expect(notice).to_have_text('The latest evaluation run (18 Oct 2026) is not published: '+reason+'.')
        if with_publication:
            expect(block.get_by_text('Different version · not representative of the current app',exact=True)).to_be_visible()
        optional=pathlib.Path(__file__).resolve().parents[2]/'web/src/evalsummary.tsx'
        if not optional.is_file() or not with_publication:
            # This is an explicit, skip-free absent-module assertion, not a claimed report render.
            empty=block.get_by_role('heading',name='AI evaluation not yet published.',exact=True)
            expect(empty).to_be_visible()
            expect(block.locator('table')).to_have_count(0)
            assert notice.evaluate('(el) => el.getBoundingClientRect().top') < empty.evaluate('(el)=>el.getBoundingClientRect().top')
        else:
            expect(block).to_contain_text('Preliminary evaluation · 10 scenarios × 3 runs')
        assert 'accuracy' not in block.inner_text().lower() and '%' not in block.inner_text()
