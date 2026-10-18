"""Reports routes (08-CONTEXT D-01, D-04, D-15; 04-API-CONTRACT 'Reports (08)'):
independent read-only requests through `_read`, with store scope enforced by api/reporting.py.
Text parameters are single, nonblank and bounded by `_query_text`.
"""
import re

from fastapi import Request

from . import reporting
from .app import Session, _query_int, _query_text, _read, app
from .errors import ApiError


@app.get("/api/reports/workspace")
def workspace(request: Request, s: dict = Session):
    branch = _query_text(request, "branch")
    page = _query_int(request, "page", 1) or 1
    page_size = _query_int(request, "page_size", 1, 100) or 20
    return _read(s, lambda conn, run, actor, s: reporting.workspace(conn, run, actor, branch, page, page_size))


@app.get("/api/reports/benchmark")
def benchmark(request: Request, s: dict = Session):
    branch = _query_text(request, "branch")
    ident = _query_text(request, 'benchmark_id')
    by, chart = _query_text(request, 'breakdown'), _query_text(request, 'chart')
    page = _query_int(request, 'page', 1) or 1
    size = _query_int(request, 'page_size', 1, 100) or 20
    if ident is not None and not re.fullmatch(r'bm-[0-9a-f]{12}', ident):
        raise ApiError(400, 'bad_request', 'benchmark_id is malformed')
    if (by is not None or chart is not None) and (by not in ('day', 'branch') or
                                                chart not in ('money', 'waste') or ident is None):
        raise ApiError(400, 'bad_request', 'breakdown requires benchmark_id, day or branch, and money or waste')
    return _read(s, lambda conn, run, actor, s: reporting.benchmark(
        conn, run, actor, branch, benchmark_id=ident, chart=chart, by=by, page=page, page_size=size))
