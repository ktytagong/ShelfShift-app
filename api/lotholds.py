"""Lot holds: an explicit, audited block on one lot (07-CONTEXT D-11, D-13; 04-API-CONTRACT "Hold"; acceptance
check 10).

A hold is always a person's explicit action, never a side effect:

- **Placing** (`place`): the lot's branch manager (the seat is checked against the lot's branch on the workspace's
  record, api/overlay.py) places a CONDITION hold ("Hold this lot for review"), or a MIXED_DATES hold by answering an
  expiry check "Mixed dates in this lot" (`MIXED_TEXT`, "Separate stock and verify before moving"). It records who
  (`placed_by`, the account) and when (`placed_at`, the database's now()). One ACTIVE hold per (lot, kind): placing
  again returns the active one.
- **Releasing** (`release`): only the lot's branch manager, with a note ("Inspect and release"): RELEASED.
- **Resolving** (`resolve_by_fact`): a printed date confirmed for the lot, checked after the hold was placed (the stock
  was separated first), resolves its MIXED_DATES hold: RESOLVED. A CONDITION hold is never resolved by a fact.

A hold is never cleared by any plan choice: no assumption, adoption, plan selection, re-plan or automatic step
changes a hold, and nothing outside this module writes `lot_holds` (D-12, D-13; T-07-26). Every change is logged in
the caller's transaction (`hold_placed`, `hold_released`, `hold_resolved`). The app never divides a lot into new lots:
staff separate the stock themselves, and a hold only says so.

Which cases a hold blocks (commit and Prepare for every case whose involved lots include it) is the commitment rule's
(07-09; 06.1's dispatch check already reads ACTIVE rows); this module lists them (`cases_on`) for the view.
"""
from __future__ import annotations

import uuid

from . import accounts, db, overlay, realtime
from .errors import ApiError

WORLD = "shelfshift"
KINDS = ("CONDITION", "MIXED_DATES")
MIXED_TEXT = "Separate stock and verify before moving"
RELEASED = "released"
RESOLVED = "printed date confirmed"


def _lot(conn, run: dict, lot_id: str) -> dict:
    """The lot's record row as the workspace reads it; 404 not_found when it is not on the record."""
    lot = overlay.lot(conn, run, WORLD, lot_id)
    if lot is None:
        raise ApiError(404, "not_found", f"No lot {lot_id}")
    return lot


def _owner(actor: dict, branch: str, what: str) -> None:
    """Only the manager of the lot's branch (from the record, never from a request body) places or releases."""
    if actor.get("role") != "manager" or actor.get("branch") != branch:
        raise ApiError(403, "forbidden", f"Only the {branch} manager can {what} a hold on this lot")


def _row(conn, run_id: str, hold_id: str, lock: bool = False) -> dict | None:
    return db.one(conn, "SELECT * FROM lot_holds WHERE run_id = :r AND hold_id = :h" + (" FOR UPDATE" if lock else ""),
                  r=run_id, h=hold_id)


def _active(conn, run_id: str, lot_id: str, kind: str) -> dict | None:
    return db.one(conn, "SELECT * FROM lot_holds WHERE run_id = :r AND lot_id = :l AND kind = :k AND state = 'ACTIVE'",
                  r=run_id, l=lot_id, k=kind)


def place(conn, run: dict, actor: dict, lot_id: str, kind: str, reason: str, signal_id: str | None = None,
          request_id: str | None = None, case_id: str | None = None) -> dict:
    """An ACTIVE hold of `kind` on the lot, placed by the lot's branch manager (else 403) with a reason (blank is 400),
    `placed_by` the account and `placed_at` now(), logged `hold_placed`; the active hold of that kind when one exists
    (no second hold, the unique index one_active_hold_per_lot_kind guards a race). Returns the view."""
    if kind not in KINDS:
        raise ApiError(400, "bad_request", f"A hold is one of {', '.join(KINDS)}")
    lot = _lot(conn, run, lot_id)
    _owner(actor, lot["branch"], "place")
    if not isinstance(reason, str) or not reason.strip():
        raise ApiError(400, "bad_request", "A reason is required")
    existing = _active(conn, run["run_id"], lot_id, kind)
    if existing is not None:
        return view(conn, run, existing)
    hold_id = f"HOLD-{uuid.uuid4().hex[:8]}"
    made = db.one(conn, "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, signal_id, "
                        "request_id, state) VALUES (:r, :h, :l, :b, :k, :why, :by, :s, :q, 'ACTIVE') "
                        "ON CONFLICT DO NOTHING RETURNING *", r=run["run_id"], h=hold_id, l=lot_id, b=lot["branch"],
                  k=kind, why=reason.strip(), by=actor.get("account") or f"manager:{actor['branch']}", s=signal_id,
                  q=request_id)
    if made is None:  # another transaction placed it first
        return view(conn, run, _active(conn, run["run_id"], lot_id, kind))
    affected = cases_on(conn, run, lot_id)
    db.log(conn, run["run_id"], run["day"], "hold_placed", case_id, hold_id=hold_id, lot_id=lot_id, kind=kind,
           reason=made["reason"], branch=lot["branch"], signal_id=signal_id, request_id=request_id,
           placed_at=realtime.iso(made["placed_at"]), cases=[c["case_id"] for c in affected],
           actor=accounts.logged_actor(actor))
    return view(conn, run, made, affected)


def release(conn, run: dict, actor: dict, hold_id: str, note: str) -> dict:
    """The lot's branch manager inspects and releases an ACTIVE hold with a note (D-13): RELEASED with resolved_by
    (the account), resolved_at (now()), resolution "released" and the note, logged `hold_released`. A hold of another
    workspace is 404; another seat 403 (checked against the lot's branch on the record, or the hold's branch when the
    lot is gone); a blank note 400; a hold not ACTIVE 409 wrong_state "Hold is <STATE>"."""
    row = _row(conn, run["run_id"], hold_id, lock=True)
    if row is None:
        raise ApiError(404, "not_found", f"No hold {hold_id}")
    lot = overlay.lot(conn, run, WORLD, row["lot_id"])
    _owner(actor, lot["branch"] if lot is not None else row["branch"], "release")
    if not isinstance(note, str) or not note.strip():
        raise ApiError(400, "bad_request", "A note is required: say what you inspected")
    if row["state"] != "ACTIVE":
        raise ApiError(409, "wrong_state", f"Hold is {row['state']}", state=row["state"])
    done = db.one(conn, "UPDATE lot_holds SET state = 'RELEASED', resolved_by = :by, resolved_at = now(), "
                        "resolution = :how, note = :n WHERE run_id = :r AND hold_id = :h RETURNING *",
                  by=actor.get("account") or f"manager:{actor['branch']}", how=RELEASED, n=note.strip(),
                  r=run["run_id"], h=hold_id)
    affected = cases_on(conn, run, row["lot_id"])
    db.log(conn, run["run_id"], run["day"], "hold_released", None, hold_id=hold_id, lot_id=row["lot_id"],
           kind=row["kind"], note=done["note"], resolved_at=realtime.iso(done["resolved_at"]),
           cases=[c["case_id"] for c in affected], actor=accounts.logged_actor(actor))
    return view(conn, run, done, affected)


def resolve_by_fact(conn, run: dict, actor: dict, lot_id: str, fact_observed_at, fact_id: str | None,
                    case_id: str | None = None) -> list[str]:
    """A printed date confirmed for the lot (D-11): every ACTIVE MIXED_DATES hold on it placed before the date's check
    time (`fact_observed_at`) becomes RESOLVED with resolved_by (the confirming account), resolved_at now() and
    resolution "printed date confirmed", each logged `hold_resolved` with the fact id. A hold placed after the check
    stays: that date was read before the stock was separated. Returns the resolved hold ids."""
    rows = db.rows(conn, "UPDATE lot_holds SET state = 'RESOLVED', resolved_by = :by, resolved_at = now(), "
                         "resolution = :how WHERE run_id = :r AND lot_id = :l AND kind = 'MIXED_DATES' "
                         "AND state = 'ACTIVE' AND placed_at < :at RETURNING *",
                   by=actor.get("account") or f"manager:{actor['branch']}", how=RESOLVED, r=run["run_id"], l=lot_id,
                   at=fact_observed_at)
    for row in sorted(rows, key=lambda h: h["hold_id"]):
        db.log(conn, run["run_id"], run["day"], "hold_resolved", case_id, hold_id=row["hold_id"], lot_id=lot_id,
               kind=row["kind"], fact_id=fact_id, observed_at=realtime.iso(fact_observed_at),
               resolved_at=realtime.iso(row["resolved_at"]), actor=accounts.logged_actor(actor))
    return sorted(r["hold_id"] for r in rows)


def active_on(conn, run_id: str, lot_ids) -> list[dict]:
    """The ACTIVE holds on any of these lots, oldest first."""
    return db.rows(conn, "SELECT * FROM lot_holds WHERE run_id = :r AND state = 'ACTIVE' AND lot_id = ANY(:l) "
                         "ORDER BY placed_at, hold_id", r=run_id, l=sorted(set(lot_ids)))


def active_in_run(conn, run_id: str) -> list[dict]:
    """Every ACTIVE hold of the run, oldest first (`active_on`'s order; the board's set-based cards filter it per case,
    api/commitment.py `holds_on_many`)."""
    return db.rows(conn, "SELECT * FROM lot_holds WHERE run_id = :r AND state = 'ACTIVE' ORDER BY placed_at, hold_id",
                   r=run_id)


def owned_by(conn, run_id: str, branch: str | None) -> list[dict]:
    """The ACTIVE holds the branch owns (its manager alone releases them), oldest first; none for HQ."""
    if branch is None:
        return []
    return db.rows(conn, "SELECT * FROM lot_holds WHERE run_id = :r AND state = 'ACTIVE' AND branch = :b "
                         "ORDER BY placed_at, hold_id", r=run_id, b=branch)


def cases_on(conn, run: dict, lot_id: str) -> list[dict]:
    """[{case_id, key, state}]: every case whose involved lots (api/scope.py involved_lots: the case lot, the lots
    moved from it, the lot it was moved from) include this lot, in case_id order."""
    candidates = {lot_id}
    lot = overlay.lot(conn, run, WORLD, lot_id)
    if lot is not None and lot["moved_from"]:
        candidates.add(lot["moved_from"])
    read, p = overlay.everywhere(conn, run, "lots")
    candidates |= {r["lot_id"] for r in db.rows(
        conn, f"SELECT lots.lot_id FROM lots WHERE {read} AND lots.world = :w AND lots.moved_from = :l",
        w=WORLD, l=lot_id, **p)}
    return db.rows(conn, "SELECT case_id, key, state FROM cases WHERE run_id = :r AND lot_id = ANY(:l) "
                         "ORDER BY case_id", r=run["run_id"], l=sorted(candidates))


def view(conn, run: dict, row: dict, affected: list[dict] | None = None) -> dict:
    """A hold in the contract's shape (04-API-CONTRACT "Hold"): times ISO-8601 UTC, `owner_branch` the lot's branch
    (whose manager alone releases it), `cases` every case involving the lot, and the labels people read (D-09):
    `placed_label` "Placed by {store} manager · {16 Oct 2026, 20:30}" and, once lifted, `resolved_label` "Hold released
    by {store} manager · {t}" or "Resolved: printed date confirmed by {store} manager · {t}"."""
    owner = row["branch"]
    resolved = None
    if row["resolved_at"] is not None:
        resolved = (f"Hold released by {owner} manager · {realtime.label(row['resolved_at'])}"
                    if row["state"] == "RELEASED" else
                    f"Resolved: printed date confirmed by {owner} manager · {realtime.label(row['resolved_at'])}")
    return {"hold_id": row["hold_id"], "lot_id": row["lot_id"], "branch": row["branch"], "kind": row["kind"],
            "reason": row["reason"], "placed_by": row["placed_by"], "placed_at": realtime.iso(row["placed_at"]),
            "owner_branch": owner, "state": row["state"], "resolved_by": row["resolved_by"],
            "resolved_at": realtime.iso(row["resolved_at"]), "resolution": row["resolution"], "note": row["note"],
            "signal_id": row["signal_id"], "request_id": row["request_id"],
            "cases": affected if affected is not None else cases_on(conn, run, row["lot_id"]),
            "placed_label": f"Placed by {owner} manager · {realtime.label(row['placed_at'])}",
            "resolved_label": resolved}
