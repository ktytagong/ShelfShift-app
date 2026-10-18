"""The careful-day evaluation driver and the trip check the second-look tests need (04-CONTEXT D-06).

This module is an evaluation driver, not part of the app: it stands in for honest counters by reading PHYSICAL stock
(`truth_lots`), which no route may ever do (FLOW-08). This module alone reads physical stock: no module under api/
other than this one imports it, and no route may (a static test checks it).

The benchmark job (08-15; 08-CONTEXT D-02, D-21, D-27, D-34), one JSON report line on stdout:

    python -m api.weekcheck benchmark start --dataset <version>                          # days 0..6, validate, publish
    python -m api.weekcheck benchmark start --dataset <version> --trial --minutes N [--stores N]   # measured, never published
    python -m api.weekcheck benchmark resume <benchmark_id>              # same image, commit and inputs only (D-21)

Exit 0: published, or a trial report written; 1: validation failed or a reported stop (a forecast gap, a crash),
nothing published; 2: refused before any work. The dataset version is an argument from the release record, never a
code constant (D-36).

The five-seed check (08-16; RPT-05, 08-CONTEXT D-06), one JSON line per seed as it finishes, then a summary line:

    python -m api.weekcheck                       # the same as `seeds`
    python -m api.weekcheck seeds [--seeds all|N,...]

An engineering robustness check, never shown on Reports (D-06). Each evaluation seed of reference/scenarios.json, in
its listed order, gets its own generated evaluation set (synthetic, labelled so) with the labelled local stand-in
forecast, installed into the local database, and the careful week on the three app worlds (`seed_week`). Exit 1 when
any seed fails one of the four gates (units add up, no limit broken, no unit booked twice, every trip paid once) or
stops with an error, after every seed has reported; 0 otherwise; 2 refused before any work. Which scenario wins never
decides the exit code.

- `careful_day`: for each OPEN case with a version, the first fact on its active version that changes the plan and
  that a count settles (PHANTOM_STOCK, STALE_COUNT) is asked about by the case's source manager, the addressed branch
  answers every lot in the fact's lot_ids with its physical quantity on the shelf and 0 in the backroom, and confirms
  with the scope tick, all through `signals`; then the warm-up autopilot sends, accepts, approves and prepares every
  open case through the service layer (`sandbox.autopilot`).
- `correct_compare_world`: after a confirmed count, the comparison world ("Modeled store rule with equivalent stock
  checks") corrects the same lots from its own physical stock, never from ShelfShift's counted quantity (08-CONTEXT
  D-09, D-10; research Q3, Pitfall 5); a lot not on that world's record is a check with nothing to correct.
- `checked_morning`: D-11's daily check sequence for the benchmark (and the seeds, without a comparison world): every
  plan-changing count fact within the per-version cap, each lot counted at most once a day per world, answered from
  each world's own physical stock, passes until one asks nothing; a durable history in evaluation_check_history.
- `benchmark_day` / `run_days`: one simulated benchmark day per transaction in D-11 step 6's order (the day's start,
  the checked morning, `evalcommit.commit_all`, hold expiry and the due transitions, both store rules and the world
  steps, the lifecycle, the closing balance after day 6), each ending with its checkpoint under the 08-12 row-lock
  claim; days 0..6 inside the D-08 forecast rule and the benchmark's own engine-cache namespace (08-14; D-08, D-11,
  D-21, D-26, D-29).
- `trips_paid_once`: the transport in the shelfshift outcomes equals one `engine.model.trip_cost` per dispatched
  transfer (a short shipment at the quantity it shipped, a failed trip at its cancel cost), within $0.01, and no
  dispatched round still holds its source set-aside.
- `seed_week` / `seed_result` / `booked_twice`: the five-seed check's week on one seed, its win/tie/loss rule against
  the store rule, and its no-unit-booked-twice gate (08-16).
"""
from __future__ import annotations

import contextlib
import json
import time

from engine.model import trip_cost

from . import advance, cases, db, overlay, sandbox, signals

BENCH_WORLDS = ("shelfshift", "baseline_rule", "baseline_checked", "do_nothing")


def world_steps(conn, run: dict, truth, worlds=BENCH_WORLDS) -> dict:
    """Both rules on their own records, then matched hidden demand; caller owns the day's transaction."""
    from . import config, refdata, screen
    ref = refdata.get(run['dataset_version'])
    morning = db.rows(conn, "SELECT sku,expiry_day FROM lots WHERE run_id=:r AND world='shelfshift' AND qty>0",
                      r=run['run_id'])
    at_risk = sum(screen.window(l['expiry_day'], ref.schedules[l['sku']],
                               config.CASE_OPENING['days_before_first_step'])[0] <= run['day'] <= l['expiry_day']
                  for l in morning)
    decisions = {}
    for world in ("baseline_rule", "baseline_checked"):
        if world in worlds:
            decisions[world] = len(advance.baseline_morning(conn, run, world=world))
    for world in worlds:
        advance.step_world(conn, run, world, truth)
    return {world: {"completed": True, "decisions": decisions.get(world, 0), 'at_risk_lots': at_risk}
            for world in worlds}

def _no_stage(name: str):
    return contextlib.nullcontext()


def benchmark_day(conn, run: dict, bench: dict, truth, settings: dict, *, stage=_no_stage) -> dict:
    """One simulated day of the benchmark in the caller's single transaction, in D-11 step 6's order (08-14; D-08,
    D-11, D-21, D-26, D-29), ending with its checkpoint. `run` is the simulation's row (locked by the caller), `bench`
    the claimed benchmark_runs row, `settings` its stored evaluation settings.

    1. The day's start (days 1..6): the simulation moves to the next day, today's deliveries arrive in every world
       (`advance.day_start`) and the eager case morning opens and re-evaluates every qualifying lot
       (`advance.cases_morning(eager=True)`). Day 0's start and morning ran in `benchmark.create_run`. The start sits
       at the head of its own day's transaction, so a checkpoint's simulation day is the day it completed (08-12's
       `claim_resume` checks that), and after day 6 no day 7 ever starts (Pitfall 8).
    2. The checked morning (`checked_morning`, D-09..D-11).
    3. ShelfShift commits (`evalcommit.commit_all`, D-26, D-29: retry_limit and the Keep fast path from the stored
       settings).
    4. Hold expiry and the APPROVED-but-not-prepared transition, as `advance.advance_day` runs them before the worlds
       step (`bookings.expire_holds`, `advance._lifecycle_before`).
    5. Both store rules on their own records and the four world steps (`world_steps`).
    6. The lifecycle after the outcomes (`advance._lifecycle_after`).
    7. After day 6 only: the closing balance (D-08), recorded with the day.
    8. The day's checkpoint (`benchmark.mark_day`), last: the left-open reasons, retry counts (by case and by the
       refusal code that caused each recheck), planned units and the processing order's digest go into its stats
       (D-21, D-29).

    `stage(name)` is a context manager wrapped around each step (the trial's measurement, 08-15); it changes nothing
    the day does. Names: start, checks, commits, before_worlds, worlds, after_worlds, closing, checkpoint (STAGES).
    """
    from data.versions import digest
    from . import benchmark, bookings, evalcommit
    started = time.perf_counter()
    day = 0 if bench["last_day_done"] is None else bench["last_day_done"] + 1
    if day != run["day"] + (0 if day == 0 else 1) or day > bench["last_day"]:
        raise ValueError(f"benchmark day {day} does not follow the simulation day {run['day']}")
    if day > 0:
        with stage("start"):
            db.log(conn, run["run_id"], run["day"], "day_advanced", to_day=day)
            run = db.one(conn, "UPDATE demo_runs SET day = :d, version = version + 1 WHERE run_id = :r RETURNING *",
                         d=day, r=run["run_id"])
            advance.day_start(conn, run, worlds=BENCH_WORLDS)
            advance.cases_morning(conn, run, eager=True)
    with stage("checks"):  # the public benchmark names its comparison world explicitly (08-16)
        checks = checked_morning(conn, run, bench_id=bench["benchmark_id"], compare_world="baseline_checked")
    with stage("commits"):
        commits = evalcommit.commit_all(conn, run, retry_limit=settings["commits"]["retry_limit"],
                                        keep_fast_path=settings["keep_fast_path"])
    with stage("before_worlds"):
        bookings.expire_holds(conn, run)
        invalidated = advance._lifecycle_before(conn, run)
    with stage("worlds"):
        worlds = world_steps(conn, run, truth)
    with stage("after_worlds"):
        advance._lifecycle_after(conn, run)
    stats = {"completed": True, "worlds": worlds, "checks": checks, "committed": commits["committed"],
             "left_open": commits["left_open"], "retries": commits["retries"],
             "retry_codes": commits["retry_codes"], "transfer_units": commits["transfer_units"],
             "order": commits["order"], "order_digest": digest("ord-", commits["order"]), "invalidated": invalidated}
    if day == bench["last_day"]:
        with stage("closing"):
            stats["closing"] = closing_balance(conn, run)
    with stage("checkpoint"):
        benchmark.mark_day(conn, bench["benchmark_id"], day, round(time.perf_counter() - started, 3), stats)
    return {"day": day, **stats}


def run_days(bench_id: str, *, through: int, image_digest: str, git_commit: str) -> list[dict]:
    """Step the benchmark's days from its next unfinished day through `through` (at most the window's last day), one
    transaction per day (D-21). Each day first claims the benchmark under its row lock (`benchmark.claim_resume`:
    the same image digest and code commit as stored, the six versions and the settings digest; any difference is
    refused with the fresh-run instruction before anything is written) and keeps that transaction through the whole
    day and its checkpoint (08-12). The days run inside the D-08 forecast rule (`evalref.forecast_rule`) and the
    benchmark's own engine-cache namespace (`evaluate.cache_namespace`, its simulation id: stable across a resume).
    An error inside a day rolls that whole day back; the next call redoes it from the last checkpoint. Returns the
    stats of the days stepped. Trial benchmarks are not stepped here (claim_resume refuses them)."""
    from . import benchmark, evaluate, evalref, refdata
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM benchmark_runs WHERE benchmark_id = :b", b=bench_id)
    if row is None:
        raise ValueError("unknown benchmark")
    dv = row["dataset_version"]
    ref = refdata.get(dv)
    identity = dict(image_digest=image_digest, git_commit=git_commit,
                    versions=benchmark.app_versions({"dataset_version": dv}, ref),
                    settings_digest=benchmark.settings_digest(ref))
    truth = refdata.truth(dv)
    done: list[dict] = []
    with evalref.forecast_rule(dv), evaluate.cache_namespace(row["sim_run_id"]):
        while True:
            with db.tx() as conn:
                day = benchmark.claim_resume(conn, bench_id, **identity)
                bench = db.one(conn, "SELECT * FROM benchmark_runs WHERE benchmark_id = :b", b=bench_id)
                if day > min(through, bench["last_day"]):
                    break
                run = db.lock_run(conn, bench["sim_run_id"], mode="UPDATE", statuses=("INITIALIZING",))
                done.append(benchmark_day(conn, run, bench, truth, bench["settings"]))
    return done

TOLERANCE = 0.01  # money compared to the cent


def careful_day(conn, run: dict) -> None:
    """Ask about every plan-changing count fact, count honestly from physical stock, confirm, then the autopilot."""
    for listed in db.rows(conn, "SELECT case_id FROM cases WHERE run_id = :r AND state = 'OPEN' "
                                "AND active_rec_version IS NOT NULL ORDER BY case_id", r=run["run_id"]):
        case = cases.case_row(conn, run["run_id"], listed["case_id"])  # an earlier confirm may have replanned it
        rec = cases.active_rec(conn, case)
        if case["state"] != "OPEN" or rec is None:
            continue
        fact = next((f for f in rec["fact_checks"] if f["changes_plan"] and f["kind"] in signals.COUNT_KINDS), None)
        if fact is None:
            continue
        asker = {"role": "manager", "branch": case["source_branch"], "session_id": "weekcheck"}
        q = signals.request_evidence(conn, run, asker, case["case_id"], rec["rec_version"], fact["fact_id"])
        if q["state"] not in signals.ANSWERABLE:
            continue  # already settled on this version
        counter = {"role": "manager", "branch": fact["branch"], "session_id": "weekcheck"}
        physical = overlay.physical(conn, run, "shelfshift", fact["lot_ids"])  # through the template (05.2 D-09)
        counts = {lot_id: {"shelf_units": physical[lot_id], "backroom_units": 0.0} for lot_id in fact["lot_ids"]}
        s = signals.answer(conn, run, counter, q["request_id"], "counted", counts, "now")  # counted now (D-22)
        signals.confirm(conn, run, counter, s["signal_id"], s["revision"], True)
    sandbox.autopilot(conn, run)


def correct_compare_world(conn, run: dict, lot_ids, day: int, world: str = "baseline_checked") -> dict:
    """D-10: the comparison world's count reads its own physical stock and corrects its own record.

    Per lot {record_before, physical, correction}: "up", "down" or "none" when the lot is on that world's record (its
    record is set to its own physical stock, last_verified_day = day, verified_by = 'COUNT'), "gone" when it is not
    (sold, expired, shipped, or a moved-lot id that exists only in ShelfShift's world): a check with nothing to
    correct. ShelfShift's counted quantity is never read here (Pitfall 5)."""
    ids = list(dict.fromkeys(lot_ids))
    physical = overlay.physical(conn, run, world, ids)
    out = {}
    for lot_id in ids:
        row = overlay.lot(conn, run, world, lot_id)
        if row is None:
            out[lot_id] = {"record_before": None, "physical": None, "correction": "gone"}
            continue
        before, actual = float(row["qty"]), float(physical.get(lot_id, 0.0))
        overlay.materialize(conn, run, world, row["branch"], row["sku"])
        db.run(conn, "UPDATE lots SET qty = :physical, last_verified_day = :d, verified_by = 'COUNT' "
                     "WHERE run_id = :r AND world = :w AND lot_id = :l",
               physical=actual, d=day, r=run["run_id"], w=world, l=lot_id)
        out[lot_id] = {"record_before": before, "physical": actual,
                       "correction": "up" if actual > before else "down" if actual < before else "none"}
    return out


def _memory(conn, run_id: str, day: int) -> dict:
    """Today's durable check history (evaluation_check_history), so a re-entered or restarted driver sees what an
    earlier transaction committed: each world's observed lots with the value observed there, the case versions the
    per-version cap closed, and the (case, version, fact) checks already handled (asked or refused)."""
    memory = {"observed": {}, "capped": set(), "done": set()}
    for h in db.rows(conn, "SELECT * FROM evaluation_check_history WHERE sim_run_id = :r AND day = :d "
                           "ORDER BY world, seq", r=run_id, d=day):
        if h["event_kind"] == "observation":
            memory["observed"].setdefault(h["world"], {})[h["lot_id"]] = h["outcome"]["observed_units"]
        elif h["world"] == "shelfshift" and h["event_kind"] in ("request", "refusal"):
            if h["outcome"].get("code") == "too_many_requests":
                memory["capped"].add((h["case_id"], h["rec_version"]))
            elif h["fact_id"]:
                memory["done"].add((h["case_id"], h["rec_version"], h["fact_id"]))
    return memory


def _history(conn, run: dict, bench_id, world: str, kind: str, *, case_id=None, rec_version=None, fact_id=None,
             request_id=None, signal_id=None, lot_id=None, lot_ids=(), observed=None, outcome=None) -> None:
    """One evaluation_check_history row, the next seq of (simulation, day, world); bench_id is None for seeds."""
    seq = db.one(conn, "SELECT coalesce(max(seq), -1) + 1 AS n FROM evaluation_check_history WHERE sim_run_id = :r "
                       "AND day = :d AND world = :w", r=run["run_id"], d=run["day"], w=world)["n"]
    db.run(conn, "INSERT INTO evaluation_check_history (sim_run_id, benchmark_id, day, world, seq, event_kind, case_id, "
                 "rec_version, fact_id, request_id, signal_id, lot_id, lot_ids, observed_counts, outcome) VALUES "
                 "(:r, :b, :d, :w, :n, :k, :c, :v, :f, :q, :s, :l, CAST(:ls AS jsonb), CAST(:o AS jsonb), "
                 "CAST(:out AS jsonb))", r=run["run_id"], b=bench_id, d=run["day"], w=world, n=seq, k=kind,
           c=case_id, v=rec_version, f=fact_id, q=request_id, s=signal_id, l=lot_id, ls=db.js(list(lot_ids)),
           o=db.js(observed or {}), out=db.js(outcome or {}))


def _observation_row(conn, bench_id: str, run: dict, lot_id: str, branch: str, case_id: str, rec_version: str,
                     request_id: str, results: dict) -> None:
    """One benchmark_observations row (the D-11 observation schedule), the next seq of (benchmark, day)."""
    seq = db.one(conn, "SELECT coalesce(max(seq), -1) + 1 AS n FROM benchmark_observations WHERE benchmark_id = :b "
                       "AND day = :d", b=bench_id, d=run["day"])["n"]
    db.run(conn, "INSERT INTO benchmark_observations (benchmark_id, day, seq, lot_id, branch, case_id, rec_version, "
                 "request_id, results) VALUES (:b, :d, :n, :l, :br, :c, :v, :q, CAST(:res AS jsonb))",
           b=bench_id, d=run["day"], n=seq, l=lot_id, br=branch, c=case_id, v=rec_version, q=request_id,
           res=db.js(results))


def _direction(before, actual) -> str:
    if before is None:
        return "gone"
    return "up" if actual > before else "down" if actual < before else "none"


def checked_morning(conn, run: dict, *, bench_id=None, compare_world="baseline_checked") -> dict:
    """D-11's checked morning on the simulation's own INITIALIZING run, in the caller's transaction, before anyone
    acts that day (the commit loop and both store rules come after it).

    Each pass reads every OPEN case's active version in case_id order. For each version it first asks, as the
    source manager, every count fact (signals.COUNT_KINDS) with changes_plan of the plan in force, in the engine's
    order (commitment.checks_in_force), within requests.max_per_version: a 409 too_many_requests ends that version's
    asking and is remembered; a fact whose lots are all counted today (or already asked this pass) is not asked (a lot
    is counted at most once a day). Then each request is answered by the addressed branch with the request's exact
    declared lot set (signals.answer validates it): a lot already observed today reuses that world's observed value,
    the others are read freshly from ShelfShift's own physical stock (shelf = physical, backroom 0, observed "now"),
    and confirmed with statuses=("INITIALIZING",). A 409 from confirm (older_evidence, review_required,
    count_mismatch, ...) is recorded and never retried with altered values. After each confirmation the comparison
    world (compare_world; None for seeds) corrects the same lots from its own physical stock
    (`correct_compare_world`, D-10). Every confirmation re-evaluates the cases in its scope (facts.apply); a case whose
    version changed is read again in the next pass, so an old version's unasked checks are never asked. Passes repeat
    until one asks nothing; each asking pass counts at least one new lot, so the loop ends.

    Every request, observation, confirmation, replan and refusal is an evaluation_check_history row, in seeds mode
    too (bench_id None); with a bench_id each observed lot is also a benchmark_observations row with each world's
    record before, physical and correction. Returns {passes, asked, confirmed, refused, lots_counted}: requests and
    freshly counted lots, counted separately (D-11)."""
    from . import commitment
    from .errors import ApiError
    run_id, day = run["run_id"], run["day"]
    worlds = ("shelfshift",) + ((compare_world,) if compare_world else ())
    totals = {"passes": 0, "asked": 0, "confirmed": 0, "refused": 0, "lots_counted": 0}
    while True:
        totals["passes"] += 1
        asked = 0
        memory = _memory(conn, run_id, day)  # reread every pass: what an earlier transaction committed counts
        seen = memory["observed"].setdefault("shelfshift", {})
        cseen = memory["observed"].setdefault(compare_world, {}) if compare_world else {}
        snapshot = db.rows(conn, "SELECT case_id, active_rec_version FROM cases WHERE run_id = :r AND state = 'OPEN' "
                                 "AND active_rec_version IS NOT NULL ORDER BY case_id", r=run_id)
        for listed in snapshot:
            case = cases.case_row(conn, run_id, listed["case_id"])
            if case["state"] != "OPEN" or case["active_rec_version"] != listed["active_rec_version"]:
                continue  # replanned during this pass: its new version is read in the next pass
            rec = cases.active_rec(conn, case)
            if rec is None:
                continue
            cid, version = case["case_id"], rec["rec_version"]
            asker = {"role": "manager", "branch": case["source_branch"], "session_id": "weekcheck"}
            pending, requests = set(), []
            for fact in commitment.checks_in_force(case, rec):
                if not fact["changes_plan"] or fact["kind"] not in signals.COUNT_KINDS:
                    continue
                key, base = (cid, version, fact["fact_id"]), dict(case_id=cid, rec_version=version,
                                                                  fact_id=fact["fact_id"])
                if (cid, version) in memory["capped"]:
                    break
                if key in memory["done"] or set(fact["lot_ids"]) <= set(seen) | pending:
                    continue
                try:
                    q = signals.request_evidence(conn, run, asker, cid, version, fact["fact_id"])
                except ApiError as refused:
                    _history(conn, run, bench_id, "shelfshift", "refusal", lot_ids=fact["lot_ids"], **base,
                             outcome={"stage": "request", "code": refused.code, "message": refused.message})
                    totals["refused"] += 1
                    if refused.code == "too_many_requests":
                        memory["capped"].add((cid, version))
                    else:
                        memory["done"].add(key)
                    break  # the cap, or a version this case can no longer ask on
                memory["done"].add(key)
                if q["state"] not in signals.CONFIRMABLE:  # settled, expired or superseded: nothing to confirm
                    _history(conn, run, bench_id, "shelfshift", "refusal", lot_ids=q["lot_ids"],
                             request_id=q["request_id"], **base,
                             outcome={"stage": "request", "code": "already_settled", "state": q["state"]})
                    totals["refused"] += 1
                    continue
                for world in worlds:
                    _history(conn, run, bench_id, world, "request", request_id=q["request_id"], lot_ids=q["lot_ids"],
                             **base, outcome={"to_branch": q["to_branch"]})
                pending |= set(q["lot_ids"])
                requests.append((base, q))
                asked += 1
                totals["asked"] += 1
            for base, q in requests:
                lot_ids = list(q["lot_ids"])  # the request's exact declared set, never shrunk
                fresh = [l for l in lot_ids if l not in seen]
                read = overlay.physical(conn, run, "shelfshift", fresh)  # through the template (05.2 D-09)
                values = {l: float(seen[l]) if l in seen else float(read.get(l, 0.0)) for l in lot_ids}
                ss = {}
                for l in fresh:
                    row = overlay.lot(conn, run, "shelfshift", l)
                    before = float(row["qty"]) if row is not None else None
                    ss[l] = {"record_before": before, "physical": values[l], "correction": _direction(before, values[l])}
                    _history(conn, run, bench_id, "shelfshift", "observation", request_id=q["request_id"], lot_id=l,
                             lot_ids=[l], observed={l: values[l]}, **base,
                             outcome={**ss[l], "recorded_units": before if before is not None else 0.0,
                                      "observed_units": values[l]})
                    seen[l] = values[l]
                totals["lots_counted"] += len(fresh)
                counter = {"role": "manager", "branch": q["to_branch"], "session_id": "weekcheck"}
                counts = {l: {"shelf_units": values[l], "backroom_units": 0.0} for l in lot_ids}
                s = signals.answer(conn, run, counter, q["request_id"], "counted", counts, "now")  # counted now (D-22)
                ids = dict(request_id=q["request_id"], signal_id=s["signal_id"], lot_ids=lot_ids)
                compare = {}
                try:
                    with conn.begin_nested():  # a refused confirm leaves nothing half-written
                        done = signals.confirm(conn, run, counter, s["signal_id"], s["revision"], True,
                                               statuses=("INITIALIZING",))
                except ApiError as refused:
                    totals["refused"] += 1
                    confirm = {"confirmed": False, "code": refused.code, "message": refused.message}
                    for world in worlds:
                        _history(conn, run, bench_id, world, "refusal", **ids, **base,
                                 outcome={"stage": "confirm", "code": refused.code, "message": refused.message})
                else:
                    totals["confirmed"] += 1
                    confirm = {"confirmed": True, "fact_id": done["fact_id"], "unchanged": done["unchanged"]}
                    _history(conn, run, bench_id, "shelfshift", "confirm", **ids, **base, outcome=confirm)
                    for moved in done["cases"]:
                        _history(conn, run, bench_id, "shelfshift", "replan", case_id=moved["case_id"],
                                 rec_version=moved["rec_version"], request_id=q["request_id"],
                                 outcome={"by_case": base["case_id"], "by_version": base["rec_version"]})
                    if compare_world:
                        cfresh = [l for l in lot_ids if l not in cseen]
                        compare = correct_compare_world(conn, run, cfresh, day, compare_world)
                        for l in cfresh:
                            o = compare[l]
                            _history(conn, run, bench_id, compare_world, "observation", request_id=q["request_id"],
                                     lot_id=l, lot_ids=[l], observed={l: o["physical"]}, **base,
                                     outcome={**o, "recorded_units": o["record_before"] or 0.0,
                                              "observed_units": o["physical"] or 0.0})
                            cseen[l] = o["physical"] or 0.0
                        _history(conn, run, bench_id, compare_world, "confirm", **ids, **base,
                                 outcome={"confirmed": True,
                                          "corrections": {l: compare[l]["correction"] for l in cfresh}})
                if bench_id is not None:
                    for l in dict.fromkeys([*fresh, *compare]):
                        results = {"confirm": confirm}
                        if l in ss:
                            results["shelfshift"] = ss[l]
                        if l in compare:
                            results[compare_world] = compare[l]
                        _observation_row(conn, bench_id, run, l, q["to_branch"], base["case_id"], base["rec_version"],
                                         q["request_id"], results)
        if asked == 0:
            return totals

def trips_paid_once(conn, run_id: str, world='shelfshift') -> list[str]:
    """[] when every dispatched trip was charged exactly once and no dispatched round still holds its set-aside;
    else one line per problem."""
    audits = db.rows(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:r AND world=:w', r=run_id, w=world)
    benchmark = db.one(conn, 'SELECT benchmark_id FROM benchmark_runs WHERE sim_run_id=:r', r=run_id)
    run = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=run_id)
    if not benchmark and run.get('template_run_id'):
        own = {a['action_id'] for a in audits}
        audits += [a for a in db.rows(conn, 'SELECT * FROM benchmark_transfer_audit WHERE sim_run_id=:r AND world=:w',
                                     r=run['template_run_id'], w=world) if a['action_id'] not in own]
    if audits or benchmark:
        return audited_trips(conn, run_id, world, audits)
    shipped = {(e["day"], e["payload"]["lot_id"]): float(e["payload"]["shipped"]) for e in db.rows(
        conn, "SELECT day, payload FROM world_events WHERE run_id = :r AND world = :w "
              "AND kind = 'SHORT_SHIPMENT'", r=overlay.world_events_run(conn, run_id), w=world)}
    due = sum(trip_cost(a["fixed_cost"], a["cancel_cost"], a["per_unit_cost"],
                        shipped.get((a["run_day"], a["lot_id"]), float(a["qty"])))[0]
              for a in db.rows(conn, "SELECT * FROM world_actions WHERE run_id = :r AND world = :w "
                                     "AND kind = 'TRANSFER' AND state = 'DONE' ORDER BY action_id", r=run_id, w=world))
    read, p = overlay.everywhere(conn, run_id, "outcomes")  # every series, own or the template's (05.2 D-09)
    paid = float(db.one(conn, f"SELECT coalesce(sum(transport), 0) AS t FROM outcomes WHERE {read} "
                              f"AND outcomes.world = :w", **p, w=world)["t"])
    # a round's own transfer is the action prepare wrote for it: "<case>#<round>:transfer"
    left = db.one(conn, "SELECT count(*) AS n FROM reservations s JOIN world_actions a ON a.run_id = s.run_id "
                        "AND a.world = 'shelfshift' AND a.case_id = s.case_id AND a.kind = 'TRANSFER' "
                        "AND a.state = 'DONE' AND a.action_id = s.case_id || '#' || s.round || :suffix "
                        "WHERE s.run_id = :r AND s.kind = 'SOURCE_LOT' AND s.state = 'COMMITTED'", r=run_id,
                  suffix=":transfer")["n"] if world == 'shelfshift' else 0
    return ([f"transport paid {paid:.2f} != {due:.2f} due"] if abs(paid - due) > TOLERANCE else []) + \
           ([f"{left} shipped set-aside(s) still booked"] if left else [])


def audited_trips(conn, run_id, world, audits):
    from data.versions import digest
    from .metrics import constraint_reasons
    errors, totals, identities = [], {}, set()
    actions = db.rows(conn, "SELECT * FROM world_actions WHERE run_id=:r AND world=:w AND kind='TRANSFER' "
                           "AND state='DONE' ORDER BY action_id", r=run_id, w=world)
    pending = db.one(conn, "SELECT count(*) AS n FROM world_actions a JOIN demo_runs d ON d.run_id=a.run_id "
        "WHERE a.run_id=:r AND a.world=:w AND a.kind='TRANSFER' AND a.run_day<=d.day AND a.state<>'DONE'",
        r=run_id, w=world)['n']
    if pending:
        errors.append(f'{pending} due transfer(s) lack execution evidence')
    if {a['action_id'] for a in actions} != {a['action_id'] for a in audits} or len(actions) != len(audits):
        errors.append('incomplete or duplicate per-action charge coverage')
    by_id = {a['action_id']: a for a in actions}
    prepared = {(r['case_id'], r['payload'].get('round')): r['payload'].get('rec_version') for r in db.rows(conn,
        "SELECT case_id,payload FROM activity_log WHERE run_id=:r AND type='prepared' ORDER BY seq", r=run_id)}
    for audit in audits:
        action = by_id.get(audit['action_id'])
        snap = audit['effective_input']
        if action is None:
            continue
        label = f"{audit['action_id']}: "
        try:
            round_ = snap['round']
            identity = digest('charge-', audit['sim_run_id'], world, action['action_id'], round_, audit['rec_version'])
            if audit['charge_id'] != identity or identity in identities:
                errors.append(label + 'charge identity differs or repeats')
            identities.add(identity)
            if action['case_id'] and (prepared.get((action['case_id'], round_)) != audit['rec_version'] or
                    action['action_id'] != f"{action['case_id']}#{round_}:transfer"):
                errors.append(label + 'original round/version differs')
            if audit['rec_version'] != snap['rec_version']:
                errors.append(label + 'saved version differs')
            for field in ('action_id', 'case_id', 'lot_id', 'sku', 'origin', 'dest', 'run_code', 'run_day', 'qty',
                          'fixed_cost', 'per_unit_cost', 'cancel_cost', 'new_lot_id', 'sellable_from'):
                if action[field] != snap['action'][field]:
                    errors.append(label + f'action {field} differs')
            if (audit['day'], audit['run_code'], audit['planned_units'], audit['destination']) != (
                    action['run_day'], action['run_code'], action['qty'], action['dest']):
                errors.append(label + 'execution identity differs')
            expected_shipped = min(action['qty'], snap['source']['available_before'])
            if abs(audit['shipped_units'] - expected_shipped) > 1e-8:
                errors.append(label + 'shipped amount differs from execution source')
            expected = trip_cost(action['fixed_cost'], action['cancel_cost'], action['per_unit_cost'], audit['shipped_units'])
            if any(abs(audit[field] - value) > 1e-8 for field, value in
                   zip(('transport_amount', 'handling_amount'), expected)):
                errors.append(label + 'per-action transport/handling differs from committed quote')
            reasons = constraint_reasons(snap)
            if reasons != audit['constraint_reasons']:
                errors.append(label + 'constraint result differs from recorded input')
            key = action['run_day'], action['origin'], action['sku']
            total = totals.setdefault(key, [0., 0.])
            total[0] += audit['transport_amount']
            total[1] += audit['handling_amount']
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(label + f'incomplete execution evidence: {type(exc).__name__}')
    read, params = overlay.everywhere(conn, run_id, 'outcomes')
    outcomes = {(r['day'], r['branch'], r['sku']): (r['transport'], r['handling']) for r in db.rows(conn,
        f'SELECT day,branch,sku,transport,handling FROM outcomes WHERE {read} AND outcomes.world=:w', **params, w=world)}
    for key in set(totals) | set(outcomes):
        if any(abs(a - b) > TOLERANCE for a, b in zip(totals.get(key, (0, 0)), outcomes.get(key, (0, 0)))):
            errors.append(f'{key}: audit transport/handling differs from outcomes')
    if world == 'shelfshift':
        left = db.one(conn, "SELECT count(*) AS n FROM reservations s JOIN world_actions a ON a.run_id=s.run_id "
            "AND a.world='shelfshift' AND a.case_id=s.case_id AND a.kind='TRANSFER' AND a.state='DONE' "
            "AND a.action_id=s.case_id || '#' || s.round || :suffix WHERE s.run_id=:r "
            "AND s.kind='SOURCE_LOT' AND s.state='COMMITTED'", r=run_id, suffix=':transfer')['n']
        if left:
            errors.append(f'{left} shipped set-aside(s) still booked')
    return errors


def closing_balance(conn, run, worlds=BENCH_WORLDS):
    """Physical closing stock; moved lots not yet sellable belong only to transit."""
    from .metrics import WORLD_KEYS, cents
    result = {}
    for world in worlds:
        amounts = db.one(conn, 'SELECT coalesce(sum(t.qty) FILTER (WHERE a.sellable_from>:d),0) AS transit, '
            'coalesce(sum(t.qty) FILTER (WHERE a.sellable_from IS NULL OR a.sellable_from<=:d),0) AS hand '
            'FROM truth_lots t JOIN lots l USING(run_id,world,lot_id) LEFT JOIN world_actions a '
            "ON a.run_id=l.run_id AND a.world=l.world AND a.new_lot_id=l.lot_id AND a.kind='TRANSFER' "
            'WHERE t.run_id=:r AND t.world=:w', r=run['run_id'], w=world, d=run['day'])
        open_transfers = db.one(conn, "SELECT count(*) AS n FROM world_actions WHERE run_id=:r AND world=:w "
            "AND kind='TRANSFER' AND state IN ('PLANNED','DISPATCHED')", r=run['run_id'], w=world)['n']
        costs = db.one(conn, 'SELECT coalesce(sum(transport+handling+disposal),0) AS n FROM outcomes '
                             'WHERE run_id=:r AND world=:w', r=run['run_id'], w=world)['n']
        result[WORLD_KEYS[world]] = dict(on_hand_units=amounts['hand'], in_transit_units=amounts['transit'],
                                       open_transfers=open_transfers, costs_charged=cents(costs))
    return result


def validate(conn, run, ref, truth, worlds=BENCH_WORLDS):
    """All gates execute before a receipt is issued. Failure marks only this unpublished run."""
    from . import benchmark, metrics
    row = benchmark._locked(conn, run['run_id'])
    if row['status'] != 'RUNNING' or row['published_at'] is not None:
        raise ValueError('validation requires RUNNING unpublished benchmark')
    errors = benchmark.domain_errors(conn, row)
    if tuple(worlds) != BENCH_WORLDS:
        errors.append('all four worlds in the expected order required')
    if run['day'] != 6:
        errors.append('closing day must be 6')
    for check in (lambda: sandbox.network_check(conn, run, ref, truth, worlds=worlds),
                  lambda: sandbox.limits_check(conn, run, ref)):
        try:
            check()
        except sandbox.TemplateError as exc:
            errors.append(str(exc))
    for world in worlds:
        errors.extend(f'{world}: {error}' for error in trips_paid_once(conn, run['run_id'], world=world))
        broken = metrics.limit_breaks(conn, run, ref, world)
        if broken:
            errors.append(f'{world}: {broken} execution/resource limit break(s)')
    try:
        moved = closing_moved(conn, run, worlds)
        for scenario, stock in moved.items():
            if (min(stock.values()) < -stock['tolerance_units'] or stock['on_hand_units'] + stock['in_transit_units'] +
                    stock['wasted_units'] > stock['shipped_units'] + stock['tolerance_units']):
                errors.append(f'{scenario}: inconsistent moved-stock closing balance')
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(f'moved-stock evidence: {exc}')
    if errors:
        benchmark.fail(conn, run['run_id'], errors)
    else:
        receipt = dict(gates='passed', checked_records=benchmark.record_digest(conn, run['run_id']),
                       closing=closing_balance(conn, run, worlds), checked_worlds=list(worlds),
                       physical=physical_digest(conn, run['run_id']), moved_closing=moved)
        db.run(conn, 'UPDATE benchmark_runs SET validation_receipt=CAST(:v AS jsonb) WHERE benchmark_id=:b',
               b=run['run_id'], v=db.js(receipt))
    return errors


def lot_day_evidence(conn, run, world, by_lot):
    """Bind complete actual per-lot captures to every recorded sales/waste cell."""
    import math
    from . import refdata
    ref = refdata.get(run['dataset_version'])
    captures = db.rows(conn, "SELECT day,payload FROM world_events WHERE run_id=:r AND world=:w "
                       "AND kind='BENCHMARK_LOT_DAY' ORDER BY day,seq", r=run['run_id'], w=world)
    expected = {(d, sku) for d in range(run['day'] + 1) for sku in ref.products}
    if (len(captures) != len(expected) or
            {(c['day'], c['payload'].get('sku')) for c in captures} != expected):
        raise ValueError('incomplete or duplicate lot-day capture coverage')
    outcomes = {(o['day'], o['branch'], o['sku']): o for o in db.rows(conn,
        'SELECT day,branch,sku,sold,wasted FROM outcomes WHERE run_id=:r AND world=:w', r=run['run_id'], w=world)}
    totals, moved, expired_ids = {}, {}, set()
    for capture in captures:
        day, payload = capture['day'], capture['payload']
        sku = payload['sku']
        if payload.get('schema_version') != 1 or payload.get('branches') != sorted(ref.branches):
            raise ValueError('incomplete lot-day demand-branch capture')
        for field, kind in (('sold', 'TRANSFER_SOLD'), ('expired', 'TRANSFER_EXPIRED')):
            seen = set()
            for entry in payload[field]:
                lot_id, branch = entry['lot_id'], entry['branch']
                value = entry['sold' if field == 'sold' else 'wasted']
                timing = payload['timing'][lot_id]
                if (lot_id in seen or branch not in ref.branches or timing['branch'] != branch or
                        not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or
                        (field == 'sold' and not timing['sellable_from'] <= day <= timing['expiry_day']) or
                        (field == 'expired' and (day < timing['expiry_day'] or lot_id in expired_ids))):
                    raise ValueError('invalid actual lot-day sale/expiry capture')
                seen.add(lot_id)
                if field == 'expired':
                    expired_ids.add(lot_id)
                key = day, branch, sku
                total = totals.setdefault(key, dict(sold=0., wasted=0.))
                total['sold' if field == 'sold' else 'wasted'] += value
                if lot_id in by_lot:
                    action = by_lot[lot_id]
                    if (branch != action['dest'] or sku != action['sku'] or
                            timing['expiry_day'] != action['effective_input']['source']['expiry_day'] or
                            (day == action['run_day'] and timing['sellable_from'] != action['sellable_from']) or
                            (field == 'sold' and day < action['sellable_from'])):
                        raise ValueError('moved-lot capture timing/identity differs from executed action')
                    moved[kind, day, lot_id] = dict(entry, sku=sku, action_id=action['action_id'])
    for key in set(totals) | set(outcomes):
        if (key[0], key[2]) not in expected or any(abs(totals.get(key, {}).get(f, 0) -
                outcomes.get(key, {}).get(f, 0)) > 1e-8 for f in ('sold', 'wasted')):
            raise ValueError('actual lot-day sale/expiry capture differs from outcomes')
    return moved


def closing_moved(conn, run, worlds=BENCH_WORLDS):
    """Conserve each moved lot from actual sales, expiry, onward shipping and closing stock.

    Physical quantities are persisted to six decimals after every day. Seven daily
    roundings can accumulate at most 3.5e-6 units; this unit tolerance is separate
    from monetary reconciliation and never changes a recorded sale or charge.
    """
    from .metrics import WORLD_KEYS, UNIT_TOLERANCE
    import math
    unit_tolerance = UNIT_TOLERANCE
    result = {}
    for world in worlds:
        moves = {a['action_id']: a for a in db.rows(conn, 'SELECT a.*,b.shipped_units,b.effective_input '
            'FROM world_actions a JOIN benchmark_transfer_audit b ON b.sim_run_id=a.run_id AND b.world=a.world '
            'AND b.action_id=a.action_id WHERE a.run_id=:r AND a.world=:w', r=run['run_id'], w=world)}
        by_lot = {a['new_lot_id']: a for a in moves.values()}
        if len(by_lot) != len(moves):
            raise ValueError('ambiguous moved-lot identity')
        captured_moved = lot_day_evidence(conn, run, world, by_lot)
        physical = {l['lot_id']: l for l in db.rows(conn,
            'SELECT l.lot_id,l.branch,l.sku,t.qty FROM truth_lots t JOIN lots l USING(run_id,world,lot_id) '
            'WHERE t.run_id=:r AND t.world=:w', r=run['run_id'], w=world)}
        events = db.rows(conn, "SELECT day,kind,payload FROM world_events WHERE run_id=:r AND world=:w "
                         "AND kind IN ('TRANSFER_EXPIRED','TRANSFER_SOLD') ORDER BY day,seq",
                         r=run['run_id'], w=world)
        sales, waste, seen, actual_moved = {}, {}, set(), {}
        for event in events:
            payload = event['payload']
            action = moves.get(payload.get('action_id'))
            is_sale = event['kind'] == 'TRANSFER_SOLD'
            key = (event['kind'], payload.get('lot_id'), event['day'] if is_sale else None)
            units = payload.get('sold' if is_sale else 'wasted')
            if (not action or action['new_lot_id'] != payload.get('lot_id') or key in seen or
                    payload.get('branch') != action['dest'] or payload.get('sku') != action['sku'] or
                    not action['run_day'] <= event['day'] <= run['day'] or
                    not isinstance(units, (int, float)) or not math.isfinite(units) or units < 0 or
                    (is_sale and event['day'] < action['sellable_from']) or
                    (not is_sale and event['day'] < action['effective_input']['source']['expiry_day'])):
                raise ValueError('missing, duplicate or ambiguous moved-lot sale/expiry identity')
            seen.add(key)
            actual_moved[event['kind'], event['day'], payload['lot_id']] = payload
            target = sales if is_sale else waste
            target[action['new_lot_id']] = target.get(action['new_lot_id'], 0) + units
        if actual_moved != captured_moved:
            raise ValueError('moved sale/expiry events differ from actual lot-day capture')
        hand = transit = 0.
        for lot_id, action in by_lot.items():
            closing = physical.get(lot_id)
            qty = closing['qty'] if closing else 0.
            if closing and (closing['branch'], closing['sku']) != (action['dest'], action['sku']):
                raise ValueError('physical moved-lot identity differs')
            onward = sum(a['shipped_units'] for a in moves.values() if a['lot_id'] == lot_id)
            accounted = sales.get(lot_id, 0) + waste.get(lot_id, 0) + qty + onward
            if not math.isfinite(qty) or qty < 0 or abs(accounted - action['shipped_units']) > unit_tolerance:
                raise ValueError('incomplete moved-lot conservation: sales/expiry/closing/onward differ from shipped')
            if action['sellable_from'] > run['day']:
                transit += qty
            else:
                hand += qty
        result[WORLD_KEYS[world]] = dict(shipped_units=sum(a['shipped_units'] for a in moves.values()),
            on_hand_units=hand, in_transit_units=transit, sold_units=sum(sales.values()), wasted_units=sum(waste.values()),
            tolerance_units=unit_tolerance * max(1, len(moves)))
    return result


def physical_digest(conn, run_id):
    from data.versions import digest
    return digest('physical-', db.rows(conn, 'SELECT * FROM truth_lots WHERE run_id=:r ORDER BY world,lot_id', r=run_id))


def publication_check(conn, benchmark_row):
    """Explicit driver callback, invoked by the publisher while holding its finalization lock."""
    if physical_digest(conn, benchmark_row['sim_run_id']) != benchmark_row['validation_receipt'].get('physical'):
        return ['physical closing inputs differ from validation receipt']
    run = db.one(conn, 'SELECT * FROM demo_runs WHERE run_id=:r', r=benchmark_row['sim_run_id'])
    if closing_moved(conn, run) != benchmark_row['validation_receipt'].get('moved_closing'):
        return ['moved-stock closing inputs differ from validation receipt']
    return []


# ------------------------------------------------------------------------------------------- the job (08-15)
#
# Contract: 04-API-CONTRACT "## Reports (08)" "Job command and output". Stdout carries exactly one JSON report line on
# every exit path, with counts, ids and version labels only: the image digest is reported as a boolean, and every
# free-text reason passes `_safe`, so no project, bucket, account, URL or digest value is printed (T-08-27).

REPORT_VERSION = 1
REPORT_KEYS = ("report_version", "benchmark_id", "status", "days_done", "completed_world_days", "versions",
               "dataset_version", "git_commit", "image_digest_recorded", "elapsed_s", "left_open_by_reason",
               "retries_by_reason", "validation", "reasons")
TRIAL_KEYS = ("minutes", "slice", "setup_s", "setup_complete", "measured_s", "cleanup_s", "stopped", "stopped_in",
              "partial", "per_day", "stages", "cases_evaluated", "blocked_by_reason", "rows_per_day", "peak_rss_mib",
              "worker_peak_rss_mib", "setup")
STAGES = ("start", "checks", "commits", "before_worlds", "worlds", "after_worlds", "closing", "checkpoint")
EXPIRY_BLOCK = "commitment_blocked:required_check:" + signals.EXPIRY_KINDS[0]  # D-34: a missing expiry check
_clock = time.monotonic  # the trial's deadline clock (a test replaces it)
_INSERT = None  # compiled on first use
_PRIVATE = None


class Refused(Exception):
    """Refused before any work (exit 2); `context` fills the report's identity keys."""

    def __init__(self, reasons, **context):
        self.reasons, self.context = [reasons] if isinstance(reasons, str) else list(reasons), context
        super().__init__("; ".join(self.reasons))


class TrialTimeUp(Exception):
    """The trial's time budget ended: raised by the deadline listener before the next SQL statement runs (D-27).
    The listener disarms itself first, so the rollback and the cleanup that follow are never refused."""


def _safe(text) -> str:
    """Free text for stdout with URLs, e-mail-like and digest-like values replaced (T-08-27)."""
    import re
    global _PRIVATE
    if _PRIVATE is None:
        _PRIVATE = re.compile(r"[a-z][a-z0-9+.-]*://\S*|\S*@\S*|sha256:[0-9a-f]+|\b[0-9a-f]{40,}\b|"
                              r"\b[a-z][a-z0-9-]*:[a-z0-9-]+:[a-z0-9-]+\b", re.I)
    return _PRIVATE.sub("<private>", str(text))[:2000]


def _report(**fields) -> dict:
    from . import benchmark
    report = {"report_version": REPORT_VERSION, "benchmark_id": None, "status": None, "days_done": 0,
              "completed_world_days": 0, "versions": {k: None for k in benchmark.VERSION_KEYS},
              "dataset_version": None, "git_commit": None, "image_digest_recorded": False, "elapsed_s": None,
              "left_open_by_reason": {}, "retries_by_reason": {}, "validation": {"status": "not_run", "errors": []},
              "reasons": []}
    report.update(fields)
    return report


def _installed(dataset_version: str) -> bool:
    from . import publicref, refdata
    return publicref.kind_of(dataset_version) is not None or bool(refdata._read(
        "SELECT 1 FROM ref_data WHERE dataset_version = :v LIMIT 1", v=dataset_version))


def _versions(dataset_version: str, ref) -> dict:
    from . import benchmark
    try:
        return benchmark.app_versions({"dataset_version": dataset_version}, ref)
    except ValueError:
        return {k: None for k in benchmark.VERSION_KEYS}


def _eligible(dataset_version: str, *, trial: bool):
    """The installed reference and every reason it can't be evaluated (D-02, D-28, D-36). A full run refuses on any
    reason; a trial refuses only when no settings object can be built, and records the rest (it never publishes)."""
    from . import benchmark, refdata
    if not isinstance(dataset_version, str) or not dataset_version.strip() or not _installed(dataset_version):
        raise Refused(f"dataset {dataset_version} is not installed", dataset_version=dataset_version)
    ref = refdata.get(dataset_version)
    context = dict(dataset_version=dataset_version, versions=_versions(dataset_version, ref))
    reasons = benchmark.refusals(ref, trial=trial)
    try:
        benchmark.settings_for(ref, trial=trial)
    except ValueError as error:
        if trial:
            raise Refused(str(error), **context) from None
        reasons.append(str(error))
    if reasons and not trial:
        raise Refused(reasons, **context)
    return ref, reasons, context


def _progress(bench_id: str) -> dict:
    """The report's identity and checkpoint keys, read from the saved rows."""
    from . import benchmark
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM benchmark_runs WHERE benchmark_id = :b", b=bench_id)
        days = db.rows(conn, "SELECT day, stats FROM benchmark_days WHERE benchmark_id = :b ORDER BY day", b=bench_id)
    left, retries = {}, {}
    for d in days:
        for reason, ids in d["stats"].get("left_open", {}).items():
            left[reason] = left.get(reason, 0) + (len(ids) if isinstance(ids, list) else int(ids))
        for code, n in d["stats"].get("retry_codes", {}).items():
            retries[code] = retries.get(code, 0) + int(n)
    worlds = sum(sum(w.get("completed") is True for w in d["stats"].get("worlds", {}).values()) for d in days)
    return dict(benchmark_id=bench_id, status=row["status"], days_done=len(days), completed_world_days=worlds,
                versions=benchmark.versions_of(row), dataset_version=row["dataset_version"],
                git_commit=row["git_commit"], image_digest_recorded=bool(row["image_digest"]),
                left_open_by_reason=left, retries_by_reason=retries)


def _fail(bench_id: str, reasons) -> None:
    from . import benchmark
    with db.tx() as conn:
        benchmark.fail(conn, bench_id, reasons)


@contextlib.contextmanager
def _engine_worker():
    """The engine worker for the job's lifetime, as the app's lifespan and casebench start it (OPS-06)."""
    from . import config, worker
    started = config.ENGINE_WORKER and worker.pid() is None
    if started:
        worker.start()
    try:
        yield
    finally:
        if started:
            worker.stop()


def _finish(bench_id: str, *, git_commit: str, image_digest: str) -> tuple[int, dict]:
    """Days through 6 (run_days, D-21), then validation, the saved result and publication in one locked transaction
    (D-02). A forecast gap or any other stop fails the run, which keeps its id for a resume; nothing is published."""
    from . import benchmark, evalref, metrics, refdata
    try:
        run_days(bench_id, through=6, image_digest=image_digest, git_commit=git_commit)
    except evalref.ForecastGap as gap:
        _fail(bench_id, [f"stopped: {gap}"])
        return 1, _report(**_progress(bench_id), reasons=[_safe(f"stopped: {gap}")])
    except ValueError as error:
        if "already claimed" in str(error):
            raise Refused(str(error), **_progress(bench_id)) from None
        _fail(bench_id, [f"stopped: {error}"])
        return 1, _report(**_progress(bench_id), reasons=[_safe(f"stopped: {error}")])
    except Exception as error:  # noqa: BLE001 - the run is marked FAILED (resumable) and the report says why
        reason = f"stopped: {type(error).__name__}: {error}"
        try:
            _fail(bench_id, [reason])
        except Exception:  # noqa: BLE001 - the database itself may be the failure; the report still prints
            pass
        return 1, _report(**_progress_or_id(bench_id), reasons=[_safe(reason)])
    with db.tx() as conn:
        dv = db.one(conn, "SELECT dataset_version FROM benchmark_runs WHERE benchmark_id = :b",
                    b=bench_id)["dataset_version"]
        ref, truth = refdata.get(dv), refdata.truth(dv)
        run = db.lock_run(conn, bench_id, mode="UPDATE", statuses=("INITIALIZING",))
        errors = validate(conn, run, ref, truth)
        if not errors:
            try:
                rows = metrics.branch_day_rows(conn, bench_id, BENCH_WORLDS, ref)
                payload = metrics.summary(conn, run, ref, rows, closing_balance(conn, run))
                benchmark.save_result(conn, bench_id, payload, rows, final_check=publication_check)
                benchmark.publish(conn, bench_id, final_check=publication_check)
            except ValueError as error:  # save_result and publish mark the run FAILED themselves; a summary error not
                errors = [str(error)]
                if db.one(conn, "SELECT status FROM benchmark_runs WHERE benchmark_id = :b",
                          b=bench_id)["status"] != "FAILED":
                    benchmark.fail(conn, bench_id, errors)
    report = _report(**_progress(bench_id))
    if errors:
        report.update(validation={"status": "failed", "errors": [_safe(e) for e in errors]},
                      reasons=["validation failed: nothing published"])
        return 1, report
    report["validation"] = {"status": "passed", "errors": []}
    return (0 if report["status"] == "PUBLISHED" else 1), report


def _progress_or_id(bench_id: str) -> dict:
    try:
        return _progress(bench_id)
    except Exception:  # noqa: BLE001
        return {"benchmark_id": bench_id, "status": "FAILED"}


def start(dataset_version: str, *, git_commit: str, image_digest: str) -> tuple[int, dict]:
    """A full run: refusals, the image digest, a new benchmark id, days 0..6, validation and publication (D-02)."""
    from . import benchmark, evaluate, evalref
    ref, _, context = _eligible(dataset_version, trial=False)
    if not image_digest:
        raise Refused("IMAGE_DIGEST is not set: a full run records the image it ran on (D-21)", **context)
    identity = dict(context, git_commit=git_commit)
    bench_id = benchmark.new_id()
    with _engine_worker():
        try:
            # the day-0 start and eager morning run inside create_run: under the D-08 rule and the run's own
            # engine-cache namespace, like every later day (08-14 open item)
            with evalref.forecast_rule(dataset_version), evaluate.cache_namespace(bench_id):
                with db.tx() as conn:
                    benchmark.create_run(conn, dataset_version, trial=False, git_commit=git_commit,
                                         image_digest=image_digest, worlds=BENCH_WORLDS, benchmark_id=bench_id)
        except evalref.ForecastGap as gap:
            return 1, _report(**identity, status="FAILED", reasons=[_safe(f"stopped before day 0: {gap}")])
        except Exception as error:  # noqa: BLE001 - nothing was saved: the setup transaction rolled back
            return 1, _report(**identity, status="FAILED",
                              reasons=[_safe(f"stopped before day 0: {type(error).__name__}: {error}")])
        return _finish(bench_id, git_commit=git_commit, image_digest=image_digest)


def resume(bench_id: str, *, git_commit: str, image_digest: str) -> tuple[int, dict]:
    """Continue a stopped run from its last completed day, on the same image, commit and inputs only (D-21); any other
    case is refused before work, with the fresh-run instruction."""
    from . import benchmark, refdata
    if not isinstance(bench_id, str) or not benchmark.BENCHMARK_ID.fullmatch(bench_id):
        raise Refused("a benchmark id is bm- and 12 lowercase hex")
    with db.tx() as conn:
        row = db.one(conn, "SELECT * FROM benchmark_runs WHERE benchmark_id = :b", b=bench_id)
    if row is None:
        raise Refused("unknown benchmark", benchmark_id=bench_id)
    context = _progress(bench_id)
    dv = row["dataset_version"]
    if not _installed(dv):
        raise Refused(f"dataset {dv} is not installed", **context)
    ref = refdata.get(dv)
    try:
        versions, digest_ = benchmark.app_versions({"dataset_version": dv}, ref), benchmark.settings_digest(ref)
    except ValueError as error:
        raise Refused(f"the installed inputs refuse an evaluation: {error}", **context) from None
    try:
        with db.tx() as conn:
            locked = benchmark._locked(conn, bench_id)
            allowed, why = benchmark.can_resume(locked, image_digest, git_commit, versions, digest_)
            if not allowed:
                raise Refused(why, **context)
            benchmark._resume_checkpoint(conn, locked)
    except ValueError as error:
        raise Refused(str(error), **context) from None
    with _engine_worker():
        return _finish(bench_id, git_commit=git_commit, image_digest=image_digest)


# ------------------------------------------------------------------------------------------- the trial (D-27, D-34)

def _counters(**extra) -> dict:
    return {"elapsed_s": 0.0, "db_s": 0.0, "statements": 0, "rows": 0, "rows_by_table": {}, "stores_screened": 0,
            "lots_screened": 0, "cases_opened": 0, "decisions": 0, "confirmations": 0, "commits": 0,
            "cases_evaluated": 0, **extra}


def _stage_counters() -> dict:
    return {"elapsed_s": 0.0, "db_s": 0.0, "statements": 0, "rows": 0}


class _Meter:
    """The trial's measurement, kept in process memory outside every transaction, so a day the deadline stops still
    reports what it did before its rollback. One SQL listener counts statements, database time and inserted rows per
    stage and per day, and enforces the deadline before each statement (D-27)."""

    def __init__(self, deadline: float):
        import threading
        self.deadline, self.armed, self.thread = deadline, False, threading.get_ident()
        self.setup = _counters(complete=False)
        self.days: list[dict] = []
        self.current, self.stage_name = self.setup, None
        self.stages = {name: _stage_counters() for name in STAGES}
        self.blocked: dict = {}
        self.left_open: dict = {}
        self.retries: dict = {}
        self.screened: set = set()
        self.tripped_in = None
        self._t0 = None

    def where(self) -> dict:
        if self.current is self.setup:
            return {"day": None, "stage": "setup"}
        return {"day": self.current["day"], "stage": self.stage_name or "claim"}

    def before(self, conn, cursor, statement, parameters, context, executemany):
        import threading
        if not self.armed or threading.get_ident() != self.thread:
            return
        if _clock() >= self.deadline:
            self.armed = False  # disabled before anything rolls back: the rollback and cleanup are never refused
            self.tripped_in = self.where()
            raise TrialTimeUp(f"trial time budget ended in {self.tripped_in}")
        self._t0 = time.perf_counter()

    def after(self, conn, cursor, statement, parameters, context, executemany):
        import re
        import threading
        global _INSERT
        if not self.armed or self._t0 is None or threading.get_ident() != self.thread:
            return
        spent, self._t0 = time.perf_counter() - self._t0, None
        if _INSERT is None:
            _INSERT = re.compile(r"\s*INSERT\s+INTO\s+([a-z_]+)", re.I)
        found = _INSERT.match(statement)
        table, rows = (found.group(1).lower(), max(cursor.rowcount or 0, 0)) if found else (None, 0)
        targets = [self.current]
        if self.stage_name is not None:
            targets += [self.current["stages"][self.stage_name], self.stages[self.stage_name]]
        for c in targets:
            c["statements"] += 1
            c["db_s"] += spent
            c["rows"] += rows
        if table:
            by = self.current["rows_by_table"]
            by[table] = by.get(table, 0) + rows
            if table == "cases":
                self.current["cases_opened"] += rows
            elif table == "recommendations":
                self.current["decisions"] += rows

    @contextlib.contextmanager
    def stage(self, name: str):
        day, previous, started = self.current, self.stage_name, time.perf_counter()
        self.stage_name = name
        try:
            yield
        finally:
            spent = time.perf_counter() - started
            day["stages"][name]["elapsed_s"] += spent
            self.stages[name]["elapsed_s"] += spent
            self.stage_name = previous

    def open_day(self, day: int) -> dict:
        self.current = _counters(day=day, partial=False, stages={name: _stage_counters() for name in STAGES})
        self.screened = set()
        self.days.append(self.current)
        return self.current

    def decided(self, reason, units: int, codes: dict) -> None:
        self.current["cases_evaluated"] += 1
        if reason is None:
            self.current["commits"] += 1
        else:
            self.left_open[reason] = self.left_open.get(reason, 0) + 1
            key = EXPIRY_BLOCK if EXPIRY_BLOCK in reason.split("+") else reason
            entry = self.blocked.setdefault(key, {"cases": 0, "transfer_units": 0})
            entry["cases"] += 1
            entry["transfer_units"] += int(units)
        for code, n in codes.items():
            self.retries[code] = self.retries.get(code, 0) + n


@contextlib.contextmanager
def _instrumented(meter: _Meter, stores):
    """Trial only: count lots and stores screened, confirmations and each commit attempt's outcome, and restrict the
    screen and the store rule's candidates to the slice's stores (`--stores`). Every wrapper calls the real function;
    all are restored on exit."""
    from . import evalcommit
    real = (cases.screen_candidates, advance.baseline_candidates, signals.confirm, evalcommit._commit_one)

    def screen_candidates(conn, run):
        lots = real[0](conn, run)
        if stores is not None:
            lots = [lot for lot in lots if lot["branch"] in stores]
        meter.current["lots_screened"] += len(lots)
        meter.screened.update(lot["branch"] for lot in lots)
        meter.current["stores_screened"] = len(meter.screened)
        return lots

    def baseline_candidates(conn, run, world="baseline_rule"):
        lots = real[1](conn, run, world=world)
        return lots if stores is None else [lot for lot in lots if lot["branch"] in stores]

    def confirm(*args, **kwargs):
        done = real[2](*args, **kwargs)
        meter.current["confirmations"] += 1
        return done

    def commit_one(conn, run, case, rec, retry_limit, keep_fast_path, out):
        units, codes = dict(out["transfer_units"]), dict(out["retry_codes"])
        reason = real[3](conn, run, case, rec, retry_limit, keep_fast_path, out)
        meter.decided(reason, out["transfer_units"].get(reason, 0) - units.get(reason, 0) if reason else 0,
                      {c: n - codes.get(c, 0) for c, n in out["retry_codes"].items() if n != codes.get(c, 0)})
        return reason

    cases.screen_candidates, advance.baseline_candidates = screen_candidates, baseline_candidates
    signals.confirm, evalcommit._commit_one = confirm, commit_one
    try:
        yield
    finally:
        cases.screen_candidates, advance.baseline_candidates, signals.confirm, evalcommit._commit_one = real


def _trial_claim(conn, bench_id: str):
    """The trial's own guarded checkpoint step: only its unpublished TRIAL row, under the row lock, with ordered
    checkpoints and the simulation on the last completed day. Resume and publish refuse TRIAL rows (08-12)."""
    from . import benchmark
    row = benchmark._locked(conn, bench_id)
    if row["status"] != "TRIAL" or row["trial"] is not True or row["published_at"] is not None:
        raise ValueError("the trial driver steps only its own unpublished TRIAL benchmark")
    done = [d["day"] for d in db.rows(conn, "SELECT day FROM benchmark_days WHERE benchmark_id = :b ORDER BY day",
                                      b=bench_id)]
    last = done[-1] if done else None
    if done != list(range(len(done))) or row["last_day_done"] != last:
        raise ValueError("trial checkpoints are not the ordered days from 0")
    sim = db.lock_run(conn, row["sim_run_id"], mode="UPDATE", statuses=("INITIALIZING",))
    if sim["day"] != (0 if last is None else last):
        raise ValueError("trial simulation day differs from its last checkpoint")
    return row, sim, 0 if last is None else last + 1


def _peak_mib(who) -> float:
    import resource
    import sys
    peak = resource.getrusage(who).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def _rounded(c: dict) -> dict:
    return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()}


def trial(dataset_version: str, *, minutes: int, stores: int | None, git_commit: str, image_digest: str,
          started: float) -> tuple[int, dict]:
    """The time-boxed trial (D-27, D-34): the real job on the real path until `minutes` after `started` (setup
    included, so a slow setup still reports before the task timeout), then one report. Never published: the row is
    TRIAL, and resume and publish refuse it. A day the deadline stops rolls back whole; its counters, read in process
    memory before the rollback, are reported with "partial": true. Cleanup, the rollback and saving the report are
    timed apart from the measurement (cleanup_s)."""
    import resource
    from sqlalchemy import event
    from . import benchmark, evaluate, evalref, refdata
    ref, reasons, context = _eligible(dataset_version, trial=True)
    codes = sorted(ref.branches)
    if stores is not None and not 1 <= stores <= len(codes):
        raise Refused(f"--stores is between 1 and {len(codes)} for this dataset", **context)
    chosen = None if stores is None else set(codes[:stores])
    deadline = _clock() + minutes * 60
    meter = _Meter(deadline)
    bench_id, stopped, gap, failure = benchmark.new_id(), "complete", None, None
    engine = db.engine()
    measured_from = time.perf_counter()
    event.listen(engine, "before_cursor_execute", meter.before)
    event.listen(engine, "after_cursor_execute", meter.after)
    setup_started = time.perf_counter()
    try:
        with _engine_worker(), _instrumented(meter, chosen), evalref.forecast_rule(dataset_version), \
                evaluate.cache_namespace(bench_id):
            truth = refdata.truth(dataset_version)
            if _clock() >= deadline:
                stopped, meter.tripped_in = "budget", {"day": None, "stage": "before_setup"}
            else:
                meter.armed = True
                try:
                    with db.tx() as conn:
                        benchmark.create_run(conn, dataset_version, trial=True, git_commit=git_commit,
                                             image_digest=image_digest or None, worlds=BENCH_WORLDS,
                                             benchmark_id=bench_id)
                    meter.setup["complete"] = True
                finally:
                    meter.setup["elapsed_s"] = time.perf_counter() - setup_started
                while True:
                    if _clock() >= deadline:  # a day boundary: nothing is in flight
                        stopped, meter.tripped_in = "budget", {"day": len(meter.days), "stage": "boundary"}
                        break
                    day_started = time.perf_counter()
                    counters = None
                    try:
                        with db.tx() as conn:
                            bench, run, day = _trial_claim(conn, bench_id)
                            if day > bench["last_day"]:
                                break
                            counters = meter.open_day(day)
                            benchmark_day(conn, run, bench, truth, bench["settings"], stage=meter.stage)
                    finally:
                        if counters is not None:
                            counters["elapsed_s"] = time.perf_counter() - day_started
    except TrialTimeUp:
        stopped = "budget"
        if meter.days and meter.tripped_in and meter.tripped_in["stage"] != "setup":
            meter.days[-1]["partial"] = True
    except evalref.ForecastGap as found:
        stopped, gap = "forecast_gap", found
        meter.tripped_in = meter.where()
        if meter.days:
            meter.days[-1]["partial"] = True
    except Exception as error:  # noqa: BLE001 - the stopped day rolled back; the report says why (exit 1)
        stopped, failure = "error", f"{type(error).__name__}: {error}"
        meter.tripped_in = meter.where()
        if meter.days:
            meter.days[-1]["partial"] = True
    finally:
        meter.armed = False
        event.remove(engine, "before_cursor_execute", meter.before)
        event.remove(engine, "after_cursor_execute", meter.after)
    measured = time.perf_counter() - measured_from
    cleanup_from = time.perf_counter()
    evaluated = sum(d["cases_evaluated"] for d in meter.days)
    blocked = {EXPIRY_BLOCK: meter.blocked.get(EXPIRY_BLOCK, {"cases": 0, "transfer_units": 0})}
    for reason, entry in sorted(meter.blocked.items(), key=lambda kv: (-kv[1]["cases"], kv[0])):
        blocked.setdefault(reason, entry)
    for entry in blocked.values():
        entry["pct"] = round(100.0 * entry["cases"] / evaluated, 2) if evaluated else 0.0
    per_day = []
    for d in meter.days:
        out = _rounded({k: v for k, v in d.items() if k != "stages"})
        out["stages"] = {name: _rounded(s) for name, s in d["stages"].items()}
        per_day.append(out)
    persisted = meter.setup["complete"]
    report = _report(**{**context, "git_commit": git_commit, **(_progress(bench_id) if persisted else {})})
    report.update(status="TRIAL", image_digest_recorded=bool(image_digest) and persisted,
                  left_open_by_reason=dict(meter.left_open), retries_by_reason=dict(meter.retries),
                  validation={"status": "not_run", "errors": [], "note": "a trial is never validated or published"},
                  reasons=[_safe(r) for r in reasons] + [_safe(f"stopped: {why}") for why in (gap, failure) if why])
    report.update(
        minutes=minutes,
        slice=None if stores is None else {"stores": stores, "of": len(codes), "codes": codes[:stores]},
        setup_s=round(meter.setup["elapsed_s"], 3), setup_complete=persisted,
        measured_s=round(measured, 3), cleanup_s=None, stopped=stopped, stopped_in=meter.tripped_in,
        partial=bool(meter.days and meter.days[-1]["partial"]) or (meter.tripped_in or {}).get("stage") == "setup",
        per_day=per_day, stages={name: _rounded(s) for name, s in meter.stages.items()},
        cases_evaluated=evaluated, blocked_by_reason=blocked,
        rows_per_day=[{"day": d["day"], "rows": d["rows"], "partial": d["partial"],
                       "by_table": dict(sorted(d["rows_by_table"].items()))} for d in meter.days],
        peak_rss_mib=_peak_mib(resource.RUSAGE_SELF), worker_peak_rss_mib=_peak_mib(resource.RUSAGE_CHILDREN) or None)
    report["setup"] = _rounded({k: v for k, v in meter.setup.items()})
    report["cleanup_s"] = round(time.perf_counter() - cleanup_from, 3)
    report["elapsed_s"] = round(time.perf_counter() - started, 3)
    if persisted:  # terminal report persistence, after the measurement budget (D-27)
        with db.tx() as conn:
            db.run(conn, "UPDATE benchmark_runs SET stats = coalesce(stats, '{}'::jsonb) || jsonb_build_object("
                         "'trial_report', CAST(:r AS jsonb)) WHERE benchmark_id = :b AND status = 'TRIAL' AND trial",
                   b=bench_id, r=json.dumps(report))
    return (1 if gap or failure else 0), report


# ------------------------------------------------------------------------------------------- the five seeds (08-16)
#
# RPT-05, 08-CONTEXT D-06: the careful week on each generated evaluation seed, the four gates, and for information only
# the waste-price column, the open stock decisions per day and win/tie/loss against the store rule. Every value that
# describes the retailer or the check comes from data: the seeds from reference/scenarios.json, the tie threshold and
# the waste price from api/evaluation.json, the window from the generated set's calendar.

GATES = ("units_add_up", "no_limit_broken", "no_unit_booked_twice", "every_trip_paid_once")
SEED_KEYS = ("report_version", "command", "seed", "status", "run_id", "dataset_version", "label", "forecast", "days",
             "gates", "failures", "nets", "delta", "result", "kg_wasted", "waste_info_usd", "waste_info",
             "open_per_day", "open_max", "open_mean", "left_open_by_reason", "checks", "elapsed_s")
SEED_WORLDS = advance.WORLDS  # the three app worlds; the seeds never create the comparison world (D-06, D-11)


def evaluation_seeds() -> list[int]:
    """The evaluation seeds in reference order (reference/scenarios.json `evaluation.seeds`, through data.config)."""
    from data import config as reference_config
    return [int(s) for s in reference_config.load()["scenarios"]["evaluation"]["seeds"]]


def seed_result(shelfshift_net, store_rule_net, tie_threshold) -> tuple[str, float]:
    """(win | tie | loss, the cent delta) of ShelfShift's net against the store rule's (RPT-05; research A8).

    One declared conversion: each net becomes cents once, as a Decimal of its shortest decimal string rounded half up
    (the same rule as metrics.cents), and only then are they subtracted, so binary floating point never decides a
    boundary. |delta| below the threshold is a tie; at or above it, a win or a loss by its sign. The threshold is a
    fixed setting (api/evaluation.json `tie_threshold`), never tuned after a seed."""
    from decimal import ROUND_HALF_UP, Decimal
    cent = Decimal("0.01")
    ours, rule = (Decimal(str(v)).quantize(cent, rounding=ROUND_HALF_UP) for v in (shelfshift_net, store_rule_net))
    delta = ours - rule
    if abs(delta) < Decimal(str(tie_threshold)):
        result = "tie"
    else:
        result = "win" if delta > 0 else "loss"
    return result, float(delta) or 0.0


def booked_twice(conn, run_id: str) -> list[str]:
    """[] when no unit is booked twice on ShelfShift's record (RPT-05, FLOW-09); else one line per problem: a lot whose
    live source set-asides exceed its record, a dispatched round that still holds its set-aside, a moved lot created by
    more than one transfer, or a moved lot that is both stock and still inbound."""
    problems = [f"lot {r['bucket']}: {float(r['booked']):g} set aside > {float(r['record'] or 0):g} on the record"
                for r in db.rows(conn, "SELECT s.bucket, sum(s.qty) AS booked, max(l.qty) AS record "
                                       "FROM reservations s LEFT JOIN lots l ON l.run_id = s.run_id "
                                       "AND l.world = 'shelfshift' AND l.lot_id = s.bucket WHERE s.run_id = :r "
                                       "AND s.kind = 'SOURCE_LOT' AND (s.state = 'COMMITTED' OR (s.state = 'HELD' "
                                       "AND s.expires_at > now())) GROUP BY s.bucket "
                                       "HAVING sum(s.qty) > coalesce(max(l.qty), 0) + 1e-9 ORDER BY s.bucket",
                                 r=run_id)]
    left = db.one(conn, "SELECT count(*) AS n FROM reservations s JOIN world_actions a ON a.run_id = s.run_id "
                        "AND a.world = 'shelfshift' AND a.case_id = s.case_id AND a.kind = 'TRANSFER' "
                        "AND a.state = 'DONE' AND a.action_id = s.case_id || '#' || s.round || :suffix "
                        "WHERE s.run_id = :r AND s.kind = 'SOURCE_LOT' AND s.state = 'COMMITTED'",
                  r=run_id, suffix=":transfer")["n"]
    if left:
        problems.append(f"{left} shipped set-aside(s) still booked")
    problems += [f"{r['world']}: moved lot {r['new_lot_id']} created by {r['n']} transfers" for r in db.rows(
        conn, "SELECT world, new_lot_id, count(*) AS n FROM world_actions WHERE run_id = :r AND kind = 'TRANSFER' "
              "AND state = 'DONE' AND new_lot_id IS NOT NULL GROUP BY world, new_lot_id HAVING count(*) > 1 "
              "ORDER BY world, new_lot_id", r=run_id)]
    problems += [f"{r['world']}: lot {r['lot_id']} is stock and still inbound" for r in db.rows(
        conn, "SELECT l.world, l.lot_id FROM lots l JOIN inbound i ON i.run_id = l.run_id AND i.world = l.world "
              "AND i.inbound_id = l.lot_id WHERE l.run_id = :r ORDER BY l.world, l.lot_id", r=run_id)]
    return problems


def _open_now(conn, run_id: str) -> int:
    """The stock decisions open right now: cases in OPEN, WAITING or PENDING_APPROVAL (cases.LIVE_STATES)."""
    return int(db.one(conn, "SELECT count(*) AS n FROM cases WHERE run_id = :r AND state = ANY(:s)",
                      r=run_id, s=list(cases.LIVE_STATES))["n"])


def open_summary(per_day: list[int]) -> dict:
    """{open_max, open_mean} of the open stock decisions per day: a day with none counts 0 and is in the mean."""
    return {"open_max": max(per_day, default=0), "open_mean": sum(per_day) / len(per_day) if per_day else 0.0}


def _seed_report(**fields) -> dict:
    report = {k: None for k in SEED_KEYS}
    report.update(report_version=REPORT_VERSION, command="seeds", gates={g: None for g in GATES}, failures=[])
    report.update(fields)
    return report


def seed_week(seed: int, workdir) -> dict:
    """The careful week on one evaluation seed (RPT-05, D-06): one report dict (SEED_KEYS).

    1. The seed's evaluation set (`data.seed.generate(seed, "evaluation")`: no designed lots or skips; synthetic, and
       labelled so) and the local stand-in forecast (development and tests only, labelled so) are written into
       `workdir` and installed (`refdata.install(allow_local=True)`). Nothing is read from the cloud.
    2. A TEMPLATE simulation of its own, INITIALIZING for its whole life (never handed to a workspace), with the three
       app worlds only; day 0's start and the eager case morning (`advance.day_start`, `advance.cases_morning(eager=
       True)`) run before the first checked morning.
    3. Each day 0..the set's last demo day, one transaction: the day's start (days 1..), the D-11 checked morning with
       no comparison world (`checked_morning(compare_world=None)`: answered from ShelfShift's own physical stock; its
       history is durable with no benchmark id), the open stock decisions counted, the benchmark commit loop
       (`evalcommit.commit_all` with the evaluation settings' retry limit and Keep fast path), the no-unit-booked-twice
       check, hold expiry and the due transitions, the store rule and the three world steps (`world_steps`), and the
       lifecycle. No day after the last one ever starts.
    4. The four gates on the closing state: units add up in every world (`sandbox.network_check`); no limit broken
       (`sandbox.limits_check` and `metrics.limit_breaks` per world); no unit booked twice (`booked_twice`, after each
       day's commits and at the end); every trip paid once (`trips_paid_once` per world).
    5. For information only, never a gate: each scenario's net revenue after operating costs and kg wasted
       (`metrics.branch_day_rows`), kg wasted × the evaluation.json `waste_price_info` assumption rounded to cents,
       open stock decisions per day (a day with none counts 0 and is in the mean) with max and mean, and
       win/tie/loss against the store rule (`seed_result`), ties and losses as they come out."""
    import pathlib
    import uuid
    from decimal import Decimal
    from data import forecast as F, seed as S
    from . import benchmark, bookings, evalcommit, evaluate, metrics, refdata
    started = time.perf_counter()
    leaves = json.loads(benchmark.SETTINGS_FILE.read_text())
    values = benchmark.load_settings()
    out = pathlib.Path(workdir)
    seed_json, truth_json = S.generate(seed, "evaluation")
    S.write(out, seed_json, truth_json)
    forecast = F.build(seed_json, F.LOCAL_PROVIDER)  # as `python -m data.forecast --provider local --ref DIR` writes it
    forecast["quality"] = F.score(truth_json, forecast, seed_json)
    F.check_candidate(forecast, seed_json)
    F.write_forecast(out / "forecast.json", forecast)
    dv = refdata.install(out, allow_local=True)
    ref, truth = refdata.get(dv), refdata.truth(dv)
    settings = benchmark.settings_for(ref)  # the benchmark's commit settings (retry limit, Keep fast path)
    last = int(ref.calendar["last_demo_day"])
    run_id = "wk-" + uuid.uuid4().hex[:12]
    problems = {g: [] for g in GATES}
    open_per_day, left_open = [], {}
    checks = {"asked": 0, "confirmed": 0, "refused": 0, "lots_counted": 0}
    with evaluate.cache_namespace(run_id):
        with db.tx() as conn:
            run = db.one(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) "
                               "VALUES (:r, 'TEMPLATE', :v, 'INITIALIZING', 0, 1) RETURNING *", r=run_id, v=dv)
            sandbox.init_worlds(conn, run_id, ref, truth, worlds=SEED_WORLDS)
            advance.day_start(conn, run, worlds=SEED_WORLDS)
            advance.cases_morning(conn, run, eager=True)
        for day in range(last + 1):
            with db.tx() as conn:
                run = db.lock_run(conn, run_id, mode="UPDATE", statuses=("INITIALIZING",))
                if day > 0:
                    db.log(conn, run_id, run["day"], "day_advanced", to_day=day)
                    run = db.one(conn, "UPDATE demo_runs SET day = :d, version = version + 1 WHERE run_id = :r "
                                       "RETURNING *", d=day, r=run_id)
                    advance.day_start(conn, run, worlds=SEED_WORLDS)
                    advance.cases_morning(conn, run, eager=True)
                done = checked_morning(conn, run, bench_id=None, compare_world=None)
                for key in checks:
                    checks[key] += done[key]
                open_per_day.append(_open_now(conn, run_id))
                commits = evalcommit.commit_all(conn, run, retry_limit=settings["commits"]["retry_limit"],
                                                keep_fast_path=settings["keep_fast_path"])
                for reason, ids in commits["left_open"].items():
                    left_open[reason] = left_open.get(reason, 0) + len(ids)
                problems["no_unit_booked_twice"] += [f"day {day}: {p}" for p in booked_twice(conn, run_id)]
                bookings.expire_holds(conn, run)
                advance._lifecycle_before(conn, run)
                world_steps(conn, run, truth, worlds=SEED_WORLDS)
                advance._lifecycle_after(conn, run)
        with db.tx() as conn:
            run = db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)
            try:
                sandbox.network_check(conn, run, ref, truth, worlds=SEED_WORLDS)
            except sandbox.TemplateError as error:
                problems["units_add_up"].append(str(error))
            try:
                sandbox.limits_check(conn, run, ref)
            except sandbox.TemplateError as error:
                problems["no_limit_broken"].append(str(error))
            for world in SEED_WORLDS:
                broken = metrics.limit_breaks(conn, run, ref, world)
                if broken:
                    problems["no_limit_broken"].append(f"{world}: {broken} execution/resource limit break(s)")
                problems["every_trip_paid_once"] += [f"{world}: {p}" for p in trips_paid_once(conn, run_id, world)]
            problems["no_unit_booked_twice"] += [f"end: {p}" for p in booked_twice(conn, run_id)]
            rows = metrics.branch_day_rows(conn, run_id, SEED_WORLDS, ref)
    scenarios = [s["key"] for s in metrics.SCENARIOS if s["world"] in SEED_WORLDS]
    money = metrics.money(rows)["by_scenario"]
    nets = {key: money[key] for key in scenarios}
    kg = {key: round(sum(r["wasted_kg"] for r in rows if metrics.WORLD_KEYS[r["scenario"]] == key), 6)
          for key in scenarios}
    price = leaves["waste_price_info"]
    result, delta = seed_result(nets["shelfshift"], nets["store_rule"], values["tie_threshold"])
    gates = {g: not problems[g] for g in GATES}
    return _seed_report(
        seed=seed, status="PASSED" if all(gates.values()) else "FAILED", run_id=run_id, dataset_version=dv,
        label=seed_json["dataset"]["label"],
        forecast={"provider": F.LOCAL, "label": F.PROVIDERS[F.LOCAL]},
        days=[0, last], gates=gates,
        failures=[_safe(f"{g}: {p}") for g in GATES for p in problems[g]],
        nets=nets, delta=delta, result=result, kg_wasted=kg,
        waste_info_usd={key: metrics.cents(Decimal(str(kg[key])) * Decimal(str(values["waste_price_info"])))
                        for key in scenarios},
        waste_info={"price": values["waste_price_info"], "unit": price["unit"], "type": price["type"],
                    "source": price["source"], "use": "information only, never a gate"},
        open_per_day=open_per_day, **open_summary(open_per_day),
        left_open_by_reason=dict(sorted(left_open.items())), checks=checks,
        elapsed_s=round(time.perf_counter() - started, 3))


def _emit(report: dict) -> None:
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), default=str), flush=True)


def seeds(selection: str = "all", *, started: float | None = None) -> int:
    """Every selected evaluation seed in reference order, one line each as it finishes, then the summary line; each
    seed runs on its own set and simulation, so a seed that stops with an error is reported for that seed and the
    others still run. Exit 1 when any seed failed a gate or stopped, 0 otherwise (results never decide it)."""
    import tempfile
    started = time.perf_counter() if started is None else started
    reference = evaluation_seeds()
    if selection == "all":
        chosen = reference
    else:
        try:
            wanted = {int(part) for part in selection.split(",") if part.strip()}
        except ValueError:
            raise Refused("--seeds is all, or seed numbers separated by commas") from None
        unknown = sorted(wanted - set(reference))
        if not wanted or unknown:
            raise Refused(f"--seeds names seeds outside reference/scenarios.json's evaluation seeds: {unknown}")
        chosen = [s for s in reference if s in wanted]  # always reference order, never by result
    lines = []
    with _engine_worker():
        for seed in chosen:
            seed_started = time.perf_counter()
            try:
                with tempfile.TemporaryDirectory(prefix="shelfshift-seed-") as tmp:
                    report = seed_week(seed, tmp)
            except Exception as error:  # noqa: BLE001 - reported for this seed; the next seed still runs
                report = _seed_report(seed=seed, status="ERROR",
                                      failures=[_safe(f"stopped: {type(error).__name__}: {error}")],
                                      elapsed_s=round(time.perf_counter() - seed_started, 3))
            lines.append(report)
            _emit(report)
    failed = [r["seed"] for r in lines if r["status"] != "PASSED"]
    _emit({"report_version": REPORT_VERSION, "command": "seeds", "seeds": [r["seed"] for r in lines],
           "status": "FAILED" if failed else "PASSED", "gates_failed": len(failed), "failed_seeds": failed,
           "errors": sum(r["status"] == "ERROR" for r in lines),
           "results": {k: sum(r["result"] == k for r in lines) for k in ("win", "tie", "loss")},
           "results_note": "engineering check on synthetic seeds, information only; never a gate",
           "elapsed_s": round(time.perf_counter() - started, 3)})
    return 1 if failed else 0


# ------------------------------------------------------------------------------------------- command line

def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    from . import config

    class Parser(argparse.ArgumentParser):
        def error(self, message):  # a usage error is a refusal with its own report line (exit 2)
            raise Refused(f"usage: {message}")

    started = time.perf_counter()
    report: dict | None = None
    try:
        ap = Parser(prog="python -m api.weekcheck",
                    description="The Phase 8 benchmark job (08-CONTEXT D-02, D-21) and the five-seed check (RPT-05).")
        commands = ap.add_subparsers(dest="command")  # no command: the five-seed check, as `seeds` (RPT-05)
        bench = commands.add_parser("benchmark", help="the four-world benchmark week")
        actions = bench.add_subparsers(dest="action", required=True)
        begin = actions.add_parser("start", help="a new benchmark id: days 0..6, validate, publish")
        begin.add_argument("--dataset", required=True, help="the installed dataset version (from the release record)")
        begin.add_argument("--trial", action="store_true", help="a time-boxed measurement, never published (D-27)")
        begin.add_argument("--minutes", type=int, help="trial only: the time budget in whole minutes (0 or more)")
        begin.add_argument("--stores", type=int, help="trial only: the first N stores in code order")
        again = actions.add_parser("resume", help="continue a stopped benchmark on the same build and inputs (D-21)")
        again.add_argument("benchmark_id")
        check = commands.add_parser("seeds", help="the careful week on the evaluation seeds, four gates (RPT-05)")
        check.add_argument("--seeds", default="all", help="all (default), or seed numbers from "
                                                          "reference/scenarios.json separated by commas")
        args = ap.parse_args(argv)
        if args.command in (None, "seeds"):
            return seeds(getattr(args, "seeds", "all"), started=started)  # its own lines (one per seed, a summary)
        git = os.environ.get("GIT_COMMIT") or config.GIT_COMMIT
        image = (os.environ.get("IMAGE_DIGEST") or "").strip()
        if args.action == "start":
            if not args.trial and (args.minutes is not None or args.stores is not None):
                raise Refused("--minutes and --stores are trial only: a full run covers the whole network (D-07)",
                              dataset_version=args.dataset)
            if args.trial and (args.minutes is None or args.minutes < 0):
                raise Refused("a trial needs --minutes N, a whole number of 0 or more", dataset_version=args.dataset)
            if args.trial:
                code, report = trial(args.dataset, minutes=args.minutes, stores=args.stores, git_commit=git,
                                     image_digest=image, started=started)
            else:
                code, report = start(args.dataset, git_commit=git, image_digest=image)
        else:
            code, report = resume(args.benchmark_id, git_commit=git, image_digest=image)
    except Refused as refused:
        code = 2
        report = _report(**{"status": "REFUSED", **refused.context})
        report["status"] = "REFUSED"
        report["reasons"] = [_safe(r) for r in refused.reasons]
    if report.get("elapsed_s") is None:
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":"), default=str), flush=True)
    return code


if __name__ == "__main__":
    import sys
    sys.exit(main())
