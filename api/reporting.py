"""Reports: read-only, store-bound views of one workspace and the published benchmark (08-CONTEXT
D-01, D-03, D-04, D-15, D-18; 04-API-CONTRACT 'Reports (08)').

No engine worker, physical stock or hidden demand. Activity uses one SQL statement snapshot,
including decisions and movement records. Managers always read their store, including incoming
and outgoing plans, independently of the Decision board's configurable default scope.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from . import benchmark as evaluation, db, inputs, refdata
from .errors import ApiError
from .stock import _store

REPORTS = ("open", "pending_approval", "unanswered_check", "committed", "cancelled")
FIGURES = ("net_change", "waste_change_units", "waste_change_kg", "transport_and_handling")

# Read the version approved by the source in THIS round, even if active_rec_version has moved.
# to_jsonb(c) carries Phase 7's additive selected field when integrated, without a schema fork.
COMMITTED = """
    SELECT to_jsonb(c) AS case_row, to_jsonb(r) AS rec, d.rec_version AS committed_version,
      a.payload->>'plan' AS committed_plan
    FROM cases c
    LEFT JOIN decisions d ON d.run_id = c.run_id AND d.case_id = c.case_id AND d.round = c.round
      AND d.role = 'source' AND d.decision = 'approve'
    LEFT JOIN recommendations r ON r.run_id = c.run_id AND r.case_id = c.case_id
      AND r.rec_version = d.rec_version
    LEFT JOIN LATERAL (SELECT payload FROM activity_log l WHERE l.run_id = c.run_id
      AND l.case_id = c.case_id AND l.type = 'committed' AND l.payload->>'round' = c.round::text
      ORDER BY l.seq DESC LIMIT 1) a ON true
    WHERE c.run_id = :r AND c.state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')
"""
RECORDED = """
    SELECT
      (SELECT coalesce(jsonb_agg(to_jsonb(m)), '[]'::jsonb) FROM movements m
       WHERE m.run_id = :r AND EXISTS (SELECT 1 FROM committed_data c
         WHERE c.case_row->>'case_id' = m.case_id)) AS movements,
      (SELECT coalesce(jsonb_agg(to_jsonb(d)), '[]'::jsonb) FROM discrepancies d
       WHERE d.run_id = :r AND d.state = 'OPEN' AND EXISTS (SELECT 1 FROM committed_data c
         WHERE c.case_row->>'case_id' = d.case_id)) AS discrepancies
"""


def footprint(engine_input: dict) -> tuple[str, frozenset[str]]:
    """SKU crossed with every projected branch, including lots beyond the selected destination."""
    branches = {l['branch'] for l in engine_input['lots']}
    if engine_input.get('source'):
        branches.add(engine_input['source'])
    for trip in engine_input['runs']:
        branches.update((trip['origin'], trip['dest']))
    return engine_input['product']['sku'], frozenset(branches)


def _plan_in_force(case: dict, rec: dict | None):
    """Phase 7's resolver, or its exact version-binding seam before integration (08-18)."""
    from .cases import parse_plan

    selected = case.get('selected')
    if selected and (not rec or selected.get('rec_version') != rec['rec_version'] or
                     selected.get('rec_version') != case.get('active_rec_version') or
                     selected.get('round', case['round']) != case['round']):
        return None, 'unbound_selection'
    try:
        from .selection import plan_in_force
    except ModuleNotFoundError as exc:
        if exc.name != __package__ + '.selection':
            raise
        key = selected['plan_key'] if selected else rec['result']['recommended'] if rec else None
    else:
        key, _, _ = plan_in_force(case, rec)
    try:
        return parse_plan(key) if key else None, None
    except (ValueError, TypeError):
        return None, 'plan_not_computed'


def _committed(conn, run: dict) -> list[dict]:
    return db.rows(conn, COMMITTED, r=run['run_id'])


def _bound_plan(item: dict):
    """Resolve against the approved version and check the immutable committed-round key."""
    case, rec = item['case_row'], item['rec']
    plan, reason = _plan_in_force({**case, 'active_rec_version': item['committed_version']}, rec)
    committed = item['committed_plan']
    if not reason and (not item['committed_version'] or not committed or not plan or plan.key != committed):
        reason = 'commitment_not_bound'
    if reason:
        from .cases import parse_plan
        try:
            plan = parse_plan(committed) if committed else None
        except (ValueError, TypeError):
            plan = None
    return plan, reason


def _cents(value: Decimal) -> float:
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) + 0.0


def expected(conn, run: dict, actor: dict, branch: str | None = None, page: int = 1,
             page_size: int = 20, *, snapshot: list[dict] | None = None) -> dict:
    """Stored committed projections only; totals span pages and refuse overlapping footprints."""
    store = scope_store(refdata.get(run['dataset_version']), actor, branch)
    rows, footprints = [], []
    for item in _committed(conn, run) if snapshot is None else snapshot:
        case, rec = item['case_row'], item['rec']
        plan, reason = _bound_plan(item)
        dest = plan.dest if plan else None
        if store is not None and store not in (case['source_branch'], dest):
            continue
        if plan and plan.kind == 'KEEP':
            continue
        inp = rec['engine_input'] if rec else None
        comparison = rec.get('simple_rule') if rec else None
        row = {k: case[k] for k in ('case_id', 'key', 'sku', 'source_branch')}
        row.update(plan_key=plan.key if plan else item['committed_plan'], plan_label=plan.label if plan else None,
                   plan_kind=plan.kind if plan else None, dest_branch=dest,
                   horizon={'first_day': inp['today'],
                            'last_day': max((f['day'] for f in inp['forecasts']), default=None)} if inp else None,
                   computed=False, **dict.fromkeys(FIGURES))
        if not reason and (not comparison or comparison.get('status') != 'ok'):
            reason = (comparison or {}).get('reason') or 'comparison_not_computed'
        if not reason:
            columns = comparison.get('columns', [])
            chosen = next((c for c in columns if c.get('key') == plan.key), None)
            keep = next((c for c in columns if c.get('column') == 'keep' and c.get('key') == 'keep'), None)
            try:
                if chosen is None or keep is None:
                    raise ValueError('no stored plan column')
                amounts = [Decimal(str(chosen['delta_vs_keep'])),
                           Decimal(str(chosen['wasted_units'])) - Decimal(str(keep['wasted_units'])),
                           Decimal(str(chosen['wasted_kg'])) - Decimal(str(keep['wasted_kg'])),
                           Decimal(str(chosen['transport']['base']))]
                if not all(n.is_finite() for n in amounts):
                    raise ValueError('non-finite stored figure')
                row.update(zip(FIGURES, map(_cents, amounts)))
                row.update(computed=True, plan_label=chosen['label'])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                row.update(dict.fromkeys(FIGURES))
                reason = 'plan_not_computed'
        if reason:
            row['reason'] = reason
        rows.append(row)
        footprints.append(footprint(inp) if inp else None)
    rows.sort(key=lambda r: (r['key'], r['case_id']))
    combined = None
    if rows:
        reason = 'not_computed' if any(not r['computed'] for r in rows) else None
        seen: dict[str, set[str]] = {}
        if reason is None:
            for sku, branches in footprints:
                if seen.setdefault(sku, set()).intersection(branches):
                    reason = 'overlap'
                    break
                seen[sku].update(branches)
        combined = {'additive': reason is None, 'plans': len(rows)}
        if reason:
            combined['reason'] = reason
        else:
            combined.update({f: _cents(sum((Decimal(str(r[f])) for r in rows), Decimal(0))) for f in FIGURES})
    offset = (page - 1) * page_size
    return {'kind': 'SIMULATED', 'plans': rows[offset:offset + page_size], 'page': page, 'page_size': page_size,
            'total': len(rows), 'combined': combined}


def actual(conn, run: dict, actor: dict, branch: str | None = None, *, snapshot: dict | None = None) -> dict:
    """Confirmed movement quantities and open discrepancies; after-receipt sales/waste are unrecorded."""
    from .movement import DISPATCH, RECEIPT

    store = scope_store(refdata.get(run['dataset_version']), actor, branch)
    if snapshot is None:
        snapshot = db.one(conn, f"WITH committed_data AS MATERIALIZED ({COMMITTED}), "
                          f"recorded AS ({RECORDED}) SELECT recorded.*, "
                          "(SELECT coalesce(jsonb_agg(to_jsonb(c)), '[]'::jsonb) FROM committed_data c) AS plans "
                          "FROM recorded", r=run['run_id'])
    ids = set()
    for item in snapshot['plans']:
        plan, _ = _bound_plan(item)
        if plan and plan.kind == 'TRANSFER' and (store is None or store in
                                                (item['case_row']['source_branch'], plan.dest)):
            ids.add(item['case_row']['case_id'])
    moves = [m for m in snapshot['movements'] if m['case_id'] in ids]
    dispatched = [m for m in moves if m['kind'] == DISPATCH]
    return {'kind': 'RECORDED', 'dispatched_units': sum(m['qty'] for m in dispatched),
            'dispatched_transfers': len({m['action_id'] for m in dispatched}),
            'received_units': sum(m['qty'] for m in moves if m['kind'] == RECEIPT),
            'discrepancies_open': sum(d['case_id'] in ids for d in snapshot['discrepancies']),
            'sales_after_receipt': None, 'waste_after_receipt': None}


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


# The destination of the plan in force over alias c (D-04). A commitment, cancelled or not, keeps the
# plan key committed in its current round, exactly as `expected` and `actual` bind it (`_bound_plan`),
# never the denormalized plan_dest a newer version or selection may have moved. A live case reads
# plan_dest, which follows its plan in force. Keep, a markdown or a missing commitment has none.
DEST_IN_FORCE = """(CASE WHEN c.state IN ('APPROVED','PREPARED','EXECUTED','OBSERVED')
      OR (c.state = 'CLOSED' AND c.closed_as = 'CANCELLED') THEN (
        SELECT substring(l.payload->>'plan' from '^[^:]+:([^:]+):[^:]*:[^:]*:[^:]*$') FROM activity_log l
        WHERE l.run_id = c.run_id AND l.case_id = c.case_id AND l.type = 'committed'
          AND l.payload->>'round' = c.round::text ORDER BY l.seq DESC LIMIT 1)
      ELSE c.plan_dest END)"""


def case_scope(store: str | None) -> tuple[str, dict]:
    """Strict Reports involvement over alias c, never the board's configurable default (D-04)."""
    if store is None:
        return "true", {}
    return f"(c.source_branch = :report_store OR {DEST_IN_FORCE} = :report_store)", {"report_store": store}


def workspace(conn, run: dict, actor: dict, branch: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """Activity, projections and confirmed records share one statement snapshot under `_read` (D-04)."""
    from . import cases
    from .simplerule import parse_plan

    store = scope_store(refdata.get(run["dataset_version"]), actor, branch)
    where, params = case_scope(store)
    row = db.one(conn, f"""
        WITH committed_data AS MATERIALIZED ({COMMITTED}), recorded AS ({RECORDED}), projections AS (
          SELECT coalesce(jsonb_agg(to_jsonb(p)), '[]'::jsonb) AS plans FROM committed_data p
        ), scoped AS MATERIALIZED (
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
            AND {DEST_IN_FORCE} IS NOT NULL
        ), owners AS (
          SELECT coalesce(jsonb_agg(jsonb_build_object('state', c.state, 'source_branch', c.source_branch,
            'selected', c.selected, 'active_rec_version', c.active_rec_version,
            'rec', CASE WHEN r.rec_version IS NOT NULL THEN jsonb_build_object('rec_version', r.rec_version,
              'result', jsonb_build_object('recommended', r.result->'recommended'), 'fact_checks', '[]'::jsonb) END,
            'decided', (SELECT coalesce(jsonb_object_agg(d.role, d.decision),
            '{{}}'::jsonb) FROM decisions d WHERE d.run_id = c.run_id AND d.case_id = c.case_id
            AND d.rec_version = c.active_rec_version AND d.round = c.round))), '[]'::jsonb) AS rows
          FROM scoped c LEFT JOIN recommendations r ON r.run_id = c.run_id AND r.case_id = c.case_id
            AND r.rec_version = c.active_rec_version WHERE c.state IN ('OPEN','WAITING','PENDING_APPROVAL')
        )
        SELECT activity.*, checks.n AS checks, checks.past_due, checks.addressed,
          transfers.dispatched, transfers.received, owners.rows AS owners, projections.plans,
          recorded.movements, recorded.discrepancies
        FROM activity CROSS JOIN checks CROSS JOIN transfers CROSS JOIN owners CROSS JOIN projections CROSS JOIN recorded
        """, r=run["run_id"], store=store, network=store is None,
                 seat=actor["branch"] if actor["role"] != "hq" else None, **params)
    need, waiting = 0, 0
    if actor["role"] != "hq":
        from .selection import plan_in_force
        for case in row["owners"]:
            # Who acts next follows Phase 7's plan in force, as the board's cards do (07 D-23; 08-18): a selection
            # bound to the active version, else its recommendation. The snapshot carries only what that reads.
            plan_key, _, _ = plan_in_force(case, case["rec"])
            plan = parse_plan(plan_key) if plan_key else None
            yours = any(o["branch"] == actor["branch"] for o in cases.next_owner(case, plan, case["decided"]))
            need += yours
            waiting += yours and case["state"] == "PENDING_APPROVAL"
    return {"scope": {"branch": store, "role": actor["role"]}, "activity": {
        "open": {"count": row["open"], "need_action": need},
        "pending_approval": {"count": row["pending"], "waiting_for_you": waiting},
        "unanswered_check": {"count": row["checks"], "past_due": row["past_due"],
                             "addressed_to_you": row["addressed"]},
        "committed": {"count": row["committed"], "dispatched": row["dispatched"], "received": row["received"]},
        "cancelled": {"count": row["cancelled"]}},
        "expected": expected(conn, run, actor, branch, page, page_size, snapshot=row['plans']),
        "actual": actual(conn, run, actor, branch, snapshot=row)}


SETTINGS_REFUSED = ("ordering", "price_response")  # else "evaluation_settings"


def live_versions(run: dict, ref) -> tuple[dict, str | None]:
    """(the app's six versions, why its settings have no digest or None).

    Live settings an evaluation refuses (ordering enabled at a store, a price response other than
    1.0, a public set without one; D-28, D-33/D-36) leave `settings` null, so no saved result can be
    current. The read still answers and names the refusal by a fixed code, never the error text.
    """
    try:
        return evaluation.app_versions(run, ref), None
    except ValueError as error:
        code = str(error).split(':', 1)[0].split('.', 1)[0]
    manifest = ref.manifest if hasattr(ref, 'manifest') else ref.seed['dataset']
    return ({'engine': evaluation.ENGINE_VERSION, 'dataset': run['dataset_version'],
             'policy': manifest['reference_fingerprint'],
             'forecast_runs': [r['forecast_run_id'] for _, r in sorted(ref.forecast_runs.items())
                               if r.get('job') != 'backtest'],
             'evaluator': evaluation.EVAL_VERSION, 'settings': None},
            code if code in SETTINGS_REFUSED else 'evaluation_settings')


def benchmark(conn, run: dict, actor: dict, branch: str | None = None, *, benchmark_id: str | None = None,
              chart: str | None = None, by: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """Latest matching published result, otherwise latest historical result (D-03/D-24)."""
    ref = refdata.get(run["dataset_version"])
    store = scope_store(ref, actor, branch)
    app, refused = live_versions(run, ref)
    row = db.one(conn, """
        SELECT b.*, s.payload FROM benchmark_runs b JOIN benchmark_summary s USING (benchmark_id)
        WHERE b.status = 'PUBLISHED' AND (CAST(:ident AS text) IS NULL OR b.benchmark_id = :ident)
        ORDER BY coalesce(b.engine_version = :e AND b.dataset_version = :d AND b.policy_version = :p
          AND b.forecast_runs = CAST(:f AS jsonb) AND b.eval_version = :ev AND b.settings_digest = :sd, false) DESC,
          b.published_at DESC, b.benchmark_id DESC LIMIT 1
        """, e=app['engine'], d=app['dataset'], p=app['policy'], f=db.js(app['forecast_runs']),
        ev=app['evaluator'], sd=app['settings'], ident=benchmark_id)
    result = {'status': 'none', 'app': app, 'current': None, 'historical': None, 'settings_note': [],
              'settings_refused': refused}
    if row is None:
        if benchmark_id is not None:
            raise ApiError(404, 'not_found', f'No benchmark {benchmark_id}')
        return result
    versions = {'engine': row['engine_version'], 'dataset': row['dataset_version'],
                'policy': row['policy_version'], 'forecast_runs': row['forecast_runs'],
                'evaluator': row['eval_version'], 'settings': row['settings_digest']}
    differs = [k for k in evaluation.VERSION_KEYS if versions[k] != app[k]]
    status = 'historical' if differs else 'current'
    result['status'] = status
    result[status] = {'kind': 'SIMULATED', 'benchmark_id': row['benchmark_id'],
                      'published_at': row['published_at'], 'git_commit': row['git_commit'],
                      'window': {'first_day': row['first_day'], 'last_day': row['last_day']},
                      'versions': versions, 'differs': differs, 'summary': row['payload'], 'breakdown': None}
    if by is not None:
        result[status]['breakdown'] = breakdown(conn, row['benchmark_id'], chart, by, actor, store, page, page_size)
    if status == 'current':
        recorded = row['payload']['policy']
        result['settings_note'] = [{'field': k, 'workspace': v, 'benchmark': recorded.get(k)}
                                   for k, v in asdict(inputs.sandbox_policy(conn, run, ref)).items()
                                   if v != recorded.get(k)]
    return result


def breakdown(conn, benchmark_id: str, chart: str, by: str, actor: dict, branch: str | None,
              page: int = 1, page_size: int = 20) -> dict:
    """Aggregate only immutable published rows selected by the header's identity (D-15/D-18).

    Scenario columns are the public legend keys, mapped from the outcome ledger's world IDs.
    Branch order always uses absolute net change, including on waste pages. All scope and paging
    happens in SQL. Day pages retain every recorded day rather than paginating the week.
    Money branch rows also expose recorded waste units, ShelfShift movement units and unmet
    demand units. Missing scenario rows leave every figure that needs them null, the money and
    waste columns and their changes included; recorded zeros stay zero.
    """
    if by not in ('day', 'branch') or chart not in ('money', 'waste'):
        raise ApiError(400, 'bad_request', 'breakdown is day or branch; chart is money or waste')
    manager = actor['branch'] if actor['role'] == 'manager' else None
    column = 'day' if by == 'day' else 'branch'
    metric = 'net' if chart == 'money' else 'wasted'
    where = 'benchmark_id = :b'
    if by == 'branch' and manager is None and branch is not None:
        where += ' AND branch = :branch'
    # A missing scenario row leaves its sums null, never 0, and a null change sorts after every recorded one.
    order = 'day' if by == 'day' else 'pinned DESC, abs(total_change_money) DESC NULLS LAST, branch'
    branch_metrics = ''
    if by == 'branch' and chart == 'money':
        branch_metrics = """,
          jsonb_build_object(
            'store_rule', sum(wasted) FILTER (WHERE scenario='baseline_rule'),
            'store_rule_checked', sum(wasted) FILTER (WHERE scenario='baseline_checked'),
            'shelfshift', sum(wasted) FILTER (WHERE scenario='shelfshift'),
            'markdowns_only', sum(wasted) FILTER (WHERE scenario='do_nothing')) AS waste_units,
          sum(moved_out) FILTER (WHERE scenario='shelfshift') AS moved_out,
          sum(moved_in) FILTER (WHERE scenario='shelfshift') AS moved_in,
          jsonb_build_object(
            'store_rule', sum(lost) FILTER (WHERE scenario='baseline_rule'),
            'shelfshift', sum(lost) FILTER (WHERE scenario='shelfshift')) AS demand_not_met
        """
    # These identifiers are chosen exclusively from the closed lists above, never query text.
    aggregate = f"""
        SELECT {column},
          sum({metric}) FILTER (WHERE scenario='baseline_rule') AS store_rule,
          sum({metric}) FILTER (WHERE scenario='baseline_checked') AS store_rule_checked,
          sum({metric}) FILTER (WHERE scenario='shelfshift') AS shelfshift,
          sum({metric}) FILTER (WHERE scenario='do_nothing') AS markdowns_only,
          sum(net) FILTER (WHERE scenario='shelfshift')
            - sum(net) FILTER (WHERE scenario='baseline_rule') AS total_change_money
          {branch_metrics}
        FROM benchmark_branch_day WHERE {where} GROUP BY {column}
    """
    pinned = 'false' if by == 'day' else '(branch = :manager) IS TRUE'
    limit = '' if by == 'day' else ' LIMIT :size OFFSET :offset'
    result = db.one(conn, f"""
        WITH grouped AS ({aggregate}), projected AS (
          SELECT *, store_rule_checked-store_rule AS checks_change,
            shelfshift-store_rule_checked AS decisions_change, shelfshift-store_rule AS total_change,
            shelfshift-markdowns_only AS change_vs_markdowns_only, {pinned} AS pinned
          FROM grouped), numbered AS (
          SELECT *, row_number() OVER (ORDER BY {order}) AS row_order FROM projected), paged AS (
          SELECT * FROM numbered ORDER BY row_order {limit})
        SELECT (SELECT count(*) FROM grouped) AS total,
          coalesce((SELECT jsonb_agg(to_jsonb(paged) - 'total_change_money' - 'row_order' ORDER BY row_order)
                    FROM paged), '[]'::jsonb) AS rows
        """, b=benchmark_id, branch=branch, manager=manager, size=page_size, offset=(page-1)*page_size)
    return {'benchmark_id': benchmark_id, 'kind': 'SIMULATED', 'chart': chart, 'by': by,
            'rows': result['rows'], 'page': 1 if by == 'day' else page,
            'page_size': result['total'] if by == 'day' else page_size, 'total': result['total']}
