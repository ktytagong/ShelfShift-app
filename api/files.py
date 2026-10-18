"""Files attached to answers (and, from 07-13, to field reports): checked by content, stored privately under a
content-addressed name, read back only through the API (07-CONTEXT D-14, D-20; research F-5, F-6, Q4; 04-API-CONTRACT
"Answer photos and file access", `GET /api/signals/{id}/file`).

- **Type** (`sniff`): PNG, JPEG or PDF by the file's first bytes (`MAGIC`), never its name or the client's
  Content-Type; anything else is 415 bad_file. Larger than `uploads.max_bytes` (a setting, 5 MB) is 413 too_large.
- **Storage** (`save`): the object name is `<run_id>/<sha256>.<ext>`: never the client's file name, so no name can
  reach a path, and a retried upload of the same bytes names the same object. With `config.UPLOADS_BUCKET` set the
  object goes to that private bucket through `_client()` (google-cloud-storage, imported lazily inside it, as
  api/db.py imports the Cloud SQL connector; tests replace the seam with a fake), skipped when it is already there;
  otherwise to `config.UPLOAD_DIR` (tests, local runs), written atomically. Cloud Run's own disk is in memory, so the
  local branch is never the cloud's (research A2).
- **Preloaded material** (`asset_uri`): a packaged file in `config.DEMO_SIGNALS_DIR`, named `asset://<name>`; the
  template needs no bucket write (research Q4, F-16).
- **Status** (`storage_status`): "ok" or "degraded", never an exception, for readyz's `storage` key (its wiring is
  handed off: readyz is build/deploy's, research F-24).
- **Reading** (`read`): `gs://` from the bucket, `file://` from the upload folder, `asset://<name>` from the packaged
  read-only folder `config.DEMO_SIGNALS_DIR` (preloaded material; a name with a path separator or ".." is refused).
  The route (api/app.py) looks the row up by the session's workspace first (`lookup`: another workspace's file is
  404), then checks the seat (`may_read`: HQ, or a manager of a branch party to the item, else 403), and serves the
  bytes with the stored type, `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store` and
  `Content-Disposition` (`attachment` for a PDF): never a redirect, never a bucket URL (`headers`).

Threats this module carries (07-07 threat register, audited by /gsd-secure-phase): T-07-19 another workspace or an
unrelated seat reading a file (`lookup` by the session's run_id, then `may_read`), T-07-20 a disguised or polyglot
file (magic bytes; served with nosniff; never HTML or SVG), T-07-21 path traversal (content names; asset names
refused), T-07-22 an oversized file (the size check here, the route's body bound), T-07-23 a public URL (proxied
reads only). Which spend a file makes is the caller's (D-20: an answer photo spends only the cross-workspace cap).
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import tempfile
from dataclasses import dataclass

from . import config, db
from .errors import ApiError

MAGIC = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"%PDF-": "application/pdf"}
EXT = {"image/png": "png", "image/jpeg": "jpg", "application/pdf": "pdf"}
BAD_FILE = "Only PNG, JPEG or PDF files can be attached"
NO_FILE = "No such file"
FORBIDDEN = "You can't open this file"
ASSET = "asset://"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # a server-made run id: one safe path segment


@dataclass(frozen=True)
class Stored:
    """A stored file: where it is (never shown to a client), its sniffed type, its sha256 and its size in bytes."""
    uri: str
    mime: str
    sha256: str
    size: int


_cached = None  # the storage client, made on first use (one per process)


def _client():
    """The Cloud Storage client: the module-level seam tests replace with a fake. The library is imported here, on
    the bucket branch only, so a local run and the tests never load it (research F-6)."""
    global _cached
    if _cached is None:
        from google.cloud import storage  # lazily: only the bucket branch needs the client
        _cached = storage.Client()
    return _cached


def too_large() -> ApiError:
    """413 too_large naming the setting's limit in MB ("Files can be at most 5 MB")."""
    return ApiError(413, "too_large", f"Files can be at most {config.UPLOAD_MAX_BYTES / (1024 * 1024):g} MB")


def sniff(data: bytes) -> str:
    """The file's type from its first bytes; 413 too_large over `uploads.max_bytes`, 415 bad_file unless a PNG, JPEG
    or PDF signature matches."""
    if len(data) > config.UPLOAD_MAX_BYTES:
        raise too_large()
    for magic, mime in MAGIC.items():
        if data.startswith(magic):
            return mime
    raise ApiError(415, "bad_file", BAD_FILE)


def _write_atomically(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def save(run_id: str, data: bytes) -> Stored:
    """Store the file for the workspace under `<run_id>/<sha256>.<ext>` (bucket or local folder) and return it."""
    mime = sniff(data)
    if not RUN_ID.match(run_id or ""):
        raise ValueError(f"not a run id: {run_id!r}")
    sha = hashlib.sha256(data).hexdigest()
    name = f"{run_id}/{sha}.{EXT[mime]}"
    bucket = config.UPLOADS_BUCKET
    if bucket:
        blob = _client().bucket(bucket).blob(name)
        if not blob.exists():  # the same bytes are already there under the same name
            blob.upload_from_string(data, content_type=mime)
        return Stored(f"gs://{bucket}/{name}", mime, sha, len(data))
    path = pathlib.Path(config.UPLOAD_DIR).resolve() / run_id / f"{sha}.{EXT[mime]}"
    if not path.is_file():
        _write_atomically(path, data)
    return Stored(f"file://{path}", mime, sha, len(data))


def _asset_path(name: str) -> pathlib.Path | None:
    """The packaged file `name` directly in DEMO_SIGNALS_DIR, or None for a name that could leave it."""
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith(".") or "\x00" in name:
        return None
    folder = pathlib.Path(config.DEMO_SIGNALS_DIR).resolve()
    path = (folder / name).resolve()
    return path if path.parent == folder else None


def asset_uri(name: str) -> str:
    """`asset://<name>` for a packaged file directly in DEMO_SIGNALS_DIR (preloaded material); ValueError for a name
    that could leave the folder or a file that is not there (a programming error, never a client's input)."""
    path = _asset_path(name)
    if path is None or not path.is_file():
        raise ValueError(f"not a packaged file in DEMO_SIGNALS_DIR: {name!r}")
    return f"{ASSET}{name}"


def storage_status() -> str:
    """"ok" when files can be stored (the bucket answers a one-object listing through `_client`, or the upload folder
    can be created and written), else "degraded"; never raises (readyz's `storage` key, handed off)."""
    try:
        bucket = config.UPLOADS_BUCKET
        if bucket:
            next(iter(_client().list_blobs(bucket, max_results=1)), None)
            return "ok"
        folder = pathlib.Path(config.UPLOAD_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        return "ok" if folder.is_dir() and os.access(folder, os.W_OK) else "degraded"
    except Exception:  # noqa: BLE001 - a status, never an error (the cause is not shown to a visitor)
        return "degraded"


def read(uri: str) -> bytes:
    """The stored bytes behind a uri the server wrote (gs://, file:// under UPLOAD_DIR, asset://<name>); 404
    not_found for anything else or a file that is gone."""
    try:
        if uri.startswith("gs://"):
            bucket, _, name = uri[len("gs://"):].partition("/")
            return _client().bucket(bucket).blob(name).download_as_bytes()
        if uri.startswith("file://"):
            path = pathlib.Path(uri[len("file://"):]).resolve()
            if not path.is_relative_to(pathlib.Path(config.UPLOAD_DIR).resolve()):
                raise ApiError(404, "not_found", NO_FILE)
            return path.read_bytes()
        if uri.startswith(ASSET):
            path = _asset_path(uri[len(ASSET):])
            if path is None:
                raise ApiError(404, "not_found", NO_FILE)
            return path.read_bytes()
    except FileNotFoundError:
        raise ApiError(404, "not_found", NO_FILE) from None
    raise ApiError(404, "not_found", NO_FILE)


def headers(mime: str, name: str) -> dict:
    """The download's headers: nosniff, private no-store, inline for an image and attachment for a PDF."""
    disposition = "attachment" if mime == "application/pdf" else "inline"
    return {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Disposition": f'{disposition}; filename="{name}.{EXT[mime]}"'}


def lookup(conn, run: dict, signal_id: str) -> dict:
    """The signal row holding a file, found by the session's workspace only, with the request it answers and the
    linked case (None when there is none): {signal_id, uri, mime, signal, request, case}. The case is the request's
    for an answer, else the signal's own `case_id` (a field report linked to a case has no request), so `may_read`
    sees the case's source and destination as `reports._party` does (07-14 review V2). Another workspace's signal, an
    unknown one or one with no file (or a type outside MAGIC) is 404 not_found."""
    s = db.one(conn, "SELECT * FROM signals WHERE run_id = :r AND signal_id = :s", r=run["run_id"], s=signal_id)
    if s is None or not s["file_uri"] or s["file_type"] not in EXT:
        raise ApiError(404, "not_found", NO_FILE)
    q = db.one(conn, "SELECT * FROM evidence_requests WHERE run_id = :r AND request_id = :q", r=run["run_id"],
               q=s["request_id"]) if s["request_id"] else None
    case_id = q["case_id"] if q else s.get("case_id")
    c = db.one(conn, "SELECT * FROM cases WHERE run_id = :r AND case_id = :c", r=run["run_id"],
               c=case_id) if case_id else None
    return {"signal_id": s["signal_id"], "uri": s["file_uri"], "mime": s["file_type"], "signal": s, "request": q,
            "case": c}


def may_read(actor: dict, signal: dict, request: dict | None = None, case: dict | None = None) -> None:
    """The seat rule for a file inside its workspace (07-CONTEXT D-20; research A6): HQ always; a manager when their
    branch is the signal's reporter or subject branch, the request's addressed branch, or the linked case's source or
    its plan's destination; anyone else is 403 forbidden. The seat comes from the session row (FLOW-10)."""
    if actor.get("role") == "hq":
        return
    parties = {signal.get("reporter_branch"), signal.get("branch")}
    if request is not None:
        parties.add(request.get("to_branch"))
    if case is not None:
        parties |= {case.get("source_branch"), case.get("plan_dest")}
    parties.discard(None)
    if actor.get("role") == "manager" and actor.get("branch") in parties:
        return
    raise ApiError(403, "forbidden", FORBIDDEN)
