"""The case-budget benchmark on the real story case (spec §6 Time budget; 04-CONTEXT D-07; 06-CONTEXT D-15, D-16, D-19).

Four commands, one case budget (3 s, the spec's, never raised by any option, variable or file):

  python -m api.bench
      The release gate (D-16). Times evaluate + fact checks on SUS-19's stored raw input from the ACTIVE template and
      passes when the slowest of 3 runs is under 3 s. Exit 0 pass, 1 over budget, 2 no ACTIVE template or no SUS-19
      recommendation. The gate line the release reads is unchanged since Phase 4 (and is the last line unless
      --shortlist-info adds its information lines after it):
      `SUS-19: evaluate + fact checks, slowest of 3 = X.XX s (budget 3 s)`.
  python -m api.bench --input FILE
      The early check (D-15): the same timing on a frozen copy of that input, with no database, so it runs as a Cloud
      Run job on the release image before any database exists. It passes when the median of 3 runs is under 3 s.
      Exit 0 pass, 1 over budget, 2 when FILE is missing, unreadable, not an engine input or of another engine
      version. It never replaces the release gate: the release still runs the slowest-of-3 check on the database.
  python -m api.bench --export FILE
      Local only: writes SUS-19's raw input from the ACTIVE template to FILE (`make bench-input` writes
      api/bench_input.json). Exit 0, or 2 when there is no ACTIVE template or no SUS-19 recommendation.
  python -m api.bench --top N [--resets M]
      The release gate on a public set, which has no SUS-19 (05.2 D-12; quick 261017-v6z). The N slowest cases by
      api/casebench.py's rule (`slowest_candidates`, ranked by construction before any timing) are each timed as the
      SUS-19 gate times its case: the raw input built on the ACTIVE template, then evaluate + fact checks 3 times. An
      item whose input lacks data (HorizonError or LookupError) is printed as not timed. Then M resets
      (`casebench.time_resets`; 0, the default, times none). One line per case, then the gate lines the release reads:
      `top N: evaluate + fact checks, slowest of 3 = X.XX s on <key> (budget 3 s); <k> not timed` and, when M > 0,
      `resets: M, slowest <ms> ms (budget 2000 ms)`. Exit 0 when the slowest timed run is under 3 s and every reset
      under 2000 ms, 1 when over, 2 with no ACTIVE template or nothing timed.

`--shortlist-info` (with either timing mode) adds information lines for shortlist_step 5 and 10, marked
"info, not the gate"; they never change the exit code, and the gate always times the input as shipped.

Both timing modes print, in order (D-19): a `config:` line (ENGINE_WORKER, engine_version, git commit, the cgroup
cpu.max as quota/period and memory.max, or `unknown` where the file cannot be read, the cpu count, and the Cloud Run
job, execution and revision, `-` when absent), the `input:` line with the file's sha256 (file mode), the in-process
startup labelled "not a platform cold start" (module imports and the worker warm-up timed apart from engine time),
one line per run, and the gate line. Every run goes through `worker.run(evaluate._compute, inp)`, the path a request
takes: in the one warmed engine worker when ENGINE_WORKER is on, inline when it is off. The gates compare unrounded
seconds; the two-decimal value printed never decides.

api/bench_input.json is engine input produced by code (the export above), not a hand fixture and not an expected
output, so re-exporting it after an engine or data change is allowed: the hand-fixture rule (01-CONTEXT D-23) does
not apply to it. tests/api/test_bench.py fails when it no longer matches the template's SUS-19 input.
"""
from __future__ import annotations

import time

_T0 = time.perf_counter()  # before the engine and api imports, so the startup line can report them

import argparse  # noqa: E402
import dataclasses  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402

from engine import ENGINE_VERSION, from_dict, to_dict  # noqa: E402
from engine.simulate import HorizonError  # noqa: E402

from . import casebench, cases, config, db, evaluate, inputs, sandbox, worker  # noqa: E402

IMPORT_S = time.perf_counter() - _T0

BUDGET_S = 3.0  # spec §6: one case, evaluate + fact checks, within 3 s. Never raised.
RUNS = 3
SHORTLIST_INFO = (5, 10)  # information only: the gate always times the shipped input
CGROUP = pathlib.Path("/sys/fs/cgroup")
REEXPORT = "re-export with make bench-input"


def story_input(conn, template: dict):
    """SUS-19's stored raw input on the template (found by its milk-story tag), or None."""
    case = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND tag = :t ORDER BY opened_day DESC LIMIT 1",
                  r=template["run_id"], t=cases.STORY_TAG)
    rec = cases.active_rec(conn, case) if case else None
    return from_dict(rec["engine_input"]) if rec else None


def _cgroup(name: str) -> str:
    """A cgroup v2 limit as one word (cpu.max's "quota period" as quota/period), or unknown when unreadable."""
    try:
        text = (CGROUP / name).read_text().strip()
    except OSError:
        return "unknown"
    return "/".join(text.split()) or "unknown"


def config_line() -> str:
    e = os.environ.get
    return (f"config: ENGINE_WORKER={'on' if config.ENGINE_WORKER else 'off'} engine_version={ENGINE_VERSION} "
            f"git_commit={config.GIT_COMMIT} cpu.max={_cgroup('cpu.max')} memory.max={_cgroup('memory.max')} "
            f"cpu_count={os.cpu_count()} job={e('CLOUD_RUN_JOB') or '-'} "
            f"execution={e('CLOUD_RUN_EXECUTION') or '-'} revision={e('K_REVISION') or '-'}")


def _time_runs(inp, runs: int = RUNS) -> list[float]:
    """Seconds per run of evaluate + fact checks, through the engine worker as a request runs it."""
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        worker.run(evaluate._compute, inp)
        times.append(time.perf_counter() - t0)
    return times


def _read_input(path: str):
    """(EngineInput, sha256 of the file bytes), or an error message for exit 2."""
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError as exc:
        return None, f"cannot read the bench input {path}: {exc.strerror or exc}"
    digest = hashlib.sha256(raw).hexdigest()
    try:
        inp = from_dict(json.loads(raw))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return None, f"{path} is not an engine input ({type(exc).__name__}: {exc}); {REEXPORT}"
    if inp.engine_version != ENGINE_VERSION:
        return None, f"{path} holds engine_version {inp.engine_version!r}, not {ENGINE_VERSION!r}: {REEXPORT}"
    return (inp, digest), None


def _export(path: str) -> int:
    with db.tx() as conn:
        template = sandbox.active_template(conn)
        inp = story_input(conn, template) if template else None
    if template is None:
        print("no ACTIVE template: run python -m api.sandbox build first", file=sys.stderr)
        return 2
    if inp is None:
        print(f"the ACTIVE template has no {cases.STORY_KEY} recommendation", file=sys.stderr)
        return 2
    body = (json.dumps(to_dict(inp), sort_keys=True, indent=1) + "\n").encode("utf-8")
    pathlib.Path(path).write_bytes(body)
    print(f"wrote {path}: {cases.STORY_KEY} from template {template['run_id']}, {len(body)} bytes, "
          f"sha256={hashlib.sha256(body).hexdigest()}")
    return 0


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("at least 1")
    return n


def _not_negative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("0 or more")
    return n


def _top(top: int, resets: int) -> int:
    """The public-set gate (--top): the ranked cases timed as the SUS-19 gate times its case, then the resets."""
    print(config_line(), flush=True)
    with db.tx() as conn:
        template = sandbox.active_template(conn)
        candidates = casebench.slowest_candidates(conn, template, top) if template else []
    if template is None:
        print("no ACTIVE template: run python -m api.sandbox build first", file=sys.stderr)
        return 2
    timed, untimed = [], 0
    started = config.ENGINE_WORKER and worker.pid() is None
    try:
        if started:
            t0 = time.perf_counter()
            worker.start()  # warm, as the app's lifespan starts it before the first request
            warm = f"worker warm-up {time.perf_counter() - t0:.2f} s"
        else:
            warm = "worker warm-up already running" if config.ENGINE_WORKER else "worker warm-up off (inline)"
        print(f"in-process startup (not a platform cold start): imports {IMPORT_S:.2f} s, {warm}", flush=True)
        with db.tx() as conn:
            for c in candidates:
                head = f"{c['key']} {c['store']} {c['product']} dests={c['dests']} lots={c['lots']}"
                try:
                    times = _time_runs(inputs.build_input(conn, template, c["lot_id"]))
                except (HorizonError, LookupError) as exc:
                    print(f"{head} not timed: {exc}", flush=True)
                    untimed += 1
                    continue
                print(f"{head} slowest={max(times):.3f} s median={statistics.median(times):.3f} s", flush=True)
                timed.append((max(times), c["key"]))
    finally:
        if started:
            worker.stop()
    if not timed:
        print("nothing timed: the ACTIVE template has no flag or live case with its data", file=sys.stderr)
        return 2
    reset_ms = casebench.time_resets(resets) if resets else []
    slowest, key = max(timed, key=lambda t: t[0])
    print(f"top {top}: evaluate + fact checks, slowest of {RUNS} = {slowest:.2f} s on {key} "
          f"(budget {BUDGET_S:g} s); {untimed} not timed")
    if resets:
        print(f"resets: {resets}, slowest {max(reset_ms)} ms (budget {casebench.RESET_BUDGET_MS} ms)")
    over = slowest >= BUDGET_S or any(ms >= casebench.RESET_BUDGET_MS for ms in reset_ms)
    return 1 if over else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m api.bench", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--input", metavar="FILE",
                      help="the early check (no database): median of 3 on a frozen engine input, e.g. "
                           "api/bench_input.json")
    mode.add_argument("--export", metavar="FILE",
                      help="local only: write SUS-19's raw input from the ACTIVE template to FILE")
    mode.add_argument("--top", metavar="N", type=_positive,
                      help="the public-set gate: time the N slowest cases by api/casebench.py's rule")
    ap.add_argument("--resets", metavar="M", type=_not_negative, default=0,
                    help="with --top: also time M resets (default 0)")
    ap.add_argument("--shortlist-info", action="store_true",
                    help="also time shortlist_step 5 and 10 (information only, never the gate)")
    args = ap.parse_args([] if argv is None else argv)
    if args.resets and not args.top:
        ap.error("--resets applies to --top only")
    if args.top:
        if args.shortlist_info:
            ap.error("--shortlist-info applies to the SUS-19 modes, not to --top")
        return _top(args.top, args.resets)
    if args.export:
        if args.shortlist_info:
            ap.error("--shortlist-info times runs; it does not apply to --export")
        return _export(args.export)

    print(config_line(), flush=True)
    if args.input:
        loaded, err = _read_input(args.input)
        if err:
            print(err, file=sys.stderr)
            return 2
        inp, digest = loaded
        print(f"input: {args.input} sha256={digest}", flush=True)
    else:
        with db.tx() as conn:
            template = sandbox.active_template(conn)
            inp = story_input(conn, template) if template else None
        if template is None:
            print("no ACTIVE template: run python -m api.sandbox build first", file=sys.stderr)
            return 2
        if inp is None:
            print(f"the ACTIVE template has no {cases.STORY_KEY} recommendation", file=sys.stderr)
            return 2

    started = config.ENGINE_WORKER and worker.pid() is None
    try:
        if started:
            t0 = time.perf_counter()
            worker.start()  # warm, as the app's lifespan starts it before the first request
            warm = f"worker warm-up {time.perf_counter() - t0:.2f} s"
        else:
            warm = "worker warm-up already running" if config.ENGINE_WORKER else "worker warm-up off (inline)"
        print(f"in-process startup (not a platform cold start): imports {IMPORT_S:.2f} s, {warm}", flush=True)
        times = _time_runs(inp)
        for n, t in enumerate(times, 1):
            print(f"run {n}: {t:.2f} s")
        if args.input:
            median = statistics.median(times)
            print(f"{cases.STORY_KEY} (frozen input): evaluate + fact checks, median of {RUNS} = {median:.2f} s "
                  f"(budget {BUDGET_S:g} s)")
            code = 0 if median < BUDGET_S else 1
        else:
            slowest = max(times)
            print(f"{cases.STORY_KEY}: evaluate + fact checks, slowest of {RUNS} = {slowest:.2f} s "
                  f"(budget {BUDGET_S:g} s)")
            code = 0 if slowest < BUDGET_S else 1
        if args.shortlist_info:
            for step in SHORTLIST_INFO:
                info = dataclasses.replace(inp, policy=dataclasses.replace(inp.policy, shortlist_step=step))
                m = statistics.median(_time_runs(info))
                print(f"info, not the gate: shortlist_step {step}: median of {RUNS} = {m:.2f} s")
    finally:
        if started:
            worker.stop()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
