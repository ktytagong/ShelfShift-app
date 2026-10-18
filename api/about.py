"""Public release metadata (11-CONTEXT D-05, D-06, D-08; dated judge-ready API contract).

Only declared public fields leave this service. All database reads use the caller's transaction;
missing optional tables are a publication state and every database failure propagates.
"""
from __future__ import annotations

import importlib
import ast
from pathlib import Path

from engine.model import ENGINE_VERSION
from . import config, db, publicref, refdata, sandbox


def _exists(conn, table: str) -> bool:
    return db.one(conn, "SELECT to_regclass(:t) IS NOT NULL AS ok", t=f"public.{table}")["ok"]


def _assistant_version() -> str | None:
    try:
        return importlib.import_module("agent.config").ASSISTANT_VERSION
    except (ModuleNotFoundError, AttributeError):
        return None


def _forecast(ref, tpl: dict) -> dict | None:
    if ref.kind != publicref.KIND:
        return None
    fr = ref.forecast_runs.get(tpl["day"])
    if fr is None:
        return None
    summary = ref.forecast["provider_summary"]
    return {"headline": summary["headline"], "providers": summary["demo"]["by_provider"],
            "run_id": fr["forecast_run_id"], "cutoff_date": ref.date(fr["cutoff_day"]).isoformat()}


def _error_model(ref) -> dict | None:
    if ref.kind == publicref.KIND:
        settings = ref.settings
        if settings.get("error_share") is None or settings.get("error_range") is None:
            return None
        return {"share": settings["error_share"], "range": list(settings["error_range"])}
    model = ref.seed.get("error_model")
    return {"share": model["share"], "range": list(model["range"])} if model else None


def _weekcheck_status() -> str:
    # FLOW-08: inspect the entry point without importing a driver that can read hidden truth.
    command = Path(__file__).with_name("weekcheck.py")
    if not command.is_file():
        return "not_in_release"
    definitions = ast.parse(command.read_text(encoding="utf-8")).body
    has_main = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
                   for node in definitions)
    return "in_code" if has_main else "not_in_release"



def _reporting():
    try:
        return importlib.import_module('api.reporting')
    except ModuleNotFoundError as exc:
        if exc.name != 'api.reporting':
            raise
        return None


def _iso(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


def _benchmark(conn, tpl, ref):
    if tpl is None or ref is None:
        return None
    reporting = _reporting()
    if reporting is not None:
        selected = reporting.benchmark(conn, tpl, {'role':'hq','branch':None})
        if selected['status'] == 'none':
            return None
        row = selected[selected['status']]
        # 08-10's early Reports slice returns selection identity without full summary/window.
        # Read metadata for that exact selected result, preserving Reports' conservative status.
        window = row.get('window') or db.one(conn,
          'SELECT first_day,last_day FROM benchmark_runs WHERE benchmark_id=:b', b=row['benchmark_id'])
        payload = row.get('summary')
        if payload is None:
            stored = db.one(conn,'SELECT payload FROM benchmark_summary WHERE benchmark_id=:b',b=row['benchmark_id']) \
                if _exists(conn,'benchmark_summary') else None
            payload = stored['payload'] if stored else {}
        dataset = row['versions']['dataset']
        match, differs = selected['status'] == 'current', row['differs']
    else:
        row = db.one(conn, """SELECT benchmark_id, published_at, git_commit, first_day, last_day,
            dataset_version, engine_version FROM benchmark_runs WHERE status = 'PUBLISHED'
            ORDER BY published_at DESC, benchmark_id DESC LIMIT 1""")
        if row is None:
            return None
        summary = db.one(conn, 'SELECT payload FROM benchmark_summary WHERE benchmark_id=:b', b=row['benchmark_id']) \
            if _exists(conn,'benchmark_summary') else None
        payload = summary['payload'] if summary else {}
        window = row
        dataset = row['dataset_version']
        differs = [key for key, live in [('dataset_version',tpl['dataset_version']),('engine_version',ENGINE_VERSION)]
                   if row[key] != live]
        match = not differs
    published_ref = refdata.get(dataset) if dataset != tpl['dataset_version'] else ref
    return {'benchmark_id':row['benchmark_id'], 'dataset_version':dataset, 'published_at':_iso(row['published_at']),
            'git_commit':row['git_commit'], 'first_day':published_ref.date(window['first_day']).isoformat(),
            'last_day':published_ref.date(window['last_day']).isoformat(), 'stores':payload.get('network',{}).get('stores'),
            'series':payload.get('series'), 'version_match':match, 'differs':differs}



def _live_fingerprint(dataset_version):
    try:
        module=importlib.import_module('agent.eval.fingerprint')
    except ModuleNotFoundError as exc:
        if exc.name not in ('agent.eval','agent.eval.fingerprint'):
            raise
        return None
    return module.compute(module.HERE,dataset_version,ENGINE_VERSION)


def _latest_unpublished(conn, published_run):
    row=db.one(conn,"""SELECT started_at,status,publish_state,block_reason FROM eval_runs
        WHERE publish_state <> 'PUBLISHED' AND (CAST(:started AS timestamptz) IS NULL OR started_at > CAST(:started AS timestamptz))
        ORDER BY started_at DESC,eval_id DESC LIMIT 1""", started=published_run['started_at'] if published_run else None)
    if row is None:
        return None
    return {'run_date':_iso(row['started_at']), 'reason':row['block_reason'] if row['publish_state']=='BLOCKED'
            else 'The run has not finished.'}


def _ai_eval(conn, dataset_version, tables):
    if not tables['eval_runs']:
        return None,None
    row=db.one(conn,"""SELECT r.eval_id,r.fingerprint,r.model_id,r.build_sha,r.assistant_version,
        r.engine_version,r.dataset_version,r.started_at,p.published_at,p.summary
        FROM eval_publications p JOIN eval_runs r USING (eval_id) WHERE r.publish_state='PUBLISHED'
        ORDER BY p.published_at DESC,r.eval_id DESC LIMIT 1""") if tables['eval_publications'] else None
    latest=_latest_unpublished(conn,row)
    if row is None:
        return None,latest
    live={'model_id':config.GEMINI_MODEL,'assistant_version':_assistant_version(),
          'dataset_version':dataset_version,'engine_version':ENGINE_VERSION,
          'reference_fingerprint':_live_fingerprint(dataset_version)}
    result={key:row[key] for key in ('eval_id','model_id','assistant_version','dataset_version','engine_version')}
    result.update(published_at=_iso(row['published_at']),build_commit=row['build_sha'],reference_fingerprint=row['fingerprint'])
    differs=[key for key,value in live.items() if value != result[key] or (key=='reference_fingerprint' and value is None)]
    result.update(version_match=not differs,differing_fields=differs)
    from .aboutschema import validate_categories
    if not validate_categories({**row['summary'],**result}):
        from .ready import log_dependency_error
        log_dependency_error('readiness','invalid_eval_counts')
        return None,{'run_date':_iso(row['started_at']),'reason':'Its recorded counts do not add up.'}
    return result,latest


def about(conn) -> dict:
    tpl = sandbox.active_template(conn)
    ref = refdata.get(tpl["dataset_version"]) if tpl else None
    # Future publication readers are allowed only after these same-transaction existence checks.
    tables = {name: _exists(conn, name) for name in ("benchmark_runs", "eval_publications", "eval_runs")}
    benchmark = _benchmark(conn, tpl, ref) if tables["benchmark_runs"] else None
    evaluation, latest_unpublished = _ai_eval(conn, tpl["dataset_version"] if tpl else "unseeded", tables)
    return {"git_commit": config.GIT_COMMIT, "engine_version": ENGINE_VERSION,
            "dataset_version": tpl["dataset_version"] if tpl else "unseeded",
            "assistant_version": _assistant_version(), "gemini_model": config.GEMINI_MODEL,
            "vertex_location": config.VERTEX_LOCATION, "label": ref.label if ref else None,
            "data_as_of": ref.data_as_of if ref else None, "forecast": _forecast(ref, tpl) if ref else None,
            "error_model": _error_model(ref) if ref else None, "contact": config.CONTACT_EMAIL,
            "google_cloud": config.GCP, "weekcheck_status": _weekcheck_status(),
            "benchmark_status": "published" if benchmark else "not_published", "benchmark": benchmark,
            "ai_eval_status": "published" if evaluation else "not_published", "ai_eval": evaluation, "latest_unpublished": latest_unpublished}
