"""Evaluation identity, settings and the benchmark's lifecycle (D-02, D-03, D-21, D-24, D-36): versions, refusals,
create_run, day checkpoints, resume claims, the saved result and publication.

Reads evaluation.json and Ref metadata; physical stock only where create_run sets up the worlds and day 0
(sandbox.init_worlds, advance.day_start); never the evaluator driver (api/weekcheck.py).
Code/image provenance is deliberately outside the six-version currentness comparison.
"""
from __future__ import annotations

import json
import math
import re
import shlex
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import DBAPIError

from data.versions import digest
from data import forecast as F
from engine.model import ENGINE_VERSION

from . import advance, config, db, refdata, sandbox

# Bump whenever evaluation output changes: counts, fourth world, aggregation, closing or commit rule.
EVAL_VERSION = "eval-1"
VERSION_KEYS = ("engine", "dataset", "policy", "forecast_runs", "evaluator", "settings")
SETTINGS_FILE = Path(__file__).with_name('evaluation.json')


def load_settings(path=None) -> dict:
    tree = json.loads(Path(path or SETTINGS_FILE).read_text())
    required = {'retry_limit', 'tie_threshold', 'waste_price_info'}
    if not isinstance(tree, dict) or set(tree) != required:
        raise ValueError('evaluation: requires retry_limit, tie_threshold and waste_price_info')
    values = {}
    for name, leaf in tree.items():
        if not isinstance(leaf, dict) or not {'value', 'unit', 'type', 'source'} <= leaf.keys():
            raise ValueError(f'evaluation.{name}: requires value, unit, type and source')
        if any(not isinstance(leaf[k], str) or not leaf[k].strip() for k in ('unit', 'type', 'source')):
            raise ValueError(f'evaluation.{name}: metadata must be nonblank text')
        v = leaf['value']
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f'evaluation.{name}: requires a finite nonnegative number')
        if name == 'retry_limit' and not isinstance(v, int):
            raise ValueError(f'evaluation.{name}: requires a whole number')
        values[name] = v
    return values


def ordering_state(ref, *, allow_enabled=False) -> dict:
    """Effective placement list, not mere block presence. Active ordering needs D-33 enforcement.

    Current release has no block. Explicit enablement configs are validated before use. Datasets
    with the replenishment schema use the same readiness predicate as their Orders implementation.
    """
    ordering = ref.settings.get('ordering')
    enabled = []
    if ordering is not None:
        if not isinstance(ordering, dict) or not ordering:
            raise ValueError('ordering: malformed configuration')
        control = {'enabled', 'enabled_default', 'enabled_stores'}
        if set(ordering) <= control:
            for key in ('enabled', 'enabled_default'):
                if key in ordering and not isinstance(ordering[key], bool):
                    raise ValueError(f'ordering.{key}: must be boolean')
            enabled = ordering.get('enabled_stores', [])
            if not isinstance(enabled, list) or any(not isinstance(s, str) or not s.strip() for s in enabled):
                raise ValueError('ordering.enabled_stores: must be a list of store codes')
            if ordering.get('enabled_default', False):
                if not hasattr(ref, 'branches'):
                    raise ValueError('ordering.enabled_default: requires the dataset store list')
                enabled = list(ref.branches)
            if ordering.get('enabled') is False:
                enabled = []
            elif 'enabled_stores' not in ordering and 'enabled_default' not in ordering:
                raise ValueError('ordering: missing enabled-store configuration')
        else:
            # This schema is installed with the ordering dataset, not inferred from a block name.
            if not callable(getattr(ref, 'ordering_policy', None)):
                raise ValueError('ordering: unsupported configuration')
            from .orders import readiness
            enabled = [s for s in ref.branches if readiness(ref, s)['placement_enabled']]
    enabled = list(dict.fromkeys(enabled))
    if enabled and not allow_enabled:
        raise ValueError('ordering: enabled stores require modeled-store-rule destination enforcement (D-33/D-36)')
    return {'enabled_stores': enabled, 'rule': 'store-rule ordering limits deferred (D-33)' if enabled
            else 'no store enabled'}


def settings_for(ref, *, trial=False) -> dict:
    values = load_settings()
    ordering = ordering_state(ref, allow_enabled=trial)
    response = ref.settings.get('truth', {}).get('price_response')
    if response is not None and (not isinstance(response, dict) or set(response) != {'m20', 'm30'} or
                                 any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                                     for v in response.values())):
        raise ValueError('price_response: m20 and m30 must be finite numbers')
    if response is not None and not trial and any(v != 1.0 for v in response.values()):
        raise ValueError('price_response: evaluation requires sales replay, m20 and m30 both 1.0')
    if getattr(ref, 'kind', None) == 'public' and response is None:
        raise ValueError('price_response: public evaluation requires dataset settings.truth.price_response')
    return {
        'forecast_rule': 'latest issued before the day; d0..d3 on days 0..3, d3 target dates on days 4..6; stop on a missing target',
        'closing_rule': "after day 6's world step, before day 7 starts; closing stock and transit are neither revenue nor waste",
        'checks': {'evidence': ['PHANTOM_STOCK', 'STALE_COUNT'], 'cap_per_version': config.MAX_REQUESTS_PER_VERSION,
                   'lot_once_per_day': True, 'answers': "exact, from each world's own physical stock"},
        'cancel_cost': 'ShelfShift net minus scenario 1 net plus wholly unshipped ShelfShift transport minus wholly unshipped scenario 1 transport; decisions and stock fixed; partial shipments keep the full quote',
        'keep_fast_path': True, 'price_response': None if response is None else dict(response),
        'commits': {'retry_limit': values['retry_limit'], 'retry_codes': ['conflict', 'hold_expired', 'stale_version'],
                    'leave_open': 'any other refusal, recorded by reason', 'order': 'case_id ascending'},
        'ordering': ordering, 'sensitivity': None}


def settings_digest(ref) -> str:
    return digest('es-', settings_for(ref))


def app_versions(run: dict, ref, *, settings=None) -> dict:
    manifest = ref.manifest if hasattr(ref, 'manifest') else ref.seed['dataset']
    return {'engine': ENGINE_VERSION, 'dataset': run['dataset_version'],
            'policy': manifest['reference_fingerprint'],
            'forecast_runs': list(dict.fromkeys(r['forecast_run_id'] for _, r in sorted(ref.forecast_runs.items())
                                                if r.get('job') != 'backtest')),
            'evaluator': EVAL_VERSION,
            'settings': settings_digest(ref) if settings is None else digest('es-', settings)}


def refusals(ref, *, trial) -> list[str]:
    """All unfair-input reasons, including for a trial's permanent unpublishable record.

    Only declared settings and reference forecast providers are read here, never physical stock or demand.
    """
    reasons = []
    try:
        ordering = ordering_state(ref, allow_enabled=True)
    except ValueError as error:
        reasons.append(str(error))
    else:
        if ordering['enabled_stores']:
            reasons.append('ordering is enabled in this dataset; the store-rule ordering limits (D-33) are not built')
    response = ref.settings.get('truth', {}).get('price_response')
    if ((getattr(ref, 'kind', None) == 'public' and response is None) or
            response is not None and (not isinstance(response, dict) or set(response) != {'m20', 'm30'} or
                                      any(isinstance(v, bool) or v != 1.0 for v in response.values()))):
        reasons.append('price response must be 1.0 (D-28)')
    forecast = ref.forecast
    runs = [*forecast.get('runs', []), *ref.forecast_runs.values()]
    local = (forecast.get('stand_in', False) or forecast.get('providers', {}).get(F.LOCAL, 0) > 0 or
             any(r.get('providers', {}).get(F.LOCAL, 0) > 0 for r in runs) or
             any(group.get('by_provider', {}).get(F.LOCAL, 0) > 0
                 for group in (forecast.get('provider_summary', {}).get('demo', {}),
                               forecast.get('provider_summary', {}).get('backtest', {}))))
    if getattr(ref, 'kind', None) == 'public':
        # Check actual points too: metadata alone must not hide a stand-in series.
        local = bool(refdata._read('SELECT 1 FROM ref_forecast_point WHERE dataset_version = :v '
                                   'AND provider = :p LIMIT 1', v=ref.dataset_version, p=F.LOCAL)) or local
    else:
        local = (F.LOCAL in ref.point_provider.values() or
                 any(p.get('provider') == F.LOCAL for p in forecast.get('points', [])) or local)
    if local:
        reasons.append('forecasts come from the local stand-in')
    return reasons


BENCHMARK_ID = re.compile(r'bm-[0-9a-f]{12}')


def new_id() -> str:
    return 'bm-' + uuid.uuid4().hex[:12]


def create_run(conn, dataset_version, *, trial, git_commit, image_digest, worlds, benchmark_id=None):
    """Create an isolated day-zero simulation. Its TEMPLATE stays INITIALIZING for life.

    `benchmark_id` (from `new_id`) lets the job open its engine-cache namespace before the day-0 morning runs here
    (08-15); without it a fresh id is drawn."""
    ref = refdata.get(dataset_version)
    reasons = refusals(ref, trial=trial)
    if reasons and not trial:
        raise ValueError('; '.join(reasons))
    settings = settings_for(ref, trial=trial)
    rid = new_id() if benchmark_id is None else benchmark_id
    if not isinstance(rid, str) or not BENCHMARK_ID.fullmatch(rid):
        raise ValueError('a benchmark id is bm- and 12 lowercase hex')
    versions = app_versions({'dataset_version': dataset_version}, ref, settings=settings)
    run = db.one(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) "
                       "VALUES (:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1) RETURNING *", r=rid, v=dataset_version)
    sandbox.init_worlds(conn, rid, ref, refdata.truth(dataset_version), worlds=worlds)
    advance.day_start(conn, run, worlds=worlds)
    advance.cases_morning(conn, run, eager=True)
    db.run(conn, "INSERT INTO benchmark_runs (benchmark_id, status, trial, sim_run_id, dataset_version, "
                 "engine_version, policy_version, forecast_runs, eval_version, settings_digest, settings, "
                 "image_digest, git_commit, first_day, last_day, expected_domain, stats) VALUES "
                 "(:r, :status, :trial, :r, :v, :engine, :policy, CAST(:forecasts AS jsonb), :eval, :digest, "
                 "CAST(:settings AS jsonb), :image, :git, 0, 6, CAST(:domain AS jsonb), CAST(:stats AS jsonb))",
           r=rid, status='TRIAL' if trial else 'RUNNING', trial=trial, v=dataset_version,
           engine=versions['engine'], policy=versions['policy'], forecasts=db.js(versions['forecast_runs']),
           eval=versions['evaluator'], digest=versions['settings'], settings=db.js(settings), image=image_digest,
           git=git_commit, domain=db.js({'worlds': list(worlds), 'branches': sorted(ref.branches), 'days': list(range(7))}),
           stats=db.js({'refusals': reasons}))
    return run


def mark_day(conn, benchmark_id, day, elapsed_s, stats):
    """The checkpoint is committed in exactly the same transaction as all world rows."""
    row = _locked(conn, benchmark_id)
    if row['status'] not in ('RUNNING', 'TRIAL') or row['published_at'] is not None:
        raise ValueError('checkpoint requires RUNNING or TRIAL unpublished benchmark')
    if day not in range(7) or row['last_day_done'] != (day - 1 if day else None):
        raise ValueError('checkpoint must be the next ordered day 0..6')
    stats = {**stats, 'outcomes_digest': digest('out-', db.rows(conn,
        'SELECT * FROM outcomes WHERE run_id=:r AND day=:d ORDER BY world, branch, sku', r=benchmark_id, d=day))}
    db.run(conn, "INSERT INTO benchmark_days (benchmark_id, day, completed_at, elapsed_s, stats) "
                 "VALUES (:b, :d, now(), :elapsed, CAST(:stats AS jsonb))",
           b=benchmark_id, d=day, elapsed=elapsed_s, stats=db.js(stats))
    db.run(conn, "UPDATE benchmark_runs SET last_day_done = :d WHERE benchmark_id = :b", b=benchmark_id, d=day)


def _locked(conn, benchmark_id):
    # A refused NOWAIT must not poison the caller's transaction. Releasing the
    # savepoint on success retains the row lock until the outer transaction ends.
    try:
        with conn.begin_nested():
            row = db.one(conn, 'SELECT * FROM benchmark_runs WHERE benchmark_id=:b FOR UPDATE NOWAIT', b=benchmark_id)
    except DBAPIError as error:
        detail = error.orig.args[0] if error.orig.args else None
        code = getattr(error.orig, 'sqlstate', None) or (detail.get('C') if isinstance(detail, dict) else None)
        if code == '55P03':
            raise ValueError('benchmark is already claimed by another worker') from error
        raise
    if row is None:
        raise ValueError('unknown benchmark')
    return row


def versions_of(row):
    return dict(engine=row['engine_version'], dataset=row['dataset_version'], policy=row['policy_version'],
                forecast_runs=row['forecast_runs'], evaluator=row['eval_version'], settings=row['settings_digest'])


def can_resume(row, image_digest, git_commit, versions, settings_digest):
    """Resume identity is stricter than a published result's six-version currentness."""
    fresh = ('start a fresh benchmark: python -m api.weekcheck benchmark start --dataset '
             + shlex.quote(str(row.get('dataset_version') or '<dataset-version>')))
    def refused(reason):
        return False, reason + '; ' + fresh
    if row.get('status') not in ('RUNNING', 'FAILED'):
        return refused('status must be RUNNING or FAILED')
    if row.get('trial') is not False:
        return refused('trial benchmark cannot resume')
    if row.get('published_at') is not None:
        return refused('published benchmark cannot resume')
    for field, supplied, name in (('image_digest', image_digest, 'image digest'),
                                  ('git_commit', git_commit, 'git commit'),
                                  ('settings_digest', settings_digest, 'settings digest')):
        if not isinstance(supplied, str) or not supplied.strip() or row.get(field) != supplied:
            return refused(name + ' differs or is missing')
    if not isinstance(versions, dict) or set(versions) != set(VERSION_KEYS):
        return refused('exactly six versions are required')
    columns = dict(engine='engine_version', dataset='dataset_version', policy='policy_version',
                   forecast_runs='forecast_runs', evaluator='eval_version', settings='settings_digest')
    for key in VERSION_KEYS:
        if row.get(columns[key]) != versions[key]:
            return refused(key + ' version differs')
    return True, ''


def _resume_checkpoint(conn, row):
    """Read checkpoint evidence only after the caller has acquired the benchmark lock."""
    if row['status'] not in ('RUNNING', 'FAILED') or row['trial'] or row['published_at'] is not None:
        raise ValueError('resume requires RUNNING or FAILED unpublished non-trial benchmark')
    domain = row['expected_domain'] or {}
    worlds = ['shelfshift', 'baseline_rule', 'baseline_checked', 'do_nothing']
    if (domain.get('worlds') != worlds or domain.get('days') != list(range(7))
            or domain.get('branches') != sorted(refdata.get(row['dataset_version']).branches)
            or row['first_day'] != 0 or row['last_day'] != 6):
        raise ValueError('resume requires the complete benchmark domain')
    days = db.rows(conn, 'SELECT * FROM benchmark_days WHERE benchmark_id=:b ORDER BY day', b=row['benchmark_id'])
    if len(days) > 7 or [d['day'] for d in days] != list(range(len(days))):
        raise ValueError('resume requires contiguous ordered checkpoints from day 0')
    last = days[-1]['day'] if days else None
    if row['last_day_done'] != last:
        raise ValueError('resume checkpoint and last completed day differ')
    simulation = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=row['sim_run_id'])
    if (simulation is None or simulation['kind'] != 'TEMPLATE' or simulation['status'] != 'INITIALIZING'
            or simulation['dataset_version'] != row['dataset_version'] or row['sim_run_id'] != row['benchmark_id']
            or simulation['day'] != (0 if last is None else last)):
        raise ValueError('resume simulation identity or checkpoint day differs')
    for day in days:
        stats = day['stats']
        if (stats.get('completed') is not True or set(stats.get('worlds', {})) != set(worlds)
                or any(stats['worlds'][w].get('completed') is not True for w in worlds)):
            raise ValueError(f"resume requires complete world checkpoint {day['day']}")
        outcomes = db.rows(conn, 'SELECT * FROM outcomes WHERE run_id=:r AND day=:d ORDER BY world, branch, sku',
                           r=row['sim_run_id'], d=day['day'])
        if stats.get('outcomes_digest') != digest('out-', outcomes):
            raise ValueError(f"resume outcome checkpoint differs on day {day['day']}")
    return 0 if last is None else last + 1


def resume_day(conn, benchmark_id):
    """Return the next complete-day boundary, retaining the lock in the caller's transaction.

    Execution must use claim_resume, which checks the running build under this same lock.
    """
    return _resume_checkpoint(conn, _locked(conn, benchmark_id))


def claim_resume(conn, benchmark_id, *, image_digest, git_commit, versions, settings_digest):
    """Claim one day transaction: lock, verify identity, then read its checkpoint.

    Keep this transaction open through all world steps and mark_day. Reacquire and
    recheck before each later day; committing a claim separately is not a lease.
    A concurrent claimant is refused immediately without aborting its transaction.
    """
    row = _locked(conn, benchmark_id)
    allowed, reason = can_resume(row, image_digest, git_commit, versions, settings_digest)
    if not allowed:
        raise ValueError(reason)
    if digest('es-', row['settings']) != row['settings_digest']:
        raise ValueError('stored settings digest differs; start a fresh benchmark: '
                         'python -m api.weekcheck benchmark start --dataset '
                         + shlex.quote(row['dataset_version']))
    day = _resume_checkpoint(conn, row)
    if row['status'] == 'FAILED':
        db.run(conn, "UPDATE benchmark_runs SET status='RUNNING', failure=NULL, validation_receipt=NULL "
                     'WHERE benchmark_id=:b', b=benchmark_id)
    return day


def record_digest(conn, benchmark_id):
    """Bind the immutable execution ledger and effective booking records, with stable ordering."""
    parts = {}
    for table, order in (('outcomes', 'world, day, branch, sku'), ('world_actions', 'world, action_id'),
                         ('world_events', 'world, day, seq'), ('reservations', 'case_id, round, kind, bucket, day'),
                         ('lots', 'world, lot_id'), ('inbound', 'world, inbound_id'), ('run_policy', 'field'),
                         ('activity_log', 'seq')):
        parts[table] = db.rows(conn, f'SELECT * FROM {table} WHERE run_id=:b ORDER BY {order}', b=benchmark_id)
    parts['audit'] = db.rows(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:b '
                                  'ORDER BY world, action_id', b=benchmark_id)
    parts['observations'] = db.rows(conn, 'SELECT * FROM benchmark_observations WHERE benchmark_id=:b '
                                         'ORDER BY day, seq', b=benchmark_id)
    parts['history'] = db.rows(conn, 'SELECT * FROM evaluation_check_history WHERE sim_run_id=:b '
                                    'ORDER BY day, world, seq', b=benchmark_id)
    return digest('records-', _digestable(parts))


def _digestable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _digestable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_digestable(v) for v in value]
    return value


def domain_errors(conn, row):
    ref = refdata.get(row['dataset_version'])
    expected = dict(worlds=['shelfshift', 'baseline_rule', 'baseline_checked', 'do_nothing'],
                    branches=sorted(ref.branches), days=list(range(7)))
    errors = []
    if row['trial'] or row['status'] == 'TRIAL':
        errors.append('trial cannot publish')
    if row['expected_domain'] != expected or row['first_day'] != 0 or row['last_day'] != 6:
        errors.append('expected full-network domain differs')
    days = db.rows(conn, 'SELECT * FROM benchmark_days WHERE benchmark_id=:b ORDER BY day', b=row['benchmark_id'])
    if [r['day'] for r in days] != list(range(7)) or row['last_day_done'] != 6:
        errors.append('complete ordered checkpoints 0..6 required')
    for day in days:
        stats = day['stats']
        if stats.get('completed') is not True or set(stats.get('worlds', {})) != set(expected['worlds']) or any(
                stats.get('worlds', {}).get(w, {}).get('completed') is not True for w in expected['worlds']):
            errors.append(f"incomplete world checkpoint {day['day']}")
        rows = db.rows(conn, 'SELECT * FROM outcomes WHERE run_id=:r AND day=:d ORDER BY world, branch, sku',
                       r=row['sim_run_id'], d=day['day'])
        if stats.get('outcomes_digest') != digest('out-', rows):
            errors.append(f"outcome checkpoint differs on day {day['day']}")
    if digest('es-', row['settings']) != row['settings_digest']:
        errors.append('settings digest differs')
    if versions_of(row) != app_versions({'dataset_version': row['dataset_version']}, ref):
        errors.append('installed six versions differ')
    sim = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=row['sim_run_id'])
    if sim is None or sim['day'] != 6:
        errors.append('closing must follow day 6 before day 7')
    if (sim is None or sim['kind'] != 'TEMPLATE' or sim['status'] != 'INITIALIZING'
            or sim['dataset_version'] != row['dataset_version'] or row['sim_run_id'] != row['benchmark_id']):
        errors.append('immutable simulation identity differs')
    return errors


def fail(conn, benchmark_id, reasons):
    row = _locked(conn, benchmark_id)
    if row['status'] == 'PUBLISHED' or row['published_at'] is not None:
        raise ValueError('published benchmark is immutable')
    db.run(conn, "UPDATE benchmark_runs SET status='FAILED', failure=:f, validation_receipt=NULL "
                 'WHERE benchmark_id=:b', b=benchmark_id, f='; '.join(reasons))


def _result_receipt(conn, row, payload, rows):
    checkpoints = db.rows(conn, 'SELECT day, stats FROM benchmark_days WHERE benchmark_id=:b ORDER BY day',
                          b=row['benchmark_id'])
    return dict(domain=digest('domain-', row['expected_domain']), checkpoints=digest('days-', checkpoints),
                rows=digest('rows-', rows), summary=digest('summary-', payload), settings=row['settings_digest'],
                versions=versions_of(row), records=record_digest(conn, row['sim_run_id']),
                provenance={'git_commit': row['git_commit'], 'image_digest': row['image_digest']},
                physical=(row['validation_receipt'] or {}).get('physical'),
                moved_closing=(row['validation_receipt'] or {}).get('moved_closing'),
                checked_worlds=(row['validation_receipt'] or {}).get('checked_worlds'))


def save_result(conn, benchmark_id, summary, branch_rows, *, final_check=None):
    """Caller owns the transaction. Evidence and complete rows precede the guarded state change."""
    from . import metrics
    row = _locked(conn, benchmark_id)
    if row['status'] != 'RUNNING' or row['published_at'] is not None:
        raise ValueError('save_result requires RUNNING unpublished benchmark')
    errors = domain_errors(conn, row)
    receipt = row['validation_receipt'] or {}
    if (receipt.get('checked_records') != record_digest(conn, row['sim_run_id']) or receipt.get('gates') != 'passed'
            or receipt.get('checked_worlds') != row['expected_domain'].get('worlds')):
        errors.append('successful validation receipt required')
    if not callable(final_check):
        errors.append('final physical gate callback required')
    elif receipt:
        errors.extend(final_check(conn, row))
    expected = metrics.branch_day_rows(conn, row['sim_run_id'], row['expected_domain'].get('worlds', []))
    ordered = lambda rows: sorted(rows, key=lambda r: (r['scenario'], r['day'], r['branch']))
    if ordered(branch_rows) != ordered(expected):
        errors.append('complete branch/day rows must equal records')
    if summary.get('money') != metrics.money(branch_rows) or summary.get('waste') != metrics.waste(branch_rows):
        errors.append('summary and final rows differ')
    if summary.get('settings') != row['settings'] or summary.get('closing') != receipt.get('closing'):
        errors.append('summary settings or closing differs from validation')
    if not errors:
        simulation = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=row['sim_run_id'])
        expected_summary = metrics.summary(conn, simulation, refdata.get(row['dataset_version']),
                                           expected, receipt['closing'])
        if summary != expected_summary:
            errors.append('complete summary must equal the record-derived result')
    if errors:
        fail(conn, benchmark_id, errors)
        raise ValueError('; '.join(errors))
    db.run(conn, 'DELETE FROM benchmark_branch_day WHERE benchmark_id=:b', b=benchmark_id)
    db.insert_rows(conn, 'benchmark_branch_day', ('benchmark_id', *metrics.ROW_FIELDS),
                   [(benchmark_id, *(r[f] for f in metrics.ROW_FIELDS)) for r in branch_rows])
    db.run(conn, 'INSERT INTO benchmark_summary (benchmark_id,payload) VALUES (:b,CAST(:p AS jsonb)) '
                 'ON CONFLICT (benchmark_id) DO UPDATE SET payload=EXCLUDED.payload', b=benchmark_id, p=db.js(summary))
    final = _result_receipt(conn, row, summary, ordered(branch_rows))
    db.run(conn, "UPDATE benchmark_runs SET status='VALIDATED', validation_receipt=CAST(:r AS jsonb) "
                 'WHERE benchmark_id=:b', b=benchmark_id, r=db.js(final))


def publish(conn, benchmark_id, *, final_check=None):
    """Publish only a locked, unchanged fully validated result; never overwrite a publication."""
    from . import metrics
    row = _locked(conn, benchmark_id)
    if row['status'] != 'VALIDATED' or row['published_at'] is not None:
        raise ValueError('publish requires VALIDATED unpublished benchmark')
    receipt = row['validation_receipt']
    if not receipt:
        raise ValueError('validation receipt required')
    if not callable(final_check):
        raise ValueError('final physical gate callback required')
    errors = domain_errors(conn, row)
    errors.extend(final_check(conn, row))
    payload = db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id=:b', b=benchmark_id)
    rows = db.rows(conn, 'SELECT * FROM benchmark_branch_day WHERE benchmark_id=:b ORDER BY scenario,day,branch', b=benchmark_id)
    rows = [{k: r[k] for k in metrics.ROW_FIELDS} for r in rows]
    if payload is None or receipt != _result_receipt(conn, row, payload['payload'], rows):
        errors.append('immutable validation receipt differs')
    if errors:
        fail(conn, benchmark_id, errors)
        raise ValueError('; '.join(errors))
    changed = db.run(conn, "UPDATE benchmark_runs SET status='PUBLISHED', published_at=now() "
                           "WHERE benchmark_id=:b AND status='VALIDATED' AND published_at IS NULL", b=benchmark_id)
    if changed != 1:
        raise ValueError('publication refused')
