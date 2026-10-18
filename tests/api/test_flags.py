"""Detect, then evaluate on demand (spec §5 Public dataset; 05.2-CONTEXT D-23..D-25, D-28; api/flags.py).

On the tiny public set the morning screen writes flags and runs no search; a flag reads as Needs evaluation with the
screen's figures and nothing the engine would say; opening it runs the engine once through `cases.evaluate_case`, and
the same item opened in another workspace is served from the shared cache. The module's tiny public template (a
scratch day-0 run with the designed flag restored, quick 261017-ofn) is screened once, made ACTIVE for the module so
every new session's workspace is cloned from it, and the synthetic template is ACTIVE again at teardown, whatever
happened.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from api import cases, db, evaluate, inputs, refdata, screen
from api.app import app
from engine.simulate import HorizonError
from tests.api.conftest import (as_seat, drop_runs, fresh, key, own_series, restore_the_designed_flag,
                               sign_in)

NO_PLAN = ("suggested", "suggested_key", "dest", "at_risk_units", "at_risk_kg", "at_risk_value", "est_benefit", "due",
           "decide_by", "markdown_step", "act_before", "wait", "rec_version")


@pytest.fixture(scope="module")
def flagged(template, public_dv):
    """A scratch day-0 template of the tiny public set with the designed flag restored (conftest
    restore_the_designed_flag; quick 261017-ofn), after its day-0 screen (engine calls counted), ACTIVE for the module.
    At teardown the synthetic template is ACTIVE again, then the scratch run and every workspace cloned from it go."""
    from api import advance, sandbox
    run_id = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=public_dv)
        sandbox.init_worlds(conn, run_id, refdata.get(public_dv), refdata.truth(public_dv))
        advance.day_start(conn, fresh_in(conn, run_id))
        restore_the_designed_flag(conn, fresh_in(conn, run_id))
    calls = []
    real = evaluate.compute
    mp = pytest.MonkeyPatch()
    mp.setattr(evaluate, "compute", lambda inp: calls.append(inp) or real(inp))
    try:
        try:
            with db.tx() as conn:
                opened = cases.open_cases(conn, fresh(run_id))
        finally:
            mp.undo()
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'RETIRED' WHERE run_id = :t", t=template)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :p", p=run_id)
        yield {"run": fresh(run_id), "opened": opened, "searches": len(calls)}
    finally:
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'FAILED', failure = 'test scratch' WHERE run_id = :p", p=run_id)
            db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE' WHERE run_id = :t", t=template)
        with db.tx() as conn:
            clones = [r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE template_run_id = :r",
                                                   r=run_id)]
            drop_runs(conn, [run_id, *clones])


def fresh_in(conn, run_id: str) -> dict:
    """The run's demo_runs row inside an open transaction."""
    return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)


@pytest.fixture
def searches(monkeypatch):
    """Every engine search from here on, counted (evaluate.compute is the one door to the worker)."""
    calls = []
    real = evaluate.compute
    monkeypatch.setattr(evaluate, "compute", lambda inp: calls.append(inp) or real(inp))
    return calls


def flag_rows(run_id: str) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r ORDER BY key", r=run_id)


def session_client(seat: str | None = "") -> TestClient:
    """A new session (its own workspace, cloned from the ACTIVE template); HQ when seat is None, a store's manager
    when it names one, the first store's manager by default."""
    c = TestClient(app)
    sign_in(c)
    if seat != "":
        as_seat(c, seat)
    return c


def workspace(c: TestClient) -> dict:
    return fresh(c.get("/api/session").json()["run_id"])


def test_the_morning_screen_flags_every_qualifying_lot_and_runs_no_search(flagged):
    run = flagged["run"]
    rows = flag_rows(run["run_id"])
    assert rows, "the tiny set's day-0 screen flags at least one lot"
    assert flagged["opened"] == [] and flagged["searches"] == 0  # detection only: no case, no engine search
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r", r=run["run_id"])["n"] == 0
        assert db.one(conn, "SELECT count(*) AS n FROM recommendations WHERE run_id = :r", r=run["run_id"])["n"] == 0
        # every lot the screen qualifies is flagged with the screen's own figures (D-28: none dropped)
        qualifying = {}
        for lot in cases.screen_candidates(conn, run):
            found = screen.screen(inputs.build_input(conn, run, lot["lot_id"]), cases.config.CASE_OPENING)
            if found["qualifies"]:
                qualifying[lot["lot_id"]] = found
    assert {r["lot_id"] for r in rows} == set(qualifying)
    for r in rows:
        found = qualifying[r["lot_id"]]
        assert (r["day"], r["missing_data"]) == (run["day"], None)
        assert {k: r[k] for k in ("a", "b", "step_day", "usable_runs")} == \
            {k: found[k] for k in ("a", "b", "step_day", "usable_runs")}
        assert r["keep_waste"] == pytest.approx(found["keep_waste"]) and \
            r["stock_at_step"] == pytest.approx(found["stock_at_step"])
        assert r["key"].startswith("SUS-")
    assert len({r["key"] for r in rows}) == len(rows)


def test_a_lot_the_screen_cannot_judge_is_flagged_with_its_reason(flagged, public_dv, monkeypatch):
    """A scratch day-0 template of the tiny set whose first candidate lot has no forecast: flagged, never skipped."""
    from api import advance, sandbox
    run_id = f"tpl-pub-{uuid.uuid4().hex[:8]}"
    with db.tx() as conn:
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1)", r=run_id, v=public_dv)
        sandbox.init_worlds(conn, run_id, refdata.get(public_dv), refdata.truth(public_dv))
        advance.day_start(conn, db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id))
    try:
        with db.tx() as conn:
            first = cases.screen_candidates(conn, fresh(run_id))[0]
        real = screen.screen

        def no_forecast(inp, thresholds):
            if inp.case_lot_id == first["lot_id"]:
                raise HorizonError(f"no forecast for {first['branch']} on day 1")
            return real(inp, thresholds)
        monkeypatch.setattr(screen, "screen", no_forecast)
        with db.tx() as conn:
            assert cases.open_cases(conn, fresh(run_id)) == []
        row = next(r for r in flag_rows(run_id) if r["lot_id"] == first["lot_id"])
        assert row["missing_data"] == f"missing data: no forecast for {first['branch']} on day 1"
        assert (row["a"], row["b"], row["keep_waste"], row["stock_at_step"], row["step_day"]) == (None,) * 5
    finally:
        with db.tx() as conn:
            drop_runs(conn, [run_id])


def test_the_board_shows_a_flag_as_needs_evaluation_with_the_screens_figures_only(flagged):
    rows = flag_rows(flagged["run"]["run_id"])
    c = session_client(None)  # HQ sees every store
    board = c.get("/api/cases")
    assert board.status_code == 200, board.json()
    cards = {x["key"]: x for x in board.json()["cases"]}
    for r in rows:
        card = cards[r["key"]]
        assert card["needs_evaluation"] is True and card["column"] == "New"
        assert card["risk"] == {k: r[k] for k in ("a", "b", "keep_waste", "stock_at_step", "step_day")}
        assert all(card[k] is None for k in NO_PLAN), {k: card[k] for k in NO_PLAN}
        assert card["next_owner"] == [] and card["decisions"] == {} and card["hold"] is None
        assert (card["source"], card["sku"], card["lot_id"]) == (r["branch"], r["sku"], r["lot_id"])
        d = c.get(f"/api/cases/{r['key']}")
        assert d.status_code == 200, d.json()
        d = d.json()
        assert d["needs_evaluation"] is True and d["result"] is None and d["fact_checks"] == []
        assert d["engine_input"] is None and d["summary"] == "" and d["approvals"]["resources"] == []
        assert d["lot"]["lot_id"] == r["lot_id"] and d["risk"] == card["risk"]
        assert d["eligibility"]["applies"] is True
    assert c.get("/api/cases/SUS-99999").status_code == 404


def test_opening_a_flag_runs_the_engine_once_and_answers_the_evaluated_case(flagged, searches):
    flag = flag_rows(flagged["run"]["run_id"])[0]
    c = session_client(flag["branch"])
    k = key()
    first = c.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=k)
    assert first.status_code == 200, first.json()
    d = first.json()
    assert d["case_id"] == d["key"] == flag["key"] and "needs_evaluation" not in d
    assert d["result"] is not None and d["rec_version"] and d["suggested_key"] == d["result"]["recommended"]
    assert len(searches) <= 1  # one search, or none when another test already cached this exact input
    ws = workspace(c)
    with db.tx() as conn:
        case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", r=ws["run_id"], c=flag["key"])
        log = db.rows(conn, "SELECT type, payload FROM activity_log WHERE run_id = :r AND case_id = :c ORDER BY seq",
                      r=ws["run_id"], c=flag["key"])
    assert (case["lot_id"], case["source_branch"], case["state"], case["opened_day"]) == \
        (flag["lot_id"], flag["branch"], "OPEN", ws["day"])
    assert [e["type"] for e in log] == ["opened", "new_version"]
    assert {k: log[0]["payload"][k] for k in ("a", "b", "step_day", "usable_runs")} == \
        {k: flag[k] for k in ("a", "b", "step_day", "usable_runs")}
    n = len(searches)
    again = c.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=k)  # the same key: the first answer
    assert again.status_code == 200 and again.json() == d
    other = c.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=key())  # a new key: the same case
    assert other.status_code == 200, other.json()
    assert (other.json()["case_id"], other.json()["rec_version"]) == (d["case_id"], d["rec_version"])
    assert len(searches) == n  # neither ran the engine again
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r", r=ws["run_id"])["n"] == 1
    cards = [x for x in c.get("/api/cases").json()["cases"] if x["lot_id"] == flag["lot_id"]]
    assert len(cards) == 1 and "needs_evaluation" not in cards[0] and cards[0]["rec_version"] == d["rec_version"]


def test_the_same_item_in_another_workspace_is_served_from_the_cache(flagged, monkeypatch):
    flag = flag_rows(flagged["run"]["run_id"])[0]
    a, b = session_client(flag["branch"]), session_client(flag["branch"])
    first = a.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=key())
    assert first.status_code == 200, first.json()
    calls = []
    monkeypatch.setattr(evaluate, "compute", lambda inp: calls.append(inp) or pytest.fail("the engine ran again"))
    second = b.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=key())
    assert second.status_code == 200, second.json()
    assert calls == []
    assert workspace(a)["run_id"] != workspace(b)["run_id"]
    assert second.json()["rec_version"] == first.json()["rec_version"]
    assert second.json()["result"] == first.json()["result"]


def test_a_flag_whose_lot_is_gone_answers_409_and_opens_nothing(flagged, searches):
    flag = flag_rows(flagged["run"]["run_id"])[0]
    c = session_client(flag["branch"])
    ws = workspace(c)
    own_series(ws, flag["branch"], flag["sku"])  # the workspace's own copy, then its lot sells out
    with db.tx() as conn:
        db.run(conn, "UPDATE lots SET qty = 0 WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
               r=ws["run_id"], l=flag["lot_id"])
    r = c.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=key())
    assert r.status_code == 409 and r.json()["error"] == "gone", r.json()
    assert r.json()["message"] == "Lot sold out or expired before evaluation"
    assert searches == []
    with db.tx() as conn:
        assert db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r", r=ws["run_id"])["n"] == 0


def test_a_public_case_says_its_data_is_not_synthetic_and_fills_the_scenario_date(flagged):
    """versions.synthetic is false on a public set (D-03) and every label has its {date} filled (D-04)."""
    ref = refdata.get(flagged["run"]["dataset_version"])
    d = ref.date(3)
    want = ref.label.replace("{date}", f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {d.year}")
    assert "{date}" in ref.label and ref.data_as_of == d.isoformat() and want.endswith("Sat 17 Oct 2026")
    flag = flag_rows(flagged["run"]["run_id"])[0]
    c = session_client(flag["branch"])
    before = c.get(f"/api/cases/{flag['key']}").json()["versions"]
    after = c.post(f"/api/cases/{flag['key']}/evaluate", json={}, headers=key()).json()["versions"]
    for v in (before, after):
        assert (v["synthetic"], v["label"]) == (False, want)
    export = c.get(f"/api/cases/{flag['key']}/export")
    assert export.status_code == 200 and export.json()["label"] == want
    assert "{date}" not in export.text


def oracle_write_flags(conn, run: dict, candidates: list[tuple[dict, dict]]) -> list[str]:
    """write_flags as it was before quick 261017-p52 (one lot at a time, cases.next_key): the oracle."""
    keys = []
    for lot, found in candidates:
        p = {"r": run["run_id"], "d": run["day"], "l": lot["lot_id"]}
        if db.one(conn, "SELECT 1 AS x FROM case_flags WHERE run_id = :r AND day = :d AND lot_id = :l", **p):
            continue
        earlier = db.one(conn, "SELECT key FROM case_flags WHERE run_id = :r AND lot_id = :l ORDER BY day DESC LIMIT 1",
                         r=run["run_id"], l=lot["lot_id"])
        key = earlier["key"] if earlier else cases.next_key(conn, run, lot)
        db.run(conn, "INSERT INTO case_flags (run_id, day, lot_id, key, branch, sku, a, b, keep_waste, stock_at_step, "
                     "step_day, usable_runs, missing_data, tag, origin) VALUES (:r, :d, :l, :k, :br, :s, :a, :b, :w, "
                     ":st, :sd, CAST(:u AS jsonb), :m, :t, :o)", k=key, br=lot["branch"], s=lot["sku"],
               a=found.get("a"), b=found.get("b"), w=found.get("keep_waste"), st=found.get("stock_at_step"),
               sd=found.get("step_day"), u=db.js(list(found.get("usable_runs") or [])), m=found.get("missing"),
               t=lot.get("tag"), o=lot.get("origin"), **p)
        keys.append(key)
    return keys


def flag_candidates(lot_ids, story: str | None = None) -> list[tuple[dict, dict]]:
    """(lot row, screen result) pairs with every kind of value: floats, ints, None, a missing-data reason, runs."""
    out = []
    for i, lot_id in enumerate(lot_ids):
        lot = {"lot_id": lot_id, "branch": f"S{i % 3}", "sku": f"P{i % 2}", "tag": cases.STORY_TAG if lot_id == story
               else None, "origin": None if i % 2 else "simulated"}
        found = ({"qualifies": True, "a": None, "b": None, "usable_runs": [], "step_day": None, "keep_waste": None,
                  "stock_at_step": None, "missing": f"missing data: day {i}"} if i % 5 == 4 else
                 {"qualifies": True, "a": bool(i % 2), "b": not i % 3, "keep_waste": 0.1 * i + 0.2, "stock_at_step": i,
                  "step_day": i % 4, "usable_runs": [f"R-{i}", f"R-{i + 1}"] if i % 3 else []})
        out.append((lot, found))
    return out


def test_write_flags_matches_the_per_row_oracle(schema):
    """Set-based write_flags (quick 261017-p52) writes the same case_flags rows and leaves the same case counter as the
    per-row loop it replaced, on two scratch runs in a rolled-back transaction: numbering across SUS-19 (skipped), the
    story lot's SUS-19 without a number, a lot keeping its earlier key on a second morning while new lots continue the
    numbering, and nothing written by a second call on the same morning."""
    from api import flags
    runs = {"oracle": f"p52-o-{uuid.uuid4().hex[:6]}", "batch": f"p52-b-{uuid.uuid4().hex[:6]}"}
    write = {"oracle": oracle_write_flags, "batch": flags.write_flags}
    first = [f"L{i:03d}" for i in range(12)]
    second = first[3:9] + [f"N{i:03d}" for i in range(5)] + first[:2]
    with db.engine().connect() as conn:
        for rid in runs.values():
            db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                         "(:r, 'TEMPLATE', 'x', 'INITIALIZING', 0, 15)", r=rid)
        got = {}
        for name, rid in runs.items():
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid)
            keys = [write[name](conn, run, flag_candidates(first, story="L007"))]
            db.run(conn, "UPDATE demo_runs SET day = 1 WHERE run_id = :r", r=rid)
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=rid)
            keys.append(write[name](conn, run, flag_candidates(second)))
            keys.append(write[name](conn, run, flag_candidates(second)))  # the same morning again
            rows = [{k: v for k, v in r.items() if k != "run_id"} for r in
                    db.rows(conn, "SELECT * FROM case_flags WHERE run_id = :r ORDER BY day, lot_id", r=rid)]
            got[name] = (keys, rows, db.one(conn, "SELECT next_case_no FROM demo_runs WHERE run_id = :r",
                                            r=rid)["next_case_no"])
        conn.rollback()
    assert got["batch"] == got["oracle"]
    keys, rows, next_no = got["batch"]
    assert keys[2] == [] and len(rows) == len(first) + len(second)  # one row per lot per morning
    assert "SUS-19" in keys[0] and keys[0][first.index("L007")] == "SUS-19" and keys[0].count("SUS-19") == 1
    by_day = {(r["day"], r["lot_id"]): r["key"] for r in rows}
    assert all(by_day[(1, lot)] == by_day[(0, lot)] for lot in first[3:9] + first[:2])
    assert next_no == 15 + len(first) - 1 + 1 + 5  # 11 numbered lots, SUS-19 skipped, 5 new lots on day 1
