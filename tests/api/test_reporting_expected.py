"""Committed projections and recorded movements, through Reports on fresh workspaces (08 D-04).

The selection contract fixture bridges Phase 7 until 08-18 integrates its real helper. It never
changes the engine or seed fixtures. Amounts in boundary fixtures are independent stored cents.
"""
from copy import deepcopy
from decimal import Decimal

import pytest
import sqlalchemy as sa

from api import cases, db, refdata, reporting
from tests.api.conftest import as_seat, by_tag, key, owned_send, seat
from tests.api.test_reporting_api import workspace_of
from tests.api.verify_helpers import ack, shown


FIGURES = ('net_change', 'waste_change_units', 'waste_change_kg', 'transport_and_handling')


def read(client, **params):
    response = client.get('/api/reports/workspace', params=params)
    assert response.status_code == 200, response.json()
    return response.json()


def committed_story(client):
    run = workspace_of(client)
    case = by_tag(run, cases.STORY_TAG)
    with db.tx() as conn:
        # The template has warm-up commitments. Isolate this test's projection domain on its own clone.
        db.run(conn, "UPDATE cases SET state = 'CLOSED' WHERE run_id = :r AND case_id <> :c "
               "AND state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')", r=run['run_id'], c=case['case_id'])
        rec = cases.active_rec(conn, case)
    plan = cases.parse_plan(rec['result']['recommended'])
    assert plan.kind == 'TRANSFER'
    owned_send(run, seat(case['source_branch']), case['case_id'], rec['rec_version'])
    # An approve or accept names the plan in force and acknowledges its Advisory facts (07 review C, D-26).
    named = {'plan_key': shown(run, case['case_id']), 'acknowledged': ack(run, case['case_id'])}
    for branch, decision in ((plan.dest, 'accept'), (case['source_branch'], 'approve')):
        as_seat(client, branch)
        response = client.post(f"/api/cases/{case['case_id']}/decision",
                               json={'rec_version': rec['rec_version'], 'decision': decision, **named},
                               headers=key())
        assert response.status_code == 200, response.json()
    return run, case, rec, plan


def test_committed_projection_uses_stored_base_comparison(client):
    run, case, rec, plan = committed_story(client)
    body = read(client)
    assert 'expected' in body
    expected = body['expected']
    [row] = expected['plans']
    columns = {c['column']: c for c in rec['simple_rule']['columns']}
    recommended, keep = columns['recommended'], columns['keep']
    assert expected['kind'] == 'SIMULATED'
    assert row['case_id'] == case['case_id'] and row['key'] == case['key']
    assert row['plan_key'] == plan.key and row['plan_kind'] == 'TRANSFER'
    assert row['plan_label'] == recommended['label']
    assert row['dest_branch'] == plan.dest and row['source_branch'] == case['source_branch']
    assert row['computed'] is True
    assert row['net_change'] == recommended['delta_vs_keep']
    assert row['waste_change_units'] == pytest.approx(recommended['wasted_units'] - keep['wasted_units'])
    assert row['waste_change_kg'] == pytest.approx(recommended['wasted_kg'] - keep['wasted_kg'])
    assert row['transport_and_handling'] == recommended['transport']['base']
    assert row['horizon'] == {'first_day': rec['engine_input']['today'],
                             'last_day': max(f['day'] for f in rec['engine_input']['forecasts'])}
    assert expected['combined'] == {'additive': True, 'plans': 1, **{f: row[f] for f in FIGURES}}
    assert (expected['page'], expected['page_size'], expected['total']) == (1, 20, 1)


@pytest.mark.parametrize('bad', ['page_size=101', 'page_size=0', 'page=0', 'page=x',
                                  'page_size=1&page_size=2', 'page_size=1.5'])
def test_projection_paging_rejects_invalid_parameters(client, bad):
    response = client.get('/api/reports/workspace?' + bad)
    assert (response.status_code, response.json().get('error')) == (400, 'bad_request')


@pytest.mark.parametrize('comparison', [None, {'status': 'missing_data', 'reason': 'forecast day missing', 'columns': []},
                                       {'status': 'ok', 'columns': []}])
def test_missing_comparison_has_no_figures_or_total(client, comparison):
    run, case, rec, _ = committed_story(client)
    with db.tx() as conn:
        db.run(conn, 'UPDATE recommendations SET simple_rule = CAST(:x AS jsonb) '
               'WHERE run_id = :r AND case_id = :c AND rec_version = :v', x=None if comparison is None else db.js(comparison),
               r=run['run_id'], c=case['case_id'], v=rec['rec_version'])
    expected = read(client)['expected']
    [row] = expected['plans']
    assert row['computed'] is False and all(row[f] is None for f in FIGURES)
    assert row['reason']
    assert expected['combined'] == {'additive': False, 'plans': 1, 'reason': 'not_computed'}


def selection_fixture(monkeypatch, case, rec, plan_key, *, version=None, round_=None):
    """Contract-shaped selected plan and immutable commitment; no Phase 7 schema/service edits."""
    original = reporting._plan_in_force
    selected = {'rec_version': version or rec['rec_version'], 'plan_key': plan_key,
                'label': 'Manager selection', 'fact_checks': [], 'by': case['source_branch'],
                'at': '2026-10-18T00:00:00Z'}
    if round_ is not None:
        selected['round'] = round_
    def resolve(committed, recommendation):
        if committed['case_id'] == case['case_id']:
            committed = {**committed, 'selected': selected}
        return original(committed, recommendation)
    monkeypatch.setattr(reporting, '_plan_in_force', resolve)
    with db.tx() as conn:
        db.run(conn, "UPDATE activity_log SET payload = jsonb_set(payload, '{plan}', CAST(:p AS jsonb)) "
               "WHERE run_id = :r AND case_id = :c AND type = 'committed'", p=db.js(plan_key),
               r=case['run_id'], c=case['case_id'])


def test_selected_keep_excluded_even_when_engine_recommends_transfer(client, monkeypatch):
    _, case, rec, _ = committed_story(client)
    selection_fixture(monkeypatch, case, rec, 'keep')
    body = read(client)
    assert body['activity']['committed']['count'] == 1
    assert body['expected']['plans'] == [] and body['expected']['combined'] is None


@pytest.mark.parametrize('stale', ['version', 'round'])
def test_unbound_selection_never_borrows_recommendation(client, monkeypatch, stale):
    _, case, rec, plan = committed_story(client)
    selection_fixture(monkeypatch, case, rec, plan.key,
                      version='older-version' if stale == 'version' else None,
                      round_=-1 if stale == 'round' else None)
    [row] = read(client)['expected']['plans']
    assert row['computed'] is False and row['reason'] == 'unbound_selection'
    assert all(row[f] is None for f in FIGURES)


def stored_comparison(plan_key, net='12.37', transport='3.19'):
    """Independent persisted-output fixture, deliberately unrelated to the live engine's pick."""
    return {'status': 'ok', 'reason': None, 'columns': [
        {'column': 'recommended', 'key': plan_key, 'label': 'Fixture plan', 'delta_vs_keep': float(net),
         'wasted_units': 7.12, 'wasted_kg': 2.14, 'transport': {'base': float(transport)}},
        {'column': 'keep', 'key': 'keep', 'wasted_units': 10.23, 'wasted_kg': 3.15}]}


@pytest.mark.parametrize('available', [False, True])
def test_alternate_destination_uses_only_its_own_stored_column(client, monkeypatch, available):
    run, case, rec, plan = committed_story(client)
    other = next(b for b in refdata.get(run['dataset_version']).branches
                 if b not in (case['source_branch'], plan.dest))
    alternative = f'transfer:{other}:{plan.run_code}:{plan.qty}:hold'
    # Plan.key prefixes transfers with tx, independent of the parser's permissive prefix.
    alternative = cases.parse_plan(alternative).key
    selection_fixture(monkeypatch, case, rec, alternative)
    if available:
        comparison = deepcopy(rec['simple_rule'])
        alternate_column = stored_comparison(alternative)['columns'][0]
        alternate_column['column'] = 'simple_rule'
        comparison['columns'].append(alternate_column)
        comparison['columns'] = [c if c['column'] != 'keep' else stored_comparison(alternative)['columns'][1]
                                 for c in comparison['columns']]
        with db.tx() as conn:
            db.run(conn, 'UPDATE recommendations SET simple_rule = CAST(:x AS jsonb) WHERE run_id = :r '
                   'AND case_id = :c AND rec_version = :v', x=db.js(comparison), r=run['run_id'],
                   c=case['case_id'], v=rec['rec_version'])
    as_seat(client, other)
    [row] = read(client)['expected']['plans']
    assert row['plan_key'] == alternative and row['dest_branch'] == other
    assert row['computed'] is available
    if available:
        assert [row[f] for f in FIGURES] == [12.37, -3.11, -1.01, 3.19]
    else:
        assert all(row[f] is None for f in FIGURES) and row['reason'] == 'plan_not_computed'
    as_seat(client, plan.dest)
    assert read(client)['expected']['plans'] == []  # stale denormalized plan_dest never widens scope


def test_new_active_recommendation_cannot_replace_committed_version(client):
    run, case, rec, plan = committed_story(client)
    with db.tx() as conn:
        db.run(conn, 'INSERT INTO recommendations (run_id, case_id, rec_version, day, engine_input, result, '
               "fact_checks, simple_rule) SELECT run_id, case_id, 'newer-version', day, engine_input, "
               "jsonb_set(result, '{recommended}', '\"keep\"'), fact_checks, NULL FROM recommendations "
               'WHERE run_id = :r AND case_id = :c AND rec_version = :v', r=run['run_id'], c=case['case_id'],
               v=rec['rec_version'])
        db.run(conn, "UPDATE cases SET active_rec_version = 'newer-version' WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=case['case_id'])
    [row] = read(client)['expected']['plans']
    assert row['computed'] is True and row['plan_key'] == plan.key
    assert row['net_change'] == rec['simple_rule']['columns'][0]['delta_vs_keep']


@pytest.mark.parametrize('relation', ['one_shared_branch', 'disjoint', 'different_sku'])
def test_combined_footprints_and_cents_cover_every_page(client, relation):
    run, case, rec, plan = committed_story(client)
    with db.tx() as conn:
        # Clone only this test's committed case and immutable approval records as a second independent projection.
        db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, state, active_rec_version, "
               "round, opened_day, plan_dest) SELECT run_id, 'projection-2', 'ZZZ-2', 'projection-lot', "
               'source_branch, sku, state, active_rec_version, round, opened_day, plan_dest FROM cases '
               'WHERE run_id = :r AND case_id = :c', r=run['run_id'], c=case['case_id'])
        db.run(conn, "INSERT INTO decisions (run_id, case_id, rec_version, round, role, decision) SELECT run_id, "
               "'projection-2', rec_version, round, role, decision FROM decisions WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=case['case_id'])
        db.log(conn, run['run_id'], run['day'], 'committed', 'projection-2', plan=plan.key, round=1)
        for cid, branches, net, transport in ((case['case_id'], ['A', 'B'], '0.10', '0.10'),
                                             ('projection-2', ['C', 'D'] if relation == 'disjoint' else ['B', 'C'],
                                              '0.20', '0.20')):
            inp = deepcopy(rec['engine_input'])
            inp['lots'] = [{**inp['lots'][0], 'branch': branches[0]}]
            inp['runs'] = [{**inp['runs'][0], 'origin': branches[0], 'dest': branches[1]}]
            if relation == 'different_sku' and cid == 'projection-2':
                inp['product']['sku'] = 'other-sku'
            comparison = stored_comparison(plan.key, net, transport)
            if cid == case['case_id']:
                db.run(conn, 'UPDATE recommendations SET engine_input = CAST(:i AS jsonb), simple_rule = CAST(:x AS jsonb) '
                       'WHERE run_id = :r AND case_id = :c AND rec_version = :v', i=db.js(inp), x=db.js(comparison),
                       r=run['run_id'], c=cid, v=rec['rec_version'])
            else:
                db.run(conn, 'INSERT INTO recommendations (run_id, case_id, rec_version, day, engine_input, result, '
                       "fact_checks, simple_rule) VALUES (:r, :c, :v, 3, CAST(:i AS jsonb), CAST(:res AS jsonb), "
                       "'[]', CAST(:x AS jsonb))", r=run['run_id'], c=cid, v=rec['rec_version'], i=db.js(inp),
                       res=db.js(rec['result']), x=db.js(comparison))
    as_seat(client, None)
    first, second = (read(client, page=p, page_size=1)['expected'] for p in (1, 2))
    assert first['total'] == second['total'] == 2
    assert [first['plans'][0]['key'], second['plans'][0]['key']] == [case['key'], 'ZZZ-2']
    combined = first['combined']
    assert combined == second['combined']
    if relation == 'one_shared_branch':
        assert combined == {'additive': False, 'plans': 2, 'reason': 'overlap'}
        with db.tx() as conn:
            db.run(conn, "UPDATE recommendations SET simple_rule = NULL WHERE run_id = :r AND case_id = 'projection-2'",
                   r=run['run_id'])
        assert read(client)['expected']['combined'] == {'additive': False, 'plans': 2, 'reason': 'not_computed'}
    else:
        assert combined == {'additive': True, 'plans': 2, 'net_change': 0.3,
                            'waste_change_units': -6.22, 'waste_change_kg': -2.02, 'transport_and_handling': 0.3}
        assert Decimal(str(combined['net_change'])) == sum(Decimal(str(r['net_change']))
                                                        for e in (first, second) for r in e['plans'])


@pytest.mark.parametrize('viewer', ['source', 'destination', 'other', 'hq', 'hq_source', 'hq_other'])
def test_projection_strict_store_scope(client, monkeypatch, viewer):
    run, case, _, plan = committed_story(client)
    ref = refdata.get(run['dataset_version'])
    monkeypatch.setitem(ref.settings, 'board', {'default_scope': 'all', 'page_size': 2})
    other = next(b for b in ref.branches if b not in (case['source_branch'], plan.dest))
    store = {'source': case['source_branch'], 'destination': plan.dest, 'other': other}.get(viewer)
    as_seat(client, store)
    requested = case['source_branch'] if viewer == 'hq_source' else other
    expected = read(client, **({} if viewer == 'hq' else {'branch': requested}))['expected']
    assert expected['total'] == (0 if viewer in ('other', 'hq_other') else 1)


def test_footprint_uses_all_lots_source_and_run_destinations():
    inp = {'product': {'sku': 'sku'}, 'source': 'S', 'lots': [{'branch': 'L'}, {'branch': 'M'}],
           'runs': [{'origin': 'S', 'dest': 'D'}, {'origin': 'S', 'dest': 'E'}]}
    assert hasattr(reporting, 'footprint')
    assert reporting.footprint(inp) == ('sku', frozenset({'S', 'L', 'M', 'D', 'E'}))


def test_no_movements_has_recorded_zeros_and_unrecorded_after_receipt_fields(client):
    body = read(client)
    assert 'actual' in body
    assert set(body) == {'scope', 'activity', 'expected', 'actual'}
    assert body['actual'] == {'kind': 'RECORDED', 'dispatched_units': 0, 'dispatched_transfers': 0,
                              'received_units': 0, 'discrepancies_open': 0,
                              'sales_after_receipt': None, 'waste_after_receipt': None}


def test_confirmed_dispatch_short_receipt_and_strict_actual_scope(client):
    from tests.api.test_execution import committed, dispatch_body, post, prepared, receipt_body

    run, action, source, destination = prepared(client)
    sent = committed(action) - 6
    assert post(client, f"/api/cases/{action['case_id']}/dispatch", dispatch_body(action, sent)).status_code == 200
    assert read(client)['actual'] == {'kind': 'RECORDED', 'dispatched_units': sent, 'dispatched_transfers': 1,
                                     'received_units': 0, 'discrepancies_open': 0,
                                     'sales_after_receipt': None, 'waste_after_receipt': None}
    as_seat(client, destination)
    got = sent - 4
    receipt = post(client, f"/api/cases/{action['case_id']}/receipt",
                   receipt_body(action, got, reason='four units missing'))
    assert receipt.status_code == 200, receipt.json()
    expected = {'kind': 'RECORDED', 'dispatched_units': sent, 'dispatched_transfers': 1,
                'received_units': got, 'discrepancies_open': 1, 'sales_after_receipt': None, 'waste_after_receipt': None}
    assert read(client)['actual'] == expected
    as_seat(client, source)
    assert read(client)['actual'] == expected
    other = next(b for b in refdata.get(run['dataset_version']).branches if b not in (source, destination))
    as_seat(client, other)
    assert read(client, branch=source)['actual'] == {
        'kind': 'RECORDED', 'dispatched_units': 0, 'dispatched_transfers': 0, 'received_units': 0,
        'discrepancies_open': 0, 'sales_after_receipt': None, 'waste_after_receipt': None}
    as_seat(client, None)
    assert read(client)['actual'] == read(client, branch=source)['actual'] == expected
    assert read(client, branch=other)['actual']['dispatched_units'] == 0
    # A different signed-in workspace has no access to these confirmed movements.
    from api.app import app
    from fastapi.testclient import TestClient
    from tests.api.conftest import sign_in
    with TestClient(app) as independent:
        sign_in(independent, 'HQ')
        assert workspace_of(independent)['run_id'] != run['run_id']
        assert read(independent)['actual']['dispatched_units'] == 0


def test_workspace_groups_share_one_statement_snapshot_during_second_connection_write(client):
    run, case, rec, _ = committed_story(client)
    as_seat(client, None)
    before = read(client)
    mutations = []
    def after_read(conn, cursor, statement, parameters, context, executemany):
        if mutations or 'WITH committed_data' not in statement:
            return
        mutations.append(True)
        # A second connection writes after the report statement has its snapshot. Its records belong only
        # to this test's workspace; the real dispatch/receipt route behavior is checked above.
        with db.tx() as writer:
            assert writer is not conn
            db.run(writer, 'UPDATE recommendations SET simple_rule = NULL WHERE run_id = :r AND case_id = :c '
                   'AND rec_version = :v', r=run['run_id'], c=case['case_id'], v=rec['rec_version'])
            db.run(writer, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r "
                   "AND state = 'OPEN'", r=run['run_id'])
            db.run(writer, 'INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, qty, '
                   "recorded_by, role, scenario_day) VALUES (:r, 'snapshot', :c, 'snapshot', 'DISPATCH', 7, "
                   "'test', 'manager', :day)", r=run['run_id'], c=case['case_id'], day=run['day'])
    sa.event.listen(db.engine(), 'after_cursor_execute', after_read)
    try:
        during = read(client)
    finally:
        sa.event.remove(db.engine(), 'after_cursor_execute', after_read)
    assert mutations == [True]
    assert during == before
    after = read(client)
    assert after['activity']['cancelled']['count'] > before['activity']['cancelled']['count']
    assert before['expected']['plans'][0]['computed'] is True
    assert after['expected']['plans'][0]['computed'] is False
    assert before['actual']['dispatched_units'] == 0 and after['actual']['dispatched_units'] == 7


def test_actual_counts_transfers_once_and_only_open_discrepancies(client):
    run, case, _, _ = committed_story(client)
    with db.tx() as conn:
        for movement, kind, qty in [('one', 'DISPATCH', 3), ('two', 'DISPATCH', 4), ('three', 'RECEIPT', 6)]:
            db.run(conn, 'INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, qty, '
                   "recorded_by, role, scenario_day) VALUES (:r, :m, :c, 'same-transfer', :k, :q, 'test', 'manager', :d)",
                   r=run['run_id'], m=movement, c=case['case_id'], k=kind, q=qty, d=run['day'])
        for ident, state in [('open', 'OPEN'), ('resolved', 'RESOLVED')]:
            db.run(conn, 'INSERT INTO discrepancies (run_id, discrepancy_id, case_id, action_id, kind, units, '
                   "open_units, state) VALUES (:r, :id, :c, 'same-transfer', 'SHORT', 1, :u, :s)",
                   r=run['run_id'], id=ident, c=case['case_id'], u=1 if state == 'OPEN' else 0, s=state)
    assert read(client)['actual'] == {'kind': 'RECORDED', 'dispatched_units': 7, 'dispatched_transfers': 1,
                                     'received_units': 6, 'discrepancies_open': 1,
                                     'sales_after_receipt': None, 'waste_after_receipt': None}


def test_lost_selection_binding_keeps_committed_destination_and_withholds_projection(client):
    run, case, rec, plan = committed_story(client)
    other = next(b for b in refdata.get(run['dataset_version']).branches if b not in (case['source_branch'], plan.dest))
    alternative = cases.parse_plan(f'tx:{other}:{plan.run_code}:{plan.qty}:hold').key
    with db.tx() as conn:
        # The immutable committed key survives a lost selection. Neither scope nor row identity may
        # switch back to the recommendation, and amounts remain uncomputed until binding is known.
        db.run(conn, "UPDATE activity_log SET payload = jsonb_set(payload, '{plan}', CAST(:p AS jsonb)) "
               "WHERE run_id = :r AND case_id = :c AND type = 'committed'", p=db.js(alternative),
               r=run['run_id'], c=case['case_id'])
        db.run(conn, 'INSERT INTO movements (run_id, movement_id, case_id, action_id, kind, qty, '
               "recorded_by, role, scenario_day) VALUES (:r, 'bound', :c, 'bound', 'DISPATCH', 7, 'test', 'manager', :d)",
               r=run['run_id'], c=case['case_id'], d=run['day'])
    as_seat(client, other)
    body = read(client)
    [row] = body['expected']['plans']
    assert row['plan_key'] == alternative and row['dest_branch'] == other
    assert row['computed'] is False and row['reason'] == 'commitment_not_bound'
    assert all(row[f] is None for f in FIGURES)
    assert body['actual']['dispatched_units'] == 7
    as_seat(client, plan.dest)
    body = read(client)
    assert body['expected']['plans'] == [] and body['actual']['dispatched_units'] == 0


def test_missing_approval_version_never_uses_active_recommendation(client):
    run, case, _, plan = committed_story(client)
    with db.tx() as conn:
        db.run(conn, "DELETE FROM decisions WHERE run_id = :r AND case_id = :c AND role = 'source'",
               r=run['run_id'], c=case['case_id'])
    [row] = read(client)['expected']['plans']
    assert row['plan_key'] == plan.key and row['horizon'] is None
    assert row['computed'] is False and row['reason'] == 'commitment_not_bound'
    assert all(row[f] is None for f in FIGURES)


def test_committed_alternative_destination_scopes_activity_drilldown_and_expected(client, monkeypatch):
    """One plan in force for every Reports figure: the committed plan, not the denormalized plan_dest (gap 7)."""
    run, case, rec, plan = committed_story(client)
    other = next(b for b in refdata.get(run['dataset_version']).branches
                 if b not in (case['source_branch'], plan.dest))
    alternative = cases.parse_plan(f'tr:{other}:{plan.run_code}:{plan.qty}:hold').key
    selection_fixture(monkeypatch, case, rec, alternative)
    with db.tx() as conn:  # the board column still names the recommendation's destination
        assert db.one(conn, 'SELECT plan_dest FROM cases WHERE run_id = :r AND case_id = :c',
                      r=run['run_id'], c=case['case_id'])['plan_dest'] == plan.dest

    def drilldown(report, **params):
        response = client.get('/api/cases', params={'report': report, **params})
        assert response.status_code == 200, response.json()
        return [c['case_id'] for c in response.json()['cases'] if c['case_id'] == case['case_id']]

    def check(report, involved, **params):
        body = read(client, **params)
        tile = 'committed' if report == 'committed' else 'cancelled'
        assert body['activity'][tile]['count'] == int(involved), (report, params)
        assert drilldown(report, **params) == ([case['case_id']] if involved else []), (report, params)
        return body

    for viewer, involved in ((other, True), (plan.dest, False), (case['source_branch'], True)):
        as_seat(client, viewer)
        body = check('committed', involved)
        assert [p['case_id'] for p in body['expected']['plans']] == ([case['case_id']] if involved else [])
        if involved:
            assert body['expected']['plans'][0]['dest_branch'] == other
    as_seat(client, None)
    for store, involved in ((other, True), (plan.dest, False)):
        body = check('committed', involved, branch=store)
        assert len(body['expected']['plans']) == int(involved)

    # A cancelled commitment keeps the destination it was committed to.
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r AND case_id = :c",
               r=run['run_id'], c=case['case_id'])
    for store, involved in ((other, True), (plan.dest, False)):
        check('cancelled', involved, branch=store)
