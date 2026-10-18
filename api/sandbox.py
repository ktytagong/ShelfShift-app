"""Template build, sandbox clone and reset (spec §8 Sandboxes, template and reset).

    python -m api.sandbox build [ref_dir] [--allow-local] [--run-id RUN_ID] [--no-activate]
    python -m api.sandbox activate RUN_ID                 # explicit publication; never generates briefs
    python -m api.sandbox restore RUN_ID [--first]        # undo a failed release, preserving workspaces
    python -m api.sandbox active                          # read-only ACTIVE template, no migration/install

A TEMPLATE run starts at day 0 from the seed's day-0 lots and inbound in all three worlds (record in `lots`, physical
in `truth_lots`), runs the day start, then warms up through `advance.advance_day`, the same function a visitor's
Next day calls, until its day is the seed calendar's `demo_open_day`; cases open through the ESL-02 screen every
morning from day 0 on. Before each warm-up advance the scripted autopilot sends, accepts, approves and prepares every
open case's recommendation through the same service layer a visitor uses (`workflow`), unchanged (D-14): a refusal
there is a stop-and-hand-back, never worked around (research Pitfall 13). Units must add up on physical stock in
every world, no committed storage or truck booking may exceed what exists (`limits_check`), and the D-21 checks
(`outcome_problems`:
the four reviewer cases open on that day, SUS-19's D-15 beats, strawberries only by cold chain) must pass, before it
is marked READY, then ACTIVE by default. With --no-activate the completed template stays READY for the separate
user-run brief job and number/source review; explicit activation publishes it. No lifecycle command generates.
Any failure marks it FAILED with the message naming each broken beat, and the previous ACTIVE
template stays in use. Nothing in api/ is tuned to make a beat hold: a broken beat goes back to Phase 3 (D-14). A sandbox is a clone of the ACTIVE template: every per-run row copied with INSERT … SELECT, changing only
run_id. The table and column names come from information_schema for the current schema (never from a request) and
are read once per process (04-RESEARCH Pattern 5). Sandbox creation and Reset are one transaction: the session row
then the old run row FOR UPDATE, a clone, the session pointed at it and the old sandbox RETIRED; on any failure the
caller's transaction rolls back and the session keeps its old sandbox. The 2 s reset target is a test, never raised.

Releases (06-CONTEXT D-17): a failed release never leaves new visitors on a template it built. The release picks the
run id (`--run-id`, `tpl-` then lowercase letters, digits and hyphens), so it knows the new template without reading
logs. `build` prints `previous ACTIVE: <id|none>` first; activation (step 6) retires the old ACTIVE template with
retired_at stamped, in the same transaction that makes the new one ACTIVE; right after `template <id> ACTIVE for <dv>`
it prints `retired: <id|none>`, the template this activation actually retired (`predecessor`, from the retired_at
stamp), which the release records as the previous template (06-REVIEWS R2-11). A failure after promotion (in that
lookup or while pruning) is not caught: the job exits non-zero with the new template ACTIVE. `restore <new>` then
makes the predecessor ACTIVE and <new> RETIRED in one transaction, touching only TEMPLATE rows: existing sandboxes,
including those cloned from <new> during the window, keep their rows and stay ACTIVE. A restore of a template that is
not ACTIVE (unknown, FAILED, INITIALIZING, or already restored) does nothing and exits 0, so the release runs it
after every failed or uncertain build outcome (R2-1); restore only ever goes back to an older template, and exits 1
with nothing changed when there is none. The one exception is `restore <new> --first`, which a failed first release
passes (06-REVIEW WR-06): when <new> is ACTIVE and its activation retired no template, <new> is RETIRED so that none
is ACTIVE, the state before that release, and it prints `first release undone: <new> RETIRED`; it is the only path
that retires a template without putting a predecessor back, and otherwise it is the plain restore. Every non-failing
restore outcome ends with `ACTIVE: <id|none>`, the template ACTIVE after the restore ran, which the release checks
against the template it captured (06-REVIEW CR-02). `active` prints the same `ACTIVE:` line and changes nothing; the
release's --status reads it. A database with no template table yet (a first build that failed at migrate) reads as
no template: restore does nothing and both print `ACTIVE: none`.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import time
import uuid

from data import config as refconfig

from engine.model import TRANSFER

from . import advance, buildsteps, cases, config, db, overlay, refdata, workflow
from .errors import ApiError

NOT_CLONED = {"demo_runs", "sessions", "idempotency_keys"}  # run_id columns that are not per-run state
# the four reviewer cases of spec §5: demo design ids (03-CONTEXT D-12), not retailer values
REVIEWER_TAGS = ("milk-story", "strawberry-cold-chain", "bread-keep", "milk-close-call")
EPS_UNITS = 1e-3  # the network check's tolerance: rows store stock rounded to 6 places each day
RUN_ID = re.compile(r"tpl-[a-z0-9][a-z0-9-]{0,61}")  # a template run id the release may choose (at most 66 chars)

_tables: list[str] | None = None
_columns: dict[str, list[str]] = {}


class TemplateError(RuntimeError):
    pass


def per_run_tables(conn) -> list[str]:
    """Every table in the current schema with a run_id column, except NOT_CLONED. Cached per process."""
    global _tables
    if _tables is None:
        _tables = [r["table_name"] for r in db.rows(conn, "SELECT DISTINCT table_name FROM information_schema.columns "
                                                          "WHERE table_schema = current_schema() AND column_name = 'run_id' "
                                                          "ORDER BY 1")
                   if r["table_name"] not in NOT_CLONED]
    return _tables


def columns(conn, table: str) -> list[str]:
    """A table's columns in order, from information_schema. Cached per process."""
    if table not in _columns:
        _columns[table] = [r["column_name"] for r in db.rows(conn, "SELECT column_name FROM information_schema.columns "
                                                                   "WHERE table_schema = current_schema() "
                                                                   "AND table_name = :t ORDER BY ordinal_position",
                                                             t=table)]
    return _columns[table]


def clear_cache() -> None:
    global _tables
    _tables = None
    _columns.clear()


def init_worlds(conn, run_id: str, ref: refdata.Ref, truth: dict) -> None:
    """The seed's day-0 lots (record, plus physical from truth) and its inbound, in every world. A public set's are
    copied inside the database from its shared tables, set-based, never through memory (05.2 D-08, D-11)."""
    if ref.kind == "public":
        ref.copy_worlds(conn, run_id, advance.WORLDS)
        return
    lots, deliveries = ref.lots0(), ref.inbound0()
    physical = truth.physical_of([l["lot_id"] for l in lots])
    for world in advance.WORLDS:
        for l in lots:
            advance.insert_lot(conn, run_id, world, {**l, "moved_from": None}, physical.get(l["lot_id"], l["qty"]))
        for i in deliveries:
            db.run(conn, "INSERT INTO inbound (run_id, world, inbound_id, branch, sku, qty, arrival_day, arrival_min, "
                         "expiry_day, received_seq, expiry_source, last_verified_day, tag, origin) VALUES (:r, :w, :i, "
                         ":b, :s, :q, :d, :m, :e, :seq, :es, :lv, :tag, :o)", r=run_id, w=world, i=i["inbound_id"],
                   b=i["branch"], s=i["sku"], q=i["qty"], d=i["arrival_day"], m=i["arrival_min"], e=i["expiry_day"],
                   seq=i["received_seq"], es=i["expiry_source"], lv=i["last_verified_day"], tag=i["tag"],
                   o=i["origin"])


def network_check(conn, run: dict, ref: refdata.Ref, truth: dict) -> None:
    """initial + external inbound = sold + wasted + on hand, on PHYSICAL stock, in every world (spec §6).

    Inbound counts once it has arrived: arrival_day <= the run day, because the day start has already moved the
    run day's deliveries into lots (D-21). A public set's seeded units are summed in SQL, one product at a time."""
    seeded = {}
    for sku in sorted(ref.products):
        if ref.kind == "public":
            seeded[sku] = truth.units0(sku, run["day"])
            continue
        lots = ref.lots0(sku=sku)
        arrived = [i for i in ref.inbound0(sku=sku) if i["arrival_day"] <= run["day"]]
        physical = truth.physical_of([l["lot_id"] for l in lots] + [i["inbound_id"] for i in arrived])
        seeded[sku] = (sum(physical.get(l["lot_id"], l["qty"]) for l in lots),
                       sum(physical.get(i["inbound_id"], i["qty"]) for i in arrived))
    for world in advance.WORLDS:
        for sku in sorted(ref.products):
            initial, inbound = seeded[sku]
            o = db.one(conn, "SELECT coalesce(sum(sold), 0) AS sold, coalesce(sum(wasted), 0) AS wasted FROM outcomes "
                             "WHERE run_id = :r AND world = :w AND sku = :s", r=run["run_id"], w=world, s=sku)
            on_hand = db.one(conn, "SELECT coalesce(sum(t.qty), 0) AS q FROM truth_lots t JOIN lots l "
                                   "USING (run_id, world, lot_id) WHERE t.run_id = :r AND t.world = :w AND l.sku = :s",
                             r=run["run_id"], w=world, s=sku)["q"]
            if abs(initial + inbound - (o["sold"] + o["wasted"] + on_hand)) > EPS_UNITS:
                raise TemplateError(f"units do not add up in {world} for {sku} on day {run['day']}: {initial:.3f} + "
                                    f"{inbound:.3f} != {o['sold']:.3f} + {o['wasted']:.3f} + {on_hand:.3f}")


def autopilot(conn, run: dict) -> list[str]:
    """The scripted warm-up: take ownership as the source store, send, accept (a transfer), approve and prepare every
    OPEN case's recommendation, in case id order, through the service layer, as the case's own managers would (only
    the owner sends: 06.1 D-54, D-67). Its accept and approve name the plan (07 review C) and acknowledge the
    version's Advisory facts, logged on the warm-up's own decisions (07-CONTEXT D-12, D-26; research F-11). Nothing is
    tweaked to make it pass: an ApiError (a Required check or a lot hold's commitment_blocked included) becomes a
    TemplateError naming the case (D-14; research Pitfall 13). Returns the case ids it acted on."""
    from . import ownership  # imported here so the 06.1 change stays inside this function (build/deploy merge)
    from . import commitment  # Phase 7 (07-09), imported here for the same reason
    acted = []
    for case in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND state = 'OPEN' ORDER BY case_id",
                        r=run["run_id"]):
        rec = cases.active_rec(conn, case)
        if rec is None:  # no recommendation (missing data): nothing to approve
            continue
        plan, cid, v = cases.parse_plan(rec["result"]["recommended"]), case["case_id"], case["active_rec_version"]
        ack = commitment.expected_ack(rec, plan, commitment.checks_in_force(case, rec))
        src = {"account": case["source_branch"], "role": "manager", "branch": case["source_branch"],
               "session_id": "warmup"}
        try:
            ownership.take_ownership(conn, run, src, cid)
            workflow.send(conn, run, src, cid, v)
            if plan.kind == TRANSFER:
                workflow.decide(conn, run, {"account": plan.dest, "role": "manager", "branch": plan.dest,
                                            "session_id": "warmup"}, cid, v, "accept", None, ack, plan.key)
            if workflow.decide(conn, run, src, cid, v, "approve", None, ack, plan.key)["state"] == "APPROVED":
                workflow.prepare(conn, run, src, cid)
        except ApiError as exc:
            extra = f" {exc.extra}" if exc.extra else ""
            raise TemplateError(f"warm-up could not approve {cid} ({case['tag'] or case['lot_id']}, {plan.key}) "
                                f"on day {run['day']}: {exc.code}: {exc.message}{extra}") from None
        acted.append(cid)
    return acted


def limits_check(conn, run: dict, ref: refdata.Ref) -> None:
    """No plan broke a limit: committed or done STORAGE never exceeds the destination zone's capacity that day, and
    TRUCK never exceeds the run's spare totes (spec §8 template step 4)."""
    problems = []
    for r in db.rows(conn, "SELECT kind, bucket, day, sum(qty) AS q FROM reservations WHERE run_id = :r AND kind IN "
                           "('STORAGE', 'TRUCK') AND state IN ('COMMITTED', 'DONE') GROUP BY kind, bucket, day "
                           "ORDER BY kind, bucket, day", r=run["run_id"]):
        a, _, b = r["bucket"].partition("|")
        if r["kind"] == "STORAGE":
            cap = ref.free_totes(a, b, r["day"])
        else:
            trip = ref.run_on(a, int(b))
            cap = trip.spare_totes if trip else None
        if cap is None or r["q"] > cap + 1e-9:
            problems.append(f"{r['kind']} {r['bucket']} day {r['day']} booked {r['q']:g} > {cap}")
    if problems:
        raise TemplateError("limits broken: " + "; ".join(problems))


def _run(conn, run_id: str) -> dict:
    return db.one(conn, "SELECT * FROM demo_runs WHERE run_id = :r", r=run_id)


def phantom_lot() -> tuple[str, str]:
    """(lot_id, branch) of the designed lot whose options mark it phantom, from reference/scenarios.json."""
    designed = refconfig.load()["scenarios"]["demo"]["designed_lots"]
    found = [(lot_id, d["branch"]) for lot_id, d in sorted(designed.items()) if d.get("options", {}).get("phantom")]
    if len(found) != 1:
        raise TemplateError(f"reference/scenarios.json marks {len(found)} designed lots phantom, not one")
    return found[0]


def outcome_problems(conn, run: dict) -> list[str]:
    """The D-21 template checks, each failure named (empty list = all good; 04-CONTEXT D-14, D-21).

    - each reviewer tag has a case opened on the run's day in a live state, and the milk-story case is SUS-19;
    - SUS-19's D-15 beats as 03-08 measures them: a PHANTOM_STOCK fact "<phantom branch>:phantom" whose lot_ids hold
      the phantom lot, with pick_changed; markdown_step on the next day at the product schedule's first percent with
      days_until 1; decide_by on the run's day; an option row on today's run from the story lot's branch to the
      phantom lot's branch;
    - every rejected strawberry row with a run code carries the "cold chain" reason.
    Bread-keep's and the close call's outcomes are not checked here: 04-08 records them in regression.json. Every
    expected value is read from the reference data, the seed or the engine's result; no seed number is a literal."""
    ref = refdata.get(run["dataset_version"])
    day = run["day"]
    problems: list[str] = []
    live = {c["tag"]: c for c in db.rows(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = ANY(:t) "
                                               "AND opened_day = :d AND state = ANY(:s) ORDER BY case_id",
                                         r=run["run_id"], t=list(REVIEWER_TAGS), d=day, s=list(cases.LIVE_STATES))}
    problems += [f"no live {tag} case opened on day {day}" for tag in REVIEWER_TAGS if tag not in live]
    story = live.get(cases.STORY_TAG)
    if story is not None:
        if story["key"] != cases.STORY_KEY:
            problems.append(f"{cases.STORY_TAG} is {story['key']}, not {cases.STORY_KEY}")
        problems += _story_beats(conn, run, ref, story)
    straw = live.get("strawberry-cold-chain")
    if straw is not None:
        rec = cases.active_rec(conn, straw)
        if rec is None:
            problems.append("strawberry-cold-chain has no recommendation")
        else:
            bad = [r for r in rec["result"]["rejected"] if r["run_code"] and "cold chain" not in r["reasons"]]
            problems += [f"strawberry-cold-chain: transfer on {r['run_code']} rejected without 'cold chain' "
                         f"({', '.join(r['reasons'])})" for r in bad]
    return problems


def _story_beats(conn, run: dict, ref: refdata.Ref, story: dict) -> list[str]:
    day, tag = run["day"], cases.STORY_TAG
    rec = cases.active_rec(conn, story)
    if rec is None:
        return [f"{tag}: no recommendation ({story['missing_data']})"]
    result, problems = rec["result"], []
    lot_id, branch = phantom_lot()
    fact = next((f for f in rec["fact_checks"] if f["kind"] == "PHANTOM_STOCK" and f["fact_id"] == f"{branch}:phantom"
                 and lot_id in f["lot_ids"]), None)
    if fact is None:
        problems.append(f"{tag}: no PHANTOM_STOCK fact {branch}:phantom covering {lot_id}")
    elif not fact["pick_changed"]:
        problems.append(f"{tag}: the {branch}:phantom fact does not change the pick")
    schedule = ref.schedules[story["sku"]]
    first_pct = max(schedule)[1]  # the step with the most days before expiry applies first
    want = {"day": day + 1, "pct": first_pct, "days_until": 1}
    if result.get("markdown_step") != want:
        problems.append(f"{tag}: markdown_step is {result.get('markdown_step')}, not {want}")
    if (result.get("decide_by") or {}).get("day") != day:
        problems.append(f"{tag}: decide_by is {result.get('decide_by')}, not on day {day}")
    runs = [r.run_code for r in ref.runs if r.origin == story["source_branch"] and r.dest == branch and r.day == day]
    if not runs:
        problems.append(f"{tag}: no run today from {story['source_branch']} to {branch}")
    elif not any(o["run_code"] in runs for o in result["options"]):
        problems.append(f"{tag}: no option row on the story run {', '.join(runs)}")
    return problems


ANALYZED_TABLES = ("lots", "truth_lots", "inbound")  # analyzed in the build transaction before the day-0 screen


def build_template(dataset_version: str, run_id: str | None = None, *, activate: bool = True) -> str:
    """Build, warm up and check a template; return its run_id once ACTIVE, or READY if not activating.

    `run_id` is the release's choice (D-17), else `tpl-<10 hex>`; a run id that already exists is refused before
    anything is written. Any failure marks it FAILED with the message and raises TemplateError; the previous ACTIVE
    template stays."""
    ref, truth = refdata.get(dataset_version), refdata.truth(dataset_version)
    run_id = run_id or f"tpl-{uuid.uuid4().hex[:10]}"
    with db.tx() as conn:
        if _run(conn, run_id) is not None:
            raise TemplateError(f"template run id {run_id} already exists")
    with db.tx() as conn:  # step 1: a TEMPLATE run, INITIALIZING; no session can point at it
        db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, status, day, next_case_no) VALUES "
                     "(:r, 'TEMPLATE', :v, 'INITIALIZING', 0, :n)", r=run_id, v=dataset_version,
               n=ref.calendar["case_no_start"])
    try:
        # one log line per step (api/buildsteps.py, quick 261018-4nj); day 0's steps are labelled with step 1's row
        day0 = {"kind": "TEMPLATE", "status": "INITIALIZING", "day": 0}
        with db.tx() as conn:
            with buildsteps.step(day0, "init_worlds"):
                init_worlds(conn, run_id, ref, truth)
            with buildsteps.step(day0, "day_start"):
                advance.day_start(conn, _run(conn, run_id))
            # planner statistics that see this transaction's rows, or each per-series query walks the whole run
            # (quick 261017-nhi). ANALYZE needs the table owner, the deploy account that creates the tables;
            # PostgreSQL 17 warns and skips a table it cannot analyze. The names are code, never a request's
            with buildsteps.step(day0, "analyze"):
                for table in ANALYZED_TABLES:
                    db.run(conn, f"ANALYZE {table}")
            with buildsteps.step(day0, "open_cases"):
                cases.open_cases(conn, _run(conn, run_id))  # day 0's screen, as every later morning's
        # step 3: the warm-up: the autopilot acts through the services, then the same advance-day a visitor's
        # Next day calls
        while True:
            with db.tx() as conn:
                run = _run(conn, run_id)
                if run["day"] >= ref.calendar["demo_open_day"]:
                    break
                with buildsteps.step(run, "autopilot"):
                    autopilot(conn, db.lock_run(conn, run_id, statuses=("INITIALIZING",)))
            with db.tx() as conn:
                with buildsteps.step(run, "advance_day"):
                    advance.advance_day(conn, run_id, _run(conn, run_id)["version"])
        with db.tx() as conn:  # step 4: units add up, no limit broken; step 6: the D-21 checks
            from . import catalogue  # a public set's reviewer cases: the catalogue on the open day (05.2 D-18, D-38)
            public = catalogue.applies(ref)
            if public:
                with buildsteps.step(run, "pick"):
                    catalogue.pick(conn, _run(conn, run_id))
            from . import preload  # 07-16: the preloaded material, by its rule, before the checks (07-CONTEXT D-18)
            with buildsteps.step(run, "preload"):
                preload.generate(conn, _run(conn, run_id))  # a minimum it can't meet is logged, never a failure (D-39)
            with buildsteps.step(run, "network_check"):
                network_check(conn, _run(conn, run_id), ref, truth)
            with buildsteps.step(run, "limits_check"):
                limits_check(conn, _run(conn, run_id), ref)
            with buildsteps.step(run, "problems"):
                problems = (catalogue.problems if public else outcome_problems)(conn, _run(conn, run_id))
            if problems:  # a broken beat stops the build and goes back to Phase 3's knobs (D-14)
                raise TemplateError("; ".join(problems))
    except Exception as exc:
        message = str(exc) if isinstance(exc, TemplateError) else f"{type(exc).__name__}: {exc}"
        with db.tx() as conn:
            db.run(conn, "UPDATE demo_runs SET status = 'FAILED', failure = :f WHERE run_id = :r",
                   f=message[:2000], r=run_id)
        if isinstance(exc, TemplateError):
            raise
        raise TemplateError(message) from exc
    with db.tx() as conn:
        if activate:
            # Acquire the lifecycle lock before the first demo_runs UPDATE;
            # simultaneous default builds must not deadlock upgrading row-writer locks.
            db.run(conn, "LOCK TABLE demo_runs IN SHARE ROW EXCLUSIVE MODE")
        db.run(conn, "UPDATE demo_runs SET status = 'READY' WHERE run_id = :r", r=run_id)
        if activate:  # the legacy default still publishes only after every check passed
            activate_template(conn, run_id)
    return run_id


def active_template(conn) -> dict | None:
    return db.one(conn, "SELECT * FROM demo_runs WHERE kind = 'TEMPLATE' AND status = 'ACTIVE' "
                        "ORDER BY created_at DESC LIMIT 1")


def predecessor(conn, new_run_id: str) -> str | None:
    """Exact previous activation for new releases; retain the release owner's legacy timestamp fallback.

    An inactive build may predate the current ACTIVE template. Its actual
    predecessor therefore cannot be inferred from template creation ordering.
    NULL activated_at identifies legacy rows, without inventing old provenance.
    """
    new = _run(conn, new_run_id)
    if new is None or new["kind"] != "TEMPLATE":
        return None
    if new.get("activated_at") is not None:
        return new.get("predecessor_run_id")
    row = db.one(conn, "SELECT p.run_id FROM demo_runs p JOIN demo_runs n ON n.run_id = :n "
                       "WHERE p.kind = 'TEMPLATE' AND p.status = 'RETIRED' AND p.retired_at IS NOT NULL "
                       "AND p.created_at < n.created_at AND p.retired_at >= n.created_at "
                       "ORDER BY p.retired_at DESC, p.created_at DESC LIMIT 1", n=new_run_id)
    return row["run_id"] if row else None


def _lock_templates(conn):
    # Serialize even the first activation, when there are no ACTIVE rows to lock.
    # This short transaction contains no model work and changes no reset lock order.
    db.run(conn, "LOCK TABLE demo_runs IN SHARE ROW EXCLUSIVE MODE")
    return {row["run_id"]: row for row in db.rows(conn, "SELECT * FROM demo_runs WHERE kind = 'TEMPLATE' "
                                                       "ORDER BY run_id FOR UPDATE")}


def activate_template(conn, run_id: str) -> str | None:
    """Publish one checked inactive template atomically; return what this activation retired.

    A retry on the same ACTIVE run is a read of its retained provenance. Neither
    publication nor rollback generates, migrates, installs or copies any AI row.
    """
    templates = _lock_templates(conn)
    target = templates.get(run_id)
    if target is not None and target["status"] == "ACTIVE":
        return predecessor(conn, run_id)
    if target is None or target["status"] != "READY" or target["failure"]:
        raise TemplateError(f"{run_id} is not a completed inactive template (READY)")
    if db.one(conn, "SELECT 1 AS busy FROM ai_outputs WHERE run_id = :r AND status = 'GENERATING' LIMIT 1", r=run_id):
        raise TemplateError(f"{run_id} has an unfinished baseline attempt")
    previous = active_template(conn)
    previous_id = previous["run_id"] if previous else None
    db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = now() "
                 "WHERE kind = 'TEMPLATE' AND status = 'ACTIVE'")
    db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE', retired_at = NULL, activated_at = now(), "
                 "predecessor_run_id = :p WHERE run_id = :r AND kind = 'TEMPLATE'", p=previous_id, r=run_id)
    return previous_id


def has_template_table(conn, table: str = "demo_runs") -> bool:
    """A fresh database may have failed before migration; status/restore remain read-only there."""
    return bool(db.one(conn, "SELECT to_regclass(CAST(:t AS text)) IS NOT NULL AS present", t=table)["present"])


def restore(conn, new_run_id: str, *, first: bool = False) -> tuple[str | None, str] | None:
    """Undo only an ACTIVE TEMPLATE, leaving all sandboxes/per-run rows intact (release D-17).

    Unknown, unfinished, failed or already restored runs are idempotent no-ops.
    A first release may explicitly restore the prior absence of an ACTIVE run.
    """
    templates = _lock_templates(conn)
    new = templates.get(new_run_id)
    if new is None or new["status"] != "ACTIVE":
        return None
    prev = predecessor(conn, new_run_id)
    if prev is None:
        if not first:
            raise TemplateError(f"no previous template to restore before {new_run_id}")
    elif prev not in templates or templates[prev]["status"] != "RETIRED":
        raise TemplateError(f"previous template {prev} is not available to restore before {new_run_id}")
    db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = now() "
                 "WHERE run_id = :r AND kind = 'TEMPLATE'", r=new_run_id)
    if prev is not None:
        db.run(conn, "UPDATE demo_runs SET status = 'ACTIVE', retired_at = NULL "
                     "WHERE run_id = :r AND kind = 'TEMPLATE'", r=prev)
    return prev, new_run_id


def clone(conn, template: dict) -> str:
    """Copy the template's workflow rows into a new SANDBOX run with INSERT … SELECT; only run_id changes. The world
    tables (overlay.OVERLAID) are read through the template until the workspace writes a series (05.2 D-08..D-10).

    Assistant rows use this same column-for-column copy, including exact checked
    sources, original unsent drafts and retained baseline usage. Re-read the
    persisted source: a caller's metadata must never promote a personal workspace
    (and its private post-change outputs) to a shared baseline.
    """
    template = _run(conn, template["run_id"])
    if template is None or template["kind"] != "TEMPLATE":
        raise TemplateError("a sandbox can only clone a persisted template")
    new = f"sbx-{uuid.uuid4().hex[:12]}"
    db.run(conn, "INSERT INTO demo_runs (run_id, kind, dataset_version, template_run_id, status, day, version, "
                 "next_case_no) VALUES (:n, 'SANDBOX', :v, :t, 'ACTIVE', :d, :ver, :no)", n=new,
           v=template["dataset_version"], t=template["run_id"], d=template["day"], ver=template["version"],
           no=template["next_case_no"])
    for table in (t for t in per_run_tables(conn) if t not in overlay.OVERLAID):
        names = ", ".join(c for c in columns(conn, table) if c != "run_id")
        db.run(conn, f"INSERT INTO {table} (run_id, {names}) SELECT :n, {names} FROM {table} WHERE run_id = :t",
               n=new, t=template["run_id"])
    from . import preload  # 07-16: the preloaded times start with this workspace (07-CONTEXT D-05, D-18)
    preload.rebase(conn, new, template)
    return new


def reset(conn, session_id: str) -> tuple[str, int]:
    """Sandbox creation and Reset in the caller's transaction (spec §8): returns (new run_id, elapsed ms).

    Locks the session row, then its old run row, FOR UPDATE (§8 Locking rule); the caller commits or rolls back, so
    a failure partway leaves the session on its old sandbox and no half-cloned run."""
    start = time.perf_counter()
    s = db.one(conn, "SELECT * FROM sessions WHERE session_id = :s FOR UPDATE", s=session_id)
    if s is None:
        raise ApiError(401, "locked", "Sign in to continue.")  # (proposed) app.LOCKED, never imported here
    if s["run_id"]:
        db.one(conn, "SELECT run_id FROM demo_runs WHERE run_id = :r FOR UPDATE", r=s["run_id"])
    template = active_template(conn)
    if template is None:
        raise ApiError(503, "preparing", "The workspace is being prepared. Try again in a moment.")  # (proposed)
    new = clone(conn, template)
    ms = math.ceil((time.perf_counter() - start) * 1000)
    db.run(conn, "UPDATE sessions SET run_id = :n, last_reset_ms = :ms WHERE session_id = :s", n=new, ms=ms, s=session_id)
    if s["run_id"]:
        db.run(conn, "UPDATE demo_runs SET status = 'RETIRED', retired_at = now() WHERE run_id = :r "
                     "AND kind = 'SANDBOX'", r=s["run_id"])
    return new, ms


def _delete_some(conn, table: str, where: str, **params) -> int:
    """Delete at most `prune.rows_per_batch` rows of a shared table matching `where`, skipping rows another
    transaction holds (so two pruning requests never wait on each other)."""
    return db.run(conn, f"DELETE FROM {table} WHERE ctid = ANY(ARRAY(SELECT ctid FROM {table} WHERE {where} "
                        f"LIMIT :lim FOR UPDATE SKIP LOCKED))", lim=config.PRUNE["rows_per_batch"], **params)


def prune(conn) -> dict[str, int]:
    """One bounded batch of what no live session can reach (IN-01; retention and batch sizes in settings `prune`):

    - up to `runs_per_batch` SANDBOX runs RETIRED longer ago than `retired_sandbox_hours` that no session points at,
      with every per-run row, their idempotency keys and their demo_runs row (a write still in flight on one gets 409
      sandbox_reset, as on any retired sandbox); claimed FOR UPDATE SKIP LOCKED, so concurrent batches never collide;
    - session-scoped idempotency keys (run_id '') older than `session_key_days`, the session cookie's life;
    - usage_counts windows older than `usage_window_days` (caps read only the current window);
    - engine_cache rows older than `engine_cache_days` (a later use computes the same result again).

    A live sandbox (ACTIVE, or retired inside the retention) and its keys are never touched. There is no background
    work on Cloud Run, so a reset and a new session each run one batch after their own write commits
    (app._prune), and the deploy's template job drains the backlog. Returns the counts removed."""
    p = config.PRUNE
    runs = [r["run_id"] for r in db.rows(
        conn, "SELECT run_id FROM demo_runs d WHERE kind = 'SANDBOX' AND status = 'RETIRED' "
              "AND coalesce(retired_at, created_at) < now() - make_interval(hours => :h) "
              "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.run_id = d.run_id) "
              "ORDER BY coalesce(retired_at, created_at), run_id LIMIT :n FOR UPDATE SKIP LOCKED",
        h=p["retired_sandbox_hours"], n=p["runs_per_batch"])]
    if runs:
        for table in per_run_tables(conn):  # names from information_schema, never from a request
            db.run(conn, f"DELETE FROM {table} WHERE run_id = ANY(:ids)", ids=runs)
        db.run(conn, "DELETE FROM idempotency_keys WHERE run_id = ANY(:ids)", ids=runs)
        db.run(conn, "DELETE FROM demo_runs WHERE run_id = ANY(:ids)", ids=runs)
    return {"sandboxes": len(runs),
            "session_keys": _delete_some(conn, "idempotency_keys", "run_id = '' AND created_at < now() - "
                                                                   "make_interval(days => :d)", d=p["session_key_days"]),
            "usage_counts": _delete_some(conn, "usage_counts", "window_start < now() - make_interval(days => :d)",
                                         d=p["usage_window_days"]),
            "engine_cache": _delete_some(conn, "engine_cache", "created_at < now() - make_interval(days => :d)",
                                         d=p["engine_cache_days"])}


def _run_id(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("a template run id is 'tpl-' then lowercase letters, digits and hyphens "
                                         "(at most 66 characters)")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m api.sandbox", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="install a reference set and build its demo template")
    build.add_argument("ref_dir", nargs="?", default=str(refdata.REF), help="the reference directory (data/reference)")
    build.add_argument("--allow-local", action="store_true",
                       help="accept the local stand-in forecast (local builds only; the deploy refuses it)")
    build.add_argument("--run-id", type=_run_id, default=None,
                       help="the new template's run id, chosen by the release (tpl-...); default tpl-<10 hex>")
    build.add_argument("--no-activate", action="store_true", help="leave the checked template READY for the user-run brief job")
    pub = sub.add_parser("activate", help="publish a completed inactive template; never generate briefs")
    pub.add_argument("run_id", metavar="RUN_ID", type=_run_id)
    rest = sub.add_parser("restore", help="a failed release: make the template RUN_ID's activation retired ACTIVE "
                                          "again; a no-op when RUN_ID is not ACTIVE; either way it ends with "
                                          "'ACTIVE: <id|none>', the template ACTIVE afterwards")
    rest.add_argument("run_id", metavar="RUN_ID", type=_run_id, help="the template the failed release built")
    rest.add_argument("--first", action="store_true",
                      help="a first release with no earlier template: when RUN_ID is ACTIVE and its activation retired "
                           "no template, retire it so that none is ACTIVE (the state before that release); otherwise "
                           "the same as without it")
    sub.add_parser("active", help="read-only: print 'ACTIVE: <id|none>', the template a new sandbox clones now (bash "
                                  "deploy/release.sh --status reads it); no migrate, no reference install")
    args = ap.parse_args(argv)
    if args.command == "active":  # read-only: SELECTs only, no migrate, no reference install
        if not db.in_tx(has_template_table):  # a fresh database whose first build failed at migrate (WR-06)
            print("ACTIVE: none")
            return 0
        active = db.in_tx(active_template)
        print(f"ACTIVE: {active['run_id'] if active else 'none'}")
        return 0
    if args.command == "restore":  # no migrate, no reference install: only TEMPLATE rows change
        if not db.in_tx(has_template_table):  # no template table, so no template is ACTIVE (WR-06)
            print(f"restore: {args.run_id} is not ACTIVE; nothing to do")
            print("ACTIVE: none")
            return 0
        try:
            done = db.in_tx(restore, args.run_id, first=args.first)
        except TemplateError as exc:
            print(f"restore failed: {exc}", file=sys.stderr)
            return 1
        if done is None:
            print(f"restore: {args.run_id} is not ACTIVE; nothing to do")
        elif done[0] is None:
            print(f"first release undone: {done[1]} RETIRED")
        else:
            print(f"restored: {done[0]} ACTIVE, {done[1]} RETIRED")
        active = db.in_tx(active_template)  # read after the restore committed: what the release checks (CR-02)
        print(f"ACTIVE: {active['run_id'] if active else 'none'}")
        return 0
    if args.command == "activate":
        try:
            previous = db.in_tx(activate_template, args.run_id)
        except TemplateError as exc:
            print(f"activation failed: {exc}", file=sys.stderr)
            return 1
        print(f"template {args.run_id} ACTIVE for {db.in_tx(_run, args.run_id)['dataset_version']}")
        print(f"retired: {previous or 'none'}", flush=True)
        return 0
    db.migrate()
    try:
        dv = refdata.install(args.ref_dir, allow_local=args.allow_local)
        previous = db.in_tx(active_template)
        print(f"previous ACTIVE: {previous['run_id'] if previous else 'none'}", flush=True)
        run_id = build_template(dv, args.run_id, activate=not args.no_activate)
    except (ValueError, TemplateError) as exc:
        print(f"template build failed: {exc}", file=sys.stderr)
        return 1
    print(f"template {run_id} {'READY' if args.no_activate else 'ACTIVE'} for {dv}")
    if not args.no_activate:
        # From here the new template is ACTIVE: a failure is not caught, so the job exits non-zero and the release's
        # restore puts the predecessor back (R2-1, 06-07).
        print(f"retired: {db.in_tx(predecessor, run_id) or 'none'}", flush=True)
    removed: dict[str, int] = {}
    while True:  # the deploy job drains what requests left behind, one bounded batch per transaction (IN-01)
        batch = db.in_tx(prune)
        removed = {k: removed.get(k, 0) + v for k, v in batch.items()}
        if not any(batch.values()):
            break
    print("pruned " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in removed.items()))
    with db.tx() as conn:  # after the freeze's pruned line, which follows retired: (test_release_safety); 07-16: a preload minimum the data does not give is reported, never silent (D-39)
        from . import preload
        missing = preload.shortfall(conn, run_id)
    print(f"preload: missing {'; '.join(missing)}" if missing else "preload: every minimum met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
