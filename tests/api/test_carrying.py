"""Transfer options on a public set use only stores that carry the product (05.2-CONTEXT D-34's product-aware
destination rule, applied in stage 1; 08-CONTEXT D-26; quick 261017-va5).

A store "carries" a product when (store, product) is one of the set's series: the set's sales rows, the rule the install
builds its series from. Stage 1 lists a run whose destination does not carry the lot's product as excluded, reason
"does not carry the product", so it never reaches the engine input (with or without a morning screen's ScreenReads, and
on the second-look build), the screen, the case, the morning flags, the store rule's reach or casebench's ranking. A
store that does carry the product but has no forecast day still makes the lot open as missing data naming it (05.2
D-28): a data gap is never hidden as "not carried".

The tiny set (fixtures/public_tiny) has every series, so it cannot show the cause. The variant here is built at test
time from the tiny set's own parts with one destination series removed (its sales, forecast points, hidden demand, lots,
deliveries and their physical rows), under a dataset_version of its own: the fixture files and the reference rows of
other versions are never touched (D-08). Every store, run, lot and product code is derived by a rule from the parts or
the run's rows, never pasted (tests/api/test_eligibility.py's rule).
"""
from __future__ import annotations

import re
import uuid

import pytest

from api import advance, casebench, cases, config, db, eligibility, inputs, publicref, refdata, sandbox, screen
from tests.api.conftest import TINY, drop_runs, fresh
from tests.api.test_casebench import by_hand
from tests.api.test_eligibility import open_case
from tests.api.test_publicref import manifest, part_rows, rewrite_part, variant

NOT_CARRIED = "does not carry the product"
SERIES_PARTS = ("sales", "forecast_points", "truth_demand", "lots0", "inbound0")  # rows keyed by (store, sku)


# ------------------------------------------------------------------------------------------------ the pick

def pick() -> dict:
    """R, S, D, P, Q from the unmodified tiny parts, by rule. R: the first day-0 cold-chain run (run_code order) whose
    origin and destination are both mapped and whose origin S holds untagged lot0 rows of at least two products. P: the
    first of S's untagged products, in code order, whose series at D is not the designed one and of which D holds no
    tagged lot. Q: another of S's untagged products; (D, Q) stays a series."""
    mapped = {s["store"] for s in part_rows("stores") if s["geo_known"] == "true"}
    designed = manifest()["settings"]["designed"]
    lots = part_rows("lots0")
    runs = sorted((r for r in part_rows("runs") if r["day"] == "0" and r["cold_chain"] == "true"
                   and r["origin"] in mapped and r["dest"] in mapped), key=lambda r: r["run_code"])
    for r in runs:
        s, d = r["origin"], r["dest"]
        products = sorted({l["sku"] for l in lots if l["store"] == s and not l["tag"]})
        if len(products) < 2:
            continue
        tagged_at_d = {l["sku"] for l in lots if l["store"] == d and l["tag"]}
        p = next((k for k in products if (d, k) != (designed["store"], designed["product"]) and k not in tagged_at_d),
                 None)
        if p is None:
            continue
        q = next(k for k in products if k != p)
        return {"R": r["run_code"], "S": s, "D": d, "P": p, "Q": q}
    raise AssertionError("the tiny set has no day-0 cold-chain run from a store with two untagged products")


def first_part_lot(store: str, sku: str) -> dict:
    """The first untagged lot0 row of `sku` at `store`, in part order, as stage 1 reads a lot (branch, expiry_day)."""
    row = next(l for l in part_rows("lots0") if l["store"] == store and l["sku"] == sku and not l["tag"])
    return {"lot_id": row["lot_id"], "branch": store, "expiry_day": int(row["expiry_day"])}


def series_of(dv: str) -> set[tuple[str, str]]:
    with db.tx() as conn:
        return {(r["store"], r["sku"]) for r in db.rows(conn, "SELECT DISTINCT store, sku FROM ref_sales_day "
                                                              "WHERE dataset_version = :v", v=dv)}


@pytest.fixture(scope="module")
def picked(public_dv) -> dict:
    """The pick, with its preconditions checked on the unmodified installed tiny set: R is kept today for S's first
    untagged lot of P and for its first untagged lot of Q."""
    k = pick()
    ref = refdata.get(public_dv)
    for sku in (k["P"], k["Q"]):
        assert (k["D"], sku) in series_of(public_dv)
        runs, _ = eligibility.eligible_runs(ref, first_part_lot(k["S"], sku), ref.products[sku], 0, ref.policy)
        assert k["R"] in {r.run_code for r in runs}, (sku, runs)
    return k


# ------------------------------------------------------------------------------------------------ the variant

@pytest.fixture(scope="module")
def no_carry_dv(picked, tmp_path_factory) -> str:
    """The tiny set without the series (D, P): its rows dropped from every series part, and the physical rows of the
    dropped lots and deliveries, installed under its own dataset_version."""
    d, p = picked["D"], picked["P"]
    root = variant(tmp_path_factory.mktemp("carry"), "no-carry")
    dropped: set[str] = set()
    for part in SERIES_PARTS:
        header = manifest(root)["files"][part]["columns"]
        rows = part_rows(part, root)
        id_col = {"lots0": "lot_id", "inbound0": "inbound_id"}.get(part)
        if id_col:
            dropped |= {r[id_col] for r in rows if (r["store"], r["sku"]) == (d, p)}
        rewrite_part(root, part, header, [[r[c] for c in header] for r in rows if (r["store"], r["sku"]) != (d, p)])
    assert dropped, f"the tiny set has lots or deliveries of {p} at {d}: their physical rows go too"
    header = manifest(root)["files"]["truth_physical"]["columns"]
    rewrite_part(root, "truth_physical", header,
                 [[r[c] for c in header] for r in part_rows("truth_physical", root) if r["lot_id"] not in dropped])
    dv = refdata.install(root, allow_local=True)
    assert dv == manifest(root)["dataset_version"] != manifest(TINY)["dataset_version"]
    tiny = manifest(TINY)["dataset_version"]
    assert series_of(dv) == series_of(tiny) - {(d, p)}
    return dv


@pytest.fixture
def carry_run(no_carry_dv):
    """A scratch TEMPLATE run of the variant on day 0 (init_worlds, then the day start), built as `public_run` is; its
    rows and its clones' are deleted at teardown."""
    run_id = f"tpl-carry-{uuid.uuid4().hex[:8]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=no_carry_dv)
        sandbox.init_worlds(conn, run_id, refdata.get(no_carry_dv), refdata.truth(no_carry_dv))
        advance.day_start(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id))
    yield fresh(run_id)
    with db.tx() as conn:
        clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = :r",
                                               r=run_id)]
        drop_runs(conn, [run_id, *clones])


def record_lot(run: dict, store: str, sku: str) -> dict:
    """The first untagged lot of `sku` at `store` with stock on the run's record, in lot_id order, among the lots the
    morning screen looks at today (`cases.screen_candidates`: a lot outside its window is never screened or flagged)."""
    with db.tx() as conn:
        screened = {l["lot_id"] for l in cases.screen_candidates(conn, fresh(run["run_id"]))}
        rows = db.rows(conn, "SELECT * FROM lots WHERE run_id = :r AND world = 'shelfshift' AND branch = :b "
                             "AND sku = :s AND qty > 0 AND coalesce(tag, '') = '' ORDER BY lot_id",
                       r=run["run_id"], b=store, s=sku)
    found = [l for l in rows if l["lot_id"] in screened]
    assert found, f"the run's record has an untagged lot of {sku} at {store} in today's screen window"
    return found[0]


def names(text: str | None, store: str) -> bool:
    return bool(text) and re.search(rf"\b{re.escape(store)}\b", text) is not None


def input_stores(inp) -> set[str]:
    return ({r.dest for r in inp.runs} | {l.branch for l in inp.lots} | {f.branch for f in inp.forecasts}
            | {c.branch for c in inp.capacity} | {i.branch for i in inp.inbound} | {b for b, _ in inp.sales_yesterday})


# ------------------------------------------------------------------------------------------------ tests

def test_carries_is_the_sets_series(public_dv, no_carry_dv, picked):
    for dv in (public_dv, no_carry_dv):
        ref, series = refdata.get(dv), series_of(dv)
        for store in ref.branches:
            for sku in ref.products:
                assert ref.carries(store, sku) is ((store, sku) in series), (dv, store, sku)
    tiny, var = refdata.get(public_dv), refdata.get(no_carry_dv)
    assert var.carries(picked["D"], picked["P"]) is False
    assert {(s, k) for s in tiny.branches for k in tiny.products if tiny.carries(s, k)} - \
        {(s, k) for s in var.branches for k in var.products if var.carries(s, k)} == {(picked["D"], picked["P"])}


def test_a_destination_without_the_product_is_never_a_transfer_option(carry_run, picked):
    ref, day = refdata.get(carry_run["dataset_version"]), carry_run["day"]
    s, d, r_code = picked["S"], picked["D"], picked["R"]
    lot, lot_q = record_lot(carry_run, s, picked["P"]), record_lot(carry_run, s, picked["Q"])
    product = ref.products[picked["P"]]
    with db.tx() as conn:
        run = fresh(carry_run["run_id"])
        policy = inputs.sandbox_policy(conn, run, ref)
        runs, excluded = eligibility.eligible_runs(ref, lot, product, day, policy)
        assert r_code not in {r.run_code for r in runs}
        assert {"run_code": r_code, "dest": d, "reason": NOT_CARRIED} in excluded

        plain = inputs.build_input(conn, run, lot["lot_id"])
        screened = inputs.build_input(conn, run, lot["lot_id"], reads=inputs.ScreenReads(conn, run))
        second = inputs.build_input(conn, run, lot["lot_id"], exclude_case=f"T-CARRY-{uuid.uuid4().hex[:6]}")
        for inp in (plain, screened, second):
            assert d not in input_stores(inp), sorted(input_stores(inp))
        assert screened == plain
        assert d not in {r.dest for r in inputs.case_runs(ref, s, day, lot=lot, product=product, policy=policy)}

        e = eligibility.summary(conn, run, {"source_branch": s, "sku": picked["P"], "lot_id": lot["lot_id"]}, lot)
        assert {"run_code": r_code, "dest": d, "reason": NOT_CARRIED} in e["excluded"]
        assert e["considered"] == e["eligible"] + len(e["excluded"])
        assert e["restricted"] is True

        found = screen.screen(plain, config.CASE_OPENING)  # figures, never a HorizonError naming d
        assert {"qualifies", "usable_runs"} <= set(found) and r_code not in found["usable_runs"]

        # the rule depends on the product: the same run stays a transfer option for the other product at s
        runs_q, _ = eligibility.eligible_runs(ref, lot_q, ref.products[picked["Q"]], day, policy)
        assert r_code in {r.run_code for r in runs_q}


def test_a_lot_whose_destination_lacks_the_product_gets_a_real_recommendation(carry_run, picked):
    lot = record_lot(carry_run, picked["S"], picked["P"])
    key = f"T-CARRY-{uuid.uuid4().hex[:6]}"
    detail = open_case(carry_run, lot, key)
    with db.tx() as conn:
        case = cases.case_row(conn, carry_run["run_id"], key)
    assert case["missing_data"] is None, case["missing_data"]
    assert case["active_rec_version"] and detail["result"]
    transfers = [o for o in detail["result"]["options"] if o["kind"] == "transfer"]
    assert picked["D"] not in {o["dest"] for o in transfers}


def test_the_morning_screen_flags_no_missing_data_for_a_store_without_the_product(carry_run, picked):
    with db.tx() as conn:
        cases.open_cases(conn, fresh(carry_run["run_id"]))
        rows = db.rows(conn, "SELECT lot_id, missing_data FROM case_flags WHERE run_id = :r UNION ALL "
                             "SELECT lot_id, missing_data FROM cases WHERE run_id = :r", r=carry_run["run_id"])
    assert [r for r in rows if names(r["missing_data"], picked["D"])] == []


def test_a_carrying_store_with_a_missing_forecast_still_opens_as_missing_data(carry_run, picked, monkeypatch):
    d, q = picked["D"], picked["Q"]
    with db.tx() as conn:
        assert db.one(conn, "SELECT 1 AS x FROM ref_sales_day WHERE dataset_version = :v AND store = :s AND sku = :k "
                            "LIMIT 1", v=carry_run["dataset_version"], s=d, k=q)  # (d, q) is a series
    ref = refdata.get(carry_run["dataset_version"])
    real_forecast, real_of_product = ref.forecast, ref.forecast_of_product
    monkeypatch.setattr(ref, "forecast", publicref.Called(
        dict(real_forecast), lambda day, store, sku: {} if (store, sku) == (d, q) else real_forecast(day, store, sku)))
    monkeypatch.setattr(ref, "forecast_of_product", lambda day, sku: {
        store: points for store, points in real_of_product(day, sku).items() if (store, sku) != (d, q)})

    lot2 = record_lot(carry_run, picked["S"], q)
    with db.tx() as conn:
        run = fresh(carry_run["run_id"])
        policy = inputs.sandbox_policy(conn, run, ref)
        runs, _ = eligibility.eligible_runs(ref, lot2, ref.products[q], run["day"], policy)
        assert picked["R"] in {r.run_code for r in runs}  # carried, so still a transfer option
        cases.open_cases(conn, run)
        marked = db.rows(conn, "SELECT missing_data FROM case_flags WHERE run_id = :r AND lot_id = :l UNION ALL "
                               "SELECT missing_data FROM cases WHERE run_id = :r AND lot_id = :l",
                         r=run["run_id"], l=lot2["lot_id"])
    assert marked and all(names(m["missing_data"], d) for m in marked), marked
    key = f"T-CARRY-{uuid.uuid4().hex[:6]}"
    open_case(carry_run, lot2, key)
    with db.tx() as conn:
        case = cases.case_row(conn, carry_run["run_id"], key)
    assert names(case["missing_data"], d), case["missing_data"]
    assert case["missing_data"].startswith("missing data: ")


def test_casebench_counts_the_destinations_stage_one_keeps(carry_run, picked):
    lot, lot2 = record_lot(carry_run, picked["S"], picked["P"]), record_lot(carry_run, picked["S"], picked["Q"])
    keys = {}
    with db.tx() as conn:
        for l in (lot, lot2):
            keys[l["lot_id"]] = f"T-CARRY-{uuid.uuid4().hex[:6]}"
            db.run(conn, "INSERT INTO cases (run_id, case_id, key, lot_id, source_branch, sku, state, opened_day) "
                         "VALUES (:r, :c, :c, :l, :b, :s, 'OPEN', :d)", r=carry_run["run_id"], c=keys[l["lot_id"]],
                   l=l["lot_id"], b=l["branch"], s=l["sku"], d=carry_run["day"])
        run = fresh(carry_run["run_id"])
        got = [(c["key"], c["store"], c["product"], c["dests"], c["lots"])
               for c in casebench.slowest_candidates(conn, run, 100)]
        want = by_hand(conn, run)
    assert got == want
    by_key = {g[0]: g for g in got}
    assert by_key[keys[lot["lot_id"]]][3] == 0, by_key  # its one run goes to a store without the product
    assert by_key[keys[lot2["lot_id"]]][3] >= 1, by_key  # the same run carries the other product
