"""The model drafts; the source manager sends through the existing service.

Bindings come from actual engine fact checks, never pasted seed quantities.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.agent.test_brief import business_state, install, rows, workspace


def draft(run, case, fact, text="Please check the recorded stock."):
    from agent import tools
    from tests.api.conftest import seat
    from tests.api.test_workflow import version
    from tests.api.verify_helpers import shown
    ctx = {"scope": "CASE", "run_id": run["run_id"], "case_id": case["case_id"],
           "rec_version": version(run, case["case_id"]), "plan_key": shown(run, case["case_id"]),
           "actor": seat(case["source_branch"]), "model_id": "offline-scripted-model",
           "assistant_version": "offline-release"}
    return asyncio.run(tools.draft_evidence_request(fact["fact_id"], text, tool_context=SimpleNamespace(state={"ctx": ctx})))


def setup_draft(client):
    from tests.api.test_evidence import phantom
    session, run, case = workspace(client)
    fact = phantom(run, case["case_id"])
    made = draft(run, case, fact)
    saved, = rows(run["run_id"], "evidence_drafts")
    assert made["draft_id"] == saved["draft_id"]
    return session, run, case, fact, saved


def send(client, case, saved, *, message="  Human wording: shelf and backroom.\nKeep this line.  ", headers=None, **changes):
    from tests.api.conftest import key
    body = {"draft_id": saved["draft_id"], "rec_version": saved["rec_version"],
            "plan_key": saved["plan_key"], "message": message, **changes}
    return client.post(f"/api/cases/{case['case_id']}/requests", json=body, headers=headers or key())


def test_draft_only_writes_checked_original_and_immutable_context_no_deadline_or_business_action(client):
    from agent import tools
    from tests.api.test_evidence import phantom
    _, run, case = workspace(client)
    fact = phantom(run, case["case_id"])
    before = business_state(run["run_id"])
    made = draft(run, case, fact, "Please count USD 123456789.12 of stock.")
    saved, = rows(run["run_id"], "evidence_drafts")
    assert saved["draft_id"] == made["draft_id"] and saved["state"] == "DRAFT"
    assert saved["original_text"] == "⚠ number removed: not traceable to a tool result"
    assert (saved["fact_id"], saved["to_branch"], saved["sku"], saved["lot_ids"]) == (
        fact["fact_id"], fact["branch"], case["sku"], fact["lot_ids"])
    assert saved["request_id"] is None and saved.get("due_at") is None and saved.get("sent_at") is None
    assert saved["request_context"]["lot_ids"] == fact["lot_ids"]
    assert saved["model_id"] and saved["assistant_version"] and saved["checks"] is not None
    assert business_state(run["run_id"]) == before
    listing = client.get(f"/api/cases/{case['case_id']}/drafts").json()
    assert listing["requests_left"] == 2 and [d["draft_id"] for d in listing["drafts"]] == [saved["draft_id"]]


def test_send_keeps_exact_human_text_separate_and_replays_without_spend(client, fake_model, monkeypatch):
    from api import caps
    from tests.api.conftest import key
    from tests.api.test_caps import counts
    fake = install(fake_model, monkeypatch, [])
    session, run, case, fact, saved = setup_draft(client)
    before = counts(caps.GLOBAL, "ai")
    headers, message = key(), "  Count 987654321 units?\nPlease report what you see.  "
    first = send(client, case, saved, message=message, headers=headers)
    assert first.status_code == 200, first.text
    assert send(client, case, saved, message=message, headers=headers).json() == first.json()
    request, = rows(run["run_id"], "evidence_requests")
    assert request["message"] == message and request["draft_id"] == saved["draft_id"]
    assert request["sent_by"] == session["account"] and request["sent_at"] == request["asked_at"]
    assert request["due_at"] - request["sent_at"] == timedelta(minutes=60)
    assert request["request_context"] == saved["request_context"] and request["plan_key"] == saved["plan_key"]
    after, = rows(run["run_id"], "evidence_drafts")
    assert after["original_text"] == saved["original_text"] and after["state"] == "SENT"
    assert after["request_id"] == request["request_id"]
    assert fake.requests == [] and counts(caps.GLOBAL, "ai") == before
    assert client.get(f"/api/cases/{case['case_id']}/drafts").json()["requests_left"] == 1
    log = client.get(f"/api/cases/{case['case_id']}/activity").json()
    drafted = [e for e in log if e["type"] == "evidence_drafted"]
    sent = [e for e in log if e["type"] == "evidence_requested"]
    assert len(drafted) == len(sent) == 1 and drafted[0]["group"] == "Assistant" and sent[0]["group"] == "Data"


@pytest.mark.parametrize("message", ["", " \n\t ", "bad\x00text", "oversize"])
def test_invalid_human_wording_creates_no_slot_deadline_or_request(client, message):
    from agent import tools
    from api import config
    _, run, case, _, saved = setup_draft(client)
    if message == "oversize":
        # The new service setting is a named contract: no bound baked into the
        # test or the Send route. All other existing request settings stay data.
        message = "x" * (config.SETTINGS["requests"]["max_message_chars"] + 1)
    before = rows(run["run_id"], "evidence_drafts")
    refused = send(client, case, saved, message=message)
    assert (refused.status_code, refused.json()["error"]) == (400, "bad_request")
    assert rows(run["run_id"], "evidence_requests") == [] and rows(run["run_id"], "evidence_drafts") == before
    assert client.get(f"/api/cases/{case['case_id']}/drafts").json()["requests_left"] == 2


@pytest.mark.parametrize("change,error", [("selection", "stale_plan"), ("version", "stale_version"), ("state", "wrong_state")])
def test_stale_send_and_case_state_order_preserve_draft_without_request(client, change, error):
    from agent import tools
    from api import selection, workflow
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call
    _, run, case, _, saved = setup_draft(client)
    original = dict(saved)
    actor = seat(case["source_branch"])
    if change == "selection":
        call(run, selection.select, actor, case["case_id"], saved["rec_version"], feasible_others(run, case["case_id"])[0])
    elif change == "version":
        call(run, workflow.adopt, actor, case["case_id"],
             [{"kind": "demand_multiplier", "branch": case["source_branch"],
               "day_from": run["day"], "day_to": run["day"], "value": .9}], "Manager scenario")
    else:
        call(run, workflow.reject, actor, case["case_id"], "Manager closed case")
        saved = {**saved, "rec_version": "also-stale-version"}
    refused = send(client, case, saved)
    assert (refused.status_code, refused.json()["error"]) == (409, error)
    assert rows(run["run_id"], "evidence_requests") == []
    retained, = rows(run["run_id"], "evidence_drafts")
    assert retained["request_id"] is None
    for field in ("original_text", "request_context", "rec_version", "plan_key", "fact_id"):
        assert retained[field] == original[field]
    historical = client.get(f"/api/cases/{case['case_id']}/drafts").json()["drafts"]
    if change != "state":
        assert all(d["draft_id"] != saved["draft_id"] or d["state"] == "HISTORICAL" for d in historical)


@pytest.mark.parametrize("forgery", [{"to_branch": "foreign-branch"}, {"lot_ids": ["foreign-lot"]}, {"fact_id": "foreign-fact"}])
def test_client_cannot_rebind_a_draft_to_other_facts_lots_or_branch(client, forgery):
    from agent import tools
    _, run, case, _, saved = setup_draft(client)
    refused = send(client, case, saved, **forgery)
    assert (refused.status_code, refused.json()["error"]) == (400, "bad_request")
    assert rows(run["run_id"], "evidence_requests") == []
    assert rows(run["run_id"], "evidence_drafts")[0]["request_id"] is None


def test_only_source_manager_sends_and_existing_open_request_is_linked(client):
    from agent import tools
    from tests.api.conftest import as_seat
    from tests.api.test_evidence import ask as manual_ask
    _, run, case, fact, saved = setup_draft(client)
    as_seat(client, None)
    forbidden = send(client, case, saved)
    assert (forbidden.status_code, forbidden.json()["error"]) == (403, "forbidden")
    assert rows(run["run_id"], "evidence_requests") == []
    original = manual_ask(run, case["case_id"], fact["fact_id"])
    as_seat(client, case["source_branch"])
    linked = send(client, case, saved)
    assert linked.status_code == 200 and linked.json()["request_id"] == original["request_id"]
    assert len(rows(run["run_id"], "evidence_requests")) == 1
    assert rows(run["run_id"], "evidence_drafts")[0]["request_id"] == original["request_id"]


@pytest.mark.parametrize("combined", [False, True])
def test_selected_plan_checks_bind_both_single_and_combined_human_requests(run, combined):
    from agent import tools
    from api import cases, db, selection, signals
    from api.errors import ApiError
    from tests.api.conftest import seat
    from tests.api.test_evidence import active, story
    from tests.api.test_selection import engine_checks, feasible_others
    from tests.api.test_workflow import call, version
    case = story(run)
    if combined:
        from tests.api.test_requests import pair_fixture
        case, count, expiry = pair_fixture(run)
    cid, v = case["case_id"], version(run, case["case_id"])
    old = active(run, cid)["fact_checks"]
    # Pick an actual alternative with different eligible facts, not edited checks.
    alternatives = [(key, engine_checks(run, cid, key)) for key in feasible_others(run, cid)]
    old_ids = {count["fact_id"], expiry["fact_id"]} if combined else {
        f["fact_id"] for f in old if f["changes_plan"]}
    key, checks = next((k, fs) for k, fs in alternatives
                      if old_ids - {f["fact_id"] for f in fs if f["changes_plan"]})
    actor = seat(case["source_branch"])
    call(run, selection.select, actor, cid, v, key)
    excluded = next(f for f in old_ids if f not in {x["fact_id"] for x in checks if x["changes_plan"]})
    with pytest.raises(ApiError) as caught:
        db.in_tx(lambda conn: signals.request_evidence(conn, db.lock_run(conn, run["run_id"]), actor, cid, v,
            None if combined else excluded, fact_ids=[count["fact_id"], expiry["fact_id"]] if combined else None,
            message="Manager asks for a check.", plan_key=key))
    assert (caught.value.status, caught.value.code) == (400, "unknown_fact")
    assert rows(run["run_id"], "evidence_requests") == []


def test_combined_human_send_uses_one_slot_and_third_draft_send_is_refused(client, fake_model, monkeypatch):
    from tests.api.conftest import key
    from tests.api.test_evidence import add_facts
    from tests.api.test_requests import pair_fixture
    from tests.api.test_workflow import version
    from tests.api.verify_helpers import shown
    fake = install(fake_model, monkeypatch, [])
    _, run, _ = workspace(client)
    case, count, expiry = pair_fixture(run)
    cid = case["case_id"]
    second, third = ({**count, "fact_id": "TEST:second"}, {**count, "fact_id": "TEST:third"})
    add_facts(run, cid, [second, third])
    draft(run, case, third)
    saved, = rows(run["run_id"], "evidence_drafts")
    binding = {"rec_version": version(run, cid), "plan_key": shown(run, cid), "message": "Human requests the checks."}
    combined = client.post(f"/api/cases/{cid}/requests", json={**binding,
        "fact_ids": [count["fact_id"], expiry["fact_id"]]}, headers=key())
    assert combined.status_code == 200, combined.text
    parts = rows(run["run_id"], "evidence_requests")
    assert len(parts) == 2 and {p["part"] for p in parts} == {"count", "expiry"}
    assert len({p["request_group"] for p in parts}) == len({p["due_at"] for p in parts}) == 1
    assert client.get(f"/api/cases/{cid}/drafts").json()["requests_left"] == 1
    sent = client.post(f"/api/cases/{cid}/requests", json={**binding, "fact_id": second["fact_id"]}, headers=key())
    assert sent.status_code == 200
    before = rows(run["run_id"], "evidence_requests")
    refused = send(client, case, saved)
    assert (refused.status_code, refused.json()["error"]) == (409, "too_many_requests")
    assert rows(run["run_id"], "evidence_requests") == before and len(before) == 3
    assert rows(run["run_id"], "evidence_drafts")[0]["state"] == "DRAFT"
    assert client.get(f"/api/cases/{cid}/drafts").json()["requests_left"] == 0 and fake.requests == []


def test_simultaneous_human_send_of_one_draft_creates_one_request_and_one_deadline(client, fake_model, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    fake = install(fake_model, monkeypatch, [])
    _, run, case, _, saved = setup_draft(client)
    ready = threading.Barrier(2)

    def submit():
        ready.wait(timeout=2)
        return send(client, case, saved)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        replies = [f.result(timeout=10) for f in futures]
    assert [r.status_code for r in replies] == [200, 200]
    assert replies[0].json()["request_id"] == replies[1].json()["request_id"]
    request, = rows(run["run_id"], "evidence_requests")
    assert request["due_at"] - request["sent_at"] == timedelta(minutes=60)
    assert len([e for e in rows(run["run_id"], "activity_log") if e["type"] == "evidence_requested"]) == 1
    assert client.get(f"/api/cases/{case['case_id']}/drafts").json()["requests_left"] == 1
    assert fake.requests == []


# 09-09 additions. The complete 09-04 contract above is retained unchanged.
def draft_context(run, case=None, **changes):
    from tests.api.conftest import seat
    from tests.api.test_evidence import active, story
    case = case or story(run)
    rec = active(run, case["case_id"])
    ctx = {"scope": "CASE", "run_id": run["run_id"], "case_id": case["case_id"],
           "rec_version": rec["rec_version"], "plan_key": rec["result"]["recommended"],
           "actor": seat(case["source_branch"]), "model_id": "offline-scripted-model",
           "assistant_version": "offline-release", **changes}
    return case, rec, ctx


def invoke_draft(ctx, fact_id, text="Please check shelf and backroom stock."):
    from agent import tools
    return asyncio.run(tools.draft_evidence_request(fact_id, text, SimpleNamespace(state={"ctx": ctx})))


def test_draft_refuses_an_unrelated_real_branch_and_preserves_business_state(run):
    from api import cases, refdata
    from tests.api.conftest import seat
    from tests.api.test_evidence import phantom
    case, rec, ctx = draft_context(run)
    outside = next(b for b in refdata.get(run["dataset_version"]).branches
                   if b not in {case["source_branch"], cases.parse_plan(ctx["plan_key"]).dest})
    before = business_state(run["run_id"])
    result = invoke_draft({**ctx, "actor": seat(outside)}, phantom(run, case["case_id"])["fact_id"])
    assert result.get("error") == "forbidden", "A valid unrelated branch is still outside the case's authority"
    assert rows(run["run_id"], "evidence_drafts") == []
    assert business_state(run["run_id"]) == before


@pytest.mark.parametrize("field", ["run_id", "case_id", "rec_version", "plan_key"])
@pytest.mark.parametrize("empty", [None, ""])
def test_draft_missing_binding_is_a_refusal_without_application_write(run, field, empty):
    from tests.api.test_evidence import phantom
    case, _, ctx = draft_context(run)
    before = business_state(run["run_id"])
    result = invoke_draft({**ctx, field: empty}, phantom(run, case["case_id"])["fact_id"])
    assert result.get("error") == "forbidden"
    assert rows(run["run_id"], "evidence_drafts") == []
    assert business_state(run["run_id"]) == before


@pytest.mark.parametrize("axis,error", [("rec_version", "stale_version"), ("plan_key", "stale_plan")])
def test_draft_refuses_a_stale_version_or_plan_without_rebinding(run, axis, error):
    from tests.api.test_evidence import phantom
    case, _, ctx = draft_context(run)
    result = invoke_draft({**ctx, axis: "not-the-current-binding"}, phantom(run, case["case_id"])["fact_id"])
    assert result["error"] == error
    assert rows(run["run_id"], "evidence_drafts") == rows(run["run_id"], "evidence_requests") == []


@pytest.mark.parametrize("fact_id", [None, "", "not-flagged", ["not-a-fact-id"]])
def test_draft_refuses_missing_or_unflagged_fact_ids(run, fact_id):
    _, _, ctx = draft_context(run)
    result = invoke_draft(ctx, fact_id)
    assert result.get("error") in {"bad_request", "unknown_fact"}
    assert rows(run["run_id"], "evidence_drafts") == rows(run["run_id"], "evidence_requests") == []


def test_draft_checks_the_selected_plan_instead_of_recommendation_facts(run):
    from agent import tools
    from api import selection
    from tests.api.test_selection import engine_checks, feasible_others
    from tests.api.test_workflow import call
    case, rec, ctx = draft_context(run)
    original = {f["fact_id"] for f in rec["fact_checks"] if f["changes_plan"]}
    alternatives = [(key, engine_checks(run, case["case_id"], key)) for key in feasible_others(run, case["case_id"])]
    key, checks = next((key, fs) for key, fs in alternatives
                      if original - {f["fact_id"] for f in fs if f["changes_plan"]})
    call(run, selection.select, ctx["actor"], case["case_id"], ctx["rec_version"], key)
    excluded = next(f for f in original if f not in {x["fact_id"] for x in checks if x["changes_plan"]})
    result = invoke_draft({**ctx, "plan_key": key}, excluded)
    assert result["error"] == "unknown_fact" and rows(run["run_id"], "evidence_drafts") == []
    selected = asyncio.run(tools.get_fact_checks(SimpleNamespace(state={"ctx": {**ctx, "plan_key": key}})))
    assert set(selected["fact_checks"]) == {f["fact_id"] for f in checks}


@pytest.mark.parametrize("count", [0, 1, 5])
def test_flagged_checks_keep_required_first_and_all_overflow_without_spending(run, count):
    from agent import tools
    from api import db
    from tests.api.test_evidence import phantom
    case, rec, ctx = draft_context(run)
    base = phantom(run, case["case_id"])
    # A test-owned stored-check permutation, derived from the real engine fact.
    # It changes no hand fixture or seed: it exercises ordering independently
    # of the released scenario's current number of checks.
    checks = [{**base, "fact_id": f"TEST:order-{i}", "pick_feasible_if_alt": bool(i % 2),
               "changes_plan": True} for i in range(count)]
    with db.tx() as conn:
        db.run(conn, "UPDATE recommendations SET fact_checks = CAST(:f AS jsonb) WHERE run_id = :r "
                     "AND case_id = :c AND rec_version = :v", f=db.js(checks), r=run["run_id"],
               c=case["case_id"], v=ctx["rec_version"])
    before = business_state(run["run_id"])
    found = asyncio.run(tools.get_fact_checks(SimpleNamespace(state={"ctx": ctx})))
    expected = [f["fact_id"] for f in checks if not f["pick_feasible_if_alt"]]
    expected += [f["fact_id"] for f in checks if f["pick_feasible_if_alt"]]
    assert list(found["fact_checks"]) == expected
    assert found["fact_order"] == expected
    audit = next(r for r in rows(run["run_id"], "tool_results") if r["result_id"] == found["result_id"])
    assert audit["result"]["fact_order"] == expected
    for fid in reversed(expected):
        assert invoke_draft(ctx, fid)["draft_id"]
    assert len(rows(run["run_id"], "evidence_drafts")) == count
    assert business_state(run["run_id"]) == before
    if count == 0:
        assert found.get("reason") and found.get("next_action") == "review_current_checks"
    else:
        assert {f["owner"] for f in found["fact_checks"].values()} == {case["source_branch"]}
        assert all(f["reason"] and f["next_action"] for f in found["fact_checks"].values())


def test_open_request_takes_priority_over_an_older_unsent_draft(run):
    from tests.api.test_evidence import ask, phantom
    case, _, ctx = draft_context(run)
    fact = phantom(run, case["case_id"])
    first = invoke_draft(ctx, fact["fact_id"])
    request = ask(run, case["case_id"], fact["fact_id"])
    saved = rows(run["run_id"], "evidence_drafts")
    linked = invoke_draft(ctx, fact["fact_id"], "Do not replace the original checked wording.")
    assert linked.get("linked") is True and linked["request_id"] == request["request_id"]
    assert linked["draft_id"] == first["draft_id"]
    assert rows(run["run_id"], "evidence_drafts") == saved
    assert len(rows(run["run_id"], "evidence_requests")) == 1


def test_draft_original_uses_only_exact_authorized_tool_sources(run):
    from agent import tools
    from tests.api.test_evidence import phantom
    case, _, ctx = draft_context(run)
    case_result = asyncio.run(tools.get_case(SimpleNamespace(state={"ctx": ctx})))
    checked = invoke_draft(ctx, phantom(run, case["case_id"])["fact_id"],
                           f"Please check [[{case_result['result_id']}:lot.recorded_qty]] units.")
    saved, = rows(run["run_id"], "evidence_drafts")
    assert saved["refs"] == checked["refs"] and len(saved["refs"]) == 1
    assert saved["refs"][0]["result_id"] == case_result["result_id"]
    assert saved["checks"] == {"claims": 1, "removed": 0}
    assert saved["original_text"] == checked["original_text"]


def test_draft_list_returns_checked_provenance_and_immutable_context_without_generation(client, fake_model, monkeypatch):
    fake = install(fake_model, monkeypatch, [])
    _, run, case, _, saved = setup_draft(client)
    response = client.get(f"/api/cases/{case['case_id']}/drafts")
    assert response.status_code == 200, response.text
    listing = response.json()
    assert (listing["rec_version"], listing["plan_key"], listing["requests_left"]) == (
        saved["rec_version"], saved["plan_key"], 2)
    item, = listing["drafts"]
    for field in ("draft_id", "run_id", "case_id", "rec_version", "plan_key", "fact_id", "class", "owner",
                  "to_branch", "sku", "lot_ids", "original_text", "refs", "checks", "model_id",
                  "assistant_version", "request_context"):
        assert item[field] == saved[field], field
    assert item["can_send"] is True and item["read_only"] is False
    assert item["reason"] and item["next_action"] == "review_draft" and item["created_at"]
    assert "message" not in item and item.get("sent_at") is None and item.get("due_at") is None
    assert rows(run["run_id"], "evidence_drafts") == [saved]
    assert rows(run["run_id"], "evidence_requests") == [] and fake.requests == []


@pytest.mark.parametrize("account", ["hq", "destination", "unrelated"])
def test_draft_list_authorizes_current_case_permissions_and_source_only_send(client, account):
    from api import cases, refdata
    from tests.api.conftest import as_seat
    _, run, case, _, saved = setup_draft(client)
    dest = cases.parse_plan(saved["plan_key"]).dest
    branch = None if account == "hq" else dest if account == "destination" else next(
        b for b in refdata.get(run["dataset_version"]).branches if b not in {case["source_branch"], dest})
    as_seat(client, branch)
    response = client.get(f"/api/cases/{case['case_id']}/drafts")
    if account == "unrelated":
        assert response.status_code == 403 and response.json()["error"] == "forbidden"
        assert saved["draft_id"] not in response.text and saved["original_text"] not in response.text
    else:
        assert response.status_code == 200, response.text
        item, = response.json()["drafts"]
        assert item["owner"] == case["source_branch"] and item["to_branch"] == saved["to_branch"]
        assert item["read_only"] is True and item["can_send"] is False
        assert item["next_action"] == "review_current_checks" and item["reason"]


@pytest.mark.parametrize("change", ["selection", "version", "state"])
def test_draft_list_keeps_stale_original_read_only_without_rebinding(client, change):
    from api import selection, workflow
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others
    from tests.api.test_workflow import call
    _, run, case, _, saved = setup_draft(client)
    actor = seat(case["source_branch"])
    if change == "selection":
        call(run, selection.select, actor, case["case_id"], saved["rec_version"], feasible_others(run, case["case_id"])[0])
    elif change == "version":
        call(run, workflow.adopt, actor, case["case_id"], [{"kind": "demand_multiplier",
             "branch": case["source_branch"], "day_from": run["day"], "day_to": run["day"], "value": .9}], "Manager scenario")
    else:
        call(run, workflow.reject, actor, case["case_id"], "Manager closed case")
    response = client.get(f"/api/cases/{case['case_id']}/drafts")
    assert response.status_code == 200, response.text
    item, = response.json()["drafts"]
    assert item["state"] == "HISTORICAL" and item["read_only"] is True and item["can_send"] is False
    assert item["next_action"] == "review_current_checks" and item["reason"]
    for field in ("original_text", "request_context", "rec_version", "plan_key", "fact_id", "refs", "checks"):
        assert item[field] == saved[field]
    assert rows(run["run_id"], "evidence_drafts") == [saved]
    assert rows(run["run_id"], "evidence_requests") == []


def test_draft_list_retains_required_overflow_and_links_existing_manual_requests(client):
    from api import commitment, db
    from tests.api.test_evidence import ask, phantom
    _, run, case = workspace(client)
    _, _, ctx = draft_context(run, case)
    base = phantom(run, case["case_id"])
    checks = [{**base, "fact_id": f"TEST:list-{i}", "pick_feasible_if_alt": bool(i % 2),
               "changes_plan": True} for i in range(5)]
    with db.tx() as conn:
        db.run(conn, "UPDATE recommendations SET fact_checks = CAST(:f AS jsonb) WHERE run_id = :r "
                     "AND case_id = :c AND rec_version = :v", f=db.js(checks), r=run["run_id"],
               c=case["case_id"], v=ctx["rec_version"])
    for fact in reversed(checks):
        assert invoke_draft(ctx, fact["fact_id"])["draft_id"]
    originals = rows(run["run_id"], "evidence_drafts")
    existing = [ask(run, case["case_id"], f["fact_id"]) for f in checks[:2]]
    listing = client.get(f"/api/cases/{case['case_id']}/drafts").json()
    expected = [f["fact_id"] for f in sum(commitment.classes(checks), [])]
    assert [d["fact_id"] for d in listing["drafts"]] == expected
    assert [a["fact_id"] for a in listing["attention"]] == expected and listing["requests_left"] == 0
    by_fact = {d["fact_id"]: d for d in listing["drafts"]}
    for request in existing:
        item = by_fact[request["fact_id"]]
        assert item["request_id"] == request["request_id"] and item["next_action"] == "view_request"
    assert all(not d["can_send"] and d["reason"] for d in listing["drafts"])
    assert all(a["owner"] == case["source_branch"] and a["to_branch"] == base["branch"]
               and a["reason"] and a["next_action"] for a in listing["attention"])
    assert rows(run["run_id"], "evidence_drafts") == originals
    assert len(rows(run["run_id"], "evidence_requests")) == len(existing)


def test_draft_list_with_no_engine_checks_offers_a_supported_path_without_inventing_rows(client):
    from api import db
    _, run, case = workspace(client)
    _, _, ctx = draft_context(run, case)
    with db.tx() as conn:
        db.run(conn, "UPDATE recommendations SET fact_checks = '[]' WHERE run_id = :r "
                     "AND case_id = :c AND rec_version = :v", r=run["run_id"], c=case["case_id"], v=ctx["rec_version"])
    listing = client.get(f"/api/cases/{case['case_id']}/drafts")
    assert listing.status_code == 200, listing.text
    assert listing.json()["drafts"] == listing.json()["attention"] == []
    assert listing.json()["reason"] and listing.json()["next_action"] == "review_current_checks"
    assert rows(run["run_id"], "evidence_drafts") == rows(run["run_id"], "evidence_requests") == []


def test_shared_activity_groups_actual_drafts_and_manual_requests_on_the_server(client):
    from api import views
    from tests.api.test_evidence import ask
    _, run, case, fact, saved = setup_draft(client)
    request = ask(run, case["case_id"], fact["fact_id"])
    response = client.get(f"/api/cases/{case['case_id']}/activity")
    assert response.status_code == 200, response.text
    drafted, = [e for e in response.json() if e["type"] == "evidence_drafted"]
    sent, = [e for e in response.json() if e["type"] == "evidence_requested"]
    assert drafted["group"] == "Assistant" and drafted["payload"]["draft_id"] == saved["draft_id"]
    assert sent["group"] == "Data" and sent["payload"]["request_id"] == request["request_id"]
    assert views._group("evidence_answered") == views._group("count_confirmed") == "Data"


def test_shared_activity_omits_private_ask_even_in_expanded_draft_metadata(client, fake_model, monkeypatch):
    import json
    from api import db
    from tests.api.conftest import as_seat, key
    from tests.agent.test_model import response
    fake = install(fake_model, monkeypatch, [response("PRIVATE-ASK-ANSWER-SENTINEL")])
    _, run, case, _, saved = setup_draft(client)
    question = "PRIVATE-ASK-QUESTION-SENTINEL"
    answer = client.post(f"/api/cases/{case['case_id']}/chat", json={"question": question,
        "request_id": "private-activity-check"}, headers=key())
    assert answer.status_code == 200 and len(fake.requests) == 1
    output, = rows(run["run_id"], "ai_outputs")
    with db.tx() as conn:
        db.log(conn, run["run_id"], run["day"], "ask_completed", case["case_id"], scope="ASK",
               question=question, answer=output["text"], output_id=output["output_id"])
        db.log(conn, run["run_id"], run["day"], "evidence_drafted", case["case_id"], draft_id=saved["draft_id"],
               metadata={"question": question, "tool_results": [{"result": output["text"]}]})
    as_seat(client, None)
    shared = client.get(f"/api/cases/{case['case_id']}/activity")
    assert shared.status_code == 200, shared.text
    assert question not in shared.text and output["text"] not in shared.text
    assert all(e["type"] != "ask_completed" for e in shared.json())
    drafted = [e for e in shared.json() if e["type"] == "evidence_drafted"]
    assert drafted and all(e["group"] == "Assistant" for e in drafted)
    assert all("metadata" not in e["payload"] for e in drafted)
    assert len(fake.requests) == 1


# 09-10: successful Send/link identity and concurrent allowance/selection guards.
def test_sent_draft_keeps_its_first_request_link_after_a_manual_recount(client):
    from tests.api.test_evidence import answer, counted, recount

    _, run, case, _, saved = setup_draft(client)
    first = send(client, case, saved, message=" First sent text.\nDo not rebind it. ")
    assert first.status_code == 200, first.text
    original = first.json()
    answer(run, original, counted(original))
    replacement = recount(run, case["case_id"], original["request_id"])
    assert replacement["request_id"] != original["request_id"]
    before = rows(run["run_id"], "evidence_requests")
    retried = send(client, case, saved, message="Unsent edits from another tab")
    assert retried.status_code == 200, retried.text
    assert retried.json()["request_id"] == original["request_id"]
    assert retried.json()["message"] == original["message"]
    retained, = rows(run["run_id"], "evidence_drafts")
    assert retained["request_id"] == original["request_id"] and retained["state"] == "SENT"
    assert rows(run["run_id"], "evidence_requests") == before
    assert retained["original_text"] == saved["original_text"]


def test_two_distinct_drafts_race_for_the_last_request_group_slot(client, fake_model, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from tests.api.test_evidence import add_facts, ask

    fake = install(fake_model, monkeypatch, [])
    _, run, case, fact, _ = setup_draft(client)
    extra = [{**fact, "fact_id": f"TEST:send-race-{i}"} for i in range(2)]
    add_facts(run, case["case_id"], extra)
    for item in extra:
        draft(run, case, item)
    ask(run, case["case_id"], fact["fact_id"])
    pending = [d for d in rows(run["run_id"], "evidence_drafts") if d["fact_id"] != fact["fact_id"]]
    ready = threading.Barrier(2)

    def submit(saved):
        ready.wait(timeout=5)
        return send(client, case, saved)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, d) for d in pending]
        replies = [f.result(timeout=15) for f in futures]
    assert sorted(r.status_code for r in replies) == [200, 409]
    refused, = [r for r in replies if r.status_code == 409]
    assert refused.json()["error"] == "too_many_requests"
    requests = rows(run["run_id"], "evidence_requests")
    assert len(requests) == 2 and len({r["request_group"] or r["request_id"] for r in requests}) == 2
    assert all(r["due_at"] - r["sent_at"] == timedelta(minutes=60) for r in requests)
    states = [d["state"] for d in rows(run["run_id"], "evidence_drafts") if d["draft_id"] in {
        p["draft_id"] for p in pending}]
    assert sorted(states) == ["DRAFT", "SENT"]
    assert client.get(f"/api/cases/{case['case_id']}/drafts").json()["requests_left"] == 0
    assert len([e for e in rows(run["run_id"], "activity_log") if e["type"] == "evidence_requested"]) == 2
    assert fake.requests == []


def test_queued_draft_send_rechecks_selection_after_the_case_lock(client, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from api import cases, db, selection
    from tests.api.conftest import seat
    from tests.api.test_selection import feasible_others

    _, run, case, _, saved = setup_draft(client)
    other = feasible_others(run, case["case_id"])[0]
    waiting = threading.Event()
    real = cases.case_row

    def watched(conn, run_id, case_id, lock=False):
        if lock:
            waiting.set()
        return real(conn, run_id, case_id, lock=lock)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="draft-waiter") as pool:
        with db.tx() as conn:
            locked = db.lock_run(conn, run["run_id"])
            selection.select(conn, locked, seat(case["source_branch"]), case["case_id"], saved["rec_version"], other)
            monkeypatch.setattr(cases, "case_row", watched)
            future = pool.submit(send, client, case, saved)
            assert waiting.wait(timeout=10), "Send never reached its case lock"
            assert not future.done(), "Send finished while selection was uncommitted"
        refused = future.result(timeout=15)
    assert (refused.status_code, refused.json()["error"]) == (409, "stale_plan")
    assert rows(run["run_id"], "evidence_requests") == []
    assert rows(run["run_id"], "evidence_drafts") == [saved]
