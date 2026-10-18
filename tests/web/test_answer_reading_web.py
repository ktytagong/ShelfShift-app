"""Tasks keeps saved answers, model readings and manager confirmation separate."""
from __future__ import annotations

import pytest
import sqlalchemy as sa
import json
from playwright.sync_api import expect

from tests.web.conftest import db_tx, session_json, switch_seat
from tests.web.test_tasks import PNG, api_request, checked_just_now, open_tasks_page, request_card, request_stock_check

pytestmark = pytest.mark.web
PHOTO_ONLY = PNG + b'Plan11-photo-only'


def test_photo_answer_is_read_after_save(visitor):
    page = visitor.page
    _, request = request_stock_check(page)
    switch_seat(page, request['to_branch'])
    open_tasks_page(page)
    card = request_card(page, request)
    path = f"/api/requests/{request['request_id']}/answer"
    calls, chips_at_read_start = [], []
    page.on('response', lambda response: calls.append(('answer' if path in response.url else 'reading', response.status))
            if path in response.url or response.url.endswith('/reading') else None)
    page.on('request', lambda pending: chips_at_read_start.append(
        card.get_by_text('Photo received · count missing', exact=True).is_visible())
        if pending.url.endswith('/reading') and pending.method == 'POST' else None)
    card.locator(f"input[id='{request['request_id']}-file']").set_input_files(
        {'name': 'p11-photo.png', 'mimeType': 'image/png', 'buffer': PHOTO_ONLY})
    checked_just_now(card)
    with page.expect_response(lambda response: path in response.url and response.request.method == 'POST') as saved:
        card.get_by_role('button', name='Send answer', exact=True).click()
    assert saved.value.status == 200
    original = saved.value.json()
    expect(card.get_by_text('Photo received · count missing', exact=True)).to_be_visible()
    reading = card.get_by_role('group', name='Read by Gemini', exact=True)
    expect(reading.get_by_role('status')).to_have_text('Reading evidence…')
    typed = card.get_by_role('group', name=f"Typed by {request['to_branch']}", exact=True)
    expect(typed).to_contain_text('Not checked')
    expect(card.get_by_role('group', name=f"Photo from {request['to_branch']}", exact=True)).to_be_visible()
    expect(card.get_by_test_id('deadline-bar')).to_be_visible()
    expect(card.get_by_role('button', name="Can't check", exact=True)).to_be_enabled()
    expect(reading).to_contain_text('Read from photo: visible slot empty')
    expect(reading).to_contain_text("Backroom stock not checked, so the total can't be confirmed")
    expect(reading).to_contain_text('Live · Gemini')
    expect(card.get_by_text('Photo received · count missing', exact=True)).to_be_visible()
    expect(card.get_by_role('button', name='Confirm count', exact=True)).to_have_count(0)
    current = api_request(page, request['request_id'])['answer']
    assert current['fields'] == original['fields']
    assert calls == [('answer', 200), ('reading', 200)]
    assert chips_at_read_start == [True], 'The saved chip must render before starting a reading'


def save_reply(visitor, text, counts=None):
    page = visitor.page
    _, request = request_stock_check(page)
    switch_seat(page, request['to_branch'])
    open_tasks_page(page)
    card = request_card(page, request)
    if counts:
        for lot in request['lot_ids']:
            for field, value in zip(('shelf', 'backroom'), counts):
                card.locator(f"input[id='{request['request_id']}-{lot}-{field}']").fill(str(value))
    card.get_by_label('Reply', exact=True).fill(text)
    checked_just_now(card)
    with page.expect_response(lambda r: r.url.endswith('/reading')):
        card.get_by_role('button', name='Send answer', exact=True).click()
    return request, card


def test_conflicting_reply_requires_explicit_choice(visitor):
    page = visitor.page
    request, card = save_reply(visitor, 'None on the shelf, none in the backroom either.', (4, 2))
    choice = card.get_by_role('group', name='Count to confirm', exact=True)
    expect(choice.get_by_role('radio', checked=True)).to_have_count(0)
    expect(card.get_by_role('button', name='Confirm count', exact=True)).to_be_disabled()
    expect(card.get_by_text('Choose which count to confirm.', exact=True)).to_be_visible()
    choice.get_by_role('radio', name='Read from reply:', exact=False).check()
    card.get_by_label('I checked the shelf and the backroom for this lot', exact=True).check()
    with page.expect_response(lambda r: r.url.endswith('/confirm')) as confirmation:
        card.get_by_role('button', name='Confirm count', exact=True).click()
    assert confirmation.value.status == 200
    payload = confirmation.value.request.post_data_json
    current = api_request(page, request['request_id'])['answer']
    assert payload['reading_id'] == current['reading']['reading_id']
    assert payload['value_source'] == 'reading' and payload['revision'] == current['revision']
    assert all(c == {'shelf_units': 4, 'backroom_units': 2} for c in current['fields']['counts'].values())
    assert any('reply' in line for line in confirmation.value.json()['labels'])


def test_failed_reading_is_final_on_reopen(visitor):
    page = visitor.page
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/reading') else None)
    request, card = save_reply(visitor, 'Plan11 unreadable answer')
    expect(card.get_by_text("Gemini couldn't read this answer. The typed values, reply and photo are kept.", exact=True)).to_be_visible()
    page.reload()
    card = request_card(page, request)
    expect(card.get_by_text("Gemini couldn't read this answer. The typed values, reply and photo are kept.", exact=True)).to_be_visible()
    assert len(calls) == 1


def change_reading(page, request, **values):
    answer = api_request(page, request['request_id'])['answer']
    with db_tx() as conn:
        conn.execute(sa.text('UPDATE readings SET ' + ', '.join(f'{key}=:{key}' for key in values)
            + ' WHERE run_id=:run AND reading_id=:reading'),
            dict(values, run=session_json(page)['run_id'], reading=answer['reading']['reading_id']))
    return answer


def test_reply_only_can_confirm_without_changing_typed_inputs(visitor):
    page = visitor.page
    request, card = save_reply(visitor, 'None on the shelf, none in the backroom either.')
    expect(card.get_by_test_id('confirm-block')).to_contain_text('Pending manager check')
    card.get_by_label('I checked the shelf and the backroom for this lot', exact=True).check()
    with page.expect_response(lambda r: r.url.endswith('/confirm')) as result:
        card.get_by_role('button', name='Confirm count', exact=True).click()
    assert result.value.status == 200
    answer = api_request(page, request['request_id'])['answer']
    assert all(c == {'shelf_units': None, 'backroom_units': None} for c in answer['fields']['counts'].values())


def test_stale_reading_keeps_server_refusal_and_reload_action(visitor):
    page = visitor.page
    request, card = save_reply(visitor, 'None on the shelf, none in the backroom either.', (4, 2))
    card.get_by_role('radio', name='Read from reply:', exact=False).check()
    card.get_by_label('I checked the shelf and the backroom for this lot', exact=True).check()
    change_reading(page, request, status='FAILED')
    with page.expect_response(lambda r: r.url.endswith('/confirm')) as result:
        card.get_by_role('button', name='Confirm count', exact=True).click()
    assert result.value.status == 409
    expect(card).to_contain_text(result.value.json()['message'])
    expect(card.get_by_role('button', name='Reload the stock decision', exact=True)).to_be_visible()
    card.get_by_role('button', name='Reload the stock decision', exact=True).click()
    expect(card).to_contain_text("Gemini couldn't read this answer. The typed values, reply and photo are kept.")
    expect(card.get_by_role('radio', name='Read from reply:', exact=False)).to_have_count(0)
    expect(card.get_by_label('I checked the shelf and the backroom for this lot', exact=True)).not_to_be_checked()
    expect(card.get_by_role('button', name='Confirm count', exact=True)).to_be_disabled()


@pytest.mark.parametrize('status,attempt,expected_calls', [('INTERRUPTED', 1, 2), ('INTERRUPTED', 2, 1), ('REFUSED', 1, 1)])
def test_only_interrupted_first_attempt_retries_on_open(visitor, status, attempt, expected_calls):
    from tests.web.test_tasks import show
    page = visitor.page
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/reading') else None)
    request, card = save_reply(visitor, 'Plan11 unreadable answer')
    change_reading(page, request, status=status, attempt=attempt, reason='cap_reached' if status == 'REFUSED' else None)
    show(page, 'Closed')
    show(page, 'Open')
    card = request_card(page, request)
    if expected_calls == 2:
        expect(card).to_contain_text("Gemini couldn't read this answer. The typed values, reply and photo are kept.")
    elif status == 'REFUSED':
        expect(card).to_contain_text("Today's AI limit is reached. It resets at 00:00 Bangkok time.")
    else:
        expect(card.get_by_role('group', name='Read by Gemini', exact=True)).to_be_visible()
    assert len(calls) == expected_calls


def test_missing_reading_retries_once_after_reload(visitor):
    page = visitor.page
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/reading') else None)
    page.route('**/api/signals/*/reading', lambda route: route.fulfill(status=503, json={'error': 'unavailable', 'message': 'Reading interrupted.'}))
    request, _ = save_reply(visitor, 'Plan11 unreadable answer')
    assert api_request(page, request['request_id'])['answer']['reading'] is None
    page.unroute('**/api/signals/*/reading')
    page.reload()
    card = request_card(page, request)
    expect(card).to_contain_text("Gemini couldn't read this answer. The typed values, reply and photo are kept.")
    assert len(calls) == 2
    page.reload()
    request_card(page, request)
    assert len(calls) == 2


def test_missing_reading_recovers_once_on_collapse_reopen(visitor):
    page = visitor.page
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/reading') else None)
    page.route('**/api/signals/*/reading', lambda route: route.fulfill(
        status=503, json={'error': 'unavailable', 'message': 'Reading interrupted.'}))
    request, card = save_reply(visitor, 'Plan11 unreadable answer')
    expect(card).to_contain_text('Reading interrupted.')
    assert api_request(page, request['request_id'])['answer']['reading'] is None
    assert len(calls) == 1
    card.get_by_role('heading', level=3).get_by_role('button').click()
    card = request_card(page, request)
    expect(card).to_contain_text('Reading interrupted.')
    assert len(calls) == 2
    # A second failed transport must not turn collapse/reopen into an unlimited retry.
    card.get_by_role('heading', level=3).get_by_role('button').click()
    card = request_card(page, request)
    expect(card.get_by_role('group', name='Read by Gemini', exact=True)).to_be_visible()
    assert len(calls) == 2


def test_reading_finished_after_confirmation_stays_history(visitor):
    from tests.web.test_tasks import show
    page = visitor.page
    _, request = request_stock_check(page)
    switch_seat(page, request['to_branch'])
    open_tasks_page(page)
    card = request_card(page, request)
    for lot in request['lot_ids']:
        for field, value in [('shelf', '4'), ('backroom', '2')]:
            card.locator(f"input[id='{request['request_id']}-{lot}-{field}']").fill(value)
    card.get_by_label('Reply', exact=True).fill('Plan11 delayed reply: None on the shelf, none in the backroom either.')
    checked_just_now(card)
    card.get_by_role('button', name='Send answer', exact=True).click()
    expect(card.get_by_role('status')).to_have_text('Reading evidence…')
    card.get_by_label('I checked the shelf and the backroom for this lot', exact=True).check()
    held_open = []
    def hold_open_refresh(route):
        if 'view=open' in route.request.url:
            held_open.append((route, route.fetch()))
        else:
            route.continue_()
    # Keep the real post-confirmation Open response pending until Closed has rendered.
    # This preserves the same mounted request across the filter change and exposes stale callbacks.
    page.route('**/api/tasks?*', hold_open_refresh)
    with page.expect_response(lambda r: r.url.endswith('/confirm')) as result:
        card.get_by_role('button', name='Confirm count', exact=True).click()
    assert result.value.status == 200
    show(page, 'Closed')
    card = request_card(page, request)
    assert held_open, 'The real Open refresh must be held to exercise the filter race'
    for route, response in held_open:
        route.fulfill(response=response)
    page.unroute('**/api/tasks?*', hold_open_refresh)
    expect(card).to_contain_text('Read after confirmation · not applied')
    expect(page.get_by_role('group', name='Show').get_by_role('button', name='Closed', exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(card).to_contain_text('Differs from the confirmed count')
    expect(card.get_by_role('button', name='Confirm count', exact=True)).to_have_count(0)


def test_photo_date_requires_its_extra_check_before_confirm(visitor):
    from tests.api.test_requests import expiry_fixture
    from tests.web.test_tasks import workspace_run, ask_as_source, printed_iso
    page = visitor.page
    case, fact = expiry_fixture(workspace_run(page))
    request = ask_as_source(page, case['case_id'], {'fact_id': fact['fact_id']})
    switch_seat(page, request['to_branch'])
    open_tasks_page(page)
    card = request_card(page, request)
    card.locator(f"input[id='{request['request_id']}-file']").set_input_files(
        {'name': 'printed.png', 'mimeType': 'image/png', 'buffer': PHOTO_ONLY})
    checked_just_now(card)
    with page.expect_response(lambda r: r.url.endswith('/reading')):
        card.get_by_role('button', name='Send answer', exact=True).click()
    answer = api_request(page, request['request_id'])['answer']
    lot = request['lot_ids'][0]
    date = printed_iso(page, lot)
    rows = [{'field': 'printed_date', 'value': date, 'source': 'file', 'state': 'suggested'},
            {'field': 'lot_id', 'value': lot, 'source': 'file', 'state': 'suggested'}]
    with db_tx() as conn:
        conn.execute(sa.text('UPDATE readings SET status=\'DONE\', suggestions=CAST(:rows AS jsonb) WHERE run_id=:run AND reading_id=:reading'),
            {'rows': json.dumps(rows), 'run': session_json(page)['run_id'], 'reading': answer['reading']['reading_id']})
    page.reload()
    card = request_card(page, request)
    button = card.get_by_role('button', name='Confirm printed date', exact=True)
    expect(button).to_be_disabled()
    card.get_by_label(f'I checked the printed date on lot {lot}', exact=True).check()
    with page.expect_response(lambda r: r.url.endswith('/confirm')) as result:
        button.click()
    assert result.value.status == 200
    assert result.value.request.post_data_json['checked_date'] is True
    assert result.value.request.post_data_json['reading_id'] == answer['reading']['reading_id']


def test_no_reading_without_evidence(visitor):
    page = visitor.page
    _, request = request_stock_check(page)
    switch_seat(page, request['to_branch'])
    open_tasks_page(page)
    card = request_card(page, request)
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/reading') else None)
    for lot in request['lot_ids']:
        for field in ('shelf', 'backroom'):
            card.locator(f"input[id='{request['request_id']}-{lot}-{field}']").fill('0')
    checked_just_now(card)
    card.get_by_role('button', name='Send answer', exact=True).click()
    expect(card.get_by_test_id('confirm-block')).to_be_visible()
    expect(card.get_by_role('group', name='Read by Gemini', exact=True)).to_have_count(0)
    page.reload()
    request_card(page, request)
    assert calls == []


def test_preloaded_response_opens_without_a_call(visitor):
    page = visitor.page
    request, _ = save_reply(visitor, 'None on the shelf, none in the backroom either.', (4, 2))
    model_id = 'offline-precomputed-model-' + 'long-model-id-' * 30
    answer = change_reading(page, request, provenance='PRECOMPUTED', model_id=model_id)
    with db_tx() as conn:
        conn.execute(sa.text("UPDATE signals SET provenance='simulated' WHERE run_id=:run AND signal_id=:signal"),
            {'run': session_json(page)['run_id'], 'signal': answer['signal_id']})
    calls = []
    page.on('request', lambda r: calls.append(r.url) if r.url.endswith('/reading') else None)
    page.reload()
    card = request_card(page, request)
    expect(card.get_by_text('Simulated response', exact=True)).to_be_visible()
    expect(card).to_contain_text(f'Precomputed · Gemini {model_id}')
    page.set_viewport_size({'width': 375, 'height': 812})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert calls == []


def test_several_lots_and_long_reply_remain_separate(visitor):
    page = visitor.page
    text = 'None on the shelf, none in the backroom either. ' + 'Long reply text. ' * 40
    request, _ = save_reply(visitor, text, (4, 2))
    answer = api_request(page, request['request_id'])['answer']
    other = 'SECOND-STORED-LOT'
    lots = request['lot_ids'] + [other]
    fields = dict(answer['fields'], lot_ids=lots,
        counts={**answer['fields']['counts'], other: {'shelf_units': 7, 'backroom_units': 3}})
    # A stored multi-lot answer fixture tests the read view; no confirmation/engine mutation is requested.
    with db_tx() as conn:
        params = {'run': session_json(page)['run_id'], 'request': request['request_id'], 'signal': answer['signal_id'],
            'lots': json.dumps(lots), 'fields': json.dumps(fields)}
        conn.execute(sa.text('UPDATE evidence_requests SET lot_ids=CAST(:lots AS jsonb) WHERE run_id=:run AND request_id=:request'), params)
        conn.execute(sa.text('UPDATE signals SET fields=CAST(:fields AS jsonb) WHERE run_id=:run AND signal_id=:signal'), params)
    page.reload()
    card = request_card(page, request)
    typed = card.get_by_role('group', name=f"Typed by {request['to_branch']}", exact=True)
    expect(typed).to_contain_text(f'Lot {other}')
    expect(typed).to_contain_text('Shelf 7 · Backroom 3 · Total 10 units')
    expect(typed).to_contain_text('Shelf 4 · Backroom 2 · Total 6 units')
    reading = card.get_by_role('group', name='Read by Gemini', exact=True)
    expect(reading).to_contain_text(f"Lot {request['lot_ids'][0]} · Read from reply: shelf 0 · backroom 0")
    expect(reading).not_to_contain_text(other)
    expect(card.get_by_role('group', name=f"Reply from {request['to_branch']}", exact=True)).to_contain_text(text)
    page.set_viewport_size({'width': 375, 'height': 812})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_keyboard_and_widths(visitor):
    page = visitor.page
    request, card = save_reply(visitor, 'None on the shelf, none in the backroom either.', (4, 2))
    page.set_viewport_size({'width': 375, 'height': 812})
    radios = card.get_by_role('group', name='Count to confirm', exact=True).get_by_role('radio')
    radios.first.focus()
    page.keyboard.press('Space')
    page.keyboard.press('ArrowDown')
    expect(radios.nth(1)).to_be_checked()
    tick = card.get_by_label('I checked the shelf and the backroom for this lot', exact=True)
    tick.focus()
    page.keyboard.press('Space')
    expect(tick).to_be_checked()
    expect(card.get_by_role('button', name='Confirm count', exact=True)).to_be_enabled()
    for input in [radios.first, radios.nth(1), tick]:
        assert input.locator('..').bounding_box()['height'] >= 44
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_photo_observation_has_a_decorative_scan_icon(visitor):
    test_photo_answer_is_read_after_save(visitor)
    group = visitor.page.get_by_role('group', name='Read by Gemini', exact=True)
    line = group.get_by_text('Read from photo: visible slot empty', exact=True)
    expect(line.locator('svg[aria-hidden=true]')).to_have_count(1)
    photo = visitor.page.get_by_test_id('answer-photo')
    box = photo.get_by_role('img').bounding_box()
    assert box['width'] == box['height'] == 64
    expect(photo.get_by_role('link', name='View photo', exact=True)).to_be_visible()
