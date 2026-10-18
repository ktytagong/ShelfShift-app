"""The three-way comparison on a stock decision: Recommended, Simple rule and Keep (06.1-CONTEXT D-27, D-43;
06.1-RESEARCH Pattern 2; 04-API-CONTRACT "Product mode (06.1)" Comparison).

Beats only: the case is found by its tag, and no seed quantity, amount or plan key is asserted. Each column is
compared with the engine's own option row as stored, key for key, never recomputed by hand: when a column's plan key
is one of the stored result's options, the composition must reproduce that row exactly (the proof that it reuses the
engine's numbers). The Simple rule is engine/baseline.py's plan on the same stored input.
"""
import json
from numbers import Number

import pytest
from fastapi.testclient import TestClient

from api import cases, db, evaluate, simplerule, worker, workflow
from api.app import app
from engine import baseline, baseline_plan, from_dict
from engine.model import SCENARIOS
from engine.simulate import HorizonError
from tests.api.conftest import as_seat, by_tag, fresh, key, sign_in

ROW_KEYS = ("key", "label", "kind", "pct", "qty", "dest", "run_code", "net", "score", "waste_policy", "delta_vs_keep",
            "sold", "wasted_units", "wasted_kg", "shortfall_filled")  # engine/search.py _row, the contract's list
COLUMNS = ["recommended", "simple_rule", "keep"]


def detail(run_id, case_id):
    with db.tx() as conn:
        return cases.detail(conn, fresh(run_id), case_id)


def stored(run_id, case_id):
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run_id, case_id))


def test_the_story_case_compares_three_columns_that_are_the_engines_option_rows(run):
    case = by_tag(run, cases.STORY_TAG)
    comparison = detail(run["run_id"], case["case_id"])["comparison"]
    rec = stored(run["run_id"], case["case_id"])
    result = rec["result"]

    assert comparison["status"] == "ok" and comparison["reason"] is None
    assert [c["column"] for c in comparison["columns"]] == COLUMNS
    recommended, simple, keep = comparison["columns"]
    assert recommended["key"] == result["recommended"] and keep["key"] == "keep"
    assert simple["key"] == baseline_plan(from_dict(rec["engine_input"])).key  # the store's simple rule, same input

    options = {o["key"]: o for o in result["options"]}
    matched = [c["column"] for c in comparison["columns"] if c["key"] in options]
    assert {"recommended", "keep"} <= set(matched)  # the best plan and Keep are always option rows
    for column in comparison["columns"]:
        if column["key"] in options:
            assert {k: column[k] for k in ROW_KEYS} == options[column["key"]], column["column"]
        assert set(column["transport"]) == set(column["moved"]) == set(SCENARIOS)
        assert all(isinstance(column[part][s], Number) for part in ("transport", "moved") for s in SCENARIOS)
    assert all(v == 0 for v in keep["moved"].values())  # Keep moves nothing


def test_the_engine_result_is_unchanged(run):
    case = by_tag(run, cases.STORY_TAG)
    result = stored(run["run_id"], case["case_id"])["result"]
    assert "simple_rule" not in result and "comparison" not in result
    assert result["engine_version"] == "engine-3"  # D-43: no engine output changed, no version bump


def test_a_simple_rule_without_its_forecast_day_is_missing_data_never_a_figure(run, monkeypatch):
    """A fresh evaluation (a what-if no other test asks, so the cache misses) whose simple rule lacks a forecast day
    stores missing_data with the engine's message and no column; what-if versions store their comparison too."""
    case = by_tag(run, cases.STORY_TAG)
    trip = stored(run["run_id"], case["case_id"])["engine_input"]["runs"][0]
    message = f"no forecast for {case['source_branch']} on day 99 (test)"

    def no_forecast(inp):
        raise HorizonError(message)
    monkeypatch.setattr(baseline, "baseline_plan", no_forecast)
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 7 + 1}]
    with db.tx() as conn:
        version = workflow.whatif(conn, fresh(run["run_id"]), case["case_id"], quote)["rec_version"]
        row = db.one(conn, "SELECT is_whatif, simple_rule FROM recommendations WHERE run_id = :r AND case_id = :c "
                           "AND rec_version = :v", r=run["run_id"], c=case["case_id"], v=version)
    assert row["is_whatif"] is True
    assert row["simple_rule"] == {"status": "missing_data", "reason": message, "columns": []}


def test_a_version_stored_before_the_comparison_reads_not_computed_and_the_next_write_fills_it(client, monkeypatch):
    """GET reads the stored column only: with the version row and its cache row emptied (a version stored before
    06.1) the page says not_computed and the worker is never called. The next re-evaluation of the case computes it
    once through the worker and fills the same version's row."""
    run_id = client.get("/api/session").json()["run_id"]
    case = by_tag(run_id, cases.STORY_TAG)
    rec = stored(run_id, case["case_id"])
    with db.tx() as conn:
        db.run(conn, "UPDATE recommendations SET simple_rule = NULL WHERE run_id = :r AND case_id = :c",
               r=run_id, c=case["case_id"])
        db.run(conn, "UPDATE engine_cache SET simple_rule = NULL WHERE cache_key = :k",
               k=evaluate.cache_key(from_dict(rec["engine_input"])))
    real = worker.run
    monkeypatch.setattr(worker, "run", lambda *args: pytest.fail("a read called the engine worker"))
    r = client.get(f"/api/cases/{case['case_id']}")
    assert r.status_code == 200, r.json()
    assert r.json()["comparison"] == {"status": "not_computed", "reason": None, "columns": []}

    calls = []
    monkeypatch.setattr(worker, "run", lambda fn, *args: calls.append(fn) or real(fn, *args))
    with db.tx() as conn:
        version = cases.evaluate_case(conn, fresh(run_id), cases.case_row(conn, run_id, case["case_id"]))
    assert version == rec["rec_version"]  # the same version: its row is filled, never replaced
    assert calls == [simplerule.compose]  # the cached search is reused; only the comparison ran
    comparison = client.get(f"/api/cases/{case['case_id']}").json()["comparison"]
    assert comparison["status"] == "ok"
    assert comparison == json.loads(db.js(simplerule.compose(from_dict(rec["engine_input"]), rec["result"])))


# Last in this module: `public_template` keeps the tiny public template ACTIVE until the module ends, so every test
# above clones the synthetic one.
def test_evaluating_a_flag_on_the_public_template_answers_its_comparison(public_template):
    with db.tx() as conn:
        tpl = fresh(public_template)
        # a flag nobody opened when there is one (the engine runs on POST), else one the catalogue opened (its case)
        flag = db.one(conn, "SELECT * FROM case_flags f WHERE f.run_id = :r AND f.day = :d ORDER BY EXISTS (SELECT 1 "
                            "FROM cases c WHERE c.run_id = f.run_id AND c.lot_id = f.lot_id), f.key LIMIT 1",
                      r=tpl["run_id"], d=tpl["day"])
    assert flag is not None, "the public template's open day has flags"
    c = TestClient(app)
    sign_in(c)
    as_seat(c, flag["branch"])
    r = c.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=key())
    assert r.status_code == 200, r.json()
    comparison = r.json()["comparison"]
    assert comparison["status"] in ("ok", "missing_data")
    if comparison["status"] == "ok":
        assert [col["column"] for col in comparison["columns"]] == COLUMNS
    else:
        assert comparison["columns"] == [] and comparison["reason"]
