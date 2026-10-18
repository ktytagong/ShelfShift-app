"""Sandboxes: a clone equals its template, never touches another sandbox, and a reset replaces it inside 2 s or not at
all (spec §8 Sandboxes, template and reset; FLOW-01, FLOW-02). The 2 s target is the literal below and is never
raised: a slow reset is fixed in the clone. The passcode route and its cookie are tested in 04-11.

The template's warm-up acts through the same services a visitor uses, and no plan breaks a limit. The reviewer cases'
exact outcomes live only in tests/api/regression.json: `REGEN=1` rewrites it from the template, and its diff is
reviewed before it is committed; it is never regenerated to make a failing test pass (D-14, D-21).
"""
import json
import time
import os
from pathlib import Path

import pytest

from api import advance, cases, db, refdata, sandbox
from api.errors import ApiError
from data import forecast as F
from tests.api.conftest import fresh, sign_in
from tests.api.test_advance import advance_once, prepare_story, rows_without_run_id
from tests.api.test_db_and_refdata import ref_copy

ISOLATED = ("lots", "truth_lots", "outcomes", "inbound", "world_events", "world_actions", "activity_log")


def tables():
    with db.tx() as conn:
        return sandbox.per_run_tables(conn)


def add_session(session_id, run_id):
    with db.tx() as conn:
        db.run(conn, "INSERT INTO sessions (session_id, role, branch, run_id) VALUES (:s, 'manager', NULL, :r)",
               s=session_id, r=run_id)


def session_run(session_id):
    with db.tx() as conn:
        return db.one(conn, "SELECT run_id, last_reset_ms FROM sessions WHERE session_id = :s", s=session_id)


def test_a_clone_equals_its_template(run, template):
    """Rewritten for the shared dataset (05.2 D-08..D-10, the user's reset-protocol change): every workflow table's
    rows equal the template's except run_id; the world tables are read through the template (tests/api/
    test_overlay.py), so the new run has no row of its own in any of them."""
    from api import overlay
    assert {"lots", "truth_lots", "inbound", "outcomes", "activity_log", "run_policy"} <= set(tables())
    assert not set(tables()) & sandbox.NOT_CLONED
    assert set(overlay.OVERLAID) <= set(tables())
    for table in tables():
        if table in overlay.OVERLAID:
            assert rows_without_run_id(run["run_id"], table) == [], table
        else:
            assert rows_without_run_id(run["run_id"], table) == rows_without_run_id(template, table), table
    assert rows_without_run_id(run["run_id"], "activity_log")  # the warm-up's history was copied, not an empty run
    assert rows_without_run_id(template, "outcomes")  # the world the clone reads through its template
    t = fresh(template)
    assert (run["kind"], run["status"], run["template_run_id"]) == ("SANDBOX", "ACTIVE", template)
    assert (run["day"], run["version"], run["next_case_no"]) == (t["day"], t["version"], t["next_case_no"])


def test_nothing_in_a_clone_points_at_the_template(run, template):
    with db.tx() as conn:
        for table in sandbox.per_run_tables(conn):
            dump = json.dumps(db.rows(conn, f"SELECT * FROM {table} WHERE run_id = :r", r=run["run_id"]), default=str)
            assert template not in dump, table


def test_the_template_opens_on_the_demo_open_day(template):
    t = fresh(template)
    calendar = refdata.get(t["dataset_version"]).calendar
    assert (t["kind"], t["status"], t["failure"]) == ("TEMPLATE", "ACTIVE", None)
    assert t["day"] == calendar["demo_open_day"]
    assert t["version"] == calendar["demo_open_day"]  # one advance per warm-up day
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template
        days = [r["day"] for r in db.rows(conn, "SELECT DISTINCT day FROM outcomes WHERE run_id = :r ORDER BY day",
                                         r=template)]
    assert days == list(range(calendar["demo_open_day"]))


def template_ids():
    with db.tx() as conn:
        return {r["run_id"] for r in db.rows(conn, "SELECT run_id FROM demo_runs WHERE kind = 'TEMPLATE'")}


def test_a_template_whose_warm_up_fails_is_marked_failed_and_the_previous_stays(template, monkeypatch):
    real, calls = advance.advance_day, []

    def breaks_on_the_second_day(conn, run_id, expected_version):
        calls.append(run_id)
        if len(calls) == 2:
            raise RuntimeError("warm-up broke")
        return real(conn, run_id, expected_version)

    monkeypatch.setattr(advance, "advance_day", breaks_on_the_second_day)
    before = template_ids()
    with pytest.raises(sandbox.TemplateError, match="warm-up broke"):
        sandbox.build_template(fresh(template)["dataset_version"])
    [new] = template_ids() - before
    failed = fresh(new)
    assert (failed["status"], failed["day"]) == ("FAILED", 1)
    assert "warm-up broke" in failed["failure"]
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template
    assert fresh(template)["status"] == "ACTIVE"


def test_two_sandboxes_never_affect_each_other(run, template):
    with db.tx() as conn:
        other = sandbox.clone(conn, sandbox.active_template(conn))
    before = fresh(other)
    snapshot = {t: rows_without_run_id(other, t) for t in ISOLATED}
    advance_once(run)
    advance_once(run)
    assert fresh(run["run_id"])["day"] == run["day"] + 2
    after = fresh(other)
    assert (after["day"], after["version"], after["status"]) == (before["day"], before["version"], before["status"])
    assert {t: rows_without_run_id(other, t) for t in ISOLATED} == snapshot
    assert rows_without_run_id(run["run_id"], "outcomes") != snapshot["outcomes"]  # the advanced one did move


def test_a_reset_replaces_the_sandbox_within_2_s(run):
    add_session("reset-ok", run["run_id"])
    old = run["run_id"]
    for _ in range(2):
        new, ms = db.in_tx(sandbox.reset, "reset-ok")
        assert new != old
        assert 1 <= ms < 2000  # the 2 s target (FLOW-01), never raised
        assert session_run("reset-ok") == {"run_id": new, "last_reset_ms": ms}
        assert fresh(old)["status"] == "RETIRED"
        n = fresh(new)
        assert (n["kind"], n["status"], n["day"]) == ("SANDBOX", "ACTIVE", run["day"])
        with pytest.raises(ApiError) as e:  # a write still in flight on the old sandbox
            db.in_tx(db.lock_run, old)
        assert e.value.code == "sandbox_reset"
        old = new


def test_a_reset_that_fails_partway_keeps_the_old_sandbox(run, monkeypatch):
    add_session("reset-fails", run["run_id"])
    half = "sbx-half-cloned"

    def half_clone(conn, template):
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, template_run_id, status, day, next_case_no) "
                     "VALUES (:n, 'SANDBOX', :v, :t, 'ACTIVE', :d, :no)", n=half, v=template["dataset_version"],
               t=template["run_id"], d=template["day"], no=template["next_case_no"])
        db.run(conn, "INSERT INTO lots SELECT :n, world, lot_id, branch, sku, qty, expiry_day, received_seq, markdown_pct, "
                     "expiry_source, last_verified_day, verified_by, tag, origin, moved_from FROM lots WHERE run_id = :t",
               n=half, t=template["run_id"])
        raise RuntimeError("connection lost mid-clone")

    monkeypatch.setattr(sandbox, "clone", half_clone)
    with pytest.raises(RuntimeError, match="mid-clone"):
        db.in_tx(sandbox.reset, "reset-fails")
    assert session_run("reset-fails") == {"run_id": run["run_id"], "last_reset_ms": None}
    assert fresh(run["run_id"])["status"] == "ACTIVE"
    assert fresh(half) is None
    assert rows_without_run_id(half, "lots") == []


def test_a_reset_without_an_active_template_is_preparing(run, monkeypatch):
    add_session("reset-preparing", run["run_id"])
    monkeypatch.setattr(sandbox, "active_template", lambda conn: None)
    with pytest.raises(ApiError) as e:
        db.in_tx(sandbox.reset, "reset-preparing")
    assert (e.value.status, e.value.code, e.value.message) == (503, "preparing", "The workspace is being prepared. Try again in a moment.")
    assert session_run("reset-preparing")["run_id"] == run["run_id"]


def test_the_build_cli_refuses_the_stand_in_without_allow_local(template, tmp_path, capsys):
    out = ref_copy(tmp_path)
    seed = json.loads((out / "seed.json").read_text(encoding="utf-8"))
    F.write_forecast(out / "forecast.json", F.run_forecasts(seed, F.LOCAL))
    before = template_ids()
    assert sandbox.main(["build", str(out)]) == 1
    assert "local stand-in" in capsys.readouterr().err
    assert template_ids() == before
    with db.tx() as conn:
        assert sandbox.active_template(conn)["run_id"] == template


# --- the warm-up acts through the services; no plan broke a limit; the reviewer outcomes (D-14, D-21) ----------------

REGRESSION = Path(__file__).with_name("regression.json")
DONE_OR_LATER = ("PREPARED", "EXECUTED", "OBSERVED", "CLOSED")


def test_the_warm_up_acts_through_the_service_layer(template):
    t = fresh(template)
    with db.tx() as conn:
        warm = db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND opened_day < :d ORDER BY case_id",
                       r=template, d=t["day"])
        for case in warm:
            cid = case["case_id"]
            seats = {d["role"]: d["session_id"] for d in db.rows(
                conn, "SELECT role, session_id FROM decisions WHERE run_id = :r AND case_id = :c", r=template, c=cid)}
            types = [a["type"] for a in db.rows(conn, "SELECT type FROM activity_log WHERE run_id = :r AND case_id = :c "
                                                      "ORDER BY seq", r=template, c=cid)]
            work = db.rows(conn, "SELECT kind, state FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                                 "AND case_id = :c", r=template, c=cid)
            plan = cases.parse_plan(cases.active_rec(conn, case)["result"]["recommended"])
            assert case["state"] in DONE_OR_LATER, (cid, case["state"])
            assert seats.get("source") == "warmup", cid  # the source manager's approval, through workflow.decide
            assert ("destination" in seats) == (plan.kind == "TRANSFER"), cid
            assert types.index("sent") < types.index("committed"), cid
            if plan.kind != "KEEP":
                assert "prepared" in types and work, cid
            assert {w["state"] for w in work} <= {"DONE", "VOIDED"}, cid  # every work order ran or was voided
    assert warm, "cases open during the warm-up"


def test_no_plan_broke_a_limit(run, template):
    t = fresh(template)
    ref = refdata.get(t["dataset_version"])
    with db.tx() as conn:
        sandbox.limits_check(conn, t, ref)
    prepare_story(run)  # a committed transfer books storage and truck space, then ships
    advance_once(run)
    r = fresh(run["run_id"])
    with db.tx() as conn:
        booked = db.rows(conn, "SELECT kind, state FROM reservations WHERE run_id = :r AND kind IN ('STORAGE', 'TRUCK')",
                         r=r["run_id"])
        sandbox.limits_check(conn, r, ref)
        storage = db.one(conn, "SELECT bucket, day FROM reservations WHERE run_id = :r AND kind = 'STORAGE' "
                               "ORDER BY day LIMIT 1", r=r["run_id"])
        dest, zone = storage["bucket"].split("|")
        cap = ref.capacity[(dest, zone, storage["day"])]
        db.run(conn, "INSERT INTO reservations (run_id, res_id, case_id, round, kind, bucket, day, qty, state) VALUES "
                     "(:r, 'test-over', 'TEST', 1, 'STORAGE', :b, :d, :q, 'COMMITTED')", r=r["run_id"],
               b=storage["bucket"], d=storage["day"], q=float(cap))  # with the case's own totes, one over capacity
        with pytest.raises(sandbox.TemplateError, match=f"STORAGE {storage['bucket']} day {storage['day']}"):
            sandbox.limits_check(conn, r, ref)
        conn.rollback()
    assert {b["kind"] for b in booked} == {"STORAGE", "TRUCK"}


def regression_snapshot(template) -> dict:
    """The Day-3 reviewer cases' outcomes on the template, found by tag (only SUS-19's key is stable)."""
    t = fresh(template)
    out = {"dataset_version": t["dataset_version"], "day": t["day"], "cases": {}}
    with db.tx() as conn:
        for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = ANY(:t) AND opened_day = :d "
                               "ORDER BY tag", r=template, t=list(sandbox.REVIEWER_TAGS), d=t["day"]):
            rec = cases.active_rec(conn, c)
            res = rec["result"]
            out["cases"][c["tag"]] = {
                "key": c["key"], "recommended": res["recommended"], "scenario_wins": res["scenario_wins"],
                "decide_by": res.get("decide_by"), "markdown_step": res.get("markdown_step"),
                "net_base": {o["key"]: round(o["net"]["base"], 2) for o in res["options"]},
                "fact_checks": [{k: f[k] for k in ("fact_id", "kind", "lot_ids", "pick_changed", "changes_plan")}
                                for f in rec["fact_checks"]],
            }
    return json.loads(json.dumps(out))  # the JSON form the file holds


def test_generated_regression_set(template):
    """Catches unintended changes only; it does not prove correctness. REGEN=1 rewrites the file: review the diff
    before committing it, and never regenerate it to make a failing test pass (D-14)."""
    got = regression_snapshot(template)
    assert sorted(got["cases"]) == sorted(sandbox.REVIEWER_TAGS)
    assert got["cases"][cases.STORY_TAG]["key"] == cases.STORY_KEY
    if os.environ.get("REGEN") == "1":
        REGRESSION.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        pytest.skip("regression.json written: review the diff before committing it")
    assert REGRESSION.exists(), "no tests/api/regression.json: rebuild with REGEN=1 and review the diff"
    want = json.loads(REGRESSION.read_text(encoding="utf-8"))
    assert got == want, "the reviewer outcomes changed: rebuild with REGEN=1 and review the diff"


# --- pruning what no live session can reach (IN-01) ------------------------------------------------------------------

def every_row(run_id) -> dict:
    """Every per-run row of a run, without run_id, plus its demo_runs row: what pruning must never touch."""
    with db.tx() as conn:
        head = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
    return {"demo_runs": head, **{t: rows_without_run_id(run_id, t) for t in tables()}}


KEPT = (("keys", "SELECT key, session_id, run_id, endpoint FROM idempotency_keys ORDER BY 1, 2, 3, 4"),
        ("usage", "SELECT scope, kind, window_start FROM usage_counts ORDER BY 1, 2, 3"),
        ("cache", "SELECT cache_key FROM engine_cache ORDER BY 1"))


def shared_rows() -> dict:
    with db.tx() as conn:
        return {name: db.rows(conn, sql) for name, sql in KEPT}


def test_pruning_ends_only_unreachable_data_in_bounded_batches_and_reset_stays_within_2_s(client, template):
    """IN-01: a reset prunes one bounded batch (settings `prune`) of what no live session can reach: sandboxes
    retired longer ago than the retention, with their idempotency keys; session-scoped keys older than the session
    cookie; usage counts past their window; old engine-cache rows. A live sandbox, a sandbox retired just now, the
    current keys, windows and cache rows are untouched, and the whole reset, prune included, meets the 2 s target."""
    from fastapi.testclient import TestClient
    from api import config
    from api.app import app
    from tests.api.conftest import key
    p = config.PRUNE
    live_client = TestClient(app, client=("in01-live", 50000))
    sign_in(live_client)
    live = live_client.get("/api/session").json()["run_id"]
    sign_in(live_client, "HQ")  # a keyed sign-in on a session with a workspace: a live session-scoped key (06.1-06)
    mine = client.get("/api/session").json()["run_id"]  # retired by the reset below: inside the retention
    with db.tx() as conn:
        old = [sandbox.clone(conn, sandbox.active_template(conn)) for _ in range(p["runs_per_batch"] + 1)]
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = now() - make_interval(hours => :h) "
                     "WHERE run_id = ANY(:ids)", h=p["retired_sandbox_hours"] + 1, ids=old)
        db.run(conn, "INSERT INTO idempotency_keys (key, session_id, run_id, endpoint, body_hash, created_at) "
                     "SELECT 'in01-run', 'in01', run_id, 'send:x', 'h', now() FROM unnest(CAST(:ids AS text[])) "
                     "AS run_id", ids=old)
        db.run(conn, "INSERT INTO idempotency_keys (key, session_id, run_id, endpoint, body_hash, created_at) VALUES "
                     "('in01-old', 'in01', '', 'reset', 'h', now() - make_interval(days => :d))",
               d=p["session_key_days"] + 1)
        db.run(conn, "INSERT INTO usage_counts (scope, kind, window_start, count) VALUES "
                     "('in01', 'reset', now() - make_interval(days => :d), 1)", d=p["usage_window_days"] + 1)
        db.run(conn, "INSERT INTO engine_cache (cache_key, rec_version, result, fact_checks, created_at) VALUES "
                     "('in01-old', 'v', '{}', '[]', now() - make_interval(days => :d))", d=p["engine_cache_days"] + 1)
    kept = shared_rows()
    before = {run_id: every_row(run_id) for run_id in (live, mine)}
    started = time.perf_counter()
    r = client.post("/api/demo/reset", json={}, headers=key())
    elapsed = time.perf_counter() - started
    assert r.status_code == 200 and r.json()["reset_ms"] < 2000 and elapsed < 2.0  # the 2 s target, never raised
    assert every_row(live) == before[live]  # a live sandbox: not one row touched
    after = every_row(mine)  # retired just now: only its status and retirement time changed
    assert after["demo_runs"]["status"] == "RETIRED" and after["demo_runs"]["retired_at"] is not None
    assert {t: v for t, v in after.items() if t != "demo_runs"} == {t: v for t, v in before[mine].items()
                                                                    if t != "demo_runs"}
    gone = [run_id for run_id in old if fresh(run_id) is None]
    assert len(gone) == p["runs_per_batch"]  # one bounded batch per request; the rest waits for the next one
    assert all(every_row(run_id) == {"demo_runs": None, **{t: [] for t in tables()}} for run_id in gone)
    now = shared_rows()
    lost = {name: [row for row in rows if row not in now[name]] for name, rows in kept.items()}
    assert lost == {"keys": [k for k in kept["keys"] if k["key"] == "in01-old" or k["run_id"] in gone],
                    "usage": [u for u in kept["usage"] if u["scope"] == "in01"],
                    "cache": [{"cache_key": "in01-old"}]}
    assert live_client.get("/api/cases").status_code == 200  # the live sandbox still works
    with db.tx() as conn:
        sandbox.prune(conn)  # the next batch takes the rest
    assert [run_id for run_id in old if fresh(run_id) is not None] == []
