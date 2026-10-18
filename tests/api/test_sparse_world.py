"""A public product has demand at its actual stores, not every store in the dataset."""
import uuid

import pytest

from api import advance, db, refdata, sandbox
from tests.api.conftest import drop_runs


@pytest.fixture
def sparse_run(public_dv):
    run_id = "tpl-sparse-" + uuid.uuid4().hex[:8]
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id,kind,dataset_version,status,day,next_case_no) "
                     "VALUES (:r,'TEMPLATE',:v,'INITIALIZING',0,1)", r=run_id, v=public_dv)
        ref, truth = refdata.get(public_dv), refdata.truth(public_dv)
        sandbox.init_worlds(conn, run_id, ref, truth)
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=run_id)
        advance.day_start(conn, run)
    yield run
    with db.tx() as conn:
        drop_runs(conn, [run_id])


def test_public_world_omits_unrepresented_store_product_pairs(sparse_run, monkeypatch):
    ref, truth = refdata.get(sparse_run["dataset_version"]), refdata.truth(sparse_run["dataset_version"])
    expected = {(sku, branch): demand for sku in ref.products
                for branch, demand in truth.demand_for(sku, sparse_run["day"]).items()}
    unrelated = "OTHER-PRODUCT-ONLY-STORE"
    monkeypatch.setattr(ref, "branches", [unrelated, *ref.branches])
    with db.tx() as conn:
        error = None
        try:
            advance.step_world(conn, sparse_run, "do_nothing", truth)
        except KeyError as exc:
            error = exc
        assert error is None, f"Absent store/product combinations must not be indexed: {error}"
        outcomes = db.rows(conn, "SELECT branch,sku,demand,sold,lost FROM outcomes WHERE run_id=:r "
                                "AND world='do_nothing'", r=sparse_run["run_id"])
        assert {(row["sku"], row["branch"]): row["demand"] for row in outcomes} == expected
        assert all(row["branch"] != unrelated for row in outcomes)
        assert all(row["sold"] >= 0 and row["lost"] >= 0 for row in outcomes)
        assert outcomes, "the real engine must have produced outcomes"


@pytest.mark.skip(reason="release C keeps the release's step_world (05.2 D-11: a participating store without a "
                  "demand series steps with 0.0); Phase 10's strict refusal (0eb0f70) is not merged")
def test_public_world_refuses_missing_participating_stock_demand(sparse_run, monkeypatch):
    ref, truth = refdata.get(sparse_run["dataset_version"]), refdata.truth(sparse_run["dataset_version"])
    first = sorted(ref.products)[0]
    with db.tx() as conn:
        stock = db.one(conn, "SELECT branch FROM lots WHERE run_id=:r AND world='do_nothing' "
                            "AND sku=:s ORDER BY branch LIMIT 1", r=sparse_run["run_id"], s=first)
    assert stock is not None
    original = truth.demand_for
    def missing(sku, day):
        values = original(sku, day)
        if sku == first:
            assert stock["branch"] in values
            del values[stock["branch"]]
        return values
    monkeypatch.setattr(truth, "demand_for", missing)
    with db.tx() as conn:
        error = None
        try:
            advance.step_world(conn, sparse_run, "do_nothing", truth)
        except (KeyError, ValueError) as exc:
            error = exc
        assert isinstance(error, ValueError) and "missing hidden demand" in str(error).lower(), error
        assert stock["branch"] in str(error) and first in str(error)
        assert not db.rows(conn, "SELECT 1 FROM outcomes WHERE run_id=:r", r=sparse_run["run_id"])
