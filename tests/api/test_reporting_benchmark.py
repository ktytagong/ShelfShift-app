"""Published benchmark HTTP reads with real installed public dataset identities (08-05)."""
import copy
from contextlib import ExitStack
from dataclasses import asdict
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import benchmark, db, refdata
from api.app import app
from tests.api.conftest import as_seat, fresh, key, sign_in
from tests.api.test_reporting_api import published_fixture


@pytest.fixture
def public_client(public_template):
    with TestClient(app) as client:
        sign_in(client, 'HQ')
        yield client


@pytest.fixture
def published(public_client):
    run = fresh(public_client.get('/api/session').json()['run_id'])
    ref = refdata.get(run['dataset_version'])
    versions = benchmark.app_versions(run, ref)
    with ExitStack() as stack:
        def create(*, changes=None, status='PUBLISHED'):
            ident = stack.enter_context(published_fixture(run))
            v = {**versions, **(changes or {})}
            summary = json.loads(Path(__file__).with_name('fixtures').joinpath('reports_summary.json').read_text())
            summary.pop('_about')
            summary['policy'] = asdict(ref.policy)
            summary['settings'] = benchmark.settings_for(ref)
            with db.tx() as conn:
                db.run(conn, 'UPDATE benchmark_runs SET status=:status, engine_version=:e, policy_version=:p, '
                       'dataset_version=:v, forecast_runs=CAST(:f AS jsonb), eval_version=:ev, settings_digest=:sd, '
                       'settings=CAST(:s AS jsonb), image_digest=:i WHERE benchmark_id=:b',
                       b=ident, status=status, e=v['engine'], p=v['policy'], v=v['dataset'],
                       f=db.js(v['forecast_runs']), ev=v['evaluator'], sd=v['settings'],
                       s=db.js(summary['settings']), i='private-fixture-digest')
                db.run(conn, 'UPDATE benchmark_summary SET payload=CAST(:p AS jsonb) WHERE benchmark_id=:b',
                       b=ident, p=db.js(summary))
            return ident, summary
        yield create


def read(client, **params):
    response = client.get('/api/reports/benchmark', params=params)
    assert response.status_code == 200, response.json()
    return response.json()


def test_published_matching_result_is_current(public_client, published):
    ident, summary = published()
    result = read(public_client)
    assert result['status'] == 'current'
    saved = result['current']
    assert saved['benchmark_id'] == ident
    assert saved['kind'] == 'SIMULATED'
    assert saved['versions'] == result['app']
    assert saved['differs'] == [] and saved['summary'] == summary
    assert saved['window'] == {'first_day': 0, 'last_day': 6}
    assert saved['breakdown'] is None
    assert result['historical'] is None and result['settings_note'] == []
    assert 'image_digest' not in json.dumps(result)


def test_none_is_honest(public_client):
    result = read(public_client)
    assert result['status'] == 'none'
    assert result['current'] is None and result['historical'] is None
    assert result['settings_note'] == []


def test_provenance_does_not_change_currentness(public_client, published):
    ident, _ = published()
    with db.tx() as conn:
        db.run(conn, 'UPDATE benchmark_runs SET git_commit=:g, image_digest=:i WHERE benchmark_id=:b',
               b=ident, g='presentation-only', i='different-private-fixture')
    result = read(public_client)
    assert result['status'] == 'current'
    assert result['current']['git_commit'] == 'presentation-only'
    assert 'image_digest' not in json.dumps(result)


@pytest.mark.parametrize('field', benchmark.VERSION_KEYS)
def test_each_of_six_differences_is_historical(public_client, published, field):
    replacement = ['different-run'] if field == 'forecast_runs' else 'different-version'
    ident, _ = published(changes={field: replacement})
    result = read(public_client)
    assert result['status'] == 'historical'
    assert result['historical']['benchmark_id'] == ident
    assert result['historical']['differs'] == [field]
    assert result['settings_note'] == [] and result['current'] is None


def test_latest_matching_precedes_newer_nonmatching(public_client, published):
    a, _ = published()
    b, _ = published(changes={'evaluator': 'different-evaluator'})
    with db.tx() as conn:
        db.run(conn, "UPDATE benchmark_runs SET published_at=now()-interval '1 hour' WHERE benchmark_id=:b", b=a)
    assert read(public_client)['current']['benchmark_id'] == a
    c, _ = published()
    assert read(public_client)['current']['benchmark_id'] == c
    with db.tx() as conn:
        db.run(conn, "UPDATE benchmark_runs SET eval_version='older-evaluator' WHERE benchmark_id=ANY(:b)", b=[a, c])
        db.run(conn, "UPDATE benchmark_runs SET published_at=now()+interval '1 hour' WHERE benchmark_id=:b", b=b)
    assert read(public_client)['historical']['benchmark_id'] == b


@pytest.mark.parametrize('status', ['RUNNING', 'VALIDATED', 'FAILED', 'TRIAL'])
def test_unpublished_results_never_visible(public_client, published, status):
    published(status=status)
    result = read(public_client)
    assert result['status'] == 'none' and result['current'] is None and result['historical'] is None


def test_settings_note_follows_effective_workspace_policy(public_client, published):
    _, summary = published()
    original = summary['policy']['waste_price']
    for price in (original, original + 0.1, original):
        session = public_client.get('/api/session').json()
        response = public_client.patch('/api/settings', json={
            'expected_version': session['version'], 'changes': {'waste_price': price}}, headers=key())
        assert response.status_code == 200, response.json()
        result = read(public_client)
        assert result['status'] == 'current'
        assert result['settings_note'] == ([] if price == original else [
            {'field': 'waste_price', 'workspace': price, 'benchmark': original}])


@pytest.fixture
def branch_rows(public_client, published):
    ident, _ = published()
    run = fresh(public_client.get('/api/session').json()['run_id'])
    branches = refdata.get(run['dataset_version']).branches[:3]
    worlds = ['baseline_rule', 'baseline_checked', 'shelfshift', 'do_nothing']
    rows = []
    for index, branch in enumerate(branches):
        for day in range(7):
            base = 100 + day
            delta = [0, 2, -5][index] if day != 6 else 0
            nets = [base, base + (1 if index and day != 6 else 0), base + delta, base - 3]
            for i, world in enumerate(worlds):
                waste = [4, 3, index + 1, 5][i]
                lost = index + i + day
                moved_out = (i + 1) * (index + 1) * (day + 1) if world != 'do_nothing' else 0
                moved_in = (i + 1) * ((index - 1) % len(branches) + 1) * (day + 1) if world != 'do_nothing' else 0
                rows.append((ident, world, day, branch, nets[i], 0, 0, 0, nets[i], 20 + day,
                             waste, waste/2, lost, 20 + day + lost, moved_out, moved_in))
    with db.tx() as conn:
        db.run(conn, 'DELETE FROM benchmark_branch_day WHERE benchmark_id=:b', b=ident)
        db.insert_rows(conn, 'benchmark_branch_day',
            ('benchmark_id', 'scenario', 'day', 'branch', 'revenue', 'transport', 'handling', 'disposal',
             'net', 'sold', 'wasted', 'wasted_kg', 'lost', 'demand', 'moved_out', 'moved_in'), rows)
    return ident, branches


def breakdown(client, ident, chart='money', by='day', **params):
    result = read(client, benchmark_id=ident, breakdown=by, chart=chart, **params)
    selected = result['current'] or result['historical']
    assert selected['benchmark_id'] == ident
    value = selected['breakdown']
    assert value is not None
    assert value['benchmark_id'] == ident and value['chart'] == chart and value['by'] == by
    return value


def test_breakdown_reads_seven_saved_days(public_client, branch_rows):
    ident, branches = branch_rows
    money = breakdown(public_client, ident)
    assert money['total'] == 7 and len(money['rows']) == 7
    assert [r['day'] for r in money['rows']] == list(range(7))
    for day, row in enumerate(money['rows']):
        assert row['store_rule'] == 3 * (100 + day)
        assert row['store_rule_checked'] == 3 * (100 + day) + (2 if day != 6 else 0)
        assert row['shelfshift'] == 3 * (100 + day) + (-3 if day != 6 else 0)
        assert row['total_change'] == (-3 if day != 6 else 0)
    waste = breakdown(public_client, ident, chart='waste')
    for row in waste['rows']:
        assert [row[k] for k in ('store_rule', 'store_rule_checked', 'shelfshift', 'markdowns_only')] == [12, 9, 6, 15]
        assert row['change_vs_markdowns_only'] == -9


def test_branch_order_paging_zero_and_filter(public_client, branch_rows):
    ident, branches = branch_rows
    result = breakdown(public_client, ident, by='branch', page_size=2)
    assert result['page_size'] == 2 and result['total'] == 3
    assert [r['branch'] for r in result['rows']] == [branches[2], branches[1]]
    assert [r['total_change'] for r in result['rows']] == [-30, 12]
    last = breakdown(public_client, ident, by='branch', page_size=2, page=2)['rows']
    assert last[0]['branch'] == branches[0] and last[0]['total_change'] == 0
    assert breakdown(public_client, ident, by='branch', page=99)['rows'] == []
    assert breakdown(public_client, ident, by='branch')['page_size'] == 20
    assert len(breakdown(public_client, ident, by='branch', page_size=100)['rows']) == 3
    assert [r['branch'] for r in breakdown(public_client, ident, by='branch', branch=branches[0])['rows']] == [branches[0]]
    waste = breakdown(public_client, ident, by='branch', chart='waste', branch=branches[0])['rows'][0]
    assert waste['shelfshift'] == 7 and waste['change_vs_markdowns_only'] == -28
    bad = public_client.get('/api/reports/benchmark', params={
        'benchmark_id': ident, 'breakdown': 'branch', 'chart': 'money', 'branch': 'unknown'})
    assert (bad.status_code, bad.json()['error']) == (400, 'bad_request')


def test_branch_money_projection_includes_saved_waste_moves_and_unmet_demand(public_client, branch_rows):
    ident, branches = branch_rows
    result = breakdown(public_client, ident, by='branch')
    for row in result['rows']:
        index = branches.index(row['branch'])
        assert row['waste_units'] == {
            'store_rule': 28, 'store_rule_checked': 21, 'shelfshift': 7 * (index + 1), 'markdowns_only': 35}
        assert row['moved_out'] == 84 * (index + 1)
        assert row['moved_in'] == 84 * ((index - 1) % len(branches) + 1)
        assert row['demand_not_met'] == {'store_rule': 7 * index + 21, 'shelfshift': 7 * (index + 2) + 21}
        # The operational figures supplement the existing net-money columns.
        assert row['store_rule'] == 721
        assert row['shelfshift'] == 721 + [0, 12, -30][index]

    extra = {'waste_units', 'moved_out', 'moved_in', 'demand_not_met'}
    for chart, by in [('money', 'day'), ('waste', 'branch'), ('waste', 'day')]:
        assert all(extra.isdisjoint(row) for row in breakdown(public_client, ident, chart=chart, by=by)['rows'])


def test_branch_money_projection_distinguishes_recorded_zero_from_missing_rows(public_client, branch_rows):
    ident, branches = branch_rows
    with db.tx() as conn:
        db.run(conn, 'UPDATE benchmark_branch_day SET wasted=0, lost=0, moved_out=0, moved_in=0 '
               'WHERE benchmark_id=:b AND branch=:branch', b=ident, branch=branches[0])
    row = breakdown(public_client, ident, by='branch', branch=branches[0])['rows'][0]
    assert row['waste_units'] == dict.fromkeys(('store_rule', 'store_rule_checked', 'shelfshift', 'markdowns_only'), 0)
    assert row['moved_out'] == row['moved_in'] == 0
    assert row['demand_not_met'] == {'store_rule': 0, 'shelfshift': 0}

    # Incomplete recorded input must never be replaced by a calculated or typed zero.
    with db.tx() as conn:
        db.run(conn, "DELETE FROM benchmark_branch_day WHERE benchmark_id=:b AND branch=:branch "
               "AND scenario IN ('baseline_checked', 'shelfshift')", b=ident, branch=branches[0])
    row = breakdown(public_client, ident, by='branch', branch=branches[0])['rows'][0]
    assert row['waste_units'] == {'store_rule': 0, 'store_rule_checked': None,
                                 'shelfshift': None, 'markdowns_only': 0}
    assert row['moved_out'] is None and row['moved_in'] is None
    assert row['demand_not_met'] == {'store_rule': 0, 'shelfshift': None}


def test_manager_store_is_pinned_without_workspace_access(public_client, branch_rows):
    ident, branches = branch_rows
    before = public_client.get('/api/reports/workspace').json()
    as_seat(public_client, branches[0])
    own_run = public_client.get('/api/session').json()['run_id']
    result = breakdown(public_client, ident, by='branch', branch=branches[2], page_size=2)
    assert [r['branch'] for r in result['rows']] == [branches[0], branches[2]]
    assert [r['pinned'] for r in result['rows']] == [True, False]
    second = breakdown(public_client, ident, by='branch', page=2, page_size=2)
    assert [r['branch'] for r in second['rows']] == [branches[1]]
    workspace = public_client.get('/api/reports/workspace', params={'branch': branches[2], 'run_id': 'foreign'}).json()
    assert workspace['scope']['branch'] == branches[0]
    assert public_client.get('/api/session').json()['run_id'] == own_run
    assert before['scope']['role'] == 'hq'


def test_ties_use_branch_code(public_client, branch_rows):
    ident, branches = branch_rows
    with db.tx() as conn:
        db.run(conn, "UPDATE benchmark_branch_day SET net=100+day WHERE benchmark_id=:b", b=ident)
    assert [r['branch'] for r in breakdown(public_client, ident, by='branch')['rows']] == sorted(branches)


def test_identity_survives_new_publication_and_currentness_change(public_client, branch_rows, published, monkeypatch):
    a, _ = branch_rows
    initial = read(public_client)['current']
    assert initial['benchmark_id'] == a
    day_a = breakdown(public_client, a)
    branch_a = breakdown(public_client, a, by='branch', page_size=1, page=2)
    b, _ = published()
    with db.tx() as conn:
        db.run(conn, "UPDATE benchmark_runs SET published_at=now()+interval '1 hour' WHERE benchmark_id=:b", b=b)
    assert read(public_client)['current']['benchmark_id'] == b
    assert breakdown(public_client, a) == day_a
    assert breakdown(public_client, a, by='branch', page_size=1, page=2) == branch_a
    monkeypatch.setattr(benchmark, 'EVAL_VERSION', 'eval-next')
    assert read(public_client)['status'] == 'historical'
    selected = read(public_client, benchmark_id=a)
    assert selected['historical']['benchmark_id'] == a and selected['historical']['differs'] == ['evaluator']
    assert breakdown(public_client, a) == day_a
    monkeypatch.setattr(benchmark, 'EVAL_VERSION', 'eval-1')
    assert read(public_client, benchmark_id=a)['status'] == 'current'


@pytest.mark.parametrize('params', [
    {'breakdown': 'day'}, {'chart': 'money'}, {'breakdown': 'unknown', 'chart': 'money'},
    {'breakdown': 'day', 'chart': 'unknown'}, {'breakdown': 'day', 'chart': 'money'},
    {'page': '0'}, {'page': '-1'}, {'page': '1.5'}, {'page_size': '0'}, {'page_size': '101'},
    {'page_size': 'x'}, {'benchmark_id': 'bm-nope'}, {'benchmark_id': ''},
    {'benchmark_id': 'bm-' + 'a'*13}, {'benchmark_id': 'bm-' + 'G'*12},
    [('breakdown', 'day'), ('breakdown', 'branch'), ('chart', 'money')]])
def test_bad_breakdown_queries_are_400(public_client, params):
    response = public_client.get('/api/reports/benchmark', params=params)
    assert (response.status_code, response.json().get('error')) == (400, 'bad_request')


@pytest.mark.parametrize('status', ['RUNNING', 'VALIDATED', 'FAILED', 'TRIAL', 'unknown'])
def test_explicit_unpublished_or_unknown_id_is_404(public_client, published, status):
    published()  # a readable latest result must never substitute for the requested ID
    ident = 'bm-000000000000' if status == 'unknown' else published(status=status)[0]
    for extra in ({}, {'breakdown': 'day', 'chart': 'money'}, {'breakdown': 'branch', 'chart': 'waste'}):
        response = public_client.get('/api/reports/benchmark', params={'benchmark_id': ident, **extra})
        assert response.status_code == 404
        assert response.json() == {'error': 'not_found', 'message': f'No benchmark {ident}'}


def test_unsigned_benchmark_read_is_locked():
    with TestClient(app) as client:
        response = client.get('/api/reports/benchmark')
        assert (response.status_code, response.json()['error']) == (401, 'locked')


def test_forecast_run_order_matters(public_client, published):
    current = read(public_client)['app']
    ident, _ = published(changes={'forecast_runs': list(reversed(current['forecast_runs']))})
    result = read(public_client)
    assert result['status'] == 'historical'
    assert result['historical']['benchmark_id'] == ident
    assert result['historical']['differs'] == ['forecast_runs']


def test_foreign_workspace_cannot_supply_policy_note(public_client, published):
    published()
    own_run = public_client.get('/api/session').json()['run_id']
    with TestClient(app) as other:
        sign_in(other, 'HQ')
        other_session = other.get('/api/session').json()
        assert other_session['run_id'] != own_run
        response = other.patch('/api/settings', headers=key(), json={
            'expected_version': other_session['version'], 'changes': {'waste_price': 0.2}})
        assert response.status_code == 200, response.json()
        assert read(other)['settings_note'] == [{'field': 'waste_price', 'workspace': 0.2, 'benchmark': 0.0}]
        result = read(public_client, run_id=other_session['run_id'])
        assert result['settings_note'] == []
        assert public_client.get('/api/session').json()['run_id'] == own_run


def refuse_live_settings(public_client, monkeypatch, change):
    """Live dataset settings an evaluation refuses (D-28, D-33/D-36), on the cached reference only."""
    run = fresh(public_client.get('/api/session').json()['run_id'])
    ref = refdata.get(run['dataset_version'])
    settings = copy.deepcopy(ref.settings)
    if change == 'ordering':
        settings['ordering'] = {'enabled_stores': [ref.branches[0]]}
    elif change == 'price_response':
        settings['truth']['price_response'] = {'m20': 1.38, 'm30': 1.52}
    else:
        del settings['truth']['price_response']
    monkeypatch.setattr(ref, 'settings', settings)
    with pytest.raises(ValueError):
        benchmark.settings_for(ref)


REFUSALS = [('ordering', 'ordering'), ('price_response', 'price_response'),
            ('missing_price_response', 'price_response')]


@pytest.mark.parametrize('change, reason', REFUSALS)
def test_refused_live_settings_still_answer_without_a_current_result(public_client, branch_rows, monkeypatch,
                                                                     change, reason):
    ident, _ = branch_rows
    refuse_live_settings(public_client, monkeypatch, change)
    result = read(public_client)
    assert result['status'] == 'historical' and result['current'] is None
    assert result['settings_refused'] == reason
    assert result['app']['settings'] is None and set(result['app']) == set(benchmark.VERSION_KEYS)
    assert result['historical']['benchmark_id'] == ident and result['historical']['differs'] == ['settings']
    assert result['settings_note'] == []
    # The saved result stays readable by its identity, breakdowns included.
    assert read(public_client, benchmark_id=ident)['historical']['benchmark_id'] == ident
    assert breakdown(public_client, ident)['total'] == 7
    monkeypatch.undo()
    restored = read(public_client)
    assert restored['status'] == 'current' and restored['settings_refused'] is None
    assert restored['app']['settings'] == benchmark.settings_digest(refdata.get(restored['app']['dataset']))


@pytest.mark.parametrize('change, reason', REFUSALS)
def test_refused_live_settings_with_nothing_published_is_none(public_client, monkeypatch, change, reason):
    refuse_live_settings(public_client, monkeypatch, change)
    result = read(public_client)
    assert result['status'] == 'none' and result['current'] is None and result['historical'] is None
    assert result['settings_refused'] == reason and result['app']['settings'] is None


def test_branch_money_columns_are_null_when_a_scenario_row_is_missing(public_client, branch_rows):
    ident, branches = branch_rows
    with db.tx() as conn:
        db.run(conn, "DELETE FROM benchmark_branch_day WHERE benchmark_id=:b AND branch=:branch "
               "AND scenario IN ('baseline_checked', 'shelfshift')", b=ident, branch=branches[0])
    row = breakdown(public_client, ident, by='branch', branch=branches[0])['rows'][0]
    assert row['store_rule'] == 721 and row['markdowns_only'] == 700
    assert row['store_rule_checked'] is None and row['shelfshift'] is None
    for derived in ('checks_change', 'decisions_change', 'total_change', 'change_vs_markdowns_only'):
        assert row[derived] is None, derived
    # A row with no recorded change never sorts as a zero or as -721: known changes come first.
    rows = breakdown(public_client, ident, by='branch')['rows']
    assert [r['branch'] for r in rows] == [branches[2], branches[1], branches[0]]
    waste = breakdown(public_client, ident, chart='waste', by='branch', branch=branches[0])['rows'][0]
    assert waste['store_rule'] == 28 and waste['shelfshift'] is None and waste['change_vs_markdowns_only'] is None


def test_refused_live_settings_historical_is_the_latest_published(public_client, published, monkeypatch):
    a, _ = published()
    b, _ = published(changes={'evaluator': 'different-evaluator'})
    with db.tx() as conn:
        db.run(conn, "UPDATE benchmark_runs SET published_at=now()+interval '1 hour' WHERE benchmark_id=:b", b=b)
    assert read(public_client)['current']['benchmark_id'] == a
    refuse_live_settings(public_client, monkeypatch, 'ordering')
    result = read(public_client)
    assert result['status'] == 'historical' and result['historical']['benchmark_id'] == b
    assert result['historical']['differs'] == ['evaluator', 'settings']
