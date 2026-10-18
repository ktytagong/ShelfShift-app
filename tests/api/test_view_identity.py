"""Byte identity of the shell's counts, Today, the board, the schedule and the case detail against the BASE oracle
(quick 261017-tuf; tests/api/oracle_views.py holds the frozen BASE copies of the changed view functions).

For HQ and every store of the reference data, in one transaction per account with the run row locked as a read route
locks it (FOR SHARE) and rolled back afterwards, every response is computed twice: first under `oracle()` (the BASE
path), then on the current code. Each is rendered as the route renders it (`JSONResponse(jsonable_encoder(x)).body`)
and the bytes must be equal. Hold expiry runs in the first computation and expires nothing new in the second (now()
is constant inside one transaction), so both read the same records.

The state matrix is written straight into the test's own workspace: a documented test shortcut, as
test_planning_views.py and test_board_steps.py write states, so every state a card can reach (owned and unowned,
holds live and expired, a side already decided, work orders, an open discrepancy, stock checks addressed to another
store) is covered without waiting on the routes that record them.

The statement budgets pin the set-based reads: HQ's menu counts send a fixed number of statements however many cases
and flags the workspace holds, and a store never sends more than BASE did.
"""
from __future__ import annotations

import contextlib
import math
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api import cases, db, planning, refdata, sandbox, views
from engine.model import TRANSFER
from tests.api.conftest import fresh
from tests.api.oracle_views import oracle
from tests.api.test_planning_views import (dispatched_and_short, no_worker, prepare_story,  # noqa: F401
                                           public_flags, scratch)

STORY = cases.STORY_KEY
HQ_NAV_BUDGET = 8  # statements for HQ's menu counts, whatever the number of cases and flags
MATRIX = ("open_unowned", "open_source", "open_hq", "open_hold_expired", "open_no_version", "waiting", "invalidated",
          "pa_live_hold", "pa_expired_hold", "pa_destination_decided", "approved", "prepared_direct", "executed_hq",
          "observed", "closed", "rejected", "missing_data")
TRANSFER_FIRST = ("pa_live_hold", "pa_expired_hold", "pa_destination_decided", "approved", "prepared_direct")


# --- the state matrix (test shortcut) ---------------------------------------------------------------------------------

def _recommended(run_id: str) -> dict[str, str | None]:
    """{case_id: the active version's recommended key, or None} of the workspace's OPEN cases but SUS-19."""
    with db.tx() as conn:
        return {r["case_id"]: r["rec"] for r in db.rows(
            conn, "SELECT c.case_id, r.result->>'recommended' AS rec FROM cases c LEFT JOIN recommendations r "
                  "ON r.run_id = c.run_id AND r.case_id = c.case_id AND r.rec_version = c.active_rec_version "
                  "WHERE c.run_id = :r AND c.state = 'OPEN' AND c.case_id <> :s ORDER BY c.case_id",
            r=run_id, s=STORY)}


def _hold(run_id: str, case: dict, minutes: int) -> None:
    scratch(run_id, "INSERT INTO reservations (run_id, res_id, case_id, round, kind, bucket, day, qty, state, "
                    "expires_at) VALUES (:r, :i, :c, :n, 'DEMAND', 'test', :d, 1, 'HELD', :x)",
            i=f"{case['case_id']}-matrix-hold", c=case["case_id"], n=case["round"], d=fresh(run_id)["day"],
            x=datetime.now(timezone.utc) + timedelta(minutes=minutes))


def _request(run_id: str, case: dict, to_branch: str, state: str) -> None:
    day = fresh(run_id)["day"]
    scratch(run_id, "INSERT INTO evidence_requests (run_id, request_id, case_id, rec_version, fact_id, fact_kind, sku, "
                    "lot_id, to_branch, ask_text, due_day, due_min, state, created_by, created_day) VALUES (:r, :q, :c, "
                    ":v, :f, 'STALE_COUNT', :s, :l, :b, 'Count the lot', :d, 540, :st, 'manager', :d)",
            q=f"Q-matrix-{state.lower()}", c=case["case_id"], v=case["active_rec_version"], f=f"F-matrix-{state}",
            s=case["sku"], l=case["lot_id"], b=to_branch, d=day, st=state)


def _copy_cases(run_id: str, n: int) -> None:
    """Add n OPEN cases to the workspace (test shortcut): copies of its cases with an active version (a transfer pick
    first, cycling over the transfers when there are any), each with its active recommendation row, under a new case
    id and key and a lot id no run holds (so its lot reads as gone and the card falls back to the seed row, a path of
    its own), reset to OPEN with no owner. Column names come from the database (sandbox.columns), never from a
    request."""
    with db.tx() as conn:
        rec = {r["case_id"]: r["rec"] for r in db.rows(
            conn, "SELECT c.case_id, r.result->>'recommended' AS rec FROM cases c JOIN recommendations r "
                  "ON r.run_id = c.run_id AND r.case_id = c.case_id AND r.rec_version = c.active_rec_version "
                  "WHERE c.run_id = :r ORDER BY c.case_id", r=run_id)}
        moving = [c for c in rec if cases.parse_plan(rec[c]).kind == TRANSFER]
        sources = moving or list(rec)
        assert sources, "the workspace has an evaluated case to copy"
        case_cols = [c for c in sandbox.columns(conn, "cases") if c not in ("run_id", "case_id", "key", "lot_id")]
        rec_cols = [c for c in sandbox.columns(conn, "recommendations") if c not in ("run_id", "case_id")]
        for i in range(n):
            src, new = sources[i % len(sources)], f"MX-{i + 1:02d}"
            db.run(conn, f"INSERT INTO cases (run_id, case_id, key, lot_id, {', '.join(case_cols)}) SELECT run_id, "
                         f":n, :n, lot_id || '-mx{i + 1}', {', '.join(case_cols)} FROM cases WHERE run_id = :r "
                         f"AND case_id = :c", r=run_id, n=new, c=src)
            db.run(conn, f"INSERT INTO recommendations (run_id, case_id, {', '.join(rec_cols)}) SELECT run_id, :n, "
                         f"{', '.join(rec_cols)} FROM recommendations r WHERE run_id = :r AND case_id = :c AND "
                         f"rec_version = (SELECT active_rec_version FROM cases WHERE run_id = :r AND case_id = :c)",
                   r=run_id, n=new, c=src)
        db.run(conn, "UPDATE cases SET state = 'OPEN', owner_taken_by = NULL, owner_taken_at = NULL, "
                     "hold_expired = false, missing_data = NULL, reason = NULL, waiting_until_day = NULL, tag = NULL, "
                     "closed_as = NULL, closed_at = NULL, closed_by = NULL WHERE run_id = :r AND case_id LIKE 'MX-%'",
               r=run_id)


def matrix(run_id: str) -> dict[str, str]:
    """Write the state matrix into the workspace's OPEN cases (SUS-19 left as it is), adding copies when the
    workspace has fewer OPEN cases than labels: {label: case_id} for every label (a transfer pick first where the
    label needs a destination)."""
    rec = _recommended(run_id)
    moving = sum(1 for k in rec.values() if k and cases.parse_plan(k).kind == TRANSFER)
    short = max(len(MATRIX) - len(rec), len(TRANSFER_FIRST) - moving)
    if short > 0:
        _copy_cases(run_id, short)
    rec = _recommended(run_id)
    ref = refdata.get(fresh(run_id)["dataset_version"])
    transfers = [c for c, k in rec.items() if k and cases.parse_plan(k).kind == TRANSFER]
    free = list(rec)
    chosen: dict[str, str] = {}
    for label in MATRIX:
        if label in TRANSFER_FIRST:  # a transfer pick; the other labels leave the transfers to these
            pool = [c for c in transfers if c in free]
        else:
            pool = [c for c in free if c not in transfers and (rec[c] or label == "open_no_version")]
        pool = pool or [c for c in free if rec[c]] or free
        if not pool:
            break
        chosen[label] = pool[0]
        free.remove(pool[0])
    row = {c["case_id"]: c for c in [*_rows(run_id)]}
    for label, cid in chosen.items():
        case = row[cid]
        src = case["source_branch"]
        own = {"o": src, "c": cid}
        if label == "open_source":
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o WHERE run_id = :r AND case_id = :c", **own)
            other = next(b for b in ref.branches if b != src)
            _request(run_id, case, other, "OPEN")
            _request(run_id, case, other, "ANSWERED")
        elif label == "open_hq":
            scratch(run_id, "UPDATE cases SET owner_taken_by = 'HQ' WHERE run_id = :r AND case_id = :c", c=cid)
        elif label == "open_hold_expired":
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o, hold_expired = true WHERE run_id = :r "
                            "AND case_id = :c", **own)
        elif label == "open_no_version":
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o, active_rec_version = NULL WHERE run_id = :r "
                            "AND case_id = :c", **own)
        elif label in ("waiting", "invalidated", "approved", "observed"):
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o, state = :s, waiting_until_day = :w, reason = :why "
                            "WHERE run_id = :r AND case_id = :c", s=label.upper(),
                    w=fresh(run_id)["day"] + 1 if label == "waiting" else None,
                    why="matrix" if label in ("waiting", "invalidated") else None, **own)
        elif label in ("pa_live_hold", "pa_expired_hold", "pa_destination_decided"):
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o, state = 'PENDING_APPROVAL' WHERE run_id = :r "
                            "AND case_id = :c", **own)
            if label == "pa_live_hold":
                _hold(run_id, case, 7)  # minutes_left <= 10: the "Held until" badge
            elif label == "pa_expired_hold":
                _hold(run_id, case, -1)  # the read expires it: OPEN with hold_expired
            else:
                scratch(run_id, "INSERT INTO decisions (run_id, case_id, rec_version, round, role, decision) VALUES "
                                "(:r, :c, :v, :n, 'destination', 'accept')", c=cid, v=case["active_rec_version"],
                        n=case["round"])
        elif label == "prepared_direct":
            plan = cases.parse_plan(rec[cid])
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o, state = 'PREPARED' WHERE run_id = :r "
                            "AND case_id = :c", **own)
            moves = plan.kind == TRANSFER
            scratch(run_id, "INSERT INTO world_actions (run_id, world, action_id, case_id, kind, lot_id, sku, qty, dest, "
                            "run_code, run_day, origin, state) VALUES (:r, 'shelfshift', :a, :c, :k, :l, :s, 5, :d, "
                            ":rc, :rd, :o, 'PLANNED')",
                    a=f"{cid}#{case['round']}:matrix", c=cid, k=TRANSFER if moves else "MARKDOWN",
                    l=case["lot_id"], s=case["sku"], d=plan.dest if moves else None,
                    rc=plan.run_code if moves else None, rd=fresh(run_id)["day"] if moves else None,
                    o=src if moves else None)
        elif label == "executed_hq":
            scratch(run_id, "UPDATE cases SET owner_taken_by = 'HQ', state = 'EXECUTED' WHERE run_id = :r "
                            "AND case_id = :c", c=cid)
        elif label in ("closed", "rejected"):
            scratch(run_id, "UPDATE cases SET owner_taken_by = :o, state = :s, reason = 'matrix', closed_as = :a "
                            "WHERE run_id = :r AND case_id = :c", s=label.upper(),
                    a="COMPLETED" if label == "closed" else None, **own)
        elif label == "missing_data":
            scratch(run_id, "UPDATE cases SET missing_data = 'missing data: matrix' WHERE run_id = :r "
                            "AND case_id = :c", c=cid)
    return chosen


def _rows(run_id: str) -> list[dict]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT * FROM cases WHERE run_id = :r ORDER BY case_id", r=run_id)


def coverage(run_id: str) -> set[str]:
    """What the workspace holds, read back from the database (not from the matrix's own bookkeeping)."""
    with db.tx() as conn:
        q = lambda sql: db.rows(conn, sql, r=run_id)  # noqa: E731
        have = set()
        for c in q("SELECT * FROM cases WHERE run_id = :r"):
            s, o = c["state"], c["owner_taken_by"]
            if s == "OPEN":
                have |= {"open_unowned"} if o is None and not c["missing_data"] else set()
                have |= {"open_source"} if o == c["source_branch"] else set()
                have |= {"open_hq"} if o == "HQ" else set()
                have |= {"open_hold_expired"} if o and c["hold_expired"] else set()
                have |= {"open_no_version"} if o and not c["active_rec_version"] else set()
            have |= {s.lower()} if s in ("WAITING", "INVALIDATED", "APPROVED", "OBSERVED", "CLOSED", "REJECTED") \
                else set()
            have |= {"executed_hq"} if s == "EXECUTED" and o == "HQ" else set()
            have |= {"missing_data"} if c["missing_data"] else set()
        holds = q("SELECT r.expires_at > now() AS live FROM reservations r JOIN cases c USING (run_id, case_id) "
                  "WHERE r.run_id = :r AND r.state = 'HELD' AND c.state = 'PENDING_APPROVAL'")
        have |= {"pa_live_hold"} if any(h["live"] for h in holds) else set()
        have |= {"pa_expired_hold"} if any(not h["live"] for h in holds) else set()
        if q("SELECT 1 FROM decisions d JOIN cases c USING (run_id, case_id) WHERE d.run_id = :r AND d.role = "
             "'destination' AND c.state = 'PENDING_APPROVAL' AND d.round = c.round"):
            have.add("pa_destination_decided")
        if q("SELECT 1 FROM world_actions a JOIN cases c USING (run_id, case_id) WHERE a.run_id = :r "
             "AND a.kind = 'TRANSFER' AND a.state = 'PLANNED' AND c.state = 'PREPARED'"):
            have.add("prepared_direct")
        if q("SELECT 1 FROM discrepancies d JOIN cases c USING (run_id, case_id) WHERE d.run_id = :r "
             "AND d.state = 'OPEN' AND c.state = 'EXECUTED'"):
            have.add("executed_short")
        for st in ("OPEN", "ANSWERED"):
            if q(f"SELECT 1 FROM evidence_requests q JOIN cases c USING (run_id, case_id) WHERE q.run_id = :r "
                 f"AND q.state = '{st}' AND q.to_branch <> c.source_branch"):
                have.add(f"request_{st.lower()}_elsewhere")
    return have


# --- computing the responses ------------------------------------------------------------------------------------------

class _Rollback(Exception):
    pass


def rolled_back(fn):
    """fn(conn) in one transaction that is rolled back afterwards: its result."""
    out = {}
    try:
        with db.tx() as conn:
            out["v"] = fn(conn)
            raise _Rollback
    except _Rollback:
        pass
    return out["v"]


def seat(account: str) -> dict:
    hq = account == "HQ"
    return {"account": account, "role": "hq" if hq else "manager", "branch": None if hq else account,
            "session_id": "t"}


def body(value) -> bytes:
    """The bytes a route sends for this value."""
    return JSONResponse(jsonable_encoder(value)).body


def responses(conn, run: dict, account: str, details: list[str] | None = None) -> dict[str, bytes]:
    """The account's session, Today (every page of HQ's network form), cases page 1 (the route's composition), the
    schedule (default; week grouped by run) and, when given, these cases' details, rendered as the routes render
    them."""
    actor = seat(account)
    session = {**actor, "run_id": run["run_id"], "last_reset_ms": None}
    out = {"session": views.session_info(conn, session, run)}
    first = planning.today(conn, run, actor)
    out["today:1"] = first
    if account == "HQ":
        table = first["branches"]
        for n in range(2, math.ceil(table["total"] / table["page_size"]) + 1 if table["total"] else 0):
            out[f"today:{n}"] = planning.today(conn, run, actor, None, n, None)
    out["cases:1"] = {"day": run["day"], "version": run["version"], **cases.board_page(
        conn, run, actor, branch=None, risk=None, page=1, page_size=None, owner=None, product=None, deadline=None,
        blocked=None)}
    out["schedule"] = planning.schedule(conn, run, actor)
    out["schedule:week:run"] = planning.schedule(conn, run, actor, None, "week", "run")
    for cid in details or []:
        out[f"detail:{cid}"] = cases.detail(conn, run, cid)
    return {k: body(v) for k, v in out.items()}


def compare(run_id: str, accounts: list[str], details: list[str]) -> dict[str, dict]:
    """For each account, BASE (oracle) against the current code in one rolled-back transaction: asserts equal bytes
    and returns {account: the parsed session nav_counts and cases page}."""
    seen = {}
    for account in accounts:
        def both(conn):
            run = db.lock_run(conn, run_id)  # FOR SHARE, as a read route locks it
            with oracle():
                old = responses(conn, run, account, details if account == "HQ" else None)
            new = responses(conn, run, account, details if account == "HQ" else None)
            return old, new
        old, new = rolled_back(both)
        assert list(old) == list(new), account
        for name in old:
            assert old[name] == new[name], f"{account} {name}: the response bytes differ from BASE"
        seen[account] = {"session": jsonable_encoder(_json(new["session"])),
                         "cases": _json(new["cases:1"]), "today": _json(new["today:1"])}
    return seen


def _json(raw: bytes):
    import json
    return json.loads(raw)


def workspace() -> str:
    """A new workspace cloned from the ACTIVE template: its run_id."""
    with db.tx() as conn:
        return sandbox.clone(conn, sandbox.active_template(conn))


# --- statement counting -----------------------------------------------------------------------------------------------

@contextlib.contextmanager
def statements():
    """The statements sent to the database inside the block."""
    sent: list[str] = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        sent.append(statement)
    sa.event.listen(db.engine(), "before_cursor_execute", hook)
    try:
        yield sent
    finally:
        sa.event.remove(db.engine(), "before_cursor_execute", hook)


def nav_statements(run_id: str, account: str, base: bool) -> int:
    """Statements of one warm nav_counts call for the account (BASE's under the oracle)."""
    actor = seat(account)
    session = {**actor, "run_id": run_id, "last_reset_ms": None}

    def go(conn):
        run = db.lock_run(conn, run_id)
        ctx = oracle() if base else contextlib.nullcontext()
        with ctx:
            planning.nav_counts(conn, run, session)  # warm
            with statements() as sent:
                planning.nav_counts(conn, run, session)
        return len(sent)
    return rolled_back(go)


# --- the tests --------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic(template):
    """Two synthetic workspaces carrying the state matrix: SUS-19 prepared (a PLANNED transfer), and SUS-19
    dispatched and received short (EXECUTED with an OPEN discrepancy). {name: run_id}."""
    out = {}
    for name in ("prepared", "dispatched_short"):
        run_id = workspace()
        action = prepare_story(run_id)
        if name == "dispatched_short":
            dispatched_and_short(run_id, action, 2)
        matrix(run_id)
        out[name] = run_id
    return out


@pytest.fixture(scope="module")
def public_ws(public_flags):
    """A tiny public workspace (cloned from `public_flags`, which flags more lots) with the matrix items that fit."""
    run_id = workspace()
    assert fresh(run_id)["template_run_id"] == public_flags
    matrix(run_id)
    return run_id


def _accounts(run_id: str) -> list[str]:
    return ["HQ", *refdata.get(fresh(run_id)["dataset_version"]).branches]


def _case_ids(run_id: str) -> list[str]:
    return [c["case_id"] for c in _rows(run_id)]


@pytest.mark.parametrize("name", ["prepared", "dispatched_short"])
def test_every_account_answers_byte_identical_to_base(synthetic, name, no_worker):
    run_id = synthetic[name]
    have = coverage(run_id)
    need = set(MATRIX) | {"request_open_elsewhere", "request_answered_elsewhere"}
    need |= {"executed_short"} if name == "dispatched_short" else {"prepared_direct"}
    assert need <= have, f"the matrix lacks {sorted(need - have)}"
    if name == "prepared":
        with db.tx() as conn:
            assert db.one(conn, "SELECT 1 AS x FROM world_actions WHERE run_id = :r AND case_id = :c AND "
                                "kind = 'TRANSFER' AND state = 'PLANNED'", r=run_id, c=STORY), "SUS-19 is prepared"
    seen = compare(run_id, _accounts(run_id), _case_ids(run_id))
    nav = {a: s["session"]["nav_counts"] for a, s in seen.items()}
    assert nav["HQ"]["today"] > 0, nav  # not vacuous: HQ has cards
    assert any(n["today"] > 0 for a, n in nav.items() if a != "HQ"), nav
    assert nav["HQ"]["board"] > 0, nav


VIEW_BUDGET = 25  # statements for HQ's board page and Today after the batched card reads


def view_statements(run_id: str, account: str, which: str, base: bool) -> tuple[int, int]:
    """(statements of one warm board_page() or today() call for the account outside the dispatch check, statements
    inside it): BASE's under the oracle. The dispatch check (api/execution.py) runs once per PREPARED case with a
    PLANNED transfer, unchanged by this quick, so the budget counts the reads around it."""
    from api import execution
    actor = seat(account)
    call = {"board_page": lambda conn, run: cases.board_page(conn, run, actor),
            "today": lambda conn, run: planning.today(conn, run, actor)}[which]
    real, inside = execution.dispatch_check, []

    def checked(*args, **kwargs):
        inside.append(True)
        try:
            return real(*args, **kwargs)
        finally:
            inside.pop()

    def go(conn):
        run = db.lock_run(conn, run_id)
        with oracle() if base else contextlib.nullcontext(), pytest.MonkeyPatch.context() as mp:
            mp.setattr(execution, "dispatch_check", checked)
            call(conn, run)  # warm
            outside, within = [], []

            def hook(conn_, cursor, statement, parameters, context, executemany):
                (within if inside else outside).append(statement)
            sa.event.listen(db.engine(), "before_cursor_execute", hook)
            try:
                call(conn, run)
            finally:
                sa.event.remove(db.engine(), "before_cursor_execute", hook)
        return len(outside), len(within)
    return rolled_back(go)


def test_board_and_today_are_batched(synthetic, no_worker):
    run_id = synthetic["prepared"]
    n_cases = len(_case_ids(run_id))
    for which in ("board_page", "today"):
        (new, checks), (old, old_checks) = (view_statements(run_id, "HQ", which, False),
                                            view_statements(run_id, "HQ", which, True))
        print(f"HQ {which}: {new} statements (+{checks} in dispatch checks), BASE {old} (+{old_checks}), "
              f"over {n_cases} cases")
        assert 0 < checks <= old_checks, "the same dispatch checks (BASE's Today ran them twice)"
        assert new <= VIEW_BUDGET, f"HQ {which} sent {new} statements"
        assert old >= n_cases, f"BASE {which} sent {old} statements for {n_cases} cases"
        for store in refdata.get(fresh(run_id)["dataset_version"]).branches:
            s_new, s_old = (sum(view_statements(run_id, store, which, False)),
                            sum(view_statements(run_id, store, which, True)))
            assert s_new <= s_old, f"{store} {which}: {s_new} statements, BASE {s_old}"


def test_public_accounts_answer_byte_identical_to_base(public_ws, no_worker):
    ref = refdata.get(fresh(public_ws)["dataset_version"])
    assert ref.kind == "public"
    have = coverage(public_ws)
    assert {"open_unowned", "open_source", "request_open_elsewhere"} <= have, have
    seen = compare(public_ws, _accounts(public_ws), _case_ids(public_ws))
    board = seen["HQ"]["cases"]
    assert board["total"] - board["evaluated"] > 0, "flags this workspace has not opened"
    assert board["at_risk"] > board["evaluated"] > 0
    assert seen["HQ"]["session"]["nav_counts"]["board"] > 0


def test_hq_nav_counts_is_set_based(synthetic, public_ws, no_worker):
    for run_id in (synthetic["prepared"], public_ws):
        hq = nav_statements(run_id, "HQ", base=False)
        print(f"HQ nav_counts statements: {hq} (BASE {nav_statements(run_id, 'HQ', base=True)}) on {run_id}")
        assert hq <= HQ_NAV_BUDGET, f"HQ's menu counts sent {hq} statements"
        for store in refdata.get(fresh(run_id)["dataset_version"]).branches:
            new, old = nav_statements(run_id, store, base=False), nav_statements(run_id, store, base=True)
            assert new <= old, f"{store}: {new} statements, BASE {old}"
