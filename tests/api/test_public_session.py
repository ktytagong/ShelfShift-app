"""The dataset's own calendar in every label the server writes, and the session's kind, Data-as-of date and account
(spec §5 Public dataset; 05.2-CONTEXT D-04 as amended 2026-10-17, D-13, D-37).

D-13: the public set's dates are its own sales dates shifted +840 days, weekdays kept: day 0 is Wed 14 Oct 2026 and
"Data as of" (the scenario date) is day 3, Sat 17 Oct 2026. The synthetic set keeps its calendar: day 0 Mon 12 Oct
2026, so day 3 is Thu 15 Oct (spec §5). Expected dates are derived from each set's own reference calendar (`day0`),
and the two spec anchors are checked against them, never read from a response.

The public workspace is cloned through the real `POST /api/session` from the module's tiny public template, made
ACTIVE inside the test; the synthetic template is ACTIVE again at teardown, whatever happened.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, timedelta

import pytest

from api import accounts, cases, db, refdata, views
from tests.api.conftest import key, sign_in

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
API = pathlib.Path(__file__).resolve().parents[2] / "api"


def day0(ref) -> date:
    return date.fromisoformat(ref.calendar["day0"])


def test_ref_date_is_day0_plus_day_from_each_datasets_own_calendar(template, public_dv):
    synthetic, public = refdata.get(refdata_version(template)), refdata.get(public_dv)
    for ref in (synthetic, public):
        for d in (-1, 0, 3, ref.calendar["horizon_end"]):
            assert ref.date(d) == day0(ref) + timedelta(days=d)
    assert (synthetic.date(3), WEEKDAYS[synthetic.date(3).weekday()]) == (date(2026, 10, 15), "Thu")  # spec §5
    assert (public.date(0), WEEKDAYS[public.date(0).weekday()]) == (date(2026, 10, 14), "Wed")  # D-13
    assert (public.date(3), WEEKDAYS[public.date(3).weekday()]) == (date(2026, 10, 17), "Sat")  # D-13: Data as of
    assert public.data_as_of == public.date(3).isoformat() and synthetic.data_as_of is None


def refdata_version(run_id: str) -> str:
    with db.tx() as conn:
        return db.one(conn, "SELECT dataset_version FROM demo_runs WHERE run_id = :r", r=run_id)["dataset_version"]


def test_every_server_label_follows_the_runs_calendar(public_run):
    ref = refdata.get(public_run["dataset_version"])
    d3 = ref.date(3)
    assert cases.weekday(3, ref) == WEEKDAYS[d3.weekday()]
    assert cases.date_label(3, ref) == f"{WEEKDAYS[d3.weekday()]} {d3.day} {d3.strftime('%b')}"
    due = cases.due({"decide_by": {"day": 3, "latest_decision_min": 600}}, ref)
    assert (due["date"], due["weekday"], due["label"]) == (d3.isoformat(), WEEKDAYS[d3.weekday()],
                                                           f"Decide by {WEEKDAYS[d3.weekday()]} 10:00")
    assert views.clock(ref, 3).startswith(cases.date_label(3, ref) + ", ")
    assert views.calendar(ref)[3] == {"day": 3, "date": d3.isoformat(), "weekday": WEEKDAYS[d3.weekday()]}
    # No server text names a day from the synthetic calendar constant (data.world): every label reads the run's Ref.
    for path in sorted(API.glob("*.py")):
        assert not re.search(r"\bdemo_date\b|\bDAY0\b", path.read_text()), path.name


@pytest.fixture
def public_client(template, public_run):
    """A TestClient whose session (and workspace) comes from the tiny public template, made ACTIVE for this test; the
    synthetic template is ACTIVE again at teardown."""
    from fastapi.testclient import TestClient
    from api.app import app
    with db.tx() as conn:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :t", t=template)
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :p", p=public_run["run_id"])
    try:
        c = TestClient(app)
        sign_in(c)
        yield c
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'INITIALIZING' WHERE run_id = :p", p=public_run["run_id"])
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=template)


def test_the_public_session_carries_kind_data_as_of_and_every_store(public_client, public_run):
    ref = refdata.get(public_run["dataset_version"])
    s = public_client.get("/api/session").json()
    assert s["kind"] == "public" and s["data_as_of"] == ref.date(3).isoformat() == ref.calendar["data_as_of"]
    assert "{date}" in s["label"] and s["label"] == ref.settings["mark"]["text"]  # the UI fills {date} (D-04)
    assert "seats" not in s and s["account"] == s["branch"] in ref.branches  # 06.1 D-47: the seat is the account's
    assert all(accounts.resolve_account(ref, b) == (b, "manager", b) for b in ref.stores)  # every store signs in
    assert s["calendar"][0] == {"day": 0, "date": ref.date(0).isoformat(), "weekday": WEEKDAYS[ref.date(0).weekday()]}
    assert s["clock"].startswith(cases.date_label(s["day"], ref))


def test_next_day_on_a_public_workspace_is_refused_on_the_tiny_set(public_client):
    """D-37 B (user): the backend refuses Next day on a public dataset's workspace with 409 not_available and
    changes nothing (05.2-13 built the refusal; this is the tiny public set's own workspace)."""
    before = public_client.get("/api/session").json()
    r = public_client.post("/api/demo/advance-day", json={"expected_version": before["version"]}, headers=key())
    assert r.status_code == 409 and r.json()["error"] == "not_available"
    after = public_client.get("/api/session").json()
    assert (after["day"], after["version"], after["run_id"]) == (before["day"], before["version"], before["run_id"])
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM run_series WHERE run_id = :r", r=after["run_id"])["n"] == 0


def test_the_synthetic_session_keeps_its_label_and_has_no_data_as_of(client, template):
    ref = refdata.get(refdata_version(template))
    s = client.get("/api/session").json()
    assert (s["kind"], s["data_as_of"], s["label"]) == ("synthetic", None, ref.label)
    assert "seats" not in s and s["account"] == s["branch"] in ref.branches  # 06.1 D-47: the seat is the account's
