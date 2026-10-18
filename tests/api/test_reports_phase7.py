"""Reports workspace figures on Phase 7's real flows, on the integrated server (08-18; 08-CONTEXT D-04, D-15, D-18,
D-35; 08 supervisor gap 7; 08-18-ACCESS-PRECHECK; 04-API-CONTRACT "Reports (08) as built").

Before Phase 7 merged, the Reports tests used contract-shaped stand-ins: a monkeypatched selection on
`reporting._plan_in_force`, a `committed` activity payload rewritten by UPDATE, and state changes written by UPDATE
for a cancel. Here every step is Phase 7's own HTTP route: Select another plan (`POST /api/cases/{id}/select`), Send,
the destination's accept and the source's approve (each naming the plan in force and acknowledging its Advisory
facts), Cancel approved plan (`POST /api/cases/{id}/cancel`, `closing.cancel`) and a count request
(`POST /api/cases/{id}/requests`, `signals.request_evidence`). The one test-side write is Phase 7's own deadline move,
`UPDATE evidence_requests SET due_at = now() - interval '1 second'`; nothing sleeps.

Every figure is checked against an oracle that does not use `api.reporting`: the plan in force read from the stored
rows (a commitment's `committed` entry for its current round; a live case's bound selection, else its recommendation),
the contract's activity definitions in SQL, and the Decision board's own cards (`next_owner`) for who acts next.
"""
from __future__ import annotations

import pytest

from api import cases, db, refdata
from engine.model import TRANSFER
from tests.api.conftest import as_seat, key, own
from tests.api.test_reporting_api import workspace_of
from tests.api.test_selection import active, feasible_others
from tests.api.verify_helpers import ack, shown

REASON = "Destination has no cold room space after all"  # a reason, as the cancel route requires
LIVE = ("OPEN", "WAITING", "PENDING_APPROVAL")
COMMITTED = ("APPROVED", "PREPARED", "EXECUTED", "OBSERVED")


def read(client, **params) -> dict:
    response = client.get("/api/reports/workspace", params=params)
    assert response.status_code == 200, response.json()
    return response.json()


def board(client, report: str, **params) -> list[dict]:
    """Every card of the board's `report=` drilldown, all pages."""
    out, page = [], 1
    while True:
        response = client.get("/api/cases", params={"report": report, "page": page, **params})
        assert response.status_code == 200, response.json()
        body = response.json()
        out += body["cases"]
        if page * body["page_size"] >= body["total"]:
            return out
        page += 1


def dest_in_force(conn, run_id: str, case: dict) -> str | None:
    """TEST ONLY oracle for the store a case is bound to beside its source (contract row 2026-10-18 "Reports (08) as
    built"): a commitment (cancelled or not) reads the plan key logged `committed` in its current round; a live case
    its selection bound to the active version, else the active version's recommendation. Keep or a markdown: none."""
    if case["state"] in COMMITTED or (case["state"] == "CLOSED" and case["closed_as"] == "CANCELLED"):
        entry = db.one(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND case_id = :c AND type = 'committed' "
                             "AND payload->>'round' = :n ORDER BY seq DESC LIMIT 1",
                       r=run_id, c=case["case_id"], n=str(case["round"]))
        key_ = entry["payload"]["plan"] if entry else None
    else:
        rec = db.one(conn, "SELECT result FROM recommendations WHERE run_id = :r AND case_id = :c AND rec_version = :v",
                     r=run_id, c=case["case_id"], v=case["active_rec_version"])
        selected = case["selected"]
        bound = selected and selected.get("rec_version") == case["active_rec_version"]
        key_ = selected["plan_key"] if bound else rec["result"]["recommended"] if rec else None
    plan = cases.parse_plan(key_) if key_ else None
    return plan.dest if plan and plan.kind == TRANSFER else None


def oracle(run_id: str, store: str | None, seat_branch: str | None) -> dict:
    """The five activity groups by the contract's definitions, from the stored rows, without api.reporting."""
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r", r=run_id)
        scoped = [c for c in rows if store is None or store in (c["source_branch"], dest_in_force(conn, run_id, c))]
        ids = {c["case_id"] for c in scoped}
        requests = db.rows(conn, "SELECT case_id, to_branch, (due_at IS NOT NULL AND due_at <= now()) AS late "
                                 "FROM evidence_requests WHERE run_id = :r AND state = 'OPEN'", r=run_id)
    checks = [q for q in requests if store is None or q["case_id"] in ids or q["to_branch"] == store]
    return {
        "open": {c["case_id"] for c in scoped if c["state"] in LIVE},
        "pending_approval": {c["case_id"] for c in scoped if c["state"] == "PENDING_APPROVAL"},
        "committed": {c["case_id"] for c in scoped if c["state"] in COMMITTED},
        "cancelled": {c["case_id"] for c in scoped if c["state"] == "CLOSED" and c["closed_as"] == "CANCELLED"},
        "checks": len(checks), "past_due": sum(bool(q["late"]) for q in checks),
        "addressed": sum(q["to_branch"] == seat_branch for q in checks) if seat_branch else 0,
    }


def agree(client, run_id: str, seat_branch: str | None, store: str | None = None) -> dict:
    """The Reports read for the signed-in seat equals the oracle, the board drilldowns list exactly the same cases, and
    need_action / waiting_for_you equal the board's own cards' `next_owner` (Phase 7's plan in force). The body."""
    params = {} if store is None or seat_branch is not None else {"branch": store}
    scope = seat_branch if seat_branch is not None else store
    body = read(client, **params)
    want = oracle(run_id, scope, seat_branch)
    activity = body["activity"]
    assert body["scope"]["branch"] == scope
    for tile in ("open", "pending_approval", "committed", "cancelled"):
        assert activity[tile]["count"] == len(want[tile]), (tile, seat_branch, store)
        assert {c["case_id"] for c in board(client, tile, **params)} == want[tile], (tile, seat_branch, store)
    assert (activity["unanswered_check"]["count"], activity["unanswered_check"]["past_due"],
            activity["unanswered_check"]["addressed_to_you"]) == (want["checks"], want["past_due"], want["addressed"])
    mine = [c for c in board(client, "open", **params)
            if seat_branch is not None and any(o["branch"] == seat_branch for o in c["next_owner"])]
    assert activity["open"]["need_action"] == len(mine), (seat_branch, [c["key"] for c in mine])
    assert activity["pending_approval"]["waiting_for_you"] == sum(c["state"] == "PENDING_APPROVAL" for c in mine)
    return body


def case_now(run_id: str, case_id: str) -> dict:
    with db.tx() as conn:
        return cases.case_row(conn, run_id, case_id)


def decide(client, run: dict, case_id: str, branch: str, decision: str) -> dict:
    """An accept or approve over HTTP as that store's manager, naming the plan in force and acknowledging its Advisory
    facts (07 review C, D-26)."""
    as_seat(client, branch)
    response = client.post(f"/api/cases/{case_id}/decision", headers=key(), json={
        "rec_version": case_now(run["run_id"], case_id)["active_rec_version"], "decision": decision,
        "plan_key": shown(run, case_id), "acknowledged": ack(run, case_id)})
    assert response.status_code == 200, response.json()
    return response.json()


def send(client, run: dict, case_id: str, source: str) -> None:
    as_seat(client, source)
    own(client, case_id)
    response = client.post(f"/api/cases/{case_id}/send", headers=key(),
                           json={"rec_version": case_now(run["run_id"], case_id)["active_rec_version"]})
    assert response.status_code == 200, response.json()
    assert response.json()["state"] == "PENDING_APPROVAL"


def cancel(client, case_id: str, branch: str) -> None:
    as_seat(client, branch)
    response = client.post(f"/api/cases/{case_id}/cancel", headers=key(), json={"reason": REASON})
    assert response.status_code == 200, response.json()
    assert response.json() == {"state": "CLOSED", "closed_as": "CANCELLED"}


def other_store(run: dict, *not_these) -> str:
    return next(b for b in refdata.get(run["dataset_version"]).branches if b not in not_these)


def recommended_transfer(run: dict) -> tuple[dict, object]:
    """The first OPEN case (case_id order) whose active version recommends a transfer: (its row, the plan)."""
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' AND active_rec_version IS NOT "
                             "NULL ORDER BY case_id", r=run["run_id"])
    for case in rows:
        plan = cases.parse_plan(active(run, case["case_id"])["result"]["recommended"])
        if plan.kind == TRANSFER:
            return case, plan
    pytest.fail("hand-back: no OPEN case on the template recommends a transfer")


def alternative_destination(run: dict) -> tuple[dict, object, object]:
    """An OPEN case whose active version recommends a transfer to store A and also offers a transfer to another store
    B that `check_plan` accepts on today's input (an engine option, never a pasted key): (row, A's plan, B's plan)."""
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' AND active_rec_version IS NOT "
                             "NULL ORDER BY case_id", r=run["run_id"])
    for case in rows:
        recommended = cases.parse_plan(active(run, case["case_id"])["result"]["recommended"])
        if recommended.kind != TRANSFER:
            continue
        for k in feasible_others(run, case["case_id"], kinds=(TRANSFER,)):
            alternative = cases.parse_plan(k)
            if alternative.dest not in (recommended.dest, case["source_branch"]):
                return case, recommended, alternative
    pytest.fail("hand-back: no OPEN case offers a feasible transfer to a store other than its recommendation's")


# --- Cancel approved plan (07-11, D-23, D-25) -------------------------------------------------------------------------

def test_a_plan_cancelled_through_phase_7_counts_under_cancellations(client):
    """A recommended transfer sent, accepted, approved and then cancelled through Phase 7's routes: it moves from
    Committed plans to Cancellations for its source, its destination and HQ (that store), never for an unrelated
    store, and `report=cancelled` lists it there."""
    run = workspace_of(client)
    case, plan = recommended_transfer(run)
    cid, source = case["case_id"], case["source_branch"]
    unrelated = other_store(run, source, plan.dest)
    send(client, run, cid, source)
    decide(client, run, cid, plan.dest, "accept")
    assert decide(client, run, cid, source, "approve")["state"] == "APPROVED"
    seen = {}
    for branch in (source, plan.dest, unrelated):
        as_seat(client, branch)
        seen[branch] = agree(client, run["run_id"], branch)["activity"]
    assert cid in oracle(run["run_id"], plan.dest, plan.dest)["committed"]

    cancel(client, cid, source)
    row = case_now(run["run_id"], cid)
    assert (row["state"], row["closed_as"], row["cancel_reason"]) == ("CLOSED", "CANCELLED", REASON)
    for branch, involved in ((source, True), (plan.dest, True), (unrelated, False)):
        as_seat(client, branch)
        after = agree(client, run["run_id"], branch)["activity"]
        assert after["cancelled"]["count"] == seen[branch]["cancelled"]["count"] + involved, branch
        assert after["committed"]["count"] == seen[branch]["committed"]["count"] - involved, branch
        assert (cid in {c["case_id"] for c in board(client, "cancelled")}) is involved, branch
    as_seat(client, None)
    for store, involved in ((plan.dest, True), (unrelated, False)):
        agree(client, run["run_id"], None, store)
        assert (cid in {c["case_id"] for c in board(client, "cancelled", branch=store)}) is involved, store
    # the stand-in the pre-Phase-7 tests used holds on the real record: CLOSED/CANCELLED, and the round's `committed`
    # entry still names the plan the cancelled destination is read from
    with db.tx() as conn:
        assert dest_in_force(conn, run["run_id"], row) == plan.dest
        (entry,) = db.rows(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND case_id = :c "
                                 "AND type = 'cancelled'", r=run["run_id"], c=cid)
    assert (entry["payload"]["plan"], entry["payload"]["closed_as"]) == (plan.key, "CANCELLED")


# --- A count request and its deadline (07-04, D-05, D-20) -------------------------------------------------------------

def plan_changing_fact(run: dict, case_id: str) -> dict | None:
    return next((f for f in active(run, case_id)["fact_checks"]
                 if f["kind"] in ("PHANTOM_STOCK", "STALE_COUNT") and f["changes_plan"]), None)


def test_a_request_made_through_phase_7_counts_and_turns_past_due_when_its_deadline_passes(client):
    """A count request made through Phase 7's request route is an unanswered check for the source, for the branch it is
    addressed to (addressed to you) and for HQ; it is not past due until its deadline passes. Moving `due_at` one second
    behind the database's clock makes it past due everywhere it counts."""
    run = workspace_of(client)
    with db.tx() as conn:
        rows = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' AND active_rec_version IS NOT "
                             "NULL ORDER BY case_id", r=run["run_id"])
    case, fact = next(((c, f) for c in rows if (f := plan_changing_fact(run, c["case_id"]))), (None, None))
    assert case is not None, "hand-back: no OPEN case has a plan-changing count fact"
    cid, source = case["case_id"], case["source_branch"]
    as_seat(client, source)
    before = agree(client, run["run_id"], source)["activity"]["unanswered_check"]
    response = client.post(f"/api/cases/{cid}/requests", headers=key(),
                           json={"rec_version": case["active_rec_version"], "fact_id": fact["fact_id"]})
    assert response.status_code == 200, response.json()
    with db.tx() as conn:
        (q,) = db.rows(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND case_id = :c AND state = 'OPEN' "
                             "AND fact_id = :f", r=run["run_id"], c=cid, f=fact["fact_id"])
    assert q["due_at"] is not None  # Phase 7 sets the deadline (07-CONTEXT D-05); the 08 copy of its DDL is gone
    seats = [source] + ([q["to_branch"]] if q["to_branch"] != source else [])
    counted = {}
    for branch in seats:
        as_seat(client, branch)
        counted[branch] = agree(client, run["run_id"], branch)["activity"]["unanswered_check"]
        if branch == source:
            assert cid in {c["case_id"] for c in board(client, "unanswered_check")}
    assert counted[source]["count"] == before["count"] + 1
    assert counted[source]["past_due"] == before["past_due"]
    assert counted[q["to_branch"]]["addressed_to_you"] >= 1

    with db.tx() as conn:  # Phase 7's own deadline move (tests/api/test_evidence.py `past_due`)
        db.run(conn, "UPDATE evidence_requests SET due_at = now() - interval '1 second' WHERE run_id = :r "
                     "AND request_id = :q", r=run["run_id"], q=q["request_id"])
    for branch in seats:
        as_seat(client, branch)
        late = agree(client, run["run_id"], branch)["activity"]["unanswered_check"]
        assert late["count"] == counted[branch]["count"]
        assert late["past_due"] == counted[branch]["past_due"] + 1, branch
    as_seat(client, None)
    hq = agree(client, run["run_id"], None, q["to_branch"])["activity"]["unanswered_check"]
    assert hq["past_due"] >= 1 and hq["addressed_to_you"] == 0


# --- Select another plan: the plan in force wins everywhere (gap 7, D-23) ---------------------------------------------

def test_a_selected_destination_is_the_plan_in_force_for_every_reports_figure(client):
    """The case's recommendation names store A; its source manager selects another engine option, a transfer to store
    B, through Phase 7's Select another plan, sends it, B accepts and the source approves. At every step the Reports
    activity, the board's `report=` drilldown, who acts next (need_action, waiting_for_you), `report=committed` and
    the Expected row attribute the case to B and never to A; a cancel keeps it at B."""
    run = workspace_of(client)
    case, recommended, alternative = alternative_destination(run)
    cid, source, a, b = case["case_id"], case["source_branch"], recommended.dest, alternative.dest
    rec = active(run, cid)

    def involved(branch: str, tile: str) -> bool:
        as_seat(client, branch)
        agree(client, run["run_id"], branch)
        return cid in {c["case_id"] for c in board(client, tile)}

    def acts(branch: str) -> tuple[int, int]:
        as_seat(client, branch)
        activity = agree(client, run["run_id"], branch)["activity"]
        return activity["open"]["need_action"], activity["pending_approval"]["waiting_for_you"]

    assert involved(a, "open") and not involved(b, "open")  # before: the recommendation's store
    as_seat(client, source)
    own(client, cid)
    response = client.post(f"/api/cases/{cid}/select", headers=key(),
                           json={"rec_version": rec["rec_version"], "plan_key": alternative.key})
    assert response.status_code == 200, response.json()
    assert response.json()["selected"]["plan_key"] == alternative.key
    # the bridge the pre-Phase-7 tests used (test_reporting_expected.selection_fixture) has the real selection's keys
    stored = case_now(run["run_id"], cid)["selected"]
    assert {"rec_version", "plan_key", "label", "fact_checks", "by", "at"} <= set(stored)
    assert (stored["rec_version"], stored["plan_key"]) == (rec["rec_version"], alternative.key)
    assert shown(run, cid) == alternative.key
    assert involved(b, "open") and not involved(a, "open")

    send(client, run, cid, source)  # PENDING_APPROVAL: the destination of the plan in force decides, not A
    assert involved(b, "pending_approval") and not involved(a, "pending_approval")
    need_b, waiting_b = acts(b)  # agree() checks each against the board's cards for that seat
    acts(a)
    as_seat(client, b)
    card = next(c for c in board(client, "pending_approval") if c["case_id"] == cid)
    assert {"role": "manager", "branch": b} in card["next_owner"]  # the board's own view
    assert all(o["branch"] != a for o in card["next_owner"])
    assert waiting_b >= 1 and need_b >= 1
    with db.tx() as conn:  # A is not waited on for this case: its counts are exactly the cases where A is next owner
        assert dest_in_force(conn, run["run_id"], case_now(run["run_id"], cid)) == b

    decide(client, run, cid, b, "accept")
    assert decide(client, run, cid, source, "approve")["state"] == "APPROVED"
    with db.tx() as conn:
        (entry,) = db.rows(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND case_id = :c "
                                 "AND type = 'committed'", r=run["run_id"], c=cid)
    assert (entry["payload"]["plan"], entry["payload"]["round"]) == (alternative.key, case_now(run["run_id"], cid)["round"])
    assert case_now(run["run_id"], cid)["plan_dest"] == b

    columns = {c.get("key"): c for c in (rec.get("simple_rule") or {}).get("columns", [])}
    for branch, mine in ((b, True), (a, False), (source, True)):
        assert involved(branch, "committed") is mine, branch
        rows = [p for p in read(client)["expected"]["plans"] if p["case_id"] == cid]
        assert len(rows) == int(mine), branch
        if mine:
            (row,) = rows
            assert (row["plan_key"], row["plan_kind"], row["dest_branch"]) == (alternative.key, TRANSFER, b)
            assert row.get("reason") != "unbound_selection"  # Phase 7's real resolver binds it, no stand-in needed
            chosen, keep = columns.get(alternative.key), columns.get("keep")
            if rec.get("simple_rule", {}).get("status") == "ok" and chosen is not None and keep is not None:
                assert row["computed"] is True
                assert row["net_change"] == pytest.approx(chosen["delta_vs_keep"], abs=0.005)
                assert row["transport_and_handling"] == pytest.approx(chosen["transport"]["base"], abs=0.005)
            else:  # no stored column for the selected plan: never estimated (contract row, gap 15)
                assert row["computed"] is False and row["reason"] in ("plan_not_computed", "comparison_not_computed")
                assert all(row[f] is None for f in ("net_change", "waste_change_units", "waste_change_kg",
                                                    "transport_and_handling"))
    as_seat(client, None)
    for store, mine in ((b, True), (a, False)):
        agree(client, run["run_id"], None, store)
        assert (cid in {c["case_id"] for c in board(client, "committed", branch=store)}) is mine, store
        assert len([p for p in read(client, branch=store)["expected"]["plans"] if p["case_id"] == cid]) == int(mine)

    cancel(client, cid, b)  # the destination of the plan in force may cancel it (D-23)
    for branch, mine in ((b, True), (a, False)):
        assert involved(branch, "cancelled") is mine, branch
