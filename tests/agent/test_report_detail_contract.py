"""Detail correction contracts retain report history, server authority and attribution."""
from tests.api import conftest as api_fixtures
from tests.api.conftest import key
from tests.api.test_signals import today_iso
from tests.agent.test_report_reading import submit, chiller
from tests.agent.test_readings_http import FakeModels, install
from agent.reading import Reading
import pytest
from datetime import datetime, timezone, date, timedelta


def irrelevant(client, monkeypatch):
    install(monkeypatch, FakeModels(Reading(kind="NOT_RELEVANT")))
    text = "Staff party Friday."
    reading = client.post("/api/readings", files={"text": (None, text)}).json()
    response = submit(client, "NOT_RELEVANT", {}, text, reading["reading_id"], {"kind": "gemini_accepted"})
    assert response.status_code == 200, response.text
    return response.json()


def test_not_relevant_corrects_kind_in_same_report_with_idempotency(client, monkeypatch):
    report = irrelevant(client, monkeypatch)
    day = today_iso(client)
    body = {"revision": 1, "kind": "CAPACITY", "fields": {"zone": "chilled", "free_totes": 0, "valid_from": day, "valid_to": day}}
    headers = key()
    changed = client.patch(f"/api/signals/{report['signal_id']}", json=body, headers=headers)
    assert changed.status_code == 200, changed.text
    out = changed.json()
    assert (out["report_id"], out["revision"], out["kind"], out["state"]) == (report["report_id"], 2, "CAPACITY", "READ")
    assert out["signal_id"] != report["signal_id"] and out["reading"] == report["reading"]
    again = client.patch(f"/api/signals/{report['signal_id']}", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["signal_id"] == out["signal_id"]
    history = client.get(f"/api/signals/{report['signal_id']}").json()["revisions"]
    assert [(r["kind"], r["state"]) for r in history] == [("NOT_RELEVANT", "REPLACED"), ("CAPACITY", "READ")]
    assert out["field_corrections"]["kind"]["role"] == "manager"


def test_correction_attribution_is_field_and_revision_specific(client, monkeypatch):
    text, reading, suggested, _ = chiller(client, monkeypatch)
    fields = {name: suggested[name] for name in ("zone", "free_totes", "valid_from", "valid_to")}
    report = submit(client, "CAPACITY", fields, text, reading["reading_id"],
        {name: "gemini_accepted" for name in ("kind", *fields)}).json()
    changed = client.patch(f"/api/signals/{report['signal_id']}", json={"revision": 1,
        "fields": {"free_totes": fields["free_totes"] + 1}}, headers=key()).json()
    corrections = changed.get("field_corrections")
    assert corrections and set(corrections) == {"free_totes"}
    assert corrections["free_totes"] == {"role": "manager", "branch": report["branch"], "revision": 2}
    assert changed["field_sources"]["free_totes"] == "typed"  # existing contract stays intact
    later = client.patch(f"/api/signals/{changed['signal_id']}", json={"revision": 2,
        "fields": {"zone": "ambient"}}, headers=key()).json()
    assert later["field_corrections"]["free_totes"] == corrections["free_totes"]
    assert later["field_corrections"]["zone"]["revision"] == 3
    history = client.get(f"/api/signals/{report['signal_id']}").json()["revisions"]
    assert history[0]["field_corrections"] == {}
    assert history[1]["field_corrections"] == corrections


def test_kind_correction_respects_actor_role_and_stale_revision(client, monkeypatch):
    report = irrelevant(client, monkeypatch)
    bad = client.patch(f"/api/signals/{report['signal_id']}", json={"revision": 9, "kind": "CAPACITY", "fields": {}}, headers=key())
    assert bad.status_code == 409 and bad.json()["error"] == "stale_version"
    api_fixtures.sign_in(client, "HQ")
    hq = irrelevant(client, monkeypatch)
    bad = client.patch(f"/api/signals/{hq['signal_id']}", json={"revision": 1, "kind": "CAPACITY", "fields": {}}, headers=key())
    assert bad.status_code == 403


def test_existing_operational_kind_cannot_be_reclassified(client, monkeypatch):
    text, reading, suggested, _ = chiller(client, monkeypatch)
    fields = {name: suggested[name] for name in ("zone", "free_totes", "valid_from", "valid_to")}
    report = submit(client, "CAPACITY", fields, text, reading["reading_id"],
        {name: "gemini_accepted" for name in ("kind", *fields)}).json()
    result = client.patch(f"/api/signals/{report['signal_id']}", json={"revision": 1, "kind": "DEMAND_EVENT", "fields": {}}, headers=key())
    assert result.status_code == 400 and result.json()["error"] == "bad_request"


@pytest.mark.parametrize("kind", ["STOCK_COUNT", "EXPIRY"])
@pytest.mark.parametrize("observed", [None, "", "valid"])
def test_checked_kind_correction_requires_explicit_observation(client, monkeypatch, kind, observed):
    from tests.agent.test_readings_http import identity
    from tests.api.conftest import world_rows
    report = irrelevant(client, monkeypatch)
    run, actor = identity(client)
    lot = next(row for row in world_rows(run, "lots") if row["world"] == "shelfshift" and row["branch"] == actor["branch"])
    fields = {"lot_id": lot["lot_id"]}
    fields.update({"shelf_units": 0, "backroom_units": 0} if kind == "STOCK_COUNT" else
        {"printed_date": (date.fromisoformat(today_iso(client)) + timedelta(days=1)).isoformat()})
    body = {"revision": 1, "kind": kind, "fields": fields}
    if observed is not None:
        body["observed"] = datetime.now(timezone.utc).isoformat() if observed == "valid" else observed
    response = client.patch(f"/api/signals/{report['signal_id']}", json=body, headers=key())
    if observed == "valid":
        assert response.status_code == 200, response.text
        assert response.json()["observed_at"] is not None and response.json()["revision"] == 2
    else:
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "check_time"
        history = client.get(f"/api/signals/{report['signal_id']}").json()["revisions"]
        assert len(history) == 1 and history[0]["state"] == "READ"
