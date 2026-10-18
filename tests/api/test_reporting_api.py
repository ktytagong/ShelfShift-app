"""Reports HTTP reads against the isolated reports test database (08-CONTEXT D-04, D-15).

Expected case sets and counts are derived independently from the contract, not reporting helpers.
All mutations belong to fresh test workspaces; published fixtures are removed at teardown.
"""
from contextlib import contextmanager
from dataclasses import asdict
from uuid import uuid4

import sqlalchemy as sa
import pytest

from api import cases, db, refdata, sandbox
from tests.api.conftest import as_seat, by_tag, fresh, owned_send, seat


def workspace_of(client):
    return fresh(client.get('/api/session').json()['run_id'])


def activity(client, branch=None):
    r = client.get('/api/reports/workspace', params={} if branch is None else {'branch': branch})
    assert r.status_code == 200, r.json()
    return r.json()


def oracle(run, store, manager):
    """Contract-derived sets, including requests addressed outside their case's involvement."""
    with db.tx() as conn:
        rows = db.rows(conn, 'SELECT * FROM cases WHERE run_id = :r', r=run['run_id'])
        requests = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND state = 'OPEN'",
                           r=run['run_id'])
        movements = db.rows(conn, 'SELECT * FROM movements WHERE run_id = :r', r=run['run_id'])
        decisions = db.rows(conn, 'SELECT * FROM decisions WHERE run_id = :r', r=run['run_id'])
        now = db.one(conn, 'SELECT now() AS t')['t']
    scoped = [c for c in rows if store is None or store in (c['source_branch'], c['plan_dest'])]
    ids = {c['case_id'] for c in scoped}
    live = [c for c in scoped if c['state'] in ('OPEN', 'WAITING', 'PENDING_APPROVAL')]
    pending = [c for c in live if c['state'] == 'PENDING_APPROVAL']
    committed = [c for c in scoped if c['state'] in ('APPROVED', 'PREPARED', 'EXECUTED', 'OBSERVED')]
    checks = [q for q in requests if store is None or q['case_id'] in ids or q['to_branch'] == store]
    def yours(c):
        if manager is None:
            return False
        if c['state'] != 'PENDING_APPROVAL':
            return c['source_branch'] == manager
        decided = {d['role'] for d in decisions if d['case_id'] == c['case_id'] and
                   d['rec_version'] == c['active_rec_version'] and d['round'] == c['round']}
        return ((c['source_branch'] == manager and 'source' not in decided) or
                (c['plan_dest'] == manager and 'destination' not in decided))
    def moved(kind):
        return sum(c['plan_dest'] is not None and any(m['case_id'] == c['case_id'] and m['kind'] == kind
                   for m in movements) for c in committed)
    return {'open': {'count': len(live), 'need_action': sum(yours(c) for c in live)},
            'pending_approval': {'count': len(pending), 'waiting_for_you': sum(yours(c) for c in pending)},
            'unanswered_check': {'count': len(checks),
                                'past_due': sum(q['due_at'] is not None and q['due_at'] <= now for q in checks),
                                'addressed_to_you': sum(q['to_branch'] == manager for q in checks)},
            'committed': {'count': len(committed), 'dispatched': moved('DISPATCH'), 'received': moved('RECEIPT')},
            'cancelled': {'count': sum(c['state'] == 'CLOSED' and c['closed_as'] == 'CANCELLED' for c in scoped)}}


def add_request(conn, run, case, branch, due='NULL'):
    ident = 'report-' + uuid4().hex
    db.run(conn, 'INSERT INTO evidence_requests (run_id, request_id, case_id, rec_version, fact_id, fact_kind, '
           'sku, lot_id, to_branch, ask_text, due_day, due_min, state, created_by, created_day, due_at) '
           f"VALUES (:r, :q, :c, :v, :q, 'PHANTOM_STOCK', :sku, :lot, :b, 'Count the stock', 0, 0, 'OPEN', "
           f"'test', 0, {due})", r=run['run_id'], q=ident, c=case['case_id'], v=case['active_rec_version'],
           sku=case['sku'], lot=case['lot_id'], b=branch)
    return ident


def test_workspace_manager_and_hq_scope_and_sql_oracle(client):
    run = workspace_of(client)
    story = by_tag(run, cases.STORY_TAG)
    mine = story['source_branch']
    other = next(b for b in refdata.get(run['dataset_version']).branches if b != mine)
    as_seat(client, mine)
    result = activity(client)
    assert result['scope'] == {'branch': mine, 'role': 'manager'}
    assert result == activity(client, other)
    assert result['activity'] == oracle(run, mine, mine)
    as_seat(client, None)
    assert activity(client)['activity'] == oracle(run, None, None)
    assert activity(client, mine)['activity'] == oracle(run, mine, None)
    assert activity(client)['scope'] == {'branch': None, 'role': 'hq'}
    bad = client.get('/api/reports/workspace?branch=not-a-store')
    assert (bad.status_code, bad.json()['error']) == (400, 'bad_request')


def test_pending_approval_owners_and_incoming_scope(client):
    run = workspace_of(client)
    story = by_tag(run, cases.STORY_TAG)
    src, dest = story['source_branch'], story['plan_dest']
    assert dest is not None
    before = activity(client)['activity']['pending_approval']['count']
    owned_send(run, seat(src), story['case_id'], story['active_rec_version'])
    for branch in (src, dest):
        as_seat(client, branch)
        result = activity(client)['activity']
        assert result == oracle(run, branch, branch)
        assert result['pending_approval'] == {'count': before + 1, 'waiting_for_you': 1}
    as_seat(client, None)
    assert activity(client)['activity']['pending_approval']['waiting_for_you'] == 0
    from tests.api.verify_helpers import approve_with_ack
    approve_with_ack(run, story['case_id'], seat(src))  # names the plan in force (07-17; 07-CONTEXT D-23, D-32)
    for branch, count in ((src, 0), (dest, 1)):
        as_seat(client, branch)
        assert activity(client)['activity']['pending_approval']['waiting_for_you'] == count


def test_due_boundary_and_cancelled_and_zero_tiles(client):
    run = workspace_of(client)
    story = by_tag(run, cases.STORY_TAG)
    as_seat(client, story['source_branch'])
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r",
               r=run['run_id'])
        db.run(conn, 'DELETE FROM evidence_requests WHERE run_id = :r', r=run['run_id'])
    inserted = []
    def before_read(conn, cursor, statement, parameters, context, executemany):
        if inserted or 'from cases' not in statement.lower():
            return
        inserted.append(True)
        # Insert into this HTTP read's transaction: exact equality and +1 second share its database clock.
        add_request(conn, run, story, story['source_branch'], 'now()')
        add_request(conn, run, story, story['source_branch'], "now() + interval '1 second'")
        add_request(conn, run, story, story['source_branch'])
    sa.event.listen(db.engine(), 'before_cursor_execute', before_read)
    try:
        result = activity(client)['activity']
    finally:
        sa.event.remove(db.engine(), 'before_cursor_execute', before_read)
    assert inserted == [True]
    assert result['unanswered_check'] == {'count': 3, 'past_due': 1, 'addressed_to_you': 3}
    assert result['open'] == {'count': 0, 'need_action': 0}
    assert result['pending_approval'] == {'count': 0, 'waiting_for_you': 0}
    assert result['cancelled']['count'] > 0


def test_committed_transfers_count_movements_once_and_keep_is_included(client):
    run = workspace_of(client)
    story = by_tag(run, cases.STORY_TAG)
    as_seat(client, None)
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = 'APPROVED' WHERE run_id = :r", r=run['run_id'])
        for kind in ('DISPATCH', 'DISPATCH', 'RECEIPT'):
            db.run(conn, 'INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, qty, '
                   "recorded_by, role, scenario_day) VALUES (:r, :m, :c, 'test', :k, 1, 'test', 'hq', 0)",
                   r=run['run_id'], m=uuid4().hex, c=story['case_id'], k=kind)
    result = activity(client)['activity']
    assert result == oracle(run, None, None)
    assert result['committed']['dispatched'] == result['committed']['received'] == 1
    assert result['committed']['count'] > 1


def test_activity_uses_one_snapshot_during_a_coordinated_second_connection_write(client):
    run = workspace_of(client)
    as_seat(client, None)
    before = activity(client)['activity']
    story = by_tag(run, cases.STORY_TAG)
    mutations = []
    def after_read(conn, cursor, statement, parameters, context, executemany):
        if mutations or not statement.lstrip().lower().startswith(('select', 'with')) or 'from cases' not in statement.lower():
            return
        mutations.append(True)
        # A distinct connection commits between the first activity read and any attempted later group reads.
        with db.tx() as writer:
            assert writer is not conn
            db.run(writer, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' "
                   'WHERE run_id = :r AND case_id = :c', r=run['run_id'], c=story['case_id'])
            add_request(writer, run, story, story['source_branch'], 'now()')
    sa.event.listen(db.engine(), 'after_cursor_execute', after_read)
    try:
        result = activity(client)['activity']
    finally:
        sa.event.remove(db.engine(), 'after_cursor_execute', after_read)
    assert mutations == [True]
    assert result == before
    after = activity(client)['activity']
    assert after['open']['count'] == before['open']['count'] - 1
    assert after['cancelled']['count'] == before['cancelled']['count'] + 1
    assert after['unanswered_check']['count'] == before['unanswered_check']['count'] + 1


@contextmanager
def published_fixture(run):
    """Complete seven-day, four-world zero-activity contract fixture; no writer/publication claim."""
    ident = 'bm-' + uuid4().hex[:12]
    worlds = ['baseline_rule', 'baseline_checked', 'shelfshift', 'do_nothing']
    branches = list(refdata.get(run['dataset_version']).branches)
    domain = {'worlds': worlds, 'branches': branches, 'days': list(range(7))}
    from data.versions import digest
    settings = {
        'forecast_rule': 'latest issued before the day, d3 reused through day 6',
        'closing_rule': 'after day 6, before day 7',
        'checks': {'evidence': ['PHANTOM_STOCK', 'STALE_COUNT'], 'cap_per_version': 3,
                   'lot_once_per_day': True, 'answers': "exact, from each world's own physical stock"},
        'cancel_cost': 'difference versus scenario 1 adjusted for wholly unshipped trip charges',
        'keep_fast_path': True, 'price_response': {'m20': 1.0, 'm30': 1.0},
        'commits': {'retry_limit': 2, 'retry_codes': ['conflict', 'hold_expired', 'stale_version'],
                    'leave_open': 'any other refusal, recorded by reason', 'order': 'case_id ascending'},
        'ordering': {'enabled_stores': [], 'rule': 'no store enabled'}, 'sensitivity': None}
    checkpoints = [{'day': d, 'completed': True, 'worlds': dict.fromkeys(worlds, True)} for d in range(7)]
    result_rows = [(ident, w, d, b, *([0] * 12)) for w in worlds for d in range(7) for b in branches]
    daily_zero = [{'day': d, 'units': 0, 'kg': 0} for d in range(7)]
    payload = {
        'kind': 'SIMULATED',
        'scenarios': [{'id': name, 'world': world, 'label': label} for name, world, label in zip(
            ('store_rule', 'store_rule_checked', 'shelfshift', 'markdowns_only'), worlds,
            ('Modeled store rule with original records', 'Modeled store rule with equivalent stock checks',
             'ShelfShift decisions with those checks', 'Automatic markdowns only · no transfers or additional intervention'))],
        'network': {'stores': len(branches), 'at_risk_lots_by_day': [{'day': d, 'lots': 0} for d in range(7)]},
        'money': {'by_scenario': dict.fromkeys(worlds, 0), 'steps': [0, 0, 0, 0],
                  'by_day': {w: [{'day': d, 'net': 0} for d in range(7)] for w in worlds}},
        'settings': settings, 'policy': asdict(refdata.get(run['dataset_version']).policy), 'footnote_x': 0,
        'waste': {w: {'by_day': daily_zero, 'totals': {'units': 0, 'kg': 0},
                      'change_vs_markdowns_only': {'units': 0, 'kg': 0}} for w in worlds},
        'headline': {w: {'net': 0, 'full_price_coverage': None, 'shortfall_filled': None,
                        'action_adjusted_fulfillment': None, 'incremental_units_sold_vs_markdowns_only': 0,
                        'units_wasted': 0, 'kg_wasted': 0, 'sell_through_moved': None,
                        'reasons': {'full_price_coverage': 'no demand', 'shortfall_filled': 'no shortfall',
                                    'action_adjusted_fulfillment': 'no demand', 'sell_through_moved': 'no transfers'}}
                     for w in worlds},
        'checks': {w: (dict.fromkeys(('requests', 'lots_counted', 'corrected_up', 'corrected_down', 'nothing_to_correct'), 0)
                       if w in ('baseline_checked', 'shelfshift') else None) for w in worlds},
        'trips': {w: {'short': 0, 'failed': 0, 'transport_unshipped': 0} for w in worlds},
        'closing': {w: {'on_hand_units': 0, 'in_transit_units': 0, 'open_transfers': 0, 'costs_charged': 0} for w in worlds},
        'decision_quality': {w: {'limit_breaks': 0, 'opened': 0, 'approved': 0, 'rejected': 0, 'waiting': 0,
                                 'left_open': {}, 'retry_limit': 2, 'time_to_decision': None} for w in worlds},
        'forecast_quality': {'forecast_run_id': None, 'window': None, 'cutoff_day': None, 'wape': None,
                             'baseline_wape': None, 'bias': None, 'baseline_bias': None, 'coverage_80': None,
                             'scored_days': 0, 'excluded_stockout_days': 0, 'providers': [], 'input_counts': {},
                             'series_without_in_stock_days': 0, 'reason': 'No forecast series in this zero-activity fixture'},
        'assumptions': {'markdown_response': {'m20': 1.0, 'm30': 1.0}, 'stock_record_errors': 'none'},
        'categories_without_schedule': []}
    receipt = {'expected_domain_digest': digest('domain-', domain),
               'completed_checkpoint_digest': digest('days-', checkpoints),
               'final_row_digest': digest('rows-', result_rows, payload)}
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) "
               "VALUES (:b, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", b=ident, v=run['dataset_version'])
        db.run(conn, 'INSERT INTO benchmark_runs (benchmark_id, status, sim_run_id, dataset_version, '
               'engine_version, policy_version, forecast_runs, eval_version, settings_digest, settings, '
               'git_commit, first_day, last_day, last_day_done, published_at, expected_domain, validation_receipt) '
               "VALUES (:b, 'PUBLISHED', :b, :v, 'fixture-engine', 'fixture-policy', '[]', 'eval-1', "
               ":sd, CAST(:settings AS jsonb), 'fixture-commit', 0, 6, 6, now(), CAST(:d AS jsonb), CAST(:receipt AS jsonb))",
               b=ident, v=run['dataset_version'], d=db.js(domain), sd=digest('es-', settings),
               settings=db.js(settings), receipt=db.js(receipt))
        for day in range(7):
            db.run(conn, 'INSERT INTO benchmark_days VALUES (:b, :d, now(), 0, CAST(:stats AS jsonb))',
                   b=ident, d=day, stats=db.js({'completed': True, 'worlds': dict.fromkeys(worlds, True)}))
        db.insert_rows(conn, 'benchmark_branch_day',
                       ('benchmark_id', 'scenario', 'day', 'branch', 'revenue', 'transport', 'handling', 'disposal',
                        'net', 'sold', 'wasted', 'wasted_kg', 'lost', 'demand', 'moved_out', 'moved_in'),
                       result_rows)
        db.run(conn, 'INSERT INTO benchmark_summary VALUES (:b, CAST(:p AS jsonb))', b=ident,
               p=db.js(payload))
    try:
        yield ident
    finally:
        with db.tx() as conn:
            for table in ('benchmark_summary', 'benchmark_branch_day', 'benchmark_observations', 'benchmark_days',
                          'benchmark_runs'):
                db.run(conn, f'DELETE FROM {table} WHERE benchmark_id = :b', b=ident)
            db.run(conn, 'DELETE FROM demo_runs WHERE run_id = :b', b=ident)


def test_benchmark_none_then_historical_never_current(client):
    assert client.get('/api/reports/benchmark').json() == {'status': 'none', 'current': None, 'historical': None}
    with published_fixture(workspace_of(client)) as ident:
        for branch in (client.get('/api/session').json()['branch'], None):
            as_seat(client, branch)
            result = client.get('/api/reports/benchmark').json()
            assert result['status'] == 'historical' and result['current'] is None
            assert result['historical']['benchmark_id'] == ident
            assert set(result['historical']['versions']) == {'engine', 'dataset', 'policy', 'forecast_runs',
                                                           'evaluator', 'settings'}
            assert result['historical']['differs'] == []
            assert 'image_digest' not in result['historical']
        bad = client.get('/api/reports/benchmark?branch=not-a-store')
        assert (bad.status_code, bad.json()['error']) == (400, 'bad_request')
        for status in ('RUNNING', 'VALIDATED', 'FAILED', 'TRIAL'):
            with db.tx() as conn:
                db.run(conn, 'UPDATE benchmark_runs SET status = :s WHERE benchmark_id = :b', s=status, b=ident)
            assert client.get('/api/reports/benchmark').json() == {'status': 'none', 'current': None, 'historical': None}


REPORT_NAMES = ('open', 'pending_approval', 'unanswered_check', 'committed', 'cancelled')


def contributing_ids(run, store, report):
    """Independent SQL oracle, retaining addressed requests even outside a case's source/destination."""
    states = {'open': "c.state IN ('OPEN','WAITING','PENDING_APPROVAL')",
              'pending_approval': "c.state = 'PENDING_APPROVAL'",
              'committed': "c.state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')",
              'cancelled': "c.state = 'CLOSED' AND c.closed_as = 'CANCELLED'"}
    involvement = '(:all_stores OR c.source_branch = :store OR c.plan_dest = :store)'
    clause = ("EXISTS (SELECT 1 FROM evidence_requests q WHERE q.run_id = c.run_id AND q.case_id = c.case_id "
              f"AND q.state = 'OPEN' AND ({involvement} OR q.to_branch = :store))" if report == 'unanswered_check'
              else f'({states[report]}) AND {involvement}')
    with db.tx() as conn:
        return {r['case_id'] for r in db.rows(conn, f'SELECT c.case_id FROM cases c WHERE c.run_id = :r AND {clause}',
                                            r=run['run_id'], store=store, all_stores=store is None)}


def walk_report(client, report, branch=None, **filters):
    query = {'report': report, **filters}
    if branch is not None:
        query['branch'] = branch
    cards, page = [], 1
    while True:
        result = client.get('/api/cases', params={**query, 'page': page})
        assert result.status_code == 200, result.json()
        body = result.json()
        cards.extend(body['cases'])
        assert all(not c.get('needs_evaluation') for c in body['cases'])
        assert sum(body['stage_counts'].values()) == body['total']
        if len(cards) >= body['total']:
            assert len(cards) == len({c['case_id'] for c in cards}) == body['total']
            return cards, body
        assert body['cases'], 'paging stopped before reaching total'
        page += 1


def test_report_rejects_unknown(client):
    result = client.get('/api/cases?report=anything-else')
    assert (result.status_code, result.json().get('error')) == (400, 'bad_request')
    assert result.json()['message'] == 'report is one of ' + ', '.join(REPORT_NAMES)


def set_up_report_cases(client):
    run = workspace_of(client)
    story = by_tag(run, cases.STORY_TAG)
    owned_send(run, seat(story['source_branch']), story['case_id'], story['active_rec_version'])
    with db.tx() as conn:
        others = db.rows(conn, 'SELECT * FROM cases WHERE run_id = :r AND case_id <> :c ORDER BY case_id',
                         r=run['run_id'], c=story['case_id'])
        assert len(others) >= 3
        db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=others[0]['case_id'])
        db.run(conn, "UPDATE cases SET state = 'APPROVED' WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=others[1]['case_id'])
        # Addressed requests count even when a case belongs to other stores, once per case in the board.
        foreign = next(c for c in others if story['source_branch'] not in (c['source_branch'], c['plan_dest']))
        add_request(conn, run, foreign, story['source_branch'])
        add_request(conn, run, foreign, story['source_branch'])
        add_request(conn, run, story, story['plan_dest'])
    return run, story


@pytest.mark.parametrize('default_scope', [None, 'branch', 'all'])
@pytest.mark.parametrize('viewer', ['source', 'destination', 'hq', 'hq_source'])
def test_report_case_sets_match_tiles_with_strict_scope_and_existing_filters(client, monkeypatch, default_scope, viewer):
    run, story = set_up_report_cases(client)
    ref = refdata.get(run['dataset_version'])
    if default_scope is not None:
        monkeypatch.setitem(ref.settings, 'board', {'default_scope': default_scope, 'page_size': 2})
    source, destination = story['source_branch'], story['plan_dest']
    manager = source if viewer == 'source' else destination if viewer == 'destination' else None
    store = source if viewer == 'hq_source' else manager
    as_seat(client, manager)
    # Manager asks for another branch even on an all-stores board; HQ explicitly scopes only when requested.
    requested = source if viewer == 'hq_source' else destination if viewer == 'source' else source if manager else None
    tiles = activity(client, requested)['activity']
    assert tiles == oracle(run, store, manager)
    for report in REPORT_NAMES:
        cards, body = walk_report(client, report, requested)
        expected = contributing_ids(run, store, report)
        assert {c['case_id'] for c in cards} == expected, (viewer, default_scope, report)
        assert body['scope']['branch'] == store
        if report != 'unanswered_check':
            assert body['total'] == tiles[report]['count']
        if report == 'pending_approval':
            assert expected == {story['case_id']}
        filtered, _ = walk_report(client, report, requested, product=story['sku'])
        assert {c['case_id'] for c in filtered} == {c['case_id'] for c in cards if c['sku'] == story['sku']}


def test_public_template_report_excludes_flags_and_filters_case_ids(public_template, client, monkeypatch):
    from api import config
    run = workspace_of(client)
    monkeypatch.setattr(config, 'CASE_OPENING', {**config.CASE_OPENING, 'min_waste_units': 0})
    with db.tx() as conn:
        cases.open_cases(conn, run)
    as_seat(client, None)
    initial = client.get('/api/cases').json()
    assert initial['at_risk'] > 0
    for report in REPORT_NAMES:
        cards, body = walk_report(client, report)
        assert {c['case_id'] for c in cards} == contributing_ids(run, None, report)
        assert body['total'] == activity(client)['activity'][report]['count']


def test_shared_results_survive_workspace_reset_and_real_pruning(client):
    from tests.api.conftest import key
    from tests.api.test_reporting_static import RESULT_TABLES

    run = workspace_of(client)
    with published_fixture(run) as ident:
        def counts(conn):
            return {table: db.one(conn, f'SELECT count(*) AS n FROM {table} WHERE benchmark_id = :b',
                                  b=ident)['n'] for table in RESULT_TABLES}
        with db.tx() as conn:
            assert not RESULT_TABLES.intersection(sandbox.per_run_tables(conn))
            before = counts(conn)
            # The two history tables support non-benchmark seed checks too. Neither is workspace state.
            db.run(conn, 'INSERT INTO benchmark_transfer_audit (sim_run_id, benchmark_id, world, day, action_id, '
                   'planned_units, shipped_units, effective_input, constraint_reasons, charge_id, '
                   'transport_amount, handling_amount) '
                   "VALUES (:b, :b, 'shelfshift', 0, 'audit', 0, 0, '{}', '[]', 'charge', 0, 0)", b=ident)
            db.run(conn, 'INSERT INTO evaluation_check_history (sim_run_id, benchmark_id, day, world, seq, '
                   "event_kind, lot_ids, observed_counts, outcome) VALUES (:b, :b, 0, 'shelfshift', 0, 'refusal', "
                   "'[]', '{}', '{}')", b=ident)
            before = counts(conn)
        try:
            reset = client.post('/api/demo/reset', json={}, headers=key())
            assert reset.status_code == 200, reset.json()
            assert workspace_of(client)['run_id'] != run['run_id']
            with db.tx() as conn:
                assert counts(conn) == before
                db.run(conn, "UPDATE demo_runs SET retired_at = now() - interval '365 days' WHERE run_id = :r",
                       r=run['run_id'])
                pruned = sandbox.prune(conn)
                assert pruned['sandboxes'] >= 1
                assert db.one(conn, 'SELECT 1 FROM demo_runs WHERE run_id = :r', r=run['run_id']) is None
                assert counts(conn) == before
                assert db.one(conn, 'SELECT status FROM benchmark_runs WHERE benchmark_id = :b',
                              b=ident)['status'] == 'PUBLISHED'
        finally:
            with db.tx() as conn:
                for table in ('benchmark_transfer_audit', 'evaluation_check_history'):
                    db.run(conn, f'DELETE FROM {table} WHERE benchmark_id = :b', b=ident)
