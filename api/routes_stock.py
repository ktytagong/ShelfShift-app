"""Stock & lots, Analytics and Forecasts routes (06.1 D-31, D-32, D-51, D-52, D-56; 04-API-CONTRACT "Product mode
(06.1)", Read views): read-only, through `_read`, every one bound to a store (api/stock.py).

Query parameters are checked here: text ones single, non-blank, without NUL and at most 200 characters; `page` and
`page_size` whole numbers from 1; `at_risk` and `with_case` 1 or 0. Anything else is 400 bad_request.
"""
from __future__ import annotations

from fastapi import Request

from . import stock
from .app import Session, _query_int, _query_text, _read, app
from .errors import ApiError


def _switch(request: Request, name: str) -> bool:
    """A 1-or-0 filter (absent is 0); anything else is 400 bad_request."""
    value = _query_text(request, name)
    if value is None:
        return False
    if value not in ("1", "0"):
        raise ApiError(400, "bad_request", f"{name} is 1 or 0")
    return value == "1"


def _page(request: Request) -> tuple[int, int | None]:
    return _query_int(request, "page", 1) or 1, _query_int(request, "page_size", 1)


@app.get("/api/lots")
def lot_list(request: Request, s: dict = Session):
    """The store's lots, filtered and paged on the server (06.1 D-31)."""
    branch, product = _query_text(request, "branch"), _query_text(request, "product")
    at_risk, with_case = _switch(request, "at_risk"), _switch(request, "with_case")
    page, size = _page(request)
    return _read(s, lambda conn, run, actor, s: stock.lots(conn, run, actor, branch, product, at_risk, with_case,
                                                            page, size))


@app.get("/api/lots/{lot_id}")
def lot_drawer(lot_id: str, s: dict = Session):
    """One lot's drawer (06.1 D-56); another store's lot is 404 for a manager."""
    return _read(s, lambda conn, run, actor, s: stock.lot_detail(conn, run, actor, lot_id))


@app.get("/api/analytics")
def store_analytics(request: Request, s: dict = Session):
    """One store's analytics (06.1 D-32, D-51)."""
    branch, product = _query_text(request, "branch"), _query_text(request, "product")
    return _read(s, lambda conn, run, actor, s: stock.analytics(conn, run, actor, branch, product))


@app.get("/api/forecasts")
def store_forecasts(request: Request, s: dict = Session):
    """One store's forecasts, read-only (06.1 D-52)."""
    branch = _query_text(request, "branch")
    page, size = _page(request)
    return _read(s, lambda conn, run, actor, s: stock.forecasts(conn, run, actor, branch, page, size))
