#!/usr/bin/env bash
# The early Cloud Run bench (06-CONTEXT D-15, D-16, D-19). You run it in your own terminal; Claude only runs --dry-run
# with a synthetic configuration.
#
# Run it after bash deploy/setup.sh base and before bash deploy/setup.sh data: it needs no database. It builds (or
# reuses) the release image from git archive of the full HEAD SHA, runs the early job on 1 vCPU and 1 GiB with the
# engine worker on and the frozen input (python -m api.bench --input api/bench_input.json --shortlist-info), reads its
# lines back and writes deploy/releases/<UTC>-<sha12>-early-bench.json (digest, input hash, configuration, startup,
# runs, median, pass or fail). Commit that file with the releases.
#
# A median at or above 3 s stops the deployment for investigation; the 3 s budget is never raised. This check never
# replaces the release bench (slowest of 3 under 3 s), which stays the gate (D-16).
#
#   bash deploy/bench-early.sh
#   bash deploy/bench-early.sh --dry-run --env-file <file>   print every command and run nothing (needs no gcloud)
#
# It shares the release lock: it never runs while a release or rollback runs from this checkout. The logic lives in
# deploy/release.py, because macOS ships bash 3.2.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec .venv/bin/python -m deploy.release --early-bench "$@"
