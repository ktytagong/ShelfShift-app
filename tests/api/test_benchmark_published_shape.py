"""A benchmark published by the real job reads as current through the Reports API, and its figures agree with
independent oracles (08-18; 08-CONTEXT D-02, D-03, D-08, D-11, D-19, D-21, D-24, D-27, D-35; 04-API-CONTRACT
"## Reports (08)").

The job runs as built: `weekcheck.main(["benchmark", "start", "--dataset", <eligible test set>])`, on the TEST ONLY
eligible dataset (`benchmark_fixtures.eligible_benchmark_dv`), with clearly test-only IMAGE_DIGEST and GIT_COMMIT
(`test_benchmark_cli.job`, which also removes what it made). The stand-in `public_dv` is only ever the refusal case.

While the job holds day 3's single transaction open (its uncommitted outcomes, decisions and engine-cache rows), the
app keeps working on other real connections: the same engine input evaluates in the app's own cache namespace, a store
manager reads Reports and the Decision board, an account signs in, a workspace resets and the health check answers;
all of them finish before the job is released. The threads meet at events; the timeouts only guard against a
deadlock and never make a check pass.

The key tree of the published summary equals the web fixture's (`tests/web/fixtures/reports_benchmark_current.json`),
but the amounts are what this file checks: every figure is compared with a derivation straight from the outcome
ledger, the per-trip audit, the count history and the engine's own limit check, never from `api.metrics`.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import benchmark, cases, db, evaluate, refdata, selection, weekcheck
from api.app import app
from engine import from_dict, to_dict
from engine.limits import check_plan
from engine.model import KEEP, TRANSFER, Plan
from tests.api.benchmark_fixtures import (eligible_benchmark_dv, eligible_benchmark_workspace,  # noqa: F401
                                          ledger_oracle, SCENARIO_OF)
from tests.api.conftest import key, sign_in
from tests.api.test_benchmark_cli import bind_session, job, key_tree, row  # noqa: F401  (job is a fixture)

GUARD_S = 120  # a deadlock guard only
HELD_DAY = 3
WEB_FIXTURE = Path(__file__).resolve().parents[1] / 'web' / 'fixtures' / 'reports_benchmark_current.json'
SCENARIOS = tuple(SCENARIO_OF.values())


def public_key(inp) -> str:
    """The app's cache key, written out here: sha256 of the canonical input (test_eval_cache_isolation)."""
    body = json.dumps(to_dict(inp), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, (path, response.json())
    return response.json()


def hq_breakdown(client, ident, chart, by):
    body = get(client, '/api/reports/benchmark', benchmark_id=ident, breakdown=by, chart=chart, page_size=100)
    status = body['status']
    assert body[status]['benchmark_id'] == ident
    return body[status]['breakdown']


def same_figures(before, after, path=''):
    """Equal identities and keys; numbers equal to 1e-9. A breakdown is summed from the stored float rows in SQL at
    read time, and Postgres does not fix the order of a float sum, so a change of exactly zero can read back as
    5.7e-14 after other rows are added to the table (reported in 08-18's notes). The stored rows are compared exactly."""
    if isinstance(before, dict):
        assert isinstance(after, dict) and set(before) == set(after), path
        for k in before:
            same_figures(before[k], after[k], f'{path}.{k}')
    elif isinstance(before, list):
        assert isinstance(after, list) and len(before) == len(after), path
        for i, (x, y) in enumerate(zip(before, after)):
            same_figures(x, y, f'{path}[{i}]')
    elif isinstance(before, (int, float)) and not isinstance(before, bool):
        assert after == pytest.approx(before, abs=1e-9), path
    else:
        assert after == before, path


def keyed(breakdown):
    """A breakdown's rows by their day or branch: rows whose order key ties may swap under float noise."""
    by = 'day' if breakdown['by'] == 'day' else 'branch'
    return {**breakdown, 'rows': {r[by]: r for r in breakdown['rows']}}


def normalized_fixture(summary: dict) -> dict:
    """The web fixture's summary with its dataset-defined leaves taken from the real one: the policy fields and the
    assumptions the set records, the forecast backtest's providers, input counts and window (as 08-15's check), and
    the two reason-keyed maps whose keys are the run's own reasons (left open by reason; a headline's null reasons),
    the dataset's price response (its `settings.truth.price_response` map; the fixture writes a scalar) and the
    dataset's categories without a schedule (a list, empty on the tiny set)."""
    fixture = json.loads(WEB_FIXTURE.read_text())['current']['summary']
    for name in ('policy', 'assumptions', 'categories_without_schedule'):
        fixture[name] = summary[name]
    fixture['settings']['price_response'] = summary['settings']['price_response']
    for name in ('providers', 'input_counts', 'window'):
        fixture['forecast_quality'][name] = summary['forecast_quality'][name]
    fixture['decision_quality']['left_open'] = summary['decision_quality']['left_open']
    for scenario in SCENARIOS:
        fixture['headline'][scenario]['reasons'] = summary['headline'][scenario]['reasons']
    return fixture


def test_public_dv_stays_a_refusal(job, public_dv):
    """The stand-in forecasts are never evaluated (D-36): exit 2, a REFUSED line, no benchmark row."""
    code, report = job('benchmark', 'start', '--dataset', public_dv)
    assert code == 2 and report['status'] == 'REFUSED' and report['benchmark_id'] is None
    assert any('stand-in' in r for r in report['reasons']), report['reasons']
    assert job.made == set()


def test_a_published_benchmark_reads_current_with_independently_checked_figures(
        job, eligible_benchmark_dv, eligible_benchmark_workspace, client, monkeypatch, capsys):
    dv = eligible_benchmark_dv
    ref = refdata.get(dv)
    installed = type(ref)
    forecast_runs_before = dict(ref.forecast_runs)
    workspace = eligible_benchmark_workspace()
    manager_store = sorted(ref.branches)[0]
    viewer = TestClient(app)
    sign_in(viewer)
    bind_session(viewer, workspace['run_id'], role='manager', branch=manager_store)
    assert get(viewer, '/api/reports/benchmark')['status'] == 'none'  # before any publication

    # --- the job, held inside day 3's open transaction -----------------------------------------------------------
    held, release, out = threading.Event(), threading.Event(), {}
    real_world_steps = weekcheck.world_steps

    def holding_world_steps(conn, run, truth, worlds=weekcheck.BENCH_WORLDS):
        result = real_world_steps(conn, run, truth, worlds)
        if run['day'] == HELD_DAY and not held.is_set():
            recs = db.rows(conn, 'SELECT case_id, rec_version, engine_input FROM recommendations WHERE run_id = :r '
                                 'AND NOT is_whatif ORDER BY case_id, rec_version', r=run['run_id'])
            out['sim'] = run['run_id']
            out['recs'] = {(r['case_id'], r['rec_version']): r['engine_input'] for r in recs}
            out['bench_keys'] = {k: evaluate.cache_key(from_dict(v)) for k, v in out['recs'].items()}
            seen = {r['cache_key'] for r in db.rows(conn, 'SELECT cache_key FROM engine_cache WHERE cache_key = '
                                                          'ANY(:k)', k=list(out['bench_keys'].values()))}
            out['bench_sees'] = {k for k, v in out['bench_keys'].items() if v in seen}
            out['bench_ref'] = type(refdata.get(run['dataset_version'])).__name__
            held.set()
            out['released_after_app'] = release.wait(GUARD_S)
        return result

    monkeypatch.setattr(weekcheck, 'world_steps', holding_world_steps)

    def run_job():
        try:
            out['job'] = job('benchmark', 'start', '--dataset', dv)
        except BaseException as exc:  # surfaced in the main thread
            out['error'] = exc
            held.set()

    worker = threading.Thread(target=run_job)
    worker.start()
    timings = {}
    try:
        assert held.wait(GUARD_S * 5) and 'error' not in out, out.get('error')
        with db.tx() as conn:  # what the main connection can see: only rows committed before day 3
            committed = {(r['case_id'], r['rec_version']) for r in db.rows(
                conn, 'SELECT case_id, rec_version FROM recommendations WHERE run_id = :r', r=out['sim'])}
            visible_cache = {r['cache_key'] for r in db.rows(conn, 'SELECT cache_key FROM engine_cache WHERE '
                                                                   'cache_key = ANY(:k)',
                                                             k=list(out['bench_keys'].values()))}
        uncommitted = sorted(k for k in out['bench_sees'] if k not in committed
                             and out['bench_keys'][k] not in visible_cache)
        assert uncommitted, 'the held day wrote engine-cache rows this connection cannot see yet'
        inp = from_dict(out['recs'][uncommitted[0]])
        app_key = public_key(inp)
        assert evaluate.cache_key(inp) == app_key != out['bench_keys'][uncommitted[0]]  # no namespace leaks here
        with db.tx() as conn:
            db.run(conn, 'DELETE FROM engine_cache WHERE cache_key = :k', k=app_key)  # a miss: the app computes
        started = time.perf_counter()
        with db.tx() as conn:
            timings['evaluate'] = evaluate.run_engine(conn, inp), time.perf_counter() - started
        assert type(refdata.get(dv)) is installed and dict(refdata.get(dv).forecast_runs) == forecast_runs_before
        for path in ('/api/reports/workspace', '/api/reports/benchmark', '/api/cases', '/api/session'):
            started = time.perf_counter()
            get(viewer, path)
            timings[path] = time.perf_counter() - started
        assert get(viewer, '/api/reports/benchmark')['status'] == 'none'  # a RUNNING benchmark is never read
        account = TestClient(app)
        sign_in(account, 'HQ')
        reset = client.post('/api/demo/reset', json={}, headers=key())
        assert reset.status_code == 200, reset.json()
        timings['reset_ms'] = reset.json()['reset_ms']
        assert client.get('/api/healthz').status_code == 200
    finally:
        release.set()
        worker.join(GUARD_S * 10)
    assert not worker.is_alive() and 'error' not in out, out.get('error')
    assert out['released_after_app'] is True  # every app request above finished while the job held its day
    assert out['bench_ref'] == 'EvaluationRef'  # the job's own D-08 view, only inside its context
    assert timings['evaluate'][1] < 3.0  # the case budget, never raised
    assert timings['reset_ms'] < 2000  # the reset target, never raised
    code, report = out['job']
    assert code == 0 and report['status'] == 'PUBLISHED', report
    ident = report['benchmark_id']
    with db.tx() as conn:  # the app's and the job's rows for that input never collided: one each, same answer
        rows = {r['cache_key']: r['result'] for r in db.rows(conn, 'SELECT cache_key, result FROM engine_cache '
                'WHERE cache_key = ANY(:k)', k=[app_key, out['bench_keys'][uncommitted[0]]])}
    assert set(rows) == {app_key, out['bench_keys'][uncommitted[0]]} and len(set(map(json.dumps, rows.values()))) == 1

    # --- current through the Reports API, for the signed-in workspace -----------------------------------------------
    body = get(viewer, '/api/reports/benchmark')
    assert body['status'] == 'current' and body['historical'] is None and body['settings_refused'] is None
    current = body['current']
    assert current['benchmark_id'] == ident and current['differs'] == [] and current['kind'] == 'SIMULATED'
    assert current['versions'] == body['app'] == report['versions'] == benchmark.app_versions({'dataset_version': dv},
                                                                                              ref)
    assert current['window'] == {'first_day': 0, 'last_day': 6} and current['git_commit'] == report['git_commit']
    assert 'image_digest' not in json.dumps(body) and row(ident)['image_digest'] not in json.dumps(body)
    summary = current['summary']
    assert key_tree(summary) == key_tree(normalized_fixture(summary))  # supplemental only; amounts follow

    with db.tx() as conn:
        # seven complete days, four completed worlds each, and the complete branch x day x world domain
        days = db.rows(conn, 'SELECT day, stats FROM benchmark_days WHERE benchmark_id = :b ORDER BY day', b=ident)
        assert [d['day'] for d in days] == list(range(7))
        for d in days:
            assert d['stats']['completed'] is True and set(d['stats']['worlds']) == set(SCENARIO_OF)
            assert all(w['completed'] is True for w in d['stats']['worlds'].values())
        stored_rows = db.rows(conn, 'SELECT * FROM benchmark_branch_day WHERE benchmark_id = :b', b=ident)
        assert {(r['scenario'], r['day'], r['branch']) for r in stored_rows} == {
            (w, d, b) for w in SCENARIO_OF for d in range(7) for b in ref.branches}
        assert len(stored_rows) == 4 * 7 * len(ref.branches)
        oracle = ledger_oracle(conn, ident)
        ledger = {(r['world'], r['day'], r['branch']): r for r in db.rows(
            conn, 'SELECT world, day, branch, sum(revenue) AS revenue, sum(transport) AS transport, '
                  'sum(handling) AS handling, sum(disposal) AS disposal, sum(sold) AS sold, sum(wasted) AS wasted, '
                  'sum(lost) AS lost, sum(demand) AS demand FROM outcomes WHERE run_id = :r '
                  'GROUP BY world, day, branch', r=ident)}
        audits = db.rows(conn, 'SELECT a.*, w.origin, w.case_id FROM benchmark_transfer_audit a JOIN world_actions w '
                               'ON w.run_id = a.sim_run_id AND w.world = a.world AND w.action_id = a.action_id '
                               'WHERE a.sim_run_id = :r', r=ident)
        committed = db.rows(conn, "SELECT case_id, payload FROM activity_log WHERE run_id = :r AND type = 'committed' "
                                  'ORDER BY seq', r=ident)
        case_rows = {c['case_id']: c for c in db.rows(conn, 'SELECT * FROM cases WHERE run_id = :r', r=ident)}
        recs = {(r['case_id'], r['rec_version']): r for r in db.rows(
            conn, 'SELECT * FROM recommendations WHERE run_id = :r AND NOT is_whatif', r=ident)}
        approvals = {(d['case_id'], d['round']): d['rec_version'] for d in db.rows(
            conn, "SELECT case_id, round, rec_version FROM decisions WHERE run_id = :r AND role = 'source' "
                  "AND decision = 'approve'", r=ident)}
        history = db.rows(conn, "SELECT world, outcome FROM evaluation_check_history WHERE sim_run_id = :r "
                                "AND event_kind = 'observation'", r=ident)
        requests = db.rows(conn, "SELECT world, count(*) AS n FROM evaluation_check_history WHERE sim_run_id = :r "
                                 "AND event_kind = 'request' GROUP BY world", r=ident)

    # the stored branch-day rows against the raw ledger at the grain where money is rounded: each row's revenue and
    # charges to the cent, its units exactly, its net their difference; a branch-day with no outcome is all zeros
    for r in stored_rows:
        got = ledger.get((r['scenario'], r['day'], r['branch']))
        for field in ('revenue', 'transport', 'handling', 'disposal'):
            assert r[field] == pytest.approx(float(got[field]) if got else 0.0, abs=0.005), (field, r)
        for field in ('sold', 'wasted', 'lost', 'demand'):
            assert r[field] == pytest.approx(float(got[field]) if got else 0.0, abs=1e-6), (field, r)
        assert r['net'] == pytest.approx(r['revenue'] - r['transport'] - r['handling'] - r['disposal'], abs=1e-6)
    assert set(ledger) <= {(r['scenario'], r['day'], r['branch']) for r in stored_rows}

    def stored(scenario, field='net', **match):
        return sum(r[field] for r in stored_rows if SCENARIO_OF[r['scenario']] == scenario
                   and all(r[k] == v for k, v in match.items()))

    # every summary total is exactly the sum of those rows (D-04), and agrees with the ledger to the cent per row
    for scenario, net in oracle['net'].items():
        assert summary['money']['by_scenario'][scenario] == pytest.approx(stored(scenario), abs=1e-6), scenario
        assert abs(summary['money']['by_scenario'][scenario] - net) <= 0.005 * 7 * len(ref.branches) + 1e-9
        assert summary['waste']['totals'][scenario]['units'] == pytest.approx(oracle['wasted_units'][scenario],
                                                                              abs=1e-6), scenario
        assert summary['waste']['totals'][scenario]['units'] == pytest.approx(stored(scenario, 'wasted'), abs=1e-6)
    steps = [s['value'] for s in summary['money']['steps']]
    assert steps[0] == pytest.approx(summary['money']['by_scenario']['store_rule'], abs=1e-6)
    assert steps[-1] == pytest.approx(summary['money']['by_scenario']['shelfshift'], abs=1e-6)
    assert steps[0] + sum(steps[1:-1]) == pytest.approx(steps[-1], abs=1e-6)  # the steps add up (D-17)
    for d in summary['money']['by_day']:
        for scenario in SCENARIOS:
            assert d[scenario] == pytest.approx(stored(scenario, day=d['day']), abs=1e-6)
    for d in summary['waste']['by_day']:
        for scenario in SCENARIOS:
            assert d[scenario] == pytest.approx(stored(scenario, 'wasted', day=d['day']), abs=1e-6)

    # by branch through the Reports API (HQ): every store's money, waste, movement and unmet demand per scenario
    sign_in(viewer, 'HQ')
    bind_session(viewer, workspace['run_id'])
    money_rows = hq_breakdown(viewer, ident, 'money', 'branch')
    assert money_rows['total'] == len(ref.branches) and {r['branch'] for r in money_rows['rows']} == set(ref.branches)
    for r in money_rows['rows']:
        for scenario in SCENARIOS:
            assert r[scenario] == pytest.approx(stored(scenario, branch=r['branch']), abs=1e-6), (r['branch'], scenario)
            assert r['waste_units'][scenario] == pytest.approx(stored(scenario, 'wasted', branch=r['branch']), abs=1e-6)
        assert r['total_change'] == pytest.approx(r['shelfshift'] - r['store_rule'], abs=1e-6)
        assert r['moved_out'] == pytest.approx(sum(a['shipped_units'] for a in audits
                                                   if a['world'] == 'shelfshift' and a['origin'] == r['branch']))
        assert r['moved_in'] == pytest.approx(sum(a['shipped_units'] for a in audits
                                                  if a['world'] == 'shelfshift' and a['destination'] == r['branch']))
        assert r['demand_not_met']['shelfshift'] == pytest.approx(stored('shelfshift', 'lost', branch=r['branch']))
        assert r['demand_not_met']['store_rule'] == pytest.approx(stored('store_rule', 'lost', branch=r['branch']))
    totals = [abs(r['total_change']) for r in money_rows['rows']]
    assert totals == sorted(totals, reverse=True)  # largest absolute change first (HQ, nothing pinned)
    waste_days = hq_breakdown(viewer, ident, 'waste', 'day')
    assert [r['day'] for r in waste_days['rows']] == list(range(7))
    for r in waste_days['rows']:
        for scenario in SCENARIOS:
            assert r[scenario] == pytest.approx(stored(scenario, 'wasted', day=r['day']), abs=1e-6)

    # each commit is the plan in force on the version it committed (Phase 7's selection.plan_in_force), and every
    # ShelfShift trip that shipped belongs to exactly one committed transfer, to that plan's store and run
    assert committed, 'the commit loop acted during the week'
    kinds = {}
    plans = {}
    for entry in committed:
        case = case_rows[entry['case_id']]
        version = approvals.get((entry['case_id'], entry['payload']['round']), case['active_rec_version'])
        rec = recs[entry['case_id'], version]
        in_force, _, _ = selection.plan_in_force({**case, 'active_rec_version': version}, rec)
        plan = cases.parse_plan(in_force)
        assert entry['payload']['plan'] == plan.key, entry
        kinds[plan.kind] = kinds.get(plan.kind, 0) + 1
        plans.setdefault(entry['case_id'], []).append(plan)  # a case can commit again in a later round
    shipped = [a for a in audits if a['world'] == 'shelfshift']
    assert shipped, 'ShelfShift moved stock during the week'
    for a in shipped:
        assert (TRANSFER, a['destination'], a['run_code']) in {(p.kind, p.dest, p.run_code)
                                                               for p in plans[a['case_id']]}, a['action_id']

    # trips, per-trip charges and measured limit checks (D-19): each audited trip carries its own unique charge, the
    # classes follow shipped against planned, and the engine's own check on each frozen execution input agrees with
    # the stored reasons; no scenario's breaks are fewer than the trips the engine refuses
    assert summary['trips'] == oracle['trips']
    assert len({(a['world'], a['charge_id']) for a in audits}) == len(audits)
    assert all(a['transport_amount'] >= 0 and a['handling_amount'] >= 0 and a['planned_units'] > 0 for a in audits)
    refused = {s: 0 for s in SCENARIOS}
    for a in audits:
        snapshot = a['effective_input']
        reasons = check_plan(from_dict(snapshot['raw_input']), Plan(**snapshot['plan']))
        assert bool(reasons) == bool(a['constraint_reasons']), a['action_id']
        refused[SCENARIO_OF[a['world']]] += bool(reasons)
    breaks = summary['decision_quality']['limit_breaks']
    assert set(breaks) == set(SCENARIOS) and all(isinstance(v, int) for v in breaks.values())
    assert all(breaks[s] >= refused[s] for s in SCENARIOS)
    assert breaks['markdowns_only'] == 0 and not [a for a in audits if a['world'] == 'do_nothing']

    # fair checks (D-09..D-11): both checked worlds asked and counted, and the summary's counts are the history's
    asked = {SCENARIO_OF[r['world']]: r['n'] for r in requests}
    assert asked.get('shelfshift', 0) > 0 and asked.get('store_rule_checked', 0) > 0, asked
    for scenario, world in (('shelfshift', 'shelfshift'), ('store_rule_checked', 'baseline_checked')):
        mine = [h['outcome'] for h in history if h['world'] == world]
        pairs = [(o['recorded_units'], o['observed_units']) for o in mine]
        assert summary['checks'][scenario]['lots_counted'] == len(mine) > 0, scenario
        assert summary['checks'][scenario]['corrected_up'] == sum(b > a for a, b in pairs)
        assert summary['checks'][scenario]['corrected_down'] == sum(b < a for a, b in pairs)
        assert summary['checks'][scenario]['nothing_to_correct'] == sum(a == b for a, b in pairs)

    # open item from 08-14/08-15 (reported in 08-18's notes, not changed here): Keep plans committed through the D-26
    # fast path write no decision, so they are not in decision_quality.approved
    approved_cases = {c for c, _ in approvals}
    keep_cases = {c for c, ps in plans.items() if any(p.kind == KEEP for p in ps)}
    by_case = {}
    for entry in committed:
        by_case.setdefault(entry['case_id'], []).append(cases.parse_plan(entry['payload']['plan']).kind)
    dq = summary['decision_quality']
    with capsys.disabled():
        print(f"\nFINDING-08-18 committed_entries={len(committed)} committed_cases={len(by_case)} "
              f"kinds_per_case={sorted(by_case.values())} keep_committed_cases={len(keep_cases)} "
              f"keep_cases_also_approved={len(keep_cases & approved_cases)} cases_with_source_approve="
              f"{len(approved_cases)} summary={{approved: {dq['approved']}, opened: {dq['opened']}, "
              f"rejected: {dq['rejected']}, waiting: {dq['waiting']}, left_open: {dq['left_open']}}}")

    # --- a second publication never changes the first (D-02) -------------------------------------------------------
    first = {(c, b): hq_breakdown(viewer, ident, c, b) for c in ('money', 'waste') for b in ('day', 'branch')}
    first_row, first_summary = row(ident), summary
    first_stored = sorted(stored_rows, key=lambda r: (r['scenario'], r['day'], r['branch']))
    monkeypatch.setattr(weekcheck, 'world_steps', real_world_steps)
    code, second = job('benchmark', 'start', '--dataset', dv)
    assert code == 0 and second['status'] == 'PUBLISHED' and second['benchmark_id'] != ident, second
    body = get(viewer, '/api/reports/benchmark')
    assert body['status'] == 'current' and body['current']['benchmark_id'] == second['benchmark_id']
    pinned = get(viewer, '/api/reports/benchmark', benchmark_id=ident)
    assert pinned[pinned['status']]['benchmark_id'] == ident and pinned[pinned['status']]['summary'] == first_summary
    assert row(ident) == first_row
    with db.tx() as conn:  # the published record itself is unchanged, row for row
        assert sorted(db.rows(conn, 'SELECT * FROM benchmark_branch_day WHERE benchmark_id = :b', b=ident),
                      key=lambda r: (r['scenario'], r['day'], r['branch'])) == first_stored
        assert db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id = :b',
                      b=ident)['payload'] == first_summary
    for (c, b), before in first.items():
        same_figures(keyed(before), keyed(hq_breakdown(viewer, ident, c, b)), f'{c}/{b}')
