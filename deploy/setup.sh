#!/usr/bin/env bash
# One-time Google Cloud setup for ShelfShift (OPS-08). You run it in your own terminal; Claude only runs --dry-run
# with a synthetic configuration.
#
#   bash deploy/setup.sh base   APIs, the image repository, the run and deploy service accounts, their roles
#   bash deploy/setup.sh data   Cloud SQL with IAM login, the database and its IAM users, the passcode secret,
#                               the release-data bucket and its reader, the budget alert, and the schema grants to
#                               apply by hand
#   bash deploy/setup.sh ai     Phase 9: the Vertex AI API and roles/aiplatform.user for the run and deploy accounts
#
# Order: base; then the model metadata check and bash deploy/bench-early.sh; then data; ai before the first Phase 9
# release. Every mode is safe to re-run:
# what exists is left as it is.
# Options (after the mode): --dry-run prints every command and runs nothing; --env-file PATH reads another
# configuration (give an absolute path: the script runs from the repository root).
# The configuration is deploy/.env.local (git-ignored; its keys are listed in deploy/.env.example). The logic lives in
# deploy/provision.py, because macOS ships bash 3.2.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
exec .venv/bin/python -m deploy.provision "$@"
