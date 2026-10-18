"""The slowest cases by construction, and the reset, timed against the fixed budgets (spec §6 Time budget, §8 Reset
target; 05.2-CONTEXT D-12, D-21, D-28).

    python -m api.casebench [--top M] [--resets N] [--export PATH]

The slowest case by a stated rule, never a hand-picked one; budgets fixed. A case's time grows with the plans the engine
weighs, so the candidates are ranked by construction, with no engine call, in one SQL query over the ACTIVE template's
open day: every flag the template has not opened and every live case, by its eligible destinations (stage 1,
api/eligibility.py, on a public set, the carrying rule included; every run leaving the source today on the synthetic
set), then the lots of the product at the source and those destinations (what the engine allocates over), both
descending, then key. The top M are timed through the same path a request takes: each item's raw input from
`inputs.build_input` on the template, then `worker.run(evaluate._compute, inp)` (evaluate + fact checks) in the one
engine worker, three times, and the median is printed per item with the slowest last. The worker is started and warmed
first, as the app's lifespan does before the first request (inline when ENGINE_WORKER is off). Then N throwaway sessions
each get a workspace through `sandbox.reset` in its own transaction, timed from before the transaction to after its
commit (the reset's own figure printed beside it), and every session and workspace is removed afterwards.

It exits 0 when every budget holds, 1 when the slowest median is 3 s or more or any reset is 2000 ms or more, and 2
with no ACTIVE template or nothing to time. `--export PATH` writes the slowest item's raw input as the engine's
`to_dict` JSON, which `from_dict` reads back to the same `rec_version`. The budgets are the spec's (api/bench.py holds
the same case budget for the story case) and are never raised; a miss is reported, never tuned away.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import secrets
import statistics
import sys
import time

from engine import to_dict
from engine.simulate import HorizonError

from . import cases, config, db, evaluate, inputs, refdata, sandbox, worker

CASE_BUDGET_S = 3.0  # spec §6: one case, evaluate + fact checks, within 3 s. Never raised.
RESET_BUDGET_MS = 2000  # spec §8: a reset (or a new workspace) within 2 s. Never raised.
RUNS = 3  # each case is timed three times; the median counts

# The candidates by construction (one query). :public turns stage 1 on: a mapped source, a cold-chain run for a
# chilled product, a first sellable day that leaves min_sellable_days before expiry, free totes on the arrival day, a
# destination that carries the product (a series of it in ref_sales_day, PublicRef.carries) (api/eligibility.py, the
# same rules in the same terms). The synthetic set's runs come in as JSON (its runs live in the seed, not in ref_run).
_CANDIDATES = """
WITH items AS (
  SELECT c.case_id AS key, c.source_branch AS store, c.sku, c.lot_id FROM cases c
   WHERE c.run_id = :r AND c.state = ANY(:live)
  UNION ALL
  SELECT f.key, f.branch, f.sku, f.lot_id FROM case_flags f
   WHERE f.run_id = :r AND f.day = :d
     AND NOT EXISTS (SELECT 1 FROM cases c WHERE c.run_id = f.run_id AND c.lot_id = f.lot_id)),
runs AS (
  SELECT run_code, origin, dest, arrives_min, cold_chain FROM ref_run WHERE :public AND dataset_version = :v AND day = :d
  UNION ALL
  SELECT run_code, origin, dest, arrives_min, cold_chain
    FROM jsonb_to_recordset(CAST(:runs AS jsonb)) AS j(run_code text, origin text, dest text, arrives_min int,
                                                        cold_chain boolean)
   WHERE NOT :public),
dests AS (
  SELECT DISTINCT i.key, r.dest FROM items i
    JOIN lots l ON l.run_id = :r AND l.world = 'shelfshift' AND l.lot_id = i.lot_id
    JOIN runs r ON r.origin = i.store
    LEFT JOIN ref_store s ON s.dataset_version = :v AND s.store = i.store
    LEFT JOIN ref_product p ON p.dataset_version = :v AND p.sku = i.sku
    LEFT JOIN ref_capacity k ON k.dataset_version = :v AND k.store = r.dest AND k.zone = p.zone
                            AND k.day = :d + r.arrives_min / 1440
   WHERE NOT :public OR (s.geo_known
     AND (p.zone <> 'chilled' OR r.cold_chain)
     AND l.expiry_day - (:d + r.arrives_min / 1440 + CASE WHEN mod(r.arrives_min, 1440) >= :same_day THEN 1 ELSE 0 END)
         + 1 >= :min_days
     AND coalesce(k.free_totes, 0) > 0
     AND EXISTS (SELECT 1 FROM ref_sales_day sd WHERE sd.dataset_version = :v AND sd.store = r.dest
                                                  AND sd.sku = i.sku)))
SELECT i.key, i.store, i.sku AS product, i.lot_id,
       (SELECT count(*) FROM dests d WHERE d.key = i.key) AS dests,
       (SELECT count(*) FROM lots l WHERE l.run_id = :r AND l.world = 'shelfshift' AND l.sku = i.sku AND l.qty > 0
          AND (l.branch = i.store OR l.branch IN (SELECT d.dest FROM dests d WHERE d.key = i.key))) AS lots
  FROM items i
 ORDER BY dests DESC, lots DESC, i.key COLLATE "C"
 LIMIT :top
"""


def slowest_candidates(conn, template: dict, top: int) -> list[dict]:
    """The template's flags and live cases of its day, most eligible destinations first, then most lots, then key:
    [{key, store, product, lot_id, dests, lots}], at most `top` of them. One SQL query, no engine call."""
    ref = refdata.get(template["dataset_version"])
    policy = inputs.sandbox_policy(conn, template, ref)
    public = ref.kind == "public"
    runs = [] if public else [{"run_code": r.run_code, "origin": r.origin, "dest": r.dest, "arrives_min": r.arrives_min,
                               "cold_chain": r.cold_chain} for r in ref.runs if r.day == template["day"]]
    rows = db.rows(conn, _CANDIDATES, r=template["run_id"], d=template["day"], v=template["dataset_version"],
                   live=list(cases.LIVE_STATES), public=public, runs=json.dumps(runs),
                   same_day=policy.sellable_same_day_before_min, min_days=policy.min_sellable_days, top=top)
    return [{**r, "dests": int(r["dests"]), "lots": int(r["lots"])} for r in rows]


def time_cases(conn, template: dict, candidates: list[dict], runs: int = RUNS) -> list[dict]:
    """Each candidate's raw input built on the template, then evaluate + fact checks through the worker `runs` times;
    prints `<key> <store> <product> dests=<n> lots=<n> median=<s> s` and returns the candidates with `median_s` and
    `input`. An item whose input lacks data (a missing forecast day) is printed as such and not timed."""
    out = []
    for c in candidates:
        try:
            inp = inputs.build_input(conn, template, c["lot_id"])
        except (HorizonError, LookupError) as exc:
            print(f"{c['key']} {c['store']} {c['product']} dests={c['dests']} lots={c['lots']} not timed: {exc}")
            continue
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            try:
                worker.run(evaluate._compute, inp)
            except HorizonError as exc:
                print(f"{c['key']} {c['store']} {c['product']} dests={c['dests']} lots={c['lots']} not timed: {exc}")
                break
            times.append(time.perf_counter() - t0)
        else:
            median = statistics.median(times)
            print(f"{c['key']} {c['store']} {c['product']} dests={c['dests']} lots={c['lots']} median={median:.3f} s")
            out.append({**c, "median_s": median, "input": inp})
    return out


def _drop(conn, run_ids: list[str], session_ids: list[str]) -> None:
    """Delete these throwaway workspaces (every per-run row, their keys, their demo_runs rows) and sessions."""
    for table in sandbox.per_run_tables(conn):  # names from information_schema, never from a request
        db.run(conn, f"DELETE FROM {table} WHERE run_id = ANY(:ids)", ids=run_ids)
    db.run(conn, "DELETE FROM idempotency_keys WHERE run_id = ANY(:ids) OR session_id = ANY(:s)", ids=run_ids,
           s=session_ids)
    db.run(conn, "DELETE FROM sessions WHERE session_id = ANY(:s)", s=session_ids)
    db.run(conn, "DELETE FROM demo_runs WHERE run_id = ANY(:ids) AND kind = 'SANDBOX'", ids=run_ids)


def time_resets(n: int) -> list[int]:
    """N throwaway sessions, each given a workspace by `sandbox.reset` in its own transaction; prints and returns
    each reset's wall time in ms (before the transaction to after its commit), the reset's own figure beside it, then
    removes the sessions and their workspaces."""
    sids, runs, times = [], [], []
    try:
        for i in range(n):
            sid = f"bench-{secrets.token_hex(8)}"
            with db.tx() as conn:
                db.run(conn, "INSERT INTO sessions (session_id, role) VALUES (:s, 'hq')", s=sid)
            sids.append(sid)
            t0 = time.perf_counter()
            with db.tx() as conn:
                new, ms = sandbox.reset(conn, sid)
            wall = math.ceil((time.perf_counter() - t0) * 1000)
            runs.append(new)
            times.append(wall)
            print(f"reset {i + 1}: {wall} ms (reset_ms {ms})")
    finally:
        with db.tx() as conn:
            _drop(conn, runs, sids)
    return times


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m api.casebench", description=__doc__.splitlines()[0])
    ap.add_argument("--top", type=int, default=20, help="how many of the slowest candidates to time (default 20)")
    ap.add_argument("--resets", type=int, default=10, help="how many resets to time (default 10)")
    ap.add_argument("--export", help="write the slowest item's raw input (engine to_dict JSON) here")
    args = ap.parse_args(argv)
    with db.tx() as conn:
        template = sandbox.active_template(conn)
        if template is None:
            print("no ACTIVE template: run python -m api.sandbox build first", file=sys.stderr)
            return 2
        candidates = slowest_candidates(conn, template, args.top)
    print(f"template {template['run_id']} ({template['dataset_version']}, day {template['day']}): "
          f"{len(candidates)} candidates by eligible destinations, then lots")
    started = config.ENGINE_WORKER and worker.pid() is None
    if started:
        worker.start()  # warm, as the app's lifespan starts it before the first request
    try:
        with db.tx() as conn:
            timed = time_cases(conn, template, candidates)
    finally:
        if started:
            worker.stop()
    resets = time_resets(args.resets)
    if not timed:
        print("nothing to time: the template has no flag or live case with its data", file=sys.stderr)
        return 2
    slowest = max(timed, key=lambda t: t["median_s"])
    print(f"slowest: {slowest['key']} {slowest['store']} {slowest['product']} dests={slowest['dests']} "
          f"lots={slowest['lots']} median={slowest['median_s']:.3f} s (budget {CASE_BUDGET_S:g} s)")
    if resets:
        print(f"resets: {len(resets)}, slowest {max(resets)} ms (budget {RESET_BUDGET_MS} ms)")
    if args.export and slowest["input"] is not None:
        path = pathlib.Path(args.export)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_dict(slowest["input"]), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote the slowest input to {path}")
    over = slowest["median_s"] >= CASE_BUDGET_S or any(ms >= RESET_BUDGET_MS for ms in resets)
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
