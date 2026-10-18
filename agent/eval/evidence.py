"""Private evidence objects, local or behind a lazy replaceable storage client."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import tempfile

_cached = None


def _client():
    global _cached
    if _cached is None:
        from google.cloud import storage
        _cached = storage.Client()
    return _cached


class Writer:
    def __init__(self, target):
        self.target = str(target).rstrip("/")
        if not self.target:
            raise ValueError("An evidence destination is required")
        if self.target.startswith("gs://"):
            self.bucket, separator, self.prefix = self.target[5:].partition("/")
            if not self.bucket or not separator or not self.prefix:
                raise ValueError("Evidence storage requires a private prefix")

    def _write(self, key, value, content_type):
        try:
            if self.target.startswith("gs://"):
                _client().bucket(self.bucket).blob(self.prefix + "/" + key).upload_from_string(
                    value, content_type=content_type)
            else:
                path = Path(self.target) / key
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".part-")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as out:
                        out.write(value)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
        except Exception:
            # Resource identifiers and raw evidence never enter logs/public errors.
            raise ValueError("Evaluation evidence could not be written") from None
        return self.target + "/" + key

    def trace(self, scenario, run_no, trace):
        if not re.fullmatch(r"s[0-9]{2}", scenario) or type(run_no) is not int or not 1 <= run_no <= 3:
            raise ValueError("Invalid evidence attempt key")
        return self._write(f"{scenario}/{run_no}.json", json.dumps(
            trace, ensure_ascii=False, sort_keys=True, default=str), "application/json")

    def checklist(self, eval_id, fingerprint, rows):
        from .store import CHECKLIST_COLUMNS
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["eval_id", eval_id, "fingerprint", fingerprint])
        writer.writerow(CHECKLIST_COLUMNS)
        writer.writerows([r[k] for k in CHECKLIST_COLUMNS] for r in rows)
        return self._write("checklist.csv", stream.getvalue(), "text/csv; charset=utf-8")
