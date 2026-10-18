"""Take ownership (06.1 D-25, D-67; 04-API-CONTRACT "Ownership and the steps"): the route only. The rules live in
api/ownership.py; this module registers on `app` when `app.load_route_modules` imports it."""
from __future__ import annotations

from fastapi import Request

from . import ownership
from .app import Body, Session, _write, app


@app.post("/api/cases/{case_id}/take-ownership")
def take_ownership(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The source store manager or HQ takes the stock decision (key; `{}`); {owner, taken_at}."""
    return _write(request, s, f"take-ownership:{case_id}", body,
                  lambda conn, run, actor, s: ownership.take_ownership(conn, run, actor, case_id))
