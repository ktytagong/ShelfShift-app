"""Workspaces share their template's world rows until they write a series (spec §8 Sandboxes, template and reset, as
amended in rev 6.6; 05.2-CONTEXT D-08..D-10, D-37).

A workspace (a SANDBOX run) is created and reset by copying its template's workflow rows only (cases,
recommendations, reservations, decisions, activity, signals, reported facts, requests, work orders, settings,
assistant rows). The world rows (`OVERLAID`: recorded stock, physical stock, deliveries, outcomes, world events) are
not copied: the workspace reads them through its template until it writes them.

The unit of ownership is a series, (world, branch, sku). `run_series` records the series a workspace owns. The one
read rule (`series_filter`): for each series a read touches, the workspace's own rows when it owns the series, else
its template's. The first write to a series (`materialize`: a confirmed count, a committed delivery, a released
delivery the template holds) copies that series' rows from the template into the workspace and records the ownership,
in the caller's transaction, so a failure leaves neither. Next day (`materialize_all`) first owns every series (the
'*' marker), so everything advance-day reads and writes is the workspace's own; on a public dataset it is refused
instead (D-37 B, in advance.py).

A TEMPLATE run owns everything it holds: every function here reads and writes its own rows and copies nothing.
Templates are never pruned, so a workspace's template rows outlive the workspace (a RETIRED template included).

Concurrency: ownership is claimed with INSERT ... ON CONFLICT DO NOTHING, and rows are copied only when that insert
claimed it. A second transaction claiming the same series waits on the first one's uncommitted key, then finds it
taken and copies nothing (it reads the committed copy). `materialize_all` runs only under the run lock FOR UPDATE
(advance-day), which every other workspace write excludes by holding the run FOR SHARE or FOR UPDATE.

Table and column names come from `OVERLAID` and information_schema (sandbox.columns), never from a request; every
value is a bound parameter. The `alias` of `series_filter` is checked against a plain identifier pattern.
"""
from __future__ import annotations

import re

from . import db

OVERLAID = ("lots", "truth_lots", "inbound", "outcomes", "world_events", "run_series",
            "case_flags")  # the morning screen's flags: read through the template (flags_run), never cloned
SERIES_TABLES = ("lots", "inbound", "outcomes")  # tables with (world, branch, sku) columns
EVERY = "*"  # the run_series marker of a workspace that owns every series (Next day)
_ALIAS = re.compile(r"^[a-z_][a-z0-9_]*$")


def _run(conn, run) -> dict:
    """The demo_runs row for a run_id or a run dict that lacks the kind or the template."""
    if isinstance(run, str):
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run)
    if "kind" not in run or "template_run_id" not in run:
        return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run["run_id"])
    return run


def _columns(conn, table: str) -> list[str]:
    from . import sandbox  # deferred: sandbox imports the modules that import this one
    return [c for c in sandbox.columns(conn, table) if c != "run_id"]


def base_of(run: dict) -> str | None:
    """The run a workspace reads unowned series from: its template; None for a template (it owns everything)."""
    return run["template_run_id"] if run["kind"] == "SANDBOX" else None


def owns_every(conn, run_id: str) -> bool:
    return db.one(conn, "SELECT 1 AS x FROM run_series WHERE run_id = :r AND world = :e", r=run_id, e=EVERY) is not None


def owned(conn, run, world: str, sku: str, branches) -> set[str]:
    """The branches among `branches` whose (world, branch, sku) series the run reads from its own rows."""
    run = _run(conn, run)
    branches = set(branches)
    if base_of(run) is None:
        return branches
    rows = db.rows(conn, "SELECT world, branch FROM run_series WHERE run_id = :r AND (world = :e OR (world = :w "
                         "AND sku = :s AND branch = ANY(:b)))", r=run["run_id"], e=EVERY, w=world, s=sku,
                   b=sorted(branches))
    if any(r["world"] == EVERY for r in rows):
        return branches
    return {r["branch"] for r in rows}


def series_filter(conn, run, world: str, sku: str, branches, alias: str) -> tuple[str, dict]:
    """(SQL condition, its parameters) selecting the rows of `alias` (lots, inbound or outcomes) the run reads for
    the (world, branch, sku) series of `branches`: its own for an owned series, its template's otherwise."""
    if not _ALIAS.match(alias):
        raise ValueError(f"alias {alias!r} is not a plain identifier")
    run = _run(conn, run)
    branches = sorted(set(branches))
    base = base_of(run)
    head = f"{alias}.world = :ov_w AND {alias}.sku = :ov_s AND "
    if base is None:
        return (head + f"{alias}.run_id = :ov_own AND {alias}.branch = ANY(:ov_b)",
                {"ov_w": world, "ov_s": sku, "ov_own": run["run_id"], "ov_b": branches})
    mine = owned(conn, run, world, sku, branches)
    return (head + f"(({alias}.run_id = :ov_own AND {alias}.branch = ANY(:ov_mine)) OR "
                   f"({alias}.run_id = :ov_base AND {alias}.branch = ANY(:ov_theirs)))",
            {"ov_w": world, "ov_s": sku, "ov_own": run["run_id"], "ov_base": base,
             "ov_mine": sorted(mine), "ov_theirs": [b for b in branches if b not in mine]})


def lot(conn, run, world: str, lot_id: str) -> dict | None:
    """The lot's record row as the run reads it (the one read rule): its own row while its series is owned, else the
    template's while the series is not owned (an owned series without the row means the lot is gone in this
    workspace). None when there is none."""
    run = _run(conn, run)
    q = "SELECT * FROM lots WHERE run_id = :r AND world = :w AND lot_id = :l"
    row = db.one(conn, q, r=run["run_id"], w=world, l=lot_id)
    base = base_of(run)
    if base is None:
        return row
    if row is not None and owned(conn, run, world, row["sku"], [row["branch"]]):
        return row
    row = db.one(conn, q, r=base, w=world, l=lot_id)
    if row is None or owned(conn, run, world, row["sku"], [row["branch"]]):
        return None
    return row


def lot_rows(conn, run, world: str, lot_ids) -> dict[str, dict | None]:
    """`lot`'s read rule applied to many lots at once: {lot_id: the record row the run reads, or None}, in three reads
    at most (the run's own rows, its template's rows, and the run_series claims of the products involved) instead of
    one to three per lot. Per id: the own row while its series is owned, else the template's row while that series
    is not owned, else None; a template reads its own rows. `lot` stays the single-lot path the build uses;
    tests/api/test_overlay.py `test_lot_rows_matches_lot` pins the equality."""
    run = _run(conn, run)
    ids = sorted(set(lot_ids))
    if not ids:
        return {}
    q = "SELECT * FROM lots WHERE run_id = :r AND world = :w AND lot_id = ANY(:l)"
    own = {r["lot_id"]: r for r in db.rows(conn, q, r=run["run_id"], w=world, l=ids)}
    base = base_of(run)
    if base is None:
        return {i: own.get(i) for i in ids}
    theirs = {r["lot_id"]: r for r in db.rows(conn, q, r=base, w=world, l=ids)}
    skus = sorted({r["sku"] for r in (*own.values(), *theirs.values())})
    claims = db.rows(conn, "SELECT world, branch, sku FROM run_series WHERE run_id = :r AND (world = :e OR "
                           "(world = :w AND sku = ANY(:s)))", r=run["run_id"], e=EVERY, w=world, s=skus) if skus else []
    every = any(c["world"] == EVERY for c in claims)
    mine = {(c["branch"], c["sku"]) for c in claims if c["world"] == world}

    def owns(row: dict) -> bool:
        return every or (row["branch"], row["sku"]) in mine
    out = {}
    for i in ids:
        row = own.get(i)
        if row is not None and owns(row):
            out[i] = row
            continue
        row = theirs.get(i)
        out[i] = None if row is None or owns(row) else row
    return out


def lots(conn, run, world: str, lot_ids, branch: str | None = None) -> list[dict]:
    """`lot` for several lots (those the run reads, optionally only at `branch`), in lot_id order."""
    out = [r for r in (lot(conn, run, world, l) for l in sorted(set(lot_ids))) if r is not None]
    return [r for r in out if branch is None or r["branch"] == branch]


def physical(conn, run, world: str, lot_ids) -> dict[str, float]:
    """{lot_id: physical qty} from truth_lots of each lot's source run, the run `lot` reads its record row from (its
    own or its template). Read only by evaluation drivers and advance-day, never a route (FLOW-08)."""
    run = _run(conn, run)
    sources: dict[str, list[str]] = {}
    for l in sorted(set(lot_ids)):
        row = lot(conn, run, world, l)
        if row is not None:
            sources.setdefault(row["run_id"], []).append(l)
    return {t["lot_id"]: float(t["qty"]) for src, ids in sorted(sources.items()) for t in db.rows(
        conn, "SELECT lot_id, qty FROM truth_lots WHERE run_id = :r AND world = :w AND lot_id = ANY(:l)",
        r=src, w=world, l=ids)}


def materialize(conn, run, world: str, branch: str, sku: str) -> bool:
    """Before a workspace writes the (world, branch, sku) series: claim it and, only when this call claimed it, copy
    its template rows (lots, their truth_lots, inbound, outcomes) with run_id replaced. A no-op for a template, an
    owned series or a workspace that owns every series. Returns True when it copied."""
    run = _run(conn, run)
    base = base_of(run)
    if base is None or owns_every(conn, run["run_id"]):
        return False
    claimed = db.run(conn, "INSERT INTO run_series (run_id, world, branch, sku) VALUES (:r, :w, :b, :s) "
                           "ON CONFLICT DO NOTHING", r=run["run_id"], w=world, b=branch, s=sku)
    if not claimed:
        return False
    own, keys = run["run_id"], {"base": base, "w": world, "b": branch, "s": sku}
    series = "run_id = :base AND world = :w AND branch = :b AND sku = :s"
    for table in SERIES_TABLES:
        names = ", ".join(_columns(conn, table))
        db.run(conn, f"INSERT INTO {table} (run_id, {names}) SELECT :own, {names} FROM {table} WHERE {series}",
               own=own, **keys)
    names = _columns(conn, "truth_lots")
    db.run(conn, f"INSERT INTO truth_lots (run_id, {', '.join(names)}) SELECT :own, {', '.join('t.' + c for c in names)} "
                 f"FROM truth_lots t WHERE t.run_id = :base AND t.world = :w "
                 f"AND t.lot_id IN (SELECT lot_id FROM lots WHERE {series})", own=own, **keys)
    return True


def materialize_deliveries(conn, run, case_id: str) -> None:
    """Before a workspace deletes a case's pending delivery: materialize every shelfshift series in which its
    template holds a delivery of that case, so the delete reaches the row the workspace reads."""
    run = _run(conn, run)
    base = base_of(run)
    if base is None or owns_every(conn, run["run_id"]):
        return
    for s in db.rows(conn, "SELECT DISTINCT branch, sku FROM inbound WHERE run_id = :r AND world = 'shelfshift' "
                           "AND source_case_id = :c ORDER BY branch, sku", r=base, c=case_id):
        materialize(conn, run, "shelfshift", s["branch"], s["sku"])


def materialize_all(conn, run) -> int:
    """Before Next day in a workspace: copy every template row of every series it does not own yet (and the world
    events, which no other write touches), then record the '*' marker. Runs once per workspace; a no-op for a
    template. The caller holds the run FOR UPDATE. Returns the number of rows copied."""
    run = _run(conn, run)
    base = base_of(run)
    if base is None or owns_every(conn, run["run_id"]):
        return 0
    own, copied = run["run_id"], 0
    not_owned = ("NOT EXISTS (SELECT 1 FROM run_series o WHERE o.run_id = :own AND o.world = x.world "
                 "AND o.branch = x.branch AND o.sku = x.sku)")
    for table in SERIES_TABLES:
        names = _columns(conn, table)
        copied += db.run(conn, f"INSERT INTO {table} (run_id, {', '.join(names)}) SELECT :own, "
                               f"{', '.join('x.' + c for c in names)} FROM {table} x WHERE x.run_id = :base "
                               f"AND {not_owned}", own=own, base=base)
    names = _columns(conn, "truth_lots")
    copied += db.run(conn, f"INSERT INTO truth_lots (run_id, {', '.join(names)}) SELECT :own, "
                           f"{', '.join('t.' + c for c in names)} FROM truth_lots t JOIN lots x ON x.run_id = t.run_id "
                           f"AND x.world = t.world AND x.lot_id = t.lot_id WHERE t.run_id = :base AND {not_owned}",
                     own=own, base=base)
    names = _columns(conn, "world_events")
    copied += db.run(conn, f"INSERT INTO world_events (run_id, {', '.join(names)}) SELECT :own, {', '.join(names)} "
                           f"FROM world_events WHERE run_id = :base", own=own, base=base)
    db.run(conn, "INSERT INTO run_series (run_id, world, branch, sku) VALUES (:r, :e, :e, :e) ON CONFLICT DO NOTHING",
           r=own, e=EVERY)
    return copied


def world_events_run(conn, run) -> str:
    """The run whose world_events a run reads: its own once it owns every series (only Next day writes events),
    else its template's."""
    run = _run(conn, run)
    base = base_of(run)
    return run["run_id"] if base is None or owns_every(conn, run["run_id"]) else base


def flags_run(conn, run) -> str:
    """The run whose case_flags a run reads (05.2 D-23, api/flags.py): a workspace reads its template's flags while
    its day is the template's day (the morning the template screened), and its own once its day differs; a template
    reads its own."""
    run = _run(conn, run)
    base = base_of(run)
    if base is None:
        return run["run_id"]
    template = db.one(conn, "SELECT day FROM demo_runs WHERE run_id = :r", r=base)
    return base if template is not None and template["day"] == run["day"] else run["run_id"]


def everywhere(conn, run, alias: str) -> tuple[str, dict]:
    """(SQL condition, parameters) selecting every row of `alias` (lots, inbound or outcomes) the run reads, across
    all series: its own rows of owned series and its template's rows of the others."""
    if not _ALIAS.match(alias):
        raise ValueError(f"alias {alias!r} is not a plain identifier")
    run = _run(conn, run)
    base = base_of(run)
    if base is None or owns_every(conn, run["run_id"]):
        return f"{alias}.run_id = :ov_own", {"ov_own": run["run_id"]}
    claim = (f"EXISTS (SELECT 1 FROM run_series ov WHERE ov.run_id = :ov_own AND ov.world = {alias}.world "
             f"AND ov.branch = {alias}.branch AND ov.sku = {alias}.sku)")
    return (f"(({alias}.run_id = :ov_own AND {claim}) OR ({alias}.run_id = :ov_base AND NOT {claim}))",
            {"ov_own": run["run_id"], "ov_base": base})
