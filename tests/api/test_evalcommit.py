"""The benchmark's commit loop (08-14; 08-CONTEXT D-26, D-29; research Q5, Q6).

Every scenario runs on the TEST ONLY eligible tiny public set (tests/api/benchmark_fixtures.py) in a benchmark
simulation of its own. Two simulations made from the same inputs are compared on deterministic keys only: world
actions by action id, cases by case key, plans by plan key (never request or fact ids, research Pitfall 6). The app's
own commit path, `sandbox.autopilot`, is the slow path; `evalcommit.commit_all` with the Keep fast path is compared
against it. Refusals are produced by real app calls (a lot hold placed by the lot's manager) or, for the savepoint
regressions, by a refusal raised right after the real service call has written its rows.
"""
from __future__ import annotations

import pytest

from api import advance, bookings, db, evalcommit, evalref, lotholds, refdata, sandbox, weekcheck, workflow
from api.errors import ApiError
from tests.api.benchmark_fixtures import eligible_dv  # noqa: F401
from tests.api.test_benchmark import rows
from tests.api.test_checked_morning import drop
from tests.api.conftest import seat

WORLD_TABLES = ('outcomes', 'world_actions', 'world_events', 'lots', 'truth_lots')
# every case column a commit or a day changes, by value; the owner columns, decided_ms and timestamps are left out
# (the fast path takes no ownership and records no decision time: nobody decided)
CASE_COLUMNS = ('case_id', 'key', 'lot_id', 'source_branch', 'sku', 'tag', 'state', 'active_rec_version', 'round',
                'opened_day', 'look_round', 'waiting_until_day', 'reason', 'hold_expired', 're_review', 'adopted',
                'missing_data', 'plan_dest', 'selected', 'closed_as', 'cancel_reason', 'fingerprint', 'lot_status',
                'predecessor', 'considered')
KEEP_DAY, LAST_DAY = 3, 6  # the tiny set's day 3 opens a Keep (SUS-1) before a transfer (SUS-4); day 6 three Keeps


def new_run(dv):
    from api import benchmark
    with db.tx() as conn:
        return benchmark.create_run(conn, dv, trial=False, git_commit='test-only', image_digest='test-only',
                                    worlds=weekcheck.BENCH_WORLDS)


@pytest.fixture
def pair(eligible_dv):
    runs = [new_run(eligible_dv), new_run(eligible_dv)]
    yield runs
    for run in runs:
        drop(run)


@pytest.fixture
def one(eligible_dv):
    run = new_run(eligible_dv)
    yield run
    drop(run)


def slow(conn, run):
    sandbox.autopilot(conn, run)


def fast(conn, run):
    return evalcommit.commit_all(conn, run, retry_limit=2)


def day(conn, run, truth, commit):
    """One simulated day as D-11 step 6 orders it (test-local; weekcheck.benchmark_day composes it in Task 3)."""
    weekcheck.checked_morning(conn, run, bench_id=run['run_id'])
    result = commit(conn, run)
    bookings.expire_holds(conn, run)
    advance._lifecycle_before(conn, run)
    weekcheck.world_steps(conn, run, truth)
    advance._lifecycle_after(conn, run)
    return result


def next_day(conn, run):
    run = db.one(conn, 'UPDATE demo_runs SET day = day + 1 WHERE run_id = :r RETURNING *', r=run['run_id'])
    advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
    advance.cases_morning(conn, run, eager=True)
    return run


def to_day(conn, run, truth, target):
    """Days before `target` through the app's own commit path (the slow path), then target's morning."""
    while run['day'] < target:
        day(conn, run, truth, slow)
        run = next_day(conn, run)
    return run


def snapshot(conn, run):
    out = {t: {w: rows(conn, run, t, w) for w in weekcheck.BENCH_WORLDS} for t in WORLD_TABLES}
    out['reservations'] = sorted(({k: v for k, v in r.items() if k not in ('run_id', 'res_id')}
                                  for r in db.rows(conn, 'SELECT * FROM reservations WHERE run_id = :r',
                                                   r=run['run_id'])), key=repr)
    out['cases'] = [{c: r[c] for c in CASE_COLUMNS}
                    for r in db.rows(conn, 'SELECT * FROM cases WHERE run_id = :r ORDER BY case_id', r=run['run_id'])]
    return out


def open_plans(conn, run):
    from api import cases
    return {c['case_id']: cases.active_rec(conn, c)['result']['recommended']
            for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' "
                                   "AND active_rec_version IS NOT NULL ORDER BY case_id", r=run['run_id'])}


def test_keep_fast_path_and_the_app_path_give_identical_outcomes(pair, eligible_dv):
    """D-26 acceptance on a fixed day (day 3: a Keep, then a transfer) and on to the window's end (day 6: three
    Keeps): the app's own path and commit_all give identical outcomes, world actions, world events, lots, physical
    lots, case states and reservations after each day's world steps."""
    truth = refdata.truth(eligible_dv)
    a, b = pair
    with evalref.forecast_rule(eligible_dv):
        with db.tx() as conn:
            a, b = to_day(conn, a, truth, KEEP_DAY), to_day(conn, b, truth, KEEP_DAY)
            for run in (a, b):  # the day's checked morning first (asked again inside day(), it asks nothing)
                weekcheck.checked_morning(conn, run, bench_id=run['run_id'])
            plans = open_plans(conn, a)
            assert plans == open_plans(conn, b)
            assert 'keep' in plans.values() and any(p.startswith('tr:') for p in plans.values())
            day(conn, a, truth, slow)
            result = day(conn, b, truth, fast)
            assert result['committed'] == result['order'] == sorted(plans)
            assert result['left_open'] == {} and result['retries'] == {}
            assert snapshot(conn, a) == snapshot(conn, b)
            while a['day'] < LAST_DAY:
                a, b = next_day(conn, a), next_day(conn, b)
                day(conn, a, truth, slow)
                result = day(conn, b, truth, fast)
                assert result['order'] == sorted(result['order'])
            assert snapshot(conn, a) == snapshot(conn, b)
            keeps = db.rows(conn, "SELECT case_id, payload FROM activity_log WHERE run_id = :r AND type = 'committed' "
                                  "AND payload->>'plan' = 'keep' ORDER BY seq", r=b['run_id'])
            assert len(keeps) >= 2 and all(k['payload']['state'] == 'EXECUTED' for k in keeps)
            for k in keeps:  # the Keep fast path made no approval round trip in the round it committed
                assert db.rows(conn, "SELECT seq FROM activity_log WHERE run_id = :r AND case_id = :c "
                                     "AND type IN ('sent', 'decision') AND (payload->>'round')::int = :n",
                               r=b['run_id'], c=k['case_id'], n=k['payload']['round']) == []


def test_keeps_committed_through_the_fast_path_count_as_approved_as_the_app_path_would(pair, eligible_dv):
    """D-26 (08-18 finding): the app path commits a Keep through the source's approve, so a Keep the fast path
    commits counts as approved in the published decision quality, read from its `committed` entry; no decision row
    is ever invented for it. The whole week, app path (a) against commit_all (b): the same opened, approved,
    rejected and waiting counts, and on the fast path every opened case falls in exactly one bucket."""
    from api import metrics
    truth = refdata.truth(eligible_dv)
    a, b = pair
    stats = []
    with evalref.forecast_rule(eligible_dv):
        ref = refdata.get(eligible_dv)
        with db.tx() as conn:
            while True:
                day(conn, a, truth, slow)
                stats.append({'left_open': day(conn, b, truth, fast)['left_open']})
                if a['day'] >= LAST_DAY:
                    break
                a, b = next_day(conn, a), next_day(conn, b)
            fast_keeps = {k['case_id'] for k in db.rows(
                conn, "SELECT case_id FROM activity_log WHERE run_id = :r AND type = 'committed' "
                      "AND payload->>'plan' = 'keep'", r=b['run_id'])}
            approves = {r['case_id'] for r in db.rows(
                conn, "SELECT case_id FROM activity_log WHERE run_id = :r AND type = 'decision' "
                      "AND payload->>'decision' = 'approve'", r=b['run_id'])}
            assert fast_keeps - approves, 'the tiny set commits at least one case only as a fast-path Keep'
            decision_rows = db.one(conn, 'SELECT count(*) AS n FROM decisions WHERE run_id = :r', r=b['run_id'])['n']
            app = metrics.decision_quality(conn, a, ref, [], 2)
            quality = metrics.decision_quality(conn, b, ref, stats, 2)
            assert db.one(conn, 'SELECT count(*) AS n FROM decisions WHERE run_id = :r',
                          r=b['run_id'])['n'] == decision_rows  # read only: no decision row invented
    counts = ('opened', 'approved', 'rejected', 'waiting')
    assert {k: quality[k] for k in counts} == {k: app[k] for k in counts}
    assert quality['opened'] == (quality['approved'] + quality['rejected'] + quality['waiting']
                                 + sum(quality['left_open'].values()))
    assert quality['approved'] == len(approves | fast_keeps)


def test_a_keep_under_an_active_lot_hold_is_left_open_and_nothing_is_written(one, eligible_dv):
    truth = refdata.truth(eligible_dv)
    with evalref.forecast_rule(eligible_dv):
        with db.tx() as conn:
            run = to_day(conn, one, truth, KEEP_DAY)
            weekcheck.checked_morning(conn, run, bench_id=run['run_id'])
            plans = open_plans(conn, run)
            keep = next(c for c, p in plans.items() if p == 'keep')
            case = db.one(conn, 'SELECT * FROM cases WHERE run_id = :r AND case_id = :c', r=run['run_id'], c=keep)
            lotholds.place(conn, run, seat(case['source_branch']), case['lot_id'], 'CONDITION', 'Crushed crates')
            before = db.one(conn, 'SELECT * FROM cases WHERE run_id = :r AND case_id = :c', r=run['run_id'], c=keep)
            logged = db.one(conn, 'SELECT count(*) AS n FROM activity_log WHERE run_id = :r AND case_id = :c',
                            r=run['run_id'], c=keep)['n']
            result = fast(conn, run)
            assert result['order'] == sorted(plans)
            assert result['left_open'] == {'commitment_blocked:lot_hold': [keep]}
            assert keep not in result['committed'] and set(result['committed']) == set(plans) - {keep}
            assert db.one(conn, 'SELECT * FROM cases WHERE run_id = :r AND case_id = :c', r=run['run_id'],
                          c=keep) == before
            assert db.one(conn, 'SELECT count(*) AS n FROM activity_log WHERE run_id = :r AND case_id = :c',
                          r=run['run_id'], c=keep)['n'] == logged


def fail_after(monkeypatch, name, when=lambda *args: True):
    """The real service call runs and writes its rows, then a refusal is raised: what a refusal later in the same
    attempt leaves behind if the attempt were not rolled back."""
    real = getattr(workflow, name)

    def refused(*args, **kwargs):
        out = real(*args, **kwargs)
        if when(*args):
            raise ApiError(409, 'wrong_state', 'TEST: refused after the call wrote its rows', state='TEST')
        return out
    monkeypatch.setattr(workflow, name, refused)


def trail(conn, run, case_id):
    """Everything an attempt on this case can write: the case row (ownership included), its holds and bookings,
    decisions, work orders, moved stock and log entries (the case's earlier days stay as they were)."""
    out = {'case': db.one(conn, 'SELECT * FROM cases WHERE run_id = :r AND case_id = :c', r=run['run_id'], c=case_id)}
    for table, order in (('reservations', 'res_id'), ('decisions', 'rec_version, round, role'),
                         ('world_actions', 'world, action_id'), ('activity_log', 'seq')):
        out[table] = db.rows(conn, f'SELECT * FROM {table} WHERE run_id = :r AND case_id = :c ORDER BY {order}',
                             r=run['run_id'], c=case_id)
    out['inbound'] = db.rows(conn, 'SELECT * FROM inbound WHERE run_id = :r AND source_case_id = :c '
                                   'ORDER BY world, inbound_id', r=run['run_id'], c=case_id)
    return out


@pytest.mark.parametrize('where', ['send', 'accept', 'prepare'])
def test_a_refused_attempt_rolls_back_every_write_and_keeps_earlier_commits(one, eligible_dv, monkeypatch, where):
    """The review's savepoint rule: a refusal after send, after the accept, or during prepare leaves no hold, booking,
    decision, work order, ownership or log entry of that attempt, the case is OPEN as before, and the earlier valid
    commit of the same day stays."""
    truth = refdata.truth(eligible_dv)
    with evalref.forecast_rule(eligible_dv):
        with db.tx() as conn:
            run = to_day(conn, one, truth, KEEP_DAY)
            weekcheck.checked_morning(conn, run, bench_id=run['run_id'])
            plans = open_plans(conn, run)
            keep = next(c for c, p in plans.items() if p == 'keep')
            moving = next(c for c, p in plans.items() if p.startswith('tr:'))
            assert keep < moving  # the earlier valid commit comes first in case_id order
            before = trail(conn, run, moving)
            if where == 'accept':
                fail_after(monkeypatch, 'decide', lambda conn, run, actor, cid, v, decision, *rest: decision == 'accept')
            else:
                fail_after(monkeypatch, where)
            result = fast(conn, run)
            monkeypatch.undo()
            assert moving in [c for ids in result['left_open'].values() for c in ids]
            assert keep in result['committed']
            assert trail(conn, run, moving) == before
            kept = db.one(conn, 'SELECT state FROM cases WHERE run_id = :r AND case_id = :c', r=run['run_id'], c=keep)
            assert kept['state'] == 'EXECUTED'


# --- D-29: capacity clashes rechecked at most retry_limit times; missing evidence leaves the decision open (Task 2) --

TWIN, OTHER = 'S12-P101--1-r', 'S13-P101--1-r'  # the designed lot, and a second lot of its product moved beside it


def _only_these_p101(conn, run_id, keep):
    db.run(conn, "DELETE FROM truth_lots WHERE run_id = :r AND lot_id IN (SELECT lot_id FROM lots "
                 "WHERE run_id = :r AND sku = 'P101' AND NOT lot_id = ANY(:l))", r=run_id, l=keep)
    db.run(conn, "DELETE FROM lots WHERE run_id = :r AND sku = 'P101' AND NOT lot_id = ANY(:l)", r=run_id, l=keep)
    db.run(conn, "DELETE FROM inbound WHERE run_id = :r AND sku = 'P101'", r=run_id)


def twins(conn, run_id):
    """Two lots of one product at S12, 53 each on the record and physically, counted today, expiring today; the
    product's other stock and deliveries are removed (S11 then has unmet demand only one of them can use). Test-local
    record changes in every world, before the eager morning; never demand, forecasts or a fact."""
    keep = [TWIN, OTHER]
    db.run(conn, "UPDATE lots SET branch = 'S12', qty = 53, expiry_day = 0, last_verified_day = 0 "
                 "WHERE run_id = :r AND lot_id = ANY(:l)", r=run_id, l=keep)
    db.run(conn, "UPDATE truth_lots SET qty = 53 WHERE run_id = :r AND lot_id = ANY(:l)", r=run_id, l=keep)
    _only_these_p101(conn, run_id, keep)


def estimated_expiry(conn, run_id):
    """The designed lot as above, alone, with its expiry date ESTIMATED on the record (as Phase 7's tests make a
    Required expiry check: a record change, never a fact insert); the engine decides whether the check is Required."""
    db.run(conn, "UPDATE lots SET qty = 53, expiry_day = 0, last_verified_day = 0, expiry_source = 'ESTIMATED' "
                 "WHERE run_id = :r AND lot_id = :l", r=run_id, l=TWIN)
    db.run(conn, "UPDATE truth_lots SET qty = 53 WHERE run_id = :r AND lot_id = :l", r=run_id, l=TWIN)
    _only_these_p101(conn, run_id, [TWIN])


@pytest.fixture
def twin_run(eligible_dv, monkeypatch):
    from tests.api.test_checked_morning import make_run
    run = make_run(eligible_dv, monkeypatch, twins)
    yield run
    drop(run)


@pytest.fixture
def expiry_run(eligible_dv, monkeypatch):
    from tests.api.test_checked_morning import make_run
    run = make_run(eligible_dv, monkeypatch, estimated_expiry)
    yield run
    drop(run)


def plan_of(conn, run, case_id):
    from api import cases
    return cases.active_rec(conn, cases.case_row(conn, run['run_id'], case_id))['result']['recommended']


def test_a_capacity_clash_is_rechecked_and_commits_its_refreshed_plan(twin_run):
    """Two transfers into S11 whose unmet demand only one can use: the second gets 409 conflict, is re-evaluated
    against the first one's bookings and commits the refreshed plan, within retry_limit rechecks."""
    from api import cases
    with db.tx() as conn:
        first, second = sorted(open_plans(conn, twin_run).items())
        assert first[1] == second[1] and first[1].startswith('tr:S11:')  # the same plan, evaluated independently
        old = cases.case_row(conn, twin_run['run_id'], second[0])['active_rec_version']
        result = fast(conn, twin_run)
        assert result['order'] == [first[0], second[0]]
        assert 1 <= result['retries'].get(second[0], 0) <= 2 and first[0] not in result['retries']
        assert result['left_open'] == {} and result['committed'] == [first[0], second[0]]
        case = cases.case_row(conn, twin_run['run_id'], second[0])
        assert case['active_rec_version'] != old  # committed on its new version, never the refused one
        committed = db.one(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND case_id = :c "
                                 "AND type = 'committed' ORDER BY seq DESC LIMIT 1", r=twin_run['run_id'], c=second[0])
        assert committed['payload']['plan'] == plan_of(conn, twin_run, second[0]) != first[1]
        assert sandbox.limits_check(conn, twin_run, refdata.get(twin_run['dataset_version'])) is None


def test_with_no_rechecks_left_the_clash_stays_open_with_its_planned_units(twin_run):
    with db.tx() as conn:
        (first, plan), (second, _) = sorted(open_plans(conn, twin_run).items())
        result = evalcommit.commit_all(conn, twin_run, retry_limit=0)
        assert result['committed'] == [first] and result['retries'] == {}
        assert result['left_open'] == {'conflict_after_retries': [second]}
        assert result.get('transfer_units') == {'conflict_after_retries': int(plan.split(':')[3])}
        assert db.one(conn, 'SELECT state FROM cases WHERE run_id = :r AND case_id = :c', r=twin_run['run_id'],
                      c=second)['state'] == 'OPEN'


@pytest.mark.parametrize('code', ['conflict', 'hold_expired', 'stale_version'])
def test_rechecks_never_exceed_the_retry_limit_and_leave_nothing_behind(twin_run, monkeypatch, code):
    """TEST SEAM (disclosed): send writes its holds, then the same capacity-clash code is raised every time, so the
    limit itself is reached; the engine cannot keep recommending a clashing plan on its own. Each stock decision is
    re-evaluated exactly retry_limit times, then left open as conflict_after_retries with nothing of any attempt left."""
    from api import cases
    real_send, evaluated = workflow.send, []

    def clashing(conn, run, actor, case_id, version):
        real_send(conn, run, actor, case_id, version)
        raise ApiError(409, code, 'TEST: clash after the holds were written')
    real_eval = cases.evaluate_case

    def counted(conn, run, case, *args, **kwargs):
        evaluated.append(case['case_id'])
        return real_eval(conn, run, case, *args, **kwargs)
    monkeypatch.setattr(workflow, 'send', clashing)
    monkeypatch.setattr(cases, 'evaluate_case', counted)
    with db.tx() as conn:
        ids = sorted(open_plans(conn, twin_run))
        before = {c: trail(conn, twin_run, c) for c in ids}
        result = evalcommit.commit_all(conn, twin_run, retry_limit=2)
        monkeypatch.undo()
        assert result['left_open'] == {'conflict_after_retries': ids} and result['committed'] == []
        assert result['retries'] == {c: 2 for c in ids}
        assert sorted(evaluated) == sorted(ids * 2)
        for c in ids:
            now = trail(conn, twin_run, c)
            assert {k: v for k, v in now.items() if k != 'activity_log'} == \
                {k: v for k, v in before[c].items() if k != 'activity_log'}
            assert not [e for e in now['activity_log'] if e['type'] in
                        ('ownership_taken', 'sent', 'decision', 'committed', 'prepared')]


def test_a_required_expiry_check_leaves_the_decision_open_and_nothing_confirms_it(expiry_run):
    """D-29, D-34: the benchmark answers counts only, so a plan with a Required ESTIMATED_EXPIRY check is left open
    with its planned transfer units; no expiry is confirmed, no fact written, the record unchanged."""
    from api import cases, commitment
    with db.tx() as conn:
        [(case_id, plan)] = open_plans(conn, expiry_run).items()
        case = cases.case_row(conn, expiry_run['run_id'], case_id)
        required, _ = commitment.classes(commitment.checks_in_force(case, cases.active_rec(conn, case)))
        assert [c['kind'] for c in required] == ['ESTIMATED_EXPIRY'] and plan.startswith('tr:')
        weekcheck.checked_morning(conn, expiry_run, bench_id=expiry_run['run_id'])
        result = fast(conn, expiry_run)
        reason = 'commitment_blocked:required_check:ESTIMATED_EXPIRY'
        assert result['left_open'] == {reason: [case_id]} and result['retries'] == {}
        assert result.get('transfer_units') == {reason: int(plan.split(':')[3])}
        assert db.rows(conn, 'SELECT * FROM reported_facts WHERE run_id = :r', r=expiry_run['run_id']) == []
        assert db.one(conn, "SELECT expiry_source FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
                      r=expiry_run['run_id'], l=TWIN)['expiry_source'] == 'ESTIMATED'


def test_any_other_refusal_is_recorded_under_its_own_code_and_not_retried(twin_run, monkeypatch):
    """TEST SEAM (disclosed): a refusal outside the capacity-clash codes (here missing_data, raised after send wrote
    its holds) is never retried; each case is left open under that code, with its planned units."""
    real_send = workflow.send

    def missing(conn, run, actor, case_id, version):
        real_send(conn, run, actor, case_id, version)
        raise ApiError(409, 'missing_data', 'TEST: missing data after the holds were written')
    monkeypatch.setattr(workflow, 'send', missing)
    with db.tx() as conn:
        plans = open_plans(conn, twin_run)
        result = evalcommit.commit_all(conn, twin_run, retry_limit=2)
        monkeypatch.undo()
        assert result['left_open'] == {'missing_data': sorted(plans)} and result['retries'] == {}
        assert result.get('transfer_units') == {'missing_data': sum(int(p.split(':')[3]) for p in plans.values())}
