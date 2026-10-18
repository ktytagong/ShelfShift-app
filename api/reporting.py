"""Reports: read-only, store-bound views of one workspace and the published benchmark (08-CONTEXT
D-01, D-03, D-04, D-15, D-18; 04-API-CONTRACT 'Reports (08)').

No engine worker, physical stock or hidden demand. Activity uses one SQL statement snapshot,
including decisions and movement records. Managers always read their store, including incoming
and outgoing plans, independently of the Decision board's configurable default scope.
"""
from __future__ import annotations

from . import db, refdata
from .stock import _store

REPORTS = ("open", "pending_approval", "unanswered_check", "committed", "cancelled")


def report_clause(report: str, store: str | None) -> tuple[str, dict]:
    """The activity's contributing case predicate over c, with strict store involvement (D-18).

    An addressed OPEN request also contributes its case, as it does to the unanswered-check tile.
    Query text never becomes SQL: choose a fixed allow-listed clause and bind the store.
    """
    from .errors import ApiError

    if report not in REPORTS:
        raise ApiError(400, "bad_request", f"report is one of {', '.join(REPORTS)}")
    scope, params = case_scope(store)
    if report == "unanswered_check":
        addressed = " OR q.to_branch = :report_store" if store is not None else ""
        return ("EXISTS (SELECT 1 FROM evidence_requests q WHERE q.run_id = c.run_id AND q.case_id = c.case_id "
                f"AND q.state = 'OPEN' AND ({scope}{addressed}))"), params
    states = {"open": "c.state IN ('OPEN','WAITING','PENDING_APPROVAL')",
              "pending_approval": "c.state = 'PENDING_APPROVAL'",
              "committed": "c.state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')",
              "cancelled": "c.state = 'CLOSED' AND c.closed_as = 'CANCELLED'"}
    return f"({states[report]}) AND ({scope})", params


def report_ids(conn, run: dict, actor: dict, branch: str | None, report: str) -> tuple[set[str], str | None]:
    """Synthetic-board adapter: resolve Reports scope before returning contributing IDs (D-18)."""
    store = scope_store(refdata.get(run["dataset_version"]), actor, branch)
    clause, params = report_clause(report, store)
    return {c["case_id"] for c in db.rows(conn, f"SELECT c.case_id FROM cases c WHERE c.run_id = :r AND {clause}",
                                        r=run["run_id"], **params)}, store


def scope_store(ref, actor: dict, branch: str | None) -> str | None:
    """Reports' store boundary: the manager's own store, or HQ's validated choice (D-15)."""
    return _store(ref, actor, branch)


def case_scope(store: str | None) -> tuple[str, dict]:
    """Strict Reports involvement over alias c, never the board's configurable default (D-04)."""
    if store is None:
        return "true", {}
    return "(c.source_branch = :report_store OR c.plan_dest = :report_store)", {"report_store": store}


def workspace(conn, run: dict, actor: dict, branch: str | None = None) -> dict:
    """The five activity groups from one statement snapshot, with role-relative owners (D-04)."""
    from . import cases
    from .simplerule import parse_plan

    store = scope_store(refdata.get(run["dataset_version"]), actor, branch)
    where, params = case_scope(store)
    row = db.one(conn, f"""
        WITH scoped AS MATERIALIZED (
          SELECT c.* FROM cases c WHERE c.run_id = :r AND {where}
        ), activity AS (
          SELECT count(*) FILTER (WHERE state IN ('OPEN','WAITING','PENDING_APPROVAL')) AS open,
            count(*) FILTER (WHERE state = 'PENDING_APPROVAL') AS pending,
            count(*) FILTER (WHERE state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')) AS committed,
            count(*) FILTER (WHERE state = 'CLOSED' AND closed_as = 'CANCELLED') AS cancelled
          FROM scoped
        ), checks AS (
          SELECT count(*) AS n,
            count(*) FILTER (WHERE q.due_at IS NOT NULL AND q.due_at <= now()) AS past_due,
            count(*) FILTER (WHERE q.to_branch = :seat) AS addressed
          FROM evidence_requests q WHERE q.run_id = :r AND q.state = 'OPEN'
            AND (:network OR q.to_branch = :store OR EXISTS (
              SELECT 1 FROM scoped c WHERE c.case_id = q.case_id))
        ), transfers AS (
          SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM movements m WHERE m.run_id = c.run_id
            AND m.case_id = c.case_id AND m.kind = 'DISPATCH')) AS dispatched,
            count(*) FILTER (WHERE EXISTS (SELECT 1 FROM movements m WHERE m.run_id = c.run_id
            AND m.case_id = c.case_id AND m.kind = 'RECEIPT')) AS received
          FROM scoped c WHERE c.state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')
            AND c.plan_dest IS NOT NULL
        ), owners AS (
          SELECT coalesce(jsonb_agg(jsonb_build_object('state', c.state, 'source_branch', c.source_branch,
            'plan', r.result->>'recommended', 'decided', (SELECT coalesce(jsonb_object_agg(d.role, d.decision),
            '{{}}'::jsonb) FROM decisions d WHERE d.run_id = c.run_id AND d.case_id = c.case_id
            AND d.rec_version = c.active_rec_version AND d.round = c.round))), '[]'::jsonb) AS rows
          FROM scoped c LEFT JOIN recommendations r ON r.run_id = c.run_id AND r.case_id = c.case_id
            AND r.rec_version = c.active_rec_version WHERE c.state IN ('OPEN','WAITING','PENDING_APPROVAL')
        )
        SELECT activity.*, checks.n AS checks, checks.past_due, checks.addressed,
          transfers.dispatched, transfers.received, owners.rows AS owners
        FROM activity CROSS JOIN checks CROSS JOIN transfers CROSS JOIN owners
        """, r=run["run_id"], store=store, network=store is None,
                 seat=actor["branch"] if actor["role"] != "hq" else None, **params)
    need, waiting = 0, 0
    if actor["role"] != "hq":
        for case in row["owners"]:
            plan = parse_plan(case["plan"]) if case["plan"] else None
            yours = any(o["branch"] == actor["branch"] for o in cases.next_owner(case, plan, case["decided"]))
            need += yours
            waiting += yours and case["state"] == "PENDING_APPROVAL"
    return {"scope": {"branch": store, "role": actor["role"]}, "activity": {
        "open": {"count": row["open"], "need_action": need},
        "pending_approval": {"count": row["pending"], "waiting_for_you": waiting},
        "unanswered_check": {"count": row["checks"], "past_due": row["past_due"],
                             "addressed_to_you": row["addressed"]},
        "committed": {"count": row["committed"], "dispatched": row["dispatched"], "received": row["received"]},
        "cancelled": {"count": row["cancelled"]}}}


def benchmark(conn, run: dict, actor: dict, branch: str | None = None) -> dict:
    """Only published results, conservatively historical until 08-05 compares versions (D-03)."""
    scope_store(refdata.get(run["dataset_version"]), actor, branch)
    row = db.one(conn, "SELECT * FROM benchmark_runs WHERE status = 'PUBLISHED' "
                       "ORDER BY published_at DESC, benchmark_id DESC LIMIT 1")
    if row is None:
        return {"status": "none", "current": None, "historical": None}
    return {"status": "historical", "current": None, "historical": {
        "benchmark_id": row["benchmark_id"], "published_at": row["published_at"], "git_commit": row["git_commit"],
        "versions": {"engine": row["engine_version"], "dataset": row["dataset_version"],
                     "policy": row["policy_version"], "forecast_runs": row["forecast_runs"],
                     "evaluator": row["eval_version"], "settings": row["settings_digest"]}, "differs": []}}
