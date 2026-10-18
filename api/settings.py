"""Policy settings per sandbox (04-CONTEXT D-09, D-23; 04-API-CONTRACT "Settings").

HQ may change three Policy fields of its workspace, and a store manager reads them (06.1 D-53): the ones
`api/settings_bounds.json` lists (`config.SETTINGS_BOUNDS`). The file decides what is writable and each field's
inclusive bound (`{min, max}` or `{allowed}`); nothing here holds a bound. A unit names no currency in the file:
"{currency}" in it is filled in from the reference data's currency when a response or a refusal is written (IN-07). A write follows every write rule: an
Idempotency-Key (the route's `_write`), the run lock FOR UPDATE with the run version checked, and all changes or none.

An accepted write stores `run_policy` rows for this run only (a clone copies them like any per-run table, so the
template, the reference data and every other sandbox are untouched), bumps the run version, applies hold expiry,
then re-evaluates every live case through `cases.evaluate_case`: a case whose version changes while PENDING_APPROVAL
has its approval cancelled and its holds released, as advance-day does. `settings_changed` is logged for the run and
for each live case in the same transaction, before that case's new version. The engine adds no ceiling of its own:
the new Policy is checked with `engine.adjust.validate_input` on a live case's input, and an InputError is the plain
400 `bad_input` (errors.engine_error), never a limit conflict.
"""
from __future__ import annotations

import copy
import math
from dataclasses import fields

from engine.adjust import InputError, validate_input
from engine.model import Policy

from . import accounts, bookings, cases, config, db, inputs, refdata
from .errors import ApiError, engine_error

HQ_ONLY = "Only HQ can change policies."  # (proposed) 06.1 contract: only HQ writes policies (D-53)
WHOLE = {f.name for f in fields(Policy) if type(f.default) is int}  # Policy fields that hold a whole number


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, float) else str(x)


def _refuse(field: str, bound: dict) -> ApiError:
    """400 out_of_bounds naming the field and its bound from the file: "<field> must be between <min> and <max> <unit>"
    or "<field> must be one of <allowed>"."""
    if "allowed" in bound:
        text = f"{field} must be one of {', '.join(_num(v) for v in bound['allowed'])}"
    else:
        kind = "a whole number between" if field in WHOLE else "between"
        unit = f" {bound['unit']}" if bound.get("unit") else ""
        text = f"{field} must be {kind} {_num(bound['min'])} and {_num(bound['max'])}{unit}"
    return ApiError(400, "out_of_bounds", text, field=field, bound=copy.deepcopy(bound))


def bounds(currency: str) -> dict:
    """The bounds file's bounds, each unit with "{currency}" filled in ("{currency}/kg" -> "USD/kg") (IN-07)."""
    out = copy.deepcopy(config.SETTINGS_BOUNDS)
    for bound in out.values():
        if "unit" in bound:
            bound["unit"] = bound["unit"].replace("{currency}", currency)
    return out


def _currency(run: dict) -> str:
    return refdata.get(run["dataset_version"]).currency


def checked(field: str, value, currency: str):
    """The value to store for `field`, or ApiError: not_writable for a field the bounds file does not list;
    out_of_bounds for a bool, a non-number, a non-finite number, a fraction in a whole-number field, or a value
    outside the inclusive bound (named in `currency`)."""
    bound = bounds(currency).get(field)
    if bound is None:
        raise ApiError(400, "not_writable", f"{field} is not a setting you can change here", field=field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _refuse(field, bound)
    try:
        if not math.isfinite(value):
            raise _refuse(field, bound)
        if field in WHOLE:
            if value != int(value):
                raise _refuse(field, bound)
            value = int(value)
        else:
            value = float(value)
    except OverflowError:  # a whole number too large for a float is outside every bound
        raise _refuse(field, bound) from None
    inside = value in bound["allowed"] if "allowed" in bound else bound["min"] <= value <= bound["max"]
    if not inside:
        raise _refuse(field, bound)
    return value


def read_settings(conn, run: dict) -> dict:
    """{version, values, bounds}: the run version, this sandbox's current value of each writable field, the bounds."""
    policy = inputs.sandbox_policy(conn, run)
    return {"version": run["version"],
            "values": {field: getattr(policy, field) for field in config.SETTINGS_BOUNDS},
            "bounds": bounds(_currency(run))}


def _validate(conn, run: dict, live: list[dict]) -> None:
    """The new Policy through the engine's own input check, on the first live case whose input builds."""
    for case in live:
        try:
            inp = cases.current_input(conn, run, case)
        except LookupError:  # the lot is gone; evaluate_case and the morning close it
            continue
        try:
            validate_input(inp)
        except InputError as exc:
            raise engine_error(exc) from None  # 400 bad_input with the engine's own message
        return


def write_settings(conn, run: dict, actor: dict, changes, expected_version: int) -> dict:
    """Write `changes` ({field: value}) to this sandbox's Policy, all or none (D-09, D-23). Returns the read_settings
    shape plus `reevaluated`, the live case ids re-evaluated in case_id order. Only HQ writes (06.1 D-53): the role
    is the session row's (app._actor), never a body field; a store manager is 403 before anything is read."""
    if actor.get("role") != "hq":
        raise ApiError(403, "forbidden", HQ_ONLY)
    run = db.lock_run(conn, run["run_id"], mode="UPDATE", version=expected_version)
    if not isinstance(changes, dict) or not changes:
        raise ApiError(400, "bad_request", "Name at least one setting to change")
    currency = _currency(run)
    clean = {field: checked(field, changes[field], currency) for field in sorted(changes)}  # the first bad one named
    before = read_settings(conn, run)["values"]
    for field, value in clean.items():
        db.run(conn, "INSERT INTO run_policy (run_id, field, value) VALUES (:r, :f, CAST(:v AS jsonb)) "
                     "ON CONFLICT (run_id, field) DO UPDATE SET value = EXCLUDED.value",
               r=run["run_id"], f=field, v=db.js(value))
    bookings.expire_holds(conn, run)  # an hour that is up ends its round first, as advance-day does
    live = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = ANY(:s) ORDER BY case_id",
                   r=run["run_id"], s=list(cases.LIVE_STATES))
    _validate(conn, run, live)
    run = db.one(conn, "UPDATE demo_runs SET version = version + 1 WHERE run_id = :r RETURNING *", r=run["run_id"])
    changed = {field: {"from": before[field], "to": value} for field, value in clean.items()}
    db.log(conn, run["run_id"], run["day"], "settings_changed", changes=changed, version=run["version"],
           by=actor.get("role"), branch=actor.get("branch"))
    for case in live:
        db.log(conn, run["run_id"], run["day"], "settings_changed", case["case_id"], changes=changed,
               version=run["version"], actor=accounts.logged_actor(actor))
        cases.evaluate_case(conn, run, case)
    return {**read_settings(conn, run), "reevaluated": [c["case_id"] for c in live]}
