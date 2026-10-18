"""Detect, then evaluate on demand: the morning screen's flags on a public set (spec §5 Public dataset; 05.2-CONTEXT
D-23..D-25, D-28).

On a public set whose reference settings say `evaluation.on_demand`, the morning screen (`cases.open_cases`) runs the
same ESL-02 screen as before (api/screen.py: no search, no Gemini call) and writes one `case_flags` row per lot that
qualifies, instead of opening a case with a full engine search. A flag holds the lot, its key (the run's case counter
in screen order, kept by the lot on a later morning) and the screen's own figures: rule (a) and (b), Keep waste, the
stock at the next step day, that day and the usable runs. A lot the screen could not judge for lack of data (a
HorizonError) is flagged with `missing_data` set, never skipped (D-28). The synthetic set keeps its eager opening.

Detection is not a recommendation (D-24): a flag reads as "Needs evaluation" with the screen's figures and nothing
else. Its card and detail carry `needs_evaluation` true and `risk`, and no plan, amount, deadline, owner, result or
fact check.

The engine runs when someone opens the item (D-25): `open_from_flag` (POST /api/cases/{key}/evaluate, inside the
app's idempotent write under the run lock) opens the case from its flag, with the flag's key and one open case per
lot, logs `opened` with the screen's figures, and evaluates it through `cases.evaluate_case`. That is the only path a
case's version takes, so the version rules hold as for every case (a new version cancels a pending approval and
releases its bookings) and the result comes from `evaluate.run_engine`'s shared cache, keyed by the full input and
its version: the same item opened in another workspace is served from the cache with no engine run.

A workspace reads its template's flags while its day is the template's (api/overlay.py `flags_run`): flags are never
cloned, so a workspace costs nothing per flag. Nothing here reads physical stock or hidden demand.
"""
from __future__ import annotations

from . import cases, db, eligibility, overlay, refdata, simplerule, stepkeys
from .errors import ApiError

GONE = "Lot sold out or expired before evaluation"
RISK_KEYS = ("a", "b", "keep_waste", "stock_at_step", "step_day")


def on_demand(ref) -> bool:
    """Whether this set's morning screen writes flags instead of opening cases (reference `evaluation.on_demand`)."""
    return ref.kind == "public" and bool((ref.settings.get("evaluation") or {}).get("on_demand"))


FLAG_COLUMNS = ("run_id", "day", "lot_id", "key", "branch", "sku", "a", "b", "keep_waste", "stock_at_step",
                "step_day", "usable_runs", "missing_data", "tag", "origin")


def write_flags(conn, run: dict, candidates: list[tuple[dict, dict]]) -> list[str]:
    """One case_flags row per (lot row, screen result) in the screen's order; returns their keys.

    A lot keeps the key of its earlier flag in this run; a new lot takes the run's next case number (`cases.next_keys`,
    `cases.next_key`'s rules). A lot already flagged this morning is left as it is, so a second screen of the same
    morning writes nothing. Set-based (quick 261017-p52): one read of this morning's flags, one of the earlier keys,
    one counter reservation and one multi-row INSERT per chunk, instead of about four round trips per flag."""
    run_id, day = run["run_id"], run["day"]
    ids = [lot["lot_id"] for lot, _ in candidates]
    if not ids:
        return []
    today = {r["lot_id"] for r in db.rows(conn, "SELECT lot_id FROM case_flags WHERE run_id = :r AND day = :d "
                                                "AND lot_id = ANY(:ids)", r=run_id, d=day, ids=ids)}
    earlier = {r["lot_id"]: r["key"] for r in db.rows(
        conn, "SELECT DISTINCT ON (lot_id) lot_id, key FROM case_flags WHERE run_id = :r AND lot_id = ANY(:ids) "
              "ORDER BY lot_id, day DESC", r=run_id, ids=ids)}
    todo = []
    for lot, found in candidates:  # a lot listed twice is written once, as the old per-row check did
        if lot["lot_id"] in today:
            continue
        today.add(lot["lot_id"])
        todo.append((lot, found))
    new = iter(cases.next_keys(conn, run, [lot for lot, _ in todo if lot["lot_id"] not in earlier]))
    keys = [earlier[lot["lot_id"]] if lot["lot_id"] in earlier else next(new) for lot, _ in todo]
    db.insert_rows(conn, "case_flags", FLAG_COLUMNS, [
        (run_id, day, lot["lot_id"], k, lot["branch"], lot["sku"], found.get("a"), found.get("b"),
         found.get("keep_waste"), found.get("stock_at_step"), found.get("step_day"),
         db.js(list(found.get("usable_runs") or [])), found.get("missing"), lot.get("tag"), lot.get("origin"))
        for k, (lot, found) in zip(keys, todo)], casts={"usable_runs": "jsonb"})
    return keys


def reopens(conn, run: dict, prev: dict, lot_row: dict, seen: dict | None = None) -> list[str]:
    """The public screen's rule for a lot whose latest case a person closed (07-CONTEXT D-24, D-28; 07-12): the same
    `closing.reopen_allowed` as every path, without the Next-day thresholds (product mode reassesses on new data,
    confirmed evidence or Reassess), so a closed lot that still qualifies is never excluded and never reopened on
    unchanged inputs; when its inputs changed the screen opens its successor (never a flag of the closed case's key)."""
    from . import closing  # it reads cases at import
    return closing.reopen_allowed(conn, run, prev, lot_row, next_day=False, seen=seen)


def find(conn, run: dict, key: str) -> dict | None:
    """The flag with this key that the run reads this morning (`overlay.flags_run`), or None."""
    return db.one(conn, "SELECT * FROM case_flags WHERE run_id = :f AND day = :d AND key = :k",
                  f=overlay.flags_run(conn, run), d=run["day"], k=key)


def risk(row: dict) -> dict:
    """The screen's own figures for a flagged lot (api/screen.py); null where the screen had missing data."""
    return {k: row[k] for k in RISK_KEYS}


def flag_card(ref, row: dict) -> dict:
    """A flag as a board card (04-API-CONTRACT Card): every card key present, `needs_evaluation` true and `risk`, and
    no plan, amount, deadline or owner (D-24). It reads as a New item until someone opens it."""
    product = ref.products[row["sku"]]
    return {
        "case_id": row["key"], "key": row["key"], "tag": row["tag"], "origin": row["origin"],
        "state": "OPEN", "column": cases.COLUMNS["OPEN"],
        "sku": row["sku"], "product": ref.names[row["sku"]], "zone": product.zone,
        "source": row["branch"], "dest": None, "lot_id": row["lot_id"],
        "suggested": None, "suggested_key": None, "at_risk_units": None, "at_risk_kg": None, "at_risk_value": None,
        "est_benefit": None,
        "due": None, "decide_by": None, "markdown_step": None, "act_before": None, "wait": None, "rec_version": None,
        "next_owner": [], "decisions": {}, "hold": None,
        "badges": [{"kind": "missing_data", "text": "Missing data"}] if row["missing_data"] else [],
        "reason": row["missing_data"], "waiting_until_day": None,
        "selected_plan": None,  # no plan, so no selection (07-CONTEXT D-23)
        "lot_status": None, "predecessor": None, "successor": None,  # no closed plan, no link (07-CONTEXT D-24)
        "needs_evaluation": True, "risk": risk(row),
        **stepkeys.flag_keys(row),  # Trigger, Needs evaluation, Evaluate (06.1 D-24, D-54)
    }


def flag_detail(conn, run: dict, row: dict) -> dict:
    """GET /api/cases/{key} for a flag the engine has not evaluated: the card, the lot as the workspace reads it, the
    stage-1 eligibility (which destinations stage 1 keeps, never a plan) and every case-detail key with no result,
    no fact checks, no summary and no approvals (D-24); its `comparison` is `not_computed`, the case's shape before
    any engine run (06.1 D-27)."""
    ref = refdata.get(run["dataset_version"])
    pseudo = {"lot_id": row["lot_id"], "source_branch": row["branch"], "sku": row["sku"], "tag": row["tag"]}
    lot = cases.lot_meta(conn, run, pseudo)
    return {
        **flag_card(ref, row),
        "lot": lot,
        "eligibility": eligibility.summary(conn, run, pseudo, lot),
        "adopted": [], "adopted_ended": [], "requests": [],
        "result": None, "fact_checks": [], "summary": "", "engine_input": None,
        "comparison": simplerule.empty(simplerule.NOT_COMPUTED),  # the case's shape: no engine run yet (06.1 D-27)
        "versions": cases.base_versions(run, ref),
        "approvals": {"round": 0, "required": [], "decisions": {}, "hold": None, "resources": [], "next_owner": []},
    }


def open_from_flag(conn, run: dict, key: str) -> dict:
    """POST /api/cases/{key}/evaluate, inside the app's idempotent write under the run lock: the case detail after
    the engine has evaluated the item (D-25).

    - A case with this key already exists in the workspace: its detail, evaluated first when it is live and has no
      active version (a missing-data case tries again). An evaluated case is never searched again here.
    - Else the flag this workspace reads: 404 not_found without one; 409 gone when its lot has no stock on the record
      any more or is past expiry (nothing is opened). Otherwise the case opens with the flag's key (state OPEN, today
      as opened_day, missing_data from the flag), `opened` is logged with the screen's figures, and `cases.evaluate_case`
      runs the engine through the shared cache and the version rules.
    Two requests opening the same item at once: the second waits on the first one's row, then answers its case."""
    case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c FOR UPDATE", r=run["run_id"], c=key)
    if case is not None:
        if case["active_rec_version"] is None and case["state"] in cases.LIVE_STATES:
            cases.evaluate_case(conn, run, case)
        return cases.detail(conn, run, key)
    flag = find(conn, run, key)
    if flag is None:
        raise ApiError(404, "not_found", f"No case {key}")
    lot = overlay.lot(conn, run, "shelfshift", flag["lot_id"])
    if lot is None or lot["qty"] <= 0 or lot["expiry_day"] < run["day"]:
        raise ApiError(409, "gone", GONE)
    opened = db.one(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, tag, state, opened_day, "
                          "missing_data) VALUES (:r, :c, :c, :l, :b, :s, :t, 'OPEN', :d, :why) ON CONFLICT DO NOTHING "
                          "RETURNING case_id", r=run["run_id"], c=key, l=flag["lot_id"], b=flag["branch"],
                    s=flag["sku"], t=lot["tag"], d=run["day"], why=flag["missing_data"])
    if opened is None:  # another request opened this item (or the lot has a live case) and committed first
        other = db.one(conn, "SELECT case_id FROM cases WHERE run_id = :r AND (case_id = :c OR (lot_id = :l AND "
                             "state NOT IN ('CLOSED', 'REJECTED'))) ORDER BY case_id = :c DESC LIMIT 1",
                       r=run["run_id"], c=key, l=flag["lot_id"])
        if other is None:
            raise ApiError(409, "wrong_state", "This item changed while it was opened. Try again.")
        return cases.detail(conn, run, other["case_id"])
    db.log(conn, run["run_id"], run["day"], "opened", key, lot_id=flag["lot_id"], a=flag["a"], b=flag["b"],
           usable_runs=flag["usable_runs"], step_day=flag["step_day"], keep_waste=flag["keep_waste"],
           stock_at_step=flag["stock_at_step"], reason=flag["missing_data"], on_demand=True)
    cases.evaluate_case(conn, run, cases.case_row(conn, run["run_id"], key))
    return cases.detail(conn, run, key)
