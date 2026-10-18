"""The benchmark job from the command line (08-15; 08-CONTEXT D-02, D-21, D-27, D-34; 04-API-CONTRACT "## Reports (08)"
"Job command and output").

Every run uses the TEST ONLY eligible dataset `eligible_benchmark_dv` (benchmark_fixtures.py), a separately versioned
copy of the tiny public set with constructed non-local provider metadata; the stand-in `public_dv` is only ever the
negative case. IMAGE_DIGEST and GIT_COMMIT are clearly test-only values set on the environment. Each test removes the
benchmarks it made, so no other test sees a published result from here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api import benchmark, cases, db, refdata, weekcheck
from tests.api.benchmark_fixtures import (eligible_benchmark_dv, eligible_benchmark_workspace,  # noqa: F401
                                          ledger_oracle)
from tests.api.conftest import drop_runs, sign_in

TEST_IMAGE = 'test-only-image-digest-08-15'
TEST_COMMIT = 'test-only-commit-08-15'
RESULT_TABLES = ('benchmark_days', 'benchmark_observations', 'benchmark_branch_day', 'benchmark_summary',
                 'benchmark_transfer_audit', 'evaluation_check_history', 'benchmark_runs')
BANNED = ('http', 'gs://', '@', TEST_IMAGE)


def benchmark_ids() -> set[str]:
    with db.tx() as conn:
        return {r['benchmark_id'] for r in db.rows(conn, 'SELECT benchmark_id FROM benchmark_runs')}


def simulation_ids() -> set[str]:
    with db.tx() as conn:
        return {r['run_id'] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE run_id LIKE 'bm-%'")}


def drop_benchmarks(ids) -> None:
    with db.tx() as conn:
        for ident in ids:
            for table in RESULT_TABLES:
                column = 'sim_run_id' if table in ('benchmark_transfer_audit', 'evaluation_check_history') \
                    else 'benchmark_id'
                db.run(conn, f'DELETE FROM {table} WHERE {column} = :r', r=ident)
        drop_runs(conn, list(ids))


@pytest.fixture
def job(monkeypatch, capsys):
    """`job(*argv)` runs `weekcheck.main(argv)` and returns (exit code, the one report line parsed). It asserts the
    line is the only stdout, carries the versioned schema and no identifying value, then removes what it made."""
    monkeypatch.setenv('IMAGE_DIGEST', TEST_IMAGE)
    monkeypatch.setenv('GIT_COMMIT', TEST_COMMIT)
    made: set[str] = set()

    def call(*argv):
        before, sims = benchmark_ids(), simulation_ids()
        capsys.readouterr()
        try:
            code = weekcheck.main(list(argv))
        finally:
            made.update(benchmark_ids() - before, simulation_ids() - sims)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert len(lines) == 1, out
        for banned in BANNED:
            assert banned not in lines[0], (banned, lines[0])
        report = json.loads(lines[0])
        assert list(report)[:len(weekcheck.REPORT_KEYS)] == list(weekcheck.REPORT_KEYS)
        assert report['report_version'] == weekcheck.REPORT_VERSION == 1
        assert set(report['versions']) == set(benchmark.VERSION_KEYS)
        assert isinstance(report['image_digest_recorded'], bool) and isinstance(report['elapsed_s'], float)
        return code, report

    call.made = made
    yield call
    drop_benchmarks(made)


def row(ident):
    with db.tx() as conn:
        return db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :b', b=ident)


def key_tree(value):
    if isinstance(value, dict):
        return {k: key_tree(v) for k, v in value.items() if k != '_about'}
    if isinstance(value, list):
        return [key_tree(value[0])] if value else []
    return None


def direction(before, physical):
    if before is None:
        return 'gone'
    return 'up' if physical > before else 'down' if physical < before else 'none'


def bind_session(client, workspace_id, *, role='hq', branch=None):
    """Point this client's signed-in session at a workspace on the CLI's dataset (test-local session rewrite)."""
    current = client.get('/api/session').json()['run_id']
    with db.tx() as conn:
        db.run(conn, 'UPDATE sessions SET run_id = :new, role = :role, branch = :b WHERE run_id = :old',
               new=workspace_id, role=role, b=branch, old=current)


def test_start_runs_the_week_validates_publishes_and_reports_current(job, eligible_benchmark_dv,
                                                                     eligible_benchmark_workspace, client):
    dv = eligible_benchmark_dv
    ref = refdata.get(dv)
    code, report = job('benchmark', 'start', '--dataset', dv)
    assert code == 0, report
    ident = report['benchmark_id']
    assert benchmark.BENCHMARK_ID.fullmatch(ident)
    assert report['status'] == 'PUBLISHED' and report['validation'] == {'status': 'passed', 'errors': []}
    assert report['days_done'] == 7 and report['completed_world_days'] == 28 and report['reasons'] == []
    assert report['dataset_version'] == dv and report['git_commit'] == TEST_COMMIT
    assert report['image_digest_recorded'] is True
    assert report['versions'] == benchmark.app_versions({'dataset_version': dv}, ref)
    stored = row(ident)
    assert stored['image_digest'] == TEST_IMAGE  # kept privately in the database, never printed
    with db.tx() as conn:
        # seven ordered checkpoints, all four worlds completed on each, the simulation closed on day 6
        days = db.rows(conn, 'SELECT day, stats FROM benchmark_days WHERE benchmark_id = :b ORDER BY day', b=ident)
        assert [d['day'] for d in days] == list(range(7))
        assert all(d['stats']['completed'] is True and set(d['stats']['worlds']) == set(weekcheck.BENCH_WORLDS)
                   and all(w['completed'] is True for w in d['stats']['worlds'].values()) for d in days)
        sim = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id = :r', r=ident)
        assert (sim['kind'], sim['status'], sim['day']) == ('TEMPLATE', 'INITIALIZING', 6)
        # four-world counts: the complete branch x day domain per world, and matched outcome coverage
        branch_rows = db.rows(conn, 'SELECT * FROM benchmark_branch_day WHERE benchmark_id = :b', b=ident)
        assert len(branch_rows) == 4 * 7 * len(ref.branches)
        for world in weekcheck.BENCH_WORLDS:
            assert sum(r['scenario'] == world for r in branch_rows) == 7 * len(ref.branches)
        coverage = {r['world']: r['n'] for r in db.rows(
            conn, 'SELECT world, count(*) AS n FROM outcomes WHERE run_id = :r GROUP BY world', r=ident)}
        assert set(coverage) == set(weekcheck.BENCH_WORLDS) and len(set(coverage.values())) == 1
        # money, waste and trips: the saved summary against an independent derivation from the raw ledger
        payload = db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id = :b', b=ident)['payload']
        oracle = ledger_oracle(conn, ident)
        for key, value in oracle['net'].items():
            assert payload['money']['by_scenario'][key] == pytest.approx(value, abs=0.01), key
            assert sum(r['net'] for r in branch_rows if r['scenario'] == oracle['world'][key]) == \
                pytest.approx(value, abs=0.01)
        for key, value in oracle['wasted_units'].items():
            assert payload['waste']['totals'][key]['units'] == pytest.approx(value, abs=1e-6), key
        assert payload['trips'] == oracle['trips']
        assert payload['money']['steps'][-1]['value'] == pytest.approx(oracle['net']['shelfshift'], abs=0.01)
        assert payload['money']['steps'][0]['value'] == pytest.approx(oracle['net']['store_rule'], abs=0.01)
        # left-open reasons: the report's counts are the checkpoints' and the summary's
        assert report['left_open_by_reason'] == payload['decision_quality']['left_open']
        # each world's count correction comes from its own physical stock (D-10): the direction matches the record
        # and the physical value recorded for that world, and the comparison world's corrected lots are marked counted
        observations = db.rows(conn, 'SELECT day, lot_id, results FROM benchmark_observations WHERE benchmark_id = :b '
                                     'ORDER BY day, seq', b=ident)
        history = {(h['day'], h['world'], h['lot_id']): h['outcome'] for h in db.rows(
            conn, "SELECT day, world, lot_id, outcome FROM evaluation_check_history WHERE sim_run_id = :r "
                  "AND event_kind = 'observation'", r=ident)}
        assert observations, 'the week counted lots (D-11)'
        checked = 0
        for o in observations:
            for world in ('shelfshift', 'baseline_checked'):
                result = o['results'].get(world)
                if result is None:
                    continue
                checked += 1
                assert result['correction'] == direction(result['record_before'], result['physical']), o
                assert history[(o['day'], world, o['lot_id'])]['observed_units'] == (result['physical'] or 0.0)
                if world == 'baseline_checked' and result['correction'] != 'gone':
                    # a lot still on that world's record carries its own count; a sold-out lot left the record later
                    lot = db.one(conn, "SELECT verified_by, last_verified_day FROM lots WHERE run_id = :r "
                                       "AND world = 'baseline_checked' AND lot_id = :l", r=ident, l=o['lot_id'])
                    assert lot is None or (lot['verified_by'] == 'COUNT' and lot['last_verified_day'] >= o['day'])
        assert checked >= len(observations)
        # the selected plan: each commit is the plan the engine recommended on the version it committed
        committed = db.rows(conn, "SELECT case_id, payload FROM activity_log WHERE run_id = :r AND type = 'committed' "
                                  "ORDER BY seq", r=ident)
        assert committed, 'the commit loop acted during the week'
        for entry in committed:
            recommended = {cases.parse_plan(r['result']['recommended']).key for r in db.rows(
                conn, 'SELECT result FROM recommendations WHERE run_id = :r AND case_id = :c', r=ident,
                c=entry['case_id'])}
            assert entry['payload']['plan'] in recommended, entry
    # currentness through the Reports read, on a workspace of the same dataset
    workspace = eligible_benchmark_workspace()
    sign_in(client, 'HQ')
    bind_session(client, workspace['run_id'])
    body = client.get('/api/reports/benchmark').json()
    assert body['status'] == 'current', body
    current = body['current']
    assert current['benchmark_id'] == ident and current['window'] == {'first_day': 0, 'last_day': 6}
    assert current['versions'] == report['versions'] and current['differs'] == []
    assert current['summary'] == payload
    assert 'image_digest' not in json.dumps(body) and TEST_IMAGE not in json.dumps(body)
    fixture = json.loads((Path(__file__).parent / 'fixtures/reports_summary.json').read_text())
    for name in ('policy', 'assumptions'):  # dataset-defined leaves (as test_metrics' key-tree check)
        fixture[name] = current['summary'][name]
    for name in ('providers', 'input_counts', 'window'):
        fixture['forecast_quality'][name] = current['summary']['forecast_quality'][name]
    assert key_tree(current['summary']) == key_tree(fixture)  # supplemental to the amounts above
    # the pinned breakdown: a manager of one store sees its row first, under the header's benchmark id
    store = sorted(ref.branches)[-1]
    bind_session(client, workspace['run_id'], role='manager', branch=store)
    response = client.get('/api/reports/benchmark', params={'benchmark_id': ident, 'breakdown': 'branch',
                                                            'chart': 'money'})
    assert response.status_code == 200, response.json()
    breakdown = response.json()['current']['breakdown']
    assert breakdown['benchmark_id'] == ident and breakdown['total'] == len(ref.branches)
    assert breakdown['rows'][0]['branch'] == store and breakdown['rows'][0]['pinned'] is True
    assert all(r['pinned'] is False for r in breakdown['rows'][1:])
    for r in breakdown['rows']:
        mine = [b for b in branch_rows if b['branch'] == r['branch']]
        assert r['shelfshift'] == pytest.approx(sum(b['net'] for b in mine if b['scenario'] == 'shelfshift'),
                                                abs=0.01)


def test_the_stand_in_dataset_is_refused_and_creates_no_benchmark(job, public_dv):
    before = benchmark_ids()
    code, report = job('benchmark', 'start', '--dataset', public_dv)
    assert code == 2 and report['status'] == 'REFUSED' and report['benchmark_id'] is None
    assert 'forecasts come from the local stand-in' in report['reasons']
    assert report['dataset_version'] == public_dv and benchmark_ids() == before


def test_a_full_run_without_image_digest_is_refused_before_any_work(job, eligible_benchmark_dv, monkeypatch):
    monkeypatch.delenv('IMAGE_DIGEST')
    before, sims = benchmark_ids(), simulation_ids()
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv)
    assert code == 2 and report['status'] == 'REFUSED'
    assert report['reasons'] == ['IMAGE_DIGEST is not set: a full run records the image it ran on (D-21)']
    assert report['image_digest_recorded'] is False
    assert benchmark_ids() == before and simulation_ids() == sims


@pytest.mark.parametrize('argv,reason', [
    (('benchmark', 'start', '--dataset', 'ds-000000000000'), 'dataset ds-000000000000 is not installed'),
    (('benchmark', 'start', '--dataset', '{dv}', '--stores', '2'), '--minutes and --stores are trial only'),
    (('benchmark', 'start', '--dataset', '{dv}', '--minutes', '5'), '--minutes and --stores are trial only'),
    (('benchmark', 'start', '--dataset', '{dv}', '--trial'), 'a trial needs --minutes N'),
    (('benchmark', 'resume', 'bm-000000000000'), 'unknown benchmark'),
    (('benchmark', 'resume', 'not-an-id'), 'a benchmark id is bm- and 12 lowercase hex'),
    (('benchmark',), 'usage:'),
    (('report',), 'usage:'),  # bare `python -m api.weekcheck` is now the five-seed check (08-16, RPT-05)
])
def test_refusals_exit_two_with_one_report_line_and_change_nothing(job, eligible_benchmark_dv, argv, reason):
    before, sims = benchmark_ids(), simulation_ids()
    code, report = job(*(a.replace('{dv}', eligible_benchmark_dv) for a in argv))
    assert code == 2 and report['status'] == 'REFUSED', report
    assert any(r.startswith(reason) for r in report['reasons']), report['reasons']
    assert benchmark_ids() == before and simulation_ids() == sims


def test_a_second_start_is_a_new_id_and_the_first_stays_published_until_the_second_publishes(
        job, eligible_benchmark_dv, monkeypatch):
    code, first = job('benchmark', 'start', '--dataset', eligible_benchmark_dv)
    assert code == 0, first
    published = row(first['benchmark_id'])
    with db.tx() as conn:
        saved = db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id = :b',
                       b=first['benchmark_id'])['payload']
    seen = []
    real = benchmark.publish

    def watched(conn, ident, **kwargs):
        # just before the second publishes: the first is the only published one, unchanged
        seen.append(ident)
        latest = db.one(conn, "SELECT benchmark_id FROM benchmark_runs WHERE status = 'PUBLISHED' "
                              "AND dataset_version = :v ORDER BY published_at DESC LIMIT 1", v=eligible_benchmark_dv)
        assert latest['benchmark_id'] == first['benchmark_id']
        assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :b',
                      b=first['benchmark_id']) == published
        return real(conn, ident, **kwargs)
    monkeypatch.setattr(benchmark, 'publish', watched)
    code, second = job('benchmark', 'start', '--dataset', eligible_benchmark_dv)
    assert code == 0, second
    assert second['benchmark_id'] != first['benchmark_id'] and seen == [second['benchmark_id']]
    assert row(first['benchmark_id']) == published  # immutable after the second publishes too
    with db.tx() as conn:
        assert db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id = :b',
                      b=first['benchmark_id'])['payload'] == saved
    # a resume of a PUBLISHED id is refused and changes nothing
    code, refused = job('benchmark', 'resume', first['benchmark_id'])
    assert code == 2 and refused['status'] == 'REFUSED'
    assert any('status must be RUNNING or FAILED' in r for r in refused['reasons'])
    assert row(first['benchmark_id']) == published


def test_a_stopped_run_fails_with_its_reason_and_resume_publishes_it(job, eligible_benchmark_dv, monkeypatch):
    real = weekcheck.world_steps

    def failing(conn, run, truth, *args, **kwargs):
        if run['day'] == 3:
            raise RuntimeError('TEST: infrastructure failure inside day 3')
        return real(conn, run, truth, *args, **kwargs)
    monkeypatch.setattr(weekcheck, 'world_steps', failing)
    code, stopped = job('benchmark', 'start', '--dataset', eligible_benchmark_dv)
    assert code == 1 and stopped['status'] == 'FAILED' and stopped['days_done'] == 3, stopped
    assert stopped['reasons'] == ['stopped: RuntimeError: TEST: infrastructure failure inside day 3']
    ident = stopped['benchmark_id']
    monkeypatch.setattr(weekcheck, 'world_steps', real)
    # another build is refused before any work, with the fresh-run path
    monkeypatch.setenv('GIT_COMMIT', 'another-test-commit')
    before = row(ident)
    code, refused = job('benchmark', 'resume', ident)
    assert code == 2 and refused['benchmark_id'] == ident and refused['days_done'] == 3
    assert any('git commit differs' in r and 'start a fresh benchmark' in r for r in refused['reasons'])
    assert row(ident) == before
    monkeypatch.setenv('GIT_COMMIT', TEST_COMMIT)
    code, resumed = job('benchmark', 'resume', ident)
    assert code == 0 and resumed['status'] == 'PUBLISHED' and resumed['benchmark_id'] == ident, resumed
    assert resumed['days_done'] == 7 and resumed['validation']['status'] == 'passed'


def test_a_forecast_gap_stops_the_run_with_its_text_and_publishes_nothing(job, eligible_benchmark_dv, monkeypatch):
    from api import evalref
    real = weekcheck.benchmark_day

    def gap(conn, run, bench, truth, settings, **kwargs):
        if (bench['last_day_done'] or 0) >= 1:
            raise evalref.ForecastGap('S-test', 'P-test', 2, 'run-test')
        return real(conn, run, bench, truth, settings, **kwargs)
    monkeypatch.setattr(weekcheck, 'benchmark_day', gap)
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv)
    assert code == 1 and report['status'] == 'FAILED' and report['days_done'] == 2, report
    assert report['reasons'] == ['stopped: forecast gap: store S-test, product P-test, day 2, run run-test']
    assert row(report['benchmark_id'])['failure'].startswith('stopped: forecast gap')
    assert row(report['benchmark_id'])['published_at'] is None


def test_a_validation_failure_exits_one_and_publishes_nothing(job, eligible_benchmark_dv, monkeypatch):
    monkeypatch.setattr(weekcheck, 'trips_paid_once', lambda conn, run_id, world='shelfshift': ['TEST: unpaid trip'])
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv)
    assert code == 1 and report['status'] == 'FAILED' and report['days_done'] == 7, report
    assert report['validation']['status'] == 'failed'
    assert any('TEST: unpaid trip' in e for e in report['validation']['errors'])
    assert row(report['benchmark_id'])['published_at'] is None


# ------------------------------------------------------------------------------------------- the trial (Task 2)

TRIAL_REPORT_KEYS = ('minutes', 'slice', 'setup_s', 'setup_complete', 'measured_s', 'cleanup_s', 'stopped',
                     'stopped_in', 'partial', 'per_day', 'stages', 'cases_evaluated', 'blocked_by_reason',
                     'rows_per_day', 'peak_rss_mib', 'worker_peak_rss_mib', 'setup')
DAY_KEYS = ('day', 'elapsed_s', 'db_s', 'statements', 'rows', 'stores_screened', 'lots_screened', 'cases_opened',
            'decisions', 'confirmations', 'commits', 'cases_evaluated', 'partial', 'stages')
STAGE_NAMES = ('start', 'checks', 'commits', 'before_worlds', 'worlds', 'after_worlds', 'closing', 'checkpoint')
EXPIRY = 'commitment_blocked:required_check:ESTIMATED_EXPIRY'


def trial_report(report):
    assert report['status'] == 'TRIAL', report
    assert set(TRIAL_REPORT_KEYS) <= set(report), set(TRIAL_REPORT_KEYS) - set(report)
    assert list(report['stages']) == list(STAGE_NAMES)
    assert next(iter(report['blocked_by_reason'])) == EXPIRY  # D-34: the missing-expiry-check reason first
    for entry in report['blocked_by_reason'].values():
        assert set(entry) == {'cases', 'pct', 'transfer_units'}
    for day in report['per_day']:
        assert set(DAY_KEYS) <= set(day) and list(day['stages']) == list(STAGE_NAMES)
    assert report['validation']['status'] == 'not_run' and report['peak_rss_mib'] > 0
    return report


@pytest.fixture
def clock(monkeypatch):
    """The trial's deadline clock under test control: `clock.trip()` moves it past any budget."""
    state = {'now': 0.0}
    monkeypatch.setattr(weekcheck, '_clock', lambda: state['now'], raising=False)

    class Clock:
        @staticmethod
        def trip():
            state['now'] = 1e12
    return Clock


def listeners():
    engine = db.engine()
    return len(engine.dispatch.before_cursor_execute), len(engine.dispatch.after_cursor_execute)


def test_a_zero_minute_trial_stops_before_day_zero_with_every_counter_zero(job, eligible_benchmark_dv):
    before, sims, hooks = benchmark_ids(), simulation_ids(), listeners()
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '0')
    assert code == 0
    trial_report(report)
    assert report['days_done'] == 0 and report['per_day'] == [] and report['rows_per_day'] == []
    assert report['cases_evaluated'] == 0 and report['minutes'] == 0 and report['slice'] is None
    assert all(v == 0 for stage in report['stages'].values() for v in stage.values())
    assert report['blocked_by_reason'] == {EXPIRY: {'cases': 0, 'pct': 0.0, 'transfer_units': 0}}
    assert isinstance(report['setup_s'], float) and report['setup_complete'] is False
    assert report['benchmark_id'] is None and report['stopped'] == 'budget'
    assert benchmark_ids() == before and simulation_ids() == sims and listeners() == hooks


def test_a_generous_trial_measures_the_whole_week_and_can_never_publish(job, eligible_benchmark_dv):
    hooks = listeners()
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '60')
    assert code == 0
    trial_report(report)
    ident = report['benchmark_id']
    assert report['stopped'] == 'complete' and report['partial'] is False and report['setup_complete'] is True
    assert report['days_done'] == 7 and [d['day'] for d in report['per_day']] == list(range(7))
    assert all(d['partial'] is False and d['statements'] > 0 and d['db_s'] > 0 and d['elapsed_s'] > 0
               for d in report['per_day'])
    assert [r['day'] for r in report['rows_per_day']] == list(range(7)) and all(r['rows'] > 0
                                                                              for r in report['rows_per_day'])
    assert report['setup']['statements'] > 0 and report['setup_s'] > 0
    assert listeners() == hooks and db.engine().pool.checkedout() == 0
    stored = row(ident)
    assert stored['status'] == 'TRIAL' and stored['trial'] is True and stored['published_at'] is None
    assert stored['stats']['trial_report'] == report  # the printed report is the one saved
    with db.tx() as conn:
        # the measured counters against the saved checkpoints and rows, independently
        days = [d['stats'] for d in db.rows(conn, 'SELECT stats FROM benchmark_days WHERE benchmark_id = :b '
                                                  'ORDER BY day', b=ident)]
        assert report['cases_evaluated'] == sum(len(d['order']) for d in days)
        assert sum(d['commits'] for d in report['per_day']) == sum(len(d['committed']) for d in days)
        assert sum(d['confirmations'] for d in report['per_day']) == sum(d['checks']['confirmed'] for d in days)
        left = {}
        for d in days:
            for reason, ids in d['left_open'].items():
                left[reason] = left.get(reason, 0) + len(ids)
        assert report['left_open_by_reason'] == left
        assert sum(e['cases'] for e in report['blocked_by_reason'].values()) == sum(left.values())
        opened = db.one(conn, 'SELECT count(*) AS n FROM cases WHERE run_id = :r', r=ident)['n']
        assert report['setup']['cases_opened'] + sum(d['cases_opened'] for d in report['per_day']) == opened
        versions = db.one(conn, 'SELECT count(*) AS n FROM recommendations WHERE run_id = :r', r=ident)['n']
        assert report['setup']['decisions'] + sum(d['decisions'] for d in report['per_day']) == versions
        # T-08-28: a trial is never publishable, and publishing refuses without changing anything
        with pytest.raises(ValueError, match='VALIDATED'):
            benchmark.publish(conn, ident, final_check=weekcheck.publication_check)
    assert row(ident) == stored
    code, refused = job('benchmark', 'resume', ident)
    assert code == 2 and refused['status'] == 'REFUSED' and row(ident) == stored


def wrap_trip(monkeypatch, module, name, clock, when=lambda *a, **k: True):
    real = getattr(module, name)

    def tripping(*args, **kwargs):
        if when(*args, **kwargs):
            clock.trip()  # the deadline passes here: the next SQL statement is refused
        return real(*args, **kwargs)
    monkeypatch.setattr(module, name, tripping)


@pytest.mark.parametrize('where', ['statement', 'confirmation', 'commit'])
def test_a_deadline_inside_a_day_reports_its_partial_counters_and_rolls_the_day_back(
        job, eligible_benchmark_dv, monkeypatch, clock, where):
    from api import advance, evalcommit, signals
    hooks = listeners()
    if where == 'statement':
        wrap_trip(monkeypatch, advance, 'step_world', clock, when=lambda conn, run, *a, **k: run['day'] == 1)
        stage = 'worlds'
    elif where == 'confirmation':
        wrap_trip(monkeypatch, signals, 'confirm', clock)
        stage = 'checks'
    else:
        wrap_trip(monkeypatch, evalcommit, '_keep', clock)
        wrap_trip(monkeypatch, evalcommit, '_through_app', clock)
        stage = 'commits'
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '60')
    assert code == 0
    trial_report(report)
    assert report['stopped'] == 'budget' and report['partial'] is True and report['setup_complete'] is True
    stopped = report['per_day'][-1]
    assert stopped['partial'] is True and report['stopped_in'] == {'day': stopped['day'], 'stage': stage}
    assert stopped['statements'] > 0 and stopped['stages'][stage]['statements'] >= 0
    assert all(d['partial'] is False for d in report['per_day'][:-1])
    assert report['days_done'] == stopped['day'] == len(report['per_day']) - 1
    assert listeners() == hooks and db.engine().pool.checkedout() == 0
    ident, d = report['benchmark_id'], stopped['day']
    with db.tx() as conn:  # nothing of the stopped day remains
        assert [r['day'] for r in db.rows(conn, 'SELECT day FROM benchmark_days WHERE benchmark_id = :b ORDER BY day',
                                          b=ident)] == list(range(d))
        assert db.one(conn, 'SELECT day FROM demo_runs WHERE run_id = :r', r=ident)['day'] == max(d - 1, 0)
        for table, key in (('outcomes', 'run_id'), ('evaluation_check_history', 'sim_run_id'),
                           ('benchmark_observations', 'benchmark_id')):
            assert not db.rows(conn, f'SELECT 1 FROM {table} WHERE {key} = :r AND day >= :d', r=ident, d=d), table
        if d > 0:
            assert not db.rows(conn, 'SELECT 1 FROM activity_log WHERE run_id = :r AND day >= :d', r=ident, d=d)
    assert row(ident)['stats']['trial_report'] == report


def test_a_deadline_during_setup_saves_nothing_and_still_reports(job, eligible_benchmark_dv, monkeypatch, clock):
    from api import advance
    wrap_trip(monkeypatch, advance, 'cases_morning', clock)  # the day-0 morning inside the setup
    before, sims, hooks = benchmark_ids(), simulation_ids(), listeners()
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '60')
    assert code == 0
    trial_report(report)
    assert report['stopped_in'] == {'day': None, 'stage': 'setup'} and report['partial'] is True
    assert report['setup_complete'] is False and report['benchmark_id'] is None and report['per_day'] == []
    assert report['setup']['statements'] > 0  # what the setup did before it stopped, read before its rollback
    assert benchmark_ids() == before and simulation_ids() == sims
    assert listeners() == hooks and db.engine().pool.checkedout() == 0


def test_a_deadline_at_a_day_boundary_stops_cleanly(job, eligible_benchmark_dv, monkeypatch, clock):
    real = benchmark.mark_day

    def mark(conn, ident, day, *args, **kwargs):
        real(conn, ident, day, *args, **kwargs)
        if day == 1:
            clock.trip()  # after day 1's checkpoint, before the next day claims anything
    monkeypatch.setattr(benchmark, 'mark_day', mark)
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '60')
    assert code == 0
    trial_report(report)
    assert report['stopped'] == 'budget' and report['stopped_in'] == {'day': 2, 'stage': 'boundary'}
    assert report['partial'] is False and report['days_done'] == 2
    assert [d['day'] for d in report['per_day']] == [0, 1] and not any(d['partial'] for d in report['per_day'])


def test_a_store_slice_is_trial_only_and_restricts_both_strategies(job, eligible_benchmark_dv):
    ref = refdata.get(eligible_benchmark_dv)
    codes = sorted(ref.branches)
    for bad in ('0', str(len(codes) + 1)):
        code, refused = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '1',
                            '--stores', bad)
        assert code == 2 and refused['status'] == 'REFUSED', refused
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '60',
                       '--stores', '2')
    assert code == 0
    trial_report(report)
    assert report['slice'] == {'stores': 2, 'of': len(codes), 'codes': codes[:2]}
    assert all(d['stores_screened'] <= 2 for d in report['per_day']) and report['setup']['stores_screened'] <= 2
    with db.tx() as conn:
        sources = {r['source_branch'] for r in db.rows(conn, 'SELECT source_branch FROM cases WHERE run_id = :r',
                                                       r=report['benchmark_id'])}
        rule = {r['origin'] for r in db.rows(conn, "SELECT origin FROM world_actions WHERE run_id = :r "
                                                   "AND world IN ('baseline_rule', 'baseline_checked') "
                                                   "AND case_id IS NULL", r=report['benchmark_id'])}
    assert sources and sources <= set(codes[:2]) and rule <= set(codes[:2])
    assert row(report['benchmark_id'])['status'] == 'TRIAL'


def test_a_trial_needs_no_image_digest_and_records_none(job, eligible_benchmark_dv, monkeypatch):
    """Written after the implementation (no RED): only a full run requires IMAGE_DIGEST (contract exit 2)."""
    monkeypatch.delenv('IMAGE_DIGEST')
    code, report = job('benchmark', 'start', '--dataset', eligible_benchmark_dv, '--trial', '--minutes', '60')
    assert code == 0
    trial_report(report)
    assert report['image_digest_recorded'] is False and row(report['benchmark_id'])['image_digest'] is None
