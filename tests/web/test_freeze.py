"""Independent freeze manifest and live gates (11-CONTEXT D-09, D-10, D-12).

This is the gate's own required-surface copy. Move a surface here and in release.ts only at a reviewed freeze.
"""
from __future__ import annotations

import pathlib
import re
import urllib.parse

import pytest
from playwright.sync_api import expect
from tests.web.conftest import new_context, switch_seat
from tests.web.test_states import PAGES, registry_paths

pytestmark = pytest.mark.web
ROOT = pathlib.Path(__file__).resolve().parents[2]
REQUIRED_SHIPPED = {
    'today':'/', 'board':'/board', 'decision':'/cases/:key', 'schedule':'/schedule', 'tasks':'/tasks',
    'transfers':'/transfers', 'stock':'/stock', 'analytics':'/analytics', 'forecasts':'/forecasts',
    'reports':'/reports', 'policies':'/policies', 'about':'/about',
    'events':'/events', 'verification':None,  # Phase 7 ships with the full version (07-19..07-22)
    'assistant':None,  # Phase 9's briefs and Ask ship with the full version (deploy session, 2026-10-18)
}
REQUIRED_NOT_IN_RELEASE = {
    'orders':None, 'task-detail':'/tasks/:id', 'reading':None,
}
PLACEHOLDERS = re.compile(r'coming soon|not implemented|\bTODO\b|\bTBD\b|placeholder|\blorem\b|Next day', re.I)


def test_manifest_matches_release_ts():
    source = (ROOT / 'web/src/release.ts').read_text()
    assert parse_manifest(source, 'SHIPPED') == REQUIRED_SHIPPED
    assert parse_manifest(source, 'NOT_IN_RELEASE') == REQUIRED_NOT_IN_RELEASE
    assert not REQUIRED_SHIPPED.keys() & REQUIRED_NOT_IN_RELEASE.keys()
    assert 'orders' in REQUIRED_NOT_IN_RELEASE
    assert not re.search(r'projected.stock', source, re.I)


@pytest.mark.parametrize('role', ['manager', 'hq'])
def test_shipped_routes_render_for_each_role(visitor, web, role):
    page = visitor.page
    if role == 'hq': switch_seat(page, None)
    nav = page.get_by_role('navigation', name='Main')
    expected_nav = {'#' + path for id, path in REQUIRED_SHIPPED.items() if id != 'decision' and path}
    assert set(nav.locator('a').evaluate_all('(links) => links.map(a => a.getAttribute("href"))')) == expected_nav
    for id, path in route_paths(page, web, role):
        show_route(page, path)
        expect(page.get_by_test_id('dataset-mark').first).to_be_visible()
        if id == 'tasks' and role == 'hq':
            expect(page.get_by_role('main')).to_contain_text("Tasks go to store managers. Transport reports for HQ are under Business events.")  # 07-19 (taskview.TASKS_HQ_P7)
        if id == 'policies' and role == 'manager':
            expect(page.get_by_role('main')).to_contain_text('Only HQ can change policies.')
            expect(page.get_by_role('main').locator('input')).to_have_count(0)


def test_signed_out_routes(browser, web):
    context = new_context(browser, web)
    try:
        page = context.new_page()
        for id, path in REQUIRED_SHIPPED.items():
            if path is None: continue  # a shipped surface with no page of its own
            path = path.replace(':key', 'signed-out-route')
            page.goto('/#' + path)
            if id == 'about':
                expect(page.get_by_role('heading', name='About this prototype', exact=True)).to_be_visible()
            else:
                expect(page.get_by_label('Store code', exact=True)).to_be_visible()
            assert urllib.parse.urlsplit(page.url).fragment == path
            expect(page.get_by_test_id('dataset-mark').first).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize('role', ['manager', 'hq'])
def test_not_in_release_is_absent(visitor, role):
    page = visitor.page
    if role == 'hq': switch_seat(page, None)
    nav = page.get_by_role('navigation', name='Main')
    expect(nav.get_by_role('link', name=re.compile(r'^(?:Orders|Branch orders|Task detail)$'))).to_have_count(0)
    for path in REQUIRED_NOT_IN_RELEASE.values():
        if path is None: continue
        page.goto('/#' + path.replace(':id', 'not-in-release'))
        expect(page.get_by_text('Page not found.', exact=True)).to_be_visible()
        expect(page.get_by_role('link', name='Back to Today', exact=True)).to_be_visible()
    page.goto('/#/about')
    expect(page.get_by_text('Branch orders is not part of this release.', exact=True)).to_be_visible()
    expect(page.get_by_role('main')).not_to_contain_text(re.compile(r'projected.stock chart', re.I))


def test_registry_pages_have_no_placeholder_text(visitor, web):
    page = visitor.page
    paths = dict(route_paths(page, web, 'manager'))
    registered = registry_paths()
    assert set(registered) == {path.replace('/cases/:key', '/cases') for path in REQUIRED_SHIPPED.values() if path}
    for id, path in paths.items():
        show_route(page, path)
        text = page.locator('main#content').inner_text()
        assert not PLACEHOLDERS.search(text), f'placeholder visible at {id}'


def parse_manifest(source, name):
    block = re.search(r'export const ' + re.escape(name) + r'\b[^=]*=\s*\[(.*?)\];', source, re.S)
    assert block is not None, f'{name} release manifest missing'
    pairs = re.findall(r'\bid\s*:\s*"([^"]+)"[^{}]*?\bpath\s*:\s*(null|"[^"]+")', block[1])
    result = {id: None if path == 'null' else path[1:-1] for id, path in pairs}
    assert len(result) == len(pairs), f'{name} contains a repeated surface'
    return result


def route_paths(page, web, role):
    response = page.request.get('/api/cases')
    assert response.status == 200
    cards = response.json()['cases']
    assert cards, 'a real case is required for the decision route gate'
    case_id = urllib.parse.quote(cards[0]['case_id'], safe='')
    paths = []
    for id, path in REQUIRED_SHIPPED.items():
        if path is None: continue
        path = path.replace(':key', case_id)
        if role == 'hq' and id in {'analytics', 'forecasts'}:
            path += '?branch=' + urllib.parse.quote(web.store, safe='')
        paths.append((id, path))
    return [*paths, ('board-table', '/board?view=table')]


def show_route(page, path):
    # A different page first forces the actual route loader, including when the current route is Today already.
    page.goto('/#/about')
    expect(page.get_by_role('heading', name='About this prototype', exact=True)).to_be_visible()
    page.goto('/#' + path)
    main = page.locator('main#content')
    expect(main).to_be_visible()
    key = '/cases' if path.startswith('/cases/') else path.split('?')[0]
    state = PAGES.get(key)
    if state is not None:
        expect(main.get_by_text(state.loading, exact=True)).to_have_count(0)
        expect(main.get_by_text(state.failed, exact=True)).to_have_count(0)
    expect(main.get_by_role('heading', level=1).first).to_be_visible()
    expect(main).not_to_contain_text('Page not found.')

from tests.data.test_honesty import denylist
from tests.web.conftest import public_only
from tests.web.test_paused import PAUSED, HEADING
from tests.web.test_forecasts import SHORT_PROVIDER, mark_date


def test_mark_on_every_state(visitor, browser, web):
    page = visitor.page
    for _, path in route_paths(page, web, 'manager'):
        show_route(page, path)
        expect(page.get_by_test_id('dataset-mark').first).to_be_visible()
    page.goto('/#/about')
    page.route(re.compile(r'.*/api/cases(?:\?.*)?$'), lambda route: route.fulfill(status=500, json={'error':'internal', 'message':'Something went wrong on our side.'}))
    page.goto('/#/board')
    expect(page.get_by_role('heading', name="The Decision board didn't load", exact=True)).to_be_visible()
    expect(page.get_by_test_id('dataset-mark').first).to_be_visible()
    page.goto('/#/unregistered-freeze-route')
    expect(page.get_by_text('Page not found.', exact=True)).to_be_visible()
    expect(page.get_by_test_id('dataset-mark').first).to_be_visible()
    context = new_context(browser, web)
    try:
        guest = context.new_page()
        guest.goto('/#/')
        expect(guest.get_by_label('Store code', exact=True)).to_be_visible()
        expect(guest.get_by_test_id('dataset-mark').first).to_be_visible()
        guest.goto('/#/about')
        expect(guest.get_by_role('heading', name='About this prototype', exact=True)).to_be_visible()
        expect(guest.get_by_test_id('dataset-mark').first).to_be_visible()
        guest.route('**/api/session', lambda route: route.fulfill(status=503, json=PAUSED))
        guest.goto('/#/')
        guest.reload()
        expect(guest.get_by_role('heading', name=HEADING, exact=True)).to_be_visible()
        expect(guest.get_by_test_id('dataset-mark').first).to_be_visible()
    finally:
        context.close()


@public_only
def test_series_provenance_everywhere(visitor):
    page = visitor.page
    data = page.request.get('/api/forecasts').json()
    assert data['available'] and data['items']
    page.goto('/#/forecasts')
    for item in data['items']:
        expect(page.get_by_test_id(f"forecast-provider-{item['sku']}")).to_have_text(short_provider(item['provider_codes']))
    expect(page.get_by_test_id('forecast-run')).to_contain_text(data['run']['id'])
    expect(page.get_by_test_id('forecast-run')).to_contain_text(mark_date(data['run']['history_to']))
    cards = page.request.get('/api/cases').json()['cases']
    details = [page.request.get('/api/cases/' + urllib.parse.quote(card['case_id'], safe='')).json() for card in cards]
    detail = next((d for d in details if d.get('result') is not None and d['versions']['forecast_series_providers']), None)
    assert detail is not None, 'a real recommended case is required for the Context gate'
    versions = detail['versions']
    page.goto('/#/cases/' + urllib.parse.quote(detail['case_id'], safe='') + '?step=context')
    line = page.get_by_test_id('context-forecast-provenance')
    expect(line).to_contain_text('Precomputed · ' + short_provider(versions['forecast_series_providers']))
    expect(line).to_contain_text(versions['forecast_run_id'])
    expect(line).to_contain_text('history to ' + versions['forecast_cutoff'])
    analytics = page.request.get('/api/analytics').json()
    series = analytics['series']
    assert series and series['forecast']
    page.goto('/#/analytics')
    title = 'Sales and forecast · ' + series['product']
    card = page.get_by_role('region', name=title, exact=True)
    codes = ordered_providers(p['provider'] for p in series['forecast'] if p['provider'])
    expect(card).to_contain_text(' · Precomputed · ' + short_provider(codes) + ' · run ' + series['forecast_run_id'])
    expect(card).to_contain_text('history to ' + mark_date(series['forecast_cutoff']))


@pytest.mark.parametrize('role', ['manager', 'hq'])
def test_no_ai_provenance_while_not_shipped(visitor, web, role):
    if {'assistant', 'reading'} & REQUIRED_SHIPPED.keys():
        pytest.skip('AI provenance is covered by the shipped-surface gate')
    page = visitor.page
    if role == 'hq': switch_seat(page, None)
    for id, path in route_paths(page, web, role):
        show_route(page, path)
        main = page.locator('main#content')
        # D-05 requires configured Gemini metadata, and D-07 names excluded AI surfaces on About.
        # Neither is an output claim: the legend and Real now must still offer no Gemini provenance.
        if id == 'about':
            expect(page.locator('[data-about-section="labels"]')).not_to_contain_text('Gemini')
            expect(page.locator('[data-about-section="real-now"]')).not_to_contain_text('Gemini on Google Cloud')
            expect(main).not_to_contain_text(re.compile(r'Precomputed · Gemini|Live · Gemini|Fallback ·'))
        else:
            expect(main).not_to_contain_text(re.compile(r'Gemini|Precomputed · Gemini|Live · Gemini|Fallback ·'))


def test_rendered_text_names_no_retailer(visitor, browser, web):
    names = denylist()
    page = visitor.page
    for role in ['manager', 'hq']:
        if role == 'hq': switch_seat(page, None)
        for id, path in route_paths(page, web, role):
            show_route(page, path)
            assert_no_private_names(names, page.locator('body').inner_text(), role + ':' + id)
    context = new_context(browser, web)
    try:
        guest = context.new_page()
        for path in ['/', '/about']:
            guest.goto('/#' + path)
            if path == '/': expect(guest.get_by_label('Store code', exact=True)).to_be_visible()
            else: expect(guest.get_by_role('heading', name='About this prototype', exact=True)).to_be_visible()
            assert_no_private_names(names, guest.locator('body').inner_text(), 'signed-out:' + path)
    finally:
        context.close()


def short_provider(codes):
    return ' + '.join(SHORT_PROVIDER.get(code, code) for code in codes)


def ordered_providers(codes):
    order = ['ai_forecast', 'arima_plus', 'local-seasonal-naive']
    return sorted(set(codes), key=lambda code: (order.index(code) if code in order else len(order), code))



def test_dom_denylist_rule_reports_an_index_without_private_text():
    # Artificial fixture only; never reads the user's list.
    entry = "private" + ".fixture"
    assert_no_private_names([entry], "xprivate.fixturex privateXfixture", "fixture-route")
    with pytest.raises(pytest.fail.Exception, match="denylist entry 1 at fixture-route") as failed:
        assert_no_private_names([entry], "PRIVATE.FIXTURE", "fixture-route")
    assert entry not in str(failed.value)

def assert_no_private_names(names, text, location):
    # The shared matching rule: entry text never reaches an assertion value or a log.
    for index, name in enumerate(names, 1):
        if re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text, re.I):
            pytest.fail(f'denylist entry {index} at {location}', pytrace=False)


@pytest.mark.parametrize('surface',['assistant','reading'])
def test_ai_provenance_when_shipped(visitor, surface):
    if surface not in REQUIRED_SHIPPED:
        pytest.skip(surface+' not in this release')
    page=visitor.page
    if surface=='assistant':
        from tests.web.test_assistant import assistant_states
        from tests.web.test_case import decision_href
        script=assistant_states.__wrapped__(visitor)('current')
        # Deliberately different from any configured model; provenance belongs to the stored output.
        script.output['model_id']='stored-model-'+'x'*80
        page.goto(decision_href(script.binding['case_id'],'compare'))
        brief=page.get_by_role('region',name='AI brief',exact=True)
        expect(brief.get_by_text('Precomputed',exact=True)).to_be_visible()
        expect(brief).to_contain_text('Gemini '+script.output['model_id']+' · Recorded ')  # the stored output's model
        script.state='generating';page.reload()
        expect(brief).to_contain_text('Template summary · AI brief being written')
        script.state='fallback';page.reload()
        expect(brief.get_by_text('Fallback · Gemini unavailable · figures from the engine',exact=True)).to_be_visible()
        expect(brief.get_by_test_id('template-summary')).to_have_text(script.summary)
        expect(brief.get_by_test_id('checked-brief')).to_have_count(0)
        page.set_viewport_size({'width':360,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    else:
        # Phase 10's evidence-first UI: use the real report controls and capture its own stored reading.
        readings=[]
        def capture(response):
            if response.request.method=='POST' and response.status==200 and '/read' in response.url:
                readings.append(response.json())
        page.on('response',capture)
        page.goto('/#/events')
        page.get_by_role('button',name='Report a fact',exact=True).click()
        page.get_by_label('Message (optional)',exact=True).fill('Please check the printed expiry on this lot.')
        page.get_by_role('button',name='Next · analyse report',exact=True).click()
        expect(page.get_by_role('heading',name='Review suggested fields',exact=True)).to_be_visible()
        assert readings, 'the reading UI did not use a stored API reading'
        stored=readings[-1];model=stored['model_id']
        label=('Precomputed' if stored.get('provenance')=='PRECOMPUTED' else 'Live')+' · Gemini '+model
        expect(page.get_by_text(label,exact=True)).to_be_visible()
        page.get_by_role('button',name='Close without submitting',exact=True).click()
        page.route(re.compile(r'.*/api/.*read.*'),lambda route:route.fulfill(status=503,json={'error':'ai_unavailable','message':'AI reading unavailable — enter or confirm fields manually'}))
        page.get_by_role('button',name='Report a fact',exact=True).click()
        page.get_by_label('Message (optional)',exact=True).fill('A different note to avoid reusing the previous reading.')
        page.get_by_role('button',name='Next · analyse report',exact=True).click()
        expect(page.get_by_text('AI reading unavailable — enter or confirm fields manually',exact=True)).to_be_visible()
