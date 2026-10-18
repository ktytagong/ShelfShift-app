"""Independent incoming API card baseline for the Phase10 web/API merge.

Copied from api/cases.py at bfcfcba144225a2097eae547f19e8e7d100e4511, with only the function
name and package-relative import rewritten. The original quick261017-tuf oracle
remains unchanged. This baseline is not derived from the optimized merged path.
"""
from api import refdata, realtime, reviewflags, stepkeys
from api.cases import (COLUMNS, DUE_STATES, TERMINAL, active_rec, badges, decided_roles, due,
                       execution_state, hold_info, lot_meta, next_owner, parse_plan, wait_view)


def upstream_card(conn, run: dict, case: dict, requests: list[dict], now=None) -> dict:
    """One board card, every contract key present (04-API-CONTRACT Card), with the step, stage, owner, next action
    and blocked keys computed once by api/stepkeys.py (06.1 D-23, D-24, D-54), and `lot_status`, `predecessor` and
    `successor` (07-11: a rejected or cancelled plan's lot stays visible while at risk, api/closing.py; D-24). `requests` may hold other cases'
    (stored rows or request views). `now` is the database's now() of the read (the board reads it once); a card
    given stored rows with a deadline and no `now` reads it itself, so the badge's request rule (D-06) uses one clock."""
    from api import closing, commitment, selection  # they read this module (through scope, or directly): imported here
    ref = refdata.get(run["dataset_version"])
    rec = active_rec(conn, case)
    result = rec["result"] if rec else None
    key, _, chosen = selection.plan_in_force(case, rec)  # the selection bound to the version, else the recommended
    plan = parse_plan(key) if key else None
    by = selection.decide_by(case, rec)  # the plan in force's own deadline (07 review G, D-32)
    best = next(r for r in result["options"] if r["key"] == key) if result else None
    keep = next((r for r in result["options"] if r["key"] == "keep"), None) if result else None
    decided = decided_roles(conn, case)
    holds = hold_info(conn, case)
    meta = lot_meta(conn, run, case)
    product = ref.products[case["sku"]]
    mine = [r for r in requests if r["case_id"] == case["case_id"]]
    if now is None and any(r.get("due_at") is not None and "overdue" not in r for r in mine):
        now = realtime.db_now(conn)
    steps = stepkeys.keys(conn, run, case, plan, decided, mine, execution_state(conn, case))
    flags = reviewflags.open_for(conn, case["run_id"], case["case_id"]) if case["state"] in reviewflags.STATES else []
    lot_holds = commitment.holds_on(conn, run, case) if case["state"] not in TERMINAL else []
    required = commitment.required_ids(case, rec)
    if case["state"] in commitment.COMMIT_AHEAD:  # the commitment rule's blocks (07-CONTEXT D-12, D-13)
        extra = [k for k, on in (("required_check", required), ("lot_hold", lot_holds)) if on]
        steps = {**steps, "blocked": steps["blocked"] or bool(extra), "blocked_reasons": steps["blocked_reasons"] + extra}
    return {
        "case_id": case["case_id"], "key": case["key"], "tag": case["tag"], "origin": meta["origin"],
        "state": case["state"], "column": COLUMNS[case["state"]],
        "sku": case["sku"], "product": ref.names[case["sku"]], "zone": product.zone,
        "source": case["source_branch"], "dest": plan.dest if plan else None, "lot_id": case["lot_id"],
        "suggested": (best["label"] if chosen else result["recommended_label"]) if result else None,  # in force
        "suggested_key": key,
        "selected_plan": selection.card_view(chosen),  # "Selected plan · {option}" (07-CONTEXT D-23)
        "at_risk_units": keep["wasted_units"] if keep else None,
        "at_risk_value": round(keep["wasted_units"] * product.price, 2) if keep else None,
        "est_benefit": best["delta_vs_keep"] if best else None,
        "due": due({"decide_by": by}, ref) if case["state"] in DUE_STATES else None,
        "decide_by": by, "markdown_step": (result or {}).get("markdown_step"),
        "act_before": (result or {}).get("act_before"),
        "wait": wait_view(result, rec["engine_input"] if rec else None),
        "rec_version": case["active_rec_version"],
        "next_owner": next_owner(case, plan, decided),
        "decisions": decided, "hold": holds,
        "badges": badges(case, rec, holds, mine, now, flags, lot_holds, required),
        "reason": case["reason"] or case["missing_data"], "waiting_until_day": case["waiting_until_day"],
        # closing never hides at-risk stock (07-CONTEXT D-24; api/closing.py label): "Needs decision", "Awaiting
        # verification" or None on a plan a person closed; the case it follows and the case that follows it (07-12)
        "lot_status": closing.label(conn, run, case) if closing.is_closed_by_person(case) else None,
        "predecessor": case.get("predecessor"), "successor": closing.successor(conn, case),  # the links (07-12)
        **steps,
    }

