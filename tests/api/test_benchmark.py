"""Matched evaluator worlds, transactional checkpoints and input eligibility."""
import pytest

from api import advance, benchmark, cases, db, refdata, sandbox, weekcheck
from tests.api.conftest import drop_runs
from tests.api.benchmark_fixtures import eligible_dv, eligible_workspace  # noqa: F401


@pytest.fixture
def bm(eligible_dv, request, monkeypatch):
    if getattr(request, 'param', False):
        initialize = sandbox.init_worlds
        def expiring_stock(conn, run_id, ref, truth, **kwargs):
            initialize(conn, run_id, ref, truth, **kwargs)
            lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' "
                               "AND tag = 'designed'", r=run_id)
            db.run(conn, "UPDATE lots SET qty = 53, expiry_day = 0, last_verified_day = -10 "
                         "WHERE run_id = :r AND lot_id = :l", r=run_id, l=lot['lot_id'])
            db.run(conn, "UPDATE truth_lots SET qty = 50 WHERE run_id = :r AND lot_id = :l",
                   r=run_id, l=lot['lot_id'])
            # Remove destination stock/deliveries in this scratch simulation, never demand or forecasts.
            db.run(conn, "DELETE FROM truth_lots WHERE run_id = :r AND lot_id IN (SELECT lot_id FROM lots "
                         "WHERE run_id = :r AND sku = :s AND branch <> :b)",
                   r=run_id, s=lot['sku'], b=lot['branch'])
            db.run(conn, "DELETE FROM lots WHERE run_id = :r AND sku = :s AND branch <> :b",
                   r=run_id, s=lot['sku'], b=lot['branch'])
            db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND sku = :s", r=run_id, s=lot['sku'])
        monkeypatch.setattr(sandbox, 'init_worlds', expiring_stock)
    with db.tx() as conn:
        run = benchmark.create_run(conn, eligible_dv, trial=False, git_commit='test-only',
                                   image_digest='test-only', worlds=weekcheck.BENCH_WORLDS)
    yield run
    with db.tx() as conn:
        for table in ('benchmark_days', 'benchmark_observations', 'benchmark_branch_day', 'benchmark_summary',
                      'benchmark_transfer_audit', 'evaluation_check_history', 'benchmark_runs'):
            db.run(conn, f'DELETE FROM {table} WHERE benchmark_id = :r', r=run['run_id'])
        drop_runs(conn, [run['run_id']])


def rows(conn, run, table, world):
    data = db.rows(conn, f'SELECT * FROM {table} WHERE run_id = :r AND world = :w', r=run['run_id'], w=world)
    return sorted(({k: v for k, v in r.items() if k not in ('run_id', 'world')} for r in data), key=repr)


def test_four_worlds_are_matched_and_never_active(bm, template):
    with db.tx() as conn:
        assert sandbox.active_template(conn)['run_id'] == template
        assert db.one(conn, 'SELECT status FROM demo_runs WHERE run_id = :r', r=bm['run_id'])['status'] == 'INITIALIZING'
        expected = rows(conn, bm, 'lots', 'baseline_rule')
        assert expected
        for world in weekcheck.BENCH_WORLDS:
            assert rows(conn, bm, 'lots', world) == expected
        meta = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :r', r=bm['run_id'])
        assert meta['status'] == 'RUNNING'
        assert meta['expected_domain'] == {'worlds': list(weekcheck.BENCH_WORLDS),
                                          'branches': sorted(refdata.get(bm['dataset_version']).branches),
                                          'days': list(range(7))}
        assert not set(sandbox.per_run_tables(conn)) & {'benchmark_runs', 'benchmark_days', 'benchmark_summary',
            'benchmark_observations', 'benchmark_branch_day', 'benchmark_transfer_audit', 'evaluation_check_history'}
    assert advance.WORLDS == ('shelfshift', 'baseline_rule', 'do_nothing')


def test_day_checkpoint_and_rule_equivalence(bm):
    with db.tx() as conn:
        stats = weekcheck.world_steps(conn, bm, refdata.truth(bm['dataset_version']))
        benchmark.mark_day(conn, bm['run_id'], 0, 0.1, {'completed': True, 'worlds': stats})
    with db.tx() as conn:
        assert db.one(conn, 'SELECT day FROM benchmark_days WHERE benchmark_id = :r', r=bm['run_id'])['day'] == 0
        for table in ('world_actions', 'outcomes', 'world_events', 'lots'):
            assert rows(conn, bm, table, 'baseline_rule') == rows(conn, bm, table, 'baseline_checked')
        assert rows(conn, bm, 'outcomes', 'baseline_rule')


def test_store_rules_match_without_corrections_through_issued_days(bm):
    from api import evalref
    decisions = 0
    with evalref.forecast_rule(bm['dataset_version']):
        for day in range(7):
            with db.tx() as conn:
                run = db.one(conn, 'UPDATE demo_runs SET day = :d WHERE run_id = :r RETURNING *',
                             r=bm['run_id'], d=day)
                advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
                stats = weekcheck.world_steps(conn, run, refdata.truth(run['dataset_version']))
                decisions += stats['baseline_rule']['decisions']
                benchmark.mark_day(conn, run['run_id'], day, 0.1, {'completed': True, 'worlds': stats})
                for table in ('world_actions', 'outcomes', 'world_events', 'lots'):
                    assert rows(conn, run, table, 'baseline_rule') == rows(conn, run, table, 'baseline_checked')
    assert decisions > 0


def test_mid_day_failure_rolls_back_mornings_and_preserves_previous_day(bm, monkeypatch):
    truth = refdata.truth(bm['dataset_version'])
    with db.tx() as conn:
        stats = weekcheck.world_steps(conn, bm, truth)
        benchmark.mark_day(conn, bm['run_id'], 0, 0.1, {'completed': True, 'worlds': stats})
        before = rows(conn, bm, 'lots', 'baseline_rule')
        actions = rows(conn, bm, 'world_actions', 'baseline_rule')
    def fail(*args, **kwargs):
        raise RuntimeError('test-only interruption after both mornings')
    monkeypatch.setattr(advance, 'step_world', fail)
    with pytest.raises(RuntimeError, match='interruption'):
        with db.tx() as conn:
            run = {**bm, 'day': 1}
            advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
            stats = weekcheck.world_steps(conn, run, truth)
            benchmark.mark_day(conn, bm['run_id'], 1, 0.1, stats)
    with db.tx() as conn:
        assert rows(conn, bm, 'lots', 'baseline_rule') == before
        assert rows(conn, bm, 'world_actions', 'baseline_rule') == actions
        assert [r['day'] for r in db.rows(conn, 'SELECT day FROM benchmark_days WHERE benchmark_id = :r',
                                        r=bm['run_id'])] == [0]


@pytest.mark.parametrize('bm', [True], indirect=True)
def test_eager_day_zero_opens_real_cases(bm):
    # Stock-only stress case: 53 recorded / 50 physical, expiring today; immutable forecasts and demand.
    with db.tx() as conn:
        lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' "
                           "AND last_verified_day = -10 ORDER BY lot_id LIMIT 1", r=bm['run_id'])
        opened = db.rows(conn, 'SELECT * FROM cases WHERE run_id = :r AND lot_id = :l',
                         r=bm['run_id'], l=lot['lot_id'])
        assert opened
        assert lot['expiry_day'] == bm['day'] == 0
        assert all(c['opened_day'] == 0 and c['active_rec_version'] for c in opened)
        assert any(cases.active_rec(conn, c)['fact_checks'] for c in opened)
        from api import overlay, signals
        from api.errors import ApiError
        from tests.api.conftest import seat
        case = opened[0]
        rec = cases.active_rec(conn, case)
        assert rec['result']['recommended'].startswith('tr:')
        counts = [f for f in rec['fact_checks'] if f['kind'] == 'STALE_COUNT' and f['changes_plan']
                  and f['lot_ids'] == [lot['lot_id']] and f['branch'] == case['source_branch']]
        assert counts, rec['fact_checks']
        fact = counts[0]
        assert fact['recorded_value'] == 53 and fact['alt_value'] == 37.1
        assert cases.parse_plan(rec['result']['recommended']).qty == 20
        q = signals.request_evidence(conn, bm, seat(case['source_branch']), case['case_id'],
                                     rec['rec_version'], fact['fact_id'])
        actual = overlay.physical(conn, bm, 'shelfshift', fact['lot_ids'])
        assert actual == {lot['lot_id']: 50}
        answer = signals.answer(conn, bm, seat(fact['branch']), q['request_id'], 'counted',
                                {l: {'shelf_units': actual[l], 'backroom_units': 0.0} for l in fact['lot_ids']}, 'now')
        with pytest.raises(ApiError) as refused:
            signals.confirm(conn, bm, seat(fact['branch']), answer['signal_id'], answer['revision'], True)
        assert refused.value.code == 'sandbox_reset'
        result = signals.confirm(conn, bm, seat(fact['branch']), answer['signal_id'], answer['revision'], True,
                                 statuses=('INITIALIZING',))
        assert result['fact_id']
        replacement = cases.active_rec(conn, cases.case_row(conn, bm['run_id'], case['case_id']))
        assert replacement['rec_version'] != rec['rec_version']
        assert cases.parse_plan(replacement['result']['recommended']).qty == 18
        sandbox.autopilot(conn, bm)
        committed = cases.case_row(conn, bm['run_id'], case['case_id'])
        assert committed['state'] == 'PREPARED'
        action = db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                              "AND case_id = :c AND kind = 'TRANSFER'", r=bm['run_id'], c=case['case_id'])
        assert action['qty'] == 18 and action['run_day'] == 0
        activity = db.rows(conn, "SELECT day, type, payload FROM activity_log WHERE run_id = :r "
                                "AND case_id = :c AND type IN ('committed', 'prepared')",
                           r=bm['run_id'], c=case['case_id'])
        assert {a['type'] for a in activity} == {'committed', 'prepared'}
        assert all(a['day'] == 0 and a['payload']['plan'] == replacement['result']['recommended'] for a in activity)
        assert db.one(conn, 'SELECT confirmed_day FROM reported_facts WHERE run_id = :r AND fact_id = :f',
                      r=bm['run_id'], f=result['fact_id'])['confirmed_day'] == 0


def test_eligible_artifact_is_accepted_while_original_stand_in_is_refused(eligible_dv, public_dv):
    assert callable(getattr(benchmark, 'refusals', None)), 'evaluator must refuse unfair inputs before work'
    assert benchmark.refusals(refdata.get(eligible_dv), trial=False) == []
    reasons = benchmark.refusals(refdata.get(public_dv), trial=False)
    assert any('forecasts come from the local stand-in' in reason for reason in reasons)
    assert eligible_dv != public_dv


@pytest.mark.parametrize('ordering,blocked', [
    ({'enabled_stores': []}, False),
    ({'enabled': False, 'enabled_stores': ['test-store']}, False),
    ({'enabled_default': False}, False),
    ({'enabled_stores': ['test-store']}, True),
    ({'enabled_default': True}, True),
])
def test_ordering_guard_uses_effective_enabled_stores(eligible_dv, ordering, blocked):
    import copy
    ref = copy.copy(refdata.get(eligible_dv))
    ref.settings = copy.deepcopy(ref.settings)
    ref.settings['ordering'] = ordering
    reasons = benchmark.refusals(ref, trial=False)
    assert any('ordering is enabled in this dataset; the store-rule ordering limits (D-33) are not built'
               in reason for reason in reasons) is blocked


@pytest.mark.parametrize('field', ['m20', 'm30'])
def test_price_response_must_be_one(eligible_dv, field):
    import copy
    ref = copy.copy(refdata.get(eligible_dv))
    ref.settings = copy.deepcopy(ref.settings)
    ref.settings['truth']['price_response'][field] = 1.1
    assert any('price response must be 1.0 (D-28)' in reason for reason in benchmark.refusals(ref, trial=False))


def test_local_point_is_refused_even_when_metadata_claims_eligible(eligible_dv):
    from data import forecast as F
    class Rollback(Exception):
        pass
    with pytest.raises(Rollback):
        with db.tx() as conn:
            db.run(conn, 'UPDATE ref_forecast_point SET provider = :p WHERE dataset_version = :v',
                   v=eligible_dv, p=F.LOCAL)
            reasons = benchmark.refusals(refdata.get(eligible_dv), trial=False)
            assert any('forecasts come from the local stand-in' in reason for reason in reasons)
            raise Rollback()


def test_stand_in_metadata_is_refused_even_when_points_claim_eligible(eligible_dv):
    import copy
    ref = copy.copy(refdata.get(eligible_dv))
    ref.forecast = copy.deepcopy(dict(ref.forecast))
    ref.forecast['stand_in'] = True
    assert any('forecasts come from the local stand-in' in reason for reason in benchmark.refusals(ref, trial=False))


def test_all_refusals_happen_before_any_row_is_written(eligible_dv, monkeypatch):
    import copy
    original = refdata.get(eligible_dv)
    ref = copy.copy(original)
    ref.settings = copy.deepcopy(original.settings)
    ref.settings['ordering'] = {'enabled_stores': [original.branches[0]]}
    ref.settings['truth']['price_response']['m20'] = 1.1
    def never_initialize(*args, **kwargs):
        pytest.fail('ineligible input reached world initialization')
    monkeypatch.setattr(sandbox, 'init_worlds', never_initialize)
    with refdata.override(eligible_dv, ref):
        with db.tx() as conn:
            counts = lambda: [db.one(conn, f'SELECT count(*) AS n FROM {table}')['n']
                              for table in ('demo_runs', 'benchmark_runs', 'lots', 'truth_lots', 'cases')]
            before = counts()
            reasons = benchmark.refusals(ref, trial=False)
            assert len(reasons) == 2
            with pytest.raises(ValueError) as rejected:
                benchmark.create_run(conn, eligible_dv, trial=False, git_commit='test-only',
                                     image_digest='test-only', worlds=weekcheck.BENCH_WORLDS)
            assert all(reason in str(rejected.value) for reason in reasons)
            assert counts() == before


def test_trial_records_refusals_and_remains_unpublishable(public_dv):
    import copy
    ref = copy.copy(refdata.get(public_dv))
    ref.settings = copy.deepcopy(ref.settings)
    ref.settings['ordering'] = {'enabled_stores': [ref.branches[0]]}
    ref.settings['truth']['price_response']['m30'] = 1.1
    with refdata.override(public_dv, ref), db.tx() as conn:
        run = benchmark.create_run(conn, public_dv, trial=True, git_commit='test-only',
                                   image_digest='test-only', worlds=weekcheck.BENCH_WORLDS)
        meta = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :r', r=run['run_id'])
        assert meta['trial'] and meta['status'] == 'TRIAL'
        reasons = meta['stats']['refusals']
        assert len(reasons) == 3
        assert any('ordering is enabled' in reason for reason in reasons)
        assert any('price response must be 1.0' in reason for reason in reasons)
        assert any('local stand-in' in reason for reason in reasons)
        assert meta['settings']['ordering']['enabled_stores'] == ref.branches[:1]
        assert meta['settings']['price_response']['m30'] == 1.1
        assert run['status'] == 'INITIALIZING'
        for table in ('benchmark_runs',):
            db.run(conn, f'DELETE FROM {table} WHERE benchmark_id = :r', r=run['run_id'])
        drop_runs(conn, [run['run_id']])


def test_reset_and_real_prune_never_clone_or_remove_benchmark(bm, template, eligible_workspace):
    import uuid
    workspace = eligible_workspace()
    session = 'test-bm-' + uuid.uuid4().hex
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET created_at = now() - interval '30 days' WHERE run_id = :r",
               r=bm['run_id'])
        before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :r', r=bm['run_id'])
        db.run(conn, "INSERT INTO sessions (session_id, role, run_id) VALUES (:s, 'hq', :r)",
               s=session, r=workspace['run_id'])
        new, _ = sandbox.reset(conn, session)
        assert new != bm['run_id']
        assert db.one(conn, 'SELECT template_run_id FROM demo_runs WHERE run_id = :r', r=new)['template_run_id'] == template
        db.run(conn, "UPDATE demo_runs SET retired_at = now() - interval '30 days' WHERE run_id = :r",
               r=workspace['run_id'])
        assert sandbox.prune(conn)['sandboxes'] > 0
        assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :r', r=bm['run_id']) == before
        assert db.one(conn, 'SELECT status FROM demo_runs WHERE run_id = :r', r=bm['run_id'])['status'] == 'INITIALIZING'
        assert sandbox.active_template(conn)['run_id'] == template
        drop_runs(conn, [new])
