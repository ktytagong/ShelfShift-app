"""D-14: engine and fixtures only, never a model.

Draft references are reproducible on a template. Each scenario gets its own
fresh clone, retired even on failure. No timestamps or random workspace/request
identifiers enter the output. Hidden truth is described by probe keys only.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path

PENDING = {"s06": [("api.reports", "07-13"), ("api.impact", "07-15")],
           "s07": [("api.reports", "07-13"), ("api.impact", "07-15"), ("api.preload", "07-16")]}
HERE = Path(__file__).resolve().parent


def _summary_words():
    """Read the actual policy without importing or constructing an AI service."""
    from api import config
    policy = config.SETTINGS.get("impact")
    if policy is None:
        return None
    value = policy.get("summary_max_words")
    if type(value) is not int or value <= 0:
        raise ValueError("settings.impact.summary_max_words must be a positive integer")
    return value


def _upstream(name):
    if importlib.util.find_spec(name) is None:
        return None
    return importlib.import_module(name)


def _fact(checks):
    from api import signals
    for check in sorted(checks, key=lambda c: c["fact_id"]):
        if check.get("pick_changed") and check["kind"] in (*signals.COUNT_KINDS, *signals.EXPIRY_KINDS):
            return {"fact_id": check["fact_id"], "kind": check["kind"], "branch": check["branch"],
                    "lot_ids": sorted(check["lot_ids"]), "part": signals.part_of(check["kind"])}
    raise ValueError("No plan-changing requestable fact in the plan in force")


S06_RULE = "chilled_transfer_with_half_capacity_preview_change"


def _capacity_fields(run, inp, destination):
    """The unchanged report: floor-half effective free totes today through tomorrow."""
    from api import cases
    from engine.adjust import effective
    free = next(c.free_totes for c in effective(inp).capacity
                if c.branch == destination and c.day == run["day"])
    return {"zone": inp.product.zone, "free_totes": free // 2,
            "valid_from": cases.iso_of(run, run["day"]),
            "valid_to": cases.iso_of(run, run["day"] + 1)}


def _capacity_preview_rows(conn, run, destination, fields):
    """Real preview scope/input/row semantics without persistent preview/cache writes."""
    from api import cases, config, db, evaluate, impact, inputs, reports, selection, worker
    from api.simplerule import parse_plan
    from engine import check_plan, evaluate as engine_evaluate, from_dict, rec_version, to_dict
    cleaned = reports.clean(run, "CAPACITY", fields, inputs.sandbox_policy(conn, run))
    pending, found = impact.unconfirmed(conn, run, {
        "kind": "CAPACITY", "branch": destination, "fields": cleaned, "corrects": None})
    confirmation_order, live = impact.live_cases(conn, run, found, lock=False)
    shown = impact.order(conn, run, live)[:config.REPORT_PREVIEW_CASES]
    got = impact.whatif_inputs(conn, run, confirmation_order, shown, pending)
    rows = []
    for case in shown:
        rec = cases.active_rec(conn, case)
        value = got.get(case["case_id"])
        if rec is None:
            rows.append(impact.row(case, None, error=impact.NO_VERSION))
            continue
        if not isinstance(value, tuple):
            rows.append(impact.row(case, rec, error=impact.GONE.format(lot=case["lot_id"])))
            continue
        inp, label = value
        if label == rec["rec_version"]:
            rows.append(impact.row(case, rec))
            continue
        try:
            raw = from_dict(to_dict(inp))
            cached = db.one(conn, "SELECT result FROM engine_cache WHERE cache_key=:k",
                            k=evaluate.cache_key(raw))
            result = cached["result"] if cached else worker.run(engine_evaluate, raw)
            key = selection.plan_in_force(case, rec)[0]
            allowed = not check_plan(raw, parse_plan(key))
            rows.append(impact.row(case, rec, rec_version(raw), result, allowed))
        except impact.ENGINE_ERRORS as exc:
            rows.append(impact.row(case, rec, error=str(exc)))
    return rows


def _select_capacity_case(conn, run):
    from api import cases, db, selection
    from api.simplerule import parse_plan
    from engine import from_dict
    for case in db.rows(conn, 'SELECT * FROM cases WHERE run_id=:r AND state=ANY(:states) '
                        'ORDER BY key COLLATE "C", case_id COLLATE "C"',
                        r=run["run_id"], states=list(cases.LIVE_STATES)):
        rec = cases.active_rec(conn, case)
        key = selection.plan_in_force(case, rec)[0]
        if not key or not key.startswith("tr:"):
            continue
        inp, plan = from_dict(rec["engine_input"]), parse_plan(key)
        if inp.product.zone != "chilled":
            continue
        fields = _capacity_fields(run, inp, plan.dest)
        if any(row["changed"] is True for row in _capacity_preview_rows(conn, run, plan.dest, fields)):
            return case
    raise ValueError("No suitable chilled transfer case with a changed displayed half-capacity preview")


def select_case(conn, run, selector):
    """Select by tag priority, then case key in codepoint order; no pasted IDs."""
    from api import cases, db, selection
    if selector.get("rule") == S06_RULE:
        if selector != {"rule": S06_RULE, "target": "destination"}:
            raise ValueError("Invalid eval case selector for half-capacity preview")
        return _select_capacity_case(conn, run)
    tags = selector.get("tags", [selector.get("tag")])
    if not tags or any(not isinstance(t, str) or not t for t in tags) or set(selector) - {
            "tags", "tag", "rule", "target"}:
        raise ValueError("Invalid eval case selector")
    if selector.get("rule") not in (None, "transfer_with_plan_changing_fact"):
        raise ValueError("Unknown eval case selector rule")
    for tag in tags:
        for case in db.rows(conn, 'SELECT * FROM cases WHERE run_id=:r AND tag=:t '
                            'ORDER BY key COLLATE "C", case_id COLLATE "C"', r=run["run_id"], t=tag):
            if selector.get("rule"):
                key, checks, _ = selection.plan_in_force(case, cases.active_rec(conn, case))
                if not key or not key.startswith("tr:"):
                    continue
                try:
                    _fact(checks)
                except ValueError:
                    continue
            return case
    raise ValueError("No case matches eval selector " + json.dumps(selector, sort_keys=True))


def _alternative(inp, result, plan, ref):
    from engine.limits import check_plan
    from engine.model import Plan, TRANSFER
    rejected = sorted((r for r in result["rejected"] if r.get("dest") and r["dest"] != plan.dest),
                      key=lambda r: (r["dest"], r["run_code"] or "", r["label"]))
    if rejected:
        return {"store": rejected[0]["dest"], "reasons": rejected[0]["reasons"]}
    for store in sorted(set(ref.branches) - {inp.case_lot().branch, plan.dest}):
        runs = sorted((r for r in inp.runs if r.dest == store), key=lambda r: r.run_code)
        run_code = runs[0].run_code if runs else plan.run_code
        reasons = check_plan(inp, Plan(TRANSFER, plan.pct, plan.qty, store, run_code))
        if reasons:
            return {"store": store, "reasons": reasons}
        rows = [row for row in result["options"] if row.get("dest") == store]
        if rows:
            return {"store": store, "reasons": ["Lower-ranked engine option"],
                    "comparison": {"in_force": next(r for r in result["options"] if r["key"] == plan.key),
                                   "alternative": rows[0]}}
    raise ValueError("No alternative store for the frozen why-not question")


def _resolve(value, inputs):
    if isinstance(value, str):
        return value.format_map(inputs)
    if isinstance(value, list):
        return [_resolve(v, inputs) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, inputs) for k, v in value.items()}
    return value


def _report_refs(conn, run, scenario_id, inputs, expected, inp, ref, modules):
    """Use the upstream hand-report/preview contracts, never an AI extraction."""
    from api import cases
    import hashlib
    reports, impact = modules["api.reports"], modules["api.impact"]
    if scenario_id == "s06":
        if inp.product.zone != "chilled":
            raise ValueError("Scenario 6 requires a chilled product")
        fields = _capacity_fields(run, inp, inputs["dest_store"])
        kind = "CAPACITY"
        actor = {"role": "manager", "branch": inputs["dest_store"]}
        expected["reading_fields"] = {"kind": kind, **fields}
    else:
        quote, pdf = modules["api.preload"].quote_for_run(run, inputs["run_code"], ambiguous=True)
        amount = quote["amount"]
        inputs["quote_pdf"] = {**quote, "sha256": hashlib.sha256(pdf).hexdigest()}
        fields = {"what": "QUOTE", "run_code": inputs["run_code"], "amount": amount,
                  "pricing_basis": None, "includes_handling": None, "valid_until": None}
        kind, actor = "TRANSPORT", {"role": "hq", "branch": None}
        expected["reading_fields"] = {"kind": kind, **fields}
    inputs["report_fields"] = deepcopy(fields)
    report = reports.create(conn, run, actor, kind, fields, "", inputs["case_id"], None)
    preview = impact.preview(conn, run, actor, report["signal_id"], report["revision"])
    if scenario_id == "s06" and not any(row.get("changed") is True and row.get("plan_now")
                                       and row.get("plan_if") for row in preview["cases"]):
        raise ValueError("Scenario 6 real preview has no changed displayed recommendation")
    # Explicit stable projection: never copy a random signal/result ID or time.
    expected["preview"] = {"rows": [{k: row[k] for k in (
        "case_id", "plan_now", "plan_if", "benefit_now", "benefit_if", "changed") if k in row}
        for row in preview["cases"]], "more": preview["more"]}
    if scenario_id == "s06":
        expected["summary_max_words"] = _summary_words()


def _scenario(conn, run, scenario):
    from api import cases, refdata, selection, signals, workflow
    from api.simplerule import parse_plan
    from engine import from_dict
    sid = scenario["id"]
    modules = {}
    if sid in PENDING:
        modules = {name: _upstream(name) for name, _ in PENDING[sid]}
        pending = [plan_id for name, plan_id in PENDING[sid] if modules[name] is None]
        if pending:
            return {"pending": pending}
        if sid == "s06" and _summary_words() is None:
            return {"pending": ["10-14"]}
    case = select_case(conn, run, scenario["selector"])
    rec = cases.active_rec(conn, case)
    if rec is None:
        raise ValueError("Selected eval case has no engine result")
    key, checks, _ = selection.plan_in_force(case, rec)
    inp, plan = from_dict(rec["engine_input"]), parse_plan(key)
    ref = refdata.get(run["dataset_version"])
    inputs = {"case_id": case["case_id"], "case_key": case["key"], "tag": case["tag"],
              "lot_id": case["lot_id"], "source_store": case["source_branch"],
              "dest_store": plan.dest, "run_code": plan.run_code}
    expected = {"plan_in_force": key}
    sid = scenario["id"]
    if sid == "s01":
        expected["fact"] = _fact(checks)
        expected["alternative"] = _alternative(inp, rec["result"], plan, ref)
        inputs["alternative_store"] = expected["alternative"]["store"]
    elif sid == "s02":
        args = {"overrides": [{"kind": "run_available", "run_code": plan.run_code, "value": False}]}
        result = workflow.whatif(conn, run, case["case_id"], args["overrides"])
        expected.update(simulate_args=args, after_plan=result["result"]["recommended"], result=result["result"])
    elif sid == "s03":
        expected["md30"] = next(r for r in rec["result"]["options"] if r["key"] == "md30")
        expected["in_force"] = next(r for r in rec["result"]["options"] if r["key"] == key)
    elif sid == "s05":
        expected["truth_probe"] = {"tables": ["ref_truth_demand", "truth_lots"],
                                  "keys": {"dataset_version": run["dataset_version"], "sku": case["sku"],
                                           "store": case["source_branch"], "day": run["day"],
                                           "lot_id": case["lot_id"], "world": "shelfshift"}}
    elif sid == "s08":
        fact = _fact(checks)
        actor = {"role": "manager", "branch": case["source_branch"]}
        request = signals.request_evidence(conn, run, actor, case["case_id"], rec["rec_version"], fact["fact_id"])
        expected["request_fields"] = {**fact, "lot_ids": request["lot_ids"], "part": request["part"]}
        inputs["request_fields"] = deepcopy(expected["request_fields"])
        # The typed count deliberately conflicts with the zero-count reply; it
        # is a test input derived from the record, never from the photo/truth.
        inputs["typed_counts"] = {"shelf_units": max(1, int(inp.case_lot().qty)), "backroom_units": 0}
        expected["typed_counts"] = deepcopy(inputs["typed_counts"])
    if sid in PENDING:
        _report_refs(conn, run, sid, inputs, expected, inp, ref, modules)
    inputs["steps"] = _resolve(scenario["steps"], inputs)
    return {"resolved_inputs": inputs, "expected": expected}


def build_refs(conn, template_run_id, scenarios):
    """Build sorted, deterministic drafts; retire every independent clone."""
    from api import db, sandbox
    from engine.model import ENGINE_VERSION
    template = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r AND kind='TEMPLATE'", r=template_run_id)
    if template is None:
        raise ValueError("No such reference template")
    out = {}
    for scenario in scenarios:
        run_id = sandbox.clone(conn, template)
        try:
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id=:r", r=run_id)
            out[scenario["id"]] = _scenario(conn, run, scenario)
        finally:
            db.run(conn, "UPDATE demo_runs SET status='RETIRED' WHERE run_id=:r", r=run_id)
    return {"dataset_version": template["dataset_version"], "engine_version": ENGINE_VERSION,
            "template_ref": template_run_id, "scenarios": dict(sorted(out.items()))}


def build_refs_main(argv=None):
    from api import db, sandbox
    parser = argparse.ArgumentParser(prog="agent.eval build-refs")
    parser.add_argument("--template", default="active")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    scenarios = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    with db.tx() as conn:
        template = sandbox.active_template(conn) if args.template == "active" else {"run_id": args.template}
        if template is None:
            raise ValueError("No active reference template")
        doc = build_refs(conn, template["run_id"], scenarios)
    Path(args.out).write_text(json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pending = [(sid, row["pending"]) for sid, row in doc["scenarios"].items() if "pending" in row]
    if pending:
        print("PENDING: " + "; ".join(f"{sid}: {', '.join(plans)}" for sid, plans in pending))
        return 3
    print("Draft references built; approval and locking belong to 10-19")
    return 0
