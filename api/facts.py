"""Confirmed facts: the check-time rule, the fact row with its real-time label, and the one record correction
(07-CONTEXT D-15, D-17, D-21, D-22; research F-9, F-23, F-25; 04-API-CONTRACT "Verify path" 7 and 15).

One pipeline for request answers now and field reports later (07-13, 07-14):

- **In force** (`in_force`): for a lot and an adjustment kind (`stock_count`, `expiry`), the latest confirmed fact
  not replaced (`replaced_by` null), ordered by its check time (`observed_at`; a fact confirmed before Phase 7 has
  none and uses `confirmed_at`), with that adjustment's value.
- **Newer evidence wins** (`compare`, `check_newer`, D-22): the candidate's check time is compared with the fact in
  force for each lot, at timestamptz precision (microseconds), never with submission or confirmation times. Earlier
  is 409 `older_evidence` naming every such lot (the whole answer is refused, research A7); equal with another value
  is 409 `review_required`; equal with the same value applies and changes nothing (`check_newer` returns those lots).
  An equal time against a fact confirmed on an earlier business day is older evidence too: the same check given
  again on a later day is no new evidence for that day, so the request stays open for a new check (07 review H).
  The caller holds the run FOR UPDATE, so two confirms for one lot serialise and the second is judged against the
  first one's fact.
- **The fact** (`write`): one `reported_facts` row with its check time, branch, zone, source and the label people
  read: a count says "Count confirmed by {branch} manager · {16 Oct 2026, 20:45} · shelf a + backroom b = c"
  (`label_count`), a printed date "Printed date confirmed by {branch} manager · {t} · {Thu 15 Oct}" (`label_expiry`,
  07-08), a field report "Reported by {role} · {t} · {source}" (`label`, 07-13). The time is the real
  confirmation time (D-09); the adjustments carry business days (D-21), and nothing here moves the business day or
  the run version.
- **A correction** (`replace`, D-15, 07-13): a field report's confirmed correction marks the earlier fact replaced
  (`replaced_by` the new fact); both stay in history, and a replaced fact is never in force (`in_force`) nor in an
  input (api/inputs.py), so two corrections never stack.
- **The record** (`correct_record`): the only world-row write Phase 7 makes (research F-25). It materializes the lot's
  series first (api/overlay.py), then sets the record's qty, last_verified_day and verified_by COUNT. A printed expiry
  date is never written to the record (F-23): it stays a reported fact.
- **The effects** (`apply`, D-17): every affected live case is re-evaluated, every committed case in scope is flagged
  (no engine run decides a flag, no committed plan is rewritten); scopes come from api/scope.py. A pending approval
  the re-evaluation cancels rechecks the closed lots its released holds can affect at once (D-38.2).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from engine.adjust import AdjustmentError, InputError
from engine.simulate import HorizonError

from . import cases, db, overlay, realtime, reviewflags
from . import scope as scopes
from .errors import ApiError, engine_error

VALUE_FIELD = {"stock_count": "units", "expiry": "expiry_day"}  # the value two checks of a lot compare
OLDER = "Checked earlier than the confirmed evidence · kept, not applied"
SAME_TIME = "Same check time as the confirmed evidence, different value · review required"
ENGINE_ERRORS = (InputError, AdjustmentError, HorizonError)


def in_force(conn, run_id: str, lot_id: str, kind: str) -> dict | None:
    """{fact_id, at, value, day} of the confirmed fact in force for the lot and adjustment kind, or None: the latest
    not replaced by its check time (coalesce(observed_at, confirmed_at)), then by confirmation, then by id; `day` is
    the business day it was confirmed on."""
    row = db.one(conn, "SELECT f.fact_id, coalesce(f.observed_at, f.confirmed_at) AS at, f.confirmed_day, a.adj "
                       "FROM reported_facts f "
                       "CROSS JOIN LATERAL jsonb_array_elements(f.adjustments) AS a(adj) WHERE f.run_id = :r "
                       "AND f.replaced_by IS NULL AND a.adj->>'kind' = :k AND a.adj->>'lot_id' = :l "
                       "ORDER BY coalesce(f.observed_at, f.confirmed_at) DESC, f.confirmed_at DESC, f.fact_id DESC "
                       "LIMIT 1", r=run_id, k=kind, l=lot_id)
    if row is None:
        return None
    return {"fact_id": row["fact_id"], "at": row["at"], "value": row["adj"].get(VALUE_FIELD[kind]),
            "day": row["confirmed_day"]}


def _same(a, b) -> bool:
    """Two values of one field: numbers compare as numbers (a jsonb number may come back as Decimal)."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def compare(conn, run_id: str, kind: str, items: dict[str, tuple[datetime, object]],
            day: int | None = None) -> dict[str, list[str]]:
    """{"older": [...], "conflict": [...], "same": [...]}: the lots (in `items` order) whose check time is earlier
    than the fact in force, equal with another value, or equal with the same value. `items` maps lot_id to
    (observed_at, value). An equal time against a fact confirmed on an earlier business day than `day` (the
    workspace's) is older (07 review H). A lot with no fact in force, or a later check time, is in none of them."""
    out = {"older": [], "conflict": [], "same": []}
    for lot_id, (at, value) in items.items():
        held = in_force(conn, run_id, lot_id, kind)
        if held is None or at > held["at"]:
            continue
        if at < held["at"] or (day is not None and held["day"] < day):  # an earlier day's check is no news today
            out["older"].append(lot_id)
        elif _same(value, held["value"]):
            out["same"].append(lot_id)
        else:
            out["conflict"].append(lot_id)
    return out


def check_newer(conn, run_id: str, kind: str, items: dict[str, tuple[datetime, object]],
                day: int | None = None) -> list[str]:
    """The newer-evidence rule (D-22): 409 older_evidence when any lot was checked earlier than its fact in force (or
    at its time when that fact was confirmed on a business day before `day`, 07 review H), else 409 review_required
    when any lot has the same check time and another value, each with `lot_ids`; otherwise the lots equal in time and
    value (they change nothing)."""
    found = compare(conn, run_id, kind, items, day)
    if found["older"]:
        raise ApiError(409, "older_evidence", OLDER, lot_ids=found["older"])
    if found["conflict"]:
        raise ApiError(409, "review_required", SAME_TIME, lot_ids=found["conflict"])
    return found["same"]


def label_count(branch: str, at: datetime, parts: dict, photo: bool = False) -> str:
    """A confirmed count's label with the real confirmation time (D-09, D-21), ending " · photo attached" when the
    confirmed answer carried a file (VER-04: the photo goes with the count, never stands for it)."""
    shelf, back = parts["shelf_units"], parts["backroom_units"]
    return (f"Count confirmed by {branch} manager · {realtime.label(at)} · shelf {shelf:g} + backroom {back:g} = "
            f"{shelf + back:g}" + (" · photo attached" if photo else ""))


def label_expiry(branch: str, at: datetime, business_date: str, photo: bool = False) -> str:
    """A confirmed printed date's label (07-CONTEXT D-09, D-11, D-21): "Printed date confirmed by {branch} manager ·
    {16 Oct 2026, 20:45} · {Thu 15 Oct}", the real confirmation time and the printed business date, ending " · photo
    attached" when the confirmed answer carried a file."""
    return (f"Printed date confirmed by {branch} manager · {realtime.label(at)} · {business_date}"
            + (" · photo attached" if photo else ""))


def label(role: str, branch: str | None, at: datetime, source: str) -> str:
    """A confirmed field report's label (D-15): "Reported by {branch} {role} · {t} · {source}" (no branch for HQ)."""
    who = f"{branch} {role}" if branch else role
    return f"Reported by {who} · {realtime.label(at)} · {source}"


def write(conn, run: dict, *, kind: str, adjustments: list[dict], sku: str | None, branch: str | None,
          zone: str | None, valid_from: int, valid_to: int, label: str, confirmed_by: str, observed_at: datetime | None,
          source: str, signal_id: str) -> str:
    """One confirmed reported fact on the workspace's business day (`confirmed_day`), its confirmation time the
    database's now(); returns its fact_id."""
    fact_id = f"FACT-{uuid.uuid4().hex[:8]}"
    db.run(conn, "INSERT INTO reported_facts (run_id, fact_id, signal_id, kind, adjustments, sku, valid_from, valid_to, "
                 "label, confirmed_by, confirmed_day, observed_at, branch, zone, source) VALUES (:r, :f, :s, :k, "
                 "CAST(:a AS jsonb), :sku, :vf, :vt, :lab, :by, :d, :o, :b, :z, :src)", r=run["run_id"], f=fact_id,
           s=signal_id, k=kind, a=db.js(adjustments), sku=sku, vf=valid_from, vt=valid_to, lab=label, by=confirmed_by,
           d=run["day"], o=observed_at, b=branch, z=zone, src=source)
    return fact_id


def replace(conn, run: dict, old_fact_id: str, new_fact_id: str) -> None:
    """The earlier fact is replaced by the later one from now on (D-15): its `replaced_by` is set once, both kept. A
    fact already replaced is 409 wrong_state (correct the latest report instead); nothing is written then."""
    n = db.run(conn, "UPDATE reported_facts SET replaced_by = :new WHERE run_id = :r AND fact_id = :old "
                     "AND replaced_by IS NULL", new=new_fact_id, r=run["run_id"], old=old_fact_id)
    if not n:
        raise ApiError(409, "wrong_state", "This report was corrected already; correct the latest report")


def correct_record(conn, run: dict, lot_id: str, units: float, day: int) -> None:
    """The lot's record comes into line with a confirmed count from now on (spec §8 Corrected records): the one
    world-row write Phase 7 makes, through the workspace's own series (overlay.materialize first, research F-25)."""
    row = overlay.lot(conn, run, "shelfshift", lot_id)
    if row is None:
        raise LookupError(f"lot {lot_id} is gone")
    overlay.materialize(conn, run, "shelfshift", row["branch"], row["sku"])
    db.run(conn, "UPDATE lots SET qty = :q, last_verified_day = :d, verified_by = 'COUNT' WHERE run_id = :r "
                 "AND world = 'shelfshift' AND lot_id = :l", q=units, d=day, r=run["run_id"], l=lot_id)


def apply(conn, run: dict, fact_id: str, scope: scopes.FactScope, note: str) -> dict:
    """What a confirmed fact does, by case state (D-17), for every case in its scope (`scope.affected`, case_id order;
    the caller holds the run FOR UPDATE): each live case is re-evaluated (a new version; a pending approval is
    cancelled and its holds released, as cases.evaluate_case does), never only a capped few; each committed case gets
    one review flag with the fact id and keeps its version, state and bookings (no engine run, no rewritten plan);
    closed cases stay history, and their lots are reassessed (api/closing.py `reassess_lots`, trigger "fact": a
    successor opens only when the lot's decision-inputs fingerprint changed and it still qualifies; 07-CONTEXT D-24,
    D-28). A pending approval the new version cancelled released its holds, so the closed lots those can affect are
    rechecked at once, through the helper a close uses (`closing.recheck_neighbours`, trigger "release"; D-38.2).
    Returns {"cases": [{case_id, key, rec_version, from_version, recommended}], "flagged": [case_id, ...],
    "reassessed": [successor key, ...]}."""
    from . import closing  # it reads cases at import
    live, committed, closed = scopes.affected(conn, run, scope)
    out, cancelled = [], []
    for case in live:
        try:
            version = cases.evaluate_case(conn, run, case, note=note)
        except ENGINE_ERRORS as exc:
            raise engine_error(exc) from None
        if case["state"] == "PENDING_APPROVAL" and version != case["active_rec_version"]:
            cancelled.append(case)  # its approval round ended and its holds were released (cases.evaluate_case)
        rec = cases.active_rec(conn, cases.case_row(conn, run["run_id"], case["case_id"]))
        out.append({"case_id": case["case_id"], "key": case["key"], "rec_version": version,
                    "from_version": case["active_rec_version"],
                    "recommended": rec["result"]["recommended"] if rec else None})
    flagged = reviewflags.raise_for(conn, run, committed, fact_id)
    reassessed = closing.reassess_lots(conn, run, closed, "fact")
    for case in cancelled:  # the row as it was: its closed version's branches are where the released holds were
        reassessed += closing.recheck_neighbours(conn, run, case, "release")
    return {"cases": out, "flagged": flagged, "reassessed": reassessed}
