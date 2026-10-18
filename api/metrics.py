"""Benchmark accounting from records only; never reads hidden stock or re-prices a decision."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from dataclasses import asdict, fields
import math
import json
from pathlib import Path

from . import db

# Seven days of six-decimal physical persistence can accumulate 3.5e-6 units per lot.
UNIT_TOLERANCE = 1e-5

SCENARIOS = (
    {'key': 'store_rule', 'world': 'baseline_rule', 'label': 'Modeled store rule with original records'},
    {'key': 'store_rule_checked', 'world': 'baseline_checked',
     'label': 'Modeled store rule with equivalent stock checks'},
    {'key': 'shelfshift', 'world': 'shelfshift', 'label': 'ShelfShift decisions with those checks'},
    {'key': 'markdowns_only', 'world': 'do_nothing',
     'label': 'Automatic markdowns only · no transfers or additional intervention'},
)
WORLD_KEYS = {s['world']: s['key'] for s in SCENARIOS}
ROW_FIELDS = ('scenario', 'day', 'branch', 'revenue', 'transport', 'handling', 'disposal', 'net',
              'sold', 'wasted', 'wasted_kg', 'lost', 'demand', 'moved_out', 'moved_in')


def cents(value):
    result = float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    return result if result else 0.0


def footnote_x(net_ss, net_s1, unshipped_ss, unshipped_s1):
    return cents(Decimal(str(cents(net_ss))) - Decimal(str(cents(net_s1)))
                 + Decimal(str(cents(unshipped_ss))) - Decimal(str(cents(unshipped_s1))))


def steps(values):
    a, b, c = (cents(values[k]) for k in ('store_rule', 'store_rule_checked', 'shelfshift'))
    return [dict(key=k, label=label, kind=kind, value=cents(v)) for k, label, kind, v in (
        ('store_rule', 'Modeled store rule', 'total', a),
        ('checks_change', 'Change from equivalent stock checks', 'change', b - a),
        ('decisions_change', 'Change from ShelfShift decisions vs modeled rule', 'change', c - b),
        ('shelfshift', 'ShelfShift total', 'total', c))]


def trip_class(planned, shipped):
    return 'failed' if shipped == 0 else 'short' if 0 < shipped < planned else None


def branch_day_rows(conn, run_id, worlds, ref=None):
    """Explicit complete domain, with source-branch charges from the outcome ledger."""
    if ref is None:
        from . import refdata
        run = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=run_id)
        ref = refdata.get(run['dataset_version'])
    ledger = db.rows(conn, 'SELECT * FROM outcomes WHERE run_id=:r ORDER BY world, day, branch, sku', r=run_id)
    grouped = {}
    for world in worlds:
        for day in range(7):
            for branch in sorted(ref.branches):
                grouped[world, day, branch] = dict(scenario=world, day=day, branch=branch,
                    **{f: 0.0 for f in ROW_FIELDS[3:]})
    for row in ledger:
        key = row['world'], row['day'], row['branch']
        if key not in grouped:
            raise ValueError('outcome outside expected domain')
        dest = grouped[key]
        for field in ('revenue', 'transport', 'handling', 'disposal', 'sold', 'wasted', 'lost', 'demand'):
            dest[field] += float(row[field])
        dest['wasted_kg'] += float(row['wasted']) * ref.products[row['sku']].kg_per_unit
    for a in db.rows(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:r', r=run_id):
        action = db.one(conn, 'SELECT origin FROM world_actions WHERE run_id=:r AND world=:w AND action_id=:a',
                        r=run_id, w=a['world'], a=a['action_id'])
        grouped[a['world'], a['day'], action['origin']]['moved_out'] += a['shipped_units']
        grouped[a['world'], a['day'], a['destination']]['moved_in'] += a['shipped_units']
    for row in grouped.values():
        for field in ('revenue', 'transport', 'handling', 'disposal'):
            row[field] = cents(row[field])
        row['net'] = cents(row['revenue'] - row['transport'] - row['handling'] - row['disposal'])
    return list(grouped.values())


def money(rows):
    totals = {key: cents(sum(r['net'] for r in rows if WORLD_KEYS.get(r['scenario']) == key))
              for key in WORLD_KEYS.values()}
    days = [dict(day=day, **{key: cents(sum(r['net'] for r in rows
            if r['day'] == day and WORLD_KEYS.get(r['scenario']) == key)) for key in totals}) for day in range(7)]
    return dict(by_scenario=totals, steps=steps(totals), by_day=days)


def waste(rows, ref=None):
    totals = {key: {'units': sum(r['wasted'] for r in rows if WORLD_KEYS.get(r['scenario']) == key),
                    'kg': sum(r['wasted_kg'] for r in rows if WORLD_KEYS.get(r['scenario']) == key)}
              for key in WORLD_KEYS.values()}
    days = [dict(day=day, **{key: sum(r['wasted'] for r in rows if r['day'] == day
            and WORLD_KEYS.get(r['scenario']) == key) for key in totals}) for day in range(7)]
    base = totals['markdowns_only']
    return dict(by_day=days, totals=totals,
                change_vs_markdowns_only={k: {f: v[f] - base[f] for f in base} for k, v in totals.items()})


def headline(rows, moved, closing):
    """Demand coverage is capped on each branch/product/day before aggregation."""
    result = {}
    base = [r for r in rows if r['world'] == 'do_nothing']
    base_sold = sum(r['sold'] for r in base)
    base_covered = sum(min(r['sold'], r['demand']) for r in base)
    for world, key in WORLD_KEYS.items():
        ledger = [r for r in rows if r['world'] == world]
        demand = sum(r['demand'] for r in ledger)
        covered = sum(min(r['sold'], r['demand']) for r in ledger)
        stock = moved.get(key, {})
        shipped, transit = stock.get('shipped_units', 0), stock.get('in_transit_units', 0)
        received = shipped - transit
        if 'sold_units' not in stock:
            raise ValueError('missing moved-lot sales evidence')
        sold_moved = stock['sold_units']
        reasons = dict(full_price_coverage=None, shortfall_filled=None, sell_through_moved=None,
                       action_adjusted_fulfillment='not recorded by the simulator')
        coverage = covered / demand if demand else None
        if coverage is None:
            reasons['full_price_coverage'] = 'no recorded demand in this window'
        tolerance = stock.get('tolerance_units', UNIT_TOLERANCE)
        if min(shipped, transit, received, sold_moved) < -tolerance or sold_moved > received + tolerance:
            raise ValueError('inconsistent moved-stock closing balance')
        sell_through = sold_moved / received if received > 0 and world != 'do_nothing' else None
        if sell_through is None:
            reasons['sell_through_moved'] = ('scenario has no transfers' if world == 'do_nothing'
                                            else 'no moved units received in this window')
        result[key] = dict(net=cents(sum(r['revenue'] - r['transport'] - r['handling'] - r['disposal']
                                         for r in ledger)), full_price_coverage=coverage,
            shortfall_filled=covered - base_covered, action_adjusted_fulfillment=None,
            incremental_units_sold_vs_markdowns_only=sum(r['sold'] for r in ledger) - base_sold,
            units_wasted=sum(r['wasted'] for r in ledger), kg_wasted=sum(r['wasted_kg'] for r in ledger),
            sell_through_moved=sell_through, reasons=reasons)
    return result


def forecast_quality(report_path, ref):
    report = dict(report_path) if isinstance(report_path, dict) else json.loads(Path(report_path).read_text())
    runs = list(ref.forecast_runs.values())
    runs += getattr(ref, 'manifest', {}).get('forecast', {}).get('runs', [])
    runs += getattr(ref, 'forecast', {}).get('runs', [])
    backtests = {r['forecast_run_id'] for r in runs if r.get('job') == 'backtest'}
    if report.get('forecast_run_id') not in backtests or len(backtests) != 1:
        raise ValueError('forecast report differs from the dataset backtest run')
    fields = ('forecast_run_id', 'window', 'cutoff_day', 'wape', 'baseline_wape', 'bias', 'baseline_bias',
              'coverage_80', 'scored_days', 'excluded_stockout_days', 'providers', 'input_counts',
              'series_without_in_stock_days')
    if any(f not in report for f in fields):
        raise ValueError('incomplete shipped forecast report')
    return {f: report[f] for f in fields}


def constraint_reasons(snapshot):
    """Replay hard limits on the complete frozen raw input, never on today's records."""
    from engine.canonical import from_dict, rec_version, to_dict
    from engine.adjust import effective
    from engine.limits import check_plan
    from engine.model import EngineInput, Plan
    from engine.timing import first_sellable_day
    reasons = list(snapshot.get('input_errors', ()))
    try:
        raw = snapshot['raw_input']
        required = {f.name for f in fields(EngineInput)} - {'applied_overrides'}
        if snapshot['schema_version'] != 1 or not isinstance(raw, dict) or set(raw) != required:
            return reasons + ['incomplete execution input']
        inp = from_dict(raw)
        action, plan = snapshot['action'], Plan(**snapshot['plan'])
        if rec_version(inp) != snapshot['execution_input_version']:
            reasons.append('execution input version differs')
        if to_dict(effective(inp)) != snapshot['effective_input']:
            reasons.append('effective execution input differs')
        if not snapshot['rec_version'] or (action['case_id'] and snapshot['round'] is None):
            reasons.append('missing original decision identity')
        if not action['case_id'] and snapshot['rec_version'] != snapshot['execution_input_version']:
            reasons.append('baseline execution version differs')
        if (inp.case_lot_id, inp.today, inp.product.sku, plan.dest, plan.run_code, plan.qty) != (
                action['lot_id'], action['run_day'], action['sku'], action['dest'], action['run_code'], action['qty']):
            reasons.append('action differs from execution input')
        trip = next(r for r in inp.runs if r.run_code == plan.run_code)
        if action['sellable_from'] != first_sellable_day(trip.day, trip.arrives_min, inp.policy):
            reasons.append('executed sellability differs from run arrival policy')
        source = snapshot['source']
        if source['set_aside_units'] > source['recorded_units'] + 1e-9:
            reasons.append('source units booked twice')
        if any(not math.isfinite(source[k]) or source[k] < 0 for k in
               ('recorded_units', 'physical_units', 'set_aside_units', 'available_before')):
            reasons.append('invalid source execution quantities')
        reasons.extend(check_plan(inp, plan))
    except (KeyError, TypeError, ValueError, LookupError, StopIteration) as exc:
        reasons.append(f'incomplete or invalid execution evidence: {type(exc).__name__}: {exc}')
    return reasons


def trips(conn, run_id, world):
    audit = db.rows(conn, 'SELECT planned_units,shipped_units,transport_amount FROM benchmark_transfer_audit '
                         'WHERE sim_run_id=:r AND world=:w', r=run_id, w=world)
    values = [(a['planned_units'], a['shipped_units'], a['transport_amount']) for a in audit]
    if not audit:
        values = [(e['payload']['planned'], e['payload']['shipped'], e['payload']['transport_paid']) for e in
                  db.rows(conn, "SELECT payload FROM world_events WHERE run_id=:r AND world=:w "
                                "AND kind='SHORT_SHIPMENT'", r=run_id, w=world)]
    return dict(short=sum(trip_class(p, s) == 'short' for p, s, _ in values),
                failed=sum(trip_class(p, s) == 'failed' for p, s, _ in values),
                transport_unshipped=cents(sum(t for _, s, t in values if s == 0)))


def limit_breaks(conn, run, ref, world):
    """Count failed action checks and independently measured resource overruns."""
    audits = db.rows(conn, 'SELECT effective_input FROM benchmark_transfer_audit WHERE sim_run_id=:r AND world=:w',
                     r=run['run_id'], w=world)
    count = sum(bool(constraint_reasons(a['effective_input'])) for a in audits)
    if world == 'shelfshift':
        held = db.rows(conn, "SELECT kind,bucket,day,sum(qty) AS q FROM reservations WHERE run_id=:r "
            "AND state IN ('COMMITTED','DONE') AND kind IN ('STORAGE','TRUCK') GROUP BY kind,bucket,day", r=run['run_id'])
        for row in held:
            branch, _, zone = row['bucket'].partition('|')
            trip = ref.run_on(branch, int(zone)) if row['kind'] == 'TRUCK' else None
            cap = (trip.spare_totes if trip else None) if row['kind'] == 'TRUCK' else ref.free_totes(branch, zone, row['day'])
            count += cap is None or row['q'] > cap + 1e-9
    elif world in ('baseline_rule', 'baseline_checked'):
        groups = {}
        for a in db.rows(conn, "SELECT * FROM world_actions WHERE run_id=:r AND world=:w AND kind='TRANSFER' "
                              "AND state IN ('PLANNED','DISPATCHED','DONE')", r=run['run_id'], w=world):
            product, trip = ref.products[a['sku']], ref.run_on(a['run_code'], a['run_day'])
            totes = math.ceil(a['qty'] / product.units_per_tote)
            for key in (('truck', a['run_code'], a['run_day']),
                        ('storage', a['dest'], product.zone, trip.day + trip.arrives_min // 1440) if trip else
                        ('missing', a['action_id'])):
                groups[key] = groups.get(key, 0) + totes
        for key, quantity in groups.items():
            if key[0] == 'truck':
                trip = ref.run_on(key[1], key[2])
                cap = trip.spare_totes if trip else None
            elif key[0] == 'storage':
                cap = ref.free_totes(key[1], key[2], key[3])
            else:
                cap = None
            count += cap is None or quantity > cap + 1e-9
    return int(count)


def workload(conn, benchmark_id):
    """Durable history works for evaluation and seed simulations alike.

    An observation records outcome.recorded_units and outcome.observed_units;
    those are the before/after quantities, not a repeated answer or confirmation.
    """
    result = {key: None for key in WORLD_KEYS.values()}
    history = db.rows(conn, 'SELECT * FROM evaluation_check_history WHERE sim_run_id=:r OR benchmark_id=:r '
                           'ORDER BY day,world,seq', r=benchmark_id)
    for world in ('shelfshift', 'baseline_checked'):
        rows = [h for h in history if h['world'] == world]
        observations = [h for h in rows if h['event_kind'] == 'observation']
        quantities = []
        for h in observations:
            o = h['outcome']
            if 'recorded_units' not in o or 'observed_units' not in o:
                raise ValueError('observation lacks recorded/observed workload quantities')
            quantities.append((o['recorded_units'], o['observed_units']))
        result[WORLD_KEYS[world]] = dict(requests=len({h['request_id'] for h in rows
            if h['event_kind'] == 'request' and h['request_id']}), lots_counted=len(observations),
            corrected_up=sum(b > a for a, b in quantities), corrected_down=sum(b < a for a, b in quantities),
            nothing_to_correct=sum(a == b for a, b in quantities))
    return result


def decision_quality(conn, run, ref, day_stats, retry_limit):
    logs = db.rows(conn, 'SELECT type,case_id,payload FROM activity_log WHERE run_id=:r', r=run['run_id'])
    left = {}
    for day in day_stats:
        for reason, cases in day.get('left_open', {}).items():
            left[reason] = left.get(reason, 0) + (len(cases) if isinstance(cases, list) else int(cases))
    counts = {kind: len({r['case_id'] for r in logs if r['type'] == kind})
              for kind in ('opened', 'rejected', 'waiting')}
    # D-26: the app path commits every plan, Keep included, through the source's approve; the Keep fast path writes
    # only its `committed` entry, so a committed case counts as approved exactly as the app path would count it.
    approved = len({r['case_id'] for r in logs if (r['type'] == 'decision'
                    and r['payload'].get('decision') == 'approve') or r['type'] == 'committed'})
    return dict(limit_breaks={key: limit_breaks(conn, run, ref, world) for world, key in WORLD_KEYS.items()},
                approved=approved, **counts, left_open=left, retry_limit=retry_limit, time_to_decision=None)


def categories_without_schedule(ref):
    missing = [sku for sku in ref.products if not ref.schedules[sku]]
    if getattr(ref, 'kind', None) == 'public':
        from . import publicref
        categories = {r['sku']: r['category'] for r in publicref._read(
            'SELECT sku,category FROM ref_product WHERE dataset_version=:v', v=ref.dataset_version)}
    else:
        categories = {p['sku']: p.get('category', p['name']) for p in ref.seed['products']}
    return sorted({categories[sku] for sku in missing})


def summary(conn, run, ref, rows, closing):
    from . import benchmark, refdata
    totals, trip = money(rows), {key: trips(conn, run['run_id'], world) for world, key in WORLD_KEYS.items()}
    checkpoints = db.rows(conn, 'SELECT day,stats FROM benchmark_days WHERE benchmark_id=:r ORDER BY day', r=run['run_id'])
    days = [c['stats'] for c in checkpoints]
    at_risk = [c['stats'].get('worlds', {}).get('shelfshift', {}).get('at_risk_lots') for c in checkpoints]
    if any(value is None for value in at_risk):
        raise ValueError('checkpoint lacks morning at-risk count')
    row = db.one(conn, 'SELECT validation_receipt FROM benchmark_runs WHERE sim_run_id=:r', r=run['run_id'])
    moved = row['validation_receipt'].get('moved_closing') if row and row['validation_receipt'] else None
    if moved is None:
        raise ValueError('validated moved-stock closing evidence required')
    ledger = db.rows(conn, 'SELECT * FROM outcomes WHERE run_id=:r', r=run['run_id'])
    for entry in ledger:
        entry['wasted_kg'] = entry['wasted'] * ref.products[entry['sku']].kg_per_unit
    report = getattr(ref, 'manifest', {}).get('forecast', {}).get('backtest_report')
    if report is None:
        report = refdata.REF / 'reports' / 'public-backtest.json'
    settings = benchmark.settings_for(ref)
    assumptions = dict(markdown_response=(ref.settings.get('markdown_response') or
                                         getattr(ref, 'seed', {}).get('markdown_assumption')),
                       stock_record_errors=(ref.settings.get('stock_record_errors') or
                           ref.settings.get('designed') or
                           getattr(ref, 'seed', {}).get('error_model')))
    if any(value is None for value in assumptions.values()):
        raise ValueError('dataset lacks benchmark assumptions')
    return dict(kind='SIMULATED', scenarios=list(SCENARIOS), network=dict(stores=len(ref.branches),
        at_risk_lots_by_day=[dict(day=c['day'], lots=n) for c, n in zip(checkpoints, at_risk)]),
        policy=asdict(ref.policy), money=totals, waste=waste(rows),
        footnote_x=footnote_x(totals['by_scenario']['shelfshift'], totals['by_scenario']['store_rule'],
                             trip['shelfshift']['transport_unshipped'], trip['store_rule']['transport_unshipped']),
        headline=headline(ledger, moved, closing), checks=workload(conn, run['run_id']), trips=trip, closing=closing,
        decision_quality=decision_quality(conn, run, ref, days, settings['commits']['retry_limit']),
        forecast_quality=forecast_quality(report, ref), settings=settings, assumptions=assumptions,
        categories_without_schedule=categories_without_schedule(ref))
