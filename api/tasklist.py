"""Tasks as one list (07-CONTEXT D-36 item 3; 07-19; 06.1 D-17 and 07-SCREENS-MAP G5: the server orders and filters
every list page, the client never does). GET /api/tasks keeps `signals.tasks_for`'s keys and adds:

- `tiles`: the six open counts the page's tiles show, `to_answer`, `to_confirm`, `lot_holds`, `advisory` (equal to
  `counts`: a check per request part) and `dispatches`, `receipts` (the store's planned outgoing and dispatched incoming
  transfers, as the transfer list counts them). Their sum is the session's `nav_counts.tasks` (`open_beyond_checks`).
- `list`: one page of task items for `view` ("open", the default, or "closed") and an optional `tile`, with `page`,
  `page_size` and `total` (the reference list page size, as the transfer list pages).

An item is `{"key", "kind", "tile", "open", "urgent", "blocked", "blocked_reasons", "owner_branch", "due_at", "at",
"sku", "parts", "confirmations", "exposure", "hold", "advisory", "transfer"}`; `kind` is request, hold, advisory, dispatch or
receipt, and only its own payload key is filled (`parts` for a request: every part of the request group, the count
part first, so a combined request is one item, never two; `confirmations` {request_id: {"at", "labels"}} for its
confirmed parts, the confirm's own labels from the history, "Count confirmed by {store} manager · {t} · shelf a +
backroom b = c" or "Printed date confirmed by …"; `exposure` {request_id: the ask's decision regret or null}; `sku`
the product code the request, transfer or task is about).

- A request is open while a part waits for its answer or its confirmation; its tile is `to_answer` while any part
  waits for an answer (`urgent` when one of those is overdue; `due_at` the nearest of their deadlines), else
  `to_confirm`. Closed: confirmed, Can't check, superseded, held and legacy expired parts; its tile is `to_confirm`
  once a part was confirmed, else `to_answer`.
- A lot hold is open while ACTIVE (the store owns its lot); released or resolved, it is closed.
- An advisory task is open until Mark done.
- A dispatch is the store's outgoing transfer: open while planned (`blocked` with the dispatch check's lot-hold and
  review-flag blocks, which the dispatch route enforces), closed once dispatched or received. A receipt is a transfer
  coming in: open while dispatched, closed once received.

Open order (urgent and blocked first, D-36): requests to answer by the nearest deadline (an overdue one is the
nearest, so it keeps its place at the top), answers to confirm (oldest answer first), lot holds (oldest first),
dispatches (blocked ones first, then the transfer list's order: run day, departure, work order), receipts (the same
order), advisory tasks (oldest first). Closed order: newest first by the item's real time (`at`: a request's latest
asked, answered or closed time, a hold's resolution, a task's Mark done), then the recorded transfers, latest run first.
Nothing here writes."""
from __future__ import annotations

from datetime import datetime

from . import cases, db, execution, lotholds, movement, refdata, signals
from .errors import ApiError

TILES = ("to_answer", "to_confirm", "lot_holds", "advisory", "dispatches", "receipts")
VIEWS = ("open", "closed")
WAITING = set(signals.TO_ANSWER) | set(signals.TO_CONFIRM)  # a part still waiting on the store
DISPATCH_CLOSED = ("dispatched", "partially_received", "received", "discrepancy")
RECEIPT_CLOSED = ("partially_received", "received", "discrepancy")
_LATEST = datetime.max.replace(tzinfo=None)


def _when(iso: str | None) -> datetime | None:
    return None if iso is None else datetime.fromisoformat(iso).replace(tzinfo=None)


def _item(key: str, kind: str, tile: str, open_: bool, owner: str | None, **payload) -> dict:
    return {"key": key, "kind": kind, "tile": tile, "open": open_, "urgent": False, "blocked": False,
            "blocked_reasons": [], "owner_branch": owner, "due_at": None, "at": None, "sku": None, "parts": [],
            "confirmations": {}, "exposure": {}, "hold": None, "advisory": None, "transfer": None, **payload}


def _confirmations(conn, run: dict, request_ids: list[str]) -> dict:
    """{request_id: {"at", "labels"}} from each confirmed request's latest count_confirmed or expiry_confirmed entry,
    in one statement."""
    if not request_ids:
        return {}
    rows = db.rows(conn, "SELECT DISTINCT ON (payload->>'request_id') payload->>'request_id' AS q, at, payload->'labels' "
                         "AS labels FROM activity_log WHERE run_id = :r AND type IN ('count_confirmed', "
                         "'expiry_confirmed') AND payload->>'request_id' = ANY(:q) ORDER BY payload->>'request_id', "
                         "seq DESC", r=run["run_id"], q=request_ids)
    return {x["q"]: {"at": x["at"].isoformat(), "labels": list(x["labels"] or [])} for x in rows}


def _stored(conn, run: dict, request_ids: list[str]) -> dict:
    """{request_id: (sku, regret)} as the requests store them (the ask's decision regret: the "estimated decision
    exposure" the row's "Why it matters" line words, D-10)."""
    if not request_ids:
        return {}
    return {x["request_id"]: (x["sku"], None if x["regret"] is None else float(x["regret"])) for x in db.rows(
        conn, "SELECT request_id, sku, regret FROM evidence_requests WHERE run_id = :r AND request_id = ANY(:q)",
        r=run["run_id"], q=request_ids)}


def _request_items(conn, run: dict, requests: list[dict]) -> list[tuple[tuple, dict]]:
    """(sort key, item) per request group, the parts count first (07-CONTEXT D-11, D-22)."""
    confirmed = _confirmations(conn, run, [r["request_id"] for r in requests if r["state"] == "CONFIRMED"])
    stored = _stored(conn, run, [r["request_id"] for r in requests])
    groups: dict[str, list[dict]] = {}
    for r in requests:
        groups.setdefault(r.get("request_group") or r["request_id"], []).append(r)
    out = []
    for gid, parts in groups.items():
        parts.sort(key=lambda p: (p.get("part") != "count", p["request_id"]))
        answer = [p for p in parts if p["status_kind"] in signals.TO_ANSWER]
        confirm = [p for p in parts if p["status_kind"] in signals.TO_CONFIRM]
        open_ = bool(answer or confirm)
        item = _item(f"request:{gid}", "request", "to_answer" if answer or (not open_ and not any(
            p["state"] == "CONFIRMED" for p in parts)) else "to_confirm", open_, parts[0]["to_branch"], parts=parts,
            sku=stored.get(parts[0]["request_id"], (None, None))[0],
            exposure={p["request_id"]: stored.get(p["request_id"], (None, None))[1] for p in parts},
            confirmations={p["request_id"]: confirmed[p["request_id"]] for p in parts if p["request_id"] in confirmed})
        dues = sorted(d for d in (_when(p.get("due_at")) for p in answer) if d is not None)
        item["due_at"] = next((p["due_at"] for p in answer if _when(p.get("due_at")) == dues[0]), None) if dues else None
        item["urgent"] = any(p.get("overdue") for p in answer)
        stamps = [v for p in parts for v in (p.get("closed_at"), p.get("answered_at"), p.get("asked_at"),
                                             item["confirmations"].get(p["request_id"], {}).get("at")) if v is not None]
        item["at"] = max(stamps, key=_when) if stamps else None
        if answer:
            rank = (0, dues[0] if dues else _LATEST, gid)
        else:
            answered = sorted(t for t in (_when(p.get("answered_at")) for p in confirm) if t is not None)
            rank = (1, answered[0] if answered else _LATEST, gid)
        out.append((rank, item))
    return out


def _closed_holds(conn, run: dict, branch: str) -> list[dict]:
    return db.rows(conn, "SELECT * FROM lot_holds WHERE run_id = :r AND branch = :b AND state <> 'ACTIVE' "
                         "ORDER BY resolved_at DESC, hold_id", r=run["run_id"], b=branch)


def _all_transfers(conn, run: dict, actor: dict, direction: str) -> list[dict]:
    """Every transfer row of the direction for the store, in the transfer list's order (every page of it)."""
    first = movement.transfers(conn, run, actor, direction=direction)
    items, page = list(first["items"]), 1
    while len(items) < first["total"]:
        page += 1
        more = movement.transfers(conn, run, actor, direction=direction, page=page, page_size=first["page_size"])
        if not more["items"]:
            break
        items += more["items"]
    return items


def _dispatch_blocks(conn, run: dict, case_id: str) -> list[dict]:
    """The dispatch check's lot-hold and review-flag blocks for the case (api/execution.py, the same reads the
    dispatch route refuses on); the recheck is not run here: a list page never calls the engine."""
    if not execution.support_present(conn):
        return []
    case = cases.case_row(conn, run["run_id"], case_id)
    return execution._hold_blocks(conn, run, case) + execution._flag_blocks(conn, run, case)


def view(conn, run: dict, actor: dict, view: str = "open", tile: str | None = None, page: int = 1,
         page_size: int | None = None) -> dict:
    """GET /api/tasks with `tiles` and `list` (module docstring). An unknown `view` or `tile` is 400 bad_request."""
    if view not in VIEWS:
        raise ApiError(400, "bad_request", f"view is one of {', '.join(VIEWS)}")
    if tile is not None and tile not in TILES:
        raise ApiError(400, "bad_request", f"tile is one of {', '.join(TILES)}")
    if page < 1:
        raise ApiError(400, "bad_request", "page must be a whole number from 1")
    base = signals.tasks_for(conn, run, actor)
    branch = actor.get("branch") if actor.get("role") == "manager" else None
    ranked: list[tuple[tuple, dict]] = []
    tiles = {**base["counts"], "dispatches": 0, "receipts": 0}
    if branch is not None:
        ranked += _request_items(conn, run, base["requests"])
        for h in base["holds"]:
            ranked.append(((2, _when(h["placed_at"]), h["hold_id"]), _item(
                f"hold:{h['hold_id']}", "hold", "lot_holds", True, h["owner_branch"], hold=h, at=h["placed_at"])))
        for h in _closed_holds(conn, run, branch):
            v = lotholds.view(conn, run, h)
            ranked.append(((9, None, h["hold_id"]), _item(
                f"hold:{h['hold_id']}", "hold", "lot_holds", False, v["owner_branch"], hold=v, at=v["resolved_at"])))
        task_skus = {x["signal_id"]: x["sku"] for x in db.rows(
            conn, "SELECT signal_id, sku FROM signals WHERE run_id = :r AND signal_id = ANY(:s)", r=run["run_id"],
            s=[t["signal_id"] for t in base["advisory"]])} if base["advisory"] else {}
        for i, t in enumerate(base["advisory"]):
            done = t["task"]["done_at"]
            ranked.append(((5, i, t["signal_id"]), _item(
                f"advisory:{t['signal_id']}", "advisory", "advisory", done is None, t.get("branch") or branch,
                advisory=t, at=done or t.get("confirmed_at"), sku=task_skus.get(t["signal_id"]))))
        for i, t in enumerate(_all_transfers(conn, run, actor, "outgoing")):
            if t["movement_state"] == "planned":
                blocks = _dispatch_blocks(conn, run, t["case_id"]) if t["decision_state"] == "PREPARED" else []
                tiles["dispatches"] += 1
                ranked.append(((3, 0 if blocks else 1, i), _item(
                    f"dispatch:{t['action_id']}", "dispatch", "dispatches", True, t["source"], transfer=t,
                    sku=t["sku"], blocked=bool(blocks), blocked_reasons=blocks)))
            elif t["movement_state"] in DISPATCH_CLOSED:
                ranked.append(((9, None, i), _item(
                    f"dispatch:{t['action_id']}", "dispatch", "dispatches", False, t["source"], transfer=t, sku=t["sku"])))
        for i, t in enumerate(_all_transfers(conn, run, actor, "incoming")):
            if t["movement_state"] == "dispatched":
                tiles["receipts"] += 1
                ranked.append(((4, i, t["action_id"]), _item(
                    f"receipt:{t['action_id']}", "receipt", "receipts", True, t["dest"], transfer=t, sku=t["sku"])))
            elif t["movement_state"] in RECEIPT_CLOSED:
                ranked.append(((9, None, i), _item(
                    f"receipt:{t['action_id']}", "receipt", "receipts", False, t["dest"], transfer=t, sku=t["sku"])))
    chosen = [(k, it) for k, it in ranked if it["open"] == (view == "open") and (tile is None or it["tile"] == tile)]
    if view == "open":
        chosen.sort(key=lambda p: p[0])
        items = [it for _, it in chosen]
    else:
        timed = sorted((it for _, it in chosen if it["at"] is not None), key=lambda it: (_when(it["at"]), it["key"]),
                       reverse=True)
        moved = [it for _, it in chosen if it["at"] is None]
        moved.sort(key=lambda it: (it["transfer"]["run_day"] or 0, it["transfer"]["departs_min"] or 0,
                                   it["key"]), reverse=True)
        items = timed + moved
    size = movement._page_size(refdata.get(run["dataset_version"]), page_size, len(items))
    first = (page - 1) * size
    return {**base, "tiles": tiles,
            "list": {"view": view, "tile": tile, "items": items[first:first + size], "page": page, "page_size": size,
                     "total": len(items)}}


def open_beyond_checks(conn, run: dict, branch: str) -> int:
    """The store's ACTIVE lot holds plus its open advisory tasks, in one statement: what `nav_counts.tasks` adds to
    the stock checks and the transfer totals, so the menu count is the six tiles' sum (07-SCREENS-MAP section 1)."""
    return int(db.one(conn, "SELECT (SELECT count(*) FROM lot_holds WHERE run_id = :r AND state = 'ACTIVE' "
                            "AND branch = :b) + (SELECT count(*) FROM signals WHERE run_id = :r AND report_id IS NOT "
                            "NULL AND kind = 'CONDITION' AND state = 'CONFIRMED' AND fields->>'condition' = 'DISPLAY' "
                            "AND branch = :b AND task_done_at IS NULL) AS n", r=run["run_id"], b=branch)["n"])
