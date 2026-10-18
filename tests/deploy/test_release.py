"""deploy/release.py and deploy/manifest.py: the release the user runs (06-CONTEXT D-01, D-09, D-10, D-17, D-20,
D-21, D-24; OPS-02, OPS-05, OPS-07; 06-REVIEWS R1-1, R2-1, R2-8, R2-10, R2-11).

Nothing here talks to Google Cloud. The configuration is tests.deploy.synthetic_env's (project x-demo-12), every
command goes to `Cloud`, a scripted DryRunner that answers like Cloud Run would and runs nothing, git is a fake, the
clock is fixed and every sleep is recorded instead of waited. The CLI tests run with a PATH that holds no gcloud.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys

import pytest

from deploy import envfile, manifest, release
from deploy.cloud import CommandError, DryRunner
from engine.model import ENGINE_VERSION
from tests.deploy import synthetic_env
from tests.deploy.synthetic_env import SYNTHETIC

ROOT = pathlib.Path(__file__).resolve().parents[2]
NO_GCLOUD_PATH = "/usr/bin:/bin"  # macOS ships no gcloud there
PROJECT = SYNTHETIC["PROJECT"]
REGION = "asia-southeast1"
RUN_SA = SYNTHETIC["RUN_SA_EMAIL"]
DEPLOY_SA = SYNTHETIC["DEPLOY_SA_EMAIL"]
SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = dt.datetime(2026, 10, 16, 14, 5, 6, tzinfo=dt.timezone.utc)
RID = "20261016T140506Z-0123456789ab"
NEW = "tpl-0123456789ab-20261016140506"
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/demo-repo/demo-image"
DIGEST = "sha256:" + "a" * 64
REF = f"{IMAGE}@{DIGEST}"
PASSCODE = "fake-passcode-7q"  # synthetic: what the scripted secret access returns
HQ_PASSCODE = "fake-hq-passcode-42"  # synthetic: the HQ secret's value (at least 12 characters, not PASSCODE)
BUILD_EXEC = "demo-build-x7k2p"
RESTORE_EXEC = "demo-build-r5t6y"
ACTIVE_EXEC = "demo-build-a8c3d"  # a --status run of api.sandbox active
BENCH_EXEC = "demo-bench-q9w4r"
NEW_REV = "demo-svc-00002-new"
OLD_REV = "demo-svc-00001-old"
FAILED_REV = "demo-svc-00002-failed"
URL = "https://demo-svc-abc.a.run.example.invalid"
REF_FILES = {"seed.json": b'{"seed": 1}\n', "forecast.json": b'{"forecast": 2}\n', "truth.json": b'{"truth": 3}\n'}
# Phase 9: the build leaves the template READY (--no-activate); the briefs job and the activate execution follow
BUILD_LINES = ["previous ACTIVE: tpl-old", f"template {NEW} READY for dv-1", "pruned 0 sandbox runs"]
ACTIVATE_LINES = [f"template {NEW} ACTIVE for dv-1", "retired: tpl-old"]
BRIEFS_EXEC = "demo-build-b2r9f"
ACTIVATE_EXEC = "demo-build-v4n1k"
SUMMARY = {"type": "summary", "run_id": NEW, "cases": 6, "eligible": 5, "ineligible": 1, "attempted": 5, "success": 5,
           "failure": 0, "reused": 0, "model_id": "gemini-3.5-flash", "location": "asia-southeast1",
           "assistant_version": "assistant-3", "usage": {"calls": 5, "input_tokens": 9000, "output_tokens": 1200},
           "baseline_attempts": 5, "baseline_usage": {"calls": 5, "input_tokens": 9000, "output_tokens": 1200},
           "cost": None, "cost_reason": "Billed cost is not recorded here; retain provider usage for the release record."}


def summary_line(**changes) -> str:
    """The briefs CLI's own summary format (agent/briefs.py: json.dumps(..., sort_keys=True))."""
    return json.dumps({**SUMMARY, **changes}, ensure_ascii=False, sort_keys=True)


def briefs_case_line(case_id: str = "C-1") -> str:
    return json.dumps({"type": "case", "run_id": NEW, "case_id": case_id, "status": "success"}, sort_keys=True)
RESTORED_LINES = [f"restored: tpl-old ACTIVE, {NEW} RETIRED", "ACTIVE: tpl-old"]
GOOD_TPL = "tpl-aaaaaaaaaaaa-20261001000000"
BENCH_LINES = ["config: ENGINE_WORKER=on engine_version=engine-3 git_commit=x cpu.max=100000/100000",
               "in-process startup (not a platform cold start): imports 0.50 s, worker warm-up 0.30 s",
               "run 1: 1.10 s", "run 2: 1.00 s", "run 3: 1.20 s",
               "SUS-19: evaluate + fact checks, slowest of 3 = 1.20 s (budget 3 s)"]
BENCH_INPUT = b'{"engine_version": "engine-3", "frozen": true}\n'
INPUT_SHA = hashlib.sha256(BENCH_INPUT).hexdigest()
EARLY_EXEC = "demo-early-m3n8v"
EARLY_ARGS = "-m,api.bench,--input,api/bench_input.json,--shortlist-info"


def early_lines(median: str = "1.10", sha: str = INPUT_SHA) -> list[str]:
    return ["config: ENGINE_WORKER=on engine_version=engine-3 git_commit=x cpu.max=100000/100000",
            f"input: api/bench_input.json sha256={sha}",
            "in-process startup (not a platform cold start): imports 0.50 s, worker warm-up 0.30 s",
            "run 1: 1.20 s", f"run 2: {median} s", "run 3: 1.00 s",
            f"SUS-19 (frozen input): evaluate + fact checks, median of 3 = {median} s (budget 3 s)",
            "info, not the gate: shortlist_step 5: median of 3 = 0.25 s",
            "info, not the gate: shortlist_step 10: median of 3 = 0.18 s"]


SMOKE_OK = [("healthz", "ok"), ("readyz", "ok"), ("chat ping", "not run (no chat route until Phase 9)")]


def words(argv: list[str], n: int) -> str:
    return " ".join(argv[:n])


KNOWN = ("artifacts docker images describe", "run jobs executions list", "run jobs executions describe",
         "run jobs executions cancel", "builds submit", "secrets versions list", "secrets versions access",
         "run services describe", "run services get-iam-policy", "run services update-traffic", "run services update",
         "run revisions describe", "run jobs deploy", "run jobs execute", "run deploy", "logging read")
NAMED = {"run jobs deploy", "run jobs execute", "run deploy", "run services get-iam-policy", "run services update",
         "run services update-traffic"}


def verb(argv: list[str]) -> str:
    """The command's verb, with the job or service name for the verbs that take one (for order checks)."""
    text = " ".join(argv[1:])
    for k in KNOWN:
        if text == k or text.startswith(k + " "):
            return f"{k} {argv[1 + len(k.split())]}" if k in NAMED else k
    return text


class Cloud(DryRunner):
    """Scripted Cloud Run, Cloud Build, Secret Manager and Logging. Attributes change what it answers; `raise_on`
    holds (predicate, CommandError) pairs checked before the answer; `on_call` hooks see every argv first."""

    def __init__(self):
        super().__init__()
        self.image_exists = False
        self.digest: str | None = DIGEST
        self.versions = [{"name": f"projects/p/secrets/demo-pass/versions/{n}", "state": s}
                         for n, s in ((3, "ENABLED"), (2, "DISABLED"), (1, "ENABLED"))]
        self.hq_versions = [{"name": f"projects/p/secrets/demo-hq-pass/versions/{n}", "state": s}
                            for n, s in ((5, "ENABLED"), (4, "DISABLED"))]
        self.store_value = PASSCODE  # what a read of the passcode secret returns
        self.hq_value = HQ_PASSCODE  # what a read of the HQ secret returns (a test scripts a refusal with it)
        self.service_before: dict | None = None  # None: the describe says the service does not exist
        self.build_lines = list(BUILD_LINES)
        self.activate_lines: list[str] | None = None  # None: derived from build_lines (`activate_view`)
        self.briefs_lines: list[str] | None = None  # None: one case line and SUMMARY for the briefs execute's id
        self.restore_lines = list(RESTORED_LINES)
        self.active_lines = [f"ACTIVE: {GOOD_TPL}"]
        self.active_named = True  # False: the active execute answers no execution name
        self.bench_lines = list(BENCH_LINES)
        self.early_lines = early_lines()
        self.min_scale: str | None = "0"
        self.max_scale: str | None = "1"
        self.traffic = 100  # the percent a --to-latest move sends to the latest ready revision
        self.new_revision = NEW_REV  # the revision a deploy makes the latest ready one
        self.created_revision: str | None = None  # the latest created revision after a deploy (None: new_revision)
        self.moves_ignored = False  # True: a --to-revisions move is accepted but not applied
        self.routes_after_move: list[dict] | None = None  # set: the traffic after any --to-revisions move attempt
        self.revision_digest = REF
        self.policy = {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}
        self.invoker_disabled = "true"
        self.executions: list[dict] = []
        self.exec_states: list[dict] = []
        self.raise_on: list = []
        self.on_call: list = []

    def did(self, *lead: str) -> bool:
        return any(c[1:1 + len(lead)] == list(lead) for c in self.calls)

    def build_view(self) -> list[str]:
        """The inactive build's log from build_lines. A script written in the one-execution shape (`template <id>
        ACTIVE for <dv>` and `retired:`) reads as its Phase 9 split: the build prints READY and no retired: line,
        and the same activation's lines come from the activate execution (`activate_view`)."""
        out = []
        for line in self.build_lines:
            m = re.match(r"^template (\S+) ACTIVE for (\S+)$", line)
            if m:
                out.append(f"template {m.group(1)} READY for {m.group(2)}")
            elif not line.startswith("retired: "):
                out.append(line)
        return out

    def activate_view(self) -> list[str]:
        """activate_lines when set; else the one-execution script's ACTIVE and retired: lines; else the READY template
        made ACTIVE, retiring the build's `previous ACTIVE:` template."""
        if self.activate_lines is not None:
            return self.activate_lines
        shaped = [line for line in self.build_lines
                  if re.match(r"^template \S+ ACTIVE for \S+$", line) or line.startswith("retired: ")]
        if shaped:
            return shaped
        ready = [m for line in self.build_lines for m in [re.match(r"^template (\S+) READY for (\S+)$", line)] if m]
        before = [line.split(": ", 1)[1] for line in self.build_lines if line.startswith("previous ACTIVE: ")]
        return ([f"template {ready[0].group(1)} ACTIVE for {ready[0].group(2)}"] if ready else []) + \
            ([f"retired: {before[0]}"] if before else [])

    def briefs_view(self) -> list[str]:
        if self.briefs_lines is not None:
            return self.briefs_lines
        runs = [x.rsplit(",", 1)[1] for c in self.calls for x in c if x.startswith("--args=-m,agent.briefs,")]
        return [briefs_case_line(), summary_line(run_id=runs[-1] if runs else NEW)]

    def _answer(self, argv, stdin, default):
        self._echo(argv, stdin)
        self.calls.append(list(argv))
        self.inputs.append("(withheld)" if stdin is not None else None)
        for hook in self.on_call:
            hook(argv, self)
        for predicate, exc in self.raise_on:
            if predicate(argv):
                raise exc
        return self.respond(argv)

    def replay(self) -> tuple[str | None, list[dict]]:
        """(latest ready revision, traffic) after the calls so far. A deploy makes new_revision the latest ready one
        and leaves traffic where it was (Cloud Run after a recovery pinned traffic: the strict case); --to-latest
        sends `traffic`% to the latest ready one; --to-revisions <name>=<pct> sends pct to name."""
        status = (self.service_before or {}).get("status") or {}
        latest, routes = status.get("latestReadyRevisionName"), list(status.get("traffic") or [])
        for c in self.calls:
            if c[1:3] == ["run", "deploy"] and unready(c):
                continue  # its revision never became ready: the latest ready revision stays
            if c[1:3] == ["run", "deploy"]:
                latest = self.new_revision
            elif c[1:4] == ["run", "services", "update-traffic"]:
                if "--to-latest" in c:
                    routes = [{"revisionName": latest, "percent": self.traffic}]
                elif "--to-revisions" in c and self.routes_after_move is not None:
                    routes = list(self.routes_after_move)
                elif "--to-revisions" in c and not self.moves_ignored:
                    name, pct = flag(c, "--to-revisions").split("=")
                    routes = [{"revisionName": name, "percent": int(pct)}]
        return latest, routes

    def respond(self, a: list[str]):
        w = a[1:]
        if w[:2] == ["run", "deploy"] and unready(a):
            raise CommandError("gcloud run deploy", 1, "ERROR: (gcloud.run.deploy) Revision is not ready and cannot "
                                                       "serve traffic. The user-provided container failed to start.")
        if w[:4] == ["artifacts", "docker", "images", "describe"]:
            if not (self.image_exists or self.did("builds", "submit")):
                raise CommandError("gcloud artifacts docker images", 1, "ERROR: Image not found")
            return {"image_summary": {"digest": self.digest}} if self.digest else {"image_summary": {}}
        if w[:3] == ["secrets", "versions", "list"]:
            return self.hq_versions if w[3] == "demo-hq-pass" else self.versions
        if w[:3] == ["secrets", "versions", "access"]:
            return self.hq_value if flag(a, "--secret") == "demo-hq-pass" else self.store_value
        if w[:3] == ["run", "services", "describe"]:
            latest, routes = self.replay()
            if not self.did("run", "deploy"):
                if self.service_before is None and not self.did("run", "services", "update-traffic"):
                    raise CommandError("gcloud run services describe", 1, "ERROR: Cannot find service [demo-svc]")
                before = self.service_before or {}
                return {**before, "status": {**(before.get("status") or {}), "traffic": routes}}
            ann = {}
            if self.min_scale is not None:
                ann["run.googleapis.com/minScale"] = self.min_scale
            if self.max_scale is not None:
                ann["run.googleapis.com/maxScale"] = self.max_scale
            if self.did("run", "services", "update"):
                ann["run.googleapis.com/invoker-iam-disabled"] = self.invoker_disabled
            return {"metadata": {"annotations": ann},
                    "spec": {"template": {"metadata": {"annotations": {
                        "run.googleapis.com/cpu-throttling": "true", "run.googleapis.com/startup-cpu-boost": "true",
                        "run.googleapis.com/execution-environment": "gen2"}}}},
                    "status": {"url": URL, "latestReadyRevisionName": latest,
                               "latestCreatedRevisionName": self.created_revision or latest, "traffic": routes}}
        if w[:3] == ["run", "revisions", "describe"]:
            return {"status": {"imageDigest": self.revision_digest}}
        if w[:3] == ["run", "jobs", "execute"]:
            job_name = w[3].removesuffix("-drill")  # a drill configuration's jobs answer like the live ones
            if job_name == "demo-build" and any(x.startswith("--args=-m,api.sandbox,build") for x in w):
                return {"metadata": {"name": BUILD_EXEC}}
            if job_name == "demo-build" and any(x.startswith("--args=-m,agent.briefs,") for x in w):
                return {"metadata": {"name": BRIEFS_EXEC}}
            if job_name == "demo-build" and any(x.startswith("--args=-m,api.sandbox,activate,") for x in w):
                return {"metadata": {"name": ACTIVATE_EXEC}}
            if job_name == "demo-bench":
                return {"metadata": {"name": BENCH_EXEC}}
            if job_name == "demo-early":
                return {"metadata": {"name": EARLY_EXEC}}
            if job_name == "demo-build" and any(x.startswith("--args=-m,api.sandbox,restore,") for x in w):
                return {"metadata": {"name": RESTORE_EXEC}}
            if job_name == "demo-build" and "--args=-m,api.sandbox,active" in w:
                return {"metadata": {"name": ACTIVE_EXEC}} if self.active_named else ""
            return ""
        if w[:2] == ["logging", "read"]:
            if BUILD_EXEC in w[2]:
                return [{"textPayload": line} for line in self.build_view()]
            if BRIEFS_EXEC in w[2]:
                return [{"textPayload": line} for line in self.briefs_view()]
            if ACTIVATE_EXEC in w[2]:
                return [{"textPayload": line} for line in self.activate_view()]
            if BENCH_EXEC in w[2]:
                return [{"textPayload": line} for line in self.bench_lines]
            if EARLY_EXEC in w[2]:
                return [{"textPayload": line} for line in self.early_lines]
            if RESTORE_EXEC in w[2]:
                return [{"textPayload": line} for line in self.restore_lines]
            if ACTIVE_EXEC in w[2]:
                return [{"textPayload": line} for line in self.active_lines]
            return []
        if w[:3] == ["run", "services", "get-iam-policy"]:
            return self.policy
        if w[:4] == ["run", "jobs", "executions", "list"]:
            return self.executions
        if w[:4] == ["run", "jobs", "executions", "describe"]:
            return self.exec_states.pop(0) if self.exec_states else {"status": {"completionTime": "t"}}
        return ""


def unready(argv: list[str]) -> bool:
    """A service deploy whose GLOBAL_AI_CAP is not all digits: the app refuses it at import, so the revision never
    becomes ready and gcloud run deploy exits 1 (the drill's --drill-unready-deploy)."""
    return not env_of(argv).get("GLOBAL_AI_CAP", "0").isdigit()


class FakeGit:
    def __init__(self, dirty: str = "", sha: str = SHA):
        self.dirty, self.sha = dirty, sha
        self.archived: list[str] = []

    def status_porcelain(self) -> str:
        return self.dirty

    def head(self) -> str:
        return self.sha

    def archive(self, sha: str, dest) -> None:
        self.archived.append(sha)
        ref = pathlib.Path(dest) / "data" / "reference"
        ref.mkdir(parents=True)
        for name, body in REF_FILES.items():
            (ref / name).write_bytes(body)
        (pathlib.Path(dest) / "api").mkdir()
        (pathlib.Path(dest) / "api" / "bench_input.json").write_bytes(BENCH_INPUT)


class Fakes:
    """The smoke and journey stand-ins; they record what they were handed."""

    def __init__(self):
        self.smoke_calls: list[tuple[str, str, str]] = []
        self.journey_calls: list[tuple[str, str, str]] = []
        self.smoke_result = list(SMOKE_OK)
        self.journey_result = {"passed": True, "exit": 0}
        self.sleeps: list[float] = []

    def smoke(self, url, passcode, hq):
        self.smoke_calls.append((url, passcode, hq))
        return list(self.smoke_result)

    def journey(self, url, passcode, hq):
        self.journey_calls.append((url, passcode, hq))
        return dict(self.journey_result)

    def sleep(self, s):
        self.sleeps.append(s)


@pytest.fixture
def cfg(tmp_path) -> dict[str, str]:
    return envfile.load(synthetic_env.write(tmp_path / "env"))


@pytest.fixture
def drill_cfg(tmp_path) -> dict[str, str]:
    return envfile.load(synthetic_env.write(tmp_path / "drill-env", **synthetic_env.DRILL))


@pytest.fixture
def out_dir(tmp_path) -> pathlib.Path:
    return tmp_path / "releases"


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


def run_release(cfg, cloud, out_dir, fakes, git=None, **kw) -> int:
    return release.release(cfg, runner=cloud, git=git or FakeGit(), now=lambda: NOW, checks=lambda: "dv-1",
                           smoke=fakes.smoke, journey=fakes.journey, out_dir=out_dir, sleep=fakes.sleep, **kw)


def records(out_dir) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(pathlib.Path(out_dir).glob("*.json"))]


def flag(argv: list[str], name: str) -> str:
    for a in argv:
        if a.startswith(f"{name}="):
            return a.split("=", 1)[1]
    return argv[argv.index(name) + 1]


def env_of(argv: list[str]) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in flag(argv, "--set-env-vars").split(","))


def call(cloud: Cloud, *lead: str, name: str | None = None) -> list[str]:
    hits = [c for c in cloud.calls if c[1:1 + len(lead)] == list(lead) and (name is None or name in c)]
    assert hits, f"no {' '.join(lead)} call"
    return hits[0]


# --- manifest.py ------------------------------------------------------------------------------------------------------

def test_release_id_is_the_utc_time_and_the_short_sha():
    assert manifest.release_id(NOW, SHA) == RID
    assert manifest.release_id(NOW, SHA, "rollback") == RID + "-rollback"
    later = dt.datetime(2026, 10, 16, 21, 5, 6, tzinfo=dt.timezone(dt.timedelta(hours=7)))
    assert manifest.release_id(later, SHA) == RID  # any zone is written as UTC


def test_write_refuses_to_overwrite_and_save_is_atomic(tmp_path):
    out = tmp_path / "r"
    path = manifest.write({"kind": "release", "id": RID, "status": "known_good"}, out)
    assert path == out / f"{RID}.json"
    assert path.read_text().endswith("}\n")
    assert json.loads(path.read_text())["status"] == "known_good"
    with pytest.raises(FileExistsError):
        manifest.write({"kind": "release", "id": RID}, out)
    manifest.save(path, {"kind": "release", "id": RID, "status": "failed_bench"})
    assert json.loads(path.read_text())["status"] == "failed_bench"
    assert sorted(p.name for p in out.iterdir()) == [f"{RID}.json"]  # no temporary file left


def test_open_record_writes_status_in_progress_and_refuses_an_existing_file(tmp_path):
    rec = {"kind": "release", "id": RID}
    path = manifest.open_record(rec, tmp_path)
    assert json.loads(path.read_text())["status"] == "in_progress" and rec["status"] == "in_progress"
    with pytest.raises(FileExistsError):
        manifest.open_record({"kind": "release", "id": RID}, tmp_path)


def test_save_withholds_a_secret_value(tmp_path):
    path = manifest.write({"kind": "release", "id": RID}, tmp_path)
    manifest.save(path, {"kind": "release", "id": RID, "note": f"x {PASSCODE} y"}, withhold=[PASSCODE])
    assert PASSCODE not in path.read_text()


def test_latest_goes_by_filename_and_filters_kind_and_status(tmp_path):
    assert manifest.latest(tmp_path / "missing") is None
    for rid, kind, status in (("20261001T000000Z-aaaaaaaaaaaa", "release", "known_good"),
                              ("20261002T000000Z-bbbbbbbbbbbb", "release", "failed_bench"),
                              ("20261003T000000Z-cccccccccccc-early-bench", "early_bench", "pass"),
                              ("20261004T000000Z-dddddddddddd", "release", "in_progress")):
        manifest.write({"kind": kind, "id": rid, "status": status}, tmp_path)
    assert manifest.latest(tmp_path)["id"].startswith("20261004")
    assert manifest.latest(tmp_path, status="known_good")["id"].startswith("20261001")
    assert manifest.latest(tmp_path, kind="early_bench")["status"] == "pass"
    assert manifest.latest(tmp_path, status="rolled_back") is None


# --- local checks and compatibility --------------------------------------------------------------------------------

def test_check_commands_are_the_reference_check_the_deploy_gate_and_the_browser_probe(tmp_path):
    env_file = synthetic_env.write(tmp_path / "env")
    cmds = release.check_commands(env_file, browser=True)
    assert [name for name, _, _ in cmds] == ["data.reference --release", "pytest tests/deploy", "browser probe"]
    _, ref_argv, ref_env = cmds[0]
    assert ref_argv[1:] == ["-m", "data.reference", "--release"] and ref_env == {}
    _, gate_argv, gate_env = cmds[1]
    assert gate_argv[1:4] == ["-m", "pytest", "-q"] and gate_argv[-1] == "tests/deploy"
    assert gate_env == {"SS_DEPLOY_ENV_FILE": str(env_file.resolve())}  # never PROJECT or any other value
    assert [name for name, _, _ in release.check_commands(env_file, browser=False)] == [
        "data.reference --release", "pytest tests/deploy"]


def test_local_checks_return_the_dataset_version_and_name_a_failing_check(tmp_path):
    env_file = synthetic_env.write(tmp_path / "env")
    seen = []

    def ok(argv, **kw):
        seen.append((argv, kw["env"]))
        out = "ok dv-7 (headline)\n" if "data.reference" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    assert release.local_checks(env_file, browser=True, run=ok)() == "dv-7"
    assert len(seen) == 3
    assert seen[1][1] == {**os.environ, "SS_DEPLOY_ENV_FILE": str(env_file.resolve())}  # nothing of the config

    for failing, name in (("data.reference", "data.reference --release"), ("pytest", "pytest tests/deploy"),
                          ("-c", "browser probe")):
        def bad(argv, failing=failing, **kw):
            code = 1 if failing in argv else 0
            out = "ok dv-7 (h)\n" if "data.reference" in argv else ""
            return subprocess.CompletedProcess(argv, code, out, "boom")
        with pytest.raises(release.ReleaseError, match=re.escape(name)):
            release.local_checks(env_file, browser=True, run=bad)()


def test_compat_is_first_compatible_or_breaking(tmp_path):
    assert release.compat(tmp_path) == (ENGINE_VERSION, "first")
    manifest.write({"kind": "release", "id": "20261001T000000Z-aaaaaaaaaaaa", "status": "known_good",
                    "engine_version": ENGINE_VERSION}, tmp_path)
    assert release.compat(tmp_path) == (ENGINE_VERSION, "compatible")
    manifest.write({"kind": "release", "id": "20261002T000000Z-bbbbbbbbbbbb", "status": "known_good",
                    "engine_version": "engine-0"}, tmp_path)
    assert release.compat(tmp_path) == (ENGINE_VERSION, "breaking")
    manifest.write({"kind": "release", "id": "20261003T000000Z-cccccccccccc", "status": "known_good"}, tmp_path)
    assert release.compat(tmp_path) == (ENGINE_VERSION, "breaking")  # a manifest without the field


def test_the_build_log_parse_reads_the_inactive_build_lines_or_says_not_read_back():
    got = release.parse_build(BUILD_LINES)
    assert got == {"active_before_build": "tpl-old", "ready_template": NEW}
    first = release.parse_build(["previous ACTIVE: none", f"template {NEW} READY for dv-1"])
    assert first == {"active_before_build": None, "ready_template": NEW}
    assert set(release.parse_build([]).values()) == {manifest.NOT_READ_BACK}
    # an activating build's ACTIVE line is not a READY line
    assert release.parse_build([f"template {NEW} ACTIVE for dv-1"])["ready_template"] == manifest.NOT_READ_BACK


def test_the_activate_log_parse_reads_the_active_and_retired_lines():
    assert release.parse_activate(ACTIVATE_LINES) == {"activated_template": NEW, "previous_template": "tpl-old"}
    assert release.parse_activate([f"template {NEW} ACTIVE for dv-1", "retired: none"]) == {
        "activated_template": NEW, "previous_template": None}
    assert set(release.parse_activate([]).values()) == {manifest.NOT_READ_BACK}


def test_the_briefs_summary_is_the_last_summary_line_for_the_template():
    lines = [briefs_case_line(), summary_line(success=1), "not json", summary_line(run_id="tpl-other"),
             summary_line()]
    assert release.briefs_summary(lines, NEW) == SUMMARY
    assert release.briefs_summary([briefs_case_line(), summary_line(run_id="tpl-other")], NEW) is None
    # a JSON log entry's numbers come back as floats (jsonPayload); the counts are ints again
    floats = json.dumps({**SUMMARY, "success": 5.0, "eligible": 5.0})
    assert release.briefs_summary([floats], NEW)["success"] == 5
    assert isinstance(release.briefs_summary([floats], NEW)["eligible"], int)


# --- the happy path ---------------------------------------------------------------------------------------------------

def test_the_happy_path_issues_the_commands_in_order_and_records_a_known_good_manifest(cfg, out_dir, fakes):
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    order = [verb(c) for c in cloud.calls]
    # the secret step is first (06.1 D-64): both pins, then both reads for the HQ preflight, before the image
    assert order[:22] == [
        "secrets versions list", "secrets versions list", "secrets versions access", "secrets versions access",
        "artifacts docker images describe", "builds submit", "artifacts docker images describe",
        "run services describe", "run jobs deploy demo-build", "run jobs execute demo-build",
        "logging read", "run jobs execute demo-build", "logging read", "run jobs execute demo-build", "logging read",
        "run jobs deploy demo-bench", "run jobs execute demo-bench", "logging read",
        "run deploy demo-svc", "run services update-traffic demo-svc", "run services describe",
        "run revisions describe"]
    assert [c[4] for c in cloud.calls[:2]] == ["demo-pass", "demo-hq-pass"]
    assert [flag(c, "--secret") for c in cloud.calls[2:4]] == ["demo-pass", "demo-hq-pass"]
    assert order[22] == "run services get-iam-policy demo-svc"
    assert order[23:] == ["secrets versions access", "secrets versions access"]
    # Phase 9: inactive build, then the briefs job, then activate, then the bench (args unchanged), then the service
    executes = [c for c in cloud.calls if verb(c).startswith("run jobs execute")]
    assert [flag(c, "--args") if any(x.startswith("--args=") for x in c) else None for c in executes] == [
        f"-m,api.sandbox,build,--no-activate,--run-id,{NEW}", f"-m,agent.briefs,{NEW}",
        f"-m,api.sandbox,activate,{NEW}", None]
    [briefs_read] = [c for c in cloud.calls if c[1:3] == ["logging", "read"] and BRIEFS_EXEC in c[3]]
    assert briefs_read[3].endswith(' AND (textPayload:"summary" OR jsonPayload.type="summary")')
    for other in (c for c in cloud.calls if c[1:3] == ["logging", "read"] and BRIEFS_EXEC not in c[3]):
        assert "summary" not in other[3]  # every other log read is unchanged
    [route] = to_latest(cloud)
    assert "--to-revisions" not in route
    deployed, routed = cloud.calls.index(call(cloud, "run", "deploy")), cloud.calls.index(route)
    described = [i for i in indexes(cloud, is_call("run", "services", "describe")) if i > deployed]
    assert deployed < routed < described[0]
    assert not cloud.did("run", "services", "update")  # the access fallback belongs to a refused binding only
    submit = call(cloud, "builds", "submit")
    assert flag(submit, "--tag") == f"{IMAGE}:{SHA}" and flag(submit, "--region") == REGION
    assert pathlib.Path(submit[3]).name.startswith("shelfshift-src-")  # the archive directory, not the tree
    build_exec = call(cloud, "run", "jobs", "execute", name="demo-build")
    assert f"--args=-m,api.sandbox,build,--no-activate,--run-id,{NEW}" in build_exec and "--wait" in build_exec
    deploy = call(cloud, "run", "deploy")
    assert "--allow-unauthenticated" in deploy

    [rec] = records(out_dir)
    assert pathlib.Path(out_dir, f"{RID}.json").exists()
    assert not [p for p in pathlib.Path(out_dir).iterdir() if not p.name.endswith(".json")]  # no .tmp left
    assert rec["kind"] == "release" and rec["id"] == RID and rec["status"] == "known_good"
    assert rec["source_commit"] == SHA and rec["image_digest"] == DIGEST and rec["image"] == REF
    assert rec["project"] == PROJECT and rec["region"] == REGION and rec["time"] == NOW.isoformat()
    assert rec["new_template"] == NEW and rec["activated_template"] == NEW and rec["ready_template"] == NEW
    assert rec["previous_template"] == "tpl-old" and rec["active_before_build"] == "tpl-old"
    assert rec["activation"] == "separate" and rec["activate_execution"] == ACTIVATE_EXEC
    assert rec["briefs"] == {"execution": BRIEFS_EXEC, "summary": SUMMARY, "result": "pass"}
    assert rec["min_instances"] == "0" and rec["max_instances"] == "1"
    assert rec["revision"] == NEW_REV and rec["url"] == URL and rec["previous_revision"] is None
    assert rec["access"] == "public" and rec["secret_version"] == "3" and rec["hq_secret_version"] == "5"
    assert rec["resources"]["hq_passcode_secret"] == "demo-hq-pass"
    assert rec["dataset_version"] == "dv-1"
    assert rec["reference_files"] == {f"data/reference/{n}": hashlib.sha256(b).hexdigest()
                                      for n, b in REF_FILES.items()}
    assert rec["smoke"] == [list(s) for s in SMOKE_OK] and rec["journey"] == {"passed": True, "exit": 0}
    assert rec["model"]["configured"] == cfg["GEMINI_MODEL"] and rec["model"]["endpoint"] == cfg["VERTEX_LOCATION"]
    assert "briefs job" in rec["model"]["used"] and "service" in rec["model"]["used"]
    assert "not until Phase 9" not in json.dumps(rec)
    assert rec["engine_version"] == ENGINE_VERSION and rec["template_compat"] == "first"
    assert rec["previous_known_good"] is None
    assert rec["bench"]["result"] == "pass" and rec["bench"]["gate"] == BENCH_LINES[-1]
    assert rec["bench"]["execution"] == BENCH_EXEC and rec["build_execution"] == BUILD_EXEC
    assert rec["resources"]["service"] == "demo-svc" and rec["resources"]["build_job"] == "demo-build"
    steps = [p["step"] for p in rec["progress"]]
    assert steps == ["secret", "image", "previous_revision", "build_deploy", "build_execute", "build_log", "briefs",
                     "activate", "bench", "service_deploy", "read_back", "access", "smoke", "journey"]
    assert {p["state"] for p in rec["progress"]} == {"ok"}
    assert rec["progress"][0] == {"step": "secret", "state": "ok", "version": "3", "hq_version": "5",
                                  "preflight": "ok"}


def test_the_build_job_and_the_service_get_exactly_the_model_keys_and_the_bench_none(cfg, out_dir, fakes):
    """Phase 9 (09-24 checklist item 2; 06-CONTEXT D-04 updated): GCP=on, GEMINI_MODEL, VERTEX_LOCATION and PROJECT
    reach the build job (its executions include the briefs job) and the service; never a GOOGLE_GENAI_USE_* flag; the
    bench job's variables are unchanged; no passcode value is in any argv."""
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    model = {"GCP": "on", "GEMINI_MODEL": cfg["GEMINI_MODEL"], "VERTEX_LOCATION": cfg["VERTEX_LOCATION"],
             "PROJECT": PROJECT}
    build = env_of(call(cloud, "run", "jobs", "deploy", name="demo-build"))
    service = env_of(call(cloud, "run", "deploy"))
    for env in (build, service):
        assert {k: env[k] for k in model} == model
        assert not [k for k in env if k.startswith("GOOGLE_GENAI")]
    assert list(build)[-4:] == list(model) and list(service)[-4:] == list(model)
    assert service["GLOBAL_AI_CAP"] == cfg["GLOBAL_AI_CAP"]
    bench = env_of(call(cloud, "run", "jobs", "deploy", name="demo-bench"))
    assert bench == {"CLOUD_SQL_INSTANCE": f"{PROJECT}:{REGION}:demo-sql", "DB_USER": "run-sa@x-demo-12.iam",
                     "DB_NAME": "demo_db", "ENGINE_WORKER": "on", "GIT_COMMIT": SHA}
    for argv in cloud.calls:
        assert PASSCODE not in " ".join(argv) and HQ_PASSCODE not in " ".join(argv)


def test_provider_failures_are_recorded_and_do_not_fail_the_release(cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    cloud.briefs_lines = [briefs_case_line("C-1"), briefs_case_line("C-2"), summary_line(success=3, failure=2)]
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    rec = only_record(out_dir)
    assert rec["status"] == "known_good"
    assert rec["briefs"]["summary"]["failure"] == 2 and rec["briefs"]["summary"]["success"] == 3
    assert rec["briefs"]["summary"]["usage"] == SUMMARY["usage"]
    assert rec["briefs"]["result"] == "pass with 2 provider failures (Fallback)"
    out = capsys.readouterr().out
    [line] = [x for x in out.splitlines() if x.startswith("briefs:")]
    assert line == ("briefs: 3 success, 2 failure, 0 reused of 5 eligible; model gemini-3.5-flash at "
                    "asia-southeast1, assistant-3")


def test_a_json_payload_summary_is_read_as_cloud_run_stores_it(cfg, out_dir, fakes):
    """Cloud Run turns a JSON line on stdout into jsonPayload (its numbers become floats): the summary still reads."""
    cloud = Cloud()
    original = cloud.respond

    def respond(a):
        if a[1:3] == ["logging", "read"] and BRIEFS_EXEC in a[3]:
            return [{"jsonPayload": {**SUMMARY, **{k: float(SUMMARY[k]) for k in release.SUMMARY_COUNTS}}}]
        return original(a)

    cloud.respond = respond
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    assert only_record(out_dir)["briefs"]["summary"] == SUMMARY


def _briefs_refused(cloud):
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=f"--args=-m,agent.briefs,{NEW}"),
                           boom("gcloud run jobs execute", 1, "baseline job refused: not READY")))


def _briefs_lines(*lines):
    def apply(cloud):
        cloud.briefs_lines = list(lines)
    return apply


BRIEFS_STOPS = [
    ("refused", _briefs_refused, "(exit 1)"),
    ("no summary line", _briefs_lines(briefs_case_line()), "summary\"' line"),
    ("another template's summary", _briefs_lines(summary_line(run_id="tpl-other")), "summary\"' line"),
    ("zero briefs", _briefs_lines(summary_line(success=0, failure=5)), "a model, location or IAM problem"),
    ("another model", _briefs_lines(summary_line(model_id="gemini-2.5-flash")), "not GEMINI_MODEL"),
    ("another location", _briefs_lines(summary_line(location="global")), "not VERTEX_LOCATION"),
]


@pytest.mark.parametrize("case, setup, message", BRIEFS_STOPS, ids=[c[0] for c in BRIEFS_STOPS])
def test_a_briefs_stop_fails_before_activation(case, setup, message, cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    setup(cloud)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_briefs" and rec["progress"][-1]["step"] == "briefs"
    assert rec["progress"][-1]["state"] == "failed" and message in rec["error"]
    assert not indexes(cloud, is_call("run", "jobs", "execute", having=f"--args=-m,api.sandbox,activate,{NEW}"))
    assert not indexes(cloud, lambda c: "demo-bench" in c) and not cloud.did("run", "deploy")
    assert "briefs:" not in capsys.readouterr().out
    # nothing activated <new>: the build is settled, nothing is restored, the READY template stays inactive
    assert not restores(cloud) and rec["recovery"]["restore"] == NOT_ACTIVATED and rec["reconcile"] is None
    assert rec["recovery"]["settle"] == {"execution": BUILD_EXEC, "state": "finished"}


def test_zero_eligible_cases_pass_the_briefs_step(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.briefs_lines = [summary_line(cases=2, eligible=0, ineligible=2, attempted=0, success=0, failure=0)]
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    assert only_record(out_dir)["briefs"]["result"] == "pass"


def test_a_failed_activate_execute_is_settled_then_restored(cfg, out_dir, fakes):
    """The activate execute exits non-zero, so no execution name was recorded: the activate execution is found among
    the build job's executions by `activate,<new>`, settled, then `restore <new>` is verified against tpl-old."""
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=f"--args=-m,api.sandbox,activate,{NEW}"),
                           boom("gcloud run jobs execute")))
    cloud.executions = [activation_listed(state=RUNNING)]
    cloud.exec_states = [FINISHED]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_activate"
    assert rec["recovery"]["settle_activate"] == {"execution": ACTIVATE_EXEC, "state": "finished"}
    assert rec["activate_execution"] == ACTIVATE_EXEC  # found and recorded by the settle
    [restore] = restores(cloud)
    assert max(settles(cloud)) < restore and RESTORE_ARGS in cloud.calls[restore]
    assert FIRST_RESTORE_ARGS not in cloud.calls[restore]
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE" and rec["reconcile"] is None
    assert not indexes(cloud, lambda c: "demo-bench" in c) and not cloud.did("run", "deploy")


def test_an_unreadable_activate_execution_runs_no_restore_and_stays_pending(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=f"--args=-m,api.sandbox,activate,{NEW}"),
                           boom("gcloud run jobs execute")))
    cloud.executions = [activation_listed(), activation_listed(name="demo-build-twin")]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["recovery"]["settle_activate"].startswith("unknown: 2 executions of the build job hold activate")
    assert rec["recovery"]["restore"].startswith("not run: ") and not restores(cloud)
    assert rec["reconcile"] == "pending"


def interrupted(out_dir, steps: list[str], **fields) -> dict:
    """An in_progress Phase 9 record killed while its last step was requested."""
    rec = {"kind": "release", "id": "20261016T120000Z-0123456789ab", "status": "in_progress", "source_commit": SHA,
           "new_template": NEW, "activation": "separate", "build_execution": BUILD_EXEC,
           "active_before_build": "tpl-old", "previous_revision": None, "previous_known_good": None,
           "progress": [{"step": x, "state": "ok"} for x in steps[:-1]] + [{"step": steps[-1], "state": "requested"}],
           "reconcile": None, "recovery": None, **fields}
    manifest.write(rec, out_dir)
    return rec


PRE_BRIEFS = ["secret", "image", "previous_revision", "build_deploy", "build_execute", "build_log"]


def test_a_release_killed_in_briefs_is_interrupted_without_a_restore(cfg, out_dir, fakes):
    interrupted(out_dir, [*PRE_BRIEFS, "briefs"])
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "interrupted" and rec["reconcile"] is None
    assert rec["recovery"]["restore"] == NOT_ACTIVATED and not restores(cloud)
    assert not indexes(cloud, is_call("run", "jobs", "execute", having=BUILD_ARGS))


def test_a_release_killed_in_activate_finds_settles_and_restores_the_activation(cfg, out_dir, fakes):
    interrupted(out_dir, [*PRE_BRIEFS, "briefs", "activate"])
    cloud = Cloud()
    cloud.executions = [listed(), activation_listed()]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "interrupted" and rec["reconcile"] is None
    assert rec["recovery"]["settle_activate"] == {"execution": ACTIVATE_EXEC, "state": "finished"}
    [restore] = restores(cloud)
    assert max(settles(cloud)) < restore
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert not indexes(cloud, is_call("run", "jobs", "execute", having=BUILD_ARGS)) and not cloud.did("run", "deploy")


def test_an_old_record_without_activation_recovers_as_before(cfg, out_dir, fakes):
    """A record from before Phase 9 (no `activation` field: its build activated) killed after build_log still
    settles and restores <new> against active_before_build, exactly as before."""
    rec = interrupted(out_dir, [*PRE_BRIEFS[:-1], "build_log"])
    del rec["activation"]
    pathlib.Path(out_dir, f"{rec['id']}.json").write_text(json.dumps(rec))
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    got = only_record(out_dir)
    assert "activation" not in got and got["status"] == "interrupted"
    [restore] = restores(cloud)
    assert got["recovery"]["restore"] == "restored: tpl-old ACTIVE" and "settle_activate" not in got["recovery"]


@pytest.mark.parametrize("lines, message", [
    ([f"template tpl-other ACTIVE for dv-1", "retired: tpl-old"], "names tpl-other ACTIVE"),
    ([f"template {NEW} ACTIVE for dv-1", "retired: tpl-else"], "retired tpl-else, not tpl-old"),
    ([f"template {NEW} ACTIVE for dv-1"], "retired: line was not read"),
], ids=["another template", "another predecessor", "no retired line"])
def test_the_activate_log_must_name_the_new_template_and_its_predecessor(lines, message, cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.activate_lines = lines
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_activate" and message in rec["error"]
    assert rec["activate_execution"] == ACTIVATE_EXEC
    assert not indexes(cloud, lambda c: "demo-bench" in c) and not cloud.did("run", "deploy")


def test_every_image_is_by_digest_and_each_job_runs_as_its_account(cfg, out_dir, fakes):
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    images = [flag(c, "--image") for c in cloud.calls if "--image" in c]
    assert len(images) == 3 and set(images) == {REF}
    build = call(cloud, "run", "jobs", "deploy", name="demo-build")
    assert flag(build, "--service-account") == DEPLOY_SA
    assert env_of(build)["DB_USER"] == "deploy-sa@x-demo-12.iam"
    assert env_of(build)["RUN_DB_USER"] == "run-sa@x-demo-12.iam"
    bench = call(cloud, "run", "jobs", "deploy", name="demo-bench")
    assert flag(bench, "--service-account") == RUN_SA and env_of(bench)["ENGINE_WORKER"] == "on"
    assert flag(bench, "--args") == "-m,api.bench"
    for job in (build, bench):
        assert (flag(job, "--cpu"), flag(job, "--memory"), flag(job, "--max-retries")) == ("1", "1Gi", "0")
        assert flag(job, "--command") == "python"
    # the full public dataset's template build needs more than 15 minutes on one CPU; the bench keeps 15
    assert (flag(build, "--task-timeout"), flag(bench, "--task-timeout")) == ("180m", "15m")
    svc = call(cloud, "run", "deploy")
    assert flag(svc, "--service-account") == RUN_SA and env_of(svc)["ENGINE_WORKER"] == "on"
    assert env_of(svc)["CLOUD_SQL_INSTANCE"] == f"{PROJECT}:{REGION}:demo-sql"
    assert flag(svc, "--set-secrets") == "DEMO_PASSCODE=demo-pass:3,HQ_PASSCODE=demo-hq-pass:5"
    assert env_of(svc)["DEMO_MODE"] == "off"  # 06.1 D-62: the product app runs with demo mode off
    assert flag(svc, "--min") == "0" and flag(svc, "--max") == "1"


def test_the_record_shows_each_step_before_its_cloud_call(cfg, out_dir, fakes):
    """R2-8: in_progress before the first call; the build execute and the deploy requested before they are issued."""
    seen = {}

    def look(argv, cloud):
        files = sorted(pathlib.Path(out_dir).glob("*.json"))
        rec = json.loads(files[0].read_text()) if files else None
        if not cloud.calls[:-1]:
            seen["first"] = rec
        if argv[1:4] == ["run", "jobs", "execute"] and "demo-build" in argv and "build_exec" not in seen:
            seen["build_exec"] = rec
        if argv[1:3] == ["run", "deploy"]:
            seen["deploy"] = rec

    cloud = Cloud()
    cloud.on_call.append(look)
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    assert seen["first"] is not None and seen["first"]["status"] == "in_progress"
    assert pathlib.Path(out_dir, f"{RID}.json").exists()
    last = seen["build_exec"]["progress"][-1]
    assert (last["step"], last["state"]) == ("build_execute", "requested")
    last = seen["deploy"]["progress"][-1]
    assert (last["step"], last["state"]) == ("service_deploy", "requested")


def test_the_passcode_reaches_the_smoke_and_the_journey_and_nothing_else(cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    assert fakes.smoke_calls == [(URL, PASSCODE, HQ_PASSCODE)]
    assert fakes.journey_calls == [(URL, PASSCODE, HQ_PASSCODE)]
    out = capsys.readouterr()
    for value in (PASSCODE, HQ_PASSCODE):
        assert value not in json.dumps(cloud.calls)
        assert value not in out.out and value not in out.err
        assert all(value not in p.read_text() for p in pathlib.Path(out_dir).iterdir())
    reads = [c for c in cloud.calls if c[1:4] == ["secrets", "versions", "access"]]
    # the preflight's two reads and the smoke's two, each of the pinned version, never latest
    assert [(c[4], flag(c, "--secret")) for c in reads] == [("3", "demo-pass"), ("5", "demo-hq-pass")] * 2
    assert not [c for c in cloud.calls if "latest" in c]


def test_the_journey_child_gets_the_url_and_the_passcode_in_its_environment_only():
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"], seen["cwd"] = argv, kw["env"], kw["cwd"]
        return subprocess.CompletedProcess(argv, 0)

    assert release.run_journey(URL, PASSCODE, HQ_PASSCODE, run=fake_run) == {"passed": True, "exit": 0}
    assert seen["env"]["SS_DEPLOYED_URL"] == URL and seen["env"]["SMOKE_PASSCODE"] == PASSCODE
    assert seen["env"]["SMOKE_HQ_PASSCODE"] == HQ_PASSCODE
    assert PASSCODE not in seen["argv"] and HQ_PASSCODE not in seen["argv"]
    assert seen["argv"][1:4] == ["-m", "pytest", "-q"]
    assert seen["argv"][-3:] == ["-m", "deployed", "tests/deployed"] and pathlib.Path(seen["cwd"]) == ROOT
    assert release.run_journey(URL, PASSCODE, HQ_PASSCODE,
                               run=lambda a, **k: subprocess.CompletedProcess(a, 1)) == {"passed": False, "exit": 1}


def test_a_failing_journey_child_never_prints_a_passcode(capsys):
    # Playwright's call log writes fill("<value>") into a timeout error, so the child's output is captured and shown
    # with both credentials replaced by the withheld label, as every other release output
    def fake_run(argv, **kw):
        assert kw["stdout"] == subprocess.PIPE and kw["stderr"] == subprocess.STDOUT
        return subprocess.CompletedProcess(argv, 1, stdout=f'Call log:\n  - fill("{PASSCODE}")\n  - fill("{HQ_PASSCODE}")\n'
                                                            "1 failed\n")

    assert release.run_journey(URL, PASSCODE, HQ_PASSCODE, run=fake_run) == {"passed": False, "exit": 1}
    shown = capsys.readouterr()
    assert PASSCODE not in shown.out + shown.err and HQ_PASSCODE not in shown.out + shown.err
    assert 'fill("(withheld)")' in shown.out and "1 failed" in shown.out


def test_an_existing_image_tag_is_reused_without_a_build(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.image_exists = True
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    assert not cloud.did("builds", "submit")
    assert [verb(c) for c in cloud.calls][3:6] == ["secrets versions access", "artifacts docker images describe",
                                                   "run services describe"]


def test_a_digest_that_cannot_be_read_back_stops_the_image_step(cfg, tmp_path):
    cloud = Cloud()
    cloud.digest = None
    with pytest.raises(release.ReleaseError, match="digest"):
        release.image(cfg, cloud, SHA, tmp_path)


SDK_NOT_FOUND = ["ERROR: (gcloud.artifacts.docker.images.describe) Image not found.", "",
                 "A valid container image can be referenced by tag or digest, has the format of",
                 "  LOCATION-docker.DOMAIN/PROJECT-ID/REPOSITORY-ID/IMAGE:tag",
                 "  LOCATION-docker.DOMAIN/PROJECT-ID/REPOSITORY-ID/IMAGE@sha256:digest"]
UPDATE_NOTICE = ["", "Updates are available for some Google Cloud CLI components.  To install them,", "please run:",
                 "  $ gcloud components update"]


def _describe_error(code: int, lines: list[str]) -> CommandError:
    """What Runner.run raises: the last 5 stderr lines as the tail, the whole stderr kept for matching only."""
    return CommandError("gcloud artifacts docker images", code, "\n".join(lines[-5:]), stderr="\n".join(lines))


IMAGE_CASES = [
    ("a: the SDK's not-found text", _describe_error(1, SDK_NOT_FOUND), True),
    ("b: not found above an update notice", _describe_error(1, SDK_NOT_FOUND + UPDATE_NOTICE), True),
    ("c: signed out", _describe_error(1, [
        "ERROR: (gcloud.artifacts.docker.images.describe) There was a problem refreshing your current auth tokens: "
        "Reauthentication failed. cannot prompt during non-interactive execution."]), False),
    ("d: permission denied", _describe_error(1, [
        "ERROR: (gcloud.artifacts.docker.images.describe) PERMISSION_DENIED: Permission "
        "'artifactregistry.repositories.downloadArtifacts' denied on resource (or it may not exist)."]), False),
    ("e: gcloud missing", _describe_error(127, ["gcloud: command not found"]), False),
]


@pytest.mark.parametrize("case, exc, builds", IMAGE_CASES, ids=[c[0].split(":")[0] for c in IMAGE_CASES])
def test_only_a_not_found_answer_builds_the_image(case, exc, builds, cfg, out_dir, fakes, capsys):
    """A3: only gcloud's not-found answer (docker_util's `Image not found.`, NOT_FOUND or `could not be found`, read
    in the whole masked stderr) builds the image. Any other describe failure fails closed at the image step before
    any build, and its recovery is `not needed`."""
    if case.startswith("b"):
        assert "image not found" not in exc.stderr_tail.lower() and len(exc.stderr_tail.splitlines()) == 5
    cloud = Cloud()
    cloud.image_exists = True
    cloud.raise_on.append((lambda a: is_call("artifacts", "docker", "images", "describe")(a)
                           and not cloud.did("builds", "submit"), exc))
    rc = run_release(cfg, cloud, out_dir, fakes)
    rec = only_record(out_dir)
    if builds:
        assert rc == 0 and rec["status"] == "known_good"
        assert len(indexes(cloud, is_call("builds", "submit"))) == 1
    else:
        assert rc == 1 and rec["status"] == "failed_image"
        # only the secret step's four calls before the describe, and nothing after it
        assert not cloud.did("builds", "submit") and len(cloud.calls) == 5
        assert verb(cloud.calls[-1]) == "artifacts docker images describe"
        assert rec["error"].startswith("the image tag 0123456789ab could not be read, so nothing was built: ")
        assert rec["recovery"]["restore"].startswith("not needed")
        assert "release failed at image (failed_image): the image tag 0123456789ab could not be read" in \
            capsys.readouterr().err


# --- the CLI ----------------------------------------------------------------------------------------------------------

def cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": NO_GCLOUD_PATH, **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "deploy.release", *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=120)


def test_the_cli_dry_run_needs_no_gcloud_prints_the_order_and_writes_nothing(tmp_path):
    env_file = synthetic_env.write(tmp_path / "env")
    before = sorted(ROOT.glob("deploy/releases/*"))
    p = cli("--dry-run", "--env-file", str(env_file))
    assert p.returncode == 0, p.stderr
    lines = [line for line in p.stdout.splitlines() if line.startswith("+ gcloud ")]
    order = [verb(shlex.split(line[2:])) for line in lines]
    executes = [line for line in lines if line.startswith("+ gcloud run jobs execute ")]
    kinds = ["build" if "api.sandbox,build," in x else "briefs" if "agent.briefs," in x
             else "activate" if "api.sandbox,activate," in x else "bench" if " demo-bench " in x else x
             for x in executes]
    assert kinds == ["build", "briefs", "activate", "bench"]  # Phase 9: briefs and activate before the bench
    assert "--no-activate" in executes[0] and "briefs:" not in p.stdout  # no briefs result printed as observed
    for later, earlier in (("run jobs execute demo-bench", "run jobs execute demo-build"),
                           ("run deploy demo-svc", "run jobs execute demo-bench"),
                           ("run services get-iam-policy demo-svc", "run deploy demo-svc")):
        assert order.index(earlier) < order.index(later)
    assert "builds submit" in order and "secrets versions access" not in order
    # both pins come first, before the image describe; the HQ preflight is named, never run
    assert order[:3] == ["secrets versions list", "secrets versions list", "artifacts docker images describe"]
    assert "dry run: the HQ preflight is skipped" in p.stdout
    assert "DEMO_PASSCODE=demo-pass:1,HQ_PASSCODE=demo-hq-pass:1" in p.stdout and "DEMO_MODE=off" in p.stdout
    assert "DEMO_MODE=on" not in p.stdout
    assert "@sha256:" + "0" * 64 in p.stdout
    for skipped in ("smoke", "journey", "manifest"):
        assert re.search(rf"dry run: .*{skipped}.* skipped", p.stdout), skipped
    assert sorted(ROOT.glob("deploy/releases/*")) == before
    assert "fake-passcode" not in p.stdout


def test_a_configuration_whose_hq_secret_is_the_passcode_secret_is_refused_before_any_command(tmp_path):
    """06-REVIEW IN-05: the same secret for both credentials is a key-only config error in every mode."""
    same = synthetic_env.write(tmp_path / "env", HQ_PASSCODE_SECRET=SYNTHETIC["PASSCODE_SECRET"])
    for argv in (["--dry-run"], ["--dry-run", "--rollback"], ["--dry-run", "--status"], ["--dry-run", "--early-bench"]):
        p = cli(*argv, "--env-file", str(same))
        assert p.returncode == 1, argv
        assert "config error: HQ_PASSCODE_SECRET" in p.stderr and "PASSCODE_SECRET" in p.stderr
        assert "+ gcloud" not in p.stdout and SYNTHETIC["PASSCODE_SECRET"] not in p.stdout + p.stderr


def test_release_sh_is_an_executable_wrapper():
    path = ROOT / "deploy" / "release.sh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    code = [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert code == ["set -euo pipefail", 'cd "$(git rev-parse --show-toplevel)"',
                    'exec .venv/bin/python -m deploy.release "$@"']
    assert path.read_text().startswith("#!/usr/bin/env bash\n")


# --- Task 2: recovery, the lock, resume, access, compatibility, rollback ---------------------------------------------

BUILD_ARGS = f"--args=-m,api.sandbox,build,--no-activate,--run-id,{NEW}"
RESTORE_ARGS = f"--args=-m,api.sandbox,restore,{NEW}"
FIRST_RESTORE_ARGS = RESTORE_ARGS + ",--first"  # a first release's restore (06-REVIEW WR-06)
NOT_ACTIVATED = f"not needed: {NEW} was built inactive (--no-activate) and never activated; it is left READY, inactive"  # Phase 9: nothing to restore before activate
OLD_SERVICE = {"status": {"latestReadyRevisionName": OLD_REV, "url": URL,
                          "traffic": [{"revisionName": OLD_REV, "percent": 100}]}}
RUNNING = {"status": {"startTime": "t0"}}
FINISHED = {"status": {"completionTime": "t1"}}


def boom(argv0: str = "gcloud x", code: int = 1, tail: str = "ERROR: boom") -> CommandError:
    return CommandError(argv0, code, tail)


def is_call(*lead: str, having: str | None = None):
    return lambda a: a[1:1 + len(lead)] == list(lead) and (having is None or having in a)


def indexes(cloud: Cloud, predicate) -> list[int]:
    return [i for i, c in enumerate(cloud.calls) if predicate(c)]


def restores(cloud: Cloud) -> list[int]:
    """The restore executions of <new>, with or without --first (a first-shaped release passes it; WR-06)."""
    return indexes(cloud, lambda c: is_call("run", "jobs", "execute", having=RESTORE_ARGS)(c)
                   or is_call("run", "jobs", "execute", having=FIRST_RESTORE_ARGS)(c))


def settles(cloud: Cloud) -> list[int]:
    return indexes(cloud, lambda c: c[1:4] == ["run", "jobs", "executions"])


def traffic(cloud: Cloud) -> list[list[str]]:
    """The --to-revisions moves (a recovery or a rollback)."""
    return [c for c in cloud.calls if c[1:4] == ["run", "services", "update-traffic"] and "--to-revisions" in c]


def to_latest(cloud: Cloud) -> list[list[str]]:
    """The --to-latest routes (after each deploy)."""
    return [c for c in cloud.calls if c[1:4] == ["run", "services", "update-traffic"] and "--to-latest" in c]


def updates_traffic(cloud: Cloud) -> list[list[str]]:
    return [c for c in cloud.calls if c[1:4] == ["run", "services", "update-traffic"]]


def listed(run_id: str = NEW, state: dict | None = None, name: str = BUILD_EXEC) -> dict:
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{"args": ["-m", "api.sandbox", "build", "--run-id",
                                                                    run_id]}]}}},
            **(state or FINISHED)}


def activation_listed(state: dict | None = None, name: str = ACTIVATE_EXEC) -> dict:
    return {"metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{"args": ["-m", "api.sandbox", "activate", NEW]}]}}},
            **(state or FINISHED)}


def only_record(out_dir) -> dict:
    [rec] = records(out_dir)
    return rec


def test_the_release_lock_file_is_git_ignored():
    p = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "deploy/.release.lock"])
    assert p.returncode == 0


@pytest.mark.parametrize("mode", [[], ["--rollback"], ["--early-bench"], ["--status"]],
                         ids=["release", "rollback", "early-bench", "status"])
def test_a_second_release_refuses_while_the_lock_is_held(mode, tmp_path, monkeypatch, capsys, out_dir, fakes):
    import fcntl
    lock = tmp_path / "release.lock"
    monkeypatch.setattr(release, "LOCK", lock)
    env_file = synthetic_env.write(tmp_path / "env")
    cloud = Cloud()
    with open(lock, "a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc = release.main([*mode, "--env-file", str(env_file)], runner=cloud, git=FakeGit(), out_dir=out_dir,
                          checks=lambda: "dv-1", smoke=fakes.smoke, journey=fakes.journey, now=lambda: NOW,
                          sleep=fakes.sleep)
        assert rc == 1
        assert "another release or rollback is running from this checkout" in capsys.readouterr().err
        assert cloud.calls == [] and not pathlib.Path(out_dir).exists()
        dry = DryRunner()
        assert release.main(["--dry-run", "--env-file", str(env_file)], runner=dry, git=FakeGit()) == 0
        assert dry.calls  # the dry run takes no lock


def test_the_lock_is_held_for_the_whole_release(tmp_path, monkeypatch, out_dir, fakes):
    import fcntl
    lock = tmp_path / "release.lock"
    monkeypatch.setattr(release, "LOCK", lock)
    probes = []

    def probe(argv, cloud):
        with open(lock, "a") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                probes.append("free")
            except BlockingIOError:
                probes.append("held")

    cloud = Cloud()
    cloud.on_call.append(probe)
    rc = release.main(["--env-file", str(synthetic_env.write(tmp_path / "env"))], runner=cloud, git=FakeGit(),
                      out_dir=out_dir, checks=lambda: "dv-1", smoke=fakes.smoke, journey=fakes.journey,
                      now=lambda: NOW, sleep=fakes.sleep)
    assert rc == 0 and probes and set(probes) == {"held"}
    with open(lock, "a") as f:  # released when main returns
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("cause", ["dirty", "data.reference --release", "pytest tests/deploy", "browser probe"])
def test_a_refusal_before_the_cloud_records_nothing(cause, cfg, out_dir, fakes, capsys):
    cloud = Cloud()

    def checks():
        raise release.ReleaseError(f"the {cause} check failed (exit 1)")

    git = FakeGit(dirty=" M api/app.py\n?? new.txt\n" if cause == "dirty" else "")
    rc = release.release(cfg, runner=cloud, git=git, now=lambda: NOW,
                         checks=(lambda: "dv-1") if cause == "dirty" else checks, smoke=fakes.smoke,
                         journey=fakes.journey, out_dir=out_dir, sleep=fakes.sleep)
    assert rc == 1 and cloud.calls == [] and not pathlib.Path(out_dir).exists()
    err = capsys.readouterr().err
    assert ("uncommitted or untracked" in err) if cause == "dirty" else (cause in err)


def test_an_invalid_configuration_refuses_before_any_command(tmp_path, monkeypatch, capsys, out_dir):
    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    bad = synthetic_env.write(tmp_path / "env", SERVICE="<service name>")
    cloud = Cloud()
    assert release.main(["--env-file", str(bad)], runner=cloud, git=FakeGit(), out_dir=out_dir) == 1
    assert cloud.calls == [] and "config error: SERVICE" in capsys.readouterr().err


def known_good(out_dir, rid="20261001T000000Z-aaaaaaaaaaaa", **fields) -> dict:
    rec = {"kind": "release", "id": rid, "status": "known_good", "engine_version": ENGINE_VERSION,
           "source_commit": "a" * 40, "new_template": GOOD_TPL, "activated_template": GOOD_TPL,
           "previous_revision": OLD_REV, **fields}
    manifest.write(rec, out_dir)
    return rec


def test_a_breaking_template_refuses_without_the_flag_and_is_recorded_with_it(cfg, out_dir, fakes, capsys):
    known_good(out_dir, engine_version="engine-0")
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    assert cloud.calls == [] and len(records(out_dir)) == 1
    err = capsys.readouterr().err
    assert "409 engine_version" in err and "--breaking-template" in err and "no judge is using the app" in err
    assert run_release(cfg, cloud, out_dir, fakes, breaking_template=True) == 0
    rec = records(out_dir)[-1]
    assert rec["template_compat"] == "breaking" and rec["status"] == "known_good"


def test_the_previous_known_good_release_is_named_and_compatible(cfg, out_dir, fakes):
    good = known_good(out_dir)
    assert run_release(cfg, Cloud(), out_dir, fakes) == 0
    rec = records(out_dir)[-1]
    assert rec["previous_known_good"] == good["id"] and rec["template_compat"] == "compatible"


@pytest.mark.parametrize("status, failing", [
    ("failed_image", is_call("builds", "submit")),
    ("failed_secret", is_call("secrets", "versions", "list")),
    ("failed_service_lookup", is_call("run", "services", "describe")),
    ("failed_build_deploy", is_call("run", "jobs", "deploy", having="demo-build")),
])
def test_a_failure_before_the_build_execute_runs_no_restore(status, failing, cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.raise_on.append((failing, boom()))
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == status
    assert not indexes(cloud, is_call("run", "jobs", "execute")) and not updates_traffic(cloud)
    assert not settles(cloud)
    assert rec["recovery"]["restore"].startswith("not needed")
    assert rec["progress"][-1]["state"] == "failed"


HQ_NOT_FOUND = CommandError("gcloud secrets versions list", 1,
                            "ERROR: (gcloud.secrets.versions.list) NOT_FOUND: Secret [demo-hq-pass] not found")


def _hq_disabled(cloud):
    cloud.hq_versions = [{"name": "projects/p/secrets/demo-hq-pass/versions/1", "state": "DISABLED"}]


def _hq_missing(cloud):
    cloud.raise_on.append((lambda a: a[1:4] == ["secrets", "versions", "list"] and a[4] == "demo-hq-pass",
                           HQ_NOT_FOUND))


def _hq_value(value):
    def apply(cloud):
        cloud.hq_value = value
    return apply


def _store_value(value):
    def apply(cloud):
        cloud.store_value = value
    return apply


# 06.1 D-64, D-68 and HQ handoff items 2 and 3: each refusal at the secret step, before any image, build, job or deploy
SECRET_REFUSALS = [
    ("no enabled HQ version", _hq_disabled, "HQ_PASSCODE_SECRET has no enabled version", None),
    ("HQ secret not found", _hq_missing, "HQ_PASSCODE_SECRET names no secret", None),
    ("HQ value blank", _hq_value("   \n"), "HQ_PASSCODE_SECRET: the pinned version is empty", None),
    ("HQ value 11 characters", _hq_value("  short-hq-11 \n"),
     "HQ_PASSCODE_SECRET: the pinned version is shorter than 12 characters", "short-hq-11"),
    ("HQ value equals the store passcode", _hq_value(f"  {PASSCODE}\n"),
     "HQ_PASSCODE_SECRET: the pinned version equals the store passcode (PASSCODE_SECRET)", PASSCODE),
    ("store value empty", _store_value(""), "PASSCODE_SECRET: the pinned version is empty", None),
]


@pytest.mark.parametrize("case, setup, message, value", SECRET_REFUSALS, ids=[c[0] for c in SECRET_REFUSALS])
def test_the_secret_step_refuses_before_any_image_build_job_or_deploy(case, setup, message, value, cfg, out_dir,
                                                                      fakes, capsys):
    cloud = Cloud()
    setup(cloud)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_secret" and [p["step"] for p in rec["progress"]] == ["secret"]
    assert rec["progress"][0]["state"] == "failed"
    assert {words(c[1:], 3) for c in cloud.calls} <= {"secrets versions list", "secrets versions access"}
    assert not cloud.did("artifacts") and not cloud.did("builds") and not cloud.did("run")
    assert rec["recovery"]["restore"].startswith("not needed") and rec["reconcile"] is None
    assert not fakes.smoke_calls and not fakes.journey_calls
    err = capsys.readouterr()
    assert f"release failed at secret (failed_secret): {message}" in err.err
    assert message in rec["error"]
    for secret in filter(None, (value, HQ_PASSCODE, PASSCODE)):
        assert secret not in err.out + err.err and secret not in json.dumps(rec)
        assert secret not in json.dumps(cloud.calls)


def test_the_hq_preflight_drops_both_values_and_keeps_them_masked(cfg):
    cloud = Cloud()
    assert release.hq_preflight(cfg, cloud, "3", "5") is None
    assert cloud.mask(f"x {PASSCODE} {HQ_PASSCODE} y") == "x (withheld) (withheld) y"
    reads = [(c[4], flag(c, "--secret")) for c in cloud.calls]
    assert reads == [("3", "demo-pass"), ("5", "demo-hq-pass")]


def test_the_hq_minimum_length_is_the_apps():
    """A9: the release's HQ_MIN_LENGTH is api.config's when the product app is merged; before that, the Phase 5 app
    has neither the constant nor HQ_PASSCODE. Never skipped."""
    from api import config

    assert release.HQ_MIN_LENGTH == 12
    if hasattr(config, "HQ_MIN_LENGTH"):
        assert release.HQ_MIN_LENGTH == config.HQ_MIN_LENGTH
    else:
        assert not hasattr(config, "HQ_PASSCODE")


def _fail_build_execute(cloud):
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=BUILD_ARGS), boom("gcloud run jobs execute")))
    cloud.executions = [listed("tpl-other-1"), listed()]


def _no_execution_name(cloud):
    cloud.raise_on.append((lambda a: False, None))
    original = cloud.respond
    cloud.respond = lambda a: {} if is_call("run", "jobs", "execute", having=BUILD_ARGS)(a) else original(a)
    cloud.executions = [listed()]


def _fail_build_log(cloud):
    cloud.raise_on.append((lambda a: a[1:3] == ["logging", "read"] and BUILD_EXEC in a[3], boom("gcloud logging")))


def _build_lines(*lines):
    def apply(cloud):
        cloud.build_lines = list(lines)
    return apply


def _activate_lines(*lines):
    def apply(cloud):
        cloud.activate_lines = list(lines)
    return apply


def _fail_bench(cloud):
    cloud.raise_on.append((is_call("run", "jobs", "execute", having="demo-bench"), boom("gcloud run jobs execute")))


def _fail_deploy(cloud):
    cloud.raise_on.append((is_call("run", "deploy"), boom("gcloud run deploy")))
    cloud.new_revision = OLD_REV  # the deploy made no new revision, so the latest ready one is still the serving one


def _readback(**attrs):
    def apply(cloud):
        for k, v in attrs.items():
            setattr(cloud, k, v)
    return apply


def _smoke_fail(fakes):
    fakes.smoke_result = [("healthz", "ok"), ("session", "FAIL 401 bad_passcode")]


def _journey_fail(fakes):
    fakes.journey_result = {"passed": False, "exit": 1}


def _smoke_raises(fakes):
    def smoke(url, code, hq):
        raise RuntimeError("connection reset")
    fakes.smoke = smoke


AFTER_EXECUTE = [
    ("failed_build", "build job failed after promotion", _fail_build_execute, None, False),
    ("failed_build", "no execution name", _no_execution_name, None, False),
    ("failed_build", "build log read-back", _fail_build_log, None, False),
    ("failed_build", "build log lacks previous ACTIVE",
     _build_lines(f"template {NEW} ACTIVE for dv-1", "retired: tpl-old"), None, False),
    ("failed_activate", "activate log lacks retired", _activate_lines(f"template {NEW} ACTIVE for dv-1"), None, False),
    ("failed_bench", "bench", _fail_bench, None, False),
    ("failed_deploy", "deploy", _fail_deploy, None, True),
    ("failed_readback", "max 2", _readback(max_scale="2"), None, True),
    ("failed_readback", "max not read back", _readback(max_scale=None), None, True),
    ("failed_readback", "min differs", _readback(min_scale="1"), None, True),
    ("failed_readback", "digest differs", _readback(revision_digest=f"{IMAGE}@sha256:{'b' * 64}"), None, True),
    ("failed_readback", "traffic 50", _readback(traffic=50), None, True),
    ("failed_readback", "access", _readback(policy={"bindings": []}, invoker_disabled="false"), None, True),
    ("failed_smoke", "smoke FAIL", None, _smoke_fail, True),
    ("failed_smoke", "smoke raises", None, _smoke_raises, True),
    ("failed_journey", "journey", None, _journey_fail, True),
]


@pytest.mark.parametrize("status, case, cloud_setup, fakes_setup, deployed", AFTER_EXECUTE,
                         ids=[f"{s}-{c.replace(' ', '_')}" for s, c, *_ in AFTER_EXECUTE])
def test_a_failure_from_the_build_execute_on_settles_then_restores(status, case, cloud_setup, fakes_setup, deployed,
                                                                   cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    if cloud_setup:
        cloud_setup(cloud)
    if fakes_setup:
        fakes_setup(fakes)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == status
    assert rec["recovery"]["settle"]["execution"] == BUILD_EXEC
    if status == "failed_build":  # Phase 9: the build left <new> READY and nothing activated it: settle, no restore
        assert settles(cloud) and not restores(cloud)
        assert rec["recovery"]["restore"] == NOT_ACTIVATED and "settle_activate" not in rec["recovery"]
        assert not indexes(cloud, is_call("run", "jobs", "execute", having=f"--args=-m,api.sandbox,activate,{NEW}"))
        assert not traffic(cloud) and rec["reconcile"] is None
        return
    assert settles(cloud) and restores(cloud), "settle, then restore"
    assert max(settles(cloud)) < min(restores(cloud))
    assert len(restores(cloud)) == 1
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert rec["recovery"]["settle_activate"] == {"execution": ACTIVATE_EXEC, "state": "finished"}
    if case.startswith(("build log lacks", "activate log lacks")):  # a log gate: never on to the bench or deploy
        assert not indexes(cloud, lambda c: "demo-bench" in c) and not cloud.did("run", "deploy")
    if deployed:
        [move] = traffic(cloud)
        assert flag(move, "--to-revisions") == f"{OLD_REV}=100" and min(restores(cloud)) < cloud.calls.index(move)
        assert rec["recovery"]["traffic"] == f"{OLD_REV}=100"
    else:
        assert not traffic(cloud) and "traffic" not in rec["recovery"]
    assert rec["reconcile"] is None
    if case == "access":
        assert rec["reason"] == "access" and "deploy/README.md (access step)" in capsys.readouterr().err


def test_an_ambiguous_build_execute_is_settled_before_restore(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=BUILD_ARGS), boom("gcloud run jobs execute")))
    cloud.executions = [listed(state=RUNNING)]
    cloud.exec_states = [RUNNING, RUNNING, FINISHED]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_build"
    lists = indexes(cloud, is_call("run", "jobs", "executions", "list"))
    describes = indexes(cloud, is_call("run", "jobs", "executions", "describe"))
    assert len(lists) == 1 and len(describes) == 3 and lists[0] < describes[0] < describes[-1]
    assert not restores(cloud) and rec["recovery"]["restore"] == NOT_ACTIVATED and rec["reconcile"] is None
    assert flag(cloud.calls[lists[0]], "--job") == "demo-build"
    assert fakes.sleeps.count(10) == 3 and not indexes(cloud, is_call("run", "jobs", "executions", "cancel"))


def test_settle_cancels_an_execution_still_running_after_five_minutes(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having="demo-bench"), boom("gcloud run jobs execute")))
    cloud.exec_states = [RUNNING] * 31 + [RUNNING, FINISHED]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    [cancel] = indexes(cloud, is_call("run", "jobs", "executions", "cancel"))
    assert BUILD_EXEC in cloud.calls[cancel]
    describes = indexes(cloud, is_call("run", "jobs", "executions", "describe"))
    assert len([i for i in describes if i < cancel]) == 31  # the first read, then 30 polls 10 s apart
    assert max(describes) < restores(cloud)[0] and cancel < restores(cloud)[0]
    rec = only_record(out_dir)
    assert rec["recovery"]["settle"]["state"] == "cancelled"
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"


@pytest.mark.parametrize("how", ["list raises", "two match", "nameless", "describe raises"])
def test_an_unreadable_execution_state_runs_no_restore_and_leaves_reconcile_pending(how, cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    if how == "describe raises":
        _fail_bench(cloud)
        cloud.raise_on.append((is_call("run", "jobs", "executions", "describe"), boom()))
    else:
        cloud.raise_on.append((is_call("run", "jobs", "execute", having=BUILD_ARGS), boom()))
        if how == "list raises":
            cloud.raise_on.append((is_call("run", "jobs", "executions", "list"), boom()))
        elif how == "two match":
            cloud.executions = [listed(), listed(name="demo-build-other")]
        else:
            cloud.executions = [{k: v for k, v in listed().items() if k != "metadata"}]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["reconcile"] == "pending" and not restores(cloud) and not traffic(cloud)
    assert rec["recovery"]["settle"].startswith("unknown")
    assert "rerun bash deploy/release.sh" in capsys.readouterr().err


def test_a_failed_restore_is_recorded_and_printed(cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    _fail_bench(cloud)
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=RESTORE_ARGS), boom("gcloud run jobs", 1)))
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_bench" and rec["recovery"]["restore"] == "failed: 1"
    assert rec["reconcile"] == "pending"
    err = capsys.readouterr().err
    assert "restore failed: 1" in err and "rerun bash deploy/release.sh" in err and "--rollback" not in err


@pytest.mark.parametrize("lines, want, pending", [
    (RESTORED_LINES, "restored: tpl-old ACTIVE", False),
    ([f"restored: tpl-other ACTIVE, {NEW} RETIRED", "ACTIVE: tpl-other"], "mismatch: ", True),
    ([f"restore: {NEW} is not ACTIVE; nothing to do", "ACTIVE: none"], "mismatch: ", True),
    ([], "unverified: ", True),
], ids=["matching", "another template ACTIVE", "no template ACTIVE", "log not read"])
def test_recovery_checks_the_restore_against_the_captured_template(lines, want, pending, cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    cloud.restore_lines = list(lines)
    _journey_fail(fakes)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["recovery"]["restore"].startswith(want)
    if "tpl-other" in str(lines):
        assert "tpl-other" in rec["recovery"]["restore"] and "tpl-old" in rec["recovery"]["restore"]
    [restore] = restores(cloud)
    assert max(settles(cloud)) < restore
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100" and restore < cloud.calls.index(move)
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100"
    assert rec["reconcile"] == ("pending" if pending else None)
    err = capsys.readouterr().err
    if pending:
        assert "rerun bash deploy/release.sh" in err and "--rollback" not in err


def test_a_build_execute_that_never_started_is_settled_as_never_started(cfg, out_dir, fakes):
    """06-REVIEW WR-03: no execution holds --run-id <new> after 3 lists, so nothing can promote; the verified no-op
    restore runs and the record is not left pending (it would wedge every later release)."""
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=BUILD_ARGS), boom("gcloud run jobs execute")))
    cloud.executions = [listed("tpl-someone-else")]
    cloud.restore_lines = [f"restore: {NEW} is not ACTIVE; nothing to do", "ACTIVE: tpl-old"]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    lists = indexes(cloud, is_call("run", "jobs", "executions", "list"))
    assert len(lists) == 3 and fakes.sleeps == [release.SETTLE_EVERY_S] * 2
    assert not indexes(cloud, is_call("run", "jobs", "executions", "describe"))
    rec = only_record(out_dir)
    assert rec["recovery"]["settle"] == {"execution": None, "state": "never started"}
    assert not restores(cloud)  # Phase 9: an inactive build never needs one
    assert rec["recovery"]["restore"] == NOT_ACTIVATED and rec["reconcile"] is None


def test_an_unmatched_running_build_execution_keeps_the_record_pending_until_it_ends(cfg, out_dir, fakes, capsys):
    """06-REVIEW WR-08: no listed execution holds a recognisable --run-id <new>, but one has not ended: it may be this
    release's build (an executions-list shape the tool does not read), so settle fails closed and nothing restores.
    Once every listed execution has ended, the next run concludes never started and restores."""
    unmatched = {"metadata": {"name": "demo-build-zz9"}}
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=BUILD_ARGS), boom("gcloud run jobs execute")))
    cloud.executions = [{**unmatched, **RUNNING}]
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    assert len(indexes(cloud, is_call("run", "jobs", "executions", "list"))) == 3
    assert fakes.sleeps == [release.SETTLE_EVERY_S] * 2
    rec = only_record(out_dir)
    assert rec["recovery"]["settle"].startswith("unknown: ") and "still running" in rec["recovery"]["settle"]
    assert not restores(cloud) and not traffic(cloud) and rec["reconcile"] == "pending"
    assert "rerun bash deploy/release.sh" in capsys.readouterr().err

    nxt = Cloud()
    nxt.executions = [{**unmatched, **FINISHED}]
    nxt.restore_lines = [f"restore: {NEW} is not ACTIVE; nothing to do", "ACTIVE: tpl-old"]
    assert run_release(cfg, nxt, out_dir, Fakes()) == 1
    rec = only_record(out_dir)
    assert rec["recovery"]["settle"] == {"execution": None, "state": "never started"}
    assert not restores(nxt) and rec["recovery"]["restore"] == NOT_ACTIVATED
    assert rec["reconcile"] is None and rec["status"] == "interrupted"
    assert not indexes(nxt, is_call("run", "jobs", "execute", having=BUILD_ARGS))


def test_a_settle_that_cannot_read_the_build_still_moves_traffic_back(cfg, out_dir, fakes, capsys):
    """06-REVIEW WR-11: the smoke failed after the service deploy and the build execution's state cannot be read: the
    restore waits (pending), but traffic still goes back to the captured serving revision and is read back."""
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    cloud.raise_on.append((is_call("run", "jobs", "executions", "describe"), boom()))
    _smoke_fail(fakes)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_smoke"
    assert rec["recovery"]["settle"].startswith("unknown: ")
    assert rec["recovery"]["restore"].startswith("not run: ") and not restores(cloud)
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    assert cloud.calls.index(move) < max(indexes(cloud, is_call("run", "services", "describe")))  # read back
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] == "pending"
    assert "rerun bash deploy/release.sh" in capsys.readouterr().err

    nxt = Cloud()
    nxt.service_before = OLD_SERVICE
    assert run_release(cfg, nxt, out_dir, Fakes()) == 1
    rec = only_record(out_dir)
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] is None
    assert not indexes(nxt, is_call("run", "jobs", "execute", having=BUILD_ARGS)) and not nxt.did("run", "deploy")


FIRST_NOTHING = [f"restore: {NEW} is not ACTIVE; nothing to do", "ACTIVE: none"]
FIRST_UNDONE = [f"first release undone: {NEW} RETIRED", "ACTIVE: none"]


@pytest.mark.parametrize("path", ["build execute failed", "build log not read"])
def test_a_first_release_whose_build_fails_restores_nothing(path, cfg, out_dir, fakes, capsys):
    """Phase 9: a first release whose inactive build fails activated nothing, so it settles, restores nothing and is
    not left reconcile: pending (the WR-06 `--first` path now follows an activation that retired none)."""
    cloud = Cloud()
    if path == "build execute failed":
        cloud.raise_on.append((is_call("run", "jobs", "execute", having=BUILD_ARGS), boom("gcloud run jobs execute")))
        cloud.executions = [listed()]
    else:
        cloud.build_lines = []
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_build"
    assert rec["recovery"]["settle"] == {"execution": BUILD_EXEC, "state": "finished"}
    assert not restores(cloud) and rec["recovery"]["restore"] == NOT_ACTIVATED and rec["reconcile"] is None
    assert "recovery is not finished" not in capsys.readouterr().err


@pytest.mark.parametrize("outcome", ["undone", "nothing to do"])
def test_an_activation_that_retired_none_is_undone_with_first(outcome, cfg, out_dir, fakes, capsys):
    """09-24 checklist item 7, Recovery: the activation retired none (`retired: none`), then the bench failed: the
    activate execution is settled, then `restore <new> --first`, and no template ACTIVE afterwards is verified."""
    cloud = Cloud()
    cloud.build_lines = ["previous ACTIVE: none", f"template {NEW} READY for dv-1"]
    cloud.restore_lines = list(FIRST_UNDONE if outcome == "undone" else FIRST_NOTHING)
    _fail_bench(cloud)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_bench" and rec["previous_template"] is None
    assert rec["recovery"]["settle_activate"] == {"execution": ACTIVATE_EXEC, "state": "finished"}
    [restore] = restores(cloud)
    assert FIRST_RESTORE_ARGS in cloud.calls[restore]
    assert rec["recovery"]["restore"] == f"{outcome}: none ACTIVE" and rec["reconcile"] is None


@pytest.mark.parametrize("not_first", ["a known-good release", "a serving revision", "an earlier activation"])
def test_none_active_is_a_mismatch_unless_this_is_the_first_release(not_first, cfg, out_dir, fakes, capsys):
    """WR-06's guard: the same `ACTIVE: none` for a release that is not first stays a mismatch and pending."""
    cloud = Cloud()
    if not_first == "a known-good release":
        known_good(out_dir)
    elif not_first == "a serving revision":
        cloud.service_before = OLD_SERVICE
    else:
        manifest.write({"kind": "release", "id": "20261015T000000Z-bbbbbbbbbbbb", "status": "failed_bench",
                        "previous_known_good": None, "activated_template": "tpl-x", "reconcile": None}, out_dir)
    _fail_bench(cloud)  # after an activation that retired tpl-old
    cloud.restore_lines = list(FIRST_NOTHING)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = records(out_dir)[-1]
    assert rec["id"] == RID
    [restore] = restores(cloud)
    assert RESTORE_ARGS in cloud.calls[restore] and FIRST_RESTORE_ARGS not in cloud.calls[restore]
    assert rec["recovery"]["restore"].startswith("mismatch: nothing to do, none ACTIVE")
    assert rec["reconcile"] == "pending"


def test_an_undone_outcome_with_a_template_active_is_a_mismatch(cfg):
    cloud = Cloud()
    cloud.restore_lines = [f"first release undone: {NEW} RETIRED", "ACTIVE: tpl-x"]
    got = release.restore(cfg, cloud, NEW, manifest.NOT_READ_BACK, first=True, sleep=lambda s: None)
    assert got.startswith("mismatch: undone, tpl-x ACTIVE")


class Killed(BaseException):
    """The laptop closes mid-recovery."""


@pytest.mark.parametrize("variant, exc", [("fails", boom("gcloud run jobs", 1)),
                                          ("raises", ValueError("unexpected gcloud output")),
                                          ("killed", Killed())], ids=["fails", "raises", "killed"])
def test_a_failed_undo_is_retried_by_the_next_release(variant, exc, cfg, out_dir, fakes, capsys):
    """06-REVIEW CR-02, WR-02: the record is saved reconcile: pending before recovering, so an undo that fails, raises
    or dies is retried by the next run, which starts no build and clears pending once the pair is proved back."""
    def failing_cloud():
        cloud = Cloud()
        cloud.service_before = OLD_SERVICE
        cloud.raise_on.append((is_call("run", "jobs", "execute", having=RESTORE_ARGS), exc))
        return cloud

    _journey_fail(fakes)
    if variant == "killed":
        with pytest.raises(Killed):
            run_release(cfg, failing_cloud(), out_dir, fakes)
    else:
        assert run_release(cfg, failing_cloud(), out_dir, fakes) == 1
        assert only_record(out_dir)["recovery"]["restore"] == ("failed: 1" if variant == "fails"
                                                               else "failed: ValueError")
        assert "--rollback" not in capsys.readouterr().err
    rec = only_record(out_dir)
    assert rec["status"] == "failed_journey" and rec["reconcile"] == "pending"
    if variant == "fails":  # the next run's restore fails again: still pending
        assert run_release(cfg, failing_cloud(), out_dir, Fakes()) == 1
        rec = only_record(out_dir)
        assert rec["reconcile"] == "pending" and rec["status"] == "interrupted"
        assert rec["status_before_reconcile"] == "failed_journey"

    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    assert run_release(cfg, cloud, out_dir, Fakes()) == 1
    assert not cloud.did("artifacts") and not cloud.did("builds") and not cloud.did("run", "deploy")
    assert not indexes(cloud, is_call("run", "jobs", "execute", having=BUILD_ARGS))
    rec = only_record(out_dir)
    assert rec["status"] == "interrupted" and rec["status_before_reconcile"] == "failed_journey"
    assert rec["reconcile"] is None
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE" and rec["recovery"]["traffic"] == f"{OLD_REV}=100"
    [restore], [move] = restores(cloud), traffic(cloud)
    assert settles(cloud)[0] < restore < cloud.calls.index(move)

    later = NOW + dt.timedelta(minutes=1)
    newer = f"tpl-{SHA[:12]}-{later:%Y%m%d%H%M%S}"
    fresh = Cloud()
    fresh.build_lines = [line.replace(NEW, newer) for line in BUILD_LINES]
    assert release.release(cfg, runner=fresh, git=FakeGit(), now=lambda: later, checks=lambda: "dv-1",
                           smoke=Fakes().smoke, journey=Fakes().journey, out_dir=out_dir, sleep=Fakes().sleep) == 0


def interrupted_record(out_dir, **fields) -> dict:
    rec = {"kind": "release", "id": "20261016T120000Z-0123456789ab", "status": "in_progress",
           "new_template": NEW, "build_execution": BUILD_EXEC, "previous_revision": OLD_REV, "reconcile": None,
           "active_before_build": "tpl-old",
           "progress": [{"step": "image", "state": "ok"}, {"step": "build_execute", "state": "ok"},
                        {"step": "service_deploy", "state": "requested"}], **fields}
    manifest.write(rec, out_dir)
    return rec


@pytest.mark.parametrize("left", ["in_progress", "reconcile pending"])
def test_an_interrupted_release_is_reconciled_not_rebuilt(left, cfg, out_dir, fakes, capsys):
    if left == "in_progress":
        interrupted_record(out_dir)
    else:
        interrupted_record(out_dir, status="failed_bench", reconcile="pending",
                           progress=[{"step": "build_execute", "state": "ok"}, {"step": "bench", "state": "failed"}])
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    assert not cloud.did("artifacts") and not cloud.did("builds") and not cloud.did("run", "deploy")
    assert not indexes(cloud, is_call("run", "jobs", "execute", having=BUILD_ARGS))
    assert len(records(out_dir)) == 1  # no new record
    rec = only_record(out_dir)
    assert rec["status"] == "interrupted" and rec["reconcile"] is None
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert restores(cloud) and settles(cloud)[0] < restores(cloud)[0]
    assert bool(traffic(cloud)) == (left == "in_progress")
    out = capsys.readouterr()
    assert "commit" in out.out + out.err and "run bash deploy/release.sh again" in out.out + out.err


def test_an_interrupted_first_release_is_settled_by_the_next_run_and_the_one_after_builds(cfg, out_dir, fakes, capsys):
    """The WR-06 tracer: a first release killed during its build execute (no execution name, no build log read) is
    settled by the next run, which retires its template (`--first`) so none is ACTIVE, clears pending and builds
    nothing; the run after that builds and is known good, with no hand-editing."""
    rec = interrupted_record(out_dir, previous_revision=None, previous_known_good=None,
                             progress=[{"step": "image", "state": "ok"}, {"step": "previous_revision", "state": "ok"},
                                       {"step": "build_execute", "state": "requested"}])
    for key in ("build_execution", "active_before_build"):
        rec.pop(key)
    manifest.save(manifest.path_for(rec, out_dir), rec)
    cloud = Cloud()
    cloud.executions = [listed()]
    cloud.restore_lines = list(FIRST_UNDONE)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    got = only_record(out_dir)
    assert got["status"] == "interrupted" and got["reconcile"] is None
    assert got["recovery"]["restore"] == "undone: none ACTIVE"
    [restore] = restores(cloud)
    assert FIRST_RESTORE_ARGS in cloud.calls[restore]
    assert not indexes(cloud, is_call("run", "jobs", "execute", having=BUILD_ARGS)) and not cloud.did("run", "deploy")
    assert "recovery is not finished" not in capsys.readouterr().err

    later = NOW + dt.timedelta(minutes=1)
    newer = f"tpl-{SHA[:12]}-{later:%Y%m%d%H%M%S}"
    fresh = Cloud()
    fresh.build_lines = [line.replace(NEW, newer) for line in BUILD_LINES]
    assert release.release(cfg, runner=fresh, git=FakeGit(), now=lambda: later, checks=lambda: "dv-1",
                           smoke=Fakes().smoke, journey=Fakes().journey, out_dir=out_dir, sleep=Fakes().sleep) == 0
    assert release.known_good(out_dir)["new_template"] == newer


def test_rollback_reconciles_an_interrupted_release_first(cfg, out_dir, fakes):
    interrupted_record(out_dir)
    cloud = Cloud()
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    assert only_record(out_dir)["status"] == "interrupted"
    assert not list(pathlib.Path(out_dir).glob("*-rollback.json"))


def test_a_refused_public_binding_falls_back_to_no_invoker_iam_check(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.policy = {"bindings": [{"role": "roles/run.invoker", "members": ["domain:example.invalid"]}]}
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    [update] = indexes(cloud, is_call("run", "services", "update", having="--no-invoker-iam-check"))
    assert indexes(cloud, is_call("run", "services", "get-iam-policy"))[0] < update
    rec = only_record(out_dir)
    assert rec["access"] == "invoker check disabled" and rec["status"] == "known_good"
    assert fakes.smoke_calls  # it went on to the smoke


@pytest.mark.parametrize("how", ["update raises", "annotation not true"])
def test_a_refused_invoker_check_fails_at_access_and_recovers(how, cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    cloud.policy = {"bindings": []}
    if how == "update raises":
        cloud.raise_on.append((is_call("run", "services", "update"), boom()))
    else:
        cloud.invoker_disabled = "false"
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert (rec["status"], rec["reason"]) == ("failed_readback", "access")
    assert restores(cloud) and traffic(cloud) and not fakes.smoke_calls
    assert "organisation policy" in capsys.readouterr().err


def test_a_non_zero_deploy_whose_revision_is_ready_goes_on_to_the_access_check(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    cloud.raise_on.append((is_call("run", "deploy"), boom("gcloud run deploy", 1, "allUsers binding refused")))
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    rec = only_record(out_dir)
    [deployed] = [e for e in rec["progress"] if e["step"] == "service_deploy"]
    assert rec["status"] == "known_good" and "non-zero" in deployed["note"]
    assert indexes(cloud, is_call("run", "services", "get-iam-policy"))
    deployed = cloud.calls.index(call(cloud, "run", "deploy"))
    described = [i for i in indexes(cloud, is_call("run", "services", "describe")) if i > deployed]
    [route] = to_latest(cloud)
    policy = indexes(cloud, is_call("run", "services", "get-iam-policy"))[0]
    assert deployed < described[0] < cloud.calls.index(route) < described[-1] < policy


@pytest.mark.parametrize("how", ["no new revision", "another digest"])
def test_a_non_zero_deploy_routes_only_a_new_revision_with_the_built_digest(how, cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    cloud.raise_on.append((is_call("run", "deploy"), boom("gcloud run deploy")))
    if how == "no new revision":
        cloud.new_revision = OLD_REV
    else:
        cloud.revision_digest = f"{IMAGE}@sha256:{'c' * 64}"
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    assert only_record(out_dir)["status"] == "failed_deploy"
    assert to_latest(cloud) == []
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"


@pytest.mark.parametrize("case", ["a: an earlier failed revision of the same image", "b: another revision created"])
def test_a_non_zero_deploy_never_routes_a_revision_it_did_not_create(case, cfg, out_dir, fakes):
    """06-REVIEW WR-09, the invariant: after a non-zero deploy, traffic goes only to the revision this deploy created
    (the latest created one, absent before the deploy, ready, with the built digest); never to a revision name that
    existed before it, nor to a ready revision the deploy did not create."""
    cloud = Cloud()
    if case.startswith("a"):
        cloud.service_before = {"status": {"latestReadyRevisionName": FAILED_REV,
                                           "latestCreatedRevisionName": FAILED_REV, "url": URL,
                                           "traffic": [{"revisionName": OLD_REV, "percent": 100}]}}
        cloud.new_revision, cloud.created_revision = FAILED_REV, "demo-svc-00003-notready"
    else:
        cloud.service_before = OLD_SERVICE
        cloud.new_revision, cloud.created_revision = NEW_REV, "demo-svc-00004-newer"
    before = {OLD_REV, FAILED_REV} if case.startswith("a") else {OLD_REV}
    cloud.raise_on.append((is_call("run", "deploy"), boom("gcloud run deploy")))
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_deploy" and to_latest(cloud) == []
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    routed = {flag(c, "--to-revisions").split("=")[0] for c in traffic(cloud)}
    assert routed <= before and routed == {rec["previous_revision"]}
    assert release.known_good(out_dir) is None


def svc_with(traffic_list, latest=OLD_REV) -> dict:
    status = {"url": URL, "traffic": traffic_list}
    if latest is not None:
        status["latestReadyRevisionName"] = latest
    return {"status": status}


SPLIT = [{"revisionName": OLD_REV, "percent": 50}, {"revisionName": FAILED_REV, "percent": 50}]
NAMELESS = [{"latestRevision": True, "percent": 100}]


@pytest.mark.parametrize("svc, want", [
    (svc_with([{"revisionName": OLD_REV, "percent": 100}], latest=FAILED_REV), OLD_REV),
    (svc_with(NAMELESS, latest=FAILED_REV), FAILED_REV),
    (svc_with([{"revisionName": OLD_REV, "percent": 100}, {"revisionName": FAILED_REV, "percent": 0, "tag": "t"}]),
     OLD_REV),
    (svc_with(SPLIT), None),
    (svc_with([]), None),
    (svc_with([{"percent": 100}]), None),
    (svc_with(NAMELESS, latest=None), None),
], ids=["100% on old while latest ready failed", "latestRevision entry", "0% tagged entry ignored", "50/50 split",
        "no traffic", "nameless entry", "latestRevision without a latest ready revision"])
def test_the_serving_revision_is_read_from_status_traffic(svc, want, cfg):
    if want is None:
        with pytest.raises(release.ReleaseError, match="split or unreadable"):
            release.serving(svc)
    else:
        assert release.serving(svc) == want
    assert release.previous_revision(cfg, Cloud()) is None  # describe: not found
    assert release.previous_revision(cfg, DryRunner()) is None  # an empty answer


@pytest.mark.parametrize("routes, latest", [(SPLIT, OLD_REV), ([], OLD_REV), ([{"percent": 100}], OLD_REV)],
                         ids=["50/50 split", "no traffic", "nameless entry with a ready revision"])
def test_split_traffic_refuses_before_any_change(routes, latest, cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.service_before = svc_with(routes, latest=latest)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_service_lookup" and "split" in rec["error"]
    assert rec["recovery"]["restore"].startswith("not needed")
    assert not [c for c in cloud.calls if c[1:3] == ["run", "jobs"]]
    assert not cloud.did("run", "deploy") and not updates_traffic(cloud)


@pytest.mark.parametrize("routes", [[], NAMELESS], ids=["no traffic", "nameless entry"])
def test_a_service_with_no_ready_revision_is_released_as_a_first_release(routes, cfg, out_dir, fakes):
    """06-REVIEW WR-07: a service whose only revision never became ready (no latest ready revision, and no traffic
    entry names a revision) has nothing serving, so the release goes on as a first release."""
    cloud = Cloud()
    cloud.service_before = {"status": {"latestCreatedRevisionName": "demo-svc-00001-broken", "url": URL,
                                       "traffic": routes}}
    assert release.previous_revision(cfg, cloud) is None
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    rec = only_record(out_dir)
    assert rec["status"] == "known_good" and rec["previous_revision"] is None
    assert len(to_latest(cloud)) == 1


def test_two_failed_releases_in_a_row_send_traffic_back_to_the_serving_revision(cfg, out_dir, fakes):
    """After a first failed release the latest ready revision is the failed one, while 100% is still on OLD_REV
    (06-REVIEW CR-01): the second failed release goes back to OLD_REV, never to the failed revision."""
    cloud = Cloud()
    cloud.service_before = svc_with([{"revisionName": OLD_REV, "percent": 100}], latest=FAILED_REV)
    _journey_fail(fakes)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["previous_revision"] == OLD_REV
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    assert not [c for c in updates_traffic(cloud) if any(FAILED_REV in x for x in c)]
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] is None


@pytest.mark.parametrize("how, want", [("another revision serves", "mismatch: "),
                                       ("traffic unreadable", "unverified: ")])
def test_a_traffic_move_that_does_not_read_back_leaves_reconcile_pending(how, want, cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    if how == "another revision serves":
        cloud.moves_ignored = True
    else:
        cloud.raise_on.append((lambda a: a[1:4] == ["run", "services", "describe"]
                               and any("--to-revisions" in c for c in cloud.calls), boom()))
    _journey_fail(fakes)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["recovery"]["traffic"].startswith(want) and rec["reconcile"] == "pending"
    assert "rerun bash deploy/release.sh" in capsys.readouterr().err


A5_LINE = (f"traffic: the move to {OLD_REV} exited 1, but the read-back shows {OLD_REV}=100: counted as moved")


@pytest.mark.parametrize("case", ["a: read back on the revision", "b: the move did not land", "c: read back as a split",
                                  "d: the read-back fails"])
def test_a_traffic_move_that_exits_non_zero_counts_only_when_the_read_back_proves_it(case, cfg, out_dir, fakes,
                                                                                     capsys):
    """A5 (approved 2026-10-17): update-traffic can exit non-zero after Cloud Run applied the route (its wait for the
    service's Ready condition failed). The move then counts only when the read-back shows exactly that revision at
    100%, the check a zero exit passes; anything else stays `failed: <code>` and pending. The stderr is never shown."""
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    cloud.raise_on.append((is_call("run", "services", "update-traffic", having="--to-revisions"),
                           boom("gcloud run services update-traffic", 1, "ERROR: secret-ish detail")))
    if case.startswith("b"):
        cloud.moves_ignored = True
    elif case.startswith("c"):
        cloud.routes_after_move = SPLIT
    elif case.startswith("d"):
        cloud.raise_on.append((lambda a: a[1:4] == ["run", "services", "describe"]
                               and any("--to-revisions" in c for c in cloud.calls), boom()))
    _smoke_fail(fakes)
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    err = capsys.readouterr().err
    assert "secret-ish" not in err and "secret-ish" not in json.dumps(rec)
    if case.startswith("a"):
        assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] is None
        assert err.splitlines().count(A5_LINE) == 1
        assert cloud.calls.index(move) < max(indexes(cloud, is_call("run", "services", "describe")))  # read back
    else:
        assert rec["recovery"]["traffic"] == "failed: 1" and rec["reconcile"] == "pending"
        assert "traffic: the move to" not in err


@pytest.mark.parametrize("lands", [True, False], ids=["read back", "not landed"])
def test_a_rollback_traffic_move_that_exits_non_zero_counts_only_on_its_read_back(lands, cfg, out_dir, fakes, capsys):
    """A5 in rollback(): the same helper. A raised move whose read-back shows the previous revision alone records
    rolled_back (the predecessor is known good again); one that did not land stays failed_rollback with failed: 1."""
    manifest.write({"kind": "release", "id": PREDECESSOR, "status": "known_good", "engine_version": ENGINE_VERSION,
                    "source_commit": "b" * 40, "new_template": "tpl-older", "activated_template": "tpl-older",
                    "previous_revision": None}, out_dir)
    good = known_good(out_dir, previous_template="tpl-older")
    cloud = Cloud()
    cloud.restore_lines = [f"restored: tpl-older ACTIVE, {GOOD_TPL} RETIRED", "ACTIVE: tpl-older"]
    cloud.raise_on.append((is_call("run", "services", "update-traffic"),
                           boom("gcloud run services update-traffic", 1, "ERROR: secret-ish detail")))
    cloud.moves_ignored = not lands
    rc = release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep)
    [rb] = [json.loads(p.read_text()) for p in pathlib.Path(out_dir).glob("*-rollback.json")]
    out = capsys.readouterr()
    assert "secret-ish" not in out.out + out.err
    if lands:
        assert rc == 0 and rb["status"] == "rolled_back" and rb["results"]["traffic"] == f"{OLD_REV}=100"
        assert out.err.splitlines().count(A5_LINE) == 1
        assert release.known_good(out_dir)["id"] == PREDECESSOR
    else:
        assert rc == 1 and rb["status"] == "failed_rollback" and rb["results"]["traffic"] == "failed: 1"
        assert "traffic: the move to" not in out.err
        assert release.known_good(out_dir)["id"] == good["id"]


@pytest.mark.parametrize("max_i, min_i, m, fails", [
    (manifest.NOT_READ_BACK, "0", "0", True),
    ("1", manifest.NOT_READ_BACK, "1", True),
    ("1", manifest.NOT_READ_BACK, "0", False),
    ("1", "1", "1", False),
], ids=["max not read back", "min not read back with MIN_INSTANCES 1", "min not read back with MIN_INSTANCES 0",
        "both read back"])
def test_a_missing_scale_value_fails_the_read_back(max_i, min_i, m, fails, cfg):
    rb = {"max_instances": max_i, "min_instances": min_i, "revision_digest": DIGEST, "traffic_percent": 100,
          "url": URL}
    problems = release.read_back_problems(rb, {**cfg, "MIN_INSTANCES": m}, DIGEST)
    assert bool(problems) == fails
    if fails:
        assert "not read back" in " ".join(problems)


def test_rollback_restores_the_latest_known_good_template_and_revision(cfg, out_dir, fakes, capsys):
    good = known_good(out_dir, previous_template="tpl-older")
    cloud = Cloud()
    cloud.restore_lines = [f"restored: tpl-older ACTIVE, {GOOD_TPL} RETIRED", "ACTIVE: tpl-older"]
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 0
    assert "traffic: the move to" not in capsys.readouterr().err  # a zero exit prints no read-back line (A5)
    [restore] = [c for c in cloud.calls if c[1:4] == ["run", "jobs", "execute"]]
    assert f"--args=-m,api.sandbox,restore,{good['activated_template']}" in restore and "demo-build" in restore
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    [rb] = [json.loads(p.read_text()) for p in pathlib.Path(out_dir).glob("*-rollback.json")]
    assert rb["kind"] == "rollback" and rb["status"] == "rolled_back" and rb["of"] == good["id"]
    assert rb["id"] == f"20261016T140506Z-{'a' * 12}-rollback"
    assert rb["results"]["restore"] == "restored: tpl-older ACTIVE"
    # the rolled-back release no longer counts as known good for the next release
    assert release.compat(out_dir) == (ENGINE_VERSION, "first")
    again = Cloud()
    assert release.rollback(cfg, runner=again, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 0
    assert again.calls == []  # already rolled back: nothing to do


@pytest.mark.parametrize("previous", [None, manifest.NOT_READ_BACK])
def test_rollback_without_a_previous_revision_refuses(previous, cfg, out_dir, fakes, capsys):
    known_good(out_dir, previous_revision=previous)
    cloud = Cloud()
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    assert cloud.calls == [] and "previous revision" in capsys.readouterr().err


@pytest.mark.parametrize("lines, want", [
    ([f"restore: {GOOD_TPL} is not ACTIVE; nothing to do", f"ACTIVE: {NEW}"], "mismatch: nothing to do"),
    ([f"restore: {GOOD_TPL} is not ACTIVE; nothing to do", "ACTIVE: tpl-older"], "nothing to do: tpl-older ACTIVE"),
    ([f"restored: tpl-other ACTIVE, {GOOD_TPL} RETIRED", "ACTIVE: tpl-other"], "mismatch: restored"),
    ([], "unverified: "),
], ids=["newer template ACTIVE", "already back", "another template restored", "log not read"])
def test_rollback_moves_no_traffic_unless_the_restore_put_the_previous_template_back(lines, want, cfg, out_dir,
                                                                                      fakes, capsys):
    good = known_good(out_dir, previous_template="tpl-older")
    cloud = Cloud()
    cloud.restore_lines = lines
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    assert not traffic(cloud)
    [rb] = [json.loads(p.read_text()) for p in pathlib.Path(out_dir).glob("*-rollback.json")]
    assert rb["status"] == "failed_rollback" and rb["results"]["restore"].startswith(want)
    assert rb["results"]["traffic"].startswith("not moved")
    err = capsys.readouterr().err
    assert "rollback refused" in err and "no traffic was moved" in err
    assert release.known_good(out_dir)["id"] == good["id"]


PREDECESSOR = "20260930T000000Z-bbbbbbbbbbbb"


def test_a_half_done_rollback_is_finished_by_the_next_rollback(cfg, out_dir, fakes, capsys):
    """06-REVIEW WR-10: a rollback whose restore put the previous template back but whose traffic move failed is
    finished by the next --rollback (`nothing to do: <previous template> ACTIVE` after an earlier `restored:`), which
    moves the traffic, reads it back and records rolled_back, so the records name the predecessor as serving."""
    manifest.write({"kind": "release", "id": PREDECESSOR, "status": "known_good", "engine_version": "engine-2",
                    "source_commit": "b" * 40, "new_template": "tpl-older", "activated_template": "tpl-older",
                    "previous_revision": None}, out_dir)
    good = known_good(out_dir, previous_template="tpl-older")
    assert release.compat(out_dir) == (ENGINE_VERSION, "compatible")

    cloud = Cloud()
    cloud.restore_lines = [f"restored: tpl-older ACTIVE, {GOOD_TPL} RETIRED", "ACTIVE: tpl-older"]
    cloud.raise_on.append((is_call("run", "services", "update-traffic"), boom("gcloud run services", 1)))
    cloud.moves_ignored = True  # the move did not land, so the read-back after the non-zero exit cannot prove it (A5)
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    [first] = [json.loads(p.read_text()) for p in pathlib.Path(out_dir).glob("*-rollback.json")]
    assert first["status"] == "failed_rollback" and first["results"]["restore"] == "restored: tpl-older ACTIVE"
    assert first["results"]["traffic"] == "failed: 1"
    assert release.compat(out_dir) == (ENGINE_VERSION, "compatible")
    capsys.readouterr()

    again = Cloud()
    again.restore_lines = [f"restore: {GOOD_TPL} is not ACTIVE; nothing to do", "ACTIVE: tpl-older"]
    later = NOW + dt.timedelta(minutes=1)
    assert release.rollback(cfg, runner=again, now=lambda: later, out_dir=out_dir, sleep=fakes.sleep) == 0
    [move] = traffic(again)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    assert again.calls.index(move) < max(indexes(again, is_call("run", "services", "describe")))  # read back
    second = json.loads((pathlib.Path(out_dir) / f"{later:%Y%m%dT%H%M%SZ}-{'a' * 12}-rollback.json").read_text())
    assert second["status"] == "rolled_back" and second["of"] == good["id"] and second["resumes"] == first["id"]
    assert second["results"]["restore"] == "nothing to do: tpl-older ACTIVE"
    assert second["results"]["traffic"] == f"{OLD_REV}=100"
    assert f"rollback: resumes {first['id']}" in capsys.readouterr().out
    assert release.known_good(out_dir)["id"] == PREDECESSOR
    assert release.compat(out_dir) == (ENGINE_VERSION, "breaking")  # read from the predecessor's engine-2


@pytest.mark.parametrize("earlier", ["mismatch: restored, tpl-other ACTIVE, expected tpl-older",
                                     "unverified: the restore's log was not read back"], ids=["mismatch", "unverified"])
def test_a_rollback_resumes_only_after_an_earlier_rollback_restored_the_template(earlier, cfg, out_dir, fakes,
                                                                                 capsys):
    good = known_good(out_dir, previous_template="tpl-older")
    manifest.write({"kind": "rollback", "id": f"20261016T130000Z-{'a' * 12}-rollback", "of": good["id"],
                    "status": "failed_rollback", "template": GOOD_TPL, "previous_template": "tpl-older",
                    "previous_revision": OLD_REV, "results": {"restore": earlier, "traffic": "not moved: x"}},
                   out_dir)
    cloud = Cloud()
    cloud.restore_lines = [f"restore: {GOOD_TPL} is not ACTIVE; nothing to do", "ACTIVE: tpl-older"]
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    assert not traffic(cloud)
    rb = json.loads((pathlib.Path(out_dir) / f"20261016T140506Z-{'a' * 12}-rollback.json").read_text())
    assert rb["status"] == "failed_rollback" and "resumes" not in rb
    assert rb["results"]["restore"] == "nothing to do: tpl-older ACTIVE"
    err = capsys.readouterr().err
    assert "rollback refused" in err and "no traffic was moved" in err and "ACTIVE" in err
    assert release.known_good(out_dir)["id"] == good["id"]


@pytest.mark.parametrize("previous_template", ["missing", None, manifest.NOT_READ_BACK])
def test_rollback_without_a_recorded_previous_template_refuses(previous_template, cfg, out_dir, fakes, capsys):
    known_good(out_dir, **({} if previous_template == "missing" else {"previous_template": previous_template}))
    cloud = Cloud()
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    assert cloud.calls == [] and not list(pathlib.Path(out_dir).glob("*-rollback.json"))
    assert "previous template" in capsys.readouterr().err


def test_rollback_without_a_known_good_release_refuses(cfg, out_dir, fakes):
    cloud = Cloud()
    assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep) == 1
    assert cloud.calls == []


# Spelled in pieces so no file in this repository spells them whole (the scan below covers deploy/ only).
FORBIDDEN = ["--min" + "-instances", "--max" + "-instances", "--no-cpu" + "-throttling", "--use" + "-http2"]


def test_no_deploy_script_spells_a_forbidden_flag():
    files = sorted(ROOT.glob("deploy/*.py")) + sorted(ROOT.glob("deploy/*.sh"))
    assert any(f.name == "release.py" for f in files)
    for f in files:
        text = f.read_text()
        for bad in FORBIDDEN:
            assert bad not in text, f"{f.name} spells {bad}"


def test_every_release_argv_keeps_the_flag_and_environment_rules(cfg, out_dir, tmp_path, fakes):
    happy = Cloud()
    assert run_release(cfg, happy, out_dir, fakes) == 0
    fallback = Cloud()
    fallback.policy = {"bindings": []}
    assert run_release(cfg, fallback, tmp_path / "other", fakes) == 0
    for cloud in (happy, fallback):
        for argv in cloud.calls:
            text = " ".join(argv)
            assert not any(bad in argv for bad in FORBIDDEN)
            assert SYNTHETIC["BILLING_ACCOUNT"] not in text
            assert "POOL_" not in text and "FORWARDED_ALLOW_IPS" not in text and "K_SERVICE" not in text
            if "--no-invoker-iam-check" in argv:
                assert argv[1:4] == ["run", "services", "update"] and cloud is fallback
        [deploy] = [c for c in cloud.calls if c[1:3] == ["run", "deploy"]]
        assert deploy.count("--max") == 1 and flag(deploy, "--max") == "1"
        assert deploy.count("--allow-unauthenticated") == 1
        assert list(env_of(deploy)) == ["CLOUD_SQL_INSTANCE", "DB_USER", "DB_NAME", "DEMO_MODE", "ENGINE_WORKER",
                                        "GIT_COMMIT", "GLOBAL_AI_CAP", "CONTACT_EMAIL", "DATA_BUCKET",
                                        "UPLOADS_BUCKET", "GCP", "GEMINI_MODEL", "VERTEX_LOCATION", "PROJECT"]
        assert env_of(deploy)["CONTACT_EMAIL"] == SYNTHETIC["CONTACT_EMAIL"] and env_of(deploy)["GCP"] == "on"
        assert env_of(deploy)["UPLOADS_BUCKET"] == SYNTHETIC["UPLOADS_BUCKET"]
        # Phase 9 (09-24 checklist item 2): the model's keys reach the service; billing never does
        assert {k: env_of(deploy)[k] for k in ("GCP", "GEMINI_MODEL", "VERTEX_LOCATION", "PROJECT")} == {
            "GCP": "on", "GEMINI_MODEL": cfg["GEMINI_MODEL"], "VERTEX_LOCATION": cfg["VERTEX_LOCATION"],
            "PROJECT": PROJECT}
        for absent in ("BILLING_ACCOUNT", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE"):
            assert absent not in env_of(deploy)
    assert not [c for c in happy.calls if "--no-invoker-iam-check" in c]



def test_a_release_with_an_empty_contact_refuses_before_any_cloud_call(cfg, out_dir, fakes, capsys):
    # the About page and the unavailable screen show CONTACT_EMAIL (11-CONTEXT D-04): a release never ships without it
    cloud = Cloud()
    assert run_release({**cfg, "CONTACT_EMAIL": ""}, cloud, out_dir, fakes) == 1
    assert cloud.calls == [] and records(out_dir) == []
    assert "CONTACT_EMAIL" in capsys.readouterr().err

# --- Task 3: the early bench (D-15, D-19) ------------------------------------------------------------------------------

def run_early(cfg, cloud, out_dir, fakes, git=None, **kw) -> int:
    return release.early_bench(cfg, runner=cloud, git=git or FakeGit(), now=lambda: NOW, checks=lambda: "dv-1",
                               out_dir=out_dir, sleep=fakes.sleep, **kw)


def early_record(out_dir) -> dict:
    [path] = list(pathlib.Path(out_dir).glob("*.json"))
    assert path.name == f"{RID}-early-bench.json"  # only the evidence file, never a release record
    return json.loads(path.read_text())


def test_the_early_bench_runs_the_file_input_job_with_no_database_and_records_its_evidence(cfg, out_dir, fakes):
    cloud = Cloud()
    assert run_early(cfg, cloud, out_dir, fakes) == 0
    assert [verb(c) for c in cloud.calls] == [
        "artifacts docker images describe", "builds submit", "artifacts docker images describe",
        "run jobs deploy demo-early", "run jobs execute demo-early", "logging read"]
    [deploy] = [c for c in cloud.calls if c[1:4] == ["run", "jobs", "deploy"]]
    assert flag(deploy, "--image") == REF and flag(deploy, "--service-account") == RUN_SA
    assert (flag(deploy, "--cpu"), flag(deploy, "--memory"), flag(deploy, "--max-retries")) == ("1", "1Gi", "0")
    assert env_of(deploy) == {"ENGINE_WORKER": "on", "GIT_COMMIT": SHA}
    assert flag(deploy, "--command") == "python" and flag(deploy, "--args") == EARLY_ARGS
    text = json.dumps(cloud.calls)
    assert "CLOUD_SQL_INSTANCE" not in text and "DB_" not in text and "--set-secrets" not in text
    assert not cloud.did("secrets")
    ev = early_record(out_dir)
    assert ev["kind"] == "early_bench" and ev["status"] == "pass" and ev["id"] == f"{RID}-early-bench"
    assert ev["image_digest"] == DIGEST and ev["source_commit"] == SHA and ev["execution"] == EARLY_EXEC
    assert ev["input"] == {"path": "api/bench_input.json", "sha256_local": INPUT_SHA, "sha256_job": INPUT_SHA,
                           "match": True}
    lines = early_lines()
    assert ev["config"] == lines[0] and ev["startup"] == lines[2] and ev["runs"] == lines[3:6]
    assert ev["median"] == lines[6] and ev["info"] == lines[7:] and ev["job"] == "succeeded"


def test_an_existing_tag_is_reused_by_the_early_bench(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.image_exists = True
    assert run_early(cfg, cloud, out_dir, fakes) == 0
    assert not cloud.did("builds", "submit")


def test_a_median_at_or_over_the_budget_is_fail_evidence(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "jobs", "execute", having="demo-early"), boom("gcloud run jobs execute")))
    cloud.executions = [{"metadata": {"name": "demo-early-older", "creationTimestamp": "2026-10-15T00:00:00Z"}},
                        {"metadata": {"name": EARLY_EXEC, "creationTimestamp": "2026-10-16T14:05:07Z"}}]
    cloud.early_lines = early_lines(median="3.40")
    assert run_early(cfg, cloud, out_dir, fakes) == 1
    ev = early_record(out_dir)
    assert ev["status"] == "fail" and ev["job"] == "failed" and ev["execution"] == EARLY_EXEC
    assert "3.40" in ev["median"]


def test_no_median_line_is_error_evidence(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.early_lines = early_lines()[:3]
    assert run_early(cfg, cloud, out_dir, fakes) == 1
    ev = early_record(out_dir)
    assert ev["status"] == "error" and ev["median"] == manifest.NOT_READ_BACK
    assert fakes.sleeps == [10] * 5  # the log read is retried for a minute


def test_a_printed_input_hash_that_differs_is_error_evidence(cfg, out_dir, fakes):
    cloud = Cloud()
    cloud.early_lines = early_lines(sha="f" * 64)
    assert run_early(cfg, cloud, out_dir, fakes) == 1
    ev = early_record(out_dir)
    assert ev["status"] == "error" and ev["input"]["match"] is False and ev["input"]["sha256_job"] == "f" * 64


def test_a_median_printed_as_3_00_by_a_passing_job_is_pass(cfg, out_dir, fakes):
    """2.996 s prints 3.00 and passes (06-04): the job's exit decides, the rounded line is not compared to 3."""
    cloud = Cloud()
    cloud.early_lines = early_lines(median="3.00")
    assert run_early(cfg, cloud, out_dir, fakes) == 0
    assert early_record(out_dir)["status"] == "pass"


def test_the_early_bench_refuses_a_dirty_tree_before_any_command(cfg, out_dir, fakes):
    cloud = Cloud()
    assert run_early(cfg, cloud, out_dir, fakes, git=FakeGit(dirty="?? x\n")) == 1
    assert cloud.calls == [] and not pathlib.Path(out_dir).exists()


def test_the_release_manifest_names_the_early_bench_of_its_own_digest(cfg, out_dir, fakes):
    for rid, digest in (("20261015T000000Z-0123456789ab-early-bench", DIGEST),
                        ("20261016T000000Z-0123456789ab-early-bench", "sha256:" + "c" * 64)):
        manifest.write({"kind": "early_bench", "id": rid, "status": "pass", "image_digest": digest}, out_dir)
    assert run_release(cfg, Cloud(), out_dir, fakes) == 0
    assert manifest.latest(out_dir)["early_bench"] == "20261015T000000Z-0123456789ab-early-bench"


def test_without_early_bench_evidence_for_its_digest_the_manifest_says_null(cfg, out_dir, fakes):
    assert run_release(cfg, Cloud(), out_dir, fakes) == 0
    assert manifest.latest(out_dir)["early_bench"] is None


def test_the_early_bench_dry_run_needs_no_gcloud(tmp_path):
    env_file = synthetic_env.write(tmp_path / "env")
    p = subprocess.run(["bash", "deploy/bench-early.sh", "--dry-run", "--env-file", str(env_file)], cwd=ROOT,
                       env={**os.environ, "PATH": NO_GCLOUD_PATH}, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120)
    assert p.returncode == 0, p.stderr
    lines = [line for line in p.stdout.splitlines() if line.startswith("+ gcloud ")]
    assert any(line.startswith("+ gcloud run jobs deploy demo-early ") and EARLY_ARGS in line for line in lines)
    assert any(line.startswith("+ gcloud run jobs execute demo-early ") for line in lines)
    assert not any("demo-build" in line or "run deploy" in line for line in lines)
    assert "dry run: " in p.stdout and "evidence" in p.stdout


def test_bench_early_sh_is_an_executable_wrapper():
    path = ROOT / "deploy" / "bench-early.sh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    code = [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert code == ["set -euo pipefail", 'cd "$(git rev-parse --show-toplevel)"',
                    'exec .venv/bin/python -m deploy.release --early-bench "$@"']


# --- 261017-4oa: --status, read-only (the serving revision, the ACTIVE template, the newest records) -----------------

ACTIVE_ARGS = "--args=-m,api.sandbox,active"
FIRST_RID, FAILED_RID = "20260920T000000Z-999999999999", "20261010T000000Z-bbbbbbbbbbbb"
FIRST_REV = "demo-svc-00000-first"
GOOD_RID = "20261001T000000Z-aaaaaaaaaaaa"


def run_status(cfg, cloud, out_dir, fakes, capsys) -> tuple[int, list[str]]:
    rc = release.status(cfg, runner=cloud, out_dir=out_dir, sleep=fakes.sleep)
    return rc, [line for line in capsys.readouterr().out.splitlines() if not line.startswith("+ ")]


def files_under(path) -> dict[str, bytes]:
    p = pathlib.Path(path)
    return {f.name: f.read_bytes() for f in sorted(p.iterdir())} if p.exists() else {}


def status_records(out_dir) -> None:
    """An older known-good release (FIRST_REV), the known-good one serving now (OLD_REV, GOOD_TPL) and a newer
    failed release whose revision FAILED_REV is the latest ready one."""
    known_good(out_dir, rid=FIRST_RID, revision=FIRST_REV, new_template="tpl-first", activated_template="tpl-first",
               previous_revision=None)
    known_good(out_dir, revision=OLD_REV, previous_revision=FIRST_REV)
    manifest.write({"kind": "release", "id": FAILED_RID, "status": "failed_smoke", "revision": FAILED_REV,
                    "reconcile": None, "new_template": "tpl-failed"}, out_dir)


def test_status_prints_the_traffic_the_serving_revision_the_active_template_and_the_records(cfg, out_dir, fakes,
                                                                                           capsys):
    status_records(out_dir)
    before = files_under(out_dir)
    cloud = Cloud()
    cloud.service_before = svc_with([{"revisionName": OLD_REV, "percent": 100}], latest=FAILED_REV)
    rc, lines = run_status(cfg, cloud, out_dir, fakes, capsys)
    assert rc == 0
    assert lines == [
        f"traffic: 100% to {OLD_REV} [release {GOOD_RID}]",
        f"latest ready: {FAILED_REV} [release {FAILED_RID}]",
        f"serving: {OLD_REV} [release {GOOD_RID}]",
        f"ACTIVE: {GOOD_TPL}",
        f"newest record: {FAILED_RID} failed_smoke",
        f"known good: {GOOD_RID} (template {GOOD_TPL})",
        "check: serving is the known-good revision: yes",
        "check: ACTIVE is the known-good template: yes",
        f"check: the known-good release's previous revision: [release {FIRST_RID}]",
    ]
    assert files_under(out_dir) == before  # writes nothing
    [execute] = [c for c in cloud.calls if c[1:4] == ["run", "jobs", "execute"]]
    assert execute[4] == "demo-build" and ACTIVE_ARGS in execute
    assert "--run-id" not in " ".join(execute)  # settle never mistakes it for a build
    for verb_ in (("run", "deploy"), ("run", "services", "update-traffic"), ("run", "jobs", "deploy"),
                  ("secrets", "versions", "access"), ("run", "services", "update")):
        assert not cloud.did(*verb_), verb_
    text = "\n".join(lines)
    assert PASSCODE not in text and URL not in text and "http" not in text


def test_status_names_a_pending_record_and_the_newest_rollback(cfg, out_dir, fakes, capsys):
    status_records(out_dir)
    manifest.write({"kind": "rollback", "id": "20261011T000000Z-aaaaaaaaaaaa-rollback", "of": GOOD_RID,
                    "status": "failed_rollback"}, out_dir)
    pending = json.loads(pathlib.Path(out_dir, f"{FAILED_RID}.json").read_text())
    manifest.save(pathlib.Path(out_dir, f"{FAILED_RID}.json"), {**pending, "reconcile": "pending"})
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    rc, lines = run_status(cfg, cloud, out_dir, fakes, capsys)
    assert rc == 0
    assert f"newest record: {FAILED_RID} failed_smoke, reconcile: pending" in lines
    assert "newest rollback: 20261011T000000Z-aaaaaaaaaaaa-rollback failed_rollback" in lines
    assert f"known good: {GOOD_RID} (template {GOOD_TPL})" in lines  # a failed rollback undoes nothing


def _active_execute_fails(cloud):
    cloud.raise_on.append((is_call("run", "jobs", "execute", having=ACTIVE_ARGS), boom("gcloud run jobs execute", 1)))


def _active_unnamed(cloud):
    cloud.active_named = False


def _active_log_fails(cloud):
    cloud.raise_on.append((lambda a: a[1:3] == ["logging", "read"] and ACTIVE_EXEC in a[3], boom("gcloud logging")))


def _active_no_line(cloud):
    cloud.active_lines = []


@pytest.mark.parametrize("setup, why", [
    (_active_execute_fails, "the build job's execution failed, exit 1: gcloud itself may have failed, for example "
                            "when signed out, or the build job is missing or its image predates api.sandbox active"),
    (_active_unnamed, "the execution's name was not read back"),
    (_active_log_fails, "the job's log could not be read"),
    (_active_no_line, "the job's log holds no ACTIVE line"),
], ids=["execute fails", "no execution name", "log unreadable", "no ACTIVE line"])
def test_status_says_active_unknown_and_still_prints_the_rest(setup, why, cfg, out_dir, fakes, capsys):
    status_records(out_dir)
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    setup(cloud)
    rc, lines = run_status(cfg, cloud, out_dir, fakes, capsys)
    assert rc == 0
    assert f"ACTIVE: unknown ({why})" in lines
    assert "check: ACTIVE is the known-good template: unknown (ACTIVE could not be read)" in lines
    assert f"known good: {GOOD_RID} (template {GOOD_TPL})" in lines
    assert "check: serving is the known-good revision: yes" in lines
    assert lines[0] == f"traffic: 100% to {OLD_REV} [release {GOOD_RID}]"


@pytest.mark.parametrize("how", ["no service", "split"])
def test_status_before_the_first_release_and_with_split_traffic(how, cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    if how == "split":
        status_records(out_dir)
        cloud.service_before = svc_with(SPLIT, latest=FAILED_REV)
    rc, lines = run_status(cfg, cloud, out_dir, fakes, capsys)
    assert rc == 0
    if how == "no service":
        assert lines[0] == "service: not deployed yet"
        assert "newest record: none" in lines and "known good: none" in lines
        assert not [line for line in lines if line.startswith(("traffic:", "latest ready:", "serving:"))]
        assert "check: serving is the known-good revision: unknown (no known-good release)" in lines
        assert "check: the known-good release's previous revision: unknown (no known-good release)" in lines
    else:
        assert lines[:2] == [f"traffic: 50% to {OLD_REV} [release {GOOD_RID}]",
                             f"traffic: 50% to {FAILED_REV} [release {FAILED_RID}]"]
        assert "serving: none (the traffic is split or unreadable; a release would refuse)" in lines
        assert "check: serving is the known-good revision: unknown (no single serving revision)" in lines
    assert any(line.startswith("ACTIVE: ") for line in lines)


def test_status_says_an_unreadable_service_without_its_stderr(cfg, out_dir, fakes, capsys):
    cloud = Cloud()
    cloud.raise_on.append((is_call("run", "services", "describe"), boom("gcloud run services describe", 2,
                                                                        "ERROR: secret-ish detail")))
    rc, lines = run_status(cfg, cloud, out_dir, fakes, capsys)
    assert rc == 0 and lines[0] == "service: could not be read (gcloud run services describe exited 2)"
    assert "secret-ish" not in "\n".join(lines)
    assert "check: serving is the known-good revision: unknown (no known-good release)" in lines


@pytest.mark.parametrize("how", ["unreadable", "not found"])
def test_status_names_why_the_serving_check_is_unknown(how, cfg, out_dir, fakes, capsys):
    """A1: a describe that failed for a reason other than not found says the service could not be read; only a
    not-found answer says there is no service yet."""
    status_records(out_dir)
    cloud = Cloud()
    if how == "unreadable":
        cloud.raise_on.append((is_call("run", "services", "describe"), boom(
            "gcloud run services describe", 1,
            "ERROR: (gcloud.run.services.describe) There was a problem refreshing your current auth tokens")))
    rc, lines = run_status(cfg, cloud, out_dir, fakes, capsys)
    assert rc == 0
    if how == "unreadable":
        assert lines[0] == "service: could not be read (gcloud run services describe exited 1)"
        assert "check: serving is the known-good revision: unknown (the service could not be read)" in lines
        assert "auth tokens" not in "\n".join(lines)
    else:
        assert lines[0] == "service: not deployed yet"
        assert "check: serving is the known-good revision: unknown (no service yet)" in lines


NEVER_READY_LINE = "serving: none (no revision is ready; the next release is a first release)"
UNSERVABLE_LINE = "serving: none (the traffic is split or unreadable; a release would refuse)"
BROKEN = "demo-svc-00001-broken"
SERVABILITY = [
    ("nameless entry", {"status": {"latestCreatedRevisionName": BROKEN, "url": URL, "traffic": NAMELESS}}, True),
    ("no traffic", {"status": {"latestCreatedRevisionName": BROKEN, "url": URL, "traffic": []}}, True),
    ("an entry without a name", {"status": {"latestCreatedRevisionName": BROKEN, "url": URL,
                                            "traffic": [{"percent": 100}]}}, True),
    ("split with a ready revision", svc_with(SPLIT), False),
    ("no traffic with a ready revision", svc_with([]), False),
    ("no status block", {"metadata": {}}, False),
]


@pytest.mark.parametrize("svc, never", [c[1:] for c in SERVABILITY], ids=[c[0] for c in SERVABILITY])
def test_status_says_a_service_with_no_ready_revision_is_a_first_release(svc, never, cfg, out_dir, fakes, capsys):
    """A4: the serving line for a service that never had a ready revision comes from WR-07's predicate (the one
    revisions_before uses), so --status says the next release is a first release; every other unservable case keeps
    the refusal line."""
    def answering(cloud):
        original = cloud.respond
        cloud.respond = lambda a: svc if is_call("run", "services", "describe")(a) else original(a)
        return cloud

    rc, lines = run_status(cfg, answering(Cloud()), out_dir, fakes, capsys)
    assert rc == 0
    assert (NEVER_READY_LINE in lines) is never and (UNSERVABLE_LINE in lines) is not never
    assert release._never_ready(svc.get("status")) is never
    if never:
        assert release.previous_revision(cfg, answering(Cloud())) is None
    else:
        with pytest.raises(release.ReleaseError):
            release.previous_revision(cfg, answering(Cloud()))


def test_the_status_dry_run_runs_nothing(tmp_path, out_dir, capsys):
    env_file = synthetic_env.write(tmp_path / "env")
    dry = DryRunner()
    assert release.main(["--status", "--dry-run", "--env-file", str(env_file)], runner=dry, git=FakeGit(),
                        out_dir=out_dir) == 0
    assert [c for c in dry.calls if c[1:4] == ["run", "jobs", "execute"] and ACTIVE_ARGS in c]
    out = capsys.readouterr().out
    assert "ACTIVE: none" in out.splitlines() and "dry run: complete" in out
    assert not [c for c in dry.calls if c[1:3] == ["run", "deploy"] or c[1:4] == ["run", "services", "update-traffic"]]
    assert not pathlib.Path(out_dir).exists()


# --- 261017-4oa: the recovery drill (drill configurations, their records directory, forced failures) ----------------

def drill_main(argv, env_file, cloud, fakes, now=NOW) -> int:
    return release.main([*argv, "--env-file", str(env_file)], runner=cloud, git=FakeGit(), checks=lambda: "dv-1",
                        smoke=fakes.smoke, journey=fakes.journey, now=lambda: now, sleep=fakes.sleep)


@pytest.mark.parametrize("key", list(synthetic_env.DRILL))
def test_a_drill_configuration_marks_all_five_names_or_none(key, cfg, drill_cfg):
    assert release.is_drill(cfg) is False and release.is_drill(drill_cfg) is True
    with pytest.raises(release.ReleaseError) as e:
        release.is_drill({**cfg, key: synthetic_env.DRILL[key]})
    message = str(e.value)
    assert key in message and "all five" in message
    assert "demo" not in message  # keys only, never a value
    with pytest.raises(release.ReleaseError):
        release.is_drill({**drill_cfg, key: cfg[key]})  # four marked


MODES = [[], ["--rollback"], ["--early-bench"], ["--status"], ["--dry-run"], ["--dry-run", "--status"]]


@pytest.mark.parametrize("mode", MODES, ids=[" ".join(m) or "release" for m in MODES])
def test_a_mixed_configuration_is_refused_by_every_mode_before_any_command(mode, tmp_path, monkeypatch, capsys,
                                                                           fakes):
    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(release, "RELEASES", tmp_path / "live")
    monkeypatch.setattr(release, "RELEASES_DRILL", tmp_path / "drill")
    mixed = synthetic_env.write(tmp_path / "env", SERVICE=synthetic_env.DRILL["SERVICE"])
    cloud = Cloud()
    assert drill_main(mode, mixed, cloud, fakes) == 1
    assert cloud.calls == []
    err = capsys.readouterr().err
    assert "config error:" in err and "SERVICE" in err and "demo-svc-drill" not in err
    assert not (tmp_path / "live").exists() and not (tmp_path / "drill").exists()


def test_drill_records_go_to_their_own_directory_and_a_live_run_never_reads_them(tmp_path, monkeypatch, capsys,
                                                                                 fakes):
    live, drill = tmp_path / "live", tmp_path / "drill"
    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    monkeypatch.setattr(release, "RELEASES", live)
    monkeypatch.setattr(release, "RELEASES_DRILL", drill)
    live_env = synthetic_env.write(tmp_path / "env")
    drill_env = synthetic_env.write(tmp_path / "drill-env", **synthetic_env.DRILL)
    pending = interrupted_record(drill)
    drill_before = files_under(drill)

    cloud = Cloud()  # a live release: the pending drill record is never seen
    assert drill_main([], live_env, cloud, fakes) == 0
    assert not restores(cloud) and files_under(drill) == drill_before
    [rec] = records(live)
    assert rec["id"] == RID and rec["status"] == "known_good" and "drill" not in rec
    out = capsys.readouterr()
    assert f"commit the manifest deploy/releases/{RID}.json before the next release" in out.out
    assert "releases-drill" not in out.out + out.err and "drill configuration" not in out.out

    live_before = files_under(live)
    cloud = Cloud()  # a drill release reconciles the drill record, never the live one
    assert drill_main([], drill_env, cloud, Fakes()) == 1
    assert restores(cloud) and all("demo-build-drill" in cloud.calls[i] for i in restores(cloud))
    assert json.loads((drill / f"{pending['id']}.json").read_text())["status"] == "interrupted"
    assert files_under(live) == live_before
    out = capsys.readouterr()
    assert "drill configuration: records go to deploy/releases-drill/ (git-ignored)" in out.out
    assert (f"deploy/releases-drill/{pending['id']}.json is a drill record (git-ignored): commit nothing, then run "
            f"bash deploy/release.sh --env-file <the drill configuration> again") in out.err
    assert "deploy/releases/" not in out.out + out.err

    cloud = Cloud()
    assert drill_main([], drill_env, cloud, Fakes()) == 0
    rec = json.loads((drill / f"{RID}.json").read_text())
    assert rec["status"] == "known_good" and rec["drill"] == {"fail_after": None, "fail_restore_once": False,
                                                       "fail_settle_once": False, "unready_deploy": False}
    assert rec["resources"]["service"] == "demo-svc-drill" and rec["resources"]["db_name"] == "demo_db_drill"
    assert files_under(live) == live_before
    out = capsys.readouterr().out
    assert f"deploy/releases-drill/{RID}.json is a drill record (git-ignored): commit nothing" in out
    assert "deploy/releases/" not in out


def test_the_drill_records_and_the_drill_configuration_are_git_ignored():
    for path in ("deploy/releases-drill/x.json", "deploy/.env.drill"):
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", path]).returncode == 0, path
    assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "deploy/releases/x.json"]).returncode == 1


@pytest.mark.parametrize("flags", [{"drill_fail_after": "smoke"}, {"drill_restore_fails": True}],
                         ids=["fail after", "restore once"])
def test_the_drill_flags_need_a_drill_configuration(flags, cfg, out_dir, fakes, capsys, tmp_path, monkeypatch):
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes, **flags) == 1
    assert cloud.calls == [] and not pathlib.Path(out_dir).exists()
    assert "drill configuration" in capsys.readouterr().err

    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    live_env = synthetic_env.write(tmp_path / "env")
    argv = ["--env-file", str(live_env), "--drill-fail-after", "smoke"]
    if flags.get("drill_restore_fails"):
        argv.append("--drill-fail-restore-once")
    cloud = Cloud()
    assert release.main(argv, runner=cloud, git=FakeGit(), out_dir=out_dir, checks=lambda: "dv-1",
                        smoke=fakes.smoke, journey=fakes.journey, now=lambda: NOW, sleep=fakes.sleep) == 1
    assert cloud.calls == [] and not pathlib.Path(out_dir).exists()
    dry = DryRunner()
    assert release.main(["--dry-run", *argv], runner=dry, git=FakeGit(), out_dir=out_dir) == 1
    assert dry.calls == []
    assert "drill configuration" in capsys.readouterr().err


RELEASE_ONLY = ("--drill-fail-after, --drill-fail-restore-once, --drill-fail-settle-once and --drill-unready-deploy "
                "run only with a release")
WRONG_DRILL = [
    ("rollback", ["--rollback", "--drill-fail-after", "smoke"], RELEASE_ONLY),
    ("early bench", ["--early-bench", "--drill-fail-after", "smoke"], RELEASE_ONLY),
    ("status", ["--status", "--drill-fail-after", "smoke"], RELEASE_ONLY),
    ("status dry run", ["--status", "--dry-run", "--drill-fail-after", "smoke"], RELEASE_ONLY),
    ("restore once alone", ["--drill-fail-restore-once"], "--drill-fail-restore-once needs --drill-fail-after STEP"),
    ("unknown step", ["--drill-fail-after", "nope"], "invalid choice"),
    ("rollback unready", ["--rollback", "--drill-unready-deploy"], RELEASE_ONLY),
    ("early bench settle once", ["--early-bench", "--drill-fail-settle-once", "--drill-fail-after", "smoke"],
     RELEASE_ONLY),
    ("rollback traffic once on a release", ["--drill-fail-rollback-traffic-once"],
     "--drill-fail-rollback-traffic-once runs only with --rollback"),
    ("status rollback traffic once", ["--status", "--drill-fail-rollback-traffic-once"],
     "--drill-fail-rollback-traffic-once runs only with --rollback"),
    ("early bench rollback traffic once", ["--early-bench", "--drill-fail-rollback-traffic-once"],
     "--drill-fail-rollback-traffic-once runs only with --rollback"),
    ("settle once alone", ["--drill-fail-settle-once"], "--drill-fail-settle-once needs --drill-fail-after STEP"),
    ("settle once with restore once", ["--drill-fail-after", "smoke", "--drill-fail-settle-once",
                                       "--drill-fail-restore-once"],
     "--drill-fail-settle-once and --drill-fail-restore-once are not combined: with the settle failed, no restore "
     "runs"),
    ("unready with fail after", ["--drill-unready-deploy", "--drill-fail-after", "smoke"],
     "--drill-unready-deploy runs without the other drill flags: its failing service deploy is the forced failure"),
    ("rollback traffic once with fail after", ["--rollback", "--drill-fail-rollback-traffic-once",
                                               "--drill-fail-after", "smoke"], RELEASE_ONLY),
]


@pytest.mark.parametrize("argv, message", [c[1:] for c in WRONG_DRILL], ids=[c[0] for c in WRONG_DRILL])
def test_the_drill_flags_run_only_with_a_release(argv, message, tmp_path, monkeypatch, fakes, capsys):
    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    drill_env = synthetic_env.write(tmp_path / "drill-env", **synthetic_env.DRILL)
    cloud = Cloud()
    with pytest.raises(SystemExit) as e:
        drill_main(argv, drill_env, cloud, fakes)
    assert e.value.code == 2 and cloud.calls == []
    assert not list(tmp_path.glob("*releases*"))
    assert message in capsys.readouterr().err


LIVE_REFUSALS = [
    ("settle once", {"drill_fail_after": "smoke", "drill_settle_fails": True},
     ["--drill-fail-after", "smoke", "--drill-fail-settle-once"]),
    ("unready deploy", {"drill_unready": True}, ["--drill-unready-deploy"]),
    ("rollback traffic once", {"drill_traffic_fails": True}, ["--rollback", "--drill-fail-rollback-traffic-once"]),
]


@pytest.mark.parametrize("flags, argv", [c[1:] for c in LIVE_REFUSALS], ids=[c[0] for c in LIVE_REFUSALS])
def test_the_new_drill_switches_need_a_drill_configuration(flags, argv, cfg, tmp_path, monkeypatch, fakes, capsys):
    """Each new switch is refused for a live configuration before any command (before reconcile too): by the direct
    call, through main() and through main() --dry-run. A known-good release exists, so a rollback would otherwise
    run."""
    out_dir = tmp_path / "releases"
    if "drill_traffic_fails" in flags:
        known_good(out_dir, previous_template="tpl-older")
        before = files_under(out_dir)
        cloud = Cloud()
        assert release.rollback(cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep, **flags) == 1
        assert cloud.calls == [] and files_under(out_dir) == before
        assert "rollback refused: the drill flags need a drill configuration" in capsys.readouterr().err
    else:
        before = {}
        cloud = Cloud()
        assert run_release(cfg, cloud, out_dir, fakes, **flags) == 1
        assert cloud.calls == [] and not out_dir.exists()
        assert "release refused: the drill flags need a drill configuration" in capsys.readouterr().err

    monkeypatch.setattr(release, "LOCK", tmp_path / "release.lock")
    live_env = synthetic_env.write(tmp_path / "env")
    cloud = Cloud()
    assert release.main([*argv, "--env-file", str(live_env)], runner=cloud, git=FakeGit(), out_dir=out_dir,
                        checks=lambda: "dv-1", smoke=fakes.smoke, journey=fakes.journey, now=lambda: NOW,
                        sleep=fakes.sleep) == 1
    assert cloud.calls == [] and files_under(out_dir) == before
    dry = DryRunner()
    assert release.main(["--dry-run", *argv, "--env-file", str(live_env)], runner=dry, git=FakeGit(),
                        out_dir=out_dir) == 1
    assert dry.calls == [] and files_under(out_dir) == before
    assert "drill configuration" in capsys.readouterr().err


DRILL_CASES = [("build_execute", "failed_build", False), ("build_log", "failed_build", False),
               ("briefs", "failed_briefs", False), ("activate", "failed_activate", False),
               ("bench", "failed_bench", False),
               ("service_deploy", "failed_deploy", True), ("read_back", "failed_readback", True),
               ("access", "failed_readback", True), ("smoke", "failed_smoke", True),
               ("journey", "failed_journey", True)]


@pytest.mark.parametrize("step, status, deployed", DRILL_CASES, ids=[c[0] for c in DRILL_CASES])
def test_a_forced_failure_after_each_step_runs_the_real_recovery(step, status, deployed, drill_cfg, out_dir, fakes,
                                                                 capsys):
    assert [c[0] for c in DRILL_CASES] == list(release.DRILL_STEPS)
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_fail_after=step) == 1
    rec = only_record(out_dir)
    assert rec["status"] == status and rec["error"] == f"drill: forced failure after {step}"
    assert rec["reason"] == "drill"
    assert rec["progress"][-1] == {**rec["progress"][-1], "step": step, "state": "ok"}
    assert {p["state"] for p in rec["progress"]} == {"ok"}  # no later step started
    assert rec["drill"] == {"fail_after": step, "fail_restore_once": False, "fail_settle_once": False,
                            "unready_deploy": False}
    if step in ("build_execute", "build_log", "briefs"):  # Phase 9: before activate, nothing to restore
        assert settles(cloud) and not restores(cloud) and rec["recovery"]["restore"] == NOT_ACTIVATED
        assert not traffic(cloud) and not cloud.did("run", "deploy") and rec["reconcile"] is None
        return
    assert len(restores(cloud)) == 1 and settles(cloud) and max(settles(cloud)) < min(restores(cloud))
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert rec["recovery"]["settle_activate"] == {"execution": ACTIVATE_EXEC, "state": "finished"}
    if deployed:
        [move] = traffic(cloud)
        assert flag(move, "--to-revisions") == f"{OLD_REV}=100" and min(restores(cloud)) < cloud.calls.index(move)
        assert rec["recovery"]["traffic"] == f"{OLD_REV}=100"
    else:
        assert not traffic(cloud) and not cloud.did("run", "deploy") and "traffic" not in rec["recovery"]
    assert rec["reconcile"] is None
    if step == "smoke":
        assert fakes.smoke_calls and fakes.journey_calls == []
    err = capsys.readouterr().err
    assert f"release failed at {step} ({status}): drill: forced failure after {step}" in err


def test_a_drill_first_release_failed_after_its_activation_goes_back_to_no_template(drill_cfg, out_dir, fakes,
                                                                                   capsys):
    """WR-06 on the drill, Phase 9 shape: a first release whose activation retired none (`retired: none`) and then
    fails right after activate ends ok settles the build and activate executions by their recorded names, is undone
    by `restore <new> --first` (`undone: none ACTIVE`), moves no traffic and is not left pending."""
    manifest.write({"kind": "release", "id": "20261016T120000Z-0123456789ab", "status": "failed_build",
                    "previous_known_good": None, "previous_revision": None, "reconcile": None}, out_dir)
    cloud = Cloud()
    cloud.build_lines = ["previous ACTIVE: none", f"template {NEW} READY for dv-1"]
    cloud.restore_lines = list(FIRST_UNDONE)
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_fail_after="activate") == 1
    rec = records(out_dir)[-1]
    assert rec["id"] == RID and rec["status"] == "failed_activate"
    assert rec["error"] == "drill: forced failure after activate" and rec["reason"] == "drill"
    last = rec["progress"][-1]
    assert (last["step"], last["state"], last["execution"]) == ("activate", "ok", ACTIVATE_EXEC)
    assert rec["recovery"]["settle"] == {"execution": BUILD_EXEC, "state": "finished"}
    assert rec["recovery"]["settle_activate"] == {"execution": ACTIVATE_EXEC, "state": "finished"}
    assert not indexes(cloud, is_call("run", "jobs", "executions", "list"))
    [restore] = restores(cloud)
    assert FIRST_RESTORE_ARGS in cloud.calls[restore]
    assert rec["recovery"]["restore"] == "undone: none ACTIVE"
    assert "traffic" not in rec["recovery"] and rec["reconcile"] is None and not updates_traffic(cloud)
    err = capsys.readouterr().err
    assert "recovery: restore: undone: none ACTIVE" in err and "recovery is not finished" not in err


def test_the_build_execute_drill_on_an_inactive_build_restores_nothing(drill_cfg, out_dir, fakes):
    """The build_execute drill on a Phase 9 release: the build left <new> READY and its log was never read, so the
    recovery settles by the recorded name and restores nothing."""
    cloud = Cloud()
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_fail_after="build_execute") == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_build" and "activated_template" not in rec
    assert not [c for c in cloud.calls if c[1:3] == ["logging", "read"] and BUILD_EXEC in c[3]]
    assert rec["recovery"]["settle"] == {"execution": BUILD_EXEC, "state": "finished"}
    assert not restores(cloud) and rec["recovery"]["restore"] == NOT_ACTIVATED and rec["reconcile"] is None


def test_the_build_execute_drill_dry_run_prints_where_it_would_fail(tmp_path, out_dir, capsys):
    drill_env = synthetic_env.write(tmp_path / "drill-env", **synthetic_env.DRILL)
    dry = DryRunner()
    assert release.main(["--dry-run", "--env-file", str(drill_env), "--drill-fail-after", "build_execute"],
                        runner=dry, git=FakeGit(), out_dir=out_dir) == 0
    out = capsys.readouterr().out.splitlines()
    assert ("dry run: --drill-fail-after build_execute: a real run fails right after build_execute ends and runs its "
            "real recovery") in out
    assert any(line.startswith("dry run: complete") for line in out)
    assert not [c for c in dry.calls if any(x.startswith("--args=-m,api.sandbox,restore,") for x in c)]


def test_restore_once_leaves_the_record_pending_and_the_next_run_does_the_real_restore(drill_cfg, out_dir, fakes,
                                                                                      capsys):
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_fail_after="smoke", drill_restore_fails=True) == 1
    assert not restores(cloud)
    rec = only_record(out_dir)
    assert rec["recovery"]["restore"] == "failed: drill, not run"
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] == "pending"
    assert rec["drill"] == {"fail_after": "smoke", "fail_restore_once": True, "fail_settle_once": False,
                            "unready_deploy": False}
    hint = "bash deploy/release.sh --env-file <the drill configuration>"
    out, err = capsys.readouterr()  # every drill hint names --env-file: typed as printed, none starts a live release
    assert f"rerun {hint}" in err and (out + err).count("bash deploy/release.sh") == (out + err).count(hint)

    nxt = Cloud()  # the next plain drill run: the real restore and traffic move, no build
    nxt.service_before = OLD_SERVICE
    assert run_release(drill_cfg, nxt, out_dir, Fakes()) == 1
    out, err = capsys.readouterr()
    assert f"then run {hint} again" in err and (out + err).count("bash deploy/release.sh") == (out + err).count(hint)
    assert not nxt.did("artifacts") and not nxt.did("builds") and not nxt.did("run", "deploy")
    assert not indexes(nxt, is_call("run", "jobs", "execute", having=BUILD_ARGS))
    [restore], [move] = restores(nxt), traffic(nxt)
    assert settles(nxt)[0] < restore < nxt.calls.index(move)
    rec = only_record(out_dir)
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE" and rec["recovery"]["traffic"] == f"{OLD_REV}=100"
    assert rec["reconcile"] is None and rec["status"] == "interrupted"
    assert rec["status_before_reconcile"] == "failed_smoke"


def test_a_drill_dry_run_prints_where_it_would_fail_and_runs_no_recovery(tmp_path, out_dir, capsys):
    drill_env = synthetic_env.write(tmp_path / "drill-env", **synthetic_env.DRILL)
    dry = DryRunner()
    assert release.main(["--dry-run", "--env-file", str(drill_env), "--drill-fail-after", "smoke",
                         "--drill-fail-restore-once"], runner=dry, git=FakeGit(), out_dir=out_dir) == 0
    out = capsys.readouterr().out.splitlines()
    assert "drill configuration: records go to deploy/releases-drill/ (git-ignored)" in out
    assert ("dry run: --drill-fail-after smoke: a real run fails right after smoke ends and runs its real recovery"
            in out)
    assert ('dry run: --drill-fail-restore-once: a real run\'s recovery restore returns "failed: drill, not run" and '
            "leaves reconcile: pending") in out
    assert any(line.startswith("dry run: complete") for line in out)
    assert not [c for c in dry.calls if any(x.startswith("--args=-m,api.sandbox,restore,") for x in c)]
    assert not [c for c in dry.calls if "--to-revisions" in c]
    assert not pathlib.Path(out_dir).exists()


# --- 261017-lel: the drill switches for the rare recovery paths (WR-07, WR-09, WR-10, WR-11) ------------------------

LIVE_KEYS = ["CLOUD_SQL_INSTANCE", "DB_USER", "DB_NAME", "DEMO_MODE", "ENGINE_WORKER", "GIT_COMMIT", "GLOBAL_AI_CAP",
             "CONTACT_EMAIL", "DATA_BUCKET", "UPLOADS_BUCKET", "GCP", "GEMINI_MODEL", "VERTEX_LOCATION", "PROJECT"]
UNREADY_REV = "demo-svc-drill-00001-unready"


def test_the_unready_drill_value_stops_the_app_before_it_listens(tmp_path):
    """B2, the local proof that the unready value breaks only the app's start: api/config.py reads GLOBAL_AI_CAP with
    int() at import (`_env_or("GLOBAL_AI_CAP", ..., int)`); api/app.py imports config at module import; the image runs
    `exec uvicorn api.app:app`, and uvicorn imports the app (config.load) before startup binds the port. So uvicorn
    exits with a ValueError and never listens: Cloud Run's revision never becomes ready. deploy/envfile.py accepts
    digits only for GLOBAL_AI_CAP, so the value never comes from a configuration, and --set-env-vars replaces every
    variable on each deploy, so the next plain deploy carries the configured value again. No database is touched."""
    assert "uvicorn api.app:app" in (ROOT / "Dockerfile").read_text()
    # HQ_PASSCODE: a sound fake, so that after the product merge the drill value stays the only refusal
    env = {**os.environ, "GLOBAL_AI_CAP": release.DRILL_UNREADY_CAP, "DEMO_PASSCODE": "fake-passcode-7q",
           "HQ_PASSCODE": HQ_PASSCODE, "ENGINE_WORKER": "off"}
    p = subprocess.run([sys.executable, "-m", "uvicorn", "api.app:app", "--host", "127.0.0.1", "--port", "0",
                        "--no-proxy-headers"], cwd=ROOT, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    out = p.stdout + p.stderr
    assert p.returncode != 0
    assert "invalid literal for int()" in out and release.DRILL_UNREADY_CAP in out
    assert "Uvicorn running" not in out and "Application startup complete" not in out


def test_an_unready_drill_deploy_keeps_demo_mode_off_and_both_mounts(drill_cfg, out_dir, fakes):
    cloud = Cloud()
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_unready=True) == 1
    [deploy] = [c for c in cloud.calls if c[1:3] == ["run", "deploy"]]
    env = env_of(deploy)
    assert list(env) == LIVE_KEYS and env["DEMO_MODE"] == "off"
    assert {k: v for k, v in env.items() if k != "GLOBAL_AI_CAP"} == {
        k: v for k, v in release.service_env(drill_cfg, SHA).items() if k != "GLOBAL_AI_CAP"}
    assert env["GLOBAL_AI_CAP"] == release.DRILL_UNREADY_CAP
    assert flag(deploy, "--set-secrets") == "DEMO_PASSCODE=demo-pass:3,HQ_PASSCODE=demo-hq-pass:5"
    rec = only_record(out_dir)
    assert rec["secret_version"] == "3" and rec["hq_secret_version"] == "5"
    assert PASSCODE not in json.dumps(rec) and HQ_PASSCODE not in json.dumps(rec)


def test_an_unready_first_deploy_is_recovered_and_the_next_release_is_first(drill_cfg, out_dir, fakes, capsys):
    """B2 on a first release (DU, WR-07): the deploy whose revision never becomes ready fails the release at
    service_deploy; with no template ACTIVE before the build the restore does not run, no traffic moves and nothing is
    pending. The next plain release sees a service with no ready revision, so it is a first release and known good."""
    cloud = Cloud()
    cloud.build_lines = ["previous ACTIVE: none", f"template {NEW} ACTIVE for dv-1", "retired: none"]
    cloud.restore_lines = list(FIRST_UNDONE)
    cloud.created_revision = UNREADY_REV
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_unready=True) == 1
    [deploy] = [c for c in cloud.calls if c[1:3] == ["run", "deploy"]]
    assert list(env_of(deploy)) == LIVE_KEYS and env_of(deploy)["GLOBAL_AI_CAP"] == release.DRILL_UNREADY_CAP
    rec = only_record(out_dir)
    assert rec["status"] == "failed_deploy" and rec["drill"]["unready_deploy"] is True
    # Phase 9: the activation retired none, so its undo is `restore <new> --first` (09-24 checklist item 7)
    [restore] = restores(cloud)
    assert FIRST_RESTORE_ARGS in cloud.calls[restore]
    assert rec["recovery"]["restore"] == "undone: none ACTIVE"
    assert rec["recovery"]["traffic"] == "not moved: no previous revision (the first release)"
    assert rec["reconcile"] is None and not updates_traffic(cloud)
    assert release.known_good(out_dir) is None
    err = capsys.readouterr().err
    assert "release failed at service_deploy (failed_deploy): gcloud run deploy failed (exit 1)" in err
    assert "recovery is not finished" not in err

    later = NOW + dt.timedelta(minutes=1)
    newer = f"tpl-{SHA[:12]}-{later:%Y%m%d%H%M%S}"
    nxt = Cloud()
    nxt.service_before = {"status": {"latestCreatedRevisionName": UNREADY_REV, "url": URL, "traffic": NAMELESS}}
    nxt.build_lines = [f"previous ACTIVE: {NEW}", f"template {newer} ACTIVE for dv-1", f"retired: {NEW}"]
    assert release.release(drill_cfg, runner=nxt, git=FakeGit(), now=lambda: later, checks=lambda: "dv-1",
                           smoke=Fakes().smoke, journey=Fakes().journey, out_dir=out_dir, sleep=Fakes().sleep) == 0
    good = release.known_good(out_dir)
    assert good["new_template"] == newer and good["previous_revision"] is None
    assert good["template_compat"] == "first"
    [deploy] = [c for c in nxt.calls if c[1:3] == ["run", "deploy"]]
    assert env_of(deploy)["GLOBAL_AI_CAP"] == drill_cfg["GLOBAL_AI_CAP"] and len(to_latest(nxt)) == 1


def test_an_unready_deploy_after_a_failed_smoke_never_routes_the_failed_revision(drill_cfg, out_dir, fakes):
    """B2 after a failed-smoke release (DW, WR-09): the latest ready revision after the failed deploy is the earlier
    failed release's (same digest, a name captured before the deploy), so the fallback re-raises and never routes it;
    the recovery restores the captured template and sends traffic back to the serving revision."""
    good = known_good(out_dir)
    cloud = Cloud()
    cloud.service_before = {"status": {"latestReadyRevisionName": FAILED_REV, "latestCreatedRevisionName": FAILED_REV,
                                       "url": URL, "traffic": [{"revisionName": OLD_REV, "percent": 100}]}}
    cloud.created_revision = UNREADY_REV
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_unready=True) == 1
    rec = records(out_dir)[-1]
    assert rec["id"] == RID and rec["status"] == "failed_deploy" and to_latest(cloud) == []
    deployed = cloud.calls.index(call(cloud, "run", "deploy"))
    assert [i for i in indexes(cloud, is_call("run", "revisions", "describe")) if i > deployed]  # the fallback read
    assert {flag(c, "--to-revisions") for c in updates_traffic(cloud)} == {f"{OLD_REV}=100"}
    assert not [c for c in updates_traffic(cloud) if any(FAILED_REV in x or UNREADY_REV in x for x in c)]
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] is None
    [entry] = [p for p in rec["progress"] if p["step"] == "service_deploy"]
    assert "note" not in entry
    assert release.known_good(out_dir)["id"] == good["id"]


def test_a_drill_rollback_whose_traffic_fails_once_is_resumed_by_the_plain_rollback(drill_cfg, out_dir, fakes,
                                                                                     capsys):
    """B3 (WR-10 on the drill, DR): the first rollback runs the real restore and records its traffic move as
    `failed: drill, not run` without any update-traffic call; the plain --rollback resumes it, moves the traffic,
    reads it back and records rolled_back with `resumes`."""
    manifest.write({"kind": "release", "id": PREDECESSOR, "status": "known_good", "engine_version": "engine-2",
                    "source_commit": "b" * 40, "new_template": "tpl-older", "activated_template": "tpl-older",
                    "previous_revision": None}, out_dir)
    good = known_good(out_dir, previous_template="tpl-older")
    cloud = Cloud()
    cloud.restore_lines = [f"restored: tpl-older ACTIVE, {GOOD_TPL} RETIRED", "ACTIVE: tpl-older"]
    assert release.rollback(drill_cfg, runner=cloud, now=lambda: NOW, out_dir=out_dir, sleep=fakes.sleep,
                            drill_traffic_fails=True) == 1
    assert not updates_traffic(cloud) and len(restores_of(cloud, GOOD_TPL)) == 1
    [first] = [json.loads(p.read_text()) for p in pathlib.Path(out_dir).glob("*-rollback.json")]
    assert first["status"] == "failed_rollback" and first["results"]["restore"] == "restored: tpl-older ACTIVE"
    assert first["results"]["traffic"] == "failed: drill, not run"
    assert first["drill"] == {"fail_rollback_traffic_once": True}
    assert release.known_good(out_dir)["id"] == good["id"]
    out = capsys.readouterr().out
    assert "rollback: traffic: failed: drill, not run" in out and "commit nothing" in out

    again = Cloud()
    again.restore_lines = [f"restore: {GOOD_TPL} is not ACTIVE; nothing to do", "ACTIVE: tpl-older"]
    later = NOW + dt.timedelta(minutes=1)
    assert release.rollback(drill_cfg, runner=again, now=lambda: later, out_dir=out_dir, sleep=fakes.sleep) == 0
    [move] = traffic(again)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    assert again.calls.index(move) < max(indexes(again, is_call("run", "services", "describe")))  # read back
    second = json.loads((pathlib.Path(out_dir) / f"{later:%Y%m%dT%H%M%SZ}-{'a' * 12}-rollback.json").read_text())
    assert second["status"] == "rolled_back" and second["resumes"] == first["id"]
    assert second["drill"] == {"fail_rollback_traffic_once": False}
    assert f"rollback: resumes {first['id']}" in capsys.readouterr().out
    assert release.known_good(out_dir)["id"] == PREDECESSOR


def restores_of(cloud: Cloud, template: str) -> list[int]:
    return indexes(cloud, is_call("run", "jobs", "execute", having=f"--args=-m,api.sandbox,restore,{template}"))


def test_a_drill_settle_failure_still_moves_traffic_back_and_the_rerun_finishes(drill_cfg, out_dir, fakes, capsys):
    """B4 (WR-11 on the drill, DS): --drill-fail-settle-once records the settle as `unknown: drill, not run` without
    any executions call; the restore waits, traffic still goes back and is read back, and the record stays pending.
    The plain rerun settles for real, restores, moves the traffic and clears pending, building nothing."""
    cloud = Cloud()
    cloud.service_before = OLD_SERVICE
    assert run_release(drill_cfg, cloud, out_dir, fakes, drill_fail_after="smoke", drill_settle_fails=True) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_smoke" and rec["drill"]["fail_settle_once"] is True
    assert not settles(cloud) and not restores(cloud)
    assert rec["recovery"]["settle"] == "unknown: drill, not run"
    assert rec["recovery"]["restore"] == "not run: a build that may still be running could promote after it"
    [move] = traffic(cloud)
    assert flag(move, "--to-revisions") == f"{OLD_REV}=100"
    assert cloud.calls.index(move) < max(indexes(cloud, is_call("run", "services", "describe")))  # read back
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] == "pending"
    err = capsys.readouterr().err
    assert ("recovery is not finished, so the record stays reconcile: pending: rerun bash deploy/release.sh "
            "--env-file <the drill configuration> to retry it") in err

    nxt = Cloud()
    nxt.service_before = OLD_SERVICE
    assert run_release(drill_cfg, nxt, out_dir, Fakes()) == 1
    rec = only_record(out_dir)
    assert rec["recovery"]["settle"] == {"execution": BUILD_EXEC, "state": "finished"}
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE"
    assert rec["recovery"]["traffic"] == f"{OLD_REV}=100" and rec["reconcile"] is None
    assert not indexes(nxt, is_call("run", "jobs", "execute", having=BUILD_ARGS)) and not nxt.did("run", "deploy")


DRY_LINES = {
    "unready": ("dry run: --drill-unready-deploy: a real run's service deploy sets GLOBAL_AI_CAP to a value the app "
                "refuses at import, so its revision never becomes ready, the deploy exits non-zero and its real "
                "recovery runs"),
    "settle": ('dry run: --drill-fail-settle-once: a real run\'s recovery settle returns "unknown: drill, not run" '
               "without running, so the restore waits, traffic moves back and the record stays reconcile: pending"),
    "rollback": ('dry run: --drill-fail-rollback-traffic-once: a real rollback\'s traffic move returns "failed: '
                 'drill, not run" after its real restore, so it records failed_rollback and the next --rollback '
                 "resumes it"),
}


@pytest.mark.parametrize("which, argv", [
    ("unready", ["--drill-unready-deploy"]),
    ("settle", ["--drill-fail-after", "smoke", "--drill-fail-settle-once"]),
    ("rollback", ["--rollback", "--drill-fail-rollback-traffic-once"]),
])
def test_the_new_drill_dry_runs_print_their_lines_and_run_no_recovery(which, argv, tmp_path, out_dir, capsys):
    drill_env = synthetic_env.write(tmp_path / "drill-env", **synthetic_env.DRILL)
    dry = DryRunner()
    assert release.main(["--dry-run", "--env-file", str(drill_env), *argv], runner=dry, git=FakeGit(),
                        out_dir=out_dir) == 0
    out = capsys.readouterr().out
    assert DRY_LINES[which] in out.splitlines()
    assert any(line.startswith("dry run: complete") for line in out.splitlines())
    assert not [c for c in dry.calls if "--to-revisions" in c]
    if which == "rollback":
        assert [c for c in dry.calls if any(x.startswith("--args=-m,api.sandbox,restore,") for x in c)]
    else:
        assert not [c for c in dry.calls if any(x.startswith("--args=-m,api.sandbox,restore,") for x in c)]
        [deploy] = [c for c in dry.calls if c[1:3] == ["run", "deploy"]]
        want = release.DRILL_UNREADY_CAP if which == "unready" else synthetic_env.SYNTHETIC.get("GLOBAL_AI_CAP")
        assert list(env_of(deploy)) == LIVE_KEYS
        if which == "unready":
            assert env_of(deploy)["GLOBAL_AI_CAP"] == want and "GLOBAL_AI_CAP=drill-unready" in out
        else:
            assert env_of(deploy)["GLOBAL_AI_CAP"].isdigit()
    assert not pathlib.Path(out_dir).exists()


# --- quick 261017-v60: the public set goes to the build job through a Cloud Storage volume ---------------------------

PUBLIC_PARTS = {"sales": b"store,day,units\ns1,1,3\n", "stores": b"store,city\ns1,c1\n"}
DATA_VOLUME = "name=release-data,type=cloud-storage,bucket=demo-data,readonly=true"
DATA_MOUNT = "volume=release-data,mount-path=/mnt/release-data"


def public_set(base: pathlib.Path) -> tuple[pathlib.Path, str]:
    """A small public artifact set under `base / "public set"` (a space, as the user's folder holds one): two listed
    parts and the unlisted forecast input, with a manifest whose dataset_version recomputes. Returns (folder, dv)."""
    from data import versions

    folder = base / "public set"
    (folder / "parts").mkdir(parents=True)
    files = {}
    for name, body in PUBLIC_PARTS.items():
        (folder / "parts" / f"{name}.csv.gz").write_bytes(body)
        files[name] = {"path": f"parts/{name}.csv.gz", "rows": 1, "sha256": hashlib.sha256(body).hexdigest(),
                       "table": name, "columns": body.decode().splitlines()[0].split(","), "truth": False}
    (folder / "parts" / "forecast_input.csv.gz").write_bytes(b"store,day,units\n")
    m = {"kind": "public", "format": 1, "files": files, "settings": {},
         "forecast": {"input": {"path": "parts/forecast_input.csv.gz"}}}
    m["dataset_version"] = versions.digest("ds-", m)
    (folder / "manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    return folder, m["dataset_version"]


def public_cfg(tmp_path, folder) -> dict[str, str]:
    return envfile.load(synthetic_env.write(tmp_path / "public-env", RELEASE_DATA_DIR=str(folder)))


def public_build_lines(dv: str) -> list[str]:
    return ["previous ACTIVE: tpl-old", f"template {NEW} ACTIVE for {dv}", "retired: tpl-old"]


# api.bench's public mode on a public template (no SUS-19 story case): the 20 hardest cases selected before timing,
# each slowest of 3 under 3 s, then 10 resets under 2 s; its two gate lines
PUBLIC_TOP = "top 20: evaluate + fact checks, slowest of 3 = 1.23 s on flag-17 (budget 3 s); 1 not timed"
PUBLIC_RESETS = "resets: 10, slowest 640 ms (budget 2000 ms)"
PUBLIC_BENCH_LINES = [BENCH_LINES[0], BENCH_LINES[1],
                      "flag-03 s011 p0102 dests=4 lots=7 slowest=0.902 s median=0.811 s",
                      "flag-17 s042 p0311 dests=6 lots=9 slowest=1.234 s median=1.101 s",
                      "flag-21 s007 p0400 dests=0 lots=1 not timed: no eligible destination", PUBLIC_TOP, PUBLIC_RESETS]
PUBLIC_BENCH_ARGS = "--args=-m,api.bench,--top,20,--resets,10"


def public_cloud(dv: str) -> Cloud:
    cloud = Cloud()
    cloud.build_lines = public_build_lines(dv)
    cloud.bench_lines = list(PUBLIC_BENCH_LINES)
    return cloud


def storage_cps(folder: pathlib.Path, dv: str) -> list[list[str]]:
    """The two uploads: the listed parts to <dv>/parts/, then manifest.json last."""
    parts = [str(folder / f"parts/{name}.csv.gz") for name in PUBLIC_PARTS]
    return [release.gcloud("storage", "cp", *parts, f"gs://demo-data/{dv}/parts/", project=PROJECT),
            release.gcloud("storage", "cp", str(folder / "manifest.json"), f"gs://demo-data/{dv}/manifest.json",
                           project=PROJECT)]


def secret_step_calls(cloud: Cloud) -> int:
    """How many of the first calls are the secret step's (both pins and the HQ preflight), which runs first, before
    the data upload and any image, build, job or deploy command (06.1 D-64; HQ handoff item 3)."""
    n = next((i for i, c in enumerate(cloud.calls) if c[1] != "secrets"), len(cloud.calls))
    assert n > 0, "the secret step issued no call"
    return n


def test_a_public_set_is_uploaded_mounted_and_built_and_its_version_recorded(tmp_path, out_dir, fakes, capsys):
    folder, dv = public_set(tmp_path)
    cfg = public_cfg(tmp_path, folder)
    assert cfg["RELEASE_DATA_DIR"] == str(folder) and cfg["DATA_BUCKET"] == "demo-data"
    cloud = public_cloud(dv)
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    n = secret_step_calls(cloud)
    assert cloud.calls[n:n + 2] == storage_cps(folder, dv)
    assert [verb(c) for c in cloud.calls[n + 2:n + 5]] == ["artifacts docker images describe", "builds submit",
                                                           "artifacts docker images describe"]
    build = call(cloud, "run", "jobs", "deploy", name="demo-build")
    i = build.index("--set-env-vars")
    assert build[i + 2:i + 9] == ["--add-volume", DATA_VOLUME, "--add-volume-mount", DATA_MOUNT, "--command", "python",
                                  f"--args=-m,api.sandbox,build,/mnt/release-data/{dv},--no-activate"]
    build_exec = call(cloud, "run", "jobs", "execute", name="demo-build")
    assert f"--args=-m,api.sandbox,build,/mnt/release-data/{dv},--no-activate,--run-id,{NEW}" in build_exec
    bench = call(cloud, "run", "jobs", "deploy", name="demo-bench")
    assert "--add-volume" not in bench and "--add-volume-mount" not in bench
    assert bench[bench.index("--command"):] == ["--command", "python", PUBLIC_BENCH_ARGS, "--project", PROJECT]
    assert call(cloud, "run", "jobs", "execute", name="demo-bench") == [
        "gcloud", "run", "jobs", "execute", "demo-bench", "--region", REGION, "--wait", "--project", PROJECT,
        "--format=json"]
    assert "forecast_input" not in json.dumps(cloud.calls)

    rec = only_record(out_dir)
    assert rec["status"] == "known_good" and rec["dataset_version"] == dv
    assert rec["bench"]["result"] == "pass" and rec["bench"]["gate"] == PUBLIC_TOP
    assert rec["bench"]["resets"] == PUBLIC_RESETS  # beside the gate line; today's fields keep their names
    assert set(rec["bench"]) == {"execution", "result", "gate", "resets", "lines"}
    steps = [p["step"] for p in rec["progress"]]
    assert steps == ["secret", "data", "image", "previous_revision", "build_deploy", "build_execute", "build_log",
                     "briefs", "activate", "bench", "service_deploy", "read_back", "access", "smoke", "journey"]
    assert rec["progress"][1] == {"step": "data", "state": "ok", "dataset_version": dv, "parts": 2}
    assert str(folder) not in json.dumps(rec)
    out = capsys.readouterr()
    assert str(folder) not in out.out and str(folder) not in out.err
    assert "<RELEASE_DATA_DIR>/parts/" in out.out


def test_with_release_data_dir_empty_the_release_is_unchanged(cfg, out_dir, fakes):
    assert cfg["RELEASE_DATA_DIR"] == ""
    cloud = Cloud()
    assert run_release(cfg, cloud, out_dir, fakes) == 0
    assert not [c for c in cloud.calls if c[1] == "storage"]
    assert not [c for c in cloud.calls if "--add-volume" in c or "--add-volume-mount" in c]
    build = call(cloud, "run", "jobs", "deploy", name="demo-build")
    assert build == [
        "gcloud", "run", "jobs", "deploy", "demo-build", "--image", REF, "--region", REGION, "--service-account",
        DEPLOY_SA, "--cpu", "1", "--memory", "1Gi", "--max-retries", "0", "--task-timeout", "180m", "--set-env-vars",
        f"CLOUD_SQL_INSTANCE={PROJECT}:{REGION}:demo-sql,DB_USER=deploy-sa@x-demo-12.iam,"
        f"RUN_DB_USER=run-sa@x-demo-12.iam,DB_NAME=demo_db,GIT_COMMIT={SHA},"
        f"GCP=on,GEMINI_MODEL=gemini-3.5-flash,VERTEX_LOCATION=asia-southeast1,PROJECT={PROJECT}",
        "--command", "python", "--args=-m,api.sandbox,build,--no-activate", "--project", PROJECT]
    assert f"--args=-m,api.sandbox,build,--no-activate,--run-id,{NEW}" in call(cloud, "run", "jobs", "execute",
                                                                              name="demo-build")
    assert call(cloud, "run", "jobs", "deploy", name="demo-bench") == [
        "gcloud", "run", "jobs", "deploy", "demo-bench", "--image", REF, "--region", REGION, "--service-account",
        RUN_SA, "--cpu", "1", "--memory", "1Gi", "--max-retries", "0", "--task-timeout", "15m", "--set-env-vars",
        f"CLOUD_SQL_INSTANCE={PROJECT}:{REGION}:demo-sql,DB_USER=run-sa@x-demo-12.iam,DB_NAME=demo_db,"
        f"ENGINE_WORKER=on,GIT_COMMIT={SHA}",
        "--command", "python", "--args=-m,api.bench", "--project", PROJECT]
    assert call(cloud, "run", "jobs", "execute", name="demo-bench") == [
        "gcloud", "run", "jobs", "execute", "demo-bench", "--region", REGION, "--wait", "--project", PROJECT,
        "--format=json"]
    assert not [c for c in cloud.calls if "--top" in " ".join(c) or "--resets" in " ".join(c)]
    rec = only_record(out_dir)
    assert [p["step"] for p in rec["progress"]] == [
        "secret", "image", "previous_revision", "build_deploy", "build_execute", "build_log", "briefs", "activate",
        "bench", "service_deploy", "read_back", "access", "smoke", "journey"]
    assert rec["dataset_version"] == "dv-1" and rec["bench"]["gate"] == BENCH_LINES[-1]
    assert set(rec["bench"]) == {"execution", "result", "gate", "lines"}


def rewrite_manifest(folder: pathlib.Path, change, recompute: bool = True) -> None:
    """Change the set's manifest.json in place; with `recompute`, its dataset_version is taken again, so only the
    change itself can fail."""
    from data import versions

    path = folder / "manifest.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    change(m)
    if recompute:
        m["dataset_version"] = versions.digest("ds-", {k: v for k, v in m.items() if k != "dataset_version"})
    path.write_text(json.dumps(m, indent=1), encoding="utf-8")


def _set_path(path):
    return lambda folder: rewrite_manifest(folder, lambda m: m["files"]["sales"].update(path=path))


REFUSED_SETS = [
    ("changed part", lambda f: (f / "parts/sales.csv.gz").write_bytes(b"store,day,units\ns1,1,4\n"),
     ["parts/sales.csv.gz", "sha256"]),
    ("deleted part", lambda f: (f / "parts/sales.csv.gz").unlink(), ["parts/sales.csv.gz", "missing"]),
    ("parent path", _set_path("../outside.csv.gz"), ["../outside.csv.gz", "parts/"]),
    ("sub folder", _set_path("parts/sub/x.csv.gz"), ["parts/sub/x.csv.gz", "parts/"]),
    ("other folder", _set_path("data/x.csv.gz"), ["data/x.csv.gz", "parts/"]),
    ("private kind", lambda f: rewrite_manifest(f, lambda m: m.update(kind="private")), ["manifest.json", "public"]),
    ("edited settings", lambda f: rewrite_manifest(f, lambda m: m.update(settings={"x": 1}), recompute=False),
     ["manifest.json", "does not recompute"]),
    ("no manifest", lambda f: (f / "manifest.json").unlink(), ["manifest.json"]),
    ("no files", lambda f: rewrite_manifest(f, lambda m: m.update(files={})), ["manifest.json"]),
]


@pytest.mark.parametrize("case, spoil, names", REFUSED_SETS, ids=[c[0].replace(" ", "-") for c in REFUSED_SETS])
def test_a_bad_public_set_is_refused_before_any_cloud_command(case, spoil, names, tmp_path, out_dir, fakes, capsys):
    folder, _ = public_set(tmp_path)
    spoil(folder)
    cloud = Cloud()
    assert run_release(public_cfg(tmp_path, folder), cloud, out_dir, fakes) == 1
    assert cloud.calls == [] and not pathlib.Path(out_dir).exists()
    out = capsys.readouterr()
    assert out.err.startswith("release refused: "), out.err
    for name in names:
        assert name in out.err, name
    for text in (out.out, out.err):
        assert str(folder) not in text and str(tmp_path) not in text
        assert not re.search(r"[0-9a-f]{64}", text) and "demo-data" not in text


def test_the_cli_dry_run_refuses_a_bad_public_set_before_any_command(tmp_path):
    folder, _ = public_set(tmp_path)
    (folder / "parts/stores.csv.gz").unlink()
    env_file = synthetic_env.write(tmp_path / "env", RELEASE_DATA_DIR=str(folder))
    p = cli("--dry-run", "--env-file", str(env_file))
    assert p.returncode == 1
    assert not [line for line in p.stdout.splitlines() if line.startswith("+ gcloud")]
    assert "release refused: parts/stores.csv.gz: missing" in p.stderr


def test_a_failed_upload_records_failed_data_and_restores_nothing(tmp_path, out_dir, fakes, capsys):
    folder, dv = public_set(tmp_path)
    cfg = public_cfg(tmp_path, folder)
    cloud = Cloud()
    cloud.raise_on.append((lambda a: a[1:3] == ["storage", "cp"] and f"gs://demo-data/{dv}/manifest.json" in a,
                           boom("gcloud storage cp", 1)))
    assert run_release(cfg, cloud, out_dir, fakes) == 1
    n = secret_step_calls(cloud)
    assert cloud.calls[n:] == storage_cps(folder, dv)
    assert not cloud.did("builds") and not cloud.did("run", "jobs")
    rec = only_record(out_dir)
    assert rec["status"] == "failed_data" and rec["progress"][1:] == [{"step": "data", "state": "failed"}]
    assert (rec["progress"][0]["step"], rec["progress"][0]["state"]) == ("secret", "ok")
    assert rec["recovery"]["restore"].startswith("not needed") and rec["reconcile"] is None
    assert str(folder) not in json.dumps(rec) and str(folder) not in capsys.readouterr().err

    later = NOW + dt.timedelta(minutes=1)
    newer = f"tpl-{SHA[:12]}-{later:%Y%m%d%H%M%S}"
    again = public_cloud(dv)
    again.build_lines = [line.replace(NEW, newer) for line in public_build_lines(dv)]
    assert release.release(cfg, runner=again, git=FakeGit(), now=lambda: later, checks=lambda: "dv-1",
                           smoke=fakes.smoke, journey=fakes.journey, out_dir=out_dir, sleep=fakes.sleep) == 0
    n = secret_step_calls(again)
    assert again.calls[n:n + 2] == storage_cps(folder, dv)  # plain cp, the same destinations: identical objects


def test_a_killed_upload_leaves_no_manifest_and_the_next_run_reconciles_it(tmp_path, out_dir, fakes):
    folder, dv = public_set(tmp_path)
    cfg = public_cfg(tmp_path, folder)
    cloud = Cloud()
    cloud.raise_on.append((lambda a: a[1:3] == ["storage", "cp"] and f"gs://demo-data/{dv}/parts/" in a, Killed()))
    with pytest.raises(Killed):
        run_release(cfg, cloud, out_dir, fakes)
    n = secret_step_calls(cloud)
    assert cloud.calls[n:] == storage_cps(folder, dv)[:1]  # the manifest was never copied
    rec = only_record(out_dir)
    assert rec["status"] == "in_progress" and rec["progress"][1:] == [{"step": "data", "state": "started"}]
    assert (rec["progress"][0]["step"], rec["progress"][0]["state"]) == ("secret", "ok")
    nxt = Cloud()
    assert run_release(cfg, nxt, out_dir, Fakes()) == 1
    assert not [c for c in nxt.calls if c[1] == "storage" or c[1:3] == ["run", "jobs"]]
    rec = only_record(out_dir)
    assert rec["status"] == "interrupted" and rec["reconcile"] is None
    assert rec["recovery"]["restore"].startswith("not needed")


@pytest.mark.parametrize("lines", [
    ["previous ACTIVE: tpl-old", f"template {NEW} ACTIVE for ds-000000000000", "retired: tpl-old"],
    ["previous ACTIVE: tpl-old", "retired: tpl-old"],
], ids=["another-version", "no-active-for-line"])
def test_the_build_must_activate_the_uploaded_dataset_version(lines, tmp_path, out_dir, fakes):
    folder, dv = public_set(tmp_path)
    cloud = Cloud()
    cloud.build_lines = lines
    assert run_release(public_cfg(tmp_path, folder), cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_build" and rec["progress"][-1] == {"step": "build_log", "state": "failed"}
    if "ds-000000000000" in json.dumps(lines):  # another version: the dataset check names both
        assert dv in rec["error"] and "ds-000000000000" in rec["error"]
    else:  # no READY line at all: the template check fails first, naming the template it wanted
        assert "READY line" in rec["error"] and NEW in rec["error"]
    assert settles(cloud) and not restores(cloud)  # Phase 9: the build left <new> READY; nothing to restore
    assert rec["recovery"]["restore"] == NOT_ACTIVATED and rec["reconcile"] is None
    assert not cloud.did("run", "deploy")


def test_failed_data_is_a_recorded_status():
    assert release.STEP_STATUS["data"] == "failed_data" and "failed_data" in manifest.STATUSES
    assert set(release.STEP_STATUS.values()) <= set(manifest.STATUSES)


def test_the_cli_dry_run_of_a_public_set_uploads_mounts_and_names_the_dataset_check(tmp_path):
    folder, dv = public_set(tmp_path)
    env_file = synthetic_env.write(tmp_path / "env", RELEASE_DATA_DIR=str(folder))
    before = sorted(ROOT.glob("deploy/releases/*"))
    p = cli("--dry-run", "--env-file", str(env_file))
    assert p.returncode == 0, p.stderr
    lines = [line for line in p.stdout.splitlines() if line.startswith("+ gcloud ")]
    cps = [i for i, line in enumerate(lines) if line.startswith("+ gcloud storage cp ")]
    submit = next(i for i, line in enumerate(lines) if line.startswith("+ gcloud builds submit "))
    assert len(cps) == 2 and cps[1] < submit
    assert shlex.split(lines[cps[1]][2:])[3:5] == ["<RELEASE_DATA_DIR>/manifest.json",
                                                   f"gs://demo-data/{dv}/manifest.json"]
    assert shlex.split(lines[cps[0]][2:])[3:6] == ["<RELEASE_DATA_DIR>/parts/sales.csv.gz",
                                                   "<RELEASE_DATA_DIR>/parts/stores.csv.gz",
                                                   f"gs://demo-data/{dv}/parts/"]
    [build] = [line for line in lines if line.startswith("+ gcloud run jobs deploy demo-build ")]
    assert "--add-volume" in build and "mount-path=/mnt/release-data" in build
    [execute] = [line for line in lines if line.startswith("+ gcloud run jobs execute demo-build ")
                 and "api.sandbox,build" in line]
    assert f"/mnt/release-data/{dv},--no-activate,--run-id" in execute
    [bench] = [line for line in lines if line.startswith("+ gcloud run jobs deploy demo-bench ")]
    assert f" {PUBLIC_BENCH_ARGS} " in bench
    assert [line for line in p.stdout.splitlines() if line.startswith("dry run: ") and "ACTIVE for" in line]
    for text in (p.stdout, p.stderr):
        assert "forecast_input" not in text and str(folder) not in text
    assert sorted(ROOT.glob("deploy/releases/*")) == before


@pytest.mark.parametrize("how", ["over budget", "nothing to time", "no top line", "no resets line",
                                 "story lines only"])
def test_a_public_release_gate_needs_exit_0_and_both_gate_lines(how, tmp_path, out_dir, fakes):
    """A public template has no SUS-19 story case, so the release gate is api.bench's public mode: exit 0 with its
    `top 20: ... slowest of 3` line and its `resets:` line. Exit 1 (over a budget) or 2 (nothing to time), or a
    missing gate line, fails at the bench with today's recovery."""
    folder, dv = public_set(tmp_path)
    cloud = public_cloud(dv)
    if how == "no top line":
        cloud.bench_lines = [x for x in PUBLIC_BENCH_LINES if x != PUBLIC_TOP]
    elif how == "no resets line":
        cloud.bench_lines = [x for x in PUBLIC_BENCH_LINES if x != PUBLIC_RESETS]
    elif how == "story lines only":
        cloud.bench_lines = list(BENCH_LINES)  # the story bench's line is not this gate
    else:
        code = 1 if how == "over budget" else 2
        cloud.raise_on.append((is_call("run", "jobs", "execute", "demo-bench"), boom("gcloud run jobs", code)))
    assert run_release(public_cfg(tmp_path, folder), cloud, out_dir, fakes) == 1
    rec = only_record(out_dir)
    assert rec["status"] == "failed_bench" and rec["progress"][-1] == {"step": "bench", "state": "failed"}
    assert restores(cloud) and settles(cloud)[0] < restores(cloud)[0]
    assert rec["recovery"]["restore"] == "restored: tpl-old ACTIVE" and not cloud.did("run", "deploy")
    if how in ("no top line", "story lines only"):
        assert "top 20" in rec["error"]
    elif how == "no resets line":
        assert "resets" in rec["error"]
