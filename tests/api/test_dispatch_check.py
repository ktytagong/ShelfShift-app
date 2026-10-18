"""The dispatch check on the server (06.1 D-35, D-49; research Pattern 5; 04-API-CONTRACT "Product mode (06.1)",
Execution).

The story transfer (SUS-19, the milk-story tag) is driven to PREPARED at service level through send, accept, approve
and prepare (test_advance.prepare_story), then lot holds and review flags are written straight into Phase 7's tables
(test scratch in the test's own workspace). Every block kind the contract names is asserted with its reason.
"""
import re

from api import cases, db, execution
from engine.limits import check_plan
from tests.api.conftest import fresh, own_series
from tests.api.test_advance import prepare_story

STORY = cases.STORY_KEY


def check(run, action_id: str, case_id: str = STORY) -> list[dict]:
    """dispatch_check in its own transaction after the run lock, as the dispatch route makes it."""
    def go(conn):
        r = db.lock_run(conn, run["run_id"])
        case = cases.case_row(conn, r["run_id"], case_id, lock=True)
        action = db.one(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = 'shelfshift' "
                              "AND action_id = :a", r=r["run_id"], a=action_id)
        return execution.dispatch_check(conn, r, case, action)
    return db.in_tx(go)


def add_hold(run, lot_id: str, branch: str, reason: str, hold_id: str = "h-1", kind: str = "MIXED_DATES",
             state: str = "ACTIVE") -> None:
    with db.tx() as conn:
        db.run(conn, "INSERT INTO lot_holds (run_id, hold_id, lot_id, branch, kind, reason, placed_by, state) VALUES "
                     "(:r, :h, :l, :b, :k, :why, :b, :s)", r=run["run_id"], h=hold_id, l=lot_id, b=branch, k=kind,
               why=reason, s=state)


def test_the_story_transfer_prepared_is_allowed(run):
    action = prepare_story(run)
    assert action["state"] == "PLANNED"
    assert check(run, action["action_id"]) == []


def test_an_active_hold_on_its_lot_blocks_it_with_its_reason(run):
    action = prepare_story(run)
    with db.tx() as conn:
        row = cases.case_row(conn, run["run_id"], STORY)
    add_hold(run, row["lot_id"], row["source_branch"], "Mixed dates found on the shelf")
    assert check(run, action["action_id"]) == [{
        "kind": "lot_hold", "id": "h-1", "lot_ids": [row["lot_id"]], "owner_branch": row["source_branch"],
        "reason": "Mixed dates found on the shelf", "resolution": execution.HOLD_RESOLUTION}]


# --- every block kind (Task 2) ---------------------------------------------------------------------------------------

def story_row(run) -> dict:
    with db.tx() as conn:
        return cases.case_row(conn, run["run_id"], STORY)


def scratch(run, sql: str, **params) -> None:
    """A test-only write in the test's own workspace."""
    with db.tx() as conn:
        db.run(conn, sql, r=run["run_id"], **params)


def add_flag(run, flag_id: str = "f-1", fact_id: str = "fact-1", cleared: bool = False) -> None:
    scratch(run, "INSERT INTO review_flags (run_id, flag_id, case_id, fact_id, reason, cleared_at, cleared_by) VALUES "
                 "(:r, :f, :c, :x, 'A count changed the stock behind this plan', "
                 "CASE WHEN :done THEN now() END, CASE WHEN :done THEN 'S01' END)",
            f=flag_id, c=STORY, x=fact_id, done=cleared)


def set_action(run, action_id: str, state: str) -> None:
    scratch(run, "UPDATE world_actions SET state = :s WHERE run_id = :r AND world = 'shelfshift' AND action_id = :a",
            s=state, a=action_id)


def test_an_uncleared_review_flag_blocks_and_a_cleared_one_does_not(run):
    action, row = prepare_story(run), story_row(run)
    add_flag(run, "f-old", "fact-old", cleared=True)
    assert check(run, action["action_id"]) == []
    add_flag(run, "f-1", "fact-1")
    assert check(run, action["action_id"]) == [{
        "kind": "review_flag", "id": "f-1", "lot_ids": [row["lot_id"]], "owner_branch": row["source_branch"],
        "reason": "New confirmed information · review required", "resolution": "Review before dispatch"}]


def test_a_case_not_prepared_or_a_work_order_not_planned_is_wrong_state(run):
    action = prepare_story(run)
    set_action(run, action["action_id"], "DISPATCHED")
    blocks = check(run, action["action_id"])
    assert [(b["kind"], b["id"], b["reason"]) for b in blocks] == [
        ("wrong_state", action["action_id"], "Work order is DISPATCHED")]
    set_action(run, action["action_id"], "PLANNED")
    scratch(run, "UPDATE cases SET state = 'EXECUTED' WHERE run_id = :r AND case_id = :c", c=STORY)
    blocks = check(run, action["action_id"])
    assert [(b["kind"], b["reason"]) for b in blocks] == [("wrong_state", "Case is EXECUTED")]
    assert all(b["resolution"] == execution.WRONG_STATE_RESOLUTION for b in blocks)
    assert [b["kind"] for b in check(run, "no-such-action")] == ["wrong_state", "wrong_state"]


def test_cancelled_work_is_refused_with_its_own_reason(run):
    action, row = prepare_story(run), story_row(run)
    set_action(run, action["action_id"], "CANCELLED")
    expected = [{"kind": "cancelled", "id": action["action_id"], "lot_ids": [action["lot_id"]],
                 "owner_branch": row["source_branch"],
                 "reason": "This work order was cancelled. Nothing can be dispatched.",
                 "resolution": execution.CANCELLED_RESOLUTION}]
    assert check(run, action["action_id"]) == expected
    scratch(run, "UPDATE cases SET state = 'CLOSED', closed_as = 'CANCELLED' WHERE run_id = :r AND case_id = :c",
            c=STORY)  # 07-11's cancel: the case CLOSED as CANCELLED, never also "Case is CLOSED"
    assert check(run, action["action_id"]) == expected


def test_a_failed_recheck_carries_the_engines_reasons_word_for_word(run):
    action, row = prepare_story(run), story_row(run)
    own_series(run, row["source_branch"], row["sku"])  # the test writes the source series directly
    scratch(run, "UPDATE lots SET qty = :q WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l",
            q=float(action["qty"]) - 1, l=row["lot_id"])
    with db.tx() as conn:
        r = fresh(run["run_id"])
        case = cases.case_row(conn, r["run_id"], STORY)
        plan = cases.parse_plan(cases.active_rec(conn, case)["result"]["recommended"])
        reasons = list(check_plan(cases.current_input(conn, r, case), plan))
    assert reasons, "lowering the source record below the work order must fail the engine's recheck"
    blocks = check(run, action["action_id"])
    assert [b["reason"] for b in blocks] == reasons
    assert {(b["kind"], b["id"], b["owner_branch"], b["resolution"]) for b in blocks} == {
        ("recheck", None, row["source_branch"], execution.RECHECK_RESOLUTION)}


def test_holds_on_every_involved_lot_block_and_others_do_not(run):
    action, row = prepare_story(run), story_row(run)
    dest = cases.parse_plan(  # the plan's destination: where a lot moved from the case lot would be
        db.in_tx(lambda conn: cases.active_rec(conn, row))["result"]["recommended"]).dest
    own_series(run, row["source_branch"], row["sku"])  # the case lot's row is then the workspace's own
    own_series(run, dest, row["sku"])
    scratch(run, "INSERT INTO lots (run_id, world, lot_id, branch, sku, qty, expiry_day, received_seq, expiry_source, "
                 "moved_from) SELECT run_id, world, 'TEST-MOVED', :b, sku, 1, expiry_day, received_seq, expiry_source, "
                 "lot_id FROM lots WHERE run_id = :r AND world = 'shelfshift' AND lot_id = :l", b=dest, l=row["lot_id"])
    with db.tx() as conn:
        assert execution.involved_lots(conn, fresh(run["run_id"]), row) == sorted([row["lot_id"], "TEST-MOVED"])
    add_hold(run, "TEST-MOVED", dest, "Damage reported on the moved lot", hold_id="h-moved")
    add_hold(run, row["lot_id"], row["source_branch"], "Released earlier", hold_id="h-old", state="RELEASED")
    add_hold(run, "SOME-OTHER-LOT", row["source_branch"], "Not this case", hold_id="h-other")
    held = [b for b in check(run, action["action_id"]) if b["kind"] == "lot_hold"]  # the scratch lot moves the recheck
    assert [(b["id"], b["lot_ids"], b["owner_branch"]) for b in held] == [("h-moved", ["TEST-MOVED"], dest)]


def test_the_lot_a_case_lot_was_moved_from_is_involved(run):
    row = story_row(run)
    own_series(run, row["source_branch"], row["sku"])
    scratch(run, "UPDATE lots SET moved_from = 'TEST-ORIGIN' WHERE run_id = :r AND world = 'shelfshift' "
                 "AND lot_id = :l", l=row["lot_id"])
    with db.tx() as conn:
        assert execution.involved_lots(conn, fresh(run["run_id"]), row) == sorted([row["lot_id"], "TEST-ORIGIN"])


def test_missing_support_is_the_only_block_and_never_clear(run, monkeypatch):
    action, row = prepare_story(run), story_row(run)
    real = execution._regclass
    for missing in execution.SUPPORT:
        monkeypatch.setattr(execution, "_regclass",
                            lambda conn, name, m=missing: None if name == m else real(conn, name))
        assert check(run, action["action_id"]) == [{
            "kind": "checks_unavailable", "id": None, "lot_ids": [row["lot_id"]], "owner_branch": None,
            "reason": "Dispatch checks unavailable", "resolution": "Dispatch stays blocked until the checks can run."}]
    monkeypatch.setattr(execution, "_regclass", real)
    assert check(run, action["action_id"]) == []  # the real tables exist: the same work order is clear


def after(run) -> list[dict]:
    def go(conn):
        r = db.lock_run(conn, run["run_id"])
        return execution.after_dispatch(conn, r, cases.case_row(conn, r["run_id"], STORY))
    return db.in_tx(go)


def test_a_flag_after_dispatch_is_an_operational_exception_that_blocks_nothing(run):
    action = prepare_story(run)
    add_flag(run, "f-1", "fact-1")
    assert after(run) == []  # before dispatch the flag blocks instead
    assert [b["kind"] for b in check(run, action["action_id"])] == ["review_flag"]
    for moved in ("DISPATCHED", "RECEIVED"):
        set_action(run, action["action_id"], moved)
        assert after(run) == [{"flag_id": "f-1", "fact_id": "fact-1",
                               "reason": "A count changed the stock behind this plan",
                               "label_key": "operational_exception"}]
        assert "review_flag" not in [b["kind"] for b in check(run, action["action_id"])]  # nothing moved is blocked
    add_flag(run, "f-2", "fact-2", cleared=True)
    assert [f["flag_id"] for f in after(run)] == ["f-1"]


# --- Phase 7's DDL, character for character (Pitfall 5) -------------------------------------------------------------
# Copied from 07-08-PLAN.md step 2 (lot_holds) and 07-06-PLAN.md step 2 (review_flags), as 04-API-CONTRACT "Tables and
# error codes" quotes them. Phase 7's CREATE ... IF NOT EXISTS is a no-op only on an identical table, so a drift
# between the phases fails here, not in production.
PHASE7_DDL = (
    "CREATE TABLE IF NOT EXISTS lot_holds (run_id text, hold_id text, lot_id text NOT NULL, branch text NOT NULL, "
    "kind text NOT NULL, reason text NOT NULL, placed_by text NOT NULL, placed_at timestamptz NOT NULL DEFAULT now(), "
    "signal_id text, request_id text, state text NOT NULL, resolved_by text, resolved_at timestamptz, resolution text, "
    "note text, PRIMARY KEY (run_id, hold_id));",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_active_hold_per_lot_kind ON lot_holds (run_id, lot_id, kind) "
    "WHERE state = 'ACTIVE';",
    "CREATE TABLE IF NOT EXISTS review_flags (run_id text, flag_id text, case_id text NOT NULL, fact_id text NOT NULL, "
    "reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), cleared_at timestamptz, cleared_by text, "
    "note text, PRIMARY KEY (run_id, flag_id));",
    "CREATE UNIQUE INDEX IF NOT EXISTS one_flag_per_case_fact ON review_flags (run_id, case_id, fact_id);",
)
TYPES = {"text": "text", "timestamptz": "timestamp with time zone"}
TABLE = re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+) \((.*), PRIMARY KEY \(([^)]*)\)\);$")
INDEX = re.compile(r"^CREATE UNIQUE INDEX IF NOT EXISTS (\w+) ON (\w+) \(([^)]*)\)(?: WHERE (\w+) = '(\w+)')?;$")


def expected_tables() -> dict:
    """{table: ([(column, type, nullable, default)], [primary key columns])} read from the DDL text above."""
    out = {}
    for ddl in PHASE7_DDL:
        m = TABLE.match(ddl)
        if not m:
            continue
        cols, pk = [], m.group(3).split(", ")
        for part in m.group(2).split(", "):
            name, typ, *rest = part.split(" ")
            tail = " ".join(rest)  # a primary key column is NOT NULL without saying so
            cols.append((name, TYPES[typ], "NOT NULL" not in tail and name not in pk,
                         tail.split("DEFAULT ", 1)[1] if "DEFAULT " in tail else None))
        out[m.group(1)] = (cols, pk)
    return out


def test_ddl_matches_phase7(schema):
    text = db.SCHEMA.read_text(encoding="utf-8")
    assert all(ddl in text for ddl in PHASE7_DDL), "api/schema.sql no longer holds Phase 7's DDL word for word"
    with db.tx() as conn:
        here = db.one(conn, "SELECT current_schema() AS s")["s"]
        for table, (cols, pk) in expected_tables().items():
            got = [(c["column_name"], c["data_type"], c["is_nullable"] == "YES", c["column_default"])
                   for c in db.rows(conn, "SELECT column_name, data_type, is_nullable, column_default "
                                          "FROM information_schema.columns WHERE table_schema = :s "
                                          "AND table_name = :t ORDER BY ordinal_position", s=here, t=table)]
            assert got == cols, table
            keys = [k["column_name"] for k in db.rows(
                conn, "SELECT k.column_name FROM information_schema.table_constraints c "
                      "JOIN information_schema.key_column_usage k ON k.constraint_schema = c.constraint_schema "
                      "AND k.constraint_name = c.constraint_name AND k.table_name = c.table_name "
                      "WHERE c.table_schema = :s AND c.table_name = :t "
                      "AND c.constraint_type = 'PRIMARY KEY' ORDER BY k.ordinal_position", s=here, t=table)]
            assert keys == pk, table
        indexes = 0
        for ddl in PHASE7_DDL:
            m = INDEX.match(ddl)
            if not m:
                continue
            name, table, cols, col, value = m.groups()
            where = f" WHERE ({col} = '{value}'::text)" if col else ""
            row = db.one(conn, "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND indexname = :i",
                         s=here, i=name)
            assert row is not None and row["indexdef"] == (
                f"CREATE UNIQUE INDEX {name} ON {here}.{table} USING btree ({cols}){where}"), name
            indexes += 1
    assert indexes == 2 and len(expected_tables()) == 2
