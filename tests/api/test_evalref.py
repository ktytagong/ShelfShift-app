"""D-08 forecast selection, independent readers, and transactional benchmark failures.

Expected forecast values come from the committed tiny artifact, not engine output. Stock stress
cases change one scratch simulation in Postgres; they never change the artifact or its demand.
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from api import advance, benchmark, db, inputs, refdata, screen, weekcheck
from engine.simulate import HorizonError
from tests.api.test_benchmark import bm, rows  # noqa: F401 -- shared scratch-run fixture
from tests.api.benchmark_fixtures import eligible_dv  # noqa: F401 -- bm's eligible input
from tests.api.test_publicref import part_rows


def evaluator():
    # A missing implementation fails on the planned API assertion, never during collection.
    assert importlib.util.find_spec('api.evalref') is not None, 'D-08 needs an evaluation-only forecast view'
    module = importlib.import_module('api.evalref')
    assert callable(getattr(module, 'forecast_rule', None)), 'D-08 needs a scoped forecast_rule'
    return module


def issued(ref, day=3):
    return ref.forecast_runs[day]['forecast_run_id']


def fixture_points(ref, store=None, sku=None, day=3):
    """The selected run's points, grouped by real carried series, directly from the CSV."""
    out = {}
    for row in part_rows('forecast_points'):
        if row['forecast_run_id'] != issued(ref, day):
            continue
        if store not in (None, row['store']) or sku not in (None, row['sku']):
            continue
        out.setdefault((row['store'], row['sku']), {})[int(row['day'])] = tuple(
            float(row[name]) for name in ('low', 'base', 'high'))
    assert out, 'the tiny artifact must contain the selected run and series'
    return out


def a_series(ref):
    return next(iter(fixture_points(ref)))


def test_context_override_selects_one_reference_and_restores_the_cached_app(public_dv):
    """RED target: the existing cache needs an explicit context-local selection seam."""
    original = refdata.get(public_dv)
    override = getattr(refdata, 'override', None)
    assert callable(override), 'refdata.get must support a context-local override without replacing its cache'
    replacement = copy.copy(original)
    with override(public_dv, replacement):
        assert refdata.get(public_dv) is replacement
        assert refdata.get(public_dv) is replacement
        assert refdata._refs[public_dv] is original
    assert refdata.get(public_dv) is original


def test_forecast_rule_maps_issued_runs_and_restores_normal_day_four(public_dv):
    ev = evaluator()
    original = refdata.get(public_dv)
    store, sku = a_series(original)
    before = copy.deepcopy(original.forecast_runs)
    assert original.forecast(4, store, sku) == {}
    with ev.forecast_rule(public_dv):
        view = refdata.get(public_dv)
        assert view is not original
        assert refdata.get(public_dv) is view
        for day in range(7):
            selected = min(day, 3)
            assert view.forecast_runs[day]['forecast_run_id'] == issued(original, selected)
            assert view.forecast(day, store, sku) == fixture_points(original, store, sku, selected)[store, sku]
    assert refdata.get(public_dv) is original
    assert original.forecast_runs == before
    assert original.forecast(4, store, sku) == {}


def test_forecast_rule_preserves_the_dataset_six_version_identity(public_dv):
    ev = evaluator()
    run = {'dataset_version': public_dv}
    original = benchmark.app_versions(run, refdata.get(public_dv))
    with ev.forecast_rule(public_dv):
        assert benchmark.app_versions(run, refdata.get(public_dv)) == original


def test_uncarried_series_stays_empty_in_scalar_and_bulk_reads(public_dv):
    ev = evaluator()
    original = refdata.get(public_dv)
    store, sku = a_series(original)
    with ev.forecast_rule(public_dv):
        view = refdata.get(public_dv)
        assert view.forecast(4, 'unknown-test-store', sku) == {}
        assert 'unknown-test-store' not in view.forecast_of_product(4, sku)
        assert view.forecast(6, store, 'unknown-test-product') == {}
        assert view.forecast_of_product(6, 'unknown-test-product') == {}


@pytest.mark.parametrize('day', [4, 6])
def test_scalar_bulk_and_provider_read_the_same_d3_target_dates(public_dv, day):
    ev = evaluator()
    original = refdata.get(public_dv)
    store, sku = a_series(original)
    expected = fixture_points(original, sku=sku)
    providers = {(r['store'], int(r['day'])): r['provider'] for r in part_rows('forecast_points')
                 if r['forecast_run_id'] == issued(original) and r['sku'] == sku}
    with ev.forecast_rule(public_dv):
        view = refdata.get(public_dv)
        bulk = view.forecast_of_product(day, sku)
        assert bulk == {s: points for (s, _), points in expected.items()}
        for (s, _), points in expected.items():
            assert view.forecast(day, s, sku) == points
            for target in range(day, int(view.calendar['horizon_end']) + 1):
                assert bulk[s][target] == points[target]
                assert view.provider_of(day, s, sku, target) == providers[s, target]
        # Callers cannot corrupt another scalar or bulk read by changing a returned dict.
        view.forecast(day, store, sku).clear()
        bulk[store].clear()
        assert view.forecast(day, store, sku) == expected[store, sku]
        assert view.forecast_of_product(day, sku)[store] == expected[store, sku]


def test_warmed_app_readers_and_every_view_cache_are_independent(public_dv):
    ev = evaluator()
    original = refdata.get(public_dv)
    store, sku = a_series(original)
    original.forecast(3, store, sku)
    original.forecast(4, store, sku)
    original.provider_of(3, store, sku, 6)
    original.runs_from(store, 0)
    run = next((r for r in original.runs_from(store, 0)), None)
    original.run_on(run.run_code if run else 'unknown-test-run', 0)
    original.free_totes(store, original.products[sku].zone, 0)
    original.sale(store, sku, -1)
    original.carries(store, sku)
    readers = ('_points', '_runs', 'run_on', 'free_totes', 'sale', '_carried')
    before = {name: getattr(original, name).cache_info() for name in readers}
    settings = copy.deepcopy(original.settings)
    with ev.forecast_rule(public_dv):
        view = refdata.get(public_dv)
        for name in readers:
            assert getattr(view, name) is not getattr(original, name)
        assert view.forecast(4, store, sku)
        misses = view._points.cache_info().misses
        assert view.forecast(4, store, sku)
        assert view._points.cache_info().misses == misses
        assert view._points.cache_info().hits > 0
        view.runs_from(store, 0)
        view.run_on(run.run_code if run else 'unknown-test-run', 0)
        view.free_totes(store, view.products[sku].zone, 0)
        view.sale(store, sku, -1)
        view.carries(store, sku)
        view.settings['truth']['price_response']['m20'] = 17.0
        view.settings['calendar']['day0'] = '2000-01-01'
        view.names[sku] = 'test-only isolated label'
        view.forecast_runs[4]['providers'].clear()
        assert original.settings == settings
        assert original.names[sku] != 'test-only isolated label'
        assert original.forecast_runs[3]['providers']
        assert {name: getattr(original, name).cache_info() for name in readers} == before
    assert original.forecast(4, store, sku) == {}


def test_nested_rules_restore_the_outer_view_and_exception_resets_the_token(public_dv, schema):
    ev = evaluator()
    original = refdata.get(public_dv)
    other = refdata.get(schema)
    with pytest.raises(RuntimeError, match='test-only interruption'):
        with ev.forecast_rule(public_dv):
            outer = refdata.get(public_dv)
            assert refdata.get(schema) is other
            with ev.forecast_rule(public_dv):
                inner = refdata.get(public_dv)
                assert inner is not outer
                assert inner is not original
                inner.settings['truth']['price_response']['m20'] = 17.0
                assert outer.settings['truth']['price_response']['m20'] == 1.0
            assert refdata.get(public_dv) is outer
            replacement = copy.copy(other)
            with refdata.override(schema, replacement):
                assert refdata.get(schema) is replacement
                assert refdata.get(public_dv) is outer
            assert refdata.get(schema) is other
            raise RuntimeError('test-only interruption')
    assert refdata.get(public_dv) is original
    assert refdata.get(schema) is other


def test_concurrent_evaluators_and_normal_app_reads_do_not_share_a_view(public_dv):
    ev = evaluator()
    original = refdata.get(public_dv)
    store, sku = a_series(original)
    barrier = Barrier(3)

    def evaluate():
        with ev.forecast_rule(public_dv):
            view = refdata.get(public_dv)
            barrier.wait(timeout=15)
            assert refdata.get(public_dv) is view
            assert view.forecast(4, store, sku)
            barrier.wait(timeout=15)
            return view

    def app_read():
        barrier.wait(timeout=15)
        ref = refdata.get(public_dv)
        assert ref is original
        assert ref.forecast(4, store, sku) == {}
        barrier.wait(timeout=15)
        return ref

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(evaluate)
        second = pool.submit(evaluate)
        normal = pool.submit(app_read)
        one, two, app = first.result(timeout=30), second.result(timeout=30), normal.result(timeout=30)
    assert one is not two
    assert one is not app and two is not app
    assert refdata.get(public_dv) is original


@pytest.mark.parametrize('bulk,whole_series', [(False, False), (True, False), (True, True)])
def test_a_missing_d3_target_raises_a_named_gap_and_leaves_the_artifact_intact(public_dv, bulk, whole_series):
    ev = evaluator()
    original = refdata.get(public_dv)
    store, sku = a_series(original)
    run_id, target = issued(original), 6
    with pytest.raises(ev.ForecastGap) as failed:
        with db.tx() as conn:
            db.run(conn, 'DELETE FROM ref_forecast_point WHERE dataset_version = :v AND forecast_run_id = :f '
                         'AND store = :s AND sku = :k' + ('' if whole_series else ' AND day = :d'),
                   v=public_dv, f=run_id, s=store, k=sku, **({} if whole_series else {'d': target}))
            with ev.forecast_rule(public_dv):
                view = refdata.get(public_dv)
                if bulk:
                    view.forecast_of_product(6 if whole_series else 4, sku)
                else:
                    view.forecast(4, store, sku)
    message = str(failed.value)
    for value in (store, sku, str(target), run_id):
        assert value in message
    assert refdata.get(public_dv) is original
    with db.tx() as conn:
        assert db.one(conn, 'SELECT day FROM ref_forecast_point WHERE dataset_version = :v AND forecast_run_id = :f '
                           'AND store = :s AND sku = :k AND day = :d',
                      v=public_dv, f=run_id, s=store, k=sku, d=target)


def day_four_stock(conn, bm):
    """An excess-stock stress case on this run only, derived from real d3 demand bands."""
    ref = refdata.get(bm['dataset_version'])
    lot = db.one(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'baseline_rule' AND tag = 'designed'",
                 r=bm['run_id'])
    assert lot
    points = fixture_points(ref, lot['branch'], lot['sku'])[lot['branch'], lot['sku']]
    quantity = max(ref.products[lot['sku']].units_per_tote, sum(p[2] for p in points.values())) * 10
    params = {'r': bm['run_id'], 's': lot['sku'], 'l': lot['lot_id']}
    db.run(conn, 'DELETE FROM truth_lots WHERE run_id = :r AND lot_id IN (SELECT lot_id FROM lots '
                 'WHERE run_id = :r AND sku = :s AND lot_id <> :l)', **params)
    db.run(conn, 'DELETE FROM lots WHERE run_id = :r AND sku = :s AND lot_id <> :l', **params)
    db.run(conn, 'DELETE FROM inbound WHERE run_id = :r AND sku = :s', r=bm['run_id'], s=lot['sku'])
    db.run(conn, 'UPDATE lots SET qty = :q, expiry_day = 4, last_verified_day = 4 '
                 'WHERE run_id = :r AND lot_id = :l', r=bm['run_id'], l=lot['lot_id'], q=quantity)
    db.run(conn, 'UPDATE truth_lots SET qty = :q WHERE run_id = :r AND lot_id = :l',
           r=bm['run_id'], l=lot['lot_id'], q=quantity)
    run = db.one(conn, 'UPDATE demo_runs SET day = 4 WHERE run_id = :r RETURNING *', r=bm['run_id'])
    advance.day_start(conn, run, worlds=weekcheck.BENCH_WORLDS)
    return run, lot


class ScratchRollback(Exception):
    pass


def test_identical_inputs_produce_identical_world_results_across_benchmark_ids(bm):
    ev = evaluator()
    with pytest.raises(ScratchRollback):
        with ev.forecast_rule(bm['dataset_version']):
            with db.tx() as conn:
                other = benchmark.create_run(conn, bm['dataset_version'], trial=False, git_commit='test-only',
                                             image_digest='test-only', worlds=weekcheck.BENCH_WORLDS)
                assert other['run_id'] != bm['run_id']
                truth = refdata.truth(bm['dataset_version'])
                for run in (bm, other):
                    weekcheck.world_steps(conn, run, truth)
                for world in ('baseline_rule', 'baseline_checked', 'do_nothing'):
                    for table in ('world_actions', 'outcomes', 'world_events', 'lots'):
                        assert rows(conn, bm, table, world) == rows(conn, other, table, world)
                raise ScratchRollback()


def test_day_four_world_steps_make_real_engine_decisions_instead_of_skipping(bm, monkeypatch):
    ev = evaluator()
    truth = refdata.truth(bm['dataset_version'])
    with pytest.raises(ScratchRollback):
        with db.tx() as conn:
            run, lot = day_four_stock(conn, bm)
            raw = inputs.build_input(conn, run, lot['lot_id'], world='baseline_rule')
            assert not raw.forecasts
            with pytest.raises(HorizonError):
                screen.screen(raw, advance.config.CASE_OPENING)
            stats = weekcheck.world_steps(conn, run, truth)
            assert stats['baseline_rule']['decisions'] == 0
            raise ScratchRollback()

    decisions = []
    baseline = advance.baseline_plan

    def capture(raw):
        plan = baseline(raw)
        decisions.append((raw, plan))
        return plan

    monkeypatch.setattr(advance, 'baseline_plan', capture)
    with ev.forecast_rule(bm['dataset_version']):
        with db.tx() as conn:
            run, lot = day_four_stock(conn, bm)
            raw = inputs.build_input(conn, run, lot['lot_id'], world='baseline_rule')
            assert raw.forecasts
            stats = weekcheck.world_steps(conn, run, truth)
            # 08-12 orders checkpoints strictly (day 0..6, each the next): this scratch stress case simulates day 4
            # only, so days 0..3 are checkpointed first, in order, before day 4's checkpoint is recorded.
            for earlier in range(4):
                benchmark.mark_day(conn, run['run_id'], earlier, 0.0, {'completed': True, 'worlds': {}})
            benchmark.mark_day(conn, run['run_id'], 4, 0.1, {'completed': True, 'worlds': stats})
            assert db.one(conn, 'SELECT last_day_done FROM benchmark_runs WHERE benchmark_id = :b',
                          b=run['run_id'])['last_day_done'] == 4
            assert stats['baseline_rule']['decisions'] > 0
            assert stats['baseline_checked']['decisions'] > 0
    assert decisions
    assert all(raw.today == 4 and raw.forecasts for raw, _ in decisions)


def snapshot(conn, run):
    tables = ('demo_runs', 'lots', 'truth_lots', 'inbound', 'world_actions', 'outcomes', 'world_events')
    data = {t: sorted(db.rows(conn, f'SELECT * FROM {t} WHERE run_id = :r', r=run['run_id']), key=repr)
            for t in tables}
    for table in ('benchmark_runs', 'benchmark_days'):
        data[table] = sorted(db.rows(conn, f'SELECT * FROM {table} WHERE benchmark_id = :r',
                                    r=run['run_id']), key=repr)
    return data


def test_forecast_gap_rolls_back_the_whole_day_and_preserves_previous_checkpoint(bm):
    ev = evaluator()
    truth = refdata.truth(bm['dataset_version'])
    with db.tx() as conn:
        stats = weekcheck.world_steps(conn, bm, truth)
        benchmark.mark_day(conn, bm['run_id'], 0, 0.1, {'completed': True, 'worlds': stats})
        before = snapshot(conn, bm)
    original = refdata.get(bm['dataset_version'])
    with pytest.raises(ev.ForecastGap) as failed:
        with db.tx() as conn:
            run, lot = day_four_stock(conn, bm)
            db.run(conn, 'DELETE FROM ref_forecast_point WHERE dataset_version = :v AND forecast_run_id = :f '
                         'AND store = :s AND sku = :k AND day = 4',
                   v=bm['dataset_version'], f=issued(original), s=lot['branch'], k=lot['sku'])
            with ev.forecast_rule(bm['dataset_version']):
                stats = weekcheck.world_steps(conn, run, truth)
                benchmark.mark_day(conn, run['run_id'], 4, 0.1, {'completed': True, 'worlds': stats})
    for value in (lot['branch'], lot['sku'], '4', issued(original)):
        assert value in str(failed.value)
    with db.tx() as conn:
        assert snapshot(conn, bm) == before
    assert refdata.get(bm['dataset_version']) is original
