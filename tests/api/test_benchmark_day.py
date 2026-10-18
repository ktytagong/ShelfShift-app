"""One simulated day per transaction in D-11's order, checkpoints by day, resume equals an uninterrupted run (08-14;
08-CONTEXT D-08, D-11, D-21; research Pitfalls 6 and 8).

Every scenario runs on the TEST ONLY eligible tiny public set in benchmark simulations of their own. Runs are compared
on deterministic keys only (world actions by action id, cases by case key, plans by plan key, outcome tables), never
request, fact or reservation ids, and never elapsed times.
"""
from __future__ import annotations

import pytest

from api import benchmark, db, refdata, weekcheck
from tests.api.benchmark_fixtures import eligible_dv  # noqa: F401
from tests.api.test_checked_morning import drop
from tests.api.test_evalcommit import snapshot

BUILD = {'image_digest': 'test-only', 'git_commit': 'test-only'}  # what bm runs are created with


def new_run(dv):
    with db.tx() as conn:
        return benchmark.create_run(conn, dv, trial=False, worlds=weekcheck.BENCH_WORLDS, **BUILD)


@pytest.fixture
def runs(eligible_dv):
    made = []

    def make():
        made.append(new_run(eligible_dv))
        return made[-1]
    yield make
    for run in made:
        drop(run)


def sim(conn, run):
    return db.one(conn, 'SELECT * FROM demo_runs WHERE run_id = :r', r=run['run_id'])


def days(conn, run):
    return db.rows(conn, 'SELECT day, stats FROM benchmark_days WHERE benchmark_id = :b ORDER BY day', b=run['run_id'])


def everything(run):
    """The whole simulated state and its history by deterministic keys."""
    with db.tx() as conn:
        out = snapshot(conn, run)
        out['inbound'] = db.rows(conn, 'SELECT world, inbound_id, branch, sku, qty, arrival_day, arrival_min, '
                                       'expiry_day, source_case_id, origin FROM inbound WHERE run_id = :r '
                                       'ORDER BY world, inbound_id', r=run['run_id'])
        # outcomes_digest (08-12's mark_day) hashes the outcome rows with their run id, so it differs between two
        # benchmark ids by construction; the outcome rows themselves are compared above
        out['days'] = [{'day': d['day'], **{k: v for k, v in d['stats'].items()
                                             if k not in ('elapsed_s', 'outcomes_digest')}} for d in days(conn, run)]
        out['observations'] = db.rows(conn, 'SELECT day, seq, lot_id, branch, case_id, rec_version FROM '
                                            'benchmark_observations WHERE benchmark_id = :b ORDER BY day, seq',
                                      b=run['run_id'])
        out['log'] = db.rows(conn, 'SELECT day, type, case_id FROM activity_log WHERE run_id = :r ORDER BY seq',
                             r=run['run_id'])
        out['sim'] = {k: sim(conn, run)[k] for k in ('day', 'status', 'next_case_no')}
    return out


def test_seven_days_each_checkpointed_and_closing_after_day_six(runs):
    run = runs()
    result = weekcheck.run_days(run['run_id'], through=6, **BUILD)
    with db.tx() as conn:
        assert [d['day'] for d in days(conn, run)] == list(range(7)) == [s['day'] for s in result]
        assert db.one(conn, 'SELECT last_day_done FROM benchmark_runs WHERE benchmark_id = :b',
                      b=run['run_id'])['last_day_done'] == 6
        assert sim(conn, run)['day'] == 6  # no day-7 start (Pitfall 8)
        assert not db.rows(conn, "SELECT seq FROM activity_log WHERE run_id = :r AND type = 'day_advanced' "
                                 "AND (payload->>'to_day')::int > 6", r=run['run_id'])
        stored = days(conn, run)
        assert all(d['stats']['completed'] is True and set(d['stats']['worlds']) == set(weekcheck.BENCH_WORLDS)
                   for d in stored)
        assert stored[6]['stats']['closing'] == weekcheck.closing_balance(conn, sim(conn, run))
        assert all('closing' not in d['stats'] for d in stored[:6])
        assert sum(len(d['stats']['committed']) for d in stored) > 0  # the commit loop acted during the week
        assert all(set(d['stats']) >= {'checks', 'left_open', 'retries', 'order_digest', 'transfer_units'}
                   for d in stored)
        # Pitfall 4: the week runs inside the D-08 forecast rule, so a case opened on days 4..6 has a real version
        late = db.rows(conn, 'SELECT case_id, missing_data, active_rec_version FROM cases WHERE run_id = :r '
                             'AND opened_day >= 4', r=run['run_id'])
        assert late and all(c['missing_data'] is None and c['active_rec_version'] for c in late)
    # a later run_days on the finished week does nothing
    assert weekcheck.run_days(run['run_id'], through=6, **BUILD) == []


def test_two_runs_from_the_same_inputs_are_identical(runs):
    a, b = runs(), runs()
    weekcheck.run_days(a['run_id'], through=6, **BUILD)
    weekcheck.run_days(b['run_id'], through=6, **BUILD)
    assert everything(a) == everything(b)


def test_a_run_stopped_after_day_two_and_resumed_equals_an_uninterrupted_run(runs):
    a, b = runs(), runs()
    weekcheck.run_days(a['run_id'], through=6, **BUILD)
    assert [s['day'] for s in weekcheck.run_days(b['run_id'], through=2, **BUILD)] == [0, 1, 2]
    with db.tx() as conn:
        assert [d['day'] for d in days(conn, b)] == [0, 1, 2] and sim(conn, b)['day'] == 2
    assert [s['day'] for s in weekcheck.run_days(b['run_id'], through=6, **BUILD)] == [3, 4, 5, 6]
    assert everything(a) == everything(b)


def test_an_error_inside_day_three_rolls_back_the_whole_day_and_resume_redoes_it(runs, monkeypatch):
    a, b = runs(), runs()
    weekcheck.run_days(a['run_id'], through=6, **BUILD)
    real = weekcheck.world_steps

    def failing(conn, run, truth, *args, **kwargs):
        out = real(conn, run, truth, *args, **kwargs)  # the day's world rows are written, then the day fails
        if run['day'] == 3:
            raise RuntimeError('TEST: infrastructure failure inside day 3')
        return out
    monkeypatch.setattr(weekcheck, 'world_steps', failing)
    with pytest.raises(RuntimeError, match='inside day 3'):
        weekcheck.run_days(b['run_id'], through=6, **BUILD)
    monkeypatch.undo()
    with db.tx() as conn:
        assert [d['day'] for d in days(conn, b)] == [0, 1, 2] and sim(conn, b)['day'] == 2
        for table, column in (('outcomes', 'day'), ('activity_log', 'day'), ('evaluation_check_history', 'day')):
            key = 'sim_run_id' if table == 'evaluation_check_history' else 'run_id'
            assert not db.rows(conn, f'SELECT 1 FROM {table} WHERE {key} = :r AND {column} >= 3', r=b['run_id']), table
    assert [s['day'] for s in weekcheck.run_days(b['run_id'], through=6, **BUILD)] == [3, 4, 5, 6]
    assert everything(a) == everything(b)


@pytest.mark.parametrize('field', ['image_digest', 'git_commit'])
def test_a_resume_on_another_build_is_refused_with_the_fresh_run_path_and_writes_nothing(runs, field):
    run = runs()
    weekcheck.run_days(run['run_id'], through=0, **BUILD)
    before = everything(run)
    with db.tx() as conn:
        row = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :b', b=run['run_id'])
    with pytest.raises(ValueError) as refused:
        weekcheck.run_days(run['run_id'], through=6, **{**BUILD, field: 'another-build'})
    assert 'start a fresh benchmark: python -m api.weekcheck benchmark start --dataset' in str(refused.value)
    assert ('image digest' if field == 'image_digest' else 'git commit') in str(refused.value)
    assert everything(run) == before
    with db.tx() as conn:
        assert db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id = :b', b=run['run_id']) == row



def test_the_composed_week_equals_the_apps_normal_flow_day_by_day(runs, eligible_dv):
    """The oracle: the same week driven by the app's own commit path (`sandbox.autopilot`) in advance_day's order
    (checked morning, commits, hold expiry and the due transitions, both store rules and the world steps, the
    lifecycle, the next day's start and eager morning). World rows (action and event order included), physical
    lots, reservations and case states equal run_days' after the whole week."""
    from api import evalref
    from tests.api.test_evalcommit import day, next_day, slow, snapshot as world
    a, b = runs(), runs()
    weekcheck.run_days(a['run_id'], through=6, **BUILD)
    truth = refdata.truth(eligible_dv)
    with evalref.forecast_rule(eligible_dv):
        with db.tx() as conn:
            run = b
            while True:
                day(conn, run, truth, slow)
                if run['day'] == 6:
                    break
                run = next_day(conn, run)
    with db.tx() as conn:
        assert world(conn, a) == world(conn, b)
