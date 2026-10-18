"""TEST ONLY: eligible public artifacts and matching workspaces for evaluator regressions.

The numeric inputs remain the tiny synthetic test set. Provider markers exercise the
eligibility contract; they are constructed test metadata, never evidence of a paid forecast.
Original fixtures and their stand-in refusal coverage stay unchanged.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
import uuid

import pytest

from api import advance, db, refdata, sandbox
from data import forecast as F
from data.public_forecast import score_public
from data.versions import digest
from tests.api.conftest import TINY, drop_runs


def measured_test_backtest(root, manifest):
    """Measure this tiny TEST artifact; never borrow the released dataset's figures."""
    def read(name):
        return list(csv.DictReader(gzip.open(root / manifest['files'][name]['path'], 'rt')))
    products = {r['sku']: r for r in read('products')}
    grouped = {}
    for row in read('sales'):
        key = row['store'], row['sku']
        grouped.setdefault(key, []).append(dict(day=int(row['day']), sale_amount=float(row['sale_amount']),
            discount=float(row['discount']), stockout=row['stockout'] == 'true',
            unknown_price=row['unknown_price'] == 'true'))
    source = [((None, store[1:], sku[1:],
                tuple(products[sku][k] for k in ('mgmt', 'cat1', 'cat2', 'cat3'))), rows)
              for (store, sku), rows in sorted(grouped.items())]
    settings = manifest['settings']
    cfg = {**settings, 'policy_json': {'markdown_response': settings['markdown_response']}}
    report = score_public(root / manifest['files']['forecast_points']['path'], source, cfg)
    report['test_only'] = True
    report['note'] = 'TEST ONLY: measured tiny synthetic fixture with constructed provider metadata; no cloud forecast'
    return report


def eligible_artifact(root, purpose='evaluator eligibility contract'):
    """Construct a complete, independently versioned TEST artifact under a temporary root. `purpose` is part of the
    manifest, so a different purpose is a different dataset version with the same tiny numbers."""
    shutil.copytree(TINY, root, dirs_exist_ok=True)
    manifest = json.loads((root / 'manifest.json').read_text())
    for name in ('forecast_points', 'forecast_runs'):
        part = manifest['files'][name]
        path = root / part['path']
        rows = list(csv.DictReader(gzip.open(path, 'rt', encoding='utf-8', newline='')))
        for row in rows:
            if name == 'forecast_points':
                row['provider'] = F.AI_FORECAST
            else:
                meta = json.loads(row['meta'])
                meta['providers'] = {F.AI_FORECAST: sum(meta['providers'].values())}
                meta['test_only'] = True
                row['meta'] = json.dumps(meta, sort_keys=True)
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=part['columns'], lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        payload = gzip.compress(output.getvalue().encode(), mtime=0)
        path.write_bytes(payload)
        part['sha256'] = hashlib.sha256(payload).hexdigest()
        part['rows'] = len(rows)
    forecast = manifest['forecast']
    forecast['providers'] = {F.AI_FORECAST: forecast['points']}
    forecast['stand_in'] = False
    forecast['note'] = 'TEST ONLY: constructed eligible provider metadata; no cloud forecast was run'
    for run in forecast['runs']:
        run['providers'] = {F.AI_FORECAST: sum(run['providers'].values())}
        run['test_only'] = True
    forecast['backtest_report'] = measured_test_backtest(root, manifest)
    manifest['settings']['truth']['price_response'] = {'m20': 1.0, 'm30': 1.0}
    manifest['settings']['ordering'] = {'enabled_stores': []}
    manifest['fixture'] = {'test_only': True, 'purpose': purpose}
    manifest['dataset_version'] = digest('ds-', {k: v for k, v in manifest.items() if k != 'dataset_version'})
    (root / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    return root


@pytest.fixture(scope='session')
def eligible_dv(schema, tmp_path_factory):
    root = eligible_artifact(tmp_path_factory.mktemp('eligible-benchmark'))
    return refdata.install(root, allow_local=False)


def workspace_factory(dataset_version, created):
    """A creator of app workspaces on `dataset_version`, never the release template; ids go into `created`."""
    def create():
        with db.tx() as conn:
            rid = 'tpl-test-' + uuid.uuid4().hex[:12]
            template = db.one(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) "
                                   "VALUES (:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1) RETURNING *",
                              r=rid, v=dataset_version)
            created.append(rid)
            sandbox.init_worlds(conn, rid, refdata.get(dataset_version), refdata.truth(dataset_version))
            advance.day_start(conn, template)
            workspace = sandbox.clone(conn, template)
            created.append(workspace)
            return db.one(conn, 'SELECT * FROM demo_runs WHERE run_id = :r', r=workspace)
    return create


@pytest.fixture
def eligible_workspace(eligible_dv):
    """Factory for app workspaces matching the eligible dataset, without changing the release template."""
    created = []
    yield workspace_factory(eligible_dv, created)
    with db.tx() as conn:
        drop_runs(conn, list(reversed(created)))


@pytest.fixture(scope='session')
def eligible_benchmark_dv(schema, tmp_path_factory):
    """The CLI's own eligible TEST dataset (08-15): the eligible artifact above with its own purpose, so its version
    differs from `eligible_dv` and a benchmark the CLI publishes is current only for workspaces on this version.
    Same complete seven-day forecast targets, the same constructed (non-local) provider metadata, an empty effective
    ordering list and price_response 1.0 for m20 and m30; never the stand-in `public_dv` relabelled."""
    root = eligible_artifact(tmp_path_factory.mktemp('eligible-benchmark-cli'), purpose='benchmark job end to end')
    return refdata.install(root, allow_local=False)


@pytest.fixture
def eligible_benchmark_workspace(eligible_benchmark_dv):
    """Factory for app workspaces on the CLI's dataset (the signed-in workspace of the end-to-end read)."""
    created = []
    yield workspace_factory(eligible_benchmark_dv, created)
    with db.tx() as conn:
        drop_runs(conn, list(reversed(created)))


# TEST ONLY oracle: the world-to-scenario names written out here, not imported from api.metrics.
SCENARIO_OF = {'baseline_rule': 'store_rule', 'baseline_checked': 'store_rule_checked', 'shelfshift': 'shelfshift',
               'do_nothing': 'markdowns_only'}


def ledger_oracle(conn, benchmark_id):
    """TEST ONLY: money, waste and trips derived straight from the outcome ledger and the per-trip audit in SQL,
    without api.metrics, to check a saved summary against. Net is revenue - transport - handling - disposal (D-17);
    a trip that shipped 0 failed, 0 < shipped < planned is short, and the fees on wholly unshipped trips are their own
    line (D-19). This is an independent derivation from the records, not hand arithmetic."""
    net, wasted = {}, {}
    for r in db.rows(conn, 'SELECT world, round(sum(revenue - transport - handling - disposal)::numeric, 2) AS net, '
                           'sum(wasted) AS wasted FROM outcomes WHERE run_id = :r GROUP BY world', r=benchmark_id):
        net[SCENARIO_OF[r['world']]] = float(r['net'])
        wasted[SCENARIO_OF[r['world']]] = float(r['wasted'])
    trips = {key: {'short': 0, 'failed': 0, 'transport_unshipped': 0.0} for key in SCENARIO_OF.values()}
    for r in db.rows(conn, 'SELECT world, count(*) FILTER (WHERE shipped_units > 0 AND shipped_units < planned_units) '
                           'AS short, count(*) FILTER (WHERE shipped_units = 0) AS failed, round(coalesce(sum('
                           'transport_amount) FILTER (WHERE shipped_units = 0), 0)::numeric, 2) AS unshipped '
                           'FROM benchmark_transfer_audit WHERE sim_run_id = :r GROUP BY world', r=benchmark_id):
        trips[SCENARIO_OF[r['world']]] = {'short': r['short'], 'failed': r['failed'],
                                          'transport_unshipped': float(r['unshipped'])}
    return {'net': net, 'wasted_units': wasted, 'trips': trips, 'world': {v: k for k, v in SCENARIO_OF.items()}}
