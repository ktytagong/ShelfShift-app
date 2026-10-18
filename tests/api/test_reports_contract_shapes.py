"""DB-free 08-03/06 boundary checks. Later outcome/summary projections belong to 08-18/21."""
import json
from pathlib import Path

from api import reporting

FIXTURES = Path(__file__).resolve().parents[1] / 'web' / 'fixtures'


def key_tree(value):
    """Object keys and nesting only; fixture figures never become a numeric oracle."""
    return {k: key_tree(v) for k, v in value.items()} if isinstance(value, dict) else None


def projection(body, *keys):
    return {k: body[k] for k in keys}


def fixture(name):
    return json.loads((FIXTURES / f'{name}.json').read_text())


def test_real_workspace_function_matches_contract_and_web_projection(monkeypatch):
    # The integrated server's one statement snapshot (08-18): the five activity groups plus the committed plans and
    # recorded movements that `expected` and `actual` read from the same row (08-04, D-04).
    monkeypatch.setattr(reporting, 'scope_store', lambda *args: None)
    monkeypatch.setattr(reporting.refdata, 'get', lambda _: None)
    monkeypatch.setattr(reporting.db, 'one', lambda *args, **kwargs: {
        'open': 3, 'pending': 1, 'committed': 2, 'cancelled': 0, 'checks': 4,
        'past_due': 1, 'addressed': 0, 'dispatched': 1, 'received': 0, 'owners': [],
        'plans': [], 'movements': [], 'discrepancies': []})
    answer = reporting.workspace(None, {'dataset_version': 'test', 'run_id': 'test'}, {'role': 'hq'})
    expected = {'scope': {'branch': None, 'role': None}, 'activity': {
        'open': {'count': None, 'need_action': None},
        'pending_approval': {'count': None, 'waiting_for_you': None},
        'unanswered_check': {'count': None, 'past_due': None, 'addressed_to_you': None},
        'committed': {'count': None, 'dispatched': None, 'received': None}, 'cancelled': {'count': None}},
        'expected': dict.fromkeys(('kind', 'plans', 'page', 'page_size', 'total', 'combined')),
        'actual': dict.fromkeys(('kind', 'dispatched_units', 'dispatched_transfers', 'received_units',
                                 'discrepancies_open', 'sales_after_receipt', 'waste_after_receipt'))}
    assert key_tree(answer) == expected
    assert key_tree(projection(fixture('reports_workspace'), *expected)) == expected


def test_real_none_function_matches_contract_and_web_projection(monkeypatch):
    # The integrated read names the live app's six versions (`app`) and any refusal of its settings (gap 5); the
    # versions themselves are the evaluator's, checked against a real dataset in test_benchmark_published_shape.py.
    monkeypatch.setattr(reporting, 'scope_store', lambda *args: None)
    monkeypatch.setattr(reporting.refdata, 'get', lambda _: None)
    monkeypatch.setattr(reporting, 'live_versions', lambda run, ref: (dict.fromkeys(fixture('reports_benchmark_none')
                                                                                    ['app'], 'x'), None))
    monkeypatch.setattr(reporting.db, 'one', lambda *args, **kwargs: None)
    answer = reporting.benchmark(None, {'dataset_version': 'test'}, {'role': 'hq'})
    expected = {'status': 'none', 'current': None, 'historical': None}
    web = fixture('reports_benchmark_none')
    assert projection(answer, *expected) == expected == projection(web, *expected)
    assert set(web) - {'_about'} <= set(answer)
    assert key_tree(answer['app']) == key_tree(web['app'])
    assert answer['settings_note'] == web['settings_note'] == [] and answer['settings_refused'] is None


# --- 08-21: a saved result read through every Reports read, against independent oracles ---------------------------
#
# The oracle below reads the stored rows, the outcome ledger, the per-trip audit, the count history, the decisions
# table and the activity log in SQL, and the shipped backtest report, never `api.metrics` or `api.reporting`. Shape
# equality (key_tree) stays a supplement to it. Shared with tests/web/test_reports_integrated.py.

import pytest  # noqa: E402

from api import db, refdata  # noqa: E402
from tests.api.benchmark_fixtures import (SCENARIO_OF, eligible_benchmark_dv,  # noqa: E402, F401
                                          eligible_benchmark_workspace, ledger_oracle)
from tests.api.test_benchmark_cli import bind_session, job  # noqa: E402, F401  (job is a fixture)

SCENARIOS = tuple(SCENARIO_OF.values())
WORLD = {v: k for k, v in SCENARIO_OF.items()}
CHART_FIELD = {'money': 'net', 'waste': 'wasted'}
CENTS = 0.005


def saved_oracle(conn, ident, ref):
    """TEST ONLY: what a saved result's screens must say, derived from its records (see the block comment)."""
    stored = db.rows(conn, 'SELECT * FROM benchmark_branch_day WHERE benchmark_id = :b', b=ident)

    def total(field, scenario, **match):
        return sum(r[field] for r in stored if r['scenario'] == WORLD[scenario]
                   and all(r[k] == v for k, v in match.items()))

    net = {s: total('net', s) for s in SCENARIOS}
    kg = {s: 0.0 for s in SCENARIOS}
    head = {}
    rows = db.rows(conn, 'SELECT world, sku, sold, demand, wasted FROM outcomes WHERE run_id = :r', r=ident)
    for s in SCENARIOS:
        mine = [r for r in rows if r['world'] == WORLD[s]]
        kg[s] = sum(r['wasted'] * ref.products[r['sku']].kg_per_unit for r in mine)
        demand = sum(r['demand'] for r in mine)
        head[s] = {'units_wasted': sum(r['wasted'] for r in mine), 'kg_wasted': kg[s],
                   'sold': sum(r['sold'] for r in mine),
                   'full_price_coverage': sum(min(r['sold'], r['demand']) for r in mine) / demand if demand else None}
    for s in SCENARIOS:
        head[s]['incremental_units_sold_vs_markdowns_only'] = head[s]['sold'] - head['markdowns_only']['sold']
    history = db.rows(conn, "SELECT world, event_kind, outcome FROM evaluation_check_history WHERE sim_run_id = :r",
                      r=ident)
    checks = {}
    for s in ('store_rule_checked', 'shelfshift'):
        seen = [h['outcome'] for h in history if h['world'] == WORLD[s] and h['event_kind'] == 'observation']
        pairs = [(o['recorded_units'], o['observed_units']) for o in seen]
        checks[s] = {'requests': sum(h['world'] == WORLD[s] and h['event_kind'] == 'request' for h in history),
                     'lots_counted': len(seen), 'corrected_up': sum(b > a for a, b in pairs),
                     'corrected_down': sum(b < a for a, b in pairs), 'nothing_to_correct': sum(a == b for a, b in pairs)}
    log = db.rows(conn, 'SELECT type, case_id, payload FROM activity_log WHERE run_id = :r', r=ident)
    approves = {d['case_id'] for d in db.rows(conn, "SELECT case_id FROM decisions WHERE run_id = :r AND role = 'source' "
                                                    "AND decision = 'approve'", r=ident)}
    left = {}
    for d in db.rows(conn, 'SELECT stats FROM benchmark_days WHERE benchmark_id = :b', b=ident):
        for reason, ids in d['stats'].get('left_open', {}).items():
            left[reason] = left.get(reason, 0) + len(ids)
    decisions = {'opened': len({r['case_id'] for r in log if r['type'] == 'opened'}),
                 # D-26: a Keep committed through the fast path is approved as the app path would approve it
                 'approved': len(approves | {r['case_id'] for r in log if r['type'] == 'committed'}),
                 'rejected': len({r['case_id'] for r in log if r['type'] == 'rejected'}),
                 'waiting': len({r['case_id'] for r in log if r['type'] == 'waiting'}), 'left_open': left}
    report = ref.manifest.get('forecast', {}).get('backtest_report')
    report = report if isinstance(report, dict) else json.loads(Path(report or refdata.REF / 'reports' /
                                                                     'public-backtest.json').read_text())
    return {
        'ident': ident, 'stored': stored, 'net': net, 'ledger': ledger_oracle(conn, ident),
        'steps': [net['store_rule'], net['store_rule_checked'] - net['store_rule'],
                  net['shelfshift'] - net['store_rule_checked'], net['shelfshift']],
        'waste_units': {s: total('wasted', s) for s in SCENARIOS}, 'waste_kg': kg, 'headline': head,
        'checks': checks, 'decisions': decisions, 'forecast': report,
        'by': {(chart, 'day'): {d: {s: total(f, s, day=d) for s in SCENARIOS} for d in range(7)}
               for chart, f in CHART_FIELD.items()} | {
              (chart, 'branch'): {b: {s: total(f, s, branch=b) for s in SCENARIOS} for b in ref.branches}
              for chart, f in CHART_FIELD.items()},
    }


def assert_summary_matches(summary, oracle):
    """The saved summary's money, waste, headline, checks, trips, decision and forecast blocks equal the oracle."""
    for s in SCENARIOS:
        assert summary['money']['by_scenario'][s] == pytest.approx(oracle['net'][s], abs=1e-6), s
        assert abs(oracle['net'][s] - oracle['ledger']['net'][s]) <= CENTS * len(oracle['stored']) / 4 + 1e-9, s
        assert summary['waste']['totals'][s]['units'] == pytest.approx(oracle['waste_units'][s], abs=1e-6), s
        assert summary['waste']['totals'][s]['kg'] == pytest.approx(oracle['waste_kg'][s], abs=1e-6), s
        assert summary['waste']['change_vs_markdowns_only'][s]['units'] == pytest.approx(
            oracle['waste_units'][s] - oracle['waste_units']['markdowns_only'], abs=1e-6), s
        h, want = summary['headline'][s], oracle['headline'][s]
        assert h['net'] == pytest.approx(oracle['ledger']['net'][s], abs=CENTS), s
        for field in ('units_wasted', 'kg_wasted', 'incremental_units_sold_vs_markdowns_only'):
            assert h[field] == pytest.approx(want[field], abs=1e-6), (s, field)
        assert (h['full_price_coverage'] is None) is (want['full_price_coverage'] is None), s
        if want['full_price_coverage'] is not None:
            assert h['full_price_coverage'] == pytest.approx(want['full_price_coverage'], abs=1e-9), s
        for field, value in h.items():  # a value the run can't have is null with its reason, never estimated
            if value is None:
                assert h['reasons'].get(field), (s, field)
    assert [step['value'] for step in summary['money']['steps']] == pytest.approx(oracle['steps'], abs=1e-6)
    assert [step['key'] for step in summary['money']['steps']] == [
        'store_rule', 'checks_change', 'decisions_change', 'shelfshift']
    assert summary['trips'] == oracle['ledger']['trips']
    assert summary['checks']['store_rule'] is None and summary['checks']['markdowns_only'] is None
    for s, want in oracle['checks'].items():
        assert summary['checks'][s] == want, s
    dq = summary['decision_quality']
    assert {k: dq[k] for k in oracle['decisions']} == oracle['decisions']
    assert dq['opened'] == dq['approved'] + dq['rejected'] + dq['waiting'] + sum(dq['left_open'].values())
    assert oracle['forecast'] is not None
    for field, value in summary['forecast_quality'].items():
        assert value == oracle['forecast'][field], field


def assert_breakdown(body, ident, chart, by, oracle, *, rows=None, pinned=None, branches=None):
    """One breakdown read: the saved ID echoed at both levels, the complete domain, and the oracle's amounts."""
    run = body[body['status']]
    assert run['benchmark_id'] == ident and run['kind'] == 'SIMULATED'
    data = run['breakdown']
    assert (data['benchmark_id'], data['chart'], data['by']) == (ident, chart, by)
    want = oracle['by'][chart, by]
    key = 'day' if by == 'day' else 'branch'
    got = {r[key]: r for r in data['rows']}
    expected_keys = list(range(7)) if by == 'day' else sorted(branches if branches is not None else want)
    assert sorted(got) == expected_keys and data['total'] == len(expected_keys)
    if by == 'day':
        assert [r['day'] for r in data['rows']] == list(range(7))
    for k, r in got.items():
        for s in SCENARIOS:
            assert r[s] == pytest.approx(want[k][s], abs=1e-6), (chart, by, k, s)
    if pinned is not None:
        assert data['rows'][0]['branch'] == pinned and data['rows'][0]['pinned'] is True
        assert all(r['pinned'] is False for r in data['rows'][1:])
    return data


def every_breakdown(viewer, ident, **params):
    found = {}
    for chart in CHART_FIELD:
        for by in ('day', 'branch'):
            body = viewer.get('/api/reports/benchmark', params=dict(
                benchmark_id=ident, chart=chart, breakdown=by, page_size=100, **params))
            assert body.status_code == 200, body.json()
            found[chart, by] = body.json()
    return found


def test_a_saved_result_reads_with_one_identity_a_complete_domain_and_independent_amounts(
        job, template, eligible_benchmark_dv, eligible_benchmark_workspace, monkeypatch):
    """08-21 behaviours on the integrated server: (1) the current saved result's money, waste, check, trip,
    headline, decision and forecast figures equal an independent oracle, through every breakdown, for a manager
    (own store pinned, every branch's saved rows) and HQ (network, or one named store); (2) after a second result B
    publishes, every read by A's ID returns A's identity and A's amounts, never B's; (4) the build that serves the
    page (its commit and image digest) is not one of the six versions that decide current."""
    from fastapi.testclient import TestClient
    from api import benchmark
    from api.app import app
    from tests.api.conftest import sign_in

    dv = eligible_benchmark_dv
    ref = refdata.get(dv)
    stores = sorted(ref.branches)
    workspace = eligible_benchmark_workspace()
    code, report = job('benchmark', 'start', '--dataset', dv)
    assert code == 0 and report['status'] == 'PUBLISHED', report
    a = report['benchmark_id']
    with db.tx() as conn:
        oracle = saved_oracle(conn, a, ref)

    manager = TestClient(app)
    sign_in(manager)
    bind_session(manager, workspace['run_id'], role='manager', branch=stores[0])
    hq = TestClient(app)
    sign_in(hq, 'HQ')
    bind_session(hq, workspace['run_id'])

    for viewer, pinned in ((manager, stores[0]), (hq, None)):
        body = viewer.get('/api/reports/benchmark').json()
        assert body['status'] == 'current' and body['current']['benchmark_id'] == a
        assert body['current']['window'] == {'first_day': 0, 'last_day': 6} and body['current']['breakdown'] is None
        assert_summary_matches(body['current']['summary'], oracle)
        web = fixture('reports_benchmark_current')  # supplemental: the read's shape is the web fixture's
        assert set(web) - {'_about'} <= set(body)
        assert key_tree({k: v for k, v in body['current'].items() if k != 'summary'}) == key_tree(
            {k: v for k, v in web['current'].items() if k != 'summary'})
        for (chart, by), read in every_breakdown(viewer, a).items():
            assert_breakdown(read, a, chart, by, oracle, pinned=pinned if by == 'branch' else None)
    # HQ names one store: the by-branch rows narrow to it; the by-day rows and the summary stay network-wide
    for store in stores:
        for (chart, by), read in every_breakdown(hq, a, branch=store).items():
            assert_breakdown(read, a, chart, by, oracle, branches=[store] if by == 'branch' else None)
        assert hq.get('/api/reports/benchmark', params={'branch': store}).json()['current']['summary'] == \
            hq.get('/api/reports/benchmark').json()['current']['summary']
    # a manager's branch parameter is ignored (D-15): the same pinned, every-branch rows
    other = manager.get('/api/reports/benchmark', params=dict(benchmark_id=a, chart='money', breakdown='branch',
                                                               page_size=100, branch=stores[-1])).json()
    assert_breakdown(other, a, 'money', 'branch', oracle, pinned=stores[0])

    # (4) a presentation-only build: another commit and image digest serve the page; the six versions are the same
    versions = benchmark.app_versions(workspace, ref)
    monkeypatch.setenv('GIT_COMMIT', 'test-only-presentation-only-commit-08-21')
    monkeypatch.setenv('IMAGE_DIGEST', 'test-only-presentation-only-image-08-21')
    assert benchmark.app_versions(workspace, ref) == versions == report['versions']
    assert 'git_commit' not in versions and 'image_digest' not in versions
    body = hq.get('/api/reports/benchmark').json()
    assert body['status'] == 'current' and body['current']['differs'] == [] and body['app'] == versions
    assert body['current']['git_commit'] != 'test-only-presentation-only-commit-08-21'
    monkeypatch.setenv('GIT_COMMIT', 'test-only-commit-08-15')  # the job fixture's values for the second run
    monkeypatch.setenv('IMAGE_DIGEST', 'test-only-image-digest-08-15')

    # (2) a second publication B; a TEST ONLY marker on B's stored ShelfShift rows makes A and B differ in value
    code, second = job('benchmark', 'start', '--dataset', dv)
    assert code == 0 and second['status'] == 'PUBLISHED' and second['benchmark_id'] != a, second
    b = second['benchmark_id']
    with db.tx() as conn:
        db.run(conn, "UPDATE benchmark_branch_day SET revenue = revenue + 1000, net = net + 1000, wasted = wasted + 7 "
                     "WHERE benchmark_id = :b AND scenario = 'shelfshift'", b=b)
        marked = saved_oracle(conn, b, ref)
        assert marked['net']['shelfshift'] != oracle['net']['shelfshift']
    for viewer, pinned in ((manager, stores[0]), (hq, None)):
        assert viewer.get('/api/reports/benchmark').json()['current']['benchmark_id'] == b
        pinned_a = viewer.get('/api/reports/benchmark', params={'benchmark_id': a}).json()
        assert pinned_a['current']['benchmark_id'] == a
        assert_summary_matches(pinned_a['current']['summary'], oracle)
        for (chart, by), read in every_breakdown(viewer, a).items():
            assert_breakdown(read, a, chart, by, oracle, pinned=pinned if by == 'branch' else None)
        for (chart, by), read in every_breakdown(viewer, b).items():
            assert_breakdown(read, b, chart, by, marked, pinned=pinned if by == 'branch' else None)
