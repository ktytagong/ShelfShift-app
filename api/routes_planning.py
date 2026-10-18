"""Today and the planned schedule routes (06.1 D-11, D-17, D-18, D-22, D-29, D-33; 04-API-CONTRACT "Product mode
(06.1)", Read views): read-only, through `_read`, never the engine worker. The views live in api/planning.py; this
module registers on `app` when `app.load_route_modules` imports it.

Query parameters are checked here (single, non-blank, without NUL, at most 200 characters; whole numbers in range) and
against the enumerations in the view; anything else is 400 bad_request.
"""
from __future__ import annotations

from fastapi import Request

from . import planning
from .app import Session, _query_int, _query_text, _read, app


@app.get("/api/today")
def today(request: Request, s: dict = Session):
    """Today for the signed-in store; HQ may name a `branch` (a manager's is ignored), else reads the network form,
    whose table of stores takes `page` and `page_size`."""
    branch = _query_text(request, "branch")
    page, size = _query_int(request, "page", 1) or 1, _query_int(request, "page_size", 1)
    return _read(s, lambda conn, run, actor, s: planning.today(conn, run, actor, branch, page, size))


@app.get("/api/schedule")
def schedule(request: Request, s: dict = Session):
    """The read-only planned schedule: `date` (YYYY-MM-DD, default the scenario date), `view` day | week | month,
    `group` work_order | branch | run, `branch` (HQ only; a manager's is ignored), `activity`, `status`, `page`,
    `page_size`."""
    on, branch = _query_text(request, "date"), _query_text(request, "branch")
    view, group = _query_text(request, "view") or "day", _query_text(request, "group") or "work_order"
    activity, status = _query_text(request, "activity"), _query_text(request, "status")
    planning.check_schedule(view, group, activity, status)
    page, size = _query_int(request, "page", 1) or 1, _query_int(request, "page_size", 1)
    return _read(s, lambda conn, run, actor, s: planning.schedule(
        conn, run, actor, on, view, group, branch, activity, status, page, size))
