"""Saved ANSWER readings on permitted request reads; every GET stays read-only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest
from fastapi.testclient import TestClient

# Initialize the approved offline API fixture before importing model configuration.
from tests.api.conftest import as_seat, sign_in
from api import db, files, refdata
from api.app import app
from agent import readings_job
from tests.agent.test_answer_reading import answer_reading, saved_answer
from tests.agent.test_readings_http import FakeModels, counts, identity, install


def request_view(client, request):
    result = client.get('/api/requests')
    assert result.status_code == 200, result.text
    return next(row for row in result.json() if row['request_id'] == request['request_id'])


def snapshot(run):
    with db.tx() as conn:
        return {name: sorted(json.dumps(row, sort_keys=True, default=str) for row in db.rows(
            conn, f'SELECT * FROM {name} WHERE run_id=:r', r=run['run_id']))
            for name in ('readings', 'signals', 'evidence_requests', 'ai_usage', 'ai_outputs')}


def stored_reading(run, request, answer, *, status='DONE', provenance='LIVE', attempt=1,
                   expired=False, **overrides):
    """An owned saved job, including its real answer/run identity, without a provider."""
    now = datetime.now(timezone.utc)
    params = dict(r=run['run_id'], id=uuid.uuid4().hex, key=uuid.uuid4().hex, purpose='ANSWER',
        status=status, provenance=provenance, attempt=attempt, signal=answer['signal_id'],
        revision=answer['revision'], request=request['request_id'],
        lease=now + timedelta(seconds=-5 if expired else 60),
        finished=now if status != 'GENERATING' else None)
    params.update(overrides)
    with db.tx() as conn:
        return db.one(conn, "INSERT INTO readings (run_id,reading_id,input_key,purpose,status,provenance,"
            "attempt,signal_id,signal_revision,request_id,model_id,assistant_version,created_by,lease_until,"
            "finished_at,observations) VALUES (:r,:id,:key,:purpose,:status,:provenance,:attempt,:signal,"
            ":revision,:request,'offline-projection-model','offline-projection-version','test-projection',"
            ":lease,:finished,'[\"visible slot empty\"]'::jsonb) RETURNING *", **params)


def no_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A request GET must not read upload bytes or start model work')
    monkeypatch.setattr(files, 'read', forbidden)
    monkeypatch.setattr(readings_job, 'genai_client', forbidden)
    monkeypatch.setattr(readings_job.reading, 'read', forbidden)


def test_request_read_exposes_saved_answer_reading(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    fake = install(monkeypatch, FakeModels(None))
    generated = answer_reading(client, answer)
    assert generated.status_code == 200, generated.text
    before, allowances = snapshot(run), counts()
    no_external_work(monkeypatch)

    shown = request_view(client, request)

    assert 'reading' in shown['answer'], 'Request.answer must expose its saved reading'
    reading = shown['answer']['reading']
    assert reading == {**generated.json(), 'served': 'reuse', 'attempt': 1}
    assert generated.json()['attempt'] == 1
    assert shown['answer']['fields'] == answer['fields']
    assert shown['confirmable'] is False and shown['answerable'] is True
    assert counts() == allowances and snapshot(run) == before
    assert len(fake.calls) == 1
    # Only public projections may leave storage; attachment metadata remains type/size.
    assert set(shown['answer']['file']) == {'type', 'size'}
    encoded = json.dumps(shown)
    assert not any(private in encoded for private in ('file_uri', 'file_sha256', 'created_by', 'lease_until',
                                                      'input_key', 'usage', str(tmp_path), 'shelf.jpg'))


@pytest.mark.parametrize('status,provenance,attempt,expired,expected', [
    ('DONE', 'LIVE', 1, False, 'DONE'),
    ('DONE', 'PRECOMPUTED', 1, False, 'DONE'),
    ('FAILED', 'LIVE', 1, False, 'FAILED'),
    ('REFUSED', 'LIVE', 1, False, 'REFUSED'),
    ('INTERRUPTED', 'LIVE', 1, False, 'INTERRUPTED'),
    ('INTERRUPTED', 'LIVE', 2, False, 'INTERRUPTED'),
    ('GENERATING', 'LIVE', 1, False, 'GENERATING'),
    ('GENERATING', 'LIVE', 1, True, 'INTERRUPTED'),
    ('FAILED', 'PRECOMPUTED', 1, False, None),
    ('REFUSED', 'PRECOMPUTED', 1, False, None),
    ('INTERRUPTED', 'PRECOMPUTED', 1, False, None),
])
def test_request_read_preserves_saved_state_without_spending(client, monkeypatch, tmp_path,
                                                            status, provenance, attempt, expired, expected):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    row = stored_reading(run, request, answer, status=status, provenance=provenance,
                         attempt=attempt, expired=expired)
    before, allowances = snapshot(run), counts()
    no_external_work(monkeypatch)

    shown = request_view(client, request)['answer']

    assert 'reading' in shown, 'A saved answer must explicitly project reading or null'
    reading = shown['reading']
    if expected is None:
        assert reading is None
    else:
        assert (reading['reading_id'], reading['status'], reading['attempt']) == (row['reading_id'], expected, attempt)
        served = 'generating' if expected == 'GENERATING' else 'precomputed' if provenance == 'PRECOMPUTED' else 'reuse'
        assert reading['served'] == served and reading['observations'] == ['visible slot empty']
    assert snapshot(run) == before and counts() == allowances


def test_request_read_selects_only_this_run_answer_revision_and_purpose(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    correct = stored_reading(run, request, answer)
    other = TestClient(app)
    sign_in(other)
    other_run, _ = identity(other)
    # These newer rows must never displace the one current ANSWER reading.
    stored_reading(run, request, answer, revision=answer['revision'] + 1)
    stored_reading(run, request, answer, purpose='REPORT')
    stored_reading(run, request, answer, signal=uuid.uuid4().hex)
    stored_reading(other_run, request, answer)
    no_external_work(monkeypatch)

    shown = request_view(client, request)['answer']
    assert 'reading' in shown, 'Request.answer must expose its scoped reading'
    assert shown['reading']['reading_id'] == correct['reading_id']
    assert all(row['request_id'] != request['request_id'] for row in other.get('/api/requests').json())


def test_request_read_missing_or_new_revision_projects_null(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    shown = request_view(client, request)['answer']
    assert 'reading' in shown and shown['reading'] is None
    stored_reading(run, request, answer, revision=answer['revision'] - 1)
    assert request_view(client, request)['answer']['reading'] is None


def test_request_read_keeps_existing_party_authorization(client, monkeypatch, tmp_path):
    run, request, answer = saved_answer(client, monkeypatch, tmp_path)
    stored_reading(run, request, answer)
    with db.tx() as conn:
        case = db.one(conn, 'SELECT * FROM cases WHERE run_id=:r AND case_id=:c',
                      r=run['run_id'], c=request['case_id'])
    outsider = next(branch for branch in refdata.get(run['dataset_version']).branches
                    if branch not in (request['to_branch'], case['source_branch'], case.get('plan_dest')))
    as_seat(client, outsider)
    no_external_work(monkeypatch)
    result = client.get('/api/requests')
    assert result.status_code == 200
    assert all(row['request_id'] != request['request_id'] for row in result.json())
