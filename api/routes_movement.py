"""The case's transport details and the Branch transfers list (06.1 D-17, D-30, D-33, D-52, D-55; 04-API-CONTRACT
"Product mode (06.1)", Read views): the routes only, both reads. The views live in api/movement.py; this module
registers on `app` when `app.load_route_modules` imports it."""
from __future__ import annotations

from fastapi import Request

from . import movement
from .app import Session, _query_int, _query_text, _read, app


@app.get("/api/cases/{case_id}/transport")
def case_transport(case_id: str, s: dict = Session):
    """The stock decision's Scheduled truck run, milestones, decision and movement (read-only; no engine call)."""
    return _read(s, lambda conn, run, actor, s: movement.transport_view(conn, run, case_id))


@app.get("/api/transfers")
def branch_transfers(request: Request, s: dict = Session):
    """Branch transfers, outgoing by default (`direction` outgoing, incoming or all), for the signed-in store (HQ may
    name a `branch`), filtered by movement `state`, one `page` of `page_size`; anything else is 400 bad_request."""
    direction = _query_text(request, "direction") or "outgoing"
    branch, state = _query_text(request, "branch"), _query_text(request, "state")
    page, size = _query_int(request, "page", 1), _query_int(request, "page_size", 1)
    return _read(s, lambda conn, run, actor, s: movement.transfers(
        conn, run, actor, direction=direction, branch=branch, state=state, page=page or 1, page_size=size))
