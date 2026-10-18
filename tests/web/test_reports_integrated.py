"""08-10: real browser/session/Reports HTTP reads, with independent database count and case-ID oracles.

The retained 08-03 SQL oracles read cases, requests, decisions and movements directly. They never
call reporting helpers or derive an expected set from the response under test. No Reports interception.
"""
from contextlib import contextmanager
import re
from urllib.parse import urlencode, urlsplit, parse_qs
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from api import cases, db
from tests.api.conftest import by_tag, fresh
from tests.api.test_reporting_api import REPORT_NAMES, add_request, contributing_ids, oracle
from tests.api.test_reports_contract_shapes import fixture, key_tree, projection
from tests.web.conftest import case_json, decision_body, session_json, switch_seat, take_ownership

pytestmark = pytest.mark.web

LABELS = {'open': 'Open decisions', 'pending_approval': 'Pending approvals',
          'unanswered_check': 'Unanswered checks', 'committed': 'Committed plans', 'cancelled': 'Cancellations'}


def read(browser_page, path, **query):
    response = browser_page.request.get(path + ('?' + urlencode(query) if query else ''))
    assert response.status == 200, response.status
    return response.json()


def post(page, path, body):
    response = page.request.post(path, data=body, headers={'Idempotency-Key': str(uuid4())})
    assert response.status == 200, response.status
    return response.json()


def workspace(page):
    return fresh(session_json(page)['run_id'])


def panel(page):
    return page.get_by_role('tabpanel')


def tiles(page, body):
    """The visible counts equal the live response; its semantic truth is checked separately below."""
    for report, label in LABELS.items():
        n = body['activity'][report]['count']
        tile = panel(page).get_by_role('group', name=re.compile(r'^' + label + f': {n},'))
        expect(tile).to_be_visible()
        expect(tile.locator('p.num')).to_have_text(str(n))
        expect(tile.get_by_role('link', name=f'View breakdown: {label}')).to_have_count(1 if n else 0)


def assert_workspace(page, store, manager, requested=None):
    body = read(page, '/api/reports/workspace', **({'branch': requested} if requested else {}))
    assert body['scope'] == {'branch': store, 'role': 'manager' if manager else 'hq'}
    assert body['activity'] == oracle(workspace(page), store, manager)
    assert key_tree(projection(body, 'scope', 'activity')) == key_tree(
        projection(fixture('reports_workspace'), 'scope', 'activity'))
    tiles(page, body)
    return body


def board_ids(page, report, branch=None):
    found = []
    page_no = 1
    while True:
        body = read(page, '/api/cases', report=report, page=page_no, **({'branch': branch} if branch else {}))
        assert all(not c.get('needs_evaluation') for c in body['cases'])
        found.extend(c['case_id'] for c in body['cases'])
        if len(found) >= body['total']:
            assert len(found) == len(set(found)) == body['total']
            return set(found), body
        assert body['cases'], 'a page before the total was empty'
        page_no += 1


@contextmanager
def board_default(run, value):
    """Give ONLY this workspace a unique test-local reference copy so the API child loads this default.

    Changing a Python object in pytest would not configure the real child process. Persist the copied
    seed in the browser test DB, use a unique dataset identity to avoid its reference cache, and restore
    the workspace before removing it. No fixture file or original reference record changes.
    """
    ident = 'reports-test-' + uuid4().hex
    with db.tx() as conn:
        db.run(conn, 'INSERT INTO ref_data (dataset_version, part, payload) '
               'SELECT :v, part, payload FROM ref_data WHERE dataset_version = :old',
               v=ident, old=run['dataset_version'])
        db.run(conn, "UPDATE ref_data SET payload = jsonb_set(payload, '{settings,board}', "
               "jsonb_build_object('default_scope', CAST(:scope AS text), 'page_size', 2)) "
               "WHERE dataset_version = :v AND part = 'seed'", v=ident, scope=value)
        db.run(conn, 'UPDATE demo_runs SET dataset_version = :v WHERE run_id = :r', v=ident, r=run['run_id'])
    try:
        yield
    finally:
        with db.tx() as conn:
            db.run(conn, 'UPDATE demo_runs SET dataset_version = :v WHERE run_id = :r',
                   v=run['dataset_version'], r=run['run_id'])
            db.run(conn, 'DELETE FROM ref_data WHERE dataset_version = :v', v=ident)


def mixed_activity(page):
    run = workspace(page)
    story = by_tag(run, cases.STORY_TAG)
    switch_seat(page, story['source_branch'])
    take_ownership(page, story['case_id'])
    post(page, f"/api/cases/{story['case_id']}/send", {'rec_version': story['active_rec_version']})
    assert case_json(page, story['case_id'])['state'] == 'PENDING_APPROVAL'
    with db.tx() as conn:
        others = db.rows(conn, 'SELECT * FROM cases WHERE run_id = :r AND case_id <> :c ORDER BY case_id',
                         r=run['run_id'], c=story['case_id'])
        assert len(others) >= 3
        db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=others[0]['case_id'])
        db.run(conn, "UPDATE cases SET state = 'APPROVED' WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=others[1]['case_id'])
        foreign = next(c for c in others if story['source_branch'] not in (c['source_branch'], c['plan_dest']))
        add_request(conn, run, foreign, story['source_branch'], "now() - interval '1 minute'")
        add_request(conn, run, foreign, story['source_branch'])
        add_request(conn, run, story, story['plan_dest'])
    return run, story


@pytest.mark.parametrize('default_scope', ['branch', 'all'])
@pytest.mark.parametrize('viewer', ['source', 'destination', 'hq', 'hq_branch'])
def test_real_tiles_drilldown_ids_and_strict_scope(visitor, default_scope, viewer):
    page = visitor.page
    run, story = mixed_activity(page)
    source, dest = story['source_branch'], story['plan_dest']
    manager = source if viewer == 'source' else dest if viewer == 'destination' else None
    store = source if viewer == 'hq_branch' else manager
    # A manager's malicious branch is ignored by both Reports and the board; HQ may narrow explicitly.
    requested = dest if viewer == 'source' else source if viewer in ('destination', 'hq_branch') else None
    switch_seat(page, manager)
    with board_default(run, default_scope):
        report_hash = '/#/reports' + ('?branch=' + requested if requested else '')
        page.goto(report_hash)
        body = assert_workspace(page, store, manager, requested)
        for report, label in LABELS.items():
            expected = contributing_ids(run, store, report)
            found, board = board_ids(page, report, requested)
            assert found == expected
            assert board['scope']['branch'] == store
            if report != 'unanswered_check':
                assert len(expected) == body['activity'][report]['count']
            if not body['activity'][report]['count']:
                continue
            # Actual click, actual board HTTP, exact rendered row identities on every server page.
            with page.expect_response(lambda r: urlsplit(r.url).path == '/api/cases'
                                      and parse_qs(urlsplit(r.url).query).get('report') == [report]) as got:
                panel(page).get_by_role('link', name=f'View breakdown: {label}').click()
            shown = got.value.json()
            assert shown['scope']['branch'] == store
            expect(page.get_by_text(f'Reports: {label}', exact=False)).to_be_visible()
            rendered = set()
            while True:
                rows = page.locator("[data-testid^='decision-row-']")
                expect(rows).to_have_count(len(shown['cases']))
                expected_keys = [c['key'] for c in shown['cases']]
                assert [r.get_attribute('data-testid').removeprefix('decision-row-') for r in rows.all()] == expected_keys
                rendered.update(c['case_id'] for c in shown['cases'])
                if len(rendered) >= shown['total']:
                    break
                with page.expect_response(lambda r: urlsplit(r.url).path == '/api/cases'
                                          and parse_qs(urlsplit(r.url).query).get('page') == [str(shown['page'] + 1)]) as got:
                    page.get_by_role('button', name='Next page', exact=True).click()
                shown = got.value.json()
            assert rendered == expected
            page.get_by_role('link', name='Back to Reports', exact=True).click()
            page.goto(report_hash)
            tiles(page, body)


def test_real_none_has_no_comparisons_or_charts(visitor):
    page = visitor.page
    for manager in (session_json(page)['branch'], None):
        switch_seat(page, manager)
        body = read(page, '/api/reports/benchmark')
        assert projection(body, 'status', 'current', 'historical') == projection(
            fixture('reports_benchmark_none'), 'status', 'current', 'historical')
        with db.tx() as conn:
            assert db.one(conn, "SELECT count(*) AS n FROM benchmark_runs WHERE status = 'PUBLISHED'")['n'] == 0
        page.goto('/#/reports?tab=benchmark')
        expect(panel(page).get_by_role('heading', name='Evaluation not yet available')).to_be_visible()
        expect(panel(page).locator('canvas, [role=group], table')).to_have_count(0)
        assert '$' not in panel(page).inner_text()
        assert not re.search(r'\b0\b', panel(page).inner_text())


def test_real_pending_commit_account_and_reset_transitions(visitor):
    page = visitor.page
    run = workspace(page)
    story = by_tag(run, cases.STORY_TAG)
    source, dest, cid = story['source_branch'], story['plan_dest'], story['case_id']
    switch_seat(page, source)
    page.goto('/#/reports')
    before = assert_workspace(page, source, source)
    take_ownership(page, cid)
    post(page, f'/api/cases/{cid}/send', {'rec_version': story['active_rec_version']})
    page.reload()
    pending = assert_workspace(page, source, source)
    assert pending['activity']['pending_approval']['count'] == before['activity']['pending_approval']['count'] + 1
    switch_seat(page, dest)
    page.goto('/#/reports')
    assert_workspace(page, dest, dest)
    post(page, f'/api/cases/{cid}/decision', decision_body(page, cid, 'accept', story['active_rec_version']))
    switch_seat(page, source)
    post(page, f'/api/cases/{cid}/decision', decision_body(page, cid, 'approve', story['active_rec_version']))
    assert case_json(page, cid)['state'] == 'APPROVED'
    page.reload()
    committed = assert_workspace(page, source, source)
    assert committed['activity']['pending_approval']['count'] == before['activity']['pending_approval']['count']
    assert committed['activity']['committed']['count'] == before['activity']['committed']['count'] + 1
    old_run = run['run_id']
    post(page, '/api/demo/reset', {})
    page.reload()
    assert session_json(page)['run_id'] != old_run
    assert session_json(page)['account'] == source
    reset_body = assert_workspace(page, source, source)
    assert reset_body['activity']['committed']['count'] == before['activity']['committed']['count']


def test_real_hq_branch_controls_refresh_scope(visitor):
    page = visitor.page
    run, story = mixed_activity(page)
    switch_seat(page, None)
    page.goto('/#/reports')
    assert_workspace(page, None, None)
    for branch in (story['source_branch'], story['plan_dest']):
        page.get_by_label('Branch', exact=True).fill(branch)
        page.get_by_role('button', name='Show branch', exact=True).click()
        expect(panel(page).get_by_text(f'Figures from your workspace for {branch}.', exact=False)).to_be_visible()
        assert_workspace(page, branch, None, branch)
    page.get_by_role('button', name='All branches', exact=True).click()
    expect(panel(page).get_by_text('Figures from your workspace for all branches.', exact=False)).to_be_visible()
    assert_workspace(page, None, None)


def test_real_zero_count_tiles_have_no_links(visitor):
    page = visitor.page
    run = workspace(page)
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r", r=run['run_id'])
        db.run(conn, "DELETE FROM evidence_requests WHERE run_id = :r", r=run['run_id'])
    page.goto('/#/reports')
    branch = session_json(page)['branch']
    body = assert_workspace(page, branch, branch)
    for report in ('open', 'pending_approval', 'unanswered_check', 'committed'):
        assert body['activity'][report]['count'] == 0
