"""The release the user runs: a clean commit, built by Cloud Build, deployed by digest, read back, smoked, journeyed
and recorded (06-CONTEXT D-01, D-04 to D-06, D-09, D-10, D-17, D-20, D-21, D-24; OPS-02, OPS-05, OPS-07).

The user runs `bash deploy/release.sh` in their own terminal; Claude never does. Claude only runs it through the
tests' fake runner and with `--dry-run --env-file <synthetic file>`, which needs no gcloud and runs nothing.

Order (06.1 D-64: the HQ credential is checked before anything is built, so the known-good revision keeps serving
when it is refused):
    1. local refusals, before any cloud command: the tree has uncommitted or untracked changes (D-21);
       `python -m data.reference --release` fails; the deploy gate (`pytest tests/deploy` with SS_DEPLOY_ENV_FILE set
       to the absolute path of the configuration this release loaded, so 06-08's scan loads PROJECT itself and fails
       closed) fails; headless Chromium cannot start (the journey needs it). Then the compatibility rule: the record
       keeps engine_version (engine.model.ENGINE_VERSION of the clean HEAD, which is the archived source) and
       template_compat (first, compatible or breaking against the previous known-good manifest).
    2. the release record deploy/releases/<UTC>-<sha12>.json opens with status in_progress (deploy/manifest.py) and
       is rewritten at each step's start and end (06-REVIEWS R2-8)
    3. the secret step, the first recorded step, before any image, build, job or deploy command: the latest ENABLED
       version number of PASSCODE_SECRET and of HQ_PASSCODE_SECRET, each pinned for the service (never `latest`,
       never the value; a secret with no enabled version, or one that does not exist, refuses here naming its key),
       then the HQ preflight (`hq_preflight`, 06.1 D-64, D-68): both pinned versions are read into memory only, both
       values trimmed, and the release refuses when the store value is empty or the HQ value is empty, shorter than
       HQ_MIN_LENGTH characters or equal to the store value (hmac.compare_digest on the UTF-8 bytes). Every refusal
       names the key and the rule, never a value, and both values are dropped right after the check. The product
       app's startup check repeats the same rule (api/accounts.py hq_config_problem), so a bad mount never takes
       traffic either (# ASSUMPTION A14 (06.1-HANDOFF)). A dry run pins both and skips the preflight
   3a. data, the second recorded step (only when RELEASE_DATA_DIR is set; quick 261017-v60): the public artifact
       set's manifest.json is read from that folder (a relative path is read from the repository root) at step 1,
       before any cloud command, dry runs included, and refused (exit 1, no cloud command, no record) unless its kind is public, it lists files, its
       dataset_version recomputes (data.versions.digest over the manifest without it), and every listed part is a
       file directly in parts/ whose sha256 is the manifest's (the checks the cloud install repeats, api/publicref.py);
       a refusal names manifest.json or the part's relative path, never a hash, the folder or a configuration value.
       Its dataset_version becomes the record's, and the build log's `template <id> ACTIVE for <dv>` line must name
       it, or the release fails at build_log and recovers as any build failure does. This step uploads exactly the
       manifest's listed parts with one `gcloud storage cp` to gs://<DATA_BUCKET>/<dataset_version>/parts/, then
       manifest.json last to gs://<DATA_BUCKET>/<dataset_version>/manifest.json, so a killed upload leaves no
       manifest. A plain cp: the local check proved the bytes are the version's, so a rerun rewrites identical
       objects. Printed commands show the folder as <RELEASE_DATA_DIR>
    4. image: `git archive` of the full HEAD SHA extracted to a temporary directory; Cloud Build builds it, tagged
       IMAGE:<sha>, only when the tag's describe answers not found (gcloud's `Image not found.`); any other describe
       failure (signed out, permission denied, no gcloud) stops the release here before any build, with recovery
       not needed; its digest is read back (none: the release stops) and every job and the service run
       IMAGE@sha256:... (D-21)
    5. the revision serving 100% of the service's traffic (status.traffic, never the latest ready revision; a split
       or unreadable traffic refuses the release at this step, before any build job or deploy), none on the first
       release, and none for a service that exists but has never had a ready revision (no latest ready revision and
       no traffic entry naming one; 06-REVIEW WR-07); it is recorded as previous_revision. The latest ready and
       latest created revision names are also captured here, for the non-zero deploy rule
    6. the build job (deploy account): `python -m api.sandbox build --run-id tpl-<sha12>-<UTC>` with DB_USER the
       deploy IAM user and RUN_DB_USER the run IAM user. With RELEASE_DATA_DIR set, the job also mounts DATA_BUCKET
       read-only at /mnt/release-data (a Cloud Storage volume, readable by the deploy account only) and builds the
       mounted folder: `python -m api.sandbox build /mnt/release-data/<dataset_version> --run-id ...`. Its log gives
       the activated template, the template this activation actually retired (`retired:`) and the one ACTIVE before
       the build (`previous ACTIVE:`); the release goes no further unless both the `previous ACTIVE:` and the
       `retired:` lines were read. The template ACTIVE before the build and the serving revision are the pair a
       recovery restores
    7. the release bench job (run account, ENGINE_WORKER=on): `python -m api.bench`, slowest of 3 below 3 s (D-16);
       the budget is never raised. A public release (RELEASE_DATA_DIR set) has no SUS-19 story case, so its gate is
       `python -m api.bench --top 20 --resets 10`: the 20 hardest cases selected before timing, each slowest of 3
       under 3 s, plus 10 resets under 2 s. It passes only on exit 0 with both gate lines, `top <N>: evaluate + fact
       checks, slowest of 3 = <s> s on <key> (budget 3 s); <k> not timed` (recorded where the SUS-19 line goes) and
       `resets: <M>, slowest <ms> ms (budget 2000 ms)` (recorded beside it as `resets`); exit 1 (over a budget),
       exit 2 (no ACTIVE template or nothing timed) or a missing gate line fails the release at the bench
    8. the service: min instances from MIN_INSTANCES, max the constant 1, both set at the service level only (the
       revision-level scaling flags are never used, OPS-02, D-01), 1 vCPU, 1 GiB, startup CPU boost, request-based
       billing (CPU throttled outside requests), the second-generation execution environment (the jobs' machine,
       research Pitfall 4), concurrency 20, timeout 300 s, public; the variables are exactly CLOUD_SQL_INSTANCE,
       DB_USER, DB_NAME, DEMO_MODE (off: the product app refuses the demo endpoints, 06.1 D-62), ENGINE_WORKER,
       GIT_COMMIT and GLOBAL_AI_CAP, and DEMO_PASSCODE and HQ_PASSCODE come from the two pinned secret versions
       (`--set-secrets DEMO_PASSCODE=<PASSCODE_SECRET>:<v>,HQ_PASSCODE=<HQ_PASSCODE_SECRET>:<hv>`). No model, endpoint, project or billing value reaches it (D-04; GEMINI_MODEL waits for
       Phase 9), no pool size is set (one instance holds at most 5 + 2 connections), and neither the forwarded-IPs
       variable nor K_SERVICE is ever set: the image runs uvicorn with proxy headers off and Cloud Run sets K_SERVICE
       itself, which makes the cookie Secure and the counted address the rightmost X-Forwarded-For entry (D-24).
       After the deploy, `update-traffic --to-latest` sends 100% of traffic to the new revision
    9. read-back (D-10): a max-scale value that is absent or other than 1 fails the release; a min-scale value other
       than MIN_INSTANCES fails it too, and an absent one is recorded as "not read back" and accepted only while
       MIN_INSTANCES is 0 (06-REVIEW WR-01); a revision digest other than the built one, or less than 100% of traffic
       on the new revision, fails it; any other field gcloud does not return is recorded as "not read back", never
       guessed
   10. public access (06-REVIEWS R1-1): roles/run.invoker must hold allUsers
   11. the smoke (deploy/smoke.py) and the deployed journey (tests/deployed, D-20) on the service URL, with both
       credentials read from the pinned secret versions into memory only: the smoke gets them in-process, the journey
       child in its environment (SMOKE_PASSCODE and SMOKE_HQ_PASSCODE); they are never printed, logged or written
   12. the record becomes the manifest with status known_good; only then is a release known good

Before step 1 the release takes the release lock and reconciles an interrupted record (06-REVIEWS R2-11, D-R4): one
release or rollback runs at a time from this checkout (`release_lock`), and when the newest release record is still
in_progress (an earlier run was killed) or marked reconcile: pending, this run settles that record's build execution,
restores its template, sends traffic back when its service deploy had been requested, marks it interrupted and exits 1
asking you to commit the record and run again; it never starts another build in that run. A reconcile deploys nothing
new, so it runs no HQ preflight.

The compatibility rule (R2-10): a template the image now serving cannot read (another ENGINE_VERSION than the previous
known-good release) is a breaking release. It refuses before any cloud call unless --breaking-template is given: a
sandbox cloned from the new template while the old revision still serves fails loudly with 409 engine_version (never
computes silently), so such a release never runs while judges are using the app.

Public access (R1-1): when roles/run.invoker lacks allUsers (an organisation policy refused the binding), the release
turns off the invoker IAM check (`gcloud run services update --no-invoker-iam-check`) and reads its annotation back;
the passcode gate is the access control either way. When that is refused too, the release fails at access, recovers
and names the runbook's organisation-policy step: never a silent private URL. A deploy command that exits non-zero is
read back before it is called failed: only when the latest ready revision is the one this deploy created (the
service's latest created revision after the deploy, a name absent from the serving, latest ready and latest created
names captured before it) and carries the built digest is traffic sent to it, and the read-back and access check
decide (06-REVIEW WR-09); otherwise the deploy's error fails the release.

Recovery (D-17; R2-1, R2-8) is decided from the record's progress, never from which step raised. A failure before the
build execute was requested (secret, data, image, service lookup, build-job deployment) records its failed_<step>
status and restores nothing: nothing could have promoted. A refused HQ preflight is failed_secret, the first step, so
nothing was uploaded or built either. The failed record is saved with reconcile: pending before recovering
(06-REVIEW WR-02). From the moment the build execute is requested, every failed or uncertain outcome first settles the
build execution (`settle`: its state read by its recorded name, or found among the build job's executions by --run-id
<new>, "never started" when 3 lists 10 s apart show none and every execution in the last list has ended; an unmatched
execution still running keeps the record pending, at most until the build job's 180-minute task timeout ends it, as it
may be this release's (06-REVIEW WR-08); while it runs, polled every 10 s for up to 5 minutes, then cancelled and polled
until it ends), then runs the build job with `-m api.sandbox restore <new>` (a no-op when <new> is not ACTIVE), proved
by the restore job's own `ACTIVE:` line against active_before_build ("any template but <new>" when that was not read; no
restore when it was none), then, when the service deploy was requested and a previous revision was recorded, sends 100%
of traffic back to it and reads the traffic back, also when settle fails (the restore then waits; 06-REVIEW WR-11); a
move whose command exits non-zero is read back too, and counts only when the service then serves that revision alone
(approved 2026-10-17; `traffic_back`). Each
result goes into the record and is printed. pending clears only when every action succeeded (06-REVIEW CR-02); otherwise
(a failed, mismatched or unverified restore or traffic move, or an execution state that cannot be read, when no restore
runs because a still-running build could promote after it) the next `bash deploy/release.sh` retries this release's
recovery first and starts no build. A first release (no known-good release, no earlier record in this configuration's
records that activated a template, no revision serving before it) that fails before its build log is read goes back to
no template ACTIVE and is not left pending (06-REVIEW WR-06): its restore runs with `--first`, so `nothing to do: none
ACTIVE` (the build never activated) or `undone: none ACTIVE` (its template was ACTIVE and its activation retired none,
so the restore retired it) is a verified result.

Rollback (--rollback, under the lock, after reconciling): the latest known-good release's template is restored (its
predecessor becomes ACTIVE again); only when the restore put that release's previous template back (a `restored:`
result naming it) does its previous revision get 100% of traffic, read back (a move whose command exits non-zero
counts only when that read-back shows the revision alone; `traffic_back`). A half-done rollback (an earlier
failed_rollback of the same release whose restore was `restored: <previous template> ACTIVE`, its traffic move failed
or not read back) is resumed: the next --rollback reads `nothing to do: <previous template> ACTIVE`, moves the traffic,
reads it back and records rolled_back with `resumes: <earlier rollback id>` (06-REVIEW WR-10). Any other no-op, a
mismatch, an unverified or a failed restore refuses, names the ACTIVE line it read, moves no traffic and records
failed_rollback. A <UTC>-<sha12>-rollback.json record is written. With no previous revision (the first release), or
no recorded previous template, it refuses. A rollback creates no revision and mounts no secret, so it runs no HQ
preflight: the revision it routes to keeps the secret versions pinned at its own release. A rollback past the first
product-mode release serves the Phase 5 revision with its own demo-mode setting; that is the known-good one.

The early bench (--early-bench, `bash deploy/bench-early.sh`; D-15, D-19), under the same lock and local refusals
(without the browser probe): the same image path (built or reused by digest), then the early job with no database
variable and no secret: `python -m api.bench --input api/bench_input.json --shortlist-info`, ENGINE_WORKER=on, the run
account, 1 vCPU, 1 GiB. Its log lines are read back into deploy/releases/<UTC>-<sha12>-early-bench.json (the digest,
the input's sha256 in the archive and as the job printed it, the configuration, startup, runs, median and info lines,
and pass, fail or error); it never writes a release record. Each release manifest names the latest early-bench
evidence for its own digest (else null). A median at or above 3 s stops the deployment for investigation; the 3 s
budget is never raised, and the release bench (slowest of 3) stays the gate (D-16).

Status (--status, read-only, under the lock, so it refuses while a release or rollback runs): the service's
status.traffic entries, its latest ready revision and the serving revision (`serving`), the ACTIVE template from one
build-job execution of `python -m api.sandbox active` read from its log ("unknown (<why>)" when that execution
fails, which gcloud itself can cause (signed out) as well as a missing build job or one whose image predates it, or
when its log cannot be read), the newest release and rollback records and the known-good release, then three checks:
serving is the known-good revision, ACTIVE is its template, and its previous revision. A service that never had a
ready revision says so (`_never_ready`, WR-07: the next release is a first release); a describe that fails for a
reason other than not found says the service could not be read. Each revision is labelled with the record that
deployed it. It writes no record, never reads the passcode and prints no URL.

Drill (deploy/README.md step 11): a drill configuration copies the live one, so a drill release pins both secrets,
runs the same HQ preflight and deploys DEMO_MODE=off with both mounts. A configuration whose SERVICE, BUILD_JOB, BENCH_JOB and EARLY_BENCH_JOB end with
-drill and whose DB_NAME ends with _drill is a drill configuration (`is_drill`); one that marks only some of the five
is refused by every mode, dry runs included, with key-only `config error:` lines before any command. A drill
configuration's records go to the git-ignored deploy/releases-drill/ (`records_dir`) and a live one's to
deploy/releases/ only, so neither ever reads the other's. Five switches make a rare recovery path run on a disposable
service; each is refused before any command for a live configuration, dry runs included, and the record's `drill`
field names them (a live record has no such field):
    --drill-fail-after STEP (build_execute, build_log, bench, service_deploy, read_back, access, smoke or journey)
        fails the release with `drill: forced failure after STEP` right after STEP's progress entry is saved ok, and
        the recovery then runs exactly as for any failure there (reason `drill`). build_execute forces 06-REVIEW
        WR-06 path 2: on a first release, a build that activated whose log was never read, undone by
        `restore <new> --first`
    --drill-fail-restore-once (with --drill-fail-after) makes that run's recovery restore return `failed: drill, not
        run` without running it, so the record stays reconcile: pending and the next run retries the real restore
    --drill-fail-settle-once (with --drill-fail-after, never with --drill-fail-restore-once) makes that run's recovery
        settle return `unknown: drill, not run` without any executions call: the restore waits, traffic still moves
        back when the service deploy was requested, and the record stays pending (06-REVIEW WR-11); the plain rerun
        settles for real
    --drill-unready-deploy (alone) sets GLOBAL_AI_CAP to DRILL_UNREADY_CAP in this run's service deploy only, so the
        app refuses it at import and the revision never becomes ready: the deploy exits non-zero and the real recovery
        runs. On a first release that proves 06-REVIEW WR-07 (the next release is a first release); after a failed
        smoke it proves WR-09 (the fallback never routes a revision it did not create)
    --drill-fail-rollback-traffic-once (with --rollback only) runs the rollback's real restore, then records its
        traffic move as `failed: drill, not run` without running it, so the rollback is failed_rollback and the next
        plain --rollback resumes it (06-REVIEW WR-10)
argparse refuses the release switches with --rollback, --early-bench or --status, the rollback switch without
--rollback, and the wrong combinations above, with exit 2 before any command. With --dry-run each prints what a real
run would do, and no recovery runs.

Dry run: the ordered command list against a scripted DryRunner (`dry_run_rules`); no lock, no local checks, no
passcode, no smoke, no journey and no record, manifest or evidence are run or written, and each skip is printed.

CLI: python -m deploy.release [--early-bench | --rollback | --status] [--breaking-template] [--dry-run]
     [--env-file PATH] [--drill-fail-after STEP [--drill-fail-restore-once | --drill-fail-settle-once]]
     [--drill-unready-deploy] [--drill-fail-rollback-traffic-once]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from deploy import envfile, manifest
from deploy.cloud import CommandError, DryRunner, Runner, gcloud
from deploy.manifest import NOT_READ_BACK

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
RELEASES = HERE / "releases"
# A drill configuration's records (deploy/README.md step 11): git-ignored, so drill records never mix with the
# committed live ones, and neither directory is ever read by the other kind of run.
RELEASES_DRILL = HERE / "releases-drill"
DRILL_MARKS = {"SERVICE": "-drill", "BUILD_JOB": "-drill", "BENCH_JOB": "-drill", "EARLY_BENCH_JOB": "-drill",
               "DB_NAME": "_drill"}
DRILL_STEPS = ("build_execute", "build_log", "bench", "service_deploy", "read_back", "access", "smoke", "journey")
# --drill-unready-deploy's GLOBAL_AI_CAP (06-REVIEW WR-07, WR-09 on the drill). api/config.py reads GLOBAL_AI_CAP with
# int() at import; the image runs `exec uvicorn api.app:app`, and uvicorn imports the app before it binds the port, so
# the container exits and the revision never becomes ready. deploy/envfile.py accepts digits only for GLOBAL_AI_CAP, so
# no configuration can carry it, and --set-env-vars replaces every variable on each deploy, so the next plain deploy
# sets the configured value again. --command, --args and --port are never touched.
DRILL_UNREADY_CAP = "drill-unready"
DRILL_ONLY = ("the drill flags need a drill configuration (SERVICE, BUILD_JOB, BENCH_JOB and EARLY_BENCH_JOB ending "
              "-drill, DB_NAME ending _drill; deploy/README.md step 11)")
LOCK = HERE / ".release.lock"  # git-ignored, so the clean-tree check passes while it is held
MAX_INSTANCES = "1"  # OPS-02: the only value; never a configuration key
# ASSUMPTION A9 (06.1-HANDOFF): equals api.config.HQ_MIN_LENGTH; both values trimmed, as api/accounts.py
HQ_MIN_LENGTH = 12
SECRET_HELP = {"PASSCODE_SECRET": "run bash deploy/setup.sh data",
               "HQ_PASSCODE_SECRET": "deploy/README.md step 6, The HQ secret"}
JOB_SHAPE = ("--cpu", "1", "--memory", "1Gi", "--max-retries", "0")
# the build job loads the full public dataset into a template: 25 minutes on a laptop (quick 261017-p52), slower on one
# Cloud Run CPU against the small database; the bench keeps 15
JOB_TIMEOUT, BUILD_JOB_TIMEOUT = "15m", "180m"
LOG_TRIES, LOG_EVERY_S = 6, 10  # log ingestion lags: up to a minute
SETTLE_POLLS, SETTLE_EVERY_S = 30, 10  # a running build execution is waited for up to 5 minutes, then cancelled
SETTLE_LISTS = 3  # with no recorded execution name, the build job's executions are listed up to 3 times, 10 s apart
LOCKED = "another release or rollback is running from this checkout; wait for it to finish"
BREAKING = ("the new template cannot be read by the image now serving (its engine version differs from the previous "
            "known-good release's): a visitor's sandbox opened during the release would fail loudly with 409 "
            "engine_version. Release it only while no judge is using the app, with --breaking-template")
ACCESS_REFUSED = ("public access refused: an organisation policy blocks both the allUsers binding and turning off the "
                  "invoker check; see deploy/README.md (access step)")
REFERENCE_FILES = ("data/reference/seed.json", "data/reference/forecast.json", "data/reference/truth.json")
BENCH_GATE = re.compile(r"^SUS-19: evaluate \+ fact checks, slowest of \d+ = ")
# A public release's gate (no SUS-19 story case on a public template): api.bench over the 20 hardest cases selected
# before timing, each slowest of 3 under 3 s, then 10 resets under 2 s; both budgets are never raised
PUBLIC_BENCH_ARGS = ["-m", "api.bench", "--top", "20", "--resets", "10"]
PUBLIC_GATE = re.compile(r"^top \d+: evaluate \+ fact checks, slowest of \d+ = (\d+\.\d+) s on \S+ \(budget 3 s\); "
                         r"\d+ not timed$")
RESETS_GATE = re.compile(r"^resets: \d+, slowest (\d+) ms \(budget 2000 ms\)$")
BENCH_INPUT = "api/bench_input.json"  # the frozen SUS-19 engine input (06-04)
EARLY_ARGS = ["-m", "api.bench", "--input", BENCH_INPUT, "--shortlist-info"]
EARLY_MEDIAN = re.compile(r"^SUS-19 \(frozen input\): evaluate \+ fact checks, median of \d+ = (\d+\.\d+) s")
STEP_STATUS = {
    "data": "failed_data", "image": "failed_image", "secret": "failed_secret",
    "previous_revision": "failed_service_lookup",
    "build_deploy": "failed_build_deploy", "build_execute": "failed_build", "build_log": "failed_build",
    "bench": "failed_bench", "service_deploy": "failed_deploy", "read_back": "failed_readback",
    "access": "failed_readback", "smoke": "failed_smoke", "journey": "failed_journey",
}
# The public set's route to the build job (quick 261017-v60): a read-only Cloud Storage volume of DATA_BUCKET, and the
# label the local folder prints as (never its path).
DATA_VOLUME, DATA_MOUNT, DATA_LABEL = "release-data", "/mnt/release-data", "<RELEASE_DATA_DIR>"
DATA_PART = re.compile(r"parts/[A-Za-z0-9][A-Za-z0-9._-]*")  # a plain file name directly in the set's parts/
BROWSER_PROBE = ("from playwright.sync_api import sync_playwright\n"
                 "with sync_playwright() as p:\n"
                 "    p.chromium.launch(headless=True).close()\n")


class ReleaseError(RuntimeError):
    """A refusal or a failed release step; the message names the cause and never a secret value."""


class ReleaseLocked(ReleaseError):
    """Another release or rollback holds the release lock."""


class AccessRefused(ReleaseError):
    """Neither the allUsers binding nor turning off the invoker check was accepted (R1-1)."""


class SettleUnknown(ReleaseError):
    """The build execution's state could not be read, so a restore could race a running build (R2-1)."""


class DrillFailure(ReleaseError):
    """A drill release's forced failure (--drill-fail-after STEP, one of DRILL_STEPS), raised right after STEP ended
    ok; after build_execute the build log is never read (06-REVIEW WR-06 path 2)."""


def is_drill(cfg: dict[str, str]) -> bool:
    """True when all five DRILL_MARKS names are marked, False when none is. ReleaseError (keys only) when only some
    are: mixed live and drill names are never run."""
    marked = [k for k, suffix in DRILL_MARKS.items() if cfg[k].endswith(suffix)]
    if not marked:
        return False
    if len(marked) == len(DRILL_MARKS):
        return True
    unmarked = [k for k in DRILL_MARKS if k not in marked]
    raise ReleaseError(f"{', '.join(marked)} carry the drill ending but {', '.join(unmarked)} do not: all five "
                       f"names are marked (SERVICE and the three jobs ending -drill, DB_NAME ending _drill) or none; "
                       f"mixed live and drill names are never run")


def records_dir(cfg: dict[str, str]) -> pathlib.Path:
    """The configuration's records directory: RELEASES_DRILL for a drill one, else RELEASES (read at call time)."""
    return RELEASES_DRILL if is_drill(cfg) else RELEASES


def _rerun(drill: bool) -> str:
    """The release command a printed hint names. A drill hint carries --env-file, so typed as printed it never starts
    a live release (seen on the real-cloud drill, 261017-4oa D4)."""
    return "bash deploy/release.sh" + (" --env-file <the drill configuration>" if drill else "")


def _commit_note(cfg, name: str, live: str, again: bool = False) -> str:
    """What to commit: `live` with {path} the live record's path (the live wording), or, for a drill configuration,
    that the drill record is git-ignored and nothing is committed."""
    if is_drill(cfg):
        return (f"deploy/releases-drill/{name} is a drill record (git-ignored): commit nothing"
                + (f", then run {_rerun(True)} again" if again else ""))
    return live.format(path=f"deploy/releases/{name}")


# ponytail: an exclusive flock on a local file covers one operator on one laptop: a second checkout or another machine
# is not covered. If a second operator ever releases, the upgrade path is a shared lease in the project (a lock row in
# the database or a lock object in storage) taken the same way before the first cloud call (06-REVIEWS D-R4).
@contextlib.contextmanager
def release_lock(path: pathlib.Path | None = None):
    """Hold an exclusive, non-blocking lock on the release lock file for the whole block (R2-11)."""
    path = pathlib.Path(LOCK if path is None else path)  # LOCK read at call time, so tests can point it elsewhere
    with open(path, "a") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseLocked(LOCKED) from None
        yield  # released when the file closes: at the block's end, or when the process ends


# --- local refusals -------------------------------------------------------------------------------------------------

class Git:
    """The checkout the release ships from."""

    def __init__(self, root: pathlib.Path = ROOT):
        self.root = pathlib.Path(root)

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True).stdout

    def status_porcelain(self) -> str:
        return self._git("status", "--porcelain", "--untracked-files=all")

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def archive(self, sha: str, dest) -> None:
        """Only committed files can ship: `git archive --format=tar <sha>` extracted into `dest` (D-21)."""
        archive = subprocess.Popen(["git", "-C", str(self.root), "archive", "--format=tar", sha],
                                   stdout=subprocess.PIPE)
        untar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout)
        archive.stdout.close()
        if archive.wait() != 0 or untar.returncode != 0:
            raise ReleaseError(f"git archive of {sha[:12]} could not be extracted")


def check_commands(env_file, browser: bool) -> list[tuple[str, list[str], dict[str, str]]]:
    """The local checks, in order: (name, argv, extra environment). The deploy gate's only extra variable is the
    absolute path of the configuration file this release loaded; no configuration value goes to a child."""
    py = sys.executable
    out = [("data.reference --release", [py, "-m", "data.reference", "--release"], {}),
           ("pytest tests/deploy", [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/deploy"],
            {"SS_DEPLOY_ENV_FILE": str(pathlib.Path(env_file).resolve())})]
    if browser:
        out.append(("browser probe", [py, "-c", BROWSER_PROBE], {}))
    return out


def local_checks(env_file, browser: bool, *, run=subprocess.run, root: pathlib.Path = ROOT) -> Callable[[], str]:
    """A callable that runs check_commands in order and returns the dataset version; ReleaseError names a failure."""

    def checks() -> str:
        version = None
        for name, argv, extra in check_commands(env_file, browser):
            print(f"local check: {name}", flush=True)
            p = run(argv, cwd=root, env={**os.environ, **extra}, capture_output=True, text=True)
            if p.returncode != 0:
                tail = "\n".join(((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-5:])
                raise ReleaseError(f"the {name} check failed (exit {p.returncode})" + (f":\n{tail}" if tail else ""))
            if name.startswith("data.reference"):
                m = re.search(r"^ok (\S+)", p.stdout or "", re.M)
                if not m:
                    raise ReleaseError("the data.reference --release check printed no 'ok <dataset_version>' line")
                version = m.group(1)
        return version

    return checks


def release_data(cfg: dict[str, str]) -> tuple[pathlib.Path, str, list[str]] | None:
    """The public artifact set a release uploads (quick 261017-v60): None when RELEASE_DATA_DIR is empty, else
    (folder, its dataset_version, the listed part paths in manifest order). The folder is RELEASE_DATA_DIR, read from
    the repository root when it is relative (release.sh runs there).

    Refused with ReleaseError, in this order and before any cloud command, as the cloud install (api/publicref.py)
    refuses them again: a manifest that is missing or not JSON, not a public set (kind), lists no files, or whose
    dataset_version does not recompute; then, per files entry, a path that is not a plain file name directly in
    parts/ (DATA_PART), a missing part, or a part whose sha256 is not the manifest's. A message names manifest.json or
    the part's relative path only: never a hash, the folder or a configuration value."""
    if not cfg["RELEASE_DATA_DIR"]:
        return None
    from data import config as refconfig  # lazy, as compat imports engine.model
    from data import versions

    folder = ROOT / cfg["RELEASE_DATA_DIR"]  # an absolute value wins
    try:
        m = refconfig.read(folder / "manifest.json")
    except (refconfig.ConfigError, OSError):
        raise ReleaseError("manifest.json: missing or not readable JSON in RELEASE_DATA_DIR") from None
    if not isinstance(m, dict) or m.get("kind") != "public":
        raise ReleaseError("manifest.json: not a public artifact set (kind)")
    files = m.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError("manifest.json: no files listed")
    if m.get("dataset_version") != versions.digest("ds-", {k: v for k, v in m.items() if k != "dataset_version"}):
        raise ReleaseError("manifest.json: dataset_version does not recompute; the manifest changed after the build")
    parts = []
    for key, entry in files.items():
        rel = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(rel, str) or not DATA_PART.fullmatch(rel):
            raise ReleaseError(f"manifest.json: {rel if isinstance(rel, str) else key}: not a file directly in parts/")
        path = folder / rel
        if not path.is_file():
            raise ReleaseError(f"{rel}: missing")
        with open(path, "rb") as f:
            actual = hashlib.file_digest(f, "sha256").hexdigest()
        if actual != entry.get("sha256"):
            raise ReleaseError(f"{rel}: sha256 is not the manifest's; the part changed after the build")
        parts.append(rel)
    return folder, m["dataset_version"], parts


def upload_data(cfg, runner: Runner, folder: pathlib.Path, dv: str, parts: list[str]) -> None:
    """The listed parts to gs://<DATA_BUCKET>/<dv>/parts/, then manifest.json last (only after the parts command
    returned, so a killed upload leaves no manifest). A plain cp: identical bytes on a rerun."""
    bucket = f"gs://{cfg['DATA_BUCKET']}/{dv}"
    runner.run(gcloud("storage", "cp", *(str(folder / p) for p in parts), f"{bucket}/parts/", project=cfg["PROJECT"]))
    runner.run(gcloud("storage", "cp", str(folder / "manifest.json"), f"{bucket}/manifest.json",
                      project=cfg["PROJECT"]))


def data_volume(cfg) -> tuple[str, ...]:
    """The build job's read-only Cloud Storage volume of DATA_BUCKET, mounted at DATA_MOUNT (gcloud's GA two-flag
    form; Cloud Run jobs run only in the second-generation environment, so no environment flag)."""
    return ("--add-volume", f"name={DATA_VOLUME},type=cloud-storage,bucket={cfg['DATA_BUCKET']},readonly=true",
            "--add-volume-mount", f"volume={DATA_VOLUME},mount-path={DATA_MOUNT}")


def known_good(out_dir) -> dict | None:
    """The newest known-good release that no rollback has undone: what is serving now."""
    undone = {r.get("of") for r in manifest.records(out_dir, kind="rollback") if r.get("status") == "rolled_back"}
    return manifest.latest(out_dir, status="known_good", where=lambda r: r.get("id") not in undone)


def compat(out_dir) -> tuple[str, str]:
    """(engine_version, template_compat): `first` with no known-good release, `compatible` when its engine_version
    equals this source's, `breaking` otherwise or when that manifest lacks the field (06-REVIEWS R2-10)."""
    from engine.model import ENGINE_VERSION  # the clean HEAD is the archived source

    good = known_good(out_dir)
    if good is None:
        return ENGINE_VERSION, "first"
    return ENGINE_VERSION, "compatible" if good.get("engine_version") == ENGINE_VERSION else "breaking"


# --- cloud steps ----------------------------------------------------------------------------------------------------

def image_repo(cfg: dict[str, str]) -> str:
    return f"{cfg['REGION']}-docker.pkg.dev/{cfg['PROJECT']}/{cfg['AR_REPO']}/{cfg['IMAGE_NAME']}"


def _digest(cfg, runner: Runner, tag: str) -> str | None:
    found = runner.json(gcloud("artifacts", "docker", "images", "describe", tag, project=cfg["PROJECT"])) or {}
    digest = (found.get("image_summary") or {}).get("digest")
    return digest if isinstance(digest, str) and digest.startswith("sha256:") else None


def _image_missing(exc: CommandError) -> bool:
    """Whether an image describe failed because the tag does not exist: gcloud's own answer (command_lib/artifacts/
    docker_util.py turns a not-found reply into `Image not found.` followed by help lines), or NOT_FOUND / `could not
    be found`. The whole masked stderr is read, not the 5-line tail: an update notice printed after the answer can push
    it out of the tail. Anything else (signed out, permission denied, no gcloud) is not a missing image."""
    text = exc.stderr.lower()
    return "image not found" in text or "not_found" in text or "could not be found" in text


def image(cfg: dict[str, str], runner: Runner, sha: str, src) -> tuple[str, str]:
    """(IMAGE@sha256:..., the digest): the tag IMAGE:<sha> reused when it exists, else built by Cloud Build from
    `src` (research A2: the digest path is read defensively; a missing digest stops the release). Only a not-found
    describe (`_image_missing`) builds; any other describe failure raises ReleaseError before any build, so the
    release fails at the image step and nothing could have promoted (it fails closed)."""
    repo = image_repo(cfg)
    tag = f"{repo}:{sha}"
    try:
        digest = _digest(cfg, runner, tag)
    except CommandError as exc:
        if not _image_missing(exc):
            raise ReleaseError(f"the image tag {sha[:12]} could not be read, so nothing was built: {exc}") from None
        digest = None  # no such tag yet
    if digest is None:
        runner.run(gcloud("builds", "submit", str(src), "--tag", tag, "--region", cfg["REGION"],
                          project=cfg["PROJECT"]))
        digest = _digest(cfg, runner, tag)
    if digest is None:
        raise ReleaseError(f"the image digest of {repo}:{sha[:12]}... could not be read back")
    return f"{repo}@{digest}", digest


def secret_version(cfg: dict[str, str], runner: Runner, key: str = "PASSCODE_SECRET") -> str:
    """The latest ENABLED version number of the secret cfg[key] names (PASSCODE_SECRET or HQ_PASSCODE_SECRET), pinned
    for the service (never `latest`, never the value). ReleaseError naming the key when it has no enabled version or
    does not exist (a not-found list, `_not_found`)."""
    try:
        rows = runner.json(gcloud("secrets", "versions", "list", cfg[key], project=cfg["PROJECT"])) or []
    except CommandError as exc:
        if _not_found(exc):
            raise ReleaseError(f"{key} names no secret ({SECRET_HELP[key]})") from None
        raise
    numbers = [int(r["name"].rsplit("/", 1)[-1]) for r in rows
               if r.get("state") == "ENABLED" and str(r.get("name", "")).rsplit("/", 1)[-1].isdigit()]
    if not numbers:
        raise ReleaseError(f"{key} has no enabled version ({SECRET_HELP[key]})")
    return str(max(numbers))


def _not_found(exc: CommandError) -> bool:
    tail = exc.stderr_tail.lower()
    return "cannot find service" in tail or "could not be found" in tail or "not_found" in tail


def _revision_of(entry: dict, status: dict) -> str | None:
    """A status.traffic entry's revision: its revisionName, or the latest ready revision for an entry that says
    `latestRevision: true` without a name; else None."""
    if entry.get("revisionName"):
        return entry["revisionName"]
    if entry.get("latestRevision"):
        return status.get("latestReadyRevisionName") or None
    return None


def serving(svc: dict) -> str:
    """The one revision status.traffic sends 100% to (entries at 0% ignored). ReleaseError when the traffic is split
    between revisions or cannot be read (06-REVIEW CR-01)."""
    status = (svc or {}).get("status") or {}
    entries = status.get("traffic")
    names, total = set(), 0
    for t in entries if isinstance(entries, list) else []:
        if isinstance(t, dict) and int(t.get("percent") or 0) > 0:
            names.add(_revision_of(t, status))
            total += int(t["percent"])
    if len(names) == 1 and None not in names and total == 100:
        return names.pop()
    raise ReleaseError("the service's traffic is split or unreadable; send 100% of it to one revision before "
                       "releasing (status.traffic, CR-01)")


def _never_ready(status) -> bool:
    """Whether a service has never had a ready revision (06-REVIEW WR-07): a non-empty status with no latest ready
    revision and no status.traffic entry naming a revision, so nothing serves and the next release is a first release.
    revisions_before and status() share it."""
    if not isinstance(status, dict) or not status:
        return False
    entries = status.get("traffic")
    named = any(isinstance(t, dict) and t.get("revisionName") for t in entries) if isinstance(entries, list) else False
    return not status.get("latestReadyRevisionName") and not named


def revisions_before(cfg: dict[str, str], runner: Runner) -> tuple[str | None, frozenset[str]]:
    """(the serving revision, the latest ready and latest created revision names) before this release, from one
    describe. The serving revision is the one status.traffic sends 100% to (never the latest ready revision, which
    can be a failed release's; 06-REVIEW CR-01). (None, no names) when the service does not exist yet (or the
    describe answers nothing). None, with the names, when the service has never had a ready revision: no latest
    ready revision and no status.traffic entry naming a revision (`_never_ready`), so nothing serves (06-REVIEW
    WR-07). ReleaseError
    when the traffic is split or unreadable otherwise: the release then fails at this step, before any build job or
    deploy. The names let the non-zero deploy rule prove a revision is new (06-REVIEW WR-09)."""
    try:
        svc = runner.json(gcloud("run", "services", "describe", cfg["SERVICE"], "--region", cfg["REGION"],
                                 project=cfg["PROJECT"]))
    except CommandError as exc:
        if _not_found(exc):
            return None, frozenset()
        raise
    if not svc:
        return None, frozenset()
    status = svc.get("status") if isinstance(svc, dict) else None
    status = status if isinstance(status, dict) else {}
    names = frozenset(n for n in (status.get("latestReadyRevisionName"), status.get("latestCreatedRevisionName"))
                      if isinstance(n, str) and n)
    if _never_ready(status):
        return None, names
    return serving(svc), names


def previous_revision(cfg: dict[str, str], runner: Runner) -> str | None:
    """The revision serving 100% of the service's traffic before this release (`revisions_before`)."""
    return revisions_before(cfg, runner)[0]


def _env(values: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in values.items())


def deploy_job(cfg, runner: Runner, name: str, ref: str, account: str, env: dict[str, str], args: list[str],
               timeout: str = JOB_TIMEOUT, extra: tuple[str, ...] = ()) -> None:
    """The one job deploy every job shares. `extra` (the public set's volume flags, the build job only) goes between
    the variables and --command; it is empty for every other caller, so their argv is unchanged."""
    runner.run(gcloud("run", "jobs", "deploy", name, "--image", ref, "--region", cfg["REGION"],
                      "--service-account", account, *JOB_SHAPE, "--task-timeout", timeout, "--set-env-vars", _env(env),
                      *extra, "--command", "python",
                      f"--args={','.join(args)}", project=cfg["PROJECT"]))


def execute_job(cfg, runner: Runner, name: str, run_args: list[str] | None = None) -> str:
    """Run the job and wait; the execution's name (research A3: metadata.name) or NOT_READ_BACK. A failed execution
    raises CommandError (gcloud --wait exits non-zero)."""
    extra = [f"--args={','.join(run_args)}"] if run_args else []
    done = runner.json(gcloud("run", "jobs", "execute", name, "--region", cfg["REGION"], "--wait", *extra,
                              project=cfg["PROJECT"]))
    name_read = ((done or {}).get("metadata") or {}).get("name") if isinstance(done, dict) else None
    return name_read or NOT_READ_BACK


def job(cfg, runner: Runner, name: str, ref: str, account: str, env: dict[str, str], args: list[str],
        run_args: list[str] | None = None) -> str:
    """Deploy the job, then execute it; the execution name."""
    deploy_job(cfg, runner, name, ref, account, env, args)
    return execute_job(cfg, runner, name, run_args)


def job_lines(cfg, runner: Runner, job_name: str, execution: str, expect: str, sleep=time.sleep) -> list[str] | str:
    """The execution's log lines in order, retried until one matches `expect` (up to LOG_TRIES, LOG_EVERY_S apart);
    NOT_READ_BACK when nothing came back. A failing logging command raises."""
    if execution == NOT_READ_BACK:
        return NOT_READ_BACK
    filt = (f'resource.type="cloud_run_job" AND resource.labels.job_name="{job_name}" AND '
            f'labels."run.googleapis.com/execution_name"="{execution}"')
    lines: list[str] = []
    for attempt in range(LOG_TRIES):
        rows = runner.json(gcloud("logging", "read", filt, "--order", "asc", "--limit", "500",
                                  project=cfg["PROJECT"])) or []
        lines = [r["textPayload"].rstrip("\n") for r in rows if isinstance(r, dict) and r.get("textPayload")]
        if any(re.search(expect, line) for line in lines):
            return lines
        if attempt < LOG_TRIES - 1:
            sleep(LOG_EVERY_S)
    return lines or NOT_READ_BACK


def parse_build(lines) -> dict[str, Any]:
    """The build log's three lines (06-03): `previous ACTIVE:`, `template <id> ACTIVE for <dv>`, `retired:`;
    `none` is null and a missing line is NOT_READ_BACK."""
    got = {"active_before_build": NOT_READ_BACK, "activated_template": NOT_READ_BACK,
           "previous_template": NOT_READ_BACK}
    for line in lines if isinstance(lines, list) else []:
        if m := re.match(r"^previous ACTIVE: (\S+)$", line):
            got["active_before_build"] = None if m.group(1) == "none" else m.group(1)
        elif m := re.match(r"^template (\S+) ACTIVE for \S+$", line):
            got["activated_template"] = m.group(1)
        elif m := re.match(r"^retired: (\S+)$", line):
            got["previous_template"] = None if m.group(1) == "none" else m.group(1)
    return got


def iam_users(cfg) -> tuple[str, str]:
    return envfile.iam_db_user(cfg["DEPLOY_SA_EMAIL"]), envfile.iam_db_user(cfg["RUN_SA_EMAIL"])


def sql_instance(cfg) -> str:
    return f"{cfg['PROJECT']}:{cfg['REGION']}:{cfg['SQL_INSTANCE']}"


def build_env(cfg, sha: str) -> dict[str, str]:
    deploy_user, run_user = iam_users(cfg)
    return {"CLOUD_SQL_INSTANCE": sql_instance(cfg), "DB_USER": deploy_user, "RUN_DB_USER": run_user,
            "DB_NAME": cfg["DB_NAME"], "GIT_COMMIT": sha}


def bench_env(cfg, sha: str) -> dict[str, str]:
    return {"CLOUD_SQL_INSTANCE": sql_instance(cfg), "DB_USER": iam_users(cfg)[1], "DB_NAME": cfg["DB_NAME"],
            "ENGINE_WORKER": "on", "GIT_COMMIT": sha}


def service_env(cfg, sha: str) -> dict[str, str]:
    """Exactly what the app needs (D-04): never PROJECT or BILLING_ACCOUNT. CONTACT_EMAIL is the public support address
    the About page and the unavailable screen show (11-CONTEXT D-04; release() refuses an empty one); GCP=on with the
    model, endpoint and both buckets is cloud mode (11-CONTEXT D-01, D-16, D-19: the app makes no call for
    DATA_BUCKET, and with GCP on it refuses to start without UPLOADS_BUCKET)."""
    return {"CLOUD_SQL_INSTANCE": sql_instance(cfg), "DB_USER": iam_users(cfg)[1], "DB_NAME": cfg["DB_NAME"],
            "DEMO_MODE": "off", "ENGINE_WORKER": "on", "GIT_COMMIT": sha, "GLOBAL_AI_CAP": cfg["GLOBAL_AI_CAP"],
            "CONTACT_EMAIL": cfg["CONTACT_EMAIL"], "GCP": "on", "GEMINI_MODEL": cfg["GEMINI_MODEL"],
            "VERTEX_LOCATION": cfg["VERTEX_LOCATION"], "DATA_BUCKET": cfg["DATA_BUCKET"],
            "UPLOADS_BUCKET": cfg["UPLOADS_BUCKET"]}


def deploy_service(cfg, runner: Runner, ref: str, sha: str, version: str, hq_version: str, *,
                   unready: bool = False) -> None:
    """The service deploy, with DEMO_PASSCODE and HQ_PASSCODE mounted from the two pinned versions (06.1 D-62).
    `unready` (a drill release's --drill-unready-deploy) replaces only GLOBAL_AI_CAP's value with DRILL_UNREADY_CAP in
    this deploy's variables; the keys and every other value stay service_env's."""
    env = service_env(cfg, sha)
    if unready:
        env["GLOBAL_AI_CAP"] = DRILL_UNREADY_CAP
    runner.run(gcloud("run", "deploy", cfg["SERVICE"], "--image", ref, "--region", cfg["REGION"],
                      "--service-account", cfg["RUN_SA_EMAIL"], "--min", cfg["MIN_INSTANCES"], "--max", MAX_INSTANCES,
                      "--cpu", "1", "--memory", "1Gi", "--cpu-boost", "--cpu-throttling",
                      "--execution-environment", "gen2", "--concurrency", "20", "--timeout", "300",
                      "--allow-unauthenticated", "--set-env-vars", _env(env),
                      "--set-secrets", f"DEMO_PASSCODE={cfg['PASSCODE_SECRET']}:{version},"
                                       f"HQ_PASSCODE={cfg['HQ_PASSCODE_SECRET']}:{hq_version}",
                      project=cfg["PROJECT"]))


def _service(cfg, runner: Runner) -> dict:
    return runner.json(gcloud("run", "services", "describe", cfg["SERVICE"], "--region", cfg["REGION"],
                              project=cfg["PROJECT"])) or {}


def _sha256_part(value) -> str | None:
    m = re.search(r"sha256:[0-9a-f]{64}", value or "") if isinstance(value, str) else None
    return m.group(0) if m else None


def read_back(cfg, runner: Runner) -> dict[str, Any]:
    """What Cloud Run reports for the service and its latest ready revision; NOT_READ_BACK for what it does not.
    latest_created (the latest created revision, for the non-zero deploy rule) is not copied into the record."""
    svc = _service(cfg, runner)
    status = svc.get("status") or {}
    ann = (svc.get("metadata") or {}).get("annotations") or {}
    tpl = (((svc.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {}
    revision = status.get("latestReadyRevisionName") or NOT_READ_BACK
    traffic = sum(int(t.get("percent") or 0) for t in status.get("traffic") or []
                  if isinstance(t, dict) and _revision_of(t, status) == revision)
    digest = NOT_READ_BACK
    if revision != NOT_READ_BACK:
        rev = runner.json(gcloud("run", "revisions", "describe", revision, "--region", cfg["REGION"],
                                 project=cfg["PROJECT"])) or {}
        containers = (rev.get("spec") or {}).get("containers") or [{}]
        digest = _sha256_part((rev.get("status") or {}).get("imageDigest")) or \
            _sha256_part(containers[0].get("image")) or NOT_READ_BACK
    keep = ("run.googleapis.com/cpu-throttling", "run.googleapis.com/startup-cpu-boost",
            "run.googleapis.com/execution-environment")
    return {"url": status.get("url") or NOT_READ_BACK, "revision": revision, "traffic_percent": traffic,
            "latest_created": status.get("latestCreatedRevisionName") or NOT_READ_BACK,
            "revision_digest": digest, "min_instances": ann.get("run.googleapis.com/minScale", NOT_READ_BACK),
            "max_instances": ann.get("run.googleapis.com/maxScale", NOT_READ_BACK),
            "annotations": {k: tpl[k] for k in keep if k in tpl}}


def read_back_problems(rb: dict[str, Any], cfg, digest: str) -> list[str]:
    """Why the read-back fails the release (D-10). An absent max is a failure (06-REVIEW WR-01); an absent min is
    recorded as "not read back" and accepted only while MIN_INSTANCES is 0."""
    problems = []
    if rb["max_instances"] == NOT_READ_BACK:
        problems.append("max instances were not read back (without maxScale Cloud Run allows many instances; OPS-02 "
                        "needs 1)")
    elif str(rb["max_instances"]) != MAX_INSTANCES:
        problems.append(f"max instances read back as {rb['max_instances']}, not {MAX_INSTANCES}")
    if rb["min_instances"] == NOT_READ_BACK:
        if cfg["MIN_INSTANCES"] != "0":
            problems.append("min instances were not read back while MIN_INSTANCES is not 0")
    elif str(rb["min_instances"]) != cfg["MIN_INSTANCES"]:
        problems.append(f"min instances read back as {rb['min_instances']}, not MIN_INSTANCES")
    if rb["revision_digest"] != digest:
        problems.append(f"the new revision's image digest is {rb['revision_digest']}, not the built one")
    if rb["traffic_percent"] != 100:
        problems.append(f"the new revision holds {rb['traffic_percent']}% of traffic, not 100%")
    if rb["url"] == NOT_READ_BACK:
        problems.append("the service URL was not read back")
    return problems


def access(cfg, runner: Runner) -> str:
    """`public` when roles/run.invoker holds allUsers; else the invoker IAM check is turned off and read back:
    `invoker check disabled` (06-REVIEWS R1-1). AccessRefused when both are refused."""
    try:
        policy = runner.json(gcloud("run", "services", "get-iam-policy", cfg["SERVICE"], "--region", cfg["REGION"],
                                    project=cfg["PROJECT"])) or {}
    except CommandError:
        raise AccessRefused(ACCESS_REFUSED) from None
    if any(b.get("role") == "roles/run.invoker" and "allUsers" in (b.get("members") or [])
           for b in policy.get("bindings") or []):
        return "public"
    try:
        runner.run(gcloud("run", "services", "update", cfg["SERVICE"], "--region", cfg["REGION"],
                          "--no-invoker-iam-check", project=cfg["PROJECT"]))
        ann = (_service(cfg, runner).get("metadata") or {}).get("annotations") or {}
    except CommandError:
        raise AccessRefused(ACCESS_REFUSED) from None
    if str(ann.get("run.googleapis.com/invoker-iam-disabled", "")).lower() == "true":
        return "invoker check disabled"
    raise AccessRefused(ACCESS_REFUSED)


def _read_secret(cfg, runner: Runner, key: str, version: str) -> str:
    """The pinned version of the secret cfg[key] names, trimmed, into memory only, registered with runner.hide so any
    later output shows (withheld) in its place; the runner never prints a command's output."""
    value = runner.run(gcloud("secrets", "versions", "access", version, "--secret", cfg[key],
                              project=cfg["PROJECT"])).strip()
    runner.hide(value, manifest.WITHHELD)
    return value


def passcode(cfg, runner: Runner, version: str) -> str:
    """The pinned store passcode version's value, into memory only; the runner never prints a command's output."""
    value = _read_secret(cfg, runner, "PASSCODE_SECRET", version)
    if not value:
        raise ReleaseError("the passcode secret version is empty")
    return value


def hq_passcode(cfg, runner: Runner, hq_version: str) -> str:
    """The pinned HQ credential version's value, trimmed, into memory only and hidden, for the smoke and the journey."""
    value = _read_secret(cfg, runner, "HQ_PASSCODE_SECRET", hq_version)
    if not value:
        raise ReleaseError("HQ_PASSCODE_SECRET: the pinned version is empty")
    return value


def hq_preflight(cfg, runner: Runner, version: str, hq_version: str) -> None:
    """The HQ credential rule, before any image, build, job or deploy (06.1 D-64, D-68; HQ handoff item 3): both pinned
    versions are read with the same command passcode() issues, trimmed, and compared as UTF-8 bytes with
    hmac.compare_digest. ReleaseError, naming the key and the rule and never a value, when the store value is empty,
    or the HQ value is empty, shorter than HQ_MIN_LENGTH characters or equal to the store value. Both locals are
    deleted in a `finally` right after the check; the runner's mask table (runner.hide) is the only place the values
    stay, so that any later output, error or record still shows (withheld) in their place."""
    store = hq = None
    try:
        store = _read_secret(cfg, runner, "PASSCODE_SECRET", version)
        hq = _read_secret(cfg, runner, "HQ_PASSCODE_SECRET", hq_version)
        if not store:
            raise ReleaseError("PASSCODE_SECRET: the pinned version is empty")
        if not hq:
            raise ReleaseError("HQ_PASSCODE_SECRET: the pinned version is empty")
        if len(hq) < HQ_MIN_LENGTH:
            raise ReleaseError(f"HQ_PASSCODE_SECRET: the pinned version is shorter than {HQ_MIN_LENGTH} characters")
        if hmac.compare_digest(hq.encode("utf-8"), store.encode("utf-8")):
            raise ReleaseError("HQ_PASSCODE_SECRET: the pinned version equals the store passcode (PASSCODE_SECRET); "
                               "add a different version (deploy/README.md step 6, The HQ secret)")
    finally:
        del store, hq


def run_smoke(url: str, code: str, hq: str) -> list[tuple[str, str]]:
    from deploy import smoke

    client = smoke.connect(url)
    try:
        return smoke.check(client, code, hq)
    finally:
        client.close()


def run_journey(url: str, code: str, hq: str, *, run=subprocess.run) -> dict[str, Any]:
    """The deployed journey (tests/deployed, D-20) in a child; the URL and both credentials go to its environment only
    (SMOKE_PASSCODE, SMOKE_HQ_PASSCODE), never its argv. Its output is captured and printed with both credentials
    replaced by the withheld label, longest first: Playwright's call log writes fill("<value>") into a timeout error."""
    env = {**os.environ, "SS_DEPLOYED_URL": url, "SMOKE_PASSCODE": code, "SMOKE_HQ_PASSCODE": hq}
    p = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "deployed", "tests/deployed"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = p.stdout or ""
    for value in sorted(filter(None, (code, hq)), key=len, reverse=True):
        out = out.replace(value, manifest.WITHHELD)
    print(out, end="", flush=True)
    return {"passed": p.returncode == 0, "exit": p.returncode}


def reference_hashes(src) -> dict[str, str]:
    out = {}
    for rel in REFERENCE_FILES:
        path = pathlib.Path(src) / rel
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else NOT_READ_BACK
    return out


# --- recovery (D-17; 06-REVIEWS R2-1, R2-8, R2-11) ---------------------------------------------------------------

def _holds_run_id(obj, run_id: str) -> bool:
    """Whether an execution's JSON carries the arguments --run-id <run_id> anywhere (research A3: read defensively)."""
    if isinstance(obj, dict):
        return any(_holds_run_id(v, run_id) for v in obj.values())
    if isinstance(obj, list):
        if any(obj[i] == "--run-id" and obj[i + 1] == run_id for i in range(len(obj) - 1)):
            return True
        return any(_holds_run_id(v, run_id) for v in obj)
    return isinstance(obj, str) and f"--run-id,{run_id}" in obj


def _ended(execution) -> bool:
    status = (execution or {}).get("status") or {} if isinstance(execution, dict) else {}
    if status.get("completionTime"):
        return True
    return any(c.get("type") == "Completed" and c.get("status") in ("True", "False")
               for c in status.get("conditions") or [] if isinstance(c, dict))


def settle(cfg, record: dict, *, runner: Runner, sleep=time.sleep) -> dict[str, str | None]:
    """Make sure the release's build execution has ended before anything restores (R2-1): read it by its recorded name,
    or find it among BUILD_JOB's executions by --run-id <new>, listed up to SETTLE_LISTS times SETTLE_EVERY_S apart;
    when no list holds it and every execution in the last list has ended, the execute never started one, so nothing can
    promote: {"execution": None, "state": "never started"} (06-REVIEW WR-03). When none holds it but one has not ended,
    it may be this release's (an executions-list shape `_holds_run_id` does not read), so SettleUnknown keeps the record
    pending until it ends (06-REVIEW WR-08). While it runs, poll every SETTLE_EVERY_S for SETTLE_POLLS polls, then
    cancel it and poll until it ends. SettleUnknown when its state cannot be read: a failing list or describe, more than
    one execution holding --run-id <new>, or one without a name."""
    project, region, job_name, new = cfg["PROJECT"], cfg["REGION"], cfg["BUILD_JOB"], record["new_template"]

    def describe(name):
        return runner.json(gcloud("run", "jobs", "executions", "describe", name, "--region", region, project=project))

    try:
        name = record.get("build_execution")
        if name and name != NOT_READ_BACK:
            state = describe(name)
        else:
            match: list[dict] = []
            rows: list = []
            for attempt in range(SETTLE_LISTS):
                rows = runner.json(gcloud("run", "jobs", "executions", "list", "--job", job_name, "--region",
                                          region, project=project)) or []
                match = [r for r in rows if isinstance(r, dict) and _holds_run_id(r, new)]
                if match:
                    break
                if attempt < SETTLE_LISTS - 1:
                    sleep(SETTLE_EVERY_S)
            if not match:
                running = [r for r in rows if not _ended(r)]  # a row that is not a dict counts as not ended
                if running:
                    raise SettleUnknown(f"{len(running)} build-job execution(s) "
                                        f"still running hold no recognisable --run-id {new}; "
                                        "one may be this release's (06-REVIEW WR-08)")
                return {"execution": None, "state": "never started"}
            if len(match) != 1:
                raise SettleUnknown(f"{len(match)} executions of the build job hold --run-id {new}")
            state = match[0]
            name = (state.get("metadata") or {}).get("name")
            if not name:
                raise SettleUnknown("the build execution has no name")
            record["build_execution"] = name
        polls = 0
        while not _ended(state) and polls < SETTLE_POLLS:
            sleep(SETTLE_EVERY_S)
            state = describe(name)
            polls += 1
        if _ended(state):
            return {"execution": name, "state": "finished"}
        runner.run(gcloud("run", "jobs", "executions", "cancel", name, "--region", region, "--quiet",
                          project=project))
        for _ in range(SETTLE_POLLS):
            sleep(SETTLE_EVERY_S)
            if _ended(describe(name)):
                return {"execution": name, "state": "cancelled"}
        raise SettleUnknown(f"{name} had not ended after it was cancelled")
    except CommandError as exc:
        raise SettleUnknown(f"the build execution's state could not be read ({exc.argv0} exited {exc.returncode})") \
            from None


RESTORE_OUTCOMES = ((re.compile(r"restored: \S+ ACTIVE, \S+ RETIRED"), "restored"),
                    (re.compile(r"restore: \S+ is not ACTIVE; nothing to do"), "nothing to do"),
                    (re.compile(r"first release undone: \S+ RETIRED"), "undone"))


def restore(cfg, runner: Runner, template: str, expected: str, *, first: bool = False, sleep=time.sleep) -> str:
    """The build job with `-m api.sandbox restore <template>` (06-03: a no-op when it is not ACTIVE), proved by the
    execution's own log (06-REVIEW CR-02): its outcome line and its `ACTIVE: <id|none>` line, the template ACTIVE
    after it ran. `expected` is the template that must be ACTIVE then, or NOT_READ_BACK for "any template but
    `template`". 'restored: <X> ACTIVE' or 'nothing to do: <X> ACTIVE' when the check passes; 'mismatch: <outcome>,
    <X|none> ACTIVE, expected <E>' when another template (or none) is ACTIVE; 'unverified: <why>' when the execution
    name or its log lines did not come back; 'failed: <exit code>' when the execution failed.

    `first` (a failed first release, `_first_release`; 06-REVIEW WR-06) counts only with expected NOT_READ_BACK: the
    restore then runs with `--first`, and no template ACTIVE is also a verified result after a `nothing to do` or an
    `undone` outcome ('<outcome>: none ACTIVE'). An `undone` outcome with any template ACTIVE is a mismatch. Rollback
    never passes it."""
    first = first and expected == NOT_READ_BACK
    try:
        execution = execute_job(cfg, runner, cfg["BUILD_JOB"],
                                ["-m", "api.sandbox", "restore", template, *(["--first"] if first else [])])
    except CommandError as exc:
        return f"failed: {exc.returncode}"
    if execution == NOT_READ_BACK:
        return "unverified: the restore execution's name was not read back"
    try:
        lines = job_lines(cfg, runner, cfg["BUILD_JOB"], execution, r"^ACTIVE: ", sleep)
    except CommandError:
        return "unverified: the restore's log could not be read"
    if not isinstance(lines, list):
        return "unverified: the restore's log was not read back"
    outcome = next((name for line in lines for pattern, name in RESTORE_OUTCOMES if pattern.fullmatch(line)), None)
    active = next((m.group(1) for line in lines for m in [re.fullmatch(r"ACTIVE: (\S+)", line)] if m), None)
    if outcome is None or active is None:
        return "unverified: the restore's log holds no outcome or no ACTIVE line"
    active = None if active == "none" else active
    # an `undone` outcome that still names a template ACTIVE is never verified
    if outcome != "undone" and active is not None and (active == expected or (expected == NOT_READ_BACK
                                                                              and active != template)):
        return f"{outcome}: {active} ACTIVE"
    if active is None and first and outcome in ("nothing to do", "undone"):
        return f"{outcome}: none ACTIVE"
    want = f"not {template}" if expected == NOT_READ_BACK else expected
    return f"mismatch: {outcome}, {active or 'none'} ACTIVE, expected {want}"


def _restored(result: str) -> bool:
    """Whether a restore result is a verified one (the captured template is ACTIVE, or none after a first release)."""
    return result.startswith(("restored: ", "nothing to do: ", "undone: "))


def route_latest(cfg, runner: Runner) -> None:
    """Send 100% of traffic to the latest ready revision after a deploy: once a recovery has pinned traffic with
    --to-revisions, Cloud Run no longer routes a new deploy to its revision by itself."""
    runner.run(gcloud("run", "services", "update-traffic", cfg["SERVICE"], "--region", cfg["REGION"], "--to-latest",
                      project=cfg["PROJECT"]))


def traffic_back(cfg, runner: Runner, revision: str) -> str:
    """Send 100% of traffic to `revision`, then read the service back (06-REVIEW CR-01): '<revision>=100' only when it
    then serves that revision alone; 'mismatch: <X> serves, not <revision>' when it serves another; 'unverified: ...'
    when its traffic cannot be read back; 'failed: <exit code>' when the move failed.

    The one helper behind recovery's move-back and the rollback's traffic move. A move whose command exits non-zero
    is read back too (approved 2026-10-17): update-traffic can apply the route and then fail its wait for the
    service's Ready condition, for example while a never-ready revision exists. It counts as '<revision>=100' only on
    the same check a zero exit passes (serving() names exactly that revision at 100%), with one stderr line naming
    the exit code; any other read-back, or one that cannot be read, stays 'failed: <exit code>'. The command's
    stderr is never printed or recorded."""
    try:
        runner.run(gcloud("run", "services", "update-traffic", cfg["SERVICE"], "--region", cfg["REGION"],
                          "--to-revisions", f"{revision}=100", project=cfg["PROJECT"]))
    except CommandError as exc:
        try:
            proved = serving(_service(cfg, runner)) == revision
        except (CommandError, ReleaseError):
            proved = False
        if not proved:
            return f"failed: {exc.returncode}"
        print(f"traffic: the move to {revision} exited {exc.returncode}, but the read-back shows {revision}=100: "
              f"counted as moved", file=sys.stderr)
        return f"{revision}=100"
    try:
        now = serving(_service(cfg, runner))
    except (CommandError, ReleaseError):
        return "unverified: the service's traffic could not be read back"
    return f"{revision}=100" if now == revision else f"mismatch: {now} serves, not {revision}"


def _requested(record: dict, step: str) -> bool:
    return any(p.get("step") == step for p in record.get("progress") or [])


def _first_release(record: dict, out_dir) -> bool:
    """Whether the record is a first release (06-REVIEW WR-06): no known-good release before it, no revision serving
    before it, and no other release record in this configuration's records directory that activated a template."""
    if record.get("previous_known_good") is not None or record.get("previous_revision") is not None:
        return False
    return not any(r.get("activated_template") not in (None, NOT_READ_BACK)
                   for r in manifest.records(out_dir) if r.get("id") != record.get("id"))


def _move_traffic_back(cfg, record: dict, result: dict, runner: Runner) -> bool:
    """Send 100% of traffic back to the record's previous revision and read it back, into result['traffic']; whether
    it is verified back. With no previous revision (the first release) nothing moves, and that counts as back."""
    previous = record.get("previous_revision")
    if not previous or previous == NOT_READ_BACK:
        result["traffic"] = "not moved: no previous revision (the first release)"
        return True
    try:
        result["traffic"] = traffic_back(cfg, runner, previous)
    except Exception as exc:  # noqa: BLE001 - recorded, and the record stays pending
        result["traffic"] = f"failed: {type(exc).__name__}"
    return result["traffic"] == f"{previous}=100"


def recover(cfg, record: dict, *, runner: Runner, sleep=time.sleep, drill_restore_fails: bool = False,
            first: bool = False, drill_settle_fails: bool = False) -> dict[str, Any]:
    """Recovery decided from the record's progress (R2-1, R2-8); the results are also stored in record['recovery'].

    From the build execute on, the record says reconcile: pending until every action is done: the settle, a restore
    verified against active_before_build (any template but <new> when that was not read; none runs when it was none, as
    nothing was ACTIVE to go back to), and, when the service deploy was requested, traffic moved back to
    previous_revision. Traffic moves back whatever the settle and restore results: when settle fails, the restore is not
    run and the record stays pending, but traffic still goes back (06-REVIEW WR-11). An unexpected exception in an
    action is recorded as 'failed: <type>' (06-REVIEW WR-02); a pending record is retried by the next release (CR-02).
    drill_restore_fails (a drill release's --drill-fail-restore-once) records the restore as 'failed: drill, not run'
    without running it, so the record stays pending; everything else runs as always. drill_settle_fails (a drill
    release's --drill-fail-settle-once) raises SettleUnknown('drill, not run') inside the settle step, before any
    executions call, so the WR-11 branch runs for real; reconcile() never passes it. `first` (`_first_release`) goes to
    the restore: a first release that fails before its build log is read goes back to no template ACTIVE and is not left
    pending (06-REVIEW WR-06)."""
    result: dict[str, Any] = {}
    record["recovery"] = result
    if not _requested(record, "build_execute"):
        result["restore"] = "not needed: the build was never executed, so nothing could have promoted"
        record["reconcile"] = None
        return result
    record["reconcile"] = "pending"
    try:
        if drill_settle_fails:
            raise SettleUnknown("drill, not run")
        result["settle"] = settle(cfg, record, runner=runner, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 - recorded, and the record stays pending
        result["settle"] = f"unknown: {exc}" if isinstance(exc, SettleUnknown) else f"failed: {type(exc).__name__}"
        result["restore"] = "not run: a build that may still be running could promote after it"
        if _requested(record, "service_deploy"):
            _move_traffic_back(cfg, record, result, runner)
        return result  # pending: the next run settles and restores
    expected = record.get("active_before_build", NOT_READ_BACK)
    if expected is None:
        result["restore"] = "not run: no template was ACTIVE before this build, so there is none to go back to"
        done = True
    elif drill_restore_fails:
        result["restore"] = "failed: drill, not run"
        done = False
    else:
        try:
            result["restore"] = restore(cfg, runner, record["new_template"], expected, first=first, sleep=sleep)
        except Exception as exc:  # noqa: BLE001 - recorded, and the record stays pending
            result["restore"] = f"failed: {type(exc).__name__}"
        done = _restored(result["restore"])
    if _requested(record, "service_deploy"):
        done = _move_traffic_back(cfg, record, result, runner) and done
    record["reconcile"] = None if done else "pending"
    return result


def _print_recovery(record: dict) -> None:
    for key, value in (record.get("recovery") or {}).items():
        print(f"recovery: {key}: {value}", file=sys.stderr)
    restored = str((record.get("recovery") or {}).get("restore", ""))
    rerun = _rerun("drill" in record)  # every drill record has the field, a live one never
    if restored.startswith(("failed", "mismatch", "unverified")):
        print(f"restore {restored}: {record.get('new_template')} may still be ACTIVE, or a template other than the "
              f"one ACTIVE before this release; rerun {rerun}: it retries this release's recovery "
              f"first and starts no build", file=sys.stderr)
    if record.get("reconcile") == "pending":
        print(f"recovery is not finished, so the record stays reconcile: pending: rerun {rerun} to "
              "retry it (it starts no build)", file=sys.stderr)


def reconcile(cfg, *, runner: Runner, out_dir=RELEASES, sleep=time.sleep) -> int | None:
    """When the newest release record is in_progress or reconcile: pending, recover it, mark it interrupted and
    return 1; None when there is nothing to reconcile (R2-11). It never starts a build. The record is saved pending
    before recovering, so a retry that dies is retried again; recover clears pending only when every action is done
    (06-REVIEW CR-02, WR-02). status_before_reconcile keeps the first failed_<step> across retries."""
    record = manifest.latest(out_dir)
    if record is None:
        return None
    pending = record.get("reconcile") == "pending"
    if record.get("status") != "in_progress" and not pending:
        return None
    why = "marked reconcile: pending" if pending else "still in_progress (an earlier run was interrupted)"
    print(f"reconcile: the release record {record['id']} is {why}; settling it first; no build starts in this run",
          file=sys.stderr)
    before = record.get("status")
    record["reconcile"] = "pending"
    manifest.save(manifest.path_for(record, out_dir), record)
    recover(cfg, record, runner=runner, sleep=sleep, first=_first_release(record, out_dir))
    if before not in ("in_progress", "interrupted") and "status_before_reconcile" not in record:
        record["status_before_reconcile"] = before
    record["status"] = "interrupted"
    manifest.save(manifest.path_for(record, out_dir), record)
    _print_recovery(record)
    print(_commit_note(cfg, f"{record['id']}.json", "commit {path}, then run bash deploy/release.sh again",
                       again=True), file=sys.stderr)
    return 1


def rollback(cfg, *, runner: Runner, now: Callable[[], dt.datetime], out_dir=RELEASES, sleep=time.sleep,
             drill_traffic_fails: bool = False) -> int:
    """Back to the latest known-good release's previous template and previous revision, recorded. Traffic moves only
    after a 'restored: <previous template> ACTIVE' result (06-REVIEW CR-02), or after an exact 'nothing to do:
    <previous template> ACTIVE' when an earlier failed_rollback of the same release recorded that restore (a half-done
    rollback, resumed: the record gains `resumes`; 06-REVIEW WR-10). Anything else refuses, moves no traffic and records
    failed_rollback, so that release still counts as known good. A release with no previous revision or no recorded
    previous template is refused before any command. drill_traffic_fails (a drill rollback's
    --drill-fail-rollback-traffic-once) records the traffic move as 'failed: drill, not run' without running it, after
    the real restore, so the next plain --rollback resumes it (06-REVIEW WR-10); a live configuration is refused with
    it before reconcile and before any command."""
    if drill_traffic_fails and not is_drill(cfg):
        print(f"rollback refused: {DRILL_ONLY}", file=sys.stderr)
        return 1
    out_dir = pathlib.Path(out_dir)
    acted = reconcile(cfg, runner=runner, out_dir=out_dir, sleep=sleep)
    if acted is not None:
        return acted
    good = manifest.latest(out_dir, status="known_good")
    if good is None:
        print("rollback refused: there is no known-good release to roll back", file=sys.stderr)
        return 1
    done = manifest.latest(out_dir, kind="rollback", status="rolled_back", where=lambda r: r.get("of") == good["id"])
    if done is not None:
        print(f"rollback: {good['id']} was already rolled back by {done['id']}; nothing to do")
        return 0
    previous = good.get("previous_revision")
    if not previous or previous == NOT_READ_BACK:
        print(f"rollback refused: {good['id']} has no previous revision (the first release); nothing to go back to",
              file=sys.stderr)
        return 1
    expected = good.get("previous_template")
    if expected in (None, NOT_READ_BACK):
        print(f"rollback refused: {good['id']} records no previous template (the template its build retired), so "
              f"there is nothing to put back", file=sys.stderr)
        return 1
    template = good.get("activated_template")
    if template in (None, NOT_READ_BACK):
        template = good["new_template"]
    # a half-done rollback of the same release to resume (WR-10): its restore put `expected` back, its traffic did not
    earlier = next((r for r in manifest.records(out_dir, kind="rollback")
                    if r.get("of") == good["id"] and r.get("status") == "failed_rollback"
                    and (r.get("results") or {}).get("restore") == f"restored: {expected} ACTIVE"), None)
    stamp = now()
    record = {"kind": "rollback", "id": manifest.release_id(stamp, good["source_commit"], "rollback"),
              "time": stamp.isoformat(), "project": cfg["PROJECT"], "region": cfg["REGION"], "of": good["id"],
              "template": template, "previous_template": expected, "previous_revision": previous, "results": {}}
    if is_drill(cfg):  # a live record gains no field
        record["drill"] = {"fail_rollback_traffic_once": drill_traffic_fails}
    path = manifest.open_record(record, out_dir)
    try:
        restored = restore(cfg, runner, template, expected, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 - recorded as a failed rollback
        restored = f"failed: {type(exc).__name__}"
    record["results"]["restore"] = restored
    resume = earlier is not None and restored == f"nothing to do: {expected} ACTIVE"
    if resume:
        record["resumes"] = earlier["id"]
        print(f"rollback: resumes {earlier['id']}: its restore put {expected} back; only the traffic move was left")
    manifest.save(path, record)
    if (restored.startswith("restored: ") or resume) and drill_traffic_fails:
        record["results"]["traffic"] = "failed: drill, not run"  # the drill's forced failure: no command runs
        ok = False
    elif restored.startswith("restored: ") or resume:
        try:
            record["results"]["traffic"] = traffic_back(cfg, runner, previous)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed rollback
            record["results"]["traffic"] = f"failed: {type(exc).__name__}"
        ok = record["results"]["traffic"] == f"{previous}=100"
    else:
        record["results"]["traffic"] = "not moved: the restore did not put the previous template back"
        ok = False
        if restored.startswith("nothing to do: "):
            print(f"rollback refused: {template} is not ACTIVE ({restored}), and no earlier rollback of {good['id']} "
                  f"restored {expected}; no traffic was moved", file=sys.stderr)
        else:
            print(f"rollback refused: the restore of {template} did not put {expected} back ({restored}); no traffic "
                  f"was moved", file=sys.stderr)
    record["status"] = "rolled_back" if ok else "failed_rollback"
    manifest.save(path, record)
    for key, value in record["results"].items():
        print(f"rollback: {key}: {value}")
    print(f"rollback {record['status']}; " + _commit_note(cfg, f"{record['id']}.json", "commit {path}"))
    return 0 if ok else 1


# --- the release ----------------------------------------------------------------------------------------------------

class Progress:
    """The release record on disk (R2-8): each step is written when it starts and when it ends."""

    def __init__(self, record: dict, path: pathlib.Path | None, withhold: list[str], fail_after: str | None = None):
        self.record, self.path, self.withhold, self.fail_after = record, path, withhold, fail_after
        self.current: str | None = None
        self.entry: dict | None = None

    def save(self) -> None:
        if self.path is not None:
            manifest.save(self.path, self.record, withhold=self.withhold)

    def start(self, step: str, state: str = "started") -> None:
        self.current = step
        self.entry = {"step": step, "state": state}
        self.record["progress"].append(self.entry)
        self.save()

    def end(self, **detail) -> None:
        self.entry.update({"state": "ok", **detail})
        self.save()
        if self.current == self.fail_after:  # a drill release's forced failure, after the ok entry is saved
            raise DrillFailure(f"drill: forced failure after {self.current}")

    def requested(self, step: str) -> bool:
        return any(p["step"] == step for p in self.record.get("progress") or [])


def _resources(cfg) -> dict[str, str]:
    return {"service": cfg["SERVICE"], "build_job": cfg["BUILD_JOB"], "bench_job": cfg["BENCH_JOB"],
            "early_bench_job": cfg["EARLY_BENCH_JOB"], "image_repository": cfg["AR_REPO"],
            "image_name": cfg["IMAGE_NAME"], "sql_instance": cfg["SQL_INSTANCE"], "db_name": cfg["DB_NAME"],
            "run_service_account": cfg["RUN_SA_EMAIL"], "deploy_service_account": cfg["DEPLOY_SA_EMAIL"],
            "passcode_secret": cfg["PASSCODE_SECRET"], "hq_passcode_secret": cfg["HQ_PASSCODE_SECRET"]}


def release(cfg: dict[str, str], *, runner: Runner, git, now: Callable[[], dt.datetime], checks: Callable[[], str],
            smoke: Callable[[str, str, str], list], journey: Callable[[str, str, str], dict], out_dir=RELEASES,
            sleep=time.sleep, breaking_template: bool = False, dry_run: bool = False,
            drill_fail_after: str | None = None, drill_restore_fails: bool = False, drill_settle_fails: bool = False,
            drill_unready: bool = False) -> int:
    """The release in the order of the module docstring; 0 when it is known good, 1 otherwise. The caller holds the
    release lock (main). drill_fail_after, drill_restore_fails, drill_settle_fails and drill_unready are the drill
    switches (module docstring, Drill)."""
    if (drill_fail_after or drill_restore_fails or drill_settle_fails or drill_unready) and not is_drill(cfg):
        print(f"release refused: {DRILL_ONLY}", file=sys.stderr)
        return 1
    if not cfg.get("CONTACT_EMAIL", "").strip():  # before any cloud call, dry runs too (11-CONTEXT D-04)
        print("release refused: CONTACT_EMAIL is empty; the About page and the unavailable screen show it. Set it "
              "in the deploy configuration and run again", file=sys.stderr)
        return 1
    out_dir = pathlib.Path(out_dir)
    if dry_run and drill_fail_after:
        print(f"dry run: --drill-fail-after {drill_fail_after}: a real run fails right after {drill_fail_after} ends "
              f"and runs its real recovery")
    if dry_run and drill_restore_fails:
        print('dry run: --drill-fail-restore-once: a real run\'s recovery restore returns "failed: drill, not run" '
              'and leaves reconcile: pending')
    if dry_run and drill_settle_fails:
        print('dry run: --drill-fail-settle-once: a real run\'s recovery settle returns "unknown: drill, not run" '
              "without running, so the restore waits, traffic moves back and the record stays reconcile: pending")
    if dry_run and drill_unready:
        print("dry run: --drill-unready-deploy: a real run's service deploy sets GLOBAL_AI_CAP to a value the app "
              "refuses at import, so its revision never becomes ready, the deploy exits non-zero and its real "
              "recovery runs")
    if dry_run:
        last = manifest.latest(out_dir) or {}
        if last.get("status") == "in_progress" or last.get("reconcile") == "pending":
            print("dry run: an interrupted release record would be reconciled first, and nothing built")
    else:
        acted = reconcile(cfg, runner=runner, out_dir=out_dir, sleep=sleep)
        if acted is not None:
            return acted
    try:
        if git.status_porcelain().strip():
            if not dry_run:
                raise ReleaseError("the tree has uncommitted or untracked changes; commit them first (D-21)")
            print("dry run: the tree has uncommitted or untracked changes; a real release would refuse")
        sha = git.head()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReleaseError("HEAD is not a full commit SHA")
        public = release_data(cfg)  # the public set's local read, before any cloud command, dry runs too (2a)
        if dry_run:
            print("dry run: the local checks (data.reference --release, pytest tests/deploy, the browser probe) are "
                  "skipped")
            dataset_version = NOT_READ_BACK
        else:
            dataset_version = checks()
        if public is not None:
            folder, dataset_version, parts = public
            runner.hide(str(folder), DATA_LABEL)  # before any command is echoed
        engine_version, template_compat = compat(out_dir)
        if template_compat == "breaking" and not breaking_template:
            raise ReleaseError(BREAKING)
    except ReleaseError as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 1

    stamp = now()
    new = f"tpl-{sha[:12]}-{stamp.astimezone(dt.timezone.utc):%Y%m%d%H%M%S}"
    good = known_good(out_dir)
    record: dict[str, Any] = {
        "kind": "release", "id": manifest.release_id(stamp, sha), "status": "in_progress",
        "time": stamp.isoformat(), "project": cfg["PROJECT"], "region": cfg["REGION"], "source_commit": sha,
        "engine_version": engine_version, "template_compat": template_compat, "dataset_version": dataset_version,
        "model": {"configured": cfg["GEMINI_MODEL"], "endpoint": cfg["VERTEX_LOCATION"], "used": "not until Phase 9"},
        "resources": _resources(cfg), "new_template": new, "previous_known_good": good["id"] if good else None,
        "progress": [], "reconcile": None, "recovery": None,
    }
    if is_drill(cfg):  # a live record gains no field
        record["drill"] = {"fail_after": drill_fail_after, "fail_restore_once": drill_restore_fails,
                           "fail_settle_once": drill_settle_fails, "unready_deploy": drill_unready}
    withhold: list[str] = []
    path = None if dry_run else manifest.open_record(record, out_dir)
    p = Progress(record, path, withhold, fail_after=None if dry_run else drill_fail_after)
    project, region = cfg["PROJECT"], cfg["REGION"]
    try:
        p.start("secret")  # first, before any image, build, job or deploy command (06.1 D-64)
        version = secret_version(cfg, runner, "PASSCODE_SECRET")
        hq_version = secret_version(cfg, runner, "HQ_PASSCODE_SECRET")
        record.update(secret_version=version, hq_secret_version=hq_version)
        if dry_run:
            print("dry run: the HQ preflight is skipped (a real run reads both pinned versions into memory and "
                  "refuses an HQ value that is empty, shorter than 12 characters or equal to the store passcode)")
            p.end(version=version, hq_version=hq_version, preflight="skipped (dry run)")
        else:
            hq_preflight(cfg, runner, version, hq_version)
            p.end(version=version, hq_version=hq_version, preflight="ok")

        if public is not None:
            p.start("data")
            upload_data(cfg, runner, folder, dataset_version, parts)
            p.end(dataset_version=dataset_version, parts=len(parts))

        p.start("image")
        with tempfile.TemporaryDirectory(prefix="shelfshift-src-") as src:
            git.archive(sha, src)
            record["reference_files"] = reference_hashes(src)
            ref, digest = image(cfg, runner, sha, src)
        record.update(image=ref, image_digest=digest)
        early = manifest.latest(out_dir, kind="early_bench", where=lambda r: r.get("image_digest") == digest)
        record["early_bench"] = early["id"] if early else None
        p.end(digest=digest)

        p.start("previous_revision")
        record["previous_revision"], before = revisions_before(cfg, runner)
        p.end(revision=record["previous_revision"])

        p.start("build_deploy")
        build_args = ["-m", "api.sandbox", "build"] + ([f"{DATA_MOUNT}/{dataset_version}"] if public else [])
        deploy_job(cfg, runner, cfg["BUILD_JOB"], ref, cfg["DEPLOY_SA_EMAIL"], build_env(cfg, sha), build_args,
                   timeout=BUILD_JOB_TIMEOUT, extra=data_volume(cfg) if public else ())
        p.end()

        p.start("build_execute", "requested")  # written before the call is issued (R2-8)
        execution = execute_job(cfg, runner, cfg["BUILD_JOB"], [*build_args, "--run-id", new])
        if execution == NOT_READ_BACK:
            raise ReleaseError("the build execute returned no execution name")
        record["build_execution"] = execution
        p.end(execution=execution)

        p.start("build_log")
        lines = job_lines(cfg, runner, cfg["BUILD_JOB"], execution, r"^retired: ", sleep)
        record.update(parse_build(lines))
        if record["activated_template"] not in (new, NOT_READ_BACK):
            raise ReleaseError(f"the build activated {record['activated_template']}, not {new}")
        if NOT_READ_BACK in (record["active_before_build"], record["previous_template"]):
            raise ReleaseError("the build log's previous ACTIVE: or retired: line was not read, so the template a "
                               "recovery must restore is unknown")
        if public is not None and dry_run:
            print(f"dry run: a real run fails at build_log unless the build log's 'template <id> ACTIVE for <dv>' "
                  f"line names {dataset_version}, the uploaded dataset version")
        elif public is not None:
            built = next((m.group(1) for line in (lines if isinstance(lines, list) else [])
                          for m in [re.match(r"^template \S+ ACTIVE for (\S+)$", line)] if m), None)
            if built != dataset_version:
                raise ReleaseError(f"the build log's ACTIVE for line names {built or 'nothing read'}, not the uploaded "
                                   f"dataset version {dataset_version}")
        p.end()

        p.start("bench")
        bench_args, bench_gate = (PUBLIC_BENCH_ARGS, PUBLIC_GATE) if public else (["-m", "api.bench"], BENCH_GATE)
        bench_exec = job(cfg, runner, cfg["BENCH_JOB"], ref, cfg["RUN_SA_EMAIL"], bench_env(cfg, sha), bench_args)
        # a public run waits for its last gate line (resets come after the cases)
        lines = job_lines(cfg, runner, cfg["BENCH_JOB"], bench_exec,
                          RESETS_GATE.pattern if public else bench_gate.pattern, sleep)

        def gate_line(pattern):
            return next((line for line in lines if pattern.match(line)), NOT_READ_BACK) \
                if isinstance(lines, list) else NOT_READ_BACK

        gate = gate_line(bench_gate)
        record["bench"] = {"execution": bench_exec, "result": "pass", "gate": gate, "lines": lines}
        if public:
            record["bench"]["resets"] = resets = gate_line(RESETS_GATE)
            missing = [name for name, got in (("'top 20: ... slowest of 3 = <s> s ...'", gate),
                                              ("'resets: ... (budget 2000 ms)'", resets)) if got == NOT_READ_BACK]
            if missing:
                record["bench"]["result"] = "fail"
                raise ReleaseError(f"the public bench's {' and '.join(missing)} line was not read back, so the 3 s "
                                   "case and 2 s reset budgets are unproven")
        p.end()

        p.start("service_deploy", "requested")  # written before the call is issued (R2-8)
        note = {}
        try:
            deploy_service(cfg, runner, ref, sha, version, hq_version, unready=drill_unready)
        except CommandError as exc:
            # The public binding can be refused after the revision is up (R1-1): read back before calling it failed.
            try:
                rb = read_back(cfg, runner)
            except Exception:  # noqa: BLE001 - the deploy's own failure is the one to report
                raise exc from None
            # only the revision this deploy created (WR-09): the latest created one now, a name that did not exist
            # before the deploy, ready, and with the built digest
            if (rb["revision"] in (record["previous_revision"], NOT_READ_BACK) or rb["revision"] in before
                    or rb["revision"] != rb["latest_created"] or rb["revision_digest"] != digest):
                raise exc
            note = {"note": "the deploy exited non-zero, but the revision it created is ready by digest; traffic is "
                            "sent to it and the read-back and access check decide"}
        route_latest(cfg, runner)
        p.end(**note)

        p.start("read_back")
        rb = read_back(cfg, runner)
        record.update(url=rb["url"], revision=rb["revision"], min_instances=rb["min_instances"],
                      max_instances=rb["max_instances"], annotations=rb["annotations"],
                      traffic_percent=rb["traffic_percent"], revision_digest=rb["revision_digest"])
        problems = read_back_problems(rb, cfg, digest)
        if problems:
            record["reason"] = "; ".join(problems)
            raise ReleaseError("read-back: " + "; ".join(problems))
        p.end()

        p.start("access")
        record["access"] = access(cfg, runner)
        p.end(access=record["access"])

        p.start("smoke")
        if dry_run:
            print("dry run: both passcodes, the smoke and the journey are skipped")
            record["smoke"] = record["journey"] = NOT_READ_BACK
            p.end()
        else:
            code = passcode(cfg, runner, version)
            hq = hq_passcode(cfg, runner, hq_version)
            withhold.extend((code, hq))
            steps = smoke(rb["url"], code, hq)
            record["smoke"] = [[s, status] for s, status in steps]
            failed = [s for s, status in steps if status.startswith("FAIL")]
            if failed:
                raise ReleaseError(f"smoke failed: {', '.join(failed)}")
            p.end()

            p.start("journey")
            record["journey"] = journey(rb["url"], code, hq)
            if not record["journey"].get("passed"):
                raise ReleaseError(f"the deployed journey failed (exit {record['journey'].get('exit')})")
            p.end()
    except Exception as exc:  # noqa: BLE001 - every failure is recorded and recovered; a kill leaves in_progress
        record["status"] = STEP_STATUS.get(p.current, "failed_image")
        drill = isinstance(exc, DrillFailure)
        if p.entry is not None and not drill:  # a forced failure leaves its step's entry ok
            p.entry["state"] = "failed"
        record["error"] = runner.mask(str(exc))
        if drill:
            record["reason"] = "drill"
        elif isinstance(exc, AccessRefused) or p.current == "access":
            record["reason"] = "access"
        record["reconcile"] = "pending"  # saved before recovering, so a recovery that dies is retried (WR-02)
        p.save()
        print(f"release failed at {p.current} ({record['status']}): {record['error']}", file=sys.stderr)
        recover(cfg, record, runner=runner, sleep=sleep, drill_restore_fails=drill_restore_fails,
                first=_first_release(record, out_dir), drill_settle_fails=drill_settle_fails)
        p.save()
        _print_recovery(record)
        if path is not None:
            print(_commit_note(cfg, f"{record['id']}.json", "commit the record {path} before the next release"),
                  file=sys.stderr)
        return 1

    if dry_run:
        print("dry run: complete; nothing was run, and the release record and manifest are skipped")
        return 0
    record["status"] = "known_good"
    p.save()
    print(f"release {record['id']}: known good, revision {record['revision']} at {record['url']}")
    print(_commit_note(cfg, f"{record['id']}.json", "commit the manifest {path} before the next release"))
    return 0


# --- the early bench (D-15, D-19) ----------------------------------------------------------------------------------

def newest_execution(cfg, runner: Runner, job_name: str) -> str:
    """The job's newest execution by creation time (one operator under the lock: ours); NOT_READ_BACK when none."""
    try:
        rows = runner.json(gcloud("run", "jobs", "executions", "list", "--job", job_name, "--region", cfg["REGION"],
                                  project=cfg["PROJECT"])) or []
    except CommandError:
        return NOT_READ_BACK
    named = [r["metadata"] for r in rows if isinstance(r, dict) and (r.get("metadata") or {}).get("name")]
    if not named:
        return NOT_READ_BACK
    return max(named, key=lambda m: str(m.get("creationTimestamp", "")))["name"]


def early_status(job_ok: bool, median_line, match) -> str:
    """pass: the job succeeded, its median line came back (printed at most 3.00; the unrounded value and the job's
    exit decide, 06-04) and the job read the archived input; fail: the job failed with a median line at or above
    3.00; error: anything else (no median line, another input, or a job outcome its median line contradicts)."""
    m = EARLY_MEDIAN.match(median_line) if isinstance(median_line, str) else None
    if m is None or match is not True:
        return "error"
    printed = float(m.group(1))
    if job_ok and printed <= 3.0:
        return "pass"
    if not job_ok and printed >= 3.0:
        return "fail"
    return "error"


def early_bench(cfg: dict[str, str], *, runner: Runner, git, now: Callable[[], dt.datetime],
                checks: Callable[[], str], out_dir=RELEASES, sleep=time.sleep, dry_run: bool = False) -> int:
    """The early Cloud Run bench with no database (D-15); 0 on pass, 1 on fail or error. The caller holds the lock."""
    out_dir = pathlib.Path(out_dir)
    try:
        if git.status_porcelain().strip():
            if not dry_run:
                raise ReleaseError("the tree has uncommitted or untracked changes; commit them first (D-21)")
            print("dry run: the tree has uncommitted or untracked changes; a real early bench would refuse")
        sha = git.head()
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ReleaseError("HEAD is not a full commit SHA")
        if dry_run:
            print("dry run: the local checks (data.reference --release, pytest tests/deploy) are skipped")
        else:
            checks()
    except ReleaseError as exc:
        print(f"early bench refused: {exc}", file=sys.stderr)
        return 1

    stamp = now()
    evidence: dict[str, Any] = {
        "kind": "early_bench", "id": manifest.release_id(stamp, sha, "early-bench"), "time": stamp.isoformat(),
        "project": cfg["PROJECT"], "region": cfg["REGION"], "source_commit": sha, "job_name": cfg["EARLY_BENCH_JOB"],
        "shape": {"cpu": "1", "memory": "1Gi", "ENGINE_WORKER": "on"}, "status": "error",
    }
    job_name = cfg["EARLY_BENCH_JOB"]
    try:
        with tempfile.TemporaryDirectory(prefix="shelfshift-src-") as src:
            git.archive(sha, src)
            frozen = pathlib.Path(src) / BENCH_INPUT
            local = hashlib.sha256(frozen.read_bytes()).hexdigest() if frozen.is_file() else NOT_READ_BACK
            ref, digest = image(cfg, runner, sha, src)
        evidence.update(image=ref, image_digest=digest)
        deploy_job(cfg, runner, job_name, ref, cfg["RUN_SA_EMAIL"], {"ENGINE_WORKER": "on", "GIT_COMMIT": sha},
                   EARLY_ARGS)
        try:
            execution, job_ok = execute_job(cfg, runner, job_name), True
        except CommandError as exc:  # the job failed (a median at or over 3 s exits 1); find it to read its log
            execution, job_ok = newest_execution(cfg, runner, job_name), False
            evidence["job_error"] = runner.mask(str(exc))
        evidence.update(execution=execution, job="succeeded" if job_ok else "failed")
        lines = job_lines(cfg, runner, job_name, execution, EARLY_MEDIAN.pattern, sleep)
        lines = lines if isinstance(lines, list) else []
        printed = next((m.group(1) for line in lines if line.startswith("input: ")
                        for m in [re.search(r"sha256=([0-9a-f]{64})", line)] if m), NOT_READ_BACK)
        evidence["input"] = {"path": BENCH_INPUT, "sha256_local": local, "sha256_job": printed,
                             "match": NOT_READ_BACK if NOT_READ_BACK in (local, printed) else local == printed}
        evidence["config"] = next((x for x in lines if x.startswith("config:")), NOT_READ_BACK)
        evidence["startup"] = next((x for x in lines if x.startswith("in-process startup")), NOT_READ_BACK)
        evidence["runs"] = [x for x in lines if re.match(r"^run \d+: ", x)]
        evidence["median"] = next((x for x in lines if EARLY_MEDIAN.match(x)), NOT_READ_BACK)
        evidence["info"] = [x for x in lines if x.startswith("info, not the gate:")]
        evidence["status"] = early_status(job_ok, evidence["median"], evidence["input"]["match"])
    except Exception as exc:  # noqa: BLE001 - recorded as error evidence, never a silent pass
        evidence["status"] = "error"
        evidence["error"] = runner.mask(str(exc))
        print(f"early bench failed: {evidence['error']}", file=sys.stderr)
    if dry_run:
        print("dry run: complete; nothing was run, and the early-bench evidence file is skipped")
        return 0
    path = manifest.write(evidence, out_dir)
    print(f"early bench {evidence['status']}: {evidence.get('median', NOT_READ_BACK)}")
    if evidence["status"] != "pass":
        print("a median at or above 3 s stops the deployment for investigation; the 3 s budget is never raised",
              file=sys.stderr)
    print(_commit_note(cfg, path.name, "commit the evidence {path} with the releases"))
    return 0 if evidence["status"] == "pass" else 1


# --- status (read-only) ----------------------------------------------------------------------------------------------

ACTIVE_ARGS = ["-m", "api.sandbox", "active"]


def active_now(cfg, runner: Runner, *, sleep=time.sleep) -> str:
    """The template ACTIVE now, from one build-job execution of `api.sandbox active` (read-only): a run id, `none`, or
    `unknown (<why>)` when it cannot be read. A failed execution never claims a cause: gcloud itself may have failed
    (signed out), or the build job is missing or its image predates api.sandbox active."""
    try:
        execution = execute_job(cfg, runner, cfg["BUILD_JOB"], ACTIVE_ARGS)
    except CommandError as exc:
        return (f"unknown (the build job's execution failed, exit {exc.returncode}: gcloud itself may have failed, for "
                f"example when signed out, or the build job is missing or its image predates api.sandbox active)")
    if execution == NOT_READ_BACK:
        return "unknown (the execution's name was not read back)"
    try:
        lines = job_lines(cfg, runner, cfg["BUILD_JOB"], execution, r"^ACTIVE: ", sleep)
    except CommandError:
        return "unknown (the job's log could not be read)"
    found = next((m.group(1) for line in (lines if isinstance(lines, list) else [])
                  for m in [re.fullmatch(r"ACTIVE: (\S+)", line)] if m), None)
    return found or "unknown (the job's log holds no ACTIVE line)"


def status(cfg, *, runner: Runner, out_dir=RELEASES, sleep=time.sleep) -> int:
    """Print the service's traffic, the serving revision, the ACTIVE template, the newest records and three checks
    (the module docstring's Status paragraph); 0 once printed. Writes nothing, never reads the passcode, prints no
    URL and no command's stderr. The caller holds the release lock (main)."""
    out_dir = pathlib.Path(out_dir)
    history = manifest.records(out_dir)

    def label(revision) -> str:
        rec = next((r for r in history if revision and r.get("revision") == revision), None)
        return f"release {rec['id']}" if rec else "not in the records"

    svc: dict = {}
    unreadable = False  # the describe failed for a reason other than not found
    try:
        svc = _service(cfg, runner)
        if not svc:
            print("service: not deployed yet")
    except CommandError as exc:
        unreadable = not _not_found(exc)
        print(f"service: could not be read ({exc.argv0} exited {exc.returncode})" if unreadable else
              "service: not deployed yet")
    now = None
    if svc:
        st = svc.get("status") or {}
        for t in st.get("traffic") if isinstance(st.get("traffic"), list) else []:
            if isinstance(t, dict):
                rev = _revision_of(t, st)
                print(f"traffic: {int(t.get('percent') or 0)}% to {rev or '(unnamed)'} [{label(rev)}]")
        latest = st.get("latestReadyRevisionName")
        print(f"latest ready: {latest} [{label(latest)}]" if latest else "latest ready: none")
        try:
            now = serving(svc)
            print(f"serving: {now} [{label(now)}]")
        except ReleaseError:
            print("serving: none (no revision is ready; the next release is a first release)" if _never_ready(st) else
                  "serving: none (the traffic is split or unreadable; a release would refuse)")
    active = active_now(cfg, runner, sleep=sleep)
    print(f"ACTIVE: {active}")

    newest = manifest.latest(out_dir)
    pending = ", reconcile: pending" if newest and newest.get("reconcile") == "pending" else ""
    print(f"newest record: {newest['id']} {newest.get('status')}{pending}" if newest else "newest record: none")
    undo = manifest.latest(out_dir, kind="rollback")
    if undo:
        print(f"newest rollback: {undo['id']} {undo.get('status')}")
    good = known_good(out_dir)
    template = None
    if good:
        template = good.get("activated_template")
        if template in (None, NOT_READ_BACK):
            template = good.get("new_template")
        print(f"known good: {good['id']} (template {template})")
    else:
        print("known good: none")

    def check(ok: bool | None, why: str) -> str:
        return f"unknown ({why})" if ok is None else ("yes" if ok else "no")

    if good is None:
        serving_ok = active_ok = previous = None
        why = why_active = "no known-good release"
    else:
        serving_ok = None if now is None else now == good.get("revision")
        why = ("the service could not be read" if unreadable else
               "no service yet" if not svc else "no single serving revision")
        active_ok = None if active.startswith("unknown") else active == template
        why_active = "ACTIVE could not be read"
        previous = good.get("previous_revision")
    print(f"check: serving is the known-good revision: {check(serving_ok, why)}")
    print(f"check: ACTIVE is the known-good template: {check(active_ok, why_active)}")
    if good is None:
        print("check: the known-good release's previous revision: unknown (no known-good release)")
    elif previous in (None, NOT_READ_BACK):
        print("check: the known-good release's previous revision: none (the first release)")
    else:
        print(f"check: the known-good release's previous revision: [{label(previous)}]")
    return 0


# --- dry run --------------------------------------------------------------------------------------------------------

def dry_run_rules(cfg: dict[str, str], calls: list[list[str]]) -> list[tuple[Callable[[list[str]], bool], Any]]:
    """Scripted answers for the dry run: a fresh project where everything succeeds. `calls` is the DryRunner's
    record, so the image appears only after its build and the service only after its deploy."""
    def did(*lead):
        return any(c[1:1 + len(lead)] == list(lead) for c in calls)

    def is_(*lead):
        return lambda a: a[1:1 + len(lead)] == list(lead)

    digest = "sha256:" + "0" * 64
    service, region = cfg["SERVICE"], cfg["REGION"]
    revision = f"{service}-00001-dry"
    svc = {"metadata": {"annotations": {"run.googleapis.com/minScale": cfg["MIN_INSTANCES"],
                                        "run.googleapis.com/maxScale": MAX_INSTANCES}},
           "spec": {"template": {"metadata": {"annotations": {"run.googleapis.com/execution-environment": "gen2"}}}},
           "status": {"url": f"https://{service}.example.invalid", "latestReadyRevisionName": revision,
                      "traffic": [{"revisionName": revision, "percent": 100}]}}
    build_lines = ["previous ACTIVE: none", "retired: none"]  # the activated id is the chosen one, not scripted
    bench_lines = ["SUS-19: evaluate + fact checks, slowest of 3 = 1.00 s (budget 3 s)",
                   "SUS-19 (frozen input): evaluate + fact checks, median of 3 = 1.00 s (budget 3 s)",
                   # api.bench's public mode (a public set's release gate)
                   "top 20: evaluate + fact checks, slowest of 3 = 1.00 s on dry-run (budget 3 s); 0 not timed",
                   "resets: 10, slowest 100 ms (budget 2000 ms)"]
    return [
        (lambda a: is_("artifacts", "docker", "images", "describe")(a) and did("builds", "submit"),
         {"image_summary": {"digest": digest}}),
        (is_("secrets", "versions", "list"), [{"name": "versions/1", "state": "ENABLED"}]),
        (lambda a: is_("run", "services", "describe")(a) and did("run", "deploy"), svc),
        (is_("run", "revisions", "describe"), {"status": {"imageDigest": f"{image_repo(cfg)}@{digest}"}}),
        (lambda a: is_("run", "jobs", "execute")(a) and any(x.startswith("--args=-m,api.sandbox,restore,") for x in a),
         {"metadata": {"name": "dry-run-restore"}}),
        (lambda a: is_("run", "jobs", "execute")(a) and f"--args={','.join(ACTIVE_ARGS)}" in a,
         {"metadata": {"name": "dry-run-active"}}),
        (is_("run", "jobs", "execute"), {"metadata": {"name": "dry-run-execution"}}),
        (lambda a: is_("logging", "read")(a) and cfg["EARLY_BENCH_JOB"] in a[3],
         [{"textPayload": x} for x in bench_lines[1:]]),
        (lambda a: is_("logging", "read")(a) and "dry-run-restore" in a[3], [{"textPayload": "ACTIVE: none"}]),
        (lambda a: is_("logging", "read")(a) and "dry-run-active" in a[3], [{"textPayload": "ACTIVE: none"}]),
        (lambda a: is_("logging", "read")(a) and cfg["BUILD_JOB"] in a[3], [{"textPayload": x} for x in build_lines]),
        (is_("logging", "read"), [{"textPayload": x} for x in bench_lines]),
        (is_("run", "services", "get-iam-policy"),
         {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}),
    ]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def main(argv: list[str] | None = None, *, runner: Runner | None = None, git=None, checks=None, smoke=None,
         journey=None, now=None, sleep=None, out_dir=None) -> int:
    """The CLI; the keywords let tests hand in fakes (the CLI makes the real ones)."""
    ap = argparse.ArgumentParser(prog="bash deploy/release.sh", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--early-bench", action="store_true",
                      help="time the image on Cloud Run's 1 vCPU with the frozen input and no database (D-15)")
    mode.add_argument("--rollback", action="store_true",
                      help="restore the latest known-good release's previous template and revision")
    mode.add_argument("--status", action="store_true",
                      help="read-only: print the service's traffic, the serving revision, the ACTIVE template (one "
                           "build-job run of api.sandbox active) and the newest records; writes nothing")
    ap.add_argument("--breaking-template", action="store_true",
                    help="release a template the image now serving cannot read (another engine version): a "
                         "visitor's sandbox opened during the release fails loudly with 409 engine_version; use it "
                         "only while no judge is using the app")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ordered command list and run nothing (needs no gcloud; no lock, no record)")
    ap.add_argument("--env-file", type=pathlib.Path, default=None,
                    help="the configuration to read (default: the local configuration, envfile.LOCAL)")
    ap.add_argument("--drill-fail-after", choices=DRILL_STEPS, metavar="STEP", default=None,
                    help="drill configurations only: fail the release right after STEP ends, so its real recovery "
                         "runs (deploy/README.md step 11)")
    ap.add_argument("--drill-fail-restore-once", action="store_true",
                    help="drill configurations only, with --drill-fail-after: the recovery's first restore returns "
                         "failed and is left for the next run to retry")
    ap.add_argument("--drill-fail-settle-once", action="store_true",
                    help="drill configurations only, with --drill-fail-after: the recovery's settle returns unknown "
                         "without running, so the restore waits and traffic still moves back (WR-11)")
    ap.add_argument("--drill-unready-deploy", action="store_true",
                    help="drill configurations only, alone: the service deploy carries a value the app refuses at "
                         "import, so its revision never becomes ready and the real recovery runs (WR-07, WR-09)")
    ap.add_argument("--drill-fail-rollback-traffic-once", action="store_true",
                    help="drill configurations only, with --rollback: the rollback's traffic move returns failed "
                         "after its real restore, for the next --rollback to resume (WR-10)")
    args = ap.parse_args(argv)
    others = args.drill_fail_after is not None or args.drill_fail_restore_once or args.drill_fail_settle_once
    if (others or args.drill_unready_deploy) and (args.rollback or args.early_bench or args.status):
        ap.error("--drill-fail-after, --drill-fail-restore-once, --drill-fail-settle-once and --drill-unready-deploy "
                 "run only with a release")
    if args.drill_fail_rollback_traffic_once and not args.rollback:
        ap.error("--drill-fail-rollback-traffic-once runs only with --rollback")
    if args.drill_fail_restore_once and args.drill_fail_after is None:
        ap.error("--drill-fail-restore-once needs --drill-fail-after STEP")
    if args.drill_fail_settle_once and args.drill_fail_after is None:
        ap.error("--drill-fail-settle-once needs --drill-fail-after STEP")
    if args.drill_fail_settle_once and args.drill_fail_restore_once:
        ap.error("--drill-fail-settle-once and --drill-fail-restore-once are not combined: with the settle failed, no "
                 "restore runs")
    if args.drill_unready_deploy and others:
        ap.error("--drill-unready-deploy runs without the other drill flags: its failing service deploy is the forced "
                 "failure")
    env_file = args.env_file or envfile.LOCAL  # read at call time, so tests can point it elsewhere
    picked = out_dir
    git = git or Git()
    now = now or _now
    if args.dry_run:  # no lock: a dry run reaches nothing
        cfg = _config(env_file)
        if cfg is None:
            return 1
        out_dir = _records_for(cfg, picked)
        dry = runner if runner is not None else DryRunner()
        if isinstance(dry, DryRunner) and not dry.rules:
            dry.rules = dry_run_rules(cfg, dry.calls)
        script = "bench-early.sh" if args.early_bench else "release.sh"
        print(f"dry run: the commands bash deploy/{script} would run; nothing is run")
        if args.rollback:
            return _dry_rollback(cfg, dry, drill_traffic_fails=args.drill_fail_rollback_traffic_once)
        if args.status:
            code = status(cfg, runner=dry, out_dir=out_dir, sleep=lambda s: None)
            print("dry run: complete; nothing was run")
            return code
        if args.early_bench:
            return early_bench(cfg, runner=dry, git=git, now=now, checks=lambda: NOT_READ_BACK, out_dir=out_dir,
                               sleep=lambda s: None, dry_run=True)
        return release(cfg, runner=dry, git=git, now=now, checks=lambda: NOT_READ_BACK, smoke=run_smoke,
                       journey=run_journey, out_dir=out_dir, sleep=lambda s: None, dry_run=True,
                       drill_fail_after=args.drill_fail_after, drill_restore_fails=args.drill_fail_restore_once,
                       drill_settle_fails=args.drill_fail_settle_once, drill_unready=args.drill_unready_deploy)
    try:
        with release_lock():  # before anything that could reach the cloud (R2-11)
            cfg = _config(env_file)
            if cfg is None:
                return 1
            out_dir = _records_for(cfg, picked)
            runner = runner or Runner()
            sleep = sleep or time.sleep
            if args.rollback:
                return rollback(cfg, runner=runner, now=now, out_dir=out_dir, sleep=sleep,
                                drill_traffic_fails=args.drill_fail_rollback_traffic_once)
            if args.status:
                return status(cfg, runner=runner, out_dir=out_dir, sleep=sleep)
            if args.early_bench:
                return early_bench(cfg, runner=runner, git=git, now=now,
                                   checks=checks or local_checks(env_file, browser=False), out_dir=out_dir,
                                   sleep=sleep)
            return release(cfg, runner=runner, git=git, now=now,
                           checks=checks or local_checks(env_file, browser=True), smoke=smoke or run_smoke,
                           journey=journey or run_journey, out_dir=out_dir, sleep=sleep,
                           breaking_template=args.breaking_template, drill_fail_after=args.drill_fail_after,
                           drill_restore_fails=args.drill_fail_restore_once,
                           drill_settle_fails=args.drill_fail_settle_once, drill_unready=args.drill_unready_deploy)
    except ReleaseLocked as exc:
        print(exc, file=sys.stderr)
        return 1


def _config(env_file) -> dict[str, str] | None:
    """The validated configuration, or None after one `config error:` line per problem (keys only): an invalid file,
    or a mix of live and drill names (`is_drill`)."""
    try:
        cfg = envfile.load(env_file)
        is_drill(cfg)
        return cfg
    except (envfile.ConfigError, ReleaseError) as exc:
        for problem in str(exc).splitlines():
            print(f"config error: {problem}", file=sys.stderr)
        return None


def _records_for(cfg, out_dir) -> pathlib.Path:
    """The records directory a run uses: the caller's (tests), else the configuration's (`records_dir`); a drill
    configuration says so in one line first."""
    if is_drill(cfg):
        print("drill configuration: records go to deploy/releases-drill/ (git-ignored)")
    return records_dir(cfg) if out_dir is None else out_dir


def _dry_rollback(cfg, dry: Runner, drill_traffic_fails: bool = False) -> int:
    """The rollback's two commands, with the values a real rollback reads from the latest known-good manifest. With
    drill_traffic_fails (a drill configuration only, refused for a live one) the traffic command is skipped."""
    if drill_traffic_fails and not is_drill(cfg):
        print(f"rollback refused: {DRILL_ONLY}", file=sys.stderr)
        return 1
    if drill_traffic_fails:
        print('dry run: --drill-fail-rollback-traffic-once: a real rollback\'s traffic move returns "failed: drill, '
              'not run" after its real restore, so it records failed_rollback and the next --rollback resumes it')
    restore(cfg, dry, "tpl-<the latest known-good release's template>", "tpl-<its previous template>",
            sleep=lambda s: None)
    if not drill_traffic_fails:
        traffic_back(cfg, dry, "<its previous revision>")
    print("dry run: complete; nothing was run, and the rollback record is skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
