"""Helpers for the commitment rule's tests (07-CONTEXT D-12, D-26; 07-09).

- `required_fixture`: a real Required check. It changes the workspace's record (an explicit zero sales row yesterday
  at the story case's source branch for its SKU, research Q1; and the case lot's last count moved to yesterday, since
  the story lot is counted at the template's snapshot and a lot counted today is never suspect) and re-evaluates the
  case, so the engine itself finds the phantom fact at the source and judges the recommended plan not allowed under
  it. It never injects a fact (`add_facts`), because the Required reasons are recomputed with `check_plan` and an
  injected fact names no limit.
- `ack`: the exact acknowledgement an approve or accept must send for the case's plan in force (D-26), None when
  it has no Advisory fact; `approve_with_ack` approves with it. Every approve or accept that acts on a plan with
  Advisory facts sends it (the sanctioned test change of 07-09).
- `shown`: the plan key the case page shows (the card's `suggested_key`), which every approve and accept names (07
  review C, D-32; the sanctioned test change of the review fix).
"""
from __future__ import annotations

from api import cases, db, workflow
from tests.api.conftest import by_tag, fresh, own_series

WORLD = "shelfshift"
ZERO_ROW = ("demand", "sold", "wasted", "lost", "revenue", "transport", "handling", "disposal")


def story(run) -> dict:
    return by_tag(run, cases.STORY_TAG)


def required_fixture(run, case_id: str | None = None) -> dict:
    """Make the source's phantom stock fact a Required check of the case (the story case by default) and return that
    check, read from the active version. Yesterday's outcome row of the source branch and SKU on the workspace's own
    copy of the series gets sold = 0 (a row the test writes when the record has none), the case lot's last count is
    moved to yesterday when it was counted today (a lot counted at this snapshot is never suspect), then the case is
    re-evaluated.
    Hand-back assertion: the template must give a PHANTOM_STOCK fact at the source with pick_feasible_if_alt false."""
    with db.tx() as conn:
        case = cases.case_row(conn, run["run_id"], case_id) if case_id else story(run)
    branch, sku, cid = case["source_branch"], case["sku"], case["case_id"]
    own_series(run, branch, sku)  # the workspace writes its own copy of the series first
    with db.tx() as conn:
        r = fresh(run["run_id"])
        assert r["day"] >= 1, "the fixture writes yesterday's outcome row"
        p = {"r": r["run_id"], "w": WORLD, "d": r["day"] - 1, "b": branch, "s": sku}
        n = db.run(conn, "UPDATE outcomes SET sold = 0 WHERE run_id = :r AND world = :w AND day = :d AND branch = :b "
                         "AND sku = :s", **p)
        if not n:
            db.run(conn, f"INSERT INTO outcomes (run_id, world, day, branch, sku, {', '.join(ZERO_ROW)}) VALUES "
                         f"(:r, :w, :d, :b, :s, {', '.join('0' for _ in ZERO_ROW)})", **p)
        db.run(conn, "UPDATE lots SET last_verified_day = :d WHERE run_id = :r AND world = :w AND lot_id = :l "
                     "AND last_verified_day >= :d", r=p["r"], w=WORLD, d=p["d"], l=case["lot_id"])
        cases.evaluate_case(conn, r, cases.case_row(conn, r["run_id"], cid))
        rec = cases.active_rec(conn, cases.case_row(conn, r["run_id"], cid))
    check = next((c for c in rec["fact_checks"] if c["kind"] == "PHANTOM_STOCK" and c["branch"] == branch), None)
    assert check is not None and not check["pick_feasible_if_alt"], (
        "hand-back: the template gives no Required phantom check at the source", rec["fact_checks"])
    return check


def ack(run: dict | str, case_id: str) -> dict | None:
    """{rec_version, plan_key, fact_ids} of the case's plan in force (the Advisory fact ids, sorted), or None: the
    case detail's `commitment.ack`, as a client reads it."""
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        return cases.detail(conn, fresh(run_id), case_id)["commitment"]["ack"]


def shown(run: dict | str, case_id: str) -> str | None:
    """The plan key the case page shows, which every approve and accept names (07 review C; 07-CONTEXT D-23, D-32):
    the card's `suggested_key`, the plan in force, as a client reads it."""
    run_id = run if isinstance(run, str) else run["run_id"]
    with db.tx() as conn:
        r = fresh(run_id)
        return cases.card(conn, r, cases.case_row(conn, run_id, case_id), [])["suggested_key"]


def approve_with_ack(run, case_id: str, actor: dict) -> dict:
    """The source's approve on the active version, naming the plan the page shows and acknowledging the plan in
    force's Advisory facts."""
    with db.tx() as conn:
        v = cases.case_row(conn, run["run_id"], case_id)["active_rec_version"]
    given, key = ack(run, case_id), shown(run, case_id)
    return db.in_tx(lambda conn: workflow.decide(conn, db.lock_run(conn, run["run_id"]), actor, case_id, v, "approve",
                                                 None, given, key))
