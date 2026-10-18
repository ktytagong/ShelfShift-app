"""Stock & lots, Analytics and Forecasts: read-only, store-bound views of one workspace (spec §9 Pages; 06.1-CONTEXT
D-15, D-17, D-31, D-32, D-51, D-52, D-56; 04-API-CONTRACT "Product mode (06.1)", Read views).

Read-only, store-bound. Nothing here writes a record (expired holds are applied first, as every read that touches a
case does), calls the engine worker, or reads physical stock or hidden demand.

- **Lots** (`lots`, `lot_detail`; D-31, D-56). The workspace's recorded lots of one store (HQ may name a store, or
  read every store), the stock people can act on: quantity on the record above 0, in one stable order (expiry day,
  product, lot), paged on the server. Each row carries its expiry and the expiry's source, the recorded quantity, the
  latest count day, units reserved for a stock decision and units still available, incoming deliveries of the same
  product at the store, the forecast demand there from today to the lot's expiry, the next markdown step of the
  product's schedule, open receipt discrepancies, the live stock decision's key and the transfer eligibility (stage 1,
  api/eligibility.py). `action` says what the screen may offer, so it never offers a second decision for a lot that
  has one (D-31): `view` with `case_key` for a live decision, `open` with `flag_key` for a lot this morning's screen
  flagged that has no case, else null. The drawer adds active holds, uncleared review flags (Phase 7's tables, an
  empty list when none is recorded), linked branch transfers, and the history of the lot's stock decisions and
  movements, each with its recorded time and scenario date (D-58). A manager reads only their own store's lots:
  another store's lot is 404, the same answer as a lot that does not exist.
- **Analytics** (`analytics`; D-32, D-51). One store, on a public set only: the top products by public units sold in
  the dataset's history window (the days before day 0 from the calendar's first day, 05.2 D-13), one product's daily
  units and hours out of stock with the forecast made on the workspace's day and its 80% range, the store's simulated
  stock by days to expiry, and the estimated potential waste (Keep: what goes to waste if nothing is done, from the
  stored engine result of a live decision or the morning screen's figure for a flag).
- **Forecasts** (`forecasts`; D-52). One store, on a public set only: the forecast run made on the workspace's day
  (its id, provider, the last history day it used and the period shown) and a page of the store's products, each with
  its daily base values over the period and the sums of its daily base, low and high values. Read-only.

Every value group says its kind (D-15): `public` (sales, hours out of stock), `simulated` (stock, lots, expiry,
deliveries), `estimated` (waste) and `forecast` (precomputed). No field calls a simulated value observed.

Bounds (D-17; 05.2 D-11; T-06.1-47): every read of `ref_sales_day` and `ref_forecast_point` binds the dataset version
and the store (and the product for one series), runs under the set's `series.statement_timeout_ms` and is
aggregated or paged in the database; a public set's reference data is read through `publicref` and `refdata`. Lot
lists are paged with the reference `board.page_size` (a set without one, the small synthetic set, answers one page,
as its board does). On the synthetic set Analytics and Forecasts answer `available` false with a reason instead of
inventing figures.
"""
from __future__ import annotations

from datetime import timezone

import sqlalchemy as sa

from engine.model import scheduled_pct

from data.forecast import PROVIDER_ORDER

from . import bookings, db, eligibility, overlay, publicref, refdata, screen
from .errors import DB_BUSY, ApiError
from .inputs import LIVE

WORLD = "shelfshift"
LOT_KINDS = {"stock": "simulated", "forecast": "forecast"}  # D-15: lots, quantities, expiry and deliveries are simulated
PUBLIC, SIMULATED, ESTIMATED, FORECAST = "public", "simulated", "estimated", "forecast"  # D-15 kinds
TOP = 10  # contract (06.1 Read views): sales by product and potential waste list the top 10
DAILY = 7  # contract: a forecast row carries seven daily base values
AGING = (("0–1", None, 1), ("2–3", 2, 3), ("4–7", 4, 7), ("8+", 8, None))  # contract buckets, days to expiry
NOT_PUBLIC = "Analytics covers the public sales dataset."  # (proposed) 06.1-14; contract change-log
CHOOSE_ANALYTICS = "Choose a branch to see its analytics."  # (proposed) 06.1-14; contract change-log
CHOOSE_FORECASTS = "Choose a branch to see its forecasts."  # (proposed) 06.1-14; contract change-log
BUSY = DB_BUSY  # the db_busy answer to a statement timeout, as the series route (views.series) says it
NO_LOT = "No lot {lot_id}"


# --------------------------------------------------------------------------------------------------- shared

def _iso(value) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _date(ref, day: int | None) -> str | None:
    """The scenario date of a day index (the dataset calendar's), ISO; None for no day (D-58, D-63)."""
    return ref.date(day).isoformat() if day is not None else None


def _store(ref, actor: dict, branch: str | None, required: str | None = None) -> str | None:
    """The store a view reads: a manager's own (any `branch` they send is ignored); HQ's `branch`, a known store (400
    for another), or every store when HQ names none, unless the view needs one (`required`: 400 with that text)."""
    if actor["role"] != "hq":
        return actor["branch"]
    if branch is None:
        if required is not None:
            raise ApiError(400, "bad_request", required)
        return None
    if branch not in ref.branches:
        raise ApiError(400, "bad_request", f"No store {branch}")
    return branch


def _page_size(ref, page_size: int | None) -> int | None:
    """The page size: the reference `board.page_size` by default and as the largest page (400 above it); None for a
    set without one (the synthetic set answers one page, as its board does)."""
    board = ref.settings.get("board")
    if not board:
        return None
    largest = int(board["page_size"])
    size = largest if page_size is None else page_size
    if not 1 <= size <= largest:
        raise ApiError(400, "bad_request", f"page_size runs from 1 to {largest}")
    return size


def _bounded(conn, ref, sql: str, **params) -> list[dict]:
    """Rows of a store-bound read of a public table under the set's `series.statement_timeout_ms` (set locally and put
    back), as the series route reads its history (05.2 D-11): a slow scan is 503 db_busy, never a held connection."""
    limit = ref.series.get("statement_timeout_ms")
    if limit is None:
        return db.rows(conn, sql, **params)
    before = db.one(conn, "SELECT current_setting('statement_timeout') AS t")["t"]
    db.one(conn, "SELECT set_config('statement_timeout', :t, true) AS x", t=str(int(limit)))
    try:
        rows = db.rows(conn, sql, **params)
    except sa.exc.DBAPIError as exc:
        if "statement timeout" not in str(exc):
            raise
        raise ApiError(503, "db_busy", BUSY) from None
    db.one(conn, "SELECT set_config('statement_timeout', :t, true) AS x", t=before)
    return rows


def _unavailable() -> dict:
    return {"available": False, "reason": NOT_PUBLIC}


# --------------------------------------------------------------------------------------------------- lots

LIVE_CASE = ("EXISTS (SELECT 1 FROM cases c WHERE c.run_id = :r AND c.lot_id = l.lot_id "
             "AND c.state NOT IN ('CLOSED', 'REJECTED'))")
# this morning's flag of a lot no case was opened for (the board lists exactly these flags, cases.board_page)
FLAGGED = ("EXISTS (SELECT 1 FROM case_flags f WHERE f.run_id = :f AND f.day = :d AND f.lot_id = l.lot_id "
           "AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.run_id = :r AND c.lot_id = f.lot_id))")


def next_markdown(ref, run_day: int, lot: dict) -> dict | None:
    """The product schedule's next step deeper than what is in force for the lot today (its recorded markdown or the
    schedule's step today, the deeper): {day, date, pct}; None when no deeper step comes before expiry. A step (k,
    pct) applies from day expiry_day - k (engine.model.scheduled_pct); the earliest step wins, then the deeper."""
    schedule = ref.schedules.get(lot["sku"]) or ()
    now = max(int(lot["markdown_pct"] or 0), scheduled_pct(schedule, lot["expiry_day"], run_day))
    steps = sorted((lot["expiry_day"] - k, -pct) for k, pct in schedule if pct > now and lot["expiry_day"] - k > run_day)
    if not steps:
        return None
    day, pct = steps[0]
    return {"day": day, "date": _date(ref, day), "pct": -pct}


def _rows(conn, run: dict, ref, lots: list[dict]) -> list[dict]:
    """The contract's lot rows for these record rows, in their order: every figure read for this page only."""
    if not lots:
        return []
    r, ids = run["run_id"], [l["lot_id"] for l in lots]
    reserved = {x["lot_id"]: float(x["qty"]) for x in db.rows(
        conn, f"SELECT bucket AS lot_id, sum(qty) AS qty FROM reservations WHERE run_id = :r AND kind = 'SOURCE_LOT' "
              f"AND bucket = ANY(:l) AND {LIVE} GROUP BY bucket", r=r, l=ids)}
    live = {x["lot_id"]: x for x in db.rows(
        conn, "SELECT lot_id, case_id, key, state FROM cases WHERE run_id = :r AND lot_id = ANY(:l) "
              "AND state NOT IN ('CLOSED', 'REJECTED')", r=r, l=ids)}
    flagged = {x["lot_id"]: x["key"] for x in db.rows(
        conn, "SELECT f.lot_id, f.key FROM case_flags f WHERE f.run_id = :f AND f.day = :d AND f.lot_id = ANY(:l) "
              "AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.run_id = :r AND c.lot_id = f.lot_id)",
        f=overlay.flags_run(conn, run), d=run["day"], l=ids, r=r)}
    on_hold: dict[str, dict] = {}  # the oldest ACTIVE hold on each lot: the row's "Lot hold" chip (07-UI-SPEC surface 6; 07-22)
    for x in db.rows(conn, "SELECT lot_id, hold_id, kind, reason FROM lot_holds WHERE run_id = :r AND state = 'ACTIVE' "
                           "AND lot_id = ANY(:l) ORDER BY placed_at, hold_id", r=r, l=ids):
        on_hold.setdefault(x.pop("lot_id"), x)
    labels: dict[str, str] = {}  # the label of the confirmed fact in force naming each lot, newest check first (facts.in_force's order)
    for x in db.rows(conn, "SELECT a.adj->>'lot_id' AS lot_id, f.label FROM reported_facts f CROSS JOIN LATERAL "
                           "jsonb_array_elements(f.adjustments) AS a(adj) WHERE f.run_id = :r AND f.replaced_by IS NULL "
                           "AND a.adj->>'lot_id' = ANY(:l) ORDER BY coalesce(f.observed_at, f.confirmed_at) DESC, "
                           "f.confirmed_at DESC, f.fact_id DESC", r=r, l=ids):
        labels.setdefault(x["lot_id"], x["label"])
    open_discrepancies: dict[str, int] = {}
    for x in db.rows(conn, "SELECT a.lot_id, a.new_lot_id FROM discrepancies d JOIN world_actions a ON a.run_id = "
                           "d.run_id AND a.world = :w AND a.action_id = d.action_id WHERE d.run_id = :r AND d.state = "
                           "'OPEN' AND (a.lot_id = ANY(:l) OR a.new_lot_id = ANY(:l))", r=r, w=WORLD, l=ids):
        for lot_id in {x["lot_id"], x["new_lot_id"]} & set(ids):
            open_discrepancies[lot_id] = open_discrepancies.get(lot_id, 0) + 1
    series = sorted({(l["branch"], l["sku"]) for l in lots})
    read, p = overlay.everywhere(conn, run, "i")
    incoming: dict[tuple[str, str], float] = {}
    for x in db.rows(conn, f"SELECT i.branch, i.sku, sum(i.qty) AS qty FROM inbound i WHERE {read} AND i.world = :w "
                           f"AND i.branch = ANY(:b) AND i.sku = ANY(:k) AND i.arrival_day >= :d GROUP BY i.branch, i.sku",
                     w=WORLD, b=sorted({b for b, _ in series}), k=sorted({k for _, k in series}), d=run["day"], **p):
        incoming[(x["branch"], x["sku"])] = round(float(x["qty"]), 2)
    points = {(b, k): ref.forecast(run["day"], b, k) for b, k in series}  # one series each (05.2 D-11)
    out = []
    for l in lots:
        qty, held = float(l["qty"]), reserved.get(l["lot_id"], 0.0)
        pts = points[(l["branch"], l["sku"])]
        days = range(run["day"], l["expiry_day"] + 1)
        demand = round(sum(pts[d][1] for d in days), 2) if days and all(d in pts for d in days) else None
        case, flag = live.get(l["lot_id"]), flagged.get(l["lot_id"])
        pseudo = {"lot_id": l["lot_id"], "source_branch": l["branch"], "sku": l["sku"]}
        out.append({
            "lot_id": l["lot_id"], "branch": l["branch"], "sku": l["sku"], "product": ref.names[l["sku"]],
            "zone": ref.products[l["sku"]].zone, "origin": l["origin"],
            "expiry_day": l["expiry_day"], "expiry_date": _date(ref, l["expiry_day"]),
            "expiry_source": l["expiry_source"], "qty": qty, "markdown_pct": l["markdown_pct"],
            "last_verified_day": l["last_verified_day"], "last_verified_date": _date(ref, l["last_verified_day"]),
            "reserved": round(held, 2), "available": round(max(0.0, qty - held), 2),
            "incoming": incoming.get((l["branch"], l["sku"]), 0.0),
            "forecast_to_expiry": demand, "next_markdown": next_markdown(ref, run["day"], l),
            "discrepancies": open_discrepancies.get(l["lot_id"], 0),
            "lot_hold": on_hold.get(l["lot_id"]), "fact_label": labels.get(l["lot_id"]),  # 07-22
            "case_key": case["key"] if case else None, "case_state": case["state"] if case else None,
            "flag_key": flag if case is None else None,
            "action": "view" if case else ("open" if flag else None),
            "eligibility": eligibility.summary(conn, run, pseudo, l),
            "kinds": dict(LOT_KINDS),
        })
    return out


def lots(conn, run: dict, actor: dict, branch: str | None = None, product: str | None = None, at_risk: bool = False,
         with_case: bool = False, page: int = 1, page_size: int | None = None) -> dict:
    """GET /api/lots: {items, page, page_size, total, scope}. The store's recorded lots with stock (HQ: `branch`, or
    every store), filtered on the server by `product` (a known product code, else 400), `at_risk` (flagged this
    morning or with a live stock decision) and `with_case` (a live stock decision), in one stable order (expiry day,
    product, lot id), one page of `page_size` (a page past the last is empty, with the same total)."""
    ref = refdata.get(run["dataset_version"])
    store = _store(ref, actor, branch)
    if product is not None and product not in ref.products:
        raise ApiError(400, "bad_request", f"No product {product}")
    size = _page_size(ref, page_size)
    bookings.expire_holds(conn, run)
    read, p = overlay.everywhere(conn, run, "l")
    where = f"{read} AND l.world = :w AND l.qty > 0"
    p.update(w=WORLD, r=run["run_id"], f=overlay.flags_run(conn, run), d=run["day"])
    if store is not None:
        where += " AND l.branch = :b"
        p["b"] = store
    if product is not None:
        where += " AND l.sku = :k"
        p["k"] = product
    if with_case:
        where += f" AND {LIVE_CASE}"
    if at_risk:
        where += f" AND ({LIVE_CASE} OR {FLAGGED})"
    total = int(db.one(conn, f"SELECT count(*) AS n FROM lots l WHERE {where}", **p)["n"])
    order = 'ORDER BY l.expiry_day, l.sku COLLATE "C", l.lot_id COLLATE "C"'
    if size is None:
        page, size, rows = 1, total, db.rows(conn, f"SELECT l.* FROM lots l WHERE {where} {order}", **p)
    else:
        rows = db.rows(conn, f"SELECT l.* FROM lots l WHERE {where} {order} LIMIT :lim OFFSET :off",
                       lim=size, off=(page - 1) * size, **p)
    return {"items": _rows(conn, run, ref, rows), "page": page, "page_size": size, "total": total,
            "scope": {"branch": store, "default": actor["role"] != "hq" or branch is None}}


def lot_detail(conn, run: dict, actor: dict, lot_id: str) -> dict:
    """GET /api/lots/{lot_id}: the lot's row (as the list writes it) plus `holds` (active lot holds), `review_flags`
    (uncleared flags of its stock decisions), `transfers` (branch transfer work orders that move it or created it) and
    `history` (its stock decisions and movements by recorded time, each with its scenario date). 404 for a lot this
    workspace's record does not hold, and for a manager, for another store's lot (no hint that it exists)."""
    ref = refdata.get(run["dataset_version"])
    bookings.expire_holds(conn, run)
    lot = overlay.lot(conn, run, WORLD, lot_id)
    if lot is None or (actor["role"] != "hq" and lot["branch"] != actor["branch"]):
        raise ApiError(404, "not_found", NO_LOT.format(lot_id=lot_id))
    r = run["run_id"]
    out = _rows(conn, run, ref, [lot])[0]
    decisions = db.rows(conn, "SELECT c.case_id, c.key, c.state, c.closed_as, c.opened_day, (SELECT min(g.at) FROM "
                              "activity_log g WHERE g.run_id = c.run_id AND g.case_id = c.case_id) AS recorded_at "
                              "FROM cases c WHERE c.run_id = :r AND c.lot_id = :l", r=r, l=lot_id)
    keys = {c["case_id"]: c["key"] for c in decisions}
    holds = db.rows(conn, "SELECT hold_id, kind, reason, placed_by, placed_at, state FROM lot_holds WHERE run_id = :r "
                          "AND lot_id = :l AND state = 'ACTIVE' ORDER BY placed_at, hold_id", r=r, l=lot_id)
    flags = db.rows(conn, "SELECT flag_id, case_id, fact_id, reason, created_at FROM review_flags WHERE run_id = :r "
                          "AND case_id = ANY(:c) AND cleared_at IS NULL ORDER BY created_at, flag_id",
                    r=r, c=sorted(keys))
    transfers = db.rows(conn, "SELECT a.action_id, a.case_id, c.key, a.origin, a.dest, a.qty, a.run_code, a.run_day, "
                              "a.state, a.lot_id, a.new_lot_id FROM world_actions a LEFT JOIN cases c ON c.run_id = "
                              "a.run_id AND c.case_id = a.case_id WHERE a.run_id = :r AND a.world = :w AND a.kind = "
                              "'TRANSFER' AND (a.lot_id = :l OR a.new_lot_id = :l) ORDER BY a.run_day, a.action_id",
                        r=r, w=WORLD, l=lot_id)
    movements = db.rows(conn, "SELECT m.*, c.key FROM movements m LEFT JOIN cases c ON c.run_id = m.run_id AND "
                              "c.case_id = m.case_id WHERE m.run_id = :r AND (m.lot_id = :l OR m.case_id = ANY(:c) OR "
                              "m.action_id = ANY(:a))", r=r, l=lot_id, c=sorted(keys),
                        a=sorted({t["action_id"] for t in transfers}))
    history = [{"type": "stock_decision", "id": c["case_id"], "case_id": c["case_id"], "key": c["key"],
                "state": c["state"], "closed_as": c["closed_as"], "recorded_at": _iso(c["recorded_at"]),
                "scenario_day": c["opened_day"], "scenario_date": _date(ref, c["opened_day"])} for c in decisions]
    history += [{"type": "movement", "id": m["movement_id"], "movement_id": m["movement_id"], "kind": m["kind"],
                 "case_id": m["case_id"], "key": m["key"], "action_id": m["action_id"], "lot_id": m["lot_id"],
                 "qty": float(m["qty"]), "damaged_qty": float(m["damaged_qty"]), "condition": m["condition"],
                 "reason": m["reason"], "actor": {"account": m["recorded_by"], "role": m["role"], "branch": m["branch"]},
                 "recorded_at": _iso(m["recorded_at"]), "scenario_day": m["scenario_day"],
                 "scenario_date": _date(ref, m["scenario_day"])} for m in movements]
    history.sort(key=lambda h: (h["recorded_at"] is None, h["recorded_at"] or "", h["type"], h["id"]))
    out.update(
        holds=[{**h, "placed_at": _iso(h["placed_at"])} for h in holds],
        review_flags=[{**f, "key": keys.get(f["case_id"]), "created_at": _iso(f["created_at"])} for f in flags],
        transfers=[{"action_id": t["action_id"], "case_id": t["case_id"], "key": t["key"], "source": t["origin"],
                    "dest": t["dest"], "qty": None if t["qty"] is None else float(t["qty"]), "lot_id": t["lot_id"],
                    "new_lot_id": t["new_lot_id"], "run_code": t["run_code"], "run_day": t["run_day"],
                    "run_date": _date(ref, t["run_day"]), "state": t["state"]} for t in transfers],
        history=history,
    )
    return out


# --------------------------------------------------------------------------------------------------- analytics

def history_window(ref) -> tuple[int, int]:
    """The dataset's history days from its calendar: the first day through the day before day 0 (05.2 D-13: the
    public train window; days 0 on are the scenario week, whose public sales are the held-out demand, D-15)."""
    return int(ref.calendar["first_day"]), -1


def _sells(conn, ref, store: str, sku: str) -> bool:
    lo, hi = history_window(ref)
    return bool(_bounded(conn, ref, "SELECT 1 AS x FROM ref_sales_day WHERE dataset_version = :v AND store = :s "
                                    "AND sku = :k AND day BETWEEN :a AND :b LIMIT 1",
                         v=ref.dataset_version, s=store, k=sku, a=lo, b=hi))


def _waste(conn, run: dict, ref, store: str) -> list[dict]:
    """The store's estimated potential waste, top 10 by units, each lot once: the case lot's own Keep waste
    (screen.lot_keep_waste, the board's at-risk figure) on the stored engine input of each live stock decision at the
    store, and the morning screen's Keep waste (the same projection) for each flag no case was opened for. A case is
    one lot, and a case's lot is left out of the flag rows. Waste is stated against doing nothing (Keep) only; the
    Keep option's `wasted_units` is the plan's network total over every branch in the input, so it is not used here
    (quick 261018-h9t)."""
    rows = []
    for c in db.rows(conn, "SELECT c.key, c.lot_id, c.sku, r.engine_input FROM cases c JOIN "
                           "recommendations r ON r.run_id = c.run_id AND r.case_id = c.case_id AND r.rec_version = "
                           "c.active_rec_version WHERE c.run_id = :r AND c.source_branch = :b AND c.state NOT IN "
                           "('CLOSED', 'REJECTED')", r=run["run_id"], b=store):
        units = screen.lot_keep_waste(c["engine_input"])
        if units is not None and units > 0:
            rows.append((c, float(units), "engine"))
    for f in db.rows(conn, "SELECT f.key, f.lot_id, f.sku, f.keep_waste FROM case_flags f WHERE f.run_id = :f AND "
                           "f.day = :d AND f.branch = :b AND f.keep_waste > 0 AND NOT EXISTS (SELECT 1 FROM cases c "
                           "WHERE c.run_id = :r AND c.lot_id = f.lot_id)",
                     f=overlay.flags_run(conn, run), d=run["day"], b=store, r=run["run_id"]):
        rows.append((f, float(f["keep_waste"]), "screen"))
    rows.sort(key=lambda x: (-x[1], x[0]["lot_id"]))
    out = []
    for row, units, source in rows[:TOP]:
        product = ref.products[row["sku"]]
        out.append({"lot_id": row["lot_id"], "sku": row["sku"], "product": ref.names[row["sku"]], "key": row["key"],
                    "units": round(units, 2), "kg": round(units * product.kg_per_unit, 2),
                    "value": None if product.price is None else round(units * product.price, 2),
                    "basis": "keep", "source": source, "kind": ESTIMATED})
    return out


def _aging(conn, run: dict, store: str) -> list[dict]:
    """The store's recorded stock by days to expiry (from the workspace's day) in the contract's buckets; a lot past
    its expiry but still on the record counts in the first bucket."""
    read, p = overlay.everywhere(conn, run, "l")
    by_day = db.rows(conn, f"SELECT l.expiry_day, sum(l.qty) AS units, count(*) AS lots FROM lots l WHERE {read} "
                           f"AND l.world = :w AND l.branch = :b AND l.qty > 0 GROUP BY l.expiry_day",
                     w=WORLD, b=store, **p)
    out = []
    for label, lo, hi in AGING:
        within = [x for x in by_day if (lo is None or x["expiry_day"] - run["day"] >= lo)
                  and (hi is None or x["expiry_day"] - run["day"] <= hi)]
        out.append({"bucket": label, "min_days": lo, "max_days": hi,
                    "units": round(sum(float(x["units"]) for x in within), 2), "lots": sum(int(x["lots"]) for x in within),
                    "kind": SIMULATED})
    return out


def analytics(conn, run: dict, actor: dict, branch: str | None = None, product: str | None = None) -> dict:
    """GET /api/analytics: one store's sales by product (top 10 by units in the history window, public), one product's
    daily units with hours out of stock (public) and the forecast made on the workspace's day (low, base, high: the
    80% range), aging stock (simulated) and potential waste (estimated). The product defaults to the store's top
    seller; one the store does not sell (or an unknown code) is 404. HQ must name a store (400). On the synthetic set:
    {available: false, reason}."""
    ref = refdata.get(run["dataset_version"])
    if ref.kind != publicref.KIND:
        return _unavailable()
    store = _store(ref, actor, branch, required=CHOOSE_ANALYTICS)
    lo, hi = history_window(ref)
    v = ref.dataset_version
    if product is not None and (product not in ref.products or not _sells(conn, ref, store, product)):
        raise ApiError(404, "not_found", f"No series {store} {product}")
    sales = _bounded(conn, ref, "SELECT sku, sum(units) AS units, sum(oos_hours) AS oos_hours FROM ref_sales_day "
                                "WHERE dataset_version = :v AND store = :s AND day BETWEEN :a AND :b GROUP BY sku "
                                "ORDER BY sum(units) DESC, sku COLLATE \"C\" LIMIT :n", v=v, s=store, a=lo, b=hi, n=TOP)
    sku = product or (sales[0]["sku"] if sales else None)
    series = None
    if sku is not None:
        days = _bounded(conn, ref, "SELECT day, units, oos_hours FROM ref_sales_day WHERE dataset_version = :v "
                                   "AND store = :s AND sku = :k AND day BETWEEN :a AND :b ORDER BY day",
                        v=v, s=store, k=sku, a=lo, b=hi)
        points = ref.forecast(run["day"], store, sku)  # the forecast made on the workspace's day, one series
        series = {
            "sku": sku, "product": ref.names[sku],
            "forecast_run_id": ref.forecast_runs.get(run["day"], {}).get("forecast_run_id"),
            "forecast_cutoff": (_date(ref, ref.forecast_runs[run["day"]]["cutoff_day"]) if run["day"] in ref.forecast_runs else None),
            "days": [{"day": d["day"], "date": _date(ref, d["day"]), "units": float(d["units"]),
                      "hours_out_of_stock": int(d["oos_hours"]), "kind": PUBLIC} for d in days],
            "forecast": [{"day": d, "date": _date(ref, d), "low": lo_, "base": base, "high": hi_,
                          "provider": ref.provider_of(run["day"], store, sku, d), "kind": FORECAST}
                         for d, (lo_, base, hi_) in sorted(points.items())],
            "kinds": {"days": PUBLIC, "forecast": FORECAST},
        }
    return {
        "available": True, "branch": store, "period": {"from": _date(ref, lo), "to": _date(ref, hi)},
        "sales_by_product": [{"sku": x["sku"], "product": ref.names[x["sku"]], "units": round(float(x["units"]), 2),
                              "hours_out_of_stock": int(x["oos_hours"]), "kind": PUBLIC} for x in sales],
        "series": series,
        "aging": _aging(conn, run, store),
        "waste": _waste(conn, run, ref, store),
        "kinds": {"sales_by_product": PUBLIC, "series": {"days": PUBLIC, "forecast": FORECAST},
                  "aging": SIMULATED, "waste": ESTIMATED},
    }


# --------------------------------------------------------------------------------------------------- forecasts

def forecasts(conn, run: dict, actor: dict, branch: str | None = None, page: int = 1,
              page_size: int | None = None) -> dict:
    """GET /api/forecasts: the forecast run made on the workspace's day ({id, provider, providers, label,
    confidence_level, input_label, history_to, period_from, period_to}: the last history day it used and the first
    seven days it covers) and provider_codes per item (11-CONTEXT D-12; contract "## Judge-ready release (11)")
    and one page of the store's products in product order, each with its seven daily base values
    and the sums of its daily base, low and high values (the 80% range). `simulated_inputs` is false on a public set:
    its forecasts are precomputed from the public sales history (labelled by the run's `input_label`) and no simulated
    stock, lot, expiry or delivery goes in. HQ must name a store (400). On the synthetic set: {available: false,
    reason}. Read-only."""
    ref = refdata.get(run["dataset_version"])
    if ref.kind != publicref.KIND:
        return _unavailable()
    store = _store(ref, actor, branch, required=CHOOSE_FORECASTS)
    size = _page_size(ref, page_size)
    fr = ref.forecast_runs.get(run["day"])
    if fr is None:
        return {"available": True, "branch": store, "run": None, "items": [], "page": page,
                "page_size": size or 0, "total": 0, "kind": FORECAST}
    first, last = run["day"], min(run["day"] + DAILY - 1, int(fr["last_day"]))
    providers = dict(fr.get("providers") or {})
    top = min(providers, key=lambda name: (-providers[name], name)) if providers else None
    p = {"v": ref.dataset_version, "f": fr["forecast_run_id"], "s": store, "a": first, "b": last}
    where = "dataset_version = :v AND forecast_run_id = :f AND store = :s AND day BETWEEN :a AND :b"
    total = int(_bounded(conn, ref, f"SELECT count(DISTINCT sku) AS n FROM ref_forecast_point WHERE {where}", **p)[0]["n"])
    size = total if size is None else size
    rows = _bounded(conn, ref, f"SELECT sku, array_agg(base ORDER BY day) AS daily, sum(base) AS base, sum(low) AS low, "
                               f"sum(high) AS high, array_agg(DISTINCT provider) AS provider_codes FROM ref_forecast_point WHERE {where} GROUP BY sku "
                               f"ORDER BY sku COLLATE \"C\" LIMIT :lim OFFSET :off",
                    lim=size, off=(page - 1) * size, **p) if size else []
    return {
        "available": True, "branch": store,
        "run": {"id": fr["forecast_run_id"], "provider": top, "providers": providers, "label": fr.get("label"),
                "confidence_level": fr.get("confidence_level"), "input_label": fr.get("input_label"),
                "history_to": _date(ref, fr["cutoff_day"]), "period_from": _date(ref, first),
                "period_to": _date(ref, last)},
        "items": [{"sku": x["sku"], "product": ref.names[x["sku"]], "daily_base": [float(b) for b in x["daily"]],
                   "provider_codes": sorted(x["provider_codes"], key=lambda code: (PROVIDER_ORDER.index(code) if code in PROVIDER_ORDER else len(PROVIDER_ORDER), code)),
                   "base_total": round(float(x["base"]), 2), "low_total": round(float(x["low"]), 2),
                   "high_total": round(float(x["high"]), 2), "simulated_inputs": False, "kind": FORECAST}
                  for x in rows],
        "page": page, "page_size": size, "total": total, "kind": FORECAST,
    }
