"""Engine-only reference generation on the tiny public template."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from tests.api import conftest as api_fixtures
from api import cases, db, sandbox, selection

EVAL = Path(__file__).resolve().parents[2] / "agent" / "eval"


@pytest.fixture(scope="session")
def public_dv(schema):
    return api_fixtures.public_dv.__wrapped__(schema)


@pytest.fixture(scope="module")
def raw_public_template(template, public_dv):
    yield from api_fixtures.public_template.__wrapped__(template, public_dv)


@pytest.fixture(scope="module")
def public_template(raw_public_template):
    # Reuse the real service precondition already used by the full reading eval.
    # It builds a separate disposable template and confirms capacity through the
    # application, never changing checked-in data or expected engine results.
    from tests.agent.test_eval_steps import real_eval_template
    yield from real_eval_template.__wrapped__(raw_public_template)


def refs_module():
    assert importlib.util.find_spec("agent.eval.refs") is not None, "Engine-only reference builder must exist"
    from agent.eval import refs
    return refs


@pytest.fixture
def scenarios():
    return json.loads((EVAL / "scenarios.json").read_text())


def test_refs_from_the_engine(public_template, scenarios):
    refs = refs_module()
    with db.tx() as conn:
        doc = refs.build_refs(conn, public_template, scenarios)
        template = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=public_template)
        assert doc["dataset_version"] == template["dataset_version"]
        assert doc["template_ref"] == public_template
        assert len(doc["scenarios"]) == 10
        for scenario in scenarios:
            result = doc["scenarios"][scenario["id"]]
            if "pending" in result:
                continue
            inputs = result["resolved_inputs"]
            original = cases.case_row(conn, public_template, inputs["case_id"])
            if scenario["id"] == "s06":
                from engine import from_dict
                active = cases.active_rec(conn, original)
                assert from_dict(active["engine_input"]).product.zone == "chilled"
                assert selection.plan_in_force(original, active)[0].startswith("tr:")
                assert any(row["changed"] for row in result["expected"]["preview"]["rows"])
            else:
                assert original["tag"] in scenario["selector"].get("tags", [scenario["selector"].get("tag")])
            assert not re.search(r"\{[a-z_]+\}", json.dumps(inputs["steps"]))
            assert result["expected"]["plan_in_force"] == selection.plan_in_force(
                original, cases.active_rec(conn, original))[0]
        s01 = doc["scenarios"]["s01"]
        fact = s01["expected"]["fact"]
        assert set(fact) >= {"fact_id", "kind", "branch", "lot_ids", "part"}
        case = cases.case_row(conn, public_template, s01["resolved_inputs"]["case_id"])
        key, checks, _ = selection.plan_in_force(case, cases.active_rec(conn, case))
        assert key.startswith("tr:")
        assert any(c["fact_id"] == fact["fact_id"] and c["pick_changed"] for c in checks)
        assert s01["expected"]["alternative"]["store"] == s01["resolved_inputs"]["alternative_store"]
        assert s01["expected"]["alternative"]["reasons"]
        s02 = doc["scenarios"]["s02"]
        assert s02["expected"]["simulate_args"]["overrides"] == [
            {"kind": "run_available", "run_code": s02["resolved_inputs"]["run_code"], "value": False}]
        assert s02["expected"]["after_plan"] != s02["expected"]["plan_in_force"]
        s03 = doc["scenarios"]["s03"]["expected"]
        assert s03["md30"]["key"] == "md30"
        assert s03["in_force"]["key"] == s03["plan_in_force"]
        probe = doc["scenarios"]["s05"]["expected"]["truth_probe"]
        assert probe["tables"] == ["ref_truth_demand", "truth_lots"]
        assert probe["keys"]["sku"]
        assert not any(k in probe for k in ("values", "demand", "qty", "truth_numbers"))
        s08 = doc["scenarios"]["s08"]
        request = s08["expected"]["request_fields"]
        assert request["lot_ids"] and request["part"] == "count"
        assert s08["resolved_inputs"]["typed_counts"]["shelf_units"] > 0
        clones = db.rows(conn, "SELECT * FROM demo_runs WHERE template_run_id=:r AND kind='SANDBOX'", r=public_template)
        assert len(clones) == 10
        assert all(c["status"] == "RETIRED" for c in clones)


def test_refs_are_deterministic(public_template, scenarios):
    refs = refs_module()
    with db.tx() as conn:
        first = refs.build_refs(conn, public_template, scenarios)
        second = refs.build_refs(conn, public_template, scenarios)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_pending_is_named(raw_public_template, scenarios, monkeypatch, tmp_path, capsys):
    public_template = raw_public_template
    refs = refs_module()
    monkeypatch.setattr(refs, "_upstream", lambda name: None)
    with db.tx() as conn:
        doc = refs.build_refs(conn, public_template, scenarios)
    assert doc["scenarios"]["s06"]["pending"] == ["07-13", "07-15"]
    assert doc["scenarios"]["s07"]["pending"] == ["07-13", "07-15", "07-16"]
    out = tmp_path / "draft.json"
    assert refs.build_refs_main(["--template", public_template, "--out", str(out)]) == 3
    message = capsys.readouterr().out
    assert all(s in message for s in ("PENDING", "s06", "s07", "07-13", "07-15", "07-16"))
    assert json.loads(out.read_text())["scenarios"]["s07"]["pending"]


def test_selectors_are_rules_not_ids(public_template, scenarios):
    refs = refs_module()
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=public_template)
        for s in scenarios:
            c = refs.select_case(conn, run, s["selector"])
            assert c["run_id"] == public_template
        with pytest.raises(ValueError, match="selector"):
            refs.select_case(conn, run, {"case_id": "pasted-case"})
        with pytest.raises(ValueError, match="No case"):
            refs.select_case(conn, run, {"tag": "missing-tag"})


def test_failed_reference_clone_is_retired(public_template, monkeypatch):
    refs = refs_module()
    def fail(*args):
        raise ValueError("invalid selector")
    monkeypatch.setattr(refs, "select_case", fail)
    with db.tx() as conn:
        with pytest.raises(ValueError, match="invalid selector"):
            refs.build_refs(conn, public_template, [{"id": "s01", "selector": {}}])
        assert not db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id=:r "
                                "AND kind='SANDBOX' AND status='ACTIVE'", r=public_template)


def test_clone_isolation_and_no_truth_values(public_template, scenarios, monkeypatch, tmp_path):
    refs = refs_module()
    from api import refdata, workflow
    seen = []
    original = workflow.whatif
    def whatif(conn, run, *args, **kwargs):
        seen.append(run["run_id"])
        assert run["kind"] == "SANDBOX"
        return original(conn, run, *args, **kwargs)
    monkeypatch.setattr(workflow, "whatif", whatif)
    def forbidden(*args, **kwargs):
        raise AssertionError("References must not read hidden truth")
    monkeypatch.setattr(refdata, "truth", forbidden)
    with db.tx() as conn:
        first = refs.build_refs(conn, public_template, scenarios)
        second = refs.build_refs(conn, public_template, scenarios)
        assert len(set(seen)) == 2
        assert first == second
        # Human request exists only on s08's clone, never the template.
        assert not db.rows(conn, "SELECT request_id FROM evidence_requests WHERE run_id=:r", r=public_template)
        assert len(db.rows(conn, "SELECT DISTINCT q.run_id FROM evidence_requests q JOIN demo_runs d USING(run_id) "
                                "WHERE d.template_run_id=:r", r=public_template)) >= 2


def test_present_upstreams_use_the_hand_services(public_template, scenarios, monkeypatch):
    refs = refs_module()
    from types import SimpleNamespace
    from api import config
    calls = []
    def create(conn, run, actor, kind, fields, text, case_id, corrects):
        calls.append(("create", run["run_id"], kind, fields, actor))
        assert text == "" and corrects is None
        return {"signal_id": "temporary-signal", "revision": 1}
    def preview(conn, run, actor, signal_id, revision):
        calls.append(("preview", run["run_id"]))
        return {"cases": [{"case_id": "fixture-case", "plan_now": "keep", "plan_if": "md30",
                          "changed": True, "result_id": "random-private-id"}], "more": 0}
    def quote_for_run(run, run_code, *, ambiguous):
        from api import refdata
        ref = refdata.get(run["dataset_version"])
        amount = float(ref.run_on(run_code, run["day"]).fixed_cost)
        calls.append(("pdf", run_code, amount, ref.currency))
        assert ambiguous is True
        return {"run_code": run_code, "amount": amount, "currency": ref.currency}, b"%PDF-fixture-ambiguous-quote"
    monkeypatch.setitem(config.SETTINGS, "impact", {"summary_max_words": 90})
    modules = {"api.reports": SimpleNamespace(create=create),
               "api.impact": SimpleNamespace(preview=preview),
               "api.preload": SimpleNamespace(quote_for_run=quote_for_run)}
    monkeypatch.setattr(refs, "_upstream", modules.get)
    with db.tx() as conn:
        doc = refs.build_refs(conn, public_template, scenarios[5:7])
    assert [c[0] for c in calls] == ["create", "preview", "pdf", "create", "preview"]
    assert calls[0][1] != calls[3][1]
    assert calls[0][2] == "CAPACITY" and calls[3][2] == "TRANSPORT"
    assert calls[3][3]["pricing_basis"] is None and calls[3][3]["includes_handling"] is None
    s06, s07 = doc["scenarios"]["s06"], doc["scenarios"]["s07"]
    assert s06["expected"]["reading_fields"]["kind"] == "CAPACITY"
    assert s06["expected"]["summary_max_words"] == 90
    assert s07["resolved_inputs"]["quote_pdf"]["sha256"]
    assert "random-private-id" not in json.dumps(doc)


def test_summary_reference_waits_for_its_actual_policy(raw_public_template, scenarios, monkeypatch):
    public_template = raw_public_template
    from api import config
    refs = refs_module()
    monkeypatch.delitem(config.SETTINGS, "impact", raising=False)
    with db.tx() as conn:
        doc = refs.build_refs(conn, public_template, scenarios[5:6])
    assert doc["scenarios"]["s06"] == {"pending": ["10-14"]}


def test_summary_reference_uses_the_real_preview_shape_and_policy(public_template, scenarios, monkeypatch):
    from api import config, impact
    refs = refs_module()
    monkeypatch.setitem(config.SETTINGS, "impact", {"summary_max_words": 73})
    observed = []
    original = impact.preview

    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append(result)
        return result

    monkeypatch.setattr(impact, "preview", preview)
    with db.tx() as conn:
        doc = refs.build_refs(conn, public_template, scenarios[5:6])
    expected = doc["scenarios"]["s06"]["expected"]
    assert expected["summary_max_words"] == 73
    assert len(observed) == 1 and observed[0]["cases"]
    assert expected["preview"]["more"] == observed[0]["more"]
    assert expected["preview"]["rows"] == [
        {key: row[key] for key in ("case_id", "plan_now", "plan_if", "benefit_now", "benefit_if", "changed")}
        for row in observed[0]["cases"]
    ]
    assert any(row["changed"] for row in observed[0]["cases"])
