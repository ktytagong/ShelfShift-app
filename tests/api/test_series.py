"""One series' history and forecast on request (spec §5 Public dataset; 05.2-CONTEXT D-11; GET /api/series).

The history of the selected store and product only, for a bounded window: the public daily rows before the
workspace's day and the forecast made on that day from it on. Nothing loads the dataset, and no hidden demand or
physical stock leaves the server. The module's tiny public template (day 0) is ACTIVE for the module, so each new
session's workspace is cloned from it; the synthetic template is ACTIVE again at teardown.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import db, refdata, sandbox, views
from api.app import app
from tests.api.conftest import fresh, sign_in

HISTORY_KEYS = {"day", "date", "units", "stockout", "discount", "unknown_price", "holiday", "activity", "weather"}
FORECAST_KEYS = {"day", "date", "low", "base", "high", "provider"}
WEATHER_KEYS = {"precip", "temp", "humidity", "wind"}
HIDDEN = ("demand", "physical", "qty", "truth", "sold", "wasted")


@pytest.fixture(scope="module")
def public_client(template, public_run):
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


def no_hidden_field(body) -> None:
    """No key anywhere in the answer names hidden demand, physical stock or a world's outcome."""
    if isinstance(body, dict):
        for k, v in body.items():
            assert not any(h in k for h in HIDDEN), k
            no_hidden_field(v)
    elif isinstance(body, list):
        for v in body:
            no_hidden_field(v)


def test_one_public_series_answers_its_history_and_the_days_forecast(public_client, public_run):
    ref = refdata.get(public_run["dataset_version"])
    store, sku = ref.branches[0], sorted(ref.products)[0]
    ws = fresh(public_client.get("/api/session").json()["run_id"])
    r = public_client.get(f"/api/series/{store}/{sku}?from=-14&to=3")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert (body["store"], body["sku"], body["from"], body["to"], body["day"]) == (store, sku, -14, 3, ws["day"])
    with db.tx() as conn:
        sales = db.rows(conn, "SELECT * FROM ref_sales_day WHERE dataset_version = :v AND store = :s AND sku = :k "
                              "AND day BETWEEN -14 AND :d ORDER BY day", v=ref.dataset_version, s=store, k=sku,
                        d=ws["day"] - 1)
        weather = {w["day"]: w for w in db.rows(conn, "SELECT * FROM ref_weather_day WHERE dataset_version = :v AND "
                                                      "store = :s", v=ref.dataset_version, s=store)}
    assert [h["day"] for h in body["history"]] == [s["day"] for s in sales] == list(range(-14, ws["day"]))
    for h, s in zip(body["history"], sales):
        assert set(h) == HISTORY_KEYS and h["date"] == ref.date(h["day"]).isoformat()
        assert {k: h[k] for k in ("units", "stockout", "discount", "unknown_price", "holiday", "activity")} == \
            {k: s[k] for k in ("units", "stockout", "discount", "unknown_price", "holiday", "activity")}
        w = weather[h["day"]]
        assert set(h["weather"]) == WEATHER_KEYS and h["weather"] == {k: w[k] for k in WEATHER_KEYS}
    points = ref.forecast(ws["day"], store, sku)
    assert [f["day"] for f in body["forecast"]] == [d for d in sorted(points) if ws["day"] <= d <= 3]
    assert body["forecast"], "the day's forecast run covers the window's days from today"
    for f in body["forecast"]:
        assert set(f) == FORECAST_KEYS and (f["low"], f["base"], f["high"]) == tuple(points[f["day"]])
        assert f["provider"] == ref.provider_of(ws["day"], store, sku, f["day"])
    no_hidden_field(body)


def test_the_window_is_bounded_and_the_series_must_exist(public_client, public_run):
    ref = refdata.get(public_run["dataset_version"])
    store, sku = ref.branches[0], sorted(ref.products)[0]
    longest = ref.series["max_days"]
    ok = public_client.get(f"/api/series/{store}/{sku}?from={-longest + 1}&to=0")
    assert ok.status_code == 200, ok.json()
    for query in (f"from={-longest}&to=0", "from=3&to=2", "from=-3", "to=3", "from=x&to=1", "from=-3&to=1&to=2"):
        r = public_client.get(f"/api/series/{store}/{sku}?{query}")
        assert (r.status_code, r.json()["error"]) == (400, "bad_request"), query
    for path in (f"S999/{sku}", f"{store}/P999"):
        r = public_client.get(f"/api/series/{path}?from=-3&to=1")
        assert (r.status_code, r.json()["error"]) == (404, "not_found"), path


def test_the_history_read_runs_under_the_sets_statement_timeout_and_puts_it_back(public_run):
    ref = refdata.get(public_run["dataset_version"])
    seen = []
    with db.tx() as conn:
        before = db.one(conn, "SELECT setting AS t FROM pg_settings WHERE name = 'statement_timeout'")["t"]
        real = db.rows

        def spy(c, sql, **p):
            if "ref_sales_day" in sql:
                seen.append(db.one(c, "SELECT setting AS t FROM pg_settings WHERE name = 'statement_timeout'")["t"])
            return real(c, sql, **p)
        db.rows = spy
        try:
            rows = ref.history(ref.branches[0], sorted(ref.products)[0], -3, -1)
        finally:
            db.rows = real
        after = db.one(conn, "SELECT setting AS t FROM pg_settings WHERE name = 'statement_timeout'")["t"]
    assert [r["day"] for r in rows] == [-3, -2, -1]
    assert seen == [str(ref.series["statement_timeout_ms"])] and after == before


def test_the_synthetic_set_answers_from_the_seeds_daily_sales(template, public_client):
    """A workspace of the synthetic template, cloned from it directly (the public one is ACTIVE in this module)."""
    with db.tx() as conn:
        rid = sandbox.clone(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :t", t=template))
    ws = fresh(rid)
    ref = refdata.get(ws["dataset_version"])
    store, sku = ref.branches[0], next(iter(ref.products))
    with db.tx() as conn:
        body = views.series(conn, ws, store, sku, -5, ws["day"] + 2)
    seed = {(r["day"]): r for r in ref.seed["daily_sales"] if r["branch"] == store and r["sku"] == sku}
    assert [h["day"] for h in body["history"]] == [-5, -4, -3, -2, -1]
    for h in body["history"]:
        assert set(h) == HISTORY_KEYS and h["weather"] is None and h["discount"] is None
        assert (h["units"], h["stockout"]) == (seed[h["day"]]["recorded"], seed[h["day"]]["stockout_flag"])
    points = ref.forecast(ws["day"], store, sku)
    assert [f["day"] for f in body["forecast"]] == [d for d in sorted(points) if ws["day"] <= d <= ws["day"] + 2]
    assert body["forecast"]
    no_hidden_field(body)
