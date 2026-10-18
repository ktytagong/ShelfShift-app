"""Scenario suitability: real F1 engine results; only database/input-loading seams are local."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.eval import refs
from api import cases, config, db, evaluate, impact, inputs, refdata, reports, worker
from engine import evaluate as engine_evaluate, from_dict, rec_version, to_dict
from tests.engine.fixtures import LAT_PHRAO, THU, f1, with_facts


@pytest.fixture(scope="module")
def real_inputs():
    # F1's independently specified oracle recommends 140 units with 16 free totes.
    # 32→16 leaves that plan feasible; 16→8 cannot retain its 14-tote transfer.
    changed = f1()
    unchanged = replace(changed, capacity=tuple(replace(c, free_totes=32) for c in changed.capacity))
    return [(inp, engine_evaluate(inp)) for inp in (unchanged, changed)]


@pytest.fixture
def local_cases(monkeypatch, real_inputs):
    run = {"run_id": "test-only", "day": THU, "dataset_version": "synthetic-f1"}
    rows, recs, raw_inputs = [], {}, {}
    for index, (inp, result) in enumerate(real_inputs):
        cid = f"case-{index}"
        version = rec_version(inp)
        row = {"case_id": cid, "key": f"CASE-{index}", "state": "OPEN", "selected": None,
               "active_rec_version": version, "run_id": run["run_id"], "lot_id": inp.case_lot_id,
               "source_branch": inp.case_lot().branch, "sku": inp.product.sku,
               "tag": "transfer" if index == 0 else "different-editorial-tag"}
        rows.append(row)
        recs[cid] = {"rec_version": version, "engine_input": to_dict(inp), "result": deepcopy(result),
                     "fact_checks": []}
        raw_inputs[cid] = inp
    reads, writes, pending_seen = [], [], []
    def no_write(*args, **kwargs):
        writes.append(args)
        raise AssertionError("Candidate selection must not persist anything")
    def select_rows(conn, sql, **params):
        reads.append(sql)
        assert sql.startswith("SELECT ") and 'ORDER BY key COLLATE "C", case_id COLLATE "C"' in sql
        values = [r for r in rows if r["tag"] == params["t"]] if "t" in params else rows
        return sorted(values, key=lambda r: (r["key"], r["case_id"]))
    def cache_read(conn, sql, **params):
        reads.append(sql)
        assert sql.startswith("SELECT result FROM engine_cache")
        return None
    monkeypatch.setattr(db, "rows", select_rows)
    monkeypatch.setattr(db, "one", cache_read)
    monkeypatch.setattr(db, "run", no_write)
    monkeypatch.setattr(evaluate, "store", no_write)
    monkeypatch.setattr(evaluate, "run_engine", no_write)
    monkeypatch.setattr(reports, "create", no_write)
    monkeypatch.setattr(impact, "preview", no_write)
    monkeypatch.setattr(cases, "active_rec", lambda conn, case: recs[case["case_id"]])
    monkeypatch.setattr(refdata, "get", lambda dv: SimpleNamespace(
        date=lambda day: date(2026, 1, 5) + timedelta(days=day), products={f1().product.sku: f1().product}))
    monkeypatch.setattr(inputs, "sandbox_policy", lambda conn, current: f1().policy)
    monkeypatch.setattr(worker, "run", lambda function, *args: function(*args))
    def live(conn, current, scopes, *, lock):
        assert lock is False and len(scopes) == 1
        assert scopes[0].branches == frozenset({LAT_PHRAO}) and scopes[0].zone == "chilled"
        return list(rows), list(rows)
    monkeypatch.setattr(impact, "live_cases", live)
    # Keep the real unconfirmed adjustment mapping, ordering and row classifier.
    # Replace only current-input loading; the raw F1 inputs and evaluations are real.
    def load_inputs(conn, current, order, shown, pending):
        assert order == rows
        pending_seen.append(pending)
        out = {}
        for row in shown:
            inp = with_facts(raw_inputs[row["case_id"]], *pending.fact["adjustments"])
            out[row["case_id"]] = inp, rec_version(inp)
        return out
    monkeypatch.setattr(impact, "whatif_inputs", load_inputs)
    return SimpleNamespace(run=run, rows=rows, recs=recs, raw_inputs=raw_inputs,
                           reads=reads, writes=writes, pending_seen=pending_seen)


def scenario_selector():
    scenarios = json.loads((Path(__file__).resolve().parents[2] / "agent/eval/scenarios.json").read_text())
    return next(s["selector"] for s in scenarios if s["id"] == "s06")


def test_s06_skips_earlier_no_effect_and_uses_real_changed_case(local_cases):
    # The pre-fix checked-in selector chooses case-0: a semantic wrong-case RED.
    selected = refs.select_case(None, local_cases.run, scenario_selector())
    assert selected["case_id"] == "case-1", "Skip the earlier half-capacity report with no changed recommendation"
    assert selected["tag"] == "different-editorial-tag"
    again = refs.select_case(None, local_cases.run, scenario_selector())
    assert again == selected
    assert local_cases.writes == []
    assert local_cases.pending_seen
    assert {p.fact["adjustments"][0]["free_totes"] for p in local_cases.pending_seen} == {16, 8}


def test_s06_no_match_refuses_instead_of_returning_no_change(local_cases):
    local_cases.rows[:] = local_cases.rows[:1]
    with pytest.raises(ValueError, match="No suitable chilled transfer case"):
        refs.select_case(None, local_cases.run, scenario_selector())
    assert local_cases.writes == []


def test_s06_actual_engine_preview_changes_only_for_smaller_capacity(local_cases):
    for capacity, should_change in ((16, False), (8, True)):
        fields = {"zone": "chilled", "free_totes": capacity,
                  "valid_from": cases.iso_of(local_cases.run, THU),
                  "valid_to": cases.iso_of(local_cases.run, THU + 1)}
        rows = refs._capacity_preview_rows(None, local_cases.run, LAT_PHRAO, fields)
        assert any(row["changed"] for row in rows) is should_change
        assert all(row["error"] is None for row in rows)
        assert all(row["changed"] == (row["recommended_before"] != row["recommended_after"]) for row in rows)
    assert local_cases.writes == []


def test_s06_change_beyond_display_limit_does_not_qualify(local_cases, monkeypatch):
    # The first case keeps its normal plan. The second has a real, bound alternate
    # option selected, so the same engine result differs only on that second row.
    second = local_cases.rows[1]
    second["selected"] = {"rec_version": second["active_rec_version"], "plan_key": "md20", "fact_checks": []}
    fields = {"zone": "chilled", "free_totes": 16,
              "valid_from": cases.iso_of(local_cases.run, THU),
              "valid_to": cases.iso_of(local_cases.run, THU + 1)}
    monkeypatch.setattr(impact, "order", lambda conn, run, live: list(live))
    monkeypatch.setattr(config, "REPORT_PREVIEW_CASES", 1)
    capped = refs._capacity_preview_rows(None, local_cases.run, LAT_PHRAO, fields)
    assert len(capped) == 1 and capped[0]["changed"] is False
    monkeypatch.setattr(config, "REPORT_PREVIEW_CASES", 2)
    full = refs._capacity_preview_rows(None, local_cases.run, LAT_PHRAO, fields)
    assert [row["changed"] for row in full] == [False, True]


def test_s06_benefit_only_difference_is_not_a_plan_change(local_cases):
    case = local_cases.rows[0]
    before = local_cases.recs[case["case_id"]]
    after = deepcopy(before["result"])
    for option in after["options"]:
        option["delta_vs_keep"] += 1
    row = impact.row(case, before, "different-input-version", after, False)
    assert row["benefit_now"] != row["benefit_if"]
    assert row["allowed"] is False and row["changed"] is False


def test_s06_final_real_preview_refuses_no_changed_row(local_cases, monkeypatch):
    inp = local_cases.raw_inputs["case-1"]
    created = []
    def create(*args):
        created.append(args)
        return {"signal_id": "test-report", "revision": 1}
    modules = {"api.reports": SimpleNamespace(create=create),
               "api.impact": SimpleNamespace(preview=lambda *args: {"cases": [
                   {"plan_now": "Keep", "plan_if": "Keep", "changed": False}], "more": 0})}
    with pytest.raises(ValueError, match="real preview has no changed displayed recommendation"):
        refs._report_refs(None, local_cases.run, "s06", {"dest_store": LAT_PHRAO, "case_id": "case-1"},
                          {}, inp, None, modules)
    assert len(created) == 1
    assert created[0][4]["free_totes"] == 8


@pytest.mark.parametrize("selector", [
    {"target": "destination"},
    {"rule": "unknown", "target": "destination"},
    {"rule": "chilled_transfer_with_half_capacity_preview_change", "target": "source"},
    {"rule": "chilled_transfer_with_half_capacity_preview_change", "target": "destination", "case_id": "pasted"},
])
def test_tagless_exception_is_narrow(local_cases, selector):
    with pytest.raises(ValueError, match="selector"):
        refs.select_case(None, local_cases.run, selector)
