"""Evaluation settings and the live six-version contract (08-05)."""
import copy
import json
from types import SimpleNamespace

import pytest

from api import config, refdata
from engine.model import ENGINE_VERSION


def test_benchmark_exposes_app_versions(client):
    result = client.get('/api/reports/benchmark')
    assert result.status_code == 200
    assert set(result.json().get('app', {})) == {
        'engine', 'dataset', 'policy', 'forecast_runs', 'evaluator', 'settings'}


@pytest.mark.parametrize('missing', ['value', 'unit', 'type', 'source'])
def test_settings_require_leaf_metadata(tmp_path, missing):
    from api import benchmark
    tree = json.loads(benchmark.SETTINGS_FILE.read_text())
    del tree['retry_limit'][missing]
    path = tmp_path / 'evaluation.json'
    path.write_text(json.dumps(tree))
    with pytest.raises(ValueError, match='retry_limit'):
        benchmark.load_settings(path)


def test_settings_and_real_public_versions(public_dv):
    from api import benchmark
    ref = refdata.get(public_dv)
    assert benchmark.load_settings() == {'retry_limit': 2, 'tie_threshold': 0.01, 'waste_price_info': 0.28}
    settings = benchmark.settings_for(ref)
    assert set(settings) == {'forecast_rule', 'closing_rule', 'checks', 'cancel_cost', 'keep_fast_path',
                             'price_response', 'commits', 'ordering', 'sensitivity'}
    assert settings['price_response'] == ref.settings['truth']['price_response']
    assert settings['checks']['cap_per_version'] == config.MAX_REQUESTS_PER_VERSION
    assert settings['ordering'] == {'enabled_stores': [], 'rule': 'no store enabled'}
    assert benchmark.app_versions({'dataset_version': public_dv}, ref) == {
        'engine': ENGINE_VERSION, 'dataset': public_dv, 'policy': ref.manifest['reference_fingerprint'],
        'forecast_runs': [r['forecast_run_id'] for _, r in sorted(ref.forecast_runs.items())],
        'evaluator': 'eval-1', 'settings': benchmark.settings_digest(ref)}
    assert len(benchmark.app_versions({'dataset_version': public_dv}, ref)['forecast_runs']) == 4


@pytest.mark.parametrize('ordering', [None, {'enabled_stores': []}, {'enabled': False, 'enabled_stores': ['S12']},
                                      {'enabled': False}, {'enabled_default': False, 'enabled_stores': []}])
def test_effectively_disabled_ordering_is_publishable(public_dv, ordering):
    from api import benchmark
    settings = copy.deepcopy(refdata.get(public_dv).settings)
    if ordering is not None:
        settings['ordering'] = ordering
    assert benchmark.ordering_state(SimpleNamespace(settings=settings)) == {
        'enabled_stores': [], 'rule': 'no store enabled'}


def test_enabled_ordering_refuses_before_work(public_dv):
    from api import benchmark
    settings = copy.deepcopy(refdata.get(public_dv).settings)
    settings['ordering'] = {'enabled_stores': ['S12']}
    with pytest.raises(ValueError, match='ordering.*enforcement'):
        benchmark.settings_for(SimpleNamespace(settings=settings))


@pytest.mark.parametrize('ordering', [[], 'enabled', {}, {'enabled_stores': 'S12'},
                                      {'enabled_stores': [None]}, {'enabled': 'false'},
                                      {'enabled_default': True}])
def test_malformed_ordering_refuses(public_dv, ordering):
    from api import benchmark
    settings = copy.deepcopy(refdata.get(public_dv).settings)
    settings['ordering'] = ordering
    with pytest.raises(ValueError, match='ordering'):
        benchmark.ordering_state(SimpleNamespace(settings=settings))


def test_digest_tracks_all_settings(public_dv, monkeypatch):
    from api import benchmark
    ref = refdata.get(public_dv)
    original = benchmark.settings_digest(ref)
    assert original == benchmark.settings_digest(ref)
    settings = benchmark.settings_for(ref)
    for key, value in [('commits', {**settings['commits'], 'retry_limit': 3}),
                       ('checks', {**settings['checks'], 'cap_per_version': 8}),
                       ('price_response', {'m20': 1.1, 'm30': 1.0}),
                       ('ordering', {'enabled_stores': ['S12'], 'rule': 'enforced'})]:
        monkeypatch.setattr(benchmark, 'settings_for', lambda ref, k=key, v=value: {**settings, k: v})
        assert benchmark.settings_digest(ref) != original


def test_digest_reads_live_retry_and_cap_values(public_dv, monkeypatch):
    from api import benchmark
    ref = refdata.get(public_dv)
    original = benchmark.settings_digest(ref)
    values = benchmark.load_settings()
    with monkeypatch.context() as patch:
        patch.setattr(benchmark, 'load_settings', lambda: {**values, 'retry_limit': values['retry_limit'] + 1})
        assert benchmark.settings_digest(ref) != original
    monkeypatch.setattr(config, 'MAX_REQUESTS_PER_VERSION', config.MAX_REQUESTS_PER_VERSION + 1)
    assert benchmark.settings_digest(ref) != original


def test_public_price_response_must_be_sales_replay(public_dv):
    from api import benchmark
    settings = copy.deepcopy(refdata.get(public_dv).settings)
    settings['truth']['price_response']['m20'] = 1.1
    with pytest.raises(ValueError, match='price_response'):
        benchmark.settings_for(SimpleNamespace(settings=settings))
