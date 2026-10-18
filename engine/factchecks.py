"""Fact checks: which uncertain fact would change this decision (spec §6 Fact checks, rev 6.3.0).

The engine flags and prices suspect facts. It does not decide what to do about
them; the agent does. Possible phantom stock is one fact per branch, whose
alternative sets every uncounted lot at that branch to 0. Stale counts and
estimated expiries are one fact per lot. Every fact carries lot_ids, the lots
its alternative changes; lot_id is the first of them, the lot a count request names.

A fact is priced by its decision regret on the score (net minus waste price × kg, `Scored.score`), at the
waste price in force, so a fact that changes the pick only because of waste still counts. Its parts are reported
apart: the signed net revenue difference (money), the signed waste-policy difference (a policy value, not
cash) and the signed kg difference. Following the 02-CONTEXT D-23 output contract, pick_changed says whether
the pick changes at all, and decision_regret and the three parts report the true trade-off whenever it does,
even below policy.min_regret; regret and unserved_units stay gated on changes_plan. Following 02-CONTEXT D-29 (review
CR-02), D-35 and reviews IN-11 and IN-14: pick_feasible_if_alt is True when check_plan(alt, p0) gives no reason, or only
the covered-markdown reason for p0's percent (D-35). In that case the schedule already gives the same outcome, though
check_plan, and so send, approve and prepare, would refuse the manual step, or for a transfer the whole plan, on that
record. That covered case goes through the ordinary min_regret gate. Any other reason makes the pick not allowed, and
such a fact always requests a count (changes_plan), whatever min_regret, the approved exception to engine-2 parity
(D-29). regret and decision_regret are never negative.

Following 02-CONTEXT D-32, the checks are ordered not-allowed first (pick_feasible_if_alt False), then by highest
regret, then by fact_id, and the cap of MAX_CHECKS never drops a not-allowed check: when more checks are not allowed
than the cap, all of them are returned and only allowed checks are cut, so the list is longer than MAX_CHECKS exactly
then. Today's three rules raise at most two not-allowed checks: a recommended plan breaks a limit only through the
case lot's quantity or expiry, and the case lot gets at most one quantity fact and one expiry fact.
"""
from __future__ import annotations

from dataclasses import replace

from .adjust import InputError, coerce_input, effective, validate_input
from .canonical import rec_version
from .model import ENGINE_VERSION, TRANSFER, EngineInput, Plan
from .limits import candidates, check_plan, covered_reason
from .scoring import Scored, best_base, cents
from .simulate import simulate
from .model import PHANTOM_MIN_FORECAST, STALE_AFTER_DAYS, STALE_FACTOR  # engine-2 names kept (IN-03, D-20); logic reads the Policy fields

MAX_CHECKS = 2


def _check_result(eff: EngineInput, result) -> None:
    """Refuse a result that is not the one `evaluate` returned for this input (review WR-02).

    The result must be a dict from this engine version whose effective_version is rec_version of `eff`, the
    effective input. Anything else (a stored result from another engine, a what-if's result, a result for
    another case, a hand-built dict) is an InputError, never a silently shorter fact list.
    """
    if not isinstance(result, dict):
        raise InputError(f"fact_checks needs the result dict evaluate returned, got {type(result).__name__}")
    if result.get("engine_version") != ENGINE_VERSION:
        raise InputError(f"result engine_version {result.get('engine_version')!r} is not this engine's "
                         f"{ENGINE_VERSION!r}")
    if result.get("effective_version") != rec_version(eff):
        raise InputError("result effective_version does not match this input: fact_checks needs the result "
                         "evaluate returned for this same input")


def _record_plans(eff: EngineInput, result: dict) -> tuple[Plan, Plan | None]:
    """The record's plan and the best transfer, read from the case result (no simulation).

    p0 is the candidate whose key is result["recommended"]. The best transfer is the plan of the first TRANSFER
    row in result["options"]: the rows are the group-best plans sorted by rank_key, so the first transfer row is
    the best transfer overall. None when no transfer passed the limits. Only candidates(eff) runs (limits only).
    A key that is not one of this input's candidates is an InputError naming it, never a bare KeyError.
    """
    plans, _ = candidates(eff)
    by_key = {p.key: p for p in plans}

    def plan(key) -> Plan:
        if key not in by_key:
            raise InputError(f"result plan key {key!r} is not a candidate of this input")
        return by_key[key]

    p0 = plan(result.get("recommended"))
    row = next((o for o in result.get("options", ()) if o.get("kind") == TRANSFER), None)
    return p0, (plan(row.get("key")) if row is not None else None)


def suspect_facts(inp: EngineInput, rec: Plan, best_transfer: Plan | None) -> list[dict]:
    """Deterministic rules on the snapshot, for the branches this plan moves stock from or relies on.

    Branches are taken in a fixed order, the source then the destination. Possible
    phantom stock needs an explicit zero in sales_yesterday (a missing row is not zero
    sales) and a base forecast today of at least policy.phantom_min_forecast; it is one fact
    for the branch, covering every lot on record there that was not counted at this
    snapshot. Lots it covers get no stale fact (phantom takes precedence). A lot that
    has never been verified (last_verified_day None) counts as stale. A lot counted at
    this snapshot (last_verified_day == today, D-12) is neither phantom nor stale: a
    confirmed count always ends the suspicion. The engine judges only this snapshot;
    the app keeps each flag's history (Phase 4/7), and a flag never blocks anything.
    """
    dest = rec.dest or (best_transfer.dest if best_transfer else None)
    order = [inp.source] + ([dest] if dest and dest != inp.source else [])
    sales = dict(inp.sales_yesterday)
    base_today = {f.branch: f.base for f in inp.forecasts if f.day == inp.today}
    in_scope = sorted((l for l in inp.lots if l.branch in order and l.qty > 0), key=lambda l: l.lot_id)
    uncounted = [l for l in in_scope if l.last_verified_day != inp.today]  # counted at this snapshot: not suspect
    facts: list[dict] = []
    covered_ids: set[str] = set()
    for branch in order:
        # an explicit zero row only: a branch with no sales row is unknown data, not zero sales (WR-06)
        if branch not in sales or sales[branch] != 0 or base_today.get(branch, 0.0) < inp.policy.phantom_min_forecast:
            continue
        covered = [l for l in uncounted if l.branch == branch]
        if not covered:
            continue
        covered_ids.update(l.lot_id for l in covered)
        facts.append({"fact_id": f"{branch}:phantom", "kind": "PHANTOM_STOCK", "lot_id": covered[0].lot_id,
                      "lot_ids": [l.lot_id for l in covered], "branch": branch,
                      "recorded_value": sum(l.qty for l in covered), "alt_value": 0.0})
    for lot in uncounted:
        if lot.lot_id in covered_ids:
            continue
        if lot.last_verified_day is None or inp.today - lot.last_verified_day > inp.policy.stale_after_days:
            facts.append({"fact_id": f"{lot.lot_id}:stale", "kind": "STALE_COUNT", "lot_id": lot.lot_id,
                          "lot_ids": [lot.lot_id], "branch": lot.branch, "recorded_value": lot.qty,
                          "alt_value": round(lot.qty * inp.policy.stale_factor, 2)})
    for lot in in_scope:
        if lot.expiry_source == "ESTIMATED":
            facts.append({"fact_id": f"{lot.lot_id}:expiry", "kind": "ESTIMATED_EXPIRY", "lot_id": lot.lot_id,
                          "lot_ids": [lot.lot_id], "branch": lot.branch, "recorded_value": lot.expiry_day,
                          "alt_value": lot.expiry_day - 1})
    return facts


def _with_alt(inp: EngineInput, fact: dict) -> EngineInput:
    """Set the fact's alternative on every lot in lot_ids (all uncounted lots at the branch for phantom stock)."""
    field = "expiry_day" if fact["kind"] == "ESTIMATED_EXPIRY" else "qty"
    targets = set(fact["lot_ids"])
    return replace(inp, lots=tuple(replace(l, **{field: fact["alt_value"]}) if l.lot_id in targets else l
                                   for l in inp.lots))


def fact_checks(inp: EngineInput, result: dict) -> list[dict]:
    """Price each suspect fact by its decision regret. At most MAX_CHECKS, not-allowed checks first; the cap never drops a not-allowed check.

    p0 is the record's pick and p1 the best plan if the alternative is true. Both are simulated under the
    alternative and scored at the waste price in force. The keys follow the 02-CONTEXT D-23 output contract, one
    meaning per key:
    - pick_changed = p1.key != p0.key, whatever the size of the change;
    - pick_feasible_if_alt is True when check_plan(alt, p0) gives no reason, or only the covered-markdown reason for
      p0's percent (D-35). In that case the schedule already gives the same outcome, though check_plan, and so send,
      approve and prepare, would refuse the manual step, or for a transfer the whole plan, on that record (reviews IN-11
      and IN-14). The covered reason is the text check_plan itself builds, covered_reason(alt, p0.pct). It is
      check_plan's answer (D-29), never membership in the alternative's candidates, whose shortlist grid can differ. Any
      other reason makes the pick not allowed; the covered case goes through the ordinary min_regret gate;
    - changes_plan = pick_changed and (not pick_feasible_if_alt or the unrounded score regret
      max(0, score(p1 | alt) - score(p0 | alt)) reaches policy.min_regret). D-06's gate, plus D-29's approved
      exception to engine-2 parity: a fact that makes the recommended plan break a hard limit always requests a count.
      It alone decides whether a count is requested;
    - net_revenue_difference = net(p1 | alt) - net(p0 | alt), signed money, never called regret;
    - waste_policy_difference = waste price x (kg(p0 | alt) - kg(p1 | alt)), signed, a policy value, not cash;
    - waste_kg_difference = kg(p0 | alt) - kg(p1 | alt), signed;
    - decision_regret = max(0.0, the rounded sum of the two parts above) when pick_changed (each part rounded to
      cents, review WR-03), else 0.0. Never gated by min_regret and never negative (D-29): when p0 breaks a hard limit
      under the alternative it can outscore p1, and that negative sum is reported as 0.0. So it equals
      net_revenue_difference + waste_policy_difference to the cent whenever that sum is >= 0;
    - regret = decision_regret when changes_plan, else 0.0
      (engine-2; the second sort key after pick_feasible_if_alt, D-32);
    - unserved_units = the demand at the fact's branch p0 leaves that p1 would serve, when changes_plan, else 0.0.
    The three signed figures are not gated: they are the true values whenever the pick changes, and 0 when it
    doesn't (p1 = p0). At a waste price of 0 the score is the net, so decision_regret is the net difference whenever
    that difference is >= 0.

    Order and cap (D-32): the checks are sorted by (pick_feasible_if_alt, -regret, fact_id), so checks whose
    alternative makes the recommended plan not allowed come first (False sorts first), then the highest regret, then
    fact_id. The return is checks[:max(MAX_CHECKS, the number of not-allowed checks)]: the cap never drops a
    not-allowed check, and every check past MAX_CHECKS is a not-allowed one. With today's three rules at most two
    checks are not allowed, so with MAX_CHECKS 2 a real case never returns more than two.

    The record's plan and the best transfer come from `result`, so the caller passes the result of the same input
    (as `evaluate` returns it); no second base search runs. Each fact costs one search plus one simulation (spec
    rev 6.3.0 §6 Fact checks, budget): the search under the alternative, which already holds p1's base simulation,
    plus the record's plan simulated under the alternative (review IN-03), plus one limit check of the record's plan
    (check_plan, D-29); for a transfer that check simulates Keep at its destination only. A result from another engine
    version or for another effective input
    (its effective_version differs) is an InputError (review WR-02). It computes on the coerced input (WR-07).
    """
    inp = coerce_input(inp)
    validate_input(inp)
    eff = effective(inp)
    _check_result(eff, result)
    p0, best_transfer = _record_plans(eff, result)
    checks = []
    for fact in suspect_facts(eff, p0, best_transfer):
        alt = _with_alt(eff, fact)
        alt_plans, _ = candidates(alt)
        best = best_base(alt, alt_plans)  # only the winning base simulation is used under the alternative
        p1, p1_alt = best.plan, best.sims["base"]  # review IN-03: p1's base simulation comes from its search
        p0_alt = simulate(alt, p0, "base")  # the one simulation this fact adds
        lost = lambda sim: sum(v for (b, _), v in sim.lost_by.items() if b == fact["branch"])  # noqa: E731
        unserved = max(0.0, lost(p0_alt) - lost(p1_alt))  # demand the record's plan leaves that the best plan would serve
        # both plans under the alternative, scored with the input's waste price, so the formula lives in Scored
        s0 = Scored(p0, {"base": p0_alt}, alt.policy.waste_price, alt.product.kg_per_unit)
        s1 = Scored(p1, {"base": p1_alt}, alt.policy.waste_price, alt.product.kg_per_unit)
        # decision regret on the score (net minus waste price × kg, Scored.score)
        regret = max(0.0, s1.score() - s0.score())
        picked = p1.key != p0.key  # D-23: the pick changes, whatever the size
        # D-29: check_plan's reasons for the record's pick if the fact is true. The alternative is the
        # effective input with the fact applied and nothing left to apply, so its applied_overrides marker is cleared
        # on this internal copy only: check_plan's what-if refusal guards the approval path's raw input (D-18 WR-01),
        # and fact checks also run on a what-if's input (Phase 4 workflow.whatif).
        reasons = check_plan(replace(alt, applied_overrides=()), p0)
        # D-35: a markdown, or a transfer's rest, that the schedule already covers under the fact is a redundancy, not a
        # broken limit (the schedule already gives the same outcome, though check_plan, and so send, approve and
        # prepare, would refuse the manual step, or for a transfer the whole plan, on that record; reviews IN-11 and
        # IN-14), so only that exact reason, the text check_plan itself builds with covered_reason, is ignored; every
        # other reason makes the pick not allowed (D-29) and sorts it first (D-32). Only an expiry fact under a schedule
        # can raise it.
        covered = covered_reason(alt, p0.pct) if p0.pct else None
        feasible = all(r == covered for r in reasons)
        # D-06's gate, plus D-29's exception: a pick that breaks a hard limit always requests a count
        changes = picked and (not feasible or regret >= eff.policy.min_regret)
        net_diff = s1.net() - s0.net()  # money, signed
        kg_diff = s0.kg() - s1.kg()  # kg less waste with p1, signed
        policy_diff = alt.policy.waste_price * kg_diff  # a policy value, not cash
        # review WR-03: the two parts are rounded and the reported decision regret is their rounded sum, so the
        # displayed sentence adds up to the cent; `changes` above still compares the unrounded regret with min_regret.
        # Review IN-01: every reported figure goes through cents, so none is -0.0
        net_r, policy_r = cents(net_diff), cents(policy_diff)
        decision = max(0.0, cents(net_r + policy_r)) if picked else 0.0  # D-23: never gated; D-29: never negative
        checks.append({
            **fact,
            "regret": cents(decision) if changes else 0.0,
            "decision_regret": cents(decision),
            "unserved_units": cents(unserved) if changes else 0.0,
            "net_revenue_difference": net_r,
            "waste_policy_difference": policy_r,
            "waste_kg_difference": cents(kg_diff),
            "best_plan_if_alt": p1.key,
            "best_plan_if_alt_label": p1.label,
            "pick_changed": picked,
            "pick_feasible_if_alt": feasible,
            "changes_plan": changes,
        })
    # D-32: not-allowed checks first (False sorts first), then the highest regret, then fact_id (02-CONTEXT D-32)
    checks.sort(key=lambda c: (c["pick_feasible_if_alt"], -c["regret"], c["fact_id"]))
    # D-32: the cap never drops a not-allowed check; the list is longer than MAX_CHECKS exactly when more checks are
    # not allowed than the cap
    return checks[:max(MAX_CHECKS, sum(not c["pick_feasible_if_alt"] for c in checks))]
