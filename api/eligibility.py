"""Stage 1 of the local candidate network, at case time (spec §5 Local candidate network; DATA-14; 05.2-CONTEXT
D-27, D-29..D-31).

Stage 1 only: the engine does stage 2 with cost included. A public set's case input holds only the runs a transfer
could really use. A run is eligible when:

- it leaves the case lot's store today (the reference network already limits a store's runs to its own city, the
  service limit or maximum travel time, and k runs a day: D-30, D-34);
- the destination's first sellable day (the engine's own `first_sellable_day`) leaves at least
  `Policy.min_sellable_days` before the lot's expiry, the engine's own rule ("too late to sell");
- a chilled product rides only a cold-chain run ("cold chain", the engine's wording);
- the destination has free storage in the product's zone on the arrival day: more than 0 free totes ("no receiving
  capacity"; a day the reference rows do not list is no known capacity, so it is excluded the same way);
- the destination carries the product: it has a series of it in the set ("does not carry the product";
  `PublicRef.carries`, 05.2 D-34's product-aware destination rule, 08-CONTEXT D-26). A destination that carries the
  product but lacks a forecast day stays eligible, and its lot opens as missing data naming it (D-28).

Each excluded run gets one reason, the first that fails in that order. Transport cost (a run's quote and its
handling per unit) is never read here: whether a transfer is worth it depends on the quantity, the markdown alternative
and the waste-price policy, so the engine compares every eligible transfer with keep and markdown (D-29 as adjusted).

A source store the assumed location mapping lacks (`ref.store_known` false) gets no run at all, whatever the
reference rows hold: its route is never invented (D-31). It keeps every other function (sales, forecast, stock, keep
and markdown); its case detail says `unavailable_unmapped`, which the UI words as "Transfers unavailable: location or
route data missing", never as "no beneficial transfer".

The synthetic set has no stage 1: its case input keeps every run leaving the source today (03-CONTEXT D-24), and its
summary says `applies` false.
"""
from __future__ import annotations

from engine.model import Policy, Product, Run
from engine.timing import first_sellable_day

from . import overlay, refdata

REASONS = {"cold_chain": "cold chain", "late": "too late to sell", "capacity": "no receiving capacity",
           "not_carried": "does not carry the product"}
STATUSES = ("available", "none_eligible", "unavailable_unmapped")


def _reason(ref, run: Run, expiry_day: int | None, product: Product, policy: Policy) -> str | None:
    """The first stage-1 rule `run` fails for this lot, as a REASONS key; None when it passes them all."""
    if product.zone == "chilled" and not run.cold_chain:
        return "cold_chain"
    if expiry_day is not None:  # a gone lot's expiry may be unknown: nothing can be judged late then
        sell_day = first_sellable_day(run.day, run.arrives_min, policy)
        if expiry_day - sell_day + 1 < policy.min_sellable_days:
            return "late"
    arrival_day = run.day + run.arrives_min // 1440
    free = ref.free_totes(run.dest, product.zone, arrival_day)
    if free is None or free <= 0:
        return "capacity"
    if not ref.carries(run.dest, product.sku):  # checked last: every earlier exclusion keeps its reason
        return "not_carried"
    return None


def eligible_runs(ref, lot: dict, product: Product, day: int, policy: Policy) -> tuple[list[Run], list[dict]]:
    """(the runs leaving the lot's store on `day` that pass stage 1, in reference order; the excluded ones as
    [{run_code, dest, reason}] in the same order). `lot` is the record row (its `branch` and `expiry_day`)."""
    source = lot["branch"]
    if not ref.store_known(source):
        return [], []
    runs, excluded = [], []
    for run in ref.runs_from(source, day):
        why = _reason(ref, run, lot.get("expiry_day"), product, policy)
        if why is None:
            runs.append(run)
        else:
            excluded.append({"run_code": run.run_code, "dest": run.dest, "reason": REASONS[why]})
    return runs, excluded


def summary(conn, run: dict, case: dict, lot: dict | None = None) -> dict:
    """The case detail's `eligibility`: {applies, status, considered, eligible, excluded, restricted}.

    `considered` counts the runs leaving the source today (eligible + excluded), `restricted` is true when stage 1
    excluded a run or the shortlist is on (D-27: the result is then the best among the plans evaluated). `lot` is the
    case lot's row as the case detail reads it; read here when not given."""
    from .inputs import sandbox_policy  # inputs imports this module
    ref = refdata.get(run["dataset_version"])
    policy = sandbox_policy(conn, run, ref)
    source, day = case["source_branch"], run["day"]
    if ref.kind != "public":
        runs, excluded, status = ref.runs_from(source, day), [], None
    elif not ref.store_known(source):
        runs, excluded, status = [], [], "unavailable_unmapped"
    else:
        lot = lot or overlay.lot(conn, run, "shelfshift", case["lot_id"]) or {"branch": source, "expiry_day": None}
        runs, excluded = eligible_runs(ref, {**lot, "branch": source}, ref.products[case["sku"]], day, policy)
        status = None
    return {"applies": ref.kind == "public",
            "status": status or ("available" if runs else "none_eligible"),
            "considered": len(runs) + len(excluded), "eligible": len(runs), "excluded": excluded,
            "restricted": bool(excluded) or policy.shortlist_step > 0}
