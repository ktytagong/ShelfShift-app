"""Field reports filed by hand: file, confirm by the owning role (07-CONTEXT D-14, D-15, D-16, D-17, D-20; spec §7
Field signals; 04-API-CONTRACT "Field reports" rows and the Report shape).

A report is a `signals` row with `report_id` set (the first revision's signal_id); an answer to a request has none.
**Nothing applies before the owner confirms**: filing changes no fact, recommendation, booking, approval or hold, and
blocks nothing (D-16); nothing is accepted automatically, and no model reads a report in Phase 7 (Phase 10 only
suggests the same fields, D-01).

- **Kinds** (`KINDS`): each kind's typed fields, the fields it requires and its owning role. Values are checked
  against the engine's own bounds (`engine.adjust.KINDS`; money against the workspace Policy's
  `adjustment_cost_max`), 400 out_of_bounds naming the field; dates are business dates (`cases.day_of`, D-21). A
  blank required field is allowed and listed in `needs_input`; confirming it is 409 needs_input.
- **Who files** (`create`): a store's manager for their own store, the store coming only from the session seat (a
  body naming a branch is refused; a transport report only for a run leaving that store); HQ files transport only.
  The report records the live cases in its scope when filed (`affects`, api/scope.py), for the New report badge.
- **Who confirms** (`confirm`, `owner`): the owner only, the store's manager for its store facts and HQ for
  transport; a reporter who owns the fact may confirm their own report (D-15). Confirming checks the values again,
  writes one reported fact with its zone, validity business days and the label "Reported by {store} manager · {t} ·
  field report" ({t} the real confirmation time, D-09), and applies it through the confirmation pipeline
  (`facts.apply`, D-17): every live case in scope gets a new version (a pending approval is cancelled), every
  committed case in scope a review flag, closed lots in scope are reassessed.
- **The mapping** (`adjustments_for`, spec §7; research F-9): CAPACITY is the engine's `capacity` for the store and
  its zone (the fact's `zone`, so a chilled report never reaches an ambient input, api/inputs.py); DEMAND_EVENT a
  `demand_multiplier` at the store (every product, or one SKU); TRANSPORT on the run it names, for the day it applies
  on: a PER_TRIP quote its `transport_quote` (with "includes handling: yes", a zero `handling_cost` too), a PER_UNIT
  quote its `handling_cost`, RUN_NOT_AVAILABLE `run_available` false, DELAY `departure_delay_h`. A quote needs its
  pricing basis and handling chosen (UNKNOWN is listed in needs_input), and an expired quote (`valid_until` before
  the business day) is 409 quote_expired.
- **Counts and printed dates** (07-14; D-11, D-22): STOCK_COUNT `{lot_id, shelf_units, backroom_units}` and EXPIRY
  `{lot_id, printed_date}` are about one lot on the record at the reporter's store and state "When did you check?"
  (`observed`, no default). The lot's manager confirms them under the request answers' rules, through the very steps
  an answer's confirmation takes (api/signals.py `count_newer`, `count_apply`, `expiry_newer`, `expiry_apply`): the
  tick for a count, the applicability check, the newer-evidence and equal-time rules on check times, the record
  correction for a count, and an expiry fact without a record rewrite. "MIXED" places the audited MIXED_DATES hold
  when the report is filed (lotholds.place) and leaves nothing to confirm.
- **Condition observations** (07-14; D-13): CONDITION `{lot_id, condition: DAMAGE | SAFETY | DISPLAY, hold?}` is for
  human review only: it feeds no engine input and shows no calculated impact ("Requires human review · engine
  impact not calculated"); its confirmation writes no fact. `hold: true` on a damage or possible-safety report about
  the reporter's own lot is "Hold this lot for review": a separate, logged CONDITION hold placed through
  lotholds.place when the report is filed, before any confirmation. A confirmed display issue is an advisory task
  under its branch's Tasks (`advisory`), closed by Mark done (`mark_done`), which never touches a hold.
- **Edits and files** (07-14; D-14, D-15, D-20): before confirmation the reporter or the owner edits a report
  (`patch`): each edit is a new revision of the same report, the earlier one REPLACED and kept, and confirm names
  the revision it confirms (409 stale_version otherwise); an action naming an earlier revision's id acts on the
  report's current one (`_current`). A report carries one file (PNG, JPEG or PDF by content): its first file spends
  one of the workspace's files (`caps.workspace.upload`) and the cross-workspace upload count, a replacement only the
  cross-workspace count (`_spend_file`).
- **Withdraw, dismiss, correct** (D-15): only the reporter withdraws, only before confirmation (WITHDRAWN, no
  operational effect); the owner dismisses with an optional reason (DISMISSED, `dismissed_by`, `dismissed_at`);
  neither applies anything and both keep the history. After confirmation a correction is a new report naming the
  confirmed one (`corrects`): its confirmed fact replaces the earlier fact from then on (`facts.replace`), both kept,
  and the cases in either fact's scope are re-planned, so two facts never stack.
- **The allowance** (`allowance`, D-20): the workspace's own counts (scope `run:<run_id>`) against
  `caps.workspace`; never the cross-workspace upload cap's number.

Every step logs its activity entry in the caller's transaction (`report_filed`, `report_confirmed`), and every query
is keyed by run_id, so two workspaces never see each other's reports.
"""
from __future__ import annotations

import hashlib
import math
import uuid
from datetime import timedelta

from engine import adjust

from . import accounts, bookings, caps, cases, config, db, facts, files, inputs, lotholds, movement, overlay, realtime
from . import refdata
from . import signals  # the count and printed-date steps an answer and a report share (07-14; D-11, D-22)
from . import scope as scopes
from .errors import ApiError

SOURCE = "field report"  # the label's source for a report a person filed (D-15)
SIMULATED_SOURCE = "Simulated report"  # a preloaded report's (D-18, 07-16)
OPEN = "READ"  # filed, not confirmed (spec §7 signal states)
STATES = ("READ", "CONFIRMED", "DISMISSED", "WITHDRAWN", "REPLACED")  # a report's states (04-API-CONTRACT Report)
FORBIDDEN = "You can't open this report"
COMPLETE_FIRST = "Complete the report before confirming"
SEAT_ONLY = "The store comes from your seat; leave branch out of the report"

CHOOSE = "Choose the pricing basis and whether handling is included"
WHATS = ("QUOTE", "RUN_NOT_AVAILABLE", "DELAY")  # what a transport report says about the run (spec §7)
BASES = ("PER_TRIP", "PER_UNIT")
HANDLING = ("YES", "NO", "UNKNOWN")  # UNKNOWN is a value a reading may give; the person must choose before confirming
UNKNOWN = "UNKNOWN"
MIXED = signals.MIXED  # "Mixed dates in this lot": placed as a lot hold, never a date to confirm (D-11)
WORLD = "shelfshift"
CHECKED = ("STOCK_COUNT", "EXPIRY")  # the kinds that state "When did you check?" (D-22) and follow the answers' rules
LOT_KINDS = (*CHECKED, "CONDITION")  # the kinds about one lot at the reporter's store (07-14)
CONDITIONS = ("DAMAGE", "SAFETY", "DISPLAY")  # what a person saw on the shelf (D-13)
CONDITION_LABEL = {"DAMAGE": "Damage", "SAFETY": "Possible safety issue", "DISPLAY": "Display issue"}
HOLDABLE = ("DAMAGE", "SAFETY")  # "Hold this lot for review" is offered on these only; a display issue is a task
NOT_RELEVANT_LABEL = "Not relevant · no impact preview"
REVIEW_ONLY = "Requires human review · engine impact not calculated"  # a condition's impact label (D-13)
TICK = "Tick 'I checked the shelf and the backroom for this lot'"
# kind -> its owner role and its typed fields: name -> (type, the engine adjustment field that bounds a number, or the
# allowed values of a choice, or None)
KINDS: dict[str, dict] = {
    "CAPACITY": {"owner": "manager",
                 "fields": {"zone": ("zone", None), "free_totes": ("int", ("capacity", "free_totes")),
                            "valid_from": ("date", None), "valid_to": ("date", None)}},
    "DEMAND_EVENT": {"owner": "manager",
                     "fields": {"sku": ("sku", None), "multiplier": ("float", ("demand_multiplier", "value")),
                                "valid_from": ("date", None), "valid_to": ("date", None)}},
    "TRANSPORT": {"owner": "hq",
                  "fields": {"run_code": ("run", None), "what": ("choice", WHATS),
                             "amount": ("float", ("transport_quote", "fixed_cost")),
                             "pricing_basis": ("choice", BASES), "includes_handling": ("choice", HANDLING),
                             "applies_on": ("date", None), "valid_until": ("date", None),
                             "delay_hours": ("float", ("departure_delay_h", "hours"))}},
    # 07-14: a count and a printed date about one lot at the reporter's store (D-11, D-14, D-22)
    "STOCK_COUNT": {"owner": "manager",
                    "fields": {"lot_id": ("lot", None), "shelf_units": ("float", ("stock_count", "units")),
                               "backroom_units": ("float", ("stock_count", "units"))}},
    "EXPIRY": {"owner": "manager", "fields": {"lot_id": ("lot", None), "printed_date": ("printed", None)}},
    # 07-14: a person's observation for human review only, never an engine input (D-13)
    "CONDITION": {"owner": "manager", "fields": {"lot_id": ("lot", None), "condition": ("choice", CONDITIONS),
                                                 "hold": ("bool", None)}},
    "NOT_RELEVANT": {"owner": "reporter", "fields": {}, "reading_only": True},
}
MANUAL_KINDS = tuple(kind for kind, spec in KINDS.items() if not spec.get("reading_only"))
OPTIONAL = {"DEMAND_EVENT": {"sku"}, "CONDITION": {"hold"}}  # DEMAND_EVENT blank: every product at the store
# what a transport report needs besides run_code, what and applies_on; any other filled field is refused
TRANSPORT_NEEDS = {"QUOTE": ("amount", "pricing_basis", "includes_handling", "valid_until"),
                   "RUN_NOT_AVAILABLE": (), "DELAY": ("delay_hours",)}


FIELD_SOURCES = ("typed", "gemini_accepted", "gemini_edited", "cleared")


def _reading_view(conn, run_id: str, row: dict) -> dict | None:
    if not row.get("reading_id"):
        return None
    saved = db.one(conn, "SELECT reading_id, provenance, model_id, finished_at, created_at, note, kind, suggestions "
                   "FROM readings WHERE run_id=:r AND reading_id=:id", r=run_id, id=row["reading_id"])
    if saved is None:
        return None
    return {"reading_id": saved["reading_id"], "provenance": saved["provenance"], "model_id": saved["model_id"],
            "recorded_at": realtime.iso(saved["finished_at"] or saved["created_at"]), "note": saved["note"],
            "kind": saved["kind"],
            "suggestions": [{name: suggestion[name] for name in ("field", "value", "source", "excerpt", "state")}
                            for suggestion in saved["suggestions"]]}


def _field_origins(conn, run: dict, actor: dict, kind: str, f: dict, text: str, reading_id, field_sources,
                   case_id=None, file: bytes | None = None) -> dict:
    """Verify client claims before any file/report write (T-10-35, T-10-37)."""
    from agent import reading, readings_job

    names = ("kind", *KINDS[kind]["fields"])
    if field_sources is not None and (not isinstance(field_sources, dict)
            or any(not isinstance(name, str) for name in field_sources)):
        raise ApiError(400, "bad_request", "field_sources must be a JSON object of field origins")
    sources = {name: "typed" for name in names}
    for name, source in (field_sources or {}).items():
        if name not in names or source not in FIELD_SOURCES:
            raise ApiError(400, "bad_request", "Choose a supported origin for each field", field=name)
        sources[name] = source
    saved = None
    if reading_id is not None:
        if not isinstance(reading_id, str) or not reading_id or len(reading_id) > 128:
            raise ApiError(400, "bad_request", "reading_id must name the report's reading")
        saved = db.one(conn, "SELECT * FROM readings WHERE run_id=:r AND reading_id=:id",
                       r=run["run_id"], id=reading_id)
        if (saved is None or saved["status"] != "DONE" or saved["purpose"] != "REPORT"
                or saved["signal_id"] is not None or saved["signal_revision"] is not None
                or saved["request_id"] is not None or saved["after_confirmation"]):
            raise ApiError(400, "bad_request", "Use a completed reading for this report")
        ctx = readings_job.context(conn, run, actor, case_id=case_id, purpose="report")
        expected = reading.reuse_key(ctx, text, hashlib.sha256(file).hexdigest() if file else None)
        if saved["input_key"] != expected:
            raise ApiError(400, "bad_request", "The reading does not match this report's evidence and context")
    if kind == "NOT_RELEVANT" and (saved is None or saved["kind"] != "NOT_RELEVANT"):
        raise ApiError(400, "bad_request", "Not relevant requires a completed reading of that kind")
    if kind == "CONDITION" and any(source in ("gemini_accepted", "gemini_edited")
                                    for source in sources.values()):
        raise ApiError(400, "bad_request", "Condition reports must be entered by hand")
    suggested = {row["field"]: row["value"] for row in saved["suggestions"]
                 if row["value"] is not None and row["state"] in ("suggested", "calculated")} if saved else {}
    if saved and saved["kind"] == "NOT_RELEVANT":
        suggested["kind"] = saved["kind"]
    values = {"kind": kind, **f}
    for name, source in sources.items():
        value = values[name]
        if source == "cleared" and value is not None:
            raise ApiError(400, "bad_request", "A cleared field must be blank", field=name)
        if source in ("gemini_accepted", "gemini_edited"):
            if name not in suggested:
                raise ApiError(400, "bad_request", "There is no stored suggestion for this field", field=name)
            equal = value == suggested[name]
            if source == "gemini_accepted" and not equal:
                raise ApiError(400, "bad_request", "The accepted value differs from the stored reading.", field=name)
            if source == "gemini_edited" and equal:
                raise ApiError(400, "bad_request", "An edited value must differ from the stored reading.", field=name)
    return sources


def _required(kind: str, fields: dict) -> list[str]:
    """The fields the kind needs before it can be confirmed, given what is filled (a transport report by `what`)."""
    if kind == "TRANSPORT":
        return ["run_code", "what", "applies_on", *TRANSPORT_NEEDS.get(fields.get("what"), ())]
    return [f for f in KINDS[kind]["fields"] if f not in OPTIONAL.get(kind, ())]


def needs_input(kind: str, fields: dict) -> list[dict]:
    """[{"field"}]: the required fields still blank, a quote's handling left UNKNOWN (spec §7: chosen first), and a
    printed date given as MIXED (a hold, never a date: there is nothing to confirm, D-11)."""
    return [{"field": f} for f in _required(kind, fields)
            if fields.get(f) is None or fields.get(f) == UNKNOWN or (f == "printed_date" and fields.get(f) == MIXED)]


def owner(row: dict) -> dict:
    """Who confirms: {"role": "manager", "branch": the report's store} or {"role": "hq", "branch": None}."""
    if row["kind"] == "NOT_RELEVANT":
        return {"role": "hq", "branch": None} if row["reporter_role"] == "hq" else {
            "role": "manager", "branch": row["reporter_branch"]}
    if KINDS[row["kind"]]["owner"] == "hq":
        return {"role": "hq", "branch": None}
    return {"role": "manager", "branch": row["branch"]}


def _owns(actor: dict, row: dict) -> bool:
    want = owner(row)
    return actor.get("role") == want["role"] and (want["role"] == "hq" or actor.get("branch") == want["branch"])


def _account(actor: dict) -> str:
    return actor.get("account") or ("HQ" if actor.get("role") == "hq" else actor.get("branch"))


# --- values ----------------------------------------------------------------------------------------------------------

def _num(x) -> str:
    return f"{x:g}" if isinstance(x, float) else str(x)


def _bound(adj_field: tuple[str, str], policy) -> tuple[float, float]:
    """The engine's bound of an adjustment field (engine.adjust.KINDS), money read from the workspace Policy."""
    _, bound = adjust.KINDS[adj_field[0]][adj_field[1]]
    if bound == adjust.MONEY:
        return (0.0, float(policy.adjustment_cost_max))
    return bound


def _number(name: str, value, typ: str, bound: tuple[float, float]):
    """A whole number or a number inside the engine's bound, else 400 out_of_bounds naming the field and bound. A
    JSON integer past the float range (math.isfinite and float() raise OverflowError on it) is out of bounds too,
    never a 500 (07-13 review T3)."""
    lo, hi = bound
    whole = typ == "int"
    try:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if ok and whole:
            ok = float(value).is_integer()
    except OverflowError:
        ok = False
    if not ok or not lo <= value <= hi:
        kind = "a whole number between" if whole else "between"
        raise ApiError(400, "out_of_bounds", f"{name} must be {kind} {_num(lo)} and {_num(hi)}", field=name,
                       bound={"min": lo, "max": hi})
    return int(value) if whole else float(value)


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def clean(run: dict, kind: str, fields, policy) -> dict:
    """The kind's typed fields, checked: every key of the kind present (a blank one None), unknown keys 400 (a body
    naming a branch is refused: the store comes from the seat), numbers inside the engine's bounds (400
    out_of_bounds), dates real business dates "YYYY-MM-DD" (400) with the range not ending before the business day."""
    if kind not in KINDS:
        raise ApiError(400, "bad_request", f"kind must be one of {', '.join(MANUAL_KINDS)}")
    if not isinstance(fields, dict):
        raise ApiError(400, "bad_request", "fields must be a JSON object")
    spec = KINDS[kind]["fields"]
    if "branch" in fields:
        raise ApiError(400, "bad_request", SEAT_ONLY)
    unknown = sorted(set(fields) - set(spec))
    if unknown:
        raise ApiError(400, "bad_request", f"{unknown[0]} is not a field of a {kind} report")
    ref = refdata.get(run["dataset_version"])
    out = {}
    for name, (typ, adj_field) in spec.items():
        value = fields.get(name)
        if _blank(value):
            out[name] = None
        elif typ in ("int", "float"):
            out[name] = _number(name, value, typ, _bound(adj_field, policy))
        elif typ == "date":
            cases.day_of(run, value)  # 400 unless a real business date
            out[name] = value
        elif typ in ("zone", "choice"):
            allowed = sorted({p.zone for p in ref.products.values()}) if typ == "zone" else list(adj_field)
            if value not in allowed:
                raise ApiError(400, "out_of_bounds", f"{name} must be one of {', '.join(allowed)}", field=name,
                               bound={"allowed": allowed})
            out[name] = value
        elif typ == "sku":
            if not isinstance(value, str) or value not in ref.products:
                raise ApiError(400, "bad_request", f"No product {value}", field=name)
            out[name] = value
        elif typ == "run":
            if not isinstance(value, str) or len(value) > 64:
                raise ApiError(400, "bad_request", "run_code must be a run code", field=name)
            out[name] = value
        elif typ == "lot":  # on the record at the reporter's store: `_lot_at`, which needs the database
            if not isinstance(value, str) or len(value) > 64:
                raise ApiError(400, "bad_request", "lot_id must be a lot code", field=name)
            out[name] = value
        elif typ == "printed":  # a business date in the dataset calendar, or "Mixed dates in this lot" (D-11)
            if value != MIXED:
                cases.day_of(run, value)  # 400 unless a real business date
            out[name] = value
        elif typ == "bool":
            if not isinstance(value, bool):
                raise ApiError(400, "bad_request", f"{name} must be true or false", field=name)
            out[name] = value
        else:  # pragma: no cover - a KINDS entry with an unknown type is a programming error
            raise ValueError(typ)
    if kind == "TRANSPORT" and out["what"] is not None:  # a field another kind of transport report has is refused
        extra = [n for n in ("amount", "pricing_basis", "includes_handling", "valid_until", "delay_hours")
                 if out[n] is not None and n not in TRANSPORT_NEEDS[out["what"]]]
        if extra:
            raise ApiError(400, "bad_request", f"{extra[0]} is not a field of a {out['what']} report", field=extra[0])
    if kind == "STOCK_COUNT" and out["shelf_units"] is not None and out["backroom_units"] is not None:
        hi = adjust.UNITS[1]  # a lot's count is at most the engine's bound, as an answer's (signals._clean_counts)
        if out["shelf_units"] + out["backroom_units"] > hi:
            raise ApiError(400, "out_of_bounds", f"A lot's count is at most {hi:,.0f} units", field="backroom_units",
                           bound={"min": 0.0, "max": hi})
    if kind == "CONDITION" and out["hold"] and out["condition"] not in (None, *HOLDABLE):
        raise ApiError(400, "bad_request", "Only a damage or possible safety report holds the lot; a display issue "
                                           "becomes an advisory task", field="hold")
    _check_range(run, out)
    return out


def _check_range(run: dict, f: dict) -> None:
    """valid_from on or before valid_to, a quote's valid_until on or after the day it applies on (07-13 review T2: a
    quote that ends before its run's day never applies), and the range (or a transport report's day) not ending before
    the business day (a report about the past applies to nothing; spec §7 step 3)."""
    start, end = f.get("valid_from"), f.get("valid_to")
    if start is not None and end is not None and cases.day_of(run, start) > cases.day_of(run, end):
        raise ApiError(400, "bad_request", "valid_from must be on or before valid_to", field="valid_from")
    on, until = f.get("applies_on"), f.get("valid_until")
    if on is not None and until is not None and cases.day_of(run, until) < cases.day_of(run, on):
        raise ApiError(400, "bad_request", "valid_until must be on or after applies_on", field="valid_until")
    for name in ("valid_to", "applies_on"):
        if f.get(name) is not None and cases.day_of(run, f[name]) < run["day"]:
            today = cases.iso_of(run, run["day"])
            raise ApiError(400, "out_of_bounds", f"{name} must be on or after {today}", field=name,
                           bound={"min": today})


# --- the mapping and the scope ----------------------------------------------------------------------------------------

def _days(run: dict, f: dict, start: str, end: str) -> tuple[int | None, int | None]:
    return (cases.day_of(run, f[start]) if f.get(start) else None, cases.day_of(run, f[end]) if f.get(end) else None)


def scope_of(run: dict, row: dict) -> scopes.FactScope | None:
    """The report's scope from what is filled (the same matcher confirmation uses, api/scope.py), or None when it
    names nothing to match yet."""
    f, kind = row["fields"], row["kind"]
    if kind == "CAPACITY":
        lo, hi = _days(run, f, "valid_from", "valid_to")
        return scopes.FactScope(branches=frozenset({row["branch"]}), zone=f.get("zone"), day_from=lo, day_to=hi)
    if kind == "DEMAND_EVENT":
        lo, hi = _days(run, f, "valid_from", "valid_to")
        return scopes.FactScope(branches=frozenset({row["branch"]}), sku=f.get("sku"), day_from=lo, day_to=hi)
    if kind == "TRANSPORT" and f.get("run_code"):
        on, _ = _days(run, f, "applies_on", "applies_on")
        return scopes.FactScope(run_codes=frozenset({f["run_code"]}), day_from=on, day_to=on)
    if kind in LOT_KINDS and f.get("lot_id"):  # the lot, as a count's fact scope: every case involving it (D-17)
        return scopes.FactScope(lot_ids=frozenset({f["lot_id"]}), sku=row.get("sku"), day_from=run["day"],
                                day_to=run["day"])
    return None


def adjustments_for(conn, run: dict, row: dict) -> tuple[list[dict], dict]:
    """(the engine adjustments, the fact's scope fields {sku, branch, zone, valid_from, valid_to}) of a complete
    report (spec §7; research F-9), as the module docstring's mapping says."""
    f, kind, branch = row["fields"], row["kind"], row["branch"]
    if kind == "CAPACITY":
        lo, hi = _days(run, f, "valid_from", "valid_to")
        return ([{"kind": "capacity", "branch": branch, "day_from": lo, "day_to": hi, "free_totes": f["free_totes"]}],
                {"sku": None, "branch": branch, "zone": f["zone"], "valid_from": lo, "valid_to": hi})
    if kind == "DEMAND_EVENT":
        lo, hi = _days(run, f, "valid_from", "valid_to")
        return ([{"kind": "demand_multiplier", "branch": branch, "day_from": lo, "day_to": hi,
                  "value": f["multiplier"]}],
                {"sku": f.get("sku"), "branch": branch, "zone": None, "valid_from": lo, "valid_to": hi})
    if kind == "TRANSPORT":
        on, code = cases.day_of(run, f["applies_on"]), f["run_code"]
        if f["what"] == "RUN_NOT_AVAILABLE":
            adjustments = [{"kind": "run_available", "run_code": code, "value": False}]
        elif f["what"] == "DELAY":
            adjustments = [{"kind": "departure_delay_h", "run_code": code, "hours": f["delay_hours"]}]
        elif f["pricing_basis"] == "PER_UNIT":
            adjustments = [{"kind": "handling_cost", "run_code": code, "per_unit_cost": f["amount"]}]
        else:  # PER_TRIP: the trip's fixed cost, and handling at zero when the quote includes it
            adjustments = [{"kind": "transport_quote", "run_code": code, "fixed_cost": f["amount"]}]
            if f["includes_handling"] == "YES":
                adjustments.append({"kind": "handling_cost", "run_code": code, "per_unit_cost": 0.0})
        return adjustments, {"sku": None, "branch": branch, "zone": None, "valid_from": on, "valid_to": on}
    day, lot_id = run["day"], f["lot_id"]
    if kind == "STOCK_COUNT":  # the count's own fact (signals.count_apply), valid on the business day
        return ([{"kind": "stock_count", "lot_id": lot_id, "units": float(f["shelf_units"] + f["backroom_units"]),
                  "counted_day": day}],
                {"sku": row.get("sku"), "branch": branch, "zone": None, "valid_from": day, "valid_to": day})
    if kind == "CONDITION":  # human review only: no engine input, ever (D-13, T-07-52)
        return [], {"sku": row.get("sku"), "branch": branch, "zone": None, "valid_from": day, "valid_to": day}
    if kind == "EXPIRY":  # the printed date's own fact (signals.expiry_apply), valid to the later date
        printed = cases.day_of(run, f["printed_date"])
        lot = overlay.lot(conn, run, WORLD, lot_id)
        return ([{"kind": "expiry", "lot_id": lot_id, "expiry_day": printed}],
                {"sku": row.get("sku"), "branch": branch, "zone": None, "valid_from": day,
                 "valid_to": max(day, printed, lot["expiry_day"] if lot else day)})
    raise ValueError(kind)  # pragma: no cover - every kind in KINDS maps


def _affects(conn, run: dict, row: dict) -> list[str]:
    """The live cases in the report's scope now (case_id order), for the New report badge (D-16)."""
    found = scope_of(run, row)
    if found is None:
        return []
    live, _, _ = scopes.affected(conn, run, found)
    return [c["case_id"] for c in live]


# --- rows and views -------------------------------------------------------------------------------------------------

def _row(conn, run_id: str, signal_id: str, lock: bool = False) -> dict:
    """The report row (any revision), 404 not_found for an unknown id or an answer to a request."""
    row = db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s" + (" FOR UPDATE" if lock else ""),
                 r=run_id, s=signal_id)
    if row is None or not row.get("report_id"):
        raise ApiError(404, "not_found", f"No report {signal_id}")
    return row


def _current(conn, run_id: str, signal_id: str, lock: bool = False) -> dict:
    """The report's current revision (its highest), found from any of its revisions' signal_id (the first one is the
    report_id), so an action naming an earlier revision acts on the report and its revision check refuses a stale
    one (D-15, research F-26); 404 like `_row`."""
    first = _row(conn, run_id, signal_id)
    return db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND report_id = :p ORDER BY revision DESC LIMIT 1"
                  + (" FOR UPDATE" if lock else ""), r=run_id, p=first["report_id"])


def is_report(conn, run: dict, signal_id: str) -> bool:
    """Whether the signal is a field report (report_id set), not an answer to a request."""
    row = db.one(conn, "SELECT report_id FROM signals WHERE run_id = :r AND signal_id = :s", r=run["run_id"],
                 s=signal_id)
    return bool(row and row["report_id"])


def _field_corrections(conn, run_id: str, row: dict) -> dict:
    """Only this report's audited edits up to this revision can label a value."""
    if row["revision"] <= 1:
        return {}
    edits = db.rows(conn, "SELECT payload FROM activity_log WHERE run_id=:r AND type='report_edited' "
        "AND payload->>'report_id'=:p ORDER BY seq", r=run_id, p=row["report_id"])
    result = {}
    for event in edits:
        edit = event["payload"]
        revision = edit.get("revision")
        actor = edit.get("actor") or {}
        if not isinstance(revision, int) or revision > row["revision"] or actor.get("role") not in ("manager", "hq"):
            continue
        for name in edit.get("corrected_fields", []):
            if name in ("kind", *row["fields"]):
                result[name] = {"role": actor["role"], "branch": actor.get("branch"), "revision": revision}
    return result


def report_view(conn, run_id: str, row: dict) -> dict:
    """The Report shape (04-API-CONTRACT Shapes): the row with its owner, needs_input, the fact's label and the report
    whose fact replaced this one's."""
    fact = db.one(conn, "SELECT label, replaced_by FROM reported_facts WHERE run_id = :r AND fact_id = :f",
                  r=run_id, f=row["fact_id"]) if row.get("fact_id") else None
    replaced = None
    if fact and fact["replaced_by"]:
        later = db.one(conn, "SELECT report_id FROM signals WHERE run_id = :r AND fact_id = :f", r=run_id,
                       f=fact["replaced_by"])
        replaced = later["report_id"] if later else None
    simulated = row.get("provenance") == "simulated"
    # Local import keeps report creation independent from the agent's import
    # graph. This view is read-only: it checks freshness without engine/model.
    from agent.impact import summary_view
    run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
    return {
        "signal_id": row["signal_id"], "report_id": row["report_id"], "revision": row["revision"],
        "kind": row["kind"], "state": row["state"], "branch": row["branch"],
        "reporter_account": row["reporter_branch"] or "HQ", "reporter_role": row["reporter_role"],
        "reporter_branch": row["reporter_branch"], "owner": owner(row), "text": row["text"], "fields": row["fields"],
        "needs_input": needs_input(row["kind"], row["fields"]), "observed_at": realtime.iso(row.get("observed_at")),
        "created_at": realtime.iso(row["created_at"]), "case_id": row["case_id"], "corrects": row["corrects"],
        "file": _file_of(row), "reading": _reading_view(conn, run_id, row),
        "field_sources": row.get("field_sources") or {name: "typed"
            for name in ("kind", *KINDS[row["kind"]]["fields"])},
        "field_corrections": _field_corrections(conn, run_id, row),
        "provenance": "simulated" if simulated else "person",
        "simulated_label": SIMULATED_SOURCE if simulated else None,
        "confirmed_at": realtime.iso(row["confirmed_at"]), "confirmed_by": row["confirmed_by"],
        "dismissed_at": realtime.iso(row["dismissed_at"]), "dismissed_by": row["dismissed_by"],
        "dismiss_reason": row["dismiss_reason"], "withdrawn_at": realtime.iso(row["withdrawn_at"]),
        "fact_id": row["fact_id"], "fact_label": fact["label"] if fact else None, "replaced_by": replaced,
        "hold_id": _hold_of(conn, run_id, row), "task": _task_of(row),
        "impact_label": (NOT_RELEVANT_LABEL if row["kind"] == "NOT_RELEVANT" else
                         REVIEW_ONLY if row["kind"] == "CONDITION" else None), "day": row["created_day"],
        "impact_summary": summary_view(conn, run, row),
        "affects_count": len(row.get("affects") or []),  # the live cases its scope held when filed (07-20)
        "sku": row.get("sku"),  # the product it is about (a lot's record, or a demand event's), else null (07-20)
    }


def is_task(row: dict) -> bool:
    """A confirmed DISPLAY condition report: an advisory task under its branch's Tasks (D-13)."""
    return (row["kind"] == "CONDITION" and row["state"] == "CONFIRMED"
            and (row["fields"] or {}).get("condition") == "DISPLAY")


def _task_of(row: dict) -> dict | None:
    """`task` for an advisory task: {done_at, done_by, done_label} ("Done by {store} manager · {t}" once Mark done),
    all null while it is open; else None."""
    if not is_task(row):
        return None
    at = row.get("task_done_at")
    return {"done_at": realtime.iso(at), "done_by": row.get("task_done_by"),
            "done_label": f"Done by {row['branch']} manager · {realtime.label(at)}" if at else None}


def advisory(conn, run: dict, branch: str | None) -> list[dict]:
    """The branch's advisory tasks (D-13): its confirmed DISPLAY condition reports, the open ones first (oldest
    first), then the done ones (latest done first); none for HQ (no branch)."""
    if branch is None:
        return []
    rows = db.rows(conn, "SELECT * FROM signals WHERE run_id = :r AND report_id IS NOT NULL AND kind = 'CONDITION' "
                         "AND state = 'CONFIRMED' AND fields->>'condition' = 'DISPLAY' AND branch = :b ORDER BY "
                         "task_done_at IS NOT NULL, task_done_at DESC, confirmed_at, signal_id",
                   r=run["run_id"], b=branch)
    return [report_view(conn, run["run_id"], x) for x in rows]


def open_tasks(conn, run: dict, branch: str | None) -> int:
    """How many of the branch's advisory tasks are not done (the Tasks tile and the session's tasks_waiting)."""
    if branch is None:
        return 0
    return db.one(conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r AND report_id IS NOT NULL AND kind = "
                        "'CONDITION' AND state = 'CONFIRMED' AND fields->>'condition' = 'DISPLAY' AND branch = :b "
                        "AND task_done_at IS NULL", r=run["run_id"], b=branch)["n"]


def _hold_of(conn, run_id: str, row: dict) -> str | None:
    """The latest hold placed with the report (any revision): a MIXED printed date's, or one its lot's manager placed
    naming it (`POST /api/lots/{lot_id}/holds` with its signal_id), else None."""
    held = db.one(conn, "SELECT hold_id FROM lot_holds WHERE run_id = :r AND signal_id IN (SELECT signal_id FROM "
                        "signals WHERE run_id = :r AND report_id = :p) ORDER BY placed_at DESC, hold_id DESC LIMIT 1",
                  r=run_id, p=row["report_id"])
    return held["hold_id"] if held else None


def _party(conn, run: dict, actor: dict, row: dict) -> None:
    """A seat party to the report reads it: HQ, the reporter's store, the report's store, the linked case's source or
    plan destination (as for its file, api/files.py may_read); else 403."""
    if actor.get("role") == "hq":
        return
    parties = {row["reporter_branch"], row["branch"]}
    if row["case_id"]:
        case = db.one(conn, "SELECT source_branch, plan_dest FROM cases WHERE run_id = :r AND case_id = :c",
                      r=run["run_id"], c=row["case_id"])
        if case:
            parties |= {case["source_branch"], case["plan_dest"]}
    if actor.get("role") != "manager" or actor.get("branch") not in parties - {None}:
        raise ApiError(403, "forbidden", FORBIDDEN)


def get(conn, run: dict, actor: dict, signal_id: str) -> dict:
    """The report, with its revisions (oldest first) and its latest stored impact preview (`preview`, null when none;
    its `revision` says which revision it previewed, api/impact.py; 07-15), for a seat party to it."""
    from . import impact  # it reads this module at import
    row = _row(conn, run["run_id"], signal_id)
    _party(conn, run, actor, row)
    revisions = db.rows(conn, "SELECT * FROM signals WHERE run_id = :r AND report_id = :p ORDER BY revision",
                        r=run["run_id"], p=row["report_id"])
    return {**report_view(conn, run["run_id"], row),
            "revisions": [report_view(conn, run["run_id"], x) for x in revisions],
            "preview": impact.latest(conn, run["run_id"], row["report_id"])}


def unconfirmed_for(conn, run: dict, case_id: str) -> list[dict]:
    """The unconfirmed (READ) reports whose scope held the case when filed (`affects`), oldest first: the case's New
    report badge and its detail's `reports` (D-16). A confirmed, dismissed or withdrawn report leaves them."""
    rows = db.rows(conn, "SELECT * FROM signals WHERE run_id = :r AND report_id IS NOT NULL AND state = :st "
                         "AND affects @> jsonb_build_array(CAST(:c AS text)) ORDER BY created_at, signal_id",
                   r=run["run_id"], st=OPEN, c=case_id)
    return [report_view(conn, run["run_id"], x) for x in rows]


def unconfirmed_many(conn, run: dict, case_ids) -> dict[str, list[dict]]:
    """`unconfirmed_for`'s reports for many cases in one read (the board's set-based cards, whose New report badge reads
    only their `signal_id`): {case_id: the READ reports whose scope held the case when filed, oldest first, as stored
    rows}; nothing is read when `case_ids` is empty. The same condition per case (`affects @>` the case id), so the
    lists hold the same reports in the same order."""
    out: dict[str, list[dict]] = {c: [] for c in case_ids}
    if not out:
        return out
    for row in db.rows(conn, "SELECT k.case_id AS affected_case, s.* FROM unnest(CAST(:ids AS text[])) AS k(case_id) "
                             "JOIN signals s ON s.run_id = :r AND s.report_id IS NOT NULL AND s.state = :st "
                             "AND s.affects @> jsonb_build_array(k.case_id) ORDER BY s.created_at, s.signal_id",
                       r=run["run_id"], st=OPEN, ids=sorted(out)):
        out[row.pop("affected_case")].append(row)
    return out


def _mine_to_confirm(conn, run: dict, actor: dict) -> int:
    """How many READ reports the seat owns (its store's manager-owned kinds; HQ's transport)."""
    role = actor.get("role")
    kinds = [k for k, v in KINDS.items() if v["owner"] == role]
    where, p = ("", {}) if role == "hq" else (" AND branch = :b", {"b": actor.get("branch")})
    return db.one(conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r AND report_id IS NOT NULL AND state = :st "
                        "AND kind = ANY(:k)" + where, r=run["run_id"], st=OPEN, k=kinds, **p)["n"]


def _case_of(conn, run: dict, ref: str) -> str:
    """The case_id a Business events `case` filter names: a stock decision's key or its case_id (as given when no
    case has it, so the filter matches nothing)."""
    found = db.one(conn, "SELECT case_id FROM cases WHERE run_id = :r AND (key = :c OR case_id = :c) "
                         "ORDER BY case_id = :c DESC LIMIT 1", r=run["run_id"], c=ref)
    return found["case_id"] if found else ref


def listing(conn, run: dict, actor: dict, state: str | None = None, kind: str | None = None, page: int = 1,
            page_size: int | None = None, mine: bool = False, store: str | None = None,
            case: str | None = None) -> dict:
    """The field reports of the seat's store (HQ: every store), newest first, one row per report (its current
    revision; an earlier REPLACED revision only when `state` asks for REPLACED), in pages (`page`, `page_size`: the
    reference list page size is the default and the largest, one page on a set without one; `total`), with the
    workspace's allowance and how many filed reports wait for this seat's confirmation (`needs_my_confirmation`).

    Business events' filters (07-20; 06.1 D-17: the server filters, the client never does): `mine` the READ reports
    this seat confirms (the ones `needs_my_confirmation` counts), `store` one store's reports, `case` the reports
    linked to that stock decision (a key or a case_id) or holding it in their scope when filed (`affects`)."""
    where, p = "", {}
    if actor.get("role") != "hq":
        where, p = " AND (branch = :b OR reporter_branch = :b)", {"b": actor.get("branch")}
    if state:
        where, p = where + " AND state = :st", {**p, "st": state}
    else:
        where += " AND state <> 'REPLACED'"
    if kind:
        where, p = where + " AND kind = :k", {**p, "k": kind}
    if mine:
        role = actor.get("role")
        where += " AND state = :mst AND kind = ANY(:mk)" + ("" if role == "hq" else " AND branch = :mb")
        p = {**p, "mst": OPEN, "mk": [k for k, v in KINDS.items() if v["owner"] == role], "mb": actor.get("branch")}
    if store:
        where, p = where + " AND branch = :sb", {**p, "sb": store}
    if case:
        where += " AND (case_id = :cid OR affects @> jsonb_build_array(CAST(:cid AS text)))"
        p = {**p, "cid": _case_of(conn, run, case)}
    total = db.one(conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r AND report_id IS NOT NULL" + where,
                   r=run["run_id"], **p)["n"]
    size = movement._page_size(refdata.get(run["dataset_version"]), page_size, total)  # the lists' one rule
    rows = db.rows(conn, "SELECT * FROM signals WHERE run_id = :r AND report_id IS NOT NULL" + where +
                   " ORDER BY created_at DESC, signal_id DESC LIMIT :n OFFSET :o", r=run["run_id"], n=size,
                   o=(page - 1) * size, **p)
    return {"signals": [report_view(conn, run["run_id"], x) for x in rows], "page": page, "page_size": size,
            "total": total, "allowance": allowance(conn, run),
            "needs_my_confirmation": _mine_to_confirm(conn, run, actor)}


def allowance(conn, run: dict) -> dict:
    """The workspace's allowance today (D-20): reports and first files left of `caps.workspace`, counted under the
    scope run:<run_id>, and when the window resets (UTC). Never the cross-workspace upload cap's number."""
    out = {}
    for kind, name in (("signal", "reports"), ("upload", "files")):
        limit, window = config.WORKSPACE_CAPS[kind]
        start = caps.window_start(window)
        used = db.one(conn, "SELECT count FROM usage_counts WHERE scope = :s AND kind = :k AND window_start = :w",
                      s=workspace_scope(run), k=kind, w=start)
        out[f"{name}_left"] = max(0, limit - (used["count"] if used else 0))
        out[f"{name}_limit"] = limit
        if kind == "signal":
            out["resets"] = realtime.iso(start + (timedelta(days=1) if window == "day" else timedelta(hours=1)))
    return out


def workspace_scope(run: dict) -> str:
    """The cap scope of the workspace's allowance (D-20): every seat of the workspace shares it."""
    return f"run:{run['run_id']}"


# --- file and confirm -----------------------------------------------------------------------------------------------

def may_file(actor: dict, kind) -> None:
    """Who may file (or preview before filing, api/impact.py `draft`) a report of this kind (D-15): a store's manager
    any kind for their store, HQ transport or a Not relevant reading; 400 for an unknown kind, 403 for another seat."""
    if not isinstance(kind, str) or kind not in KINDS:
        raise ApiError(400, "bad_request", f"kind must be one of {', '.join(MANUAL_KINDS)}")
    if kind == "NOT_RELEVANT" and actor.get("role") not in ("manager", "hq"):
        raise ApiError(403, "forbidden", "Only a manager or HQ files reports")
    if actor.get("role") == "hq" and kind != "NOT_RELEVANT" and KINDS[kind]["owner"] != "hq":
        raise ApiError(403, "forbidden", "HQ reports transport only; a store's manager reports for their store")
    if actor.get("role") != "manager" and KINDS[kind]["owner"] == "manager":
        raise ApiError(403, "forbidden", "Only a store's manager reports for their store")


def create(conn, run: dict, actor: dict, kind, fields, text, case_id=None, corrects=None, observed=None,
           file: bytes | None = None, reading_id=None, field_sources=None) -> dict:
    """File a report as READ (revision 1, report_id its own signal_id): the seat's store from the session only, HQ
    transport only; values checked, blanks listed as needs_input; the live cases in scope recorded in `affects`.
    Applies nothing (D-16). The caller spends the workspace allowance in the same transaction (D-20).

    STOCK_COUNT and EXPIRY (07-14) are about one lot on the record at the reporter's store (400 otherwise; the SKU is
    the record's) and state "When did you check?" (`observed`: "now" or an ISO time with its offset, no later than
    now; 400 check_time without it, never a default, D-22); a printed date sits inside the engine's bound on the lot's
    own input (400 out_of_bounds). A count, a printed date or a condition is corrected by a new check, never by
    `corrects`. A MIXED printed date places the audited MIXED_DATES hold now, placed by the reporter, the lot's manager
    (D-11, D-13); the report then has nothing to confirm.

    One optional file (PNG, JPEG or PDF by content, at most uploads.max_bytes; D-14) is checked, spent and stored after
    every other check (`_spend_file`: the report's first file spends one of the workspace's files and the
    cross-workspace upload count, D-20), so a refused report spends nothing."""
    may_file(actor, kind)
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "bad_request", "text must be a string")
    f = clean(run, kind, fields, inputs.sandbox_policy(conn, run))
    branch = _branch(run, actor, kind, f)
    lot = _lot_at(conn, run, branch, f) if kind in LOT_KINDS else None
    observed_at = None
    if kind in CHECKED:
        observed_at = realtime.parse_observed(observed, realtime.db_now(conn))  # never defaulted (D-22)
    elif not _blank(observed):
        raise ApiError(400, "bad_request", "Only a count or a printed date states when it was checked")
    if kind == "EXPIRY" and lot is not None and f["printed_date"] not in (None, MIXED):
        _printed_ok(conn, run, branch, f)
    if corrects is not None:
        if kind in LOT_KINDS:
            raise ApiError(400, "bad_request", "A count or a printed date is corrected by a new check")
        _correctable(conn, run, kind, branch, corrects)
    if case_id is not None:
        if not isinstance(case_id, str) or db.one(conn, "SELECT 1 AS x FROM cases WHERE run_id = :r AND case_id = :c",
                                                  r=run["run_id"], c=case_id) is None:
            raise ApiError(404, "not_found", f"No case {case_id}")
    sources = _field_origins(conn, run, actor, kind, f, text or "", reading_id, field_sources, case_id, file)
    stored = _spend_file(conn, run, None, file) if file else None
    sid = f"SIG-{uuid.uuid4().hex[:8]}"
    sku = lot["sku"] if lot is not None else f.get("sku")
    row = {"kind": kind, "fields": f, "branch": branch, "sku": sku}
    affects = _affects(conn, run, row)
    db.run(conn, "INSERT INTO signals (run_id, signal_id, report_id, revision, reporter_role, reporter_branch, branch, "
                 "text, kind, fields, state, provenance, created_day, case_id, sku, run_code, corrects, affects, "
                 "lot_id, observed_at, reading_id, field_sources) "
                 "VALUES (:r, :s, :s, 1, :role, :rb, :b, :t, :k, CAST(:f AS jsonb), :st, 'person', :d, :c, :sku, :run, "
                 ":fix, CAST(:a AS jsonb), :lot, :o, :reading, CAST(:sources AS jsonb))", r=run["run_id"], s=sid, role=actor["role"],
           rb=actor.get("branch"), b=branch, t=(text or "").strip(), k=kind, f=db.js(f), st=OPEN, d=run["day"],
           c=case_id, sku=sku, run=f.get("run_code"), fix=corrects, a=db.js(affects), lot=f.get("lot_id"),
           o=observed_at, reading=reading_id, sources=db.js(sources))
    if stored is not None:
        _set_file(conn, run, sid, stored)
    saved = _row(conn, run["run_id"], sid)
    db.log(conn, run["run_id"], run["day"], "report_filed", case_id, signal_id=sid, kind=kind, fields=f,
           needs_input=needs_input(kind, f), affects=affects, corrects=corrects,
           reading_id=reading_id, field_sources=sources,
           observed_at=realtime.iso(observed_at), file=_file_of(saved), file_counted=saved["file_counted"],
           actor=accounts.logged_actor(actor))
    _place_holds(conn, run, actor, saved)  # each a separate, logged action after the report (D-13)
    return report_view(conn, run["run_id"], saved)


def _file_of(row: dict) -> dict | None:
    """The report's file as a client sees it: its stored (sniffed) type and size, never where it is (D-20)."""
    return {"type": row["file_type"], "size": row.get("file_size")} if row.get("file_uri") else None


FILE_COLUMNS = ("file_uri", "file_type", "file_sha256", "file_size", "file_counted")


def _spend_file(conn, run: dict, report_id: str | None, data: bytes) -> dict:
    """A report's file, checked by content (413 too_large, 415 bad_file before anything is spent), then spent, then
    stored under `<run_id>/<sha256>.<ext>` (api/files.py; validate, spend, store, insert: research Q4). The report's
    first file (no revision of it carried one) spends one of the workspace's files and the cross-workspace upload
    count (`caps.spend` with scope run:<run_id>; `file_counted` true); a later file (a replacement) spends only the
    cross-workspace count (scope None; `file_counted` false). D-20, acceptance check 14; T-07-55: both in the
    request's transaction, so a refusal anywhere spends nothing."""
    files.sniff(data)
    first = report_id is None or db.one(
        conn, "SELECT count(*) AS n FROM signals WHERE run_id = :r AND report_id = :p AND file_uri IS NOT NULL",
        r=run["run_id"], p=report_id)["n"] == 0
    caps.spend(conn, workspace_scope(run) if first else None, "upload")
    stored = files.save(run["run_id"], data)
    return {"file_uri": stored.uri, "file_type": stored.mime, "file_sha256": stored.sha256,
            "file_size": stored.size, "file_counted": first}


def _set_file(conn, run: dict, signal_id: str, cols: dict) -> None:
    db.run(conn, "UPDATE signals SET file_uri = :file_uri, file_type = :file_type, file_sha256 = :file_sha256, "
                 "file_size = :file_size, file_counted = :file_counted WHERE run_id = :r AND signal_id = :s",
           r=run["run_id"], s=signal_id, **cols)


def _lot_at(conn, run: dict, branch: str | None, f: dict) -> dict | None:
    """The report's lot on the workspace's record at the reporter's store (api/overlay.py), or None while lot_id is
    blank; a lot not on the record there is 400 naming lot_id (a manager reports their own store's lots)."""
    if f.get("lot_id") is None:
        return None
    lot = overlay.lot(conn, run, WORLD, f["lot_id"])
    if lot is None or lot["branch"] != branch:
        raise ApiError(400, "bad_request", f"Lot {f['lot_id']} is not on the record at {branch}", field="lot_id")
    return lot


class _AsPrinted:
    """Re-raises a printed date's bound refusal (signals.check_printed names "dates.<lot_id>") naming the report's
    field, printed_date."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if isinstance(exc, ApiError) and exc.code == "out_of_bounds" and str(exc.extra.get("field", "")).startswith(
                "dates."):
            raise ApiError(400, "out_of_bounds", exc.message, field="printed_date",
                           bound=exc.extra.get("bound")) from None
        return False


def _printed_ok(conn, run: dict, branch: str, f: dict) -> None:
    """The printed date inside the engine's expiry bound on the lot's own input today (signals.check_dates, T-07-27):
    400 out_of_bounds naming printed_date."""
    with _AsPrinted():
        signals.check_dates(conn, run, {"to_branch": branch}, {f["lot_id"]: cases.day_of(run, f["printed_date"])})


def _hold_trigger(row: dict) -> tuple | None:
    """What makes a report place a hold when filed or edited (`_place_holds`): a MIXED printed date, or hold true on
    a damage or possible safety condition, with its lot; else None."""
    f = row["fields"] or {}
    if row["kind"] == "EXPIRY" and f.get("printed_date") == MIXED and f.get("lot_id"):
        return ("MIXED_DATES", f["lot_id"])
    if row["kind"] == "CONDITION" and f.get("hold") and f.get("condition") in HOLDABLE and f.get("lot_id"):
        return ("CONDITION", f["lot_id"], f["condition"])
    return None


def _place_holds(conn, run: dict, actor: dict, row: dict) -> list[dict]:
    """The holds a report places when it is filed (D-11, D-13), each through lotholds.place (logged `hold_placed`,
    one ACTIVE hold per lot and kind): a MIXED printed date's MIXED_DATES hold "Separate stock and verify before
    moving", placed by the reporter, the lot's manager."""
    f = row["fields"]
    if row["kind"] == "EXPIRY" and f.get("printed_date") == MIXED and f.get("lot_id"):
        return [lotholds.place(conn, run, actor, f["lot_id"], "MIXED_DATES", lotholds.MIXED_TEXT,
                               signal_id=row["signal_id"], case_id=row["case_id"])]
    if row["kind"] == "CONDITION" and f.get("hold") and f.get("condition") in HOLDABLE and f.get("lot_id"):
        return [lotholds.place(conn, run, actor, f["lot_id"], "CONDITION",  # "Hold this lot for review" (D-13)
                               f"Held for review: {CONDITION_LABEL[f['condition']]}", signal_id=row["signal_id"],
                               case_id=row["case_id"])]
    return []


def _branch(run: dict, actor: dict, kind: str, f: dict) -> str | None:
    """The store the report is about: the seat's own, or for transport the named run's origin on the day it applies
    (today while that is blank). A run that does not exist then is 400; a store's manager reports only runs leaving
    their store (403); HQ's report with no run yet is about no store until it names one."""
    if kind != "TRANSPORT" or f["run_code"] is None:
        return actor.get("branch")
    day = cases.day_of(run, f["applies_on"]) if f["applies_on"] else run["day"]
    trip = refdata.get(run["dataset_version"]).run_on(f["run_code"], day)
    if trip is None:
        raise ApiError(400, "bad_request", f"No run {f['run_code']} on {cases.iso_of(run, day)}", field="run_code")
    if actor.get("role") == "manager" and trip.origin != actor.get("branch"):
        raise ApiError(403, "forbidden", "You can report transport only for runs leaving your store")
    return trip.origin


def _correctable(conn, run: dict, kind: str, branch: str | None, corrects) -> dict:
    """The report a correction names (D-15): a CONFIRMED report of the same kind about the same store whose fact is
    still in force; a report corrected already is 409 (correct the latest one), any other state 409 wrong_state."""
    if not isinstance(corrects, str):
        raise ApiError(400, "bad_request", "corrects must name a report")
    old = _row(conn, run["run_id"], corrects)
    if old["kind"] != kind or old["branch"] != branch:
        raise ApiError(400, "bad_request", "A correction is a report of the same kind about the same store")
    if old["state"] != "CONFIRMED":
        raise ApiError(409, "wrong_state", f"Report is {old['state']}; only a confirmed report is corrected",
                       state=old["state"])
    fact = db.one(conn, "SELECT replaced_by FROM reported_facts WHERE run_id = :r AND fact_id = :f",
                  r=run["run_id"], f=old["fact_id"])
    if fact is None or fact["replaced_by"]:
        raise ApiError(409, "wrong_state", "This report was corrected already; correct the latest report")
    return old


def patch(conn, run: dict, actor: dict, signal_id: str, revision, fields=None, text=None, observed=None,
          file: bytes | None = None, *, kind=None) -> dict:
    """Edit a report before confirmation (D-15, research F-26; T-07-54): the reporter or the owner, on the report's
    current revision while it is READ (else 409 wrong_state), naming that revision (else 409 stale_version). The edit
    is a new revision of the same report (same report_id, revision + 1, the reporter unchanged) and the earlier one
    becomes REPLACED, kept. `fields` corrects the named fields over the current ones and the whole set is checked
    again as at filing (the lot on the record at the store, a printed date's bound; a transport report's store from
    its run); `text` replaces the text; `observed` restates a count's or printed date's check time (D-22); `affects`
    is recomputed. A count's or printed date's revision keeps the report's day (`created_day`) unless the edit gives
    a new check time: the check is from the day it was taken, so a count filed on an earlier day and edited by its
    text or fields alone is still refused at confirmation (409 stale_version, CR-02; 07-14 review V1). Every other
    kind's revision takes today. A file follows `_spend_file`: the report's first file spends one of the workspace's
    files, a replacement only the cross-workspace count, and an edit without a file spends nothing and keeps the
    report's file.
    A hold the edit newly asks for is placed (a MIXED printed date, hold on a damage or safety condition). Logged
    `report_edited`. Confirm names the revision it confirms."""
    row = _current(conn, run["run_id"], signal_id, lock=True)
    mine = (actor.get("role"), actor.get("branch")) == (row["reporter_role"], row["reporter_branch"])
    if not mine and not _owns(actor, row):
        raise ApiError(403, "forbidden", "Only the person who filed this report or its owner can edit it")
    if row["state"] != OPEN:
        raise ApiError(409, "wrong_state", f"Report is {row['state']}", state=row["state"])
    if isinstance(revision, bool) or revision != row["revision"]:
        raise ApiError(409, "stale_version", "The report changed since you looked at it", revision=row["revision"])
    if fields is not None and not isinstance(fields, dict):
        raise ApiError(400, "bad_request", "fields must be a JSON object")
    if text is not None and not isinstance(text, str):
        raise ApiError(400, "bad_request", "text must be a string")
    if fields is None and text is None and _blank(observed) and not file:
        raise ApiError(400, "bad_request", "Nothing to change: send fields, text, observed or a file")
    if kind is not None and kind != row["kind"]:
        if row["kind"] != "NOT_RELEVANT":
            raise ApiError(400, "bad_request", "Only a Not relevant report can change its kind")
        # Creation's identical checks are inline on the API-only source lane.
        if not isinstance(kind, str) or kind not in KINDS:
            raise ApiError(400, "bad_request", f"kind must be one of {', '.join(MANUAL_KINDS)}")
        if actor.get("role") == "hq" and KINDS[kind]["owner"] != "hq":
            raise ApiError(403, "forbidden", "HQ reports transport only; a store's manager reports for their store")
        if actor.get("role") not in ("manager", "hq"):
            raise ApiError(403, "forbidden", "Only a manager or HQ files reports")
    kind = row["kind"] if kind is None else kind
    f = clean(run, kind, {**(row["fields"] or {}), **(fields or {})}, inputs.sandbox_policy(conn, run))
    branch = _branch(run, actor, kind, f) if kind == "TRANSPORT" else row["branch"]
    lot = _lot_at(conn, run, branch, f) if kind in LOT_KINDS else None
    observed_at, created = row["observed_at"], row["created_day"] if kind in CHECKED else run["day"]
    if kind in CHECKED and (kind != row["kind"] or not _blank(observed)):
        observed_at, created = realtime.parse_observed(observed, realtime.db_now(conn)), run["day"]
    elif kind not in CHECKED and not _blank(observed):
        raise ApiError(400, "bad_request", "Only a count or a printed date states when it was checked")
    if kind == "EXPIRY" and lot is not None and f["printed_date"] not in (None, MIXED):
        _printed_ok(conn, run, branch, f)
    sources = row.get("field_sources") or {name: "typed" for name in ("kind", *KINDS[kind]["fields"])}
    sources = dict(sources)
    corrected_fields = []
    if kind != row["kind"]:
        sources = {name: "typed" for name in ("kind", *KINDS[kind]["fields"])}
        corrected_fields.append("kind")
    for name in fields or {}:
        if f[name] != (row["fields"] or {}).get(name):
            sources[name] = "cleared" if f[name] is None else "typed"
            corrected_fields.append(name)
    stored = _spend_file(conn, run, row["report_id"], file) if file else {c: row[c] for c in FILE_COLUMNS}
    sid, n = f"SIG-{uuid.uuid4().hex[:8]}", row["revision"] + 1
    sku = lot["sku"] if lot is not None else f.get("sku")
    affects = _affects(conn, run, {"kind": kind, "fields": f, "branch": branch, "sku": sku})
    db.run(conn, "UPDATE signals SET state = 'REPLACED' WHERE run_id = :r AND signal_id = :s", r=run["run_id"],
           s=row["signal_id"])
    db.run(conn, "INSERT INTO signals (run_id, signal_id, report_id, revision, reporter_role, reporter_branch, branch, "
                 "text, kind, fields, state, provenance, created_day, case_id, sku, run_code, corrects, affects, "
                 "lot_id, observed_at, reading_id, field_sources) VALUES (:r, :s, :p, :n, :role, :rb, :b, :t, :k, "
                 "CAST(:f AS jsonb), :st, :prov, "
                 ":d, :c, :sku, :run, :fix, CAST(:a AS jsonb), :lot, :o, :reading, CAST(:sources AS jsonb))", r=run["run_id"], s=sid, p=row["report_id"],
           n=n, role=row["reporter_role"], rb=row["reporter_branch"], b=branch,
           t=row["text"] if text is None else text.strip(), k=kind, f=db.js(f), st=OPEN, prov=row["provenance"],
           d=created, c=row["case_id"], sku=sku, run=f.get("run_code"), fix=row["corrects"], a=db.js(affects),
           lot=f.get("lot_id"), o=observed_at, reading=row.get("reading_id"), sources=db.js(sources))
    if stored["file_uri"]:
        _set_file(conn, run, sid, stored)
    saved = _row(conn, run["run_id"], sid)
    db.log(conn, run["run_id"], run["day"], "report_edited", row["case_id"], signal_id=sid,
           replaces=row["signal_id"], report_id=row["report_id"], revision=n, kind=kind, fields=f,
           corrected_fields=corrected_fields,
           reading_id=row.get("reading_id"), field_sources=sources,
           needs_input=needs_input(kind, f), affects=affects, observed_at=realtime.iso(observed_at),
           file=_file_of(saved), file_spent=("workspace" if stored["file_counted"] else "global") if file else None,
           actor=accounts.logged_actor(actor))
    if _hold_trigger(saved) is not None and _hold_trigger(saved) != _hold_trigger(row):
        _place_holds(conn, run, actor, saved)
    return report_view(conn, run["run_id"], saved)


def _who(row: dict) -> tuple[str, str | None]:
    """The label's reporter: ("manager", their store) or ("HQ", None)."""
    return ("manager", row["reporter_branch"]) if row["reporter_role"] == "manager" else ("HQ", None)


def _refuse_expired(run: dict, row: dict) -> None:
    """An expired quote can't be confirmed (spec §7): 409 quote_expired "This quote expired on <date>" when a QUOTE's
    stored `valid_until` is before the business day. Checked before the fields are checked again: since valid_until is
    never before applies_on (`_check_range`, 07-13 review T2), the quote's run day has passed too, and the range check
    would otherwise answer first."""
    f = row["fields"] or {}
    if row["kind"] != "TRANSPORT" or f.get("what") != "QUOTE" or not f.get("valid_until"):
        return
    until = cases.day_of(run, f["valid_until"])
    if until < run["day"]:
        raise ApiError(409, "quote_expired", f"This quote expired on "
                                             f"{cases.date_label(until, refdata.get(run['dataset_version']))}")


def confirm(conn, run: dict, actor: dict, signal_id: str, revision, checked_both=None) -> dict:
    """The owner confirms the report's current revision (self-confirmation allowed, D-15): the values are checked
    again, a blank required field is 409 needs_input, then one reported fact with its label is written and applied by
    case state (`facts.apply`, D-17). Under the run lock FOR UPDATE, like a count's confirmation. Returns {fact_id,
    labels, cases, flagged, reassessed}. STOCK_COUNT and EXPIRY follow the request answers' rules (`_confirm_lot`,
    07-14)."""
    run = db.lock_run(conn, run["run_id"], mode="UPDATE")
    row = _current(conn, run["run_id"], signal_id, lock=True)  # an earlier revision's id names the report (F-26)
    signal_id = row["signal_id"]
    if not _owns(actor, row):
        who = "HQ" if owner(row)["role"] == "hq" else f"the {row['branch']} manager"
        raise ApiError(403, "forbidden", f"Only {who} can confirm this report")
    if row["state"] != OPEN:
        raise ApiError(409, "wrong_state", f"Report is {row['state']}", state=row["state"])
    if isinstance(revision, bool) or revision != row["revision"]:
        raise ApiError(409, "stale_version", "The report changed since you looked at it", revision=row["revision"])
    if row["kind"] == "NOT_RELEVANT":
        raise ApiError(409, "wrong_state", "A Not relevant report can be dismissed, never confirmed")
    _refuse_expired(run, row)  # before the range checks: an expired quote's day has passed too (07-13 review T2)
    f = clean(run, row["kind"], row["fields"], inputs.sandbox_policy(conn, run))  # the bounds again (T-07-47)
    missing = needs_input(row["kind"], f)
    if missing:
        only_choice = all(m["field"] in ("pricing_basis", "includes_handling") for m in missing)
        raise ApiError(409, "needs_input", CHOOSE if only_choice else COMPLETE_FIRST, fields=missing)
    if row["kind"] in CHECKED:
        return _confirm_lot(conn, run, actor, {**row, "fields": f}, checked_both)
    if row["kind"] == "CONDITION":
        return _confirm_condition(conn, run, actor, row)
    old = None
    if row["corrects"]:  # the corrected report's fact must still be in force (D-15)
        old = _correctable(conn, run, row["kind"], row["branch"], row["corrects"])
    bookings.expire_holds(conn, run)
    row = {**row, "fields": f}
    adjustments, sf = adjustments_for(conn, run, row)
    at = realtime.db_now(conn)  # the confirmation time the fact row and the report store (now(), fixed in the tx)
    source = SIMULATED_SOURCE if row.get("provenance") == "simulated" else SOURCE
    label = facts.label(*_who(row), at, source)
    fact_id = facts.write(conn, run, kind=row["kind"], adjustments=adjustments, sku=sf["sku"], branch=sf["branch"],
                          zone=sf["zone"], valid_from=sf["valid_from"], valid_to=sf["valid_to"], label=label,
                          confirmed_by="hq" if actor["role"] == "hq" else f"manager:{actor['branch']}",
                          observed_at=None, source=source, signal_id=signal_id)
    db.run(conn, "UPDATE signals SET state = 'CONFIRMED', confirmed_at = now(), confirmed_by = :by, fact_id = :f "
                 "WHERE run_id = :r AND signal_id = :s", by=_account(actor), f=fact_id, r=run["run_id"], s=signal_id)
    found = [scopes.of_adjustments(adjustments, sku=sf["sku"], branch=sf["branch"], zone=sf["zone"],
                                   valid_from=sf["valid_from"], valid_to=sf["valid_to"])]
    if old is not None:  # the earlier fact stops applying from now on; the cases in its scope are re-planned too
        earlier = db.one(conn, "SELECT * FROM reported_facts WHERE run_id = :r AND fact_id = :f", r=run["run_id"],
                         f=old["fact_id"])
        facts.replace(conn, run, earlier["fact_id"], fact_id)
        found.append(scopes.of_adjustments(earlier["adjustments"], sku=earlier["sku"], branch=earlier["branch"],
                                           zone=earlier["zone"], valid_from=earlier["valid_from"],
                                           valid_to=earlier["valid_to"]))
    seq = db.one(conn, "SELECT nextval('activity_seq') AS n")["n"]  # the report is logged before what it causes
    done = _apply(conn, run, fact_id, found, note=f"report confirmed ({fact_id})")
    db.run(conn, "INSERT INTO activity_log (run_id, seq, day, type, case_id, payload) VALUES (:r, :n, :d, "
                 "'report_confirmed', :c, CAST(:p AS jsonb))", r=run["run_id"], n=seq, d=run["day"], c=row["case_id"],
           p=db.js({"signal_id": signal_id, "report_id": row["report_id"], "revision": row["revision"],
                    "kind": row["kind"], "fact_id": fact_id, "label": label, "cases": done["cases"],
                    "replaced": old["fact_id"] if old is not None else None,
                    "flagged": done["flagged"], "reassessed": done["reassessed"],
                    "actor": accounts.logged_actor(actor)}))
    return {"fact_id": fact_id, "labels": [label], "flagged": done["flagged"], "reassessed": done["reassessed"],
            "cases": [{"case_id": c["case_id"], "key": c["key"], "rec_version": c["rec_version"]}
                      for c in done["cases"]]}


def _applicable(conn, run: dict, row: dict) -> None:
    """The applicability check before a lot report is confirmed (D-22), as for an answer: the lot still on the record
    at the report's store with the SKU it had when filed, else 409 count_mismatch naming the lot."""
    lot_id = row["fields"]["lot_id"]
    lot = overlay.lot(conn, run, WORLD, lot_id)
    if lot is None or lot["branch"] != row["branch"]:
        raise ApiError(409, "count_mismatch", f"Lot {lot_id} is no longer on the record at {row['branch']}",
                       lot_id=lot_id)
    if lot["sku"] != row["sku"]:
        raise ApiError(409, "count_mismatch", f"Lot {lot_id} is no longer recorded as {row['sku']}", lot_id=lot_id)


def _confirm_lot(conn, run: dict, actor: dict, row: dict, checked_both) -> dict:
    """A STOCK_COUNT or EXPIRY report's confirmation by the lot's manager, under the request answers' rules and through
    the same steps (api/signals.py `count_newer`/`count_apply`, `expiry_newer`/`expiry_apply`; D-11, D-22): a count
    only on the business day it was filed (409 stale_version, CR-02) and with the tick (409 scope_tick); the lot still
    on the record at the store with its SKU (409 count_mismatch); the check time against the fact in force (409
    older_evidence, review_required; the refused report stays READ); then a count corrects the record and a printed
    date writes an expiry fact without a record rewrite and resolves the MIXED_DATES holds placed before its check.
    The same check time and value change nothing (`unchanged`). The fact's label is a report's, "Reported by {store}
    manager · {t} · field report"."""
    f, kind, branch, sid = row["fields"], row["kind"], row["branch"], row["signal_id"]
    lot_id = f["lot_id"]
    if kind == "STOCK_COUNT":
        if row["created_day"] != run["day"]:  # the record moved since this count was taken (CR-02)
            raise ApiError(409, "stale_version", signals.EARLIER_DAY, answer_day=row["created_day"])
        if checked_both is not True:
            raise ApiError(409, "scope_tick", TICK)
    _applicable(conn, run, row)
    observed = row.get("observed_at") or row["created_at"]
    if kind == "STOCK_COUNT":
        units = {lot_id: float(f["shelf_units"] + f["backroom_units"])}
        same = signals.count_newer(conn, run, [lot_id], units, observed)
    else:
        days = {lot_id: cases.day_of(run, f["printed_date"])}
        with _AsPrinted():
            same = signals.expiry_newer(conn, run, branch, days, observed)
    source = SIMULATED_SOURCE if row.get("provenance") == "simulated" else SOURCE
    fact_id, labels, resolved = None, [], []
    done = {"cases": [], "flagged": [], "reassessed": []}
    if lot_id in same:  # the same check time and value as the evidence in force: nothing changes (D-22)
        seq = db.one(conn, "SELECT nextval('activity_seq') AS n")["n"]
        if kind == "EXPIRY":
            resolved = signals.resolve_holds(conn, run, actor, [lot_id], observed, None, row["case_id"])
    else:
        label = facts.label(*_who(row), realtime.db_now(conn), source)
        labels = [label]
        common = {"sku": row["sku"], "branch": branch, "observed": observed, "label": label, "source": source,
                  "signal_id": sid, "note": "report confirmed"}
        if kind == "STOCK_COUNT":
            fact_id, seq, done = signals.count_apply(conn, run, [lot_id], units, **common)
        else:
            fact_id, seq, done, resolved = signals.expiry_apply(conn, run, actor, [lot_id], days, [lot_id],
                                                                case_id=row["case_id"], **common)
    db.run(conn, "UPDATE signals SET state = 'CONFIRMED', confirmed_at = now(), confirmed_by = :by, fact_id = :f "
                 "WHERE run_id = :r AND signal_id = :s", by=_account(actor), f=fact_id, r=run["run_id"], s=sid)
    db.run(conn, "INSERT INTO activity_log (run_id, seq, day, type, case_id, payload) VALUES (:r, :n, :d, "
                 "'report_confirmed', :c, CAST(:p AS jsonb))", r=run["run_id"], n=seq, d=run["day"], c=row["case_id"],
           p=db.js({"signal_id": sid, "report_id": row["report_id"], "revision": row["revision"], "kind": kind,
                    "fact_id": fact_id, "label": labels[0] if labels else None, "cases": done["cases"],
                    "replaced": None, "flagged": done["flagged"], "reassessed": done["reassessed"],
                    "lot_id": lot_id, "observed_at": realtime.iso(observed), "unchanged": fact_id is None,
                    "holds_resolved": resolved, "actor": accounts.logged_actor(actor)}))
    return {"fact_id": fact_id, "labels": labels, "flagged": done["flagged"], "reassessed": done["reassessed"],
            "unchanged": fact_id is None,
            "cases": [{"case_id": c["case_id"], "key": c["key"], "rec_version": c["rec_version"]}
                      for c in done["cases"]]}


def _confirm_condition(conn, run: dict, actor: dict, row: dict) -> dict:
    """A CONDITION report confirmed by the lot's manager (D-13, T-07-52): CONFIRMED with who and when, and nothing
    else: no reported fact, no engine input, no re-plan, no flag, no hold change. A DISPLAY issue becomes an open
    advisory task under its branch's Tasks. Logged `report_confirmed`."""
    db.run(conn, "UPDATE signals SET state = 'CONFIRMED', confirmed_at = now(), confirmed_by = :by WHERE run_id = :r "
                 "AND signal_id = :s", by=_account(actor), r=run["run_id"], s=row["signal_id"])
    task = row["fields"].get("condition") == "DISPLAY"
    db.log(conn, run["run_id"], run["day"], "report_confirmed", row["case_id"], signal_id=row["signal_id"],
           report_id=row["report_id"], revision=row["revision"], kind=row["kind"], fact_id=None, label=None,
           cases=[], flagged=[], reassessed=[], impact_label=REVIEW_ONLY, task=task,
           actor=accounts.logged_actor(actor))
    return {"fact_id": None, "labels": [], "cases": [], "flagged": [], "reassessed": [],
            "impact_label": REVIEW_ONLY, "task": task}


def mark_done(conn, run: dict, actor: dict, signal_id: str) -> dict:
    """Mark done (D-13; acceptance check 18): the report branch's manager closes an advisory task (a confirmed DISPLAY
    condition report): `task_done_at` now() and `task_done_by` the account, logged `task_done`. It leaves the open
    count and keeps its history, and it touches only the task's two columns: it never clears or releases a lot hold
    (T-07-53). Another seat is 403; anything else, or a task done already, 409 wrong_state."""
    row = _current(conn, run["run_id"], signal_id, lock=True)
    signal_id = row["signal_id"]
    if actor.get("role") != "manager" or actor.get("branch") != row["branch"]:
        raise ApiError(403, "forbidden", f"Only the {row['branch']} manager can mark this task done")
    if not is_task(row):
        raise ApiError(409, "wrong_state", "Only a confirmed display issue is an advisory task", state=row["state"])
    if row.get("task_done_at") is not None:
        raise ApiError(409, "wrong_state", "This task is done already", state=row["state"])
    db.run(conn, "UPDATE signals SET task_done_at = now(), task_done_by = :by WHERE run_id = :r AND signal_id = :s",
           by=_account(actor), r=run["run_id"], s=signal_id)
    done = _row(conn, run["run_id"], signal_id)
    db.log(conn, run["run_id"], run["day"], "task_done", row["case_id"], signal_id=signal_id,
           report_id=row["report_id"], lot_id=row["lot_id"], done_at=realtime.iso(done["task_done_at"]),
           actor=accounts.logged_actor(actor))
    return report_view(conn, run["run_id"], done)


def _apply(conn, run: dict, fact_id: str, found: list[scopes.FactScope], note: str) -> dict:
    """`facts.apply` over each scope (a correction's own and the replaced fact's), each case once in the result: a
    live case re-evaluated twice keeps its version the second time (cases.evaluate_case), a flag is one per case and
    fact."""
    out = {"cases": [], "flagged": [], "reassessed": []}
    for one in found:
        done = facts.apply(conn, run, fact_id, one, note=note)
        seen = {c["case_id"] for c in out["cases"]}
        out["cases"] += [c for c in done["cases"] if c["case_id"] not in seen]
        out["flagged"] += [c for c in done["flagged"] if c not in out["flagged"]]
        out["reassessed"] += [c for c in done["reassessed"] if c not in out["reassessed"]]
    return out


def withdraw(conn, run: dict, actor: dict, signal_id: str) -> dict:
    """The reporter withdraws the report before confirmation (D-15): WITHDRAWN with `withdrawn_at`, no operational
    effect, the history kept. Another seat is 403; a confirmed, dismissed or withdrawn report 409 wrong_state (after
    confirmation a correction is a new report naming it)."""
    row = _current(conn, run["run_id"], signal_id, lock=True)
    signal_id = row["signal_id"]
    if (actor.get("role"), actor.get("branch")) != (row["reporter_role"], row["reporter_branch"]):
        raise ApiError(403, "forbidden", "Only the person who filed this report can withdraw it")
    if row["state"] != OPEN:
        raise ApiError(409, "wrong_state", f"Report is {row['state']}", state=row["state"])
    db.run(conn, "UPDATE signals SET state = 'WITHDRAWN', withdrawn_at = now() WHERE run_id = :r AND signal_id = :s",
           r=run["run_id"], s=signal_id)
    db.log(conn, run["run_id"], run["day"], "report_withdrawn", row["case_id"], signal_id=signal_id,
           report_id=row["report_id"], kind=row["kind"], actor=accounts.logged_actor(actor))
    return report_view(conn, run["run_id"], _row(conn, run["run_id"], signal_id))


def dismiss(conn, run: dict, actor: dict, signal_id: str, reason=None) -> dict:
    """The owner dismisses a filed report with an optional reason (D-15): DISMISSED with `dismissed_by`,
    `dismissed_at` and `dismiss_reason`; never applied, the history kept. Another seat is 403; a report no longer READ
    409 wrong_state."""
    if reason is not None and not isinstance(reason, str):
        raise ApiError(400, "bad_request", "reason must be text")
    row = _current(conn, run["run_id"], signal_id, lock=True)
    signal_id = row["signal_id"]
    if not _owns(actor, row):
        raise ApiError(403, "forbidden", "Only the report's owner can dismiss it")
    if row["state"] != OPEN:
        raise ApiError(409, "wrong_state", f"Report is {row['state']}", state=row["state"])
    why = (reason or "").strip() or None
    db.run(conn, "UPDATE signals SET state = 'DISMISSED', dismissed_at = now(), dismissed_by = :by, dismiss_reason = "
                 ":why WHERE run_id = :r AND signal_id = :s", by=_account(actor), why=why, r=run["run_id"],
           s=signal_id)
    db.log(conn, run["run_id"], run["day"], "report_dismissed", row["case_id"], signal_id=signal_id,
           report_id=row["report_id"], kind=row["kind"], reason=why, actor=accounts.logged_actor(actor))
    return report_view(conn, run["run_id"], _row(conn, run["run_id"], signal_id))
