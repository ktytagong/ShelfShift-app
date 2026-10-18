"""Per-series provenance, not the run majority (11-CONTEXT D-12; read views only)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from api import cases, db, refdata, stock
from api.app import app
from data.forecast import PROVIDER_ORDER
from tests.api.conftest import first_store, fresh, seat, sign_in


def ordered(codes):
    return sorted(set(codes), key=lambda code: (PROVIDER_ORDER.index(code) if code in PROVIDER_ORDER else len(PROVIDER_ORDER), code))


def test_forecast_rows_carry_provider_codes(public_template):
    with TestClient(app) as client:
        sign_in(client)
        response = client.get('/api/forecasts')
        assert response.status_code == 200
        data = response.json()
        assert data['items']
        for item in data['items']:
            assert item['provider_codes']
            assert set(item['provider_codes']) <= set(PROVIDER_ORDER)
            assert item['provider_codes'] == ordered(item['provider_codes'])


def test_a_mixed_series_lists_both(public_template):
    run = fresh(public_template)
    ref = refdata.get(run['dataset_version'])
    store = first_store()
    class Rollback(Exception): pass
    with pytest.raises(Rollback), db.tx() as conn:
        before = stock.forecasts(conn, run, seat(store))
        item = before['items'][0]
        point = db.one(conn, 'SELECT day, provider FROM ref_forecast_point WHERE dataset_version=:v AND forecast_run_id=:f AND store=:s AND sku=:k AND day >= :d ORDER BY day LIMIT 1', v=run['dataset_version'], f=before['run']['id'], s=store, k=item['sku'], d=run['day'])
        alternate = 'arima_plus' if point['provider'] != 'arima_plus' else 'ai_forecast'
        db.run(conn, 'UPDATE ref_forecast_point SET provider=:p WHERE dataset_version=:v AND forecast_run_id=:f AND store=:s AND sku=:k AND day=:d', p=alternate, v=run['dataset_version'], f=before['run']['id'], s=store, k=item['sku'], d=point['day'])
        after = stock.forecasts(conn, run, seat(store))
        assert after['items'][0]['provider_codes'] == ordered([*item['provider_codes'], alternate])
        assert after['items'][1:] == before['items'][1:]
        raise Rollback()


def test_case_versions_name_the_series_provider(public_template, monkeypatch):
    run = fresh(public_template)
    ref = refdata.get(run['dataset_version'])
    with db.tx() as conn:
        candidates = db.rows(conn, 'SELECT * FROM cases WHERE run_id=:r ORDER BY case_id', r=run['run_id'])
        pair = next(((case, cases.active_rec(conn, case)) for case in candidates if cases.active_rec(conn, case)), None)
        assert pair is not None, 'the public template must hold a recommendation to test its provenance'
        case, rec = pair
        days = [p['day'] for p in rec['engine_input']['forecasts'] if p['branch'] == case['source_branch']]
        assert days
        original = ref.provider_of
        def provider(day, store, sku, point_day):
            return 'arima_plus' if point_day == days[0] and store == case['source_branch'] and sku == case['sku'] else original(day, store, sku, point_day)
        monkeypatch.setattr(ref, 'provider_of', provider)
        detail = cases.detail(conn, run, case['case_id'])
        expected = ordered(provider(rec['day'], case['source_branch'], case['sku'], day) for day in days)
        assert detail['versions']['forecast_series_providers'] == expected
        assert cases.base_versions(run, ref)['forecast_series_providers'] is None


def test_analytics_supplies_the_series_run_and_cutoff(public_template):
    run = fresh(public_template)
    ref = refdata.get(run['dataset_version'])
    with db.tx() as conn:
        data = stock.analytics(conn, run, seat(first_store()))
        assert data['series'] is not None
        fr = ref.forecast_runs[run['day']]
        assert data['series']['forecast_run_id'] == fr['forecast_run_id']
        assert data['series']['forecast_cutoff'] == ref.date(fr['cutoff_day']).isoformat()
        assert all('provider' in p for p in data['series']['forecast'])
