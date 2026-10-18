"""Public About contract edges (11-CONTEXT D-05): real isolated Postgres, one read transaction."""
from __future__ import annotations

from contextlib import contextmanager
import json

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from api import about, config, db, refdata
from api.app import app

KEYS = ["git_commit", "engine_version", "dataset_version", "assistant_version", "gemini_model", "vertex_location",
        "label", "data_as_of", "forecast", "error_model", "contact", "google_cloud", "weekcheck_status",
        "benchmark_status", "benchmark", "ai_eval_status", "ai_eval", "latest_unpublished"]


@contextmanager
def active_template(run_id=None):
    with db.tx() as conn:
        before = db.rows(conn, "SELECT run_id, status FROM demo_runs WHERE kind = 'TEMPLATE'")
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE kind = 'TEMPLATE' AND status = 'ACTIVE'")
        if run_id:
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :r", r=run_id)
    try:
        yield
    finally:
        with db.tx() as conn:
            for row in before:
                db.run(conn, "UPDATE demo_runs SET status = :s WHERE run_id = :r", s=row["status"], r=row["run_id"])


def read():
    return TestClient(app, raise_server_exceptions=False).get("/api/about")


def test_unseeded_answers_without_a_template(schema):
    with active_template():
        r = read()
        assert r.status_code == 200
        p = r.json()
        assert p["dataset_version"] == "unseeded"
        assert [p[k] for k in ("forecast", "label", "data_as_of")] == [None] * 3


def test_synthetic_template(template):
    with active_template(template):
        p = read().json()
        with db.tx() as conn:
            expected = db.one(conn, "SELECT dataset_version FROM demo_runs WHERE run_id = :r", r=template)
        assert p["dataset_version"] == expected["dataset_version"]
        assert p["forecast"] is None
        assert p["error_model"] == {"share": 0.1, "range": [0.6, 0.95]}


def test_public_tiny_set(public_run):
    with active_template(public_run["run_id"]):
        r = read()
        assert r.status_code == 200
        p = r.json()
        ref = refdata.get(public_run["dataset_version"])
        fr = ref.forecast_runs[public_run["day"]]
        assert p["forecast"] == {"headline": ref.forecast["provider_summary"]["headline"],
                                 "providers": ref.forecast["provider_summary"]["demo"]["by_provider"],
                                 "run_id": fr["forecast_run_id"], "cutoff_date": ref.date(fr["cutoff_day"]).isoformat()}
        assert "{date}" in p["label"] and p["data_as_of"] == ref.data_as_of


def test_missing_tables_are_not_published(template, monkeypatch):
    looked = []
    monkeypatch.setattr(about, "_exists", lambda conn, table: (looked.append(table) or False))
    p = read().json()
    assert looked == ["benchmark_runs", "eval_publications", "eval_runs"]
    assert p["benchmark_status"] == p["ai_eval_status"] == "not_published"
    assert [p[k] for k in ("benchmark", "ai_eval", "latest_unpublished")] == [None] * 3


@pytest.mark.parametrize(("state", "status", "code"), [("08006", 503, "paused"), ("42601", 500, "internal")])
def test_a_connection_failure_is_never_not_published(template, monkeypatch, state, status, code):
    def failure(*_):
        raise sa.exc.DBAPIError(None, None, Exception({"C": state}))
    monkeypatch.setattr(about, "_exists", failure)
    r = read()
    assert r.status_code == status and r.json()["error"] == code
    assert "not_published" not in r.text


def test_exact_keys_and_no_exposure(template, monkeypatch):
    markers = {"PROJECT": "private-project-marker", "DATA_BUCKET": "private-data-marker", "UPLOADS_BUCKET": "private-uploads-marker"}
    for key, value in markers.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "UPLOADS_BUCKET", markers["UPLOADS_BUCKET"])
    r = read()
    assert r.status_code == 200 and list(r.json()) == KEYS
    assert all(marker not in r.text for marker in markers.values())
    assert not hasattr(config, "DATA_BUCKET")


def test_read_only_and_repeatable(template):
    def counts():
        with db.tx() as conn:
            return [db.one(conn, f"SELECT count(*) AS n FROM {table}")["n"] for table in ("sessions", "demo_runs")]
    before = counts()
    first, second = read(), read()
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and counts() == before
    assert "set-cookie" not in first.headers and "set-cookie" not in second.headers


def test_one_read_transaction_including_cold_reference_load(template, monkeypatch):
    real_tx = db.tx
    events = []
    @contextmanager
    def tracked():
        events.append("begin")
        with real_tx() as conn:
            yield conn
        events.append("end")
    refdata.get.cache_clear()
    monkeypatch.setattr(db, "tx", tracked)
    assert read().status_code == 200
    assert events == ["begin", "end"]


def test_about_is_in_the_public_route_sweep():
    from tests.api.test_http import PUBLIC, LATER
    assert ("GET", "/api/about") in PUBLIC and "about" not in LATER


@contextmanager
def benchmark_tables():
    # Contract DDL, isolated to the test DB. Keep upstream tables when already integrated.
    with db.tx() as conn:
        existed = {t: about._exists(conn,t) for t in ('benchmark_runs','benchmark_summary')}
        db.run(conn, """CREATE TABLE IF NOT EXISTS benchmark_runs (
          benchmark_id text PRIMARY KEY, status text NOT NULL, trial boolean NOT NULL DEFAULT false,
          sim_run_id text REFERENCES demo_runs(run_id), dataset_version text NOT NULL, engine_version text NOT NULL,
          policy_version text NOT NULL, forecast_runs jsonb NOT NULL, eval_version text NOT NULL,
          settings_digest text NOT NULL, settings jsonb NOT NULL, image_digest text, git_commit text NOT NULL,
          first_day int NOT NULL, last_day int NOT NULL, last_day_done int, started_at timestamptz NOT NULL DEFAULT now(),
          published_at timestamptz, failure text, stats jsonb NOT NULL DEFAULT '{}', expected_domain jsonb,
          validation_receipt jsonb)""")
        db.run(conn, """CREATE TABLE IF NOT EXISTS benchmark_summary (
          benchmark_id text PRIMARY KEY REFERENCES benchmark_runs(benchmark_id), payload jsonb NOT NULL)""")
    try:
        yield
    finally:
        with db.tx() as conn:
            db.run(conn,"DELETE FROM benchmark_summary WHERE benchmark_id LIKE 'p11-%'")
            db.run(conn,"DELETE FROM benchmark_runs WHERE benchmark_id LIKE 'p11-%'")
            for t in ('benchmark_summary','benchmark_runs'):
                if not existed[t]: db.run(conn, 'DROP TABLE '+t)


def seed_benchmark(conn, dataset, engine, *, ident='p11-benchmark', published=True):
    db.run(conn, """INSERT INTO benchmark_runs
      (benchmark_id,status,dataset_version,engine_version,policy_version,forecast_runs,eval_version,
       settings_digest,settings,git_commit,first_day,last_day,published_at)
      VALUES (:id,:status,:d,:e,'fixture','[]','fixture','fixture','{}',:sha,0,6,'2026-10-18T04:00:00Z')""",
      id=ident,status='PUBLISHED' if published else 'RUNNING',d=dataset,e=engine,sha='0123456789abcdef0123456789abcdef01234567')
    db.run(conn,"INSERT INTO benchmark_summary VALUES (:id, CAST(:p AS jsonb))",id=ident,p=db.js({'network':{'stores':5}}))


@pytest.mark.parametrize('engine, differs', [('engine-3',[]),('old-engine',['engine_version'])])
def test_published_benchmark_matches_or_differs(template, monkeypatch, engine, differs):
    monkeypatch.setattr(about,'_reporting',lambda:None,raising=False)
    with active_template(template), benchmark_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn)
            seed_benchmark(conn,tpl['dataset_version'],engine)
            ref=refdata.get(tpl['dataset_version'])
        r=read();assert r.status_code==200
        p=r.json();assert p['benchmark_status']=='published'
        b=p['benchmark'];assert b['version_match']==(not differs) and b['differs']==differs
        assert b['first_day']==ref.date(0).isoformat() and b['last_day']==ref.date(6).isoformat()
        assert b['stores']==5 and b['series'] is None
        assert set(b)=={'dataset_version','benchmark_id','published_at','git_commit','first_day','last_day','stores','series','version_match','differs'}


def test_unpublished_benchmark_is_not_a_result(template, monkeypatch):
    monkeypatch.setattr(about,'_reporting',lambda:None,raising=False)
    with active_template(template), benchmark_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn);seed_benchmark(conn,tpl['dataset_version'],'engine-3',published=False)
        p=read().json();assert p['benchmark_status']=='not_published' and p['benchmark'] is None


@pytest.mark.parametrize('complete',[False,True])
def test_about_uses_reports_own_selection(template, monkeypatch, complete):
    from types import SimpleNamespace
    called=[]
    def selected(conn, run, actor):
        called.append(actor)
        result = {'status':'historical','historical':{'benchmark_id':'p11-selected','published_at':'2026-10-18T04:00:00Z',
          'git_commit':'abcdef','window':{'first_day':0,'last_day':6},'differs':['policy'],
          'versions':{'dataset':run['dataset_version']},'summary':{'network':{'stores':7}}}}
        if not complete:
            result['historical'].pop('window');result['historical'].pop('summary')
        return result
    monkeypatch.setattr(about,'_reporting',lambda:SimpleNamespace(benchmark=selected),raising=False)
    with active_template(template), benchmark_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn)
            seed_benchmark(conn,tpl['dataset_version'],'engine-3',ident='p11-selected')
        p=read().json();assert p['benchmark']['benchmark_id']=='p11-selected'
        assert p['benchmark']['differs']==['policy'] and not p['benchmark']['version_match']
        assert called==[{'role':'hq','branch':None}]


@contextmanager
def eval_tables():
    with db.tx() as conn:
        existed={t:about._exists(conn,t) for t in ('eval_runs','eval_publications')}
        db.run(conn,"""CREATE TABLE IF NOT EXISTS eval_runs (
          eval_id text PRIMARY KEY, fingerprint text NOT NULL, model_id text, build_sha text, assistant_version text,
          engine_version text, dataset_version text, template_ref text, started_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz, status text NOT NULL CHECK (status IN ('RUNNING','COMPLETE','CAPPED','FAILED','DRY')),
          usage jsonb NOT NULL DEFAULT '{}', caps jsonb NOT NULL DEFAULT '{}',
          publish_state text NOT NULL DEFAULT 'PRIVATE' CHECK (publish_state IN ('PRIVATE','PUBLISHED','BLOCKED')),
          block_reason text, reviewed_by text, reviewed_at timestamptz)""")
        db.run(conn,"""CREATE TABLE IF NOT EXISTS eval_publications (
          eval_id text PRIMARY KEY REFERENCES eval_runs(eval_id), published_at timestamptz NOT NULL DEFAULT now(),
          summary jsonb NOT NULL)""")
    try: yield
    finally:
        with db.tx() as conn:
            db.run(conn,"DELETE FROM eval_publications WHERE eval_id LIKE 'p11-%'")
            db.run(conn,"DELETE FROM eval_runs WHERE eval_id LIKE 'p11-%'")
            for t in ('eval_publications','eval_runs'):
                if not existed[t]: db.run(conn,'DROP TABLE '+t)


def eval_counts():
    return {'categories':[dict(category=c,passed=2,failed=0,not_applicable=1,applicable_total=2,
      raw_model_failures=0,safeguard_catches=0,failure_notes=[]) for c in ('assistant','reading','safety')],
      'safety_gate_passed':True}


def seed_eval(conn, dataset, *, ident='p11-eval', summary=None, changes=None, published=True,
              status='COMPLETE', reason=None, started='2026-10-18T03:00:00Z'):
    values=dict(eval_id=ident, fingerprint='fixture-fingerprint', model_id='fixture-model',build_sha='different-build',
      assistant_version='fixture-assistant',engine_version='engine-3',dataset_version=dataset,
      status=status,publish_state='PUBLISHED' if published else 'BLOCKED' if reason else 'PRIVATE',
      block_reason=reason,started_at=started)
    values.update(changes or {})
    db.run(conn,"""INSERT INTO eval_runs (eval_id,fingerprint,model_id,build_sha,assistant_version,
      engine_version,dataset_version,status,publish_state,block_reason,started_at)
      VALUES (:eval_id,:fingerprint,:model_id,:build_sha,:assistant_version,:engine_version,:dataset_version,
      :status,:publish_state,:block_reason,CAST(:started_at AS timestamptz))""",**values)
    if published: db.run(conn,"INSERT INTO eval_publications VALUES (:id,'2026-10-18T04:00:00Z',CAST(:s AS jsonb))",
      id=ident,s=db.js(eval_counts() if summary is None else summary))


@pytest.fixture
def eval_live(monkeypatch):
    monkeypatch.setattr(config,'GEMINI_MODEL','fixture-model')
    monkeypatch.setattr(about,'_assistant_version',lambda:'fixture-assistant')
    monkeypatch.setattr(about,'_live_fingerprint',lambda dataset:'fixture-fingerprint',raising=False)


@pytest.mark.parametrize('field', [None,'model_id','assistant_version','dataset_version','engine_version','reference_fingerprint'])
def test_published_eval_matches_on_five_fields(template, eval_live, field):
    with active_template(template), eval_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn)
            changes={( 'fingerprint' if field=='reference_fingerprint' else field):'other-value'} if field else {}
            seed_eval(conn,tpl['dataset_version'],changes=changes)
        r=read();assert r.status_code==200
        p=r.json();assert p['ai_eval_status']=='published'
        assert p['ai_eval']['version_match']==(field is None)
        assert p['ai_eval']['differing_fields']==([field] if field else [])
        assert p['ai_eval']['build_commit']=='different-build'
        assert not any(k in r.text for k in ('reviewed_by','template_ref','usage','bucket'))


def test_missing_live_fingerprint_differs(template,eval_live,monkeypatch):
    monkeypatch.setattr(about,'_live_fingerprint',lambda dataset:None)
    with active_template(template),eval_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn);seed_eval(conn,tpl['dataset_version'])
        assert read().json()['ai_eval']['differing_fields']==['reference_fingerprint']


@pytest.mark.parametrize('status,reason,expected_reason', [('COMPLETE','Safety gate failed · not published','Safety gate failed · not published'),('RUNNING',None,'The run has not finished.')])
def test_latest_unpublished(template,eval_live,status,reason,expected_reason):
    with active_template(template),eval_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn);seed_eval(conn,tpl['dataset_version'])
            seed_eval(conn,tpl['dataset_version'],ident='p11-later',published=False,status=status,reason=reason,started='2026-10-18T05:00:00Z')
        p=read().json();assert p['ai_eval_status']=='published'
        assert p['latest_unpublished']=={'run_date':'2026-10-18T05:00:00+00:00','reason':expected_reason}


@pytest.mark.parametrize('defect',['total','order','failures','extra'])
def test_counts_must_add_up(template,eval_live,capsys,defect):
    counts=eval_counts()
    if defect=='total': counts['categories'][0]['passed']=1
    if defect=='order': counts['categories'].reverse()
    if defect=='failures': counts['categories'][0].update(passed=1,failed=1)
    if defect=='extra': counts['categories'][0]['private_path']='private-marker'
    with active_template(template),eval_tables():
        with db.tx() as conn:
            tpl=about.sandbox.active_template(conn);seed_eval(conn,tpl['dataset_version'],summary=counts)
        p=read().json();assert p['ai_eval_status']=='not_published' and p['ai_eval'] is None
        assert p['latest_unpublished']['reason']=='Its recorded counts do not add up.'
        lines=[json.loads(s) for s in capsys.readouterr().err.splitlines() if s.strip()]
        assert len(lines)==1 and lines[0]['component']=='readiness' and lines[0]['severity']=='ERROR'


def test_live_fingerprint_uses_documented_compute(monkeypatch):
    from types import SimpleNamespace
    called=[]
    module=SimpleNamespace(HERE='/fixture/eval',compute=lambda root,d,e:called.append((root,d,e)) or 'fingerprint')
    real=about.importlib.import_module
    monkeypatch.setattr(about.importlib,'import_module',lambda name:module if name=='agent.eval.fingerprint' else real(name))
    assert about._live_fingerprint('ds-fixture')=='fingerprint'
    assert called==[('/fixture/eval','ds-fixture','engine-3')]


def test_phase10_group_counts_map_to_ordered_categories():
    from api.aboutschema import validate_categories
    metadata=dict(eval_id='fixture',model_id='model',assistant_version='v',dataset_version='ds',engine_version='engine-3',
      build_commit='sha',reference_fingerprint='fp',published_at='2026-10-18T04:00:00Z',version_match=True)
    summary=dict(groups=[dict(name=name,p=1,a=2,f=1,x=0,failures=[dict(note='A failed item',context='Attempted, not executed')]) for name in ('Safety','Assistant','Reading')],
      safety=[{'result':'PASS'}])
    assert validate_categories(dict(**metadata,**summary))
    summary['groups'][1]['a']=3
    assert not validate_categories(dict(**metadata,**summary))
