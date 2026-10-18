"""The benchmark's cache rows never hold up the app (08-14 review requirement; 08-CONTEXT D-21, D-26).

A benchmark day is one long transaction, and every engine evaluation inside it inserts an engine_cache row. Without a
namespace an app request evaluating the same exact input would wait on that uncommitted row (the unique key) until the
benchmark's day ends. Here a real second connection evaluates the same input through the app while the benchmark
holds an uncommitted day claim and cache write; the threads meet at events, and the timeouts below only guard against
a deadlock (they are never what makes the test pass).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from api import benchmark, db, evaluate, refdata, weekcheck
from engine import from_dict, to_dict
from tests.api.benchmark_fixtures import eligible_dv  # noqa: F401
from tests.api.conftest import key
from tests.api.test_checked_morning import drop

GUARD_S = 30  # a deadlock guard only


@pytest.fixture
def bm_run(eligible_dv):
    with db.tx() as conn:
        run = benchmark.create_run(conn, eligible_dv, trial=False, git_commit='test-only', image_digest='test-only',
                                   worlds=weekcheck.BENCH_WORLDS)
    yield run
    drop(run)


def stored_input(template):
    with db.tx() as conn:
        row = db.one(conn, 'SELECT engine_input FROM recommendations WHERE run_id = :t AND NOT is_whatif '
                           'ORDER BY case_id, rec_version LIMIT 1', t=template)
    assert row, 'the synthetic template has a stored version'
    return from_dict(row['engine_input'])


def test_the_app_key_is_unchanged_and_the_namespace_is_context_local(template):
    inp = stored_input(template)
    body = json.dumps(to_dict(inp), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    app = hashlib.sha256(body.encode('utf-8')).hexdigest()
    assert evaluate.cache_key(inp) == app  # byte for byte the app's key
    with evaluate.cache_namespace('bm-test-one'):
        one = evaluate.cache_key(inp)
        with evaluate.cache_namespace('bm-test-two'):
            assert evaluate.cache_key(inp) not in (app, one)
        assert evaluate.cache_key(inp) == one != app
        seen = []
        other = threading.Thread(target=lambda: seen.append(evaluate.cache_key(inp)))
        other.start()
        other.join()
        assert seen == [app]  # another thread is never in this context
    with pytest.raises(RuntimeError):
        with evaluate.cache_namespace('bm-test-one'):
            raise RuntimeError('a day failed')
    assert evaluate.cache_key(inp) == app  # restored on every exit


def test_an_uncommitted_benchmark_day_never_holds_up_the_same_input_in_the_app(bm_run, template, client):
    """The benchmark claims its day (the row lock 08-12 defines) and evaluates an input inside its namespace, then
    holds the transaction open. Meanwhile the app evaluates the same input on a second connection, and a workspace
    reset runs: both finish before the benchmark releases its day, within the 3 s case and 2 s reset budgets."""
    inp = stored_input(template)
    app_key = evaluate.cache_key(inp)
    with evaluate.cache_namespace(bm_run['run_id']):
        bench_key = evaluate.cache_key(inp)
    with db.tx() as conn:  # a cache miss in both namespaces (test scratch: the cache only saves a recompute)
        db.run(conn, 'DELETE FROM engine_cache WHERE cache_key = ANY(:k)', k=[app_key, bench_key])
    ref = refdata.get(bm_run['dataset_version'])
    held, app_done, out = threading.Event(), threading.Event(), {}

    def benchmark_day():
        try:
            with db.tx() as conn:
                benchmark.claim_resume(conn, bm_run['run_id'], image_digest='test-only', git_commit='test-only',
                                       versions=benchmark.app_versions({'dataset_version': bm_run['dataset_version']},
                                                                      ref),
                                       settings_digest=benchmark.settings_digest(ref))
                with evaluate.cache_namespace(bm_run['run_id']):
                    out['bench'] = evaluate.run_engine(conn, inp)
                held.set()
                out['released_after_app'] = app_done.wait(GUARD_S)
        except BaseException as exc:  # surfaced in the main thread
            out['error'] = exc
            held.set()

    worker = threading.Thread(target=benchmark_day)
    worker.start()
    try:
        assert held.wait(GUARD_S) and 'error' not in out, out.get('error')
        started = time.perf_counter()
        with db.tx() as conn:
            app = evaluate.run_engine(conn, inp)
        out['app_s'] = time.perf_counter() - started
        reset = client.post('/api/demo/reset', json={}, headers=key())
    finally:
        app_done.set()
        worker.join(GUARD_S)
    assert 'error' not in out, out.get('error')
    assert out['released_after_app'] is True  # the app finished while the benchmark still held its day
    assert out['app_s'] < 3.0  # the case budget, never raised
    assert reset.status_code == 200 and reset.json()['reset_ms'] < 2000  # the reset target, never raised
    assert app == out['bench']  # the same version, result and checks either way
    with db.tx() as conn:
        rows = {r['cache_key']: r for r in db.rows(conn, 'SELECT cache_key, result FROM engine_cache '
                                                         'WHERE cache_key = ANY(:k)', k=[app_key, bench_key])}
    assert set(rows) == {app_key, bench_key} and rows[app_key]['result'] == rows[bench_key]['result']
