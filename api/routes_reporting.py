"""Reports routes (08-CONTEXT D-01, D-04, D-15; 04-API-CONTRACT 'Reports (08)'):
independent read-only requests through `_read`, with store scope enforced by api/reporting.py.
Text parameters are single, nonblank and bounded by `_query_text`.
"""
from fastapi import Request

from . import reporting
from .app import Session, _query_text, _read, app


@app.get("/api/reports/workspace")
def workspace(request: Request, s: dict = Session):
    branch = _query_text(request, "branch")
    return _read(s, lambda conn, run, actor, s: reporting.workspace(conn, run, actor, branch))


@app.get("/api/reports/benchmark")
def benchmark(request: Request, s: dict = Session):
    branch = _query_text(request, "branch")
    return _read(s, lambda conn, run, actor, s: reporting.benchmark(conn, run, actor, branch))
