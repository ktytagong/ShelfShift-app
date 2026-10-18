"""08-10: real browser/session/Reports HTTP reads, with independent database count and case-ID oracles.

The retained 08-03 SQL oracles read cases, requests, decisions and movements directly. They never
call reporting helpers or derive an expected set from the response under test. No Reports interception.

08-18 (D-35) adds the integrated server: Phase 7's own Select another plan, Send, accept, approve, Cancel approved
plan and count-request routes driven from the page's session, and a benchmark published by the real job
(`python -m api.weekcheck benchmark start`, in-process) on a TEST ONLY eligible dataset, read as current and then
historical with a stable identity. The complete benchmark presentation is 08-21's, after 08-09 joins.

08-21 (D-24, D-35): after 08-09's complete presentation joins, a real saved result is read through every layer of the
page (header, both charts in chart and table modes, headline, the four blocks, both breakdowns), each figure against
an independent oracle from its records (`test_reports_contract_shapes.saved_oracle`), with a second publication
racing an open breakdown, the historical disclosure for HQ and one store, accessible missing reasons, the no-results
and stale states, and canvas captures at 360/390/1280 for the end-of-phase human check. A committed transfer is in
exactly its source's and destination's figures under default_scope=all.
"""
from contextlib import contextmanager
import json
import pathlib
import re
from urllib.parse import urlencode, urlsplit, parse_qs
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from api import cases, db, refdata, weekcheck
from tests.api.benchmark_fixtures import eligible_artifact, workspace_factory
from tests.api.conftest import by_tag, drop_runs, fresh
from tests.api.test_benchmark_cli import drop_benchmarks
from tests.api.test_reports_phase7 import REASON, alternative_destination, plan_changing_fact
from tests.api.test_reporting_api import REPORT_NAMES, add_request, contributing_ids, oracle
from tests.api.test_reports_contract_shapes import (SCENARIOS, assert_summary_matches, fixture, key_tree,
                                                    projection, saved_oracle)
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
            # 08-21: the board's specified filter line (UI-SPEC "Showing: {figure} · from Reports"), table view
            expect(page.get_by_text(f'Showing: {label} · from Reports', exact=True)).to_be_visible()
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


# --- 08-18: Phase 7's real flows on the integrated server ---------------------------------------------------------

def show(page, branch):
    """The Reports page for that store's manager: tiles equal the live read, the read equals the SQL oracle."""
    switch_seat(page, branch)
    page.goto('/#/reports')
    return assert_workspace(page, branch, branch)


def test_real_phase7_selection_cancel_and_request_on_the_page(visitor):
    """A case recommended to store A, with B selected through Phase 7's Select another plan, sent, accepted by B and
    approved, then cancelled by B: the page's tiles and the board drilldowns put it at B and never at A at every step.
    A count request made through Phase 7's route turns past due when its deadline passes (Phase 7's own move)."""
    page = visitor.page
    run = workspace(page)
    case, recommended, alternative = alternative_destination(run)
    cid, source, a, b = case['case_id'], case['source_branch'], recommended.dest, alternative.dest
    switch_seat(page, source)
    take_ownership(page, cid)
    post(page, f'/api/cases/{cid}/select', {'rec_version': case['active_rec_version'], 'plan_key': alternative.key})
    post(page, f'/api/cases/{cid}/send', {'rec_version': case['active_rec_version']})
    assert case_json(page, cid)['state'] == 'PENDING_APPROVAL'
    for branch, mine in ((b, True), (a, False)):
        body = show(page, branch)
        assert (cid in board_ids(page, 'pending_approval')[0]) is mine, branch
        assert body['activity']['pending_approval']['waiting_for_you'] >= int(mine)
    switch_seat(page, b)
    post(page, f'/api/cases/{cid}/decision', decision_body(page, cid, 'accept'))
    switch_seat(page, source)
    post(page, f'/api/cases/{cid}/decision', decision_body(page, cid, 'approve'))
    assert case_json(page, cid)['state'] == 'APPROVED'
    for branch, mine in ((b, True), (a, False)):
        body = show(page, branch)
        assert (cid in board_ids(page, 'committed')[0]) is mine, branch
        assert [p['dest_branch'] for p in body['expected']['plans'] if p['case_id'] == cid] == ([b] if mine else [])
    switch_seat(page, b)
    post(page, f'/api/cases/{cid}/cancel', {'reason': REASON})
    for branch, mine in ((b, True), (a, False)):
        show(page, branch)
        assert (cid in board_ids(page, 'cancelled')[0]) is mine, branch

    post(page, '/api/demo/reset', {})  # a fresh workspace: the selection may have used the one count-fact case
    page.reload()
    run = workspace(page)
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' AND active_rec_version IS NOT "
                             "NULL ORDER BY case_id", r=run['run_id'])
    other, fact = next(((c, f) for c in rows if (f := plan_changing_fact(run, c['case_id']))), (None, None))
    assert other is not None, 'hand-back: no OPEN case has a plan-changing count fact'
    switch_seat(page, other['source_branch'])
    post(page, f"/api/cases/{other['case_id']}/requests",
         {'rec_version': other['active_rec_version'], 'fact_id': fact['fact_id']})
    before = show(page, other['source_branch'])['activity']['unanswered_check']
    with db.tx() as conn:  # Phase 7's own deadline move (tests/api/test_evidence.py `past_due`)
        db.run(conn, "UPDATE evidence_requests SET due_at = now() - interval '1 second' WHERE run_id = :r "
                     "AND case_id = :c AND fact_id = :f AND state = 'OPEN'", r=run['run_id'], c=other['case_id'],
               f=fact['fact_id'])
    after = show(page, other['source_branch'])['activity']['unanswered_check']
    assert (after['count'], after['past_due']) == (before['count'], before['past_due'] + 1)
    assert other['case_id'] in board_ids(page, 'unanswered_check')[0]


def rebind(old_run_id, new_run_id):
    """Point the page's signed-in session at another workspace (test-local session rewrite, as 08-15's API test)."""
    with db.tx() as conn:
        db.run(conn, 'UPDATE sessions SET run_id = :new WHERE run_id = :old', new=new_run_id, old=old_run_id)


def test_real_published_benchmark_reads_current_then_historical_with_a_stable_identity(
        visitor, tmp_path, monkeypatch, capsys):
    """No-results state before publication; after the real job publishes on a TEST ONLY eligible dataset, a workspace
    on that dataset reads it as current (the page shows its window and versions), the release template's workspace
    reads it as historical, and a second publication changes what is current and latest without changing the first
    one's identity or saved summary."""
    page = visitor.page
    switch_seat(page, None)
    home = session_json(page)['run_id']
    dv = refdata.install(eligible_artifact(tmp_path / 'eligible', purpose='08-18 browser identity'), allow_local=False)
    created, made = [], set()
    monkeypatch.setenv('IMAGE_DIGEST', 'test-only-image-digest-08-18')
    monkeypatch.setenv('GIT_COMMIT', 'test-only-commit-08-18')

    def publish():
        capsys.readouterr()
        code = weekcheck.main(['benchmark', 'start', '--dataset', dv])
        report = json.loads(capsys.readouterr().out.splitlines()[-1])
        if report.get('benchmark_id'):
            made.add(report['benchmark_id'])
        assert code == 0 and report['status'] == 'PUBLISHED', report
        return report['benchmark_id']

    ws = workspace_factory(dv, created)()
    try:
        page.goto('/#/reports?tab=benchmark')
        expect(panel(page).get_by_role('heading', name='Evaluation not yet available')).to_be_visible()
        first = publish()
        rebind(home, ws['run_id'])
        body = read(page, '/api/reports/benchmark')
        assert body['status'] == 'current' and body['current']['benchmark_id'] == first
        assert body['current']['versions'] == body['app'] and body['current']['differs'] == []
        page.goto('/#/reports?tab=benchmark')
        page.reload()
        expect(panel(page).get_by_text('Benchmark evaluation', exact=True)).to_be_visible()
        expect(panel(page).get_by_text('Current configuration', exact=True)).to_be_visible()
        versions = body['current']['versions']
        for name in ('engine', 'dataset', 'policy', 'evaluator'):
            expect(panel(page).get_by_text(versions[name], exact=True)).to_be_visible()
        expect(panel(page).get_by_text(versions['settings'][:12], exact=True)).to_be_visible()
        rebind(ws['run_id'], home)
        old = read(page, '/api/reports/benchmark')
        assert old['status'] == 'historical' and old['historical']['benchmark_id'] == first
        assert 'dataset' in old['historical']['differs'] and old['current'] is None
        page.goto('/#/reports?tab=benchmark')
        page.reload()
        expect(panel(page).get_by_text('No benchmark available for the current configuration.')).to_be_visible()

        saved = read(page, '/api/reports/benchmark', benchmark_id=first)
        second = publish()
        assert second != first
        latest = read(page, '/api/reports/benchmark')
        assert latest['status'] == 'historical' and latest['historical']['benchmark_id'] == second
        pinned = read(page, '/api/reports/benchmark', benchmark_id=first)
        assert pinned['historical']['benchmark_id'] == first
        assert pinned['historical']['summary'] == saved['historical']['summary']
        rebind(home, ws['run_id'])
        assert read(page, '/api/reports/benchmark')['current']['benchmark_id'] == second
        assert read(page, '/api/reports/benchmark', benchmark_id=first)['current']['benchmark_id'] == first
    finally:
        rebind(ws['run_id'], home)
        drop_benchmarks(made)
        with db.tx() as conn:
            drop_runs(conn, list(reversed(created)))


# --- 08-21: the complete presentation on the integrated server ----------------------------------------------------

def test_real_committed_transfer_is_in_exactly_its_stores_figures_under_default_scope_all(visitor):
    """A real committed transfer (Send, accept, approve through Phase 7's routes): with the board's default scope set
    to all, the source's manager (outgoing) and the destination's (incoming) see it in the Committed plans tile, its
    drilldown and Expected outcomes, an unrelated store's manager never does, and HQ sees it for the network and for
    the destination it names; every count equals the SQL oracle."""
    page = visitor.page
    run = workspace(page)
    story = by_tag(run, cases.STORY_TAG)
    cid, source, dest = story['case_id'], story['source_branch'], story['plan_dest']
    switch_seat(page, source)
    take_ownership(page, cid)
    post(page, f'/api/cases/{cid}/send', {'rec_version': story['active_rec_version']})
    switch_seat(page, dest)
    post(page, f'/api/cases/{cid}/decision', decision_body(page, cid, 'accept'))
    switch_seat(page, source)
    post(page, f'/api/cases/{cid}/decision', decision_body(page, cid, 'approve'))
    assert case_json(page, cid)['state'] == 'APPROVED'
    other = next(b for b in sorted(refdata.get(run['dataset_version']).branches) if b not in (source, dest))
    for viewer, seat, store, requested, mine in (('source', source, source, None, True),
                                                 ('destination', dest, dest, None, True),
                                                 ('other', other, other, None, False),
                                                 ('hq', None, None, None, True),
                                                 ('hq_branch', None, dest, dest, True)):
        switch_seat(page, seat)
        with board_default(run, 'all'):
            report_hash = '/#/reports' + (f'?branch={requested}' if requested else '')
            page.goto(report_hash)
            body = assert_workspace(page, store, seat, requested)
            expected = contributing_ids(run, store, 'committed')
            assert (cid in expected) is mine, viewer
            found, _ = board_ids(page, 'committed', requested)
            assert found == expected and len(expected) == body['activity']['committed']['count'], viewer
            plans = [p for p in body['expected']['plans'] if p['case_id'] == cid]
            assert [(p['source_branch'], p['dest_branch']) for p in plans] == ([(source, dest)] if mine else []), viewer
            if not expected:
                continue
            with page.expect_response(lambda r: urlsplit(r.url).path == '/api/cases'
                                      and parse_qs(urlsplit(r.url).query).get('report') == ['committed']) as got:
                panel(page).get_by_role('link', name='View breakdown: Committed plans').click()
            shown = got.value.json()
            assert shown['scope']['branch'] == store and shown['total'] == len(expected)
            expect(page.get_by_text('Showing: Committed plans · from Reports', exact=True)).to_be_visible()
            keys = [r.get_attribute('data-testid').removeprefix('decision-row-')
                    for r in page.locator("[data-testid^='decision-row-']").all()]
            assert keys == [c['key'] for c in shown['cases']] and (story['key'] in keys) is mine, viewer


CAPTURES = pathlib.Path('test-results/reports-08-21')


def bind(page, run_id, role='hq', branch=None):
    """Point the page's signed-in session at a workspace and seat (test-local session rewrite, as 08-15's API test)."""
    current = session_json(page)['run_id']
    with db.tx() as conn:
        db.run(conn, 'UPDATE sessions SET run_id = :new, role = :role, branch = :b WHERE run_id = :old',
               new=run_id, role=role, b=branch, old=current)
    page.reload()


def formats(page, values, kind):
    """Standard en-US Intl formats in the page's own engine (not the app's code): whole money, signed whole money
    with a true minus and no sign at zero, or units with at most one decimal."""
    return page.evaluate("""([values, kind, currency]) => values.map((v) => new Intl.NumberFormat('en-US',
        kind === 'units' ? { maximumFractionDigits: 1 } : { style: 'currency', currency, minimumFractionDigits: 0,
        maximumFractionDigits: 0, signDisplay: kind === 'signed' ? 'exceptZero' : 'negative' })
        .formatToParts(v).map((p) => p.type === 'minusSign' ? '\\u2212' : p.value).join(''))""",
                         [values, kind, session_json(page)['currency']])


def section_named(page, heading):
    return panel(page).get_by_role('heading', name=heading, exact=True).locator('xpath=ancestor::section[1]')


def chart_card(page, title):
    return page.get_by_role('region', name=title, exact=True)


def open_block(page, name):
    button = panel(page).locator('button[aria-expanded]').filter(has_text=re.compile('^' + re.escape(name)))
    if button.get_attribute('aria-expanded') != 'true':
        button.click()
    expect(button).to_have_attribute('aria-expanded', 'true')
    return button.locator('xpath=..')


def kv(scope, label):
    return scope.locator(f"xpath=.//dt[normalize-space()='{label}']/following-sibling::dd[1]")


def row_cells(scope, label):
    row = scope.get_by_role('row').filter(has=scope.page.get_by_role('cell', name=label, exact=True))
    expect(row).to_have_count(1)
    return row.get_by_role('cell')


def painted(page, card):
    page.wait_for_function("""canvas => canvas.width > 0 && canvas.height > 0 &&
        canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data
          .some((value, index) => index % 4 === 3 && value > 0)""", arg=card.locator('canvas').element_handle())


def query_of(request):
    return {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}


def assert_page_matches(page, oracle, summary, own=None):
    """Every presentation layer of the shown saved result against the oracle (see the module docstring)."""
    ident, net, steps = oracle['ident'], oracle['net'], oracle['steps']
    expect(page.get_by_role('region', name='Evaluation conditions and versions').get_by_text(
        re.compile(r'^Benchmark ' + re.escape(ident) + ' · published'))).to_be_visible()
    # money: the result line, then the table mode's four steps, then back to the chart
    total0, change1, change2, total3 = (formats(page, [steps[0]], 'money')[0], *formats(page, steps[1:3], 'signed'),
                                        formats(page, [steps[3]], 'money')[0])
    money = chart_card(page, 'Net revenue after operating costs')
    expect(money.get_by_text(f'ShelfShift total {total3} vs Modeled store rule {total0}: {change1} from equivalent '
                             f'stock checks and {change2} from ShelfShift decisions vs modeled rule.', exact=True)).to_be_visible()
    money.get_by_role('button', name='Show as table').click()
    for label, value in (('Modeled store rule with original records', total0),
                         ('Change from equivalent stock checks', change1),
                         ('Change from ShelfShift decisions vs modeled rule', change2),
                         ('ShelfShift decisions with those checks', total3)):
        expect(row_cells(money, label).nth(1)).to_have_text(value)
    money.get_by_role('button', name='Show as chart').click()
    expect(money.locator('canvas')).to_be_visible()
    # waste: the table mode's totals per scenario, in legend order
    waste = chart_card(page, 'Waste within the window')
    waste.get_by_role('button', name='Show as table').click()
    units = formats(page, [oracle['waste_units'][s] for s in SCENARIOS], 'units')
    kg = formats(page, [oracle['waste_kg'][s] for s in SCENARIOS], 'units')
    totals = waste.get_by_role('row').filter(has_text=re.compile(r'^Total · '))
    for i, s in enumerate(SCENARIOS):
        expect(totals.get_by_role('cell').nth(i + 1)).to_have_text(f'{units[i]} units · {kg[i]} kg')
    waste.get_by_role('button', name='Show as chart').click()
    # headline: ShelfShift's tiles, then every scenario's net; a null shows its recorded reason without a hover
    headline = section_named(page, 'Headline figures')
    head_net = formats(page, [oracle['ledger']['net'][s] for s in SCENARIOS], 'money')
    expect(headline.get_by_text(head_net[2], exact=True).first).to_be_visible()
    expect(headline.get_by_text(f'Modeled store rule: {head_net[0]} · changes shown above', exact=True)).to_be_visible()
    expect(headline.get_by_text(f"{formats(page, [oracle['headline']['shelfshift']['units_wasted']], 'units')[0]} units",
                                exact=True)).to_be_visible()
    toggle = headline.get_by_role('button', name='Show all scenario figures', exact=True)
    toggle.click()
    cells = row_cells(headline, 'Net revenue after operating costs')
    for i in range(4):
        expect(cells.nth(i + 1)).to_have_text(head_net[i])
    labels = {'action_adjusted_fulfillment': 'Action-adjusted fulfillment', 'sell_through_moved': 'Sell-through of moved stock',
              'full_price_coverage': 'Full-price demand coverage', 'shortfall_filled': 'Shortfall units filled'}
    missing = [(s, k) for s in SCENARIOS for k, label in labels.items() if summary['headline'][s][k] is None]
    assert missing, 'the simulator records no action-adjusted fulfillment, so at least one value is unavailable'
    for s, k in missing:
        button = headline.get_by_role('button', name=f'Why unavailable: {labels[k]} · {s}', exact=True)
        button.focus()
        expect(button).to_be_focused()
        assert button.get_attribute('aria-describedby')
        box = button.bounding_box()
        assert box['width'] >= 44 and box['height'] >= 44
        page.keyboard.press('Enter')
        expect(button).to_have_attribute('aria-expanded', 'true')
        expect(button.locator('xpath=..').get_by_text(summary['headline'][s]['reasons'][k], exact=True)).to_be_visible()
    # the four blocks
    checks = open_block(page, 'Stock checks & trips')
    for key, label in (('requests', 'Requests'), ('lots_counted', 'Lots counted'), ('corrected_up', 'Records corrected up'),
                       ('corrected_down', 'Records corrected down'), ('nothing_to_correct', 'Nothing to correct')):
        cells = row_cells(checks, label)
        for i, s in enumerate(SCENARIOS):
            if s in oracle['checks']:
                expect(cells.nth(i + 1)).to_have_text(formats(page, [oracle['checks'][s][key]], 'units')[0])
            else:
                expect(checks.get_by_role('button', name=f'Why unavailable: {label} · {s}', exact=True)).to_be_visible()
    for key, label in (('short', 'Short trips'), ('failed', 'Failed trips')):
        cells = row_cells(checks, label)
        for i, s in enumerate(SCENARIOS):
            expect(cells.nth(i + 1)).to_have_text(formats(page, [oracle['ledger']['trips'][s][key]], 'units')[0])
    quality = open_block(page, 'Decision quality')
    for key, label in (('opened', 'Opened'), ('approved', 'Approved'), ('rejected', 'Rejected'),
                       ('waiting', 'Waiting at the end of the window')):
        expect(kv(quality, label)).to_have_text(formats(page, [oracle['decisions'][key]], 'units')[0])
    forecast = open_block(page, 'Forecast quality')
    expect(forecast.get_by_text(f"Forecast run {oracle['forecast']['forecast_run_id']}", exact=True)).to_be_visible()
    expect(kv(forecast, 'Scored days')).to_have_text(formats(page, [oracle['forecast']['scored_days']], 'units')[0])
    branch = open_block(page, 'By branch')
    want = oracle['by']['money', 'branch']
    rows = branch.get_by_role('table').locator('tbody tr')
    expect(rows).to_have_count(len(want))
    for i in range(len(want)):
        code = rows.nth(i).locator('th span.font-mono').inner_text()
        if own is not None and i == 0:
            assert code == own
            expect(rows.nth(0).get_by_text('Your store', exact=True)).to_be_visible()
        figures = formats(page, [want[code]['store_rule'], want[code]['shelfshift']], 'money')
        expect(rows.nth(i).locator('td').nth(0)).to_have_text(figures[0])
        expect(rows.nth(i).locator('td').nth(3)).to_have_text(figures[1])


def assert_breakdown_rows(page, section, oracle, by):
    want = oracle['by']['money', by]
    rows = section.get_by_role('table').locator('tbody tr')
    expect(rows).to_have_count(len(want))
    for i in range(len(want)):
        key = i if by == 'day' else rows.nth(i).locator('span.font-mono').inner_text().split()[0]
        expect(rows.nth(i).locator('td').last).to_have_text(formats(page, [want[key]['shelfshift']], 'money')[0])


def test_real_saved_result_renders_through_every_presentation_layer(visitor, tmp_path, monkeypatch, capsys):
    """A complete saved benchmark, published by the real job on a TEST ONLY eligible dataset, read on the real
    server with no Reports interception: every presentation layer equals the independent oracle; a second
    publication never changes an open A breakdown; historical and branch scopes; missing reasons; no-results and
    stale states; canvas captures at 360/390/1280 for the end-of-phase human check (never claimed here)."""
    page = visitor.page
    switch_seat(page, None)
    home = session_json(page)['run_id']
    dv = refdata.install(eligible_artifact(tmp_path / 'eligible', purpose='08-21 presentation'), allow_local=False)
    ref = refdata.get(dv)
    stores = sorted(ref.branches)
    created, made = [], set()
    monkeypatch.setenv('IMAGE_DIGEST', 'test-only-image-digest-08-21')
    monkeypatch.setenv('GIT_COMMIT', 'test-only-commit-08-21')

    def publish():
        capsys.readouterr()
        code = weekcheck.main(['benchmark', 'start', '--dataset', dv])
        report = json.loads(capsys.readouterr().out.splitlines()[-1])
        if report.get('benchmark_id'):
            made.add(report['benchmark_id'])
        assert code == 0 and report['status'] == 'PUBLISHED', report
        return report['benchmark_id']

    ws = workspace_factory(dv, created)()
    CAPTURES.mkdir(parents=True, exist_ok=True)
    try:
        page.goto('/#/reports?tab=benchmark')  # no-results state before any publication
        expect(panel(page).get_by_role('heading', name='Evaluation not yet available')).to_be_visible()
        expect(panel(page).locator('canvas')).to_have_count(0)
        a = publish()
        with db.tx() as conn:
            oracle = saved_oracle(conn, a, ref)

        # a store manager on the dataset's workspace: current A, own store pinned
        bind(page, ws['run_id'], 'manager', stores[0])
        body = read(page, '/api/reports/benchmark')
        assert body['status'] == 'current' and body['current']['benchmark_id'] == a
        assert_summary_matches(body['current']['summary'], oracle)
        page.goto('/#/reports?tab=benchmark')
        expect(chart_card(page, 'Net revenue after operating costs')).to_be_visible()
        assert_page_matches(page, oracle, body['current']['summary'], own=stores[0])
        money = chart_card(page, 'Net revenue after operating costs')
        with page.expect_request(lambda r: urlsplit(r.url).path == '/api/reports/benchmark'
                                 and query_of(r).get('breakdown') == 'day') as asked:
            money.get_by_role('button', name='View breakdown: Net revenue after operating costs', exact=True).click()
        assert query_of(asked.value)['benchmark_id'] == a and query_of(asked.value)['chart'] == 'money'
        section = page.get_by_role('region', name='Money breakdown', exact=True)
        assert_breakdown_rows(page, section, oracle, 'day')

        # B publishes while A's breakdown is open; a TEST ONLY marker on B's stored ShelfShift rows makes them differ
        b = publish()
        with db.tx() as conn:
            db.run(conn, "UPDATE benchmark_branch_day SET revenue = revenue + 1000, net = net + 1000 "
                         "WHERE benchmark_id = :b AND scenario = 'shelfshift'", b=b)
            marked = saved_oracle(conn, b, ref)
        assert read(page, '/api/reports/benchmark')['current']['benchmark_id'] == b
        with page.expect_request(lambda r: urlsplit(r.url).path == '/api/reports/benchmark'
                                 and query_of(r).get('breakdown') == 'branch') as asked:
            section.get_by_role('button', name='By branch', exact=True).click()
        assert query_of(asked.value)['benchmark_id'] == a
        assert_breakdown_rows(page, section, oracle, 'branch')  # A's amounts, never B's
        expect(page.get_by_text(re.compile(r'^Benchmark ' + re.escape(a) + ' · published'))).to_be_visible()
        expect(page.get_by_text(re.compile(r'^Benchmark ' + re.escape(b) + ' · published'))).to_have_count(0)
        page.reload()  # a new read shows the new current result, B, with its own amounts
        expect(page.get_by_text(re.compile(r'^Benchmark ' + re.escape(b) + ' · published'))).to_be_visible()
        money = chart_card(page, 'Net revenue after operating costs')
        money.get_by_role('button', name='View breakdown: Net revenue after operating costs', exact=True).click()
        section = page.get_by_role('region', name='Money breakdown', exact=True)
        with page.expect_request(lambda r: query_of(r).get('breakdown') == 'branch') as asked:
            section.get_by_role('button', name='By branch', exact=True).click()
        assert query_of(asked.value)['benchmark_id'] == b
        assert_breakdown_rows(page, section, marked, 'branch')
        assert marked['by']['money', 'branch'] != oracle['by']['money', 'branch']

        # A by its ID stays A: canvas captures for the end-of-phase human check (negative/near-zero steps included)
        for width in (360, 390, 1280):
            page.set_viewport_size({'width': width, 'height': 800})
            page.emulate_media(reduced_motion='reduce')
            page.goto(f'/#/reports?tab=benchmark&benchmark_id={a}')
            page.reload()
            money, waste = chart_card(page, 'Net revenue after operating costs'), chart_card(page, 'Waste within the window')
            for card in (money, waste):
                expect(card.locator('canvas')).to_be_visible()
                painted(page, card)
            page.set_viewport_size({'width': width, 'height': 1600})
            money.screenshot(path=str(CAPTURES / f'{width}-real-money-chart.png'))
            waste.screenshot(path=str(CAPTURES / f'{width}-real-waste-chart.png'))
            money.get_by_role('button', name='Show as table').click()
            money.screenshot(path=str(CAPTURES / f'{width}-real-money-table.png'))
            money.get_by_role('button', name='Show as chart').click()
            section_named(page, 'Headline figures').get_by_role('button', name='Show all scenario figures').click()
            for name in ('Stock checks & trips', 'Decision quality', 'Forecast quality', 'By branch'):
                open_block(page, name)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.set_viewport_size({'width': 1280, 'height': 800})

        # HQ on the dataset's workspace, one store named: the by-branch rows narrow to it, A's ID kept
        bind(page, ws['run_id'])
        page.goto(f'/#/reports?tab=benchmark&benchmark_id={a}&branch={stores[-1]}')
        expect(page.get_by_text(re.compile(r'^Benchmark ' + re.escape(a) + ' · published'))).to_be_visible()
        with page.expect_request(lambda r: query_of(r).get('breakdown') == 'branch') as asked:
            open_block(page, 'By branch')
        assert query_of(asked.value)['benchmark_id'] == a and query_of(asked.value)['branch'] == stores[-1]
        rows = panel(page).get_by_role('table').locator('tbody tr').filter(has=page.locator('th span.font-mono'))
        expect(rows).to_have_count(1)
        expect(rows.first.locator('th span.font-mono')).to_have_text(stores[-1])
        figures = formats(page, [oracle['by']['money', 'branch'][stores[-1]]['shelfshift']], 'money')
        expect(rows.first.locator('td').nth(3)).to_have_text(figures[0])

        # HQ on the release template's workspace: historical B, collapsed and marked, its breakdown pinned to B
        bind(page, home)
        page.goto('/#/reports?tab=benchmark')
        disclosure = panel(page).get_by_role('button', name=re.compile('^Historical benchmark'))
        expect(disclosure).to_have_attribute('aria-expanded', 'false')
        expect(panel(page).locator('canvas')).to_have_count(0)
        assert '$' not in panel(page).inner_text()
        disclosure.click()
        expect(page.get_by_text(re.compile(r'^Benchmark ' + re.escape(b) + ' · published'))).to_be_visible()
        expect(panel(page).get_by_text(re.compile(r'^Differs · app ')).first).to_be_visible()
        with page.expect_request(lambda r: query_of(r).get('breakdown') == 'branch') as asked:
            open_block(page, 'By branch')
        assert query_of(asked.value)['benchmark_id'] == b and 'branch' not in query_of(asked.value)
        rows = panel(page).get_by_role('table').locator('tbody tr').filter(has=page.locator('th span.font-mono'))
        expect(rows).to_have_count(len(stores))

        # stale: the workspace tab keeps what it showed when a refresh fails, then recovers on Try again
        bind(page, ws['run_id'], 'manager', stores[0])
        page.goto('/#/reports')
        shown = assert_workspace_scope(page, stores[0])
        page.context.set_offline(True)
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        banner = page.get_by_text("Couldn't refresh. Showing what was loaded before.", exact=True)
        expect(banner).to_be_visible()
        tiles(page, shown)
        page.context.set_offline(False)
        page.get_by_role('button', name='Try again', exact=True).click()
        expect(banner).to_have_count(0)
        tiles(page, shown)
    finally:
        page.context.set_offline(False)
        bind(page, home)
        drop_benchmarks(made)
        with db.tx() as conn:
            drop_runs(conn, list(reversed(created)))


def assert_workspace_scope(page, store):
    body = read(page, '/api/reports/workspace')
    assert body['scope'] == {'branch': store, 'role': 'manager'}
    tiles(page, body)
    return body
