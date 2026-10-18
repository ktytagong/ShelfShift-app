"""Execution routes (06.1 D-34, D-48, D-49, D-65, D-67; 04-API-CONTRACT "Product mode (06.1)", Execution): a person
records each dispatch, receipt and price change, reconciles a receipt discrepancy and closes the decision, through the
write wrapper with its Idempotency-Key and the run locked FOR UPDATE, so a retried press records once (D-44). The seat comes
from the signed-in session row only (api/app.py `_actor`), never from the body.

Loaded by `app.load_route_modules`; no other module imports it.
"""
from __future__ import annotations

from fastapi import Request

from . import execution
from .app import Body, Session, _read, _text, _whole, _write, app


@app.get("/api/cases/{case_id}/execution")
def execution_view(case_id: str, s: dict = Session):
    """Dispatch {allowed, blocks}, work orders, movements, discrepancies, after-dispatch exceptions and the form
    options, in one read (never the engine worker)."""
    return _read(s, lambda conn, run, actor, s: execution.execution_view(conn, run, case_id))


@app.post("/api/cases/{case_id}/dispatch")
def dispatch(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The source manager records the dispatch of a prepared transfer: {action_id, qty, lot_id, condition}."""
    action_id, lot_id, condition = _text(body, "action_id"), _text(body, "lot_id"), _text(body, "condition")
    qty = _whole(body, "qty")
    return _write(request, s, f"dispatch:{case_id}", body, lambda conn, run, actor, s: execution.dispatch(
        conn, run, actor, case_id, action_id, qty, lot_id, condition), mode="UPDATE")


@app.post("/api/cases/{case_id}/receipt")
def receipt(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The destination manager records what arrived: {action_id, received_qty, damaged_qty, condition, reason}."""
    action_id, condition = _text(body, "action_id"), _text(body, "condition")
    received, damaged = _whole(body, "received_qty"), _whole(body, "damaged_qty")
    reason = _text(body, "reason", required=False)
    return _write(request, s, f"receipt:{case_id}", body, lambda conn, run, actor, s: execution.receipt(
        conn, run, actor, case_id, action_id, received, damaged, condition, reason), mode="UPDATE")


@app.post("/api/cases/{case_id}/price-applied")
def price_applied(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The source manager records that the shelf price was changed for a markdown work order: {action_id,
    confirmed: true}. Without the confirmation no price changes."""
    action_id, confirmed = _text(body, "action_id"), body.get("confirmed")
    return _write(request, s, f"price-applied:{case_id}", body, lambda conn, run, actor, s: execution.price_applied(
        conn, run, actor, case_id, action_id, confirmed), mode="UPDATE")


@app.post("/api/cases/{case_id}/reconcile")
def reconcile(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The owner or HQ settles a receipt discrepancy: {discrepancy_id, resolution, units, reason}."""
    gap_id, resolution = _text(body, "discrepancy_id"), _text(body, "resolution")
    units, reason = _whole(body, "units"), _text(body, "reason", required=False)
    return _write(request, s, f"reconcile:{case_id}", body, lambda conn, run, actor, s: execution.reconcile(
        conn, run, actor, case_id, gap_id, resolution, units, reason), mode="UPDATE")


@app.post("/api/cases/{case_id}/close")
def close(request: Request, case_id: str, s: dict = Session, body: dict = Body):
    """The owner closes a stock decision whose work is recorded: {reason}. An open receipt discrepancy blocks it."""
    reason = _text(body, "reason", required=False)
    return _write(request, s, f"close:{case_id}", body, lambda conn, run, actor, s: execution.close_decision(
        conn, run, actor, case_id, reason), mode="UPDATE")
