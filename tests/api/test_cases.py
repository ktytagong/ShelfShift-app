"""The Day-3 board and case detail in the contract's shapes (04-API-CONTRACT "Shapes"; 04-CONTEXT D-16).

Beats only: cases are found by tag (SUS-19 is the only stable key), branches and runs come from the seed, and no
seed quantity, amount or plan key is asserted (D-14). Engine figures are compared with the stored result, never
recomputed.
"""
import json
import re

from api import cases, db, refdata, sandbox, settings
from data.world import demo_date
from engine.model import KEEP, MARKDOWN, TRANSFER, Plan
from engine.timing import fmt_min
from tests.api.conftest import by_tag, fresh, seat

CARD_KEYS = {"case_id", "key", "tag", "origin", "state", "column", "sku", "product", "zone", "source", "dest", "lot_id",
             "suggested", "suggested_key", "at_risk_units", "at_risk_kg", "at_risk_value", "est_benefit", "due", "decide_by",
             "markdown_step", "act_before", "wait", "rec_version", "next_owner", "decisions", "hold", "badges",
             "reason", "waiting_until_day",
             # 06.1-10 (D-23, D-24, D-54): the step keys, computed once on the server (api/stepkeys.py)
             "step", "step_number", "stage", "first_stage", "owner", "next_action", "blocked", "blocked_reasons",
             "closed_as",
             "selected_plan",  # selected_plan: 07-10 (07-CONTEXT D-23; the selection bound to the active version)
             "lot_status", "predecessor", "successor"}  # 07-11 (07-CONTEXT D-24; successor links arrive in 07-12)
DETAIL_KEYS = CARD_KEYS | {"lot", "adopted", "adopted_ended", "requests", "result", "fact_checks", "summary",
                           "engine_input", "versions", "approvals", "eligibility",  # eligibility: 05.2-14
                           "comparison",  # comparison: 06.1-04 (D-27)
                           "review_flags",  # review_flags: 07-06 (07-CONTEXT D-17)
                           "holds",  # holds: 07-08 (07-CONTEXT D-13; the ACTIVE lot holds on the involved lots)
                           "commitment",  # commitment: 07-09 (07-CONTEXT D-12; blocks, advisory, ack, next_actions)
                           "options",  # options: 07-10 (07-CONTEXT D-23; the engine options for Select another plan)
                           "reports",  # reports: 07-13 (07-CONTEXT D-16; the unconfirmed field reports in scope)
                           "predecessor_closed"}  # predecessor_closed: 07-22 (07-CONTEXT D-13, D-24; the banner's closed predecessor)
CHECK_KEYS_07 = ["class", "owner_branch", "requests_left", "adoptable", "blocked_label", "reasons"]  # 07-09
LOT_KEYS = {"lot_id", "branch", "qty", "expiry_day", "expiry_source", "markdown_pct", "last_verified_day", "verified_by",
            "origin", "tag"}
VERSION_KEYS = {"rec_version", "effective_version", "engine_version", "dataset_version", "forecast_run_id",
                "forecast_providers", "forecast_series_providers",  # 11-08: approved per-series provenance
                "forecast_headline", "forecast_cutoff", "day", "synthetic", "label"}
APPROVAL_KEYS = {"round", "required", "decisions", "hold", "resources", "next_owner"}
FACT_KEYS = ["fact_id", "kind", "lot_id", "lot_ids", "branch", "recorded_value", "alt_value", "regret",
             "decision_regret", "unserved_units", "net_revenue_difference", "waste_policy_difference",
             "waste_kg_difference", "best_plan_if_alt", "best_plan_if_alt_label", "pick_changed",
             "pick_feasible_if_alt", "changes_plan"]  # the contract's order, which is the engine's
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WASTE_PRICE = 0.28  # a test waste price per kg inside the D-37 bound, above 0


def board(run_id):
    with db.tx() as conn:
        return cases.board(conn, fresh(run_id))


def detail(run_id, case_id):
    with db.tx() as conn:
        return cases.detail(conn, fresh(run_id), case_id)


def stored(run_id, case_id):
    with db.tx() as conn:
        return cases.active_rec(conn, cases.case_row(conn, run_id, case_id))


def priced(run_id, price):
    r = fresh(run_id)
    with db.tx() as conn:
        settings.write_settings(conn, r, seat(None), {"waste_price": price}, r["version"])
    return detail(run_id, cases.STORY_KEY)


def test_the_milk_story_card(run):
    milk = next(c for c in board(run["run_id"]) if c["key"] == cases.STORY_KEY)
    assert set(milk) == CARD_KEYS
    source = by_tag(run, cases.STORY_TAG)["source_branch"]
    assert (milk["key"], milk["tag"], milk["column"], milk["source"]) == (cases.STORY_KEY, cases.STORY_TAG, "New",
                                                                         source)
    assert milk["origin"] == "designed"
    assert milk["next_owner"] == [{"role": "manager", "branch": source}]
    assert milk["due"]["day"] == run["day"] and milk["markdown_step"]["day"] == run["day"] + 1
    rec = stored(run["run_id"], milk["case_id"])
    assert milk["decide_by"] == rec["result"]["decide_by"]  # as returned, never recomputed
    assert (milk["due"]["day"], milk["due"]["min"]) == (milk["decide_by"]["day"], milk["decide_by"]["latest_decision_min"])
    # the wait object: the engine's run, day and minute plus that run's departure; no engine wording
    wait = milk["wait"]
    assert "text" not in wait and set(wait) == {"run_code", "day", "latest_decision_min", "departs_min"}
    ref = refdata.get(run["dataset_version"])
    run_row = next(r for r in ref.runs if r.run_code == wait["run_code"] and r.day == wait["day"])
    assert wait["departs_min"] == run_row.departs_min
    # the unverified badge is a decision regret with its parts as numbers, never cash at stake
    badge = next(b for b in milk["badges"] if b["kind"] == "unverified")
    assert badge["decision_regret"] > 0
    assert {"fact_ids", "net_revenue_difference", "waste_policy_difference"} <= set(badge)
    assert "at stake" not in badge["text"] and not re.search(r"\d", badge["text"]) and ref.currency not in badge["text"]


def test_live_cases_sort_by_deadline_before_resolved_ones(run):
    with db.tx() as conn:  # one reviewer case resolved by hand: it has no deadline any more
        db.run(conn, "UPDATE cases SET state = 'REJECTED', reason = 'test' WHERE run_id = :r AND tag = 'bread-keep'",
               r=run["run_id"])
    cards = board(run["run_id"])
    dues = [c["due"] for c in cards]
    first_none = dues.index(None)
    assert all(d is not None for d in dues[:first_none]) and all(d is None for d in dues[first_none:])
    rejected = next(c for c in cards if c["tag"] == "bread-keep")
    assert rejected["due"] is None and cards.index(rejected) >= first_none
    live = cards[:first_none]
    assert live == sorted(live, key=lambda c: (c["due"]["day"] * 1440 + c["due"]["min"], -(c["at_risk_value"] or 0),
                                               c["key"]))
    # equal deadlines order the same way in every sandbox
    with db.tx() as conn:
        other = sandbox.clone(conn, sandbox.active_template(conn))
        db.run(conn, "UPDATE cases SET state = 'REJECTED', reason = 'test' WHERE run_id = :r AND tag = 'bread-keep'",
               r=other)
    assert [c["key"] for c in board(other)] == [c["key"] for c in cards]


def test_the_template_summary_names_the_plan_and_the_unverified_fact(run):
    d = detail(run["run_id"], cases.STORY_KEY)
    phantom, _ = sandbox.phantom_lot()
    assert d["summary"].startswith("The engine recommends")
    assert phantom in d["summary"] and "Unverified:" in d["summary"] and "decision regret" in d["summary"]
    assert refdata.get(run["dataset_version"]).currency in d["summary"] and "at stake" not in d["summary"]
    wait_text = stored(run["run_id"], cases.STORY_KEY)["result"]["wait"]["text"]
    assert wait_text not in d["summary"] and "transfer becomes impossible" not in d["summary"]
    assert not re.search(r"\bday -?\d", d["summary"])


def test_the_summary_words_a_not_allowed_fact_with_no_regret_figure(run):
    # spec §6 and 05-CONTEXT D-16: a fact under which the recommended plan breaks a hard limit is never a regret figure
    rec = stored(run["run_id"], cases.STORY_KEY)
    ref = refdata.get(run["dataset_version"])
    allowed = next(c for c in rec["fact_checks"] if c["changes_plan"] and c["pick_feasible_if_alt"])
    label = f"{allowed['branch']} {', '.join(allowed['lot_ids'])} ({allowed['kind'].replace('_', ' ').lower()})"
    not_allowed = label + ". If this fact is true, the recommended plan is not allowed."
    # the D-16 case: not allowed, with every regret figure 0
    zero = {**allowed, "pick_feasible_if_alt": False, "changes_plan": True, "regret": 0.0, "decision_regret": 0.0,
            "net_revenue_difference": 0.0, "waste_policy_difference": 0.0}
    text = cases.summary(rec["result"], [zero], ref, waste_price=WASTE_PRICE)
    assert text.startswith("The engine recommends")
    tail = text.split("Unverified: ", 1)[1]
    assert tail == not_allowed
    assert ref.currency not in tail and "regret" not in tail
    # a not-allowed fact keeping its stored regret still has no figure; an allowed one keeps its decision regret
    blocked = {**allowed, "pick_feasible_if_alt": False}
    text = cases.summary(rec["result"], [blocked, allowed], ref, waste_price=WASTE_PRICE)
    facts = [f.strip() for f in text.split("Unverified: ")[1:]]
    assert facts[0] == not_allowed
    assert facts[1] == (label + "; decision regret " + cases.money(ref, allowed["decision_regret"])
                        + " (net revenue difference " + cases.money(ref, allowed["net_revenue_difference"])
                        + ", waste policy difference " + cases.money(ref, allowed["waste_policy_difference"]) + ").")
    assert "at stake" not in text and re.search(r"\bday -?\d", text) is None


def test_the_unverified_badge_counts_a_not_allowed_fact_below_min_regret(run):
    rec = stored(run["run_id"], cases.STORY_KEY)
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], cases.STORY_KEY)
    floor = rec["engine_input"]["policy"]["min_regret"]
    assert floor > 0  # a below-floor fact needs a floor above 0
    allowed = next(c for c in rec["fact_checks"]
                   if c["changes_plan"] and c["pick_feasible_if_alt"] and c["regret"] >= floor)
    # 02-CONTEXT D-29: how the engine reports a not-allowed fact whose recorded plan outscores the best plan
    blocked = {**allowed, "fact_id": "not-allowed " + allowed["fact_id"], "pick_feasible_if_alt": False,
               "changes_plan": True, "regret": 0.0, "decision_regret": 0.0,
               "net_revenue_difference": -allowed["decision_regret"], "waste_policy_difference": 0.0}

    def unverified(r, requests=()):
        return [b for b in cases.badges(case, r, None, list(requests)) if b["kind"] == "unverified"]

    (base,) = unverified(rec)
    assert base["decision_regret"] > 0 and blocked["fact_id"] not in base["fact_ids"]
    # only a not-allowed fact: it counts, with no amount
    (only,) = unverified({**rec, "fact_checks": [blocked]})
    assert only["fact_ids"] == only["not_allowed_fact_ids"] == [blocked["fact_id"]]
    assert all(only[k] is None for k in ("decision_regret", "net_revenue_difference", "waste_policy_difference"))
    assert only["text"] == "Unverified"
    # mixed: a not-allowed fact adds no amount
    (mixed,) = unverified({**rec, "fact_checks": [blocked, *rec["fact_checks"]]})
    assert mixed["fact_ids"] == [blocked["fact_id"], *base["fact_ids"]]
    assert mixed["not_allowed_fact_ids"] == [blocked["fact_id"], *base["not_allowed_fact_ids"]]
    for k in ("decision_regret", "net_revenue_difference", "waste_policy_difference"):
        assert mixed[k] == base[k]
    # asked: an open request on the active version leaves no unverified badge
    asked = [{"fact_id": blocked["fact_id"], "state": "OPEN", "rec_version": rec["rec_version"]}]
    kinds = [b["kind"] for b in cases.badges(case, {**rec, "fact_checks": [blocked]}, None, asked)]
    assert "evidence_requested" in kinds and "unverified" not in kinds


def test_the_summary_leaves_out_the_waste_policy_part_at_a_zero_waste_price(run):
    # PROJECT.md (regret under a waste price): at 0 the second line is absent; above 0 the two parts stay separate
    ref = refdata.get(run["dataset_version"])

    def first_allowed(d):
        c = next(c for c in d["fact_checks"] if c["changes_plan"] and c["pick_feasible_if_alt"])
        return c, f"{c['branch']} {', '.join(c['lot_ids'])} ({c['kind'].replace('_', ' ').lower()})"

    d = priced(run["run_id"], 0.0)
    assert d["engine_input"]["policy"]["waste_price"] == 0
    allowed, label = first_allowed(d)
    assert ("Unverified: " + label + "; decision regret " + cases.money(ref, allowed["decision_regret"])
            + " (net revenue difference " + cases.money(ref, allowed["net_revenue_difference"]) + ").") in d["summary"]
    assert "waste policy" not in d["summary"]
    d = priced(run["run_id"], WASTE_PRICE)
    assert d["engine_input"]["policy"]["waste_price"] == WASTE_PRICE
    allowed, label = first_allowed(d)
    assert ("Unverified: " + label + "; decision regret " + cases.money(ref, allowed["decision_regret"])
            + " (net revenue difference " + cases.money(ref, allowed["net_revenue_difference"])
            + ", waste policy difference " + cases.money(ref, allowed["waste_policy_difference"])
            + ").") in d["summary"]
    assert re.search(r"\bday -?\d", d["summary"]) is None


def test_detail_returns_every_fact_check_in_engine_order(run):
    d = detail(run["run_id"], cases.STORY_KEY)
    assert set(d) == DETAIL_KEYS and set(d["lot"]) == LOT_KEYS
    assert set(d["versions"]) == VERSION_KEYS and set(d["approvals"]) == APPROVAL_KEYS
    rec = stored(run["run_id"], cases.STORY_KEY)
    # every check, in the engine's list order, none cut; each the engine's keys in its order, then the commitment
    # rule's keys for the plan in force (07-09, 07-CONTEXT D-12; 04-API-CONTRACT "Phase 7 card, detail and session keys")
    assert [{k: c[k] for k in FACT_KEYS} for c in d["fact_checks"]] == rec["fact_checks"]
    assert d["fact_checks"] and all(list(c) == FACT_KEYS + CHECK_KEYS_07 for c in d["fact_checks"])
    assert d["engine_input"] == rec["engine_input"] and d["versions"]["rec_version"] == rec["rec_version"]
    assert d["versions"]["synthetic"] is True and d["versions"]["engine_version"] == rec["result"]["engine_version"]
    assert d["approvals"]["required"] == (["source", "destination"]
                                          if cases.parse_plan(rec["result"]["recommended"]).kind == TRANSFER
                                          else ["source"])


def test_detail_never_carries_the_engines_wait_text(run):
    cards = board(run["run_id"])
    recs = {c["case_id"]: stored(run["run_id"], c["case_id"]) for c in cards}
    assert any(r and (r["result"].get("wait") or {}).get("text") for r in recs.values())  # stored: the views drop it
    for c in cards:
        d = detail(run["run_id"], c["case_id"])
        for view in (c, d):
            assert view["wait"] is None or "text" not in view["wait"]
        if d["result"] is not None and d["result"].get("wait") is not None:
            assert "text" not in d["result"]["wait"]
            assert d["result"]["wait"] == {k: v for k, v in recs[c["case_id"]]["result"]["wait"].items() if k != "text"}
        # nowhere in the board or the detail JSON is there a "text" key under a wait object
        for blob in (json.dumps(c), json.dumps(d)):
            assert not re.search(r'"wait": \{[^{}]*"text"', blob)


def test_deadline_labels_name_the_weekday(run):
    labelled = [c for c in board(run["run_id"]) if c["due"]]
    assert labelled
    for c in labelled:
        due = c["due"]
        day = demo_date(due["day"])
        assert due["date"] == day.isoformat() and due["weekday"] == WEEKDAYS[day.weekday()]
        assert due["label"] == f"Decide by {WEEKDAYS[day.weekday()]} {fmt_min(due['min'])}"
        assert "day " not in due["label"]
    cutoff = detail(run["run_id"], cases.STORY_KEY)["versions"]["forecast_cutoff"]
    assert cutoff.split()[0] in WEEKDAYS and "day " not in cutoff


def test_parse_plan_round_trips_every_key():
    for p in (Plan(KEEP), Plan(MARKDOWN, 20), Plan(MARKDOWN, 30), Plan(TRANSFER, 20, 82, "Lat Phrao", "R-02"),
              Plan(TRANSFER, 0, 5, "Bang Kapi", "R-07"), Plan(TRANSFER, 30, 1, "Thonburi", "R-03")):
        assert cases.parse_plan(p.key) == p
    assert cases.parse_plan("tr:Lat%20Phrao:R-02:82:hold").dest == "Lat Phrao"
