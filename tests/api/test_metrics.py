"""Execution evidence and the complete, record-derived evaluation summary."""
import cProfile
import copy
import json
import inspect
import sys
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from api import advance, metrics
from engine.allocator import ShelfLot
from engine.model import Product
from engine.world import Shipment


def execution_snapshot(world='baseline_rule', change=None):
    from engine.canonical import rec_version, to_dict
    from engine.adjust import effective
    from engine.model import Plan, TRANSFER
    from tests.engine.fixtures import f1, LAT_PHRAO
    inp = f1()
    if change:
        inp = change(inp)
    action = dict(action_id='case#1:transfer' if world == 'shelfshift' else 'action',
                  case_id='case' if world == 'shelfshift' else None, lot_id=inp.case_lot_id,
                  sku=inp.product.sku, origin=inp.case_lot().branch, run_day=inp.today,
                  dest=LAT_PHRAO, run_code='R-02', qty=100)
    from engine.timing import first_sellable_day
    trip = next(r for r in inp.runs if r.run_code == action['run_code'])
    action['sellable_from'] = first_sellable_day(trip.day, trip.arrives_min, inp.policy)
    return dict(schema_version=1, raw_input=to_dict(inp), effective_input=to_dict(effective(inp)),
                execution_input_version=rec_version(inp), rec_version=rec_version(inp),
                round=1 if world == 'shelfshift' else None, action=action,
                plan=asdict(Plan(TRANSFER, qty=100, dest=LAT_PHRAO, run_code='R-02')),
                source=dict(recorded_units=500, physical_units=500, available_before=500,
                            set_aside_units=100, expiry_day=5), booking_rows=[], input_errors=[])


@pytest.mark.parametrize('world', ['shelfshift', 'baseline_rule', 'baseline_checked'])
@pytest.mark.parametrize('violation,expected', [
    ('cold', 'cold chain'), ('deadline', 'past decision deadline'), ('sellable', 'not enough sellable days'),
    ('demand', 'no unmet demand'), ('stock', 'no stock'), ('truck', 'no truck'), ('storage', 'no free storage'),
])
def test_frozen_execution_replays_each_constraint_in_every_transfer_world(world, violation, expected):
    changes = dict(cold=lambda i: replace(i, runs=(replace(i.runs[0], cold_chain=False),)),
        deadline=lambda i: replace(i, decision_min=1000),
        sellable=lambda i: replace(i, policy=replace(i.policy, min_sellable_days=4)),
        demand=lambda i: replace(i, forecasts=tuple(replace(f, low=0, base=0, high=0)
                                                   if f.branch != i.case_lot().branch else f for f in i.forecasts)),
        stock=lambda i: replace(i, lots=(replace(i.lots[0], qty=1),)),
        truck=lambda i: replace(i, runs=(replace(i.runs[0], spare_totes=0),)),
        storage=lambda i: replace(i, capacity=tuple(replace(c, free_totes=0) for c in i.capacity)))
    assert metrics.constraint_reasons(execution_snapshot(world)) == []
    assert any(expected in r for r in metrics.constraint_reasons(execution_snapshot(world, changes[violation])))


@pytest.mark.parametrize('field', ['raw_input', 'effective_input', 'execution_input_version', 'rec_version', 'plan', 'source'])
def test_execution_evidence_fails_closed_on_omitted_bindings(field):
    snapshot = execution_snapshot()
    snapshot.pop(field)
    assert metrics.constraint_reasons(snapshot)


def test_execution_evidence_rejects_defaulted_schedule_and_double_booking():
    snapshot = execution_snapshot()
    snapshot['raw_input'].pop('markdown_schedule')
    assert 'incomplete execution input' in metrics.constraint_reasons(snapshot)
    snapshot = execution_snapshot()
    snapshot['source']['set_aside_units'] = 501
    assert 'source units booked twice' in metrics.constraint_reasons(snapshot)


def capture(*args, **kwargs):
    assert callable(getattr(advance, 'observed_step', None)), 'per-action execution observation is required'
    return advance.observed_step(*args, **kwargs)


def step_args(quantity=3):
    return ([ShelfLot('lot', 'source', quantity, 5, 0)], 0, {'source': 0, 'dest': 0},
            {20: 1, 30: 1}, Product('sku', 'ambient', 5, 1, 10, .5))


def shipment(**changes):
    values = dict(lot_id='lot', qty=10, dest='dest', new_lot_id='moved', sellable_from=0,
                  fixed_cost=40, per_unit_cost=2, cancel_cost=7, origin='source')
    return Shipment(**(values | changes))


def test_capture_observes_partial_and_same_source_actions_without_allocating_totals():
    first = shipment(qty=2, new_lot_id='first')
    second = shipment(fixed_cost=60, new_lot_id='second')
    out, charges = capture(*step_args(), shipments=(first, second))
    assert charges == [dict(shipment_index=0, shipped_units=2, transport_amount=40, handling_amount=4),
                       dict(shipment_index=1, shipped_units=1, transport_amount=60, handling_amount=2)]
    assert out.transport == {'source': 100} and out.handling == {'source': 6}


def test_capture_observes_the_failed_trip_return_with_no_source_lot():
    args = step_args()
    args[0].clear()
    out, charges = capture(*args, shipments=(shipment(),))
    assert charges == [dict(shipment_index=0, shipped_units=0, transport_amount=7, handling_amount=0)]
    assert out.transport == {'source': 7}


def test_capture_observes_new_moved_lot_waste_on_dispatch_day_and_restores_hook():
    args = step_args(3)
    args[0][0].expiry_day = 0
    expired, prior = [], sys.getprofile()
    out, charges = capture(*args, shipments=(shipment(qty=3),), expired=expired)
    assert expired == [dict(lot_id='moved', branch='dest', wasted=3)]
    assert out.wasted == {'dest': 3} and charges[0]['shipped_units'] == 3
    assert sys.getprofile() is prior


def test_review_sales_capture_observes_old_moved_lot_without_a_new_shipment():
    assert 'sold' in inspect.signature(advance.observed_step).parameters, 'actual per-lot sales observation required'
    args = step_args(10)
    args[0][0].lot_id = 'old>moved'
    args[0][0].expiry_day = 0
    args[2]['source'] = 4
    sold, expired, prior = [], [], sys.getprofile()
    out, charges = capture(*args, sold=sold, expired=expired)
    assert sold == [dict(lot_id='old>moved', branch='source', sold=4)]
    assert expired == [dict(lot_id='old>moved', branch='source', wasted=6)]
    assert out.sold['source'] == 4 and out.wasted['source'] == 6 and charges == []
    assert sys.getprofile() is prior


def test_review_sales_capture_omission_restores_hook_and_fails_closed(monkeypatch):
    assert 'sold' in inspect.signature(advance.observed_step).parameters, 'actual per-lot sales observation required'
    from engine import world
    original = world.allocate
    monkeypatch.setattr(world, 'allocate', lambda *a: original(*a))
    args = step_args(10)
    args[2]['source'] = 4
    prior = sys.getprofile()
    with pytest.raises(ValueError, match='incomplete sales capture'):
        capture(*args, sold=[], expired=[])
    assert sys.getprofile() is prior


def test_review_sellability_is_bound_to_original_run_arrival_and_policy():
    snapshot = execution_snapshot()
    assert metrics.constraint_reasons(snapshot) == []
    snapshot['action']['sellable_from'] -= 1
    assert 'executed sellability differs from run arrival policy' in metrics.constraint_reasons(snapshot)


def test_review_headline_refuses_to_infer_missing_moved_sales():
    moved = {key: dict(shipped_units=10, wasted_units=0, on_hand_units=0, in_transit_units=0)
             for key in metrics.WORLD_KEYS.values()}
    with pytest.raises(ValueError, match='sales evidence'):
        metrics.headline([], moved, {})


def test_review_headline_accepts_the_persisted_six_decimal_unit_tolerance_without_clamping():
    moved = {key: dict(shipped_units=1, sold_units=1 / 3 + .666667, wasted_units=0,
                      on_hand_units=0, in_transit_units=0) for key in metrics.WORLD_KEYS.values()}
    try:
        result = metrics.headline([], moved, {})
    except ValueError:
        pytest.fail('valid six-decimal physical conservation must use the same unit tolerance in headline')
    assert result['shelfshift']['sell_through_moved'] == 1 / 3 + .666667


def test_review_chained_transfer_counts_physical_units_once(monkeypatch):
    from api import weekcheck
    moves = [dict(action_id='a', new_lot_id='m1', lot_id='original', shipped_units=10,
                  dest='middle', sku='sku', run_day=0, sellable_from=0,
                  effective_input={'source': {'expiry_day': 2}}),
             dict(action_id='b', new_lot_id='m2', lot_id='m1', shipped_units=10,
                  dest='dest', sku='sku', run_day=1, sellable_from=1,
                  effective_input={'source': {'expiry_day': 2}})]
    event = dict(day=1, kind='TRANSFER_SOLD', payload=dict(action_id='b', lot_id='m2', branch='dest', sku='sku', sold=10))
    moved_records(monkeypatch, moves, [event], [], 2)
    stock = weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=2), ('baseline_rule',))['store_rule']
    # "Units received" counts each actual dispatch receipt once, including onward receipts.
    assert stock['shipped_units'] == 20 and stock['sold_units'] == 10


def test_review_prevalidation_expiry_cannot_be_changed_to_sale(monkeypatch):
    from api import weekcheck
    action = dict(action_id='a', new_lot_id='moved', lot_id='original', shipped_units=10,
                  dest='dest', sku='sku', run_day=0, sellable_from=0,
                  effective_input={'source': {'expiry_day': 0}})
    false_sale = dict(day=0, kind='TRANSFER_SOLD', payload=dict(action_id='a', lot_id='moved', branch='dest', sku='sku', sold=10))
    actual_expiry = dict(day=0, kind='TRANSFER_EXPIRED', payload=dict(action_id='a', lot_id='moved', branch='dest', sku='sku', wasted=10))
    moved_records(monkeypatch, [action], [false_sale], [], 0, actual=[actual_expiry])
    with pytest.raises(ValueError, match='capture|outcome|expiry'):
        weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=0), ('baseline_rule',))


def test_review_record_digest_binds_decision_authority(monkeypatch):
    from api import benchmark
    authority = [dict(seq=1, kind='PREPARED', payload={'rec_version': 'original'})]
    monkeypatch.setattr(benchmark.db, 'rows', lambda conn, sql, **kw: copy.deepcopy(authority)
                        if 'FROM activity_log ' in sql else [])
    before = benchmark.record_digest(None, 'run')
    authority[0]['payload']['rec_version'] = 'changed'
    assert benchmark.record_digest(None, 'run') != before, 'prepared/approval authority must bind the receipt'


def moved_records(monkeypatch, moves, events, physical, day, *, actual=None):
    """A complete captured ledger, kept independent from deliberately corrupted moved events."""
    from types import SimpleNamespace
    from api import refdata, weekcheck
    actual = copy.deepcopy(events if actual is None else actual)
    branches = ['dest', 'middle', 'source']
    monkeypatch.setattr(refdata, 'get', lambda _: SimpleNamespace(products={'sku': None}, branches=branches))
    by_lot = {a['new_lot_id']: a for a in moves}
    captures, outcomes = [], {}
    for d in range(day + 1):
        payload = dict(schema_version=1, sku='sku', branches=list(branches), sold=[], expired=[], timing={})
        for event in actual:
            if event['day'] != d:
                continue
            p = event['payload']
            action = by_lot[p['lot_id']]
            field = 'sold' if event['kind'] == 'TRANSFER_SOLD' else 'expired'
            units = 'sold' if field == 'sold' else 'wasted'
            payload[field].append({k: p[k] for k in ('lot_id', 'branch', units)})
            payload['timing'][p['lot_id']] = dict(branch=p['branch'], sellable_from=action['sellable_from'],
                expiry_day=action['effective_input']['source']['expiry_day'])
            out = outcomes.setdefault((d, p['branch']), dict(day=d, branch=p['branch'], sku='sku', sold=0, wasted=0))
            out[units] += p[units]
        captures.append(dict(day=d, payload=payload))
    def rows(conn, sql, **kw):
        if 'FROM world_actions' in sql:
            return moves
        if 'FROM truth_lots' in sql:
            return physical
        if 'FROM outcomes' in sql:
            return list(outcomes.values())
        return captures if 'BENCHMARK_LOT_DAY' in sql else events
    monkeypatch.setattr(weekcheck.db, 'rows', rows)
    return captures, outcomes


@pytest.mark.parametrize('omit', [None, 'TRANSFER_EXPIRED', 'TRANSFER_SOLD'])
def test_review_each_moved_lot_conserves_actual_sales_waste_closing_and_onward(monkeypatch, omit):
    from api import weekcheck
    moves = [dict(action_id='a', new_lot_id='m1', lot_id='original', shipped_units=10,
                  dest='middle', sku='sku', run_day=0, sellable_from=0,
                  effective_input={'source': {'expiry_day': 2}}),
             dict(action_id='b', new_lot_id='m2', lot_id='m1', shipped_units=3,
                  dest='dest', sku='sku', run_day=1, sellable_from=1,
                  effective_input={'source': {'expiry_day': 2}})]
    events = [dict(day=1, kind='TRANSFER_SOLD', payload=dict(action_id='a', lot_id='m1', branch='middle',
                   sku='sku', sold=4)),
              dict(day=2, kind='TRANSFER_EXPIRED', payload=dict(action_id='a', lot_id='m1', branch='middle',
                   sku='sku', wasted=3))]
    moved_records(monkeypatch, moves, [e for e in events if e['kind'] != omit],
                  [dict(lot_id='m2', branch='dest', sku='sku', qty=3)], 2, actual=events)
    if omit:
        with pytest.raises(ValueError, match='capture|conservation'):
            weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=2), ('baseline_rule',))
    else:
        assert weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=2), ('baseline_rule',))['store_rule'] == dict(
            shipped_units=13, sold_units=4, wasted_units=3, on_hand_units=3, in_transit_units=0, tolerance_units=2e-5)


@pytest.mark.parametrize('field,value', [('branch', 'foreign'), ('sku', 'foreign'), ('action_id', 'foreign')])
def test_review_moved_event_identity_is_required(monkeypatch, field, value):
    from api import weekcheck
    action = dict(action_id='a', new_lot_id='m1', lot_id='original', shipped_units=10,
                  dest='dest', sku='sku', run_day=0, sellable_from=1, effective_input={'source': {'expiry_day': 2}})
    event = dict(day=1, kind='TRANSFER_SOLD', payload=dict(action_id='a', lot_id='m1', branch='dest', sku='sku', sold=10))
    actual = copy.deepcopy(event)
    event['payload'][field] = value
    moved_records(monkeypatch, [action], [event], [], 2, actual=[actual])
    with pytest.raises(ValueError, match='identity'):
        weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=2), ('baseline_rule',))


def test_review_actual_unsold_expiry_cannot_be_reclassified_as_a_sale(monkeypatch):
    from api import weekcheck
    args = step_args(10)
    args[0][0].expiry_day = 0
    sales, expired = [], []
    out, _ = capture(*args, shipments=(shipment(qty=10),), sold=sales, expired=expired)
    assert out.sold['dest'] == 0 and out.wasted['dest'] == 10 and args[0] == []
    action = dict(action_id='a', new_lot_id='moved', lot_id='lot', shipped_units=10, dest='dest', sku='sku',
                  run_day=0, sellable_from=0, effective_input={'source': {'expiry_day': 0}})
    events = [dict(day=0, kind='TRANSFER_EXPIRED', payload=dict(expired[0], action_id='a', sku='sku'))]
    moved_records(monkeypatch, [action], events, [], 0)
    assert weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=0), ('baseline_rule',))['store_rule']['sold_units'] == 0
    events.clear()  # Aggregate outcome still says waste=10; omission must fail independently per moved lot.
    with pytest.raises(ValueError, match='capture|conservation'):
        weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=0), ('baseline_rule',))


@pytest.mark.parametrize('mutation', ['omit', 'duplicate', 'branch', 'outcome', 'timing'])
def test_review_complete_lot_day_capture_is_required_even_without_transfer_activity(monkeypatch, mutation):
    from api import weekcheck
    captures, outcomes = moved_records(monkeypatch, [], [], [], 0)
    if mutation == 'omit':
        captures.clear()
    elif mutation == 'duplicate':
        captures.append(copy.deepcopy(captures[0]))
    elif mutation == 'branch':
        captures[0]['payload']['branches'].pop()
    elif mutation == 'outcome':
        outcomes[0, 'dest'] = dict(day=0, branch='dest', sku='sku', sold=1, wasted=0)
    else:
        captures[0]['payload']['sold'] = [dict(lot_id='original', branch='dest', sold=1)]
        captures[0]['payload']['timing'] = dict(original=dict(branch='dest', sellable_from=0, expiry_day=-1))
        outcomes[0, 'dest'] = dict(day=0, branch='dest', sku='sku', sold=1, wasted=0)
    with pytest.raises(ValueError, match='capture|outcome'):
        weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=0), ('do_nothing',))


def test_review_sales_exception_preserves_original_error_and_prior_profiler():
    assert 'sold' in inspect.signature(advance.observed_step).parameters
    args = step_args()
    args[2]['source'] = 'invalid demand'
    prior = sys.getprofile()
    with pytest.raises(TypeError, match='not supported'):
        capture(*args, sold=[], expired=[], timing={})
    assert sys.getprofile() is prior


def test_review_reloaded_old_moved_stock_can_sell_later_without_a_new_transfer(monkeypatch):
    from api import weekcheck
    args = step_args(10)
    args[2]['dest'] = 4
    first, first_timing = [], {}
    capture(*args, shipments=(shipment(qty=10),), sold=first, expired=[], timing=first_timing)
    moved = next(l for l in args[0] if l.lot_id == 'moved')
    assert moved.qty == 6 and first == [dict(lot_id='moved', branch='dest', sold=4)]
    # step_world reloads this exact persisted stock shape; ShelfLot retains its existing default timing.
    reloaded = ShelfLot(moved.lot_id, moved.branch, moved.qty, moved.expiry_day, moved.received_seq)
    second, timing = [], {}
    out, charges = capture([reloaded], 1, {'source': 0, 'dest': 6}, args[3], args[4],
                           sold=second, expired=[], timing=timing)
    assert second == [dict(lot_id='moved', branch='dest', sold=6)] and charges == []
    assert timing['moved']['sellable_from'] == -10**9 and out.sold['dest'] == 6
    action = dict(action_id='a', new_lot_id='moved', lot_id='lot', shipped_units=10, dest='dest', sku='sku',
                  run_day=0, sellable_from=0, effective_input={'source': {'expiry_day': moved.expiry_day}})
    events = [dict(day=d, kind='TRANSFER_SOLD', payload=dict(p, action_id='a', sku='sku'))
              for d, entries in ((0, first), (1, second)) for p in entries]
    captures, _ = moved_records(monkeypatch, [action], events, [], 1)
    captures[1]['payload']['timing'] = timing
    try:
        stock = weekcheck.closing_moved(None, dict(run_id='run', dataset_version='test', day=1), ('baseline_rule',))
    except ValueError:
        pytest.fail('actual later-day reloaded sentinel must not be mistaken for an altered dispatch arrival')
    assert stock['store_rule']['sold_units'] == 10


def test_capture_expiry_hook_omission_is_detectable_without_inventing_waste(monkeypatch):
    args = step_args(3)
    args[0][0].expiry_day = 0
    expired = []
    # A changed engine boundary must fail closed, never report a zero moved-waste figure.
    from engine import world
    original = world._add
    monkeypatch.setattr(world, '_add', lambda *a: original(*a))
    with pytest.raises(ValueError, match='incomplete expiry capture'):
        capture(*args, shipments=(shipment(qty=3),), expired=expired)


def test_capture_restores_and_chains_the_existing_thread_profiler():
    prior = sys.getprofile()
    seen = []
    def profile(frame, event, arg):
        if frame.f_code.co_name == 'step_day':
            seen.append(event)
    try:
        sys.setprofile(profile)
        capture(*step_args(), shipments=(shipment(),))
        assert sys.getprofile() is profile
        assert 'call' in seen and 'return' in seen
    finally:
        sys.setprofile(prior)


def test_capture_preserves_the_original_engine_exception_and_restores_the_hook():
    prior = sys.getprofile()
    with pytest.raises(ValueError, match='quantity must be a finite number above 0'):
        capture(*step_args(), shipments=(shipment(qty=-1),))
    assert sys.getprofile() is prior


def test_capture_refuses_an_incompatible_profiler_before_movement():
    args = step_args()
    before = copy.deepcopy(args[0])
    profiler = cProfile.Profile()
    profiler.enable()
    installed = sys.getprofile()
    monitoring = sys.monitoring.get_tool(sys.monitoring.PROFILER_ID)
    try:
        with pytest.raises(ValueError, match='incompatible profiler'):
            capture(*args, shipments=(shipment(),))
        assert sys.getprofile() is installed
        assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) == monitoring
        assert args[0] == before
    finally:
        profiler.disable()


def test_capture_is_thread_local():
    prior = sys.getprofile()
    def profile(frame, event, arg):
        pass
    try:
        sys.setprofile(profile)
        def other_thread():
            assert sys.getprofile() is None
            _, charges = capture(*step_args(), shipments=(shipment(),))
            assert sys.getprofile() is None
            return charges
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(other_thread).result(timeout=10)[0]['shipped_units'] == 3
        assert sys.getprofile() is profile
    finally:
        sys.setprofile(prior)


def test_capture_fails_closed_when_a_charge_return_is_not_observed(monkeypatch):
    from engine import world
    original = world.trip_cost
    def unobserved(*args):
        return original(*args)
    monkeypatch.setattr(world, 'trip_cost', unobserved)
    prior = sys.getprofile()
    with pytest.raises(ValueError, match='incomplete charge capture'):
        capture(*step_args(), shipments=(shipment(),))
    assert sys.getprofile() is prior


def test_headline_caps_coverage_per_branch_sku_day_and_separates_transit():
    assert callable(getattr(metrics, 'headline', None)), 'record-derived headline metrics are required'
    rows = []
    for world in metrics.WORLD_KEYS:
        rows.extend([dict(world=world, day=0, branch='a', sku='first', sold=20, demand=10,
                          wasted=1, wasted_kg=.5, revenue=100, transport=0, handling=0, disposal=1),
                     dict(world=world, day=0, branch='a', sku='second', sold=0, demand=90,
                          wasted=0, wasted_kg=0, revenue=0, transport=0, handling=0, disposal=0)])
    moved = {key: dict(shipped_units=10, sold_units=4, wasted_units=1, on_hand_units=2, in_transit_units=3)
             for key in metrics.WORLD_KEYS.values()}
    result = metrics.headline(rows, moved, {})
    assert result['shelfshift']['full_price_coverage'] == .1
    assert result['shelfshift']['sell_through_moved'] == pytest.approx(4 / 7)
    assert result['markdowns_only']['sell_through_moved'] is None
    assert result['markdowns_only']['reasons']['sell_through_moved']
    assert result['shelfshift']['action_adjusted_fulfillment'] is None
    assert result['shelfshift']['reasons']['action_adjusted_fulfillment'] == 'not recorded by the simulator'


def test_forecast_snapshot_preserves_nulls_and_rejects_a_different_run(tmp_path):
    from types import SimpleNamespace
    assert callable(getattr(metrics, 'forecast_quality', None)), 'shipped forecast report snapshot is required'
    report = dict(forecast_run_id='backtest', window={'first_day': -14, 'last_day': -1}, cutoff_day=-15,
                  wape=None, baseline_wape=None, bias=0, baseline_bias=0, coverage_80=.7, scored_days=0,
                  excluded_stockout_days=2, providers={'test': 1}, input_counts={'recorded': 0},
                  series_without_in_stock_days=1)
    path = tmp_path / 'backtest.json'
    path.write_text(json.dumps(report))
    ref = SimpleNamespace(forecast_runs={-1: {'job': 'backtest', 'forecast_run_id': 'backtest'}})
    assert metrics.forecast_quality(path, ref) == report
    report['forecast_run_id'] = 'another'
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='backtest run'):
        metrics.forecast_quality(path, ref)


def test_workload_counts_requests_separately_from_unique_observations_for_seed_and_public(monkeypatch):
    rows = []
    for world in ('shelfshift', 'baseline_checked'):
        for n, (before, after) in enumerate(((2, 3), (4, 1), (5, 5))):
            rows.append(dict(world=world, day=0, seq=n, event_kind='observation', request_id='request',
                             outcome=dict(recorded_units=before, observed_units=after)))
        for kind in ('request', 'request', 'answer', 'confirm', 'refusal'):
            rows.append(dict(world=world, day=0, seq=4, event_kind=kind, request_id='request', outcome={}))
    monkeypatch.setattr(metrics.db, 'rows', lambda *args, **kwargs: rows)
    for run_id in ('public-benchmark', 'seed-simulation'):
        value = metrics.workload(None, run_id)
        for key in ('shelfshift', 'store_rule_checked'):
            assert value[key] == dict(requests=1, lots_counted=3, corrected_up=1, corrected_down=1, nothing_to_correct=1)
        assert value['store_rule'] is None and value['markdowns_only'] is None


def test_trip_counts_include_only_wholly_unshipped_transport_in_footnote(monkeypatch):
    audit = [dict(planned_units=10, shipped_units=s, transport_amount=t) for s, t in ((0, 7), (3, 40), (10, 60))]
    monkeypatch.setattr(metrics.db, 'rows', lambda *args, **kwargs: audit)
    assert metrics.trips(None, 'run', 'baseline_rule') == dict(short=1, failed=1, transport_unshipped=7)


def test_measured_fixture_backtest_is_bound_to_its_own_run(tmp_path):
    from types import SimpleNamespace
    from tests.api.benchmark_fixtures import eligible_artifact
    root = eligible_artifact(tmp_path / 'artifact')
    manifest = json.loads((root / 'manifest.json').read_text())
    report = manifest['forecast']['backtest_report']
    assert report['test_only'] is True and report['scored_days'] > 0
    ref = SimpleNamespace(forecast_runs={}, manifest=manifest)
    assert metrics.forecast_quality(report, ref)['forecast_run_id'] == report['forecast_run_id']
    report['forecast_run_id'] = 'another-dataset'
    with pytest.raises(ValueError, match='backtest run'):
        metrics.forecast_quality(report, ref)


# Imported fixtures share the same tiny completed simulation; no extra template is built.
from tests.api.test_benchmark_publish import completed, scratch, finalize  # noqa: E402,F401
from tests.api.benchmark_fixtures import eligible_dv  # noqa: E402,F401


def key_tree(value):
    if isinstance(value, dict):
        return {k: key_tree(v) for k, v in value.items() if k != '_about'}
    if isinstance(value, list):
        return [key_tree(value[0])] if value else []
    return None


def test_integration_complete_summary_has_contract_key_tree_and_measured_totals(scratch):
    from pathlib import Path
    conn, run, ref = scratch
    _, payload = finalize(conn, run, ref)
    fixture = json.loads((Path(__file__).parent / 'fixtures/reports_summary.json').read_text())
    # These are intentionally dataset-defined leaves, not illustrative fixture metadata.
    for name in ('policy', 'assumptions'):
        fixture[name] = payload[name]
    for name in ('providers', 'input_counts', 'window'):
        fixture['forecast_quality'][name] = payload['forecast_quality'][name]
    assert key_tree(payload) == key_tree(fixture)
    assert payload['forecast_quality']['scored_days'] > 0
    for world, key in metrics.WORLD_KEYS.items():
        assert 0 <= payload['headline'][key]['full_price_coverage'] <= 1
        assert payload['decision_quality']['limit_breaks'][key] == 0
        assert payload['headline'][key]['net'] == pytest.approx(payload['money']['by_scenario'][key], abs=.01)


def test_integration_ordinary_due_transfer_uses_plain_step_without_benchmark_observation(run, monkeypatch):
    from api import db, refdata
    from tests.api.conftest import fresh
    from tests.api.test_advance import prepare_story
    action = prepare_story(run)
    assert action['run_day'] == run['day']
    def forbidden(*args, **kwargs):
        pytest.fail('ordinary sandbox must never call benchmark observation or capture inputs')
    monkeypatch.setattr(advance, 'observed_step', forbidden)
    monkeypatch.setattr(advance, 'execution_inputs', forbidden)
    with db.tx() as conn:
        current = fresh(run['run_id'])
        assert db.one(conn, 'SELECT benchmark_id FROM benchmark_runs WHERE sim_run_id=:r', r=run['run_id']) is None
        advance.step_world(conn, current, 'shelfshift', refdata.truth(run['dataset_version']))
        assert db.one(conn, 'SELECT state FROM world_actions WHERE run_id=:r AND world=:w AND action_id=:a',
                      r=run['run_id'], w='shelfshift', a=action['action_id'])['state'] == 'DONE'
        assert db.rows(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:r', r=run['run_id']) == []
        assert db.rows(conn, "SELECT kind FROM world_events WHERE run_id=:r AND kind IN "
                            "('TRANSFER_SOLD','TRANSFER_EXPIRED')", r=run['run_id']) == []


@pytest.mark.parametrize('stage,kind,mutation', [
    ('validated', 'prepared', 'version'), ('saved', 'prepared', 'version'),
    ('saved', 'decision', 'remove'), ('saved', 'decision', 'reject'),
])
def test_integration_authority_changes_after_validation_or_save_refuse_publication(scratch, stage, kind, mutation):
    from api import db, weekcheck, benchmark, refdata
    conn, run, ref = scratch
    # A durable authority record is sufficient even when this tiny run's ShelfShift world has no prepared transfer.
    seq = db.one(conn, 'SELECT coalesce(max(seq),0)+1 AS n FROM activity_log WHERE run_id=:r', r=run['run_id'])['n']
    payload = dict(rec_version='original-version', round=1) if kind == 'prepared' else dict(decision='approve')
    db.run(conn, 'INSERT INTO activity_log(run_id,seq,day,type,case_id,payload) '
                 'VALUES(:r,:s,0,:k,:c,CAST(:p AS jsonb))', r=run['run_id'], s=seq, k=kind,
           c='authority-regression', p=db.js(payload))
    assert weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version'])) == []
    rows = metrics.branch_day_rows(conn, run['run_id'], weekcheck.BENCH_WORLDS, ref)
    summary = metrics.summary(conn, run, ref, rows, weekcheck.closing_balance(conn, run))
    if stage == 'saved':
        benchmark.save_result(conn, run['run_id'], summary, rows, final_check=weekcheck.publication_check)
    if mutation == 'remove':
        db.run(conn, 'DELETE FROM activity_log WHERE run_id=:r AND seq=:s', r=run['run_id'], s=seq)
    else:
        payload.update(rec_version='changed-version') if mutation == 'version' else payload.update(decision='reject')
        db.run(conn, 'UPDATE activity_log SET payload=CAST(:p AS jsonb) WHERE run_id=:r AND seq=:s',
               r=run['run_id'], s=seq, p=db.js(payload))
    with pytest.raises(ValueError):
        if stage == 'validated':
            benchmark.save_result(conn, run['run_id'], summary, rows, final_check=weekcheck.publication_check)
        else:
            benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
    assert db.one(conn, 'SELECT published_at FROM benchmark_runs WHERE benchmark_id=:r',
                  r=run['run_id'])['published_at'] is None


@pytest.mark.parametrize('world', ['baseline_rule', 'baseline_checked'])
@pytest.mark.parametrize('mutation', ['missing', 'wrong_charge', 'wrong_shipped', 'wrong_version', 'wrong_outcome'])
def test_integration_every_action_charge_binding_is_required(scratch, world, mutation):
    from api import db, weekcheck
    conn, run, ref = scratch
    assert weekcheck.trips_paid_once(conn, run['run_id'], world) == []
    row = db.one(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:r AND world=:w LIMIT 1',
                 r=run['run_id'], w=world)
    assert row, 'active rule must produce genuine execution evidence'
    if mutation == 'missing':
        db.run(conn, 'DELETE FROM benchmark_transfer_audit WHERE sim_run_id=:r AND world=:w AND action_id=:a',
               r=run['run_id'], w=world, a=row['action_id'])
    elif mutation == 'wrong_outcome':
        db.run(conn, 'UPDATE outcomes SET transport=transport+1 WHERE run_id=:r AND world=:w AND day=:d',
               r=run['run_id'], w=world, d=row['day'])
    else:
        field, value = dict(wrong_charge=('charge_id', 'foreign-charge'),
                            wrong_shipped=('shipped_units', row['shipped_units'] + 1),
                            wrong_version=('rec_version', 'foreign-version'))[mutation]
        db.run(conn, f'UPDATE benchmark_transfer_audit SET {field}=:v WHERE sim_run_id=:r AND world=:w AND action_id=:a',
               v=value, r=run['run_id'], w=world, a=row['action_id'])
    assert weekcheck.trips_paid_once(conn, run['run_id'], world)
    assert weekcheck.validate(conn, run, ref, __import__('api.refdata', fromlist=['truth']).truth(run['dataset_version']))
    assert db.one(conn, 'SELECT status FROM benchmark_runs WHERE benchmark_id=:r', r=run['run_id'])['status'] == 'FAILED'


@pytest.mark.parametrize('world', ['baseline_rule', 'baseline_checked'])
def test_integration_equal_total_swapped_action_charges_are_rejected(scratch, world):
    from api import db, weekcheck
    conn, run, _ = scratch
    rows = db.rows(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:r AND world=:w ORDER BY action_id',
                   r=run['run_id'], w=world)
    pair = next(((a, b) for a in rows for b in rows if a['transport_amount'] != b['transport_amount']), None)
    assert pair, 'fixture must exercise independently unequal actual quotes'
    a, b = pair
    for left, right in ((a, b), (b, a)):
        db.run(conn, 'UPDATE benchmark_transfer_audit SET transport_amount=:n WHERE sim_run_id=:r AND world=:w '
                     'AND action_id=:a', n=right['transport_amount'], r=run['run_id'], w=world, a=left['action_id'])
    assert sum(x['transport_amount'] for x in pair) == a['transport_amount'] + b['transport_amount']
    assert any('per-action transport/handling' in e for e in weekcheck.trips_paid_once(conn, run['run_id'], world))


@pytest.mark.parametrize('world', ['shelfshift', 'baseline_rule', 'baseline_checked'])
def test_integration_seeded_resource_overrun_is_measured_and_blocks_publication(scratch, world):
    from api import benchmark, db, refdata, weekcheck
    import math
    conn, run, ref = scratch
    assert metrics.limit_breaks(conn, run, ref, world) == 0
    if world == 'shelfshift':
        trip = next(r for day in range(7) for origin in ref.branches for r in ref.runs_from(origin, day))
        db.run(conn, "INSERT INTO reservations(run_id,res_id,case_id,round,kind,bucket,day,qty,state) "
            "VALUES(:r,'capacity-test','capacity-test',1,'TRUCK',:bucket,:day,:qty,'COMMITTED')",
            r=run['run_id'], bucket=f'{trip.run_code}|{trip.day}', day=trip.day, qty=trip.spare_totes + 1)
    else:
        actions = db.rows(conn, "SELECT * FROM world_actions WHERE run_id=:r AND world=:w AND kind='TRANSFER' "
                              "AND state IN ('PLANNED','DISPATCHED','DONE')", r=run['run_id'], w=world)
        trucks, storage = {}, {}
        for a in actions:
            trip, product = ref.run_on(a['run_code'], a['run_day']), ref.products[a['sku']]
            totes = math.ceil(a['qty'] / product.units_per_tote)
            tk = a['run_code'], a['run_day']
            sk = a['dest'], product.zone, trip.day + trip.arrives_min // 1440
            trucks[tk], storage[sk] = trucks.get(tk, 0) + totes, storage.get(sk, 0) + totes
        candidate = None
        for a in actions:
            trip, product = ref.run_on(a['run_code'], a['run_day']), ref.products[a['sku']]
            extra = trip.spare_totes - trucks[a['run_code'], a['run_day']] + 1
            sk = a['dest'], product.zone, trip.day + trip.arrives_min // 1440
            if extra > 0 and storage[sk] + extra <= ref.free_totes(*sk):
                candidate = dict(a, action_id='capacity-test', new_lot_id='capacity-test-moved',
                                 qty=extra * product.units_per_tote, state='PLANNED')
                break
        assert candidate, 'fixture must permit an isolated truck overrun without a storage overrun'
        db.insert_rows(conn, 'world_actions', tuple(candidate), [tuple(candidate.values())])
    # This assertion checks the resource measurement itself, independently of missing audit coverage.
    assert metrics.limit_breaks(conn, run, ref, world) == 1
    failures = weekcheck.validate(conn, run, ref, refdata.truth(run['dataset_version']))
    assert f'{world}: 1 execution/resource limit break(s)' in failures
    assert db.one(conn, 'SELECT status FROM benchmark_runs WHERE benchmark_id=:r', r=run['run_id'])['status'] == 'FAILED'
    assert not db.rows(conn, 'SELECT * FROM benchmark_summary WHERE benchmark_id=:r', r=run['run_id'])
    with pytest.raises(ValueError, match='VALIDATED'):
        benchmark.publish(conn, run['run_id'], final_check=weekcheck.publication_check)
