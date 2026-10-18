"""One scope matcher for a confirmed fact (07-CONTEXT D-16, D-17; research Pattern 3, F-12; 04-API-CONTRACT "Review
flag").

A fact's scope (`FactScope`) is the lots, branches, product (SKU and zone), runs and validity business days it is
about, built from its adjustments by `of_adjustments`. `affected(conn, run, scope)` splits the cases in scope by state:

- **live** (OPEN, WAITING, PENDING_APPROVAL), locked FOR UPDATE: confirmation re-plans every one of them (D-17);
- **committed** (APPROVED, PREPARED, EXECUTED, OBSERVED): confirmation flags them, never re-plans them;
- **closed** (REJECTED, and CLOSED with closed_as CANCELLED; 07-11): in scope for later reassessment (07-12). A case
  CLOSED because its lot is gone (closed_as null) or completed is in no list: it stays closed (D-24).

Each list is in case_id order. Matching reads each case's **stored** active input only (its lots, source branch, the
input runs' destinations and run codes, the product's zone, the decision day to the input's last forecast day) plus
the lots it involves (`involved_lots`: the case lot, lots moved from it, the lot it was moved from). A case with no
active version is matched on its own lot, source branch and product. Nothing in this module runs the engine or
builds a fresh input: whether a committed case is flagged never depends on a search (D-17).

- A lot fact (adjustments naming lots) matches a case whose input or involved lots hold one of its lots.
- A branch fact matches a case whose source or input destination is one of its branches.
- A run fact matches a case whose input holds one of its run codes.
- A fact with none of these (a product-wide one) matches every case of its SKU and zone.
- The SKU and zone, when the fact names them, must match; a validity range that ends before a case's decision day, or
  starts after the input's last forecast day, matches nothing.

The SQL prefilter keeps the scan to the fact's SKU (lot facts) or branches (branch facts), as research F-12 asks.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import cases, db, overlay, refdata, reviewflags

WORLD = "shelfshift"
COMMITTED = reviewflags.STATES  # flagged by scope, never re-run (D-17)
CLOSED = ("REJECTED", "CLOSED")  # closed by a person: REJECTED, or CLOSED as CANCELLED only (CLOSED_AS_CANCELLED)
CLOSED_AS_CANCELLED = " AND (c.state <> 'CLOSED' OR c.closed_as = 'CANCELLED')"  # never a lot-gone or completed close


@dataclass(frozen=True)
class FactScope:
    lot_ids: frozenset[str] = frozenset()
    branches: frozenset[str] = frozenset()
    sku: str | None = None
    zone: str | None = None
    run_codes: frozenset[str] = frozenset()
    day_from: int | None = None
    day_to: int | None = None


def of_adjustments(adjustments, *, sku: str | None, branch: str | None, zone: str | None, valid_from: int | None,
                   valid_to: int | None) -> FactScope:
    """The scope of a fact with these adjustments: their lots, run codes and branches; the fact's own `branch` counts
    as a branch only for a fact naming no lot and no run (a count is about its lots, wherever they are read)."""
    lots = {a["lot_id"] for a in adjustments if a.get("lot_id")}
    runs = {a["run_code"] for a in adjustments if a.get("run_code")}
    branches = {a["branch"] for a in adjustments if a.get("branch")}
    if branch and not lots and not runs:
        branches.add(branch)
    return FactScope(frozenset(lots), frozenset(branches), sku, zone, frozenset(runs), valid_from, valid_to)


def involved_lots(conn, run: dict, case: dict) -> list[str]:
    """The case lot, the lots moved from it (`moved_from`) and the lot it was moved from, as the workspace reads them
    (api/overlay.py)."""
    lot_id = case["lot_id"]
    out = {lot_id}
    row = overlay.lot(conn, run, WORLD, lot_id)
    if row is not None and row["moved_from"]:
        out.add(row["moved_from"])
    read, p = overlay.everywhere(conn, run, "lots")
    out |= {r["lot_id"] for r in db.rows(
        conn, f"SELECT lots.lot_id FROM lots WHERE {read} AND lots.world = :w AND lots.sku = :s "
              "AND lots.moved_from = :l", w=WORLD, s=case["sku"], l=lot_id, **p)}
    return sorted(out)


def involved_many(conn, run: dict, cases_: list[dict], lots: dict | None = None) -> dict[str, list[str]]:
    """`involved_lots` for many cases (the board's set-based cards): {case_id: the case lot, the lots moved from it and
    the lot it was moved from, sorted}, as the workspace reads them. `lots`: {lot_id: the case lot's record row or None}
    already read (overlay.lot_rows, `overlay.lot`'s rule), else read here; then one read of the lots moved from any of
    the case lots, matched to a case by its lot and SKU as `involved_lots` matches them."""
    ids = sorted({c["lot_id"] for c in cases_})
    if not ids:
        return {}
    rows = lots if lots is not None else overlay.lot_rows(conn, run, WORLD, ids)
    read, p = overlay.everywhere(conn, run, "lots")
    moved = db.rows(conn, f"SELECT lots.lot_id, lots.sku, lots.moved_from FROM lots WHERE {read} AND lots.world = :w "
                          "AND lots.moved_from = ANY(:l)", w=WORLD, l=ids, **p)
    out = {}
    for c in cases_:
        lot_id = c["lot_id"]
        found = {lot_id}
        row = rows.get(lot_id)
        if row is not None and row["moved_from"]:
            found.add(row["moved_from"])
        found |= {m["lot_id"] for m in moved if m["moved_from"] == lot_id and m["sku"] == c["sku"]}
        out[c["case_id"]] = sorted(found)
    return out


def matches(scope: FactScope, case: dict, stored_input: dict | None, involved, zone: str | None) -> bool:
    """True when the case is in the fact's scope, judged on its stored active input (None: no version) and the lots it
    involves; `zone` is the case product's zone. A pure function: no query, no engine."""
    if scope.sku is not None and case["sku"] != scope.sku:
        return False
    if scope.zone is not None and zone != scope.zone:
        return False
    lots, runs, branches = set(involved), set(), {case["source_branch"]}
    if stored_input is not None:
        start = stored_input["today"]
        end = max([start, *(f["day"] for f in stored_input["forecasts"])])
        if (scope.day_to is not None and scope.day_to < start) or (scope.day_from is not None and scope.day_from > end):
            return False
        lots |= {l["lot_id"] for l in stored_input["lots"]}
        runs = {r["run_code"] for r in stored_input["runs"]}
        branches |= {r["dest"] for r in stored_input["runs"]}
    asked = [(want, have) for want, have in ((scope.lot_ids, lots), (scope.branches, branches),
                                             (scope.run_codes, runs)) if want]
    return not asked or any(want & have for want, have in asked)


def _prefilter(scope: FactScope) -> tuple[str, dict]:
    if scope.sku is not None:
        return " AND c.sku = :sku", {"sku": scope.sku}
    if scope.branches and not scope.lot_ids and not scope.run_codes:
        return (" AND (c.source_branch = ANY(:br) OR EXISTS (SELECT 1 FROM jsonb_array_elements(r.engine_input->'runs')"
                " AS x(run) WHERE x.run->>'dest' = ANY(:br)))", {"br": sorted(scope.branches)})
    return "", {}


def affected(conn, run: dict, scope: FactScope, lock: bool = True) -> tuple[list[dict], list[dict], list[dict]]:
    """(live, committed, closed): the cases in scope by state, each list in case_id order, the live ones locked FOR
    UPDATE by default (the caller holds the run FOR UPDATE). A key read uses lock=False without case row locks.
    Each item is the cases row."""
    ref = refdata.get(run["dataset_version"])
    extra, p = _prefilter(scope)
    sql = ("SELECT c.*, r.engine_input AS scope_input FROM cases c LEFT JOIN recommendations r ON r.run_id = c.run_id "
           "AND r.case_id = c.case_id AND r.rec_version = c.active_rec_version WHERE c.run_id = :r AND c.state = ANY(:s)"
           + extra + " ORDER BY c.case_id")
    live = db.rows(conn, sql + (" FOR UPDATE OF c" if lock else ""),
                   r=run["run_id"], s=list(cases.LIVE_STATES), **p)
    rest = db.rows(conn, sql.replace(" ORDER BY c.case_id", CLOSED_AS_CANCELLED + " ORDER BY c.case_id"),
                   r=run["run_id"], s=[*COMMITTED, *CLOSED], **p)
    out = {"live": [], "committed": [], "closed": []}
    for row in live + rest:
        stored = row.pop("scope_input")
        product = ref.products.get(row["sku"])
        involved = involved_lots(conn, run, row) if scope.lot_ids else [row["lot_id"]]
        if matches(scope, row, stored, involved, product.zone if product else None):
            group = "live" if row["state"] in cases.LIVE_STATES else "committed" if row["state"] in COMMITTED \
                else "closed"
            out[group].append(row)
    return out["live"], out["committed"], out["closed"]
