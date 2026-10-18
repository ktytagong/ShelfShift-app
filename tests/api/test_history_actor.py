"""The stock decision's history names the account that acted (quick 261017-l5q; 06.1-26 SUMMARY, Deferred Issues;
06.1 D-58).

Approve, reject, accept and decline, and every other person-made write of api/workflow.py and api/signals.py, carry
the acting account as the payload's `actor` {account, role, branch} (`accounts.logged_actor`), so GET activity reads
"by <store>" (or HQ) for a person, never "by ShelfShift". Entries made by the engine or the screen keep actor null.

Convention: branches come from the case itself; no store code, case key or seed amount is pasted from the seed.
"""
from __future__ import annotations

import pytest

from api import cases, db, signals, views, workflow
from api.errors import ApiError
from engine.model import TRANSFER
from tests.api.conftest import fresh, key, own, seat, sign_in
from tests.api.test_evidence import full_loop, phantom, story
from tests.api.test_execution import post, prepared, run_of, story_case
from tests.api.test_workflow import STORY, call, case_with_pick

HQ = {"account": "HQ", "role": "hq", "branch": None}


def manager(branch: str) -> dict:
    return {"account": branch, "role": "manager", "branch": branch}


def entries(c, cid: str, kind: str) -> list[dict]:
    """GET activity's entries of this type, oldest first."""
    r = c.get(f"/api/cases/{cid}/activity")
    assert r.status_code == 200, r.json()
    return [e for e in r.json() if e["type"] == kind]


def exact(actor) -> dict:
    """The actor with its keys checked: account, role and branch only (never the session id)."""
    assert isinstance(actor, dict) and set(actor) == {"account", "role", "branch"}, actor
    return actor


def dest_of(case: dict) -> str:
    with db.tx() as conn:
        plan = cases.parse_plan(cases.active_rec(conn, case)["result"]["recommended"])
    assert plan.kind == "TRANSFER", "D-15 hand-back: SUS-19's pick is not a transfer"
    return plan.dest


def pending(c) -> tuple[str, str, str, str]:
    """SUS-19 sent for approval by its source store (signed in, ownership taken over HTTP): (cid, src, dest, v)."""
    run = run_of(c)
    case = story_case(run["run_id"])
    cid, src, v = case["case_id"], case["source_branch"], case["active_rec_version"]
    dest = dest_of(case)
    sign_in(c, src)
    own(c, cid)
    assert post(c, f"/api/cases/{cid}/send", {"rec_version": v}).json()["state"] == "PENDING_APPROVAL"
    return cid, src, dest, v


def test_approve_and_accept_name_the_store_that_decided(client):
    run, action, src, dest = prepared(client)
    cid = action["case_id"]
    by = {e["payload"]["decision"]: e for e in entries(client, cid, "decision")}
    assert exact(by["accept"]["actor"]) == manager(dest) and by["accept"]["payload"]["role"] == "destination"
    assert exact(by["approve"]["actor"]) == manager(src) and by["approve"]["payload"]["role"] == "source"
    [done] = entries(client, cid, "committed")
    assert exact(done["actor"]) == manager(src)  # the source's approval completed the round


@pytest.mark.parametrize("decision", ["reject", "decline"])
def test_reject_and_decline_name_the_store_that_decided(client, decision):
    cid, src, dest, v = pending(client)
    who = src if decision == "reject" else dest
    sign_in(client, who)
    r = post(client, f"/api/cases/{cid}/decision", {"rec_version": v, "decision": decision, "reason": "not today"})
    assert r.json() == {"state": "REJECTED"}
    [entry] = entries(client, cid, "decision")
    assert exact(entry["actor"]) == manager(who)
    assert entry["payload"]["role"] == ("source" if decision == "reject" else "destination")


def logged(run, case_id: str, kind: str) -> list[dict]:
    """The service-level read of the same entries (views.activity), for tests that drive the services directly."""
    with db.tx() as conn:
        return [e for e in views.activity(conn, fresh(run["run_id"]), case_id) if e["type"] == kind]


def test_send_and_prepare_name_the_source_store(client):
    run, action, src, dest = prepared(client)
    cid = action["case_id"]
    for kind in ("sent", "prepared"):
        [entry] = entries(client, cid, kind)
        assert exact(entry["actor"]) == manager(src), kind


def test_the_reject_route_names_the_source_store(client):
    case = story_case(run_of(client)["run_id"])
    cid, src = case["case_id"], case["source_branch"]
    sign_in(client, src)
    assert post(client, f"/api/cases/{cid}/reject", {"reason": "store will run a promotion"}).json() == \
        {"state": "REJECTED"}
    [entry] = entries(client, cid, "rejected")
    assert exact(entry["actor"]) == manager(src)


def test_hq_taking_ownership_and_sending_names_hq(client):
    case = story_case(run_of(client)["run_id"])
    cid, v = case["case_id"], case["active_rec_version"]
    sign_in(client, "HQ")
    own(client, cid)
    assert post(client, f"/api/cases/{cid}/send", {"rec_version": v}).json()["state"] == "PENDING_APPROVAL"
    for kind in ("ownership_taken", "sent"):
        [entry] = entries(client, cid, kind)
        assert exact(entry["actor"]) == HQ, kind


def test_adopting_assumptions_names_the_source_and_the_new_version_stays_the_engines(client):
    case = story_case(run_of(client)["run_id"])
    cid, src = case["case_id"], case["source_branch"]
    sign_in(client, src)
    d = client.get(f"/api/cases/{cid}").json()
    best = next(o for o in d["result"]["options"] if o["key"] == d["result"]["recommended"])
    trip = next(r for r in d["engine_input"]["runs"] if r["run_code"] == best["run_code"] and r["dest"] == best["dest"])
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    r = post(client, f"/api/cases/{cid}/adopt", {"overrides": quote, "note": "carrier quote by phone"})
    assert r.status_code == 200, r.json()
    [entry] = entries(client, cid, "assumptions_adopted")
    assert exact(entry["actor"]) == manager(src)
    versions = entries(client, cid, "new_version")
    assert versions and all(e["actor"] is None for e in versions)


def test_hq_changing_a_policy_names_hq_on_each_live_case(client):
    cid = story_case(run_of(client)["run_id"])["case_id"]
    sign_in(client, "HQ")
    got = client.get("/api/settings").json()
    step = next(v for v in got["bounds"]["shortlist_step"]["allowed"] if v != got["values"]["shortlist_step"])
    body = {"expected_version": client.get("/api/session").json()["version"], "changes": {"shortlist_step": step}}
    r = client.patch("/api/settings", json=body, headers=key())
    assert r.status_code == 200, r.json()
    [entry] = entries(client, cid, "settings_changed")
    assert exact(entry["actor"]) == HQ


def test_a_stock_count_names_who_asked_who_answered_and_who_confirmed(run):
    case_id = story(run)["case_id"]
    with db.tx() as conn:
        src = cases.case_row(conn, run["run_id"], case_id)["source_branch"]
    q, _ = full_loop(run, case_id)
    [asked] = logged(run, case_id, "evidence_requested")
    assert exact(asked["actor"]) == manager(src)
    for kind in ("evidence_answered", "count_confirmed"):
        [entry] = logged(run, case_id, kind)
        assert exact(entry["actor"]) == manager(q["to_branch"]), kind
    versions = logged(run, case_id, "new_version")
    assert versions and all(e["actor"] is None for e in versions)


def test_waiting_names_the_source_store(run):
    case = case_with_pick(run, TRANSFER)
    src = case["source_branch"]
    assert call(run, workflow.wait, seat(src), STORY, run["day"] + 1, "waiting for the count") == {"state": "WAITING"}
    [entry] = logged(run, STORY, "waiting")
    assert exact(entry["actor"]) == manager(src)


def test_a_stock_check_on_a_replaced_version_says_reload_the_stock_decision(run):
    """The request path's stale_version sentence is the Case one in the 06.1 words (D-61)."""
    case_id = story(run)["case_id"]
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], case_id)
    v, fact = case["active_rec_version"], phantom(run, case_id)
    stale = "0" * len(v)
    assert stale != v
    with pytest.raises(ApiError) as e:
        call(run, signals.request_evidence, seat(case["source_branch"]), case_id, stale, fact["fact_id"])
    assert (e.value.status, e.value.code) == (409, "stale_version")
    assert e.value.message == "This recommendation was replaced by a newer version. Reload the stock decision."
