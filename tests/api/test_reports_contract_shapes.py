"""DB-free 08-03/06 boundary checks. Later outcome/summary projections belong to 08-18/21."""
import json
from pathlib import Path

from api import reporting

FIXTURES = Path(__file__).resolve().parents[1] / 'web' / 'fixtures'


def key_tree(value):
    """Object keys and nesting only; fixture figures never become a numeric oracle."""
    return {k: key_tree(v) for k, v in value.items()} if isinstance(value, dict) else None


def projection(body, *keys):
    return {k: body[k] for k in keys}


def fixture(name):
    return json.loads((FIXTURES / f'{name}.json').read_text())


def test_real_workspace_function_matches_contract_and_web_projection(monkeypatch):
    monkeypatch.setattr(reporting, 'scope_store', lambda *args: None)
    monkeypatch.setattr(reporting.refdata, 'get', lambda _: None)
    monkeypatch.setattr(reporting.db, 'one', lambda *args, **kwargs: {
        'open': 3, 'pending': 1, 'committed': 2, 'cancelled': 0, 'checks': 4,
        'past_due': 1, 'addressed': 0, 'dispatched': 1, 'received': 0, 'owners': []})
    answer = reporting.workspace(None, {'dataset_version': 'test', 'run_id': 'test'}, {'role': 'hq'})
    expected = {'scope': {'branch': None, 'role': None}, 'activity': {
        'open': {'count': None, 'need_action': None},
        'pending_approval': {'count': None, 'waiting_for_you': None},
        'unanswered_check': {'count': None, 'past_due': None, 'addressed_to_you': None},
        'committed': {'count': None, 'dispatched': None, 'received': None}, 'cancelled': {'count': None}}}
    assert key_tree(answer) == expected
    assert key_tree(projection(fixture('reports_workspace'), 'scope', 'activity')) == expected


def test_real_none_function_matches_contract_and_web_projection(monkeypatch):
    monkeypatch.setattr(reporting, 'scope_store', lambda *args: None)
    monkeypatch.setattr(reporting.refdata, 'get', lambda _: None)
    monkeypatch.setattr(reporting.db, 'one', lambda *args, **kwargs: None)
    answer = reporting.benchmark(None, {'dataset_version': 'test'}, {'role': 'hq'})
    expected = {'status': 'none', 'current': None, 'historical': None}
    assert answer == expected == projection(fixture('reports_benchmark_none'), *expected)
