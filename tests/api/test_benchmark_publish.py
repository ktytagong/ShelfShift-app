"""Finalization requires complete evidence and preserves the previous publication."""
import copy
import csv
import gzip
import hashlib
import io
import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from api import advance, benchmark, db, evalref, metrics, refdata, weekcheck
from tests.api.benchmark_fixtures import eligible_dv, eligible_workspace  # noqa: F401
from tests.api.test_benchmark import bm  # noqa: F401
from tests.api.conftest import drop_runs


def resume_identity(status='RUNNING'):
    versions = dict(engine='engine-test', dataset='dataset-test', policy='policy-test',
                    forecast_runs=['forecast-first', 'forecast-second'], evaluator='eval-test',
                    settings='settings-test')
    row = dict(status=status, trial=False, published_at=None, image_digest='image-test', git_commit='commit-test',
               engine_version=versions['engine'], dataset_version=versions['dataset'],
               policy_version=versions['policy'], forecast_runs=list(versions['forecast_runs']),
               eval_version=versions['evaluator'], settings_digest=versions['settings'])
    return row, versions


def resume_rule(row, versions, *, image='image-test', commit='commit-test', settings='settings-test'):
    rule = getattr(benchmark, 'can_resume', None)
    assert callable(rule), 'strict resume rule must exist'
    return rule(row, image, commit, versions, settings)


@pytest.mark.parametrize('status', ['RUNNING', 'FAILED'])
def test_can_resume_exact_unpublished_build_and_inputs(status):
    row, versions = resume_identity(status)
    assert resume_rule(row, versions) == (True, '')


@pytest.mark.parametrize('field,value,reason', [
    ('status', 'VALIDATED', 'status'), ('status', 'PUBLISHED', 'status'), ('status', 'TRIAL', 'status'),
    ('trial', True, 'trial'), ('published_at', 'already-published', 'published'),
    ('image_digest', 'another-image', 'image digest'), ('git_commit', 'another-commit', 'git commit'),
    ('engine_version', 'another-engine', 'engine'), ('dataset_version', 'another-dataset', 'dataset'),
    ('policy_version', 'another-policy', 'policy'), ('forecast_runs', ['forecast-second', 'forecast-first'], 'forecast_runs'),
    ('eval_version', 'another-evaluator', 'evaluator'), ('settings_digest', 'another-settings', 'settings'),
    ('image_digest', None, 'image digest'), ('git_commit', '', 'git commit'),
])
def test_can_resume_every_state_build_and_input_difference_refuses(field, value, reason):
    row, versions = resume_identity()
    row[field] = value
    allowed, why = resume_rule(row, versions)
    assert allowed is False and reason in why
    assert 'start a fresh benchmark: python -m api.weekcheck benchmark start --dataset ' in why


def test_can_resume_settings_digest_is_checked_independently_of_version_labels():
    row, versions = resume_identity()
    allowed, why = resume_rule(row, versions, settings='different-settings')
    assert allowed is False and 'settings digest' in why


@pytest.mark.parametrize('change', ['missing', 'extra'])
def test_can_resume_requires_exactly_the_six_version_fields(change):
    row, versions = resume_identity()
    if change == 'missing':
        versions.pop('policy')
    else:
        versions['git_commit'] = row['git_commit']
    allowed, why = resume_rule(row, versions)
    assert allowed is False and 'six versions' in why


@pytest.fixture(scope='module')
def completed(eligible_dv):
    with db.tx() as conn:
        run = benchmark.create_run(conn, eligible_dv, trial=False, git_commit='test-build',
                                   image_digest='test-image', worlds=weekcheck.BENCH_WORLDS)
    with evalref.forecast_rule(eligible_dv):
        for day in range(7):
            with db.tx() as conn:
                run = db.one(conn, 'UPDATE demo_runs SET day=:d WHERE run_id=:r RETURNING *', d=day, r=run['run_id'])
                advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
                stats = weekcheck.world_steps(conn, run, refdata.truth(eligible_dv))
                benchmark.mark_day(conn, run['run_id'], day, .01, {'completed': True, 'worlds': stats})
    yield run
    with db.tx() as conn:
        for table in ('benchmark_days', 'benchmark_observations', 'benchmark_branch_day', 'benchmark_summary',
                      'benchmark_transfer_audit', 'evaluation_check_history', 'benchmark_runs'):
            db.run(conn, f'DELETE FROM {table} WHERE benchmark_id=:r', r=run['run_id'])
        drop_runs(conn, [run['run_id']])


@pytest.fixture
def scratch(completed):
    with db.engine().connect() as conn:
        tx = conn.begin()
        try:
            yield conn, completed, refdata.get(completed['dataset_version'])
        finally:
            tx.rollback()


def finalize(conn, run, ref):
    assert callable(getattr(weekcheck, 'validate', None)), 'all-world validation must exist'
    assert weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version']), weekcheck.BENCH_WORLDS) == []
    rows = metrics.branch_day_rows(conn, run['run_id'], weekcheck.BENCH_WORLDS, ref)
    payload = metrics.summary(conn, run, ref, rows, weekcheck.closing_balance(conn, run, weekcheck.BENCH_WORLDS))
    benchmark.save_result(conn, run['run_id'], payload, rows, final_check=weekcheck.publication_check)
    return rows, payload


def test_complete_result_publishes_once(scratch):
    conn, run, ref = scratch
    rows, payload = finalize(conn, run, ref)
    assert len(rows) == 4 * 7 * len(ref.branches)
    for world in weekcheck.BENCH_WORLDS:
        raw = db.one(conn, 'SELECT coalesce(sum(revenue-transport-handling-disposal),0) AS net '
                          'FROM outcomes WHERE run_id=:r AND world=:w', r=run['run_id'], w=world)['net']
        assert payload['money']['by_scenario'][metrics.WORLD_KEYS[world]] == pytest.approx(raw, abs=.01)
    benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])
    assert before['status'] == 'PUBLISHED' and before['published_at'] is not None
    with pytest.raises(ValueError, match='VALIDATED'):
        benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    assert db.one(conn, 'SELECT published_at FROM benchmark_runs WHERE benchmark_id=:b',
                  b=run['run_id'])['published_at'] == before['published_at']


@pytest.mark.parametrize('omission', ['last', 'middle', 'world', 'partial', 'branch', 'closing'])
def test_incomplete_result_never_validates_or_publishes(scratch, omission):
    conn, run, ref = scratch
    assert callable(getattr(weekcheck, 'validate', None)), 'all-world validation must exist'
    if omission in ('last', 'middle'):
        db.run(conn, 'DELETE FROM benchmark_days WHERE benchmark_id=:b AND day=:d', b=run['run_id'],
               d=6 if omission == 'last' else 3)
    elif omission in ('world', 'partial'):
        stats = db.one(conn, 'SELECT stats FROM benchmark_days WHERE benchmark_id=:b AND day=3', b=run['run_id'])['stats']
        if omission == 'world':
            stats['worlds'].pop('baseline_checked')
        else:
            stats['completed'] = False
        db.run(conn, 'UPDATE benchmark_days SET stats=CAST(:s AS jsonb) WHERE benchmark_id=:b AND day=3',
               b=run['run_id'], s=db.js(stats))
    if omission == 'closing':
        run = {**run, 'day': 5}
    failures = weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version']), weekcheck.BENCH_WORLDS)
    rows = metrics.branch_day_rows(conn, run['run_id'], weekcheck.BENCH_WORLDS, ref)
    if omission == 'branch':
        assert failures == []
        rows.pop()
    else:
        assert failures
    with pytest.raises(ValueError):
        payload = metrics.summary(conn, run, ref, rows, weekcheck.closing_balance(conn, run, weekcheck.BENCH_WORLDS))
        benchmark.save_result(conn, run['run_id'], payload, rows, final_check=weekcheck.publication_check)
    assert db.one(conn, 'SELECT status FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])['status'] == 'FAILED'
    assert not db.rows(conn, 'SELECT * FROM benchmark_summary WHERE benchmark_id=:b', b=run['run_id'])


@pytest.mark.parametrize('tamper', ['summary', 'rows', 'checkpoint', 'settings', 'domain', 'physical'])
def test_validation_receipt_binds_every_published_input(scratch, tamper):
    conn, run, ref = scratch
    finalize(conn, run, ref)
    queries = {
        'summary': "UPDATE benchmark_summary SET payload=jsonb_set(payload,'{footnote_x}','999') WHERE benchmark_id=:b",
        'rows': 'UPDATE benchmark_branch_day SET net=net+1 WHERE benchmark_id=:b',
        'checkpoint': 'DELETE FROM benchmark_days WHERE benchmark_id=:b AND day=3',
        'settings': "UPDATE benchmark_runs SET settings_digest='different' WHERE benchmark_id=:b",
        'audit': 'DELETE FROM benchmark_transfer_audit WHERE benchmark_id=:b',
        'domain': "UPDATE benchmark_runs SET expected_domain='{}' WHERE benchmark_id=:b",
        'physical': 'UPDATE truth_lots SET qty=qty+1 WHERE run_id=:b',
    }
    db.run(conn, queries[tamper], b=run['run_id'])
    with pytest.raises(ValueError):
        benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    assert db.one(conn, 'SELECT status FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])['status'] != 'PUBLISHED'


def test_status_label_alone_cannot_publish(scratch):
    conn, run, _ = scratch
    db.run(conn, "UPDATE benchmark_runs SET status='VALIDATED' WHERE benchmark_id=:b", b=run['run_id'])
    assert callable(getattr(benchmark, 'publish', None)), 'evidence-bound publication must exist'
    with pytest.raises(ValueError, match='receipt'):
        benchmark.publish(conn, run['run_id'])


@pytest.mark.parametrize('worlds', [(), ('shelfshift',), ('shelfshift',) * 4])
def test_validation_requires_all_worlds(scratch, worlds):
    conn, run, ref = scratch
    assert weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version']), worlds)
    assert db.one(conn, 'SELECT validation_receipt FROM benchmark_runs WHERE benchmark_id=:b',
                  b=run['run_id'])['validation_receipt'] is None


@pytest.mark.parametrize('status', ['VALIDATED', 'PUBLISHED'])
def test_validation_refuses_finalized_state_without_mutating_receipt(scratch, status):
    conn, run, ref = scratch
    finalize(conn, run, ref)
    if status == 'PUBLISHED':
        benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])
    with pytest.raises(ValueError, match='RUNNING'):
        weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version']))
    assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id']) == before


def test_missing_physical_callback_refuses_publication(scratch):
    conn, run, ref = scratch
    finalize(conn, run, ref)
    with pytest.raises(ValueError, match='physical gate callback'):
        benchmark.publish(conn, run['run_id'])


def test_published_checkpoint_and_result_are_immutable(scratch):
    conn, run, ref = scratch
    rows, payload = finalize(conn, run, ref)
    benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])
    with pytest.raises(ValueError, match='unpublished'):
        benchmark.mark_day(conn, run['run_id'], 7, .01, {'completed': True})
    with pytest.raises(ValueError, match='RUNNING'):
        benchmark.save_result(conn, run['run_id'], payload, rows, final_check=weekcheck.publication_check)
    with pytest.raises(ValueError, match='immutable'):
        benchmark.fail(conn, run['run_id'], ['test refusal'])
    assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id']) == before


def test_physical_change_between_validation_and_save_is_refused(scratch):
    conn, run, ref = scratch
    assert weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version'])) == []
    closing = weekcheck.closing_balance(conn, run)
    rows = metrics.branch_day_rows(conn, run['run_id'], weekcheck.BENCH_WORLDS, ref)
    payload = metrics.summary(conn, run, ref, rows, closing)
    assert db.run(conn, 'UPDATE truth_lots SET qty=qty+1 WHERE run_id=:r', r=run['run_id']) > 0
    with pytest.raises(ValueError, match='physical closing inputs differ'):
        benchmark.save_result(conn, run['run_id'], payload, rows, final_check=weekcheck.publication_check)


@pytest.mark.parametrize('field,value', [('kind', 'ACTUAL'), ('footnote_x', 123456), ('unexpected', 'extra')])
def test_whole_summary_must_equal_the_record_derived_result(scratch, field, value):
    conn, run, ref = scratch
    assert weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version'])) == []
    rows = metrics.branch_day_rows(conn, run['run_id'], weekcheck.BENCH_WORLDS, ref)
    payload = metrics.summary(conn, run, ref, rows, weekcheck.closing_balance(conn, run))
    payload[field] = value
    with pytest.raises(ValueError, match='complete summary'):
        benchmark.save_result(conn, run['run_id'], payload, rows, final_check=weekcheck.publication_check)


def restore_result(conn, ident):
    db.run(conn, "UPDATE benchmark_runs SET status='RUNNING', published_at=NULL,validation_receipt=NULL "
                 'WHERE benchmark_id=:b', b=ident)
    for table in ('benchmark_summary', 'benchmark_branch_day'):
        db.run(conn, f'DELETE FROM {table} WHERE benchmark_id=:b', b=ident)


def test_two_connection_publish_race_has_exactly_one_winner(completed):
    with db.tx() as conn:
        finalize(conn, completed, refdata.get(completed['dataset_version']))
    gate, loser_done = Barrier(2), Event()
    def winner():
        with db.tx() as conn:
            benchmark._locked(conn, completed['run_id'])
            gate.wait(timeout=10)
            assert loser_done.wait(timeout=10)
            benchmark.publish(conn, completed['run_id'], final_check=weekcheck.publication_check)
    def loser():
        gate.wait(timeout=10)
        try:
            with db.tx() as conn:
                with pytest.raises(ValueError, match='already claimed'):
                    benchmark.publish(conn, completed['run_id'], final_check=weekcheck.publication_check)
                assert db.one(conn, 'SELECT 1 AS usable')['usable'] == 1
        finally:
            loser_done.set()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(winner), pool.submit(loser)
            a.result(timeout=20)
            b.result(timeout=20)
        with db.tx() as conn:
            row = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=completed['run_id'])
            assert row['status'] == 'PUBLISHED' and row['published_at'] is not None
    finally:
        with db.tx() as conn:
            restore_result(conn, completed['run_id'])


def claim_inputs(run):
    versions = benchmark.app_versions(run, refdata.get(run['dataset_version']))
    return dict(image_digest='test-only', git_commit='test-only', versions=versions,
                settings_digest=versions['settings'])


def step_claimed_day(conn, run, day, *, worker=None):
    run = db.one(conn, 'UPDATE demo_runs SET day=:d WHERE run_id=:r RETURNING *', d=day, r=run['run_id'])
    advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
    worlds = weekcheck.world_steps(conn, run, refdata.truth(run['dataset_version']))
    stats = {'completed': True, 'worlds': worlds}
    if worker is not None:
        stats['worker'] = worker
    benchmark.mark_day(conn, run['run_id'], day, .01, stats)
    return run


def test_resume_day_is_zero_without_checkpoints_and_seven_after_the_complete_week(bm, scratch):
    with db.tx() as conn:
        assert benchmark.resume_day(conn, bm['run_id']) == 0
    conn, run, _ = scratch
    assert benchmark.resume_day(conn, run['run_id']) == 7


@pytest.mark.parametrize('tamper', ['gap', 'marker', 'world', 'partial', 'outcomes', 'domain', 'simulation'])
def test_resume_refuses_inexact_checkpoint_evidence_without_mutating_it(scratch, tamper):
    conn, run, _ = scratch
    if tamper == 'gap':
        db.run(conn, 'DELETE FROM benchmark_days WHERE benchmark_id=:b AND day=3', b=run['run_id'])
    elif tamper == 'marker':
        db.run(conn, 'UPDATE benchmark_runs SET last_day_done=5 WHERE benchmark_id=:b', b=run['run_id'])
    elif tamper in ('world', 'partial'):
        stats = db.one(conn, 'SELECT stats FROM benchmark_days WHERE benchmark_id=:b AND day=3', b=run['run_id'])['stats']
        if tamper == 'world':
            stats['worlds'].pop('baseline_checked')
        else:
            stats['completed'] = False
        db.run(conn, 'UPDATE benchmark_days SET stats=CAST(:s AS jsonb) WHERE benchmark_id=:b AND day=3',
               b=run['run_id'], s=db.js(stats))
    elif tamper == 'outcomes':
        assert db.run(conn, 'UPDATE outcomes SET revenue=revenue+1 WHERE run_id=:r AND day=3', r=run['run_id']) > 0
    elif tamper == 'domain':
        db.run(conn, "UPDATE benchmark_runs SET expected_domain='{}' WHERE benchmark_id=:b", b=run['run_id'])
    else:
        db.run(conn, 'UPDATE demo_runs SET day=5 WHERE run_id=:r', r=run['run_id'])
    before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])
    days = db.rows(conn, 'SELECT * FROM benchmark_days WHERE benchmark_id=:b ORDER BY day', b=run['run_id'])
    with pytest.raises(ValueError, match='resume'):
        benchmark.resume_day(conn, run['run_id'])
    assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id']) == before
    assert db.rows(conn, 'SELECT * FROM benchmark_days WHERE benchmark_id=:b ORDER BY day', b=run['run_id']) == days


@pytest.mark.parametrize('changed', ['image_digest', 'git_commit', 'settings_digest', 'versions'])
def test_locked_resume_checks_the_running_build_and_preserves_refused_state(bm, changed):
    arguments = claim_inputs(bm)
    if changed == 'versions':
        arguments['versions'] = {**arguments['versions'], 'engine': 'different-engine'}
    else:
        arguments[changed] = 'different-build-or-input'
    with db.tx() as conn:
        before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=bm['run_id'])
        with pytest.raises(ValueError, match='start a fresh benchmark: python -m api.weekcheck benchmark start'):
            benchmark.claim_resume(conn, bm['run_id'], **arguments)
        assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=bm['run_id']) == before
        assert db.one(conn, 'SELECT 1 AS usable')['usable'] == 1


def test_failed_resume_checks_before_reviving_and_keeps_the_same_id(bm):
    with db.tx() as conn:
        benchmark.fail(conn, bm['run_id'], ['test-only interruption'])
        assert benchmark.claim_resume(conn, bm['run_id'], **claim_inputs(bm)) == 0
        row = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=bm['run_id'])
        assert row['status'] == 'RUNNING' and row['failure'] is None and row['validation_receipt'] is None
        assert row['benchmark_id'] == bm['run_id'] and row['published_at'] is None


@pytest.mark.parametrize('other_operation', ['resume', 'publish'])
def test_two_connection_resume_claim_serializes_a_real_day_and_preserves_loser_transaction(bm, other_operation):
    arguments = claim_inputs(bm)
    gate, loser_done = Barrier(2), Event()
    def winner():
        with db.tx() as conn:
            assert benchmark.claim_resume(conn, bm['run_id'], **arguments) == 0
            gate.wait(timeout=15)
            assert loser_done.wait(timeout=15)
            step_claimed_day(conn, bm, 0, worker='winner')
    def loser():
        gate.wait(timeout=15)
        try:
            with db.tx() as conn:
                with pytest.raises(ValueError, match='already claimed'):
                    if other_operation == 'resume':
                        benchmark.claim_resume(conn, bm['run_id'], **arguments)
                    else:
                        benchmark.publish(conn, bm['run_id'], final_check=weekcheck.publication_check)
                assert db.one(conn, 'SELECT 1 AS usable')['usable'] == 1
        finally:
            loser_done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(winner), pool.submit(loser)
        a.result(timeout=120)
        b.result(timeout=30)
    with db.tx() as conn:
        days = db.rows(conn, 'SELECT * FROM benchmark_days WHERE benchmark_id=:b ORDER BY day', b=bm['run_id'])
        assert [day['day'] for day in days] == [0] and days[0]['stats']['worker'] == 'winner'
        assert benchmark.claim_resume(conn, bm['run_id'], **arguments) == 1
        with pytest.raises(ValueError, match='next ordered day'):
            benchmark.mark_day(conn, bm['run_id'], 0, 99, {'worker': 'loser'})
        with pytest.raises(ValueError, match='VALIDATED'):
            benchmark.publish(conn, bm['run_id'], final_check=weekcheck.publication_check)
        assert db.rows(conn, 'SELECT * FROM benchmark_days WHERE benchmark_id=:b ORDER BY day', b=bm['run_id']) == days
        row = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=bm['run_id'])
        assert row['status'] == 'RUNNING' and row['published_at'] is None
        assert not db.rows(conn, 'SELECT * FROM benchmark_summary WHERE benchmark_id=:b', b=bm['run_id'])


@pytest.mark.parametrize('status', ['RUNNING', 'FAILED', 'TRIAL'])
def test_publish_refuses_every_unvalidated_state_without_changing_any_result(scratch, status):
    conn, run, _ = scratch
    db.run(conn, 'UPDATE benchmark_runs SET status=:s,trial=:t WHERE benchmark_id=:b',
           s=status, t=status == 'TRIAL', b=run['run_id'])
    before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id'])
    with pytest.raises(ValueError, match='VALIDATED'):
        benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=run['run_id']) == before
    assert not db.rows(conn, 'SELECT * FROM benchmark_summary WHERE benchmark_id=:b', b=run['run_id'])
    assert not db.rows(conn, 'SELECT * FROM benchmark_branch_day WHERE benchmark_id=:b', b=run['run_id'])


def test_failure_of_another_run_never_changes_the_previous_publication(bm, scratch):
    conn, completed, ref = scratch
    finalize(conn, completed, ref)
    benchmark.publish(conn, completed['run_id'], final_check=weekcheck.publication_check)
    before = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=completed['run_id'])
    payload = db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id=:b', b=completed['run_id'])
    benchmark.fail(conn, bm['run_id'], ['test-only failed later attempt'])
    assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b', b=completed['run_id']) == before
    assert db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id=:b', b=completed['run_id']) == payload


def test_interrupted_real_day_resumes_to_the_uninterrupted_week(bm, completed):
    arguments = claim_inputs(bm)
    with evalref.forecast_rule(bm['dataset_version']):
        for day in range(3):
            with db.tx() as conn:
                assert benchmark.claim_resume(conn, bm['run_id'], **arguments) == day
                step_claimed_day(conn, bm, day)
        with db.tx() as conn:
            records = benchmark.record_digest(conn, bm['run_id'])
            simulation = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=bm['run_id'])
            closing = weekcheck.closing_balance(conn, simulation)
        with pytest.raises(RuntimeError, match='test-only interruption after movement'):
            with db.tx() as conn:
                assert benchmark.claim_resume(conn, bm['run_id'], **arguments) == 3
                step_claimed_day(conn, bm, 3)
                raise RuntimeError('test-only interruption after movement')
        with db.tx() as conn:
            assert benchmark.resume_day(conn, bm['run_id']) == 3
            assert benchmark.record_digest(conn, bm['run_id']) == records
            assert db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=bm['run_id']) == simulation
            assert weekcheck.closing_balance(conn, simulation) == closing
            benchmark.fail(conn, bm['run_id'], ['test-only stopped process'])
        for day in range(3, 7):
            with db.tx() as conn:
                assert benchmark.claim_resume(conn, bm['run_id'], **arguments) == day
                step_claimed_day(conn, bm, day)
    with db.tx() as conn:
        resumed = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=bm['run_id'])
        expected = metrics.branch_day_rows(conn, completed['run_id'], weekcheck.BENCH_WORLDS)
        assert metrics.branch_day_rows(conn, bm['run_id'], weekcheck.BENCH_WORLDS) == expected
        assert weekcheck.closing_balance(conn, resumed) == weekcheck.closing_balance(conn, completed)
        finalize(conn, resumed, refdata.get(bm['dataset_version']))
        benchmark.publish(conn, bm['run_id'], final_check=weekcheck.publication_check)


def test_real_zero_activity_complete_week_can_publish(schema, tmp_path):
    """Versioned TEST artifact with zero inventory/demand, never a waived stand-in."""
    from data.versions import digest
    from tests.api.benchmark_fixtures import eligible_artifact
    root = eligible_artifact(tmp_path / 'zero-activity')
    manifest = json.loads((root / 'manifest.json').read_text())
    for name in ('lots0', 'inbound0', 'truth_physical', 'truth_demand'):
        part = manifest['files'][name]
        rows = list(csv.DictReader(gzip.open(root / part['path'], 'rt')))
        for row in rows:
            row['demand' if name == 'truth_demand' else 'qty'] = '0'
        buf = io.StringIO(newline='')
        writer = csv.DictWriter(buf, fieldnames=part['columns'], lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        payload = gzip.compress(buf.getvalue().encode(), mtime=0)
        (root / part['path']).write_bytes(payload)
        part['sha256'] = hashlib.sha256(payload).hexdigest()
    manifest['dataset_version'] = digest('ds-', {k: v for k, v in manifest.items() if k != 'dataset_version'})
    (root / 'manifest.json').write_text(json.dumps(manifest))
    dataset = refdata.install(root, allow_local=False)
    with db.tx() as conn:
        run = benchmark.create_run(conn, dataset, trial=False, git_commit='test-zero', image_digest='test-zero',
                                   worlds=weekcheck.BENCH_WORLDS)
    try:
        with evalref.forecast_rule(dataset):
            for day in range(7):
                with db.tx() as conn:
                    run = db.one(conn, 'UPDATE demo_runs SET day=:d WHERE run_id=:r RETURNING *',
                                 d=day, r=run['run_id'])
                    advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
                    worlds = weekcheck.world_steps(conn, run, refdata.truth(dataset))
                    assert all(w['completed'] and w['decisions'] == 0 for w in worlds.values())
                    benchmark.mark_day(conn, run['run_id'], day, .01, {'completed': True, 'worlds': worlds})
        with db.tx() as conn:
            rows, payload = finalize(conn, run, refdata.get(dataset))
            assert len(rows) == 4 * 7 * len(refdata.get(dataset).branches)
            assert all(all(row[f] == 0 for f in metrics.ROW_FIELDS[3:]) for row in rows)
            benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    finally:
        with db.tx() as conn:
            for table in ('benchmark_days', 'benchmark_observations', 'benchmark_branch_day', 'benchmark_summary',
                          'benchmark_transfer_audit', 'evaluation_check_history', 'benchmark_runs'):
                db.run(conn, f'DELETE FROM {table} WHERE benchmark_id=:r', r=run['run_id'])
            drop_runs(conn, [run['run_id']])


def test_published_tracer_is_current_through_http(completed, eligible_workspace, client):
    from tests.api.conftest import sign_in
    workspace = eligible_workspace()
    sign_in(client, 'HQ')
    current = client.get('/api/session').json()['run_id']
    with db.tx() as conn:
        db.run(conn, 'UPDATE sessions SET run_id=:new WHERE run_id=:old', new=workspace['run_id'], old=current)
        _, payload = finalize(conn, completed, refdata.get(completed['dataset_version']))
        benchmark.publish(conn, completed['run_id'], final_check=weekcheck.publication_check)
    try:
        response = client.get('/api/reports/benchmark')
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body['status'] == 'current'
        assert body['current']['benchmark_id'] == completed['run_id']
        assert body['current']['summary']['money']['steps'] == payload['money']['steps']
    finally:
        # Only this module's scratch result is restored, so subsequent independent omission tests can reuse it.
        with db.tx() as conn:
            restore_result(conn, completed['run_id'])
