"""D-14: hash every frozen input, attachment and data/engine version.

Only 10-19 writes the release FINGERPRINT, after the user's approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def compute(eval_dir, dataset_version, engine_version):
    root = Path(eval_dir)
    doc = {name: json.loads((root / (name + ".json")).read_text(encoding="utf-8"))
           for name in ("scenarios", "rubric", "references")}
    attachments = root / "attachments"
    doc["attachments"] = {p.relative_to(attachments).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(attachments.rglob("*")) if p.is_file()}
    doc.update(dataset_version=dataset_version, engine_version=engine_version)
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent.eval fingerprint")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--eval-dir", type=Path, default=HERE)
    args = parser.parse_args(argv)
    lock = args.eval_dir / "FINGERPRINT"
    if args.check and not lock.is_file():
        print("References are not locked yet")
        return 1
    try:
        references = json.loads((args.eval_dir / "references.json").read_text(encoding="utf-8"))
        if any("pending" in row for row in references["scenarios"].values()):
            raise ValueError("PENDING references cannot be locked or verified")
        fingerprint = compute(args.eval_dir, references["dataset_version"], references["engine_version"])
    except (OSError, KeyError, ValueError) as exc:
        print("Reference fingerprint could not be computed: " + str(exc))
        return 1
    if args.write:
        lock.write_text(fingerprint + "\n", encoding="utf-8")
        print("FINGERPRINT written")
        return 0
    if lock.read_text(encoding="utf-8").strip() != fingerprint:
        print("Reference fingerprint mismatch")
        return 1
    print("FINGERPRINT-OK")
    return 0
