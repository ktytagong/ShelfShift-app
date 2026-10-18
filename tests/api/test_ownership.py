"""Take ownership, and Send only by the owner (06.1 D-25, D-54, D-67; 04-API-CONTRACT "Ownership and the steps").

The story case (SUS-19, by its tag) is taken over HTTP by the account the test signs in as: its source store's
manager, its destination's manager or HQ. Branches come from the case itself (its source and its pick's destination);
no test names a store code, a case key or an amount from the seed. The race, the no-approval rule and the warm-up are
tested at service level, as `call` in test_workflow.py makes a route's call.
"""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api import cases, db, ownership, workflow
from api.app import app
from api.errors import ApiError
from tests.api.conftest import by_tag, key, seat, sign_in, take
from tests.api.verify_helpers import ack, shown  # approve and accept acknowledge (D-26) and name the plan (07 review C)

NOT_OWNED = "Take ownership before sending this stock decision."
NOT_OWNER = "Only the owner of this stock decision can send it."


def post(client, path, body=None):
    return client.post(path, json={} if body is None else body, headers=key())


def story(client) -> dict:
    """SUS-19's card, found by its milk-story tag."""
    return next(c for c in client.get("/api/cases").json()["cases"] if c["tag"] == cases.STORY_TAG)


def signed_in(account: str, like: TestClient | None = None) -> TestClient:
    """A client signed in as `account`; with `like`, in the same workspace (the same browser, signed in again)."""
    c = TestClient(app)
    if like is not None:
        c.cookies.update(like.cookies)
    sign_in(c, account)
    return c


def entries(client, case_id, type_) -> list[dict]:
    return [a for a in client.get(f"/api/cases/{case_id}/activity").json() if a["type"] == type_]


def test_send_needs_ownership_and_the_source_manager_takes_it(client):
    card = story(client)
    cid, src, v = card["case_id"], card["source"], card["rec_version"]
    me = signed_in(src, like=client)
    r = post(me, f"/api/cases/{cid}/send", {"rec_version": v})
    assert (r.status_code, r.json()["error"], r.json()["message"]) == (409, "not_owned", NOT_OWNED)
    assert story(me)["state"] == "OPEN" and entries(me, cid, "sent") == []
    took = post(me, f"/api/cases/{cid}/take-ownership")
    assert took.status_code == 200, took.json()
    body = took.json()
    assert body["owner"] == {"account": src, "role": "manager", "label": f"{src} manager"}
    assert isinstance(body["taken_at"], str) and body["taken_at"]
    sent = post(me, f"/api/cases/{cid}/send", {"rec_version": v})
    assert sent.status_code == 200 and sent.json()["state"] == "PENDING_APPROVAL"


def test_ownership_taken_is_logged_once_with_the_actor(client):
    card = story(client)
    cid, src = card["case_id"], card["source"]
    me = signed_in(src, like=client)
    first = post(me, f"/api/cases/{cid}/take-ownership")
    again = post(me, f"/api/cases/{cid}/take-ownership")  # a new key: the owner repeating it
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    logged = entries(me, cid, "ownership_taken")
    assert len(logged) == 1
    assert logged[0]["payload"]["actor"] == {"account": src, "role": "manager", "branch": src}
    assert logged[0]["actor"] == {"account": src, "role": "manager", "branch": src}


def test_the_destination_manager_may_not_take_ownership(client):
    card = story(client)
    cid, dest = card["case_id"], card["dest"]
    other = signed_in(dest, like=client)
    r = post(other, f"/api/cases/{cid}/take-ownership")
    assert (r.status_code, r.json()["error"]) == (403, "forbidden")
    assert entries(other, cid, "ownership_taken") == []


def test_hq_takes_ownership_of_an_unowned_case(client):
    cid = story(client)["case_id"]
    hq = signed_in("HQ", like=client)
    r = post(hq, f"/api/cases/{cid}/take-ownership")
    assert r.status_code == 200, r.json()
    assert r.json()["owner"] == {"account": "HQ", "role": "hq", "label": "HQ planner"}


# --- atomic, no approval through ownership, the warm-up (Task 2) ----------------------------------------------------

def call(run, fn, *args):
    """One service call in its own transaction after the run lock, as a route makes it."""
    return db.in_tx(lambda conn: fn(conn, db.lock_run(conn, run["run_id"]), *args))


def raises(fn, *args) -> ApiError:
    with pytest.raises(ApiError) as e:
        fn(*args)
    return e.value


def owner_rows(run, case_id) -> tuple[dict, list[dict]]:
    """The case row's owner columns and its ownership_taken log entries."""
    with db.tx() as conn:
        row = cases.case_row(conn, run["run_id"], case_id)
        logged = db.rows(conn, "SELECT payload FROM activity_log WHERE run_id = :r AND case_id = :c "
                               "AND type = 'ownership_taken' ORDER BY seq", r=run["run_id"], c=case_id)
    return {"by": row["owner_taken_by"], "at": row["owner_taken_at"]}, logged


def test_two_accounts_racing_get_one_owner(run):
    """The source manager holds the case row (FOR UPDATE) mid-transaction; HQ's take waits on it, then sees the
    owner: one 200, one 409 already_owned with the owner, one owner row, one log entry (D-67)."""
    case = by_tag(run, cases.STORY_TAG)
    cid, src = case["case_id"], seat(case["source_branch"])
    holding, release, out = threading.Event(), threading.Event(), {}

    def first():
        def work(conn):
            answer = ownership.take_ownership(conn, db.lock_run(conn, run["run_id"]), src, cid)
            holding.set()
            release.wait(10)  # commit only once HQ is waiting on the row
            return answer
        out["source"] = db.in_tx(work)

    def second():
        holding.wait(10)
        try:
            out["hq"] = call(run, ownership.take_ownership, seat(None), cid)
        except ApiError as e:
            out["hq"] = e

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    [t.start() for t in threads]
    assert holding.wait(10)
    waited = False
    for _ in range(100):  # HQ's take is blocked on the locked case row before the first one commits
        with db.tx() as conn:
            waited = db.one(conn, "SELECT count(*) AS n FROM pg_locks WHERE NOT granted")["n"] > 0
        if waited:
            break
        time.sleep(0.05)
    release.set()
    [t.join(20) for t in threads]
    assert waited, "HQ's take never waited on the case row"
    assert out["source"]["owner"]["account"] == src["account"]
    e = out["hq"]
    assert isinstance(e, ApiError) and (e.status, e.code) == (409, "already_owned")
    label = f"{src['account']} manager"
    assert e.message == f"This stock decision is already owned by the {label}."
    assert e.extra == {"owner": {"account": src["account"], "role": "manager", "label": label}}
    owner, logged = owner_rows(run, cid)
    assert owner["by"] == src["account"] and owner["at"] is not None
    assert [x["payload"]["actor"]["account"] for x in logged] == [src["account"]]


def test_an_owned_decision_blocks_takeover_and_the_owner_repeat_logs_nothing(run):
    case = by_tag(run, cases.STORY_TAG)
    cid, src = case["case_id"], seat(case["source_branch"])
    first = take(run, seat(None), cid)
    assert first["owner"] == {"account": "HQ", "role": "hq", "label": "HQ planner"}
    assert take(run, seat(None), cid) == first  # the owner again: the stored answer
    e = raises(take, run, src, cid)
    assert (e.code, e.extra["owner"]["label"]) == ("already_owned", "HQ planner")
    owner, logged = owner_rows(run, cid)
    assert owner["by"] == "HQ" and len(logged) == 1


def test_only_takeable_states_can_be_taken(run):
    case = by_tag(run, cases.STORY_TAG)
    cid, src = case["case_id"], seat(case["source_branch"])
    with db.tx() as conn:
        db.run(conn, "UPDATE cases SET state = 'PENDING_APPROVAL' WHERE run_id = :r AND case_id = :c",
               r=run["run_id"], c=cid)
    e = raises(take, run, src, cid)
    assert (e.status, e.code, e.message) == (409, "wrong_state", "Case is PENDING_APPROVAL")
    for state in ("WAITING", "INVALIDATED"):
        with db.tx() as conn:
            db.run(conn, "UPDATE cases SET state = :s, owner_taken_by = NULL, owner_taken_at = NULL "
                         "WHERE run_id = :r AND case_id = :c", s=state, r=run["run_id"], c=cid)
        assert take(run, src, cid)["owner"]["account"] == src["account"]


def test_ownership_grants_no_approval(run):
    """HQ owning the story transfer may send it, and only HQ may: the source's send is the owner refusal. HQ's
    approve and accept stay 403; the source approves and the destination accepts as before (D-54, D-67)."""
    case = by_tag(run, cases.STORY_TAG)
    cid, hq, src = case["case_id"], seat(None), seat(case["source_branch"])
    with db.tx() as conn:
        row = cases.case_row(conn, run["run_id"], cid)
        plan = cases.parse_plan(cases.active_rec(conn, row)["result"]["recommended"])
    v, dest = row["active_rec_version"], seat(plan.dest)
    e = raises(call, run, workflow.send, hq, cid, v)
    assert (e.status, e.code, e.message) == (409, "not_owned", NOT_OWNED)
    take(run, hq, cid)
    e = raises(call, run, workflow.send, src, cid, v)
    assert (e.status, e.code, e.message) == (403, "forbidden", NOT_OWNER)
    assert call(run, workflow.send, hq, cid, v)["state"] == "PENDING_APPROVAL"
    for decision in ("approve", "accept"):
        assert raises(call, run, workflow.decide, hq, cid, v, decision).status == 403
    assert call(run, workflow.decide, dest, cid, v, "accept", None, ack(run, cid), shown(run, cid)) == \
        {"state": "PENDING_APPROVAL"}
    assert call(run, workflow.decide, src, cid, v, "approve", None, ack(run, cid), shown(run, cid)) == \
        {"state": "APPROVED"}


def test_the_warm_up_takes_ownership_as_the_source_store(template):
    """The template build (conftest `template`, through sandbox.build_template) reaches ACTIVE, and every case its
    warm-up sent is owned by its source store, with one ownership_taken entry before its first send."""
    with db.tx() as conn:
        status = db.one(conn, "SELECT status FROM demo_runs WHERE run_id = :r", r=template)["status"]
        sent = db.rows(conn, "SELECT DISTINCT c.case_id, c.source_branch, c.owner_taken_by FROM cases c JOIN "
                             "activity_log a ON a.run_id = c.run_id AND a.case_id = c.case_id AND a.type = 'sent' "
                             "WHERE c.run_id = :r", r=template)
        order = db.rows(conn, "SELECT case_id, type FROM activity_log WHERE run_id = :r AND type IN "
                              "('ownership_taken', 'sent') ORDER BY seq", r=template)
    assert status == "ACTIVE" and sent
    assert all(c["owner_taken_by"] == c["source_branch"] for c in sent), sent
    for c in sent:
        types = [x["type"] for x in order if x["case_id"] == c["case_id"]]
        assert types[0] == "ownership_taken" and types.count("ownership_taken") == 1, (c["case_id"], types)
