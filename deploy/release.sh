#!/usr/bin/env bash
# Release ShelfShift to Cloud Run (OPS-02, OPS-07; 06-CONTEXT D-01, D-09, D-17, D-20, D-21). You run it in your own
# terminal; Claude only runs --dry-run with a synthetic configuration.
#
# In order: refuse an uncommitted tree, a failing reference check, a failing deploy gate (pytest tests/deploy) or a
# browser that cannot start; open the release record deploy/releases/<UTC>-<sha12>.json; build the image with Cloud
# Build from git archive of the full HEAD SHA (reused when that tag exists) and deploy everything by digest; build the
# template under a known run id (the build job); bench it (the bench job, slowest of 3 under 3 s); deploy the service
# (min instances from MIN_INSTANCES, max 1); read it back; check public access; run the smoke and the deployed journey;
# record what Cloud Run reports. A release is known good only after all of that passed.
#
#   bash deploy/release.sh                       a release
#   bash deploy/release.sh --rollback            back to the latest known-good release's previous template and revision
#   bash deploy/release.sh --breaking-template   a template the serving image cannot read: only while no judge is
#                                                using the app
#   bash deploy/release.sh --dry-run --env-file <file>   print every command and run nothing (needs no gcloud)
#   bash deploy/release.sh --status              read-only: the traffic, the serving revision, the ACTIVE template and
#                                                the newest records; writes nothing
#   bash deploy/release.sh --env-file deploy/.env.drill [--drill-fail-after STEP [--drill-fail-restore-once |
#       --drill-fail-settle-once] | --drill-unready-deploy]
#   bash deploy/release.sh --env-file deploy/.env.drill --rollback [--drill-fail-rollback-traffic-once]
#                                                the recovery drill (deploy/README.md step 11): each switch forces one
#                                                rare recovery path (WR-06 path 2, WR-07, WR-09, WR-10, WR-11) on the
#                                                disposable drill service, and is refused for a live configuration;
#                                                drill records go to the git-ignored deploy/releases-drill/
#
# One release or rollback at a time from this checkout. After a crash, run it again: it reconciles the interrupted
# release (settles its build, restores the previous template and revision) instead of building again. Commit the
# record it writes before the next release. The configuration is the git-ignored local file next to
# deploy/.env.example (or --env-file, an absolute path: the script runs from the repository root). The logic lives in
# deploy/release.py, because macOS ships bash 3.2.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec .venv/bin/python -m deploy.release "$@"
