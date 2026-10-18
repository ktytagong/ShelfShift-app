"""Local evaluation review commands. Storage is lazy and replaceable with a fake."""
from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
import re
import sys

from api import db
from . import store

FINGERPRINT_PATH = Path(__file__).with_name("FINGERPRINT")
_cached = None


def _client():
    global _cached
    if _cached is None:
        from google.cloud import storage
        _cached = storage.Client()
    return _cached


def _blob(path):
    bucket, separator, key = path.removeprefix("gs://").partition("/")
    if not bucket or not separator or not key:
        raise ValueError("Review storage path needs an object name")
    return _client().bucket(bucket).blob(key)


def _read(path):
    try:
        if path.startswith("gs://"):
            return _blob(path).download_as_bytes().decode("utf-8-sig")
        return Path(path).read_text(encoding="utf-8-sig")
    except Exception:
        raise ValueError("Review file could not be read") from None


def _write(path, text):
    try:
        if path.startswith("gs://"):
            _blob(path).upload_from_string(text.encode("utf-8"), content_type="text/csv; charset=utf-8")
        else:
            Path(path).write_text(text, encoding="utf-8")
    except Exception:
        raise ValueError("Review file could not be written") from None


def export_main(argv=None):
    parser = argparse.ArgumentParser(prog="agent.eval export")
    parser.add_argument("--eval-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with db.tx() as conn:
        evaluation = store._eval(conn, args.eval_id, lock=True)
        rows = store.checklist_rows(conn, args.eval_id)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["eval_id", args.eval_id, "fingerprint", evaluation["fingerprint"]])
    writer.writerow(store.CHECKLIST_COLUMNS)
    writer.writerows([r[k] for k in store.CHECKLIST_COLUMNS] for r in rows)
    _write(args.out, stream.getvalue())
    print("Review file exported")
    return 0


def import_main(argv=None):
    parser = argparse.ArgumentParser(prog="agent.eval import")
    parser.add_argument("--eval-id", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args(argv)
    reader = csv.reader(io.StringIO(_read(args.file)), strict=True)
    try:
        meta, columns = next(reader), next(reader)
        if len(meta) != 4 or meta[::2] != ["eval_id", "fingerprint"] or meta[1] != args.eval_id:
            raise ValueError("Row 1: eval_id or metadata does not match this evaluation")
        if columns != list(store.CHECKLIST_COLUMNS):
            raise ValueError("Row 2: checklist columns do not match")
        rows = []
        for number, values in enumerate(reader, 3):
            if len(values) != len(columns):
                raise ValueError(f"Row {number}: wrong number of columns")
            rows.append(dict(zip(columns, values)))
    except (StopIteration, csv.Error):
        raise ValueError(f"Row {reader.line_num or 1}: invalid review file") from None
    with db.tx() as conn:
        store.import_review(conn, args.eval_id, meta[3], rows, args.reviewer)
    print("Review imported")
    return 0


def publish_main(argv=None):
    parser = argparse.ArgumentParser(prog="agent.eval publish")
    parser.add_argument("--eval-id", required=True)
    args = parser.parse_args(argv)
    try:
        fingerprint = FINGERPRINT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        fingerprint = None
    if fingerprint is not None and not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        fingerprint = None
    blocked = None
    with db.tx() as conn:
        try:
            store.publish(conn, args.eval_id, fingerprint)
        except store.Blocked as exc:
            blocked = str(exc)  # catch here so BLOCKED and its reason commit
    if blocked:
        print(blocked, file=sys.stderr)
        return 2
    print("Evaluation published")
    return 0


def _committed_fingerprint(root, value):
    """The working file must equal this checkout's committed approval lock."""
    import subprocess
    repository = Path(__file__).resolve().parents[2]
    try:
        relative = (Path(root).resolve() / "FINGERPRINT").relative_to(repository).as_posix()
        result = subprocess.run(["git", "show", "HEAD:" + relative], cwd=repository,
                                text=True, capture_output=True, check=False)
    except (OSError, ValueError):
        return False
    return result.returncode == 0 and result.stdout.strip() == value


def preflight_main(argv=None):
    """Read local inputs and template metadata; never generate or approve anything."""
    import json
    from api import config, sandbox
    from engine.model import ENGINE_VERSION
    from . import fingerprint, refs, runner
    from .steps import REAL_STEPS
    parser = argparse.ArgumentParser(prog="agent.eval preflight")
    parser.add_argument("--template", required=True)
    parser.add_argument("--eval-dir", type=Path, default=fingerprint.HERE)
    args = parser.parse_args(argv)
    failures, documents = [], {}
    for name, shape in (("scenarios", list), ("references", dict), ("rubric", list)):
        try:
            value = json.loads((args.eval_dir / (name + ".json")).read_text(encoding="utf-8"))
            if not isinstance(value, shape) or not value:
                raise ValueError("Invalid evaluation input shape")
            documents[name] = value
        except (OSError, ValueError):
            failures.append("Evaluation inputs: " + name + " must be readable and valid")

    scenarios, references = documents.get("scenarios", []), documents.get("references", {})
    try:
        if not scenarios or any(not isinstance(s, dict) or not isinstance(s.get("id"), str) or
                not isinstance(s.get("selector"), dict) or not isinstance(s.get("steps"), list) or
                not s["steps"] or any(not isinstance(step, dict) or not isinstance(step.get("action"), str)
                                      for step in s["steps"]) for s in scenarios):
            raise ValueError("Invalid scenarios")
        actions = {step["action"].split(":", 1)[1] for scenario in scenarios
                   for step in scenario["steps"] if step["action"].startswith("ai:")}
        if any(not callable(REAL_STEPS.get(action)) for action in actions):
            failures.append("Some real service steps are unbound")
    except ValueError:
        failures.append("Evaluation inputs: scenarios need selectors and named steps")
        scenarios = []

    valid_refs = (isinstance(references.get("dataset_version"), str) and
                  isinstance(references.get("engine_version"), str) and
                  isinstance(references.get("scenarios"), dict) and
                  all(isinstance(row, dict) for row in references["scenarios"].values()))
    if not valid_refs:
        failures.append("Evaluation inputs: reference versions and scenarios are required")
    else:
        if any("pending" in row for row in references["scenarios"].values()):
            failures.append("References contain PENDING upstream inputs")
        if {scenario["id"] for scenario in scenarios} != set(references["scenarios"]):
            failures.append("Evaluation inputs: scenarios and references must match")
    try:
        locked = (args.eval_dir / "FINGERPRINT").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", locked):
            raise ValueError("Invalid lock")
        if valid_refs and len(documents) == 3:
            computed = fingerprint.compute(args.eval_dir, references["dataset_version"], references["engine_version"])
            if locked != computed:
                failures.append("FINGERPRINT must match all evaluation inputs")
        if not _committed_fingerprint(args.eval_dir, locked):
            failures.append("FINGERPRINT must be committed")
    except (OSError, ValueError):
        failures.append("A valid committed FINGERPRINT is required")

    with db.tx() as conn:
        template = sandbox.active_template(conn) if args.template == "active" else db.one(conn,
            "SELECT * FROM demo_runs WHERE run_id=:r AND kind='TEMPLATE'", r=args.template)
        if template is None:
            failures.append("Template does not exist")
        else:
            if valid_refs and (template["dataset_version"] != references["dataset_version"] or
                               references["engine_version"] != ENGINE_VERSION):
                failures.append("Template and reference dataset/engine versions differ")
            for scenario in scenarios:
                try:
                    refs.select_case(conn, template, scenario["selector"])
                except ValueError:
                    failures.append(scenario["id"] + ": selector does not resolve")
    try:
        runner.default_budget()
    except (KeyError, ValueError, TypeError):
        failures.append("Evaluation budget caps/prices must be positive and finite")
    for key in ("EVAL_BUCKET", "GEMINI_MODEL"):
        if not (config.env(key) or "").strip():
            failures.append(key + " must be set")
    for failure in failures:
        print(failure)
    if failures:
        return 1
    print("PREFLIGHT-OK")
    return 0
