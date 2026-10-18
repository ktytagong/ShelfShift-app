"""Release records and manifests: what a release actually did and deployed (06-CONTEXT D-09, D-10, D-11; 06-REVIEWS
R2-8, R2-11).

A release opens its record, deploy/releases/<YYYYMMDDTHHMMSSZ>-<sha12>.json, with status `in_progress` before its
first cloud call (`open_record`), and rewrites it atomically (`save`: a temporary file in the same directory, then
os.replace) when each step starts and when it ends, so the file on disk always shows how far the release got. The
finished record is the release manifest. A rollback writes <UTC>-<sha12>-rollback.json and the early bench
<UTC>-<sha12>-early-bench.json (`write`, which refuses to overwrite).

Release record fields (every value read back from Cloud Run, Cloud Build or the job logs, never copied from the
configuration; a value that did not come back is NOT_READ_BACK, never guessed; D-10):
    kind ("release"), id, status, time, project, region, source_commit (the full SHA), image (IMAGE@sha256:...),
    image_digest, revision, url, previous_revision, min_instances and max_instances (as read back), annotations,
    access ("public" or "invoker check disabled"), secret_version and hq_secret_version (the pinned version numbers of
    PASSCODE_SECRET and HQ_PASSCODE_SECRET, never a value), dataset_version,
    reference_files (sha256 of data/reference/seed.json, forecast.json and truth.json in the archived source),
    new_template (the run id the release chose), activated_template, previous_template (the template this activation
    actually retired: the build's `retired:` line), active_before_build (its `previous ACTIVE:` line), model
    ({configured, endpoint, used}), resources (the job, service, instance, database, repository, account and secret
    names), build_execution, bench, smoke, journey, engine_version, template_compat (first, compatible or breaking),
    previous_known_good, early_bench, progress, reconcile, recovery, error, reason, drill (drill records only:
    {fail_after, fail_restore_once}).

`progress` is a list of {step, state, ...} in step order: secret (both pins and the HQ preflight), data
(RELEASE_DATA_DIR releases only; {dataset_version, parts}), image, previous_revision, build_deploy, build_execute,
build_log, bench, service_deploy, read_back, access, smoke, journey. A step's state is `started` (the
build execute and the service deploy: `requested`, written before the call is issued), then `ok` or `failed`.
Recovery is decided from this list, never from which step raised (R2-1, R2-8).

Statuses: in_progress, known_good, failed_data, failed_image, failed_secret, failed_service_lookup, failed_build_deploy,
failed_build, failed_bench, failed_deploy, failed_readback, failed_smoke, failed_journey, interrupted, rolled_back,
failed_rollback; early bench evidence: pass, fail, error.

The records hold the project and resource names. They stay in this private repository and are excluded from the
public one (D-11), from images and from Cloud Build uploads. A drill configuration's records go to the git-ignored
deploy/releases-drill/, never deploy/releases/ (deploy/README.md step 11). No secret value is ever written: `withhold` replaces a
value with "(withheld)" before anything reaches the disk.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile
from typing import Callable, Iterable

NOT_READ_BACK = "not read back"
WITHHELD = "(withheld)"
STATUSES = ("in_progress", "known_good", "failed_data", "failed_image", "failed_secret", "failed_service_lookup",
            "failed_build_deploy", "failed_build", "failed_bench", "failed_deploy", "failed_readback",
            "failed_smoke", "failed_journey", "interrupted", "rolled_back", "failed_rollback")


def release_id(now: dt.datetime, sha: str, suffix: str = "") -> str:
    """<YYYYMMDDTHHMMSSZ>-<sha12>, plus -<suffix> when given; `now` is written in UTC whatever its zone."""
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{sha[:12]}" + (f"-{suffix}" if suffix else "")


def path_for(record: dict, out_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(out_dir) / f"{record['id']}.json"


def _text(record: dict, withhold: Iterable[str]) -> str:
    text = json.dumps(record, sort_keys=True, indent=1) + "\n"
    for value in withhold:
        if value:
            text = text.replace(value, WITHHELD)
    return text


def write(record: dict, out_dir: pathlib.Path | str, *, withhold: Iterable[str] = ()) -> pathlib.Path:
    """Write a new record as <id>.json; refuses (FileExistsError) to overwrite one."""
    path = path_for(record, out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        f.write(_text(record, withhold))
    return path


def open_record(record: dict, out_dir: pathlib.Path | str, *, withhold: Iterable[str] = ()) -> pathlib.Path:
    """Create the release record with status in_progress, before the release's first cloud call (R2-8)."""
    record["status"] = "in_progress"
    return write(record, out_dir, withhold=withhold)


def save(path: pathlib.Path | str, record: dict, *, withhold: Iterable[str] = ()) -> None:
    """Rewrite a record atomically: a temporary file in the same directory, then os.replace."""
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_text(record, withhold))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def records(out_dir: pathlib.Path | str, *, kind: str | None = "release") -> list[dict]:
    """Every record of `kind` (all kinds when None), newest first by filename."""
    out = []
    for path in sorted(pathlib.Path(out_dir).glob("*.json"), reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and (kind is None or record.get("kind") == kind):
            out.append(record)
    return out


def latest(out_dir: pathlib.Path | str, *, kind: str = "release", status: str | None = None,
           where: Callable[[dict], bool] | None = None) -> dict | None:
    """The newest record of `kind` (by filename), with `status` and matching `where` when given; an in_progress
    record is found like any other."""
    for record in records(out_dir, kind=kind):
        if (status is None or record.get("status") == status) and (where is None or where(record)):
            return record
    return None
