"""Policy settings per sandbox (04-CONTEXT D-09, D-23; 04-API-CONTRACT "Settings"). Only HQ changes them, signed in
with its own credential; a store manager reads them and is refused 403 (06.1 D-53; test_permissions.py).

Every call goes through the real app, as Phase 5 makes it. The writable fields and their bounds are read from
api/settings_bounds.json through config.SETTINGS_BOUNDS, never repeated here as numbers, except the D-37 values the
contract itself names (waste price 0 to 100 USD/kg, min_sellable_days 1 to 7, shortlist_step 0, 5 or 10), which the
inclusive-bound tests check against the file. The story case is found by its tag; its branches come from the case.
"""
from dataclasses import fields

import pytest
from fastapi.testclient import TestClient

from api import advance, cases, config, db, inputs, refdata
from api.app import app
from engine.model import Policy
from tests.api.conftest import as_seat, key, own_world, sign_in, take_and_send

# The D-37 bounds as the contract states them (04-API-CONTRACT "Settings"); the file must say the same.
CONTRACT_BOUNDS = {"waste_price": {"min": 0, "max": 100, "unit": "USD/kg"},
                   "min_sellable_days": {"min": 1, "max": 7, "unit": "days"},
                   "shortlist_step": {"allowed": [0, 5, 10]}}

WASTE_PRICE = 0.28  # a test value inside the D-37 bound, different from the seeded price


def story(client) -> dict:
    return next(c for c in client.get("/api/cases").json()["cases"] if c["tag"] == cases.STORY_TAG)


def version(client) -> int:
    return client.get("/api/session").json()["version"]


def patch(client, changes, expected=None, headers=None):
    body = {"expected_version": version(client) if expected is None else expected, "changes": changes}
    return client.patch("/api/settings", json=body, headers=key() if headers is None else headers)


def current(client) -> dict:
    return client.get("/api/settings").json()


def error(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]


@pytest.fixture
def hq(client):
    """The client signed out of its store and in as HQ, who alone changes policies (06.1 D-53); the workspace is
    kept."""
    as_seat(client, None)
    return client


def live_holds(run_id: str, case_id: str) -> int:
    with db.tx() as conn:
        return db.one(conn, "SELECT count(*) AS n FROM reservations WHERE run_id = :r AND case_id = :c "
                            "AND state IN ('HELD', 'COMMITTED')", r=run_id, c=case_id)["n"]


def test_a_settings_write_in_bounds_re_evaluates_and_cancels_pending_approval(client):
    card = story(client)
    sent = take_and_send(client, card["case_id"], card["rec_version"])  # the owner sends (06.1 D-54)
    assert sent.status_code == 200 and sent.json()["state"] == "PENDING_APPROVAL"
    as_seat(client, None)  # HQ changes the policy (06.1 D-53)
    session = client.get("/api/session").json()
    assert live_holds(session["run_id"], card["case_id"]) > 0
    r = patch(client, {"waste_price": WASTE_PRICE}, expected=session["version"])
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["version"] == session["version"] + 1
    assert out["values"]["waste_price"] == WASTE_PRICE and card["case_id"] in out["reevaluated"]
    assert set(out["values"]) == set(out["bounds"]) == set(config.SETTINGS_BOUNDS)
    after = client.get(f"/api/cases/{card['case_id']}").json()
    assert after["rec_version"] != card["rec_version"]  # a new version
    assert after["engine_input"]["policy"]["waste_price"] == WASTE_PRICE  # computed under the sandbox's new Policy
    assert after["state"] == "OPEN" and after["approvals"]["hold"] is None  # the approval round was cancelled
    assert live_holds(session["run_id"], card["case_id"]) == 0  # and its holds released
    types = [a["type"] for a in client.get(f"/api/cases/{card['case_id']}/activity").json()]
    assert types.index("settings_changed") < types.index("approval_cancelled") < len(types) - 1
    assert types[-1] == "new_version"
    assert client.get("/api/session").json()["version"] == out["version"]


@pytest.mark.parametrize("field, value", [
    ("waste_price", -0.01), ("waste_price", 100.01), ("min_sellable_days", 0), ("min_sellable_days", 8),
    ("shortlist_step", 7), ("waste_price", True), ("min_sellable_days", 2.5), ("waste_price", "5"),
    ("shortlist_step", None)])
def test_out_of_bounds_is_refused_naming_the_bound(hq, field, value):
    client = hq
    before = current(client)
    r = patch(client, {field: value})
    assert error(r) == (400, "out_of_bounds")
    body = r.json()
    bound = before["bounds"][field]  # the file's bound with the currency filled in (IN-07)
    assert body["field"] == field and body["bound"] == bound
    if "allowed" in bound:
        assert body["message"] == f"{field} must be one of " + ", ".join(str(v) for v in bound["allowed"])
    else:
        assert field in body["message"] and f"{bound['min']} and {bound['max']} {bound['unit']}" in body["message"]
    if (field, value) == ("waste_price", 100.01):
        assert body["message"] == "waste_price must be between 0 and 100 USD/kg"  # the contract's own example
    if field == "shortlist_step":
        assert body["message"] == "shortlist_step must be one of 0, 5, 10"
    assert current(client) == before  # nothing changed, not even the version


def test_bounds_are_inclusive(hq):
    client = hq
    assert current(client)["bounds"] == CONTRACT_BOUNDS  # the bounds file holds the D-37 bounds the contract names
    for changes in ({"waste_price": 0, "min_sellable_days": 1, "shortlist_step": 0},
                    {"waste_price": 100, "min_sellable_days": 7, "shortlist_step": 5},
                    {"shortlist_step": 10}):
        r = patch(client, changes)
        assert r.status_code == 200, r.json()
        assert {k: r.json()["values"][k] for k in changes} == changes
    assert current(client)["values"] == {"waste_price": 100, "min_sellable_days": 7, "shortlist_step": 10}


def test_only_the_three_fields_are_writable(hq):
    client = hq
    before = current(client)
    others = sorted({f.name for f in fields(Policy)} - set(config.SETTINGS_BOUNDS)) + ["run_id", "not_a_field"]
    assert others
    for field in others:
        r = patch(client, {field: 1})
        assert error(r) == (400, "not_writable") and r.json()["field"] == field
        assert r.json()["message"] == f"{field} is not a setting you can change here"
    r = patch(client, {"waste_price": 1.5, "packing_min": 30})  # all or none
    assert error(r) == (400, "not_writable") and r.json()["field"] == "packing_min"
    assert current(client) == before
    assert error(patch(client, {})) == (400, "bad_request")
    assert error(patch(client, ["waste_price"])) == (400, "bad_request")


def test_only_hq_writes_and_demo_mode_does_not_decide_it(client, monkeypatch):
    card = story(client)
    for seat in (card["dest"], card["source"]):  # the destination manager, the source manager
        as_seat(client, seat)
        before = current(client)
        r = patch(client, {"shortlist_step": 0})
        assert error(r) == (403, "forbidden") and r.json()["message"] == "Only HQ can change policies."
        assert current(client) == before  # nothing changed, not even the version
    for demo_mode in (True, False):  # the demo-mode gate is gone (06.1 D-53): HQ writes either way
        monkeypatch.setattr(config, "DEMO_MODE", demo_mode)
        as_seat(client, None)
        r = patch(client, {"shortlist_step": 5 if demo_mode else 10})
        assert r.status_code == 200, (demo_mode, r.json())


def test_a_stale_settings_version_is_refused(hq):
    client = hq
    v = version(client)
    assert patch(client, {"waste_price": WASTE_PRICE}, expected=v).status_code == 200
    r = patch(client, {"waste_price": 1.0}, expected=v)  # a page loaded before the first write
    assert error(r) == (409, "stale_version")
    assert r.json()["version"] == v + 1 and r.json()["message"] == "The demo moved on since you loaded this page."
    assert current(client)["values"]["waste_price"] == WASTE_PRICE and version(client) == v + 1
    assert error(patch(client, {"waste_price": 1.0}, expected=True)) == (400, "bad_request")


def test_another_sandbox_is_unchanged(hq):
    client = hq
    other = TestClient(app)
    sign_in(other)
    theirs_before, their_card = current(other), story(other)
    assert patch(client, {"waste_price": WASTE_PRICE, "min_sellable_days": 2}).status_code == 200
    assert current(client)["values"]["waste_price"] == WASTE_PRICE
    assert current(other) == theirs_before and story(other)["rec_version"] == their_card["rec_version"]
    mine, theirs = client.get("/api/session").json()["run_id"], other.get("/api/session").json()["run_id"]
    with db.tx() as conn:
        runs = {r["run_id"] for r in db.rows(conn, "SELECT DISTINCT run_id FROM run_policy")}
        template = db.one(conn, "SELECT template_run_id, dataset_version FROM demo_runs WHERE run_id = :r", r=mine)
    assert mine in runs and theirs not in runs  # rows for this sandbox only
    assert template["template_run_id"] not in runs  # the template keeps the seeded Policy
    seeded = refdata.get(template["dataset_version"]).seed["policy"]
    assert theirs_before["values"] == {f: seeded[f] for f in config.SETTINGS_BOUNDS}  # the reference data too


def test_the_store_rule_uses_the_sandbox_policy(hq):
    client = hq
    seeded = current(client)["values"]["min_sellable_days"]
    bound = config.SETTINGS_BOUNDS["min_sellable_days"]
    days = bound["max"] if seeded != bound["max"] else bound["min"]  # a value in bounds that differs from the seed
    assert patch(client, {"min_sellable_days": days}).status_code == 200
    run_id = client.get("/api/session").json()["run_id"]
    own_world(run_id)  # the store rule's screen is part of advance_day, which owns every series first
    with db.tx() as conn:
        run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
        lot = db.one(conn, "SELECT lot_id FROM lots WHERE run_id = :r AND world = 'baseline_rule' AND qty > 0 "
                           "ORDER BY lot_id LIMIT 1", r=run_id)
        rule_input = inputs.build_input(conn, run, lot["lot_id"], world="baseline_rule")
        assert inputs.sandbox_policy(conn, run).min_sellable_days == days
        candidates = advance.baseline_candidates(conn, run)
        screened = [inputs.build_input(conn, run, c["lot_id"], world="baseline_rule") for c in candidates]
    assert rule_input.policy.min_sellable_days == days  # the store rule's next input carries the sandbox Policy
    assert all(i.policy.min_sellable_days == days for i in screened)


def test_settings_need_an_idempotency_key(hq):
    client = hq
    v = version(client)
    body = {"expected_version": v, "changes": {"waste_price": WASTE_PRICE}}
    for headers in ({}, {"Idempotency-Key": ""}):
        r = client.patch("/api/settings", json=body, headers=headers)
        assert error(r) == (400, "idempotency_key")
    assert version(client) == v and current(client)["values"]["waste_price"] != WASTE_PRICE
    k = key()
    first = client.patch("/api/settings", json=body, headers=k)
    again = client.patch("/api/settings", json=body, headers=k)
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert version(client) == v + 1  # the repeat changed nothing


def test_get_settings_shows_values_and_bounds(client):
    got = current(client)
    session = client.get("/api/session").json()
    seeded = refdata.get(refdata_version(session["run_id"])).seed["policy"]
    assert got["version"] == session["version"]
    assert got["values"] == {field: seeded[field] for field in CONTRACT_BOUNDS}
    assert got["bounds"] == CONTRACT_BOUNDS


def refdata_version(run_id: str) -> str:
    with db.tx() as conn:
        return db.one(conn, "SELECT dataset_version FROM demo_runs WHERE run_id = :r", r=run_id)["dataset_version"]


def test_the_waste_price_unit_comes_from_the_currency_setting(hq, monkeypatch):
    client = hq
    """IN-07: the bounds file names no currency; the unit a client sees, in the bounds and in a refusal, is composed
    from the reference data's currency (a test currency here)."""
    raw = config.BOUNDS_FILE.read_text(encoding="utf-8")
    ref = refdata.get(refdata_version(client.get("/api/session").json()["run_id"]))
    assert ref.currency not in raw and "{currency}" in config.SETTINGS_BOUNDS["waste_price"]["unit"]
    monkeypatch.setattr(ref, "currency", "EUR")
    assert current(client)["bounds"]["waste_price"]["unit"] == "EUR/kg"
    r = patch(client, {"waste_price": 100.01})
    assert error(r) == (400, "out_of_bounds")
    assert r.json()["message"] == "waste_price must be between 0 and 100 EUR/kg"
    assert r.json()["bound"] == {"min": 0, "max": 100, "unit": "EUR/kg"}
