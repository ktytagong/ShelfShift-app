"""The impact preview of a field report: "Potential impact · unconfirmed" (07-CONTEXT D-12, D-16; spec §7 step 4;
research F-2; 04-API-CONTRACT `POST /api/signals/{id}/preview`).

Before the owner confirms a report, any seat party to it can see what it would change. **The preview changes
nothing**: no fact, recommendation in force, reservation, approval, decision, flag or hold, and it creates no block
(D-12). It is a what-if, never shown as observed.

- **Which cases** (`preview`): the live cases confirmation re-plans, found by the very matcher it uses (api/scope.py
  `affected` on `scope.of_adjustments` of the report's adjustments, `reports.adjustments_for`); for
  a correction also those of the fact it replaces, since confirmation re-plans both scopes (07-15 review P3). The first
  `reports.preview_cases` (5, a setting) by the plan in force's earliest decide-by, then case key, are re-run as
  what-ifs; undated cases sort last. `affected` counts every live case, with "Previewing 5 of N affected cases."
  The limit is the display's only: confirmation acts on every affected case (D-17).
- **The what-if** (`whatif_input`): the input the case's re-plan would read once the report is confirmed, built from
  today's data exactly as confirmation builds it (`cases.current_input`: today's bookings, this case's own left out,
  the adopted assumptions still in effect; 07-15 review P2), with the report's fact in effect after every confirmed one
  (`inputs.Unconfirmed`, by the confirmed facts' own rule `inputs.adjustments_in_effect`: the range, SKU and zone, an
  adjustment with its own days from today, the expired-date floor, the engine's validation; 07 review T1) and, for a
  correction, the fact it replaces left out (07-15 review P1); a count's record reads as its confirmation corrects it
  (facts.correct_record). Confirmation re-plans the live cases one by one (`reports._apply`: each scope's in case_id
  order), and a pending approval whose re-plan gives a new version is cancelled and its holds released before the next
  case is re-planned, so each shown case's what-if is the input of its last re-plan in that order, with the holds of
  every pending approval cancelled before it released (`whatif_inputs`; whether a version changes is read from the
  input's version label, no engine run). So the preview and the applied result agree. Counts and
  printed dates can't be overrides at all (FACT_ONLY, research F-2); capacity, demand and transport go in as facts too,
  so a case's adopted assumptions still apply after the report as they would after confirmation, and `check_plan`
  (which refuses what-if-only overrides) can say whether the plan in force stays allowed on that same input. The
  version is stored with is_whatif true (`evaluate.store(..., whatif=True)`), never made active.
- **A row** (`row`): the case, its active version and the what-if's; the plan in force now and the plan the engine
  would recommend if the report is confirmed (keys and labels), the estimated benefit of each (`delta_vs_keep`, the
  card's est_benefit), whether the plan changes, and whether the plan in force stays allowed (`check_plan` on the
  what-if input). A case whose what-if is its active version's input (same version label) keeps its version and runs
  nothing; a case without a version, one whose lot is gone, or one the engine refuses, reads null with the reason.
- **Stored per revision** (`signals.preview`), with the shared D-23 key over every affected case/version and engine
  version, and a stamp of what it read (`STAMP`, never served): the affected
  live cases, and for each shown case its state, active version, plan in force and what-if version label. A repeat
  reads the same again without the engine or case row locks and returns the stored body while both key and stamp
  are unchanged, running no engine
  and storing nothing; when any of it changed (a booking, an adopted assumption, a new version, a selection, a case
  in or out of scope) the preview is computed again and stored over it (07-15 review P4). An edit is a new revision
  and previews afresh. The route holds the run FOR UPDATE, so two previews of one revision run once (T-07-56).
- **No engine run**: a NOT_RELEVANT report reads "Not relevant · no impact preview" (10 D-11); a CONDITION report
  reads "Requires human review · engine impact not calculated" (D-13); a report
  with blank required fields reads "Complete report details to preview impact.".

It spends no cap and logs nothing: it is a reading of a possible future, not an action.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json

from engine import from_dict, rec_version, to_dict
from engine.adjust import AdjustmentError, InputError
from engine.limits import check_plan
from engine.model import ENGINE_VERSION, EngineInput
from engine.simulate import HorizonError

from . import caps, cases, config, db, evaluate, inputs, reports, selection
from . import scope as scopes
from .errors import ApiError

LABEL = "Potential impact · unconfirmed"  # D-16, word for word
INCOMPLETE = "Complete report details to preview impact."  # D-16, word for word
NO_IMPACT = "Not relevant · no impact preview"  # 10 D-11, word for word
NO_VERSION = "This case has no recommendation: data is missing"
GONE = "Lot {lot} is no longer on the record"
STAMP = "stamp"  # the stored body's record of what it read (P4); stripped from every body served
ENGINE_ERRORS = (InputError, AdjustmentError, HorizonError)


def order(conn, run: dict, case_rows: list[dict]) -> list[dict]:
    """D-08's shared display order: earliest plan-in-force decide-by, case key; undated cases last.

    Confirmation's separate re-plan order still drives `whatif_inputs` and its hold-release simulation.
    """
    def urgency(case):
        by = selection.decide_by(case, cases.active_rec(conn, case))
        return (by is None, by["day"] if by else 0, by["latest_decision_min"] if by else 0, case["key"])
    return sorted(case_rows, key=urgency)


def preview_key(signal_id: str, revision: int, pairs: list[tuple[str, str | None]], engine_version: str) -> str:
    """D-23: canonical SHA-256 over the report revision, all live affected versions and engine version."""
    value = {"signal_id": signal_id, "revision": revision, "cases": sorted(pairs), "engine_version": engine_version}
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def more_text(shown: int, affected: int) -> str | None:
    """Name the full affected count whenever the display is capped (D-08)."""
    return f"Previewing {shown} of {affected} affected cases." if affected > shown else None


def _body(row: dict, complete: bool, message: str | None, rows: list[dict], more: int, key: str) -> dict:
    return {"signal_id": row["signal_id"], "report_id": row["report_id"], "revision": row["revision"],
            "kind": row["kind"], "label": LABEL, "complete": complete, "message": message, "cases": rows,
            "affected": len(rows) + more, "preview_key": key, "more": more,
            "more_text": more_text(len(rows), len(rows) + more)}


def label_of(inp: EngineInput) -> str:
    """The version label `evaluate.run_engine` gives this raw input (normalised the same way); no engine run."""
    return rec_version(from_dict(to_dict(inp)))


def whatif_input(conn, run: dict, case: dict, unconfirmed: inputs.Unconfirmed) -> EngineInput:
    """The input the case's re-plan (cases.evaluate_case) would read once the report is confirmed: today's data with
    the report's fact in effect and a correction's replaced fact left out (`inputs.Unconfirmed`). Reads only.
    LookupError when the case lot is gone."""
    return cases.current_input(conn, run, case, unconfirmed)


def _replaced_fact(conn, run: dict, row_: dict) -> dict | None:
    """The confirmed fact a correction would replace while it is still in force (reports._correctable's rule), else
    None: a correction of a fact replaced already is refused at confirmation, so it replaces nothing here."""
    if not row_.get("corrects"):
        return None
    return db.one(conn, "SELECT f.* FROM signals s JOIN reported_facts f ON f.run_id = s.run_id "
                        "AND f.fact_id = s.fact_id WHERE s.run_id = :r AND s.signal_id = :s AND s.state = 'CONFIRMED' "
                        "AND f.replaced_by IS NULL", r=run["run_id"], s=row_["corrects"])


def unconfirmed(conn, run: dict, row_: dict) -> tuple[inputs.Unconfirmed, list[scopes.FactScope]]:
    """(the report as its confirmation would apply it, the scopes confirmation re-plans): its fact (reports.confirm,
    signals.count_apply and expiry_apply write the same adjustments, scope fields and days) and its own scope, then
    for a correction the fact it replaces and that fact's scope (reports.confirm, `reports._apply`)."""
    adjustments, sf = reports.adjustments_for(conn, run, row_)
    fact = {"adjustments": adjustments, "sku": sf["sku"], "zone": sf["zone"], "valid_from": sf["valid_from"],
            "valid_to": sf["valid_to"]}
    found = [scopes.of_adjustments(adjustments, **sf)]
    earlier = _replaced_fact(conn, run, row_)
    if earlier is not None:
        found.append(scopes.of_adjustments(earlier["adjustments"], sku=earlier["sku"], branch=earlier["branch"],
                                           zone=earlier["zone"], valid_from=earlier["valid_from"],
                                           valid_to=earlier["valid_to"]))
    counted = tuple((a["lot_id"], a["units"]) for a in adjustments if a["kind"] == "stock_count")
    return inputs.Unconfirmed(fact=fact, replaced=earlier["fact_id"] if earlier else None, counted=counted), found


def live_cases(conn, run: dict, found: list[scopes.FactScope], *, lock: bool = True) -> tuple[list[dict], list[dict]]:
    """(confirmation's re-plan order, the live cases it re-plans): each scope's live cases in case_id order, one scope
    after the other, a case in both scopes twice (`reports._apply`); then each case once, in case_id order (the
    database's collation). From api/scope.py `affected`, locked FOR UPDATE by default; lock=False reads only."""
    order: list[dict] = []
    for one in found:
        live, _, _ = scopes.affected(conn, run, one, lock=lock)
        order += live
    if len(found) == 1 or not order:
        return order, order
    seen: dict[str, dict] = {}
    for case in order:
        seen.setdefault(case["case_id"], case)
    ids = db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r AND case_id = ANY(:c) ORDER BY case_id",
                  r=run["run_id"], c=sorted(seen))
    return order, [seen[o["case_id"]] for o in ids]


def whatif_inputs(conn, run: dict, order: list[dict], shown: list[dict],
                  pending: inputs.Unconfirmed) -> dict[str, tuple[EngineInput, str] | LookupError]:
    """{case_id: (what-if input, its version label), or the LookupError of a gone lot} for each shown case: the input
    of its last re-plan in confirmation's `order` (cases.evaluate_case each in turn). A re-plan that gives a
    PENDING_APPROVAL case a new version cancels its approval and releases its current look's holds, so every case
    re-planned after it reads them released (`inputs.Unconfirmed.released`). Whether the version changes is read from
    the input's label alone: no engine run. Other cases' re-plans release nothing, so they are not built."""
    final = {case["case_id"]: i for i, case in enumerate(order)}
    shown_ids = {case["case_id"] for case in shown}
    released: list[tuple[str, int]] = []
    states: dict[str, str] = {}
    versions: dict[str, str | None] = {}
    out: dict[str, tuple[EngineInput, str] | LookupError] = {}
    for i, case in enumerate(order):
        cid = case["case_id"]
        waiting = states.get(cid, case["state"]) == "PENDING_APPROVAL"
        last = cid in shown_ids and final[cid] == i
        if not (waiting or last):
            continue
        try:
            inp = whatif_input(conn, run, case, replace(pending, released=tuple(released)))
        except LookupError as exc:  # confirmation can't re-plan it either
            if last:
                out[cid] = exc
            continue
        label = label_of(inp)
        if waiting and label != versions.get(cid, case["active_rec_version"]):  # cancelled: its holds released
            released.append((cid, case["look_round"]))
            states[cid] = "OPEN"
        versions[cid] = label
        if last:
            out[cid] = (inp, label)
    return out


def _option(result: dict, key: str | None) -> dict | None:
    return next((o for o in result["options"] if o["key"] == key), None) if result and key else None


def row(case: dict, rec: dict | None, version: str | None = None, result: dict | None = None,
        allowed: bool | None = None, error: str | None = None) -> dict:
    """One preview row: the plan in force now (the selection bound to the active version, else the recommended plan;
    api/selection.py) and the engine's recommendation if confirmed, each with its estimated benefit (delta_vs_keep),
    `changed` (the recommendation differs from the plan in force) and `allowed` (the plan in force passes check_plan
    on the what-if input). `version`/`result` None with no error: the what-if is the active version's input."""
    key, _, _ = selection.plan_in_force(case, rec)
    now = _option(rec["result"], key) if rec else None
    if rec is not None and error is None and version is None:  # unchanged: the what-if is the active version
        version, result, allowed = rec["rec_version"], rec["result"], True
    after = _option(result, result["recommended"]) if result else None
    return {"case_id": case["case_id"], "key": case["key"], "state": case["state"],
            "rec_version": case["active_rec_version"], "whatif_version": version,
            "recommended_before": key, "recommended_after": after["key"] if after else None,
            "plan_now": now["label"] if now else None, "plan_if": after["label"] if after else None,
            "benefit_now": now["delta_vs_keep"] if now else None,
            "benefit_if": after["delta_vs_keep"] if after else None,
            "changed": bool(now and after and after["key"] != now["key"]), "allowed": allowed, "error": error}


def _kept(conn, run_id: str, case_id: str, got) -> dict | None:
    """The result already stored for the case under this what-if's label: the engine is deterministic (one label, one
    result), so a repeated preview reads it back instead of running the engine again (07-20 review)."""
    if not isinstance(got, tuple):
        return None
    found = db.one(conn, "SELECT result FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                   r=run_id, c=case_id, v=got[1])
    return found["result"] if found else None


def _run_case(conn, run: dict, case: dict, rec: dict | None, got: tuple[EngineInput, str] | LookupError | None,
              kept: dict | None = None) -> dict:
    """The what-if for one case (`got`: its input and label from `whatif_inputs`): stored with is_whatif true, never
    active; check_plan on the same input for the plan in force. A what-if with the active version's label runs
    nothing, nor does one whose result is already stored (`kept`). A refusal stays on this row (the other cases still
    preview)."""
    if rec is None:
        return row(case, None, error=NO_VERSION)
    if not isinstance(got, tuple):
        return row(case, rec, error=GONE.format(lot=case["lot_id"]))
    inp, label = got
    if label == rec["rec_version"]:  # confirmation would keep this version
        return row(case, rec)
    key, _, _ = selection.plan_in_force(case, rec)
    try:
        version, result = (label, kept) if kept is not None else evaluate.store(
            conn, run["run_id"], case["case_id"], run["day"], inp, whatif=True)[:2]
        allowed = not check_plan(inp, cases.parse_plan(key))
    except ENGINE_ERRORS as exc:
        return row(case, rec, error=str(exc))
    return row(case, rec, version, result, allowed)


@dataclass
class _Reading:
    """What a preview reads before any engine run: a body needing no engine (Not relevant, CONDITION, blank fields),
    or the shown cases with their versions and what-if inputs, the count beyond them, freshness stamp and shared key.
    Reading this data takes no case row locks."""
    body: dict | None
    shown: list[dict]
    recs: dict
    got: dict
    more: int
    stamp: dict | None
    key: str


def _read(conn, run: dict, row_: dict) -> _Reading:
    key = preview_key(row_["signal_id"], row_["revision"], [], ENGINE_VERSION)
    if row_["kind"] == "NOT_RELEVANT":
        return _Reading(_body(row_, True, NO_IMPACT, [], 0, key), [], {}, {}, 0, None, key)
    if row_["kind"] == "CONDITION":  # human review only: no engine input, so no engine impact (D-13)
        return _Reading(_body(row_, True, reports.REVIEW_ONLY, [], 0, key), [], {}, {}, 0, None, key)
    f = reports.clean(run, row_["kind"], row_["fields"], inputs.sandbox_policy(conn, run))  # the bounds again
    if reports.needs_input(row_["kind"], f):
        return _Reading(_body(row_, False, INCOMPLETE, [], 0, key), [], {}, {}, 0, None, key)
    pending, found = unconfirmed(conn, run, {**row_, "fields": f})
    confirmation_order, live = live_cases(conn, run, found, lock=False)
    key = preview_key(row_["signal_id"], row_["revision"],
                      [(c["case_id"], c["active_rec_version"]) for c in live], ENGINE_VERSION)
    shown = order(conn, run, live)[:config.REPORT_PREVIEW_CASES]
    recs = {c["case_id"]: cases.active_rec(conn, c) for c in shown}
    got = whatif_inputs(conn, run, confirmation_order, shown, pending)
    stamp = {"live": [c["case_id"] for c in live],
             "cases": [[c["case_id"], c["state"], c["active_rec_version"],
                        selection.plan_in_force(c, recs[c["case_id"]])[0], _label_or_why(got.get(c["case_id"]))]
                       for c in shown]}
    return _Reading(None, shown, recs, got, len(live) - len(shown), stamp, key)


def _computed(conn, run: dict, row_: dict, reading: _Reading) -> dict:
    """The body of a reading: its own when it needs no engine, else one row per shown case. Only a what-if neither
    active nor already stored runs the engine; when any does, the `preview` cap is spent once, before the first run
    (07-20 review, 2026-10-18: the workspace's limit, then the limit across workspaces; a refusal is 429 cap_reached
    naming when it resets). So a repeated preview, or a second press while one runs (the run is held FOR UPDATE, so
    it waits and then reads the first one's results), costs no engine run and no cap."""
    if reading.body is not None:
        return reading.body
    got = {c["case_id"]: reading.got.get(c["case_id"]) for c in reading.shown}
    kept = {cid: _kept(conn, run["run_id"], cid, g) for cid, g in got.items()}
    if any(reading.recs[cid] is not None and isinstance(g, tuple) and g[1] != reading.recs[cid]["rec_version"]
           and kept[cid] is None for cid, g in got.items()):
        caps.spend(conn, reports.workspace_scope(run), "preview")
    return _body(row_, True, None, [_run_case(conn, run, c, reading.recs[c["case_id"]], got[c["case_id"]],
                                              kept[c["case_id"]]) for c in reading.shown], reading.more, reading.key)


def _label_or_why(got) -> str | None:
    """A shown case's what-if version label for the stamp, or why it has none."""
    return got[1] if isinstance(got, tuple) else None if got is None else str(got)


def _served(stored: dict) -> dict:
    """A stored body as clients see it: its stamp left out."""
    return {k: v for k, v in stored.items() if k != STAMP}


def preview(conn, run: dict, actor: dict, signal_id: str, revision) -> dict:
    """POST /api/signals/{id}/preview (D-16): any seat party to the report (as for reading it), on its current
    revision while it is READ (409 wrong_state otherwise), naming that revision (409 stale_version). The stored
    preview of the revision while both its shared key and richer stamp match (no engine run, nothing stored); else
    computed, stored with both and returned (07-15 review P4; 10 D-23). The caller holds the run FOR UPDATE to serialize
    previews; the key/freshness read itself takes no case row locks."""
    current = reports._current(conn, run["run_id"], signal_id, lock=True)
    reports._party(conn, run, actor, current)
    if current["state"] != reports.OPEN:
        raise ApiError(409, "wrong_state", f"Report is {current['state']}", state=current["state"])
    if isinstance(revision, bool) or revision != current["revision"]:
        raise ApiError(409, "stale_version", "The report changed since you looked at it", revision=current["revision"])
    reading = _read(conn, run, current)
    stored = current.get("preview")
    if stored is not None and stored.get(STAMP) == reading.stamp and stored.get("preview_key") == reading.key:
        return _served(stored)
    body = _computed(conn, run, current, reading)
    db.run(conn, "UPDATE signals SET preview = CAST(:p AS jsonb) WHERE run_id = :r AND signal_id = :s",
           p=db.js({**body, STAMP: reading.stamp}), r=run["run_id"], s=current["signal_id"])
    return body


def draft(conn, run: dict, actor: dict, kind, fields, corrects=None) -> dict:
    """POST /api/signals/preview (D-16; 07-20): the preview of a report not filed yet, so the Report an observation
    sheet shows the potential impact before Submit. The seat may file it (reports.may_file), its values are checked as
    filing checks them (`reports.clean`, the store from the seat or the run, the lot on the record there, a printed
    date's bound, a correction's target), and the same reading runs on it as on a filed revision (`_read`,
    `_run_case`). Nothing is stored for it but the what-if versions (is_whatif true): no report, no stored preview, no
    allowance, no activity entry. The caller holds the run FOR UPDATE."""
    reports.may_file(actor, kind)
    f = reports.clean(run, kind, fields, inputs.sandbox_policy(conn, run))
    branch = reports._branch(run, actor, kind, f)
    lot = reports._lot_at(conn, run, branch, f) if kind in reports.LOT_KINDS else None
    if kind == "EXPIRY" and lot is not None and f["printed_date"] not in (None, reports.MIXED):
        reports._printed_ok(conn, run, branch, f)
    if corrects is not None:
        if kind in reports.LOT_KINDS:
            raise ApiError(400, "bad_request", "A count or a printed date is corrected by a new check")
        reports._correctable(conn, run, kind, branch, corrects)
    row_ = {"signal_id": None, "report_id": None, "revision": None, "kind": kind, "fields": f, "branch": branch,
            "sku": lot["sku"] if lot is not None else f.get("sku"), "corrects": corrects}
    return _computed(conn, run, row_, _read(conn, run, row_))


def latest(conn, run_id: str, report_id: str) -> dict | None:
    """The report's latest stored preview (any revision; its `revision` tells whether it is the current one's)."""
    found = db.one(conn, "SELECT preview FROM signals WHERE run_id = :r AND report_id = :p AND preview IS NOT NULL "
                         "ORDER BY revision DESC LIMIT 1", r=run_id, p=report_id)
    return _served(found["preview"]) if found else None
