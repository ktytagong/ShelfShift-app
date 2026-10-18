"""The BASE oracle for quick 261017-tuf: verbatim copies of the view functions that quick changes, as they were at
53bfe777 (quick 261017-l5q's tip), renamed old_*.

Each copy calls the other old_* copies and the helpers this quick does not change, imported from api. `oracle()`
installs the copies over cases.card, cases.board, cases.board_page, planning._entries, planning._network and
planning.nav_counts, so that inside it views.session_info, planning.today, planning.schedule and cases.detail all run
the BASE path. tests/api/test_view_identity.py compares the two paths byte for byte.

This module is never collected (no test_ prefix) and never imported by api/. The only edits to the copied bodies are
the old_* renames and `from . import flags` written as `from api import flags` (a relative import cannot work here).
Never edit a body to match new output: the copies are the reference. The one exception is a change the user decided:
quick 261018-h9t (2026-10-18) made a card's at-risk keys the case lot's own Keep waste, so old_card computes them the
same way and the identity test keeps checking everything else.
"""
from __future__ import annotations

import contextlib

import pytest

from api import bookings, cases, db, movement, overlay, planning, refdata, screen, stepkeys  # noqa: F401
from api.cases import (COLUMNS, DUE_STATES, RISKS, _filtered, _scope, active_rec, badges, board_order,  # noqa: F401
                       check_filters, decided_roles, due, execution_state, hold_info, lot_meta, next_owner,
                       parse_plan, stage_counts, wait_view)
from api.errors import ApiError  # noqa: F401
from api.planning import CHECK_STATES, COUNTS, _abs, _cards, _counts, scenario_date, today  # noqa: F401


@contextlib.contextmanager
def oracle():
    """Inside: the BASE path for the session's counts, Today, the board, the schedule and the case detail."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cases, "card", old_card)
        mp.setattr(cases, "board", old_board)
        mp.setattr(cases, "board_page", old_board_page)
        mp.setattr(planning, "_entries", old__entries)
        mp.setattr(planning, "_network", old__network)
        mp.setattr(planning, "nav_counts", old_nav_counts)
        yield


# --- BASE api/planning.py _entries (lines 80-97) ---
def old__entries(conn, run: dict, store: str | None) -> tuple[list[dict], list[dict]]:
    """(the stock decisions in scope, each {"case", "card"}, in case order; the workspace's stock checks still waiting
    on a store). In scope: every case for HQ's network form, else the cases whose source or recommended destination is
    the store, or whose work order leaves from or arrives at it. Expired holds are applied first."""
    bookings.expire_holds(conn, run)
    where, p = "c.run_id = :r", {"r": run["run_id"]}
    if store is not None:
        where += (" AND (c.source_branch = :b OR c.plan_dest = :b OR EXISTS (SELECT 1 FROM world_actions a "
                  "WHERE a.run_id = c.run_id AND a.world = 'shelfshift' AND a.case_id = c.case_id "
                  "AND (a.origin = :b OR a.dest = :b)))")
        p["b"] = store
    rows = db.rows(conn, f"SELECT c.* FROM cases c WHERE {where} ORDER BY c.case_id", **p)
    requests = db.rows(conn, "SELECT q.*, c.key AS case_key FROM evidence_requests q "
                             "JOIN cases c USING (run_id, case_id) WHERE q.run_id = :r "
                             "ORDER BY q.created_at, q.request_id", r=run["run_id"])
    entries = [{"case": c, "card": old_card(conn, run, c, requests)} for c in rows]
    waiting = [q for q in requests if q["state"] in CHECK_STATES]
    return entries, waiting


# --- BASE api/planning.py _network (lines 478-502) ---
def old__network(conn, run: dict, ref: refdata.Ref, actor: dict, page: int, page_size: int | None) -> dict:
    """HQ's network form (D-22, D-33): the four counts summed over every store, 05.2 D-38's `evaluated` and `at_risk`
    from the board's own rule for HQ's unfiltered scope, the stores needing attention (any count above 0) ordered by
    decisions due, descending, then store code and paged, and the cards that wait on HQ."""
    day = run["day"]
    entries, waiting = old__entries(conn, run, None)
    stores = sorted({e["card"]["source"] for e in entries} | {q["to_branch"] for q in waiting} |
                    {w["branch"] for e in entries for w in e["card"]["next_owner"] if w.get("branch")})
    rows = []
    for b in stores:
        counts = _counts(entries, waiting, day, b)
        if any(counts[k] for k in COUNTS):
            rows.append({"branch": b, **counts})
    rows.sort(key=lambda r: (-r["decisions_due"], r["branch"]))
    firsts = [r["first_deadline"] for r in rows if r["first_deadline"]]
    board = old_board_page(conn, run, actor, page_size=1)
    network = {**{k: sum(r[k] for r in rows) for k in COUNTS},
               "first_deadline": min(firsts, key=_abs) if firsts else None,
               "evaluated": board.get("evaluated", board["total"]), "at_risk": board.get("at_risk", board["total"])}
    size = movement._page_size(ref, page_size, len(rows))
    first = (page - 1) * size
    return {"scenario_date": scenario_date(ref, run), "day": day, "branch": None,
            "scope": {"branch": None, "default": True}, "network": network,
            "branches": {"items": rows[first:first + size], "page": page, "page_size": size, "total": len(rows)},
            "cards": _cards(entries, waiting, ref, run, actor, None)}


# --- BASE api/planning.py nav_counts (lines 517-532) ---
def old_nav_counts(conn, run: dict, session: dict) -> dict:
    """GET /api/session `nav_counts` for the signed-in account: `today` (Today's action cards), `board` (the board's
    open stock decisions in the account's default scope: every stage but closed, flags included) and `tasks` (a
    store's stock checks waiting on it, transfers to dispatch from it and transfers to receive at it; HQ none)."""
    if not session.get("account"):
        return {"today": 0, "board": 0, "tasks": 0}
    actor = {"account": session["account"], "role": session["role"], "branch": session["branch"]}
    page = today(conn, run, actor)
    board = old_board_page(conn, run, actor, page_size=1)
    tasks = 0
    if actor["role"] != "hq":
        tasks = page["summary"]["stock_checks"] + sum(
            movement.transfers(conn, run, actor, direction=d, state=s)["total"]
            for d, s in (("outgoing", "planned"), ("incoming", "dispatched")))
    return {"today": len(page["cards"]),
            "board": sum(board["stage_counts"][k] for k in stepkeys.STAGES), "tasks": tasks}


# --- BASE api/cases.py card (lines 465-500) ---
def old_card(conn, run: dict, case: dict, requests: list[dict]) -> dict:
    """One board card, every contract key present (04-API-CONTRACT Card), with the step, stage, owner, next action
    and blocked keys computed once by api/stepkeys.py (06.1 D-23, D-24, D-54). `requests` may hold other cases'."""
    ref = refdata.get(run["dataset_version"])
    rec = active_rec(conn, case)
    result = rec["result"] if rec else None
    plan = parse_plan(result["recommended"]) if result else None
    best = next(r for r in result["options"] if r["key"] == result["recommended"]) if result else None
    risk = screen.lot_keep_waste(rec["engine_input"]) if rec else None
    decided = decided_roles(conn, case)
    holds = hold_info(conn, case)
    meta = lot_meta(conn, run, case)
    product = ref.products[case["sku"]]
    mine = [r for r in requests if r["case_id"] == case["case_id"]]
    steps = stepkeys.keys(conn, run, case, plan, decided, mine, execution_state(conn, case))
    return {
        "case_id": case["case_id"], "key": case["key"], "tag": case["tag"], "origin": meta["origin"],
        "state": case["state"], "column": COLUMNS[case["state"]],
        "sku": case["sku"], "product": ref.names[case["sku"]], "zone": product.zone,
        "source": case["source_branch"], "dest": plan.dest if plan else None, "lot_id": case["lot_id"],
        "suggested": result["recommended_label"] if result else None,
        "suggested_key": result["recommended"] if result else None,
        # quick 261018-h9t (user decision 2026-10-18): the at-risk keys are the case lot's own Keep waste
        "at_risk_units": None if risk is None else round(risk, 2),
        "at_risk_kg": None if risk is None else round(risk * product.kg_per_unit, 2),
        "at_risk_value": None if risk is None or product.price is None else round(risk * product.price, 2),
        "est_benefit": best["delta_vs_keep"] if best else None,
        "due": due(result, ref) if case["state"] in DUE_STATES else None,
        "decide_by": (result or {}).get("decide_by"), "markdown_step": (result or {}).get("markdown_step"),
        "act_before": (result or {}).get("act_before"),
        "wait": wait_view(result, rec["engine_input"] if rec else None),
        "rec_version": case["active_rec_version"],
        "next_owner": next_owner(case, plan, decided),
        "decisions": decided, "hold": holds,
        "badges": badges(case, rec, holds, mine),
        "reason": case["reason"] or case["missing_data"], "waiting_until_day": case["waiting_until_day"],
        **steps,
    }


# --- BASE api/cases.py board (lines 503-513) ---
def old_board(conn, run: dict) -> list[dict]:
    """Every case's card, sorted by the absolute minute of `due`, then at-risk value descending, then key; a card
    with no due date (resolved, or no deadline) comes last. Equal deadlines order the same way in every sandbox.
    Expired holds are applied first (spec §8 Hold expiry: any read that touches a case)."""
    bookings.expire_holds(conn, run)
    rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r ORDER BY case_id", r=run["run_id"])
    requests = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r ORDER BY created_at, request_id",
                       r=run["run_id"])
    cards = [old_card(conn, run, c, requests) for c in rows]
    cards.sort(key=board_order)
    return cards


# --- BASE api/cases.py board_page (lines 580-665) ---
def old_board_page(conn, run: dict, actor: dict | None = None, branch: str | None = None, risk: str | None = None,
               page: int = 1, page_size: int | None = None, owner: str | None = None, product: str | None = None,
               deadline: str | None = None, blocked: str | None = None) -> dict:
    """GET /api/cases as {cases, page, page_size, total, scope, stage_counts, blocked_total[, evaluated, at_risk]}
    (05.2 D-24, D-26, D-38; 06.1 D-17, D-23).

    A set whose reference settings carry no `board` (the synthetic set) answers every case's card in the board's
    order on one page, as before: page 1, page_size = total, scope every store; `branch`, `risk` and `page` change
    nothing there. A public set's board is scoped (`_scope`), filtered by `risk` ("waste": flags whose rule (a) holds
    and cases whose Keep wastes stock; "stock": flags whose rule (b) holds and cases opened on it) and paginated in
    one stable server order: the cases in scope (source or recommended destination is the store) in the board's order
    (deadline, value at risk, key), then this morning's flags at the store that this workspace has not opened, by Keep
    waste, then stock at the step day, descending (a flag the screen could not judge last), then key. `page_size`
    defaults to the reference `board.page_size`, which is also the largest page (400 above it); a page past the last
    is an empty list with the same total. `evaluated` counts the cases in scope (each went through the engine) and
    `at_risk` adds the unevaluated flags, so the board can show evaluated against total (D-38). Expired holds are
    applied first.

    The 06.1 filters apply on both sets, on the server, before paging, and never change the order: `owner=me` (cases
    the signed-in account owns), `product=<code>` (a product of the reference data, else 400), `deadline`
    (`scenario_date`: due on the session's scenario day; `none`: no deadline) and `blocked=1` (the server's blocked
    flag). Owner and blocked leave the flags out (nobody owns one and none is blocked); product and deadline apply to
    them (a flag has no deadline). `stage_counts` and `blocked_total` count the filtered list, every page of it, and
    `evaluated` and `at_risk` follow the same scope and filters."""
    from api import flags  # flags reads this module's views
    ref = refdata.get(run["dataset_version"])
    check_filters(owner, deadline, blocked)
    if product is not None and product not in ref.products:
        raise ApiError(400, "bad_request", f"No product {product}")
    if "board" not in ref.settings:
        cards = _filtered(old_board(conn, run), run, actor, owner, product, deadline, blocked)
        return {"cases": cards, "page": 1, "page_size": len(cards), "total": len(cards),
                "scope": {"branch": None, "default": True}, "stage_counts": stage_counts(cards),
                "blocked_total": sum(1 for c in cards if c["blocked"])}
    largest = int(ref.settings["board"]["page_size"])
    size = largest if page_size is None else page_size
    if not 1 <= size <= largest:
        raise ApiError(400, "bad_request", f"page_size runs from 1 to {largest}")
    if risk is not None and risk not in RISKS:
        raise ApiError(400, "bad_request", f"risk is one of {', '.join(RISKS)}")
    store, default = _scope(ref, actor, branch)
    bookings.expire_holds(conn, run)
    where, p = "c.run_id = :r", {"r": run["run_id"]}
    if store is not None:
        where += " AND (c.source_branch = :b OR c.plan_dest = :b)"
        p["b"] = store
    if risk == "stock":  # rule (b) held when the case opened (its `opened` entry carries the screen's figures)
        where += (" AND EXISTS (SELECT 1 FROM activity_log g WHERE g.run_id = c.run_id AND g.case_id = c.case_id "
                  "AND g.type = 'opened' AND g.payload->>'b' = 'true')")
    if product is not None:  # a bound parameter, never text in the statement
        where += " AND c.sku = :p"
        p["p"] = product
    rows = db.rows(conn, f"SELECT c.* FROM cases c WHERE {where} ORDER BY c.case_id", **p)
    requests = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r ORDER BY created_at, request_id",
                       r=run["run_id"])
    cards = [old_card(conn, run, c, requests) for c in rows]
    if risk == "waste":
        cards = [c for c in cards if (c["at_risk_units"] or 0.0) > 0]
    cards = _filtered(cards, run, actor, owner, product, deadline, blocked)
    cards.sort(key=board_order)
    fwhere = ("f.run_id = :f AND f.day = :d AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.run_id = :r "
              "AND c.lot_id = f.lot_id)")
    fp = {"f": overlay.flags_run(conn, run), "d": run["day"], "r": run["run_id"]}
    if store is not None:
        fwhere += " AND f.branch = :b"
        fp["b"] = store
    if risk is not None:
        fwhere += " AND f.a" if risk == "waste" else " AND f.b"
    if product is not None:
        fwhere += " AND f.sku = :p"
        fp["p"] = product
    if owner is not None or blocked is not None or deadline == "scenario_date":
        fwhere += " AND false"  # nobody owns a flag, none is blocked, and a flag has no deadline
    n_flags = db.one(conn, f"SELECT count(*) AS n FROM case_flags f WHERE {fwhere}", **fp)["n"]
    first = (page - 1) * size
    shown = cards[first:first + size]
    room = size - len(shown)
    frows = db.rows(conn, f"SELECT * FROM case_flags f WHERE {fwhere} ORDER BY f.keep_waste DESC NULLS LAST, "
                          f"f.stock_at_step DESC NULLS LAST, f.key COLLATE \"C\" LIMIT :lim OFFSET :off",
                    lim=room, off=max(0, first - len(cards)), **fp) if room > 0 else []
    total = len(cards) + int(n_flags)
    return {"cases": shown + [flags.flag_card(ref, f) for f in frows], "page": page, "page_size": size,
            "total": total, "scope": {"branch": store, "default": default},
            "evaluated": len(cards), "at_risk": total,
            "stage_counts": stage_counts(cards, int(n_flags)),
            "blocked_total": sum(1 for c in cards if c["blocked"])}
