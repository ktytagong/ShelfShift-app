"""The public template build and its reviewer cases by a stated rule (spec §5 Public dataset, §8 Template build;
05.2-CONTEXT D-18, D-21, D-25, D-26, D-38; api/catalogue.py).

On the tiny public set, `sandbox.build_template` screens every morning into flags, warms up with the autopilot (which
finds no open case: the warm-up scope is none), then on the open day evaluates flagged lots in the stated order (the
screen's stock at the step day, then Keep waste, descending, then key) until every kind is tagged or the budget is
spent, runs the units and limit checks, and goes ACTIVE. The kinds' definitions are written again here, by hand, from
D-38 and the engine's documented fields, so a test never just repeats the module's own predicate. Cases are found by
tag; no number below is pasted from a run.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from api import catalogue, db, refdata, sandbox
from api.app import app
from tests.api.conftest import drop_runs, fresh, sign_in

# the kinds of D-38 in the reference order (each tag the reference names, its report name)
NAMES = {"verify": "verify-before-move", "transfer": "transfer", "markdown": "markdown", "keep": "keep",
         "cold_chain": "cold-chain-excluded", "transfers_unavailable": "transfers-unavailable",
         "close_call": "close-call"}
VERIFY_FACTS = ("PHANTOM_STOCK", "STALE_COUNT")


def matches(tag: str, facts: dict) -> bool:
    """D-38's definition of each kind, written from the engine's documented fields (not catalogue.kind_of)."""
    result, checks, elig = facts["result"], facts["fact_checks"], facts["eligibility"]
    if result is None:
        return False
    rec, min_regret = result["recommended"], facts["engine_input"]["policy"]["min_regret"]
    return {
        "verify": any(c["kind"] in VERIFY_FACTS and c["pick_changed"] and c["decision_regret"] is not None
                      and c["decision_regret"] >= min_regret for c in checks),
        "transfer": rec.startswith("tr:"),
        "markdown": rec.startswith("md"),
        "keep": rec == "keep",
        "cold_chain": any("cold chain" in r["reasons"] for r in result["rejected"])
                      or any(x["reason"] == "cold chain" for x in elig["excluded"]),
        "transfers_unavailable": elig["status"] == "unavailable_unmapped",
        "close_call": result["scenario_wins"]["wins"] == 2,
    }[tag]


def case_facts(conn, run: dict, case: dict) -> dict:
    rec = db.one(conn, "SELECT * FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                 r=case["run_id"], c=case["case_id"], v=case["active_rec_version"]) if case["active_rec_version"] \
        else None
    from api import eligibility
    return {"result": rec["result"] if rec else None, "fact_checks": rec["fact_checks"] if rec else [],
            "engine_input": rec["engine_input"] if rec else None, "eligibility": eligibility.summary(conn, run, case)}


def stated_order(rows: list[dict]) -> list[str]:
    """D-38's order, by hand: stock at the step day, then Keep waste, both descending (a flag the screen could not
    judge last), then key by codepoint."""
    def k(f):
        return (f["stock_at_step"] is None, -(f["stock_at_step"] or 0.0), f["keep_waste"] is None,
                -(f["keep_waste"] or 0.0), f["key"])
    return [f["key"] for f in sorted(rows, key=k)]


def session_client() -> TestClient:
    c = TestClient(app)
    sign_in(c)
    return c


# --- tests that need the synthetic template ACTIVE run first: the module's public template replaces it once built

def test_on_the_synthetic_template_a_new_session_sits_at_the_first_branch(template, schema):
    with db.tx() as conn:
        active = sandbox.active_template(conn)
        assert active["run_id"] == template
        assert catalogue.start_seat(conn, active) is None  # no catalogue on the synthetic set
    s = session_client().get("/api/session").json()
    assert (s["role"], s["branch"]) == ("manager", refdata.get(schema).branches[0])


def test_a_template_with_budget_zero_still_builds_and_reports_every_kind_missing(template, public_dv, monkeypatch):
    real = catalogue.rule
    monkeypatch.setattr(catalogue, "rule", lambda ref: {**real(ref), "budget": 0})
    with db.tx() as conn:
        before = sandbox.active_template(conn)["run_id"]
    run_id = sandbox.build_template(public_dv)  # a missing kind never fails the build (05.2-18 is the gate)
    try:
        with db.tx() as conn:
            run = fresh(run_id)
            assert run["status"] == "ACTIVE"
            rep = catalogue.report(conn, run)
            assert db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r", r=run_id)["n"] == 0
            assert catalogue.problems(conn, run) == []
        assert (rep["budget"], rep["evaluated"], rep["order"]) == (0, 0, [])
        assert rep["flagged"] > 0  # the open day's flags stay visible, unevaluated (D-38)
        assert rep["missing"] == list(NAMES.values())
        assert all(v["case"] is None and v["count"] == 0 for v in rep["kinds"].values())
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :p", p=run_id)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=before)
        with db.tx() as conn:
            drop_runs(conn, [run_id])


# --- the module's public template

def test_the_public_template_tags_each_kind_it_finds_by_the_stated_rule(public_template, public_dv):
    ref = refdata.get(public_dv)
    rule = ref.settings["catalogue"]
    with db.tx() as conn:
        run = fresh(public_template)
        assert run["status"] == "ACTIVE" and run["day"] == ref.calendar["demo_open_day"]
        rep = catalogue.report(conn, run)
        evaluated = {c["case_id"]: c for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r", r=run["run_id"])}
        facts = {cid: case_facts(conn, run, c) for cid, c in evaluated.items()}
    assert rep["budget"] == rule["budget"] and rep["evaluated"] <= rep["budget"]
    assert rep["evaluated"] == len(rep["order"]) == len(evaluated) and set(rep["order"]) == set(evaluated)
    assert all(c["opened_day"] == run["day"] for c in evaluated.values())  # the warm-up acted on nothing (scope none)
    tags = {c["tag"]: cid for cid, c in evaluated.items() if c["tag"] in NAMES}
    assert len(tags) == sum(1 for c in evaluated.values() if c["tag"] in NAMES)  # no two cases share a kind's tag
    for tag, cid in tags.items():
        case = evaluated[cid]
        assert matches(tag, facts[cid]), f"{cid} is tagged {tag} but its outcome is not one"
        assert case["state"] in ("OPEN", "WAITING", "PENDING_APPROVAL") and case["active_rec_version"]
        assert rep["kinds"][NAMES[tag]]["case"]["key"] == cid
        # the first evaluated item of its kind, unless an earlier one already carries an earlier kind's tag
        for earlier in rep["order"][:rep["order"].index(cid)]:
            if matches(tag, facts[earlier]):
                assert evaluated[earlier]["tag"] in NAMES and \
                    rule["kinds"].index(evaluated[earlier]["tag"]) < rule["kinds"].index(tag), (tag, earlier)
    for tag, name in NAMES.items():
        count = sum(1 for cid in rep["order"] if matches(tag, facts[cid]))
        assert rep["kinds"][name]["count"] == count and rep["kinds"][name]["tag"] == tag
    # kinds that do not occur are reported, never forced
    assert rep["missing"] == [NAMES[t] for t in rule["kinds"]
                              if not any(matches(t, facts[cid]) for cid in rep["order"])]
    assert rep["missing"] == [NAMES[t] for t in rule["kinds"] if t not in tags]
    with db.tx() as conn:
        assert catalogue.problems(conn, run) == []


def test_the_catalogue_evaluates_the_flags_in_the_stated_order(public_template):
    with db.tx() as conn:
        run = fresh(public_template)
        rows = db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r AND day = :d", r=run["run_id"], d=run["day"])
        rep = catalogue.report(conn, run)
    assert rep["flagged"] == len(rows)
    assert rep["order"] == stated_order(rows)[:rep["evaluated"]]
    # it stopped because every kind was tagged, the budget was spent, or nothing was left to evaluate
    assert not rep["missing"] or rep["evaluated"] in (rep["budget"], len(rows))


def test_the_public_template_adds_up_and_breaks_no_limit(public_template, public_dv):
    ref, truth = refdata.get(public_dv), refdata.truth(public_dv)
    with db.tx() as conn:
        run = fresh(public_template)
        sandbox.network_check(conn, run, ref, truth)  # raises TemplateError naming the world and product
        sandbox.limits_check(conn, run, ref)
        assert catalogue.problems(conn, run) == []


def test_the_catalogue_report_names_the_rule_every_kind_and_the_start_seat(public_template, tmp_path):
    out = tmp_path / "catalogue.json"
    assert catalogue.main(["report", "--template", public_template, "--out", str(out)]) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert {"rule", "budget", "flagged", "evaluated", "order", "kinds", "missing", "start_seat"} <= set(rep)
    assert list(rep["kinds"]) == list(NAMES.values())
    for name, entry in rep["kinds"].items():
        case = entry["case"]
        if case is not None:
            assert {"key", "store", "product", "lot", "recommended", "deciding"} <= set(case)
    with db.tx() as conn:
        assert rep["start_seat"] == catalogue.start_seat(conn, fresh(public_template))


def test_a_new_session_on_the_public_template_sits_at_the_start_seat(public_template):
    with db.tx() as conn:
        template = fresh(public_template)
        seat = catalogue.start_seat(conn, template)
        verify = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = 'verify'", r=public_template)
        transfer = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = 'transfer'", r=public_template)
    # the source store of the first verify-before-move case, else of the first transfer case (D-38)
    assert seat == (verify or transfer)["source_branch"]
    s = session_client().get("/api/session").json()
    assert (s["role"], s["branch"]) == ("manager", seat)


def test_every_workspace_cloned_from_the_template_has_the_tagged_cases(public_template):
    with db.tx() as conn:
        tagged = db.rows(conn, "SELECT case_id, tag, state, active_rec_version FROM cases WHERE run_id = :r AND tag "
                               "= ANY(:t) ORDER BY case_id", r=public_template, t=list(NAMES))
        a = sandbox.clone(conn, fresh(public_template))
        b = sandbox.clone(conn, fresh(public_template))
        for ws in (a, b):
            assert db.rows(conn, "SELECT case_id, tag, state, active_rec_version FROM cases WHERE run_id = :r AND "
                                 "tag = ANY(:t) ORDER BY case_id", r=ws, t=list(NAMES)) == tagged
        drop_runs(conn, [a, b])
    assert tagged


def test_the_tiny_set_exercises_the_judges_kinds(public_template, public_dv):
    ref = refdata.get(public_dv)
    with db.tx() as conn:
        run = fresh(public_template)
        by_tag = {c["tag"]: c for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = ANY(:t)",
                                               r=run["run_id"], t=list(NAMES))}
        assert {"transfer", "verify", "transfers_unavailable"} <= set(by_tag), sorted(by_tag)
        verify = case_facts(conn, run, by_tag["verify"])
        fact = next(c for c in verify["fact_checks"] if c["kind"] in VERIFY_FACTS and c["pick_changed"])
        origins = {r["origin"] for r in db.rows(conn, "SELECT origin FROM lots WHERE run_id = :r AND world = "
                                                      "'shelfshift' AND lot_id = ANY(:l)", r=run["run_id"],
                                                l=fact["lot_ids"])}
    assert origins == {"designed"}  # the count the fact asks for is of the fixture's designed example (D-18)
    assert not ref.store_known(by_tag["transfers_unavailable"]["source_branch"])  # the fixture's unmapped store
    assert ref.store_known(by_tag["transfer"]["source_branch"])
