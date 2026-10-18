"""Stage 1 of the local candidate network at case time (spec §5 Local candidate network; DATA-14; 05.2-CONTEXT D-27,
D-29..D-31).

A public set's case input holds only the runs a transfer could use: the run leaves the source today (the network
already limits it to the same city, the service limit and k), the destination's first sellable day leaves at least
Policy.min_sellable_days before the lot's expiry, a chilled product rides only a cold-chain run, and the destination
has free storage in the product's zone on the arrival day. Transport cost filters nothing: the engine compares the
eligible transfers with keep and markdown, cost included (stage 2). A source store missing from the mapping gets no run.

Every store, run, lot and number here is read from the tiny public set (fixtures/public_tiny) or derived from it by
the rule under test, never pasted. A "copied set" (`Edited`) is the set with some rows changed in memory: the shared
reference tables are never modified (D-08).
"""
from __future__ import annotations

from dataclasses import replace

from api import cases, db, eligibility, evaluate, inputs, refdata
from engine.limits import transfer_caps
from engine.model import KEEP, MARKDOWN, TRANSFER
from engine.timing import first_sellable_day
from tests.api.conftest import by_tag, fresh, world_rows

ENGINE_SAME_RULES = ("cold chain", "not enough sellable days at destination")  # engine.limits' own reasons


class Edited:
    """A copy of a reference set with some rows changed, in memory: each run through `run`, `free` overriding free
    totes by (store, zone, day), `extra` runs added. Everything else is the set's own."""

    def __init__(self, ref, run=None, free=None, extra=()):
        self._ref, self._run, self._free, self._extra = ref, run or (lambda r: r), dict(free or {}), tuple(extra)

    def runs_from(self, origin, day):
        return ([self._run(r) for r in self._ref.runs_from(origin, day)] +
                [r for r in self._extra if r.origin == origin and r.day == day])

    def free_totes(self, store, zone, day):
        return self._free[(store, zone, day)] if (store, zone, day) in self._free else \
            self._ref.free_totes(store, zone, day)

    def __getattr__(self, name):
        return getattr(self._ref, name)


def ref_of(run):
    return refdata.get(run["dataset_version"])


def day_lots(run):
    """The record's lots with stock today, in branch, sku, lot_id order."""
    return world_rows(run, "lots", "world = 'shelfshift' AND qty > 0", "branch, sku, lot_id")


def mapped(ref):
    return [s for s in ref.branches if ref.store_known(s)]


def unmapped(ref):
    return [s for s in ref.branches if not ref.store_known(s)]


def input_of(run, lot_id):
    with db.tx() as conn:
        return inputs.build_input(conn, fresh(run["run_id"]), lot_id)


def first_lot(run, store, test=lambda lot: True):
    found = [l for l in day_lots(run) if l["branch"] == store and test(l)]
    assert found, f"the tiny set has a lot at {store} today"
    return found[0]


def open_case(run, lot, key):
    """A case on `lot` written straight into the run (the screen is not under test here), evaluated once."""
    with db.tx() as conn:
        db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, state, opened_day) VALUES "
                     "(:r, :c, :c, :l, :b, :s, 'OPEN', :d)", r=run["run_id"], c=key, l=lot["lot_id"], b=lot["branch"],
               s=lot["sku"], d=run["day"])
        r = fresh(run["run_id"])
        cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], key))
        return cases.detail(conn, r, key)


def test_stage_one_keeps_only_runs_a_transfer_could_use(public_run):
    ref, day = ref_of(public_run), public_run["day"]
    policy = ref.policy
    # Every mapped store's input holds exactly its runs today minus those stage 1 excluded, in data order, and the
    # engine rejects none of them for a rule stage 1 already applies (T-05.2-32).
    seen = 0
    for lot in [l for l in day_lots(public_run) if l["branch"] in mapped(ref)]:
        product = ref.products[lot["sku"]]
        runs, excluded = eligibility.eligible_runs(ref, lot, product, day, policy)
        dropped = {x["run_code"] for x in excluded}
        assert runs == [r for r in ref.runs_from(lot["branch"], day) if r.run_code not in dropped]
        inp = input_of(public_run, lot["lot_id"])
        assert list(inp.runs) == runs
        for r in inp.runs:
            assert not set(transfer_caps(inp, r)[1]) & set(ENGINE_SAME_RULES)
        seen += 1
    assert seen

    # Cold chain: a chilled lot at a store whose run today is not cold-chain.
    store, warm = next((s, r) for s in mapped(ref) for r in ref.runs_from(s, day) if not r.cold_chain)
    lot = first_lot(public_run, store, lambda l: ref.products[l["sku"]].zone == "chilled")
    runs, excluded = eligibility.eligible_runs(ref, lot, ref.products[lot["sku"]], day, policy)
    assert warm not in runs
    assert {"run_code": warm.run_code, "dest": warm.dest, "reason": "cold chain"} in excluded
    assert warm.run_code not in {r.run_code for r in input_of(public_run, lot["lot_id"]).runs}

    # Too late to sell, both sides of the boundary: a cold-chain run, its first sellable day by the engine's rule,
    # and the lot's days left from then on (expiry - sell day + 1). Exactly that many is eligible; one more is not.
    store, cold = next((s, r) for s in mapped(ref) for r in ref.runs_from(s, day) if r.cold_chain)
    lot = first_lot(public_run, store)
    product = ref.products[lot["sku"]]
    left = lot["expiry_day"] - first_sellable_day(cold.day, cold.arrives_min, policy) + 1
    assert left >= 1
    runs, excluded = eligibility.eligible_runs(ref, lot, product, day, replace(policy, min_sellable_days=left))
    assert cold in runs and excluded == []
    runs, excluded = eligibility.eligible_runs(ref, lot, product, day, replace(policy, min_sellable_days=left + 1))
    assert cold not in runs
    assert excluded == [{"run_code": cold.run_code, "dest": cold.dest, "reason": "too late to sell"}]

    # Receiving capacity on the arrival day, both sides: exactly 0 free totes is excluded, 1 is eligible.
    arrival = cold.day + cold.arrives_min // 1440
    full = Edited(ref, free={(cold.dest, product.zone, arrival): 0})
    runs, excluded = eligibility.eligible_runs(full, lot, product, day, policy)
    assert cold not in runs
    assert excluded == [{"run_code": cold.run_code, "dest": cold.dest, "reason": "no receiving capacity"}]
    one = Edited(ref, free={(cold.dest, product.zone, arrival): 1})
    assert cold in eligibility.eligible_runs(one, lot, product, day, policy)[0]


def test_an_unmapped_store_gets_no_transfer_and_keeps_keep_and_markdown(public_run):
    ref, day = ref_of(public_run), public_run["day"]
    gone = unmapped(ref)
    assert gone, "the tiny set leaves a store out of the mapping"
    store = gone[0]
    lot = first_lot(public_run, store, lambda l: l["markdown_pct"] == 0 and l["expiry_day"] > day)
    product = ref.products[lot["sku"]]
    # No run, even one the reference rows would offer: a store the mapping lacks never gets a route (D-31).
    mapped_dest = mapped(ref)[0]
    invented = replace(ref.runs_from(mapped_dest, day)[0], origin=store, dest=mapped_dest)
    assert eligibility.eligible_runs(Edited(ref, extra=[invented]), lot, product, day, ref.policy) == ([], [])
    inp = input_of(public_run, lot["lot_id"])
    assert inp.runs == ()
    with db.tx() as conn:
        _, result, _ = evaluate.run_engine(conn, inp)
    kinds = {o["kind"] for o in result["options"]}
    assert KEEP in kinds and MARKDOWN in kinds and TRANSFER not in kinds
    detail = open_case(public_run, lot, "T-ELIG-UNMAPPED")
    assert detail["eligibility"] == {"applies": True, "status": "unavailable_unmapped", "considered": 0,
                                     "eligible": 0, "excluded": [], "restricted": False}
    assert "no beneficial transfer" not in detail["summary"].lower()


def test_transport_cost_filters_nothing_before_the_engine(public_run):
    ref, day = ref_of(public_run), public_run["day"]
    dear = Edited(ref, run=lambda r: replace(r, fixed_cost=r.fixed_cost * 1000 + 1, per_unit_cost=r.per_unit_cost + 50))
    checked = 0
    for lot in day_lots(public_run):
        product = ref.products[lot["sku"]]
        cheap_runs, cheap_out = eligibility.eligible_runs(ref, lot, product, day, ref.policy)
        dear_runs, dear_out = eligibility.eligible_runs(dear, lot, product, day, ref.policy)
        assert [r.run_code for r in dear_runs] == [r.run_code for r in cheap_runs] and dear_out == cheap_out
        checked += len(cheap_runs)
    assert checked, "at least one run was eligible, so the cost had something to filter"


def test_a_public_case_detail_says_what_stage_one_excluded(public_run):
    ref, day = ref_of(public_run), public_run["day"]
    store, warm = next((s, r) for s in mapped(ref) for r in ref.runs_from(s, day) if not r.cold_chain)
    lot = first_lot(public_run, store, lambda l: ref.products[l["sku"]].zone == "chilled")
    e = open_case(public_run, lot, "T-ELIG-WARM")["eligibility"]
    assert set(e) == {"applies", "status", "considered", "eligible", "excluded", "restricted"}
    assert e["applies"] is True and e["considered"] == e["eligible"] + len(e["excluded"])
    assert {"run_code": warm.run_code, "dest": warm.dest, "reason": "cold chain"} in e["excluded"]
    assert e["restricted"] is True
    assert e["status"] == ("available" if e["eligible"] else "none_eligible")

    # Nothing excluded and the shortlist off: not restricted; the shortlist on: restricted (D-27).
    store = next(s for s in mapped(ref)
                 if all(r.cold_chain for r in ref.runs_from(s, day)) and ref.runs_from(s, day))
    lot = first_lot(public_run, store, lambda l: l["expiry_day"] >= day + 1)
    e = open_case(public_run, lot, "T-ELIG-COLD")["eligibility"]
    assert (e["status"], e["excluded"], e["restricted"]) == ("available", [], False)
    assert e["eligible"] == e["considered"] == len(ref.runs_from(store, day))
    with db.tx() as conn:
        db.run(conn, "INSERT INTO run_policy (run_id, field, value) VALUES (:r, 'shortlist_step', '5')",
               r=public_run["run_id"])
    try:
        with db.tx() as conn:
            case = cases.case_row(conn, public_run["run_id"], "T-ELIG-COLD")
            assert eligibility.summary(conn, fresh(public_run["run_id"]), case)["restricted"] is True
    finally:
        with db.tx() as conn:
            db.run(conn, "DELETE FROM run_policy WHERE run_id = :r", r=public_run["run_id"])


def test_the_synthetic_set_keeps_todays_runs_and_says_stage_one_does_not_apply(run):
    ref = ref_of(run)
    case = by_tag(run, cases.STORY_TAG)
    inp = input_of(run, case["lot_id"])
    assert list(inp.runs) == ref.runs_from(case["source_branch"], run["day"])
    with db.tx() as conn:
        e = cases.detail(conn, fresh(run["run_id"]), case["case_id"])["eligibility"]
    assert e["applies"] is False and e["excluded"] == [] and e["status"] == "available"
    assert e["considered"] == e["eligible"] == len(inp.runs)
