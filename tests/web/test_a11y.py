"""UI-09: computed contrast, chart alternatives and reduced motion on real freeze routes."""
from __future__ import annotations

import pathlib
import pytest
from playwright.sync_api import expect
from tests.web.conftest import new_context, switch_seat
from tests.web.test_freeze import REQUIRED_SHIPPED, route_paths, show_route
from tests.web.test_paused import PAUSED, HEADING
from tests.web.test_analytics import units, expected, short_date, table_rows

pytestmark = pytest.mark.web
ROOT = pathlib.Path(__file__).resolve().parents[2]
OFF_WHITE = """() => {
  const opaque = el => { for (let n = el; n; n = n.parentElement) {
    const bg = getComputedStyle(n).backgroundColor;
    if (bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return bg;
  } return 'rgb(255, 255, 255)'; };
  const path = el => { const parts = []; for (let n=el; n && parts.length<4; n=n.parentElement) {
    parts.unshift(n.tagName.toLowerCase() + (n.id ? '#'+n.id : '') + '.' + [...n.classList].slice(0,3).join('.'));
  } return parts.join(' > '); };
  return [...document.querySelectorAll('body *')].filter(el =>
    el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' &&
    [...el.childNodes].some(n => n.nodeType===Node.TEXT_NODE && n.textContent.trim()) &&
    getComputedStyle(el).color === 'rgb(107, 113, 144)' && opaque(el) !== 'rgb(255, 255, 255)'
  ).map(el => path(el) + ' on ' + opaque(el));
}"""


@pytest.mark.parametrize('role', ['manager', 'hq'])
def test_no_tx3_text_off_white(visitor, browser, web, role):
    page = visitor.page
    if role == 'hq': switch_seat(page, None)
    failures = []
    for id, path in route_paths(page, web, role):
        show_route(page, path)
        failures += [f'{id}: {p}' for p in page.evaluate(OFF_WHITE)]
    if role == 'manager':
        context = new_context(browser, web)
        try:
            guest = context.new_page()
            guest.goto('/#/about')
            expect(guest.get_by_role('heading', name='About this prototype', exact=True)).to_be_visible()
            failures += ['signed-out About: ' + p for p in guest.evaluate(OFF_WHITE)]
            guest.route('**/api/session', lambda route: route.fulfill(status=503, json=PAUSED))
            guest.goto('/#/'); guest.reload()
            expect(guest.get_by_role('heading', name=HEADING, exact=True)).to_be_visible()
            failures += ['unavailable: ' + p for p in guest.evaluate(OFF_WHITE)]
        finally:
            context.close()
    assert not failures, "\n".join(failures)


def analytics_expected(data):
    rows = {'Sales by product': [[r['product'], units(r['units']), units(r['hours_out_of_stock'])] for r in data['sales_by_product']],
            'Aging stock': [[f"{r['bucket']} days", units(r['units']), units(r['lots'])] for r in data['aging']],
            'Potential waste': [[f"{r['product']} · {r['lot_id']}", expected(r['units']), units(r['kg'])] for r in data['waste']]}
    s = data['series']
    if s is not None:
        rows['Sales and forecast · ' + s['product']] = (
            [[short_date(r['date']), units(r['units']), units(r['hours_out_of_stock']), '—', '—', '—'] for r in s['days']]
            + [[short_date(r['date']), '—', '—', expected(r['low']), expected(r['base']), expected(r['high'])] for r in s['forecast']])
    return rows


@pytest.mark.parametrize('role', ['manager', 'hq'])
def test_every_chart_has_a_table(visitor, web, role):
    page = visitor.page
    if role == 'hq': switch_seat(page, None)
    for id, path in route_paths(page, web, role):
        show_route(page, path)
        main = page.locator('main#content')
        # Forecasts uses one CSS bar group across product rows, rather than a Chart.js canvas.
        bars = int(id == 'forecasts' and main.locator('[role=img]').count() > 0)
        toggles = main.get_by_role('button', name='Show as table', exact=True)
        assert main.locator('canvas').count() + bars == toggles.count(), id
        if id == 'analytics' and toggles.count():
            data = page.request.get('/api/analytics' + ('?branch=' + web.store if role == 'hq' else '')).json()
            values = analytics_expected(data)
            for region in main.get_by_role('region').all():
                toggle = region.get_by_role('button', name='Show as table', exact=True)
                if not toggle.count(): continue
                title = region.get_by_role('heading').inner_text()
                toggle.click()
                expect(region.get_by_role('button', name='Show as chart')).to_have_attribute('aria-pressed', 'true')
                got = table_rows(region)
                wanted = values[title]
                # The final currency column is independently covered by Analytics' currency tests.
                assert [r[:3] for r in got] == wanted if title == 'Potential waste' else got == wanted
        elif bars:
            data = page.request.get('/api/forecasts' + ('?branch=' + web.store if role == 'hq' else '')).json()
            toggles.click()
            expect(main.get_by_role('button', name='Show as chart')).to_have_attribute('aria-pressed', 'true')
            for row, item in zip(main.get_by_test_id('forecast-row').all(), data['items'], strict=True):
                assert row.locator('td').all_inner_texts()[2:2+len(item['daily_base'])] == [expected(v) for v in item['daily_base']]
        else:
            assert toggles.count() == 0, f'{id}: new chart needs an explicit value assertion'


def test_reduced_motion(visitor):
    page = visitor.page
    page.emulate_media(reduced_motion='reduce')
    show_route(page, '/about')
    control = page.get_by_role('link', name='Versions', exact=True)
    assert control.evaluate("el => {const s=getComputedStyle(el);return [s.transitionDuration,s.animationName]}") == ['0s', 'none']
    assert 'animation: reducedMotion() ? false : undefined' in (ROOT / 'web/src/ui.tsx').read_text()
    assert 'reducedMotion() ? "auto" : "smooth"' in (ROOT / 'web/src/pages.tsx').read_text()
